"""Pipeline 路由函数。"""

from __future__ import annotations

from src.pipeline.state import JudgeState


def route_after_layer1_blind_solver(state: JudgeState) -> list[str]:
    solver = state.get("solver_validation")
    if solver and solver.ambiguity_flag:
        return ["node_aggregate"]
    return ["node_layer2_knowledge_gate"]


def route_after_layer2_knowledge_gate(state: JudgeState) -> list[str]:
    if bool(state.get("knowledge_gate_reject", False)):
        return ["node_aggregate"]
    routes = [
        "node_layer3_basic_rules_gate",
        "node_layer3_surface_a",
        "node_layer3_teaching_b",
    ]
    if bool(state["question"].is_calculation):
        routes.append("node_layer3_calc_branch")
    return routes
