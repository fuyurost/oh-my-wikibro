"""输出完整性校验:按 manifest 核对各格式文件,并校验 corpus.jsonl 引用。"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .convert import page_rel_path

# verify 只核对这三类实体文件 (jsonl 是 corpus,非逐页文件)
_VERIFY_FORMATS = ("md", "json", "pdf")
DEFAULT_FORMATS = ["md", "json"]


def verify_site(out_dir: Path, site_name: str) -> dict:
    """校验单个站点输出完整性,返回报告 dict(纯函数,便于测试)。

    报告字段:
      site              站点名
      ok                无缺失/空文件 (含 corpus.jsonl 引用)
      error             无 manifest / manifest 解析失败时的说明
      formats           实际校验的格式列表
      formats_defaulted manifest 缺失 formats 时按默认 md,json 处理
      urls              manifest 记录的成功页数
      files             每格式 {expected, found, missing, empty}
      missing_files     缺失文件清单 (相对站点输出目录)
      empty_files       空文件清单
      corpus            corpus.jsonl 校验结果
                        {exists, records, bad_lines, missing_files, empty_files}
    """
    site_dir = out_dir / site_name
    report = {
        "site": site_name,
        "ok": False,
        "formats": [],
        "formats_defaulted": False,
        "urls": 0,
        "files": {},
        "missing_files": [],
        "empty_files": [],
        "corpus": None,
    }
    manifest_path = site_dir / "manifest.json"
    if not manifest_path.is_file():
        report["error"] = f"缺少 manifest.json ({manifest_path})"
        return report
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        report["error"] = f"manifest.json 解析失败: {e}"
        return report

    formats = [f for f in (manifest.get("formats") or []) if f in _VERIFY_FORMATS]
    if not formats:
        formats = list(DEFAULT_FORMATS)
        report["formats_defaulted"] = True
    report["formats"] = formats

    urls = manifest.get("urls") or []
    report["urls"] = len(urls)
    files = {f: {"expected": 0, "found": 0, "missing": 0, "empty": 0} for f in formats}
    for url in urls:
        rel = page_rel_path(url)
        for fmt in formats:
            rel_file = f"{fmt}/{rel}.{fmt}"
            stats = files[fmt]
            stats["expected"] += 1
            p = site_dir / rel_file
            if not p.exists():
                stats["missing"] += 1
                report["missing_files"].append(rel_file)
            elif p.stat().st_size == 0:
                stats["empty"] += 1
                report["empty_files"].append(rel_file)
            else:
                stats["found"] += 1
    report["files"] = files

    corpus_path = site_dir / "corpus.jsonl"
    corpus = {
        "exists": corpus_path.is_file(),
        "records": 0,
        "bad_lines": 0,
        "missing_files": [],
        "empty_files": [],
    }
    if corpus_path.is_file():
        for line in corpus_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                corpus["bad_lines"] += 1
                continue
            corpus["records"] += 1
            ref = rec.get("file")
            if not ref:
                continue
            p = site_dir / ref
            if not p.exists():
                corpus["missing_files"].append(ref)
            elif p.stat().st_size == 0:
                corpus["empty_files"].append(ref)
    report["corpus"] = corpus

    report["ok"] = (
        not report["missing_files"]
        and not report["empty_files"]
        and not corpus["missing_files"]
        and not corpus["empty_files"]
    )
    return report


def render_report(report: dict) -> None:
    """rich 表格打印站点校验报告 (站点/格式/应生成/存在/缺失/空文件 + 清单)。"""
    console = Console()
    if report.get("error"):
        console.print(f"[red]✗ {report['site']}: {report['error']}[/red]")
        return
    table = Table(title=f"{report['site']} — 输出完整性校验")
    table.add_column("站点", style="bold")
    table.add_column("格式", style="bold")
    table.add_column("应生成", justify="right")
    table.add_column("存在", justify="right")
    table.add_column("缺失", justify="right")
    table.add_column("空文件", justify="right")
    for fmt, c in report["files"].items():
        table.add_row(report["site"], fmt, *[str(c[k]) for k in ("expected", "found", "missing", "empty")])
    console.print(table)
    for f in report["missing_files"]:
        console.print(f"  [red]缺失[/red] {f}")
    for f in report["empty_files"]:
        console.print(f"  [yellow]空文件[/yellow] {f}")
    corpus = report.get("corpus")
    if corpus and corpus["exists"]:
        tail = f" (引用缺失 {len(corpus['missing_files'])} 个)" if corpus["missing_files"] else ""
        console.print(f"corpus.jsonl: {corpus['records']} 条记录{tail}")
        for f in corpus["missing_files"]:
            console.print(f"  [red]corpus 引用缺失[/red] {f}")
        for f in corpus["empty_files"]:
            console.print(f"  [yellow]corpus 引用空文件[/yellow] {f}")
    if report.get("formats_defaulted"):
        console.print("[dim]manifest 无 formats 字段,按默认 md,json 校验[/dim]")
