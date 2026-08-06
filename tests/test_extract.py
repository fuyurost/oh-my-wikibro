from omwb.adapters import get_adapter
from omwb.extract import extract_page, looks_like_spa

DOC_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Requests Quickstart - Requests 2.32 docs</title>
  <meta name="generator" content="Sphinx 8.1">
  <meta name="description" content="快速上手 Requests">
</head>
<body>
<nav class="sidebar">
  <a href="/index">首页</a><a href="/search">搜索</a>
</nav>
<header>Site header banner</header>
<main role="main">
  <h1 id="quickstart">Quickstart¶</h1>
  <p>Let's <strong>start</strong> with a simple <code>GET</code> request.</p>
  <h2 id="make">Make a Request</h2>
  <p>Making a request with Requests is easy.</p>
  <pre><code class="python">>>> import requests
>>> r = requests.get('https://httpbin.org/get')
>>> r.status_code
200</code></pre>
  <table>
    <tr><th>方法</th><th>说明</th></tr>
    <tr><td>GET</td><td>读取</td></tr>
  </table>
  <ul>
    <li>第一项</li>
    <li>第二项</li>
  </ul>
</main>
<footer>footer noise</footer>
</body>
</html>
"""


def test_extract_markdown_structure():
    res = extract_page(DOC_HTML, "https://docs.example.com/quickstart", get_adapter("sphinx"))
    assert "首页" not in res.markdown          # 导航噪音被剔除
    assert "footer noise" not in res.markdown
    assert "# Quickstart" in res.markdown      # h1 + 锚点符号剥离
    assert "## Make a Request" in res.markdown
    assert "**start**" in res.markdown         # 加粗
    assert "`GET`" in res.markdown             # 行内代码
    assert "```" in res.markdown               # 代码块围栏
    assert ">>> import requests" in res.markdown
    assert "| 方法 |" in res.markdown          # 表格
    assert "- 第一项" in res.markdown


def test_extract_title_and_meta():
    res = extract_page(DOC_HTML, "https://docs.example.com/quickstart", get_adapter("sphinx"))
    assert res.title == "Requests Quickstart"
    assert res.meta["description"] == "快速上手 Requests"
    assert res.toc[0] == {"level": 1, "title": "Quickstart", "anchor": ""}
    assert res.toc[1]["level"] == 2


def test_extract_title_entities_and_clean():
    from omwb.extract import _clean_title
    html = DOC_HTML.replace(
        "<title>Requests Quickstart - Requests 2.32 docs</title>",
        "<title>1. 课前甜点 &#8212; Python 3.14.6 文档</title>",
    )
    res = extract_page(html, "https://docs.example.com/appetite", get_adapter("sphinx"))
    assert res.title == "1. 课前甜点"           # 实体解码 + 站点名剥离
    # 双内容标题不误伤
    assert _clean_title("安装 | 配置") == "安装 | 配置"
    assert _clean_title("A | B") == "A | B"


def test_fallback_text_extraction():
    """纯链接列表页:trafilatura 判空后回退到正文节点文本。"""
    html = """<html><body><div class="document">
    <h1>Links</h1>
    <ul>
      <li><a href="/a">Article A about requests</a></li>
      <li><a href="/b">Talk B about APIs</a></li>
      <li><a href="/c">Post C</a></li>
    </ul>
    </div></body></html>"""
    res = extract_page(html, "https://docs.example.com/links", get_adapter("sphinx"))
    assert "Article A about requests" in res.markdown


def test_looks_like_spa():
    assert looks_like_spa('<div id="root"></div><script src="app.js"></script>')
    assert not looks_like_spa("<article><p>content</p></article>")
