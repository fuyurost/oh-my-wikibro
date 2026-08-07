"""转换层:页面级输出路径 + 各格式写出。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from ..models import Page

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_EXT_STRIP = re.compile(r"\.(html?|md|xhtml|txt)$", re.I)


def _safe(part: str) -> str:
    part = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part).strip().rstrip(". ")
    if not part:
        part = "index"
    if part.upper() in _WINDOWS_RESERVED:
        part = "_" + part
    if len(part) > 180:
        part = part[:180]
    return part


def page_rel_path(url: str) -> str:
    """URL → 相对输出路径 (无扩展名)。镜像站点目录结构。"""
    parts = urlsplit(url)
    path = _EXT_STRIP.sub("", parts.path)
    segs = [p for p in path.split("/") if p]
    if len(segs) > 1 and segs[-1].lower() in ("index", "index.htm"):
        segs.pop()
    if not segs:
        segs = ["index"]
    return "/".join(_safe(s) for s in segs)


def out_file(page: Page, out_dir: Path, ext: str) -> Path:
    p = out_dir / f"{page.rel_path}.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def rel_out_file(page: Page, ext: str) -> str:
    """页面在站点输出目录内的相对路径 (如 md/docs/intro.md)。"""
    return f"{ext}/{page.rel_path}.{ext}"
