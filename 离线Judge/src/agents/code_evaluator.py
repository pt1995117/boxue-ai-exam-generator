"""
Node D: 动态代码校验器 (Code Execution Evaluator)

针对计算题，生成 Python 代码验证题干数值，并分析干扰项合理性。
"""

import re
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.agents.safe_python_runner import execute_generate_possible_answers
from src.llm import ReliableLLMClient
from src.prompt_loader import load_prompt_pair
from src.schemas.evaluation import QuestionInput


def _extract_option_numeric(option: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", option)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _build_context(question: QuestionInput) -> dict[str, Any]:
    return {
        "question_stem": question.stem,
        "textbook_rule": question.textbook_slice,
        "related_slices": question.related_slices,
        "question_type": question.question_type,
        "correct_answer": question.correct_answer,
        "options": question.options,
    }


def code_evaluator_agent(question: QuestionInput, llm: Any) -> list[str]:
    """动态代码校验器 Agent（仅针对计算题）。"""
    default_system = "# Role\n你是计算题代码校验专家。"
    default_human = "# Input\n- 教材计算规则：{textbook_slice}\n- 关联切片：{related_slices}\n- 计算题题干：{stem}"
    system_prompt, human_prompt = load_prompt_pair(
        "prompts/layer3_code_evaluator.md",
        default_system,
        default_human,
        ["textbook_slice", "related_slices", "stem"],
    )
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            ("human", human_prompt),
        ]
    )

    client = ReliableLLMClient(llm, timeout_seconds=180, retries=3)
    payload = prompt.invoke(
        {
            "stem": question.stem,
            "options": "\n".join(question.options),
            "correct_answer": question.correct_answer,
            "textbook_slice": question.textbook_slice,
            "related_slices": "\n".join(question.related_slices or []) or "无",
        }
    )

    code_snippet = client.invoke_text(payload).strip()
    issues: list[str] = []
    if not code_snippet:
        return ["计算题未返回可执行代码"]

    context = _build_context(question)
    exec_result = execute_generate_possible_answers(code_snippet, context)
    if not exec_result.get("ok"):
        issues.extend(exec_result.get("issues", []))
        return issues

    result = exec_result.get("result")
    if not isinstance(result, list):
        return ["generate_possible_answers(context) 返回值必须为列表"]

    if len(result) < 3:
        issues.append("错误路径数量不足：至少应包含1个正确结果+2个错误结果")

    correct_items = [x for x in result if isinstance(x, dict) and x.get("type") == "correct"]
    error_items = [x for x in result if isinstance(x, dict) and str(x.get("type", "")).startswith("error")]
    if not correct_items:
        issues.append("返回结构缺少 type=correct 的正确结果")
    if len(error_items) < 2:
        issues.append("返回结构缺少足够的错误路径（至少2条）")

    # Verify values are numeric-like.
    parsed_values: list[float] = []
    for row in result:
        if not isinstance(row, dict) or "value" not in row:
            issues.append("返回结构中的元素必须包含 value 字段")
            continue
        try:
            parsed_values.append(float(row["value"]))
        except (TypeError, ValueError):
            issues.append(f"value 非数值：{row.get('value')}")

    # Compare with answer option if the option is numeric.
    answer_idx = None
    ans = question.correct_answer.strip().upper()
    if len(ans) == 1 and ans in "ABCD":
        answer_idx = ord(ans) - ord("A")
    if answer_idx is not None and answer_idx < len(question.options) and correct_items:
        expected_numeric = _extract_option_numeric(question.options[answer_idx])
        if expected_numeric is not None:
            try:
                correct_value = float(correct_items[0].get("value"))
                if abs(correct_value - expected_numeric) > max(1e-6, abs(expected_numeric) * 1e-4):
                    issues.append(
                        f"正确计算值与正确选项不一致: code={correct_value}, option={expected_numeric}"
                    )
            except (TypeError, ValueError):
                issues.append("correct 结果 value 非数值")

        # Distactor quality: at least one generated error should match some wrong option numeric.
        wrong_option_values = []
        for i, opt in enumerate(question.options):
            if i == answer_idx:
                continue
            v = _extract_option_numeric(opt)
            if v is not None:
                wrong_option_values.append(v)
        generated_errors = []
        for row in error_items:
            try:
                generated_errors.append(float(row.get("value")))
            except (TypeError, ValueError):
                pass
        if wrong_option_values and generated_errors:
            matched = 0
            for g in generated_errors:
                if any(abs(g - w) <= max(1e-6, abs(w) * 1e-4) for w in wrong_option_values):
                    matched += 1
            if matched == 0:
                issues.append("生成的错误结果与题目干扰项缺乏对应关系，疑似随机编造")

    return issues
