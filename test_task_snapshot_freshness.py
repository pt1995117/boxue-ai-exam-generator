from admin_api import _pick_newer_terminal_task_snapshot


def test_pick_newer_terminal_snapshot_overrides_stale_running_snapshot():
    live = {
        "task_id": "task_x",
        "status": "running",
        "updated_at": "2026-04-02T12:52:12.803973+00:00",
        "generated_count": 0,
        "saved_count": 0,
        "progress": {"current": 0, "total": 100},
    }
    persisted = {
        "task_id": "task_x",
        "status": "failed",
        "updated_at": "2026-04-02T13:46:19.786876+00:00",
        "generated_count": 33,
        "saved_count": 33,
        "progress": {"current": 33, "total": 100},
    }
    out = _pick_newer_terminal_task_snapshot(live, persisted)
    assert isinstance(out, dict)
    assert out["status"] == "failed"
    assert int(out["generated_count"]) == 33
    assert int((out.get("progress") or {}).get("current", 0)) == 33


def test_pick_newer_terminal_snapshot_keeps_newer_running_snapshot():
    live = {
        "task_id": "task_x",
        "status": "running",
        "updated_at": "2026-04-02T13:46:20.000000+00:00",
        "generated_count": 34,
        "saved_count": 34,
    }
    persisted = {
        "task_id": "task_x",
        "status": "failed",
        "updated_at": "2026-04-02T13:46:19.786876+00:00",
        "generated_count": 33,
        "saved_count": 33,
    }
    out = _pick_newer_terminal_task_snapshot(live, persisted)
    assert isinstance(out, dict)
    assert out["status"] == "running"
    assert int(out["generated_count"]) == 34
