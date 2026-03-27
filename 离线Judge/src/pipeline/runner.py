"""Pipeline 执行入口。"""

from __future__ import annotations

from typing import Any

from src.filters.deterministic_filter import DeterministicFilter
from src.llm import reset_observability
from src.pipeline.builder import create_judge_graph
from src.pipeline.state import build_initial_state
from src.schemas.evaluation import (
    Costs,
    Decision,
    DistractorQuality,
    Evidence,
    HardGate,
    JudgeReport,
    KnowledgeMatch,
    Observability,
    QuestionInput,
    RiskAssessment,
    RiskLevel,
    Scores,
    SemanticDrift,
    SolverValidation,
    TeachingValue,
)


def run_judge(
    question: QuestionInput,
    llm: Any,
    *,
    skip_phase1: bool = False,
) -> JudgeReport:
    reset_observability()
    graph = create_judge_graph()
    init_state = build_initial_state(question, llm)
    if not skip_phase1:
        filter_result = DeterministicFilter().run(question)
        init_state["hard_rule_errors"] = list(filter_result.errors or [])
        init_state["hard_rule_warnings"] = list(filter_result.warnings or [])
    out = graph.invoke(init_state)
    report = out.get("final_report")
    if isinstance(report, JudgeReport):
        return report
    return JudgeReport(
        question_id=question.question_id,
        assessment_type=question.assessment_type,
        trace_id=init_state["trace_id"],
        decision=Decision.REJECT,
        hard_pass=False,
        scores=Scores(logic=1, knowledge=1, distractor=1, teaching=1, risk=1, confidence=0.5),
        overall_score=0.0,
        evidence=Evidence(),
        reasons=["系统异常：未生成 final_report"],
        hard_gate=HardGate(structure_legal=False, expression_standard=False, solvability_baseline=False),
        semantic_drift=SemanticDrift(limit_words_consistent=False, rule_constraints_kept=False, fingerprint_matched=False),
        solver_validation=SolverValidation(predicted_answer="", reasoning_path="系统异常", ambiguity_flag=True),
        distractor_quality=DistractorQuality(
            real_but_inapplicable=False,
            format_aligned=False,
            logic_homogenous=False,
            balance_strength=False,
        ),
        knowledge_match=KnowledgeMatch(hit_target=False, no_out_of_bounds=False, no_cross_pollution=False),
        teaching_value=TeachingValue(cognitive_level="理解", business_relevance="一般", discrimination="低", estimated_pass_rate=0.5),
        risk_assessment=RiskAssessment(risk_level=RiskLevel.HIGH),
        observability=Observability(),
        costs=Costs(),
        actionable_feedback="请检查 pipeline graph 输出。",
    )
