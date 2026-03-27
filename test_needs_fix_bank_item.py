from pathlib import Path

import admin_api


def test_build_needs_fix_bank_item_keeps_fix_context():
    final_json = {
        "题干": "示例题干",
        "正确答案": "ABC",
    }
    question_trace = {
        "critic_result": {
            "passed": False,
            "reason": "答案不唯一",
            "all_issues": ["logic:cannot_deduce_unique_answer", "explanation:invalid"],
            "fail_types": ["reverse_solve_fail", "explanation_fail"],
            "missing_conditions": ["丈夫是否为上海户籍"],
            "basis_paths": ["第三篇 > 第四章 > 五、权属转移登记备件"],
        }
    }
    attempt_error_info = {
        "error_key": "critic:per_question_loop_fused",
        "reason": "单题critic->fixer循环超过3次，熔断本题",
        "evidence": "循环超过3次",
        "solution": "结束当前题并给出修复点",
        "fail_types": ["reverse_solve_fail", "explanation_fail"],
        "missing_conditions": ["丈夫是否为上海户籍"],
        "basis_paths": ["第三篇 > 第四章 > 五、权属转移登记备件"],
    }

    item = admin_api._build_needs_fix_bank_item(
        final_json=final_json,
        question_trace=question_trace,
        attempt_error_info=attempt_error_info,
        task_id="task_x",
        task_name="批量任务",
        run_id="run_x",
    )

    assert item["审计状态"] == "needs_fix"
    assert item["是否正式通过"] is False
    assert item["待修复"] is True
    assert item["待修复错误键"] == "critic:per_question_loop_fused"
    assert item["待修复原因"] == "单题critic->fixer循环超过3次，熔断本题"
    assert item["待修复建议"] == "结束当前题并给出修复点"
    assert item["待修复问题"] == ["logic:cannot_deduce_unique_answer", "explanation:invalid"]
    assert item["待修复缺失条件"] == ["丈夫是否为上海户籍"]
    assert item["critic_result"]["passed"] is False


def test_persist_needs_fix_bank_item_appends_without_counting_success(tmp_path: Path):
    bank_path = tmp_path / "bank.jsonl"
    persisted, item, error = admin_api._persist_needs_fix_bank_item(
        path=bank_path,
        final_json={"题干": "示例题干"},
        question_trace={"critic_result": {"passed": False, "reason": "质量不过"}},
        attempt_error_info={"error_key": "critic:per_question_loop_fused", "solution": "补约束"},
        task_id="task_x",
        task_name="批量任务",
        run_id="run_x",
    )

    assert persisted is True
    assert error == ""
    assert item is not None

    rows = admin_api._load_bank(bank_path)
    assert len(rows) == 1
    assert rows[0]["审计状态"] == "needs_fix"
    assert rows[0]["_needs_fix_saved"] is True
