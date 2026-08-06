"""站点配置:YAML 配置文件 + 单站点 CLI 参数,统一为 SiteConfig。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

VALID_FORMATS = {"md", "json", "jsonl", "pdf", "all"}

DEFAULT_EXCLUDE = [
    "/search", "/tags", "/categories", "/feed", "/rss", "/atom",
    "/404", "/login", "/signup", "/account", "/admin", "/logout",
    "/_sources", "/_static", "/objects.inv", "/genindex", "/py-modindex",
    "/~gitbook", "/sitemap",
]


class SiteConfig(BaseModel):
    url: str
    name: str | None = None
    adapter: str = "auto"                      # auto|generic|llmstxt|docusaurus|gitbook|readthedocs|mkdocs|sphinx|vuepress
    formats: list[str] = Field(default_factory=lambda: ["md", "json"])
    include: list[str] = Field(default_factory=list)      # URL 路径 glob,命中才保留
    exclude: list[str] = Field(default_factory=list)      # URL 路径 glob,命中剔除 (叠加在 DEFAULT_EXCLUDE)
    max_pages: int = 0                         # 0 = 不限
    max_depth: int = 0                         # BFS 深度,0 = 不限
    concurrency: int = 8
    delay: float = 0.25                        # 同主机请求间隔(秒)
    timeout: float = 30.0
    js_render: bool = False                    # 强制浏览器渲染(SPA 文档站)
    js_fallback: bool = True                   # 提取为空时自动尝试浏览器渲染
    combined_pdf: bool = False                 # 额外产出一份合并 PDF

    @field_validator("formats")
    @classmethod
    def _check_formats(cls, v: list[str]) -> list[str]:
        for f in v:
            if f not in VALID_FORMATS:
                raise ValueError(f"未知格式: {f} (可选: {', '.join(sorted(VALID_FORMATS))})")
        if "all" in v:
            return ["md", "json", "jsonl", "pdf"]
        return v

    @property
    def site_name(self) -> str:
        if self.name:
            return self.name
        host = self.url.split("://", 1)[-1].split("/", 1)[0]
        return host.replace(":", "_")

    def path_allowed(self, url_path: str) -> bool:
        """URL 路径过滤:include 命中才放行;exclude/DEFAULT_EXCLUDE 命中剔除。"""
        from fnmatch import fnmatch

        if self.include:
            if not any(fnmatch(url_path, p) for p in self.include):
                return False
        for pat in [*DEFAULT_EXCLUDE, *self.exclude]:
            if fnmatch(url_path, pat):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


def load_sites(path: Path) -> list[SiteConfig]:
    """解析 YAML 配置 (sites: 列表)。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "sites" not in raw:
        raise ValueError(f"配置 {path} 缺少顶层 'sites' 列表")
    return [SiteConfig.model_validate(s) for s in raw["sites"]]


def site_from_args(
    url: str,
    name: str | None,
    formats: list[str] | None,
    adapter: str,
    include: list[str] | None,
    exclude: list[str] | None,
    max_pages: int,
    concurrency: int,
    delay: float,
    js_render: bool,
    combined_pdf: bool,
) -> SiteConfig:
    return SiteConfig(
        url=url,
        name=name,
        adapter=adapter,
        formats=formats or ["md", "json"],
        include=include or [],
        exclude=exclude or [],
        max_pages=max_pages,
        concurrency=concurrency,
        delay=delay,
        js_render=js_render,
        combined_pdf=combined_pdf,
    )
