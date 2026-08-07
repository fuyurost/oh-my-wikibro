"""交互模式:菜单引导的抓取/预览/增量更新,无需记命令行参数。

入口:omwb(不带子命令)。每个操作完成后回到主菜单。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import questionary
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from .adapters import get_adapter
from .cli import _run
from .config import SiteConfig, load_sites, site_from_args
from .discover import discover
from .exam import LLMConfigError, LLMError, generate_exam, review_code
from .exam.session import run_session
from .exam.variants import add_variants
from .runner import _detect_site_adapter, run_site
from .verify import render_report, verify_site

console = Console()
DEFAULT_OUT = "omwb-out"

LOGO = r"""[bold cyan]
  ___  _       __  __        __        ___ _    _ _               
 / _ \| |__   |  \/  |_   _  \ \      / (_) | _(_) |__  _ __ ___  
| | | | '_ \  | |\/| | | | |  \ \ /\ / /| | |/ / | '_ \| '__/ _ \ 
| |_| | | | | | |  | | |_| |   \ V  V / | |   <| | |_) | | | (_) |
 \___/|_| |_| |_|  |_|\__, |    \_/\_/  |_|_|\_\_|_.__/|_|  \___/ 
                      |___/                                       
[/bold cyan]"""


def _logo() -> str:
    try:
        import pyfiglet

        art = pyfiglet.figlet_format("Oh My Wikibro", font="standard").rstrip()
        return f"[bold cyan]{art}[/bold cyan]"
    except ImportError:  # 降级:无 pyfiglet 时用静态 logo
        return LOGO

FORMAT_CHOICES = [
    {"name": "Markdown (每页一 .md,带 frontmatter)", "value": "md", "checked": True},
    {"name": "JSON (每页结构化 + corpus.json)", "value": "json", "checked": True},
    {"name": "LLM 语料 (corpus.jsonl 分块 + corpus.md)", "value": "jsonl"},
    {"name": "PDF (每页 + 可选合并版)", "value": "pdf"},
]


def _progress() -> Progress:
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
    )


def _ask_site_config() -> SiteConfig | None:
    """引导填写站点参数,返回 SiteConfig;取消返回 None。"""
    url = questionary.text(
        "站点 URL(如 https://docs.python.org/zh-cn/3/)",
        validate=lambda v: v.startswith(("http://", "https://")),
    ).ask()
    if url is None:
        return None
    formats = questionary.checkbox(
        "输出格式(空格选择,回车确认)",
        choices=FORMAT_CHOICES,
    ).ask()
    if formats is None:
        return None
    if not formats:
        console.print("[red]至少选择一种格式[/red]")
        return None
    combined = False
    if "pdf" in formats:
        combined = questionary.confirm("PDF 额外生成合并版 combined.pdf?").ask() or False
    limit_raw = questionary.text("最多抓取页数(0=全量)", default="0",
                                 validate=lambda v: v.isdigit()).ask()
    if limit_raw is None:
        return None
    fresh = questionary.confirm("忽略缓存强制重新抓取?", default=False).ask() or False
    return site_from_args(
        url=url, name=None, formats=formats, adapter="auto",
        include=None, exclude=None, max_pages=int(limit_raw or 0),
        concurrency=8, delay=0.25, js_render=False, combined_pdf=combined,
    ), fresh


def _run_interactive(site: SiteConfig, fresh: bool) -> None:
    console.print(f"[dim]适配器自动检测中…[/dim]")
    try:
        with _progress() as progress:
            result = asyncio.run(run_site(site, Path(DEFAULT_OUT), fresh=fresh, progress=progress))
        _run(result)
    except KeyboardInterrupt:
        console.print("[yellow]已中断[/yellow]")
        return
    except Exception as e:
        console.print(f"[red]✗ 失败: {e}[/red]")
        return
    if questionary.confirm("打开输出目录?", default=False).ask():
        try:
            import os
            os.startfile(str(Path(DEFAULT_OUT) / site.site_name))  # noqa: S606 (Windows)
        except OSError:
            pass


def _preview_flow() -> None:
    url = questionary.text(
        "站点 URL", validate=lambda v: v.startswith(("http://", "https://"))).ask()
    if url is None:
        return
    site = SiteConfig(url=url)
    name = _detect_site_adapter(site)
    try:
        disc = discover(site, get_adapter(name))
    except Exception as e:
        console.print(f"[red]✗ 发现失败: {e}[/red]")
        return
    console.print(f"发现来源: [bold]{disc.source}[/bold] | 适配器: {name} | 共 [bold]{len(disc.urls)}[/bold] 个 URL")
    if disc.llms_full is not None:
        console.print(f"命中 llms-full.txt,正文 {len(disc.llms_full):,} 字符,将整站输出为单页")
        return
    for u in disc.urls[:20]:
        console.print(f"  {u}")
    if len(disc.urls) > 20:
        console.print(f"  … 共 {len(disc.urls)} 个")
    if disc.urls and questionary.confirm("直接开始抓取?", default=True).ask():
        _run_interactive(site_from_args(
            url=url, name=None, formats=["md", "json"], adapter="auto",
            include=None, exclude=None, max_pages=0,
            concurrency=8, delay=0.25, js_render=False, combined_pdf=False,
        ), fresh=False)


def _build_flow() -> None:
    default = "examples/sites.yaml" if Path("examples/sites.yaml").exists() else ""
    path = questionary.text("配置文件路径", default=default).ask()
    if path is None:
        return
    cfg = Path(path)
    if not cfg.exists():
        console.print(f"[red]文件不存在: {cfg}[/red]")
        return
    fresh = questionary.confirm("忽略缓存强制重新抓取?", default=False).ask() or False
    try:
        sites = load_sites(cfg)
    except Exception as e:
        console.print(f"[red]✗ 配置解析失败: {e}[/red]")
        return
    for site in sites:
        console.rule(f"{site.site_name} ({site.url})")
        try:
            with _progress() as progress:
                result = asyncio.run(run_site(site, Path(DEFAULT_OUT), fresh=fresh, progress=progress))
            _run(result)
        except Exception as e:
            console.print(f"[red]✗ {site.site_name}: {e}[/red]")


def _list_existing() -> list[tuple[str, dict]]:
    """扫描 omwb-out 下已有站点的 manifest。"""
    out = []
    for manifest in sorted(Path(DEFAULT_OUT).glob("*/manifest.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append((manifest.parent.name, data))
    return out


def _update_flow() -> None:
    sites = _list_existing()
    if not sites:
        console.print("[yellow]omwb-out 下还没有已抓取的站点[/yellow]")
        return
    table = Table(title="已抓取站点")
    table.add_column("名称", style="bold")
    table.add_column("URL")
    table.add_column("页数")
    table.add_column("来源")
    for name, m in sites:
        table.add_row(name, m.get("url", ""), str(m.get("pages", 0)), m.get("source", ""))
    console.print(table)
    choice = questionary.select(
        "选择要更新的站点",
        choices=[f"{name} ({m.get('pages', 0)} 页)" for name, m in sites] + ["返回"],
    ).ask()
    if choice is None or choice == "返回":
        return
    name = choice.split(" (")[0]
    m = dict(sites)[name]
    fresh = questionary.confirm("忽略缓存强制重新抓取?", default=False).ask() or False
    site = SiteConfig(url=m["url"], name=name, formats=["md", "json"])
    try:
        with _progress() as progress:
            result = asyncio.run(run_site(site, Path(DEFAULT_OUT), fresh=fresh, progress=progress))
        _run(result)
    except Exception as e:
        console.print(f"[red]✗ {name}: {e}[/red]")


def _verify_flow() -> None:
    """校验已抓取站点的输出完整性。"""
    sites = _list_existing()
    if not sites:
        console.print("[yellow]omwb-out 下还没有已抓取的站点[/yellow]")
        return
    names = [name for name, _ in sites]
    choice = questionary.select(
        "选择要校验的站点",
        choices=[*names, "全部站点", "返回"],
    ).ask()
    if choice is None or choice == "返回":
        return
    targets = names if choice == "全部站点" else [choice]
    bad = False
    for name in targets:
        report = verify_site(Path(DEFAULT_OUT), name)
        render_report(report)
        bad = bad or not report["ok"]
    console.print("[red]校验发现缺失/空文件[/red]" if bad else "[green]校验通过[/green]")


def _exam_flow() -> None:
    """生成练习试题:选站点 → 主题 → 难度 → 生成 → 变体 / 代码审查。"""
    sites = _list_existing()
    if not sites:
        console.print("[yellow]omwb-out 下还没有已抓取的站点,先抓取再生成试题[/yellow]")
        return
    table = Table(title="已抓取站点")
    table.add_column("名称", style="bold")
    table.add_column("URL")
    table.add_column("页数")
    for name, m in sites:
        table.add_row(name, m.get("url", ""), str(m.get("pages", 0)))
    console.print(table)
    choice = questionary.select(
        "选择语料站点",
        choices=[f"{name} ({m.get('pages', 0)} 页)" for name, m in sites] + ["返回"],
    ).ask()
    if choice is None or choice == "返回":
        return
    site = choice.split(" (")[0]
    topic = questionary.text("主题关键词", validate=lambda v: bool(v.strip())).ask()
    if not topic:
        return
    level = questionary.select("难度", choices=["basic", "medium", "hard"],
                               default="medium").ask()
    if level is None:
        return
    try:
        exam = generate_exam(site, topic, level=level, out=DEFAULT_OUT, variants=0)
    except (LLMConfigError, LLMError, FileNotFoundError, ValueError) as e:
        console.print(f"[red]✗ 生成失败: {e}[/red]")
        return
    exam_path = Path(DEFAULT_OUT) / site / "exam" / f"exam-{exam['id']}.json"
    console.print(f"[bold green]试题已生成[/bold green] [{exam['level']}] "
                  f"{exam.get('title') or exam['task'][:60]}")
    console.print(f"  要求实现: {exam['task']}")
    console.print(f"  输出规格: {exam['output_spec']}")
    ann_items = exam.get("source_annotations") or []
    n_ann = sum(len(item.get("annotations") or []) for item in ann_items)
    if n_ann:
        console.print(f"  原文讲解批注: {n_ann} 条,覆盖 {len(ann_items)} 段语料原文")
    console.print(f"[dim]  保存: {exam_path}[/dim]")
    if questionary.confirm("生成变体(举一反三)?", default=True).ask():
        try:
            variants = add_variants(exam_path, count=3)
            console.print(f"[bold green]已生成 {len(variants)} 个变体并写回[/bold green]")
        except (LLMConfigError, LLMError, ValueError) as e:
            console.print(f"[red]✗ 变体生成失败: {e}[/red]")
    if questionary.confirm("审查一个代码文件?", default=False).ask():
        code = questionary.text("代码文件路径").ask()
        if code:
            try:
                report = review_code(exam_path, Path(code), out=DEFAULT_OUT)
                total = report["total"]
                console.print(f"[bold green]审查完成[/bold green] 评分: {total['score']}/10 | "
                              f"结论: [cyan]{total['verdict']}[/cyan]")
                console.print(f"  一句话结论: {total.get('conclusion', '')}")
            except (LLMConfigError, LLMError, FileNotFoundError, ValueError) as e:
                console.print(f"[red]✗ 审查失败: {e}[/red]")


def _exam_session_flow() -> None:
    """开始测验:选站点 → 主题 → 难度 → 题数 → 逐题盲答 → 提交批改 + 错误统计。"""
    sites = _list_existing()
    if not sites:
        console.print("[yellow]omwb-out 下还没有已抓取的站点,先抓取再开始测验[/yellow]")
        return
    table = Table(title="已抓取站点")
    table.add_column("名称", style="bold")
    table.add_column("URL")
    table.add_column("页数")
    for name, m in sites:
        table.add_row(name, m.get("url", ""), str(m.get("pages", 0)))
    console.print(table)
    choice = questionary.select(
        "选择语料站点",
        choices=[f"{name} ({m.get('pages', 0)} 页)" for name, m in sites] + ["返回"],
    ).ask()
    if choice is None or choice == "返回":
        return
    site = choice.split(" (")[0]
    topic = questionary.text("主题关键词", validate=lambda v: bool(v.strip())).ask()
    if not topic:
        return
    level = questionary.select("难度", choices=["basic", "medium", "hard"],
                               default="medium").ask()
    if level is None:
        return
    count_raw = questionary.text("题目数量", default="3",
                                 validate=lambda v: v.isdigit() and int(v) >= 1).ask()
    if count_raw is None:
        return
    console.print("[dim]答题阶段不展示原文与讲解(盲答),逐题输入后以单独一行 ---END--- 结束[/dim]")
    try:
        record = run_session(site, topic, level=level, count=int(count_raw), out=DEFAULT_OUT)
    except (LLMConfigError, LLMError, FileNotFoundError, ValueError) as e:
        console.print(f"[red]✗ 测验失败: {e}[/red]")
        return
    console.print(f"[bold green]测验完成[/bold green] {record['session_id']} | "
                  f"主题: [cyan]{record['topic']}[/cyan]")
    for r in record["review"]:
        points = "、".join(r.get("error_points") or []) or "-"
        console.print(f"  第 {r['index']} 题:{r['score']}/10 [{r['verdict']}] 错误点: {points}")
    for it in record.get("error_summary") or []:
        ex = ", ".join(str(i) for i in it.get("examples") or [])
        console.print(f"  [yellow]{it['concept']}[/yellow] ×{it['count']} "
                      f"(占错题 {it['ratio']}) 例题:第 {ex} 题")
    if not record.get("error_summary"):
        console.print("[green]全部通过[/green]")
    console.print(f"[dim]报告: {Path(DEFAULT_OUT) / site / 'exam' / 'sessions' / 'review.md'}[/dim]")


def main_loop() -> None:
    console.print(_logo())
    console.print("[dim]全量抓取 wiki/在线技术文档 → Markdown / JSON / PDF / LLM 语料[/dim]\n")
    while True:
        try:
            choice = questionary.select(
                "选择操作",
                choices=[
                    "抓取新站点",
                    "预览站点 URL 清单",
                    "按配置文件批量抓取",
                    "查看/更新已抓取站点",
                    "校验输出完整性",
                    "生成练习试题",
                    "开始测验",
                    "退出",
                ],
            ).ask()
        except KeyboardInterrupt:
            console.print("\n[yellow]再见[/yellow]")
            return
        if choice is None or choice == "退出":
            console.print("[yellow]再见[/yellow]")
            return
        if choice == "抓取新站点":
            r = _ask_site_config()
            if r:
                site, fresh = r
                _run_interactive(site, fresh)
        elif choice == "预览站点 URL 清单":
            _preview_flow()
        elif choice == "按配置文件批量抓取":
            _build_flow()
        elif choice == "查看/更新已抓取站点":
            _update_flow()
        elif choice == "校验输出完整性":
            _verify_flow()
        elif choice == "生成练习试题":
            _exam_flow()
        elif choice == "开始测验":
            _exam_session_flow()
