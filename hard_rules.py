import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple

# --- Code-side fixes (detection remains for audit; fix applied in exam_graph) ---


def replace_single_quotes_in_final_json(final_json: Dict) -> Dict:
    """Replace all single quotes with double quotes in 题干, 选项1-4, 解析. Used as code fix after Writer."""
    if not isinstance(final_json, dict):
        return final_json
    out = dict(final_json)
    for key in ["题干", "解析", "选项1", "选项2", "选项3", "选项4"]:
        if key in out and isinstance(out[key], str) and "'" in out[key]:
            out[key] = out[key].replace("'", '"')
    return out


def apply_numeric_options_ascending(final_json: Dict) -> Dict:
    """
    When all options are single numeric with same unit, sort options ascending and remap 正确答案.
    Returns new dict; no-op if not all options are numeric or units differ.
    """
    if not isinstance(final_json, dict):
        return final_json
    opts = [
        str(final_json.get("选项1", "") or "").strip(),
        str(final_json.get("选项2", "") or "").strip(),
        str(final_json.get("选项3", "") or "").strip(),
        str(final_json.get("选项4", "") or "").strip(),
    ]
    if not opts or any(not o for o in opts):
        return final_json
    parsed = [_parse_numeric_option(o) for o in opts]
    if not all(p is not None for p in parsed):
        return final_json
    units = {p[1] for p in parsed if p is not None}
    if len(units) != 1:
        return final_json
    # (value, original_index, text)
    indexed = [(parsed[i][0], i, opts[i]) for i in range(4)]
    indexed.sort(key=lambda x: (x[0], x[1]))
    if indexed == [(parsed[i][0], i, opts[i]) for i in range(4)]:
        return final_json  # already ascending
    out = dict(final_json)
    for i, (_, _, text) in enumerate(indexed):
        out[f"选项{i + 1}"] = text
    # Remap answer: old letter -> old index -> new position -> new letter
    ans = str(out.get("正确答案", "") or "").strip().upper()
    if len(ans) == 1 and ans in "ABCD":
        old_idx = ord(ans) - ord("A")
        new_pos = next(j for j, (_, oi, _) in enumerate(indexed) if oi == old_idx)
        out["正确答案"] = chr(ord("A") + new_pos)
    return out


_MD_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _has_image_markers(text: str) -> bool:
    if not text:
        return False
    if re.search(r"!\[[^\]]*\]\([^)]+\)", text):
        return True
    if re.search(r"<\s*img\b", text, flags=re.IGNORECASE):
        return True
    if re.search(r"\.(png|jpe?g|gif)\b", text, flags=re.IGNORECASE):
        return True
    return False


def _has_table_markers(text: str) -> bool:
    if not text:
        return False
    if re.search(r"<\s*table\b", text, flags=re.IGNORECASE):
        return True
    lines = [line for line in str(text).splitlines() if "|" in line]
    if not lines:
        return False
    if any(_MD_TABLE_SEPARATOR_RE.match(line) for line in lines):
        return True
    # Heuristic: lines that look like Markdown table rows.
    if any(_MD_TABLE_ROW_RE.match(line) and line.count("|") >= 2 for line in lines):
        return True
    return False


def validate_media_rules(question: str, options: Iterable[str], explanation: str) -> List[str]:
    issues: List[str] = []
    q_text = str(question or "")
    exp_text = str(explanation or "")
    opts = [str(o) for o in options or []]

    if _has_image_markers(q_text):
        issues.append("题干包含图片标记或图片文件扩展名")
    if _has_image_markers(exp_text):
        issues.append("解析包含图片标记或图片文件扩展名")
    if any(_has_image_markers(opt) for opt in opts):
        issues.append("选项包含图片标记或图片文件扩展名")

    if _has_table_markers(q_text):
        issues.append("题干包含表格标记")
    if _has_table_markers(exp_text):
        issues.append("解析包含表格标记")
    if any(_has_table_markers(opt) for opt in opts):
        issues.append("选项包含表格标记")

    return issues


