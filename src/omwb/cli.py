"""CLI: omwb fetch / build / inspect。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .adapters import get_adapter
from .config import SiteConfig, load_sites, site_from_args
from .discover import discover
from .exam.generate import generate_exam
from .exam.llm import LLMConfigError, LLMError
from .exam.review import review_code, review_output_paths
from .exam.session import run_session
from .exam.variants import add_variants
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
        from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
    except ImportError:  # rich 必装,防御
        progress = None
    else:
        progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
        )
    try:
        if progress is not None:
            with progress:
                result = asyncio.run(run_site(site, Path(out), fresh=fresh, progress=progress))
        else:
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
    from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

    sites = load_sites(config)
    for site in sites:
        console.rule(f"{site.site_name} ({site.url})")
        try:
            with Progress(
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
            ) as progress:
                result = asyncio.run(run_site(site, Path(out), fresh=fresh, progress=progress))
            _run(result)
        except KeyboardInterrupt:
            raise typer.Exit(2)
        except Exception as e:
            console.print(f"[red]✗ {site.site_name}: {e}[/red]")


@app.command()
def verify(
    out: str = typer.Argument("omwb-out", help="输出根目录"),
    site: str = typer.Option(None, "--site", help="站点名;缺省校验全部站点"),
    out_opt: str = typer.Option(None, "--out", "-o", help="输出根目录 (覆盖位置参数)"),
):
    """校验输出完整性:按 manifest 核对各格式文件,并校验 corpus.jsonl 引用。"""
    from .verify import render_report, verify_site

    out_dir = Path(out_opt or out)
    if not out_dir.is_dir():
        console.print(f"[red]输出根目录不存在: {out_dir}[/red]")
        raise typer.Exit(1)
    if site:
        candidates = [site]
    else:
        candidates = sorted(d.name for d in out_dir.iterdir() if d.is_dir())
    checked = 0
    bad = False
    for name in candidates:
        site_dir = out_dir / name
        if not (site_dir / "manifest.json").is_file():
            console.print(f"[yellow]跳过 {name}: 无 manifest.json[/yellow]")
            if site:
                bad = True  # 显式指定的站点无 manifest 视为校验失败
            continue
        checked += 1
        report = verify_site(out_dir, name)
        render_report(report)
        if not report["ok"]:
            bad = True
    if checked == 0:
        console.print("[yellow]没有可校验的站点[/yellow]")
        bad = True
    if bad:
        raise typer.Exit(1)


def serve(
    out: str = typer.Option("omwb-out", "--out", "-o", help="输出根目录(含各站点 manifest.json)"),
    port: int = typer.Option(8732, "--port", help="Web UI 监听端口"),
    open_browser: bool = typer.Option(False, "--open", help="启动后自动打开浏览器"),
):
    """启动本地 Web UI:站点列表 / 发起抓取 / 详情语料 / 校验报告。"""
    import os
    import sys
    import threading

    import uvicorn

    from .webapp import create_app

    web_app = create_app(Path(out))
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        def _open():
            if sys.platform == "win32":
                os.startfile(url)
            else:
                import webbrowser

                webbrowser.open(url)
        threading.Timer(1.2, _open).start()
    console.print(f"[bold green]Web UI[/bold green] {url}  输出目录: [cyan]{out}[/cyan]")
    uvicorn.run(web_app, host="127.0.0.1", port=port, log_level="info")


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


exam_app = typer.Typer(name="exam", help="开放性试题:LLM 生成 / 批注式代码审查 / 自动举一反三")
app.add_typer(exam_app)


@exam_app.command("generate")
def exam_generate(
    site: str = typer.Argument(..., help="站点名(omwb-out 下的目录,需已抓取语料)"),
    topic: str = typer.Argument(..., help="主题关键词,用于语料选材"),
    level: str = typer.Option("medium", "--level", help="难度:basic|medium|hard"),
    out: str = typer.Option("omwb-out", "--out", "-o", help="输出根目录"),
    variants: int = typer.Option(3, "--variants", help="自动生成变体数量(0 关闭)"),
    base_url: str = typer.Option(None, "--base-url", envvar="OMWB_LLM_BASE_URL", help="LLM 服务地址"),
    api_key: str = typer.Option(None, "--api-key", envvar="OMWB_LLM_API_KEY", help="LLM API Key"),
    model: str = typer.Option(None, "--model", envvar="OMWB_LLM_MODEL", help="LLM 模型名"),
):
    """基于已抓取语料按主题生成开放性试题(含原文锚定讲解与自动变体)。"""
    try:
        exam = generate_exam(site, topic, level=level, out=out, variants=variants,
                             base_url=base_url, api_key=api_key, model=model)
    except (LLMConfigError, LLMError, FileNotFoundError, ValueError) as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1)
    path = Path(out) / site / "exam" / f"exam-{exam['id']}.json"
    console.print(f"[bold green]试题已生成[/bold green] 难度: [cyan]{exam['level']}[/cyan] | "
                  f"主题: [cyan]{exam['topic']}[/cyan]")
    console.print(f"题目: {exam.get('title') or exam['task'][:80]}")
    console.print(f"要求实现: {exam['task']}")
    console.print(f"输出规格: {exam['output_spec']}")
    ann_items = exam.get("source_annotations") or []
    n_ann = sum(len(item.get("annotations") or []) for item in ann_items)
    if n_ann:
        console.print(f"原文讲解批注: {n_ann} 条,覆盖 {len(ann_items)} 段语料原文")
    variant_list = exam.get("variants") or []
    console.print(f"变体({len(variant_list)} 个):")
    for v in variant_list:
        console.print(f"  - [{v.get('dimension', '')}] {v.get('title') or v['task'][:60]}")
    if not variant_list:
        console.print("  (未生成)")
    console.print(f"[dim]保存: {path}[/dim]")


@exam_app.command("review")
def exam_review(
    exam_json: Path = typer.Argument(..., help="试题 JSON 路径(omwb exam generate 生成)"),
    code: Path = typer.Argument(..., help="待审查的代码文件路径"),
    out: str = typer.Option("omwb-out", "--out", "-o", help="输出根目录"),
    base_url: str = typer.Option(None, "--base-url", envvar="OMWB_LLM_BASE_URL", help="LLM 服务地址"),
    api_key: str = typer.Option(None, "--api-key", envvar="OMWB_LLM_API_KEY", help="LLM API Key"),
    model: str = typer.Option(None, "--model", envvar="OMWB_LLM_MODEL", help="LLM 模型名"),
):
    """LLM 批注式审查学习者代码,输出 markdown + JSON 报告。"""
    try:
        report = review_code(exam_json, code, out=out,
                             base_url=base_url, api_key=api_key, model=model)
    except (LLMConfigError, LLMError, FileNotFoundError, ValueError) as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1)
    total = report["total"]
    console.print(f"[bold green]审查完成[/bold green] 评分: {total['score']}/10 | "
                  f"结论: [cyan]{total['verdict']}[/cyan]")
    console.print(f"一句话结论: {total.get('conclusion', '')}")
    ann = report.get("annotated_code") or []
    n_ann = sum(len(a["annotations"]) for a in ann)
    console.print(f"批注: {n_ann} 条,覆盖 {len(ann)} 行代码")
    ca = report.get("complexity_analysis") or {}
    if ca.get("complexity"):
        console.print(f"复杂度: {ca['complexity']}")
    for row in (ca.get("scale_deduction") or [])[:2]:
        console.print(f"  规模推演: {row.get('scale', '')} → {row.get('operations', '')} → "
                      f"{row.get('est_time', '')}")
    exam = json.loads(exam_json.read_text(encoding="utf-8"))
    md_path, _ = review_output_paths(exam, out)
    console.print(f"[dim]报告: {md_path}[/dim]")


@exam_app.command("variants")
def exam_variants(
    exam_json: Path = typer.Argument(..., help="试题 JSON 路径"),
    count: int = typer.Option(3, "--count", help="变体数量"),
    base_url: str = typer.Option(None, "--base-url", envvar="OMWB_LLM_BASE_URL", help="LLM 服务地址"),
    api_key: str = typer.Option(None, "--api-key", envvar="OMWB_LLM_API_KEY", help="LLM API Key"),
    model: str = typer.Option(None, "--model", envvar="OMWB_LLM_MODEL", help="LLM 模型名"),
):
    """为已有试题自动生成变体并写回 variants 数组(举一反三)。"""
    try:
        variants = add_variants(exam_json, count=count,
                                base_url=base_url, api_key=api_key, model=model)
    except (LLMConfigError, LLMError, FileNotFoundError, ValueError) as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1)
    console.print(f"[bold green]已生成 {len(variants)} 个变体并写回[/bold green] {exam_json}")
    for v in variants:
        console.print(f"  - [{v.get('dimension', '')}] {v.get('title') or v['task'][:60]}")


@exam_app.command("session")
def exam_session(
    site: str = typer.Argument(..., help="站点名(omwb-out 下的目录,需已抓取语料)"),
    topic: str = typer.Argument(..., help="主题关键词"),
    count: int = typer.Option(3, "--count", help="题目数量"),
    level: str = typer.Option("medium", "--level", help="难度:basic|medium|hard"),
    out: str = typer.Option("omwb-out", "--out", "-o", help="输出根目录"),
    answers_dir: str = typer.Option(
        None, "--answers-dir",
        help="答案目录(按题号读 01.py/02.py);缺省为交互粘贴,以单独一行 ---END--- 结束"),
    base_url: str = typer.Option(None, "--base-url", envvar="OMWB_LLM_BASE_URL", help="LLM 服务地址"),
    api_key: str = typer.Option(None, "--api-key", envvar="OMWB_LLM_API_KEY", help="LLM API Key"),
    model: str = typer.Option(None, "--model", envvar="OMWB_LLM_MODEL", help="LLM 模型名"),
):
    """完整考试会话:出题 → 盲答 → 提交批改+题解 → 错误统计(error-book 只增不改)。"""
    try:
        record = run_session(site, topic, level=level, count=count, out=out,
                             answers_dir=answers_dir,
                             base_url=base_url, api_key=api_key, model=model)
    except (LLMConfigError, LLMError, FileNotFoundError, ValueError) as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1)
    session_dir = Path(out) / site / "exam" / "sessions"
    console.print(f"[bold green]测验完成[/bold green] {record['session_id']} | "
                  f"主题: [cyan]{record['topic']}[/cyan] | 题数: {record['count']}")
    table = Table(title="批改总览")
    table.add_column("题号", style="bold")
    table.add_column("评分")
    table.add_column("结论")
    table.add_column("错误点")
    for r in record["review"]:
        table.add_row(str(r["index"]), f"{r['score']}/10", r["verdict"],
                      "、".join(r.get("error_points") or []) or "-")
    console.print(table)
    for it in record.get("error_summary") or []:
        ex = ", ".join(str(i) for i in it.get("examples") or [])
        console.print(f"  [yellow]{it['concept']}[/yellow] ×{it['count']} "
                      f"(占错题 {it['ratio']}) 例题:第 {ex} 题")
    if not record.get("error_summary"):
        console.print("[green]全部通过,无错误统计[/green]")
    console.print(f"[dim]会话记录: {session_dir / (record['session_id'] + '.json')}[/dim]")
    console.print(f"[dim]批改报告: {session_dir / 'review.md'}[/dim]")
    console.print(f"[dim]错误簿: {Path(out) / site / 'exam' / 'error-book.json'}[/dim]")


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context, version: bool = typer.Option(False, "--version", help="显示版本")):
    if version:
        console.print(f"oh-my-wikibro {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        from .interactive import main_loop

        main_loop()
        raise typer.Exit()


if __name__ == "__main__":
    app()
