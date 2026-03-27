from hard_rules import validate_hard_rules


def _issue_codes(issues):
    return [str(i.get("issue_code", "")) for i in issues]


def test_explanation_allows_chinese_delimiter_only():
    issues = validate_hard_rules(
        question="以下表述正确的是（　）。",
        options=["选项一", "选项二", "选项三", "选项四"],
        explanation="1、教材原文：规则说明\n2、试题分析：分析说明\n3、结论：本题答案为A。",
        target_type="单选题",
        answer="A",
    )
    assert "HARD_EXPL_STRUCT" not in _issue_codes(issues)


def test_explanation_rejects_dot_delimiter():
    issues = validate_hard_rules(
        question="以下表述正确的是（　）。",
        options=["选项一", "选项二", "选项三", "选项四"],
        explanation="1. 教材原文：规则说明\n2. 试题分析：分析说明\n3. 结论：本题答案为A。",
        target_type="单选题",
        answer="A",
    )
    assert "HARD_EXPL_STRUCT" in _issue_codes(issues)