def _sanitize_image_markers(text: str) -> Tuple[str, bool]:
    if not text:
        return text, False
    updated = text
    changed = False

    def _img_repl(match: re.Match) -> str:
        nonlocal changed
        alt = match.group(1).strip()
        changed = True
        return alt if alt else "图片"

    updated = re.sub(r"!\[([^\]]*)\]\([^)]+\)", _img_repl, updated)
    if re.search(r"<\s*img\b", updated, flags=re.IGNORECASE):
        updated = re.sub(r"<\s*img\b[^>]*>", "图片", updated, flags=re.IGNORECASE)
        changed = True
    if re.search(r"\.(png|jpe?g|gif)\b", updated, flags=re.IGNORECASE):
        updated = re.sub(r"\S+\.(png|jpe?g|gif)\b", "图片", updated, flags=re.IGNORECASE)
        changed = True
    return updated, changed


def _sanitize_table_markers(text: str) -> Tuple[str, bool]:
    if not text:
        return text, False
    if not _has_table_markers(text):
        return text, False
    changed = False
    lines = str(text).splitlines()
    cleaned_lines: List[str] = []
    for line in lines:
        if _MD_TABLE_SEPARATOR_RE.match(line):
            changed = True
            continue
        if "|" in line and _MD_TABLE_ROW_RE.match(line):
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if cells:
                cleaned_lines.append("，".join(cells))
                changed = True
                continue
        cleaned_lines.append(line)
    updated = "\n".join(cleaned_lines)
    if re.search(r"<\s*table\b", updated, flags=re.IGNORECASE):
        updated = re.sub(r"</\s*tr\s*>", "；", updated, flags=re.IGNORECASE)
        updated = re.sub(r"</\s*(td|th)\s*>", "，", updated, flags=re.IGNORECASE)
        updated = re.sub(r"<\s*/?\s*(table|thead|tbody|tr|td|th)\b[^>]*>", "", updated, flags=re.IGNORECASE)
        changed = True
    return updated, changed


_EXPL_SECTION_RE = re.compile(r"^\s*(?:\d+\s*、\s*)?(教材原文|试题分析|结论)\s*[:：、\s]?", re.UNICODE)
# Count section headers per type: each of 教材原文/试题分析/结论 must appear exactly once (three-part rule)
_EXPL_SECTION_HEADER_RE = re.compile(
    r"^\s*(?:\d+\s*、\s*)?(教材原文|试题分析|结论)\s*[:：、\s]",
    re.UNICODE | re.MULTILINE,
)


def _find_expl_sections(text: str) -> Tuple[Dict[str, Tuple[int, int]], bool]:
    positions: Dict[str, Tuple[int, int]] = {}
    if not text:
        return positions, True
    lines = str(text).splitlines()
    for idx, line in enumerate(lines):
        m = _EXPL_SECTION_RE.match(line)
        if m:
            label = m.group(1)
            if label not in positions:
                positions[label] = (idx, _line_start_index(lines, idx))
    strict = len(positions) == 3
    if strict:
        return positions, True
    # Fallback: locate sections anywhere in text (not necessarily line-start)
    for m in re.finditer(r"(教材原文|试题分析|结论)", str(text)):
        label = m.group(1)
        if label in positions:
            continue
        line_idx = str(text)[: m.start()].count("\n")
        positions[label] = (line_idx, m.start())
    return positions, False


def _line_start_index(lines: List[str], idx: int) -> int:
    if idx <= 0:
        return 0
    return sum(len(lines[i]) + 1 for i in range(idx))


def _get_section_body(
    exp_text: str,
    sections: Dict[str, Tuple[int, int]],
    section_name: str,
    required_order: List[str],
) -> str:
    """Extract section body text (from section title to next section start)."""
    if section_name not in sections:
        return ""
    start = sections[section_name][1]
    idx = required_order.index(section_name)
    if idx + 1 < len(required_order):
        next_name = required_order[idx + 1]
        if next_name in sections:
            end = sections[next_name][1]
            return exp_text[start:end].strip()
    return exp_text[start:].strip()


# Level keywords required in 教材原文 per spec (目标题内容即路由前三个标题 + 分级 + 教材原文)
_LEVEL_KEYWORDS_RE = re.compile(r"掌握|了解|熟悉|识记")


def _extract_conclusion_answer(text: str) -> Optional[str]:
    if not text:
        return None
    matches = re.findall(r"本题答案为\s*([A-H]+|正确|错误)", text, flags=re.IGNORECASE)
    if not matches:
        return None
    last = matches[-1]
    if re.fullmatch(r"[A-H]+", last, flags=re.IGNORECASE):
        return last.upper()
    return last


