import exam_graph


def test_random_calc_guard_requires_actual_executable_chain():
    assert (
        exam_graph._should_force_single_choice_for_random_calculation(
            "随机",
            generated_code="",
            code_status="no_calculation",
        )
        is False
    )
    assert (
        exam_graph._should_force_single_choice_for_random_calculation(
            "随机",
            generated_code="result = 30",
            code_status="error",
        )
        is False
    )


def test_random_calc_guard_forces_single_when_execution_succeeds():
    assert (
        exam_graph._should_force_single_choice_for_random_calculation(
            "随机",
            generated_code="result = 30",
            code_status="success",
        )
        is True
    )
    assert (
        exam_graph._should_force_single_choice_for_random_calculation(
            "随机",
            generated_code="print(30)",
            code_status="success_no_result",
        )
        is True
    )
