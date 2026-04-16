import json

import admin_api
from admin_api import app


def _write_bank(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows), encoding="utf-8")


def test_bank_update_single_question(tmp_path, monkeypatch):
    bank_path = tmp_path / "bank.jsonl"
    _write_bank(
        bank_path,
        [
            {
                "题干": "原题干",
                "选项1": "A1",
                "选项2": "B1",
                "正确答案": "A",
                "解析": "原解析",
                "来源路径": "P1",
            }
        ],
    )
    monkeypatch.setattr(admin_api, "tenant_bank_path", lambda _tenant_id: bank_path)

    client = app.test_client()
    resp = client.post(
        "/api/hz/bank/update",
        json={
            "question_id": 0,
            "item": {
                "题干": "新题干",
                "选项1": "A2",
                "选项2": "B2",
                "正确答案": "b",
                "解析": "新解析",
                "来源路径": "P2",
            },
        },
        headers={"X-System-User": "admin"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["updated"] == 1
    assert data["item"]["题干"] == "新题干"
    assert data["item"]["正确答案"] == "B"

    rows = admin_api._load_bank(bank_path)
    assert rows[0]["题干"] == "新题干"
    assert rows[0]["来源路径"] == "P2"


def test_bank_optimize_returns_merged_item(tmp_path, monkeypatch):
    bank_path = tmp_path / "bank.jsonl"
    _write_bank(
        bank_path,
        [
            {
                "题干": "旧题干",
                "选项1": "旧A",
                "选项2": "旧B",
                "正确答案": "A",
                "解析": "旧解析",
                "来源路径": "保留路径",
            }
        ],
    )
    monkeypatch.setattr(admin_api, "tenant_bank_path", lambda _tenant_id: bank_path)
    monkeypatch.setattr(
        admin_api,
        "_resolve_generation_llm_from_primary_key",
        lambda: ("test-key", "https://example.com/v1", "test-model"),
    )

    def fake_call_llm(**kwargs):
        _ = kwargs
        return (
            json.dumps(
                {
                    "题干": "优化后题干",
                    "选项1": "新A",
                    "选项2": "新B",
                    "正确答案": "b",
                    "解析": "优化后解析",
                },
                ensure_ascii=False,
            ),
            "",
            {},
        )

    monkeypatch.setattr(admin_api, "call_llm", fake_call_llm)

    client = app.test_client()
    resp = client.post(
        "/api/hz/bank/optimize",
        json={"question_id": 0, "feedback": "请更贴近实战，语言更简洁"},
        headers={"X-System-User": "admin"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    item = data["item"]
    assert item["题干"] == "优化后题干"
    assert item["正确答案"] == "B"
    assert item["来源路径"] == "保留路径"


def test_bank_optimize_fallback_parses_non_json_output(tmp_path, monkeypatch):
    bank_path = tmp_path / "bank.jsonl"
    _write_bank(
        bank_path,
        [
            {
                "题干": "旧题干",
                "选项1": "旧A",
                "选项2": "旧B",
                "正确答案": "A",
                "解析": "旧解析",
                "来源路径": "保留路径",
            }
        ],
    )
    monkeypatch.setattr(admin_api, "tenant_bank_path", lambda _tenant_id: bank_path)
    monkeypatch.setattr(
        admin_api,
        "_resolve_generation_llm_from_primary_key",
        lambda: ("test-key", "https://example.com/v1", "test-model"),
    )

    def fake_call_llm(**kwargs):
        _ = kwargs
        return (
            "\n".join(
                [
                    "题干：优化后的非JSON题干",
                    "A. 新A",
                    "B. 新B",
                    "正确答案：B",
                    "解析：优化后的非JSON解析",
                ]
            ),
            "",
            {},
        )

    monkeypatch.setattr(admin_api, "call_llm", fake_call_llm)
    # Force JSON parser failure to exercise text fallback path.
    monkeypatch.setattr(admin_api, "parse_json_from_response", lambda _text: (_ for _ in ()).throw(ValueError("bad json")))

    client = app.test_client()
    resp = client.post(
        "/api/hz/bank/optimize",
        json={"question_id": 0, "feedback": "请优化文案"},
        headers={"X-System-User": "admin"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    item = data["item"]
    assert item["题干"] == "优化后的非JSON题干"
    assert item["选项1"] == "新A"
    assert item["选项2"] == "新B"
    assert item["正确答案"] == "B"
    assert item["解析"] == "优化后的非JSON解析"
