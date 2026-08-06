"""Playwright 浏览器兜底:SPA 文档站渲染后再提取。"""

from __future__ import annotations

from playwright.sync_api import Error as PWError


def render_url(url: str, timeout: float = 30.0, wait_ms: int = 2500) -> str | None:
    """无头 Chromium 渲染 URL,返回渲染后 HTML;失败返回 None。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PWError as e:
            raise RuntimeError(f"Chromium 未安装: {e}\n请执行: playwright install chromium") from e
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(wait_ms)
            return page.content()
        except Exception:
            return None
        finally:
            browser.close()
