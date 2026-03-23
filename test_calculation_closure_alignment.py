from exam_graph import validate_calculation_closure


def _mk_final(stem: str, a: str, answer: str = "A"):
    return {
        "题干": stem,
        "选项1": a,
        "选项2": "0",
        "选项3": "1",
        "选项4": "2",
        "正确答案": answer,
        "解析": "1、教材原文：x\n2、试题分析：y\n3、结论：本题答案为A。",
    }


def test_calc_closure_allows_unit_scale_alignment_yuan_to_wan():
    final_json = _mk_final(
        "评估值为520万元、网签价500万元时，总额度为（　）万元。",
        "325",
        "A",
    )
    issue = validate_calculation_closure(
        final_json,
        execution_result=3250000.0,
        code_status="success",
    )
    assert issue is None


def test_calc_closure_allows_rounding_to_integer_option():
    final_json = _mk_final(
        "请计算攀登指数（　）。",
        "838",
        "A",
    )
    issue = validate_calculation_closure(
        final_json,
        execution_result=837.6470588235,
        code_status="success",
    )
    assert issue is None


def test_calc_closure_rejects_target_drift():
    final_json = _mk_final(
        "评估值为520万元、网签价500万元时，组合贷款总额度为（　）万元。",
        "325",
        "A",
    )
    issue = validate_calculation_closure(
        final_json,
        execution_result=325.0,
        code_status="success",
        expected_calc_target="商业贷款部分额度",
    )
    assert issue is not None
    assert "calculation_target_mismatch" in (issue.get("fail_types") or [])


def test_calc_closure_rejects_multiselect_single_answer_contract():
    final_json = _mk_final(
        "某家庭需补交超标款金额为（　）元。",
        "155400",
        "B",
    )
    issue = validate_calculation_closure(
        final_json,
        question_type="多选题",
        execution_result=155400.0,
        code_status="success",
    )
    assert issue is not None
    assert "calculation_multiselect_answer_contract_fail" in (issue.get("fail_types") or [])


def test_calc_closure_rejects_missing_region_condition_for_tiered_price():
    final_json = _mk_final(
        "某家庭需补交超标款金额为（　）元。",
        "155400",
        "A",
    )
    final_json["解析"] = (
        "1、教材原文：...\n"
        "2、试题分析：浮动范围内按1560元/㎡，超出部分按4000元/㎡计算。\n"
        "3、结论：本题答案为A。"
    )
    issue = validate_calculation_closure(
        final_json,
        question_type="单选题",
        execution_result=155400.0,
        code_status="success",
    )
    assert issue is not None
    assert "calculation_region_condition_missing" in (issue.get("fail_types") or [])


def test_calc_closure_rejects_unit_lock_mismatch():
    final_json = _mk_final(
        "某组合贷款的商业贷款部分额度为（　）万元。",
        "105",
        "A",
    )
    issue = validate_calculation_closure(
        final_json,
        question_type="单选题",
        execution_result=105.0,
        code_status="success",
        expected_unit_hint="元",
    )
    assert issue is not None
    assert "calculation_unit_mismatch" in (issue.get("fail_types") or [])


def test_calc_closure_accepts_unit_lock_when_consistent():
    final_json = _mk_final(
        "某组合贷款的商业贷款部分额度为（　）万元。",
        "105",
        "A",
    )
    issue = validate_calculation_closure(
        final_json,
        question_type="单选题",
        execution_result=105.0,
        code_status="success",
        expected_unit_hint="万元",
    )
    assert issue is None
