"""URL 发现:llms.txt → sitemap (robots.txt / sitemap.xml / sitemap index) → 通用 BFS。

优先级即顺序:llms.txt 是官方为 LLM 准备的入口,最优;sitemap 覆盖全量;
BFS 兜底任意站点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from lxml import etree, html

from .adapters import Adapter, is_platform_noise
from .config import SiteConfig

UA = "oh-my-wikibro/0.1 (+https://github.com/oh-my-wikibro; offline-docs crawler)"

# BFS/通用抓取时剔除的静态资源扩展名
DROP_EXTENSIONS = {
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".svg", ".ico", ".css", ".js", ".woff", ".woff2", ".mp4",
    ".webm", ".mp3", ".exe", ".msi", ".dmg", ".iso", ".rss", ".xml", ".json",
    ".yaml", ".yml", ".md", ".txt", ".csv", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx",
}
# BFS 中常见的无内容路径片段
BFS_NOISE = ("/search", "/login", "/signup", "/account", "/cart", "/checkout",
             "/feed", "/rss", "/atom", "/wp-", "/xmlrpc", "/trackback", "/privacy", "/terms")
# sitemap 中保留查询参数的键
KEEP_QUERY_KEYS = {"hl", "lang", "version"}
MAX_SITEMAP_FILES = 60  # sitemap index 递归上限

_LLMS_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


@dataclass
class Discovery:
    urls: list[str]
    source: str            # llms.txt | sitemap | bfs
    llms_full: str | None = None   # 若命中 llms-full.txt,整站内容直接在此


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA})


def normalize_url(url: str) -> str | None:
    """规范化:去 fragment、去多余查询、去默认端口、去掉 index.html 尾巴。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = parts.netloc.lower()
    if host.endswith(":80") and parts.scheme == "http":
        host = host[:-3]
    elif host.endswith(":443") and parts.scheme == "https":
        host = host[:-4]
    path = parts.path or "/"
    path = re.sub(r"/index\.(html?|md)$", "/", path)
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = ""
    if parts.query:
        keep = [f"{k}={v}" for k, v in [kv.split("=", 1) for kv in parts.query.split("&") if "=" in kv]
                if k in KEEP_QUERY_KEYS and v]
        query = "&".join(keep)
    return urlunsplit((parts.scheme, host, path, query, ""))


def _ext_path(url: str) -> str:
    path = urlsplit(url).path
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def _is_same_host(url: str, origin: str) -> bool:
    return urlsplit(url).netloc.lower() == origin.lower()


def _origin_of(url: str) -> str:
    p = urlsplit(url)
    return p.netloc.lower()


def _check(url: str, site: SiteConfig, adapter: Adapter, *, allow_md: bool = False) -> bool:
    """发现结果的通用过滤:扩展名、平台噪音、用户 include/exclude。

    同源过滤由各发现策略自行保证(它们持有 origin 上下文)。
    """
    parts = urlsplit(url)
    if not allow_md and _ext_path(url) in DROP_EXTENSIONS:
        return False
    if is_platform_noise(adapter, parts.path):
        return False
    if any(s in parts.path for s in BFS_NOISE):
        return False
    if not site.path_allowed(parts.path):
        return False
    return True


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        n = normalize_url(u)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _fetch_text(client: httpx.Client, url: str, timeout: float) -> str | None:
    try:
        r = client.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except httpx.HTTPError:
        pass
    return None


def parse_llms_links(text: str, base: str) -> list[str]:
    """从 llms.txt 文本提取链接:markdown 链接 + 裸 URL 行。"""
    links: list[str] = []
    for m in _LLMS_LINK_RE.finditer(text):
        links.append(urljoin(base, m.group(1)))
    for line in text.splitlines():
        line = line.strip().rstrip(".")
        if line.startswith("http://") or line.startswith("https://"):
            links.append(line)
    return links


