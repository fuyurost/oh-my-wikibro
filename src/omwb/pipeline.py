"""LLM 语料管道:按标题层级分块 + 重叠 + corpus.jsonl / corpus.md 输出。

分块策略:标题切块(块内保留标题行作为上下文)→ 贪心打包到 max_tokens →
超长块按段落/句子逐级切分;相邻 chunk 首尾重叠 overlap tokens。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import SiteResult

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# CJK 句读后无条件切(中文无空格);拉丁句点要求后随空白(避免切坏 URL/小数)
_SENT_RE = re.compile(r"(?<=[。！？])(?=\S)|(?<=[.!?])\s+")
_WORD_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
    r"|[^\s\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+|\s+"
)


def approx_tokens(text: str) -> int:
    """启发式 token 估算:CJK 字符 ≈ 1 token,其他 ≈ 4 字符/token。"""
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + ((other + 3) // 4 if other else 0)


@dataclass
class _Block:
    heading_path: list[str]
    text: str


def _split_blocks(markdown: str) -> list[_Block]:
    """按标题行切块;块文本保留标题行。"""
    blocks: list[_Block] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    path: list[str] = []

    def flush() -> None:
        if buf:
            blocks.append(_Block(heading_path=list(path), text="\n".join(buf).strip()))
            buf.clear()

    for line in markdown.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path = [t for _, t in stack]
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return blocks


def _hard_slice(text: str, max_tokens: int) -> list[str]:
    """按 token 估算硬切(保留空白);仅作为段落/句子切分失效时的兜底。"""
    toks = _WORD_RE.findall(text)
    out: list[str] = []
    cur: list[str] = []
    count = 0
    for t in toks:
        t_est = 1 if (len(t) == 1 and _CJK_RE.match(t)) else (len(t) + 3) // 4
        if count + t_est > max_tokens and cur:
            out.append("".join(cur))
            cur, count = [], 0
        cur.append(t)
        count += t_est
    if cur:
        out.append("".join(cur))
    return out or [text]


def _split_oversized(text: str, max_tokens: int) -> list[str]:
    """超长文本逐级切:段落 → 句子 → 硬切。"""
    if approx_tokens(text) <= max_tokens:
        return [text]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    cur = ""
    for para in paras:
        if approx_tokens(para) > max_tokens:
            if cur:
                out.append(cur.strip())
                cur = ""
            parts = [s for s in _SENT_RE.split(para) if s.strip()]
            buf = ""
            for s in parts:
                if approx_tokens(buf + s) > max_tokens:
                    if buf:
                        out.append(buf.strip())
                    buf = s
                else:
                    buf += s
            if approx_tokens(buf) > max_tokens:
                out.extend(_hard_slice(buf, max_tokens))
            elif buf:
                out.append(buf.strip())
            continue
        if approx_tokens(cur + "\n\n" + para) > max_tokens:
            out.append(cur.strip())
            cur = para
        else:
            cur = (cur + "\n\n" + para).strip()
    if cur:
        out.append(cur.strip())
    return out


_TOKEN_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]|[A-Za-z0-9_]+|."
)


def _tail_tokens(text: str, overlap: int) -> str:
    if not overlap or not text:
        return ""
    return "".join(_TOKEN_RE.findall(text)[-overlap:])


def chunk_markdown(markdown: str, max_tokens: int = 1500, overlap: int = 120) -> list[dict]:
    """整页 markdown → chunk 列表 [{heading_path, tokens, text}]。"""
    blocks = _split_blocks(markdown)
    chunks: list[dict] = []
    cur_text = ""
    cur_path: list[str] = []
    tail = ""

    def push() -> None:
        nonlocal tail
        if cur_text:
            chunks.append({"heading_path": cur_path, "tokens": approx_tokens(cur_text), "text": cur_text.strip()})
            tail = _tail_tokens(cur_text, overlap)

    for block in blocks:
        bt = approx_tokens(block.text)
        if bt > max_tokens:
            push()
            prev_tail = tail
            pieces = _split_oversized(block.text, max_tokens - overlap)
            for piece in pieces:
                prefix = prev_tail if prev_tail else ""
                chunks.append({
                    "heading_path": block.heading_path,
                    "tokens": approx_tokens((prefix + "\n\n" if prefix else "") + piece),
                    "text": (prefix + "\n\n" if prefix else "") + piece,
                })
                prev_tail = _tail_tokens(piece, overlap)
            tail = prev_tail
            cur_text = ""
            continue
        if cur_text and approx_tokens(cur_text + "\n\n" + block.text) > max_tokens:
            push()
            cur_text = (tail + "\n\n" if tail else "") + block.text
        else:
            cur_text = (cur_text + "\n\n" + block.text).strip()
        cur_path = block.heading_path
    push()
    return chunks


def write_corpus_jsonl(result: SiteResult, out_dir: Path, max_tokens: int = 1500,
                       overlap: int = 120) -> tuple[int, Path]:
    out = out_dir / "corpus.jsonl"
    n = 0
    with out.open("w", encoding="utf-8") as f:
        for page in result.pages:
            if not page.markdown:
                continue
            for seq, chunk in enumerate(chunk_markdown(page.markdown, max_tokens, overlap)):
                f.write(json.dumps({
                    "site": result.name,
                    "url": page.url,
                    "path": page.rel_path,
                    "title": page.title,
                    "seq": seq,
                    "heading_path": chunk["heading_path"],
                    "tokens": chunk["tokens"],
                    "text": chunk["text"],
                }, ensure_ascii=False) + "\n")
                n += 1
    return n, out


def write_corpus_md(result: SiteResult, out_dir: Path) -> Path:
    out = out_dir / "corpus.md"
    parts = []
    for page in result.pages:
        if not page.markdown:
            continue
        parts.append(f"# {page.title}\n\n来源: {page.url}\n\n{page.markdown}")
    out.write_text("\n\n---\n\n".join(parts) + "\n", encoding="utf-8")
    return out
