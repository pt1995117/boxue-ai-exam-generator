"""LangGraph 多闸门 Judge Pipeline（四层解耦架构）。"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate

from src.agents.layer1_blind_solver import layer1_blind_solver_agent
from src.agents.layer2_knowledge_gate import layer2_knowledge_gate_agent
from src.llm import ReliableLLMClient, build_llm, get_observability
from src.pipeline.state import JudgeState
from src.prompt_loader import load_prompt_pair
from src.schemas.evaluation import (
    Costs,
    Decision,
    DimensionResult,
    DimensionStatus,
    DistractorQuality,
    Evidence,
    HardGate,
    JudgeReport,
    KnowledgeMatch,
    Observability,
    QuestionInput,
    RiskAssessment,
    RiskLevel,
    Scores,
    SemanticDrift,
    SolverValidation,
    TeachingValue,
    TokenUsage,
)


def _risk_level_from_issues(issues: list[str]) -> RiskLevel:
    if not issues:
        return RiskLevel.LOW
    if len(issues) >= 3:
        return RiskLevel.HIGH
    return RiskLevel.MEDIUM


def _issue_key(text: str) -> str:
    """生成用于去重的 issue key，避免同一问题被重复计数。"""
    s = str(text or "").strip()
    if not s:
        return ""
    # 去掉常见前缀标签，统一空白与标点差异
    s = re.sub(r"^【[^】]+】\s*", "", s)
    s = re.sub(r"\s+", "", s)
    s = s.replace("：", ":").replace("（", "(").replace("）", ")")
    return s.lower()


def _dedupe_issues(issues: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in issues:
        txt = str(item or "").strip()
        if not txt:
            continue
        key = _issue_key(txt) or txt
        if key in seen:
            continue
        seen.add(key)
        out.append(txt)
    return out


def _clamp_score_10(v: float) -> float:
    return round(min(10.0, max(0.0, float(v))), 2)


def _calc_dimension_score_with_penalties(base: float, penalties: list[tuple[bool, float, str]]) -> tuple[float, list[str]]:
    score = float(base)
    reasons: list[str] = []
    for cond, minus, reason in penalties:
        if not cond:
            continue
        score -= float(minus)
        reasons.append(f"-{minus:g}: {reason}")
    return _clamp_score_10(score), reasons


def _strip_option_prefix(opt: str) -> str:
    return re.sub(r"^\s*[A-Da-d][\.．、]\s*", "", str(opt or "")).strip()


def _extract_keywords(text: str) -> set[str]:
    """抽取用于“题目是否直给答案”判定的关键词（长度>=2，过滤通用词）。"""
    stopwords = {
        "以下",
        "表述",
        "正确",
        "错误",
        "的是",
        "包括",
        "应当",
        "可以",
        "有",
        "本题",
        "答案",
        "属于",
        "根据",
        "关于",
        "判断",
        "说法",
        "做法",
    }
    toks = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", str(text or ""))
    return {t for t in toks if t not in stopwords}


def _has_year(text: str) -> bool:
    return bool(re.search(r"(19|20)\d{2}年", str(text or "")))


def _question_has_implicit_calculation(question: QuestionInput) -> bool:
    """非计算题中的隐含运算检测（比例/阈值/折算等）。"""
    text = f"{str(question.stem or '')}\n" + "\n".join([str(x or "") for x in (question.options or [])])
    has_nums = len(re.findall(r"\d+(?:\.\d+)?", text)) >= 2
    has_ops = bool(re.search(r"[%％÷/×xX<>≤≥]", text))
    has_calc_terms = bool(
        re.search(r"(比例|配比|占比|折算|阈值|至少|至多|不超过|不低于|大于|小于|高于|低于|倍|保留到|精确到)", text)
    )
    return has_nums and (has_ops or has_calc_terms)


def _collect_year_guard_text_fields(question: QuestionInput) -> list[str]:
    fields: list[str] = [str(question.stem or ""), str(question.explanation or "")]
    fields.extend([str(x or "") for x in (question.options or [])])
    return fields


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


_QUALITY_LLM_CACHE: dict[str, Any] = {}


def _get_quality_llm(default_llm: Any) -> Any:
    model = str(os.getenv("AIT_QUALITY_MODEL", "gpt-5.2") or "gpt-5.2").strip() or "gpt-5.2"
    key = f"ait::{model}"
    cached = _QUALITY_LLM_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        q_llm = build_llm(provider="ait", model=model, temperature=0)
        _QUALITY_LLM_CACHE[key] = q_llm
        return q_llm
    except Exception:
        # 质量模型构建失败时回退到主流程模型，避免整条链路中断
        return default_llm


def _parse_quality_structured_text(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        return {"quality_score": None, "quality_reasons": [], "scoring_basis": "", "dimension_feedback": {}}

    def _extract_block(start_tag: str, end_tag: str) -> str:
        m = re.search(re.escape(start_tag) + r"([\s\S]*?)" + re.escape(end_tag), text, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    score_txt = _extract_block("<QUALITY_SCORE>", "</QUALITY_SCORE>")
    reasons_txt = _extract_block("<QUALITY_REASONS>", "</QUALITY_REASONS>")
    basis = _extract_block("<SCORING_BASIS>", "</SCORING_BASIS>")
    dim_txt = _extract_block("<DIMENSION_FEEDBACK>", "</DIMENSION_FEEDBACK>")

    score: float | None = None
    if score_txt:
        m = re.search(r"-?\d+(?:\.\d+)?", score_txt)
        if m:
            try:
                score = float(m.group(0))
            except Exception:
                score = None

    reasons: list[str] = []
    for line in reasons_txt.splitlines():
        s = re.sub(r"^\s*[-*]\s*", "", line).strip()
        if s:
            reasons.append(s)
    reasons = reasons[:6]

    dim_feedback: dict[str, str] = {}
    for line in dim_txt.splitlines():
        s = str(line or "").strip().lstrip("-").strip()
        if not s:
            continue
        if ":" in s:
            k, v = s.split(":", 1)
        elif "：" in s:
            k, v = s.split("：", 1)
        else:
            continue
        kk = str(k or "").strip()
        vv = str(v or "").strip()
        if kk and vv:
            dim_feedback[kk] = vv

    return {
        "quality_score": score,
        "quality_reasons": reasons,
        "scoring_basis": str(basis or "").strip(),
        "dimension_feedback": dim_feedback,
    }


def _llm_quality_score_eval(
    *,
    llm: Any,
    question: QuestionInput,
) -> tuple[float, list[str], str, dict[str, str]]:
    if not llm:
        raise RuntimeError("quality_score 评估失败：LLM 未配置")
    quality_llm = _get_quality_llm(llm)

    default_system = (
        "你是房地产考试题目质量总评专家。请先进行充分的内部思考（逐步审题、核对逻辑与教学价值、再综合评分），"
        "再给出最终结论。不要泄露你的思考过程，只输出标签化结构文本，不要输出JSON。"
    )
    default_human = (
        "请基于以下信息给出 quality_score（0-10，允许1位小数）：\n"
        "stem={stem}\n"
        "options={options}\n"
        "correct_answer={correct_answer}\n"
        "explanation={explanation}\n"
        "all_slices={all_slices}\n"
        "必须严格按以下标签输出（禁止 JSON）：\n"
        "<QUALITY_SCORE>分数</QUALITY_SCORE>\n"
        "<QUALITY_REASONS>\n"
        "- 一句话结论：...\n"
        "- 题目优点：...\n"
        "- 主要问题1：... 证据：...\n"
        "- 主要问题2：... 证据：...\n"
        "- 区分度判断：...\n"
        "- 入库建议：...\n"
        "- 最小改动方案：...\n"
        "</QUALITY_REASONS>\n"
        "<SCORING_BASIS>一句话核心评分依据</SCORING_BASIS>\n"
        "<DIMENSION_FEEDBACK>\n"
        "- 考点清晰度: 弱/中/强 + 一句话\n"
        "- 题干严谨性: 弱/中/强 + 一句话\n"
        "- 干扰项质量: 弱/中/强 + 一句话\n"
        "- 区分度: 弱/中/强 + 一句话\n"
        "- 场景真实性: 弱/中/强 + 一句话\n"
        "</DIMENSION_FEEDBACK>\n"
    )
    system_prompt, human_prompt = load_prompt_pair(
        "prompts/layer4_quality_score.md",
        default_system,
        default_human,
        [
            "stem",
            "options",
            "correct_answer",
            "explanation",
            "all_slices",
        ],
    )
    all_slices_text = (
        "【教材切片】\n"
        + str(question.textbook_slice or "无")
        + "\n【关联切片】\n"
        + _related_slices_text(question)
        + "\n【参考切片】\n"
        + _reference_slices_text(question)
    )
    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", human_prompt)])
    payload = prompt.invoke(
        {
            "stem": str(question.stem or ""),
            "options": "\n".join([str(x or "") for x in (question.options or [])]),
            "correct_answer": str(question.correct_answer or ""),
            "explanation": str(question.explanation or ""),
            "all_slices": all_slices_text,
        }
    )
    client = ReliableLLMClient(quality_llm, timeout_seconds=120, retries=1)
    raw = client.invoke_text(payload)
    data = _parse_quality_structured_text(raw)
    raw_score = data.get("quality_score")
    try:
        score = float(raw_score)
    except Exception as e:
        raise RuntimeError(f"quality_score 评估失败：模型未返回有效分数（raw={raw_score!r}）") from e
    if not (0.0 <= score <= 10.0):
        raise RuntimeError(f"quality_score 评估失败：分值越界（score={score}）")
    reasons = [str(x).strip() for x in (data.get("quality_reasons") or []) if str(x).strip()]
    basis = str(data.get("scoring_basis", "") or "").strip()
    raw_dim = data.get("dimension_feedback")
    dim_feedback: dict[str, str] = {}
    if isinstance(raw_dim, dict):
        for k, v in raw_dim.items():
            kk = str(k or "").strip()
            vv = str(v or "").strip()
            if kk and vv:
                dim_feedback[kk] = vv
    # 结构字段不完整时，进行一次强约束重试；仍缺失则按原兜底规则报错。
    reasons_text = " ".join(reasons)
    evidence_hit = ("证据：" in reasons_text) or ("证据:" in reasons_text) or ("依据：" in reasons_text) or ("依据:" in reasons_text)
    required_dims = {"考点清晰度", "题干严谨性", "干扰项质量", "区分度", "场景真实性"}
    has_required_dims = len(required_dims.intersection(set(dim_feedback.keys()))) >= 4
    need_retry = (not reasons) or (not basis) or (len(reasons) < 3) or (not evidence_hit) or (not has_required_dims)
    if need_retry:
        strict_human = (
            "你上次输出缺少关键字段。请严格按标签化结构输出（禁止 JSON），且以下字段不得为空：\n"
            "1) quality_reasons 至少6条，并包含“主要问题+证据”表述；\n"
            "2) scoring_basis 必须1句；\n"
            "3) dimension_feedback 必须覆盖：考点清晰度、题干严谨性、干扰项质量、区分度、场景真实性；\n"
            "并重新给出 quality_score（0-10，可1位小数）。\n"
            f"stem={str(question.stem or '')}\n"
            f"options={chr(10).join([str(x or '') for x in (question.options or [])])}\n"
            f"correct_answer={str(question.correct_answer or '')}\n"
            f"explanation={str(question.explanation or '')}\n"
            f"all_slices={all_slices_text}\n"
            "输出模板：\n"
            "<QUALITY_SCORE>8.8</QUALITY_SCORE>\n"
            "<QUALITY_REASONS>\n"
            "- 一句话结论：可用但建议优化后入库\n"
            "- 题目优点：考点明确\n"
            "- 主要问题1：题干未明示判定口径。证据：题干只给日期未给孰先原则\n"
            "- 主要问题2：干扰项层级不一致。证据：D 引入契税票口径，A/B/C为已给日期来源\n"
            "- 区分度判断：存在“按最早日期猜对”风险\n"
            "- 入库建议：优化后入库\n"
            "- 最小改动方案：将选项改为判断逻辑而非日期来源\n"
            "</QUALITY_REASONS>\n"
            "<SCORING_BASIS>一句话</SCORING_BASIS>\n"
            "<DIMENSION_FEEDBACK>\n"
            "- 考点清晰度: 中，考点有但未充分显化\n"
            "- 题干严谨性: 中，依赖隐含规则\n"
            "- 干扰项质量: 弱，存在风格突兀项\n"
            "- 区分度: 中，可能被套路猜对\n"
            "- 场景真实性: 中，场景壳大于判断负担\n"
            "</DIMENSION_FEEDBACK>\n"
        )
        strict_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "你是题目质量总评专家，只输出标签化结构文本，且必须返回完整字段。"),
                ("human", strict_human),
            ]
        )
        strict_payload = strict_prompt.invoke({})
        strict_raw = client.invoke_text(strict_payload)
        strict_data = _parse_quality_structured_text(strict_raw)
        strict_reasons = [str(x).strip() for x in (strict_data.get("quality_reasons") or []) if str(x).strip()]
        strict_basis = str(strict_data.get("scoring_basis", "") or "").strip()
        strict_dim_feedback: dict[str, str] = {}
        strict_raw_dim = strict_data.get("dimension_feedback")
        if isinstance(strict_raw_dim, dict):
            for k, v in strict_raw_dim.items():
                kk = str(k or "").strip()
                vv = str(v or "").strip()
                if kk and vv:
                    strict_dim_feedback[kk] = vv
        if strict_reasons:
            reasons = strict_reasons[:6]
        if strict_basis:
            basis = strict_basis
        if strict_dim_feedback:
            dim_feedback = strict_dim_feedback
    if (not reasons) or (not basis):
        obs = get_observability()
        raw_preview = str(obs.get("last_raw_response", "") or "").strip()
        raise RuntimeError(
            "quality_score 评估失败：模型未返回完整结论字段（quality_reasons/scoring_basis 为空）"
            + (f"，raw_preview={raw_preview[:500]}" if raw_preview else "")
        )
    return round(score, 2), reasons[:6], basis, dim_feedback


def _normalize_true_false_answer(raw_answer: str, options: list[str]) -> str:
    """将判断题答案统一归一为“正确/错误”。

    兼容答案字段为 A/B 或 正确/错误 两种口径。
    """
    ans = (raw_answer or "").strip().upper()
    if ans in {"正确", "错误"}:
        return ans
    if ans not in {"A", "B"}:
        return ans
    if len(options) < 2:
        return ans
    a_txt = _strip_option_prefix(options[0]).strip()
    b_txt = _strip_option_prefix(options[1]).strip()
    # 常见映射：A=正确，B=错误；若题面相反则按题面反向映射
    if "正确" in a_txt and "错误" in b_txt:
        return "正确" if ans == "A" else "错误"
    if "错误" in a_txt and "正确" in b_txt:
        return "错误" if ans == "A" else "正确"
    # 无法从选项语义推断时回传原值，后续会触发不一致提示
    return ans


def _basic_rules_code_checks(question: QuestionInput) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    stem = str(question.stem or "").strip()
    media_pattern = r"!\[[^\]]*\]\([^)]+\)|<img\b|\.png\b|\.jpg\b|\.jpeg\b|\.gif\b"
    table_pattern = r"^\s*\|.+\|\s*$|<table\b"

    # 0) 题干字数校验
    stem_len = len(re.sub(r"\s+", "", stem))
    if stem_len > 400:
        errors.append(f"题干字数超限：当前约{stem_len}字，最大允许400字")
    # 简练度提醒（warning）
    if stem_len > 120:
        warnings.append(f"题干偏长：当前约{stem_len}字，建议控制在120字以内并保持简练")
    # 冗余连接词密度（仅做提醒，不做拦截）
    connector_terms = ["并且", "且", "同时", "另外", "此外", "然后", "接着", "并", "及", "以及", "从而", "其中", "也就是"]
    connector_hits = sum(stem.count(t) for t in connector_terms)
    connector_density = connector_hits / max(stem_len, 1)
    if connector_hits >= 4 and connector_density >= 0.04:
        warnings.append(
            f"题干疑似冗余：连接词出现{connector_hits}次（密度{connector_density:.2f}），建议精简表述"
        )

    # 0.1) 年份约束（降级）：若教材主切片+关联切片不含年份，题干/选项/解析出现公历年份时仅给复核提醒
    kb_text = "\n".join(
        [str(question.textbook_slice or "")]
        + [str(x or "") for x in (question.related_slices or [])]
    )
    if not _has_year(kb_text):
        year_violations = [t for t in _collect_year_guard_text_fields(question) if _has_year(t)]
        if year_violations:
            warnings.append("【年份约束复核】题干/选项/解析出现公历年份（原文未提及），需提供教材证据支持")

    # 1) 选择题设问推荐表述（仅作建议，不强制固定结尾）
    if question.question_type == "single_choice":
        single_patterns = (
            r"以下表述正确的是（[ \u3000]+）。",
            r"以下表述错误的是（[ \u3000]+）。",
        )
        if not any(re.search(p + r"$", stem) for p in single_patterns):
            warnings.append(
                "单选题设问推荐以“以下表述正确的是（ ）。”或“以下表述错误的是（ ）。”结尾（全角中文括号，且括号内有空格）；其他陈述式+占位括号亦可"
            )
    elif question.question_type == "multiple_choice":
        multi_patterns = (
            r"以下表述正确的有（[ \u3000]+）。",
            r"以下表述正确的包括（[ \u3000]+）。",
            r"以下表述错误的有（[ \u3000]+）。",
            r"以下表述错误的包括（[ \u3000]+）。",
        )
        if not any(re.search(p + r"$", stem) for p in multi_patterns):
            warnings.append(
                "多选题设问推荐以“以下表述正确/错误的有/包括（ ）。”结尾（全角中文括号，且括号内有空格）；其他陈述式+占位括号亦可"
            )
    # 1.1) 少用否定方式：双重否定易歧义
    _double_neg = ["不是不", "并非不", "没有不", "不能不", "不可不", "不会不", "不应不", "不得不"]
    if any(p in stem for p in _double_neg):
        warnings.append("设问建议少用否定方式：题干含双重否定表述，建议改为陈述式")
    # 2) 判断题行为/说法模板与双重否定，改由第3层LLM做语义判定。

    # 1.5) 题干括号不能在句首（所有题型）
    if re.match(r"^\s*[（(]", stem):
        errors.append("题干括号不能在句首")

    # 3) 括号与位置（只看题干结尾，中间括号忽略；必须全角中文括号且括号内有空格）
    if question.question_type in {"single_choice", "multiple_choice"}:
        if not re.search(r"（[ \u3000]+）。$", stem):
            errors.append("选择题括号位置错误：必须以“（ ）。”形式结尾（全角中文括号，且括号内必须有空格）")
    elif question.question_type == "true_false":
        if not re.search(r"。 （[ \u3000]+）$", stem) and not re.search(r"。（[ \u3000]+）$", stem):
            errors.append("判断题括号位置错误：必须以“。（ ）”形式结尾（全角中文括号，且括号内必须有空格）")

    # 4) 题干/选项禁单引号
    texts = [("题干", question.stem)] + [
        (f"选项{chr(65+i)}", x) for i, x in enumerate(question.options)
    ]
    for src, txt in texts:
        if any(ch in txt for ch in ("'", "‘", "’")):
            errors.append(f"{src}中使用了单引号，应统一使用双引号")

    # 4.1) 题干/选项禁止图片与表格
    for src, txt in texts:
        txt_str = str(txt or "")
        if re.search(media_pattern, txt_str, flags=re.IGNORECASE):
            errors.append(f"{src}中禁止出现图片")
        if re.search(table_pattern, txt_str, flags=re.IGNORECASE | re.MULTILINE):
            errors.append(f"{src}中禁止出现表格")

    # 5) 选项末尾禁标点
    ending_punc = re.compile(r"[。，、；：！？.!?,;:!?]$")
    for i, opt in enumerate(question.options):
        content = _strip_option_prefix(opt)
        opt_len = len(re.sub(r"\s+", "", content))
        if opt_len > 200:
            errors.append(f"选项{chr(65+i)}字数超限：当前约{opt_len}字，最大允许200字")
        if content and ending_punc.search(content):
            errors.append(f"选项{chr(65+i)}结尾禁止使用标点符号")

    # 6) 禁“以上都对/都错/皆是/皆非”
    forbidden_opts = ["以上都对", "以上都错", "以上皆是", "以上皆非", "以上选项全对", "以上选项全错"]
    for i, opt in enumerate(question.options):
        content = _strip_option_prefix(opt)
        if any(k in content for k in forbidden_opts):
            errors.append(f"选项{chr(65+i)}包含违禁兜底表述")

    # 7) 选项数量与内容完整性
    expected_count = 2 if question.question_type == "true_false" else 4
    if len(question.options) != expected_count:
        if question.question_type == "true_false":
            errors.append("判断题选项必须为2个且对应A/B")
        else:
            errors.append("选择题选项必须为4个且对应A/B/C/D")
    else:
        for i, opt in enumerate(question.options):
            if not str(opt or "").strip():
                if question.question_type == "true_false":
                    errors.append(f"选项{chr(65+i)}为空，判断题选项必须A/B完整")
                else:
                    errors.append(f"选项{chr(65+i)}为空，选项必须A/B/C/D完整")
                continue
            # 规则：选项内容前面不允许再写 A/B/C/D 标签，避免与槽位重复
            if re.match(r"^\s*[A-Da-d][\.．、]\s*", str(opt)):
                errors.append(f"选项{chr(65+i)}前禁止填写A/B/C/D标签，请仅填写选项内容")

    # 8) 数值选项升序
    if question.question_type != "true_false":
        numeric_vals: list[float] = []
        for opt in question.options:
            content = _strip_option_prefix(opt)
            m = re.search(r"(-?\d+(?:\.\d+)?)", content)
            if not m:
                numeric_vals = []
                break
            numeric_vals.append(float(m.group(1)))
        if len(numeric_vals) == 4 and numeric_vals != sorted(numeric_vals):
            warnings.append("数值选项建议按从小到大升序排列")

    # 附：选项长度均衡性（warning）
    normalized = [_strip_option_prefix(x) for x in question.options if str(x or "").strip()]
    if len(normalized) >= 2:
        lengths = [len(x) for x in normalized]
        if max(lengths) - min(lengths) >= 15:
            warnings.append("选项长度不均衡：最长与最短字数差超过15，可能影响测量公平性")

    # 9) 答案字段合法
    raw_answer = str(question.correct_answer or "").strip().upper()
    tokens = [x for x in re.split(r"[，,、\s]+", raw_answer) if x]
    if question.question_type == "single_choice":
        # 显式硬规则：单选题必须且仅能有一个正确答案
        compact = raw_answer.replace("，", ",").replace("、", ",").replace(" ", "")
        multi_like = (
            ("," in compact)
            or bool(re.fullmatch(r"[A-D]{2,}", compact))
        )
        if multi_like:
            errors.append("单选题答案不合法：只能有一个正确答案，禁止填写多个答案")
        if len(tokens) != 1 or tokens[0] not in {"A", "B", "C", "D"}:
            errors.append("答案字段不合法：单选题答案必须为 A/B/C/D 之一")
    elif question.question_type == "multiple_choice":
        if len(tokens) < 2 or any(t not in {"A", "B", "C", "D"} for t in tokens):
            errors.append("答案字段不合法：多选题答案必须由 A/B/C/D 组成（如 A,B）")
    elif question.question_type == "true_false":
        if raw_answer not in {"A", "B"}:
            errors.append("答案字段不合法：判断题答案必须为 A/B")

    # 10) 解析三段论标题是否齐全
    exp = str(question.explanation or "")
    # 10.-1) 仅检查“教材原文”段落字数：超过400字给出提醒（整体解析不设上限）
    textbook_head = re.search(r"(^|\n)\s*1[\.、]\s*教材原文(?:\s*[：:])?", exp, flags=re.MULTILINE)
    analysis_head = re.search(r"(^|\n)\s*2[\.、]\s*试题分析(?:\s*[：:])?", exp, flags=re.MULTILINE)
    if textbook_head:
        textbook_start = textbook_head.end()
        textbook_end = analysis_head.start() if analysis_head and analysis_head.start() > textbook_start else len(exp)
        textbook_raw_text = exp[textbook_start:textbook_end]
        textbook_raw_len = len(re.sub(r"\s+", "", textbook_raw_text))
        if textbook_raw_len > 400:
            warnings.append(f"教材原文字数偏长：当前约{textbook_raw_len}字，建议控制在400字以内")
    # 10.0) 解析中禁止表格/图片
    if re.search(media_pattern, exp, flags=re.IGNORECASE):
        errors.append("解析中禁止出现图片")
    if re.search(table_pattern, exp, flags=re.IGNORECASE | re.MULTILINE):
        errors.append("解析中禁止出现表格")

    section_rules = [
        ("1.教材原文", r"(^|\n)\s*1[\.、]\s*教材原文(?:\s*[：:])?"),
        ("2.试题分析", r"(^|\n)\s*2[\.、]\s*试题分析(?:\s*[：:])?"),
        ("3.结论", r"(^|\n)\s*3[\.、]\s*结论(?:\s*[：:])?"),
    ]
    for section_name, pattern in section_rules:
        if not re.search(pattern, exp, flags=re.MULTILINE):
            errors.append(f"解析缺少结构段落：{section_name}")

    # “三段论语义完整性”已解耦至教学复盘节点，此处仅保留结构存在性检查。

    # 11) 解析结论与答案字段一致
    ans_norm = raw_answer.replace("，", ",").replace("、", ",").replace(" ", "")
    if question.question_type == "true_false":
        ans_norm = _normalize_true_false_answer(ans_norm, question.options)
    elif ans_norm not in {"正确", "错误"}:
        parts = sorted(set([x for x in ans_norm.split(",") if x]))
        ans_norm = ",".join(parts)
    m = re.search(
        r"本题答案为\s*([A-D](?:\s*[、,，]\s*[A-D])*|正确|错误)\s*[。．.]?$",
        exp.strip().upper(),
    )
    if not m:
        errors.append("解析结论缺失或格式不规范：应包含“本题答案为...”")
    else:
        exp_ans = m.group(1).replace("，", ",").replace("、", ",").replace(" ", "")
        if question.question_type == "true_false":
            # PPT规范：判断题结论必须是“正确/错误”，不能写“A/B”
            if exp_ans in {"A", "B"}:
                errors.append("判断题解析结论不规范：必须写“本题答案为正确/错误”，不能写“A/B”")
            exp_ans = _normalize_true_false_answer(exp_ans, question.options)
        elif exp_ans not in {"正确", "错误"}:
            exp_parts = sorted(set([x for x in exp_ans.split(",") if x]))
            exp_ans = ",".join(exp_parts)
        if exp_ans != ans_norm:
            errors.append("解析结论与正确答案字段不一致")

    # 12) 数值题/隐含运算题“保留位数说明”
    # 若为计算题或非计算题但存在隐含运算，且选项出现小数，题干应说明保留位数
    if question.is_calculation or _question_has_implicit_calculation(question):
        opt_text = " ".join(_strip_option_prefix(x) for x in question.options)
        has_decimal_option = bool(re.search(r"\d+\.\d+", opt_text))
        if has_decimal_option and not re.search(r"(保留到?\s*\d+\s*位小数|精确到?\s*\d+\s*位小数)", stem):
            errors.append("数值题或隐含运算题缺少“保留位数说明”：出现小数结果时需在题干标注保留到几位小数")

    return errors, warnings


def _llm_layer1_gatekeeper(
    question: QuestionInput,
    llm: Any,
) -> dict[str, Any]:
    if not llm:
        return {
            "passed": True,
            "errors": [],
            "warnings": ["LLM 未配置，跳过第3层校验"],
            "ask_pattern_still_invalid": False,
            "substitution_still_invalid": False,
            "name_rule_still_invalid": False,
            "name_unnecessary_but_used": False,
            "name_length_nonideal": False,
            "rare_character_name_risk": False,
            "tf_definition_style_valid": True,
            "option_unit_still_invalid": False,
            "negation_semantic_invalid": False,
            "redundancy_semantic_warning": False,
            "wording_semantic_invalid": False,
            "ask_judgement_evidence": "LLM 未配置",
            "substitution_evidence": [],
            "name_rule_evidence": [],
            "name_unnecessary_evidence": [],
            "name_length_evidence": [],
            "rare_character_name_evidence": [],
            "tf_definition_style_evidence": [],
            "option_unit_evidence": [],
            "negation_semantic_evidence": [],
            "redundancy_semantic_evidence": [],
            "wording_semantic_evidence": [],
        }

    default_system_prompt = "你是房地产考试命题终审员。只做语义仲裁，不做改写。"
    default_human_prompt = (
        "题型：{question_type}\n评估类型：{assessment_type}\n教材切片：{textbook_slice}\n"
        "关联切片：{related_slices}\n参考切片：{reference_slices}\n题干：{stem}\n选项：{options}\n"
        "标准答案：{correct_answer}\n解析：{explanation}"
    )
    system_prompt, human_prompt = load_prompt_pair(
        "prompts/layer3_basic_rules_gate.md",
        default_system_prompt,
        default_human_prompt,
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
            "question_type": question.question_type,
            "assessment_type": question.assessment_type,
            "textbook_slice": question.textbook_slice,
            "related_slices": _related_slices_text(question),
            "reference_slices": _reference_slices_text(question),
            "stem": question.stem,
            "options": "\n".join(question.options),
            "correct_answer": question.correct_answer,
            "explanation": question.explanation,
        }
    )
    client = ReliableLLMClient(llm, timeout_seconds=180, retries=2)
    raw = client.invoke_json(
        payload,
        fallback={
            "passed": True,
            "errors": [],
            "warnings": [],
            "ask_pattern_still_invalid": False,
            "substitution_still_invalid": False,
            "name_rule_still_invalid": False,
            "name_unnecessary_but_used": False,
            "name_length_nonideal": False,
            "rare_character_name_risk": False,
            "tf_definition_style_valid": True,
            "option_unit_still_invalid": False,
            "negation_semantic_invalid": False,
            "redundancy_semantic_warning": False,
            "wording_semantic_invalid": False,
            "option_subject_consistency_still_invalid": False,
            "ask_judgement_evidence": "模型未返回可解析结果",
            "substitution_evidence": [],
            "name_rule_evidence": [],
            "name_unnecessary_evidence": [],
            "name_length_evidence": [],
            "rare_character_name_evidence": [],
            "tf_definition_style_evidence": [],
            "option_unit_evidence": [],
            "negation_semantic_evidence": [],
            "redundancy_semantic_evidence": [],
            "wording_semantic_evidence": [],
            "option_subject_consistency_evidence": [],
        },
    )
    return _normalize_layer1_gatekeeper_output(raw)


def _normalize_layer1_gatekeeper_output(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize layer1 gatekeeper output to stable schema + narrative."""
    schema = raw.get("schema") if isinstance(raw.get("schema"), dict) else raw
    narrative = raw.get("narrative") if isinstance(raw.get("narrative"), dict) else {}
    fallback_errors = [str(x) for x in (raw.get("errors") or [])]
    fallback_warnings = [str(x) for x in (raw.get("warnings") or [])]
    atomic_checks = schema.get("atomic_checks") if isinstance(schema.get("atomic_checks"), list) else []
    category_summary = schema.get("category_summary") if isinstance(schema.get("category_summary"), dict) else {}

    def _b(key: str, default: bool = False) -> bool:
        return bool(schema.get(key, default))

    def _ls(key: str) -> list[str]:
        return [str(x) for x in (schema.get(key) or [])]

    # Atomic schema extraction for deterministic machine decisions
    atomic_issue_flags: dict[str, bool] = {}
    atomic_evidence: dict[str, list[str]] = {}
    atomic_errors: list[str] = []
    atomic_warnings: list[str] = []
    for item in atomic_checks:
        if not isinstance(item, dict):
            continue
        issue_key = str(item.get("issue_key", "")).strip()
        passed = bool(item.get("passed", True))
        level = str(item.get("level", "warning")).strip().lower()
        message = str(item.get("message", "")).strip()
        evidence = [str(x) for x in (item.get("evidence") or []) if str(x).strip()]
        if issue_key:
            # tf_definition_style_valid is a positive flag; others are issue flags.
            atomic_issue_flags[issue_key] = passed if issue_key == "tf_definition_style_valid" else (not passed)
            if evidence:
                atomic_evidence[issue_key] = evidence
        if not passed and message:
            if level == "error":
                atomic_errors.append(message)
            else:
                atomic_warnings.append(message)

    out = {
        "passed": _b("passed", True),
        "ask_pattern_still_invalid": bool(atomic_issue_flags.get("ask_pattern_still_invalid", _b("ask_pattern_still_invalid"))),
        "substitution_still_invalid": bool(atomic_issue_flags.get("substitution_still_invalid", _b("substitution_still_invalid"))),
        "name_rule_still_invalid": bool(atomic_issue_flags.get("name_rule_still_invalid", _b("name_rule_still_invalid"))),
        "name_unnecessary_but_used": bool(atomic_issue_flags.get("name_unnecessary_but_used", _b("name_unnecessary_but_used"))),
        "name_length_nonideal": bool(atomic_issue_flags.get("name_length_nonideal", _b("name_length_nonideal"))),
        "rare_character_name_risk": bool(atomic_issue_flags.get("rare_character_name_risk", _b("rare_character_name_risk"))),
        "tf_definition_style_valid": bool(atomic_issue_flags.get("tf_definition_style_valid", schema.get("tf_definition_style_valid", True))),
        "option_unit_still_invalid": bool(atomic_issue_flags.get("option_unit_still_invalid", _b("option_unit_still_invalid"))),
        "negation_semantic_invalid": bool(atomic_issue_flags.get("negation_semantic_invalid", _b("negation_semantic_invalid"))),
        "redundancy_semantic_warning": bool(atomic_issue_flags.get("redundancy_semantic_warning", _b("redundancy_semantic_warning"))),
        "wording_semantic_invalid": bool(atomic_issue_flags.get("wording_semantic_invalid", _b("wording_semantic_invalid"))),
        "ask_judgement_evidence": str(schema.get("ask_judgement_evidence", "") or ""),
        "substitution_evidence": atomic_evidence.get("substitution_still_invalid", _ls("substitution_evidence")),
        "name_rule_evidence": atomic_evidence.get("name_rule_still_invalid", _ls("name_rule_evidence")),
        "name_unnecessary_evidence": atomic_evidence.get("name_unnecessary_but_used", _ls("name_unnecessary_evidence")),
        "name_length_evidence": atomic_evidence.get("name_length_nonideal", _ls("name_length_evidence")),
        "rare_character_name_evidence": atomic_evidence.get("rare_character_name_risk", _ls("rare_character_name_evidence")),
        "tf_definition_style_evidence": atomic_evidence.get("tf_definition_style_valid", _ls("tf_definition_style_evidence")),
        "option_unit_evidence": atomic_evidence.get("option_unit_still_invalid", _ls("option_unit_evidence")),
        "negation_semantic_evidence": atomic_evidence.get("negation_semantic_invalid", _ls("negation_semantic_evidence")),
        "redundancy_semantic_evidence": atomic_evidence.get("redundancy_semantic_warning", _ls("redundancy_semantic_evidence")),
        "wording_semantic_evidence": atomic_evidence.get("wording_semantic_invalid", _ls("wording_semantic_evidence")),
        "option_subject_consistency_still_invalid": bool(atomic_issue_flags.get("option_subject_consistency_still_invalid", _b("option_subject_consistency_still_invalid"))),
        "option_subject_consistency_evidence": atomic_evidence.get("option_subject_consistency_still_invalid", _ls("option_subject_consistency_evidence")),
        "category_summary": category_summary,
        "_schema_errors": atomic_errors,
        "_schema_warnings": atomic_warnings,
        "_narrative_errors": [str(x) for x in (narrative.get("errors") or fallback_errors)],
        "_narrative_warnings": [str(x) for x in (narrative.get("warnings") or fallback_warnings)],
        "_narrative_summary": str(narrative.get("summary", "") or ""),
    }
    return out


