"""自动举一反三:基于原题生成 N 个变体,写回试题 JSON 的 variants 数组。

变化维度:约束变化 / 输入规模 / 性能要求 / 错误处理 / 功能扩展。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from .llm import LLMConfig, LLMError, chat_json, resolve_config
from .prompts import TEACHING_RULES

DIMENSIONS = ["约束变化", "输入规模", "性能要求", "错误处理", "功能扩展"]


class Variant(BaseModel):
    dimension: str = ""       # 主要变化维度
    title: str | None = None
    task: str = Field(..., min_length=1)
    output_spec: str = Field(..., min_length=1)
    constraints: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)

    @field_validator("task", "output_spec")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空")
        return v


def build_variants_prompt(exam: dict, count: int) -> list[dict]:
    """组装变体 prompt:原题 + 变化维度 + 变体 JSON schema。"""
    system = (
        "你是一名资深编程教育专家,擅长设计同主题的变式训练题,"
        "让学习者在不同约束/规模/要求下反复练习同一核心能力。"
        f"\n\n{TEACHING_RULES}"
    )
    original = json.dumps(
        {k: exam.get(k) for k in ("title", "task", "output_spec", "constraints", "hints", "level")},
        ensure_ascii=False, indent=2,
    )
    user = (
        f"原题:\n{original}\n\n"
        f"请基于原题生成 {count} 个变体,每个变体从以下维度中选一个作为主要变化维度:"
        f"{'/'.join(DIMENSIONS)}。变化要具体:约束变化=加限制/换语言/禁库等,"
        "输入规模=数据量级变化,性能要求=复杂度上限变化,错误处理=新增异常/非法输入场景,"
        "功能扩展=在原功能上新增能力。难度与原题相当,严格输出 JSON 数组,每个元素:\n"
        '{"dimension": "约束变化|输入规模|性能要求|错误处理|功能扩展", "title": str, '
        '"task": str, "output_spec": str, "constraints": [str], "hints": [str]}。\n'
        "task 与 output_spec 必须非空且具体;至少覆盖 2 个不同维度。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_variants(raw: object, count: int) -> list[dict]:
    """容错解析:接受 数组 / {"variants": [...]} / 单个对象;校验并裁剪到 count。"""
    if isinstance(raw, dict):
        items = raw.get("variants") or raw.get("items")
        if items is None:
            items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("LLM 返回的变体不是数组")
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(Variant.model_validate(item).model_dump())
        if len(out) >= count:
            break
    if not out:
        raise ValueError("LLM 未返回任何有效变体")
    return out


def generate_variants(exam: dict, count: int = 3, config: LLMConfig | None = None,
                      base_url: str | None = None, api_key: str | None = None,
                      model: str | None = None) -> list[dict]:
    """生成变体并写入 exam["variants"],返回变体列表。"""
    if count <= 0:
        exam["variants"] = []
        return []
    if config is None:
        config = resolve_config(base_url, api_key, model)
    messages = build_variants_prompt(exam, count)
    last_err: Exception | None = None
    for _attempt in range(2):  # 校验失败重试一次(调用失败已由 chat_json 内部重试)
        try:
            raw = chat_json(config, messages)
            variants = _parse_variants(raw, count)
            break
        except (ValidationError, ValueError) as e:
            last_err = e
    else:
        raise LLMError(f"变体生成失败(LLM 输出未通过校验): {last_err}")
    exam["variants"] = variants
    return variants


def add_variants(exam_path: str | Path, count: int = 3, base_url: str | None = None,
                 api_key: str | None = None, model: str | None = None,
                 config: LLMConfig | None = None) -> list[dict]:
    """读试题 → 生成变体 → 写回 variants 数组(CLI `exam variants` 用)。"""
    path = Path(exam_path)
    exam = json.loads(path.read_text(encoding="utf-8"))
    variants = generate_variants(exam, count=count, config=config,
                                 base_url=base_url, api_key=api_key, model=model)
    path.write_text(json.dumps(exam, ensure_ascii=False, indent=2), encoding="utf-8")
    return variants
