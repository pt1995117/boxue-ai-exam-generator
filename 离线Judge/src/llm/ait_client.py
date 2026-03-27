from __future__ import annotations

import os


def normalize_ait_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        url = "https://openapi-ait.ke.com"
    if not url.endswith("/v1"):
        url = url + "/v1"
    return url


def _load_ait_config_file() -> None:
    """Load AIT/legacy env vars from project-root config files."""
    wanted = {
        "AIT_API_KEY",
        "AIT_BASE_URL",
        "AIT_MODEL",
        "AIT_MAX_TOKENS",
        # calculator runtime config
        "CALC_MODEL",
        "CALC_PROVIDER",
        "CALC_FALLBACK_MODEL",
        # legacy names
        "ARK_API_KEY",
        "ARK_BASE_URL",
    }
    for path in ("AIT_CONFIG.txt", "ARK_CONFIG.txt"):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k not in wanted:
                        continue
                    v = v.split("#", 1)[0].strip().strip("'").strip('"')
                    # Ignore template placeholders.
                    if not v or "在这里填写" in v or "your" in v.lower():
                        continue
                    if v and not os.getenv(k):
                        os.environ[k] = v
        except Exception:
            continue


def resolve_ait_api_key(explicit_key: str | None = None) -> str:
    _load_ait_config_file()
    return (
        explicit_key
        or os.getenv("AIT_API_KEY", "")
        or os.getenv("ARK_API_KEY", "")
        or ""
    )


def resolve_ait_model(explicit_model: str | None = None) -> str:
    _load_ait_config_file()
    return explicit_model or os.getenv("AIT_MODEL", "") or "gpt-5.2"


def resolve_ait_base_url() -> str:
    _load_ait_config_file()
    ait_url = os.getenv("AIT_BASE_URL", "").strip()
    if ait_url:
        return normalize_ait_base_url(ait_url)

    # legacy fallback: only accept ARK_BASE_URL if user already put AIT host there.
    legacy = os.getenv("ARK_BASE_URL", "").strip()
    if legacy and "openapi-ait.ke.com" in legacy:
        return normalize_ait_base_url(legacy)

    return normalize_ait_base_url("https://openapi-ait.ke.com")
