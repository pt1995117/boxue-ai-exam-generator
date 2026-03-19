import json

from langchain_core.runnables import RunnableLambda

from src.evaluation.batch_runner import GoldenRecord, evaluate_golden
from src.pipeline.graph import _basic_rules_code_checks, run_judge
from src.schemas.evaluation import Decision, QuestionInput


def _mock_llm_response(input_value):
    text = str(input_value)
    if "solver_validation" in text or "Cognitive Auditor" in text or "逻辑审计专家" in text:
        return '{"solver_validation": {"passed": true, "predicted_answer": "A", "reasoning_path": "可唯一推出", "ambiguity_found": false}, "semantic_drift": {"passed": true, "evidence_quotes": ["证据句"], "drift_issues": []}}'
    if "知识匹配审核员" in text:
        return '{"out_of_scope": false, "constraint_drift": false, "single_knowledge_point_invalid": false, "issues": []}'
    if "distractor_quality" in text or "Value Assessor" in text:
        return '{"distractor_quality": {"score": 4, "homogeneity_issues": [], "weakness_issues": []}, "pedagogical_value": {"cognitive_level": "应用", "estimated_pass_rate": 0.62, "teaching_issues": []}, "risk_assessment": {"risk_level": "LOW", "risk_issues": []}}'
    if "generate_possible_answers(context)" in text or "只输出纯 Python 代码" in text:
        return (
            "def generate_possible_answers(context):\n"
            "    return [\n"
            "        {'type': 'correct', 'value': 100.0},\n"
            "        {'type': 'error_used_wrong_tax_rate_3_percent', 'value': 90.0},\n"
            "        {'type': 'error_forgot_to_deduct_vat', 'value': 80.0},\n"
            "    ]\n"
        )
    return '{"passed": true, "issues": []}'


def test_pipeline_returns_structured_report():
    item = QuestionInput(
        question_id="Q-2",
        question_type="single_choice",
        stem="张某在北京购买首套普通住宅，以下表述正确的是（ ）。",
        options=["A. 税费按首套标准计算", "B. 必须全款", "C. 禁止贷款", "D. 交易无须备案"],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="北京普通住宅交易可申请贷款并按政策执行。",
        is_calculation=False,
    )

    llm = RunnableLambda(_mock_llm_response)
    report = run_judge(item, llm)
    assert report.question_id == "Q-2"
    assert report.decision in {
        Decision.PASS,
        Decision.REVIEW,
        Decision.REJECT,
    }
    assert report.solver_validation.ambiguity_flag is False
    # Fusion node computes quality_score (0-100) from logic/distractor/knowledge/teaching weights
    assert report.quality_score is not None
    assert 0 <= report.quality_score <= 100


def test_pipeline_computes_golden_metrics():
    item = QuestionInput(
        question_id="G-1",
        question_type="single_choice",
        stem="张某在北京购买首套普通住宅，以下表述正确的是（ ）。",
        options=["A. 税费按首套标准计算", "B. 必须全款", "C. 禁止贷款", "D. 交易无须备案"],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="北京普通住宅交易可申请贷款并按政策执行。",
        is_calculation=False,
    )

    result = evaluate_golden(
        [GoldenRecord(item=item, expected_decision=Decision.PASS)],
        RunnableLambda(_mock_llm_response),
    )
    assert result["metrics"]["total"] == 1
    assert "accuracy" in result["metrics"]


def test_short_circuit_on_solver_marks_skip_and_caps_score():
    item = QuestionInput(
        question_id="Q-short-solver",
        question_type="single_choice",
        stem="以下表述正确的是（ ）。",
        options=["A. 甲", "B. 乙", "C. 丙", "D. 丁"],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="教材切片",
        is_calculation=False,
    )

    # llm=None 会在盲答节点直接触发 ambiguity 短路
    report = run_judge(item, llm=None)
    assert report.decision == Decision.REJECT
    assert report.overall_score <= 59.0
    assert report.dimension_results["知识匹配"].status == "SKIP"
    assert report.dimension_results["业务真实性"].status == "SKIP"
    assert report.dimension_results["教学价值"].status == "SKIP"


