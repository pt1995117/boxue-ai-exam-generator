"""第2层：知识边界守门节点（独立Agent）。"""

from __future__ import annotations

from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.llm import ReliableLLMClient
from src.prompt_loader import load_prompt_pair
from src.schemas.evaluation import QuestionInput

_PROMPT_PATH = "prompts/layer2_knowledge_gate.md"


def _related_slices_text(question: QuestionInput) -> str:
    slices = [str(x or "").strip() for x in (question.related_slices or []) if str(x or "").strip()]
    if not slices:
        return "无"
    return "\n".join([f"- {x}" for x in slices[:8]])


def _reference_slices_text(question: QuestionInput) -> str:
    slices = [str(x or "").strip() for x in (question.reference_slices or []) if str(x or "").strip()]
    if not slices:
        return "无"
    return "\n".join([f"- {x}" for x in slices[:8]])


def _normalize_evidence_list(values: Any) -> list[str]:
    out: list[str] = []
    for x in (values or []):
        txt = str(x or "").strip()
        if txt and txt not in out:
            out.append(txt)
    return out


def _is_strong_short_circuit_evidence(evidence: list[str]) -> bool:
    # Require at least three non-empty evidence items for short-circuit.
    return len(evidence) >= 3


def layer2_knowledge_gate_agent(
    question: QuestionInput,
    llm: Any,
) -> tuple[list[str], dict[str, Any]]:
    default_system = "你是知识匹配审核员。"
    default_human = (
        "题型：{question_type}\n评估类型：{assessment_type}\n教材切片：{textbook_slice}\n关联切片：{related_slices}\n"
        "参考切片：{reference_slices}\n题干：{stem}\n选项：{options}\n标准答案：{correct_answer}\n解析：{explanation}"
    )
    system_prompt, human_prompt = load_prompt_pair(
        _PROMPT_PATH,
        default_system,
        default_human,
        [
            "question_type",
            "assessment_type",
            "textbook_slice",
            "related_slices",
            "reference_slices",
            "stem",
            "options",
            "correct_answer",
            "explanation",
        ],
    )
    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", human_prompt)]
    )
    payload = prompt.invoke(
        {
            "textbook_slice": question.textbook_slice,
            "related_slices": _related_slices_text(question),
            "reference_slices": _reference_slices_text(question),
            "question_type": question.question_type,
            "assessment_type": question.assessment_type,
            "stem": question.stem,
            "options": "\n".join(question.options),
            "correct_answer": question.correct_answer,
            "explanation": question.explanation,
        }
    )
    client = ReliableLLMClient(llm, timeout_seconds=180, retries=2)
    data = client.invoke_json(
        payload,
        fallback={
            "out_of_scope": False,
            "constraint_drift": False,
            "single_knowledge_point_invalid": False,
            "issues": [],
            "out_of_scope_evidence": [],
            "constraint_drift_evidence": [],
            "single_knowledge_point_evidence": [],
            "recommendation": {
                "recommended_question_types": [],
                "recommended_assessment_type": "均可",
                "recommended_focus": [],
                "recommended_difficulty": "中",
                "recommendation_rationale": "",
                "recommendation_confidence": 0.0,
            },
        },
    )

    out_of_scope_flag = bool(data.get("out_of_scope", False))
    constraint_drift_flag = bool(data.get("constraint_drift", False))
    out_of_scope_evidence = _normalize_evidence_list(data.get("out_of_scope_evidence"))
    constraint_drift_evidence = _normalize_evidence_list(data.get("constraint_drift_evidence"))
    single_knowledge_point_evidence = _normalize_evidence_list(data.get("single_knowledge_point_evidence"))

    out_of_scope_hard = out_of_scope_flag and _is_strong_short_circuit_evidence(out_of_scope_evidence)
    constraint_drift_hard = constraint_drift_flag and _is_strong_short_circuit_evidence(constraint_drift_evidence)

    issues = [str(x) for x in (data.get("issues") or [])]
    if out_of_scope_flag:
        issues.append("存在超纲风险：题目内容未能稳定命中教材切片（知识匹配LLM判定）")
        if not out_of_scope_hard:
            issues.append("超纲风险证据不足（<3条），降级为复核，不触发短路")
    if constraint_drift_flag:
        issues.append("教材限定词/边界词疑似被篡改（知识匹配LLM判定）")
        if not constraint_drift_hard:
            issues.append("边界词漂移证据不足（<3条），降级为复核，不触发短路")
    if question.question_type == "true_false" and bool(data.get("single_knowledge_point_invalid", False)):
        issues.append("判断题题干疑似涉及多个知识点，不符合单知识点考核原则（知识匹配LLM判定）")

    recommendation = data.get("recommendation") if isinstance(data.get("recommendation"), dict) else {}
    rec_types = [
        str(x).strip()
        for x in (recommendation.get("recommended_question_types") or [])
        if str(x).strip()
    ]
    rec_assessment_type = str(recommendation.get("recommended_assessment_type", "均可") or "均可")
    rec_focus = [
        str(x).strip()
        for x in (recommendation.get("recommended_focus") or [])
        if str(x).strip()
    ]
    rec_difficulty = str(recommendation.get("recommended_difficulty", "中") or "中")
    rec_rationale = str(recommendation.get("recommendation_rationale", "") or "")
    try:
        rec_confidence = float(recommendation.get("recommendation_confidence", 0.0) or 0.0)
    except Exception:
        rec_confidence = 0.0
    rec_confidence = max(0.0, min(1.0, rec_confidence))

    recommendation_suggestions: list[str] = []
    if rec_types and question.question_type not in rec_types and rec_confidence >= 0.75:
        recommendation_suggestions.append(
            f"教材切片更推荐题型为 {','.join(rec_types)}，当前为 {question.question_type}（推荐置信度={rec_confidence:.2f}）"
        )

    short_circuit_evidence_chain: list[str] = []
    if out_of_scope_hard:
        short_circuit_evidence_chain.extend([f"【超纲证据】{x}" for x in out_of_scope_evidence[:3]])
    if constraint_drift_hard:
        short_circuit_evidence_chain.extend([f"【边界漂移证据】{x}" for x in constraint_drift_evidence[:3]])

    details = {
        "out_of_scope": out_of_scope_flag,
        "constraint_drift": constraint_drift_flag,
        "out_of_scope_hard": out_of_scope_hard,
        "constraint_drift_hard": constraint_drift_hard,
        "short_circuit_reject_ready": bool(out_of_scope_hard or constraint_drift_hard),
        "single_knowledge_point_invalid": bool(data.get("single_knowledge_point_invalid", False)),
        "out_of_scope_evidence": out_of_scope_evidence,
        "constraint_drift_evidence": constraint_drift_evidence,
        "single_knowledge_point_evidence": single_knowledge_point_evidence,
        "short_circuit_evidence_chain": short_circuit_evidence_chain,
        "recommendation": {
            "recommended_question_types": rec_types,
            "recommended_assessment_type": rec_assessment_type,
            "recommended_focus": rec_focus,
            "recommended_difficulty": rec_difficulty,
            "recommendation_rationale": rec_rationale,
            "recommendation_confidence": rec_confidence,
        },
        "recommendation_suggestions": recommendation_suggestions,
    }
    return issues, details


knowledge_match_agent = layer2_knowledge_gate_agent
