#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test state contract: nodes that modify question content must write back to state;
Fixer and Router(reroute) must clear calculator state so Critic always sees latest question.
Also runs critical_decision() scenarios (pass/fix/reroute/self_heal/leakage).
"""
import sys
import inspect

import pytest

from exam_graph import (
    router_node,
    fixer_node,
    critical_decision,
    AgentState,
)

def test_router_reroute_clears_calculator_state():
    """Router when retry_count > 0 must clear stale derived/calculator state (code contract)."""
    print("\n--- test_router_reroute_clears_calculator_state ---")
    source = inspect.getsource(router_node)
    assert 'state.get(\'retry_count\', 0) > 0' in source or "state.get('retry_count', 0) > 0" in source
    assert "state_updates[\"execution_result\"]" in source or 'state_updates["execution_result"]' in source
    assert "state_updates[\"code_status\"]" in source or 'state_updates["code_status"]' in source
    assert "None" in source
    assert "execution_result" in source and "generated_code" in source and "tool_usage" in source
    assert "candidate_sentences" in source and "writer_validation_report" in source
    # Must clear draft and final_json
    assert "state_updates[\"draft\"]" in source or 'state_updates["draft"]' in source
    assert "state_updates[\"final_json\"]" in source or 'state_updates["final_json"]' in source
    print("  PASS: Router reroute branch clears draft, final_json, calculator state, and stale derived state")


def test_fixer_success_return_clears_calculator_state():
    """Fixer success return must include fresh downstream derived state and clear calculator state."""
    print("\n--- test_fixer_success_return_clears_calculator_state ---")
    # We cannot call fixer_node without full config and LLM; we only check the return structure
    # by inspecting the code or by mocking. Here we verify the contract by importing and
    # checking that fixer_node's success path returns those keys.
    source = inspect.getsource(fixer_node)
    assert "execution_result" in source and "None" in source, "fixer_node should set execution_result=None"
    assert "generated_code" in source and "None" in source, "fixer_node should set generated_code=None"
    assert "tool_usage" in source and "None" in source, "fixer_node should set tool_usage=None"
    assert "code_status" in source and "None" in source, "fixer_node should set code_status=None"
    assert "_sync_downstream_state_from_final_json" in source, "fixer should refresh downstream derived state"
    assert '"final_json": fixed_json' in source or "'final_json': fixed_json" in source, "fixer must return final_json"
    print("  PASS: Fixer success return includes final_json, refreshes derived state, and clears calculator state")


def test_router_reroute_preserves_prev():
    """Router reroute must preserve prev_final_json and prev_critic_* (code contract)."""
    print("\n--- test_router_reroute_preserves_prev ---")
    source = inspect.getsource(router_node)
    assert "prev_final_json" in source
    assert "prev_critic_feedback" in source
    assert "prev_critic_result" in source
    assert "prev_critic_required_fixes" in source
    print("  PASS: Router reroute preserves prev_final_json and prev_critic_*")

@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"critic_result": {"passed": True}, "retry_count": 0}, "pass"),
        ({"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 1}, "fix"),
        ({"critic_result": {"passed": False, "issue_type": "major"}, "retry_count": 1, "final_json": {}}, "fix"),
        (
            {"critic_result": {"passed": False, "issue_type": "major"}, "retry_count": 1, "final_json": {"_was_fixed": True}},
            "reroute",
        ),
        (
            {
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "can_deduce_unique_answer": False,
                    "fail_types": ["reverse_solve_fail"],
                },
                "retry_count": 1,
                "current_question_type": "判断题",
                "final_json": {"_was_fixed": True},
            },
            "fix",
        ),
        (
            {
                "critic_result": {
                    "passed": False,
                    "issue_type": "minor",
                    "fix_strategy": "fix_explanation",
                    "fail_types": ["explanation_fail"],
                },
                "retry_count": 1,
                "final_json": {"_was_fixed": True},
            },
            "fix",
        ),
        (
            {
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "fail_types": ["no_question"],
                },
                "retry_count": 3,
                "router_round": 0,
            },
            "self_heal",
        ),
        (
            {
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "non_current_slice_basis": True,
                },
                "retry_count": 10,
                "router_round": 10,
            },
            "reroute",
        ),
        ({"critic_result": {"passed": False, "issue_type": "major"}}, "fix"),
        ({"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 3}, "self_heal"),
    ],
    ids=[
        "critic_passes_directly",
        "first_minor_failure_goes_to_fixer",
        "first_major_failure_without_fix_marker_still_goes_to_fixer",
        "major_failure_after_fix_reroutes",
        "judge_reverse_solve_fail_stays_on_fixer",
        "fixed_explanation_only_issue_stays_on_fixer",
        "no_question_honors_round_retry_limit",
        "non_current_basis_uses_round_retry_count",
        "first_major_failure_defaults_to_fix",
        "retry_exhausted_self_heals",
    ],
)
def test_critical_decision_scenarios(state, expected):
    """Graph flow matrix for critic->decision transitions."""
    assert critical_decision(state) == expected


def test_router_reroute_clears_all_stale_question_views():
    """Reroute branch must clear every stale view that can make downstream nodes read the wrong question."""
    source = inspect.getsource(router_node)
    expected_keys = [
        "draft",
        "final_json",
        "execution_result",
        "generated_code",
        "tool_usage",
        "code_status",
        "candidate_sentences",
        "writer_format_issues",
        "writer_validation_report",
        "writer_retry_exhausted",
        "fix_summary",
        "fix_no_change",
        "fix_attempted_regen",
        "fix_required_unmet",
        "was_fixed",
    ]
    for key in expected_keys:
        assert f'state_updates["{key}"]' in source or f"state_updates['{key}']" in source


def test_fixer_success_refreshes_latest_question_views():
    """Fixer success path must write fixed final_json and refresh every downstream derived view from it."""
    source = inspect.getsource(fixer_node)
    assert "_sync_downstream_state_from_final_json(" in source
    assert '"final_json": fixed_json' in source or "'final_json': fixed_json" in source
    assert '"was_fixed": True' in source or "'was_fixed': True" in source
    assert "derived_state" in source


def run_all():
    print("=" * 60)
    print("test_state_contract: state flow and clearing")
    print("=" * 60)
    test_router_reroute_clears_calculator_state()
    test_fixer_success_return_clears_calculator_state()
    test_router_reroute_preserves_prev()
    for state, expected in [
        ({"critic_result": {"passed": True}, "retry_count": 0}, "pass"),
        ({"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 1}, "fix"),
        ({"critic_result": {"passed": False, "issue_type": "major"}, "retry_count": 1, "final_json": {}}, "fix"),
        ({"critic_result": {"passed": False, "issue_type": "major"}, "retry_count": 1, "final_json": {"_was_fixed": True}}, "reroute"),
        (
            {
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "can_deduce_unique_answer": False,
                    "fail_types": ["reverse_solve_fail"],
                },
                "retry_count": 1,
                "current_question_type": "判断题",
                "final_json": {"_was_fixed": True},
            },
            "fix",
        ),
        (
            {
                "critic_result": {
                    "passed": False,
                    "issue_type": "minor",
                    "fix_strategy": "fix_explanation",
                    "fail_types": ["explanation_fail"],
                },
                "retry_count": 1,
                "final_json": {"_was_fixed": True},
            },
            "fix",
        ),
        ({"critic_result": {"passed": False, "issue_type": "major"}}, "fix"),
        ({"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 3}, "self_heal"),
    ]:
        assert critical_decision(state) == expected
    print("\n" + "=" * 60)
    print("All state contract tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
    sys.exit(0)
