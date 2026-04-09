from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List

from runtime_paths import REPO_DATA_DIR, ensure_parent, repo_tenant_data_dir, runtime_data_root, runtime_root

BASE_DATA_DIR = runtime_data_root()
TENANTS_FILE = runtime_root() / "config" / "tenants.json"
LEGACY_TENANTS_FILE = REPO_DATA_DIR / "tenants.json"
DEFAULT_TENANTS: Dict[str, str] = {
    "hz": "杭州",
    "bj": "北京",
    "sh": "上海",
}

ROLE_PERMISSIONS: Dict[str, set[str]] = {
    "platform_admin": {
        "material.upload", "material.read", "material.effective",
        "slice.read", "slice.review", "map.read", "map.confirm",
        "gen.create", "gen.read", "export.read",
    },
    "city_admin": {
        "material.upload", "material.read", "material.effective",
        "slice.read", "slice.review", "map.read", "map.confirm",
        "gen.create", "gen.read", "export.read",
    },
    "city_teacher": {"material.upload", "material.read", "slice.read", "slice.review", "map.read", "map.confirm", "gen.read"},
    "city_viewer": {"material.read", "slice.read", "map.read", "gen.read"},
}


class TenantDataMissingError(FileNotFoundError):
    pass


def ensure_tenant_dirs(tenant_id: str) -> Path:
    root = BASE_DATA_DIR / tenant_id
    for sub in ("materials", "slices", "mapping", "bank", "exports", "audit"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _tenant_roots(tenant_id: str) -> List[Path]:
    runtime_root_dir = runtime_data_root() / tenant_id
    repo_root_dir = repo_tenant_data_dir(tenant_id)
    roots: List[Path] = []
    for candidate in (runtime_root_dir, repo_root_dir):
        if candidate not in roots:
            roots.append(candidate)
    return roots


def tenant_root(tenant_id: str) -> Path:
    return ensure_tenant_dirs(tenant_id)


def tenant_slices_dir(tenant_id: str) -> Path:
    return ensure_tenant_dirs(tenant_id) / "slices"


def tenant_slice_review_path(tenant_id: str) -> Path:
    return ensure_tenant_dirs(tenant_id) / "slices" / "slice_review.json"


def tenant_mapping_path(tenant_id: str) -> Path:
    return ensure_tenant_dirs(tenant_id) / "mapping" / "knowledge_question_mapping.json"


def tenant_mapping_review_path(tenant_id: str) -> Path:
    return ensure_tenant_dirs(tenant_id) / "mapping" / "mapping_review.json"


def tenant_bank_path(tenant_id: str) -> Path:
    return ensure_tenant_dirs(tenant_id) / "bank" / "local_question_bank.jsonl"


def tenant_material_registry_path(tenant_id: str) -> Path:
    return ensure_tenant_dirs(tenant_id) / "materials" / "registry.json"


def tenant_generation_template_path(tenant_id: str) -> Path:
    return ensure_tenant_dirs(tenant_id) / "materials" / "generation_templates.json"


def tenant_audit_log_path(tenant_id: str) -> Path:
    return ensure_tenant_dirs(tenant_id) / "audit" / "audit_log.jsonl"


def resolve_tenant_kb_path(tenant_id: str, fallback: str = "bot_knowledge_base.jsonl") -> Path:
    tid = str(tenant_id or "").strip()
    for root in _tenant_roots(tenant_id):
        slices_dir = root / "slices"
        candidates = sorted(
            [
                p
                for p in slices_dir.glob("*.jsonl")
                if p.name.startswith("knowledge_slices_")
                and p.name.endswith(".jsonl")
                and ".progress." not in p.name
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    if tid:
        raise TenantDataMissingError(
            f"租户 {tid} 缺少知识切片文件：请检查运行目录或仓库数据目录中的 {tid}/slices/knowledge_slices_*.jsonl"
        )
    return Path(fallback)


def resolve_tenant_history_path(tenant_id: str, fallback: str = "存量房买卖母卷ABCD.xls") -> Path:
    tid = str(tenant_id or "").strip()
    for root in _tenant_roots(tenant_id):
        tenant_materials = root / "materials"
        for name in ("history_questions.xlsx", "history_questions.xls", "history_questions.docx", "history_questions.txt", "history_questions.md"):
            tenant_candidate = tenant_materials / name
            if tenant_candidate.exists():
                return tenant_candidate
    if tid:
        raise TenantDataMissingError(
            f"租户 {tid} 缺少母题文件：请检查运行目录或仓库数据目录中的 {tid}/materials/history_questions.(xlsx|xls|docx|txt|md)"
        )
    return Path(fallback)


def resolve_tenant_from_env(default: str = "hz") -> str:
    value = os.getenv("TENANT_ID", default).strip()
    return value or default


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_tenant_record(tenant_id: str, value) -> Dict[str, object]:
    if isinstance(value, dict):
        name = str(value.get("name", tenant_id)).strip() or tenant_id
        is_active = bool(value.get("is_active", True))
        created_at = str(value.get("created_at", "")).strip() or _now_iso()
        updated_at = str(value.get("updated_at", "")).strip() or _now_iso()
        return {
            "tenant_id": tenant_id,
            "name": name,
            "is_active": is_active,
            "created_at": created_at,
            "updated_at": updated_at,
        }
    return {
        "tenant_id": tenant_id,
        "name": str(value).strip() or tenant_id,
        "is_active": True,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def _load_tenant_records() -> Dict[str, Dict[str, object]]:
    records: Dict[str, Dict[str, object]] = {
        tenant_id: _normalize_tenant_record(tenant_id, {"name": name, "is_active": True})
        for tenant_id, name in DEFAULT_TENANTS.items()
    }
    for source in (TENANTS_FILE, LEGACY_TENANTS_FILE):
        if not source.exists():
            continue
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key, value in data.items():
                    tenant_id = str(key).strip()
                    if tenant_id:
                        records[tenant_id] = _normalize_tenant_record(tenant_id, value)
        except Exception:
            pass
    return records


def _save_tenant_records(records: Dict[str, Dict[str, object]]) -> None:
    payload = {
        tenant_id: {
            "name": str(item.get("name", tenant_id)),
            "is_active": bool(item.get("is_active", True)),
            "created_at": str(item.get("created_at", _now_iso())),
            "updated_at": str(item.get("updated_at", _now_iso())),
        }
        for tenant_id, item in sorted(records.items())
    }
    ensure_parent(TENANTS_FILE)
    TENANTS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def list_tenants() -> List[Dict[str, str]]:
    records = _load_tenant_records()
    known = set(records.keys())
    for base_dir in (BASE_DATA_DIR, REPO_DATA_DIR):
        if not base_dir.exists():
            continue
        for d in sorted([p for p in base_dir.iterdir() if p.is_dir()]):
            if d.name not in known:
                records[d.name] = _normalize_tenant_record(d.name, {"name": d.name, "is_active": True})
                known.add(d.name)
    return [records[k] for k in sorted(records.keys())]


def upsert_tenant(tenant_id: str, name: str, is_active: bool | None = None) -> Dict[str, object]:
    tid = tenant_id.strip()
    tname = name.strip() or tid
    ensure_tenant_dirs(tid)
    records = _load_tenant_records()
    old = records.get(tid, {})
    now = _now_iso()
    records[tid] = {
        "tenant_id": tid,
        "name": tname,
        "is_active": bool(old.get("is_active", True) if is_active is None else is_active),
        "created_at": str(old.get("created_at", now)),
        "updated_at": now,
    }
    _save_tenant_records(records)
    return records[tid]


def set_tenant_status(tenant_id: str, is_active: bool) -> Dict[str, object]:
    records = _load_tenant_records()
    tid = tenant_id.strip()
    old = records.get(tid)
    if not old:
        raise KeyError("TENANT_NOT_FOUND")
    old["is_active"] = bool(is_active)
    old["updated_at"] = _now_iso()
    _save_tenant_records(records)
    return old


def delete_tenant(tenant_id: str) -> None:
    records = _load_tenant_records()
    tid = tenant_id.strip()
    # Tenant may come from auto-discovered data directory (not persisted in tenants.json).
    # In that case, allow delete flow to continue without raising.
    if tid not in records:
        return
    records.pop(tid, None)
    _save_tenant_records(records)
