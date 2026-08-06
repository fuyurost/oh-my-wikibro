"""PDF 输出:Chromium print-to-pdf (Playwright),CJK 字体走系统字体。

离线场景:剔除 <img>(不下载资源);代码块/表格由注入 CSS 排版。
v0 依赖 playwright + chromium 浏览器,未安装时给出明确报错。
"""

from __future__ import annotations

import html as html_lib
import re
import time
from pathlib import Path

import trafilatura
from playwright.sync_api import Error as PWError

from ..models import Page, SiteResult
from . import out_file

_CSS = """
<style>
  @page { size: A4; margin: 18mm 15mm 16mm 15mm; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC",
         "Noto Sans CJK SC", "Source Han Sans SC", sans-serif;
         font-size: 10.5pt; line-height: 1.65; color: #1f2328; }
  h1, h2, h3, h4 { line-height: 1.3; page-break-after: avoid; }
  h1 { font-size: 20pt; border-bottom: 2px solid #d0d7de; padding-bottom: 6px; }
  h2 { font-size: 15pt; border-bottom: 1px solid #d8dee4; padding-bottom: 4px; }
  h3 { font-size: 12.5pt; }
  pre { background: #f6f8fa; border: 1px solid #d8dee4; border-radius: 6px;
        padding: 10px 12px; white-space: pre-wrap; word-break: break-word;
        font-family: Consolas, "Cascadia Mono", "Courier New", monospace; font-size: 9pt;
        page-break-inside: avoid; }
  code { font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
         background: #f0f2f5; padding: 1px 4px; border-radius: 3px; font-size: 0.92em; }
  pre code { background: none; padding: 0; font-size: 9pt; }
  table { border-collapse: collapse; width: 100%; margin: 8px 0; page-break-inside: avoid; }
  th, td { border: 1px solid #d0d7de; padding: 5px 8px; font-size: 9.5pt; }
  th { background: #f6f8fa; }
  tr { page-break-inside: avoid; }
  a { color: #0969da; text-decoration: none; word-break: break-all; }
  blockquote { margin: 6px 0; padding: 2px 14px; color: #57606a;
               border-left: 4px solid #d0d7de; }
  img { max-width: 100%; }
  ul, ol { padding-left: 1.6em; }
  .doc-header { font-size: 8pt; color: #57606a; border-bottom: 1px solid #eee; }
  .doc-footer { font-size: 8pt; color: #57606a; }
  section.page { page-break-after: always; }
  section.page:last-child { page-break-after: auto; }
  .cover { text-align: center; margin-top: 40%; }
  .cover h1 { border: none; }
  .cover .site { color: #57606a; }
</style>
"""

_IMG_RE = re.compile(r"<img[^>]*>", re.I)


def _content_html(page: Page) -> str:
    """trafilatura 净化后的 HTML 片段 (剔除图片,离线可用)。"""
    cleaned = trafilatura.extract(page.html, url=page.url, output_format="html", include_tables=True)
    if not cleaned:
        cleaned = f"<p>{html_lib.escape(page.markdown[:2000])}</p>"
    return _IMG_RE.sub("", cleaned)


def _page_doc(page: Page, site_title: str) -> tuple[str, str, str]:
    header = (f'<span class="doc-header">{html_lib.escape(page.title)} · '
              f'{html_lib.escape(page.url)}</span>')
    footer = '<span class="doc-footer">第 {pageNumber} 页 / 共 {totalPages} 页 · {title}</span>'
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{html_lib.escape(page.title)}</title>{_CSS}</head><body>
<article>
{_content_html(page)}
</article>
</body></html>"""
    return doc, header, footer


def _render_pdf(doc_html: str, header: str, footer: str, out: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("缺少 playwright: pip install playwright && playwright install chromium")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PWError as e:
            raise RuntimeError(f"Chromium 未安装: {e}\n请执行: playwright install chromium") from e
        try:
            page = browser.new_page()
            page.set_content(doc_html, wait_until="domcontentloaded")
            page.pdf(
                path=str(out),
                format="A4",
                display_header_footer=True,
                header_template=header,
                footer_template=footer,
                print_background=True,
            )
        finally:
            browser.close()


def write_pdf(page: Page, out_dir: Path, site_title: str) -> Path:
    out = out_file(page, out_dir, "pdf")
    doc, header, footer = _page_doc(page, site_title)
    _render_pdf(doc, header, footer, out)
    return out


def write_all_pdf(result: SiteResult, out_dir: Path, combined: bool = False) -> tuple[int, Path | None]:
    n = 0
    for page in result.pages:
        if page.markdown:
            write_pdf(page, out_dir, result.name)
            n += 1
    combined_path: Path | None = None
    if combined and result.pages:
        combined_path = out_dir / "combined.pdf"
        sections = [f'<section class="page"><h1>{html_lib.escape(p.title)}</h1>'
                    f'<p style="font-size:8pt;color:#57606a">{html_lib.escape(p.url)}</p>'
                    f'{_content_html(p)}</section>' for p in result.pages if p.markdown]
        cover = (f'<section class="page"><div class="cover"><h1>{html_lib.escape(result.name)}</h1>'
                 f'<p class="site">{html_lib.escape(result.url)}</p>'
                 f'<p>{len(result.pages)} 页 · {time.strftime("%Y-%m-%d %H:%M")}</p>'
                 f'<p>oh-my-wikibro 生成</p></div></section>')
        doc = f'<!DOCTYPE html><html><head><meta charset="utf-8">{_CSS}</head><body>{cover}{"".join(sections)}</body></html>'
        header = f'<span class="doc-header">{html_lib.escape(result.name)} · 合并文档</span>'
        footer = '<span class="doc-footer">第 {pageNumber} 页 / 共 {totalPages} 页</span>'
        _render_pdf(doc, header, footer, combined_path)
    return n, combined_path
