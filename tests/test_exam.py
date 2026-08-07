"""exam 包测试:LLM 生成解析(含 ```json 容错)、批注式审查、变体写回、无配置报错、校验失败。

LLM 调用全部通过 httpx.MockTransport 注入,不发真实请求。
"""

import json
from pathlib import Path

import httpx
import pytest

from omwb.exam.generate import load_corpus, generate_exam
from omwb.exam.llm import LLMConfig, LLMConfigError, LLMError, extract_json, resolve_config
from omwb.exam.review import review_code
from omwb.exam.variants import add_variants


def _config(handler) -> LLMConfig:
    return LLMConfig(base_url="https://llm.test", model="mock-model",
                     transport=httpx.MockTransport(handler))


def _completion(content: str) -> httpx.Response:
    """模拟 OpenAI chat/completions 响应。"""
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _mock_exam_raw() -> dict:
    return {
        "title": "用列表实现 LRU 缓存",
        "task": "实现一个 LRU 缓存类,支持 get/put 操作",
        "output_spec": "get 命中返回 value,未命中返回 -1;put 超过容量淘汰最久未使用项;容量 ≤ 1000",
        "constraints": ["仅用 Python 标准库", "get/put 平均复杂度 O(1)"],
        "hints": ["参考语料中关于哈希表与链表的章节"],
        "level": "medium",
        "source_annotations": [
            {"material_index": 1,
             "anchor": "列表(list)是 Python 内置序列",
             "type": "concept_explain",
             "explanation": "列表(list)是 Python 内置的序列容器,支持按位置存取,像宿舍楼里编了号的储物柜:"
                            "想取第 3 个柜子里的东西直接走过去就行,不用从第 1 个开始数。"
                            "本题要求 get/put 平均 O(1),正好对应哈希表这个'按钥匙直接开门'的结构。"},
            {"material_index": 1,
             "anchor": "链表由节点串成,适合频繁插入删除",
             "type": "pitfall",
             "explanation": "链表每个节点只记住下一个节点,插入删除只需改邻居指针,像火车车厢摘挂:"
                            "中间插一节车厢不用挪动其他车厢。但链表按位置访问是 O(n),"
                            "用列表实现 LRU 时若在头部插入导致整体后移,每次 put 都会变成 O(n),"
                            "这是常见的复杂度陷阱,需用双向链表+哈希表组合规避。"},
            {"material_index": 2,
             "anchor": "归并排序时间复杂度 O(n log n)",
             "type": "task_link",
             "explanation": "O(n log n) 意味着数据量翻倍时操作数只多一个 log 因子,像按姓氏字典序分批整理:"
                            "先把大名单对半拆到最小,再两两合并,10 万条数据约 170 万次比较,一秒内完成。"
                            "本题虽然没有排序要求,但理解复杂度记号有助于判断你的 LRU 实现是否达标。"},
        ],
    }


def _mock_variants_raw(count: int = 3) -> list[dict]:
    dims = ["约束变化", "输入规模", "性能要求"]
    return [
        {"dimension": dims[i % len(dims)], "title": f"变体{i + 1}",
         "task": f"变体任务 {i + 1}", "output_spec": f"变体规格 {i + 1}",
         "constraints": ["同原题"], "hints": []}
        for i in range(count)
    ]


