"""Pipeline 图构建器。

LangGraph 并行与聚合行为说明（参考 LangGraph 文档）：
- 路由返回多节点时（如 route_after_layer2 返回 basic_rules_gate、surface_a、teaching_b 等），
  LangGraph 会 fan-out 并行执行这些节点（同一 super-step）。
- 聚合节点（node_aggregate）的触发时机：当所有指向它的边都有消息到达后触发。
  即 basic_rules_gate、surface_a、teaching_b、calc_branch 全部执行完毕后，aggregate 才运行。
- State 合并方式：各节点返回的 state 更新按 key 应用。未指定 reducer 的 key 采用覆盖策略
  （后写覆盖）。本图各并行节点写入不同 key（basic_rules→hard_rule_*，surface_a→realism/rigor/distractor，
  teaching_b→explanation/teaching，calc_branch→calculation），无并发写冲突。
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from src.pipeline import graph as graph_nodes
from src.pipeline.routes import route_after_layer1_blind_solver, route_after_layer2_knowledge_gate
from src.pipeline.state import JudgeState


def create_judge_graph():
    workflow = StateGraph(JudgeState)
    workflow.add_node("node_layer3_basic_rules_gate", graph_nodes.node_layer3_basic_rules_gate)
    workflow.add_node("node_layer1_blind_solver", graph_nodes.node_layer1_blind_solver)
    workflow.add_node("node_layer2_knowledge_gate", graph_nodes.node_layer2_knowledge_gate)
    workflow.add_node("node_layer3_surface_a", graph_nodes.node_layer3_surface_a)
    workflow.add_node("node_layer3_teaching_b", graph_nodes.node_layer3_teaching_b)
    workflow.add_node("node_layer3_calc_branch", graph_nodes.node_layer3_calc_branch)
    workflow.add_node("node_aggregate", graph_nodes.node_aggregate)

    workflow.set_entry_point("node_layer1_blind_solver")
    workflow.add_conditional_edges(
        "node_layer1_blind_solver",
        route_after_layer1_blind_solver,
        {
            "node_aggregate": "node_aggregate",
            "node_layer2_knowledge_gate": "node_layer2_knowledge_gate",
        },
    )
    # 知识门通过时 fan-out 到多个并行节点；短路时直连 aggregate
    workflow.add_conditional_edges(
        "node_layer2_knowledge_gate",
        route_after_layer2_knowledge_gate,
        {
            "node_aggregate": "node_aggregate",
            "node_layer3_basic_rules_gate": "node_layer3_basic_rules_gate",
            "node_layer3_surface_a": "node_layer3_surface_a",
            "node_layer3_teaching_b": "node_layer3_teaching_b",
            "node_layer3_calc_branch": "node_layer3_calc_branch",
        },
    )
    # 各并行节点均指向 aggregate；aggregate 在所有上游完成后触发，接收合并后的 state
    workflow.add_edge("node_layer3_basic_rules_gate", "node_aggregate")
    workflow.add_edge("node_layer3_surface_a", "node_aggregate")
    workflow.add_edge("node_layer3_teaching_b", "node_aggregate")
    workflow.add_edge("node_layer3_calc_branch", "node_aggregate")
    workflow.add_edge("node_aggregate", END)
    return workflow.compile()
