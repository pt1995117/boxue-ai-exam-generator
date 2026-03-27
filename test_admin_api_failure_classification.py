from admin_api import _build_abort_attempt_error, _infer_solution_by_error_key


def test_abort_loop_fused_is_process_control():
    result = _build_abort_attempt_error(
        abort_reason="单题critic->fixer循环超过3次，熔断本题",
        question_trace={},
    )
    assert result["error_key"] == "critic:per_question_loop_fused"
    assert result["category"] == "process_control"


def test_abort_reroute_round_limit_is_process_control():
    result = _build_abort_attempt_error(
        abort_reason="超出单题重路由轮次上限(2)",
        question_trace={},
    )
    assert result["error_key"] == "process:reroute_round_limit"
    assert result["category"] == "process_control"


def test_abort_elapsed_timeout_is_process_control():
    result = _build_abort_attempt_error(
        abort_reason="超出单题耗时上限(300000ms)",
        question_trace={},
    )
    assert result["error_key"] == "process:question_elapsed_timeout"
    assert result["category"] == "process_control"


def test_storage_failure_solution_is_defined():
    solution = _infer_solution_by_error_key(
        error_key="storage:append_bank_item_failed",
        fail_types=[],
        reason="Permission denied",
        missing_conditions=[],
    )
    assert "落库失败" in solution
