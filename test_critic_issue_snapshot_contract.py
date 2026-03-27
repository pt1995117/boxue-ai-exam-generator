import inspect

from exam_graph import _attach_first_failure_snapshot, _build_critic_issue_items, _infer_draft_type_for_writer, _infer_final_json_question_type, critic_node, router_node


def test_build_critic_issue_items_exposes_tag_and_specific_content():
    items = _build_critic_issue_items(
        required_fixes=[
            "logic:missing_conditions",
            "quality:issues",
            "writer:HARD_EXPL_1",
        ],
        reason_text="题目存在多处问题",
        missing_conditions=["丈夫是否为上海户籍", "是否为多子女家庭"],
        quality_issues=["题干过于直给", "设问聚焦度不足"],
        writer_issues=[
            {
                "issue_code": "HARD_EXPL_1",
                "message": "解析第一段缺少掌握程度分级",
            }
        ],
    )

    assert {"tag": "logic:missing_conditions", "content": "缺失前提条件：丈夫是否为上海户籍；是否为多子女家庭"} in items
    assert {"tag": "quality:issues", "content": "质量问题：题干过于直给；设问聚焦度不足"} in items
    assert {"tag": "writer:HARD_EXPL_1", "content": "解析第一段缺少掌握程度分级"} in items


def test_attach_first_failure_snapshot_only_records_first_failure():
    first_payload = _attach_first_failure_snapshot(
        {},
        {
            "critic_feedback": "FAIL",
            "critic_details": "第一次失败",
            "critic_result": {"passed": False, "reason": "第一次失败"},
            "critic_required_fixes": ["logic:missing_conditions"],
            "critic_issue_items": [{"tag": "logic:missing_conditions", "content": "缺失主体资格前提"}],
            "critic_rules_context": "RULE_A",
        },
    )

    assert first_payload["first_critic_details"] == "第一次失败"
    assert first_payload["first_critic_required_fixes"] == ["logic:missing_conditions"]
    assert first_payload["first_critic_issue_items"] == [{"tag": "logic:missing_conditions", "content": "缺失主体资格前提"}]
    assert first_payload["first_critic_rules_context"] == "RULE_A"

    second_payload = _attach_first_failure_snapshot(
        {"first_critic_result": {"passed": False, "reason": "第一次失败"}},
        {
            "critic_feedback": "FAIL",
            "critic_details": "第二次失败",
            "critic_result": {"passed": False, "reason": "第二次失败"},
            "critic_required_fixes": ["logic:answer_mismatch"],
            "critic_issue_items": [{"tag": "logic:answer_mismatch", "content": "答案与解析不一致"}],
            "critic_rules_context": "RULE_B",
        },
    )

    assert "first_critic_details" not in second_payload
    assert "first_critic_issue_items" not in second_payload


def test_router_reroute_uses_first_critic_snapshot_for_specialist_contract():
    source = inspect.getsource(router_node)
    assert 'prev_first_critic_feedback' in source
    assert 'prev_first_critic_result' in source
    assert 'prev_first_critic_issue_items' in source
    assert 'state.get("first_critic_feedback") or state.get("critic_feedback")' in source
    assert 'state.get("first_critic_issue_items") or state.get("critic_issue_items")' in source
    assert 'state.get("first_critic_rules_context") or state.get("critic_rules_context")' in source


def test_type_inference_prefers_generated_content_over_planned_type():
    draft = {
        "question": "示例题干（　）。",
        "options": ["选项A", "选项B", "选项C", "选项D"],
        "answer": "A",
        "explanation": "1、教材原文：...\n2、试题分析：A、C都符合。\n3、结论：本题答案为AC。",
    }
    final_json = {
        "题干": "示例题干（　）。",
        "选项1": "选项A",
        "选项2": "选项B",
        "选项3": "选项C",
        "选项4": "选项D",
        "正确答案": "A",
        "解析": "1、教材原文：...\n2、试题分析：A、C都符合。\n3、结论：本题答案为AC。",
    }

    assert _infer_draft_type_for_writer(draft) == "多选题"
    assert _infer_final_json_question_type(final_json) == "多选题"


def test_critic_uses_actual_question_type_before_locked_state():
    source = inspect.getsource(critic_node)
    assert 'state_question_type' in source
    assert 'inferred_final_type = _infer_final_json_question_type(final_json)' in source
    assert 'question_type = inferred_final_type or state_question_type' in source
