import exam_graph
import hard_rules


def test_focus_var_misalign_is_warning_not_error():
    issues = exam_graph.validate_focus_alignment(
        {
            "question": "根据公积金贷款套数认定标准，下列说法正确的是（　）。",
            "options": ["说法A", "说法B", "说法C", "说法D"],
            "explanation": "1、教材原文：...\n2、试题分析：...\n3、结论：本题答案为A。",
        },
        focus_contract={
            "focus_rule": "公积金贷款套数认定标准",
            "focus_variables": ["在沪住房套数", "全国范围内公积金贷款使用次数及状态"],
            "focus_task": "规则判定",
        },
    )
    assert issues
    assert issues[0]["issue_code"] == "FOCUS_VAR_MISALIGN"
    assert issues[0]["severity"] == "warning"


def test_hard_expl_textbook_is_warning_not_error():
    explanation = (
        "1、教材原文：客户购房主要流程包括认购、签约、办理贷款、交房、办理产权。\n"
        "2、试题分析：本题考察购房流程顺序。\n"
        "3、结论：本题答案为A。"
    )
    issues = hard_rules.validate_hard_rules(
        question="根据教材，以下关于购房流程的说法正确的是（　）。",
        options=["认购在前", "签约在前", "交房在前", "产权在前"],
        explanation=explanation,
        target_type="单选题",
        answer="A",
    )
    hit = [x for x in issues if x.get("issue_code") == "HARD_EXPL_TEXTBOOK"]
    assert hit
    assert hit[0].get("severity") == "warning"