def node_layer3_basic_rules_gate(state: JudgeState) -> JudgeState:
    def _merge_unique(base: list[str], extra: list[str]) -> list[str]:
        seen = set(base)
        out = list(base)
        for item in extra:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    existing_errors = list(state.get("hard_rule_errors", []))
    existing_warnings = list(state.get("hard_rule_warnings", []))

    code_errors, code_warnings = _basic_rules_code_checks(state["question"])
    data = _llm_layer1_gatekeeper(state["question"], state.get("llm"))
    # 决策仅依赖 schema 字段；narrative 仅用于展示。
    llm_errors: list[str] = []
    llm_warnings: list[str] = []
    llm_warnings.extend([f"【L1Schema提醒】{x}" for x in (data.get("_schema_warnings") or [])[:8]])
    llm_errors.extend([f"【L1Schema】{x}" for x in (data.get("_schema_errors") or [])[:8]])

    # narrative 展示文本：过滤格式类幻觉，避免污染展示结果
    format_patterns = [
        "标点与格式",
        "括号",
        "单引号",
        "选项未按规范标注",
        "A/B/C/D",
        "答案字段",
        "句号",
    ]
    narrative_errors = [
        str(e) for e in (data.get("_narrative_errors") or [])
        if not any(p in str(e) for p in format_patterns)
    ]
    narrative_warnings = [
        str(w) for w in (data.get("_narrative_warnings") or [])
        if not any(p in str(w) for p in format_patterns)
    ]

    if narrative_errors:
        llm_warnings.extend([f"【L1语义说明】{x}" for x in narrative_errors[:5]])
    if narrative_warnings:
        llm_warnings.extend([f"【L1语义提醒】{x}" for x in narrative_warnings[:5]])
    if data.get("_narrative_summary"):
        llm_warnings.append(f"【L1语义总结】{data.get('_narrative_summary')}")

    if data.get("substitution_still_invalid"):
        llm_errors.append("选项代入题干后语句不通顺或语义不完整（LLM判定）")
    if data.get("ask_pattern_still_invalid"):
        llm_errors.append("设问句式不规范（LLM判定）")
    if data.get("name_rule_still_invalid"):
        llm_errors.append("姓名命名不规范：存在恶搞名或明显伦理冲突命名（LLM判定）")
    if data.get("name_unnecessary_but_used"):
        llm_warnings.append("题目存在非必要命名：本可不命名但使用了姓名（LLM判定）")
    if data.get("name_length_nonideal"):
        llm_warnings.append("姓名长度不理想：建议使用2~3字通俗姓名（LLM判定）")
    if data.get("rare_character_name_risk"):
        llm_warnings.append("姓名存在生僻字风险：建议使用通俗常见姓名（LLM判定）")
    if data.get("tf_definition_style_valid") is False:
        llm_errors.append("判断题句式语义不成立：定义类/行为类模板使用不当（LLM判定）")
    if data.get("option_unit_still_invalid"):
        llm_errors.append("选项包含单位，不符合“单位应上提题干”要求（LLM判定）")
    if data.get("negation_semantic_invalid"):
        llm_errors.append("设问语义存在否定歧义或误导（LLM判定）")
    if data.get("redundancy_semantic_warning"):
        llm_warnings.append("题干语义存在冗余表达，建议精简（LLM判定）")
    if data.get("wording_semantic_invalid"):
        llm_errors.append("题干遣词造句语义不准确：存在主谓搭配或指代对象错误（LLM判定）")
    if data.get("option_subject_consistency_still_invalid"):
        llm_errors.append("选项称谓/主体与题干不一致（维度6，LLM判定）")

    errors = _merge_unique(existing_errors, code_errors + llm_errors)
    warnings = _merge_unique(existing_warnings, code_warnings + llm_warnings)

    return {
        "hard_rule_errors": errors,
        "hard_rule_warnings": warnings,
        # 第3层结果：是否存在基础校验错误（仅记录，不做门禁）
        "hard_rule_has_errors": (len(errors) > 0),
        "gate_recheck_data": data,
    }


