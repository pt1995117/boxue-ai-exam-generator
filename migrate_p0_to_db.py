from __future__ import annotations

import json
from pathlib import Path

from db_store import get_store
from tenants_config import BASE_DATA_DIR


def migrate_tenant(tenant_id: str) -> dict:
    store = get_store()
    stats = {"slice_review": 0, "mapping_review": 0, "material_registry": 0}

    slice_path = BASE_DATA_DIR / tenant_id / "slices" / "slice_review.json"
    if slice_path.exists():
        data = json.loads(slice_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                if str(k).isdigit() and isinstance(v, dict):
                    store.upsert_slice_review(
                        tenant_id=tenant_id,
                        slice_id=int(k),
                        review_status=str(v.get("review_status", "pending")),
                        reviewer=str(v.get("reviewer", "legacy")),
                        comment=str(v.get("comment", "")),
                    )
                    stats["slice_review"] += 1

    map_path = BASE_DATA_DIR / tenant_id / "mapping" / "mapping_review.json"
    if map_path.exists():
        data = json.loads(map_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for mk, v in data.items():
                if isinstance(v, dict):
                    store.upsert_mapping_review(
                        tenant_id=tenant_id,
                        map_key=str(mk),
                        confirm_status=str(v.get("confirm_status", "auto_pending")),
                        reviewer=str(v.get("reviewer", "legacy")),
                        comment=str(v.get("comment", "")),
                        target_mother_question_id=str(v.get("target_mother_question_id", "")),
                    )
                    stats["mapping_review"] += 1

    registry_path = BASE_DATA_DIR / tenant_id / "materials" / "registry.json"
    if registry_path.exists():
        data = json.loads(registry_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for rec in data:
                if not isinstance(rec, dict):
                    continue
                store.register_material_version(
                    tenant_id=tenant_id,
                    material_version_id=str(rec.get("material_version_id", "")),
                    file_path=str(rec.get("file_path", "")),
                    checksum=str(rec.get("checksum", "")),
                    status=str(rec.get("status", "ready_for_review")),
                )
                stats["material_registry"] += 1
                if rec.get("status") == "effective":
                    store.set_effective_material_version(tenant_id, str(rec.get("material_version_id", "")))

    return stats


def main() -> None:
    if not BASE_DATA_DIR.exists():
        print("No data directory found.")
        return
    for d in sorted([p for p in BASE_DATA_DIR.iterdir() if p.is_dir()]):
        stats = migrate_tenant(d.name)
        print(d.name, stats)


if __name__ == "__main__":
    main()
