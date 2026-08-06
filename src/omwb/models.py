"""核心数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Page:
    """单页抓取结果。"""

    url: str
    rel_path: str              # 由 URL 派生的相对输出路径 (无扩展名)
    title: str = ""
    status: int = 0
    html: str = ""             # 原始 HTML
    markdown: str = ""         # 提取后的正文 markdown
    toc: list[dict[str, Any]] = field(default_factory=list)  # [{level,title,anchor}]
    meta: dict[str, str] = field(default_factory=dict)       # description/date/...
    error: str = ""


@dataclass
class SiteResult:
    """一个站点抓取+转换的总结果。"""

    name: str
    url: str
    adapter: str
    out_dir: str
    pages: list[Page] = field(default_factory=list)
    failed: list[Page] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def ok_count(self) -> int:
        return len(self.pages)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "adapter": self.adapter,
            "out_dir": self.out_dir,
            "pages": self.ok_count,
            "failed": len(self.failed),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
