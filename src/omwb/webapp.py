"""Web UI:FastAPI 服务。

功能:列出已抓取站点、发起新抓取(SSE 实时进度)、浏览站点详情与语料、
展示校验报告。静态资源在 src/omwb/web/(原生 HTML/CSS/JS,无构建工具)。

CLI 入口:omwb serve [-o omwb-out] [--port 8732] [--open]
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import SiteConfig
from .convert import page_rel_path
from .runner import run_site

WEB_DIR = Path(__file__).parent / "web"

# 合并 verify-automation 分支后改用 verify_site(out_dir, site_name);
# 此处先尝试导入,不存在时 /api/verify 回退到轻量实现(不阻塞两侧开发)。
try:
    from .verify import verify_site as _verify_site  # type: ignore[import-not-found]
except ImportError:
    _verify_site = None

TEXT_EXTS = {".md", ".json", ".jsonl", ".txt", ".html"}
CORPUS_FILES = ("corpus.json", "corpus.md", "corpus.jsonl", "combined.pdf")
FORMAT_DIRS = ("md", "json", "pdf")
STAGE_LABELS = {
    "discover": "发现页面",
    "md": "生成 Markdown",
    "json": "生成 JSON",
    "jsonl": "生成语料 (JSONL/MD)",
    "pdf": "转换 PDF",
}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _file_info(path: Path, base: Path) -> dict[str, Any]:
    rel = path.relative_to(base).as_posix()
    return {"path": rel, "name": path.name, "size": path.stat().st_size,
            "mtime": path.stat().st_mtime}


def _count_files(d: Path) -> int:
    return sum(1 for p in d.rglob("*") if p.is_file()) if d.is_dir() else 0


class _FetchManager:
    """单任务抓取管理:on_event 回调 → 广播给全部 SSE 订阅者。"""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self._merged: dict[str, Any] = {}
        self._subscribers: set[asyncio.Queue] = set()

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def broadcast(self, event: dict) -> None:
        self._merged.update(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                self._subscribers.discard(q)

    def snapshot(self) -> dict[str, Any] | None:
        return dict(self._merged) if self._merged else None


async def _run_fetch(fm: _FetchManager, site: SiteConfig, out_root: Path,
                     fresh: bool) -> None:
    """后台抓取任务:事件全部经 fm 广播,结束时广播 done。"""
    fm.broadcast({
        "type": "start", "site": site.site_name, "url": site.url,
        "formats": site.formats, "done": 0, "total": 0, "stage": "discover",
    })
    try:
        result = await run_site(
            site, out_root, fresh=fresh,
            on_event=lambda ev: fm.broadcast(ev),
        )
        fm.broadcast({
            "type": "done", "site": site.site_name, "ok": True,
            "pages": len(result.pages), "failed": len(result.failed),
            "adapter": result.adapter,
        })
    except Exception as e:
        fm.broadcast({
            "type": "done", "site": site.site_name, "ok": False,
            "error": f"{type(e).__name__}: {e}",
        })
    finally:
        fm.task = None


def create_app(out_root: Path | str = "omwb-out") -> FastAPI:
    """构建 FastAPI 应用。out_root 为站点输出根目录(含各站点 manifest.json)。"""
    root = Path(out_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fm = _FetchManager()

    app = FastAPI(title="oh-my-wikibro Web UI", version="0.1.0")
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    def _site_dir(name: str) -> Path:
        site_dir = (root / name).resolve()
        if not site_dir.is_relative_to(root) or not site_dir.is_dir():
            raise HTTPException(404, f"站点不存在: {name}")
        return site_dir

    def _manifest(site_dir: Path) -> dict[str, Any]:
        mf = site_dir / "manifest.json"
        if not mf.is_file():
            raise HTTPException(404, f"{site_dir.name} 缺少 manifest.json")
        try:
            return json.loads(mf.read_text(encoding="utf-8"))
        except Exception as e:
            raise HTTPException(500, f"manifest.json 解析失败: {e}")

    def _summary(site_dir: Path) -> dict[str, Any]:
        manifest = _manifest(site_dir)
        name = manifest.get("name") or site_dir.name
        urls = manifest.get("urls") or []
        failed = manifest.get("failed_urls") or []
        updated = max(
            (site_dir / "manifest.json").stat().st_mtime,
            max((p.stat().st_mtime for p in site_dir.rglob("*") if p.is_file()), default=0.0),
        )
        formats = {f: _count_files(site_dir / f) for f in FORMAT_DIRS}
        formats["corpus"] = [f for f in CORPUS_FILES if (site_dir / f).is_file()]
        return {
            "name": name,
            "url": manifest.get("url", ""),
            "adapter": manifest.get("adapter", ""),
            "source": manifest.get("source", ""),
            "pages": manifest.get("pages", len(urls)),
            "failed": manifest.get("failed", len(failed)),
            "chunks": _count_lines(site_dir / "corpus.jsonl"),
            "started_at": manifest.get("started_at", 0.0),
            "finished_at": manifest.get("finished_at", 0.0),
            "updated": updated,
            "formats": formats,
            "failed_urls": failed,
        }

    def _count_lines(p: Path) -> int:
        if not p.is_file():
            return 0
        n = 0
        try:
            with p.open(encoding="utf-8", errors="replace") as fh:
                for _ in fh:
                    n += 1
        except OSError:
            return 0
        return n

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/sites")
    def list_sites() -> dict[str, Any]:
        sites = []
        for mf in root.glob("*/manifest.json"):
            try:
                sites.append(_summary(mf.parent))
            except HTTPException:
                continue
        sites.sort(key=lambda s: s["updated"], reverse=True)
        return {"sites": sites}

    @app.post("/api/fetch")
    async def start_fetch(body: dict) -> JSONResponse:
        if fm.busy:
            raise HTTPException(409, "已有抓取任务在进行中,请等待完成后再试")
        url = (body.get("url") or "").strip()
        if not url:
            raise HTTPException(422, "url 不能为空")
        try:
            site = SiteConfig(
                url=url,
                name=(body.get("name") or "").strip() or None,
                formats=body.get("formats") or ["md", "json"],
                max_pages=int(body.get("limit") or 0),
                combined_pdf=bool(body.get("combined")),
            )
        except Exception as e:
            raise HTTPException(422, f"抓取参数无效: {e}")
        fresh = bool(body.get("fresh"))
        fm.task = asyncio.create_task(_run_fetch(fm, site, root, fresh))
        return JSONResponse({"ok": True, "site": site.site_name,
                             "message": f"已开始抓取 {site.site_name}"})

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        q = fm.subscribe()
        snapshot = fm.snapshot()

        async def gen():
            try:
                if snapshot is not None:
                    yield _sse(snapshot)
                while True:
                    yield _sse(await q.get())
            finally:
                fm.unsubscribe(q)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/site/{name}")
    def site_summary(name: str) -> dict[str, Any]:
        """站点详情:manifest 统计 + 失败页列表(含错误)+ 各格式文件数。"""
        return _summary(_site_dir(name))

    @app.get("/api/site/{name}/files")
    def site_files(name: str,
                   fmt: str = Query("md", pattern="^(md|json|pdf|corpus)$")) -> dict[str, Any]:
        site_dir = _site_dir(name)
        if fmt == "corpus":
            files = [_file_info(site_dir / f, site_dir)
                     for f in CORPUS_FILES if (site_dir / f).is_file()]
        else:
            sub = site_dir / fmt
            files = [_file_info(p, site_dir) for p in sorted(sub.rglob("*"))
                     if p.is_file()] if sub.is_dir() else []
        return {"site": name, "fmt": fmt, "files": files}

    @app.get("/api/site/{name}/file")
    def site_file(name: str, path: str = Query(...),
                  download: bool = False) -> Any:
        site_dir = _site_dir(name)
        target = (site_dir / path).resolve()
        if not target.is_relative_to(site_dir) or not target.is_file():
            raise HTTPException(404, f"文件不存在: {path}")
        if download:
            return FileResponse(target, filename=target.name)
        if target.suffix.lower() not in TEXT_EXTS:
            raise HTTPException(422, "二进制文件请使用下载链接")
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise HTTPException(500, f"读取失败: {e}")
        return {"path": path, "name": target.name,
                "size": target.stat().st_size, "content": content}

    @app.get("/api/verify")
    def verify(name: str = Query(..., alias="site")) -> dict[str, Any]:
        """校验报告:检查 manifest.urls 对应 md/json 文件是否齐全。

        TODO: 合并 verify-automation 分支后改用 verify_site(out_dir, site_name),
        本实现仅为占位轻量检查,保证前端与路由先行可用。
        """
        site_dir = _site_dir(name)
        manifest = _manifest(site_dir)
        urls = manifest.get("urls") or []
        missing = []
        for u in urls:
            rel = page_rel_path(u)
            if not (site_dir / "md" / f"{rel}.md").is_file() and \
               not (site_dir / "json" / f"{rel}.json").is_file():
                missing.append({"url": u, "expected": rel})
        return {
            "site": name,
            "checked": len(urls),
            "ok": len(urls) - len(missing),
            "missing": missing,
            "formats": {f: _count_files(site_dir / f) for f in FORMAT_DIRS},
            "corpus": [f for f in CORPUS_FILES if (site_dir / f).is_file()],
            "manifest": {k: manifest.get(k) for k in
                         ("url", "adapter", "source", "pages", "failed",
                          "started_at", "finished_at")},
            "generated_at": time.time(),
        }

    return app
