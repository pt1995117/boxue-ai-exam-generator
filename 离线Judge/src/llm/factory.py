from __future__ import annotations

import os
from typing import Any

from .ait_client import resolve_ait_api_key, resolve_ait_base_url, resolve_ait_model


def build_llm(
    *,
    provider: str = "openai",
    model: str | None = None,
    temperature: float = 0,
    api_key: str | None = None,
) -> Any:
    provider = provider.lower().strip()

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise ValueError("缺少 OPENAI_API_KEY")
        return ChatOpenAI(
            model=model or os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=temperature,
            api_key=key,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("缺少 ANTHROPIC_API_KEY")
        return ChatAnthropic(
            model=model or os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
            temperature=temperature,
            api_key=key,
        )

    if provider == "ark":
        raise ValueError("provider=ark 已禁用，请改用 --provider ait")

    if provider == "ait":
        from langchain_openai import ChatOpenAI

        key = resolve_ait_api_key(api_key)
        if not key:
            raise ValueError("缺少 AIT_API_KEY（或 ARK_API_KEY）")

        return ChatOpenAI(
            model=resolve_ait_model(model),
            temperature=temperature,
            api_key=key,
            base_url=resolve_ait_base_url(),
            max_tokens=int(os.getenv("AIT_MAX_TOKENS", os.getenv("ARK_MAX_TOKENS", "2048"))),
        )

    raise ValueError(f"不支持的 provider: {provider}")
