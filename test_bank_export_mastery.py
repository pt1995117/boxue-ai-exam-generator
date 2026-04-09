from io import BytesIO
import json

import pandas as pd

import admin_api
from admin_api import app


def _write_bank(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows), encoding="utf-8")


def test_bank_export_includes_mastery_column(tmp_path, monkeypatch):
    bank_path = tmp_path / "bank.jsonl"
    _write_bank(
        bank_path,
        [
            {
                "题干": "题目A",
                "选项1": "选项A1",
                "选项2": "选项A2",
                "正确答案": "A",
                "解析": "解析A",
                "模板掌握度": "熟悉",
                "掌握程度": "了解",
                "来源路径": "第三篇  新房交易服务 > 第一章  个人住房商业性贷款",
            },
            {
                "题干": "题目B",
                "选项1": "选项B1",
                "选项2": "选项B2",
                "正确答案": "B",
                "解析": "解析B",
                "掌握程度": "掌握",
                "来源路径": "第二篇  新房经纪服务 > 第一章  新房房源",
            },
        ],
    )
    monkeypatch.setattr(admin_api, "tenant_bank_path", lambda _tenant_id: bank_path)

    client = app.test_client()
    resp = client.post(
        "/api/sh/bank/export",
        json={"question_ids": [0, 1]},
        headers={"X-System-User": "admin"},
    )
    assert resp.status_code == 200
    df = pd.read_excel(BytesIO(resp.data))
    assert "掌握程度" in df.columns
    # 优先模板掌握度；无模板掌握度时回退掌握程度。
    assert list(df["掌握程度"].fillna("").astype(str)) == ["熟悉", "掌握"]


def test_bank_export_only_template_official_filters_backups(tmp_path, monkeypatch):
    bank_path = tmp_path / "bank.jsonl"
    _write_bank(
        bank_path,
        [
            {
                "题干": "正式题",
                "选项1": "A1",
                "选项2": "A2",
                "正确答案": "A",
                "解析": "解析A",
                "模板任务": True,
                "模板正式题": True,
                "模板备选题": False,
                "来源路径": "第三篇  新房交易服务 > 第一章",
            },
            {
                "题干": "备选题",
                "选项1": "B1",
                "选项2": "B2",
                "正确答案": "B",
                "解析": "解析B",
                "模板任务": True,
                "模板正式题": False,
                "模板备选题": True,
                "来源路径": "第二篇  新房经纪服务 > 第一章",
            },
            {
                "题干": "普通题",
                "选项1": "C1",
                "选项2": "C2",
                "正确答案": "A",
                "解析": "解析C",
                "来源路径": "第一篇 行业与链家 > 第一章",
            },
        ],
    )
    monkeypatch.setattr(admin_api, "tenant_bank_path", lambda _tenant_id: bank_path)

    client = app.test_client()
    resp = client.post(
        "/api/sh/bank/export",
        json={"question_ids": [0, 1, 2], "only_template_official": True},
        headers={"X-System-User": "admin"},
    )
    assert resp.status_code == 200
    df = pd.read_excel(BytesIO(resp.data))
    stems = list(df["题干(必填)"].fillna("").astype(str))
    assert stems == ["正式题", "普通题"]