def test_short_circuit_on_knowledge_marks_skip_and_caps_score():
    item = QuestionInput(
        question_id="Q-short-knowledge",
        question_type="single_choice",
        stem="以下表述正确的是（ ）。",
        options=["A. 甲", "B. 乙", "C. 丙", "D. 丁"],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="教材切片",
        is_calculation=False,
    )

    def _mock_knowledge_reject(input_value):
        text = str(input_value)
        if "逻辑审计专家" in text:
            return '{"solver_evaluation":{"score":4,"predicted_answer":"A","reasoning_path":"可唯一推出","fatal_logic_issues":[]}}'
        if "知识匹配审核员" in text:
            return (
                '{"out_of_scope": true, "constraint_drift": false, "single_knowledge_point_invalid": false, "issues": ["超纲"],'
                '"out_of_scope_evidence": ["教材片段:税收条件A", "题面片段:出现条件B", "冲突说明:A与B冲突"],'
                '"constraint_drift_evidence": [], "single_knowledge_point_evidence": []}'
            )
        return '{"passed": true, "issues": []}'

    report = run_judge(item, RunnableLambda(_mock_knowledge_reject))
    assert report.decision == Decision.REJECT
    assert report.overall_score <= 59.0
    assert report.dimension_results["知识匹配"].status == "FAIL"
    assert report.dimension_results["业务真实性"].status == "SKIP"
    assert report.dimension_results["教学价值"].status == "SKIP"


def test_knowledge_gate_downgrade_to_review_when_evidence_insufficient():
    item = QuestionInput(
        question_id="Q-knowledge-review-evidence-insufficient",
        question_type="single_choice",
        stem="以下表述正确的是（ ）。",
        options=["甲", "乙", "丙", "丁"],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="教材切片",
        assessment_type="实战应用/推演",
        is_calculation=False,
    )

    def _mock_knowledge_soft(input_value):
        text = str(input_value)
        if "逻辑审计专家" in text:
            return '{"solver_evaluation":{"score":4,"predicted_answer":"A","reasoning_path":"可唯一推出","fatal_logic_issues":[]}}'
        if "知识匹配审核员" in text:
            return (
                '{"out_of_scope": true, "constraint_drift": false, "single_knowledge_point_invalid": false, "issues": ["超纲疑似"],'
                '"out_of_scope_evidence": ["教材片段:税收条件A"],'
                '"constraint_drift_evidence": [], "single_knowledge_point_evidence": []}'
            )
        if "题面综合质检专家" in text:
            return json.dumps(
                {
                    "business_realism": {
                        "passed": True,
                        "issues": [],
                        "score": 3,
                        "slice_conflict_invalid": False,
                        "slice_conflict_issues": [],
                        "scene_binding_required_violation": False,
                        "workflow_sequence_violation": False,
                        "high_risk_domain_triggered": False,
                        "high_risk_domains": [],
                        "subjective_replaces_objective": False,
                        "oral_replaces_written": False,
                        "over_authority_conclusion": False,
                        "bypass_compliance_process": False,
                        "uses_authoritative_evidence": True,
                        "introduces_professional_third_party": True,
                        "follows_compliance_sop": True,
                    },
                    "rigor": {
                        "leakage_still_invalid": False,
                        "explanation_conflict_still_invalid": False,
                        "name_consistency_still_invalid": False,
                        "legal_math_closure_invalid": False,
                        "term_mismatch_issues": [],
                        "issues": [],
                    },
                    "distractor": {
                        "distractor_quality": {
                            "real_but_inapplicable": True,
                            "format_aligned": True,
                            "logic_homogenous": True,
                            "balance_strength": True,
                        },
                        "unsupported_options": [],
                        "why_unrelated": [],
                        "overlap_pairs": [],
                        "stem_option_conflicts": [],
                        "mutual_exclusivity_fail": False,
                        "issues": [],
                    },
                },
                ensure_ascii=False,
            )
        if "教学复盘评估专家" in text:
            return json.dumps(
                {
                    "explanation_quality": {
                        "multi_option_coverage_rate": 1.0,
                        "missing_options": [],
                        "analysis_rewrite_sufficient": True,
                        "analysis_rewrite_issues": [],
                        "three_part_is_clear_and_coherent": True,
                        "three_part_semantic_invalid": False,
                        "three_part_semantic_evidence": [],
                        "first_part_missing_target_title": False,
                        "first_part_missing_level": False,
                        "first_part_missing_textbook_raw": False,
                        "theory_support_present": True,
                        "theory_support_source": "章节A",
                        "business_support_present": True,
                        "business_support_reason": "理由A",
                        "first_part_structured_issues": [],
                        "issues": [],
                    },
                    "teaching_value": {
                        "cognitive_level": "应用",
                        "business_relevance": "高",
                        "discrimination": "中",
                        "estimated_pass_rate": 0.62,
                        "has_assessment_value": True,
                        "main_assessment_points": ["知识点A"],
                        "assessment_point_aligned": True,
                        "assessment_point_issues": [],
                        "assessment_value_issues": [],
                        "issues": [],
                    },
                },
                ensure_ascii=False,
            )
        return '{"passed": true, "issues": []}'

    report = run_judge(item, RunnableLambda(_mock_knowledge_soft))
    assert report.decision == Decision.REVIEW
    assert report.dimension_results["业务真实性"].status != "SKIP"
    assert report.dimension_results["知识匹配"].status == "FAIL"
    assert any("知识边界复核" in reason for reason in report.reasons)


