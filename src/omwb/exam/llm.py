"""OpenAI 兼容 LLM 客户端:配置解析 + chat/completions + JSON 容错解析。

配置优先级:显式参数 > 环境变量 OMWB_LLM_BASE_URL / OMWB_LLM_API_KEY / OMWB_LLM_MODEL。
请求:POST {base}/v1/chat/completions,超时 120s,失败重试 2 次(间隔 2s);
JSON 解析:优先 response_format json_object(服务端不支持则忽略)→ 提取 ```json 围栏块 → 全文解析。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

import httpx

ENV_BASE_URL = "OMWB_LLM_BASE_URL"
ENV_API_KEY = "OMWB_LLM_API_KEY"
ENV_MODEL = "OMWB_LLM_MODEL"

MISSING_CONFIG_MSG = (
    "未配置 LLM:设置环境变量 OMWB_LLM_BASE_URL/OMWB_LLM_API_KEY/OMWB_LLM_MODEL,"
    "或 --base-url/--api-key/--model"
)

MAX_ATTEMPTS = 3  # 首次调用 + 失败重试 2 次
RETRY_INTERVAL = 2.0  # 重试间隔(秒);测试可 monkeypatch 为 0
TIMEOUT = 120.0


class LLMError(Exception):
    """LLM 调用或响应解析失败。"""


class LLMConfigError(LLMError):
    """缺少 LLM 配置(未设置环境变量/未传参数)。"""


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str = ""
    # 测试注入 httpx.MockTransport;生产为 None
    transport: httpx.BaseTransport | None = None

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"


def resolve_config(base_url: str | None = None, api_key: str | None = None,
                   model: str | None = None) -> LLMConfig:
    """按 显式参数 > 环境变量 解析 LLM 配置;base_url/model 缺失抛 LLMConfigError。"""
    base_url = (base_url or os.environ.get(ENV_BASE_URL) or "").strip()
    api_key = (api_key if api_key is not None else os.environ.get(ENV_API_KEY, "")).strip()
    model = (model or os.environ.get(ENV_MODEL) or "").strip()
    if not base_url or not model:
        raise LLMConfigError(MISSING_CONFIG_MSG)
    return LLMConfig(base_url=base_url, model=model, api_key=api_key)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str):
    """按 围栏块 → 全文 顺序提取 JSON;全失败抛 LLMError。"""
    if not isinstance(text, str) or not text.strip():
        raise LLMError(f"LLM 返回空内容: {text!r}")
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM 响应不是合法 JSON(围栏提取与全文解析均失败): {text[:200]!r} ({e})") from e


def chat_json(config: LLMConfig, messages: list[dict], temperature: float = 0.4,
              response_format: str | None = "json_object") -> object:
    """调用 OpenAI 兼容 chat/completions,返回解析后的 JSON;失败重试 2 次。"""
    payload: dict = {"model": config.model, "messages": messages, "temperature": temperature}
    use_format = response_format is not None
    if use_format:
        payload["response_format"] = {"type": response_format}
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    last_err: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with httpx.Client(timeout=TIMEOUT, transport=config.transport) as client:
                resp = client.post(config.endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return extract_json(content)
        except httpx.HTTPStatusError as e:
            # 服务端不支持 response_format(json_object)→ 去掉该参数重试
            if use_format and e.response.status_code == 400 and "response_format" in e.response.text:
                use_format = False
                payload.pop("response_format", None)
                last_err = e
                continue
            last_err = e
        except (httpx.TimeoutException, httpx.TransportError, KeyError, IndexError,
                json.JSONDecodeError, LLMError) as e:
            last_err = e
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(RETRY_INTERVAL)
    raise LLMError(f"LLM 调用失败(已重试 {MAX_ATTEMPTS - 1} 次): {last_err}")