def _sort_urls(urls: list[str], site: SiteConfig) -> list[str]:
    """排序:起始 URL 前缀内的页面优先,再按路径深度与字典序。

    保证 --limit N 截取的是目标子树(教程子页)而非页脚链接(版本/其它区)。
    """
    prefix = urlsplit(site.url).path
    if prefix == "/":
        prefix = ""

    def key(u: str) -> tuple[int, int, str]:
        p = urlsplit(u).path
        in_prefix = 0 if (not prefix or p.startswith(prefix)) else 1
        return (in_prefix, p.count("/"), p)

    return sorted(urls, key=key)


def _try_llmstxt(site: SiteConfig, client: httpx.Client) -> Discovery | None:
    base = site.url.split("://", 1)[0] + "://" + _origin_of(site.url)
    text = _fetch_text(client, base + "/llms.txt", site.timeout)
    if text is None:
        return None
    links = parse_llms_links(text, base)
    if not links:
        return None
    urls = _dedupe(links)
    full = [u for u in urls if u.rsplit("/", 1)[-1].startswith("llms-full")]
    if full:
        content = _fetch_text(client, full[0], site.timeout)
        if content:
            return Discovery(urls=[full[0]], source="llms.txt", llms_full=content)
        urls = [u for u in urls if u != full[0]]
    # llms.txt 模式:保留 md 链接(它们就是内容)
    urls = [u for u in urls if _is_same_host(u, _origin_of(site.url)) and site.path_allowed(urlsplit(u).path)]
    if not urls:
        return None
    return Discovery(urls=urls, source="llms.txt")


def _sitemap_locs(client: httpx.Client, sitemap_url: str, site: SiteConfig,
                  adapter: Adapter, out: list[str], depth: int = 0) -> None:
    if depth > 3 or len(out) > 100000:
        return
    text = _fetch_text(client, sitemap_url, site.timeout)
    if not text:
        return
    try:
        root = etree.fromstring(text.encode("utf-8", "replace"))
    except etree.XMLSyntaxError:
        return
    ns = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
    locs = [el.text for el in root.iter(ns + "loc") if el.text]
    child_sitemaps = [u for u in locs if "sitemap" in u.lower() and u.lower().endswith(".xml")]
    for u in child_sitemaps[:MAX_SITEMAP_FILES]:
        _sitemap_locs(client, u, site, adapter, out, depth + 1)
    for u in locs:
        n = normalize_url(u)
        if n and _is_same_host(n, _origin_of(site.url)) and _check(n, site, adapter):
            out.append(n)


def get_adapter_for_site(site: SiteConfig) -> Adapter:
    from .adapters import get_adapter
    return get_adapter(site.adapter)


def _candidate_from_href(href: str, base_url: str, origin: str, prefix: str) -> str | None:
    """链接 → 候选 URL。

    相对链接(如 RTD 的 user/install/)会 302 归位到默认版本,前缀放宽;
    绝对同源链接按前缀过滤,避免收进其他语言/版本子树。
    """
    raw = href.strip()
    cand = normalize_url(urljoin(base_url, raw))
    if not cand or not _is_same_host(cand, origin):
        return None
    p = urlsplit(cand).path
    if raw.startswith(("http://", "https://")):
        if prefix and not p.startswith(prefix):
            return None
    elif prefix and p == "/":
        return None
    return cand


def _expand_seeds(client: httpx.Client, site: SiteConfig, adapter: Adapter,
                  seeds: list[str], limit: int = 2, per_seed: int = 5000) -> list[str]:
    """从种子页面的站内链接扩展 URL 集合。

    许多站点(ReadTheDocs / Sphinx 官方文档)的 sitemap 只列版本根,
    子页面全在根页的链接里(侧栏 TOC)。抓前 limit 个种子页收集链接。
    """
    out: list[str] = []
    origin = _origin_of(site.url)
    for url in seeds[:limit]:
        try:
            r = client.get(url, timeout=site.timeout)
            if r.status_code != 200:
                continue
            base = str(r.url)  # 重定向后 URL(带尾斜杠),urljoin 才正确
            doc = html.fromstring(r.text, base_url=base)
        except (httpx.HTTPError, ValueError, etree.ParserError):
            continue
        prefix = urlsplit(url).path
        if prefix == "/":
            prefix = ""
        n = 0
        for href in doc.xpath("//a/@href"):
            cand = _candidate_from_href(href, base, origin, prefix)
            if not cand or not _check(cand, site, adapter):
                continue
            if cand not in out and cand != url:
                out.append(cand)
                n += 1
                if n >= per_seed:
                    break
    return out