def test_dimension_results_use_strong_status_enum():
    item = QuestionInput(
        question_id="Q-dim-typed",
        question_type="single_choice",
        stem="以下表述正确的是（ ）。",
        options=["A. 甲", "B. 乙", "C. 丙", "D. 丁"],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="教材切片",
        is_calculation=False,
    )

    report = run_judge(item, llm=None)
    valid_status = {"PASS", "FAIL", "SKIP"}
    for dim in report.dimension_results.values():
        assert dim.status in valid_status


def _base_question(question_id: str) -> QuestionInput:
    return QuestionInput(
        question_id=question_id,
        question_type="single_choice",
        stem="客户担心这套老房子后期改造麻烦，作为经纪人你最合适的回应是（ ）。",
        options=[
            "A. 先了解您最担心的是预算、工期还是结构限制，再给您两个可落地方案",
            "B. 这个确实不好改，基本没办法",
            "C. 房子都这样，习惯就好",
            "D. 不用担心，肯定没问题",
        ],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="客户异议处理要先共情，再澄清需求，再给出可执行方案。",
        assessment_type="实战应用/推演",
        is_calculation=False,
    )


def _mock_llm_quality_case(
    *,
    scenario_dialogue_or_objection: bool,
    negative_emotion_detected: bool,
    contains_business_action: bool,
    amplifies_defect_without_remedy: bool,
    theory_support_present: bool,
    business_support_present: bool,
    high_risk_domain_triggered: bool = False,
    subjective_replaces_objective: bool = False,
    oral_replaces_written: bool = False,
    over_authority_conclusion: bool = False,
    bypass_compliance_process: bool = False,
    uses_authoritative_evidence: bool = True,
    introduces_professional_third_party: bool = True,
    follows_compliance_sop: bool = True,
    competing_truth_violation: bool = False,
    non_discriminative_stem_risk: bool = False,
):
    def _fn(input_value):
        text = str(input_value)
        if "逻辑审计专家" in text:
            return '{"solver_evaluation":{"score":4,"predicted_answer":"A","reasoning_path":"可唯一推出","fatal_logic_issues":[]}}'
        if "知识匹配审核员" in text:
            return '{"out_of_scope": false, "constraint_drift": false, "single_knowledge_point_invalid": false, "issues": []}'
        if "题面综合质检专家" in text:
            return json.dumps(
                {
                    "business_realism": {
                        "passed": True,
                        "issues": [],
                        "score": 3,
                        "slice_conflict_invalid": False,
                        "slice_conflict_issues": [],
                        "scene_binding_required_violation": False,
                        "workflow_sequence_violation": False,
                        "scenario_dialogue_or_objection": scenario_dialogue_or_objection,
                        "negative_emotion_detected": negative_emotion_detected,
                        "contains_business_action": contains_business_action,
                        "business_action_types": ["探寻需求", "提供解决方案"],
                        "backbook_style_answer": False,
                        "amplifies_defect_without_remedy": amplifies_defect_without_remedy,
                        "high_risk_domain_triggered": high_risk_domain_triggered,
                        "high_risk_domains": ["structure_safety"] if high_risk_domain_triggered else [],
                        "subjective_replaces_objective": subjective_replaces_objective,
                        "oral_replaces_written": oral_replaces_written,
                        "over_authority_conclusion": over_authority_conclusion,
                        "bypass_compliance_process": bypass_compliance_process,
                        "uses_authoritative_evidence": uses_authoritative_evidence,
                        "introduces_professional_third_party": introduces_professional_third_party,
                        "follows_compliance_sop": follows_compliance_sop,
                        "competing_truth_violation": competing_truth_violation,
                        "competing_truth_issues": ["错误选项比正确选项更可执行"] if competing_truth_violation else [],
                        "non_discriminative_stem_risk": non_discriminative_stem_risk,
                        "non_discriminative_stem_issues": ["题干仅要求综合考虑，缺少可判别边界"] if non_discriminative_stem_risk else [],
                    },
                    "rigor": {
                        "leakage_still_invalid": False,
                        "explanation_conflict_still_invalid": False,
                        "name_consistency_still_invalid": False,
                        "legal_math_closure_invalid": False,
                        "term_mismatch_issues": [],
                        "issues": [],
                    },
                    "distractor": {
                        "distractor_quality": {
                            "real_but_inapplicable": True,
                            "format_aligned": True,
                            "logic_homogenous": True,
                            "balance_strength": True,
                        },
                        "unsupported_options": [],
                        "why_unrelated": [],
                        "overlap_pairs": [],
                        "stem_option_conflicts": [],
                        "mutual_exclusivity_fail": False,
                        "issues": [],
                    },
                },
                ensure_ascii=False,
            )
        if "教学复盘评估专家" in text:
            return json.dumps(
                {
                    "explanation_quality": {
                        "multi_option_coverage_rate": 1.0,
                        "missing_options": [],
                        "analysis_rewrite_sufficient": True,
                        "analysis_rewrite_issues": [],
                        "three_part_is_clear_and_coherent": True,
                        "three_part_semantic_invalid": False,
                        "three_part_semantic_evidence": [],
                        "first_part_missing_target_title": False,
                        "first_part_missing_level": False,
                        "first_part_missing_textbook_raw": False,
                        "theory_support_present": theory_support_present,
                        "theory_support_source": "客户异议处理章节",
                        "business_support_present": business_support_present,
                        "business_support_reason": "先共情再给方案可降低客户抵触",
                        "first_part_structured_issues": [],
                        "issues": [],
                    },
                    "teaching_value": {
                        "cognitive_level": "应用",
                        "business_relevance": "高",
                        "discrimination": "中",
                        "estimated_pass_rate": 0.62,
                        "has_assessment_value": True,
                        "main_assessment_points": ["异议处理"],
                        "assessment_point_aligned": True,
                        "assessment_point_issues": [],
                        "assessment_value_issues": [],
                        "issues": [],
                    },
                },
                ensure_ascii=False,
            )
        return '{"passed": true, "issues": []}'

    return _fn


