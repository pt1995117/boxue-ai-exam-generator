from pathlib import Path
from types import SimpleNamespace

from admin_api import app


def test_upload_text_triggers_slice_generation(monkeypatch):
    def fake_text_to_docx(text: str, output_docx: Path):
        output_docx.write_text(text, encoding="utf-8")

    def fake_run(cmd, capture_output, text, cwd):
        out_path = Path(cmd[cmd.index("--output") + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            '{"完整路径":"测试路径","掌握程度":"了解","结构化内容":{"context_before":"abc","context_after":"","tables":[],"images":[],"formulas":[],"examples":[],"key_params":[],"rules":[]}}\n',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("admin_api._text_to_docx", fake_text_to_docx)
    monkeypatch.setattr("admin_api.subprocess.run", fake_run)

    client = app.test_client()
    resp = client.post(
        "/api/hz/materials/upload",
        headers={"X-System-User": "admin"},
        data={"text": "第一章 测试教材\n这是一段内容"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["slice_count"] >= 1
    assert Path(payload["slices_file"]).exists()
