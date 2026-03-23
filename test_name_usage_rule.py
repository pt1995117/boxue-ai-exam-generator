import exam_graph


def test_name_usage_flags_anonymous_name_in_judgement_style_question():
    issues = exam_graph.validate_name_usage(
        "张某在上海购买住房。请判断下列关于其购房行为说法中正确的是（　）。",
        ["A. 说法1", "B. 说法2", "C. 说法3", "D. 说法4"],
        "1、教材原文：...\n2、试题分析：...\n3、结论：本题答案为A。",
    )
    assert any("不得使用“张某/某某”代称" in x for x in issues)


def test_name_usage_allows_anonymous_name_in_non_judgement_stem():
    issues = exam_graph.validate_name_usage(
        "在某起违规事故复盘场景中，张某的流程角色应标注为（　）。",
        ["A. 房源录入人", "B. 钥匙人", "C. 实勘人", "D. 带看人"],
        "1、教材原文：...\n2、试题分析：...\n3、结论：本题答案为A。",
    )
    assert not any("不得使用“张某/某某”代称" in x for x in issues)


def test_name_usage_flags_anonymous_name_in_non_negative_scene():
    issues = exam_graph.validate_name_usage(
        "张某计划购买首套普通住宅，以下表述正确的是（　）。",
        ["A. 说法1", "B. 说法2", "C. 说法3", "D. 说法4"],
        "1、教材原文：...\n2、试题分析：...\n3、结论：本题答案为A。",
    )
    assert any("非事故/违法违规场景不应使用“张某/某某”代称" in x for x in issues)


def test_name_usage_flags_title_and_funny_name_and_nickname():
    issues = exam_graph.validate_name_usage(
        "王先生向经纪人小李咨询，客户小宝后续将签约。",
        ["A. 张漂亮可代签", "B. 仅本人签字", "C. 贝贝可代签", "D. 贾董事可代签"],
        "1、教材原文：...\n2、试题分析：...\n3、结论：本题答案为B。",
    )
    assert any("姓+女士/先生" in x for x in issues)
    assert any("小+姓氏" in x for x in issues)
    assert any("小名/乳名" in x for x in issues)
    assert any("不规范姓名" in x for x in issues)


def test_name_usage_flags_name_inconsistency_between_stem_and_options():
    issues = exam_graph.validate_name_usage(
        "张伟购买首套住房，以下说法正确的是（　）。",
        ["A. 李娜需按二套计税", "B. 张伟可按首套计税", "C. 王强可免税", "D. 张伟可享补贴"],
        "1、教材原文：...\n2、试题分析：...\n3、结论：本题答案为B。",
    )
    assert any("选项中的人名与题干不一致" in x for x in issues)
