from admin_api import (
    _build_resume_task_body_from_source,
    _build_task_questions_from_bank,
    _build_generation_template_plan,
    _choose_generation_slice_id,
    _collect_unique_saved_template_traces,
    _hydrate_judge_run_questions_from_parent_task_if_needed,
    _hydrate_run_questions_from_task_if_needed,
    _maybe_reconcile_template_task_selection,
    _parse_template_child_task_name,
    _reconcile_template_bank_formal_selection,
    _rebuild_template_resume_gap_plan,
)


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


def test_choose_generation_slice_id_prefers_unused_peer_without_breaking_template_bucket():
    candidate_lookup = {
        "by_id": {
            101: {"slice_id": 101, "bucket_key": ("第三篇  新房交易服务", "掌握")},
            102: {"slice_id": 102, "bucket_key": ("第三篇  新房交易服务", "掌握")},
        },
        "bucket_to_ids": {
            ("第三篇  新房交易服务", "掌握"): [101, 102],
        },
        "template_bucket_to_ids": {
            ("第三篇  新房交易服务", "掌握"): [101, 102],
        },
    }
    sid, err = _choose_generation_slice_id(
        planned_slice_ids=[101],
        planned_slots=[{"slice_id": 101, "route_prefix": "第三篇  新房交易服务", "mastery": "掌握"}],
        success_index=0,
        candidate_ids=[101, 102],
        attempt_count=1,
        target_question_count=1,
        excluded_slice_ids=set(),
        candidate_lookup=candidate_lookup,
        slice_usage_counts={101: 1, 102: 0},
        max_questions_per_slice=1,
    )
    assert err == ""
    assert sid == 102


def test_parse_template_child_task_name_supports_resume_child():
    parsed = _parse_template_child_task_name(
        "上海-新房总监-100题-631-考试占比-20260327-1821",
        "上海-新房总监-100题-631-考试占比-20260327-1821#resume89",
    )
    assert parsed == {
        "kind": "resume",
        "label": "resume89",
        "round": 0,
        "shard_index": 89,
    }


def test_judge_run_hydrates_parent_task_questions_for_template_child(monkeypatch):
    def fake_build_task_questions_from_bank(_tenant_id, task_name, include_template_backups=False):
        assert task_name == "上海-新房总监-100题-631-考试占比-20260327-1821"
        assert include_template_backups is True
        return [
            {"question_id": "bank_task:1", "saved": True, "question_text": "q1"},
            {"question_id": "bank_task:2", "saved": True, "question_text": "q2"},
            {"question_id": "bank_task:3", "saved": True, "question_text": "q3"},
        ]

    monkeypatch.setattr("admin_api._build_task_questions_from_bank", fake_build_task_questions_from_bank)

    run = {
        "task_name": "上海-新房总监-100题-631-考试占比-20260327-1821#resume98_100_r1",
        "questions": [{"question_id": "child:1", "saved": True, "question_text": "only-one"}],
        "config": {
            "task_name": "上海-新房总监-100题-631-考试占比-20260327-1821#resume98_100_r1",
            "parent_task_name": "上海-新房总监-100题-631-考试占比-20260327-1821",
            "template_id": "tpl_xxx",
            "child_kind": "resume",
        },
    }

    hydrated, changed = _hydrate_judge_run_questions_from_parent_task_if_needed("sh", run, requested_ids_raw=None)

    assert changed is True
    assert len(hydrated["questions"]) == 3
    assert hydrated["judge_scope"] == {
        "mode": "task_aggregate",
        "parent_task_name": "上海-新房总监-100题-631-考试占比-20260327-1821",
        "question_count": 3,
    }


def test_collect_unique_saved_template_traces_dedupes_by_target_index():
    traces = [
        {"index": 1, "target_index": 1, "saved": True, "final_json": {"题干": "old-1"}},
        {"index": 2, "target_index": 2, "saved": True, "final_json": {"题干": "ok-2"}},
        {"index": 3, "target_index": 1, "saved": True, "final_json": {"题干": "new-1"}},
        {"index": 4, "target_index": 4, "saved": True, "final_json": {"题干": "out-of-range"}},
    ]

    deduped = _collect_unique_saved_template_traces(
        planned_slots=[
            {"slice_id": 1, "route_prefix": "A", "mastery": "掌握"},
            {"slice_id": 2, "route_prefix": "B", "mastery": "熟悉"},
            {"slice_id": 3, "route_prefix": "C", "mastery": "了解"},
        ],
        process_trace=traces,
    )

    assert [item["target_index"] for item in deduped] == [1, 2]
    assert [item["final_json"]["题干"] for item in deduped] == ["new-1", "ok-2"]


