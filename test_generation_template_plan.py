from admin_api import _build_generation_template_plan


def test_build_generation_template_plan_finds_feasible_solution_after_greedy_dead_end():
    template = {
        "mastery_ratio": {
            "掌握": 6.0,
            "熟悉": 3.0,
            "了解": 1.0,
        },
        "route_rules": [
            {"path_prefix": "第一篇  行业与链家", "ratio": 5.0},
            {"path_prefix": "第二篇  新房经纪服务", "ratio": 10.0},
            {"path_prefix": "第三篇  新房交易服务", "ratio": 50.0},
            {"path_prefix": "第四篇  服务保障", "ratio": 15.0},
            {"path_prefix": "第一篇 战略导入", "ratio": 5.0},
            {"path_prefix": "第二篇  干部管理篇", "ratio": 15.0},
        ],
    }
    candidate_slices = []
    slice_id = 1
    route_mastery_counts = [
        ("第一篇  行业与链家", {"掌握": 4, "熟悉": 7, "了解": 21}),
        ("第二篇  新房经纪服务", {"掌握": 68, "熟悉": 50, "了解": 33}),
        ("第三篇  新房交易服务", {"掌握": 26, "熟悉": 42, "了解": 17}),
        ("第四篇  服务保障", {"掌握": 11, "熟悉": 28, "了解": 23}),
        ("第一篇 战略导入", {"掌握": 0, "熟悉": 0, "了解": 1}),
        ("第二篇  干部管理篇", {"掌握": 11, "熟悉": 1, "了解": 14}),
    ]
    for path_prefix, counts in route_mastery_counts:
        for mastery, count in counts.items():
            for idx in range(count):
                candidate_slices.append(
                    {
                        "slice_id": slice_id,
                        "path": f"{path_prefix}/节点{idx + 1}",
                        "mastery": mastery,
                    }
                )
                slice_id += 1

    plan = _build_generation_template_plan(
        question_count=100,
        template=template,
        candidate_slices=candidate_slices,
    )

    route_breakdown = {row["path_prefix"]: row for row in plan["route_breakdown"]}
    assert route_breakdown["第一篇 战略导入"]["count"] == 5
    assert {item["mastery"]: item["count"] for item in route_breakdown["第一篇 战略导入"]["mastery_breakdown"]} == {
        "掌握": 0,
        "熟悉": 0,
        "了解": 5,
    }
    assert plan["mastery_counts"] == {"掌握": 60, "熟悉": 30, "了解": 10}
    assert len(plan["planned_slots"]) == 100
