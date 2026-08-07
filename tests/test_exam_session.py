"""exam 考试会话测试:错误统计聚合、error-book 只增不删、大测验数据层、盲答与文件/粘贴答题。

LLM 调用通过 httpx.MockTransport 注入;另有真实 HTTP 服务器 + 环境变量注入的
CLI 集成测试(脚本模拟:文件答题 → 提交 → 批改显示)。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path

import httpx
import pytest

from omwb.exam.llm import LLMConfig
from omwb.exam.session import (aggregate_errors, build_grand_exam_prompt,
                               format_blind_question, load_error_book, paste_answer,
                               run_session, update_error_book)


def _config(handler) -> LLMConfig:
    return LLMConfig(base_url="https://llm.test", model="mock",
                     transport=httpx.MockTransport(handler))


def _completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _mock_session_exam_raw() -> dict:
    return {
        "title": "用列表实现查找",
        "task": "实现函数 find(items, target):返回 target 在列表中的下标",
        "output_spec": "命中返回下标,未命中返回 -1;空列表返回 -1;输入为整数列表",
        "constraints": ["仅用 Python 标准库"],
        "hints": ["列表(list)支持按位置访问"],
        "level": "medium",
        "source_annotations": [
            {"material_index": 1,
             "anchor": "列表(list)是 Python 内置序列",
             "type": "concept_explain",
             "explanation": "列表(list)是 Python 内置的序列容器,支持按位置存取,像宿舍楼里编了号的储物柜:"
                            "想取第 3 个柜子里的东西直接走过去就行,不用从第 1 个开始数。"
                            "本题要求返回下标,正是按编号定位的典型场景。"},
        ],
    }


def _mock_grade_raw(pass_: bool) -> dict:
    if pass_:
        return {
            "total": {"score": 9, "verdict": "pass",
                      "conclusion": "实现正确,单次遍历 O(n),边界齐全"},
            "error_points": [],
            "covered_concepts": ["列表遍历", "文件解析"],
            "annotated_code": [{"line": 1, "code": "def find(items, target):", "annotations": [
                {"type": "explain", "text": "入口函数,参数为列表与目标值",
                 "concrete_explanation": "像报出要找的柜子号,直接按号定位"}]}],
            "complexity_analysis": {
                "algorithm": "单次线性扫描", "complexity": "时间 O(n) / 空间 O(1)",
                "scale_deduction": [
                    {"scale": "1 万元素", "operations": "10^4 次", "est_time": "毫秒级"},
                    {"scale": "100 万元素", "operations": "10^6 次", "est_time": "约 0.1 秒"},
                ],
                "notes": "若需多次查找可先排序后二分,但本题单次查找线性即可。"},
            "fixed_code": {"code": "def find(items, target):\n    for i, x in enumerate(items):\n        if x == target:\n            return i\n    return -1",
                           "annotations": []},
            "solution": {
                "correct_approach": "用 enumerate 同时取下标与值,命中即返回,遍历完未命中返回 -1。",
                "doc_annotations": [
                    {"anchor": "列表(list)是 Python 内置序列", "type": "task_link",
                     "explanation": "列表按下标 O(1) 访问,但本题要'找到目标所在下标',必须逐个检查元素,"
                                    "像在储物柜里找某件东西得逐格看;最多看 n 格,所以是 O(n)。"}]},
        }
    return {
        "total": {"score": 5, "verdict": "fail",
                  "conclusion": "双重循环冗余且漏了关键词过滤"},
        "error_points": ["文件解析", "关键词过滤"],
        "covered_concepts": ["文件解析", "关键词过滤", "列表遍历"],
        "annotated_code": [{"line": 1, "code": "def solve():", "annotations": [
            {"type": "error", "text": "未处理输入文件不存在的场景",
             "concrete_explanation": "像不看门牌号就闯进大楼,文件没有时应报错并退出",
             "doc_ref": "文档:输出规格要求文件不存在时给出提示"}]}],
        "complexity_analysis": {
            "algorithm": "双重循环全量比较", "complexity": "时间 O(n²) / 空间 O(n)",
            "scale_deduction": [
                {"scale": "1 万行", "operations": "10^8 次", "est_time": "约 1 秒"},
                {"scale": "10 万行", "operations": "10^10 次", "est_time": "约 1-2 分钟"},
            ],
            "notes": "去掉内层循环即 O(n)。"},
        "fixed_code": {"code": "def solve():\n    pass", "annotations": []},
        "solution": {
            "correct_approach": "单遍读取输入,按规则过滤后输出。",
            "doc_annotations": [
                {"anchor": "列表(list)是 Python 内置序列", "type": "pitfall",
                 "explanation": "文件解析要逐行处理而不是两层循环两两比较,像收发室按名单逐件核对,"
                                "而不是把每封信跟所有其他信都比一遍;后者在 10 万封时要做 10^10 次,"
                                "约 1-2 分钟,几乎不可用。"}]},
    }


def _session_corpus(tmp_path: Path) -> Path:
    site_dir = tmp_path / "demo-site"
    site_dir.mkdir()
    (site_dir / "corpus.jsonl").write_text(json.dumps(
        {"site": "demo-site", "url": "https://demo.dev/list", "path": "list",
         "title": "列表", "seq": 0, "heading_path": ["列表"], "tokens": 60,
         "text": "列表(list)是 Python 内置序列。"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return site_dir


# ---------- 错误统计 ----------

def test_aggregate_errors_ratio():
    reviews = [
        {"index": 1, "verdict": "fail", "error_points": ["文件解析", "关键词过滤"]},
        {"index": 2, "verdict": "revise", "error_points": ["文件解析"]},
        {"index": 3, "verdict": "pass", "error_points": []},
        {"index": 4, "verdict": "fail", "error_points": ["边界处理"]},
    ]
    summary = aggregate_errors(reviews)
    by = {it["concept"]: it for it in summary}
    assert by["文件解析"]["count"] == 2 and by["文件解析"]["ratio"] == 0.67
    assert by["关键词过滤"]["count"] == 1 and by["关键词过滤"]["ratio"] == 0.33
    assert by["边界处理"]["examples"] == [4]
    assert len(summary) == 3
    assert aggregate_errors([{"index": 1, "verdict": "pass", "error_points": []}]) == []


# ---------- error-book 只增不删 ----------

def test_error_book_append_and_resolve():
    book = {"site": "s", "errors": [
        {"id": "e-old1", "date": "2026-01-01", "session_id": "session-old",
         "question_id": "q1", "concept": "文件解析", "error_description": "旧错误", "status": "open"},
        {"id": "e-old2", "date": "2026-01-01", "session_id": "session-old",
         "question_id": "q1", "concept": "复杂度", "error_description": "旧错误", "status": "open"},
    ]}
    questions = [{"index": 1, "question_id": "q1"}, {"index": 2, "question_id": "q2"}]
    reviews = [
        {"index": 1, "verdict": "fail", "error_points": ["文件解析", "关键词过滤"],
         "annotated_code": [], "total": {"conclusion": "结论"}},
        {"index": 2, "verdict": "pass", "covered_concepts": ["文件解析"], "error_points": []},
    ]
    update_error_book(book, "session-now", questions, reviews)
    errs = book["errors"]
    assert len(errs) == 4  # 只追加,永不删除
    new_a, new_b = errs[2], errs[3]
    assert new_a["concept"] == "文件解析" and new_a["status"] == "open"
    assert new_a["session_id"] == "session-now" and new_a["question_id"] == "q1"
    assert new_b["concept"] == "关键词过滤" and new_b["status"] == "open"
    old1 = next(e for e in errs if e["id"] == "e-old1")
    old2 = next(e for e in errs if e["id"] == "e-old2")
    assert old1["status"] == "resolved"  # 答对覆盖的概念 → 旧记录标记 resolved(保留)
    assert old2["status"] == "open"      # 未覆盖的概念保持 open


# ---------- 大测验数据层 ----------

def test_build_grand_exam_prompt():
    book = {"site": "s", "errors": [
        {"concept": "关键词过滤", "session_id": "session-1", "question_id": "q1",
         "error_description": "没有按关键词过滤", "status": "open"},
        {"concept": "文件解析", "session_id": "session-1", "question_id": "q2",
         "error_description": "解析异常", "status": "resolved"},
    ]}
    sessions = [{"session_id": "session-1", "questions": [{"index": 1}, {"index": 2}],
                 "error_summary": [{"concept": "关键词过滤", "count": 1, "ratio": 1.0}]}]
    text = build_grand_exam_prompt(book, sessions, "llms.txt")
    assert "主题:llms.txt" in text
    assert "[open] 关键词过滤" in text and "没有按关键词过滤" in text
    assert text.index("[open]") < text.index("已解决")  # open 优先
    assert "已解决概念: 文件解析×1" in text
    assert "session-1" in text and "关键词过滤×1(1.0)" in text


# ---------- 盲答与答题采集 ----------

def test_format_blind_question():
    exam = {"title": "T", "task": "实现 X", "output_spec": "输出 Y",
            "constraints": ["仅标准库"], "hints": ["提示1"],
            "source_chunks": [{"url": "https://secret/doc"}],
            "source_annotations": [{"source_chunk": "机密原文", "annotations": []}]}
    text = format_blind_question(exam)
    assert "实现 X" in text and "输出 Y" in text and "仅标准库" in text and "提示1" in text
    assert "机密原文" not in text and "secret" not in text  # 盲答:不展示原文与讲解


def test_paste_answer():
    stream = StringIO("def f():\n    pass\n---END---\n")
    text = paste_answer("题目?", stream)
    assert "def f():" in text and "pass" in text
    assert "---END---" not in text


# ---------- 会话全流程 ----------

def _mock_session_handler():
    """MockTransport/真实 HTTP 服务器共用的响应逻辑。"""

    def pick(user: str) -> str:
        if "批改" in user:
            return json.dumps(_mock_grade_raw("02.py" in user), ensure_ascii=False)
        return json.dumps(_mock_session_exam_raw(), ensure_ascii=False)

    return pick


def test_session_flow_file_answers(tmp_path: Path):
    site_dir = _session_corpus(tmp_path)
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "01.py").write_text("def solve():\n    return 1\n", encoding="utf-8")
    (answers / "02.py").write_text("def find(items, target):\n    return 0\n", encoding="utf-8")
    pick = _mock_session_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return _completion(pick(body["messages"][-1]["content"]))

    record = run_session("demo-site", "list", level="medium", count=2, out=tmp_path,
                         answers_dir=answers, config=_config(handler))
    assert record["session_id"].startswith("session-")
    assert len(record["questions"]) == 2
    assert record["questions"][0]["answer"].startswith("def solve")
    assert record["questions"][0]["answer_mode"] == "file"
    assert record["questions"][0]["source_annotations"][0]["source_chunk"] == "列表(list)是 Python 内置序列。"
    # 批改:q1 错(q2 对),error_points 与题解(带原文批注)
    r1, r2 = record["review"][0], record["review"][1]
    assert r1["verdict"] == "fail" and r1["error_points"] == ["文件解析", "关键词过滤"]
    assert r2["verdict"] == "pass" and r2["error_points"] == []
    sol = r1["solution"]
    assert sol["correct_approach"] and sol["doc_annotations"][0]["anchor"]
    assert len(sol["doc_annotations"][0]["explanation"]) >= 80
    # 错误统计:错题数 = 1,两个错误点 ratio 均为 1.0
    by = {it["concept"]: it for it in record["error_summary"]}
    assert by["文件解析"]["ratio"] == 1.0 and by["关键词过滤"]["count"] == 1
    # 落盘:会话 json + review.md + error-book
    session_dir = site_dir / "exam" / "sessions"
    files = list(session_dir.glob("session-*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved["error_summary"] == record["error_summary"]
    assert (session_dir / "review.md").is_file()
    md = (session_dir / "review.md").read_text(encoding="utf-8")
    assert "考试批改报告" in md and "文件解析" in md and "题解" in md
    book = load_error_book(site_dir)
    assert len(book["errors"]) >= 2
    assert all(e["status"] == "open" for e in book["errors"])


def test_session_flow_paste_blind(tmp_path: Path, capsys):
    site_dir = _session_corpus(tmp_path)
    pick = _mock_session_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return _completion(pick(body["messages"][-1]["content"]))

    stdin = StringIO("def find(items, target):\n    return 0\n---END---\n")
    record = run_session("demo-site", "list", level="medium", count=1, out=tmp_path,
                         config=_config(handler), stdin=stdin)
    assert record["questions"][0]["answer_mode"] == "paste"
    assert "def find" in record["questions"][0]["answer"]
    out = capsys.readouterr().out
    assert "要求实现:" in out          # 展示了题目
    assert "列表(list)是 Python 内置序列" not in out  # 盲答:不展示原文
    assert site_dir / "exam" / "error-book.json"  # error-book 已生成


# ---------- CLI 集成(真实 HTTP + 环境变量)----------

def test_cli_exam_session_end_to_end(tmp_path: Path, monkeypatch):
    """脚本模拟:文件答题 → 提交 → 批改显示(真实 HTTP 服务器 + typer envvar)。"""
    site_dir = _session_corpus(tmp_path)
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "01.py").write_text("def solve():\n    return 1\n", encoding="utf-8")
    (answers / "02.py").write_text("def find(items, target):\n    return 0\n", encoding="utf-8")
    pick = _mock_session_handler()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            content = pick(body["messages"][-1]["content"])
            data = json.dumps({"choices": [{"message": {"content": content}}]},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("OMWB_LLM_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setenv("OMWB_LLM_API_KEY", "test-key")
        monkeypatch.setenv("OMWB_LLM_MODEL", "mock")
        from typer.testing import CliRunner
        from omwb.cli import app

        result = CliRunner().invoke(app, [
            "exam", "session", "demo-site", "list",
            "--count", "2", "--answers-dir", str(answers), "--out", str(tmp_path),
        ])
        assert result.exit_code == 0, result.output
        assert "测验完成" in result.output
        assert "批改总览" in result.output
        assert "文件解析" in result.output
        assert "错误簿" in result.output
    finally:
        srv.shutdown()
    assert (site_dir / "exam" / "error-book.json").is_file()
    assert list((site_dir / "exam" / "sessions").glob("session-*.json"))
