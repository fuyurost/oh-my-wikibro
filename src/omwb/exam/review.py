"""代码审查(批注式批改):题目 + 代码 → LLM 逐行批注 → 报告落盘。

输出结构:
- total: 评分 + verdict + 一句话结论
- annotated_code: 完整代码带行号,批注锚定到行(explain/error/improve/complexity 四类)
- complexity_analysis: 算法 + 复杂度 + 强制规模推演(规模→操作数→现实时间)
- fixed_code: 完整修正版代码 + 逐段修改说明

落盘:omwb-out/<site>/exam/<exam_id>-review.md 与同名 .json。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .llm import LLMConfig, LLMError, chat_json, resolve_config
from .prompts import TEACHING_RULES

MAX_CODE_BYTES = 200 * 1024  # 代码文件上限 200KB

LANG_BY_EXT = {
    ".py": "Python", ".pyw": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".go": "Go",
    ".java": "Java",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cxx": "C++",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".kt": "Kotlin", ".kts": "Kotlin",
    ".swift": "Swift",
    ".cs": "C#",
    ".sql": "SQL",
    ".lua": "Lua",
    ".r": "R",
    ".pl": "Perl",
    ".scala": "Scala",
    ".ex": "Elixir",
    ".html": "HTML", ".css": "CSS",
}


def detect_language(path: Path) -> str:
    return LANG_BY_EXT.get(path.suffix.lower(), "")


def read_code(path: Path) -> tuple[str, str]:
    """读代码文件,返回 (语言, 内容);超限/缺失抛可读错误。"""
    if not path.is_file():
        raise FileNotFoundError(f"代码文件不存在: {path}")
    data = path.read_bytes()
    if len(data) > MAX_CODE_BYTES:
        raise ValueError(f"代码文件超过 200KB 上限({len(data)} 字节): {path}")
    return detect_language(path), data.decode("utf-8", errors="replace")


class Annotation(BaseModel):
    type: Literal["explain", "error", "improve", "complexity"]
    text: str = ""                  # 批注内容(是什么/为什么/怎么改)
    concrete_explanation: str = ""  # 具象化解释:生活类比或数字示例
    doc_ref: str = ""               # 文档原文对照:引用的原文段落/句子(可选)


class AnnotatedLine(BaseModel):
    line: int = Field(..., ge=1)
    code: str = ""
    annotations: list[Annotation] = Field(default_factory=list)


class ScaleDeduction(BaseModel):
    scale: str = ""       # 输入规模(如 10万条)
    operations: str = ""  # 操作数(如 10^10 次)
    est_time: str = ""    # 现实时间(如 约 1-2 分钟)


class ComplexityAnalysis(BaseModel):
    algorithm: str = ""
    complexity: str = ""                                  # 时间/空间复杂度
    scale_deduction: list[ScaleDeduction] = Field(default_factory=list)  # 规模推演(必填)
    notes: str = ""                                       # 说明与优化建议

    @field_validator("scale_deduction")
    @classmethod
    def _need_deduction(cls, v: list[ScaleDeduction]) -> list[ScaleDeduction]:
        if not v:
            raise ValueError("缺少复杂度规模推演(规模→操作数→现实时间)")
        return v


class FixedAnnotation(BaseModel):
    segment: str = ""              # 对应修正代码片段/函数名
    text: str = ""                 # 为什么这么改
    concrete_explanation: str = ""
    doc_ref: str = ""              # 文档原文对照(可选)


class FixedCode(BaseModel):
    code: str = Field(..., min_length=1)                 # 完整修正版代码
    annotations: list[FixedAnnotation] = Field(default_factory=list)


class Total(BaseModel):
    score: int = Field(..., ge=1, le=10)
    verdict: str = "revise"
    conclusion: str = ""          # 一句话结论

    @field_validator("verdict")
    @classmethod
    def _check_verdict(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("pass", "fail", "revise"):
            raise ValueError("verdict 必须是 pass/fail/revise")
        return v


class CodeReview(BaseModel):
    total: Total
    annotated_code: list[AnnotatedLine] = Field(default_factory=list)
    complexity_analysis: ComplexityAnalysis = Field(default_factory=ComplexityAnalysis)
    fixed_code: FixedCode = Field(default_factory=FixedCode)


def build_review_prompt(exam: dict, code: str, lang: str, code_path: str) -> list[dict]:
    """组装审查 prompt:题目 + 原文对照语料 + 带行号代码 + 批注 JSON schema。"""
    numbered = "\n".join(f"{i + 1} | {line}" for i, line in enumerate(code.splitlines()))
    exam_part = json.dumps(
        {k: exam.get(k) for k in ("title", "task", "output_spec", "constraints", "hints")},
        ensure_ascii=False, indent=2,
    )
    # 文档原文(含锚定讲解),供批注时做"文档这一段说的 X,你代码这里没做到"的对照
    doc_part = json.dumps(
        [{"source_chunk": item.get("source_chunk", ""),
          "annotations": [a.get("anchor", "") for a in item.get("annotations") or []]}
         for item in exam.get("source_annotations") or []],
        ensure_ascii=False, indent=2,
    )
    system = (
        "你是一名严格的代码审查与教学批改专家:既要挑出问题,也要让学习者看懂为什么。"
        f"\n\n{TEACHING_RULES}"
    )
    user = (
        f"代码文件:{code_path}\n语言:{lang or '未知'}\n\n"
        f"===== 题目 =====\n{exam_part}\n\n"
        f"===== 相关文档原文(批注对照用)=====\n{doc_part}\n\n"
        f"===== 待审查代码(行号已标注,批注必须锚定到行号)=====\n"
        f"```{lang}\n{numbered}\n```\n\n"
        "请逐行审查并严格输出 JSON 对象,字段:\n"
        '{"total": {"score": 1-10 整数, "verdict": "pass"|"fail"|"revise", "conclusion": str 一句话结论}, '
        '"annotated_code": [{"line": int 行号, "code": str 该行原文, '
        '"annotations": [{"type": "explain"|"error"|"improve"|"complexity", '
        '"text": str 批注内容, "concrete_explanation": str 具象化解释(生活类比或数字示例), '
        '"doc_ref": str 文档原文对照(可选,引用文档段落/句子:文档这一段说的 X,代码这里没做到,因为…)}]}], '
        '"complexity_analysis": {"algorithm": str 采用的算法, "complexity": str 复杂度(必须显式给出时间与空间), '
        '"scale_deduction": [{"scale": str 输入规模, "operations": str 操作数, "est_time": str 现实时间}](至少两档), '
        '"notes": str 说明与优化建议}, '
        '"fixed_code": {"code": str 完整修正版代码, '
        '"annotations": [{"segment": str 对应片段, "text": str 为什么这么改, '
        '"concrete_explanation": str 具象化解释, "doc_ref": str 文档对照(可选)}]}}。\n'
        "要求:annotated_code 覆盖每一行代码(无批注的行给空 annotations 数组);"
        "批注四类含义:explain=讲解这段代码是什么/为什么这样写,error=纠错(哪错了/为什么/怎么改),"
        "improve=改进(可以更好),complexity=复杂度(为什么是 O(n²),因为什么结构);"
        "每条批注与修改说明的 concrete_explanation 不得留空,必须给生活类比或数字示例;"
        "复杂度规模推演必须给具体数字,如 10万条→10^10 次操作→约 1-2 分钟;"
        "若文档原文与代码相关,批注尽量给 doc_ref 做原文对照。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def normalize_annotated(code: str, raw: list[AnnotatedLine]) -> list[AnnotatedLine]:
    """以源码为准重建逐行批注视图:行号越界钳制,保证每行代码完整、批注锚定正确。"""
    src = code.splitlines()
    n = len(src) or 1
    by_line: dict[int, list[Annotation]] = {}
    for al in raw:
        line = max(1, min(al.line, n))
        by_line.setdefault(line, []).extend(al.annotations)
    return [
        AnnotatedLine(line=i + 1, code=src[i], annotations=by_line.get(i + 1, []))
        for i in range(len(src))
    ]


def render_markdown(exam: dict, report: dict, lang: str) -> str:
    """rich markdown 报告:评分/批注式代码视图/复杂度推演表/修正代码。"""
    total = report["total"]
    lines = [
        "# 代码审查报告",
        "",
        f"- 题目: {exam.get('title') or (exam.get('task') or '')[:60]}",
        f"- 评分: **{total['score']} / 10** | 结论: **{total['verdict']}**",
        f"- 一句话结论: {total.get('conclusion', '')}",
        "",
        "## 批注式代码视图",
        "",
    ]
    ann_lines = report.get("annotated_code") or []
    width = len(str(len(ann_lines))) or 1
    fence_lang = (lang or "text").lower()
    notes: dict[int, int] = {}  # 行号 → 注编号
    note_no = 0
    for al in ann_lines:
        if al["annotations"]:
            note_no += 1
            notes[al["line"]] = note_no
    lines.append(f"```{fence_lang}")
    for al in ann_lines:
        marker = f"    ← [注{notes[al['line']]}]" if al["line"] in notes else ""
        lines.append(f"{str(al['line']).rjust(width)} | {al['code']}{marker}")
    lines.append("```")
    if notes:
        lines += ["", "**批注清单**", ""]
        for al in ann_lines:
            for ann in al["annotations"]:
                lines += [
                    f"- **L{al['line']} [注{notes[al['line']]}]** [{ann['type']}] {ann['text']}",
                    f"  > {ann['concrete_explanation']}",
                ]
                if ann.get("doc_ref"):
                    lines += [f"  > [文档对照] {ann['doc_ref']}"]
    lines += ["", "## 复杂度分析", ""]
    ca = report.get("complexity_analysis") or {}
    if ca.get("algorithm"):
        lines.append(f"- 算法: {ca['algorithm']}")
    if ca.get("complexity"):
        lines.append(f"- 复杂度: {ca['complexity']}")
    deduction = ca.get("scale_deduction") or []
    if deduction:
        lines += ["", "**规模推演**", "", "| 输入规模 | 操作数 | 现实时间 |", "|---|---|---|"]
        for row in deduction:
            lines.append(f"| {row.get('scale', '')} | {row.get('operations', '')} | {row.get('est_time', '')} |")
    if ca.get("notes"):
        lines += ["", f"- 说明与优化建议: {ca['notes']}"]
    lines += ["", "## 修正代码", ""]
    fixed = report.get("fixed_code") or {}
    if fixed.get("code"):
        lines += [f"```{fence_lang}", fixed["code"], "```"]
    for fa in fixed.get("annotations") or []:
        lines += [
            "",
            f"- **[修正说明] {fa.get('segment', '')}**: {fa.get('text', '')}",
            f"  > {fa.get('concrete_explanation', '')}",
        ]
        if fa.get("doc_ref"):
            lines += [f"  > [文档对照] {fa['doc_ref']}"]
    return "\n".join(lines).rstrip() + "\n"


def _load_exam(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"试题文件不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"试题文件格式错误(应为 JSON 对象): {path}")
    return data


def review_output_paths(exam: dict, out: str | Path) -> tuple[Path, Path]:
    """审查报告输出路径(md 与 json),供落盘与 CLI 提示共用。"""
    site = exam.get("site") or ""
    exam_id = exam.get("id") or ""
    out_dir = Path(out) / site / "exam"
    return out_dir / f"{exam_id}-review.md", out_dir / f"{exam_id}-review.json"


def review_code(exam_path: str | Path, code_path: str | Path, out: str | Path = "omwb-out",
                base_url: str | None = None, api_key: str | None = None,
                model: str | None = None, config: LLMConfig | None = None) -> dict:
    """审查学习者代码:LLM 批注 → 结构校验(失败重试一次)→ 报告落盘,返回审查 dict。"""
    if config is None:
        config = resolve_config(base_url, api_key, model)
    exam = _load_exam(Path(exam_path))
    lang, code = read_code(Path(code_path))
    messages = build_review_prompt(exam, code, lang, str(code_path))
    last_err: Exception | None = None
    for _attempt in range(2):  # 校验失败重试一次(调用失败已由 chat_json 内部重试)
        try:
            raw = chat_json(config, messages)
            review = CodeReview.model_validate(raw)
            break
        except (ValidationError, ValueError) as e:
            last_err = e
    else:
        raise LLMError(f"审查生成失败(LLM 输出未通过校验): {last_err}")
    review.annotated_code = normalize_annotated(code, review.annotated_code)
    report = review.model_dump()
    md_path, json_path = review_output_paths(exam, out)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(exam, report, lang), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
