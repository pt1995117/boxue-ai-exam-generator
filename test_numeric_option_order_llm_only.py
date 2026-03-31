import exam_graph


def test_writer_postprocess_does_not_hard_sort_numeric_options():
    payload = {
        "题干": "某题金额为（　）万元。",
        "选项1": "0.4",
        "选项2": "0",
        "选项3": "0.6",
        "选项4": "1.4",
        "正确答案": "A",
        "解析": (
            "1、教材原文：规则。\n"
            "2、试题分析：选项A（0.4）与计算结果一致。\n"
            "3、结论：本题答案为A。"
        ),
    }

    out = exam_graph.repair_final_json_format(payload, "单选题")

    assert out["选项1"] == "0.4"
    assert out["选项2"] == "0"
    assert out["正确答案"] == "A"
    assert "选项A（0.4）" in out["解析"]
