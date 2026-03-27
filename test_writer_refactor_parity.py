from exam_graph import (
    _build_writer_polish_prompt_issue_only,
    _legacy_writer_precheck,
    _refactored_writer_precheck,
)


def _sorted_messages(items):
    return sorted([str(x) for x in items if str(x).strip()])


def _assert_parity(draft, target_type, term_locks=None):
    legacy_ir, legacy_issues = _legacy_writer_precheck(draft, target_type, term_locks=term_locks or [])
    ref_ir, ref_report = _refactored_writer_precheck(draft, target_type, term_locks=term_locks or [])
    ref_issues = [str(i.get("message", "")) for i in (ref_report.get("issues") or [])]

    assert legacy_ir.get("question", "") == ref_ir.get("question", "")
    assert legacy_ir.get("options", []) == ref_ir.get("options", [])
    assert legacy_ir.get("answer", "") == ref_ir.get("answer", "")
    assert legacy_ir.get("explanation", "") == ref_ir.get("explanation", "")
    assert _sorted_messages(legacy_issues) == _sorted_messages(ref_issues)
    assert ref_report.get("passed", False) == (len(ref_issues) == 0)


def test_writer_refactor_parity_single_choice():
    draft = {
        "question": "以下表述正确的是( )",
        "options": ["A. 销售合同。", "B. 经纪服务", "C. 中介费", "D. 税费"],
        "answer": "A",
        "explanation": "1、教材原文：规则说明\n2、试题分析：分析说明\n3、结论：本题答案为A",
    }
    _assert_parity(draft, "单选题")


def test_writer_refactor_parity_true_false():
    draft = {
        "question": "经纪人应当核验产权信息。( )",
        "options": ["正确", "错误"],
        "answer": "A",
        "explanation": "1、教材原文：规则说明\n2、试题分析：分析说明\n3、结论：本题答案为正确",
    }
    _assert_parity(draft, "判断题")


def test_writer_refactor_parity_with_term_lock_violation():
    draft = {
        "question": "以下表述正确的是（ ）",
        "options": ["商贷审批通过", "公积金贷款审批通过", "组合贷审批通过", "按揭贷款审批通过"],
        "answer": "A",
        "explanation": "1、教材原文：规则说明\n2、试题分析：分析说明\n3、结论：本题答案为A",
    }
    _assert_parity(draft, "单选题", term_locks=["商业贷款"])


def test_issue_only_polish_prompt_contains_only_issue_driven_contract():
    prompt = _build_writer_polish_prompt_issue_only(
        target_type="单选题",
        draft_for_prompt={
            "question": "以下表述正确的是（ ）",
            "options": ["选项1", "选项2", "选项3", "选项4"],
            "answer": "A",
            "explanation": "1、教材原文...\n2、试题分析...\n3、结论：本题答案为A",
        },
        kb_context="教材切片上下文",
        examples_text="",
        term_lock_text="",
        difficulty_instruction_writer="",
        self_check_text="",
        issue_messages=["题干括号格式不规范", "选项末尾含标点"],
    )
    assert "必须修复的问题（按优先级）" in prompt
    assert "- 题干括号格式不规范" in prompt
    assert "- 选项末尾含标点" in prompt
    assert "仅修复上述问题，不做无关改写" in prompt
