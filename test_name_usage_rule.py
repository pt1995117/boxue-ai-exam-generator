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


def test_name_usage_does_not_hard_fail_name_inconsistency_between_stem_and_options():
    issues = exam_graph.validate_name_usage(
        "张伟购买首套住房，以下说法正确的是（　）。",
        ["A. 李娜需按二套计税", "B. 张伟可按首套计税", "C. 王强可免税", "D. 张伟可享补贴"],
        "1、教材原文：...\n2、试题分析：...\n3、结论：本题答案为B。",
    )
    assert not any("选项中的人名与题干不一致" in x for x in issues)


def test_name_usage_flags_mou_goufangren_phrase():
    issues = exam_graph.validate_name_usage(
        "某购房人申请组合贷款，评估值为420万元，商业贷款部分额度为（　）万元。",
        ["100", "200", "300", "400"],
        "1、教材原文：...\n2、试题分析：...\n3、结论：本题答案为A。",
    )
    assert any("一线称谓不规范" in x and "客户" in x for x in issues)


def test_name_usage_allows_stem_only_name_and_nameless_options():
    """题干有「客户王明」、选项为法律后果表述且不含人名时，不得误判「选项人名与题干不一致」（曾由「支付」「双倍返」等误抽取触发）。"""
    stem = (
        "客户王明与开发商签订了《商品房认购书》，约定定金10万元，并约定违约金为房屋总价款的5%。"
        "后开发商违约，王明拟追究开发商责任。根据相关法律规定，王明可以要求开发商（　）。"
    )
    opts = [
        "双倍返还定金20万元并支付违约金",
        "双倍返还定金20万元，但已支付的10万元定金应从返还数额中扣减",
        "支付违约金，并退还已支付的10万元定金",
        "支付违约金，但无需退还已支付的10万元定金",
    ]
    exp = "1、教材原文：…\n2、试题分析：王明应选择主张违约金并退还定金。\n3、结论：本题答案为C。"
    issues = exam_graph.validate_name_usage(stem, opts, exp)
    assert not any("选项中的人名与题干不一致" in x for x in issues)
    assert not any("解析中的人名与题干不一致" in x for x in issues)


def test_name_usage_does_not_treat_business_terms_as_person_names():
    stem = "客户张明符合条件，关于客户方本次购房的税费认定，正确的是（　）。"
    opts = [
        "客户方本次购房按首套认定",
        "客户方本次购房按二套认定",
        "客户方本次购房可免征",
        "客户方本次购房无需判定",
    ]
    exp = "1、教材原文：...\n2、试题分析：客户方本次购房需按规则判定。\n3、结论：本题答案为B。"
    issues = exam_graph.validate_name_usage(stem, opts, exp)
    assert not any("选项中的人名与题干不一致" in x for x in issues)
    assert not any("解析中的人名与题干不一致" in x for x in issues)


def test_name_semantic_risk_detects_multi_name_conflict():
    payload = {
        "题干": "客户王明签约后，以下说法正确的是（　）。",
        "选项1": "李娜可代其解除合同",
        "选项2": "王明可继续履约",
        "解析": "试题分析：李娜与王明角色不同，需要区分。",
    }
    assert exam_graph._has_name_semantic_risk(payload) is True


def test_align_name_consistency_rewrites_option_and_explanation_names():
    stem = "客户王明购买住房，以下说法正确的是（　）。"
    opts = ["李娜需补缴税费", "王明可办理过户", "张强无需纳税", "王明需签约"]
    exp = "试题分析：李娜与张强均不符合条件，只有王明符合。"
    new_opts, new_exp, changed = exam_graph._align_name_consistency(stem, opts, exp)
    assert changed is True
    assert all("李娜" not in x and "张强" not in x for x in new_opts)
    assert "李娜" not in new_exp and "张强" not in new_exp


def test_align_name_consistency_does_not_rewrite_brand_terms():
    """回归：贝壳文化题不应触发姓名一致性替换，避免“有尊贝壳务者”类文本污染。"""
    stem = "根据贝壳文化理念，贝壳的使命是什么（　）。"
    opts = [
        "有尊严的服务者，更美好的居住",
        "坚持做难而正确的事",
        "服务3亿家庭的品质居住平台",
        "客户至上、诚实可信、合作共赢、拼搏进取",
    ]
    exp = "试题分析：贝壳的使命是“有尊严的服务者，更美好的居住”。"
    new_opts, new_exp, changed = exam_graph._align_name_consistency(stem, opts, exp)
    assert changed is False
    assert new_opts == opts
    assert new_exp == exp
