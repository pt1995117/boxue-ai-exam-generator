#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pre-release real-LLM gate for LangGraph question flow.

Runs a small but fixed set of real model scenarios and writes a JSON report with:
- actual node path
- key state snapshots per node
- whether question/state propagation remained consistent

This is intentionally separate from normal pytest because it depends on live model providers.
"""

from __future__ import annotations

import json
import sys
import time
import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import exam_factory
from exam_factory import API_KEY, BASE_URL, MODEL_NAME, KnowledgeRetriever, build_knowledge_retriever
from exam_graph import (
    app as graph_app,
    critic_node,
    critical_decision,
    fixer_node,
    route_agent,
    router_node,
    specialist_node,
    writer_node,
    calculator_node,
)


ROOT = Path(__file__).resolve().parent
REPORT_PATH = ROOT / "tmp" / "release_gate_real_llm_report.json"


def _ensure_runtime_ready() -> None:
    if not API_KEY:
        raise RuntimeError("未配置可用 API Key，无法执行真实 LLM 门禁")


def _build_retriever(
    tenant_id: Optional[str] = None,
    kb_path: Optional[str] = None,
    history_path: Optional[str] = None,
) -> KnowledgeRetriever:
    return build_knowledge_retriever(
        tenant_id=tenant_id,
        kb_path=kb_path,
        history_path=history_path,
    )


def _base_config(retriever: KnowledgeRetriever, question_type: str = "随机") -> Dict[str, Any]:
    return {
        "configurable": {
            "model": MODEL_NAME,
            "api_key": API_KEY,
            "base_url": BASE_URL,
            "retriever": retriever,
            "question_type": question_type,
            "generation_mode": "随机",
        }
    }


def _find_chunk(retriever: KnowledgeRetriever, want_calc: bool) -> Dict[str, Any]:
    candidates: List[Tuple[int, Dict[str, Any]]] = []
    calc_keywords = ["税", "贷款", "利率", "首付", "月供", "比例", "面积", "价格", "年限", "%", "容积率", "绿地率"]
    calc_path_hints = ["税费", "贷款", "契税", "公积金", "组合贷款", "面积误差", "佣金结算"]
    for chunk in retriever.kb_data:
        path = str(chunk.get("完整路径", ""))
        text = f"{path} {chunk.get('核心内容', '')}"
        struct = chunk.get("结构化内容") or {}
        has_formulas = bool(struct.get("formulas"))
        depth = path.count(">")
        if want_calc:
            if has_formulas or any(k in text for k in calc_keywords):
                candidates.append((depth, chunk))
        else:
            if chunk.get("核心内容") and not has_formulas and not any(k in text for k in calc_keywords):
                candidates.append((depth, chunk))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        for _, chunk in candidates:
            path = str(chunk.get("完整路径", ""))
            if want_calc and any(hint in path for hint in calc_path_hints):
                return chunk
            if not want_calc and path.count(">") >= 2:
                return chunk
        return candidates[0][1]
    raise RuntimeError(
        f"未找到 {'计算' if want_calc else '非计算'} 场景切片。当前 KB 共有 {len(retriever.kb_data)} 条"
    )


def _snapshot(label: str, state: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    final_json = state.get("final_json") if isinstance(state.get("final_json"), dict) else {}
    update_final = update.get("final_json") if isinstance(update.get("final_json"), dict) else {}
    chosen = update_final or final_json or {}
    draft = update.get("draft") if isinstance(update.get("draft"), dict) else (state.get("draft") if isinstance(state.get("draft"), dict) else {})
    return {
        "node": label,
        "agent_name": state.get("agent_name"),
        "current_question_type": state.get("current_question_type"),
        "current_generation_mode": state.get("current_generation_mode"),
        "draft_question_preview": str(draft.get("question", ""))[:120],
        "question_preview": str(chosen.get("题干", ""))[:120],
        "answer": chosen.get("正确答案"),
        "has_generated_code": bool(state.get("generated_code") or update.get("generated_code")),
        "execution_result": update.get("execution_result", state.get("execution_result")),
        "code_status": update.get("code_status", state.get("code_status")),
        "writer_issue_count": len(update.get("writer_format_issues") or state.get("writer_format_issues") or []),
        "candidate_sentence_count": len(update.get("candidate_sentences") or state.get("candidate_sentences") or []),
        "critic_passed": (update.get("critic_result") or {}).get("passed"),
        "critic_issue_type": (update.get("critic_result") or {}).get("issue_type"),
        "critic_feedback": update.get("critic_feedback", state.get("critic_feedback")),
        "critic_details": str(update.get("critic_details", state.get("critic_details", "")))[:200],
        "logs": list(update.get("logs") or [])[:8],
        "retry_count": update.get("retry_count", state.get("retry_count")),
    }


def _merge_state(state: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(state)
    merged.update(update)
    return merged


def _run_graph_stream_scenario(name: str, chunk: Dict[str, Any], config: Dict[str, Any], *, debug_force_fail_once: bool = False) -> Dict[str, Any]:
    inputs = {
        "kb_chunk": chunk,
        "examples": [],
        "retry_count": 0,
        "logs": [],
    }
    if debug_force_fail_once:
        inputs["debug_force_fail_once"] = True

    path: List[str] = []
    snapshots: List[Dict[str, Any]] = []
    latest_state = dict(inputs)

    for event in graph_app.stream(inputs, config=config):
        for node_name, state_update in event.items():
            path.append(node_name)
            latest_state = _merge_state(latest_state, state_update)
            snapshots.append(_snapshot(node_name, latest_state, state_update))

    final_json = latest_state.get("final_json") if isinstance(latest_state.get("final_json"), dict) else {}
    result = {
        "scenario": name,
        "mode": "graph_stream",
        "status": "passed" if final_json else "failed",
        "path": path,
        "snapshots": snapshots,
        "latest_logs": list(latest_state.get("logs") or [])[-12:],
        "final_question": str(final_json.get("题干", ""))[:200],
        "final_answer": final_json.get("正确答案"),
        "question_type": latest_state.get("current_question_type"),
        "agent_name": latest_state.get("agent_name"),
        "has_candidate_sentences": bool(latest_state.get("candidate_sentences")),
        "has_writer_report": bool(latest_state.get("writer_validation_report")),
        "critic_passed": (latest_state.get("critic_result") or {}).get("passed"),
    }
    if not final_json:
        result["error"] = "流程未产出 final_json"
    return result


def _run_manual_reroute_scenario(
    name: str,
    chunk: Dict[str, Any],
    config: Dict[str, Any],
    *,
    initial_agent: str,
    reroute_agent: str,
) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "kb_chunk": chunk,
        "examples": [],
        "retry_count": 0,
        "logs": [],
        "agent_name": initial_agent,
    }
    snapshots: List[Dict[str, Any]] = []
    path: List[str] = []

    update = router_node(state, config)
    state = _merge_state(state, update)
    path.append("router")
    snapshots.append(_snapshot("router", state, update))

    branch = route_agent(state)
    if branch == "calculator":
        update = calculator_node(state, config)
    else:
        update = specialist_node(state, config)
    state = _merge_state(state, update)
    path.append(branch)
    snapshots.append(_snapshot(branch, state, update))

    update = writer_node(state, config)
    state = _merge_state(state, update)
    path.append("writer")
    snapshots.append(_snapshot("writer", state, update))

    update = critic_node(_merge_state(state, {"debug_force_fail_once": True, "retry_count": 0}), config)
    state = _merge_state(state, update)
    path.append("critic")
    snapshots.append(_snapshot("critic", state, update))

    update = fixer_node(state, config)
    state = _merge_state(state, update)
    path.append("fixer")
    snapshots.append(_snapshot("fixer", state, update))

    forced_major_state = _merge_state(
        state,
        {
            "retry_count": 2,
            "critic_feedback": "FORCED_MAJOR_AFTER_FIX",
            "critic_details": "force reroute after fixed question",
            "critic_result": {
                "passed": False,
                "issue_type": "major",
                "reason": "forced reroute",
                "fix_strategy": "regenerate",
            },
            "final_json": dict(state.get("final_json") or {}, _was_fixed=True),
            "agent_name": reroute_agent,
        },
    )

    decision = critical_decision(forced_major_state)
    if decision != "reroute":
        return {
            "scenario": name,
            "mode": "manual_reroute",
            "status": "failed",
            "path": path,
            "snapshots": snapshots,
            "error": f"预期 reroute，实际为 {decision}",
        }

    update = router_node(forced_major_state, config)
    state = _merge_state(forced_major_state, update)
    path.append("router")
    snapshots.append(_snapshot("router", state, update))

    branch = route_agent(state)
    if branch == "calculator":
        update = calculator_node(state, config)
    else:
        update = specialist_node(state, config)
    state = _merge_state(state, update)
    path.append(branch)
    snapshots.append(_snapshot(branch, state, update))

    update = writer_node(state, config)
    state = _merge_state(state, update)
    path.append("writer")
    snapshots.append(_snapshot("writer", state, update))

    update = critic_node(state, config)
    state = _merge_state(state, update)
    path.append("critic")
    snapshots.append(_snapshot("critic", state, update))

    final_json = state.get("final_json") if isinstance(state.get("final_json"), dict) else {}
    return {
        "scenario": name,
        "mode": "manual_reroute",
        "status": "passed" if final_json else "failed",
        "path": path,
        "snapshots": snapshots,
        "latest_logs": list(state.get("logs") or [])[-12:],
        "decision": decision,
        "reroute_branch": branch,
        "final_question": str(final_json.get("题干", ""))[:200],
        "final_answer": final_json.get("正确答案"),
    }


def _run_manual_calc_probe(name: str, chunk: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "kb_chunk": chunk,
        "examples": [],
        "retry_count": 0,
        "logs": [],
    }
    snapshots: List[Dict[str, Any]] = []
    path: List[str] = []

    update = router_node(state, config)
    state = _merge_state(state, update)
    path.append("router")
    snapshots.append(_snapshot("router", state, update))

    branch = route_agent(state)
    if branch != "calculator":
        return {
            "scenario": name,
            "mode": "calc_probe",
            "status": "failed",
            "path": path + [branch],
            "snapshots": snapshots,
            "error": f"预期进入 calculator，实际进入 {branch}",
            "router_agent": state.get("agent_name"),
        }

    update = calculator_node(state, config)
    state = _merge_state(state, update)
    path.append("calculator")
    snapshots.append(_snapshot("calculator", state, update))

    draft = state.get("draft") if isinstance(state.get("draft"), dict) else {}
    return {
        "scenario": name,
        "mode": "calc_probe",
        "status": "passed" if draft else "failed",
        "path": path,
        "snapshots": snapshots,
        "router_agent": state.get("agent_name"),
        "draft_question": str(draft.get("question", ""))[:200],
        "generated_code": str(state.get("generated_code", "") or "")[:500],
        "code_status": state.get("code_status"),
        "execution_result": state.get("execution_result"),
        "latest_logs": list(state.get("logs") or [])[-12:],
        "error": None if draft else "calculator 未产出 draft",
    }


def run_release_gate(
    *,
    tenant_id: Optional[str] = None,
    kb_path: Optional[str] = None,
    history_path: Optional[str] = None,
    only: Optional[List[str]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    _ensure_runtime_ready()
    retriever = _build_retriever(tenant_id=tenant_id, kb_path=kb_path, history_path=history_path)
    config = _base_config(retriever)
    non_calc_chunk = _find_chunk(retriever, want_calc=False)
    calc_chunk = _find_chunk(retriever, want_calc=True)

    started_at = time.time()
    scenario_builders = [
        ("real_non_calc_pass", lambda: _run_graph_stream_scenario("real_non_calc_pass", non_calc_chunk, config)),
        ("real_calc_pass", lambda: _run_graph_stream_scenario("real_calc_pass", calc_chunk, config)),
        ("real_calc_probe", lambda: _run_manual_calc_probe("real_calc_probe", calc_chunk, config)),
        ("real_non_calc_fix_once", lambda: _run_graph_stream_scenario("real_non_calc_fix_once", non_calc_chunk, config, debug_force_fail_once=True)),
        ("real_calc_fix_once", lambda: _run_graph_stream_scenario("real_calc_fix_once", calc_chunk, config, debug_force_fail_once=True)),
        (
            "real_non_calc_reroute_to_calculator",
            lambda: _run_manual_reroute_scenario(
                "real_non_calc_reroute_to_calculator",
                non_calc_chunk,
                config,
                initial_agent="GeneralAgent",
                reroute_agent="CalculatorAgent",
            ),
        ),
        (
            "real_calc_reroute_to_specialist",
            lambda: _run_manual_reroute_scenario(
                "real_calc_reroute_to_specialist",
                calc_chunk,
                config,
                initial_agent="CalculatorAgent",
                reroute_agent="GeneralAgent",
            ),
        ),
    ]
    selected = set(only or [])
    scenarios = []
    for name, builder in scenario_builders:
        if selected and name not in selected:
            continue
        scenarios.append(builder())

    ok = all(item.get("status") == "passed" for item in scenarios)
    report = {
        "started_at": started_at,
        "finished_at": time.time(),
        "model": MODEL_NAME,
        "base_url": BASE_URL,
        "kb_path": getattr(retriever, "kb_path", kb_path or ""),
        "history_path": getattr(retriever, "history_path", history_path or ""),
        "tenant_id": tenant_id,
        "report_type": "release_gate_real_llm",
        "ok": ok,
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
    }
    return ok, report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real-LLM LangGraph release gate.")
    parser.add_argument("--tenant-id", help="Tenant id to load before building retriever, e.g. wh")
    parser.add_argument("--kb-path", help="Override KB jsonl path")
    parser.add_argument("--history-path", help="Override history xls/xlsx path")
    parser.add_argument("--only", action="append", help="Run only selected scenario(s), repeatable")
    args = parser.parse_args()

    try:
        ok, report = run_release_gate(
            tenant_id=args.tenant_id,
            kb_path=args.kb_path,
            history_path=args.history_path,
            only=args.only,
        )
    except Exception as exc:
        report = {
            "report_type": "release_gate_real_llm",
            "ok": False,
            "error": str(exc),
        }
        ok = False

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 80)
    print("真实 LLM 上线前质量门禁")
    print("=" * 80)
    print(f"报告路径: {REPORT_PATH}")
    print(f"结果: {'PASS' if ok else 'FAIL'}")
    if not ok and report.get("error"):
        print(f"错误: {report['error']}")
    for item in report.get("scenarios", []):
        print(f"- {item.get('scenario')}: {item.get('status')} | path={' -> '.join(item.get('path', []))}")
        if item.get("error"):
            print(f"  error={item['error']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