def test_realism_requires_business_action_in_scenario_dialogue():
    item = _base_question("Q-realism-action-required")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=False,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
        )
    )
    report = run_judge(item, llm)
    assert report.dimension_results["业务真实性"].status == "FAIL"
    assert report.dimension_results["业务真实性"].details["contains_business_action"] is False


def test_doctrinaire_fatal_gate_rejects_when_no_remedy():
    item = _base_question("Q-doctrinaire-fatal")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=True,
            contains_business_action=False,
            amplifies_defect_without_remedy=True,
            theory_support_present=True,
            business_support_present=True,
        )
    )
    report = run_judge(item, llm)
    assert report.decision == Decision.REVIEW
    assert any("教条主义拦截" in reason for reason in report.reasons)


def test_doctrinaire_gate_not_triggered_when_has_remedy():
    item = _base_question("Q-doctrinaire-has-remedy")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=True,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
        )
    )
    report = run_judge(item, llm)
    assert all("教条主义拦截" not in reason for reason in report.reasons)
    assert report.decision != Decision.REJECT


def test_explanation_requires_theory_and_business_support():
    item1 = _base_question("Q-explain-no-theory")
    llm1 = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=False,
            business_support_present=True,
        )
    )
    report1 = run_judge(item1, llm1)
    assert report1.dimension_results["解析质量"].status == "FAIL"
    assert report1.decision == Decision.REVIEW

    item2 = _base_question("Q-explain-no-business")
    llm2 = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=False,
        )
    )
    report2 = run_judge(item2, llm2)
    assert report2.dimension_results["解析质量"].status == "FAIL"
    assert report2.decision == Decision.REVIEW


def test_compliance_risk_fatal_gate_blocks_subjective_judgment():
    item = _base_question("Q-compliance-subjective")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
            high_risk_domain_triggered=True,
            subjective_replaces_objective=True,
        )
    )
    report = run_judge(item, llm)
    assert report.decision == Decision.REVIEW
    assert any("合规风控拦截" in reason for reason in report.reasons)


