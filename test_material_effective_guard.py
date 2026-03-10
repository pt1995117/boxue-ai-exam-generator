from pathlib import Path

from admin_api import app


def test_set_effective_blocked_without_dual_review_slice(monkeypatch):
    monkeypatch.setattr(
        "admin_api._find_material_record",
        lambda tenant_id, material_version_id: {
            "material_version_id": material_version_id,
            "slice_status": "success",
            "mapping_status": "success",
        },
    )
    monkeypatch.setattr("admin_api._resolve_slice_file_for_material", lambda tenant_id, material_version_id: Path("/tmp/fake_slice.jsonl"))
    monkeypatch.setattr("admin_api._resolve_mapping_path_for_material", lambda tenant_id, material_version_id: Path("/tmp/fake_mapping.json"))
    monkeypatch.setattr("admin_api._has_dual_review_completed_slice", lambda tenant_id, material_version_id: (False, 0))

    client = app.test_client()
    resp = client.post(
        "/api/hz/materials/effective",
        headers={"X-System-User": "admin"},
        json={"material_version_id": "m_test"},
    )

    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["error"]["code"] == "MATERIAL_REVIEW_NOT_READY"


def test_set_effective_passes_with_dual_review_slice(monkeypatch):
    monkeypatch.setattr(
        "admin_api._find_material_record",
        lambda tenant_id, material_version_id: {
            "material_version_id": material_version_id,
            "slice_status": "success",
            "mapping_status": "success",
        },
    )
    monkeypatch.setattr("admin_api._resolve_slice_file_for_material", lambda tenant_id, material_version_id: Path("/tmp/fake_slice.jsonl"))
    monkeypatch.setattr("admin_api._resolve_mapping_path_for_material", lambda tenant_id, material_version_id: Path("/tmp/fake_mapping.json"))
    monkeypatch.setattr("admin_api._has_dual_review_completed_slice", lambda tenant_id, material_version_id: (True, 1))
    monkeypatch.setattr(
        "admin_api.set_effective_material_version",
        lambda tenant_id, material_version_id: {"material_version_id": material_version_id, "status": "effective"},
    )

    client = app.test_client()
    resp = client.post(
        "/api/hz/materials/effective",
        headers={"X-System-User": "admin"},
        json={"material_version_id": "m_test"},
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["item"]["status"] == "effective"


def test_materials_api_contains_effective_guard_fields(monkeypatch):
    monkeypatch.setattr(
        "admin_api.list_material_versions",
        lambda tenant_id: [{
            "material_version_id": "m_test",
            "file_path": "m_test.docx",
            "status": "ready_for_review",
            "slice_status": "success",
            "mapping_status": "success",
        }],
    )
    monkeypatch.setattr("admin_api._has_dual_review_completed_slice", lambda tenant_id, material_version_id: (False, 0))

    client = app.test_client()
    resp = client.get("/api/hz/materials", headers={"X-System-User": "admin"})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["items"]
    first = payload["items"][0]
    assert first["can_set_effective"] is False
    assert first["dual_review_slice_count"] == 0
    assert isinstance(first.get("effective_block_reason"), str)
