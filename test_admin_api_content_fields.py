from admin_api import app


def test_slice_content_fields_schema_for_hz():
    client = app.test_client()
    resp = client.get("/api/hz/slices?page=1&page_size=5&status=all", headers={"X-System-User": "admin"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data.get("items"), list)
    if data["items"]:
        first = data["items"][0]
        assert isinstance(first.get("preview"), str)
        assert isinstance(first.get("slice_content"), str)


def test_mapping_content_fields_schema_for_hz():
    client = app.test_client()
    resp = client.get("/api/hz/mappings?page=1&page_size=5&status=all", headers={"X-System-User": "admin"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data.get("items"), list)
    if data["items"]:
        first = data["items"][0]
        assert isinstance(first.get("slice_content"), str)
        assert isinstance(first.get("question_stem"), str)
