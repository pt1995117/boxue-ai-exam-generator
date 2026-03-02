from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from db_store import get_store
from tenants_config import tenant_material_registry_path


def _load_registry(path: Path) -> List[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _save_registry(path: Path, items: List[dict]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def register_material_version(
    tenant_id: str,
    material_version_id: str,
    file_path: str,
    checksum: str,
    status: str = "ready_for_review",
    slice_status: str | None = None,
    mapping_status: str | None = None,
    slice_error: str | None = None,
    mapping_error: str | None = None,
) -> dict:
    record = get_store().register_material_version(
        tenant_id=tenant_id,
        material_version_id=material_version_id,
        file_path=file_path,
        checksum=checksum,
        status=status,
    )
    # File backup for compatibility
    path = tenant_material_registry_path(tenant_id)
    items = _load_registry(path)
    items = [x for x in items if x.get("material_version_id") != material_version_id]
    if slice_status is not None:
        record["slice_status"] = slice_status
    if mapping_status is not None:
        record["mapping_status"] = mapping_status
    if slice_error is not None:
        record["slice_error"] = slice_error
    if mapping_error is not None:
        record["mapping_error"] = mapping_error
    items.append(record)
    _save_registry(path, items)
    return record


def set_effective_material_version(tenant_id: str, material_version_id: str) -> Optional[dict]:
    updated = get_store().set_effective_material_version(tenant_id, material_version_id)
    # File backup for compatibility
    path = tenant_material_registry_path(tenant_id)
    items = _load_registry(path)
    now = datetime.now(timezone.utc).isoformat()
    for rec in items:
        if rec.get("material_version_id") == material_version_id:
            rec["status"] = "effective"
            rec["effective_at"] = now
            updated = rec
        elif rec.get("status") == "effective":
            # Keep file backup aligned with DB: only one effective material is allowed.
            rec["status"] = "ready_for_review"
    _save_registry(path, items)
    return updated


def list_material_versions(tenant_id: str) -> List[dict]:
    rows = get_store().list_material_versions(tenant_id)
    file_items = _load_registry(tenant_material_registry_path(tenant_id))
    if rows:
        file_index = {str(x.get("material_version_id", "")).strip(): x for x in file_items if isinstance(x, dict)}
        merged: List[dict] = []
        for row in rows:
            rec = dict(row)
            mid = str(rec.get("material_version_id", "")).strip()
            extra = file_index.get(mid, {})
            for k in ("status", "slice_status", "mapping_status", "slice_error", "mapping_error"):
                if k in extra:
                    rec[k] = extra.get(k)
            merged.append(rec)
        return merged
    return file_items


def archive_material_version(tenant_id: str, material_version_id: str) -> Optional[dict]:
    updated = get_store().archive_material_version(tenant_id, material_version_id)
    path = tenant_material_registry_path(tenant_id)
    items = _load_registry(path)
    for rec in items:
        if rec.get("material_version_id") == material_version_id:
            rec["status"] = "archived"
            updated = rec if updated is None else updated
            break
    _save_registry(path, items)
    return updated


def delete_material_version(tenant_id: str, material_version_id: str) -> bool:
    deleted = get_store().delete_material_version(tenant_id, material_version_id)
    path = tenant_material_registry_path(tenant_id)
    items = _load_registry(path)
    next_items = [x for x in items if x.get("material_version_id") != material_version_id]
    if len(next_items) != len(items):
        _save_registry(path, next_items)
        deleted = True
    return deleted


def upsert_material_runtime(
    tenant_id: str,
    material_version_id: str,
    **fields,
) -> Optional[dict]:
    target = str(material_version_id).strip()
    if not target:
        return None
    path = tenant_material_registry_path(tenant_id)
    items = _load_registry(path)
    now = datetime.now(timezone.utc).isoformat()
    hit: Optional[dict] = None
    for rec in items:
        if str(rec.get("material_version_id", "")).strip() == target:
            hit = rec
            break
    if hit is None:
        hit = {
            "material_version_id": target,
            "file_path": "",
            "checksum": "",
            "status": fields.get("status", "archived"),
            "created_at": now,
            "effective_at": None,
        }
        items.append(hit)
    for k, v in fields.items():
        if v is not None:
            hit[k] = v
    hit["updated_at"] = now
    _save_registry(path, items)
    return hit
