from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any

_OBS = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "latency_ms": 0,
    "calls": 0,
    "failed_calls": 0,
    "last_error": "",
    "last_raw_response": "",
    "last_raw_truncated": False,
}


def reset_observability() -> None:
    _OBS["prompt_tokens"] = 0
    _OBS["completion_tokens"] = 0
    _OBS["latency_ms"] = 0
    _OBS["calls"] = 0
    _OBS["failed_calls"] = 0
    _OBS["last_error"] = ""
    _OBS["last_raw_response"] = ""
    _OBS["last_raw_truncated"] = False


def get_observability() -> dict[str, Any]:
    return dict(_OBS)


def _relax_json(s: str) -> str:
    """Remove trailing commas before } or ] to tolerate some model output."""
    s = re.sub(r",(\s*})", r"\1", s)
    s = re.sub(r",(\s*])", r"\1", s)
    return s


def _extract_json_block(text: str) -> dict[str, Any] | None:
    text = text.strip()
    # Drop leading non-JSON lines (e.g. "根据分析..." or "输出如下：")
    for i, line in enumerate(text.split("\n")):
        stripped = line.strip()
        if stripped.startswith("{"):
            text = "\n".join(text.split("\n")[i:])
            break
    text = text.strip()

    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(text)

    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            pass
        try:
            return json.loads(_relax_json(c))
        except json.JSONDecodeError:
            pass

    # Last resort: first {...} (greedy to end of string)
    obj = re.search(r"\{[\s\S]*\}", text)
    if obj:
        raw = obj.group(0)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            try:
                return json.loads(_relax_json(raw))
            except json.JSONDecodeError:
                return None
    return None


class ReliableLLMClient:
    """Wrap Runnable-like LLM with timeout/retry/json-parse/fallback."""

    def __init__(self, llm: Any, *, timeout_seconds: float = 180, retries: int = 3):
        self.llm = llm
        env_timeout = os.getenv("LLM_TIMEOUT_SECONDS", "").strip()
        env_retries = os.getenv("LLM_RETRIES", "").strip()
        env_backoff = os.getenv("LLM_RETRY_BACKOFF_SECONDS", "").strip()
        self.timeout_seconds = float(env_timeout) if env_timeout else timeout_seconds
        self.retries = int(env_retries) if env_retries else retries
        self.retry_backoff_seconds = float(env_backoff) if env_backoff else 1.0

    def invoke_text(self, prompt_input: Any) -> str:
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            ex: ThreadPoolExecutor | None = None
            try:
                start = time.perf_counter()
                ex = ThreadPoolExecutor(max_workers=1)
                fut = ex.submit(self.llm.invoke, prompt_input)
                out = fut.result(timeout=self.timeout_seconds)
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                prompt_tokens, completion_tokens = self._extract_usage(out)
                _OBS["latency_ms"] += elapsed_ms
                _OBS["prompt_tokens"] += prompt_tokens
                _OBS["completion_tokens"] += completion_tokens
                _OBS["calls"] += 1
                ex.shutdown(wait=False, cancel_futures=True)
                return self._normalize_text(out)
            except FuturesTimeoutError as err:
                if ex is not None:
                    ex.shutdown(wait=False, cancel_futures=True)
                last_err = TimeoutError(f"LLM timeout after {self.timeout_seconds}s")
            except Exception as err:
                if ex is not None:
                    ex.shutdown(wait=False, cancel_futures=True)
                last_err = err
            if attempt < self.retries:
                # Exponential backoff to smooth transient provider/network jitter.
                sleep_s = self.retry_backoff_seconds * (2**attempt)
                time.sleep(sleep_s)
        _OBS["failed_calls"] += 1
        _OBS["last_error"] = str(last_err or "unknown_llm_error")
        raise RuntimeError(f"LLM invocation failed after retries: {last_err}")

    def invoke_json(self, prompt_input: Any, *, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = self.invoke_text(prompt_input)
        except Exception:
            return fallback

        parsed = _extract_json_block(raw)
        if not isinstance(parsed, dict):
            raw_text = str(raw or "")
            _OBS["last_raw_response"] = raw_text
            _OBS["last_raw_truncated"] = False
            return fallback

        merged = dict(fallback)
        merged.update(parsed)
        return merged

    @staticmethod
    def _normalize_text(out: Any) -> str:
        if hasattr(out, "content"):
            content = getattr(out, "content")
            if isinstance(content, list):
                return "\n".join(
                    x.get("text", str(x)) if isinstance(x, dict) else str(x)
                    for x in content
                )
            return str(content)
        return str(out)

    @staticmethod
    def _extract_usage(out: Any) -> tuple[int, int]:
        prompt_tokens = 0
        completion_tokens = 0

        usage_meta = getattr(out, "usage_metadata", None)
        usage_meta_found = False
        if isinstance(usage_meta, dict):
            usage_meta_found = True
            prompt_tokens += int(usage_meta.get("input_tokens", 0) or 0)
            completion_tokens += int(usage_meta.get("output_tokens", 0) or 0)

        # Avoid double counting when providers expose identical usage in both usage_metadata
        # and response_metadata.token_usage (e.g. Ark wrapper).
        if not usage_meta_found:
            resp_meta = getattr(out, "response_metadata", None)
            if isinstance(resp_meta, dict):
                token_usage = resp_meta.get("token_usage", {})
                if isinstance(token_usage, dict):
                    prompt_tokens += int(token_usage.get("prompt_tokens", 0) or 0)
                    completion_tokens += int(token_usage.get("completion_tokens", 0) or 0)

        return prompt_tokens, completion_tokens
