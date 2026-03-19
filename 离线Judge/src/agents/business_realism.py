"""
Node B: 业务场景评审员 (Realism Agent)
"""

from __future__ import annotations

from datetime import date
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.llm import ReliableLLMClient
from src.prompt_loader import load_prompt_pair
from src.schemas.evaluation import QuestionInput


def _pick_prompt_path(assessment_type: str) -> str:
    if assessment_type == "实战应用/推演":
        return "prompts/layer3_business_realism_practical.md"
    return "prompts/layer3_business_realism_concept.md"


def _related_slices_text(question: QuestionInput) -> str:
    slices = [str(x or "").strip() for x in (question.related_slices or []) if str(x or "").strip()]
    if not slices:
        return "无"
    return "\n".join([f"- {x}" for x in slices[:8]])


def business_realism_agent(
    question: QuestionInput,
    llm: Any,
    online_verifier: Any | None = None,
) -> dict:
    default_system = "你是业务真实性审核员。"
    default_human = "题型：{assessment_type}\n教材：{textbook_slice}\n关联切片：{related_slices}\n题目：{raw_question}"
    prompt_path = _pick_prompt_path(str(question.assessment_type or ""))
    system_prompt, human_prompt = load_prompt_pair(
        prompt_path,
        default_system,
        default_human,
        ["assessment_type", "textbook_slice", "related_slices", "raw_question"],
    )
    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", human_prompt)]
    )

    client = ReliableLLMClient(llm, timeout_seconds=180, retries=3)
    raw_question = "题干：\n" + question.stem + "\n\n选项：\n" + "\n".join(question.options)
    payload = prompt.invoke(
        {
            "assessment_type": question.assessment_type,
            "textbook_slice": question.textbook_slice,
            "related_slices": _related_slices_text(question),
            "raw_question": raw_question,
        }
    )
    data = client.invoke_json(
        payload,
        fallback={
            "passed": True,
            "issues": [],
            "score": 3,
            "slice_conflict_invalid": False,
            "slice_conflict_issues": [],
            "pending_online_claims": [],
            "online_verification_required": False,
            "online_verification_queries": [],
            "online_freshness_confirmed": False,
            "online_freshness_evidence": [],
            "online_verified_common_sense_invalid": False,
            "online_verified_issues": [],
            "scene_binding_required_violation": False,
            "workflow_sequence_violation": False,
        },
    )

    issues = [str(x) for x in data.get("issues", [])]
    slice_conflict_invalid = bool(data.get("slice_conflict_invalid", False))
    slice_conflict_issues = [str(x) for x in (data.get("slice_conflict_issues") or [])]
    pending_online_claims = [str(x) for x in (data.get("pending_online_claims") or [])]
    online_verification_required = bool(data.get("online_verification_required", False))
    online_verification_queries = [str(x) for x in (data.get("online_verification_queries") or [])]
    online_freshness_confirmed = bool(data.get("online_freshness_confirmed", False))
    online_freshness_evidence = [str(x) for x in (data.get("online_freshness_evidence") or [])]
    online_verified_common_sense_invalid = bool(data.get("online_verified_common_sense_invalid", False))
    online_verified_issues = [str(x) for x in (data.get("online_verified_issues") or [])]
    scene_binding_violation = bool(data.get("scene_binding_required_violation", False))
    workflow_sequence_violation = bool(data.get("workflow_sequence_violation", False))

    # Rule: conflicts with textbook slice are direct errors.
    if slice_conflict_invalid:
        if slice_conflict_issues:
            issues.extend(slice_conflict_issues)
        else:
            issues.append("题目结论与教材切片知识冲突")

    # Optional online verification hook for out-of-slice common sense checks.
    # online_verifier should return:
    # {"invalid": bool, "issues": list[str], "evidence": list[str]}
    online_verification_notes: list[str] = []
    if online_verification_required and not slice_conflict_invalid:
        if callable(online_verifier):
            try:
                verify_ret = online_verifier(
                    pending_online_claims=pending_online_claims,
                    queries=online_verification_queries,
                    question=question,
                    freshness_mode="latest_required",
                    as_of_date=date.today().isoformat(),
                ) or {}
                verified_invalid = bool(verify_ret.get("invalid", False))
                verified_issues = [str(x) for x in (verify_ret.get("issues") or [])]
                verified_evidence = [str(x) for x in (verify_ret.get("evidence") or [])]
                freshness_confirmed = bool(verify_ret.get("freshness_confirmed", True))
                freshness_evidence = [str(x) for x in (verify_ret.get("freshness_evidence") or [])]
                online_freshness_confirmed = freshness_confirmed
                if freshness_evidence:
                    online_freshness_evidence = freshness_evidence
                    online_verification_notes.extend([f"时效证据：{x}" for x in freshness_evidence])
                if not freshness_confirmed:
                    # Must not conclude final violation when freshness is not confirmed latest.
                    online_verified_common_sense_invalid = False
                    online_verified_issues = []
                    issues.append("切片外常识复核未确认最新信息时点，需重新联网复核")
                    verified_invalid = False
                if verified_invalid:
                    online_verified_common_sense_invalid = True
                    online_verified_issues = verified_issues or online_verified_issues
                    if online_verified_issues:
                        issues.extend(online_verified_issues)
                    else:
                        issues.append("联网复核后判定：存在常识违背")
                if verified_evidence:
                    online_verification_notes.extend([f"联网证据：{x}" for x in verified_evidence])
            except Exception as exc:
                online_verification_notes.append(f"联网复核执行异常：{exc}")
        else:
            online_verification_notes.append("待联网复核：未配置 online_verifier，暂不输出常识违背结论")

    # LLM already verified online and concluded invalid.
    if online_verified_common_sense_invalid and online_verified_issues:
        issues.extend([x for x in online_verified_issues if x not in issues])

    if scene_binding_violation:
        issues.append("场景绑定不足：实战题未建立必要业务场景关联")
    if workflow_sequence_violation:
        issues.append("交易流程顺序疑似错误：作业环节先后关系不合理")

    # 概念题不启用“场景绑定不足”判罚
    if question.assessment_type != "实战应用/推演":
        scene_binding_violation = False
        issues = [
            it for it in issues
            if not any(k in it for k in ["缺少场景", "脱离场景", "未结合业务场景", "业务割裂", "场景绑定不足"])
        ]

    passed = bool(data.get("passed", True)) and len(issues) == 0
    return {
        "passed": passed,
        "issues": issues,
        "score": int(data.get("score", 3) or 3),
        "slice_conflict_invalid": slice_conflict_invalid,
        "slice_conflict_issues": slice_conflict_issues,
        "pending_online_claims": pending_online_claims,
        "online_verification_required": online_verification_required,
        "online_verification_queries": online_verification_queries,
        "online_freshness_confirmed": online_freshness_confirmed,
        "online_freshness_evidence": online_freshness_evidence,
        "online_verified_common_sense_invalid": online_verified_common_sense_invalid,
        "online_verified_issues": online_verified_issues,
        "online_verification_notes": online_verification_notes,
        "scene_binding_required_violation": scene_binding_violation,
        "workflow_sequence_violation": workflow_sequence_violation,
    }
