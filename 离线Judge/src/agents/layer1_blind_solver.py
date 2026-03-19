"""第1层：盲答守门节点（Solver Agent）。"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.agents.safe_python_runner import execute_code
from src.llm import ReliableLLMClient, build_llm, resolve_ait_base_url, get_observability
from src.prompt_loader import load_prompt_pair
from src.schemas.evaluation import QuestionInput, SemanticDrift, SolverValidation


_PROMPT_PATH = "prompts/layer1_blind_solver.md"


def _normalize_question_type(question_type: str) -> str:
    qt = str(question_type or "").strip()
    if qt == "multi_choice":
        return "multiple_choice"
    return qt or "single_choice"


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


def _examples_from_question(question: QuestionInput) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for item in (question.examples or []):
        if isinstance(item, dict):
            examples.append(item)
    if question.mother_question.strip():
        examples.append({"题干": question.mother_question.strip(), "解析": "", "来源": "mother_question"})
    return examples[:5]


def _examples_text(examples: list[dict[str, Any]]) -> str:
    if not examples:
        return "无"
    lines: list[str] = []
    for i, ex in enumerate(examples, start=1):
        stem = str(ex.get("题干", "") or ex.get("stem", "") or "").strip()
        explanation = str(ex.get("解析", "") or ex.get("explanation", "") or "").strip()
        if explanation:
            lines.append(f"{i}. 题干：{stem}\n   解析：{explanation}")
        else:
            lines.append(f"{i}. 题干：{stem}")
    return "\n".join(lines)


def _examples_have_calculation(examples: list[dict[str, Any]]) -> bool:
    calc_keywords = ["计算", "公式", "=", "×", "÷", "%", "元", "平方米", "年", "税率", "贷款"]
    for ex in examples:
        content = (
            str(ex.get("题干", "") or ex.get("stem", "") or "")
            + "\n"
            + str(ex.get("解析", "") or ex.get("explanation", "") or "")
        )
        if any(k in content for k in calc_keywords):
            return True
    return False


def _term_locks_text(question: QuestionInput) -> str:
    locks = [str(x or "").strip() for x in (question.term_locks or []) if str(x or "").strip()]
    if not locks:
        return "无"
    return json.dumps(locks, ensure_ascii=False)


def _kb_context_text(question: QuestionInput) -> str:
    sections = [
        f"主教材切片：\n{question.textbook_slice or '无'}",
        f"关联切片：\n{_related_slices_text(question)}",
        f"参考切片：\n{_reference_slices_text(question)}",
    ]
    if question.mother_question.strip():
        sections.append(f"关联母题：\n{question.mother_question.strip()}")
    return "\n\n".join(sections)


def _resolve_calc_llm(primary_llm: Any) -> tuple[Any, str]:
    # 触发配置文件加载（ARK_CONFIG.txt / AIT_CONFIG.txt），确保 CALC_* 可从配置文件读取
    _ = resolve_ait_base_url()
    calc_provider = os.getenv("CALC_PROVIDER", "ait").strip() or "ait"
    calc_model = (os.getenv("CALC_MODEL", "") or "").strip()
    if not calc_model:
        return primary_llm, "inherit"

    base_url = (os.getenv("AIT_BASE_URL", "") or "").strip()
    chosen_model = calc_model
    if calc_model.lower().startswith("gpt") and "api.deepseek.com" in base_url:
        throttle_path = Path(".gpt_rate_limit.txt")
        if throttle_path.exists():
            try:
                last_ts = float(throttle_path.read_text(encoding="utf-8").strip() or "0")
                wait_needed = max(0.0, 12.0 - (time.time() - last_ts))
                if wait_needed > 5:
                    chosen_model = os.getenv("CALC_FALLBACK_MODEL", "deepseek-chat").strip() or "deepseek-chat"
            except Exception:
                pass

    try:
        calc_llm = build_llm(provider=calc_provider, model=chosen_model, temperature=0)
        return calc_llm, chosen_model
    except Exception:
        return primary_llm, f"inherit({chosen_model})"


def _plan_calculation(question: QuestionInput, llm: Any) -> dict[str, Any]:
    examples = _examples_from_question(question)
    examples_have_calc = _examples_have_calculation(examples)
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是计算专家 (CalculatorAgent)。你要先判断是否需要计算，再在需要时给出可执行 Python 代码。只输出 JSON。",
            ),
            (
                "human",
                "当前知识点掌握程度：{mastery}\n专有名词锁词：{term_locks}\n\n"
                "参考材料：\n{kb_context}\n\n"
                "参考范例（照猫画虎）：\n{examples_text}\n"
                "范例是否含计算题：{examples_have_calc}\n\n"
                "当前题目：\n题干：{stem}\n选项：{options}\n\n"
                "输出 JSON：\n"
                "{{\n"
                "  \"need_calculation\": true/false,\n"
                "  \"python_code\": \"...\",\n"
                "  \"extracted_params\": {{\"k\":\"v\"}},\n"
                "  \"reason\": \"...\"\n"
                "}}\n"
                "规则：\n"
                "1) 若 need_calculation=true，python_code 必须完整可运行，且最后将结果赋值给 result。\n"
                "2) 代码不依赖外部函数，需处理边界情况（如除零）。\n"
                "3) 若范例含计算题，优先生成计算代码。\n"
                "4) 若不需要计算，python_code 置空字符串。",
            ),
        ]
    )
    payload = prompt.invoke(
        {
            "mastery": question.mastery or "未知",
            "term_locks": _term_locks_text(question),
            "kb_context": _kb_context_text(question),
            "examples_text": _examples_text(examples),
            "examples_have_calc": "是" if examples_have_calc else "否",
            "stem": question.stem,
            "options": "\n".join(question.options),
        }
    )
    client = ReliableLLMClient(llm, timeout_seconds=120, retries=2)
    return client.invoke_json(
        payload,
        fallback={
            "need_calculation": False,
            "python_code": "",
            "extracted_params": {},
            "reason": "未识别出明确计算需求",
        },
    )


def _execute_calculation_code(python_code: str) -> tuple[bool, Any, str]:
    code = f"{python_code}\n__judge_emit({{'ok': True, 'result': result}})"
    run = execute_code(code, timeout_seconds=2.5)
    if not run.get("ok", False):
        issues = [str(x) for x in (run.get("issues") or [])]
        return False, None, "；".join(issues) if issues else "代码执行失败"
    payload = run.get("result")
    if not isinstance(payload, dict):
        return False, None, "代码输出结构无效"
    if not bool(payload.get("ok", False)):
        return False, None, str(payload.get("error", "代码执行失败"))
    return True, payload.get("result"), ""


def _option_pairs(question: QuestionInput) -> list[tuple[str, str]]:
    options = list(question.options or [])
    letters = ["A", "B", "C", "D"]
    return [(letters[i], str(options[i] or "").strip()) for i in range(min(4, len(options)))]


def _extract_explicit_letters(raw: str, *, multiple: bool) -> str | None:
    compact = re.sub(r"\s+", "", raw.upper())
    patterns = [
        r"(?:CONCLUSION|结论)\s*[:：=]\s*([ABCD]{1,4}|NONE)",
        r"(?:本题答案为|答案为|答案是|选答案|选择|应选|故选|最终答案为|正确答案为)([ABCD]{1,4})",
        r"^[（(]?\s*([ABCD]{1,4})\s*[）)]?[。．,\s]",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if not match:
            continue
        if match.group(1) == "NONE":
            return None
        value = "".join(dict.fromkeys(match.group(1)))
        if multiple:
            return value if len(value) >= 1 else None
        return value[0] if value else None
    return None


def _map_true_false_text_to_letter(raw: str, option_pairs: list[tuple[str, str]]) -> str | None:
    tf_map: dict[str, str] = {}
    for letter, text in option_pairs[:2]:
        normalized = re.sub(r"^[A-Da-d][\\.．、]\s*", "", text).strip()
        if "正确" in normalized:
            tf_map["正确"] = letter
        if "错误" in normalized:
            tf_map["错误"] = letter
    if not tf_map:
        return None
    if "不正确" in raw or "错误" in raw:
        return tf_map.get("错误")
    if "正确" in raw:
        return tf_map.get("正确")
    return None


def _deterministic_solve_from_raw(question: QuestionInput, raw_text: str) -> tuple[str | None, str]:
    """
    Best-effort 从自然语言盲答文本中直接提取唯一选项，尽量避免 JSON 失败场景。

    策略（保守）：
    1）支持 single_choice / true_false / multiple_choice（兼容 multi_choice）；
    2）优先识别“答案为A/ABC、选择B”这类显式字母结论；
    3）判断题额外支持“正确/错误”自然语言直接映射到 A/B；
    4）若没有显式字母，再用“唯一选项文本命中”恢复答案；
    5）如仍无法唯一确定，则返回 (None, 原始文本前 400 字)，交给后续 LLM 抽取器处理。
    """
    try:
        raw = str(raw_text or "").strip()
        if not raw:
            return None, ""
        question_type = _normalize_question_type(getattr(question, "question_type", ""))
        option_pairs = _option_pairs(question)
        if not option_pairs:
            return None, raw[:400]
        normalized_raw = re.sub(r"[*`“”\"']", "", raw)

        if question_type in {"single_choice", "true_false"}:
            explicit = _extract_explicit_letters(raw, multiple=False)
            if explicit:
                return explicit, raw[:400]
        elif question_type == "multiple_choice":
            explicit = _extract_explicit_letters(raw, multiple=True)
            if explicit:
                return explicit, raw[:400]

        if question_type == "true_false":
            tf_letter = _map_true_false_text_to_letter(raw, option_pairs)
            if tf_letter:
                return tf_letter, raw[:400]

        # 语义锚点：模型常输出“X的是：某选项文本”，即使后面会提到其他选项作为解释。
        # 这里不依赖单题关键词，而是抽象为“判别短语 + 局部窗口唯一命中”规则。
        anchor_patterns = [
            r"(?:不包括的是|不属于的是|错误的是|不正确的是|不符合的是|例外是|除外是)\s*[:：]?\s*(.+?)(?:[。；;\n]|$)",
            r"(?:答案是|应选|应当选|选择的是|正确的是)\s*[:：]?\s*(.+?)(?:[。；;\n]|$)",
        ]
        for pat in anchor_patterns:
            m = re.search(pat, normalized_raw)
            if not m:
                continue
            window = str(m.group(1) or "").strip()
            if not window:
                continue
            matched_letters: list[str] = []
            for letter, text in option_pairs:
                t = re.sub(r"^[A-Da-d][\\.．、]\s*", "", str(text or "")).strip()
                if not t:
                    continue
                if t in window:
                    matched_letters.append(letter)
            deduped_anchor = "".join(dict.fromkeys(matched_letters))
            if question_type in {"single_choice", "true_false"} and len(deduped_anchor) == 1:
                return deduped_anchor, raw[:400]
            if question_type == "multiple_choice" and deduped_anchor:
                return deduped_anchor, raw[:400]

        hits: list[str] = []
        for letter, text in option_pairs:
            t = re.sub(r"^[A-Da-d][\\.．、]\s*", "", text).strip()
            if t and t in normalized_raw:
                hits.append(letter)
        if question_type in {"single_choice", "true_false"} and len(hits) == 1:
            return hits[0], raw[:400]
        if question_type == "multiple_choice" and hits:
            deduped = "".join(dict.fromkeys(hits))
            return deduped, raw[:400]
        return None, raw[:400]
    except Exception:
        return None, ""


def _parse_solver_evaluation_text(raw_text: str) -> dict[str, Any]:
    """
    Parse non-JSON extractor output.

    Expected protocol (plain text):
    SCORE=0|4
    PREDICTED_ANSWER=A|B|C|D|NONE
    REASONING_PATH=...
    FATAL_LOGIC_ISSUES=issue1；issue2|无
    """
    text = str(raw_text or "").strip()
    result: dict[str, Any] = {
        "score": 0,
        "predicted_answer": "NONE",
        "reasoning_path": "",
        "fatal_logic_issues": [],
        "_parsed_keys": [],
    }
    if not text:
        return result
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            if isinstance(obj.get("solver_validation"), dict):
                sv = obj.get("solver_validation") or {}
                result["score"] = 0 if bool(sv.get("ambiguity_found", False)) else 4
                result["predicted_answer"] = str(sv.get("predicted_answer", "NONE") or "NONE")
                result["reasoning_path"] = str(sv.get("reasoning_path", "") or "")
                result["fatal_logic_issues"] = []
                result["_parsed_keys"] = ["score", "predicted_answer", "reasoning_path"]
                return result
            if isinstance(obj.get("solver_evaluation"), dict):
                se = obj.get("solver_evaluation") or {}
                result["score"] = int(se.get("score", 0) or 0)
                result["predicted_answer"] = str(se.get("predicted_answer", "NONE") or "NONE")
                result["reasoning_path"] = str(se.get("reasoning_path", "") or "")
                result["fatal_logic_issues"] = [str(x) for x in (se.get("fatal_logic_issues") or [])]
                result["_parsed_keys"] = ["score", "predicted_answer", "reasoning_path", "fatal_logic_issues"]
                return result
    except Exception:
        pass

    key_alias = {
        "score": "score",
        "评分": "score",
        "predicted_answer": "predicted_answer",
        "predicted": "predicted_answer",
        "最终答案": "predicted_answer",
        "答案": "predicted_answer",
        "reasoning_path": "reasoning_path",
        "reasoning": "reasoning_path",
        "推理链": "reasoning_path",
        "reasoning_path_summary": "reasoning_path",
        "fatal_logic_issues": "fatal_logic_issues",
        "fatal_issues": "fatal_logic_issues",
        "致命逻辑缺陷": "fatal_logic_issues",
        "致命问题": "fatal_logic_issues",
    }

    buckets: dict[str, list[str]] = {
        "score": [],
        "predicted_answer": [],
        "reasoning_path": [],
        "fatal_logic_issues": [],
    }
    parsed_keys: set[str] = set()
    current_key: str | None = None
    for raw_line in text.splitlines():
        line = str(raw_line or "").rstrip()
        if not line.strip():
            if current_key in {"reasoning_path", "fatal_logic_issues"}:
                buckets[current_key].append("")
            continue
        match = re.match(r"^\s*[\"']?([A-Za-z_一-龥]+)[\"']?\s*[:：=]\s*(.*)\s*$", line)
        if match:
            maybe_key = str(match.group(1) or "").strip().lower()
            canonical = key_alias.get(maybe_key) or key_alias.get(str(match.group(1) or "").strip())
            if canonical:
                current_key = canonical
                parsed_keys.add(canonical)
                value = str(match.group(2) or "").strip()
                value = re.sub(r",\s*$", "", value)
                buckets[canonical].append(value)
                continue
        if current_key:
            buckets[current_key].append(line.strip())

    score_raw = "\n".join(buckets["score"]).strip()
    score_match = re.search(r"-?\d+", score_raw)
    if score_match:
        score_num = int(score_match.group(0))
        result["score"] = 4 if score_num >= 4 else 0

    predicted_raw = "\n".join(buckets["predicted_answer"]).strip().upper()
    if predicted_raw:
        if "NONE" in predicted_raw or "无法" in predicted_raw or "不能" in predicted_raw:
            result["predicted_answer"] = "NONE"
        else:
            letters = re.findall(r"[ABCD]", predicted_raw)
            if letters:
                result["predicted_answer"] = "".join(dict.fromkeys(letters))

    reasoning = "\n".join(buckets["reasoning_path"]).strip()
    if reasoning:
        result["reasoning_path"] = reasoning

    fatal_raw = "\n".join(buckets["fatal_logic_issues"]).strip()
    fatal_issues: list[str] = []
    if fatal_raw and fatal_raw not in {"无", "无。", "none", "NONE", "[]"}:
        for part in re.split(r"[;\n；]+", fatal_raw):
            item = re.sub(r"^\s*[-*•]\s*", "", str(part or "").strip())
            if item:
                fatal_issues.append(item)
    result["fatal_logic_issues"] = fatal_issues
    result["_parsed_keys"] = sorted(parsed_keys)

    # Conservative recovery when parser got partial fields.
    if result["predicted_answer"] == "NONE" or result["fatal_logic_issues"]:
        result["score"] = 0
    elif not reasoning:
        result["score"] = 0
    elif result["score"] <= 0:
        result["score"] = 4
    return result


def _solver_eval_is_parse_success(parsed: dict[str, Any]) -> bool:
    parsed_keys = set(str(x) for x in (parsed.get("_parsed_keys") or []))
    if not {"score", "predicted_answer", "reasoning_path"}.issubset(parsed_keys):
        return False
    score = int(parsed.get("score", 0) or 0)
    predicted = str(parsed.get("predicted_answer", "") or "").strip().upper()
    reasoning = str(parsed.get("reasoning_path", "") or "").strip()
    if not reasoning:
        return False
    if predicted in {"", "NONE"}:
        return score == 0
    if not re.fullmatch(r"[ABCD]{1,4}", predicted):
        return False
    return score in {0, 4}


def _normalize_answer_letters(raw_answer: str, question_type: str) -> str:
    raw = str(raw_answer or "").upper()
    letters = re.findall(r"[ABCD]", raw)
    if not letters:
        return "NONE"
    if _normalize_question_type(question_type) == "multiple_choice":
        return "".join(dict.fromkeys(letters))
    return letters[0]


def _is_answer_match(predicted: str, correct: str, question_type: str) -> bool:
    p = _normalize_answer_letters(predicted, question_type)
    c = _normalize_answer_letters(correct, question_type)
    if "NONE" in {p, c}:
        return False
    if _normalize_question_type(question_type) == "multiple_choice":
        return set(p) == set(c)
    return p == c


def _llm_compare_blind_answer(question: QuestionInput, raw_solver_text: str, llm: Any) -> dict[str, Any]:
    """
    第二道判定：不要求盲答输出结构化，只要表达出答案即可。
    用 LLM 从盲答原文抽取结论，并与标准答案比对是否一致。
    """
    fallback = {
        "extracted_answer": "NONE",
        "answer_extractable": False,
        "is_consistent_with_gold": False,
        "reason": "无法从盲答原文稳定抽取最终答案。",
    }
    if not str(raw_solver_text or "").strip():
        return fallback

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是出题质检裁判。你的任务只有两件事："
                "1) 从盲答原文中抽取模型最终结论答案；"
                "2) 判断该结论是否与标准答案一致。"
                "只输出 JSON，不要附加解释。",
            ),
            (
                "human",
                "题型：{question_type}\n"
                "题干：{stem}\n"
                "选项：\n{options}\n"
                "标准答案：{correct_answer}\n\n"
                "盲答原文：\n{raw_answer}\n\n"
                "输出 JSON：\n"
                "{{\n"
                "  \"extracted_answer\": \"A/B/C/D/AB/ACD/NONE\",\n"
                "  \"answer_extractable\": true/false,\n"
                "  \"is_consistent_with_gold\": true/false,\n"
                "  \"reason\": \"一句话说明依据\"\n"
                "}}\n"
                "规则：\n"
                "1) 若盲答没有稳定最终答案，extracted_answer=\"NONE\" 且 answer_extractable=false；\n"
                "2) 多选题按集合比较（顺序不敏感）；\n"
                "3) 无法判断时，is_consistent_with_gold=false。",
            ),
        ]
    )
    payload = prompt.invoke(
        {
            "question_type": question.question_type,
            "stem": question.stem,
            "options": "\n".join(question.options),
            "correct_answer": question.correct_answer,
            "raw_answer": raw_solver_text,
        }
    )
    client = ReliableLLMClient(llm, timeout_seconds=60, retries=1)
    data = client.invoke_json(payload, fallback=fallback)
    if not isinstance(data, dict):
        return fallback
    normalized = dict(fallback)
    normalized.update(data)
    normalized["extracted_answer"] = _normalize_answer_letters(
        str(normalized.get("extracted_answer", "NONE") or "NONE"),
        question.question_type,
    )
    if normalized["extracted_answer"] == "NONE":
        normalized["answer_extractable"] = False
        normalized["is_consistent_with_gold"] = False
    return normalized


def layer1_blind_solver_agent(
    question: QuestionInput,
    llm: Any,
) -> tuple[SolverValidation, SemanticDrift, list[str], list[str], dict[str, Any]]:
    default_system = "你是房地产教研逻辑审计专家。"
    examples = _examples_from_question(question)
    calc_context: dict[str, Any] = {
        "enabled": False,
        "need_calculation": False,
        "plan_reason": "",
        "extracted_params": {},
        "code_status": "skipped",
        "result": None,
        "generated_code": "",
        "examples_count": len(examples),
        "examples_have_calculations": _examples_have_calculation(examples),
        "logs": [],
    }
    default_human = (
        "题型：{question_type}\n评估类型：{assessment_type}\n教材切片：{textbook_slice}\n关联切片：{related_slices}\n"
        "参考切片：{reference_slices}\n题干：{stem}\n选项：{options}\n参考范例：{examples_text}\n计算辅助上下文：{calc_context}"
    )
    calc_hard_fail = False
    calc_fail_reason = ""
    if bool(question.is_calculation):
        calc_llm, calc_model_used = _resolve_calc_llm(llm)
        calc_context["logs"].append(f"Calculator model: {calc_model_used}")
        plan = _plan_calculation(question, calc_llm)
        need_calc = bool(plan.get("need_calculation", False))
        python_code = str(plan.get("python_code", "") or "").strip()
        calc_context["enabled"] = True
        calc_context["need_calculation"] = need_calc
        calc_context["plan_reason"] = str(plan.get("reason", "") or "")
        calc_context["generated_code"] = python_code
        calc_context["extracted_params"] = (
            plan.get("extracted_params")
            if isinstance(plan.get("extracted_params"), dict)
            else {}
        )
        calc_context["logs"].append(
            f"Calculator plan: need_calculation={need_calc}, examples={len(examples)}, examples_have_calculations={calc_context['examples_have_calculations']}"
        )
        if need_calc and python_code:
            ok, result, err = _execute_calculation_code(python_code)
            if ok:
                calc_context["code_status"] = "success"
                calc_context["result"] = result
                calc_context["logs"].append(f"Calculator execute success: result={result}")
            else:
                calc_context["code_status"] = "error"
                calc_context["result"] = None
                calc_hard_fail = True
                calc_fail_reason = err
                calc_context["logs"].append(f"Calculator execute error: {err}")
        elif need_calc:
            calc_context["code_status"] = "error"
            calc_context["result"] = None
            calc_hard_fail = True
            calc_fail_reason = "need_calculation=true 但未返回有效 python_code"
            calc_context["logs"].append("Calculator execute skipped: missing python_code while need_calculation=true")
        else:
            calc_context["code_status"] = "no_calculation"
            calc_context["logs"].append("Calculator skipped: model judged no calculation needed")

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
            "examples_text",
            "calc_context",
        ],
    )
    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", human_prompt)])

    payload = prompt.invoke(
        {
            "question_type": question.question_type,
            "assessment_type": question.assessment_type,
            "stem": question.stem,
            "options": "\n".join(question.options),
            "textbook_slice": question.textbook_slice,
            "related_slices": _related_slices_text(question),
            "reference_slices": _reference_slices_text(question),
            "examples_text": _examples_text(examples),
            "calc_context": json.dumps(calc_context, ensure_ascii=False),
        }
    )
    client = ReliableLLMClient(llm, timeout_seconds=180, retries=3)
    score = 0
    predicted_answer = "NONE"
    reasoning_path = ""
    fatal_logic_issues: list[str] = []
    raw_solver_text = ""
    parse_ok = False

    answer_compare: dict[str, Any] = {}
    try:
        raw_solver_text = client.invoke_text(payload)
    except Exception:
        raw_solver_text = ""

    if raw_solver_text:
        try:
            answer_compare = _llm_compare_blind_answer(question, raw_solver_text, llm)
            extracted = str(answer_compare.get("extracted_answer", "NONE") or "NONE").upper()
            extractable = bool(answer_compare.get("answer_extractable", False))
            consistent = bool(answer_compare.get("is_consistent_with_gold", False))
            compare_reason = str(answer_compare.get("reason", "") or "").strip()
            if extractable and extracted != "NONE":
                predicted_answer = extracted
                parse_ok = True
                if consistent:
                    score = 4
                    reasoning_path = compare_reason or "盲答结论与标准答案一致。"
                    fatal_logic_issues = []
                else:
                    score = 0
                    gold = _normalize_answer_letters(question.correct_answer, question.question_type)
                    reasoning_path = compare_reason or "盲答结论与标准答案不一致。"
                    fatal_logic_issues = [f"盲答结论与标准答案不一致：blind={predicted_answer}, gold={gold}"]
        except Exception:
            answer_compare = {}

    if not parse_ok and raw_solver_text:
        try:
            extract_model = os.getenv("SOLVER_EXTRACT_MODEL", "").strip() or "deepseek-chat"
            extract_provider = os.getenv("SOLVER_EXTRACT_PROVIDER", "").strip() or "deepseek"
            extract_llm = build_llm(provider=extract_provider, model=extract_model, temperature=0)
            extract_client = ReliableLLMClient(extract_llm, timeout_seconds=60, retries=1)
            extract_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "你是一个参数抽取器，只负责从给定的盲答输出中抽取结构化结果，"
                        "不要自行推理题目对错。严格按纯文本键值协议输出，不要输出 JSON。\n"
                        "字段含义说明：\n"
                        "1）score：盲答可靠性评分，4 表示“能基于题干和教材唯一推出一个选项”，0 表示“存在多解/无解/条件缺失等致命问题或无法判断”。\n"
                        "2）predicted_answer：模型认为的最终选项，只能是 A/B/C/D 中的一个字母；若明确无法给出唯一答案，则填 NONE。\n"
                        "3）reasoning_path：支撑上述结论的完整推理链或失败原因描述，应尽量复用原始输出中的关键语句，不要自行脑补。\n"
                        "4）fatal_logic_issues：若从原始输出中能看出题目存在多解、无解、条件缺失等致命逻辑缺陷，请用自然语言逐条列出；若只是模型表述不清、但逻辑上仍可唯一推出答案，可以留空。",
                    ),
                    (
                        "human",
                        "【题目摘要】\n"
                        "题干：{stem}\n"
                        "选项：{options}\n\n"
                        "【上一节点原始输出】\n"
                        "{raw_answer}\n\n"
                        "请从上述【上一节点原始输出】中抽取盲答结果，按如下纯文本格式返回（不得输出 JSON）：\n"
                        "SCORE=0 或 4\n"
                        "PREDICTED_ANSWER=A/B/C/D 或 NONE\n"
                        "REASONING_PATH=完整推理链或失败原因\n"
                        "FATAL_LOGIC_ISSUES=问题1；问题2（若无填“无”）\n"
                        "规则：\n"
                        "1）如果能从原始输出中看出模型明确选择了某个选项，则返回对应字母，并把对应的推理过程复写到 reasoning_path 中；\n"
                        "2）若原始输出明确表述为多解、无解或条件缺失，则认为 score=0 且 predicted_answer=\"NONE\"，并在 fatal_logic_issues 中说明原因；\n"
                        "3）若原始输出只是在格式上不规范，但你仍能可靠看出唯一答案，请填 score=4，predicted_answer 为该选项，并在 reasoning_path 中解释依据；\n"
                        "4）若无法从原始输出中可靠抽取唯一答案，只能保守认为结果不可用：score=0, predicted_answer=\"NONE\"，"
                        "并在 fatal_logic_issues 中写明“无法从原始输出中可靠抽取答案”。\n"
                        "只返回上述四行，不要输出任何解释或多余文字。",
                    ),
                ]
            )
            extract_payload = extract_prompt.invoke(
                {
                    "stem": question.stem,
                    "options": "\n".join(question.options),
                    "raw_answer": raw_solver_text,
                }
            )
            extracted_text = extract_client.invoke_text(extract_payload)
            parsed_extract = _parse_solver_evaluation_text(extracted_text)
            if _solver_eval_is_parse_success(parsed_extract):
                predicted_answer = _normalize_answer_letters(
                    str(parsed_extract.get("predicted_answer", "NONE") or "NONE"),
                    question.question_type,
                )
                if predicted_answer != "NONE":
                    gold = _normalize_answer_letters(question.correct_answer, question.question_type)
                    matched = _is_answer_match(predicted_answer, gold, question.question_type)
                    score = 4 if matched else 0
                    reasoning_path = str(parsed_extract.get("reasoning_path", "") or "")
                    fatal_logic_issues = []
                    if not matched:
                        fatal_logic_issues.append(f"盲答结论与标准答案不一致：blind={predicted_answer}, gold={gold}")
                else:
                    score = 0
                    reasoning_path = str(parsed_extract.get("reasoning_path", "") or "")
                    fatal_logic_issues = [str(x) for x in (parsed_extract.get("fatal_logic_issues") or [])]
                parse_ok = True
        except Exception:
            pass

    if not parse_ok and raw_solver_text:
        deterministic_answer, deterministic_reasoning = _deterministic_solve_from_raw(question, raw_solver_text)
        if deterministic_answer:
            predicted_answer = _normalize_answer_letters(deterministic_answer, question.question_type)
            gold = _normalize_answer_letters(question.correct_answer, question.question_type)
            matched = _is_answer_match(predicted_answer, gold, question.question_type)
            score = 4 if matched else 0
            reasoning_path = (
                f"{deterministic_reasoning}\n[deterministic_fallback] 主协议解析失败，按原始盲答文本恢复"
                if deterministic_reasoning
                else "[deterministic_fallback] 主协议解析失败，按原始盲答文本恢复"
            )
            fatal_logic_issues = [] if matched else [f"盲答结论与标准答案不一致：blind={predicted_answer}, gold={gold}"]
            parse_ok = True

    if not parse_ok:
        obs = get_observability()
        score = 0
        predicted_answer = "NONE"
        reasoning_path = "当前盲答结果未能转化为可靠结构化结论，请结合原始输出进行人工复核。"
        fatal_logic_issues = ["盲答输出解析失败：主解析与参数抽取均未恢复出完整字段。"]
        if str(obs.get("last_error") or "").strip():
            fatal_logic_issues.append(f"LLM调用异常：{str(obs.get('last_error') or '').strip()}")

    if calc_hard_fail:
        score = 0
        predicted_answer = "NONE"
        reasoning_path = (
            f"{reasoning_path}\n[计算辅助失败] {calc_fail_reason}"
            if reasoning_path
            else f"[计算辅助失败] {calc_fail_reason}"
        )
        fatal_logic_issues.append(f"计算辅助失败：{calc_fail_reason}")

    if bool(calc_context.get("enabled")):
        calc_note = (
            f"need={calc_context.get('need_calculation')},"
            f"status={calc_context.get('code_status')},"
            f"result={calc_context.get('result')}"
        )
        reasoning_path = f"{reasoning_path}\n[计算辅助] {calc_note}" if reasoning_path else f"[计算辅助] {calc_note}"

    ambiguity = score == 0 or predicted_answer.upper() in {"", "NONE"} or len(fatal_logic_issues) > 0
    solver_result = SolverValidation(
        predicted_answer=predicted_answer,
        reasoning_path=reasoning_path,
        ambiguity_flag=ambiguity,
    )

    drift_issues: list[str] = []
    drift_result = SemanticDrift(
        limit_words_consistent=True,
        rule_constraints_kept=True,
        fingerprint_matched=True,
    )

    solver_issues: list[str] = []
    if score == 0:
        if solver_result.predicted_answer and solver_result.predicted_answer.upper() != "NONE":
            solver_issues.append(
                f"【Solver结论冲突】predicted={solver_result.predicted_answer}；reasoning={solver_result.reasoning_path}"
            )
        else:
            solver_issues.append(
                f"【Solver失败】predicted={solver_result.predicted_answer}；reasoning={solver_result.reasoning_path}"
            )
    for issue in fatal_logic_issues:
        solver_issues.append(f"【致命逻辑缺陷】{issue}")
    if solver_result.ambiguity_flag:
        solver_issues.append(
            f"【Solver歧义】predicted={solver_result.predicted_answer}；reasoning={solver_result.reasoning_path}"
        )

    calc_data = {
        "tool_usage": {
            "method": "dynamic_code_generation",
            "generated_code": calc_context.get("generated_code", ""),
            "extracted_params": calc_context.get("extracted_params", {}),
            "result": calc_context.get("result"),
            "code_status": calc_context.get("code_status"),
        },
        "execution_result": calc_context.get("result"),
        "generated_code": calc_context.get("generated_code", ""),
        "code_status": calc_context.get("code_status"),
        "examples": examples,
        "logs": [str(x) for x in (calc_context.get("logs") or [])],
        "plan_reason": calc_context.get("plan_reason", ""),
        "answer_compare": answer_compare,
    }
    return solver_result, drift_result, solver_issues, drift_issues, calc_data


reverse_solver_agent = layer1_blind_solver_agent
