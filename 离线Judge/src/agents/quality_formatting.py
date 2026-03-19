"""Node D: 测量学价值评审员 (Quality Agent)。"""

from __future__ import annotations

from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.llm import ReliableLLMClient
from src.prompt_loader import load_prompt_pair
from src.schemas.evaluation import (
    DistractorQuality,
    TeachingValue,
    QuestionInput,
    RiskAssessment,
    RiskLevel,
)


_PROMPT_PATH = "prompts/layer3_quality_formatting.md"


def _related_slices_text(question: QuestionInput) -> str:
    slices = [str(x or "").strip() for x in (question.related_slices or []) if str(x or "").strip()]
    if not slices:
        return "无"
    return "\n".join([f"- {x}" for x in slices[:8]])


def quality_formatting_agent(
    question: QuestionInput,
    llm: Any,
) -> tuple[
    DistractorQuality,
    TeachingValue,
    RiskAssessment,
    list[str],
    list[str],
    list[str],
]:
    default_system = "你是测量学质量评审员。"
    default_human = (
        "题干：{stem}\n选项：{options}\n标答：{correct_answer}\n解析：{explanation}\n"
        "教材切片：{textbook_slice}\n关联切片：{related_slices}"
    )
    system_prompt, human_prompt = load_prompt_pair(
        _PROMPT_PATH,
        default_system,
        default_human,
        ["stem", "options", "correct_answer", "explanation", "textbook_slice", "related_slices"],
    )
    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", human_prompt)]
    )
    payload = prompt.invoke(
        {
            "stem": question.stem,
            "options": "\n".join(question.options),
            "correct_answer": question.correct_answer,
            "explanation": question.explanation,
            "textbook_slice": question.textbook_slice,
            "related_slices": _related_slices_text(question),
        }
    )
    client = ReliableLLMClient(llm, timeout_seconds=180, retries=3)
    data = client.invoke_json(
        payload,
        fallback={
            "quality_evaluation": {"score": 0.5, "issues": []},
            "distractor_relevance": {
                "all_distractors_exist_or_related": True,
                "unrelated_options": [],
                "issues": [],
            },
            "explanation_quality": {
                "multi_option_coverage_rate": 1.0,
                "missing_options": [],
                "has_forbidden_media": False,
                "issues": [],
            },
            "pedagogical_value": {
                "cognitive_level": "理解",
                "estimated_pass_rate": 0.7,
                "teaching_issues": [],
                "explanation_quality_issues": [],
            },
            "risk_assessment": {
                "risk_level": "LOW",
                "risk_issues": [],
                "common_sense_issues": [],
                "distractor_basis_issues": [],
            },
        },
    )

    qe = data.get("quality_evaluation", {})
    dr = data.get("distractor_relevance", {})
    eq = data.get("explanation_quality", {})
    pv = data.get("pedagogical_value", {})
    ra = data.get("risk_assessment", {})

    issues = [str(x) for x in qe.get("issues", [])]
    dr_issues = [str(x) for x in dr.get("issues", [])]
    if not bool(dr.get("all_distractors_exist_or_related", True)):
        rel_opts = [str(x) for x in dr.get("unrelated_options", [])]
        if rel_opts:
            dr_issues.append(f"干扰项存在不相关选项：{','.join(rel_opts)}")
        else:
            dr_issues.append("干扰项存在不相关内容")
    issues.extend(dr_issues)
    score_1 = float(qe.get("score", 0.5) or 0.5)
    # 映射到旧逻辑 1~5
    raw_score = 5 if score_1 >= 1.0 else (3 if score_1 >= 0.5 else 1)

    distractor = DistractorQuality(
        real_but_inapplicable=raw_score >= 3 and len(issues) == 0,
        format_aligned=len(issues) == 0,
        logic_homogenous=len(issues) == 0,
        balance_strength=raw_score >= 3,
    )

    pedagogical = TeachingValue(
        cognitive_level=str(pv.get("cognitive_level", "理解") or "理解"),
        business_relevance="高" if raw_score >= 4 else "一般",
        discrimination="高" if raw_score >= 4 else ("中" if raw_score >= 3 else "低"),
        estimated_pass_rate=max(0.0, min(1.0, float(pv.get("estimated_pass_rate", 0.7)))),
    )

    common_sense_issues = [str(x) for x in ra.get("common_sense_issues", [])]
    raw_level = str(ra.get("risk_level", "low") or "low").strip().lower()
    if common_sense_issues and raw_level == "low":
        raw_level = "medium"
    risk_level = RiskLevel(raw_level) if raw_level in {"low", "medium", "high"} else RiskLevel.LOW
    distractor_basis_issues = [str(x) for x in ra.get("distractor_basis_issues", [])]
    all_risk_issues = [str(x) for x in ra.get("risk_issues", [])] + common_sense_issues + distractor_basis_issues

    risk = RiskAssessment(
        risk_level=risk_level,
        policy_risk=any("政策" in str(x) for x in all_risk_issues),
        legal_expression_risk=any("法律" in str(x) for x in all_risk_issues),
        dispute_risk=any("争议" in str(x) for x in all_risk_issues),
        practice_conflict=any("实务" in str(x) for x in all_risk_issues) or len(common_sense_issues) > 0,
    )

    teaching_issues = [str(x) for x in pv.get("teaching_issues", [])]
    explanation_quality_issues = [str(x) for x in pv.get("explanation_quality_issues", [])]
    # Filter out stem-vs-textbook stylistic complaints: PRD 明确允许题干与教材原文高度一致，
    # 只要没有在题干里直接泄露正确选项内容，就不应视为质量问题。
    # 因为这些提示词完全由大模型自由生成，这里用关键词黑名单在聚合层统一屏蔽。
    _stem_textbook_phrases = [
        "题干表述与教材原文",
        "题干与教材原文高度一致",
        "题干和教材原文高度一致",
        "题干与教材原文文本高度一致",
    ]
    explanation_quality_issues = [
        msg
        for msg in explanation_quality_issues
        if not any(p in msg for p in _stem_textbook_phrases)
    ]
    eq_issues = [str(x) for x in eq.get("issues", [])]
    try:
        cov = float(eq.get("multi_option_coverage_rate", 1.0))
    except Exception:
        cov = 1.0
    missing_opts = [str(x) for x in eq.get("missing_options", [])]
    if question.question_type == "multiple_choice":
        if cov < 1.0:
            eq_issues.append(f"多选解析逐项覆盖率不足：{cov:.2f}")
        if missing_opts:
            eq_issues.append(f"多选解析未覆盖选项：{','.join(missing_opts)}")
    if bool(eq.get("has_forbidden_media", False)):
        eq_issues.append("解析存在表格/图片表达（LLM兜底命中）")

    return (
        distractor,
        pedagogical,
        risk,
        issues,
        teaching_issues + explanation_quality_issues + all_risk_issues,
        eq_issues,
    )
