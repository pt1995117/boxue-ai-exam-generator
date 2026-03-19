#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run trace & CPVQ logic: build_qa_run_payload with mock process_trace, then optionally stream 1 question.
No Flask server required.
"""
import os
import sys
from pathlib import Path

# Ensure project root
os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, os.getcwd())

def _safe_div(a: float, b: float) -> float:
    if b is None or b == 0:
        return 0.0
    return float(a) / float(b)


def test_cpvq_and_trace_sync():
    print("=" * 60)
    print("1. Test _build_qa_run_payload (CPVQ & batch_metrics)")
    print("=" * 60)

    # Minimal mock: one question with llm_trace, saved
    tenant_id = "wh"
    process_trace = [
        {
            "question_id": "q1",
            "index": 1,
            "saved": True,
            "critic_result": {"passed": True},
            "llm_summary": {"total_llm_calls": 3, "error_calls": 0},
            "llm_trace": [
                {"node": "router.route", "model": "gpt-4o-mini", "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "latency_ms": 500.0, "success": True},
                {"node": "specialist.draft", "model": "deepseek-chat", "prompt_tokens": 800, "completion_tokens": 400, "total_tokens": 1200, "latency_ms": 3000.0, "success": True},
                {"node": "critic.review", "model": "gpt-4o", "prompt_tokens": 600, "completion_tokens": 200, "total_tokens": 800, "latency_ms": 2000.0, "success": True},
            ],
        }
    ]
    generated_count = 1
    saved_count = 1
    errors = []
    started_at = "2025-03-14T10:00:00Z"
    ended_at = "2025-03-14T10:01:00Z"

    try:
        from admin_api import _build_qa_run_payload
    except Exception as e:
        print(f"Import admin_api failed: {e}")
        return False

    payload = _build_qa_run_payload(
        tenant_id=tenant_id,
        run_id="run_test_1",
        material_version_id="v1",
        config_payload={},
        process_trace=process_trace,
        generated_count=generated_count,
        saved_count=saved_count,
        errors=errors,
        started_at=started_at,
        ended_at=ended_at,
    )

    bm = payload.get("batch_metrics") or {}
    cost_summary = payload.get("cost_summary") or {}
    print("batch_metrics (excerpt):")
    print(f"  generated_count = {bm.get('generated_count')}")
    print(f"  saved_count     = {bm.get('saved_count')}")
    print(f"  total_cost      = {bm.get('total_cost')}")
    print(f"  avg_cost_per_question = {bm.get('avg_cost_per_question')}")
    print(f"  cpvq            = {bm.get('cpvq')}")
    print(f"  cpvq_currency   = {bm.get('cpvq_currency')}")
    print("cost_summary:")
    print(f"  total_cost = {cost_summary.get('total_cost')}")
    print(f"  by_node    = {cost_summary.get('by_node')}")
    print(f"  by_model   = {cost_summary.get('by_model')}")

    # Assertions
    assert bm.get("saved_count") == 1, "saved_count should be 1"
    assert bm.get("cpvq") is not None, "cpvq should be set when saved_count > 0"
    assert bm.get("cpvq") == bm.get("total_cost"), "cpvq should equal total_cost when saved_count=1"
    print("\n[OK] CPVQ and batch_metrics look correct (saved_count=1).")
    return True


def test_cpvq_zero_saved():
    print("\n" + "=" * 60)
    print("2. Test CPVQ when saved_count=0 (should be None)")
    print("=" * 60)

    tenant_id = "wh"
    process_trace = [
        {
            "question_id": "q1",
            "index": 1,
            "saved": False,
            "critic_result": {"passed": False},
            "llm_summary": {"total_llm_calls": 2, "error_calls": 0},
            "llm_trace": [
                {"node": "router.route", "model": "gpt-4o-mini", "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "latency_ms": 500.0, "success": True},
                {"node": "specialist.draft", "model": "deepseek-chat", "prompt_tokens": 800, "completion_tokens": 400, "total_tokens": 1200, "latency_ms": 3000.0, "success": True},
            ],
        }
    ]

    try:
        from admin_api import _build_qa_run_payload
    except Exception as e:
        print(f"Import failed: {e}")
        return False

    payload = _build_qa_run_payload(
        tenant_id=tenant_id,
        run_id="run_test_2",
        material_version_id="v1",
        config_payload={},
        process_trace=process_trace,
        generated_count=1,
        saved_count=0,
        errors=["critic 未通过"],
        started_at="2025-03-14T10:00:00Z",
        ended_at="2025-03-14T10:01:00Z",
    )

    bm = payload.get("batch_metrics") or {}
    print(f"  saved_count = {bm.get('saved_count')}")
    print(f"  total_cost  = {bm.get('total_cost')}")
    print(f"  cpvq        = {bm.get('cpvq')}")

    assert bm.get("saved_count") == 0
    assert bm.get("cpvq") is None, "cpvq must be None when saved_count=0"
    print("\n[OK] CPVQ is None when saved_count=0.")
    return True


def test_llm_trace_sync_no_duplicate():
    print("\n" + "=" * 60)
    print("3. Test llm_trace sync (full-state replace, no duplicate)")
    print("=" * 60)

    question_llm_trace = []
    # Simulate 3 stream events, each with full state (as LangGraph does)
    events = [
        {"router": {"llm_trace": [{"node": "router.route", "model": "m1", "latency_ms": 100}]}},
        {"specialist": {"llm_trace": [{"node": "router.route", "model": "m1", "latency_ms": 100}, {"node": "specialist.draft", "model": "m2", "latency_ms": 200}]}},
        {"critic": {"llm_trace": [{"node": "router.route", "model": "m1", "latency_ms": 100}, {"node": "specialist.draft", "model": "m2", "latency_ms": 200}, {"node": "critic.review", "model": "m3", "latency_ms": 300}]}},
    ]
    for event in events:
        for _node_name, state_update in event.items():
            llm_records = state_update.get("llm_trace") or []
            if isinstance(llm_records, list):
                question_llm_trace[:] = [x for x in llm_records if isinstance(x, dict)]

    print(f"  len(question_llm_trace) = {len(question_llm_trace)}")
    for i, r in enumerate(question_llm_trace):
        print(f"    [{i}] node={r.get('node')} model={r.get('model')} latency_ms={r.get('latency_ms')}")

    assert len(question_llm_trace) == 3, "Should have exactly 3 calls (no duplicate)"
    assert question_llm_trace[0].get("node") == "router.route"
    assert question_llm_trace[2].get("node") == "critic.review"
    print("\n[OK] llm_trace sync: 3 records, no duplicate.")
    return True


if __name__ == "__main__":
    ok = True
    ok = test_cpvq_and_trace_sync() and ok
    ok = test_cpvq_zero_saved() and ok
    ok = test_llm_trace_sync_no_duplicate() and ok
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED" if ok else "SOME TESTS FAILED")
    print("=" * 60)
    sys.exit(0 if ok else 1)
