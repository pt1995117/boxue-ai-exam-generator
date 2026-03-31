import exam_graph


def test_writer_calculation_path_does_not_crash_when_calc_align_msg_absent(monkeypatch):
    monkeypatch.setattr(exam_graph, "build_extended_kb_context", lambda kb_chunk, retriever, examples: ("教材上下文", [], []))
    monkeypatch.setattr(exam_graph, "_resolve_specialist_writer_model", lambda state, model: ("gpt-5.2", "test"))
    monkeypatch.setattr(
        exam_graph,
        "call_llm",
        lambda **kwargs: ('{"question":"客户申请商业贷款，最低首付比例为（　）。","options":["15","20","25","30"],"answer":"B","explanation":"1、教材原文：教材。\\n2、试题分析：分析。\\n3、结论：本题答案为B。","difficulty":0.6}', None, {"node": kwargs.get("node_name", "writer.finalize")}),
    )
    monkeypatch.setattr(exam_graph, "parse_json_from_response", lambda text: {
        "question": "客户申请商业贷款，最低首付比例为（　）。",
        "options": ["15", "20", "25", "30"],
        "answer": "B",
        "explanation": "1、教材原文：教材。\n2、试题分析：分析。\n3、结论：本题答案为B。",
        "difficulty": 0.6,
    })
    monkeypatch.setattr(exam_graph, "_writer_normalize_phase", lambda payload, target_type: dict(payload))
    monkeypatch.setattr(exam_graph, "_writer_validate_phase", lambda *args, **kwargs: {"passed": True, "issues": [], "summary": "ok"})
    monkeypatch.setattr(exam_graph, "assess_preconditions_current_only", lambda **kwargs: (True, [], "ok", None))
    monkeypatch.setattr(exam_graph, "assess_minimal_sufficient_conditions_current_only", lambda **kwargs: (True, [], "ok", None))
    monkeypatch.setattr(exam_graph, "build_candidate_sentences", lambda question, options: [{"sentence": question}])
    monkeypatch.setattr(
        exam_graph,
        "_sync_downstream_state_from_final_json",
        lambda final_json, target_type, **kwargs: {
            "current_question_type": target_type,
            "locked_question_type": target_type,
            "candidate_sentences": [{"sentence": final_json.get("题干", "")}],
            "writer_validation_report": {"passed": True, "issues": [], "summary": "ok"},
        },
    )

    state = {
        "draft": {
            "question": "客户申请商业贷款，最低首付比例为（　）。",
            "options": ["15", "20", "25", "30"],
            "answer": "B",
            "explanation": "1、教材原文：教材。\n2、试题分析：分析。\n3、结论：本题答案为B。",
            "difficulty": 0.6,
        },
        "kb_chunk": {"完整路径": "第三篇 > 第一章 > 第一节 > 四、商业贷款政策", "掌握程度": "熟悉"},
        "code_status": "no_calculation",
        "generated_code": "",
        "current_generation_mode": "基础概念/理解记忆",
        "router_details": {"recommended_type": "单选题"},
        "examples": [],
        "term_locks": [],
    }
    config = {"configurable": {"question_type": "随机", "generation_mode": "随机", "difficulty_range": [0.5, 0.7], "retriever": object()}}

    result = exam_graph.writer_node(state, config)

    assert isinstance(result.get("final_json"), dict)
    assert result["final_json"]["题干"] == "客户申请商业贷款，最低首付比例为（　）。"
    assert result["final_json"]["正确答案"] == "B"