def node_layer1_blind_solver(state: JudgeState) -> JudgeState:
    q = state["question"]
    llm = state.get("llm")
    if not llm:
        return {
            "solver_validation": SolverValidation(
                predicted_answer="",
                reasoning_path="LLM 未配置",
                ambiguity_flag=True,
            ),
            "solver_semantic_drift": SemanticDrift(),
            "solver_issues": ["【LLM未配置】无法执行盲答检测"],
            "drift_issues": [],
            "solver_calc_data": {},
            "ran_blind_solver": True,
        }

    solver, drift, solver_issues, drift_issues, solver_calc_data = layer1_blind_solver_agent(q, llm)
    return {
        "solver_validation": solver,
        "solver_semantic_drift": drift,
        "solver_issues": solver_issues,
        "drift_issues": drift_issues,
        "solver_calc_data": solver_calc_data,
        "ran_blind_solver": True,
    }


def node_layer2_knowledge_gate(state: JudgeState) -> JudgeState:
    """第2层：知识边界快筛守门员（命中超纲/冲突直接短路）。"""
    if not state.get("llm"):
        return {
            "knowledge_issues": [],
            "knowledge_data": {},
            "knowledge_gate_reject": False,
            "knowledge_gate_reasons": [],
            "knowledge_semantic_drift": SemanticDrift(),
            "ran_knowledge_gate": True,
        }
    knowledge_issues, details = layer2_knowledge_gate_agent(state["question"], state["llm"])
    prefixed_issues = [f"【知识匹配】{x}" for x in knowledge_issues]
    gate_reject = bool(details.get("out_of_scope_hard", False)) or bool(details.get("constraint_drift_hard", False))
    gate_reasons: list[str] = []
    if gate_reject:
        gate_reasons.append("【知识边界短路】命中超纲或教材冲突（证据充分），直接打回重做")
        for ev in (details.get("short_circuit_evidence_chain") or []):
            txt = str(ev or "").strip()
            if txt:
                gate_reasons.append(f"【知识边界短路-证据链】{txt}")
    elif bool(details.get("out_of_scope", False)) or bool(details.get("constraint_drift", False)):
        gate_reasons.append("【知识边界复核】命中风险但证据不足，降级REVIEW并继续后续链路")
    return {
        "knowledge_issues": prefixed_issues,
        "knowledge_data": details,
        "knowledge_gate_reject": gate_reject,
        "knowledge_gate_reasons": gate_reasons,
        "knowledge_semantic_drift": SemanticDrift(
            fingerprint_matched=not bool(details.get("out_of_scope", False)),
            rule_constraints_kept=not bool(details.get("constraint_drift", False)),
            limit_words_consistent=not bool(details.get("constraint_drift", False)),
        ),
        "ran_knowledge_gate": True,
    }


