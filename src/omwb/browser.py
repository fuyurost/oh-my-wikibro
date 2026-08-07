"""Playwright 浏览器兜底:SPA 文档站渲染后再提取。"""

from __future__ import annotations

from playwright.sync_api import Error as PWError


def render_url(url: str, timeout: float = 30.0, wait_ms: int = 2500) -> tuple[str | None, str | None]:
    """无头 Chromium 渲染 URL,返回 (html, error)。

    成功:html 为渲染后 HTML,error 为 None;失败:html 为 None,error 为真实原因
    (不再静默吞掉异常)。
    """
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                page.wait_for_timeout(wait_ms)
                return page.content(), None
            finally:
                browser.close()
    except PWError as e:
        if "executable" in str(e).lower():
            return None, f"Chromium 未安装: {e}\n请执行: playwright install chromium"
        return None, f"{type(e).__name__}: {e}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