def test_reconcile_template_bank_formal_selection_marks_backup(monkeypatch):
    bank_rows = [
        {"题干": "正式题1", "来源切片ID": 11, "出题RunID": "run_a", "出题任务名称": "模板任务#p1"},
        {"题干": "正式题2", "来源切片ID": 12, "出题RunID": "run_a", "出题任务名称": "模板任务#p1"},
        {"题干": "备选题2", "来源切片ID": 13, "出题RunID": "run_b", "出题任务名称": "模板任务#repair1"},
    ]
    saved_rows = {}

    monkeypatch.setattr("admin_api.tenant_bank_path", lambda _tenant_id: "dummy")
    monkeypatch.setattr("admin_api._load_bank", lambda _path: bank_rows)

    def fake_save(_path, items):
        saved_rows["items"] = items

    monkeypatch.setattr("admin_api._save_bank", fake_save)

    stats = _reconcile_template_bank_formal_selection(
        tenant_id="sh",
        parent_task_name="模板任务",
        planned_slots=[
            {"route_prefix": "R1", "mastery": "掌握"},
            {"route_prefix": "R2", "mastery": "熟悉"},
        ],
        process_trace=[
            {"index": 1, "target_index": 1, "saved": True, "run_id": "run_a", "slice_id": 11, "final_json": {"题干": "正式题1"}},
            {"index": 2, "target_index": 2, "saved": True, "run_id": "run_a", "slice_id": 12, "final_json": {"题干": "正式题2"}},
        ],
    )

    assert stats == {"official_count": 2, "backup_count": 1, "updated_count": 3}
    rows = saved_rows["items"]
    assert rows[0]["模板正式题"] is True
    assert rows[0]["是否正式通过"] is True
    assert rows[1]["模板目标位次"] == 2
    assert rows[2]["模板备选题"] is True
    assert rows[2]["是否正式通过"] is False
    assert rows[2]["审计状态"] == "template_backup_pass"


def test_build_task_questions_from_bank_only_uses_template_formal_questions(monkeypatch):
    bank_rows = [
        {"题干": "正式题", "正确答案": "A", "解析": "x", "来源路径": "R", "来源切片ID": 1, "出题任务名称": "模板任务#p1", "模板正式题": True},
        {"题干": "备选题", "正确答案": "B", "解析": "y", "来源路径": "R", "来源切片ID": 2, "出题任务名称": "模板任务#resume1", "模板备选题": True, "是否正式通过": False},
    ]
    monkeypatch.setattr("admin_api.tenant_bank_path", lambda _tenant_id: "dummy")
    monkeypatch.setattr("admin_api._load_bank", lambda _path: bank_rows)

    questions = _build_task_questions_from_bank("sh", "模板任务")

    assert len(questions) == 1
    assert questions[0]["question_text"] == "正式题"


def test_build_task_questions_from_bank_can_include_template_backups_for_judge(monkeypatch):
    bank_rows = [
        {"题干": "正式题", "正确答案": "A", "解析": "x", "来源路径": "R", "来源切片ID": 1, "出题任务名称": "模板任务#p1", "模板正式题": True},
        {"题干": "备选题", "正确答案": "B", "解析": "y", "来源路径": "R", "来源切片ID": 2, "出题任务名称": "模板任务#resume1", "模板备选题": True, "审计状态": "template_backup_pass", "是否正式通过": False},
    ]
    monkeypatch.setattr("admin_api.tenant_bank_path", lambda _tenant_id: "dummy")
    monkeypatch.setattr("admin_api._load_bank", lambda _path: bank_rows)

    questions = _build_task_questions_from_bank("sh", "模板任务", include_template_backups=True)

    assert len(questions) == 2
    assert [q["question_text"] for q in questions] == ["正式题", "备选题"]
    assert questions[1]["template_backup"] is True


def test_build_resume_task_body_from_source_keeps_template_resume_open_when_count_is_full():
    body = _build_resume_task_body_from_source(
        "sh",
        {
            "task_name": "模板任务",
            "generated_count": 100,
            "saved_count": 100,
            "request": {
                "task_name": "模板任务",
                "num_questions": 100,
                "template_id": "tpl_1",
                "template_name": "模板",
            },
        },
        inplace=True,
    )

    assert isinstance(body, dict)
    assert body["num_questions"] == 0
    assert body["resume_remaining_count"] == 0


