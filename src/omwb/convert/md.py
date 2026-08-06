"""Markdown 输出:每页一个 .md,带 YAML frontmatter (title/url/来源)。"""

from __future__ import annotations

from pathlib import Path

from ..models import Page, SiteResult
from . import out_file

FRONTMATTER = "---\ntitle: {title}\nurl: {url}\nsource: {source}\n---\n\n"


def write_md(page: Page, out_dir: Path, source: str) -> Path:
    out = out_file(page, out_dir, "md")
    fm = FRONTMATTER.format(
        title=page.title.replace("\n", " ").replace(":", " -").replace("---", "—"),
        url=page.url,
        source=source,
    )
    out.write_text(fm + page.markdown + "\n", encoding="utf-8")
    return out


def write_all_md(result: SiteResult, out_dir: Path) -> int:
    n = 0
    for page in result.pages:
        if page.markdown:
            write_md(page, out_dir, result.url)
            n += 1
    return n
