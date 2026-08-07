"""试题生成:语料选材 → LLM 出题(含 concepts 教学字段)→ pydantic 校验 → 自动变体 → 写回。

输出:omwb-out/<site>/exam/exam-<id>.json,id = topic 短名 + 时间戳。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..pipeline import approx_tokens, chunk_markdown
from .llm import LLMConfig, LLMError, chat_json, resolve_config
from .prompts import TEACHING_RULES

MAX_CHUNKS = 6          # 选材条数上限
MAX_SOURCE_TOKENS = 4000  # 选材总 token 预算
DEFAULT_LEVELS = ("basic", "medium", "hard")


class Concept(BaseModel):
    """题目核心概念教学条目:做题前先看概念讲解。"""

    term: str = ""
    plain_explanation: str = ""   # 白话解释
    analogy: str = ""             # 类比或示例
    why_it_matters: str = ""      # 为什么重要


class Exam(BaseModel):
    """开放性试题结构(task/output_spec 必填非空)。"""

    id: str = ""
    site: str = ""
    topic: str = ""
    level: str = "medium"
    title: str | None = None
    task: str = Field(..., min_length=1)
    output_spec: str = Field(..., min_length=1)
    constraints: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    concepts: list[Concept] = Field(default_factory=list)
    source_chunks: list[dict] = Field(default_factory=list)
    variants: list[dict] = Field(default_factory=list)
    created_at: str = ""

    @field_validator("task", "output_spec")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空")
        return v

    def to_json(self) -> dict:
        d = self.model_dump()
        d["concepts"] = [c.model_dump() for c in self.concepts]
        return d


def load_corpus(site_dir: Path) -> list[dict]:
    """读站点语料:优先 corpus.jsonl,缺失时回退 corpus.json(逐页按标题分块)。"""
    jsonl = site_dir / "corpus.jsonl"
    if jsonl.is_file():
        out: list[dict] = []
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
        return out
    j = site_dir / "corpus.json"
    if j.is_file():
        data = json.loads(j.read_text(encoding="utf-8"))
        site = data.get("site") or site_dir.name
        out = []
        for page in data.get("pages") or []:
            md = page.get("markdown") or ""
            if not md:
                continue
            for seq, chunk in enumerate(chunk_markdown(md)):
                out.append({
                    "site": site,
                    "url": page.get("url", ""),
                    "path": page.get("path", ""),
                    "title": page.get("title", ""),
                    "seq": seq,
                    "heading_path": chunk["heading_path"],
                    "tokens": chunk["tokens"],
                    "text": chunk["text"],
                })
        return out
    raise FileNotFoundError(f"站点目录 {site_dir} 缺少 corpus.jsonl / corpus.json,请先抓取该站点")


def _score_chunk(entry: dict, topic: str) -> int:
    """topic 关键词(大小写不敏感)在 title/heading_path/text 前缀的命中打分。"""
    kw = topic.lower()
    score = 0
    if kw in (entry.get("title") or "").lower():
        score += 3
    if kw in " ".join(entry.get("heading_path") or []).lower():
        score += 2
    if kw in (entry.get("text") or "")[:400].lower():
        score += 1
    return score


def _truncate_to(text: str, max_tokens: int) -> str:
    """按 approx_tokens 估算截断文本前缀,保证估算 token ≤ max_tokens。"""
    if approx_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if approx_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "\n…(语料超出预算已截断)"


def select_chunks(corpus: list[dict], topic: str, max_chunks: int = MAX_CHUNKS,
                  max_tokens: int = MAX_SOURCE_TOKENS) -> list[dict]:
    """按 topic 打分排序取相关 chunk,控制条数与总 token(超出截断末条)。"""
    ranked = sorted((e for e in corpus if _score_chunk(e, topic) > 0),
                    key=lambda e: _score_chunk(e, topic), reverse=True)
    picked: list[dict] = []
    total = 0
    for entry in ranked:
        if total >= max_tokens or len(picked) >= max_chunks:
            break
        n = int(entry.get("tokens") or 0)
        if total + n > max_tokens:
            entry = {**entry, "text": _truncate_to(entry.get("text") or "", max_tokens - total)}
        picked.append(entry)
        total += approx_tokens(entry["text"])
    return picked


def build_generate_prompt(site: str, topic: str, level: str, chunks: list[dict]) -> list[dict]:
    """组装出题 prompt:语料素材 + 试题 JSON schema(含 concepts 教学字段)。"""
    system = (
        "你是一名资深编程教育专家,擅长把真实文档知识改编为开放性编程试题。"
        "只依据提供的语料素材出题,不编造语料中没有的知识。"
        f"\n\n{TEACHING_RULES}"
    )
    material = []
    for i, c in enumerate(chunks, 1):
        material.append(
            f"[素材 {i}] url: {c.get('url', '')}\n"
            f"标题: {c.get('title', '')}\n"
            f"章节路径: {' > '.join(c.get('heading_path') or [])}\n"
            f"内容:\n{c.get('text', '')}"
        )
    user = (
        f"站点:{site}\n主题:{topic}\n难度:{level}\n\n"
        "以下是该主题相关的文档语料:\n\n"
        + "\n\n".join(material)
        + "\n\n请基于以上语料生成 1 道开放性编程试题,严格输出 JSON 对象,字段:\n"
        '{"title": str 题目标题, "task": str 实现什么(功能需求描述), '
        '"output_spec": str 要求输出什么(输入输出规格、验收标准、边界情况), '
        '"constraints": [str] 约束(算法复杂度上限/禁用第三方库/语言要求等), '
        '"hints": [str] 提示(可给出文档线索), '
        '"level": "basic"|"medium"|"hard", '
        '"concepts": [{"term": str 核心概念, "plain_explanation": str 白话解释, '
        '"analogy": str 生活类比或数字示例, "why_it_matters": str 为什么重要}]}。\n'
        "task 与 output_spec 必须非空且足够具体,可直接验收;concepts 给 3~5 条。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _topic_slug(topic: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", topic.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:24] or "topic"


def _exam_id(topic: str) -> str:
    return f"{_topic_slug(topic)}-{time.strftime('%Y%m%d-%H%M%S')}"


def _build_exam(raw: dict, *, site: str, topic: str, level: str, chunks: list[dict]) -> Exam:
    """LLM 原始输出 + 本地元数据 → Exam 模型(source_chunks 来自实际选材,保证可溯源)。"""
    if not isinstance(raw, dict):
        raise ValueError("LLM 返回的试题不是 JSON 对象")
    raw_level = raw.get("level") or level
    if raw_level not in DEFAULT_LEVELS:
        raw_level = level
    concepts = []
    for c in raw.get("concepts") or []:
        if isinstance(c, dict):
            concepts.append(Concept.model_validate(c))
    return Exam(
        id=_exam_id(topic),
        site=site,
        topic=topic,
        level=raw_level,
        title=raw.get("title"),
        task=raw.get("task", ""),
        output_spec=raw.get("output_spec", ""),
        constraints=raw.get("constraints") or [],
        hints=raw.get("hints") or [],
        concepts=concepts,
        source_chunks=[
            {
                "url": c.get("url", ""),
                "path": c.get("path", ""),
                "heading_path": c.get("heading_path") or [],
                "title": c.get("title", ""),
            }
            for c in chunks
        ],
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def generate_exam(site: str, topic: str, level: str = "medium", out: str | Path = "omwb-out",
                  variants: int = 3, base_url: str | None = None, api_key: str | None = None,
                  model: str | None = None, config: LLMConfig | None = None) -> dict:
    """生成开放性试题(含 concepts 与自动变体),写回 omwb-out/<site>/exam/,返回试题 dict。"""
    if config is None:
        config = resolve_config(base_url, api_key, model)
    site_dir = Path(out) / site
    corpus = load_corpus(site_dir)
    chunks = select_chunks(corpus, topic)
    if not chunks:
        raise ValueError(f"站点 {site} 的语料中没有匹配主题「{topic}」的内容,换个关键词试试")
    messages = build_generate_prompt(site, topic, level, chunks)
    last_err: Exception | None = None
    for _attempt in range(2):  # 校验失败重试一次(调用失败已由 chat_json 内部重试)
        try:
            raw = chat_json(config, messages)
            exam = _build_exam(raw, site=site, topic=topic, level=level, chunks=chunks)
            break
        except ValidationError as e:
            last_err = e
        except ValueError as e:
            last_err = e
    else:
        raise LLMError(f"试题生成失败(LLM 输出未通过校验): {last_err}")
    exam_dict = exam.to_json()
    if variants > 0:
        from .variants import generate_variants
        exam_dict["variants"] = generate_variants(exam_dict, count=variants, config=config)
    exam_dir = site_dir / "exam"
    exam_dir.mkdir(parents=True, exist_ok=True)
    path = exam_dir / f"exam-{exam.id}.json"
    path.write_text(json.dumps(exam_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    return exam_dict
