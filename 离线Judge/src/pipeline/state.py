"""Pipeline 状态定义与初始化。"""

from __future__ import annotations

import uuid
from typing import Any, TypedDict

from src.schemas.evaluation import (
    DimensionResult,
    DistractorQuality,
    JudgeReport,
    QuestionInput,
    RiskAssessment,
    SemanticDrift,
    SolverValidation,
    TeachingValue,
)


class JudgeState(TypedDict):
    question: QuestionInput
    llm: Any
    trace_id: str
    hard_rule_errors: list[str]
    hard_rule_warnings: list[str]
    hard_rule_has_errors: bool
    gate_recheck_data: dict[str, Any]
    solver_validation: SolverValidation | None
    solver_semantic_drift: SemanticDrift | None
    solver_issues: list[str]
    drift_issues: list[str]
    solver_calc_data: dict[str, Any]
    knowledge_semantic_drift: SemanticDrift | None
    realism_score: float
    realism_issues: list[str]
    realism_data: dict[str, Any]
    risk_assessment: RiskAssessment | None
    rigor_data: dict[str, Any]
    rigor_issues: list[str]
    rigor_warnings: list[str]
    knowledge_issues: list[str]
    distractor_quality: DistractorQuality | None
    teaching_value: TeachingValue | None
    teaching_issues: list[str]
    teaching_data: dict[str, Any]
    distractor_issues: list[str]
    distractor_data: dict[str, Any]
    explanation_issues: list[str]
    explanation_data: dict[str, Any]
    knowledge_data: dict[str, Any]
    knowledge_gate_reject: bool
    knowledge_gate_reasons: list[str]
    calculation_issues: list[str]
    calculation_data: dict[str, Any]
    dimension_results: dict[str, DimensionResult]
    final_report: JudgeReport | None
    ran_blind_solver: bool
    ran_knowledge_gate: bool
    ran_surface_a: bool
    ran_teaching_b: bool
    ran_calc_branch: bool


def build_initial_state(question: QuestionInput, llm: Any) -> JudgeState:
    return {
        "question": question,
        "llm": llm,
        "trace_id": str(uuid.uuid4()),
        "hard_rule_errors": [],
        "hard_rule_warnings": [],
        "hard_rule_has_errors": False,
        "gate_recheck_data": {},
        "solver_validation": None,
        "solver_semantic_drift": None,
        "solver_issues": [],
        "drift_issues": [],
        "solver_calc_data": {},
        "knowledge_semantic_drift": None,
        "realism_score": 3.0,
        "realism_issues": [],
        "risk_assessment": None,
        "rigor_data": {},
        "rigor_issues": [],
        "rigor_warnings": [],
        "knowledge_issues": [],
        "distractor_quality": None,
        "teaching_value": None,
        "teaching_issues": [],
        "teaching_data": {},
        "distractor_issues": [],
        "distractor_data": {},
        "explanation_issues": [],
        "explanation_data": {},
        "knowledge_data": {},
        "knowledge_gate_reject": False,
        "knowledge_gate_reasons": [],
        "calculation_issues": [],
        "calculation_data": {},
        "dimension_results": {},
        "final_report": None,
        "ran_blind_solver": False,
        "ran_knowledge_gate": False,
        "ran_surface_a": False,
        "ran_teaching_b": False,
        "ran_calc_branch": False,
    }
