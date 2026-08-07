"""考试会话:出题 → 盲答 → 批改+题解 → 错误统计 → error-book 持久化。

流程(omwb exam session):
1. 出题:生成 N 道题,每题带原文来源(source_chunks / source_annotations)。
2. 测验模式:答题阶段不展示原文与讲解,题目只显示 task/output_spec/constraints/hints(盲答)。
3. 答题:逐题输入,支持多行粘贴(单独一行 ---END--- 结束)或 answers 目录按题号读文件。
4. 提交:全部答完一次性提交,LLM 逐题批改(行号锚定 + 原文对照),错题给题解。
5. 错误统计:批改输出每题的 error_points[](概念标签),本地聚合 {concept, count, ratio, examples}。
6. error-book 只追加不删除:错题概念追加 open 记录;答对题覆盖的概念把旧错误标记 resolved。

输出:omwb-out/<site>/exam/sessions/session-<ts>.json + review.md;omwb-out/<site>/exam/error-book.json。
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel, Field, ValidationError

from .generate import generate_exam
from .llm import LLMConfig, LLMError, chat_json, resolve_config
from .prompts import TEACHING_RULES
from .review import (AnnotatedLine, ComplexityAnalysis, FixedCode, Total,
                     detect_language, normalize_annotated, read_code, render_markdown)

ANSWER_END_MARKER = "---END---"
BLIND_FIELDS = ("title", "task", "output_spec", "constraints", "hints")  # 答题阶段仅展示这些


class SolutionDocAnnotation(BaseModel):
    anchor: str = ""               # 锚点:原文句子摘录或段落序号
    type: str = "concept_explain"
    explanation: str = ""          # 题解讲解(展开,含类比/示例)


class Solution(BaseModel):
    correct_approach: str = ""                                              # 正确思路
    doc_annotations: list[SolutionDocAnnotation] = Field(default_factory=list)  # 题解原文批注


class Grade(BaseModel):
    """单题批改:复用审查结构 + error_points/covered_concepts/题解。"""

    total: Total
    error_points: list[str] = Field(default_factory=list)       # 概念标签(错题)
    covered_concepts: list[str] = Field(default_factory=list)   # 本题考察的概念
    annotated_code: list[AnnotatedLine] = Field(default_factory=list)
    complexity_analysis: ComplexityAnalysis = Field(default_factory=ComplexityAnalysis)
    fixed_code: FixedCode = Field(default_factory=FixedCode)
    solution: Solution = Field(default_factory=Solution)


# ---------- prompt ----------

def build_grade_prompt(exam: dict, code: str, lang: str, code_path: str) -> list[dict]:
    """组装批改 prompt:题目(含原文对照语料)+ 带行号代码 + 批改 JSON schema。"""
    numbered = "\n".join(f"{i + 1} | {line}" for i, line in enumerate(code.splitlines()))
    exam_part = json.dumps(
        {k: exam.get(k) for k in ("title", "task", "output_spec", "constraints", "hints")},
        ensure_ascii=False, indent=2,
    )
    doc_part = json.dumps(
        [{"source_chunk": item.get("source_chunk", ""),
          "annotations": [a.get("anchor", "") for a in item.get("annotations") or []]}
         for item in exam.get("source_annotations") or []],
        ensure_ascii=False, indent=2,
    )
    system = (
        "你是一名严格的编程考试批改专家:逐行批改考生代码,判定对错并给出可落地的题解,"
        "讲解以原文为底、指哪打哪。"
        f"\n\n{TEACHING_RULES}"
    )
    user = (
        f"代码文件:{code_path}\n语言:{lang or '未知'}\n\n"
        f"===== 题目 =====\n{exam_part}\n\n"
        f"===== 相关文档原文(批注对照与题解用)=====\n{doc_part}\n\n"
        f"===== 考生代码(行号已标注,批注必须锚定到行号)=====\n"
        f"```{lang}\n{numbered}\n```\n\n"
        "请批改并严格输出 JSON 对象,字段:\n"
        '{"total": {"score": 1-10 整数, "verdict": "pass"|"fail"|"revise", "conclusion": str 一句话结论}, '
        '"error_points": [str] 本题答错涉及的概念标签(如"文件解析"/"关键词过滤",答对给空数组), '
        '"covered_concepts": [str] 本题考察的核心概念标签, '
        '"annotated_code": [{"line": int, "code": str 该行原文, '
        '"annotations": [{"type": "explain"|"error"|"improve"|"complexity", '
        '"text": str, "concrete_explanation": str 具象化解释(生活类比或数字示例), '
        '"doc_ref": str 文档原文对照(可选)}]}], '
        '"complexity_analysis": {"algorithm": str, "complexity": str 时间与空间复杂度, '
        '"scale_deduction": [{"scale": str 输入规模, "operations": str 操作数, "est_time": str 现实时间}](至少两档), '
        '"notes": str 说明与优化建议}, '
        '"fixed_code": {"code": str 完整修正版代码, '
        '"annotations": [{"segment": str, "text": str 为什么这么改, '
        '"concrete_explanation": str, "doc_ref": str 文档对照(可选)}]}, '
        '"solution": {"correct_approach": str 正确思路(展开讲解), '
        '"doc_annotations": [{"anchor": str 原文锚点(句子摘录或段落序号), '
        '"type": "concept_explain"|"plain_words"|"example"|"pitfall"|"task_link", '
        '"explanation": str 题解讲解(≥80 字,含类比或数字示例)}]}}。\n'
        "要求:批注四类含义 explain/error/improve/complexity;annotated_code 覆盖每一行;"
        "complexity 必须给规模推演数字;error_points 与 covered_concepts 用简短中文概念标签;"
        "solution 的 doc_annotations 锚点必须能定位回文档原文。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------- 批改 ----------

def grade_code(exam: dict, code: str, lang: str = "Python",
               code_path: str = "<考生答案>", config: LLMConfig | None = None,
               base_url: str | None = None, api_key: str | None = None,
               model: str | None = None) -> dict:
    """批改单题:LLM 批注 + 题解,按源码重建行号锚定,返回批改 dict。"""
    if config is None:
        config = resolve_config(base_url, api_key, model)
    messages = build_grade_prompt(exam, code, lang, code_path)
    last_err: Exception | None = None
    for _attempt in range(2):  # 校验失败重试一次(调用失败已由 chat_json 内部重试)
        try:
            raw = chat_json(config, messages)
            grade = Grade.model_validate(raw)
            break
        except (ValidationError, ValueError) as e:
            last_err = e
    else:
        raise LLMError(f"批改生成失败(LLM 输出未通过校验): {last_err}")
    grade.annotated_code = normalize_annotated(code, grade.annotated_code)
    report = grade.model_dump()
    report["score"] = report["total"]["score"]
    report["verdict"] = report["total"]["verdict"]
    report["lang"] = lang
    return report


# ---------- 答题采集 ----------

def format_blind_question(exam: dict) -> str:
    """盲答模式下的题目文本:只含 task/output_spec/constraints/hints,不含原文与讲解。"""
    lines = [f"题目: {exam.get('title') or exam['task'][:60]}", "",
             f"要求实现: {exam['task']}", "",
             f"输出规格: {exam['output_spec']}"]
    if exam.get("constraints"):
        lines += ["", "约束:"] + [f"- {c}" for c in exam["constraints"]]
    if exam.get("hints"):
        lines += ["", "提示:"] + [f"- {h}" for h in exam["hints"]]
    return "\n".join(lines)


def read_answer_file(answers_dir: Path, index: int) -> tuple[str, str, str]:
    """按题号读答案文件(01.py / 1.py / 01.txt…),返回 (内容, 语言, 文件名)。"""
    base = f"{index:02d}"
    candidates = sorted(answers_dir.glob(f"{base}.*")) or sorted(answers_dir.glob(f"{index}.*"))
    if not candidates:
        raise FileNotFoundError(f"缺少第 {index} 题的答案文件(期望 {answers_dir}/{base}.py 等)")
    path = candidates[0]
    return path.read_text(encoding="utf-8", errors="replace"), detect_language(path), path.name


def paste_answer(prompt: str, stream: TextIO = sys.stdin) -> str:
    """多行粘贴答题:直到单独一行 ---END--- 或 EOF。"""
    print(prompt)
    print(f"(输入完成后在单独一行输入 {ANSWER_END_MARKER} 结束,或 Ctrl+Z / Ctrl-D)")
    lines: list[str] = []
    for line in stream:
        if line.rstrip("\r\n") == ANSWER_END_MARKER:
            break
        lines.append(line)
    return "".join(lines)


# ---------- 错误统计与 error-book ----------

def aggregate_errors(reviews: list[dict]) -> list[dict]:
    """错误统计:按概念聚合错题,ratio = count / 错题数,examples 引用题号。"""
    wrong = [r for r in reviews if r.get("verdict") != "pass"]
    if not wrong:
        return []
    counts: dict[str, int] = {}
    examples: dict[str, list[int]] = {}
    for r in wrong:
        for ep in r.get("error_points") or []:
            counts[ep] = counts.get(ep, 0) + 1
            examples.setdefault(ep, []).append(r["index"])
    total = len(wrong)
    return [
        {"concept": c, "count": n, "ratio": round(n / total, 2), "examples": examples[c]}
        for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _error_description(review: dict) -> str:
    """错误描述:优先取第一条 error 批注,其次取结论。"""
    for al in review.get("annotated_code") or []:
        for ann in al.get("annotations") or []:
            if ann.get("type") == "error" and ann.get("text"):
                return ann["text"]
    return review.get("total", {}).get("conclusion", "") or ""


def load_error_book(site_dir: Path) -> dict:
    book_path = site_dir / "exam" / "error-book.json"
    if book_path.is_file():
        try:
            data = json.loads(book_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("errors"), list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"site": site_dir.name, "errors": []}


def save_error_book(site_dir: Path, book: dict) -> Path:
    path = site_dir / "exam" / "error-book.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update_error_book(book: dict, session_id: str, questions: list[dict],
                      reviews: list[dict]) -> list[dict]:
    """只追加不删除:错题概念追加 open;答对题覆盖的概念把**旧** open 错误标记 resolved。"""
    by_index = {r["index"]: r for r in reviews}
    for q in questions:
        r = by_index.get(q["index"])
        if r is None:
            continue
        qid = q["question_id"]
        if r.get("verdict") == "pass":
            for concept in r.get("covered_concepts") or []:
                for err in book["errors"]:
                    # 只处理本场测验之前的旧记录,本场新追加的保持 open
                    if (err["status"] == "open" and err["concept"] == concept
                            and err.get("session_id") != session_id):
                        err["status"] = "resolved"
        else:
            desc = _error_description(r)
            for concept in r.get("error_points") or []:
                book["errors"].append({
                    "id": f"e-{uuid.uuid4().hex[:8]}",
                    "date": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "session_id": session_id,
                    "question_id": qid,
                    "concept": concept,
                    "error_description": desc,
                    "status": "open",
                })
    return book["errors"]


# ---------- 大测验数据层 ----------

def build_grand_exam_prompt(error_book: dict, recent_sessions: list[dict], topic: str) -> str:
    """把历史错误簿(open 优先)与近期测验记录组装为结构化上下文(供大测验生成)。"""
    lines = [f"主题:{topic}", "", "## 历史错误簿(open 优先)", ""]
    errors = error_book.get("errors") or []
    open_errs = [e for e in errors if e.get("status") == "open"]
    resolved = [e for e in errors if e.get("status") != "open"]
    if not open_errs and not resolved:
        lines.append("(无历史错误记录)")
    for e in open_errs:
        lines.append(f"- [open] {e.get('concept')}({e.get('session_id')} / 题 "
                     f"{e.get('question_id')}): {e.get('error_description', '')}")
    if resolved:
        cnt = Counter(e.get("concept") for e in resolved)
        lines += ["", "已解决概念: " + ", ".join(f"{c}×{n}" for c, n in cnt.most_common())]
    lines += ["", "## 近期测验记录", ""]
    if not recent_sessions:
        lines.append("(无)")
    for s in recent_sessions[:5]:
        summary = s.get("error_summary") or []
        part = (", ".join(f"{it.get('concept')}×{it.get('count')}({it.get('ratio')})"
                          for it in summary) if summary else "无错误")
        lines.append(f"- {s.get('session_id')}: 题数 {len(s.get('questions') or [])},"
                     f"错误统计: {part}")
    return "\n".join(lines)


# ---------- 会话记录与报告 ----------

def render_session_markdown(record: dict) -> str:
    """会话批改报告:总览表 + 逐题(批注式视图 + 题解原文批注)。"""
    lines = [
        "# 考试批改报告",
        "",
        f"- 会话: {record['session_id']} | 站点: {record['site']} | 主题: {record['topic']} "
        f"| 难度: {record['level']} | 题数: {record['count']}",
        "",
        "## 总览",
        "",
        "| 题号 | 评分 | 结论 | 错误点 |",
        "|---|---|---|---|",
    ]
    for r in record["review"]:
        points = "、".join(r.get("error_points") or []) or "-"
        lines.append(f"| {r['index']} | {r['score']}/10 | {r['verdict']} | {points} |")
    lines += ["", "## 错误统计", ""]
    summary = record.get("error_summary") or []
    if summary:
        lines.append("| 概念 | 次数 | 占错题比例 | 例题 |")
        lines.append("|---|---|---|---|")
        for it in summary:
            ex = ", ".join(str(i) for i in it.get("examples") or [])
            lines.append(f"| {it['concept']} | {it['count']} | {it['ratio']} | 第 {ex} 题 |")
    else:
        lines.append("无错题。")
    by_index = {r["index"]: r for r in record["review"]}
    for q in record["questions"]:
        r = by_index.get(q["index"])
        if r is None:
            continue
        lines += ["", f"## 第 {q['index']} 题:{q.get('title') or q['task'][:40]}", "",
                  f"- 题目: {q['task']}", f"- 输出规格: {q['output_spec']}",
                  f"- 批改: **{r['score']}/10** | 结论: **{r['verdict']}**", ""]
        if r.get("error_points"):
            lines += [f"- 错误点: {'、'.join(r['error_points'])}", ""]
        render_part = render_markdown(q, r, r.get("lang") or "text")
        lines.append(render_part)
        solution = r.get("solution") or {}
        if solution.get("correct_approach") or solution.get("doc_annotations"):
            lines += ["", "### 题解", ""]
            if solution.get("correct_approach"):
                lines += ["**正确思路**", "", solution["correct_approach"], ""]
            for a in solution.get("doc_annotations") or []:
                lines += [
                    f"- **{a.get('anchor', '')}** [{a.get('type', '')}]",
                    f"  > {a.get('explanation', '')}",
                ]
    return "\n".join(lines).rstrip() + "\n"


def run_session(site: str, topic: str, level: str = "medium", count: int = 3,
                out: str | Path = "omwb-out", answers_dir: str | Path | None = None,
                config: LLMConfig | None = None, base_url: str | None = None,
                api_key: str | None = None, model: str | None = None,
                stdin: TextIO | None = None) -> dict:
    """完整考试会话:出题 → 盲答 → 批改+题解 → 错误统计 → error-book 落盘。返回会话记录。"""
    if config is None:
        config = resolve_config(base_url, api_key, model)
    if count < 1:
        raise ValueError("题目数量必须 ≥ 1")
    site_dir = Path(out) / site
    session_id = f"session-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"

    # 1) 出题(N 道,blind 模式:答题阶段看不到原文)
    questions = []
    for k in range(1, count + 1):
        exam = generate_exam(site, topic, level=level, out=out, variants=0,
                             config=config, id_tag=f"q{k}")
        questions.append({
            "index": k,
            "question_id": exam["id"],
            "title": exam.get("title", ""),
            "task": exam["task"],
            "output_spec": exam["output_spec"],
            "constraints": exam.get("constraints") or [],
            "hints": exam.get("hints") or [],
            "source_chunks": exam.get("source_chunks") or [],
            "source_annotations": exam.get("source_annotations") or [],
            "answer": "",
            "answer_mode": "",
        })

    # 2+3) 答题:文件方式(按题号)或交互粘贴(---END--- 结束)
    answers: list[tuple[str, str, str]] = []  # (code, lang, source_name)
    if answers_dir is not None:
        adir = Path(answers_dir)
        if not adir.is_dir():
            raise FileNotFoundError(f"答案目录不存在: {adir}")
        for q in questions:
            code, lang, name = read_answer_file(adir, q["index"])
            q["answer"] = code
            q["answer_mode"] = "file"
            answers.append((code, lang or "text", name))
    else:
        stream = stdin if stdin is not None else sys.stdin
        for q in questions:
            code = paste_answer(format_blind_question(q), stream)
            q["answer"] = code
            q["answer_mode"] = "paste"
            answers.append((code, "Python", f"{q['index']:02d}.py"))  # 粘贴模式默认按 Python 批改

    # 4) 提交批改 + 题解
    reviews = []
    for q, (code, lang, name) in zip(questions, answers):
        if not code.strip():
            reviews.append({
                "index": q["index"], "question_id": q["question_id"],
                "score": 0, "verdict": "fail", "conclusion": "未作答",
                "error_points": ["未作答"], "covered_concepts": [], "lang": lang,
                "annotated_code": [], "complexity_analysis": {}, "fixed_code": {},
                "solution": {},
            })
            continue
        report = grade_code(q, code, lang=lang, code_path=name, config=config)
        report["index"] = q["index"]
        report["question_id"] = q["question_id"]
        reviews.append(report)

    # 5) 错误统计 + error-book
    error_summary = aggregate_errors(reviews)
    book = load_error_book(site_dir)
    update_error_book(book, session_id, questions, reviews)
    save_error_book(site_dir, book)

    # 6) 落盘
    record = {
        "session_id": session_id,
        "site": site,
        "topic": topic,
        "level": level,
        "count": count,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "questions": questions,
        "review": reviews,
        "error_summary": error_summary,
    }
    session_dir = site_dir / "exam" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"{session_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    (session_dir / "review.md").write_text(
        render_session_markdown(record), encoding="utf-8")
    return record