def node_layer3_surface_a(state: JudgeState) -> JudgeState:
    """第3层并行节点A：题面综合质检（实操/概念双提示词）。"""
    if not state.get("llm"):
        return {
            "realism_score": 3.0,
            "realism_issues": [],
            "realism_data": {"passed": True},
            "risk_assessment": RiskAssessment(),
            "rigor_data": {},
            "rigor_issues": [],
            "rigor_warnings": [],
            "distractor_quality": DistractorQuality(),
            "distractor_issues": [],
            "distractor_data": {},
            "ran_surface_a": True,
        }

    prompt_path = (
        "prompts/layer3_surface_quality_practical.md"
        if state["question"].assessment_type == "实战应用/推演"
        else "prompts/layer3_surface_quality_concept.md"
    )
    default_system = "你是房地产考试题面综合质检专家。"
    default_human = (
        "题型：{question_type}\n评估类型：{assessment_type}\n教材切片：{textbook_slice}\n关联切片：{related_slices}\n"
        "参考切片：{reference_slices}\n"
        "题干：{stem}\n选项：{options}\n标准答案：{correct_answer}\n解析：{explanation}"
    )
    system_prompt, human_prompt = load_prompt_pair(
        prompt_path,
        default_system,
        default_human,
        [
            "assessment_type",
            "question_type",
            "textbook_slice",
            "related_slices",
            "reference_slices",
            "stem",
            "options",
            "correct_answer",
            "explanation",
        ],
    )
    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", human_prompt)])
    payload = prompt.invoke(
        {
            "assessment_type": state["question"].assessment_type,
            "question_type": state["question"].question_type,
            "textbook_slice": state["question"].textbook_slice,
            "related_slices": _related_slices_text(state["question"]),
            "reference_slices": _reference_slices_text(state["question"]),
            "stem": state["question"].stem,
            "options": "\n".join(state["question"].options),
            "correct_answer": state["question"].correct_answer,
            "explanation": state["question"].explanation,
        }
    )
    client = ReliableLLMClient(state["llm"], timeout_seconds=180, retries=2)
    data = client.invoke_json(
        payload,
        fallback={
            "business_realism": {
                "passed": True,
                "issues": [],
                "score": 3,
                "slice_conflict_invalid": False,
                "slice_conflict_issues": [],
                "scene_binding_required_violation": False,
                "workflow_sequence_violation": False,
                "scenario_dialogue_or_objection": False,
                "negative_emotion_detected": False,
                "contains_business_action": True,
                "business_action_types": [],
                "backbook_style_answer": False,
                "amplifies_defect_without_remedy": False,
                "high_risk_domain_triggered": False,
                "high_risk_domains": [],
                "subjective_replaces_objective": False,
                "oral_replaces_written": False,
                "over_authority_conclusion": False,
                "bypass_compliance_process": False,
                "uses_authoritative_evidence": True,
                "introduces_professional_third_party": True,
                "follows_compliance_sop": True,
                "competing_truth_violation": False,
                "competing_truth_issues": [],
                "non_discriminative_stem_risk": False,
                "non_discriminative_stem_issues": [],
            },
            "rigor": {
                "leakage_still_invalid": False,
                "explanation_conflict_still_invalid": False,
                "name_consistency_still_invalid": False,
                "legal_math_closure_invalid": False,
                "term_mismatch_issues": [],
                "issues": [],
            },
            "distractor": {
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
        },
    )

    realism = data.get("business_realism") or {}
    rigor = data.get("rigor") or {}
    dtr = data.get("distractor") or {}
    dtr_quality = dtr.get("distractor_quality") or {}
    term_mismatch_issues: list[dict[str, str]] = []
    for item in (rigor.get("term_mismatch_issues") or []):
        if isinstance(item, dict):
            term_mismatch_issues.append(
                {
                    "raw_term": str(item.get("raw_term", "")).strip(),
                    "suggested_term": str(item.get("suggested_term", "")).strip() or "教材标准术语",
                    "location": str(item.get("location", "")).strip() or "未知位置",
                    "source": str(item.get("source", "")).strip() or "llm_inferred",
                }
            )

    rigor_issues = [f"【严谨性】{x}" for x in (rigor.get("issues") or [])]
    for t in term_mismatch_issues:
        if t.get("raw_term"):
            rigor_issues.append(
                f"【严谨性】用词不规范：出现口语“{t.get('raw_term','')}”，建议“{t.get('suggested_term','')}”（{t.get('location','未知位置')}）"
            )

    risk_level = RiskLevel.LOW
    if bool(rigor.get("leakage_still_invalid", False)):
        risk_level = RiskLevel.HIGH
    elif bool(rigor.get("name_consistency_still_invalid", False)) or bool(rigor.get("explanation_conflict_still_invalid", False)):
        risk_level = RiskLevel.MEDIUM

    realism_issues_prefixed = _dedupe_issues([f"【业务常识】{x}" for x in (realism.get("issues") or [])])
    realism_issues_raw = _dedupe_issues([str(x) for x in (realism.get("issues") or [])])

    return {
        "realism_score": float(realism.get("score", 3) or 3),
        "realism_issues": realism_issues_prefixed,
        "realism_data": {
            "passed": bool(realism.get("passed", True)),
            "issues": realism_issues_raw,
            "score": int(realism.get("score", 3) or 3),
            "slice_conflict_invalid": bool(realism.get("slice_conflict_invalid", False)),
            "slice_conflict_issues": [str(x) for x in (realism.get("slice_conflict_issues") or [])],
            "scene_binding_required_violation": bool(realism.get("scene_binding_required_violation", False)),
            "workflow_sequence_violation": bool(realism.get("workflow_sequence_violation", False)),
            "scenario_dialogue_or_objection": bool(realism.get("scenario_dialogue_or_objection", False)),
            "negative_emotion_detected": bool(realism.get("negative_emotion_detected", False)),
            "contains_business_action": bool(realism.get("contains_business_action", True)),
            "business_action_types": [str(x) for x in (realism.get("business_action_types") or []) if str(x).strip()],
            "backbook_style_answer": bool(realism.get("backbook_style_answer", False)),
            "amplifies_defect_without_remedy": bool(realism.get("amplifies_defect_without_remedy", False)),
            "high_risk_domain_triggered": bool(realism.get("high_risk_domain_triggered", False)),
            "high_risk_domains": [str(x) for x in (realism.get("high_risk_domains") or []) if str(x).strip()],
            "subjective_replaces_objective": bool(realism.get("subjective_replaces_objective", False)),
            "oral_replaces_written": bool(realism.get("oral_replaces_written", False)),
            "over_authority_conclusion": bool(realism.get("over_authority_conclusion", False)),
            "bypass_compliance_process": bool(realism.get("bypass_compliance_process", False)),
            "uses_authoritative_evidence": bool(realism.get("uses_authoritative_evidence", False)),
            "introduces_professional_third_party": bool(realism.get("introduces_professional_third_party", False)),
            "follows_compliance_sop": bool(realism.get("follows_compliance_sop", False)),
            "competing_truth_violation": bool(realism.get("competing_truth_violation", False)),
            "competing_truth_issues": [str(x) for x in (realism.get("competing_truth_issues") or []) if str(x).strip()],
            "non_discriminative_stem_risk": bool(realism.get("non_discriminative_stem_risk", False)),
            "non_discriminative_stem_issues": [
                str(x) for x in (realism.get("non_discriminative_stem_issues") or []) if str(x).strip()
            ],
        },
        "risk_assessment": RiskAssessment(
            risk_level=risk_level,
            policy_risk=False,
            legal_expression_risk=False,
            dispute_risk=bool(rigor.get("explanation_conflict_still_invalid", False)) or bool(rigor.get("leakage_still_invalid", False)),
            practice_conflict=False,
        ),
        "rigor_data": {
            "leakage_still_invalid": bool(rigor.get("leakage_still_invalid", False)),
            "explanation_conflict_still_invalid": bool(rigor.get("explanation_conflict_still_invalid", False)),
            "name_consistency_still_invalid": bool(rigor.get("name_consistency_still_invalid", False)),
            "legal_math_closure_invalid": bool(rigor.get("legal_math_closure_invalid", False)),
            "term_mismatch_issues": term_mismatch_issues,
            "issues": [str(x) for x in (rigor.get("issues") or [])],
            "warnings": [],
        },
        "rigor_issues": rigor_issues,
        "rigor_warnings": [],
        "distractor_quality": DistractorQuality(
            real_but_inapplicable=bool(dtr_quality.get("real_but_inapplicable", True)),
            format_aligned=bool(dtr_quality.get("format_aligned", True)),
            logic_homogenous=bool(dtr_quality.get("logic_homogenous", True)),
            balance_strength=bool(dtr_quality.get("balance_strength", True)),
        ),
        "distractor_issues": [f"【干扰项】{x}" for x in (dtr.get("issues") or [])],
        "distractor_data": {
            "unsupported_options": [str(x).strip().upper() for x in (dtr.get("unsupported_options") or []) if str(x).strip()],
            "why_unrelated": [str(x) for x in (dtr.get("why_unrelated") or [])],
            "overlap_pairs": [str(x) for x in (dtr.get("overlap_pairs") or [])],
            "stem_option_conflicts": [str(x) for x in (dtr.get("stem_option_conflicts") or [])],
            "mutual_exclusivity_fail": bool(dtr.get("mutual_exclusivity_fail", False)),
        },
        "ran_surface_a": True,
    }


def node_layer3_teaching_b(state: JudgeState) -> JudgeState:
    """第3层并行节点B：教学复盘评估（解析质量+教学价值）。"""
    if not state.get("llm"):
        return {
            "explanation_issues": [],
            "explanation_data": {},
            "teaching_value": TeachingValue(
                cognitive_level="应用"
                if state["question"].assessment_type == "实战应用/推演"
                else "理解",
                business_relevance="高"
                if state["question"].assessment_type == "实战应用/推演"
                else "一般",
                discrimination="中",
                estimated_pass_rate=0.62
                if state["question"].assessment_type == "实战应用/推演"
                else 0.78,
            ),
            "teaching_issues": [],
            "teaching_data": {},
            "ran_teaching_b": True,
        }
    default_system = "你是房地产考试教学复盘评估专家。"
    default_human = (
        "题型：{question_type}\n评估类型：{assessment_type}\n是否计算题：{is_calculation}\n教材切片：{textbook_slice}\n关联切片：{related_slices}\n"
        "参考切片：{reference_slices}\n"
        "题干：{stem}\n选项：{options}\n标准答案：{correct_answer}\n解析：{explanation}"
    )
    system_prompt, human_prompt = load_prompt_pair(
        "prompts/layer3_teaching_review.md",
        default_system,
        default_human,
        [
            "assessment_type",
            "question_type",
            "is_calculation",
            "textbook_slice",
            "related_slices",
            "reference_slices",
            "stem",
            "options",
            "correct_answer",
            "explanation",
        ],
    )
    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", human_prompt)])
    payload = prompt.invoke(
        {
            "assessment_type": state["question"].assessment_type,
            "question_type": state["question"].question_type,
            "is_calculation": "是" if state["question"].is_calculation else "否",
            "textbook_slice": state["question"].textbook_slice,
            "related_slices": _related_slices_text(state["question"]),
            "reference_slices": _reference_slices_text(state["question"]),
            "stem": state["question"].stem,
            "options": "\n".join(state["question"].options),
            "correct_answer": state["question"].correct_answer,
            "explanation": state["question"].explanation,
        }
    )
    client = ReliableLLMClient(state["llm"], timeout_seconds=180, retries=2)
    data = client.invoke_json(
        payload,
        fallback={
            "explanation_quality": {
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
                "issues": [],
            },
            "teaching_value": {
                "cognitive_level": "应用" if state["question"].assessment_type == "实战应用/推演" else "理解",
                "business_relevance": "高" if state["question"].assessment_type == "实战应用/推演" else "一般",
                "discrimination": "中",
                "estimated_pass_rate": 0.62 if state["question"].assessment_type == "实战应用/推演" else 0.78,
                "has_assessment_value": True,
                "main_assessment_points": [],
                "assessment_point_aligned": True,
                "assessment_point_issues": [],
                "assessment_value_issues": [],
                "issues": [],
            },
        },
    )
    exp = data.get("explanation_quality") or {}
    tv_data = data.get("teaching_value") or {}
    tv = TeachingValue(
        cognitive_level=str(tv_data.get("cognitive_level", "理解") or "理解"),
        business_relevance=str(tv_data.get("business_relevance", "一般") or "一般"),
        discrimination=str(tv_data.get("discrimination", "中") or "中"),
        estimated_pass_rate=float(tv_data.get("estimated_pass_rate", 0.7) or 0.7),
    )
    teaching_issues = [str(x) for x in (tv_data.get("issues") or [])]
    if tv_data.get("has_assessment_value") is False:
        av_issues = [str(x) for x in (tv_data.get("assessment_value_issues") or [])]
        teaching_issues.extend(av_issues or ["题目可作答但考察意义不足（疑似无效送分题）"])
        tv.discrimination = "低"
        tv.estimated_pass_rate = max(float(tv.estimated_pass_rate), 0.9)
    ap_issues = [str(x) for x in (tv_data.get("assessment_point_issues") or []) if str(x).strip()]
    for ap in ap_issues:
        teaching_issues.append(f"【考核点】{ap}")
    explanation_issues = [f"【解析质量】{x}" for x in (exp.get("issues") or [])]
    if state["question"].is_calculation and not bool(exp.get("non_answer_numeric_basis_sufficient", True)):
        explanation_issues.append("【解析质量】计算题解析未说明非答案数值选项为何不成立（维度6/7）")
    if state["question"].is_calculation and not bool(exp.get("explanation_calculation_consistent", True)):
        explanation_issues.append("【解析质量】解析与计算过程/题干条件不一致（维度7）")
    return {
        "explanation_issues": explanation_issues,
        "explanation_data": {
            "has_forbidden_media": bool(exp.get("has_forbidden_media", False)),
            "multi_option_coverage_rate": float(exp.get("multi_option_coverage_rate", 1.0) or 1.0),
            "missing_options": [str(x) for x in (exp.get("missing_options") or [])],
            "analysis_rewrite_sufficient": bool(exp.get("analysis_rewrite_sufficient", True)),
            "analysis_rewrite_issues": [str(x) for x in (exp.get("analysis_rewrite_issues") or [])],
            "three_part_is_clear_and_coherent": bool(exp.get("three_part_is_clear_and_coherent", True)),
            "three_part_semantic_invalid": bool(exp.get("three_part_semantic_invalid", False)),
            "three_part_semantic_evidence": [str(x) for x in (exp.get("three_part_semantic_evidence") or [])],
            "first_part_missing_target_title": bool(exp.get("first_part_missing_target_title", False)),
            "first_part_missing_level": bool(exp.get("first_part_missing_level", False)),
            "first_part_missing_textbook_raw": bool(exp.get("first_part_missing_textbook_raw", False)),
            "first_part_structured_issues": [str(x) for x in (exp.get("first_part_structured_issues") or [])],
            "theory_support_present": bool(exp.get("theory_support_present", True)),
            "theory_support_source": str(exp.get("theory_support_source", "") or "").strip(),
            "business_support_present": bool(exp.get("business_support_present", True)),
            "business_support_reason": str(exp.get("business_support_reason", "") or "").strip(),
            "non_answer_numeric_basis_sufficient": bool(exp.get("non_answer_numeric_basis_sufficient", True)),
            "explanation_calculation_consistent": bool(exp.get("explanation_calculation_consistent", True)),
            "issues_count": len(exp.get("issues") or []),
        },
        "teaching_value": tv,
        "teaching_issues": [f"【教学价值】{x}" for x in teaching_issues],
        "teaching_data": {
            "has_assessment_value": bool(tv_data.get("has_assessment_value", True)),
            "assessment_value_issues": [str(x) for x in (tv_data.get("assessment_value_issues") or [])],
            "main_assessment_points": [str(x) for x in (tv_data.get("main_assessment_points") or []) if str(x).strip()],
            "assessment_point_aligned": bool(tv_data.get("assessment_point_aligned", True)),
            "assessment_point_issues": [str(x) for x in (tv_data.get("assessment_point_issues") or []) if str(x).strip()],
        },
        "ran_teaching_b": True,
    }


def node_layer3_calc_branch(state: JudgeState) -> JudgeState:
    """并行分支：计算专项节点（code_evaluator + calculation_complexity）。"""
    q = state["question"]
    calc_required = bool(q.is_calculation) or _question_has_implicit_calculation(q)
    if not calc_required:
        return {
            "calculation_issues": [],
            "calculation_data": {"enabled": False, "code_evaluator_issues": []},
            "ran_calc_branch": True,
        }
    if not state.get("llm"):
        return {
            "calculation_issues": [],
            "calculation_data": {"enabled": False, "code_evaluator_issues": []},
            "ran_calc_branch": True,
        }
    default_system = "你是房地产计算题评估专家。"
    default_human = (
        "题型：{question_type}\n评估类型：{assessment_type}\n教材切片：{textbook_slice}\n关联切片：{related_slices}\n"
        "参考切片：{reference_slices}\n题干：{stem}\n选项：{options}\n标准答案：{correct_answer}\n解析：{explanation}"
    )
    system_prompt, human_prompt = load_prompt_pair(
        "prompts/layer3_calc_branch.md",
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
    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", human_prompt)])
    payload = prompt.invoke(
        {
            "question_type": q.question_type,
            "assessment_type": q.assessment_type,
            "textbook_slice": q.textbook_slice,
            "related_slices": _related_slices_text(q),
            "reference_slices": _reference_slices_text(q),
            "stem": q.stem,
            "options": "\n".join(q.options),
            "correct_answer": q.correct_answer,
            "explanation": q.explanation,
        }
    )
    client = ReliableLLMClient(state["llm"], timeout_seconds=180, retries=2)
    data = client.invoke_json(
        payload,
        fallback={
            "code_evaluator": {"issues": [], "evidence": [], "wrong_path_count": 0, "mapped_to_options": True},
            "complexity": {
                "is_calculation_question": True,
                "digit_complexity_too_high": False,
                "step_count_too_high": False,
                "complex_decimal_present": False,
                "mental_math_level": "需草算",
                "complexity_level": "中",
                "issues": [],
                "evidence": [],
            },
        },
    )
    code_eval = data.get("code_evaluator") or {}
    complexity = data.get("complexity") or {}
    code_eval_issues = [str(x) for x in (code_eval.get("issues") or [])]
    calc_issues = [str(x) for x in (complexity.get("issues") or [])]
    if bool(complexity.get("digit_complexity_too_high", False)):
        calc_issues.append("数字位数复杂度偏高，不利于快速作答（计算复杂度维度）")
    if bool(complexity.get("step_count_too_high", False)):
        calc_issues.append("计算步骤过多，推导链路过长（计算复杂度维度）")
    if bool(complexity.get("complex_decimal_present", False)):
        calc_issues.append("出现复杂小数，心算可行性较差（计算复杂度维度）")
    if str(complexity.get("mental_math_level", "")).strip() == "明显需计算器":
        calc_issues.append("该题明显需计算器，不符合“尽可能不用计算器”导向（计算复杂度维度）")
    # Code evidence arbitration: OK | SOFT | HARD | TOOL_FAIL
    wrong_path_count = int(code_eval.get("wrong_path_count", 0) or 0)
    mapped_to_options = bool(code_eval.get("mapped_to_options", True))
    tool_fail_hints = ("未返回", "可执行代码", "返回值必须", "必须为列表", "执行失败")
    is_tool_fail = any(
        any(h in str(i) for h in tool_fail_hints) or ("执行" in str(i) and "失败" in str(i))
        for i in code_eval_issues
    )
    code_vs_standard_conflict = any("正确计算值与正确选项不一致" in str(i) for i in code_eval_issues)
    hard_conflict = code_vs_standard_conflict or (not mapped_to_options and wrong_path_count > 0)
    if is_tool_fail:
        code_evidence_status = "TOOL_FAIL"
    elif hard_conflict:
        code_evidence_status = "HARD"
    elif wrong_path_count > 0 or len(code_eval_issues) > 0:
        code_evidence_status = "SOFT"
    else:
        code_evidence_status = "OK"
    code_evaluator_evidence_list = [str(x) for x in (code_eval.get("evidence") or [])]
    code_evidence_chain = code_evaluator_evidence_list + [
        "code_eval: " + str(x) for x in (code_eval_issues[:5] if code_eval_issues else [])
    ]

    # Separate LLM review: code complies with textbook
    code_check_passed = True
    code_check_reason = ""
    code_snippet = (data.get("code_snippet") or "").strip()
    if code_snippet:
        kb_context = str(q.textbook_slice or "").strip() + "\n" + _related_slices_text(q)
        blind_question = {
            "题干": q.stem,
            "选项": q.options,
            "标准答案": q.correct_answer,
        }
        code_check_prompt = f"""# 角色
你是严厉的审计人。请检查【计算代码】是否严格符合【教材规则】与【题干条件】。

# 教材规则
{kb_context}

# 题目
{json.dumps(blind_question, ensure_ascii=False)}

# 计算代码
{code_snippet}

# 要求
1. 判断代码是否严格遵守教材公式与判定条件。
2. 若不符合，指出关键错误点（例如漏判定条件、用错计税基础、用错阈值）。

# 输出 JSON
{{
  "code_valid": true/false,
  "code_reason": "不超过80字，说明是否符合规则"
}}
"""
        code_check_payload = [HumanMessage(content=code_check_prompt)]
        code_check_data = client.invoke_json(
            code_check_payload,
            fallback={"code_valid": True, "code_reason": ""},
        )
        code_check_passed = bool(code_check_data.get("code_valid", True))
        code_check_reason = str(code_check_data.get("code_reason", "") or "").strip()
        if not code_check_passed:
            code_evidence_status = "HARD"
            code_evidence_chain = code_evidence_chain + [f"code_check: {code_check_reason}"]

    return {
        "calculation_issues": [f"【计算干扰项】{x}" for x in code_eval_issues] + [f"【计算复杂度】{x}" for x in calc_issues],
        "calculation_data": {
            "enabled": True,
            "is_calculation_question": bool(complexity.get("is_calculation_question", True)),
            "digit_complexity_too_high": bool(complexity.get("digit_complexity_too_high", False)),
            "step_count_too_high": bool(complexity.get("step_count_too_high", False)),
            "complex_decimal_present": bool(complexity.get("complex_decimal_present", False)),
            "mental_math_level": str(complexity.get("mental_math_level", "需草算") or "需草算"),
            "complexity_level": str(complexity.get("complexity_level", "中") or "中"),
            "evidence": [str(x) for x in (complexity.get("evidence") or [])],
            "code_evaluator_issues": [str(x) for x in code_eval_issues],
            "code_evaluator_evidence": code_evaluator_evidence_list,
            "wrong_path_count": wrong_path_count,
            "mapped_to_options": mapped_to_options,
            "code_evidence_status": code_evidence_status,
            "code_evidence_chain": code_evidence_chain,
            "code_check_passed": code_check_passed,
            "code_check_reason": code_check_reason,
        },
        "ran_calc_branch": True,
    }


def _score_from_state(
    *,
    logic_score: float | None,
    knowledge_score: float | None,
    distractor_score: float | None,
    teaching_score: float | None,
    risk: RiskAssessment,
    ran_surface_a: bool,
    decision: Decision,
) -> Scores:
    """兼容旧字段 Scores（前端历史依赖），主评分以 dimension_results 为准。"""
    risk_score_map = {RiskLevel.LOW: 10, RiskLevel.MEDIUM: 7, RiskLevel.HIGH: 3}
    risk_score = risk_score_map.get(risk.risk_level, 10) if ran_surface_a else 0
    confidence = 0.95 if decision == Decision.PASS else 0.9
    return Scores(
        logic=int(round(float(logic_score or 0))),
        knowledge=int(round(float(knowledge_score or 0))),
        distractor=int(round(float(distractor_score or 0))),
        teaching=int(round(float(teaching_score or 0))),
        risk=risk_score,
        confidence=confidence,
    )


def node_aggregate(state: JudgeState) -> JudgeState:
    q = state["question"]
    warnings = state.get("hard_rule_warnings", [])
    errors = state.get("hard_rule_errors", [])
    solver = state.get("solver_validation") or SolverValidation()
    solver_semantic_drift = state.get("solver_semantic_drift") or SemanticDrift()
    knowledge_semantic_drift = state.get("knowledge_semantic_drift")
    rigor_data = state.get("rigor_data") or {}
    knowledge_data = state.get("knowledge_data") or {}
    dq = state.get("distractor_quality") or DistractorQuality()
    dq_data = state.get("distractor_data") or {}
    realism_data = state.get("realism_data") or {}
    tv = state.get("teaching_value") or TeachingValue()
    risk = state.get("risk_assessment") or RiskAssessment()
    teaching_issues = list(state.get("teaching_issues", []))
    teaching_data = state.get("teaching_data") or {}
    calculation_data = state.get("calculation_data") or {}
    ran_knowledge_gate = bool(state.get("ran_knowledge_gate", False))
    ran_surface_a = bool(state.get("ran_surface_a", False))
    ran_teaching_b = bool(state.get("ran_teaching_b", False))
    ran_calc_branch = bool(state.get("ran_calc_branch", False))
    knowledge_semantic_drift_skipped = knowledge_semantic_drift is None
    if knowledge_semantic_drift is None:
        # 知识门未执行（如盲答歧义短路），不造默认值；knowledge_match.skipped=True 展示为「未检测」
        knowledge_semantic_drift = SemanticDrift(
            fingerprint_matched=True,
            rule_constraints_kept=True,
            limit_words_consistent=True,
        )  # 仅用于 AND，不惩罚分数；展示由 knowledge_match.skipped 控制
    final_semantic_drift = SemanticDrift(
        fingerprint_matched=bool(solver_semantic_drift.fingerprint_matched) and bool(knowledge_semantic_drift.fingerprint_matched),
        rule_constraints_kept=bool(solver_semantic_drift.rule_constraints_kept) and bool(knowledge_semantic_drift.rule_constraints_kept),
        limit_words_consistent=bool(solver_semantic_drift.limit_words_consistent) and bool(knowledge_semantic_drift.limit_words_consistent),
    )

    # 硬门禁
    hard_gate = HardGate(
        structure_legal=len(errors) == 0,
        expression_standard=len(errors) == 0,
        solvability_baseline=not solver.ambiguity_flag,
    )
    hard_pass = (
        hard_gate.structure_legal
        and hard_gate.expression_standard
        and hard_gate.solvability_baseline
    )

    all_reasons = (
        errors
        + state.get("solver_issues", [])
        + state.get("drift_issues", [])
        + state.get("knowledge_gate_reasons", [])
        + state.get("realism_issues", [])
        + state.get("rigor_issues", [])
        + state.get("knowledge_issues", [])
        + state.get("explanation_issues", [])
        + teaching_issues
        + state.get("calculation_issues", [])
        + state.get("distractor_issues", [])
    )
    all_reasons = _dedupe_issues([str(x) for x in all_reasons])
    warnings = warnings + state.get("rigor_warnings", [])
    recommendation_suggestions = (knowledge_data or {}).get("recommendation_suggestions") or []
    year_constraint_review_signal = any("【年份约束复核】" in str(w) for w in warnings)
    if year_constraint_review_signal:
        all_reasons.append("【年份约束复核】出现公历年份但教材切片未给年份依据，建议补充证据后复核")

    # 显式闸门：干扰项冲突信号（命中任一 => 至少 REVIEW）
    overlap_pairs = dq_data.get("overlap_pairs", []) or []
    stem_option_conflicts = dq_data.get("stem_option_conflicts", []) or []
    mutual_exclusivity_fail = bool(dq_data.get("mutual_exclusivity_fail", False))
    distractor_gate_signals_count = int(bool(overlap_pairs)) + int(bool(stem_option_conflicts)) + int(mutual_exclusivity_fail)
    explicit_distractor_gate_triggered = distractor_gate_signals_count >= 1
    if explicit_distractor_gate_triggered:
        all_reasons.append(
            f"【显式闸门】干扰项冲突信号触发（signals={distractor_gate_signals_count}：overlap_pairs={bool(overlap_pairs)}, stem_option_conflicts={bool(stem_option_conflicts)}, mutual_exclusivity_fail={mutual_exclusivity_fail}）"
        )
    fatal_doctrinaire_gate = bool(realism_data.get("negative_emotion_detected", False)) and bool(
        realism_data.get("amplifies_defect_without_remedy", False)
    )
    if fatal_doctrinaire_gate:
        all_reasons.append("【教条主义拦截】客户存在负面情绪/担忧时，正确选项仅放大问题且无补救动作")
    compliance_risk_triggered = bool(realism_data.get("high_risk_domain_triggered", False))
    compliance_fatal_behaviors = [
        bool(realism_data.get("subjective_replaces_objective", False)),
        bool(realism_data.get("oral_replaces_written", False)),
        bool(realism_data.get("over_authority_conclusion", False)),
        bool(realism_data.get("bypass_compliance_process", False)),
    ]
    fatal_compliance_risk_gate = compliance_risk_triggered and any(compliance_fatal_behaviors)
    if fatal_compliance_risk_gate:
        all_reasons.append(
            "【合规风控拦截】高危业务域中出现主观替代客观/口头替代书面/越权定论/绕流程行为，需退回重写"
        )
    competing_truth_violation = bool(realism_data.get("competing_truth_violation", False))
    non_discriminative_stem_risk = bool(realism_data.get("non_discriminative_stem_risk", False))
    if competing_truth_violation:
        all_reasons.append("【真理对抗风险】错误选项在专业度/完整性/可执行性上优于正确选项，需复核")
    if non_discriminative_stem_risk:
        all_reasons.append("【真理对抗拦截】题干为不可判别的空泛设问，无法形成稳定最优解")
    for issue in (realism_data.get("competing_truth_issues") or []):
        if str(issue).strip():
            all_reasons.append(f"【真理对抗】{str(issue).strip()}")
    for issue in (realism_data.get("non_discriminative_stem_issues") or []):
        if str(issue).strip():
            all_reasons.append(f"【题干判别性】{str(issue).strip()}")
    # Code evidence arbitration: append reasons and evidence chain when calc branch ran
    _calc_enabled_for_reasons = bool((calculation_data or {}).get("enabled", False)) and ran_calc_branch
    if _calc_enabled_for_reasons:
        code_evidence_status = (calculation_data or {}).get("code_evidence_status") or "OK"
        code_evidence_chain = (calculation_data or {}).get("code_evidence_chain") or []
        code_check_passed = (calculation_data or {}).get("code_check_passed", True)
        code_check_reason = (calculation_data or {}).get("code_check_reason") or ""
        if code_evidence_status == "TOOL_FAIL":
            all_reasons.append("【代码节点】工具执行失败，建议复核（系统异常）")
            for c in code_evidence_chain[:5]:
                all_reasons.append("【代码证据链】" + str(c))
        elif code_evidence_status == "HARD":
            if not code_check_passed and code_check_reason:
                all_reasons.append("【代码节点】计算代码不符合教材规则（" + code_check_reason + "）")
            else:
                all_reasons.append("【代码节点】题目硬冲突（代码与标准答案不一致或条件缺失/多解）")
            for c in code_evidence_chain[:5]:
                all_reasons.append("【代码证据链】" + str(c))
        elif code_evidence_status == "SOFT":
            all_reasons.append("【代码节点】答案冲突待复核")
            for c in code_evidence_chain[:5]:
                all_reasons.append("【代码证据链】" + str(c))
    compliance_pass_requirements_met = True
    if compliance_risk_triggered:
        compliance_pass_requirements_met = (
            bool(realism_data.get("uses_authoritative_evidence", False))
            and bool(realism_data.get("introduces_professional_third_party", False))
            and bool(realism_data.get("follows_compliance_sop", False))
        )
        if not compliance_pass_requirements_met:
            all_reasons.append("【合规风控】高危业务域下缺少凭证核验/专业第三方/流程留痕动作，建议复核")

    # schema-driven 维度通过状态；未执行时 dimension=SKIP（未检测），决策不惩罚
    scenario_action_violation = bool(realism_data.get("scenario_dialogue_or_objection", False)) and not bool(
        realism_data.get("contains_business_action", True)
    )
    realism_pass = (
        bool(realism_data.get("passed", len(state.get("realism_issues", [])) == 0)) and not scenario_action_violation
    ) if ran_surface_a else True
    rigor_term_mismatch = rigor_data.get("term_mismatch_issues", []) or []
    rigor_pass = True if not ran_surface_a else not (
        bool(rigor_data.get("leakage_still_invalid", False))
        or bool(rigor_data.get("explanation_conflict_still_invalid", False))
        or bool(rigor_data.get("name_consistency_still_invalid", False))
        or bool(rigor_data.get("legal_math_closure_invalid", False))
        or len(rigor_term_mismatch) > 0
    )
    distractor_pass = True if not ran_surface_a else (
        dq.real_but_inapplicable
        and dq.format_aligned
        and dq.logic_homogenous
        and dq.balance_strength
        and not explicit_distractor_gate_triggered
    )
    exp_data = state.get("explanation_data") or {}
    calc_enabled = bool((calculation_data or {}).get("enabled", False)) and ran_calc_branch
    calc_required = bool(q.is_calculation) or _question_has_implicit_calculation(q)
    explanation_pass = True if not ran_teaching_b else not (
        not bool(exp_data.get("analysis_rewrite_sufficient", True))
        or bool(exp_data.get("three_part_semantic_invalid", False))
        or bool(exp_data.get("first_part_missing_target_title", False))
        or bool(exp_data.get("first_part_missing_level", False))
        or bool(exp_data.get("first_part_missing_textbook_raw", False))
        or len(exp_data.get("first_part_structured_issues", []) or []) > 0
        or not bool(exp_data.get("theory_support_present", True))
        or not bool(exp_data.get("business_support_present", True))
        or (calc_required and not bool(exp_data.get("non_answer_numeric_basis_sufficient", True)))
        or (calc_required and not bool(exp_data.get("explanation_calculation_consistent", True)))
    )
    teaching_pass = True if not ran_teaching_b else bool((teaching_data or {}).get("has_assessment_value", True))
    knowledge_pass = True if not ran_knowledge_gate else not (
        bool(knowledge_data.get("out_of_scope", False))
        or bool(knowledge_data.get("constraint_drift", False))
        or (
            q.question_type == "true_false"
            and bool(knowledge_data.get("single_knowledge_point_invalid", False))
        )
    )
    calc_pass = True
    if calc_enabled:
        calc_pass = not (
            bool(calculation_data.get("digit_complexity_too_high", False))
            or bool(calculation_data.get("step_count_too_high", False))
            or bool(calculation_data.get("complex_decimal_present", False))
            or str(calculation_data.get("mental_math_level", "")).strip() == "明显需计算器"
        )

    # 若业务常识问题较多，联动风险（基于去重问题数）
    realism_issues = _dedupe_issues([str(x) for x in (state.get("realism_issues", []) or [])])
    if realism_issues:
        risk.practice_conflict = True
        if risk.risk_level == RiskLevel.LOW:
            risk.risk_level = _risk_level_from_issues(realism_issues)

    # 致命信号（P0收紧：仅保留三类）
    # 1) 第1层 盲答不可判定（多解/无解/盲答不唯一）
    # 2) 第2层 与教材切片直接冲突/限定词漂移（知识匹配）
    # 3) 第3层 法理/数学闭环不成立（严谨合规）
    stem_over_400 = len(re.sub(r"\s+", "", str(q.stem or ""))) > 400
    any_option_over_200 = any(
        len(re.sub(r"\s+", "", _strip_option_prefix(opt))) > 200
        for opt in (q.options or [])
    )
    # 4) 计算题代码证据硬冲突（题目硬冲突/工具与标准答案不一致且条件缺失或多解）
    code_evidence_fatal = calc_enabled and (calculation_data or {}).get("code_evidence_status") == "HARD"
    fatal_reject_signals = [
        bool(solver.ambiguity_flag),
        bool(state.get("knowledge_gate_reject", False)),
        bool(rigor_data.get("legal_math_closure_invalid", False)),
        code_evidence_fatal,
    ]

    if any(fatal_reject_signals):
        decision = Decision.REJECT
    else:
        review_signals = [
            not hard_pass,  # 结构/表达硬错从 REJECT 降级为 REVIEW
            risk.risk_level == RiskLevel.HIGH,  # 高风险从 REJECT 降级为 REVIEW
            (ran_surface_a and not realism_pass),
            (ran_surface_a and bool(realism_data.get("slice_conflict_invalid", False))),
            (ran_surface_a and not rigor_pass),  # 仅当已执行时惩罚
            bool(rigor_data.get("leakage_still_invalid", False)),
            explicit_distractor_gate_triggered,
            (ran_teaching_b and not explanation_pass),
            (ran_teaching_b and not teaching_pass),
            (ran_knowledge_gate and not knowledge_pass),
            (calc_enabled and not calc_pass),
            (ran_surface_a and compliance_risk_triggered and not compliance_pass_requirements_met),
            (ran_surface_a and competing_truth_violation),
            (ran_surface_a and fatal_doctrinaire_gate),
            (ran_surface_a and fatal_compliance_risk_gate),
            (ran_surface_a and non_discriminative_stem_risk),
            stem_over_400,
            any_option_over_200,
            year_constraint_review_signal,
            risk.risk_level == RiskLevel.MEDIUM,
            (calc_enabled and (calculation_data or {}).get("code_evidence_status") in ("SOFT", "TOOL_FAIL")),
        ]
        if any(review_signals):
            decision = Decision.REVIEW
        else:
            decision = Decision.PASS

    # 新评分：每维10分扣分制 + 执行维度动态归一化
    logic_score = 10.0
    logic_reasons: list[str] = []
    if bool(solver.ambiguity_flag):
        logic_score = 0.0
        logic_reasons.append("-10: 盲答不可判定（多解/无解/歧义）")
    else:
        logic_score, logic_reasons = _calc_dimension_score_with_penalties(
            10.0,
            [
                (not bool(hard_gate.solvability_baseline), 3, "可解性基线不稳"),
                (str(solver.predicted_answer or "").strip().upper() in {"", "NONE"}, 2, "未形成稳定预测答案"),
            ],
        )

    knowledge_score: float | None = None
    knowledge_reasons: list[str] = []
    if ran_knowledge_gate:
        if bool(state.get("knowledge_gate_reject", False)):
            knowledge_score = 0.0
            knowledge_reasons = ["-10: 知识门硬冲突（超纲/约束漂移证据充分）"]
        else:
            knowledge_score, knowledge_reasons = _calc_dimension_score_with_penalties(
                10.0,
                [
                    (bool(knowledge_data.get("out_of_scope", False)), 4, "存在超纲风险"),
                    (bool(knowledge_data.get("constraint_drift", False)), 4, "存在约束漂移"),
                    (bool(knowledge_data.get("single_knowledge_point_invalid", False)), 2, "单知识点匹配不稳"),
                ],
            )

    rigor_score: float | None = None
    rigor_reasons: list[str] = []
    if ran_surface_a:
        if bool(rigor_data.get("legal_math_closure_invalid", False)):
            rigor_score = 0.0
            rigor_reasons = ["-10: 法理/数学闭环不成立"]
        else:
            rigor_score, rigor_reasons = _calc_dimension_score_with_penalties(
                10.0,
                [
                    (bool(rigor_data.get("leakage_still_invalid", False)), 4, "题目傻瓜化直给答案风险仍未消除"),
                    (bool(rigor_data.get("explanation_conflict_still_invalid", False)), 3, "解析与结论冲突"),
                    (bool(rigor_data.get("name_consistency_still_invalid", False)), 1, "主体称谓不一致"),
                    (len(rigor_term_mismatch) > 0, min(2, len(rigor_term_mismatch)), "术语不规范"),
                ],
            )

    realism_score_10: float | None = None
    realism_reasons: list[str] = []
    if ran_surface_a:
        if fatal_compliance_risk_gate or fatal_doctrinaire_gate:
            realism_score_10 = 0.0
            realism_reasons = ["-10: 高风险场景触发致命业务真实性拦截"]
        else:
            realism_score_10, realism_reasons = _calc_dimension_score_with_penalties(
                10.0,
                [
                    (scenario_action_violation, 3, "情景题缺少业务动作"),
                    (bool(realism_data.get("backbook_style_answer", False)), 2, "答案偏背书化，实操性不足"),
                    (bool(realism_data.get("scene_binding_required_violation", False)), 2, "场景绑定不足"),
                    (bool(realism_data.get("workflow_sequence_violation", False)), 2, "业务流程顺序不当"),
                    (bool(realism_data.get("slice_conflict_invalid", False)), 3, "与切片事实冲突"),
                    (bool(realism_data.get("competing_truth_violation", False)), 4, "真理对抗风险"),
                    (bool(realism_data.get("non_discriminative_stem_risk", False)), 6, "题干判别性不足"),
                ],
            )

    distractor_score: float | None = None
    distractor_reasons: list[str] = []
    if ran_surface_a:
        distractor_score, distractor_reasons = _calc_dimension_score_with_penalties(
            10.0,
            [
                (not bool(dq.logic_homogenous), 2, "干扰项逻辑同质性不足"),
                (not bool(dq.balance_strength), 2, "干扰项强弱失衡"),
                (not bool(dq.format_aligned), 1, "干扰项格式不齐"),
                (not bool(dq.real_but_inapplicable), 2, "干扰项不够“似真但错误”"),
                (explicit_distractor_gate_triggered, 3, "命中显式冲突闸门"),
            ],
        )

    explanation_score: float | None = None
    explanation_reasons: list[str] = []
    if ran_teaching_b:
        explanation_score, explanation_reasons = _calc_dimension_score_with_penalties(
            10.0,
            [
                (not bool(exp_data.get("analysis_rewrite_sufficient", True)), 3, "解析重写充分性不足"),
                (bool(exp_data.get("three_part_semantic_invalid", False)), 2, "三段语义不清"),
                (bool(exp_data.get("first_part_missing_target_title", False)), 1, "首段缺少考核点标题"),
                (bool(exp_data.get("first_part_missing_level", False)), 1, "首段缺少能力层级"),
                (bool(exp_data.get("first_part_missing_textbook_raw", False)), 1, "首段缺少教材原文"),
                (not bool(exp_data.get("theory_support_present", True)), 2, "缺理论支撑"),
                (not bool(exp_data.get("business_support_present", True)), 2, "缺业务支撑"),
                (calc_required and not bool(exp_data.get("non_answer_numeric_basis_sufficient", True)), 2, "未解释非答案数值选项"),
                (calc_required and not bool(exp_data.get("explanation_calculation_consistent", True)), 2, "解析与计算过程不一致"),
            ],
        )

    teaching_score: float | None = None
    teaching_reasons: list[str] = []
    if ran_teaching_b:
        ap_issues = [str(x) for x in (teaching_data.get("assessment_point_issues") or []) if str(x).strip()]
        teaching_score, teaching_reasons = _calc_dimension_score_with_penalties(
            10.0,
            [
                (not bool(teaching_data.get("has_assessment_value", True)), 4, "题目考核价值不足"),
                (not bool(teaching_data.get("assessment_point_aligned", True)), 2, "考核点偏离"),
                (len(ap_issues) > 0, min(3, len(ap_issues)), "考核点问题"),
                (str(tv.discrimination or "").strip() == "低", 2, "区分度低"),
                (float(tv.estimated_pass_rate) > 0.9 or float(tv.estimated_pass_rate) < 0.2, 2, "通过率异常"),
            ],
        )

    code_evidence_status = str((calculation_data or {}).get("code_evidence_status") or "OK")
    calc_score: float | None = None
    calc_reasons: list[str] = []
    if calc_enabled:
        if code_evidence_status == "HARD":
            calc_score = 0.0
            calc_reasons = ["-10: 计算证据硬冲突（代码证据HARD）"]
        else:
            calc_score, calc_reasons = _calc_dimension_score_with_penalties(
                10.0,
                [
                    (bool(calculation_data.get("digit_complexity_too_high", False)), 3, "位数复杂"),
                    (bool(calculation_data.get("step_count_too_high", False)), 3, "步骤过多"),
                    (bool(calculation_data.get("complex_decimal_present", False)), 2, "复杂小数影响作答"),
                    (str(calculation_data.get("mental_math_level", "")).strip() == "明显需计算器", 2, "明显需计算器"),
                    (code_evidence_status == "SOFT", 2, "代码证据软冲突（SOFT）"),
                    (code_evidence_status == "TOOL_FAIL", 3, "代码工具失败（TOOL_FAIL）"),
                ],
            )

    base_weights = {
        "逻辑可解性": 0.20,
        "知识匹配": 0.20,
        "严谨合规性": 0.20,
        "业务真实性": 0.15,
        "干扰项质量": 0.10,
        "解析质量": 0.10,
        "教学价值": 0.05,
    }
    hard_rule_weight = 0.10
    if calc_enabled:
        scaled_weights = {k: round(v * 0.75, 4) for k, v in base_weights.items()}
        calc_weight = 0.15
    else:
        scaled_weights = {k: round(v * 0.9, 4) for k, v in base_weights.items()}
        calc_weight = 0.0

    solver_issues = _dedupe_issues([str(x) for x in (state.get("solver_issues", []) or [])])
    knowledge_issues = _dedupe_issues([str(x) for x in (state.get("knowledge_issues", []) or [])])
    rigor_issues = _dedupe_issues([str(x) for x in (state.get("rigor_issues", []) or [])])
    distractor_issues = _dedupe_issues([str(x) for x in (state.get("distractor_issues", []) or [])])
    explanation_issues = _dedupe_issues([str(x) for x in (state.get("explanation_issues", []) or [])])
    teaching_issues_dedup = _dedupe_issues([str(x) for x in (teaching_issues or [])])
    hard_rule_issue_texts = _dedupe_issues(
        [str(x) for x in (errors or [])]
        + [str(x) for x in (warnings or [])]
        + [str(x) for x in (recommendation_suggestions or [])]
    )

    def _calc_hard_rule_score_10(issue_texts: list[str]) -> tuple[float, list[str]]:
        if not issue_texts:
            return 10.0, []
        weighted_patterns: list[tuple[str, float, str]] = [
            ("多解", 3.0, "出现多解风险"),
            ("无解", 3.0, "出现无解风险"),
            ("超纲", 2.5, "存在超纲/越界表述"),
            ("约束漂移", 2.5, "约束条件漂移"),
            ("法理", 2.5, "法理/规则表述不严谨"),
            ("数学闭环", 2.5, "数学闭环证据不足"),
            ("语义", 2.0, "语义准确性不足"),
            ("设问", 1.5, "设问规范性不足"),
            ("选项代入", 1.5, "选项代入语义不通"),
            ("A/B/C/D", 1.2, "答案标注格式不规范"),
            ("答案为", 1.2, "答案句式规范性不足"),
            ("单位", 1.0, "单位表达规范性不足"),
            ("称谓", 0.8, "称谓一致性不足"),
            ("括号", 0.8, "括号规范性不足"),
            ("引号", 0.8, "引号规范性不足"),
            ("标点", 0.6, "标点规范性不足"),
            ("句号", 0.6, "句号位置规范性不足"),
        ]
        penalty = 0.0
        reasons: list[str] = []
        for raw in issue_texts:
            text = str(raw or "").strip()
            if not text:
                continue
            hit = next(((w, r) for p, w, r in weighted_patterns if p in text), None)
            if hit is not None:
                penalty += float(hit[0])
                reasons.append(f"-{hit[0]}: {hit[1]}")
            elif "【L1Schema】" in text or "LLM判定" in text:
                penalty += 1.0
                reasons.append("-1.0: L1 schema/判定问题")
            elif "建议" in text or "提醒" in text:
                penalty += 0.4
                reasons.append("-0.4: 规范提醒")
            else:
                penalty += 0.5
                reasons.append("-0.5: 一般规范问题")
        penalty_10 = min(10.0, penalty)
        return _clamp_score_10(10.0 - penalty_10), reasons[:8]

    hard_rule_score_10, hard_rule_reasons = _calc_hard_rule_score_10(hard_rule_issue_texts)

    dimension_results: dict[str, DimensionResult] = {
        "逻辑可解性": DimensionResult(
            status=DimensionStatus.PASS if logic_score >= 6 else DimensionStatus.FAIL,
            issues=solver_issues,
            score_10=logic_score,
            weight=scaled_weights["逻辑可解性"],
            executed=True,
            dedup_issue_count=len(solver_issues),
            reasons=logic_reasons,
            details={"ambiguity_flag": bool(solver.ambiguity_flag)},
        ),
        "知识匹配": DimensionResult(
            status=DimensionStatus.SKIP if not ran_knowledge_gate else (DimensionStatus.PASS if knowledge_pass else DimensionStatus.FAIL),
            issues=knowledge_issues,
            score_10=knowledge_score,
            weight=scaled_weights["知识匹配"],
            executed=bool(ran_knowledge_gate),
            dedup_issue_count=len(knowledge_issues),
            reasons=knowledge_reasons,
            details=knowledge_data,
        ),
        "严谨合规性": DimensionResult(
            status=DimensionStatus.SKIP if not ran_surface_a else (DimensionStatus.PASS if rigor_pass else DimensionStatus.FAIL),
            issues=rigor_issues,
            score_10=rigor_score,
            weight=scaled_weights["严谨合规性"],
            executed=bool(ran_surface_a),
            dedup_issue_count=len(rigor_issues),
            reasons=rigor_reasons,
            details=rigor_data,
        ),
        "业务真实性": DimensionResult(
            status=DimensionStatus.SKIP if not ran_surface_a else (DimensionStatus.PASS if realism_pass else DimensionStatus.FAIL),
            issues=realism_issues,
            score_10=realism_score_10,
            weight=scaled_weights["业务真实性"],
            executed=bool(ran_surface_a),
            dedup_issue_count=len(realism_issues),
            reasons=realism_reasons,
            details=realism_data,
        ),
        "干扰项质量": DimensionResult(
            status=DimensionStatus.SKIP if not ran_surface_a else (DimensionStatus.PASS if distractor_pass else DimensionStatus.FAIL),
            issues=distractor_issues,
            score_10=distractor_score,
            weight=scaled_weights["干扰项质量"],
            executed=bool(ran_surface_a),
            dedup_issue_count=len(distractor_issues),
            reasons=distractor_reasons,
            details={**dq_data, "explicit_gate_triggered": explicit_distractor_gate_triggered, "gate_signals_count": distractor_gate_signals_count},
        ),
        "解析质量": DimensionResult(
            status=DimensionStatus.SKIP if not ran_teaching_b else (DimensionStatus.PASS if explanation_pass else DimensionStatus.FAIL),
            issues=explanation_issues,
            score_10=explanation_score,
            weight=scaled_weights["解析质量"],
            executed=bool(ran_teaching_b),
            dedup_issue_count=len(explanation_issues),
            reasons=explanation_reasons,
            details=state.get("explanation_data", {}),
        ),
        "教学价值": DimensionResult(
            status=DimensionStatus.SKIP if not ran_teaching_b else (DimensionStatus.PASS if teaching_pass else DimensionStatus.FAIL),
            issues=teaching_issues_dedup,
            score_10=teaching_score,
            weight=scaled_weights["教学价值"],
            executed=bool(ran_teaching_b),
            dedup_issue_count=len(teaching_issues_dedup),
            reasons=teaching_reasons,
            details=teaching_data,
        ),
        "结构与表达规范": DimensionResult(
            status=DimensionStatus.PASS if hard_rule_score_10 >= 6 else DimensionStatus.FAIL,
            issues=hard_rule_issue_texts,
            score_10=hard_rule_score_10,
            weight=hard_rule_weight,
            executed=True,
            dedup_issue_count=len(hard_rule_issue_texts),
            reasons=hard_rule_reasons,
            details={
                "hard_pass": bool(hard_pass),
                "error_count": len(errors or []),
                "warning_count": len(warnings or []),
                "suggestion_count": len(recommendation_suggestions or []),
            },
        ),
    }
    if calc_enabled:
        calc_issues = _dedupe_issues([str(x) for x in (state.get("calculation_issues", []) or [])])
        dimension_results["计算可执行性与复杂度"] = DimensionResult(
            status=DimensionStatus.SKIP if not calc_enabled else (DimensionStatus.PASS if calc_pass and (calc_score or 0) >= 6 else DimensionStatus.FAIL),
            issues=calc_issues,
            score_10=calc_score,
            weight=calc_weight,
            executed=bool(calc_enabled),
            dedup_issue_count=len(calc_issues),
            reasons=calc_reasons,
            details=calculation_data,
        )

    scores = _score_from_state(
        logic_score=logic_score,
        knowledge_score=knowledge_score,
        distractor_score=distractor_score,
        teaching_score=teaching_score,
        risk=risk,
        ran_surface_a=ran_surface_a,
        decision=decision,
    )
    weighted_10 = 0.0
    executed_weight = 0.0
    total_weight = 0.0
    for dr in dimension_results.values():
        w = float(dr.weight or 0.0)
        total_weight += w
        if dr.executed and dr.score_10 is not None:
            weighted_10 += float(dr.score_10) * w
            executed_weight += w
    coverage = round((executed_weight / total_weight), 4) if total_weight > 0 else 0.0
    baseline_raw_10 = (weighted_10 / executed_weight) if executed_weight > 0 else 0.0
    baseline_score = round(min(10.0, max(0.0, baseline_raw_10)), 2)

    quality_score, quality_reasons, quality_basis, quality_dimension_feedback = _llm_quality_score_eval(
        llm=state.get("llm"),
        question=q,
    )
    if quality_reasons:
        all_reasons = _dedupe_issues(all_reasons + [f"【质量评分】{x}" for x in quality_reasons])
    if quality_basis:
        all_reasons = _dedupe_issues(all_reasons + [f"【质量评分依据】{quality_basis}"])

    overall_raw_10 = (0.35 * baseline_score) + (0.65 * quality_score)
    overall = round(min(10.0, max(0.0, overall_raw_10)), 1)
    if decision == Decision.REJECT:
        overall = min(overall, 5.9)

    def _llm_aggregate_reasons(
        llm: Any,
        *,
        decision: Decision,
        hard_pass: bool,
        scores: Scores,
        reasons_raw: list[str],
        warnings_list: list[str],
        recommendation_suggestions: list[str],
        dimension_results: dict[str, DimensionResult],
    ) -> tuple[list[str], str]:
        default_system = "你是评测报告聚合器，只输出JSON。"
        default_human = (
            "decision: {decision}\n"
            "hard_pass: {hard_pass}\n"
            "scores: {scores}\n"
            "warnings: {warnings}\n"
            "reasons_raw: {reasons_raw}\n"
            "recommendation_suggestions: {recommendation_suggestions}\n"
            "dimension_results: {dimension_results}"
        )
        system_prompt, human_prompt = load_prompt_pair(
            "prompts/layer5_aggregate.md",
            default_system,
            default_human,
            [
                "decision",
                "hard_pass",
                "scores",
                "warnings",
                "reasons_raw",
                "recommendation_suggestions",
                "dimension_results",
            ],
        )
        prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", human_prompt)])
        payload = prompt.invoke(
            {
                "decision": decision.value,
                "hard_pass": hard_pass,
                "scores": scores.model_dump(),
                "warnings": warnings_list,
                "reasons_raw": reasons_raw,
                "recommendation_suggestions": recommendation_suggestions,
                "dimension_results": {k: v.model_dump() for k, v in dimension_results.items()},
            }
        )
        client = ReliableLLMClient(llm, timeout_seconds=120, retries=1)
        data = client.invoke_json(payload, fallback={"reasons": reasons_raw, "actionable_feedback": ""})
        reasons = [str(x) for x in (data.get("reasons") or []) if str(x).strip()]
        if not reasons:
            reasons = reasons_raw
        actionable = str(data.get("actionable_feedback", "") or "").strip()
        if not actionable:
            actionable = "；".join((reasons + warnings_list + recommendation_suggestions)[:8]) or "通过"
        return reasons, actionable

    obs_raw = get_observability()
    prompt_tokens = int(obs_raw.get("prompt_tokens", 0) or 0)
    completion_tokens = int(obs_raw.get("completion_tokens", 0) or 0)
    usage = TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    obs = Observability(
        critic_loops=0,
        llm_calls=int(obs_raw.get("calls", 0) or 0),
        failed_calls=int(obs_raw.get("failed_calls", 0) or 0),
        last_error=str(obs_raw.get("last_error", "") or ""),
        last_raw_response=str(obs_raw.get("last_raw_response", "") or ""),
        last_raw_truncated=bool(obs_raw.get("last_raw_truncated", False)),
        tokens=usage,
        latency_ms=int(obs_raw.get("latency_ms", 0) or 0),
        unstable_flag=bool(obs_raw.get("failed_calls", 0))
        or int(obs_raw.get("latency_ms", 0) or 0) > 60000,
    )

    costs = Costs(
        per_question_usd=round((usage.total_tokens / 1000) * 0.0002, 6),
        per_node_usd={},
        per_model_usd={},
        cost_alert=False,
    )

    evidence = Evidence(
        slice_id="slice_001",
        quotes=[],
        ask_judgement_evidence=str(state.get("gate_recheck_data", {}).get("ask_judgement_evidence", "")),
        substitution_evidence=[
            str(x) for x in state.get("gate_recheck_data", {}).get("substitution_evidence", [])
        ],
        uniqueness_evidence=(
            [str(x) for x in state.get("gate_recheck_data", {}).get("uniqueness_evidence", [])]
            + [str(x) for x in (state.get("solver_calc_data", {}).get("logs") or [])]
        )[:8],
    )

    report = JudgeReport(
        question_id=q.question_id,
        assessment_type=q.assessment_type,
        trace_id=state.get("trace_id", ""),
        decision=decision,
        hard_pass=hard_pass,
        scores=scores,
        overall_score=overall,
        baseline_score=baseline_score,
        quality_score=quality_score,
        quality_dimension_feedback=quality_dimension_feedback,
        quality_reasons=quality_reasons,
        quality_scoring_basis=quality_basis,
        evidence=evidence,
        reasons=all_reasons,
        hard_gate=hard_gate,
        semantic_drift=final_semantic_drift,
        solver_validation=solver,
        distractor_quality=dq,
        knowledge_match=KnowledgeMatch(
            hit_target=final_semantic_drift.fingerprint_matched,
            no_out_of_bounds=final_semantic_drift.rule_constraints_kept,
            no_cross_pollution=final_semantic_drift.limit_words_consistent,
            skipped=knowledge_semantic_drift_skipped,
        ),
        teaching_value=tv,
        risk_assessment=risk,
        observability=obs,
        costs=costs,
        dimension_results=dimension_results,
        actionable_feedback="；".join((all_reasons + warnings + recommendation_suggestions)[:8])
        if (all_reasons or warnings or recommendation_suggestions)
        else "通过",
    )

    if state.get("llm"):
        try:
            agg_reasons, agg_actionable = _llm_aggregate_reasons(
                state.get("llm"),
                decision=decision,
                hard_pass=hard_pass,
                scores=scores,
                reasons_raw=all_reasons,
                warnings_list=warnings,
                recommendation_suggestions=recommendation_suggestions,
                dimension_results=dimension_results,
            )
            report.reasons = agg_reasons
            report.actionable_feedback = agg_actionable
        except Exception:
            pass

    return {"final_report": report}  # type: ignore[typeddict-item]


def create_judge_graph():
    from src.pipeline.builder import create_judge_graph as _create_judge_graph

    return _create_judge_graph()


def run_judge(
    question: QuestionInput,
    llm: Any,
    *,
    skip_phase1: bool = False,
) -> JudgeReport:
    from src.pipeline.runner import run_judge as _run_judge

    return _run_judge(question, llm, skip_phase1=skip_phase1)
