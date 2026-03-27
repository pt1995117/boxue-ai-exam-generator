import exam_graph


validate_calculation_closure = exam_graph.validate_calculation_closure


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


def test_calc_closure_does_not_block_on_numeric_scale_gap():
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


def test_calc_closure_does_not_block_on_rounding_gap():
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


def test_calc_closure_does_not_block_on_execution_answer_numeric_mismatch():
    final_json = _mk_final(
        "以王强作为主贷人，根据公积金贷款政策，本次申请的最高贷款年限为（　）。",
        "22年",
        "A",
    )
    final_json["选项2"] = "24年"
    final_json["选项3"] = "25年"
    final_json["选项4"] = "30年"
    final_json["解析"] = (
        "1、教材原文：...\n"
        "2、试题分析：按年龄计算可贷年限为24年。\n"
        "3、结论：本题答案为B。"
    )
    final_json["正确答案"] = "A"
    issue = validate_calculation_closure(
        final_json,
        execution_result=24,
        code_status="success",
        calc_llm_need_calculation=True,
    )
    assert issue is None


def test_calc_closure_rejects_target_drift(monkeypatch):
    monkeypatch.setattr(
        exam_graph,
        "_is_calc_target_semantically_aligned",
        lambda **kwargs: (False, "target drift", None),
    )
    final_json = _mk_final(
        "评估值为520万元、网签价500万元时，组合贷款总额度为（　）万元。",
        "325",
        "A",
    )
    issue = validate_calculation_closure(
        final_json,
        execution_result=325.0,
        code_status="success",
        expected_calc_target="应缴税费金额",
    )
    assert issue is not None
    assert "calculation_target_mismatch" in (issue.get("fail_types") or [])


def test_calc_closure_no_longer_blocks_multiselect_single_answer_contract():
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
    assert issue is None


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


def test_calc_closure_skips_executable_for_combined_loan_term_min_rule():
    """
    组合贷年限取孰短：计算节点 LLM 判定 need_calculation=false 时不强制沙箱结果。
    """
    final_json = {
        "题干": (
            "在组合贷款中，若公积金贷款年限按公积金年限确定办法计算为25年，"
            "商业贷款年限按商业贷款银行年限确定办法计算为30年，则最终贷款年限应取（　）。"
        ),
        "选项1": "25",
        "选项2": "26",
        "选项3": "28",
        "选项4": "30",
        "正确答案": "A",
        "解析": "1、教材：组合贷款取较短年限。\n2、分析：min(25,30)=25。\n3、结论：选A。",
    }
    issue = validate_calculation_closure(
        final_json,
        question_type="单选题",
        execution_result=None,
        code_status="",
        calc_llm_need_calculation=False,
    )
    assert issue is None


def test_calc_closure_requires_execution_when_llm_says_need_calculation():
    """LLM 明确 need_calculation=true 时，必须有可验证的执行结果。"""
    final_json = _mk_final("评估值为520万元、网签价500万元时，总额度为（　）万元。", "325", "A")
    issue = validate_calculation_closure(
        final_json,
        execution_result=None,
        code_status="no_calculation",
        calc_llm_need_calculation=True,
        has_generated_code=False,
    )
    assert issue is not None
    assert "calculation_execution_missing" in (issue.get("fail_types") or [])
