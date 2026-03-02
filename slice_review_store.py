from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict

from db_store import get_store
from tenants_config import tenant_slice_review_path


def load_slice_review(tenant_id: str) -> Dict[str, dict]:
    store = get_store()
    data = store.load_slice_review(tenant_id)
    if data:
        return data
    # Fallback migration from legacy file store
    path = tenant_slice_review_path(tenant_id)
    if not path.exists():
        return {}
    try:
        legacy = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(legacy, dict):
        return {}
    for k, v in legacy.items():
        if not str(k).isdigit() or not isinstance(v, dict):
            continue
        store.upsert_slice_review(
            tenant_id=tenant_id,
            slice_id=int(k),
            review_status=str(v.get("review_status", "pending")),
            reviewer=str(v.get("reviewer", "legacy")),
            comment=str(v.get("comment", "")),
        )
    return store.load_slice_review(tenant_id)


def save_slice_review(tenant_id: str, data: Dict[str, dict]) -> None:
    store = get_store()
    for k, v in data.items():
        if not str(k).isdigit() or not isinstance(v, dict):
            continue
        store.upsert_slice_review(
            tenant_id=tenant_id,
            slice_id=int(k),
            review_status=str(v.get("review_status", "pending")),
            reviewer=str(v.get("reviewer", "manual")),
            comment=str(v.get("comment", "")),
        )
    # Keep file backup for compatibility
    path = tenant_slice_review_path(tenant_id)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_slice_review(
    tenant_id: str,
    slice_id: int,
    review_status: str,
    reviewer: str,
    comment: str = "",
) -> dict:
    record = get_store().upsert_slice_review(tenant_id, int(slice_id), review_status, reviewer, comment)
    # File backup for compatibility
    store = load_slice_review(tenant_id)
    store[str(slice_id)] = record
    path = tenant_slice_review_path(tenant_id)
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return record
