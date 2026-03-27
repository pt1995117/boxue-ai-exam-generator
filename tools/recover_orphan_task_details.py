#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from admin_api import (
    _maintenance_tenant_ids,
    _qa_gen_tasks_path,
    _read_jsonl,
    _append_jsonl,
    _get_qa_run_by_id,
    _build_run_questions_from_bank,
)

ORPHAN_MSG = "任务在服务重启后未恢复，已自动标记失败，请重新发起出题任务。"


def build_items_from_questions(questions: list[dict]) -> list[dict]:
    items: list[dict] = []
    for row in questions or []:
        if not isinstance(row, dict):
            continue
        final_json = row.get("final_json") if isinstance(row.get("final_json"), dict) else {}
        item = dict(final_json) if final_json else {
            "题干": str(row.get("question_text", "") or ""),
            "正确答案": str(row.get("answer", "") or ""),
            "解析": str(row.get("explanation", "") or ""),
            "来源路径": str(row.get("slice_path", "") or ""),
            "来源切片ID": row.get("slice_id"),
        }
        if not final_json:
            options = row.get("options") if isinstance(row.get("options"), list) else []
            for idx, opt in enumerate(options[:8], start=1):
                item[f"选项{idx}"] = str(opt or "")
        items.append(item)
    return items


def build_minimal_trace(questions: list[dict]) -> list[dict]:
    trace: list[dict] = []
    for idx, row in enumerate(questions or [], start=1):
        if not isinstance(row, dict):
            continue
        options = row.get("options") if isinstance(row.get("options"), list) else []
        steps = [
            {
                "seq": 1,
                "node": "system",
                "level": "success",
                "message": "题目已恢复",
                "detail": "服务重启后从持久化结果恢复已生成题目",
                "time": "",
                "elapsed_ms": 0,
                "delta_ms": 0,
                "run_id": 0,
            },
            {
                "seq": 2,
                "node": "writer",
                "level": "info",
                "message": "定稿题干",
                "detail": str(row.get("question_text", "") or ""),
                "time": "",
                "elapsed_ms": 0,
                "delta_ms": 0,
                "run_id": 0,
            },
        ]
        if options:
            steps.append(
                {
                    "seq": 3,
                    "node": "writer",
                    "level": "info",
                    "message": "定稿选项",
                    "detail": " | ".join(
                        f"{chr(64 + opt_idx)}. {str(opt or '').strip()}"
                        for opt_idx, opt in enumerate(options[:8], start=1)
                    ),
                    "time": "",
                    "elapsed_ms": 0,
                    "delta_ms": 0,
                    "run_id": 0,
                }
            )
        answer = str(row.get("answer", "") or "").strip()
        if answer:
            steps.append(
                {
                    "seq": len(steps) + 1,
                    "node": "critic",
                    "level": "success",
                    "message": "审核通过",
                    "detail": f"答案={answer}",
                    "time": "",
                    "elapsed_ms": 0,
                    "delta_ms": 0,
                    "run_id": 0,
                }
            )
        trace.append(
            {
                "index": idx,
                "target_index": idx,
                "question_id": str(row.get("question_id", "") or ""),
                "slice_id": row.get("slice_id"),
                "slice_path": str(row.get("slice_path", "") or ""),
                "elapsed_ms": 0,
                "saved": bool(row.get("saved", True)),
                "steps": steps,
                "final_json": dict(row.get("final_json")) if isinstance(row.get("final_json"), dict) else {},
                "critic_result": {"passed": True} if answer else {},
            }
        )
    return trace


def recover_questions(tenant_id: str, task: dict) -> tuple[list[dict], str]:
    run_id = str(task.get("run_id", "") or "").strip()
    if run_id:
        run = _get_qa_run_by_id(tenant_id, run_id)
        if isinstance(run, dict):
            questions = run.get("questions") if isinstance(run.get("questions"), list) else []
            if questions:
                return questions, "qa_run"
    if run_id:
        questions = _build_run_questions_from_bank(tenant_id, run_id)
        if questions:
            return questions, "bank_by_run"
    return [], ""


def should_recover(task: dict) -> bool:
    if not isinstance(task, dict):
        return False
    if str(task.get("status", "") or "").strip().lower() != "failed":
        return False
    errors = [str(x).strip() for x in (task.get("errors") or []) if str(x).strip()]
    if ORPHAN_MSG not in errors:
        return False
    if task.get("items") or task.get("process_trace"):
        return False
    return True


def recover_tenant(tenant_id: str) -> tuple[int, int]:
    path = _qa_gen_tasks_path(tenant_id)
    rows = _read_jsonl(path)
    latest_by_task_id: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict):
            tid = str(row.get("task_id", "")).strip()
            if tid:
                latest_by_task_id[tid] = row

    scanned = 0
    recovered = 0
    for task_id, row in latest_by_task_id.items():
        if not should_recover(row):
            continue
        scanned += 1
        questions, source = recover_questions(tenant_id, row)
        if not questions:
            continue
        patched = deepcopy(row)
        patched["items"] = build_items_from_questions(questions)
        patched["process_trace"] = build_minimal_trace(questions)
        patched["generated_count"] = max(int(patched.get("generated_count", 0) or 0), len(questions))
        patched["saved_count"] = max(
            int(patched.get("saved_count", 0) or 0),
            sum(1 for q in questions if isinstance(q, dict) and q.get("saved", True)),
        )
        progress = patched.get("progress") if isinstance(patched.get("progress"), dict) else {}
        patched["progress"] = {
            "current": max(int(progress.get("current", 0) or 0), int(patched["generated_count"])),
            "total": max(int(progress.get("total", 0) or 0), int((patched.get("request") or {}).get("num_questions", 0) or 0)),
        }
        patched["recovered_from"] = source
        patched["recovered_trace_count"] = len(patched["process_trace"])
        _append_jsonl(path, patched)
        recovered += 1
    return scanned, recovered


def main() -> int:
    total_scanned = 0
    total_recovered = 0
    for tenant_id in _maintenance_tenant_ids():
        scanned, recovered = recover_tenant(tenant_id)
        total_scanned += scanned
        total_recovered += recovered
        print(json.dumps({"tenant_id": tenant_id, "candidate_tasks": scanned, "recovered_tasks": recovered}, ensure_ascii=False))
    print(json.dumps({"total_candidate_tasks": total_scanned, "total_recovered_tasks": total_recovered}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
