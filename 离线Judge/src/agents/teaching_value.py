"""第3层并行维度：教学价值（独立Agent）。"""

from __future__ import annotations

from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.llm import ReliableLLMClient
from src.prompt_loader import load_prompt_pair
from src.schemas.evaluation import QuestionInput, TeachingValue

_PROMPT_PATH = "prompts/layer3_teaching_value.md"


def _related_slices_text(question: QuestionInput) -> str:
    slices = [str(x or "").strip() for x in (question.related_slices or []) if str(x or "").strip()]
    if not slices:
        return "无"
    return "\n".join([f"- {x}" for x in slices[:8]])


def teaching_value_agent(
    question: QuestionInput,
    llm: Any,
) -> tuple[TeachingValue, list[str], dict[str, Any]]:
    default_system = "你是教学价值审核员。"
    default_human = (
        "题型：{question_type}\n评估类型：{assessment_type}\n题干：{stem}\n选项：{options}\n"
        "标准答案：{correct_answer}\n解析：{explanation}\n教材切片：{textbook_slice}\n关联切片：{related_slices}"
    )
    system_prompt, human_prompt = load_prompt_pair(
        _PROMPT_PATH,
        default_system,
        default_human,
        [
            "question_type",
            "assessment_type",
            "stem",
            "options",
            "correct_answer",
            "explanation",
            "textbook_slice",
            "related_slices",
        ],
    )
    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", human_prompt)]
    )
    payload = prompt.invoke(
        {
            "question_type": question.question_type,
            "assessment_type": question.assessment_type,
            "stem": question.stem,
            "options": "\n".join(question.options),
            "correct_answer": question.correct_answer,
            "explanation": question.explanation,
            "textbook_slice": question.textbook_slice,
            "related_slices": _related_slices_text(question),
        }
    )
    client = ReliableLLMClient(llm, timeout_seconds=180, retries=2)
    data = client.invoke_json(
        payload,
        fallback={
            "cognitive_level": "应用"
            if question.assessment_type == "实战应用/推演"
            else "理解",
            "business_relevance": "高"
            if question.assessment_type == "实战应用/推演"
            else "一般",
            "discrimination": "中",
            "estimated_pass_rate": 0.62
            if question.assessment_type == "实战应用/推演"
            else 0.78,
            "has_assessment_value": True,
            "assessment_value_issues": [],
            "issues": [],
        },
    )

    tv = TeachingValue(
        cognitive_level=str(data.get("cognitive_level", "理解") or "理解"),
        business_relevance=str(data.get("business_relevance", "一般") or "一般"),
        discrimination=str(data.get("discrimination", "中") or "中"),
        estimated_pass_rate=float(data.get("estimated_pass_rate", 0.7) or 0.7),
    )
    issues = [str(x) for x in (data.get("issues") or [])]
    if data.get("has_assessment_value") is False:
        av_issues = [str(x) for x in (data.get("assessment_value_issues") or [])]
        if av_issues:
            issues.extend(av_issues)
        else:
            issues.append("题目可作答但考察意义不足（疑似无效送分题）")
        tv.discrimination = "低"
        tv.estimated_pass_rate = max(float(tv.estimated_pass_rate), 0.9)

    details = {
        "has_assessment_value": bool(data.get("has_assessment_value", True)),
        "assessment_value_issues": [str(x) for x in (data.get("assessment_value_issues") or [])],
    }
    return tv, issues, details
