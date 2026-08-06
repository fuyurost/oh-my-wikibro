"""抓取层:httpx 异步并发 + 单主机限速 + 重试 + sqlite HTML 缓存。

缓存使增量更新免费:非 --fresh 模式下命中缓存直接复用,不重新请求。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import httpx

from .config import SiteConfig
from .discover import UA


class PageFetcher:
    def __init__(self, site: SiteConfig, cache_db: Path | None, fresh: bool = False):
        self.site = site
        self.fresh = fresh
        self._conn: sqlite3.Connection | None = None
        if cache_db is not None:
            cache_db.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(cache_db)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, status INT, "
                "html TEXT, fetched_at REAL)"
            )
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(site.concurrency)
        self._pace_lock = asyncio.Lock()
        self._last_request = 0.0

    async def _pace(self) -> None:
        async with self._pace_lock:
            now = time.monotonic()
            wait = self.site.delay - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def _http_get(self, url: str) -> httpx.Response:
        assert self._client is not None
        last_err: Exception | None = None
        for attempt in range(4):
            await self._pace()
            try:
                r = await self._client.get(url)
                if r.status_code == 429 or r.status_code >= 500:
                    retry_after = r.headers.get("Retry-After", "")
                    wait = float(retry_after) if retry_after.isdigit() else 0.5 * (2**attempt)
                    await asyncio.sleep(min(wait, 30))
                    continue
                return r
            except httpx.HTTPError as e:
                last_err = e
                await asyncio.sleep(0.5 * (2**attempt))
        raise last_err or httpx.TransportError("fetch failed")

    async def fetch(self, url: str) -> tuple[int, str]:
        """返回 (status, html)。命中缓存且非 fresh 时直接返回缓存。"""
        if self._conn is not None and not self.fresh:
            row = self._conn.execute("SELECT status, html FROM pages WHERE url=?", (url,)).fetchone()
            if row and row[1]:
                return row[0], row[1]
        async with self._sem:
            r = await self._http_get(url)
            status, html = r.status_code, r.text
            if status == 200 and self._conn is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO pages (url, status, html, fetched_at) VALUES (?,?,?,?)",
                    (url, status, html, time.time()),
                )
                self._conn.commit()
            return status, html

    async def open(self) -> None:
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.site.timeout,
            headers={"User-Agent": UA},
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._conn is not None:
            self._conn.close()
