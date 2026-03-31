from admin_api import _normalize_run_batch_metrics


def test_normalize_run_batch_metrics_prefers_question_saved_flags():
    run = {
        "run_id": "run_x",
        "batch_metrics": {
            "generated_count": 67,
            "saved_count": 67,
        },
        "questions": [
            {"question_id": "q1", "saved": True},
            {"question_id": "q2", "saved": True},
            {"question_id": "q3", "saved": False},
            {"question_id": "q4", "saved": True},
        ],
    }

    normalized, changed = _normalize_run_batch_metrics(run)

    assert changed is True
    assert normalized["batch_metrics"]["generated_count"] == 4
    assert normalized["batch_metrics"]["saved_count"] == 3
