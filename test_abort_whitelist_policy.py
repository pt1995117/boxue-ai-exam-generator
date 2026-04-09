from pathlib import Path

import admin_api


def test_abort_whitelist_pass_requires_all_fail_types_in_whitelist():
    assert admin_api._is_abort_whitelist_pass(
        {
            "passed": False,
            "fail_types": ["format_fail", "quality_fail"],
            "reason": "仅有格式和质量轻微问题",
        }
    )
    assert not admin_api._is_abort_whitelist_pass(
        {
            "passed": False,
            "fail_types": ["format_fail", "reverse_solve_fail"],
            "reason": "同时存在逻辑硬错误",
        }
    )


def test_abort_whitelist_pass_allows_additional_answer_field_mismatch_marker():
    assert admin_api._is_abort_whitelist_pass(
        {
            "passed": False,
            "fail_types": ["answer_mismatch"],
            "answer_field_mismatch_whitelist_candidate": True,
            "reason": "答案字段错误，但解析答案和反向解题一致",
        }
    )


def test_abort_whitelist_pass_allows_additional_question_type_alignment_marker():
    assert admin_api._is_abort_whitelist_pass(
        {
            "passed": False,
            "fail_types": ["locked_question_type_mismatch"],
            "question_type_alignment_whitelist_candidate": True,
            "reason": "题型字段和实际题型不一致，但题目真实类型可稳定判断",
        }
    )


def test_build_whitelist_pass_bank_item_keeps_critic_errors():
    item = admin_api._build_whitelist_pass_bank_item(
        final_json={"题干": "示例题干", "正确答案": "A"},
        critic_result={
            "passed": False,
            "fail_types": ["format_fail"],
            "reason": "存在格式问题",
            "all_issues": ["括号样式不统一"],
        },
        task_id="task_1",
        task_name="测试任务",
        run_id="run_1",
    )

    assert item["审计状态"] == "whitelist_pass"
    assert item["是否正式通过"] is True
    assert item["白名单通过"] is True
    assert item["白名单错误类型"] == ["format_fail"]
    assert "存在格式问题" in item["白名单错误内容"]


def test_record_slice_generation_failure_blocks_after_ten(tmp_path: Path, monkeypatch):
    tenant_id = "unit"
    material_version_id = "v_test"
    monkeypatch.setattr(admin_api, "tenant_root", lambda _tenant_id: tmp_path / _tenant_id)

    last = None
    for idx in range(11):
        last = admin_api._record_slice_generation_failure(
            tenant_id=tenant_id,
            material_version_id=material_version_id,
            slice_id=12,
            critic_result={
                "passed": False,
                "fail_types": ["reverse_solve_fail"],
                "reason": f"第{idx + 1}次失败",
            },
            task_id="task_x",
            run_id="run_x",
        )

    assert last is not None
    assert last["failure_count"] == 11
    assert last["blocked"] is True
    assert 12 in admin_api._blocked_slice_ids_for_material(tenant_id, material_version_id)


def test_choose_generation_slice_id_uses_same_template_bucket_fallback():
    lookup = admin_api._build_slice_candidate_lookup(
        [
            {"slice_id": 101, "path": "A > B > C > x", "mastery": "掌握"},
            {"slice_id": 102, "path": "A > B > C > y", "mastery": "掌握"},
            {"slice_id": 103, "path": "A > B > D > z", "mastery": "掌握"},
        ],
        template_route_rules=[{"path_prefix": "A", "ratio": 1}],
    )

    sid, error = admin_api._choose_generation_slice_id(
        planned_slice_ids=[101],
        planned_slots=[{"slice_id": 101, "route_prefix": "A", "mastery": "掌握"}],
        success_index=0,
        candidate_ids=[101, 102, 103],
        attempt_count=1,
        target_question_count=1,
        excluded_slice_ids={101},
        candidate_lookup=lookup,
    )

    assert error == ""
    assert sid == 102


def test_choose_generation_slice_id_falls_back_to_template_bucket_after_same_l3_exhausted():
    lookup = admin_api._build_slice_candidate_lookup(
        [
            {"slice_id": 101, "path": "A > B > C > x", "mastery": "掌握"},
            {"slice_id": 102, "path": "A > B > C > y", "mastery": "掌握"},
            {"slice_id": 103, "path": "A > B > D > z", "mastery": "掌握"},
        ],
        template_route_rules=[{"path_prefix": "A", "ratio": 1}],
    )

    sid, error = admin_api._choose_generation_slice_id(
        planned_slice_ids=[101],
        planned_slots=[{"slice_id": 101, "route_prefix": "A", "mastery": "掌握"}],
        success_index=0,
        candidate_ids=[101, 102, 103],
        attempt_count=1,
        target_question_count=1,
        excluded_slice_ids={101, 102},
        candidate_lookup=lookup,
    )

    assert error == ""
    assert sid == 103


