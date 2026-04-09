import admin_api


def test_make_gen_task_persists_planned_slots():
    body = {
        "task_name": "模板子任务#p1",
        "num_questions": 2,
        "question_type": "随机",
        "generation_mode": "随机",
        "difficulty": "中等 (0.5-0.7)",
        "planned_slice_ids": [101, "102", "x"],
        "planned_slots": [
            {"slice_id": 101, "route_prefix": "第三篇  新房交易服务", "mastery": "掌握", "_global_target_index": 1},
            {"slice_id": "102", "route_prefix": "第四篇  服务保障", "mastery": "熟悉", "_global_target_index": "2"},
            {"slice_id": "bad", "route_prefix": "第一篇", "mastery": "了解"},
        ],
    }

    task = admin_api._make_gen_task("sh", "admin", body)
    req = task.get("request") if isinstance(task.get("request"), dict) else {}

    assert req.get("planned_slice_ids") == [101, 102]
    assert req.get("planned_slots") == [
        {
            "slice_id": 101,
            "route_prefix": "第三篇  新房交易服务",
            "mastery": "掌握",
            "_global_target_index": 1,
        },
        {
            "slice_id": 102,
            "route_prefix": "第四篇  服务保障",
            "mastery": "熟悉",
            "_global_target_index": 2,
        },
    ]

