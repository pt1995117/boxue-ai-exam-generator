"""第3层并行维度：干扰项质量（独立Agent）。"""

from __future__ import annotations

from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.llm import ReliableLLMClient
from src.prompt_loader import load_prompt_pair
from src.schemas.evaluation import DistractorQuality, QuestionInput

_PROMPT_PATH = "prompts/layer3_distractor_quality.md"


def distractor_quality_agent(
    question: QuestionInput,
    llm: Any,
) -> tuple[DistractorQuality, list[str], dict[str, Any]]:
    default_system = "你是干扰项审核员。"
    default_human = "题干：{stem}\n选项：{options}\n正确答案：{correct_answer}"
    system_prompt, human_prompt = load_prompt_pair(
        _PROMPT_PATH,
        default_system,
        default_human,
        ["stem", "options", "correct_answer"],
    )
    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", human_prompt)]
    )
    payload = prompt.invoke(
        {
            "stem": question.stem,
            "options": "\n".join(question.options),
            "correct_answer": question.correct_answer,
        }
    )
    client = ReliableLLMClient(llm, timeout_seconds=180, retries=2)
    data = client.invoke_json(
        payload,
        fallback={
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
    )

    dq_raw = data.get("distractor_quality", {}) or {}
    dq = DistractorQuality(
        real_but_inapplicable=bool(dq_raw.get("real_but_inapplicable", True)),
        format_aligned=bool(dq_raw.get("format_aligned", True)),
        logic_homogenous=bool(dq_raw.get("logic_homogenous", True)),
        balance_strength=bool(dq_raw.get("balance_strength", True)),
    )
    issues = [str(x) for x in (data.get("issues") or [])]
    unsupported_options = [str(x).strip().upper() for x in (data.get("unsupported_options") or []) if str(x).strip()]
    why_unrelated = [str(x) for x in (data.get("why_unrelated") or [])]
    overlap_pairs = [str(x) for x in (data.get("overlap_pairs") or [])]
    stem_option_conflicts = [str(x) for x in (data.get("stem_option_conflicts") or [])]
    mutual_exclusivity_fail = bool(data.get("mutual_exclusivity_fail", False))
    if unsupported_options:
        issues.append(f"选项级证据：不相关/不成立选项={','.join(unsupported_options)}")
    if why_unrelated:
        issues.extend([f"选项级证据：{x}" for x in why_unrelated])
    if overlap_pairs:
        issues.extend([f"选项重叠证据：{x}" for x in overlap_pairs])
    if stem_option_conflicts:
        issues.extend([f"题干-选项冲突证据：{x}" for x in stem_option_conflicts])
    if mutual_exclusivity_fail:
        issues.append("选项互斥性不足：存在多个选项同时看似成立")
    details = {
        "unsupported_options": unsupported_options,
        "why_unrelated": why_unrelated,
        "overlap_pairs": overlap_pairs,
        "stem_option_conflicts": stem_option_conflicts,
        "mutual_exclusivity_fail": mutual_exclusivity_fail,
    }
    return dq, issues, details
