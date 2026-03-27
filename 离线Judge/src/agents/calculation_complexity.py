"""第3层并行维度：计算可执行性与复杂度（独立Agent，仅计算题）。"""

from __future__ import annotations

from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.llm import ReliableLLMClient
from src.prompt_loader import load_prompt_pair
from src.schemas.evaluation import QuestionInput

_PROMPT_PATH = "prompts/layer3_calculation_complexity.md"


def _related_slices_text(question: QuestionInput) -> str:
    slices = [str(x or "").strip() for x in (question.related_slices or []) if str(x or "").strip()]
    if not slices:
        return "无"
    return "\n".join([f"- {x}" for x in slices[:8]])


def calculation_complexity_agent(
    question: QuestionInput,
    llm: Any,
) -> tuple[list[str], dict[str, Any]]:
    if not question.is_calculation:
        return [], {"enabled": False}

    default_system = "你是计算题复杂度审核员。"
    default_human = "题干：{stem}\n选项：{options}\n教材切片：{textbook_slice}\n关联切片：{related_slices}"
    system_prompt, human_prompt = load_prompt_pair(
        _PROMPT_PATH,
        default_system,
        default_human,
        ["stem", "options", "textbook_slice", "related_slices"],
    )
    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", human_prompt)]
    )
    payload = prompt.invoke(
        {
            "stem": question.stem,
            "options": "\n".join(question.options),
            "textbook_slice": question.textbook_slice,
            "related_slices": _related_slices_text(question),
        }
    )
    client = ReliableLLMClient(llm, timeout_seconds=180, retries=2)
    data = client.invoke_json(
        payload,
        fallback={
            "is_calculation_question": True,
            "digit_complexity_too_high": False,
            "step_count_too_high": False,
            "complex_decimal_present": False,
            "mental_math_level": "需草算",
            "complexity_level": "中",
            "issues": [],
            "evidence": [],
        },
    )

    issues = [str(x) for x in (data.get("issues") or [])]
    if bool(data.get("digit_complexity_too_high", False)):
        issues.append("数字位数复杂度偏高，不利于快速作答（计算复杂度维度）")
    if bool(data.get("step_count_too_high", False)):
        issues.append("计算步骤过多，推导链路过长（计算复杂度维度）")
    if bool(data.get("complex_decimal_present", False)):
        issues.append("出现复杂小数，心算可行性较差（计算复杂度维度）")
    if str(data.get("mental_math_level", "")).strip() == "明显需计算器":
        issues.append("该题明显需计算器，不符合“尽可能不用计算器”导向（计算复杂度维度）")

    details = {
        "enabled": True,
        "is_calculation_question": bool(data.get("is_calculation_question", True)),
        "digit_complexity_too_high": bool(data.get("digit_complexity_too_high", False)),
        "step_count_too_high": bool(data.get("step_count_too_high", False)),
        "complex_decimal_present": bool(data.get("complex_decimal_present", False)),
        "mental_math_level": str(data.get("mental_math_level", "需草算") or "需草算"),
        "complexity_level": str(data.get("complexity_level", "中") or "中"),
        "evidence": [str(x) for x in (data.get("evidence") or [])],
    }
    return issues, details
