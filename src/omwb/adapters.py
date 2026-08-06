"""文档平台适配器:检测站点平台,提供 URL 过滤规则与提取提示。

平台签名来自 <meta name="generator"> 与常见 DOM 结构;adapter=auto 时按此检测,
显式指定时直接使用。generic 为通用回退。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 各平台已知的噪音路径 (URL 路径子串匹配)
PLATFORM_NOISE: dict[str, tuple[str, ...]] = {
    "sphinx": ("/_sources/", "/_static/", "/genindex", "/py-modindex", "/search", "/objects.inv", "/.doctrees"),
    "readthedocs": ("/_sources/", "/_static/", "/genindex", "/py-modindex", "/search", "/objects.inv", "/.doctrees"),
    "docusaurus": ("/search", "/tags/", "/assets/", "/img/"),
    "mkdocs": ("/search.html", "/404.html", "/sitemap.xml"),
    "gitbook": ("/~gitbook/", "/search"),
    "vuepress": ("/assets/", "/search"),
    "hugo": ("/tags/", "/categories/", "/categories/"),
}

# 各平台正文提取的回退选择器 (trafilatura 失效时使用)
PLATFORM_SELECTORS: dict[str, tuple[str, ...]] = {
    "docusaurus": ("article", "main .theme-doc-markdown", ".markdown"),
    "gitbook": ("main", ".book-body .page-inner", ".markdown-section"),
    "readthedocs": ("[role='main']", "div.document", ".body"),
    "sphinx": ("[role='main']", "div.document", ".body"),
    "mkdocs": ("[role='main']", "article", ".md-content__inner"),
    "vuepress": ("main", ".theme-default-content", ".content__default"),
    "hugo": ("article", "main", ".content"),
}

_GENERATOR_RE = re.compile(r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']([^\"']+)", re.I)


@dataclass
class Adapter:
    name: str
    noise_substrings: tuple[str, ...] = field(default_factory=tuple)
    selectors: tuple[str, ...] = field(default_factory=tuple)


ADAPTERS: dict[str, Adapter] = {
    "generic": Adapter("generic"),
    "llmstxt": Adapter("llmstxt"),
    "sphinx": Adapter("sphinx", PLATFORM_NOISE["sphinx"], PLATFORM_SELECTORS["sphinx"]),
    "readthedocs": Adapter("readthedocs", PLATFORM_NOISE["readthedocs"], PLATFORM_SELECTORS["readthedocs"]),
    "docusaurus": Adapter("docusaurus", PLATFORM_NOISE["docusaurus"], PLATFORM_SELECTORS["docusaurus"]),
    "mkdocs": Adapter("mkdocs", PLATFORM_NOISE["mkdocs"], PLATFORM_SELECTORS["mkdocs"]),
    "gitbook": Adapter("gitbook", PLATFORM_NOISE["gitbook"], PLATFORM_SELECTORS["gitbook"]),
    "vuepress": Adapter("vuepress", PLATFORM_NOISE["vuepress"], PLATFORM_SELECTORS["vuepress"]),
    "hugo": Adapter("hugo", PLATFORM_NOISE["hugo"], PLATFORM_SELECTORS["hugo"]),
}


def detect_adapter(html: str, url: str) -> str:
    """从 HTML 特征检测文档平台;返回适配器名,未知返回 'generic'。"""
    head = html[:4000]
    m = _GENERATOR_RE.search(head)
    gen = (m.group(1) if m else "").lower()
    if "docusaurus" in gen:
        return "docusaurus"
    if "gitbook" in gen:
        return "gitbook"
    if "mkdocs" in gen:
        return "mkdocs"
    if "sphinx" in gen:
        return "readthedocs" if "readthedocs" in url.lower() or "rtd" in url.lower() else "sphinx"
    if "vuepress" in gen:
        return "vuepress"
    if "hugo" in gen:
        return "hugo"
    if "jupyter-book" in gen:
        return "sphinx"
    # 无 generator 时的 DOM 特征
    if "readthedocs" in head or "readthedocs" in url.lower():
        return "readthedocs"
    if 'class="theme-doc-markdown"' in head or "theme-doc-" in head:
        return "docusaurus"
    if 'class="book-body"' in head or 'data-type="book"' in head:
        return "gitbook"
    if 'class="rst-content"' in head or 'class="wy-nav-content"' in head:
        return "readthedocs"
    if 'class="md-content__inner"' in head:
        return "mkdocs"
    if 'class="theme-default-content"' in head:
        return "vuepress"
    if "data-content_root" in head or 'class="document"' in head or "[role='main']" in head:
        return "sphinx"
    return "generic"


def get_adapter(name: str) -> Adapter:
    return ADAPTERS.get(name, ADAPTERS["generic"])


def is_platform_noise(adapter: Adapter, url_path: str) -> bool:
    return any(s in url_path for s in adapter.noise_substrings)