def test_rebuild_template_resume_gap_plan_uses_target_indexes_not_slice_counts(monkeypatch):
    monkeypatch.setattr("admin_api._get_gen_template", lambda _tenant_id, _template_id: {"template_id": "tpl_1", "route_rules": [], "mastery_ratio": {}})
    monkeypatch.setattr("admin_api._resolve_material_version_id", lambda _tenant_id, _req: "mv1")
    monkeypatch.setattr("admin_api._resolve_slice_file_for_material", lambda _tenant_id, _mv: "dummy")
    monkeypatch.setattr("admin_api._load_kb_items_from_file", lambda _kb: [{"完整路径": "R1", "掌握程度": "掌握"}])
    monkeypatch.setattr("admin_api._load_slice_review_for_material", lambda _tenant_id, _mv: {"0": {"review_status": "approved"}})
    monkeypatch.setattr("admin_api._resolve_history_path_for_material", lambda _tenant_id, _mv: "hist")
    monkeypatch.setattr("admin_api._resolve_mapping_path_for_material", lambda _tenant_id, _mv: "map")
    monkeypatch.setattr("admin_api.tenant_mapping_path", lambda _tenant_id: "map")
    monkeypatch.setattr("admin_api.tenant_mapping_review_path", lambda _tenant_id: "review")
    monkeypatch.setattr("admin_api._blocked_slice_ids_for_material", lambda _tenant_id, _mv: set())
    monkeypatch.setattr("admin_api._filter_candidate_ids_by_question_type", lambda _retriever, ids, _qt: (ids, []))

    class Retriever:
        kb_data = [{"完整路径": "R1", "掌握程度": "掌握"}]

    monkeypatch.setattr("admin_api._get_cached_retriever", lambda **_kwargs: Retriever())
    monkeypatch.setattr(
        "admin_api._build_generation_template_plan",
        lambda **_kwargs: {
            "planned_slots": [
                {"slice_id": 0, "route_prefix": "R1", "mastery": "掌握"},
                {"slice_id": 0, "route_prefix": "R2", "mastery": "熟悉"},
            ]
        },
    )
    monkeypatch.setattr("admin_api._build_slice_candidate_lookup", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "admin_api._analyze_template_parallel_result",
        lambda **_kwargs: {
            "ok": False,
            "missing_target_indexes": [2],
            "invalid_targets": [],
        },
    )

    patched, err = _rebuild_template_resume_gap_plan(
        "sh",
        {
            "request": {
                "template_id": "tpl_1",
                "num_questions": 2,
                "question_type": "随机",
                "material_version_id": "mv1",
            },
            "process_trace": [
                {"saved": True, "target_index": 1, "slice_id": 0},
                {"saved": True, "target_index": 999, "slice_id": 0},
            ],
        },
        {"num_questions": 0},
    )

    assert err is None
    assert patched["num_questions"] == 1
    assert patched["planned_slots"][0]["_global_target_index"] == 2


def test_maybe_reconcile_template_task_selection_updates_backup_count(monkeypatch):
    monkeypatch.setattr(
        "admin_api._resolve_template_parallel_context",
        lambda _tenant_id, _body: (
            {
                "planned_slots": [
                    {"slice_id": 1, "route_prefix": "R1", "mastery": "掌握"},
                    {"slice_id": 2, "route_prefix": "R2", "mastery": "熟悉"},
                ]
            },
            None,
        ),
    )
    monkeypatch.setattr(
        "admin_api._reconcile_template_bank_formal_selection",
        lambda **_kwargs: {"official_count": 2, "backup_count": 1, "updated_count": 0},
    )

    patched = _maybe_reconcile_template_task_selection(
        "sh",
        {
            "task_name": "模板任务",
            "material_version_id": "mv1",
            "request": {
                "template_id": "tpl_1",
                "num_questions": 2,
                "question_type": "随机",
                "material_version_id": "mv1",
            },
            "process_trace": [{"saved": True, "target_index": 1}],
        },
    )

    assert patched["backup_count"] == 1
    assert patched["template_selection"] == {"official_count": 2, "backup_count": 1, "updated_count": 0}


def test_hydrate_run_questions_skips_trace_scan_when_question_payload_is_already_complete():
    run = {
        "questions": [
            {
                "question_text": "题干",
                "answer": "A",
                "options": ["A. x", "B. y"],
            }
        ],
        "config": {"task_id": "task_x"},
    }

    hydrated, changed = _hydrate_run_questions_from_task_if_needed("sh", run)

    assert changed is False
    assert hydrated is run
