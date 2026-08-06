from omwb.convert import page_rel_path
from omwb.pipeline import approx_tokens, chunk_markdown


def test_page_rel_path():
    assert page_rel_path("https://a.com/") == "index"
    assert page_rel_path("https://a.com/docs/intro/") == "docs/intro"
    assert page_rel_path("https://a.com/docs/x/index.html") == "docs/x"
    assert page_rel_path("https://a.com/docs/x.html") == "docs/x"
    assert page_rel_path("https://a.com/docs/a.md") == "docs/a"
    assert page_rel_path("https://a.com/docs/CON") == "docs/_CON"  # Windows 保留名
    assert page_rel_path("https://a.com/a/b?hl=zh") == "a/b"


def test_approx_tokens():
    assert approx_tokens("你好世界") == 4
    assert approx_tokens("hello world") >= 2
    assert approx_tokens("") == 0


def test_chunk_markdown_basic():
    md = "# 标题一\n\n第一段内容。\n\n## 小节\n\n第二段。\n\n# 标题二\n\n第三段。\n"
    chunks = chunk_markdown(md, max_tokens=20, overlap=10)
    assert len(chunks) >= 2
    for c in chunks:
        assert c["tokens"] <= 30  # max_tokens + overlap 预算
    # 标题路径元数据
    c2 = [c for c in chunks if "第二段" in c["text"]]
    assert c2 and c2[0]["heading_path"] == ["标题一", "小节"]


def test_chunk_markdown_oversized():
    md = "# 大标题\n\n" + "很长的一段文字。" * 200 + "\n"
    chunks = chunk_markdown(md, max_tokens=100, overlap=10)
    assert len(chunks) > 1
    for c in chunks:
        assert c["tokens"] <= 110  # 允许 overlap 少量上浮


def test_chunk_markdown_overlap():
    md = "# A\n\n" + "段落甲内容。" * 50 + "\n\n# B\n\n" + "段落乙内容。" * 50 + "\n"
    chunks = chunk_markdown(md, max_tokens=100, overlap=20)
    assert len(chunks) >= 2
    joined = [c["text"] for c in chunks]
    # 相邻 chunk 应有重叠文本
    overlaps = sum(1 for i in range(len(chunks) - 1) if joined[i][-20:] in joined[i + 1])
    assert overlaps >= 1


def test_chunk_markdown_heading_context():
    md = "# 章\n\n## 节\n\n### 小节\n\n内容。\n"
    chunks = chunk_markdown(md, max_tokens=1000)
    assert len(chunks) == 1
    assert chunks[0]["heading_path"] == ["章", "节", "小节"]
