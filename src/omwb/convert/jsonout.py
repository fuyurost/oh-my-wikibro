"""JSON 输出:每页一个结构化 .json + 整站 corpus.json (知识库输入)。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..models import Page, SiteResult
from . import out_file


def _page_dict(page: Page) -> dict:
    return {
        "url": page.url,
        "path": page.rel_path,
        "title": page.title,
        "toc": page.toc,
        "meta": page.meta,
        "markdown": page.markdown,
    }


def write_json(page: Page, out_dir: Path) -> Path:
    out = out_file(page, out_dir, "json")
    out.write_text(json.dumps(_page_dict(page), ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def write_all_json(result: SiteResult, out_dir: Path) -> tuple[int, Path]:
    """逐页 json 写入 out_dir/json/,整站 corpus.json 写入 out_dir/。"""
    json_dir = out_dir / "json"
    n = 0
    for page in result.pages:
        if page.markdown:
            write_json(page, json_dir)
            n += 1
    corpus = out_dir / "corpus.json"
    corpus.write_text(
        json.dumps(
            {
                "site": result.name,
                "url": result.url,
                "adapter": result.adapter,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "pages": [_page_dict(p) for p in result.pages if p.markdown],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return n, corpus
