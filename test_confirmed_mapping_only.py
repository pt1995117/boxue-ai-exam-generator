import json

import pandas as pd

from exam_factory import KnowledgeRetriever, set_active_tenant
from tenants_config import tenant_mapping_review_path


def test_retriever_uses_confirmed_mapping_only(tmp_path):
    tenant_id = "ut_confirmed"
    set_active_tenant(tenant_id)

    kb_path = tmp_path / "kb.jsonl"
    mapping_path = tmp_path / "knowledge_question_mapping.json"
    history_path = tmp_path / "history.xlsx"

    kb_entry = {
        "完整路径": "第一章 > 测试知识点",
        "核心内容": "测试内容"
    }
    kb_path.write_text(json.dumps(kb_entry, ensure_ascii=False) + "\n", encoding="utf-8")

    mapping = {
        "0": {
            "完整路径": "第一章 > 测试知识点",
            "matched_questions": [
                {"question_index": 0, "confidence": 0.9, "method": "exact_path_match"},
                {"question_index": 1, "confidence": 0.9, "method": "exact_path_match"},
            ]
        }
    }
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

    review_path = tenant_mapping_review_path(tenant_id)
    review_path.write_text(
        json.dumps(
            {
                "0:0": {"confirm_status": "confirmed"},
                "0:1": {"confirm_status": "rejected"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    df = pd.DataFrame(
        [
            {"题干": "题干A", "选项1": "A1", "选项2": "B1", "选项3": "", "选项4": "", "正确答案": "A", "解析": "解析A", "难度值": 0.5, "考点": "测试"},
            {"题干": "题干B", "选项1": "A2", "选项2": "B2", "选项3": "", "选项4": "", "正确答案": "A", "解析": "解析B", "难度值": 0.5, "考点": "测试"},
        ]
    )
    df.to_excel(history_path, index=False)

    retriever = KnowledgeRetriever(str(kb_path), str(history_path), str(mapping_path))
    examples = retriever.get_examples_by_knowledge_point(kb_entry, k=5)

    assert len(examples) == 1
    assert examples[0]["题干"] == "题干A"