def test_compliance_risk_fatal_gate_blocks_oral_promise():
    item = _base_question("Q-compliance-oral")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
            high_risk_domain_triggered=True,
            oral_replaces_written=True,
        )
    )
    report = run_judge(item, llm)
    assert report.decision == Decision.REVIEW


def test_compliance_risk_fatal_gate_blocks_over_authority():
    item = _base_question("Q-compliance-over-authority")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
            high_risk_domain_triggered=True,
            over_authority_conclusion=True,
        )
    )
    report = run_judge(item, llm)
    assert report.decision == Decision.REVIEW


def test_compliance_risk_review_when_missing_required_controls():
    item = _base_question("Q-compliance-missing-controls")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
            high_risk_domain_triggered=True,
            uses_authoritative_evidence=False,
            introduces_professional_third_party=False,
            follows_compliance_sop=False,
        )
    )
    report = run_judge(item, llm)
    assert report.decision == Decision.REVIEW
    assert any("缺少凭证核验/专业第三方/流程留痕" in reason for reason in report.reasons)


def test_compliance_risk_pass_when_controls_complete():
    item = _base_question("Q-compliance-pass")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
            high_risk_domain_triggered=True,
            uses_authoritative_evidence=True,
            introduces_professional_third_party=True,
            follows_compliance_sop=True,
        )
    )
    report = run_judge(item, llm)
    assert report.decision != Decision.REJECT
    assert all("合规风控拦截" not in reason for reason in report.reasons)


def test_competing_truth_violation_triggers_review():
    item = _base_question("Q-competing-truth-review")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
            competing_truth_violation=True,
        )
    )
    report = run_judge(item, llm)
    assert report.decision == Decision.REVIEW
    assert any("真理对抗风险" in reason for reason in report.reasons)


def test_non_discriminative_stem_triggers_fatal_reject():
    item = _base_question("Q-competing-truth-fatal")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
            non_discriminative_stem_risk=True,
        )
    )
    report = run_judge(item, llm)
    assert report.decision == Decision.REVIEW
    assert any("真理对抗拦截" in reason for reason in report.reasons)


def test_competing_truth_gate_not_triggered_when_clean():
    item = _base_question("Q-competing-truth-clean")
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
            competing_truth_violation=False,
            non_discriminative_stem_risk=False,
        )
    )
    report = run_judge(item, llm)
    assert all("真理对抗拦截" not in reason for reason in report.reasons)


def test_basic_rules_demote_template_and_numeric_order_to_warnings():
    item = QuestionInput(
        question_id="Q-basic-warning-demote",
        question_type="single_choice",
        stem="请选择最合适的说法（ ）。",
        options=["100", "90", "80", "70"],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="教材切片",
        is_calculation=False,
    )
    errors, warnings = _basic_rules_code_checks(item)
    assert "数值选项建议按从小到大升序排列" in warnings
    assert all("数值选项必须按从小到大升序排列" not in x for x in errors)


def test_year_constraint_becomes_review_signal():
    item = _base_question("Q-year-review-signal")
    item.stem = "某客户在2020年购房，以下表述正确的是（ ）。"
    item.textbook_slice = "客户异议处理流程强调先共情后方案。"
    llm = RunnableLambda(
        _mock_llm_quality_case(
            scenario_dialogue_or_objection=True,
            negative_emotion_detected=False,
            contains_business_action=True,
            amplifies_defect_without_remedy=False,
            theory_support_present=True,
            business_support_present=True,
        )
    )
    report = run_judge(item, llm)
    assert report.decision == Decision.REVIEW
    assert any("年份约束复核" in reason for reason in report.reasons)


