from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Decision(str, Enum):
    PASS = "pass"
    REVIEW = "review"
    REJECT = "reject"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Scores(BaseModel):
    """0 = dimension did not run (short-circuit); 1-10 = score from that dimension."""
    logic: int = Field(ge=0, le=10)
    knowledge: int = Field(ge=0, le=10)
    distractor: int = Field(ge=0, le=10)
    teaching: int = Field(ge=0, le=10)
    risk: int = Field(ge=0, le=10)
    confidence: float = Field(ge=0.0, le=1.0)


class Evidence(BaseModel):
    slice_id: str = Field(default="unknown")
    quotes: list[str] = Field(default_factory=list)
    ask_judgement_evidence: str = ""
    substitution_evidence: list[str] = Field(default_factory=list)
    uniqueness_evidence: list[str] = Field(default_factory=list)


class HardGate(BaseModel):
    structure_legal: bool = True
    expression_standard: bool = True
    solvability_baseline: bool = True


class SemanticDrift(BaseModel):
    limit_words_consistent: bool = True
    rule_constraints_kept: bool = True
    fingerprint_matched: bool = True


class SolverValidation(BaseModel):
    predicted_answer: str = ""
    reasoning_path: str = ""
    ambiguity_flag: bool = False


class DistractorQuality(BaseModel):
    real_but_inapplicable: bool = True
    format_aligned: bool = True
    logic_homogenous: bool = True
    balance_strength: bool = True


class KnowledgeMatch(BaseModel):
    """知识匹配结果。skipped=True 表示知识门未执行，应展示为「未检测」。"""

    hit_target: bool = True
    no_out_of_bounds: bool = True
    no_cross_pollution: bool = True
    skipped: bool = Field(default=False, description="知识门未执行时为 True，展示时应输出「未检测」")


class TeachingValue(BaseModel):
    cognitive_level: str = "理解"
    business_relevance: str = "一般"
    discrimination: str = "中"
    estimated_pass_rate: float = Field(default=0.7, ge=0.0, le=1.0)


class RiskAssessment(BaseModel):
    policy_risk: bool = False
    legal_expression_risk: bool = False
    dispute_risk: bool = False
    practice_conflict: bool = False
    risk_level: RiskLevel = RiskLevel.LOW


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Observability(BaseModel):
    critic_loops: int = 0
    llm_calls: int = 0
    failed_calls: int = 0
    last_error: str = ""
    last_raw_response: str = ""
    last_raw_truncated: bool = False
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
    unstable_flag: bool = False


class Costs(BaseModel):
    per_question_usd: float = 0.0
    per_node_usd: dict[str, float] = Field(default_factory=dict)
    per_model_usd: dict[str, float] = Field(default_factory=dict)
    cost_alert: bool = False


class DimensionStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


class DimensionResult(BaseModel):
    status: DimensionStatus
    issues: list[str] = Field(default_factory=list)
    score_10: float | None = Field(default=None, ge=0.0, le=10.0, description="维度10分制得分；SKIP时为null")
    weight: float | None = Field(default=None, ge=0.0, le=1.0, description="维度权重")
    executed: bool = Field(default=False, description="该维度是否实际执行并参与计分")
    dedup_issue_count: int = Field(default=0, ge=0, description="去重后的问题数")
    reasons: list[str] = Field(default_factory=list, description="该维度扣分原因（业务可读）")
    details: dict[str, Any] = Field(default_factory=dict)


class JudgeReport(BaseModel):
    question_id: str
    assessment_type: str = "基础概念/理解记忆"
    trace_id: str = ""
    version: str = "v2.1"
    prompt_version: str = "p_critic_v3"

    decision: Decision
    hard_pass: bool
    scores: Scores
    overall_score: float = Field(default=0.0, ge=0.0, le=10.0)
    baseline_score: float | None = Field(default=None, ge=0.0, le=10.0, description="基线分（0-10，越高表示硬伤越少）")
    quality_score: float | None = Field(default=None, ge=0.0, le=10.0, description="正向质量得分（0-10，反映题目质量梯度）")
    quality_dimension_feedback: dict[str, str] = Field(
        default_factory=dict,
        description="质量评分模型输出的分维度评价（维度名 -> 简短评价）",
    )
    quality_reasons: list[str] = Field(
        default_factory=list,
        description="质量评分模型给出的独立原因（不与聚合 reasons 混用）",
    )
    quality_scoring_basis: str = Field(
        default="",
        description="质量评分模型一句话核心依据（scoring_basis）",
    )
    evidence: Evidence
    reasons: list[str] = Field(default_factory=list)

    hard_gate: HardGate
    semantic_drift: SemanticDrift
    solver_validation: SolverValidation
    distractor_quality: DistractorQuality
    knowledge_match: KnowledgeMatch
    teaching_value: TeachingValue
    risk_assessment: RiskAssessment
    observability: Observability
    costs: Costs
    dimension_results: dict[str, DimensionResult] = Field(
        default_factory=dict,
        description="第3层多维评估结果（含并行分支）",
    )

    actionable_feedback: str = ""


class QuestionInput(BaseModel):
    question_id: str
    stem: str
    options: list[str]
    correct_answer: str
    explanation: str
    textbook_slice: str
    related_slices: list[str] = Field(default_factory=list)
    reference_slices: list[str] = Field(default_factory=list)
    mother_question: str = ""
    examples: list[dict[str, Any]] = Field(default_factory=list)
    term_locks: list[str] = Field(default_factory=list)
    mastery: str = "未知"
    question_type: str = "single_choice"
    is_calculation: bool = False
    assessment_type: str = "基础概念/理解记忆"
    city_name: str = Field(
        default="",
        description="命题城市展示名（如上海），用于业务情景量级审核；空则不作城市量级核对",
    )
