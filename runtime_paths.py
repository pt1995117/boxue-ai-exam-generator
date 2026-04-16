from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

REPO_ROOT = Path(__file__).resolve().parent
REPO_DATA_DIR = REPO_ROOT / "data"
REPO_KEY_FILE = REPO_ROOT / "填写您的Key.txt"
REPO_TENANT_USER_FILE = REPO_ROOT / "tenant_users.json"

_DEFAULT_RUNTIME_ROOT = REPO_ROOT / ".local" / "runtime"
_DEFAULT_CACHE_ROOT = REPO_ROOT / ".local" / "cache"


def _expand(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def runtime_root() -> Path:
    return _expand(os.getenv("BOXUE_RUNTIME_DIR", str(_DEFAULT_RUNTIME_ROOT)))


def cache_root() -> Path:
    return _expand(os.getenv("BOXUE_CACHE_DIR", str(_DEFAULT_CACHE_ROOT)))


def runtime_data_root() -> Path:
    return runtime_root() / "data"


def runtime_config_root() -> Path:
    return runtime_root() / "config"


def runtime_db_path() -> Path:
    return runtime_root() / "db" / "admin_p0.db"


def runtime_key_file() -> Path:
    raw = os.getenv("BOXUE_KEY_FILE")
    return _expand(raw) if raw else runtime_config_root() / "填写您的Key.txt"


def runtime_tenant_user_file() -> Path:
    raw = os.getenv("BOXUE_TENANT_USER_FILE")
    return _expand(raw) if raw else runtime_config_root() / "tenant_users.json"


def repo_tenant_data_dir(tenant_id: str) -> Path:
    return REPO_DATA_DIR / str(tenant_id or "").strip()


def runtime_tenant_data_dir(tenant_id: str) -> Path:
    return runtime_data_root() / str(tenant_id or "").strip()


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_primary_key_file() -> Path:
    runtime_file = runtime_key_file()
    if runtime_file.exists() and runtime_file.stat().st_size > 0:
        return runtime_file
    if REPO_KEY_FILE.exists() and REPO_KEY_FILE.stat().st_size > 0:
        return REPO_KEY_FILE
    return runtime_file


def resolve_tenant_user_file() -> Path:
    runtime_file = runtime_tenant_user_file()
    if runtime_file.exists():
        return runtime_file
    if REPO_TENANT_USER_FILE.exists():
        return REPO_TENANT_USER_FILE
    return runtime_file


def load_primary_key_config() -> Dict[str, str]:
    cfg: Dict[str, str] = {}
    key_file = resolve_primary_key_file()
    if not key_file.exists():
        return cfg
    try:
        for line in key_file.read_text(encoding="utf-8").splitlines():
            raw = str(line).strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            key = str(k).strip()
            if key:
                cfg[key] = str(v).strip()
    except Exception:
        return {}
    return cfg
