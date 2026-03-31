#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LangGraph flow matrix tests: every graph transition and key state-flow contract gets its own case."""

import inspect
import sys

import pytest

sys.path.insert(0, ".")

from exam_graph import app, calculator_node, critic_node, critical_decision, fixer_node, route_agent, specialist_node, writer_node

pytestmark = pytest.mark.release_gate


def _graph_edge_tuples():
    graph = app.get_graph()
    return {(edge.source, edge.target, edge.conditional, edge.data) for edge in graph.edges}


def _simulate_flow(agent_sequence, critic_states):
    """
    Simulate the reachable LangGraph path using the real routing/decision functions.
    agent_sequence:
      one agent name per router visit, e.g. ["GeneralAgent"] or ["GeneralAgent", "CalculatorAgent"].
    critic_states:
      one state payload per critic visit, already shaped the way critical_decision reads it.
    """
    path = ["router"]
    router_visit = 0
    current_agent = agent_sequence[router_visit]
    branch = route_agent({"agent_name": current_agent})
    path.extend([branch, "writer"])

    critic_visit = 0
    while critic_visit < len(critic_states):
        path.append("critic")
        decision = critical_decision(critic_states[critic_visit])
        critic_visit += 1
        if decision == "pass":
            path.append("END")
            return path
        if decision == "self_heal":
            path.extend(["self_heal", "END"])
            return path
        if decision == "fix":
            path.append("fixer")
            continue
        if decision == "reroute":
            path.append("router")
            router_visit += 1
            if router_visit >= len(agent_sequence):
                raise AssertionError("reroute happened but no next router agent was provided")
            current_agent = agent_sequence[router_visit]
            branch = route_agent({"agent_name": current_agent})
            path.extend([branch, "writer"])
            continue
        raise AssertionError(f"unexpected decision: {decision}")

    raise AssertionError("critic_states exhausted before flow terminated")


def test_langgraph_contains_every_expected_transition():
    edges = _graph_edge_tuples()
    expected = {
        ("__start__", "router", False, None),
        ("router", "specialist", True, None),
        ("router", "calculator", True, None),
        ("specialist", "writer", False, None),
        ("calculator", "writer", False, None),
        ("writer", "critic", False, None),
        ("critic", "__end__", True, "pass"),
        ("critic", "fixer", True, "fix"),
        ("critic", "router", True, "reroute"),
        ("fixer", "critic", False, None),
    }
    for item in expected:
        assert item in edges
    source = inspect.getsource(critical_decision)
    assert 'return "self_heal"' in source


@pytest.mark.parametrize(
    ("agent_name", "expected_branch"),
    [
        ("GeneralAgent", "specialist"),
        ("LegalAgent", "specialist"),
        ("CalculatorAgent", "calculator"),
        ("FinanceAgent", "calculator"),
    ],
    ids=[
        "general_routes_to_specialist",
        "legal_routes_to_specialist",
        "calculator_routes_to_calculator",
        "legacy_finance_alias_routes_to_calculator",
    ],
)
def test_route_agent_matrix(agent_name, expected_branch):
    assert route_agent({"agent_name": agent_name}) == expected_branch


