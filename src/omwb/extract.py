"""正文提取: trafilatura 净化 → XML → 自有 Markdown 转换 (代码块/表格/层级可控)。

trafilatura 拿不到正文时(SPA 外壳等),按适配器选择器取子树重试;
仍为空则由调用方触发浏览器渲染兜底。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import trafilatura
from lxml import etree, html as lxml_html

from .adapters import Adapter

_TITLE_TAIL_RE = re.compile(r"\s*(?:[|\u00b7\u2013\u2014:-])\s*[^|\u00b7\u2013\u2014:-]{1,40}$")
_HI_REND = {"b": ("**", "**"), "i": ("*", "*"), "code": ("`", "`"), "strike": ("~~", "~~")}


@dataclass
class ExtractResult:
    markdown: str = ""
    toc: list[dict] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    title: str = ""
    raw_title: str = ""


def _title_of(html_text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
    if not m:
        return ""
    title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return html.unescape(title)


def _clean_title(title: str) -> str:
    """去掉 " | 站点名" / " - 站点名" 尾巴。

    剥尾条件:尾段 ≤ 40 字符,且(多段分隔符 / 尾段比首段短 / 首段很短)。
    避免误伤 "安装 | 配置" 这类真标题(单分隔、短尾段、首段不短)。
    """
    if not title:
        return ""
    segs = re.split(r"\s*[|\u00b7\u2013\u2014:-]\s*", title)
    if len(segs) > 1 and len(segs[-1]) <= 40:
        if len(segs) >= 3 or len(segs[-1]) < len(segs[0]) or (
            len(segs[0]) <= 12 and len(segs[-1]) > len(segs[0]) * 1.5
        ):
            return segs[0].strip()
    return title


def _render_inline(el) -> str:
    """XML 行内节点 → markdown 行内文本。"""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        tag = child.tag
        if tag == "ref":
            target = child.get("target") or ""
            text = _render_inline(child).strip() or target
            if target:
                parts.append(f"[{text}]({target})")
            else:
                parts.append(text)
        elif tag == "code":
            parts.append(f"`{child.text or ''}`")
        elif tag == "hi":
            rend = (child.get("rend") or "").lstrip("#")
            open_, close = _HI_REND.get(rend, ("", ""))
            parts.append(open_ + _render_inline(child) + close)
        elif tag == "del":
            parts.append("~~" + _render_inline(child) + "~~")
        elif tag == "lb":
            parts.append("\n")
        elif tag == "graphic":
            continue
        else:
            parts.append(_render_inline(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _render_table(el) -> str:
    rows: list[list[str]] = []
    for row in el.iter("row"):
        cells = [re.sub(r"\s*\n\s*", " ", _render_inline(c)).strip() for c in row.iter("cell")]
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = []
    for i, row in enumerate(rows):
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |")
        if i == 0:
            lines.append("| " + " | ".join("---" for _ in row) + " |")
    return "\n".join(lines)


def _render_list(el, depth: int = 0) -> str:
    ordered = el.get("type") == "ol"
    lines: list[str] = []
    for i, item in enumerate(el.iterchildren("item")):
        prefix = f"{i + 1}. " if ordered else "- "
        text = _render_inline(item).strip()
        lines.append("  " * depth + prefix + text)
        for sub in item.iterchildren("list"):
            lines.append(_render_list(sub, depth + 1))
    return "\n".join(lines)


_PILCROW_RE = re.compile(r"[\u00b6\u00a7\u200b]+\s*$")


def _render_codeblock(el) -> str:
    code = "".join(el.itertext())
    code = re.sub(r"^\n+", "", code)
    code = re.sub(r"\n+$", "", code)
    return "```\n" + code + "\n```"


def xml_to_markdown(xml_str: str) -> tuple[str, list[dict]]:
    """trafilatura XML → (markdown, toc)。

    注意:trafilatura 的代码块是含换行的 <code> 直接挂在 main 下,
    行内代码则是无换行的 <code>。
    """
    root = etree.fromstring(xml_str.encode("utf-8"))
    main = root.find("main")
    if main is None:
        main = root
    out: list[str] = []
    toc: list[dict] = []

    def walk(el) -> None:
        for child in el:
            tag = child.tag
            if tag == "head":
                rend = child.get("rend", "h2")
                level = int(rend[1]) if rend.startswith("h") and rend[1:].isdigit() else 2
                title = _PILCROW_RE.sub("", _render_inline(child)).strip()
                toc.append({"level": level, "title": title, "anchor": ""})
                out.append("\n" + "#" * level + " " + title + "\n")
            elif tag == "p":
                t = _render_inline(child).strip()
                if t:
                    out.append(t + "\n\n")
            elif tag == "code" and "\n" in (child.text or ""):
                out.append(_render_codeblock(child) + "\n\n")
            elif tag == "list":
                out.append(_render_list(child) + "\n\n")
            elif tag == "table":
                t = _render_table(child)
                if t:
                    out.append(t + "\n\n")
            elif tag == "codeblock":
                out.append(_render_codeblock(child) + "\n\n")
            elif tag == "quote":
                text = _render_inline(child).strip()
                if text:
                    out.append("> " + text.replace("\n", "\n> ") + "\n\n")
            elif tag in ("graphic", "formula", "time"):
                continue
            else:
                walk(child)

    walk(main)
    md = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
    return md, toc


def _fallback_extract(html_text: str, adapter: Adapter) -> str | None:
    """适配器选择器定位正文子树后重新交给 trafilatura。

    最后兜底:子树文本直接使用(纯链接列表页等 trafilatura 判空的场景)。
    """
    try:
        doc = lxml_html.fromstring(html_text)
    except (ValueError, etree.ParserError):
        return None
    for sel in adapter.selectors:
        try:
            nodes = doc.cssselect(sel)
        except Exception:
            nodes = []
        if not nodes:
            continue
        subtree = lxml_html.tostring(nodes[0], encoding="unicode")
        out = trafilatura.extract(subtree, output_format="markdown", include_tables=True,
                                  include_formatting=True, favor_precision=True)
        if out and out.strip():
            return out
        text = re.sub(r"\s*\n\s*", "\n", nodes[0].text_content()).strip()
        if len(text) >= 50:
            return text
    return None


def extract_page(html_text: str, url: str, adapter: Adapter) -> ExtractResult:
    """提取正文;返回 markdown + TOC + 元信息。markdown 为空表示提取失败(可能是 SPA)。"""
    raw_title = _title_of(html_text)
    xml_str = trafilatura.extract(
        html_text,
        url=url,
        output_format="xml",
        include_comments=False,
        include_tables=True,
        include_formatting=True,
        favor_precision=True,
        with_metadata=True,
    )
    if xml_str:
        try:
            md, toc = xml_to_markdown(xml_str)
        except etree.XMLSyntaxError:
            md, toc = "", []
        if md:
            meta = {"url": url}
            m = re.search(r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"']([^\"']+)", html_text, re.I)
            if m:
                meta["description"] = m.group(1)[:300]
            return ExtractResult(markdown=md, toc=toc, meta=meta, title=_clean_title(raw_title), raw_title=raw_title)
    fallback = _fallback_extract(html_text, adapter)
    if fallback:
        return ExtractResult(markdown=fallback, meta={"url": url}, title=_clean_title(raw_title), raw_title=raw_title)
    return ExtractResult(title=_clean_title(raw_title), raw_title=raw_title)


def looks_like_spa(html_text: str) -> bool:
    """外壳特征:存在 JS 渲染容器。提取为空 + 此特征 → 需要浏览器渲染。"""
    markers = ('id="app"', 'id="root"', "__NUXT__", "__NEXT_DATA__", "window.__INITIAL_STATE__")
    return any(m in html_text for m in markers)