def test_code_evidence_hard_triggers_reject():
    """TDD-036: calculation question with code_evidence_status=HARD -> reject and reasons contain evidence."""
    item = QuestionInput(
        question_id="Q-calc-hard",
        question_type="single_choice",
        stem="某房产总价100万，首付30%，则首付款为（ ）万元。",
        options=["A. 30", "B. 70", "C. 100", "D. 33"],
        correct_answer="A",
        explanation="1.教材\n2.分析\n3.结论\n本题答案为A",
        textbook_slice="首付比例按合同约定。",
        is_calculation=True,
    )

    def _mock_calc_hard(input_value):
        text = str(input_value)
        if "solver_validation" in text or "Cognitive Auditor" in text or "逻辑审计专家" in text:
            return '{"solver_validation": {"passed": true, "predicted_answer": "A", "reasoning_path": "可唯一推出", "ambiguity_found": false}, "semantic_drift": {"passed": true}}'
        if "知识匹配审核员" in text:
            return '{"out_of_scope": false, "constraint_drift": false, "single_knowledge_point_invalid": false, "issues": []}'
        if ("code_evaluator" in text and "complexity" in text) or ("房地产计算题评估专家" in text):
            return json.dumps({
                "code_snippet": "def generate_possible_answers(c): return []",
                "code_evaluator": {
                    "issues": ["正确计算值与正确选项不一致: code=28, option=30"],
                    "evidence": ["ev1"],
                    "wrong_path_count": 0,
                    "mapped_to_options": True,
                },
                "complexity": {
                    "is_calculation_question": True,
                    "digit_complexity_too_high": False,
                    "step_count_too_high": False,
                    "complex_decimal_present": False,
                    "mental_math_level": "需草算",
                    "complexity_level": "中",
                    "issues": [],
                    "evidence": [],
                },
            })
        if "distractor_quality" in text or "Value Assessor" in text:
            return '{"distractor_quality": {"score": 4, "homogeneity_issues": [], "weakness_issues": []}, "pedagogical_value": {"cognitive_level": "应用", "estimated_pass_rate": 0.62, "teaching_issues": []}, "risk_assessment": {"risk_level": "LOW", "risk_issues": []}}'
        return '{"passed": true, "issues": []}'

    report = run_judge(item, RunnableLambda(_mock_calc_hard))
    assert report.decision == Decision.REJECT
    calc_dr = report.dimension_results["计算可执行性与复杂度"]
    assert calc_dr.details.get("code_evidence_status") == "HARD"
    assert calc_dr.score_10 == 0


def test_code_evidence_soft_triggers_review():
    """TDD-037: calculation question with code_evidence_status=SOFT -> review, not reject."""
    item = QuestionInput(
        question_id="Q-calc-soft",
        question_type="single_choice",
        stem="某房产总价100万，首付30%，则首付款为（ ）万元。",
        options=["A. 30", "B. 70", "C. 100", "D. 33"],
        correct_answer="A",
        explanation="1.教材\n2.分析\n3.结论\n本题答案为A",
        textbook_slice="首付比例按合同约定。",
        is_calculation=True,
    )

    def _mock_calc_soft(input_value):
        text = str(input_value)
        if "solver_validation" in text or "Cognitive Auditor" in text or "逻辑审计专家" in text:
            return '{"solver_validation": {"passed": true, "predicted_answer": "A", "reasoning_path": "可唯一推出", "ambiguity_found": false}, "semantic_drift": {"passed": true}}'
        if "知识匹配审核员" in text:
            return '{"out_of_scope": false, "constraint_drift": false, "single_knowledge_point_invalid": false, "issues": []}'
        if ("code_evaluator" in text and "complexity" in text) or ("房地产计算题评估专家" in text):
            return json.dumps({
                "code_snippet": "def generate_possible_answers(c): return []",
                "code_evaluator": {
                    "issues": ["错误路径数量不足：至少应包含1个正确结果+2个错误结果"],
                    "evidence": [],
                    "wrong_path_count": 1,
                    "mapped_to_options": True,
                },
                "complexity": {
                    "is_calculation_question": True,
                    "digit_complexity_too_high": False,
                    "step_count_too_high": False,
                    "complex_decimal_present": False,
                    "mental_math_level": "需草算",
                    "complexity_level": "中",
                    "issues": [],
                    "evidence": [],
                },
            })
        if "distractor_quality" in text or "Value Assessor" in text:
            return '{"distractor_quality": {"score": 4, "homogeneity_issues": [], "weakness_issues": []}, "pedagogical_value": {"cognitive_level": "应用", "estimated_pass_rate": 0.62, "teaching_issues": []}, "risk_assessment": {"risk_level": "LOW", "risk_issues": []}}'
        return '{"passed": true, "issues": []}'

    report = run_judge(item, RunnableLambda(_mock_calc_soft))
    assert report.decision != Decision.REJECT
    assert report.decision in (Decision.REVIEW, Decision.PASS)
    calc_dr = report.dimension_results["计算可执行性与复杂度"]
    assert calc_dr.details.get("code_evidence_status") == "SOFT"
    assert calc_dr.score_10 is not None and calc_dr.score_10 < 10