def _conclusion_ends_with_answer(text: str) -> bool:
    if not text:
        return False
    tail = str(text).strip()
    return bool(re.search(r"本题答案为\s*(?:[A-H]+|正确|错误)\s*[。．]?\s*$", tail, flags=re.IGNORECASE))


def sanitize_media_payload(
    question: str, options: Iterable[str], explanation: str
) -> Tuple[str, List[str], str, bool]:
    q_text, q_changed_img = _sanitize_image_markers(str(question or ""))
    q_text, q_changed_tbl = _sanitize_table_markers(q_text)
    q_changed = q_changed_img or q_changed_tbl

    exp_text, e_changed_img = _sanitize_image_markers(str(explanation or ""))
    exp_text, e_changed_tbl = _sanitize_table_markers(exp_text)
    e_changed = e_changed_img or e_changed_tbl

    opt_list: List[str] = []
    opt_changed = False
    for opt in options or []:
        o_text, o_changed_img = _sanitize_image_markers(str(opt))
        o_text, o_changed_tbl = _sanitize_table_markers(o_text)
        if o_changed_img or o_changed_tbl:
            opt_changed = True
        opt_list.append(o_text)

    changed = q_changed or e_changed or opt_changed
    return q_text, opt_list, exp_text, changed


def _mk_issue(
    issue_code: str,
    field: str,
    message: str,
    severity: str = "error",
    suggested_fix: Optional[str] = None,
) -> Dict[str, str]:
    fix = suggested_fix if suggested_fix is not None else f"请修复问题：{message}"
    return {
        "issue_code": issue_code,
        "severity": severity,
        "field": field,
        "message": message,
        "fix_hint": f"请修复问题：{message}",
        "suggested_fix": fix,
    }


def _has_year(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"(19|20)\d{2}年?", text))


def _parse_numeric_option(option: str) -> Optional[Tuple[float, str]]:
    if option is None:
        return None
    text = str(option).strip()
    if not text:
        return None
    text = text.replace(",", "")
    numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
    if len(numbers) != 1:
        return None
    try:
        value = float(numbers[0])
    except Exception:
        return None
    unit = re.sub(r"-?\d+(?:\.\d+)?", "", text).strip()
    return value, unit


