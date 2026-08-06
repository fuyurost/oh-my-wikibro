"""编排器:单站点 发现 → 抓取 → 提取 → 转换 → 清单。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx

from .adapters import detect_adapter, get_adapter
from .browser import render_url
from .config import SiteConfig
from .convert import page_rel_path
from .convert.jsonout import write_all_json
from .convert.md import write_all_md
from .convert.pdf import write_all_pdf
from .discover import UA, discover
from .extract import extract_page, looks_like_spa
from .fetch import PageFetcher
from .models import Page, SiteResult
from .pipeline import write_corpus_jsonl, write_corpus_md


def _detect_site_adapter(site: SiteConfig) -> str:
    """auto 模式:抓首页 HTML 检测平台。"""
    try:
        r = httpx.get(site.url, follow_redirects=True, timeout=site.timeout, headers={"User-Agent": UA})
        if r.status_code == 200:
            return detect_adapter(r.text, site.url)
    except httpx.HTTPError:
        pass
    return "generic"


async def run_site(site: SiteConfig, out_root: Path, fresh: bool = False,
                   progress=None, task_id=None) -> SiteResult:
    """完整跑一个站点,返回结果。progress/task_id 供 rich 进度条推进。"""
    out_dir = out_root / site.site_name
    out_dir.mkdir(parents=True, exist_ok=True)

    adapter_name = site.adapter if site.adapter != "auto" else _detect_site_adapter(site)
    adapter = get_adapter(adapter_name)

    disc = discover(site, adapter)
    if disc.llms_full is not None:
        # llms-full.txt:整站内容就是一份大 markdown
        page = Page(
            url=disc.urls[0] if disc.urls else site.url,
            rel_path="index",
            title=site.site_name,
            status=200,
            markdown=disc.llms_full.strip(),
            meta={"url": site.url, "format": "llms-full.txt"},
        )
        result = SiteResult(
            name=site.site_name, url=site.url, adapter="llmstxt",
            out_dir=str(out_dir), pages=[page], started_at=time.time(),
        )
        result.finished_at = time.time()
        await _convert(result, site, out_dir)
        _write_manifest(result, disc.source, out_dir)
        return result

    urls = disc.urls
    if not urls:
        raise RuntimeError(f"未能发现任何页面: {site.url} (发现来源: {disc.source})")
    if site.max_pages:
        urls = urls[: site.max_pages]

    result = SiteResult(
        name=site.site_name, url=site.url, adapter=adapter_name,
        out_dir=str(out_dir), started_at=time.time(),
    )
    keep_html = "pdf" in site.formats
    fetcher = PageFetcher(site, out_dir / "cache.sqlite", fresh)
    await fetcher.open()
    sem = asyncio.Semaphore(site.concurrency)
    urls_iter = iter(urls)

    async def process(url: str) -> None:
        try:
            await _process_one(url)
        except Exception as e:  # 单页失败不拖垮整站
            page = Page(url=url, rel_path=page_rel_path(url), error=f"{type(e).__name__}: {e}")
            result.failed.append(page)
        finally:
            if progress is not None and task_id is not None:
                progress.update(task_id, advance=1)

    async def _process_one(url: str) -> None:
        async with sem:
            page = Page(url=url, rel_path=page_rel_path(url))
            status, html = await fetcher.fetch(url)
            page.status = status
            if status != 200:
                page.error = f"HTTP {status}"
                result.failed.append(page)
                return
            page.html = html
            head = html[:512].lower()
            if "<html" not in head and "<!doctype" not in head and "<head" not in head:
                # 非 HTML(llms.txt 直链的 .md / .txt):内容即正文
                page.markdown = html.strip()
                page.meta = {"url": url, "format": "text"}
                page.title = url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                result.pages.append(page)
                return
            if site.js_render:
                rendered = await asyncio.to_thread(render_url, url, site.timeout)
                if rendered:
                    page.html = rendered
            res = await asyncio.to_thread(extract_page, page.html, url, adapter)
            page.title, page.meta, page.toc, page.markdown = res.title, res.meta, res.toc, res.markdown
            if not page.markdown and site.js_fallback and looks_like_spa(html):
                rendered = await asyncio.to_thread(render_url, url, site.timeout)
                if rendered:
                    res2 = await asyncio.to_thread(extract_page, rendered, url, adapter)
                    if res2.markdown:
                        page.html = rendered
                        page.title, page.meta, page.toc, page.markdown = (
                            res2.title, res2.meta, res2.toc, res2.markdown)
            if page.markdown:
                if not keep_html:
                    page.html = ""
                result.pages.append(page)
            else:
                page.error = "正文提取为空"
                result.failed.append(page)

    try:
        await asyncio.gather(*(process(u) for u in urls))
    finally:
        await fetcher.close()

    result.finished_at = time.time()
    if not result.pages and not result.failed:
        raise RuntimeError("抓取未产生任何页面")
    await _convert(result, site, out_dir)
    _write_manifest(result, disc.source, out_dir)
    return result


async def _convert(result: SiteResult, site: SiteConfig, out_dir: Path) -> None:
    for fmt in site.formats:
        if fmt == "md":
            await asyncio.to_thread(write_all_md, result, out_dir / "md")
        elif fmt == "json":
            await asyncio.to_thread(write_all_json, result, out_dir)
        elif fmt == "jsonl":
            await asyncio.to_thread(write_corpus_jsonl, result, out_dir)
            await asyncio.to_thread(write_corpus_md, result, out_dir)
        elif fmt == "pdf":
            # playwright sync API 不能在 asyncio 事件循环线程内运行
            await asyncio.to_thread(write_all_pdf, result, out_dir / "pdf",
                                    combined=site.combined_pdf)


def _write_manifest(result: SiteResult, source: str, out_dir: Path) -> None:
    manifest = result.to_manifest()
    manifest["source"] = source
    manifest["urls"] = [p.url for p in result.pages]
    manifest["failed_urls"] = [{"url": p.url, "error": p.error} for p in result.failed]
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