@pytest.mark.parametrize(
    ("agent_sequence", "critic_states", "expected_path"),
    [
        (
            ["GeneralAgent"],
            [{"critic_result": {"passed": True}, "retry_count": 0}],
            ["router", "specialist", "writer", "critic", "END"],
        ),
        (
            ["CalculatorAgent"],
            [{"critic_result": {"passed": True}, "retry_count": 0}],
            ["router", "calculator", "writer", "critic", "END"],
        ),
        (
            ["GeneralAgent"],
            [
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 1, "final_json": {}},
                {"critic_result": {"passed": True}, "retry_count": 1},
            ],
            ["router", "specialist", "writer", "critic", "fixer", "critic", "END"],
        ),
        (
            ["CalculatorAgent"],
            [
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 1, "final_json": {}},
                {"critic_result": {"passed": True}, "retry_count": 1},
            ],
            ["router", "calculator", "writer", "critic", "fixer", "critic", "END"],
        ),
        (
            ["GeneralAgent"],
            [
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 1, "final_json": {}},
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 2, "final_json": {"_was_fixed": True}},
                {"critic_result": {"passed": True}, "retry_count": 2},
            ],
            ["router", "specialist", "writer", "critic", "fixer", "critic", "fixer", "critic", "END"],
        ),
        (
            ["GeneralAgent"],
            [
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 1, "final_json": {}},
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 2, "final_json": {"_was_fixed": True}},
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 3, "final_json": {"_was_fixed": True}},
            ],
            ["router", "specialist", "writer", "critic", "fixer", "critic", "fixer", "critic", "self_heal", "END"],
        ),
        (
            ["GeneralAgent", "GeneralAgent"],
            [
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 1, "final_json": {}},
                {"critic_result": {"passed": False, "issue_type": "major"}, "retry_count": 2, "final_json": {"_was_fixed": True}},
                {"critic_result": {"passed": True}, "retry_count": 0},
            ],
            ["router", "specialist", "writer", "critic", "fixer", "critic", "router", "specialist", "writer", "critic", "END"],
        ),
        (
            ["GeneralAgent", "CalculatorAgent"],
            [
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 1, "final_json": {}},
                {"critic_result": {"passed": False, "issue_type": "major"}, "retry_count": 2, "final_json": {"_was_fixed": True}},
                {"critic_result": {"passed": True}, "retry_count": 0},
            ],
            ["router", "specialist", "writer", "critic", "fixer", "critic", "router", "calculator", "writer", "critic", "END"],
        ),
        (
            ["CalculatorAgent", "GeneralAgent"],
            [
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 1, "final_json": {}},
                {"critic_result": {"passed": False, "issue_type": "major"}, "retry_count": 2, "final_json": {"_was_fixed": True}},
                {"critic_result": {"passed": True}, "retry_count": 0},
            ],
            ["router", "calculator", "writer", "critic", "fixer", "critic", "router", "specialist", "writer", "critic", "END"],
        ),
        (
            ["CalculatorAgent", "CalculatorAgent"],
            [
                {"critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 1, "final_json": {}},
                {"fix_required_unmet": True, "critic_result": {"passed": False, "issue_type": "minor"}, "retry_count": 2, "final_json": {"_was_fixed": True}},
                {"critic_result": {"passed": True}, "retry_count": 0},
            ],
            ["router", "calculator", "writer", "critic", "fixer", "critic", "router", "calculator", "writer", "critic", "END"],
        ),
    ],
    ids=[
        "non_calculation_pass",
        "calculation_pass",
        "non_calculation_fix_once_then_pass",
        "calculation_fix_once_then_pass",
        "non_calculation_fix_twice_then_pass",
        "non_calculation_fix_until_self_heal",
        "non_calculation_fix_then_reroute_to_specialist",
        "non_calculation_fix_then_reroute_to_calculator",
        "calculation_fix_then_reroute_to_specialist",
        "fix_required_unmet_forces_reroute",
    ],
)
def test_langgraph_flow_matrix(agent_sequence, critic_states, expected_path):
    assert _simulate_flow(agent_sequence, critic_states) == expected_path


@pytest.mark.parametrize(
    ("node_func", "required_fragments"),
    [
        (
            specialist_node,
            [
                '"draft": draft',
                '"examples": examples',
                '"self_check_issues": self_check_issues',
                '"current_generation_mode": effective_generation_mode',
                '"current_question_type": resolved_type',
                '"llm_trace": llm_records',
            ],
        ),
        (
            calculator_node,
            [
                '"draft": draft',
                '"tool_usage": {',
                '"execution_result": calc_result',
                '"generated_code": generated_code_str',
                '"code_status": code_status',
                '"current_generation_mode": effective_generation_mode',
                '"current_question_type": resolved_type',
                '"self_check_issues": self_check_issues',
            ],
        ),
        (
            writer_node,
            [
                '"final_json": excel_row',
                'derived_state = _sync_downstream_state_from_final_json(',
                '"writer_format_issues": issues',
                '"writer_validation_report": final_report',
                '"writer_retry_exhausted": True',
                'candidate_sentences = []',
            ],
        ),
        (
            critic_node,
            [
                '"critic_feedback":',
                '"critic_details":',
                '"critic_result":',
                '"critic_tool_usage": critic_tool_usage',
                '"llm_trace": llm_records',
            ],
        ),
        (
            fixer_node,
            [
                '"final_json": fixed_json',
                '"fix_summary": fix_summary',
                '"fix_required_unmet": fix_required_unmet',
                '"was_fixed": True',
                '**calc_state_updates',
            ],
        ),
    ],
    ids=[
        "specialist_output_contract",
        "calculator_output_contract",
        "writer_output_contract",
        "critic_output_contract",
        "fixer_output_contract",
    ],
)
def test_node_output_contracts_keep_required_question_state(node_func, required_fragments):
    source = inspect.getsource(node_func)
    for fragment in required_fragments:
        assert fragment in source