def _try_sitemap(site: SiteConfig, client: httpx.Client, adapter: Adapter) -> Discovery | None:
    base = site.url.split("://", 1)[0] + "://" + _origin_of(site.url)
    candidates: list[str] = [base + "/sitemap.xml", base + "/sitemap_index.xml"]
    robots = _fetch_text(client, base + "/robots.txt", site.timeout)
    if robots:
        candidates += [urljoin(base, line.split(":", 1)[1].strip())
                       for line in robots.splitlines()
                       if line.lower().startswith("sitemap:")]
    locs: list[str] = []
    seen_sitemaps: set[str] = set()
    for cand in _dedupe(candidates):
        if cand in seen_sitemaps:
            continue
        seen_sitemaps.add(cand)
        _sitemap_locs(client, cand, site, adapter, locs)
    if not locs:
        return None
    urls = _dedupe(locs)
    # 起始 URL 路径非根时,只保留前缀子树内的 URL,避免抓进其他语言/版本
    prefix = urlsplit(site.url).path
    if prefix != "/" and prefix:
        keep = [u for u in urls if urlsplit(u).path.startswith(prefix)]
        ancestors = [u for u in urls if prefix.startswith(urlsplit(u).path) and urlsplit(u).path != "/"]
        urls = keep or ancestors or [site.url]
    else:
        urls = urls or [site.url]
    expanded = _expand_seeds(client, site, adapter, urls)
    all_urls = _sort_urls(_dedupe(urls + expanded), site)
    if not all_urls:
        return None
    return Discovery(urls=all_urls, source="sitemap")


def _try_bfs(site: SiteConfig, client: httpx.Client, adapter: Adapter) -> Discovery:
    """通用 BFS:同源 + 前缀约束 + 深度限制。"""
    start = normalize_url(site.url) or site.url
    prefix = urlsplit(start).path
    if prefix == "/":
        prefix = ""
    found: list[str] = []
    seen: set[str] = set()
    origin = _origin_of(site.url)
    queue: list[tuple[str, int]] = [(start, 0)]
    while queue:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not _check(url, site, adapter):
            continue
        if site.max_pages and len(found) >= site.max_pages:
            break
        found.append(url)
        if site.max_depth and depth >= site.max_depth:
            continue
        try:
            r = client.get(url, timeout=site.timeout, headers={"User-Agent": UA})
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        try:
            doc = html.fromstring(r.text, base_url=str(r.url))
        except (ValueError, etree.ParserError):
            continue
        for href in doc.xpath("//a/@href"):
            n = _candidate_from_href(href, str(r.url), origin, prefix)
            if not n:
                continue
            if n not in seen and _check(n, site, adapter):
                queue.append((n, depth + 1))
    return Discovery(urls=_sort_urls(_dedupe(found), site), source="bfs")


def discover(site: SiteConfig, adapter: Adapter) -> Discovery:
    """按 llms.txt → sitemap → BFS 顺序发现全量 URL。"""
    with _client(site.timeout) as client:
        if site.adapter in ("auto", "llmstxt"):
            d = _try_llmstxt(site, client)
            if d:
                return d
        if site.adapter != "llmstxt":
            d = _try_sitemap(site, client, adapter)
            if d:
                return d
            d = _try_bfs(site, client, adapter)
            return d
        return Discovery(urls=[], source="llms.txt")
