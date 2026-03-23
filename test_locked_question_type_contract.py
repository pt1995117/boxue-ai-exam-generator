import exam_graph


def _base_state():
    return {
        "final_json": {
            "题干": "某规则下，正确选项有（　）。",
            "选项1": "说法一",
            "选项2": "说法二",
            "选项3": "说法三",
            "选项4": "说法四",
            "正确答案": "AC",
            "解析": "1、教材原文：...\n2、试题分析：...\n3、结论：本题答案为AC。",
            "难度值": 0.5,
            "考点": "测试考点",
        },
        "kb_chunk": {
            "完整路径": "第一篇 > 第一章 > 第一节 > 测试点",
            "掌握程度": "掌握",
            "核心内容": "测试内容",
            "结构化内容": {},
            "metadata": {},
        },
        "term_locks": [],
        "examples": [],
        "router_details": {},
        "current_question_type": "单选题",
        "locked_question_type": "单选题",
        "current_generation_mode": "随机",
        "writer_validation_report": {},
        "writer_retry_exhausted": False,
    }


def _base_config():
    return {"configurable": {"question_type": "随机", "generation_mode": "随机", "difficulty_range": (0.3, 0.7)}}


def test_infer_final_json_question_type_multiselect():
    row = {
        "选项1": "A",
        "选项2": "B",
        "选项3": "C",
        "选项4": "D",
        "正确答案": "AC",
    }
    assert exam_graph._infer_final_json_question_type(row) == "多选题"


def test_critic_fails_when_final_type_drift_from_locked(monkeypatch):
    state = _base_state()

    monkeypatch.setattr(
        exam_graph,
        "build_extended_kb_context",
        lambda kb_chunk, retriever, examples: ("测试上下文", [], []),
    )
    monkeypatch.setattr(
        exam_graph,
        "detect_option_hierarchy_conflict",
        lambda final_json, kb_context, question_type: (False, [], ""),
    )

    result = exam_graph.critic_node(state, _base_config())
    critic_result = result.get("critic_result") or {}
    assert critic_result.get("passed") is False
    assert "locked_question_type_mismatch" in (critic_result.get("fail_types") or [])

