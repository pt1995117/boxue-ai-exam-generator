from src.pipeline.graph import create_judge_graph


def test_langgraph_topology_is_stable():
    graph = create_judge_graph().get_graph()

    expected_nodes = {
        "__start__",
        "node_layer1_blind_solver",
        "node_layer2_knowledge_gate",
        "node_layer3_basic_rules_gate",
        "node_layer3_surface_a",
        "node_layer3_teaching_b",
        "node_layer3_calc_branch",
        "node_aggregate",
        "__end__",
    }

    actual_nodes = set(graph.nodes.keys())
    assert actual_nodes == expected_nodes

    expected_edges = {
        ("__start__", "node_layer1_blind_solver", False),
        ("node_layer1_blind_solver", "node_aggregate", True),
        ("node_layer1_blind_solver", "node_layer2_knowledge_gate", True),
        ("node_layer2_knowledge_gate", "node_aggregate", True),
        ("node_layer2_knowledge_gate", "node_layer3_basic_rules_gate", True),
        ("node_layer2_knowledge_gate", "node_layer3_surface_a", True),
        ("node_layer2_knowledge_gate", "node_layer3_teaching_b", True),
        ("node_layer2_knowledge_gate", "node_layer3_calc_branch", True),
        ("node_layer3_basic_rules_gate", "node_aggregate", False),
        ("node_layer3_surface_a", "node_aggregate", False),
        ("node_layer3_teaching_b", "node_aggregate", False),
        ("node_layer3_calc_branch", "node_aggregate", False),
        ("node_aggregate", "__end__", False),
    }

    actual_edges = {(edge.source, edge.target, edge.conditional) for edge in graph.edges}
    assert actual_edges == expected_edges
