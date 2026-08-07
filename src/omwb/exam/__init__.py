"""开放性试题:LLM 生成 / 批注式代码审查 / 自动举一反三。"""

from __future__ import annotations

from .generate import generate_exam
from .llm import LLMConfig, LLMConfigError, LLMError, resolve_config
from .review import review_code
from .variants import add_variants, generate_variants

__all__ = [
    "generate_exam",
    "review_code",
    "generate_variants",
    "add_variants",
    "LLMConfig",
    "LLMConfigError",
    "LLMError",
    "resolve_config",
]
