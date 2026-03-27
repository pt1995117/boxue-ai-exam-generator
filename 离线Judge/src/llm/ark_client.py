from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class ArkInvokeResponse:
    content: str
    usage_metadata: dict[str, int]
    response_metadata: dict[str, Any]


class ArkChatClient:
    """Volcengine Ark chat-completions wrapper with .invoke interface."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
        temperature: float = 0,
        max_tokens: int = 2048,
        project_name: str | None = None,
    ):
        try:
            from volcenginesdkarkruntime import Ark
        except Exception as exc:
            raise RuntimeError(
                "未安装 volcenginesdkarkruntime，请先安装相关依赖。"
            ) from exc

        self._client = Ark(api_key=api_key, base_url=base_url)
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._project_name = project_name or ""

    def invoke(self, prompt_input: Any) -> ArkInvokeResponse:
        messages = self._to_messages(prompt_input)
        extra_headers = {"X-Project-Name": self._project_name} if self._project_name else None

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                extra_headers=extra_headers,
            )
        except Exception:
            return self._invoke_with_curl(messages)

        content = ""
        choices = getattr(resp, "choices", None) or []
        if choices:
            message = getattr(choices[0], "message", None)
            if message is not None:
                content = str(getattr(message, "content", "") or "")

        usage = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", prompt_tokens + completion_tokens) or 0)

        return ArkInvokeResponse(
            content=content,
            usage_metadata={
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
            },
            response_metadata={
                "token_usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }
            },
        )

    def _invoke_with_curl(self, messages: list[dict[str, str]]) -> ArkInvokeResponse:
        """Fallback path when Python DNS in sandbox fails but curl still works."""
        endpoint = self._base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        cmd = [
            "curl",
            "-sS",
            "-X",
            "POST",
            endpoint,
            "-H",
            f"Authorization: Bearer {self._api_key}",
            "-H",
            "Content-Type: application/json",
        ]
        if self._project_name:
            cmd.extend(["-H", f"X-Project-Name: {self._project_name}"])
        cmd.extend(["--data-raw", json.dumps(payload, ensure_ascii=False)])

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"Ark curl fallback failed: {proc.stderr.strip()}")

        data = json.loads(proc.stdout)
        if "error" in data:
            raise RuntimeError(f"Ark API error: {data['error']}")

        choices = data.get("choices") or []
        content = ""
        if choices:
            content = str(((choices[0] or {}).get("message") or {}).get("content", "") or "")
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)

        return ArkInvokeResponse(
            content=content,
            usage_metadata={
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
            },
            response_metadata={
                "token_usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }
            },
        )

    @staticmethod
    def _to_messages(prompt_input: Any) -> list[dict[str, str]]:
        if isinstance(prompt_input, str):
            return [{"role": "user", "content": prompt_input}]

        # ChatPromptValue / message-like
        to_messages = getattr(prompt_input, "to_messages", None)
        if callable(to_messages):
            msgs = []
            for m in to_messages():
                m_type = getattr(m, "type", "human")
                role = "user"
                if m_type in {"system"}:
                    role = "system"
                elif m_type in {"ai", "assistant"}:
                    role = "assistant"
                msgs.append({"role": role, "content": str(getattr(m, "content", "") or "")})
            return msgs

        return [{"role": "user", "content": str(prompt_input)}]


def resolve_ark_api_key(explicit_key: str | None = None) -> str:
    _load_ark_config_file()
    key = explicit_key or os.getenv("ARK_API_KEY", "")
    if not key:
        key = os.getenv("VOLC_ACCESS_KEY_ID", "")
    return key


def _load_ark_config_file(path: str = "ARK_CONFIG.txt") -> None:
    """
    Load ARK env vars from project-root text config.
    Format: KEY=VALUE (supports inline comments after '#').
    Existing env vars take precedence.
    """
    if not os.path.exists(path):
        return

    wanted = {
        "ARK_API_KEY",
        "ARK_BASE_URL",
        "ARK_PROJECT_NAME",
        "ARK_MODEL",
        "ARK_MAX_TOKENS",
        "VOLC_ACCESS_KEY_ID",
        "VOLC_SECRET_ACCESS_KEY",
    }

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key not in wanted:
                    continue
                # Remove inline comments and quotes.
                val = val.split("#", 1)[0].strip().strip("'").strip('"')
                if val and not os.getenv(key):
                    os.environ[key] = val
    except Exception:
        # Silent fail: do not block runtime if local config is malformed.
        return