def validate_hard_rules(
    question: str,
    options: Iterable[str],
    explanation: str,
    kb_context: Optional[str] = None,
    target_type: Optional[str] = None,
    answer: Optional[str] = None,
    is_calculation: Optional[bool] = None,
) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    q_text = str(question or "")
    exp_text = str(explanation or "")
    opts = [str(o) for o in options or []]

    # Media rules (images/tables)
    media_issues = validate_media_rules(q_text, opts, exp_text)
    for msg in media_issues:
        if "图片" in msg:
            issues.append(_mk_issue("HARD_IMAGE", "global", msg))
        elif "表格" in msg:
            issues.append(_mk_issue("HARD_TABLE", "global", msg))
        else:
            issues.append(_mk_issue("HARD_MEDIA", "global", msg))

    # Single quote ban
    if "'" in q_text:
        issues.append(_mk_issue("HARD_SINGLE_QUOTE", "question", "题干包含单引号"))
    if "'" in exp_text:
        issues.append(_mk_issue("HARD_SINGLE_QUOTE", "explanation", "解析包含单引号"))
    if any("'" in opt for opt in opts):
        issues.append(_mk_issue("HARD_SINGLE_QUOTE", "options", "选项包含单引号"))

    # Banned options (aligned with Judge DeterministicFilter + REQUIREMENTS 4.6)
    banned_phrases = [
        "以上都对",
        "以上都错",
        "以上选项全对",
        "以上选项全错",
        "皆是",
        "皆非",
        "全部正确",
        "全部错误",
    ]
    for opt in opts:
        for phrase in banned_phrases:
            if phrase in opt:
                issues.append(_mk_issue("HARD_BANNED_OPTION", "options", f"选项包含禁用短语：{phrase}"))
                break

    # Numeric options should be ascending if all options are numeric with consistent unit
    parsed = [_parse_numeric_option(opt) for opt in opts]
    if opts and all(p is not None for p in parsed):
        units = {p[1] for p in parsed if p is not None}
        if len(units) == 1:
            values = [p[0] for p in parsed if p is not None]
            if values != sorted(values):
                issues.append(_mk_issue("HARD_NUMERIC_ORDER", "options", "数值型选项未按从小到大排序"))

    # Length thresholds
    if len(q_text) > 400:
        issues.append(_mk_issue("HARD_STEM_LEN", "question", f"题干长度超过400字（{len(q_text)}）"))
    if any(len(opt) > 200 for opt in opts):
        issues.append(_mk_issue("HARD_OPTION_LEN", "options", "存在选项长度超过200字"))
    # Length limit 400 applies to 教材原文 (quoted textbook part) only, not the whole explanation; no whole-explanation length check here.

    # Option length balance (Judge 4.6: max-min >= 15 -> warning)
    if len(opts) >= 2:
        lengths = [len(o) for o in opts]
        if max(lengths) - min(lengths) >= 15:
            issues.append(
                _mk_issue(
                    "HARD_OPTION_BALANCE",
                    "options",
                    "选项长度不均衡：最长与最短字数差≥15，可能影响测量公平性",
                    severity="warning",
                )
            )

    # Calculation question: if options contain decimal, stem must state precision (Judge 4.9)
    if is_calculation and opts:
        opt_text = " ".join(opts)
        has_decimal = bool(re.search(r"\d+\.\d+", opt_text))
        if has_decimal and not re.search(r"(保留到?\s*\d+\s*位小数|精确到?\s*\d+\s*位小数)", q_text):
            issues.append(
                _mk_issue(
                    "HARD_CALC_PRECISION",
                    "question",
                    "数值题缺少“保留位数说明”：选项含小数时题干需标注保留到几位小数或精确到几位小数",
                    suggested_fix="在题干中增加“保留到X位小数”或“精确到X位小数”的说明",
                )
            )

    # Year rule: if KB has no year, question/explanation must not include year
    kb_text = str(kb_context or "")
    if not _has_year(kb_text):
        if _has_year(q_text):
            issues.append(_mk_issue("HARD_YEAR", "question", "题干出现具体年份但教材未提及年份"))
        if _has_year(exp_text):
            issues.append(_mk_issue("HARD_YEAR", "explanation", "解析出现具体年份但教材未提及年份"))

    # Explanation three-section structure and conclusion format (per 试题解析三段论)
    if exp_text.strip():
        sections, strict = _find_expl_sections(exp_text)
        required = ["教材原文", "试题分析", "结论"]
        if not all(s in sections for s in required):
            issues.append(
                _mk_issue(
                    "HARD_EXPL_STRUCT",
                    "explanation",
                    "解析缺少三段式（教材原文/试题分析/结论）",
                    suggested_fix="按顺序补全三个段落：教材原文（路由前三个标题即目标题内容+分级+原文，不要写「目标题：」字样）、试题分析、结论【本题答案为…】",
                )
            )
        else:
            order = [sections[s][0] for s in required]
            if order != sorted(order):
                issues.append(
                    _mk_issue(
                        "HARD_EXPL_STRUCT",
                        "explanation",
                        "解析三段顺序不正确",
                        suggested_fix="将段落顺序调整为：教材原文 → 试题分析 → 结论",
                    )
                )
            if not strict:
                issues.append(
                    _mk_issue(
                        "HARD_EXPL_STRUCT",
                        "explanation",
                        "解析三段未显式分段（需独立成段）",
                        suggested_fix="在“教材原文”“试题分析”“结论”处独立成段，行首显式标注",
                    )
                )

            # Each of 教材原文/试题分析/结论 must appear exactly once (three-part rule: 有且只有一个)
            section_header_matches = _EXPL_SECTION_HEADER_RE.findall(exp_text)
            counts = Counter(section_header_matches)
            duplicated = [name for name in ["教材原文", "试题分析", "结论"] if counts.get(name, 0) >= 2]
            if duplicated:
                issues.append(
                    _mk_issue(
                        "HARD_EXPL_DUPLICATE_SECTION",
                        "explanation",
                        "解析三段论每段只能有且只有一个，当前存在重复："
                        + "、".join(duplicated),
                        suggested_fix="合并重复段落，使“1、教材原文”“2、试题分析”“3、结论”各只出现一次，结论段以本题答案为X收束",
                    )
                )

            # 教材原文：须含目标题内容（路由前三个标题）+分级，字数尽量≤400（贝经堂必须）
            textbook_body = _get_section_body(exp_text, sections, "教材原文", required)
            if textbook_body:
                if not _LEVEL_KEYWORDS_RE.search(textbook_body):
                    issues.append(
                        _mk_issue(
                            "HARD_EXPL_TEXTBOOK",
                            "explanation",
                            "教材原文段须包含分级（如掌握/了解/熟悉）",
                            suggested_fix="在教材原文段中标明知识点分级，例如“（掌握）”或“了解”",
                        )
                    )
                if len(textbook_body) > 400:
                    issues.append(
                        _mk_issue(
                            "HARD_EXPL_TEXTBOOK_LEN",
                            "explanation",
                            f"教材原文段建议控制在400字以内（当前{len(textbook_body)}字）",
                            severity="warning",
                            suggested_fix="精简教材原文，保留主题句或关键句，总字数≤400",
                        )
                    )

            # 试题分析：多选题须对所有答案都进行解释（第二张图要求）
            analysis_body = _get_section_body(exp_text, sections, "试题分析", required)
            ans_raw = str(answer or "").strip().upper()
            if target_type == "多选题" and analysis_body and len(ans_raw) > 1:
                missing = []
                for letter in ans_raw:
                    if letter not in "ABCDEFGH":
                        continue
                    # Option must be explicitly referred to: 选项A, A项, 选A, A正确, A错误, A是, etc.
                    if not re.search(
                        r"选项\s*[" + letter + r"]|[" + letter + r"]\s*项|选\s*[" + letter + r"]|"
                        r"[" + letter + r"]\s*[正确错误是]|[" + letter + r"]\s*[，。、]",
                        analysis_body,
                    ):
                        missing.append(letter)
                if missing:
                    issues.append(
                        _mk_issue(
                            "HARD_EXPL_ANALYSIS_MC",
                            "explanation",
                            f"多选题试题分析须对每个正确选项都解释，缺少对选项{''.join(missing)}的说明",
                            suggested_fix=f"在试题分析中补充对选项{', '.join(missing)}的解释",
                        )
                    )

            # Conclusion checks
            conclusion_start = sections["结论"][1]
            conclusion_text = exp_text[conclusion_start:]
            ans_extracted = _extract_conclusion_answer(conclusion_text)
            if not ans_extracted:
                issues.append(
                    _mk_issue(
                        "HARD_EXPL_CONCLUSION",
                        "explanation",
                        "结论缺少“本题答案为X”",
                        suggested_fix="在结论段末尾写“本题答案为…”；判断题用正确/错误，选择题用字母（如A或ACD）",
                    )
                )
            elif not _conclusion_ends_with_answer(conclusion_text):
                issues.append(
                    _mk_issue(
                        "HARD_EXPL_CONCLUSION",
                        "explanation",
                        "结论未以“本题答案为X”收束",
                        suggested_fix="将结论段最后一句改为以“本题答案为X”结尾",
                    )
                )
            # Conclusion format: use "本题答案为X" only (no 【】 brackets per product requirement)
            # No HARD_EXPL_CONCLUSION_FMT check; "本题答案为X" is the canonical format.

            if ans_extracted and target_type in ["判断题", "单选题", "多选题"]:
                ans = ans_extracted
                if target_type == "判断题":
                    if ans in ["A", "B"]:
                        issues.append(
                            _mk_issue(
                                "HARD_EXPL_CONCLUSION",
                                "explanation",
                                "判断题结论不得写A/B，应写正确/错误",
                                suggested_fix="将结论改为本题答案为正确或本题答案为错误",
                            )
                        )
                    elif ans not in ["正确", "错误"]:
                        issues.append(
                            _mk_issue(
                                "HARD_EXPL_CONCLUSION",
                                "explanation",
                                "判断题结论应为正确/错误",
                                suggested_fix="将结论改为本题答案为正确或本题答案为错误",
                            )
                        )
                else:
                    if ans in ["正确", "错误"]:
                        issues.append(
                            _mk_issue(
                                "HARD_EXPL_CONCLUSION",
                                "explanation",
                                "选择题结论应写字母答案",
                                suggested_fix="将结论改为字母答案，如本题答案为A或本题答案为ACD",
                            )
                        )

    return issues
