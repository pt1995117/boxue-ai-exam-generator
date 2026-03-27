"""第3层并行维度：解析质量（独立Agent）。"""

from __future__ import annotations

from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.llm import ReliableLLMClient
from src.prompt_loader import load_prompt_pair
from src.schemas.evaluation import QuestionInput

_PROMPT_PATH = "prompts/layer3_explanation_quality.md"


def explanation_quality_agent(
    question: QuestionInput,
    llm: Any,
) -> tuple[list[str], dict[str, Any]]:
    default_system = "你是解析质量审核员。"
    default_human = "题型：{question_type}\n题干：{stem}\n选项：{options}\n标准答案：{correct_answer}\n解析：{explanation}"
    system_prompt, human_prompt = load_prompt_pair(
        _PROMPT_PATH,
        default_system,
        default_human,
        ["question_type", "stem", "options", "correct_answer", "explanation"],
    )
    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", human_prompt)]
    )
    payload = prompt.invoke(
        {
            "question_type": question.question_type,
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
            "has_forbidden_media": False,
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
            "first_part_structured_issues": [],
            "three_part_semantic_issues": [],
            "issues": [],
        },
    )

    issues = [str(x) for x in (data.get("issues") or [])]
    if bool(data.get("has_forbidden_media", False)):
        issues.append("解析存在表格/图片表达")

    if question.question_type == "multiple_choice":
        try:
            cov = float(data.get("multi_option_coverage_rate", 1.0))
        except Exception:
            cov = 1.0
        if cov < 1.0:
            issues.append(f"多选解析逐项覆盖率不足：{cov:.2f}")
        missing = [str(x) for x in (data.get("missing_options") or [])]
        if missing:
            issues.append(f"多选解析未覆盖选项：{','.join(missing)}")

    if data.get("three_part_is_clear_and_coherent") is False:
        issues.extend([str(x) for x in (data.get("three_part_semantic_issues") or [])])
        if not any("三段论" in x for x in issues):
            issues.append("解析三段论存在语义不清或前后不自洽")
    if data.get("three_part_semantic_invalid") is True:
        evidence = [str(x) for x in (data.get("three_part_semantic_evidence") or [])]
        if evidence:
            issues.extend([f"三段论语义证据：{x}" for x in evidence])
        if not any("三段论" in x for x in issues):
            issues.append("解析三段论语义完整性不足")
    # 第1段细粒度错误（按条报具体原因）
    if bool(data.get("first_part_missing_target_title", False)):
        issues.append("解析第1段缺少目标题内容（路由前三个标题）")
    if bool(data.get("first_part_missing_level", False)):
        issues.append("解析第1段缺少“分级（了解/掌握/应用/熟悉）”")
    if bool(data.get("first_part_missing_textbook_raw", False)):
        issues.append("解析第1段缺少“教材原文”内容")
    first_part_structured_issues = [str(x) for x in (data.get("first_part_structured_issues") or [])]
    if first_part_structured_issues:
        issues.extend([f"第1段结构问题：{x}" for x in first_part_structured_issues])
    if data.get("analysis_rewrite_sufficient") is False:
        rw_issues = [str(x) for x in (data.get("analysis_rewrite_issues") or [])]
        if rw_issues:
            issues.extend(rw_issues)
        else:
            issues.append("解析第2段未充分转述：术语可保留，但句式与推理表达需重写")

    details = {
        "has_forbidden_media": bool(data.get("has_forbidden_media", False)),
        "multi_option_coverage_rate": float(data.get("multi_option_coverage_rate", 1.0) or 1.0),
        "missing_options": [str(x) for x in (data.get("missing_options") or [])],
        "analysis_rewrite_sufficient": bool(data.get("analysis_rewrite_sufficient", True)),
        "analysis_rewrite_issues": [str(x) for x in (data.get("analysis_rewrite_issues") or [])],
        "three_part_is_clear_and_coherent": bool(data.get("three_part_is_clear_and_coherent", True)),
        "three_part_semantic_invalid": bool(data.get("three_part_semantic_invalid", False)),
        "three_part_semantic_evidence": [str(x) for x in (data.get("three_part_semantic_evidence") or [])],
        "first_part_missing_target_title": bool(data.get("first_part_missing_target_title", False)),
        "first_part_missing_level": bool(data.get("first_part_missing_level", False)),
        "first_part_missing_textbook_raw": bool(data.get("first_part_missing_textbook_raw", False)),
        "first_part_structured_issues": first_part_structured_issues,
    }
    return issues, details
