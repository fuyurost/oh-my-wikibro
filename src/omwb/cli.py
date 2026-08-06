"""CLI: omwb fetch / build / inspect。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .adapters import get_adapter
from .config import SiteConfig, load_sites, site_from_args
from .discover import discover
from .runner import _detect_site_adapter, run_site

app = typer.Typer(add_completion=False, help="全量抓取 wiki/在线技术文档 → Markdown/JSON/PDF/LLM 语料")
console = Console()


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _run(result):
    table = Table(title=f"{result.name} — 完成")
    table.add_column("指标", style="bold")
    table.add_column("值")
    table.add_row("站点", result.url)
    table.add_row("适配器", result.adapter)
    table.add_row("成功页", str(len(result.pages)))
    table.add_row("失败页", str(len(result.failed)))
    table.add_row("输出目录", result.out_dir)
    console.print(table)
    for p in result.failed[:10]:
        console.print(f"  [red]✗[/red] {p.url} — {p.error}")
    if len(result.failed) > 10:
        console.print(f"  … 另有 {len(result.failed) - 10} 个失败页")


@app.command()
def fetch(
    url: str = typer.Argument(..., help="站点起始 URL,如 https://docs.python.org/zh-cn/3/"),
    name: str = typer.Option(None, "--name", "-n", help="站点名(输出目录名)"),
    out: str = typer.Option("omwb-out", "--out", "-o", help="输出根目录"),
    formats: str = typer.Option("md,json", "--formats", "-f", help="md,json,jsonl,pdf,all"),
    adapter: str = typer.Option("auto", "--adapter", "-a",
                                help="auto|generic|llmstxt|docusaurus|gitbook|readthedocs|mkdocs|sphinx|vuepress|hugo"),
    include: str = typer.Option(None, "--include", help="仅保留匹配的 URL 路径 (逗号分隔 glob)"),
    exclude: str = typer.Option(None, "--exclude", "-x", help="剔除匹配的 URL 路径 (逗号分隔 glob)"),
    limit: int = typer.Option(0, "--limit", "-l", help="最多抓取页数 (0=不限)"),
    concurrency: int = typer.Option(8, "--concurrency", "-c", help="并发请求数"),
    delay: float = typer.Option(0.25, "--delay", "-d", help="同主机请求间隔(秒)"),
    js_render: bool = typer.Option(False, "--js-render", help="所有页面强制浏览器渲染(SPA 站)"),
    combined: bool = typer.Option(False, "--combined", help="PDF 额外产出合并版 combined.pdf"),
    fresh: bool = typer.Option(False, "--fresh", help="忽略缓存重新抓取"),
):
    """单站点抓取 + 转换。"""
    site = site_from_args(
        url=url, name=name, formats=_split_csv(formats), adapter=adapter,
        include=_split_csv(include), exclude=_split_csv(exclude),
        max_pages=limit, concurrency=concurrency, delay=delay,
        js_render=js_render, combined_pdf=combined,
    )
    try:
        result = asyncio.run(run_site(site, Path(out), fresh=fresh))
    except KeyboardInterrupt:
        console.print("[yellow]已中断,部分结果已写出[/yellow]")
        raise typer.Exit(2)
    _run(result)


@app.command()
def build(
    config: Path = typer.Option(..., "-c", "--config", help="YAML 配置文件,含 sites 列表"),
    out: str = typer.Option("omwb-out", "--out", "-o", help="输出根目录"),
    fresh: bool = typer.Option(False, "--fresh", help="忽略缓存重新抓取"),
):
    """按配置文件批量抓取所有站点。"""
    sites = load_sites(config)
    for site in sites:
        console.rule(f"{site.site_name} ({site.url})")
        try:
            result = asyncio.run(run_site(site, Path(out), fresh=fresh))
            _run(result)
        except KeyboardInterrupt:
            raise typer.Exit(2)
        except Exception as e:
            console.print(f"[red]✗ {site.site_name}: {e}[/red]")


@app.command()
def inspect(
    url: str = typer.Argument(..., help="站点起始 URL"),
    adapter: str = typer.Option("auto", "--adapter", "-a", help="适配器(auto 自动检测)"),
    limit: int = typer.Option(30, "--limit", "-l", help="预览 URL 数量"),
):
    """只做发现,预览全量 URL 清单(不抓取正文)。"""
    site = SiteConfig(url=url, adapter=adapter)
    name = adapter if adapter != "auto" else _detect_site_adapter(site)
    disc = discover(site, get_adapter(name))
    console.print(f"发现来源: [bold]{disc.source}[/bold] | 适配器: {name} | 共 {len(disc.urls)} 个 URL")
    if disc.llms_full is not None:
        console.print(f"命中 llms-full.txt,正文 {len(disc.llms_full):,} 字符,将整站输出为单页")
        return
    for u in disc.urls[:limit]:
        console.print(f"  {u}")
    if len(disc.urls) > limit:
        console.print(f"  … 共 {len(disc.urls)} 个")


@app.callback(invoke_without_command=True)
def _version(version: bool = typer.Option(False, "--version", help="显示版本")):
    if version:
        console.print(f"oh-my-wikibro {__version__}")
        raise typer.Exit()


if __name__ == "__main__":
    app()
