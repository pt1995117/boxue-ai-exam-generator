from admin_api import _build_gen_task_summary, _recover_task_counts_from_subtasks


def test_recover_task_counts_from_subtasks_for_parent_task():
    task = {
        "task_id": "task_parent",
        "status": "failed",
        "generated_count": 33,
        "saved_count": 33,
        "progress": {"current": 33, "total": 100},
        "request": {"num_questions": 100},
        "subtasks": [
            {"task_id": "c1", "generated_count": 8, "saved_count": 8},
            {"task_id": "c2", "generated_count": 6, "saved_count": 6},
            {"task_id": "c3", "generated_count": 12, "saved_count": 12},
            {"task_id": "c4", "generated_count": 6, "saved_count": 6},
            {"task_id": "c5", "generated_count": 20, "saved_count": 20},
        ],
    }
    patched = _recover_task_counts_from_subtasks(task)
    assert patched["generated_count"] == 52
    assert patched["saved_count"] == 52
    assert patched["progress"]["current"] == 52
    assert patched["progress"]["total"] == 100


def test_build_gen_task_summary_applies_subtask_count_recovery():
    summary = _build_gen_task_summary(
        {
            "task_id": "task_parent",
            "tenant_id": "sh",
            "status": "failed",
            "generated_count": 33,
            "saved_count": 33,
            "progress": {"current": 33, "total": 100},
            "request": {"task_name": "T", "num_questions": 100},
            "subtasks": [
                {"task_id": "c1", "generated_count": 8, "saved_count": 8},
                {"task_id": "c2", "generated_count": 6, "saved_count": 6},
                {"task_id": "c3", "generated_count": 12, "saved_count": 12},
                {"task_id": "c4", "generated_count": 6, "saved_count": 6},
                {"task_id": "c5", "generated_count": 20, "saved_count": 20},
            ],
        }
    )
    assert summary["generated_count"] == 52
    assert summary["saved_count"] == 52
    assert summary["progress"]["current"] == 52
