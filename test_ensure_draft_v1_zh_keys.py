from exam_graph import _ensure_draft_v1


def test_ensure_draft_v1_accepts_zh_keys():
    draft = _ensure_draft_v1(
        {
            "题干": "某题干（　）。",
            "选项1": "甲",
            "选项2": "乙",
            "选项3": "丙",
            "选项4": "丁",
            "正确答案": "B",
            "解析": "1、教材原文：x\n2、试题分析：y\n3、结论：本题答案为B。",
        }
    )
    assert draft["question"] == "某题干（　）。"
    assert draft["options"][:4] == ["甲", "乙", "丙", "丁"]
    assert str(draft["answer"]).upper() == "B"
    assert "教材原文" in draft["explanation"]