@pytest.fixture
def site_dir(tmp_path: Path) -> Path:
    """构造带 corpus.jsonl 的站点目录。"""
    d = tmp_path / "demo-site"
    d.mkdir()
    chunks = [
        {"site": "demo-site", "url": "https://demo.dev/list", "path": "list",
         "title": "列表与链表", "seq": 0, "heading_path": ["列表", "链表"],
         "tokens": 60, "text": "列表(list)是 Python 内置序列。链表由节点串成,适合频繁插入删除。"},
        {"site": "demo-site", "url": "https://demo.dev/sort", "path": "sort",
         "title": "排序算法", "seq": 0, "heading_path": ["排序"],
         "tokens": 60, "text": "归并排序时间复杂度 O(n log n),比列表(list)暴力排序更快。"},
    ]
    (d / "corpus.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks) + "\n", encoding="utf-8")
    return d


# ---------- 配置 ----------

def test_resolve_config_missing(monkeypatch):
    for k in ("OMWB_LLM_BASE_URL", "OMWB_LLM_API_KEY", "OMWB_LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(LLMConfigError, match="未配置 LLM"):
        resolve_config()


def test_resolve_config_env_and_cli_priority(monkeypatch):
    monkeypatch.setenv("OMWB_LLM_BASE_URL", "https://env.test")
    monkeypatch.setenv("OMWB_LLM_API_KEY", "k-env")
    monkeypatch.setenv("OMWB_LLM_MODEL", "m-env")
    cfg = resolve_config()
    assert (cfg.base_url, cfg.model, cfg.api_key) == ("https://env.test", "m-env", "k-env")
    # 显式参数优先于环境变量
    cfg2 = resolve_config(base_url="https://cli.test", model="m-cli")
    assert (cfg2.base_url, cfg2.model) == ("https://cli.test", "m-cli")
    assert cfg2.api_key == "k-env"


def test_endpoint_v1_suffix():
    assert _config(lambda r: r).endpoint == "https://llm.test/v1/chat/completions"
    assert LLMConfig(base_url="https://x/v1", model="m").endpoint == "https://x/v1/chat/completions"


# ---------- JSON 容错解析 ----------

def test_extract_json_fence_and_full():
    assert extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert extract_json("```\n{\"a\": 2}\n```") == {"a": 2}
    assert extract_json("{\"a\": 3}") == {"a": 3}
    assert extract_json("前文说明\n```json\n[1, 2]\n```\n后文") == [1, 2]
    with pytest.raises(LLMError):
        extract_json("这不是 JSON")
    with pytest.raises(LLMError):
        extract_json("")


# ---------- 试题生成 ----------

def test_generate_parses_fenced_json_and_variants(site_dir: Path, tmp_path: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        body = json.loads(request.content)
        if "变体" in body["messages"][-1]["content"]:
            return _completion(json.dumps(_mock_variants_raw(2)))
        return _completion("```json\n" + json.dumps(_mock_exam_raw()) + "\n```")

    exam = generate_exam("demo-site", "list", out=site_dir.parent, variants=2,
                         config=_config(handler))
    assert exam["task"].startswith("实现一个 LRU 缓存类")
    assert exam["output_spec"]
    # 原文锚定讲解:source_chunk 原样绑定语料文本,anchor 能定位回原文,explanation 展开(≥80 字)
    sa = exam["source_annotations"]
    assert len(sa) == 2
    assert sa[0]["source_chunk"] == "列表(list)是 Python 内置序列。链表由节点串成,适合频繁插入删除。"
    ann0 = sa[0]["annotations"][0]
    assert ann0["anchor"] in sa[0]["source_chunk"]
    assert ann0["type"] in ("concept_explain", "plain_words", "example", "pitfall", "task_link")
    assert len(ann0["explanation"]) >= 80
    assert len(sa[0]["annotations"]) == 2  # 同一段可挂多条批注
    assert len(exam["variants"]) == 2
    assert exam["source_chunks"] and exam["source_chunks"][0]["url"].startswith("https://")
    path = site_dir / "exam" / f"exam-{exam['id']}.json"
    assert path.is_file()
    assert (site_dir / "exam" / f"exam-{exam['id']}.md").is_file()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["task"] == exam["task"]
    assert len(saved["variants"]) == 2
    assert calls == ["/v1/chat/completions", "/v1/chat/completions"]


def test_generate_variants_disabled_single_call(site_dir: Path, tmp_path: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _completion(json.dumps(_mock_exam_raw()))

    exam = generate_exam("demo-site", "list", out=site_dir.parent, variants=0,
                         config=_config(handler))
    assert exam["variants"] == []
    assert len(calls) == 1


def test_generate_missing_task_fails(site_dir: Path, tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raw = _mock_exam_raw()
        del raw["task"]
        return _completion(json.dumps(raw))

    with pytest.raises(LLMError, match="试题生成失败"):
        generate_exam("demo-site", "list", out=site_dir.parent, config=_config(handler))


def test_generate_blank_output_spec_fails(site_dir: Path, tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raw = _mock_exam_raw()
        raw["output_spec"] = "   "
        return _completion(json.dumps(raw))

    with pytest.raises(LLMError, match="试题生成失败"):
        generate_exam("demo-site", "list", out=site_dir.parent, config=_config(handler))


def test_generate_topic_not_found(site_dir: Path, tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("语料无匹配时不应调用 LLM")

    with pytest.raises(ValueError, match="没有匹配"):
        generate_exam("demo-site", "量子计算", out=site_dir.parent, config=_config(handler))


def test_generate_corpus_json_fallback(tmp_path: Path):
    d = tmp_path / "site"
    d.mkdir()
    (d / "corpus.json").write_text(json.dumps({
        "site": "site",
        "pages": [{"url": "https://x.dev/list", "path": "list", "title": "列表",
                   "markdown": "# 列表\n\n列表(list)是 Python 内置序列。"}],
    }, ensure_ascii=False), encoding="utf-8")
    corpus = load_corpus(d)
    assert corpus and corpus[0]["url"] == "https://x.dev/list"
    assert corpus[0]["heading_path"] and corpus[0]["tokens"] > 0


def test_retry_then_success(site_dir: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("omwb.exam.llm.RETRY_INTERVAL", 0.0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(500, json={"error": "boom"})
        return _completion(json.dumps(_mock_exam_raw()))

    exam = generate_exam("demo-site", "list", out=site_dir.parent, variants=0,
                         config=_config(handler))
    assert exam["task"]
    assert len(calls) == 3  # 首次 + 重试 2 次


def test_retry_exhausted(site_dir: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("omwb.exam.llm.RETRY_INTERVAL", 0.0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(LLMError, match="已重试"):
        generate_exam("demo-site", "list", out=site_dir.parent, variants=0,
                      config=_config(handler))
    assert len(calls) == 3


def test_response_format_unsupported_fallback(site_dir: Path, tmp_path: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        body = json.loads(request.content)
        if "response_format" in body:
            return httpx.Response(400, text='{"error": {"message": "response_format is not supported"}}')
        return _completion(json.dumps(_mock_exam_raw()))

    exam = generate_exam("demo-site", "list", out=site_dir.parent, variants=0,
                         config=_config(handler))
    assert exam["task"]
    assert len(calls) == 2  # 带 response_format 失败 → 去掉重试成功


# ---------- 代码审查(批注式) ----------

def _write_review_inputs(tmp_path: Path) -> tuple[Path, Path, str]:
    site = tmp_path / "demo-site"
    site.mkdir()
    exam_path = site / "exam-demo.json"
    exam_path.write_text(json.dumps({
        "id": "demo-exam-1", "site": "demo-site", "title": "LRU 缓存",
        "task": "实现 LRU 缓存", "output_spec": "get/put O(1)",
    }, ensure_ascii=False), encoding="utf-8")
    code = ("def solve(nums):\n"
            "    for i in nums:\n"
            "        for j in nums:\n"
            "            print(i, j)\n"
            "    return")
    code_path = tmp_path / "solve.py"
    code_path.write_text(code, encoding="utf-8")
    return exam_path, code_path, code


def _mock_review_raw() -> dict:
    return {
        "total": {"score": 7, "verdict": "revise", "conclusion": "功能正确但双重循环导致 O(n²)"},
        "annotated_code": [
            {"line": 1, "code": "def solve(nums):", "annotations": [
                {"type": "explain", "text": "定义入口函数",
                 "concrete_explanation": "像给整个任务取个名字,后面都从这里开始"}]},
            {"line": 3, "code": "        for j in nums:", "annotations": [
                {"type": "complexity", "text": "嵌套循环导致 O(n²)",
                 "concrete_explanation": "外层跑 n 次、内层又跑 n 次,共 n×n 次操作,就像两两握手",
                 "doc_ref": "文档:输出规格要求「get/put 平均复杂度 O(1)」,双重循环不满足"}]},
        ],
        "complexity_analysis": {
            "algorithm": "双重循环暴力遍历",
            "complexity": "时间 O(n²) / 空间 O(1)",
            "scale_deduction": [
                {"scale": "1 万条", "operations": "10^8 次", "est_time": "约 1 秒"},
                {"scale": "10 万条", "operations": "10^10 次", "est_time": "约 1-2 分钟"},
            ],
            "notes": "可改用一次遍历 + 哈希集合,降到 O(n)",
        },
        "fixed_code": {
            "code": ("def solve(nums):\n"
                     "    seen = set()\n"
                     "    for x in nums:\n"
                     "        if x in seen:\n"
                     "            return x\n"
                     "        seen.add(x)\n"
                     "    return None"),
            "annotations": [
                {"segment": "seen = set()", "text": "用集合记录已见元素",
                 "concrete_explanation": "集合查找像查通讯录,平均 O(1),不用逐个翻"}]},
    }


def test_review_annotated_anchoring_and_reports(tmp_path: Path):
    exam_path, code_path, code = _write_review_inputs(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        assert "1 | def solve(nums):" in user  # 带行号代码进入 prompt
        return _completion(json.dumps(_mock_review_raw()))

    report = review_code(exam_path, code_path, out=tmp_path, config=_config(handler))
    assert report["total"]["score"] == 7
    assert report["total"]["verdict"] == "revise"
    # 批注锚定:按源码重建视图,覆盖全部 5 行,批注落在行 1/3
    ann = report["annotated_code"]
    assert [a["line"] for a in ann] == [1, 2, 3, 4, 5]
    by_line = {a["line"]: a for a in ann}
    assert by_line[1]["code"] == "def solve(nums):"
    assert by_line[1]["annotations"][0]["type"] == "explain"
    assert by_line[3]["annotations"][0]["type"] == "complexity"
    assert by_line[2]["annotations"] == []
    assert by_line[3]["annotations"][0]["doc_ref"]  # 文档原文对照
    # 复杂度规模推演(两档)
    ca = report["complexity_analysis"]
    assert len(ca["scale_deduction"]) == 2
    assert ca["scale_deduction"][1]["scale"] == "10 万条"
    assert "10^10" in ca["scale_deduction"][1]["operations"]
    # 修正代码带批注
    assert report["fixed_code"]["code"].startswith("def solve")
    assert report["fixed_code"]["annotations"][0]["concrete_explanation"]
    # 报告文件
    md = (tmp_path / "demo-site" / "exam" / "demo-exam-1-review.md").read_text(encoding="utf-8")
    assert (tmp_path / "demo-site" / "exam" / "demo-exam-1-review.json").is_file()
    assert "| 输入规模 | 操作数 | 现实时间 |" in md
    assert "L3 [注" in md and "[complexity]" in md
    assert "```python" in md
    assert "10^10 次" in md
    assert "[文档对照]" in md


def test_review_code_too_large(tmp_path: Path):
    exam_path = tmp_path / "e.json"
    exam_path.write_text('{"id": "x", "site": "s"}', encoding="utf-8")
    code_path = tmp_path / "big.py"
    code_path.write_bytes(b"#" * (200 * 1024 + 1))
    with pytest.raises(ValueError, match="200KB"):
        review_code(exam_path, code_path, config=_config(lambda r: _completion("{}")))


def test_review_invalid_verdict_retries(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("omwb.exam.llm.RETRY_INTERVAL", 0.0)
    exam_path, code_path, _code = _write_review_inputs(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raw = _mock_review_raw()
        raw["total"]["verdict"] = "maybe"
        return _completion(json.dumps(raw))

    with pytest.raises(LLMError, match="审查生成失败"):
        review_code(exam_path, code_path, config=_config(handler))


# ---------- 变体写回 ----------

def test_add_variants_writes_back(tmp_path: Path):
    exam_path = tmp_path / "exam-x.json"
    exam_path.write_text(json.dumps(
        {"id": "x", "site": "s", "task": "t", "output_spec": "o", "variants": []},
        ensure_ascii=False), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion(json.dumps(_mock_variants_raw(2)))

    variants = add_variants(exam_path, count=2, config=_config(handler))
    assert len(variants) == 2
    assert variants[0]["dimension"] == "约束变化"
    saved = json.loads(exam_path.read_text(encoding="utf-8"))
    assert len(saved["variants"]) == 2
    assert saved["variants"][1]["task"] == "变体任务 2"


# ---------- CLI 冒烟 ----------

def test_cli_exam_generate_summary(tmp_path: Path, monkeypatch):
    import omwb.cli as cli_mod
    from typer.testing import CliRunner

    captured = {}

    def fake_generate(site, topic, **kw):
        captured.update(kw)
        return {
            "id": "demo-1", "site": site, "topic": topic, "level": "medium",
            "title": "LRU 缓存", "task": "实现 LRU 缓存", "output_spec": "get/put O(1)",
            "source_annotations": [{"source_chunk": "原文", "annotations": [
                {"anchor": "原文句子", "type": "concept_explain", "explanation": "讲解"}]}],
            "variants": [{"dimension": "性能要求", "title": "v1",
                          "task": "t1", "output_spec": "o1"}],
        }

    monkeypatch.setattr(cli_mod, "generate_exam", fake_generate)
    runner = CliRunner()
    result = runner.invoke(cli_mod.app,
                           ["exam", "generate", "demo-site", "list",
                            "--out", str(tmp_path), "--variants", "1"])
    assert result.exit_code == 0, result.output
    assert "试题已生成" in result.output
    assert "实现 LRU 缓存" in result.output
    assert "变体" in result.output
    assert captured["variants"] == 1
