from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict

from db_store import get_store
from tenants_config import tenant_mapping_review_path


def _normalize_confirm_status(value: str) -> str:
    s = str(value or "").strip().lower()
    if s == "approved":
        return "approved"
    return "pending"


def load_mapping_review(tenant_id: str) -> Dict[str, dict]:
    store = get_store()
    data = store.load_mapping_review(tenant_id)
    if data:
        normalized: Dict[str, dict] = {}
        for mk, v in data.items():
            if not isinstance(v, dict):
                continue
            normalized[mk] = {
                **v,
                "confirm_status": _normalize_confirm_status(v.get("confirm_status", "pending")),
            }
        return normalized
    # Fallback migration from legacy file store
    path = tenant_mapping_review_path(tenant_id)
    if not path.exists():
        return {}
    try:
        legacy = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(legacy, dict):
        return {}
    for mk, v in legacy.items():
        if not isinstance(v, dict):
            continue
        store.upsert_mapping_review(
            tenant_id=tenant_id,
            map_key=str(mk),
            confirm_status=_normalize_confirm_status(v.get("confirm_status", "pending")),
            reviewer=str(v.get("reviewer", "legacy")),
            comment=str(v.get("comment", "")),
            target_mother_question_id=str(v.get("target_mother_question_id", "")),
        )
    return store.load_mapping_review(tenant_id)


def save_mapping_review(tenant_id: str, data: Dict[str, dict]) -> None:
    store = get_store()
    for mk, v in data.items():
        if not isinstance(v, dict):
            continue
        store.upsert_mapping_review(
            tenant_id=tenant_id,
            map_key=str(mk),
            confirm_status=_normalize_confirm_status(v.get("confirm_status", "pending")),
            reviewer=str(v.get("reviewer", "manual")),
            comment=str(v.get("comment", "")),
            target_mother_question_id=str(v.get("target_mother_question_id", "")),
        )
    # Keep file backup for compatibility
    path = tenant_mapping_review_path(tenant_id)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_mapping_review(
    tenant_id: str,
    map_key: str,
    confirm_status: str,
    reviewer: str,
    comment: str = "",
    target_mother_question_id: str = "",
) -> dict:
    record = get_store().upsert_mapping_review(
        tenant_id=tenant_id,
        map_key=str(map_key),
        confirm_status=_normalize_confirm_status(confirm_status),
        reviewer=reviewer,
        comment=comment,
        target_mother_question_id=target_mother_question_id,
    )
    # File backup for compatibility
    store = load_mapping_review(tenant_id)
    store[map_key] = record
    path = tenant_mapping_review_path(tenant_id)
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return record