def test_template_task_failed_slice_gets_one_retry_before_exclusion():
    counts = {}
    assert admin_api._is_template_same_mastery_hard_gap(
        planned_slots=[{"slice_id": 345, "route_prefix": "第二篇  干部管理篇", "mastery": "熟悉"}],
        success_index=0,
        sid=345,
        candidate_lookup={
            "template_bucket_to_ids": {
                ("第二篇  干部管理篇", "熟悉"): [345],
            }
        },
    )
    assert not admin_api._should_exclude_failed_slice_from_task(
        allow_single_retry=True,
        sid=345,
        failure_counts=counts,
    )
    assert counts[345] == 1
    assert admin_api._should_exclude_failed_slice_from_task(
        allow_single_retry=True,
        sid=345,
        failure_counts=counts,
    )
    assert counts[345] == 2


def test_non_template_task_failed_slice_excludes_immediately():
    counts = {}
    assert admin_api._should_exclude_failed_slice_from_task(
        allow_single_retry=False,
        sid=345,
        failure_counts=counts,
    )


def test_is_task_cancelled_inherits_parent_cancel_flag():
    parent_id = "task_parent_cancel"
    child_id = "task_child_cancel"
    with admin_api.GEN_TASK_LOCK:
        original_parent = admin_api.GEN_TASKS.get(parent_id)
        original_child = admin_api.GEN_TASKS.get(child_id)
        admin_api.GEN_TASKS[parent_id] = {"task_id": parent_id, "cancel_requested": True}
        admin_api.GEN_TASKS[child_id] = {"task_id": child_id, "parent_task_id": parent_id, "cancel_requested": False}
    try:
        assert admin_api._is_task_cancelled(child_id) is True
    finally:
        with admin_api.GEN_TASK_LOCK:
            if original_parent is None:
                admin_api.GEN_TASKS.pop(parent_id, None)
            else:
                admin_api.GEN_TASKS[parent_id] = original_parent
            if original_child is None:
                admin_api.GEN_TASKS.pop(child_id, None)
            else:
                admin_api.GEN_TASKS[child_id] = original_child


def test_template_slot_with_same_mastery_alternative_should_not_retry_failed_slice():
    assert not admin_api._is_template_same_mastery_hard_gap(
        planned_slots=[{"slice_id": 345, "route_prefix": "第二篇  干部管理篇", "mastery": "熟悉"}],
        success_index=0,
        sid=345,
        candidate_lookup={
            "template_bucket_to_ids": {
                ("第二篇  干部管理篇", "熟悉"): [345, 361],
            }
        },
    )


def test_template_gap_failed_item_persists_as_needs_fix(tmp_path: Path):
    bank_path = tmp_path / "bank.jsonl"
    question_trace = {
        "critic_result": {
            "passed": False,
            "reason": "反向解题失败",
            "fix_reason": "补充题干条件",
            "fail_types": ["reverse_solve_fail"],
            "all_issues": ["题干缺少关键条件"],
        }
    }
    final_json = {
        "题干": "示例题干",
        "正确答案": "A",
        "题目类型": "单选题",
    }
    attempt_error_info = {
        "error_key": "critic:reverse_solve_fail",
        "reason": "反向解题失败",
        "evidence": "题干缺少关键条件",
        "solution": "补充题干条件",
        "fail_types": ["reverse_solve_fail"],
        "missing_conditions": ["条件X"],
        "basis_paths": ["第二篇  干部管理篇 > 第一章  干部考核与评价"],
    }

    persisted, saved_item, save_err = admin_api._persist_template_gap_failed_item(
        enabled=True,
        path=bank_path,
        final_json=final_json,
        question_trace=question_trace,
        attempt_error_info=attempt_error_info,
        task_id="task_1",
        task_name="模板任务",
        run_id="run_1",
    )

    assert persisted is True
    assert save_err == ""
    assert isinstance(saved_item, dict)
    assert saved_item["审计状态"] == "needs_fix"
    assert saved_item["是否正式通过"] is False
    assert saved_item["待修复"] is True
    assert saved_item["待修复建议"] == "补充题干条件"
    assert bank_path.exists()
    content = bank_path.read_text(encoding="utf-8")
    assert '"审计状态": "needs_fix"' in content
