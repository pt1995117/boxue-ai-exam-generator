#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def latest_by_task(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        task_id = str(row.get("task_id", "")).strip()
        if not task_id:
            continue
        latest[task_id] = row
    return list(latest.values())


def load_kb_map(kb_path: Path) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not kb_path.exists():
        return out
    for idx, line in enumerate(kb_path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out[idx] = obj
    return out


def build_testset(repo_root: Path, tenant_id: str) -> dict[str, Any]:
    gen_tasks_path = repo_root / "data" / tenant_id / "audit" / "gen_tasks.jsonl"
    all_rows = read_jsonl(gen_tasks_path)
    rows = latest_by_task(all_rows)

    # material -> slice_id -> aggregate
    agg: dict[str, dict[int, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))

    for task in rows:
        material_version_id = str(task.get("material_version_id", "")).strip()
        if not material_version_id:
            continue
        for q in task.get("process_trace") or []:
            if not isinstance(q, dict):
                continue
            critic_result = q.get("critic_result") if isinstance(q.get("critic_result"), dict) else {}
            if not critic_result or critic_result.get("passed") is not False:
                continue
            slice_id = q.get("slice_id")
            if not isinstance(slice_id, int):
                continue

            row = agg[material_version_id].get(slice_id)
            if not row:
                row = {
                    "slice_id": slice_id,
                    "slice_path": str(q.get("slice_path", "")).strip(),
                    "reject_count": 0,
                    "fail_type_counts": {},
                    "sample_reasons": [],
                }
                agg[material_version_id][slice_id] = row

            row["reject_count"] = int(row.get("reject_count", 0) or 0) + 1
            fail_type_counts = row.get("fail_type_counts") if isinstance(row.get("fail_type_counts"), dict) else {}
            for fail_type in critic_result.get("fail_types") or []:
                key = str(fail_type).strip()
                if not key:
                    continue
                fail_type_counts[key] = int(fail_type_counts.get(key, 0) or 0) + 1
            row["fail_type_counts"] = fail_type_counts

            reason = str(critic_result.get("reason", "")).strip()
            if reason:
                samples = row.get("sample_reasons") if isinstance(row.get("sample_reasons"), list) else []
                first_line = reason.split("\n")[0][:240]
                if first_line and first_line not in samples:
                    samples.append(first_line)
                row["sample_reasons"] = samples[:3]

    materials: list[dict[str, Any]] = []
    for material_version_id, slice_map in sorted(agg.items()):
        kb_path = (
            repo_root
            / "data"
            / tenant_id
            / "slices"
            / f"knowledge_slices_{material_version_id}.jsonl"
        )
        kb_map = load_kb_map(kb_path)
        slices: list[dict[str, Any]] = []
        for slice_id, row in sorted(
            slice_map.items(),
            key=lambda kv: (-int(kv[1].get("reject_count", 0) or 0), int(kv[0])),
        ):
            kb = kb_map.get(slice_id) or {}
            slice_path = str(row.get("slice_path", "")).strip() or str(kb.get("完整路径", "")).strip()
            mastery = str(kb.get("掌握程度", "")).strip()
            item = {
                "slice_id": slice_id,
                "slice_path": slice_path,
                "mastery": mastery,
                "reject_count": int(row.get("reject_count", 0) or 0),
                "fail_type_counts": row.get("fail_type_counts") or {},
                "sample_reasons": row.get("sample_reasons") or [],
            }
            slices.append(item)

        materials.append(
            {
                "material_version_id": material_version_id,
                "slice_count": len(slices),
                "total_reject_records": sum(int(x.get("reject_count", 0) or 0) for x in slices),
                "slices": slices,
                "task_payload_template": {
                    "task_name": "0.6回归测试",
                    "material_version_id": material_version_id,
                    "gen_scope_mode": "per_slice",
                    "slice_ids": [x["slice_id"] for x in slices],
                    "num_questions": len(slices),
                    "question_type": "单选题",
                    "generation_mode": "随机",
                    "difficulty": "随机",
                    "save_to_bank": False,
                },
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "source_files": {
            "gen_tasks": str(gen_tasks_path),
            "slices_dir": str(repo_root / "data" / tenant_id / "slices"),
        },
        "materials": sorted(materials, key=lambda x: (-int(x.get("slice_count", 0) or 0), str(x.get("material_version_id", "")))),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build critic-rejected risky slice testset from historical generate task traces.")
    parser.add_argument("--tenant", default="sh", help="tenant id, e.g. sh")
    parser.add_argument(
        "--output",
        default="",
        help="output json path; default: data/<tenant>/audit/critic_rejected_slice_testset.json",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    out_path = Path(args.output).resolve() if args.output else (
        repo_root / "data" / args.tenant / "audit" / "critic_rejected_slice_testset.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = build_testset(repo_root, args.tenant)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out_path))
    print(f"materials={len(payload.get('materials', []))}")


if __name__ == "__main__":
    main()
