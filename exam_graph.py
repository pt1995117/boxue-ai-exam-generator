import os
import json
import operator
import random
import re
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Annotated, List, Dict, Optional, TypedDict, Union, Any, Tuple
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field
from openai import OpenAI
from volcenginesdkarkruntime import Ark

from hard_rules import (
    apply_numeric_options_ascending,
    replace_single_quotes_in_final_json,
    sanitize_media_payload,
    validate_media_rules,
    validate_hard_rules,
)

# Reuse existing config loading
from exam_factory import (
    API_KEY,
    BASE_URL,
    MODEL_NAME,
    ROUTER_MODEL,
    SPECIALIST_MODEL,
    WRITER_MODEL,
    CALC_MODEL,
    CRITIC_API_KEY,
    CRITIC_BASE_URL,
    CRITIC_MODEL,
    CRITIC_PROVIDER,
    CODE_GEN_MODEL,
    CODE_GEN_API_KEY,
    CODE_GEN_BASE_URL,
    CODE_GEN_PROVIDER,
    ARK_API_KEY,
    VOLC_ACCESS_KEY_ID,
    VOLC_SECRET_ACCESS_KEY,
    ARK_BASE_URL,
    ARK_PROJECT_NAME,
    KnowledgeRetriever,
    KB_PATH,
    HISTORY_PATH,
)

MAX_QUESTION_RETRY_ROUNDS = 3

CALCULATION_GUIDE = """
# 计算规则说明（用于动态生成Python代码）

根据参考材料中的计算规则，动态生成Python代码来计算结果。以下是常见的计算规则示例：

## 代码生成要求
1. **从题干或参考材料中提取具体数值**（必须是数字，不能是描述性文字）
2. **严格按照教材规则编写计算逻辑**
3. **处理边界情况**（如除零检查、条件判断）
4. **代码应该是独立的Python代码片段**，可以直接执行
5. **最后将结果赋值给变量 `result`**，例如：`result = ...`
6. **如果有多个步骤，可以分步计算，最后得到最终结果**

## 常见计算规则示例

以下是常见的计算规则示例，你可以参考这些模式生成代码：

## 1. 商业贷款计算
- **规则**: 商业贷款额度 = 评估价 × 贷款成数
- **代码示例**: `result = evaluation_price * loan_ratio`

## 2. 公积金贷款计算
- **规则**: 市属公积金可贷额度 = (申请人余额 + 共同申请人余额) × 倍数 × 缴存年限系数
- **代码示例**: `result = (balance_applicant + balance_co_applicant) * multiple * year_coefficient`

## 3. 增值税及附加计算
- **规则**: 
  - 非住宅：差额征收，(计税价 - 原值) / 1.05 × 5.3%
  - 住宅满2年且普通住宅：免征，0
  - 住宅满2年但非普通：差额征收，(计税价 - 原值) / 1.05 × 5.3%
  - 住宅不满2年：全额征收，计税价 / 1.05 × 5.3%
- **代码示例**: 
```python
vat_rate = 0.053
if not is_residential:
    result = (price - original_price) / 1.05 * vat_rate
elif years_held >= 2:
    if is_ordinary:
        result = 0  # 免征
    else:
        result = (price - original_price) / 1.05 * vat_rate
else:
    result = price / 1.05 * vat_rate
```

## 4. 契税计算
- **规则**: 
  - 非住宅：计税价 × 3%
  - 住宅首套：面积≤140㎡为1%，>140㎡为1.5%
  - 住宅二套：面积≤140㎡为1%，>140㎡为2%
  - 住宅三套及以上：3%
- **代码示例**:
```python
if not is_residential:
    result = price * 0.03
elif is_first_home:
    result = price * 0.01 if area <= 140 else price * 0.015
elif is_second_home:
    result = price * 0.01 if area <= 140 else price * 0.02
else:  # 三套及以上
    result = price * 0.03
```

## 5. 土地出让金计算
- **经济适用房**: 2008-04-11前购买为网签价×10%，之后为(网签价-原购价)×70%
- **按经适房管理**: 较高值×3%
- **已购公房**: 面积×成本价×1%（成本价默认1560元/㎡）
- **代码示例（已购公房）**: `result = area * cost_price * 0.01`

## 6. 房龄计算
- **通用房龄**: 房龄 = 当前年份 - 竣工年份
- **贷款用房龄**: 房龄 = 50 - (当前年份 - 竣工年份)（用于"房龄+贷款年限≤50年"规则）
- **代码示例**: `result = current_year - completion_year` 或 `result = 50 - (current_year - completion_year)`

## 7. 其他常见计算
- **土地剩余年限**: `result = total_years - (current_year - grant_year)`
- **室内净高**: `result = floor_height - slab_thickness`
- **建筑面积**: `result = inner_area + shared_area`
- **得房率**: `result = (inner_use_area / building_area) * 100`（注意除零检查）
- **面积误差比**: `result = (registered_area - contract_area) / contract_area * 100`
- **价差率**: `result = abs((listing_price - deal_price) / deal_price) * 100`
- **容积率**: `result = total_building_area / total_land_area`
- **绿地率**: `result = (green_area / total_land_area) * 100`

## 旧版函数说明（仅供参考，请使用动态代码生成）
- calculate_loan_amount(evaluation_price, loan_ratio)
  - 作用：商业贷款额度=评估价×贷款成数
  - 适用：题干给出评估价/成交价与贷款成数，要求商业贷款金额
  - 不适用：公积金贷款；缺少评估价或贷款成数
- calculate_provident_fund_loan(balance_applicant, balance_co_applicant, multiple, year_coefficient)
  - 作用：市属公积金可贷额度=双方余额之和×倍数×缴存年限系数
  - 适用：题干给出缴存余额、倍数、年限系数，要求公积金可贷额度
  - 注意：结果仍需与最高/保底额度取低值（题目需说明）
- calculate_vat(price, original_price, years_held, is_ordinary, is_residential)
  - 作用：测算增值税及附加（住宅满2且普通免征，否则差额/全额；非住宅差额）
  - 适用：题干关键信息包括持有年限、是否普通住宅、计税价/原值
  - 不适用：缺少住宅属性或持有年限，无法判断免税条件时请勿调用
- calculate_deed_tax(price, area, is_first_home, is_second_home, is_residential)
  - 作用：测算契税；住宅按面积与套数分档，非住宅3%
  - 适用：题干给出计税价、面积、套数属性（首套/二套/三套+）
  - 不适用：未提供套数或面积
- calculate_land_grant_fee_economical(price, original_price, buy_date_is_before_2008_4_11)
  - 作用：经济适用房转让补缴土地出让金（2008-04-11前10%，之后差额70%）
  - 适用：题干明确购房时间（是否早于2008-04-11）、网签/核定价、原购价
  - 不适用：未给购房时间或房屋类型非经适房
- calculate_land_grant_fee_managed_economical(price)
  - 作用：按经适房管理住房出让金=较高值×3%
  - 适用：题干明确“按经适房管理”且给出计税较高值
- calculate_land_grant_fee_public_housing(area, cost_price=1560)
  - 作用：已购公房土地出让金（成本法）=面积×成本价×1%
  - 适用：题干给出建筑面积及当年成本价（或可用默认1560元/㎡）
  - 注意：cost_price必须是数值
- calculate_land_remaining_years(total_years, current_year, grant_year)
  - 作用：土地剩余年限=出让总年限-(当前年份-出让年份)
  - 适用：题干给出出让年限与出让年份
- calculate_house_age(current_year, completion_year, for_loan=False)
  - 作用：房龄；for_loan=True 时用于贷款年限规则“房龄+贷款年限≤50年”
  - 适用：题干提供竣工年份/建成年代（贷款题请用 for_loan=True）
- calculate_indoor_height(floor_height, slab_thickness)
  - 作用：室内净高=层高-楼板厚度
  - 适用：题干给出层高与板厚
- calculate_building_area(inner_area, shared_area)
  - 作用：建筑面积=套内面积+公摊面积
  - 适用：题干同时给出两项
- calculate_efficiency_rate(inner_use_area, building_area)
  - 作用：得房率=套内使用面积/建筑面积×100%，建筑面积不可为0
  - 适用：题干给出套内面积与建筑面积
- calculate_area_error_ratio(registered_area, contract_area)
  - 作用：面积误差比=(登记面积-合同面积)/合同面积×100%，合同面积不可为0
  - 适用：题干给出两种面积数据
- calculate_price_diff_ratio(listing_price, deal_price)
  - 作用：价差率=|挂牌价-成交价|/成交价×100%，成交价不可为0
  - 适用：题干给出挂牌价与成交价
- calculate_plot_ratio(total_building_area, total_land_area)
  - 作用：容积率=总建筑面积/总用地面积，用地面积不可为0
  - 适用：规划/合规类题目
- calculate_green_rate(green_area, total_land_area)
  - 作用：绿地率=绿化面积/总用地面积×100%，用地面积不可为0
  - 适用：规划/合规类题目
"""

CALC_PARAMETER_GROUNDING_GUIDE = """
# 参数来源与口径锁定（通用硬约束）
1. 所有参与计算的参数必须能在题干或教材规则中定位来源；禁止使用“隐含默认值”。
2. 对“系数/口径/时点”类参数必须显式锁定：
   - 系数来源：题干直接给定，或由题干给定的原始指标按教材规则推导得到；
   - 统计口径：明确是“部分额度”还是“总额度”、是“中间量”还是“最终量”；
   - 时间口径：明确是月初/月末/当期累计/期末口径中的哪一个。
3. 若关键参数来源不完整或口径不唯一，禁止硬算；应先改写题干补齐条件再计算。
4. 解析必须逐步标注参数来源与口径，不得只给结果。
"""

# Standard blank bracket: full-width parentheses with one full-width space (U+3000) inside
BLANK_BRACKET = "（\u3000）"
ENDING_PUNCTUATION_CHARS = "。．？！?!；;：:，,、"

def normalize_blank_brackets(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    # Normalize any empty bracket (half/full-width space inside) to Chinese format with full-width space only
    def _repl(m: "re.Match") -> str:
        return BLANK_BRACKET
    return re.sub(r"\s*[(（]\s*[)）]\s*", _repl, text)

def has_invalid_blank_bracket(text: str) -> bool:
    """Check if any placeholder bracket (empty or whitespace-only) is not exactly BLANK_BRACKET. Used for options."""
    if not isinstance(text, str) or not text:
        return False
    for match in re.finditer(r"[(（][)）]|[(（][ \s\u3000]*[)）]", text):
        if match.group(0) != BLANK_BRACKET:
            return True
    if re.search(r"\s" + re.escape(BLANK_BRACKET) + r"|" + re.escape(BLANK_BRACKET) + r"\s", text):
        return True
    return False


def has_invalid_ending_blank_bracket(text: str) -> bool:
    """Only the bracket at the end of the stem must have exactly one full-width space (U+3000) inside—not zero, not multiple; other brackets are not constrained."""
    if not isinstance(text, str) or not text:
        return False
    matches = list(re.finditer(r"[(（][)）]|[(（][ \s\u3000]*[)）]", text))
    if not matches:
        return False
    last = matches[-1]
    if last.group(0) != BLANK_BRACKET:
        return True
    # No spaces allowed around the ending bracket
    start, end = last.start(), last.end()
    if start > 0 and text[start - 1].isspace():
        return True
    if end < len(text) and text[end].isspace():
        return True
    return False


def has_forbidden_symbol_before_ending_blank_bracket(text: str) -> bool:
    if not isinstance(text, str) or not text:
        return False
    idx = text.rfind(BLANK_BRACKET)
    if idx <= 0:
        return False
    prev = text[idx - 1]
    return prev in ENDING_PUNCTUATION_CHARS or prev.isspace()


def detect_option_hierarchy_conflict(
    final_json: Dict[str, Any],
    kb_context: str,
    question_type: str,
) -> Tuple[bool, List[Dict[str, Any]], str]:
    """
    Detect parent/child (hierarchy) conflicts among options for single/multiple choice questions.

    This mirrors the离线 Judge DeterministicFilter._check_option_hierarchy_conflict 逻辑，
    但以结构化形式返回检测结果，供 Critic 作为“疑似多解风险”维度使用。
    """
    flag = False
    pairs: List[Dict[str, Any]] = []
    message = ""

    if question_type not in ["单选题", "多选题"]:
        return flag, pairs, message

    # Collect non-empty options and strip leading labels like "A. "
    option_texts: List[str] = []
    option_labels: List[str] = []
    for idx in range(1, 9):
        raw = str(final_json.get(f"选项{idx}", "") or "").strip()
        if not raw:
            continue
        # Remove leading letter + punctuation, similar to DeterministicFilter
        clean = re.sub(r"^[A-Ha-h][\.．、:：]\s*", "", raw).strip()
        option_texts.append(clean)
        option_labels.append(chr(64 + idx))  # 1->A, 2->B...

    if len(option_texts) < 2:
        return flag, pairs, message

    textbook = str(kb_context or "")
    if not textbook.strip():
        return flag, pairs, message

    n = len(option_texts)
    for i in range(n):
        for j in range(i + 1, n):
            a = option_texts[i]
            b = option_texts[j]
            if not a or not b or a == b:
                continue

            # a is parent, b is child
            p1 = rf"{re.escape(a)}中[^。；\n]{{0,80}}(?:称为|称作|属于|包括|包含)[^。；\n]{{0,60}}{re.escape(b)}"
            # b is parent, a is child
            p2 = rf"{re.escape(b)}中[^。；\n]{{0,80}}(?:称为|称作|属于|包括|包含)[^。；\n]{{0,60}}{re.escape(a)}"
            # X 属于 Y （更宽松兜底）
            p3 = rf"{re.escape(a)}[^。；\n]{{0,20}}属于[^。；\n]{{0,20}}{re.escape(b)}"
            p4 = rf"{re.escape(b)}[^。；\n]{{0,20}}属于[^。；\n]{{0,20}}{re.escape(a)}"

            relation = None
            parent = ""
            child = ""
            if re.search(p1, textbook) or re.search(p4, textbook):
                relation = "parent_child"
                parent, child = a, b
            elif re.search(p2, textbook) or re.search(p3, textbook):
                relation = "parent_child"
                parent, child = b, a

            if relation:
                flag = True
                pairs.append(
                    {
                        "option_indices": [i + 1, j + 1],
                        "option_labels": [option_labels[i], option_labels[j]],
                        "parent": parent,
                        "child": child,
                        "relation": relation,
                    }
                )

    if flag:
        option_pairs_desc = "; ".join(
            [
                f"{p['option_labels'][0]}:{p['parent']} / {p['option_labels'][1]}:{p['child']}"
                for p in pairs
            ]
        )
        message = (
            "单选/多选题选项疑似层级冲突（父/子类或上下位关系），"
            "需结合题干与教材规则判断是否存在多解风险。"
        )
        if option_pairs_desc:
            message += f" 触发组合: {option_pairs_desc}"

    return flag, pairs, message

def build_candidate_sentences(stem: str, options: List[str]) -> List[Dict[str, Any]]:
    """
    Build sentences by replacing the standard blank bracket in stem with each option.
    Used for stem+option readability checks in Writer/Critic.
    option_label: A/B/C/D/... so Critic and UI can refer to the same option without confusion.
    """
    results: List[Dict[str, Any]] = []
    if not isinstance(stem, str) or BLANK_BRACKET not in stem:
        return results
    for idx, opt in enumerate(options or [], 1):
        opt_text = str(opt or "").strip()
        if not opt_text:
            continue
        # index 1 = A, 2 = B, ... 26 = Z
        option_label = chr(ord("A") + idx - 1) if 1 <= idx <= 26 else str(idx)
        sentence = stem.replace(BLANK_BRACKET, opt_text)
        results.append(
            {
                "index": idx,
                "option_label": option_label,
                "option": opt_text,
                "sentence": sentence,
            }
        )
    return results


def _calc_result_grounded_in_output(calc_result: Any, final_json: Dict[str, Any]) -> bool:
    if calc_result is None or not isinstance(final_json, dict):
        return False
    try:
        numeric = float(calc_result)
    except Exception:
        return False
    texts = [
        str(final_json.get("题干", "") or ""),
        str(final_json.get("解析", "") or ""),
    ]
    for i in range(1, 9):
        texts.append(str(final_json.get(f"选项{i}", "") or ""))
    haystack = " ".join(texts)
    candidates = {
        str(int(numeric)) if float(numeric).is_integer() else "",
        f"{numeric:.1f}",
        f"{numeric:.2f}",
        str(numeric),
    }
    candidates = {x for x in candidates if x}
    return any(token in haystack for token in candidates)


def _judgment_answer_consistent(final_json: Dict[str, Any]) -> bool:
    if not isinstance(final_json, dict):
        return False
    answer = str(final_json.get("正确答案", "") or "").strip().upper()
    explanation = str(final_json.get("解析", "") or "")
    if answer == "A":
        return "本题答案为正确" in explanation
    if answer == "B":
        return "本题答案为错误" in explanation
    return False


def _choice_tail_unit_label(unit_text: str) -> str:
    raw = str(unit_text or "").strip()
    mapping = {
        "套": "套数",
        "年": "年限",
        "次": "次数",
        "天": "天数",
        "户": "户数",
        "人": "人数",
        "名": "人数",
        "%": "百分比",
        "％": "百分比",
        "元": "金额（元）",
        "万元": "金额（万元）",
        "平方米": "面积（平方米）",
        "㎡": "面积（平方米）",
    }
    return mapping.get(raw, raw)


def _normalize_choice_tail_unit(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    unit_match = re.search(
        rf"{re.escape(BLANK_BRACKET)}\s*([A-Za-z%％㎡\u4e00-\u9fff]{{1,8}})\s*[。．？！?!；;：:，,、\s]*$",
        text,
    )
    if not unit_match:
        return text
    raw_unit = unit_match.group(1).strip()
    if not raw_unit:
        return text
    normalized_unit = _choice_tail_unit_label(raw_unit)
    prefix = text[: unit_match.start()]
    prefix = re.sub(rf"[{re.escape(ENDING_PUNCTUATION_CHARS)}\s]*$", "", prefix)
    return f"{prefix}{normalized_unit}{BLANK_BRACKET}。"


def _normalize_judgment_affirmative_stem(text: str) -> str:
    """Normalize common interrogative true/false stems into affirmative declarative stems."""
    if not isinstance(text, str) or not text:
        return text
    t = text.strip()
    # Canonical replacements for the most common patterns seen online.
    t = re.sub(r"(以下|下列|上述|以上)?(说法|表述|做法|观点)?\s*是否正确$", r"\1\2正确", t)
    t = re.sub(r"(以下|下列|上述|以上)?(说法|表述|做法|观点)?\s*是否错误$", r"\1\2错误", t)
    t = re.sub(r"\s*是否正确$", "正确", t)
    t = re.sub(r"\s*是否错误$", "错误", t)
    t = re.sub(r"\s*对不对$", "正确", t)
    t = re.sub(r"\s*是不是正确$", "正确", t)
    t = re.sub(r"\s*是不是错误$", "错误", t)
    # "XX的做法是正确的" -> "XX做法正确"
    t = re.sub(r"的做法是(正确|错误)的$", r"做法\1", t)
    # Remove residual terminal interrogative particles.
    t = re.sub(r"[吗么]\s*$", "", t)
    return t.strip()


def _has_judgment_interrogative_tail(stem: str) -> bool:
    if not isinstance(stem, str):
        return False
    s = stem.strip()
    return bool(
        re.search(
            r"(是否(正确|错误)|是不是(正确|错误)|对不对|吗|么)\s*$",
            s,
        )
    )

def _readability_reason_grounded_in_candidate(
    bad_item: Dict[str, Any], candidate_sentences: List[Dict[str, Any]]
) -> bool:
    """
    Return True only if the bad_item's reason appears to refer to content that actually
    exists in the corresponding candidate (option/sentence). Used to avoid failing on
    hallucinated or 张冠李戴 content (e.g. critic citing 解析 or another question).
    """
    by_index = {c["index"]: c for c in candidate_sentences}
    idx = bad_item.get("index")
    if idx is None:
        ol = bad_item.get("option_label")
        idx = ord(ol) - ord("A") + 1 if (ol and len(ol) == 1 and "A" <= ol <= "Z") else None
    candidate = by_index.get(idx) if idx is not None else None
    if not candidate:
        return False
    candidate_text = (candidate.get("option") or "") + (candidate.get("sentence") or "")
    reason_text = (bad_item.get("reason") or "").strip()
    if not reason_text:
        return True
    # Quoted phrases in reason must appear in this candidate (no 张冠李戴/hallucination)
    import re
    quoted = re.findall(r"[「\"'][^」\"']+[」\"']", reason_text)
    for q in quoted:
        inner = q[1:-1].strip()
        if len(inner) >= 4 and inner not in candidate_text:
            return False
    # Long substantive runs (8+ chars) in reason must appear in candidate; skip meta terms
    meta_stop = ("不自然", "拗口", "过长", "结构松散", "语义", "逻辑", "通顺", "流畅", "读起来", "表达", "定语", "句式", "牵强", "困惑")
    for m in re.finditer(r"[\u4e00-\u9fff]{8,}", reason_text):
        phrase = m.group(0)
        if phrase in candidate_text:
            continue
        if any(phrase.startswith(s) or s in phrase for s in meta_stop):
            continue
        return False
    return True

def enforce_question_bracket_and_punct(text: str, target_type: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    t = normalize_blank_brackets(text.strip())
    # Remove leading bracket if present
    t = re.sub(r"^" + re.escape(BLANK_BRACKET), "", t).lstrip()
    if target_type in ["单选题", "多选题"]:
        t = _normalize_choice_tail_unit(t)
        # Canonical ending for choice questions: exact "...（　）。"
        # If the draft ends like "...（　）套/元/年..." the placeholder is not at the real end.
        # Strip the whole illegal tail and rebuild a single canonical ending placeholder.
        t = re.sub(
            rf"{re.escape(BLANK_BRACKET)}[^\u4e00-\u9fffA-Za-z0-9%％]*[\u4e00-\u9fffA-Za-z0-9%％]{{1,6}}[。．？！?!；;：:，,、\s]*$",
            "",
            t,
        )
        t = re.sub(rf"[{re.escape(ENDING_PUNCTUATION_CHARS)}\s]*$", "", t)
        t = re.sub(rf"{re.escape(BLANK_BRACKET)}\s*$", "", t)
        t = re.sub(r"[（(][ \s\u3000]*[）)]\s*$", "", t)
        t = re.sub(rf"[{re.escape(ENDING_PUNCTUATION_CHARS)}\s]*$", "", t)
        t = f"{t}{BLANK_BRACKET}。"
    elif target_type == "判断题":
        # Ensure judgement stems are declarative and end with one canonical blank bracket.
        core = re.sub(r"[(（][ \s\u3000]*[)）]", "", t)
        core = core.replace(BLANK_BRACKET, "")
        core = re.sub(r"[。．？！?!；;：:，,、\s]+$", "", core).strip()
        core = _normalize_judgment_affirmative_stem(core)
        core = re.sub(r"[。．？！?!；;：:，,、\s]+$", "", core).strip()
        t = f"{core}{BLANK_BRACKET}" if core else BLANK_BRACKET
    return t

def validate_question_template_semantics(question: str, target_type: str) -> List[str]:
    """Check question stem meets basic semantics: declarative, proper punctuation, (　) placeholder.
    Does NOT require a single fixed ending phrase; recommend but do not enforce '以下表述正确/错误的是（　）。' etc."""
    issues: List[str] = []
    q = (question or "").strip()
    if not q or target_type not in ["单选题", "多选题", "判断题"]:
        return issues
    # Single/Multiple choice: only require declarative sentence, period at end, and (　) placeholder.
    # No fixed-ending check; recommend "以下表述正确/错误的是（　）。" etc. in prompts only.
    if target_type in ["单选题", "多选题"]:
        # Already enforced in validate_writer_format: ends with 。, has BLANK_BRACKET, ends with ）。.
        pass
    # True/False: stem must contain conclusion anchor 正确/错误 (Judge deterministic_filter)
    elif target_type == "判断题":
        stem_no_blank = q.replace(BLANK_BRACKET, "")
        stem_no_blank = re.sub(r"[。．？！?!；;：:，,、\s]+$", "", stem_no_blank).strip()
        if "正确" not in q and "错误" not in q:
            issues.append("判断题题干需包含结论锚点（正确或错误）")
        if _has_judgment_interrogative_tail(stem_no_blank):
            issues.append("判断题题干应使用肯定陈述句，避免“是否正确/对不对”等疑问句")
    return issues


def validate_writer_format(question: str, options: List[str], answer, target_type: str) -> List[str]:
    issues = []
    q = question or ""
    if has_invalid_ending_blank_bracket(q):
        issues.append("题干结尾括号中间必须有且仅有一个全角空格（不能多）")
    if target_type in ["单选题", "多选题"] and has_forbidden_symbol_before_ending_blank_bracket(q):
        issues.append("题干结尾作答括号前不能有任何符号或空格")
    if target_type in ["单选题", "多选题", "判断题"]:
        if BLANK_BRACKET not in q:
            issues.append("题干缺少标准占位括号（须为全角括号且括号内有且仅有一个全角空格）")
    # Judge DeterministicFilter: 选择题题干作答占位括号（ ）只能出现一次
    if target_type in ["单选题", "多选题"] and q.count(BLANK_BRACKET) > 1:
        issues.append("选择题题干作答占位括号（ ）只能出现一次")
    if target_type in ["单选题", "多选题"]:
        if not q.endswith("。"):
            issues.append("选择题题干未以句号结尾")
        # 题干结尾括号+句号的强约束：必须精确为“（　）。”，括号前后不得有多余空格
        if BLANK_BRACKET in q and not re.search(rf"{re.escape(BLANK_BRACKET)}。$", q):
            issues.append("选择题题干结尾必须为“（　）。”，括号前后不得有多余空格")
    if target_type == "判断题":
        if not q.endswith(BLANK_BRACKET):
            issues.append("判断题题干未以括号结尾")
    issues.extend(validate_question_template_semantics(q, target_type))
    # Validate options bracket formatting if present
    for opt in options or []:
        opt_str = str(opt)
        if has_invalid_blank_bracket(opt_str):
            issues.append("选项括号格式不规范")
            break
    # Trailing punctuation check
    for opt in options or []:
        if re.search(r"[。！？；;：:，,、]\s*$", str(opt)):
            issues.append("选项末尾含标点")
            break
    # Validate answer format
    if target_type == "判断题":
        if not (isinstance(answer, str) and answer.strip().upper() in ["A", "B"]):
            issues.append("判断题答案格式应为A/B")
    elif target_type == "单选题":
        if not (isinstance(answer, str) and re.fullmatch(r"[A-Ha-h]", answer.strip())):
            issues.append("单选题答案格式应为单个字母")
    elif target_type == "多选题":
        if isinstance(answer, list):
            if not answer or not all(re.fullmatch(r"[A-Ha-h]", str(x).strip()) for x in answer):
                issues.append("多选题答案列表格式不规范")
        elif isinstance(answer, str):
            if not re.fullmatch(r"[A-Ha-h]{2,}", answer.strip()):
                issues.append("多选题答案格式应为多个字母")
        else:
            issues.append("多选题答案格式不规范")
    return issues


def _detect_option_prefix_in_draft(draft: Dict[str, Any]) -> List[str]:
    """Check raw draft options for A./B./C./D. prefix (Judge 4.6). Returns issue messages."""
    issues: List[str] = []
    if not isinstance(draft, dict):
        return issues
    options = draft.get("options") or []
    if not isinstance(options, list):
        return issues
    for i, opt in enumerate(options):
        s = str(opt or "").strip()
        if not s:
            continue
        if re.match(r"^\s*[A-Da-d][\.．、:：\s\)）]+", s):
            issues.append("选项内容前禁止再写 A/B/C/D 标签，请仅填写选项正文")
            break
    return issues


COMMON_SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁荀羊於惠甄麴家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公万俟司马上官欧阳夏侯诸葛闻人东方赫连皇甫尉迟公羊澹台公冶宗政濮阳淳于单于太叔申屠公孙仲孙轩辕令狐钟离宇文长孙慕容鲜于闾丘司徒司空丌官司寇子车颛孙端木巫马公西漆雕乐正壤驷公良拓跋夹谷宰父谷梁晋楚闫法汝鄢涂钦段干百里东郭南门呼延归海羊舌微生岳帅缑亢况后有琴梁丘左丘东门西门商牟佘佴伯赏南宫墨哈谯笪年爱阳佟"
SAFE_FALLBACK_NAMES = ["张伟", "李娜", "王强", "刘洋", "陈杰", "赵磊", "孙静", "周涛"]
FORBIDDEN_FUNNY_NAMES = {
    "张三", "李四", "王五", "赵六", "贾董事", "张漂亮", "甄真钱", "刘二", "张二", "小宝", "贝贝", "宝宝",
}
NEGATIVE_EVENT_KEYWORDS = (
    "违法", "违规", "违纪", "事故", "骗贷", "挪用", "处罚", "追责", "黑线", "红线", "搅单", "私单", "伪造", "篡改",
)

def _name_violations_in_text(text: str) -> List[str]:
    if not text:
        return []
    issues = []
    # 姓+女士/先生
    if re.search(rf"[{COMMON_SURNAMES}][\u4e00-\u9fff]{{0,1}}(女士|先生)", text):
        issues.append("使用了“姓+女士/先生”")
    # 小+常见姓氏（如小张/小李）——避免误伤“小区/小镇/小路”等非人名
    for m in re.finditer(rf"小[{COMMON_SURNAMES}](?:[\u4e00-\u9fff])?", text):
        token = m.group(0)
        if token.startswith(("小区", "小镇", "小路", "小巷", "小学", "小型")):
            continue
        issues.append("使用了“小+姓氏”称谓")
        break
    for name in FORBIDDEN_FUNNY_NAMES:
        if name in text:
            issues.append(f"使用了不规范姓名：{name}")
    if re.search(r"(小宝|贝贝|宝宝)", text):
        issues.append("使用了小名/乳名")
    return issues

def _is_judgement_style_stem(text: str) -> bool:
    if not text:
        return False
    stem = re.sub(r"\s+", "", str(text))
    patterns = [
        r"(以下|下列).{0,8}(表述|说法|选项).{0,12}(正确|错误)",
        r"(以下|下列).{0,8}关于.{0,20}(正确|错误)",
        r"判断.{0,30}(正确|错误|是否合法|是否合规)",
        r"(做法|行为|说法).{0,12}(正确|错误|是否合法|是否合规)",
        r"是否(正确|错误|合法|合规|违规|成立)",
    ]
    return any(re.search(p, stem) for p in patterns)


def _contains_anonymous_person_reference(text: str) -> bool:
    if not text:
        return False
    t = str(text)
    # 典型“张某/王某”类匿名指代
    if re.search(rf"[{COMMON_SURNAMES}]某(?:某)?(?!公司|银行|机构|单位|部门|分行|支行|小区|街道|路|号|市|区|县|省)", t):
        return True
    # “某某”匿名指代（排除明显组织/地点后缀）
    if re.search(r"某某(?!公司|银行|机构|单位|部门|分行|支行|小区|街道|路|号|市|区|县|省)", t):
        return True
    return False


def _has_negative_event_context(text: str) -> bool:
    t = str(text or "")
    return any(k in t for k in NEGATIVE_EVENT_KEYWORDS)


def _extract_person_like_names(text: str) -> set[str]:
    t = str(text or "")
    if not t:
        return set()
    names = set()
    # 常见“姓+1~2字名”及“姓某”结构（过滤地理组织后缀）
    for m in re.finditer(
        rf"[{COMMON_SURNAMES}](?:某|[\u4e00-\u9fff]{{1,2}})(?!公司|银行|机构|单位|部门|分行|支行|小区|街道|路|号|市|区|县|省)",
        t,
    ):
        token = m.group(0)
        if token in {"某市", "某区", "某县", "某省", "某路"}:
            continue
        names.add(token)
    return names


def _repair_name_style(text: str, force_named: bool = False) -> str:
    if not text:
        return text
    repaired = str(text)
    # 统一替换称谓式/小+姓氏命名
    repaired = re.sub(rf"([{COMMON_SURNAMES}])[\u4e00-\u9fff]{{0,1}}(女士|先生)", r"\1伟", repaired)
    repaired = re.sub(rf"小([{COMMON_SURNAMES}])(?:[\u4e00-\u9fff])?", r"\1伟", repaired)
    for funny in FORBIDDEN_FUNNY_NAMES:
        repaired = repaired.replace(funny, "张伟")
    repaired = re.sub(r"(小宝|贝贝|宝宝)(?=$|[，。；：、\s])", "张伟", repaired)
    if force_named:
        # 判断“正确与否”类题干禁止匿名代称，统一改为通俗姓名
        repaired = re.sub(rf"([{COMMON_SURNAMES}])某(?:某)?(?!公司|银行|机构|单位|部门|分行|支行|小区|街道|路|号|市|区|县|省)", r"\1伟", repaired)
        repaired = re.sub(r"某某(?!公司|银行|机构|单位|部门|分行|支行|小区|街道|路|号|市|区|县|省)", "张伟", repaired)
    return repaired


def _repair_name_usage(text: str) -> str:
    if not text:
        return text
    # 替换“姓+女士/先生” → “某某”
    text = re.sub(rf"[{COMMON_SURNAMES}][\u4e00-\u9fff]{{0,1}}(女士|先生)", "某某", text)
    # 替换“小+姓氏” → “某某”（同上规则，避免替换“小区/小镇”等）
    text = re.sub(rf"小[{COMMON_SURNAMES}](?:[\u4e00-\u9fff])?(?=$|[，。；：、\s])", "某某", text)
    return text

def validate_name_usage(question: str, options: List[str], explanation: str) -> List[str]:
    issues = []
    issues += _name_violations_in_text(question or "")
    for opt in options or []:
        issues += _name_violations_in_text(str(opt))
    issues += _name_violations_in_text(explanation or "")
    stem = str(question or "")
    all_text = " ".join([stem] + [str(o or "") for o in (options or [])] + [str(explanation or "")])
    anonymous_present = any(_contains_anonymous_person_reference(t) for t in [stem] + [str(o or "") for o in (options or [])] + [str(explanation or "")])
    is_judgement = _is_judgement_style_stem(stem)
    if anonymous_present and is_judgement:
        issues.append("需要判断行为/说法正确与否时，不得使用“张某/某某”代称")
    if anonymous_present and (not _has_negative_event_context(all_text)):
        issues.append("非事故/违法违规场景不应使用“张某/某某”代称")

    stem_names = _extract_person_like_names(stem)
    if stem_names:
        option_names = set()
        for opt in options or []:
            option_names |= _extract_person_like_names(str(opt))
        if option_names and not option_names.issubset(stem_names):
            issues.append("选项中的人名与题干不一致")

    exp_names = _extract_person_like_names(str(explanation or ""))
    if stem_names and exp_names and not exp_names.issubset(stem_names):
        issues.append("解析中的人名与题干不一致")
    return list(dict.fromkeys(issues))

def validate_critic_format(final_json: Dict[str, Any], question_type: str) -> List[str]:
    issues = []
    if not isinstance(final_json, dict):
        return ["题目结构非字典"]
    q = str(final_json.get("题干", "") or "")
    options = []
    for i in range(1, 9):
        key = f"选项{i}"
        val = final_json.get(key)
        if val is not None and str(val) != "":
            options.append(str(val))
    answer = final_json.get("正确答案", "")
    # Only the ending placeholder bracket must have one full-width space inside
    if has_invalid_ending_blank_bracket(q):
        issues.append("题干结尾括号中间必须有且仅有一个全角空格（不能多）")
    if question_type in ["单选题", "多选题"] and has_forbidden_symbol_before_ending_blank_bracket(q):
        issues.append("题干结尾作答括号前不能有任何符号或空格")
    if question_type in ["单选题", "多选题", "判断题"]:
        if BLANK_BRACKET not in q:
            issues.append("题干缺少标准占位括号（须为全角括号且括号内有且仅有一个全角空格）")
    if question_type in ["单选题", "多选题"]:
        if not q.endswith("。"):
            issues.append("选择题题干未以句号结尾")
        if BLANK_BRACKET in q and not q.endswith("）。"):
            issues.append("选择题括号与句号位置不规范")
    if question_type == "判断题" and not q.endswith(BLANK_BRACKET):
        issues.append("判断题题干未以括号结尾")
    for opt in options:
        if has_invalid_blank_bracket(opt):
            issues.append("选项括号格式不规范")
            break
    for opt in options:
        if re.search(r"[。！？；;：:，,、]\s*$", opt):
            issues.append("选项末尾含标点")
            break
    if question_type == "判断题":
        if not (isinstance(answer, str) and re.fullmatch(r"[ABab]", str(answer).strip())):
            issues.append("判断题答案格式应为A/B")
    elif question_type == "单选题":
        if not (isinstance(answer, str) and re.fullmatch(r"[A-Ha-h]", str(answer).strip())):
            issues.append("单选题答案格式应为单个字母")
    elif question_type == "多选题":
        if not (isinstance(answer, str) and re.fullmatch(r"[A-Ha-h]{2,}", str(answer).strip())):
            issues.append("多选题答案格式应为多个字母")
    # Name usage checks (no 姓+女士/先生 or 小+姓氏)
    q_text = str(final_json.get("题干", "") or "")
    exp_text = str(final_json.get("解析", "") or "")
    name_issues = validate_name_usage(q_text, options, exp_text)
    if name_issues:
        issues.append("人名不规范（禁止称谓/小名）")
    issues += validate_media_rules(q_text, options, exp_text)
    return issues


def _parse_answer_labels(answer: Any) -> List[str]:
    if isinstance(answer, list):
        labels = [str(x).strip().upper() for x in answer if str(x).strip()]
    else:
        labels = re.findall(r"[A-H]", str(answer or "").upper())
    out: List[str] = []
    seen: set[str] = set()
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        out.append(label)
    return out


def _coerce_number(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(",", "").replace("，", "")
    text = re.sub(r"(万元|万|元|平方米|平米|㎡|套|户|分|年|个月|月|天|次|%)$", "", text)
    text = text.strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    try:
        return float(text)
    except Exception:
        return None


def _get_answer_option_payload(final_json: Dict[str, Any]) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    for label in _parse_answer_labels(final_json.get("正确答案", "")):
        idx = ord(label) - ord("A") + 1
        if not (1 <= idx <= 8):
            continue
        opt_text = str(final_json.get(f"选项{idx}", "") or "").strip()
        if opt_text:
            rows.append((label, opt_text))
    return rows


def _extract_primary_calc_result_from_explanation(explanation: str) -> Optional[float]:
    text = str(explanation or "")
    if not text:
        return None
    keyword_lines = [
        line.strip()
        for line in re.split(r"[\n。]", text)
        if any(keyword in line for keyword in ["计算过程为", "代入计算", "严格按公式计算", "按公式计算"])
    ]
    for line in keyword_lines:
        eq_matches = re.findall(r"=\s*(-?\d+(?:\.\d+)?)", line)
        if eq_matches:
            try:
                return float(eq_matches[-1])
            except Exception:
                continue
    patterns = [
        r"计算得\s*(-?\d+(?:\.\d+)?)",
        r"结果为\s*(-?\d+(?:\.\d+)?)",
        r"应为\s*(-?\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text)
        if matches:
            try:
                return float(matches[-1])
            except Exception:
                continue
    return None


def _numbers_close(left: Optional[float], right: Optional[float], tolerance: float = 1e-6) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def _extract_decimal_places(text: str) -> int:
    m = re.search(r"\.(\d+)", str(text or ""))
    return len(m.group(1)) if m else 0


def _calc_numeric_tolerance(question_text: str, selected_option_texts: List[str]) -> float:
    q = str(question_text or "")
    if re.search(r"(约为|约等于|大约|约合)", q):
        numeric_opts = sorted(
            {
                float(v)
                for v in (_coerce_number(x) for x in (selected_option_texts or []))
                if v is not None
            }
        )
        min_gap = None
        for i in range(1, len(numeric_opts)):
            gap = abs(numeric_opts[i] - numeric_opts[i - 1])
            if gap <= 0:
                continue
            min_gap = gap if min_gap is None else min(min_gap, gap)
        if min_gap is None:
            return 10.0 + 1e-9
        # “约为”题型允许小幅近似，但不应跨越到相邻干扰项
        return min(10.0, max(0.5, (min_gap / 2.0) - 1e-6)) + 1e-9

    m = re.search(r"(?:保留到?|精确到?)\s*(\d+)\s*位小数", q)
    if m:
        places = max(int(m.group(1)), 0)
        return 0.5 * (10 ** (-places)) + 1e-9

    decimals = [_extract_decimal_places(x) for x in (selected_option_texts or [])]
    max_decimals = max(decimals) if decimals else 0
    if max_decimals > 0:
        return 0.5 * (10 ** (-max_decimals)) + 1e-9

    # Integer options: allow half-step tolerance for rounding to nearest integer.
    return 0.5 + 1e-9


def _align_numeric_scales(value: Optional[float], targets: List[float]) -> List[float]:
    if value is None:
        return []
    if not targets:
        return [value]

    # Try common unit/percent scales: 元↔万元, 比例↔百分比, 千分比.
    factors = [1.0, 1 / 10000.0, 10000.0, 100.0, 0.01, 1000.0, 0.001]
    candidates = []
    for f in factors:
        try:
            candidates.append(value * f)
        except Exception:
            continue

    # Sort by closest distance to any target first.
    def _dist(v: float) -> float:
        return min(abs(v - t) for t in targets)

    uniq = []
    seen = set()
    for v in sorted(candidates, key=_dist):
        k = round(v, 12)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(v)
    return uniq


def _extract_calc_target_signature(question_text: str) -> str:
    q = str(question_text or "")
    q = re.sub(r"\s+", "", q)
    # Keep only a compact semantic signature for key calculated targets.
    rules = [
        ("组合贷款总额度", r"(组合贷款.*总额度|总额度)"),
        ("商业贷款部分额度", r"(商业贷款部分.*额度|商业贷款.*额度)"),
        ("季度分润", r"(季度分润)"),
        ("月度分润", r"(月度分润)"),
        ("超标款", r"(超标款|补交款|补交金额|补交额度)"),
        ("攀登指数", r"(攀登指数)"),
        ("总收入", r"(总收入)"),
        ("利润率", r"(利润率)"),
        ("利润", r"(利润)"),
        ("税额", r"(税额|税费)"),
    ]
    # 优先从作答位前的“设问主语”提取目标，避免把背景里的次要量也并入签名。
    blank_idx = q.find(BLANK_BRACKET)
    if blank_idx >= 0:
        window = q[max(0, blank_idx - 48): blank_idx]
        for name, pattern in rules:
            if re.search(pattern, window):
                return name
    for name, pattern in rules:
        if re.search(pattern, q):
            return name
    # fallback: first 24 chars without punctuation
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", q)[:24]


def _infer_calc_unit_hint(question_text: str, option_texts: Optional[List[str]] = None, explanation_text: str = "") -> str:
    text = "\n".join(
        [
            str(question_text or ""),
            *(str(x or "") for x in (option_texts or [])),
            str(explanation_text or ""),
        ]
    )
    if re.search(r"(万元|万\b)", text):
        return "万元"
    if re.search(r"(?<!万)元", text):
        return "元"
    if re.search(r"(㎡|平方米|平米)", text):
        return "平方米"
    if re.search(r"%|％", text):
        return "%"
    if re.search(r"(年|个月|月|天|次|套|户)", text):
        for unit in ["年", "个月", "月", "天", "次", "套", "户"]:
            if unit in text:
                return unit
    return ""


def _is_business_context_structural(stem: str) -> Tuple[bool, str]:
    """
    业务场景结构闸门（非关键词白名单）：
    - 至少包含一个业务主体（客户/经纪人/门店/业主/公司等）
    - 至少包含一个业务动作（咨询/签约/贷款/带看/审核/交割等）
    - 至少包含一个业务目标或约束（金额/年限/税费/合规/流程结果等）
    """
    s = str(stem or "").strip()
    if not s:
        return False, "题干为空"
    has_actor = bool(re.search(r"(客户|经纪人|业主|门店|分行|公司|主管|经理|团队|申请人)", s))
    has_action = bool(re.search(r"(咨询|签约|带看|办理|申请|审核|核验|支付|缴纳|交割|报备|沟通|推荐)", s))
    has_goal = bool(re.search(r"(金额|额度|年限|税费|风险|合规|结果|应当|是否|正确|错误|流程|条件|标准)", s))
    if has_actor and has_action and has_goal:
        return True, "结构判定通过"
    missing: List[str] = []
    if not has_actor:
        missing.append("业务主体")
    if not has_action:
        missing.append("业务动作")
    if not has_goal:
        missing.append("业务目标/约束")
    return False, f"结构判定缺少：{','.join(missing)}"


def _extract_required_calc_slots(kb_chunk: Dict[str, Any]) -> List[str]:
    """
    通用可解性契约：从切片结构中提取“计算必需输入槽位”。
    仅返回可读中文槽位，避免用单字母变量导致误报。
    """
    if not isinstance(kb_chunk, dict):
        return []
    struct = kb_chunk.get("结构化内容") if isinstance(kb_chunk.get("结构化内容"), dict) else {}
    key_params = [str(x).strip() for x in (struct.get("key_params") or []) if str(x).strip()]
    formulas = [str(x) for x in (struct.get("formulas") or []) if str(x).strip()]
    seed_terms = key_params[:]
    for f in formulas:
        left = f.split("=", 1)[0]
        for cand in re.split(r"[，,、/（）()\s:+\-×*÷=<>]+", left):
            c = str(cand).strip()
            if not c:
                continue
            if len(c) <= 1:
                continue
            if re.fullmatch(r"[A-Za-z]+", c):
                continue
            seed_terms.append(c)
    uniq: List[str] = []
    seen = set()
    for t in seed_terms:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return uniq[:12]


def _detect_missing_calc_slots(stem_text: str, required_slots: List[str]) -> List[str]:
    text = str(stem_text or "")
    missing: List[str] = []
    for slot in (required_slots or []):
        s = str(slot).strip()
        if not s:
            continue
        # 容忍“年限/额度/金额”等词尾差异
        pat = re.escape(s)
        if s.endswith("年限"):
            pat = re.escape(s.replace("年限", "")) + r"(年限|期限)?"
        elif s.endswith("额度"):
            pat = re.escape(s.replace("额度", "")) + r"(额度|金额)?"
        elif s.endswith("金额"):
            pat = re.escape(s.replace("金额", "")) + r"(金额|额度)?"
        if not re.search(pat, text):
            missing.append(s)
    return missing


def validate_calculation_closure(
    final_json: Dict[str, Any],
    *,
    question_type: str = "",
    execution_result: Any = None,
    code_status: str = "",
    expected_calc_target: str = "",
    expected_unit_hint: str = "",
) -> Optional[Dict[str, Any]]:
    if not isinstance(final_json, dict):
        return None

    selected_options = _get_answer_option_payload(final_json)
    if not selected_options:
        return {
            "reason": "计算题缺少与正确答案对应的有效选项",
            "issue_type": "major",
            "fix_strategy": "regenerate",
            "required_fixes": ["calc:closure"],
            "fail_types": ["calculation_closure_fail"],
        }

    selected_option_texts = [text for _, text in selected_options]
    selected_numeric_values = [_coerce_number(text) for text in selected_option_texts]
    selected_numeric_values = [x for x in selected_numeric_values if x is not None]
    stem_text = str(final_json.get("题干", "") or "")
    tolerance = _calc_numeric_tolerance(stem_text, selected_option_texts)
    explanation_result = _extract_primary_calc_result_from_explanation(str(final_json.get("解析", "") or ""))
    execution_numeric = _coerce_number(execution_result)
    issue_messages: List[str] = []
    required_fixes: List[str] = []
    fail_types: List[str] = []
    issue_type = "minor"
    fix_strategy = "fix_both"
    qtype = str(question_type or "").strip()
    answer_labels = _parse_answer_labels(final_json.get("正确答案", ""))

    if qtype == "多选题" and len(answer_labels) < 2:
        issue_messages.append("计算题当前题型为多选题，但正确答案少于2个选项")
        required_fixes.append("type:multiselect_answer_contract")
        fail_types.append("calculation_multiselect_answer_contract_fail")
        issue_type = "major"
        fix_strategy = "regenerate"

    stem_text_join = "\n".join(
        [
            stem_text,
            *(str(final_json.get(f"选项{i}", "") or "") for i in range(1, 9)),
            str(final_json.get("解析", "") or ""),
        ]
    )
    has_city6_pricing = bool(re.search(r"(1560|4000)\s*元", stem_text_join))
    asks_amount = bool(re.search(r"(超标款|补交|金额|总额|税额|税费|费用)", stem_text))
    if has_city6_pricing and asks_amount and ("城六区" not in stem_text):
        issue_messages.append("题干缺少区域口径（是否城六区），无法唯一确定分段单价")
        required_fixes.append("calc:missing_region_condition")
        fail_types.append("calculation_region_condition_missing")
        issue_type = "major"
        fix_strategy = "regenerate"
    uses_float_rule = bool(re.search(r"(上浮一个职级|浮动范围|1560|4000)", stem_text_join))
    if uses_float_rule and ("不能分割退回" not in stem_text):
        issue_messages.append("题干缺少“不能分割退回”前置条件，无法锁定上浮规则")
        required_fixes.append("calc:missing_non_split_condition")
        fail_types.append("calculation_non_split_condition_missing")
        issue_type = "major"
        fix_strategy = "regenerate"
    level_lock_pattern = r"(上浮后.*(标准|面积)|上浮一个职级.*(至|到).*(标准|面积|㎡)|上浮后的面积标准|标准面积变为)"
    if uses_float_rule and not re.search(level_lock_pattern, stem_text):
        issue_messages.append("题干未锁定上浮后的级别口径，可能导致上浮规则歧义")
        required_fixes.append("calc:missing_level_lock")
        fail_types.append("calculation_level_lock_missing")
        issue_type = "major"
        fix_strategy = "regenerate"

    needs_executable_check = bool(
        re.search(r"(金额|额度|税额|税费|利润|指数|总收入|面积|年限|补交|超标款|计算)", stem_text)
        and re.search(r"(\d|%|％|×|/|=|元|万元|㎡|年)", stem_text_join)
    )
    if needs_executable_check and (
        str(code_status or "").strip() not in {"success", "success_no_result"} or execution_result in (None, "")
    ):
        issue_messages.append("计算题未产出可验证的代码执行结果")
        required_fixes.append("calc:missing_execution")
        fail_types.append("calculation_execution_missing")
        issue_type = "major"
        fix_strategy = "regenerate"

    if expected_calc_target:
        current_target = _extract_calc_target_signature(stem_text)
        if current_target and current_target != expected_calc_target:
            issue_messages.append(
                f"计算题设问目标发生漂移（期望: {expected_calc_target}，当前: {current_target}）"
            )
            required_fixes.append("calc:target_lock")
            fail_types.append("calculation_target_mismatch")
            issue_type = "major"
            fix_strategy = "regenerate"

    if expected_unit_hint:
        current_unit = _infer_calc_unit_hint(
            stem_text,
            [str(final_json.get(f"选项{i}", "") or "") for i in range(1, 9)],
            str(final_json.get("解析", "") or ""),
        )
        if current_unit and current_unit != expected_unit_hint:
            issue_messages.append(
                f"计算题单位口径发生漂移（期望: {expected_unit_hint}，当前: {current_unit}）"
            )
            required_fixes.append("calc:unit_lock")
            fail_types.append("calculation_unit_mismatch")
            issue_type = "major"
            fix_strategy = "regenerate"

    if explanation_result is not None and selected_numeric_values:
        aligned_expl = _align_numeric_scales(explanation_result, selected_numeric_values)
        if not any(
            _numbers_close(expl_val, val, tolerance=tolerance)
            for expl_val in aligned_expl
            for val in selected_numeric_values
        ):
            issue_messages.append(
                f"解析中的计算结果为 {explanation_result:g}，但正确选项数值不匹配"
            )
            required_fixes.append("calc:closure")
            fail_types.append("calculation_explanation_mismatch")

    if execution_numeric is not None and selected_numeric_values:
        aligned_exec = _align_numeric_scales(execution_numeric, selected_numeric_values)
        if not any(
            _numbers_close(exec_val, val, tolerance=tolerance)
            for exec_val in aligned_exec
            for val in selected_numeric_values
        ):
            issue_messages.append(
                f"代码执行结果为 {execution_numeric:g}，但正确选项数值不匹配"
            )
            required_fixes.append("calc:closure")
            fail_types.append("calculation_answer_mismatch")
            issue_type = "major"
            fix_strategy = "regenerate"

    if execution_numeric is not None and explanation_result is not None:
        aligned_exec_for_expl = _align_numeric_scales(execution_numeric, [explanation_result])
        if not any(_numbers_close(x, explanation_result, tolerance=tolerance) for x in aligned_exec_for_expl):
            issue_messages.append(
                f"代码执行结果 {execution_numeric:g} 与解析中的计算结果 {explanation_result:g} 不一致"
            )
            required_fixes.append("calc:explanation")
            fail_types.append("calculation_trace_mismatch")
            issue_type = "major"
            fix_strategy = "regenerate"

    if not issue_messages:
        return None

    dedup_required = list(dict.fromkeys(required_fixes))
    dedup_fail_types = list(dict.fromkeys(fail_types))
    return {
        "reason": "；".join(issue_messages),
        "issue_type": issue_type,
        "fix_strategy": fix_strategy,
        "required_fixes": dedup_required,
        "fail_types": dedup_fail_types or ["calculation_closure_fail"],
    }

def _extract_text_from_kb_context(kb_context: str) -> str:
    try:
        data = json.loads(kb_context)
        parts = []
        if isinstance(data, dict):
            parts.append(str(data.get("核心内容", "")))
            parts.append(str(data.get("掌握程度", "")))
            parts.append(json.dumps(data.get("结构化内容", {}), ensure_ascii=False))
        return "\n".join([p for p in parts if p])
    except Exception:
        return kb_context or ""

def material_missing_check(final_json: Dict[str, Any], kb_context: str) -> Tuple[bool, List[str]]:
    if not isinstance(final_json, dict):
        return False, []
    q = str(final_json.get("题干", "") or "")
    # Only apply to "supplement materials" questions
    if not re.search(r"(补充|还需|需要|应当|应需).*(材料|证|证明|证件)", q):
        return False, []
    kb_text = _extract_text_from_kb_context(kb_context)
    material_terms = [
        "身份证", "户口本", "结婚证", "婚姻关系证明", "出生医学证明", "独生子女证",
        "子女关系证明", "不动产权证书", "权属证明", "购房合同", "委托书", "完税证明"
    ]
    required = {m for m in material_terms if m in kb_text}
    if not required:
        return False, []
    provided = set()
    for m in required:
        if re.search(rf"(已提供|已提交|已出示|已准备|已递交|已交).{{0,6}}{re.escape(m)}", q):
            provided.add(m)
    missing = sorted(list(required - provided))
    # If more than one missing item, question is ambiguous for single-answer
    if len(missing) > 1:
        return True, missing
    return False, missing


def _extract_required_material_terms(kb_text: str) -> List[str]:
    text = str(kb_text or "")
    material_terms = [
        "身份证", "户口本", "结婚证", "婚姻关系证明", "出生医学证明", "独生子女证",
        "子女关系证明", "不动产权证书", "权属证明", "购房合同", "委托书", "完税证明",
        "营业执照", "授权委托书", "收入证明", "征信报告", "社保缴纳证明", "纳税证明",
    ]
    found = [term for term in material_terms if term in text]
    return list(dict.fromkeys(found))


def detect_router_high_risk_slice(content: str, path: str = "") -> Dict[str, Any]:
    text = str(content or "")
    full_text = f"{path}\n{text}"
    list_hits = len(re.findall(r"（\d+）|\d+\.", text))
    required_materials = _extract_required_material_terms(full_text)
    has_material_checklist = (
        len(required_materials) >= 2
        and bool(re.search(r"(材料|证件|证明|资料).*(包括|准备|提交|提供|补充)|包括.*(材料|证件|证明|资料)", full_text))
    )
    has_parallel_rules = (
        list_hits >= 2
        and bool(re.search(r"(渠道|条件|情形|规则|标准|方式|路径|材料|证件)", full_text))
    )
    prohibit_single_choice = has_material_checklist or has_parallel_rules
    return {
        "required_materials": required_materials,
        "has_material_checklist": has_material_checklist,
        "has_parallel_rules": has_parallel_rules,
        "prohibit_single_choice": prohibit_single_choice,
    }


def detect_router_formula_ambiguity_risk(content: str, path: str = "") -> Dict[str, Any]:
    text = str(content or "")
    full_text = f"{path}\n{text}"
    has_ranking_formula_ambiguity = bool(
        re.search(r"最中国式排名|排名赋分\s*=\s*（?1-最中国式排名-1", full_text)
    )
    has_loan_formula = bool(
        re.search(
            r"较小值（评估值、网签价）\s*×\s*商业贷款成数\s*-\s*公积金贷款部分额度",
            full_text,
        )
    )
    has_coeff_lookup_dependency = bool(
        re.search(r"市占考核系数S.*报盘激励系数Z.*绿金扣分系数Q", full_text)
        and re.search(r"运营总经理收入构成|季度分润", full_text)
    )
    has_parallel_formula_without_merge = bool(
        re.search(r"(可同时使用|同时使用|叠加使用)", full_text)
        and len(re.findall(r"=\s*[（(]?[^\n。]{3,}", full_text)) >= 2
        and not re.search(r"(合并公式|总公式|同时使用时.*公式|叠加后.*公式)", full_text)
    )
    return {
        "has_ranking_formula_ambiguity": has_ranking_formula_ambiguity,
        "has_loan_formula_parentheses_sensitive": has_loan_formula,
        "has_coeff_lookup_dependency": has_coeff_lookup_dependency,
        "has_parallel_formula_without_merge": has_parallel_formula_without_merge,
    }


def detect_router_rule_precondition_profile(content: str, path: str = "") -> Dict[str, Any]:
    text = str(content or "")
    full_text = f"{path}\n{text}"
    conditional_signal = bool(
        re.search(r"(仅限|方可|适用于|须|需|必须|若|当|在.+情形下|前后\d+年|满\d+年|不满\d+年|可同时使用)", full_text)
    )
    required_slots: List[str] = []
    if re.search(r"(上海|本市|在沪|城六区|郊区|区域|地区)", full_text):
        required_slots.append("适用地域")
    if re.search(r"(家庭|首套|二套|主贷人|买方|卖方|业主|居民|纳税人|主体|对象|资格)", full_text):
        required_slots.append("主体身份")
    if re.search(r"(前后\d+年|满\d+年|不满\d+年|日期|网签|签订|期限|时点|之后|之前)", full_text):
        required_slots.append("时间条件")
    if re.search(r"(仅限|方可|适用于|可同时使用|同时使用|满足.+条件|在.+情形下|若.+则)", full_text):
        required_slots.append("适用边界")
    required_slots = list(dict.fromkeys(required_slots))

    # 更细粒度的“可执行槽位”定义：用于前置校验与 fixer 验收，不再只依赖泛槽位。
    # 每个 spec 都要求题干至少命中一种 stem_patterns，避免“知道缺大类但不知道缺什么细项”。
    required_slot_specs: List[Dict[str, Any]] = []

    def _add_spec(slot: str, label: str, trigger_patterns: List[str], stem_patterns: List[str]) -> None:
        triggered = any(re.search(p, full_text) for p in trigger_patterns)
        if not triggered:
            return
        required_slot_specs.append(
            {
                "slot": slot,
                "label": label,
                "trigger_patterns": trigger_patterns,
                "stem_patterns": stem_patterns,
            }
        )

    _add_spec(
        "适用地域",
        "政策适用地域",
        [r"(上海|本市|在沪|外环|城六区|郊区|地区|区域)"],
        [r"(上海|本市|在沪|外环|城六区|郊区|地区|区域)"],
    )
    _add_spec(
        "主体身份",
        "购房/交易主体身份",
        [r"(家庭|首套|二套|主贷人|买方|卖方|业主|居民|纳税人|主体|对象|资格)"],
        [r"(家庭|首套|二套|主贷人|买方|卖方|业主|居民|纳税人|主体|对象|资格)"],
    )
    _add_spec(
        "时间条件",
        "关键时间口径",
        [r"(网签|签订|日期|时点|前后\d+年|满\d+年|不满\d+年|之后|之前)"],
        [r"(网签|签订|日期|时点|前后\d+年|满\d+年|不满\d+年|之后|之前)"],
    )
    _add_spec(
        "适用边界",
        "规则触发边界",
        [r"(仅限|方可|适用于|可同时使用|同时使用|满足.+条件|在.+情形下|若.+则|除外)"],
        [r"(仅限|方可|适用于|可同时使用|同时使用|满足.+条件|在.+情形下|若.+则|除外)"],
    )
    # 常见歧义场景：时间先后关系（动迁协议 vs 网签）必须明示先后
    _add_spec(
        "时间条件",
        "关键时间先后关系",
        [r"(动迁协议|协议签订).*(网签)|(网签).*(动迁协议|协议签订)"],
        [r"(之后|晚于|先于|不早于|不晚于|前后)"],
    )

    # 收敛槽位：最多保留2个“最影响判题唯一性”的槽位，避免题干被补槽位拉成信息过载。
    slot_priority: Dict[str, int] = {
        "主体身份": 4,
        "适用地域": 3,
        "时间条件": 2,
        "适用边界": 1,
    }
    required_slots = sorted(required_slots, key=lambda x: slot_priority.get(str(x), 0), reverse=True)[:2]

    # specs 与 required_slots 对齐，最多2个，并优先覆盖不同槽位。
    specs_filtered = [s for s in required_slot_specs if str(s.get("slot", "")).strip() in set(required_slots)]
    specs_filtered = list(dict.fromkeys(
        (json.dumps(s, ensure_ascii=False, sort_keys=True) for s in specs_filtered)
    ))
    specs_filtered = [json.loads(x) for x in specs_filtered]
    picked_specs: List[Dict[str, Any]] = []
    picked_slots: set[str] = set()
    for spec in specs_filtered:
        slot_name = str(spec.get("slot", "")).strip()
        if not slot_name or slot_name in picked_slots:
            continue
        picked_specs.append(spec)
        picked_slots.add(slot_name)
        if len(picked_specs) >= 2:
            break
    if len(picked_specs) < 2:
        for spec in specs_filtered:
            if spec in picked_specs:
                continue
            picked_specs.append(spec)
            if len(picked_specs) >= 2:
                break
    required_slot_specs = picked_specs

    enabled = conditional_signal and len(required_slots) > 0
    return {
        "enabled": enabled,
        "required_slots": required_slots,
        "required_slot_specs": required_slot_specs,
    }


def _detect_missing_rule_preconditions(final_json: Dict[str, Any], profile: Dict[str, Any]) -> List[str]:
    if not isinstance(final_json, dict):
        return []
    if not isinstance(profile, dict) or not profile.get("enabled"):
        return []
    stem_text = str(final_json.get("题干", "") or "")
    option_text = "\n".join(
        str(final_json.get(f"选项{i}", "") or "").strip()
        for i in range(1, 9)
        if str(final_json.get(f"选项{i}", "") or "").strip()
    )
    combined_text = f"{stem_text}\n{option_text}".strip()
    if not combined_text:
        return list(profile.get("required_slots") or [])
    slot_patterns: Dict[str, str] = {
        "适用地域": r"(上海|本市|在沪|城六区|郊区|区域|地区|外环)",
        "主体身份": r"(家庭|首套|二套|主贷人|买方|卖方|业主|居民|纳税人|主体|对象|资格)",
        "时间条件": r"(前后\d+年|满\d+年|不满\d+年|日期|网签|签订|期限|时点|之后|之前|晚于|先于|不早于|不晚于)",
        "适用边界": r"(仅限|方可|适用于|可同时使用|同时使用|满足.+条件|在.+情形下|若.+则|除外)",
    }
    missing: List[str] = []

    # 优先使用细粒度 specs（可执行槽位）；兼容旧格式 required_slots。
    specs = profile.get("required_slot_specs") if isinstance(profile, dict) else None
    if isinstance(specs, list) and specs:
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            label = str(spec.get("label", "") or spec.get("slot", "") or "").strip()
            patterns = [str(p) for p in (spec.get("stem_patterns") or []) if str(p).strip()]
            if not label or not patterns:
                continue
            if not any(re.search(p, combined_text) for p in patterns):
                missing.append(label)
        return list(dict.fromkeys(missing))

    for slot in (profile.get("required_slots") or []):
        pattern = slot_patterns.get(str(slot), "")
        if pattern and not re.search(pattern, combined_text):
            missing.append(str(slot))
    return list(dict.fromkeys(missing))


def build_precondition_template_block(profile: Dict[str, Any]) -> str:
    if not isinstance(profile, dict) or not profile.get("enabled"):
        return ""
    slots = [str(x).strip() for x in (profile.get("required_slots") or []) if str(x).strip()]
    if not slots:
        return ""
    examples: List[str] = []
    if "时间条件" in slots:
        examples.append("已知新购入住房网签日期在动迁协议签订日期之后。")
    if "适用地域" in slots:
        examples.append("本题按上海市相关规则执行。")
    if "主体身份" in slots:
        examples.append("本题按首套/二套（或主贷人身份）口径判定。")
    if "适用边界" in slots:
        examples.append("仅在满足上述条件时适用该规则。")
    if not examples:
        examples = ["请在题干显式写出可判定前提条件，不能只在解析补充。"]
    return (
        "\n## 前提槽位落字模板（必须执行）\n"
        f"- 必需槽位：{', '.join(slots)}\n"
        "- 要求：题干或用于判定的关键选项中必须出现可判定前提句，不得用模糊词替代。\n"
        f"- 可直接参考句：{' '.join(examples)}\n"
    )


def _derive_focus_contract(
    *,
    path: str,
    content: str,
    core_focus: str,
    has_calc_signal: bool,
    has_list: bool,
    llm_focus_rule: str = "",
    llm_focus_variables: Optional[List[str]] = None,
    llm_focus_task: str = "",
) -> Dict[str, Any]:
    text = f"{path}\n{content}"
    focus_rule = str(llm_focus_rule or "").strip()
    if not focus_rule:
        if core_focus and len(core_focus) >= 4 and core_focus not in path:
            focus_rule = core_focus
        else:
            first_line = re.split(r"[\n。；;]", str(content or "").strip())[0].strip()
            focus_rule = first_line[:80] if first_line else (core_focus or path.split(" > ")[-1])

    focus_variables: List[str] = []
    for x in (llm_focus_variables or []):
        val = str(x).strip()
        if val:
            focus_variables.append(val)

    auto_vars: List[str] = []
    if re.search(r"(上海|本市|在沪|外环|城六区|郊区)", text):
        auto_vars.append("适用地域")
    if re.search(r"(户籍|家庭|单身|居民|主贷人|买方|卖方|业主|纳税人)", text):
        auto_vars.append("主体身份")
    if re.search(r"(满\\d+年|不满\\d+年|前后\\d+年|日期|网签|时点|期限)", text):
        auto_vars.append("时间条件")
    if re.search(r"(首套|二套|套数|资格|限购|条件|可购买|可再购买)", text):
        auto_vars.append("判定条件")
    if has_calc_signal:
        auto_vars.append("计算口径")
    for v in auto_vars:
        if v not in focus_variables:
            focus_variables.append(v)
    focus_variables = focus_variables[:5]

    focus_task = str(llm_focus_task or "").strip()
    if not focus_task:
        if has_calc_signal:
            focus_task = "数值计算"
        elif re.search(r"(限购|资格|条件|适用|是否|可否|可再购买|最多)", text):
            focus_task = "规则判定"
        elif has_list or re.search(r"(流程|步骤|顺序)", text):
            focus_task = "流程判定"
        else:
            focus_task = "规则理解"

    # Guardrail: avoid collapsing to pure "发布日期/年份" memory when slice contains actionable policy rules.
    is_date_memory_focus = bool(
        re.search(
            r"(发布时间|首个|首次).{0,12}(发布|时间)|发布.{0,12}(首个|首次)|\\d{4}年\\d{1,2}月\\d{1,2}日",
            focus_rule,
        )
    )
    has_actionable_policy = bool(
        re.search(r"(限购政策如下|户籍|外环|社保|套数|购房资格|可购|可再购买|最多可购买|适用条件)", text)
    )
    if is_date_memory_focus and has_actionable_policy:
        focus_rule = "政策适用条件与结果判定规则"
        focus_task = "规则判定"
        focus_variables = ["主体身份", "适用地域", "判定条件"]

    return {
        "focus_rule": focus_rule,
        "focus_variables": focus_variables,
        "focus_task": focus_task,
    }


def validate_material_coverage_rule(
    final_json: Dict[str, Any],
    *,
    kb_context: str,
    question_type: str,
) -> Optional[Dict[str, Any]]:
    if not isinstance(final_json, dict):
        return None
    kb_text = _extract_text_from_kb_context(kb_context)
    required_materials = _extract_required_material_terms(kb_text)
    if len(required_materials) < 2:
        return None
    stem = str(final_json.get("题干", "") or "")
    asks_materials = bool(re.search(r"(材料|证件|证明|资料).*(包括|哪些|哪几项|准备|提交|提供|补充)", stem))
    if not asks_materials:
        return None

    if question_type != "多选题":
        return {
            "reason": f"当前切片包含多个必备材料（{', '.join(required_materials)}），这类题只能出多选题",
            "issue_type": "major",
            "fix_strategy": "regenerate",
            "required_fixes": ["logic:material_multiselect"],
            "fail_types": ["material_requires_multiselect"],
        }

    selected_text = stem
    for _, opt_text in _get_answer_option_payload(final_json):
        selected_text += "\n" + str(opt_text or "")
    missing = [term for term in required_materials if term not in selected_text]
    if missing:
        return {
            "reason": f"材料清单题未覆盖切片中的全部必备材料，缺失：{', '.join(missing)}",
            "issue_type": "major",
            "fix_strategy": "fix_both",
            "required_fixes": ["logic:material_coverage"],
            "fail_types": ["material_coverage_incomplete"],
        }
    return None


def detect_focus_overload_issue(
    final_json: Dict[str, Any],
    *,
    focus_contract: Optional[Dict[str, Any]] = None,
    rule_precondition_profile: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not isinstance(final_json, dict):
        return None
    stem = str(final_json.get("题干", "") or "").strip()
    if not stem:
        return None

    buckets: List[Tuple[str, str]] = [
        ("主体资格", r"(首套|二套|户籍|家庭|名下无房|主贷人|纳税人|直系亲属|资格)"),
        ("时间条件", r"(网签|签订|日期|时点|前后\d+年|满\d+年|不满\d+年|之后|之前|生效)"),
        ("地域口径", r"(上海|本市|在沪|城六区|郊区|区域|地区|外环)"),
        ("数值计算", r"(计算|应缴|税率|金额|万元|公式|核定价|增值税|总额|结果)"),
        ("流程要求", r"(流程|审批|报备|抄送|步骤|发起|提交|审核)"),
        ("例外边界", r"(全免|例外|除外|仅限|方可|不可|满足.+条件|若.+则)"),
    ]
    hit_points = [name for name, pat in buckets if re.search(pat, stem)]

    # 白名单排除：由“前提槽位补齐”带来的命中不计入“测点过载”。
    whitelist_points: List[str] = []
    if isinstance(rule_precondition_profile, dict) and rule_precondition_profile.get("enabled"):
        for slot in (rule_precondition_profile.get("required_slots") or []):
            slot_text = str(slot).strip()
            if slot_text == "时间条件" and "时间条件" not in whitelist_points:
                whitelist_points.append("时间条件")
            elif slot_text == "适用地域" and "地域口径" not in whitelist_points:
                whitelist_points.append("地域口径")
            elif slot_text == "主体身份" and "主体资格" not in whitelist_points:
                whitelist_points.append("主体资格")
            elif slot_text == "适用边界" and "例外边界" not in whitelist_points:
                whitelist_points.append("例外边界")

    effective_points = [p for p in hit_points if p not in whitelist_points]
    if len(effective_points) <= 2:
        return None

    focus_rule = ""
    if isinstance(focus_contract, dict):
        focus_rule = str(focus_contract.get("focus_rule", "") or "").strip()

    return {
        "reason": (
            f"题干测点过载（总命中：{', '.join(hit_points)}；有效计数：{', '.join(effective_points)}），单题应聚焦1-2个主测点，"
            "避免把资格/口径/计算/边界等同时堆入一道题"
        ),
        "issue_type": "major",
        "fix_strategy": "fix_question",
        "required_fixes": ["quality:focus_slimming"],
        "fail_types": ["quality_fail", "focus_overload"],
        "focus_rule": focus_rule,
        "hit_points": hit_points,
        "whitelist_points": whitelist_points,
        "effective_points": effective_points,
    }


def _enforce_calc_answer_alignment_on_final_json(
    final_json: Dict[str, Any],
    *,
    execution_result: Any,
    code_status: str,
) -> Tuple[Dict[str, Any], bool, str]:
    if not isinstance(final_json, dict):
        return final_json, False, ""
    if str(code_status or "").strip() not in {"success", "success_no_result"} or execution_result in (None, ""):
        return final_json, False, ""

    answer_text = str(final_json.get("正确答案", "") or "").strip().upper()
    if not re.fullmatch(r"[A-H]", answer_text):
        return final_json, False, ""

    stem_text = str(final_json.get("题干", "") or "")
    option_pairs: List[Tuple[str, str]] = []
    for idx in range(1, 9):
        opt = str(final_json.get(f"选项{idx}", "") or "").strip()
        if not opt:
            continue
        option_pairs.append((chr(64 + idx), opt))
    if len(option_pairs) < 2:
        return final_json, False, ""

    execution_numeric = _coerce_number(execution_result)
    if execution_numeric is None:
        return final_json, False, ""

    numeric_opts: List[Tuple[str, str, float]] = []
    for label, text in option_pairs:
        num = _coerce_number(text)
        if num is None:
            continue
        numeric_opts.append((label, text, num))
    if len(numeric_opts) < 2:
        return final_json, False, ""

    tolerance = _calc_numeric_tolerance(stem_text, [x[1] for x in numeric_opts])
    aligned_exec_values = _align_numeric_scales(execution_numeric, [x[2] for x in numeric_opts])
    if not aligned_exec_values:
        aligned_exec_values = [execution_numeric]

    chosen_label = ""
    for candidate in aligned_exec_values:
        for label, _, opt_num in numeric_opts:
            if _numbers_close(candidate, opt_num, tolerance=tolerance):
                chosen_label = label
                break
        if chosen_label:
            break

    if not chosen_label:
        return final_json, False, ""
    if chosen_label == answer_text:
        return final_json, False, ""

    updated = dict(final_json)
    updated["正确答案"] = chosen_label
    return updated, True, f"calc_answer_aligned:{answer_text}->{chosen_label}"


_CULTURE_CONCEPT_GUARDS: List[Dict[str, Any]] = [
    {
        "label": "第一性原理",
        "path_tokens": ["公司文化理念", "第一性原理"],
        "keywords": ["第一性原理", "坚持做难而正确的事"],
    },
    {
        "label": "使命",
        "path_tokens": ["公司文化理念", "使命"],
        "keywords": ["链家使命", "使命", "有尊严的服务者，更美好的居住"],
    },
    {
        "label": "客户至上",
        "path_tokens": ["公司文化理念", "核心价值观"],
        "keywords": ["客户至上", "值得信赖和依靠", "信赖和依靠"],
    },
    {
        "label": "社区友好",
        "path_tokens": ["公司文化理念", "社区友好"],
        "keywords": ["社区友好"],
    },
]


def _extract_kb_context_metadata(kb_context: Optional[str]) -> Dict[str, Any]:
    if not kb_context:
        return {}
    try:
        data = json.loads(kb_context)
        if isinstance(data, dict):
            metadata = data.get("metadata")
            if isinstance(metadata, dict):
                return metadata
    except Exception:
        return {}
    return {}


def _find_current_culture_guard(current_path: str) -> Optional[Dict[str, Any]]:
    path = str(current_path or "")
    if "公司文化理念" not in path:
        return None
    for guard in _CULTURE_CONCEPT_GUARDS:
        tokens = guard.get("path_tokens") or []
        if all(token in path for token in tokens):
            return guard
    return None


def validate_light_unique_answer_risk(
    question_ir: "QuestionIR",
    *,
    target_type: str,
    kb_context: Optional[str] = None,
) -> List["ValidationIssue"]:
    if target_type != "单选题":
        return []

    metadata = _extract_kb_context_metadata(kb_context)
    current_path = str(metadata.get("当前路径", "") or "")
    current_guard = _find_current_culture_guard(current_path)
    if not current_guard:
        return []

    question = str(question_ir.get("question", "") or "")
    options = [str(x or "") for x in (question_ir.get("options") or [])]
    explanation = str(question_ir.get("explanation", "") or "")
    option_text = "\n".join([x for x in options if x])
    full_text = "\n".join([question, option_text, explanation])

    matched_labels: List[str] = []
    option_labels: List[str] = []
    explanation_labels: List[str] = []
    for guard in _CULTURE_CONCEPT_GUARDS:
        keywords = [str(x) for x in (guard.get("keywords") or []) if str(x).strip()]
        if any(keyword in full_text for keyword in keywords):
            matched_labels.append(str(guard.get("label", "")))
        if any(keyword in option_text for keyword in keywords):
            option_labels.append(str(guard.get("label", "")))
        if any(keyword in explanation for keyword in keywords):
            explanation_labels.append(str(guard.get("label", "")))

    matched_labels = list(dict.fromkeys([x for x in matched_labels if x]))
    option_labels = list(dict.fromkeys([x for x in option_labels if x]))
    explanation_labels = list(dict.fromkeys([x for x in explanation_labels if x]))

    current_label = str(current_guard.get("label", "") or "")
    conflicting_labels = [x for x in matched_labels if x and x != current_label]
    issues: List[ValidationIssue] = []

    if len(option_labels) >= 2 and conflicting_labels:
        issues.append(
            {
                "issue_code": "UNIQUE_CULTURE_AMBIGUOUS",
                "severity": "error",
                "field": "options",
                "message": f"公司文化理念类题目混入多个易混概念（当前切片={current_label}，出现={', '.join(option_labels)}），高概率导致唯一答案不成立",
                "fix_hint": "改为只围绕当前切片概念出题，不要把使命/核心价值观/第一性原理/社区友好互相作为干扰项",
            }
        )

    if conflicting_labels and any(label in explanation_labels for label in conflicting_labels):
        issues.append(
            {
                "issue_code": "UNIQUE_CULTURE_CROSS_SLICE",
                "severity": "error",
                "field": "explanation",
                "message": f"解析引用了非当前切片的文化理念（当前切片={current_label}，解析命中={', '.join(explanation_labels)}），容易在送审时被判为跨切片才能定答案",
                "fix_hint": "解析只引用当前切片概念，不要用相邻文化理念给当前题兜底",
            }
        )

    return issues


def validate_numeric_distractor_path_quality(
    question_ir: "QuestionIR",
    *,
    target_type: str,
    is_calculation: Optional[bool] = None,
) -> List["ValidationIssue"]:
    if target_type not in {"单选题", "多选题"}:
        return []
    if not is_calculation:
        return []

    options = [str(x or "").strip() for x in (question_ir.get("options") or [])]
    if len(options) < 4:
        return []
    numeric_labels: List[str] = []
    for idx, opt in enumerate(options, 1):
        if _coerce_number(opt) is not None:
            numeric_labels.append(chr(64 + idx))
    if len(numeric_labels) < 3:
        return []

    answer_labels = set(_parse_answer_labels(question_ir.get("answer", "")))
    if not answer_labels:
        return []
    wrong_numeric_labels = [lb for lb in numeric_labels if lb not in answer_labels]
    if not wrong_numeric_labels:
        return []

    explanation = str(question_ir.get("explanation", "") or "")
    cause_keywords = [
        "误把", "遗漏", "未扣除", "错用", "误用", "口径", "税率", "分母", "分子",
        "单位", "换算", "多减", "少减", "未除以", "未乘以", "计算路径",
    ]

    issues: List[ValidationIssue] = []
    for label in wrong_numeric_labels:
        mention_re = re.compile(rf"(选项{label}|{label}项|{label}选项|{label}[、，：:])")
        if not mention_re.search(explanation):
            issues.append(
                {
                    "issue_code": "NUM_DISTRACTOR_NO_PATH",
                    "severity": "error",
                    "field": "explanation",
                    "message": f"数值题干扰项 {label} 未给出误判路径说明",
                    "fix_hint": f"在试题分析中补充对{label}项的错误来源说明（如口径/公式/税率/条件误用）",
                }
            )
            continue
        hit = False
        for seg in re.split(r"[\n。；;]", explanation):
            if mention_re.search(seg) and any(k in seg for k in cause_keywords):
                hit = True
                break
        if not hit:
            issues.append(
                {
                    "issue_code": "NUM_DISTRACTOR_NO_CAUSE",
                    "severity": "error",
                    "field": "explanation",
                    "message": f"数值题干扰项 {label} 缺少明确误判原因",
                    "fix_hint": f"对{label}项补充明确错误原因（例如漏条件、错口径、错税率、错公式项）",
                }
            )
    return issues


def validate_focus_alignment(
    question_ir: "QuestionIR",
    *,
    focus_contract: Optional[Dict[str, Any]] = None,
) -> List["ValidationIssue"]:
    if not isinstance(focus_contract, dict):
        return []
    focus_rule = str(focus_contract.get("focus_rule", "") or "").strip()
    focus_variables = [str(x).strip() for x in (focus_contract.get("focus_variables") or []) if str(x).strip()]
    focus_task = str(focus_contract.get("focus_task", "") or "").strip()
    if not focus_rule:
        return []

    q = str(question_ir.get("question", "") or "")
    opts = "\n".join([str(x or "") for x in (question_ir.get("options") or []) if str(x).strip()])
    exp = str(question_ir.get("explanation", "") or "")
    text = f"{q}\n{opts}\n{exp}"

    raw_tokens = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", focus_rule)
    stop_tokens = {"根据", "关于", "相关", "规定", "政策", "规则", "条件", "要求", "内容", "事项"}
    focus_tokens = [t for t in raw_tokens if t not in stop_tokens][:6]
    token_hits = [t for t in focus_tokens if t in text]
    if focus_tokens and not token_hits:
        return [
            {
                "issue_code": "FOCUS_RULE_MISALIGN",
                "severity": "error",
                "field": "question",
                "message": f"题干/选项/解析未命中路由主规则：{focus_rule}",
                "fix_hint": "重写题干与正确答案，使其直接围绕主规则命题，避免退化为片段事实记忆题",
            }
        ]

    missing_vars: List[str] = []
    for v in focus_variables:
        if v not in text:
            missing_vars.append(v)
    if focus_variables:
        required_hits = 2 if len(focus_variables) >= 2 else 1
        if (len(focus_variables) - len(missing_vars)) < required_hits:
            return [
                {
                    "issue_code": "FOCUS_VAR_MISALIGN",
                    "severity": "error",
                    "field": "question",
                    "message": f"主考点关键变量覆盖不足（期望至少{required_hits}项）：{', '.join(focus_variables)}",
                    "fix_hint": "在题干中显式补充主考点变量（如主体身份/区域/时间/条件），不要只考片段标题",
                }
            ]

    if focus_task:
        task_patterns = {
            "规则判定": r"(符合|不符合|可否|是否|最多|能否|应当|不得|正确的是|正确的有)",
            "数值计算": r"(计算|金额|税额|税费|额度|为（\u3000）|保留|小数)",
            "流程判定": r"(流程|步骤|顺序|先后|环节)",
            "规则理解": r"(根据|关于|下列|说法|表述)",
        }
        pattern = task_patterns.get(focus_task, "")
        if pattern and not re.search(pattern, q):
            return [
                {
                    "issue_code": "FOCUS_TASK_MISALIGN",
                    "severity": "error",
                    "field": "question",
                    "message": f"设问任务与路由主任务不一致（期望：{focus_task}）",
                    "fix_hint": f"将设问改为“{focus_task}”导向，禁止退化为纯记忆年份/名称类问题",
                }
            ]
    if focus_task in {"规则判定", "数值计算", "流程判定"} and re.search(r"(发布时间|首次发布|首个.*发布|哪一年发布|何时发布)", q):
        return [
            {
                "issue_code": "FOCUS_DEGENERATE_MEMORY",
                "severity": "error",
                "field": "question",
                "message": "题目退化为纯历史发布时间记忆，未体现主任务要求",
                "fix_hint": "改为围绕规则适用/条件判定/计算闭环的设问，避免只问发布年份",
            }
        ]
    return []

def _has_year(text: str) -> bool:
    return bool(re.search(r'(19|20)\d{2}年', text or ""))

def _collect_text_fields(final_json: Dict[str, Any]) -> List[str]:
    fields = []
    if not isinstance(final_json, dict):
        return fields
    fields.append(str(final_json.get("题干", "")))
    fields.append(str(final_json.get("解析", "")))
    for i in range(1, 9):
        key = f"选项{i}"
        if key in final_json:
            fields.append(str(final_json.get(key, "")))
    return fields

def repair_final_json_format(final_json: Dict[str, Any], question_type: str) -> Dict[str, Any]:
    if not isinstance(final_json, dict):
        return final_json
    repaired = dict(final_json)
    # Question stem
    q = repaired.get("题干", "")
    q = normalize_blank_brackets(str(q))
    repaired["题干"] = enforce_question_bracket_and_punct(q, question_type)
    # Options
    if question_type == "判断题":
        repaired["选项1"] = "正确"
        repaired["选项2"] = "错误"
        repaired["选项3"] = ""
        repaired["选项4"] = ""
    else:
        for i in range(1, 5):
            key = f"选项{i}"
            val = str(repaired.get(key, "") or "")
            # Strip leading A-H with punctuation (A. A、 A: etc.)
            val = re.sub(r'^[A-HＡ-Ｈa-h][\.\、:：\s\)）]+', '', val, flags=re.IGNORECASE)
            # Strip leading single A-H when followed by CJK (avoids "A网签" -> display "A. A网签...")
            val = re.sub(r'^[A-HＡ-Ｈa-h](?=[\u4e00-\u9fff])', '', val, flags=re.IGNORECASE)
            val = normalize_blank_brackets(val.strip())
            val = re.sub(r"[。！？；;：:，,、]+$", "", val)
            repaired[key] = val
        # Fill missing options for choice questions
        for i in range(1, 5):
            key = f"选项{i}"
            if repaired.get(key, "") == "":
                repaired[key] = "待补充选项"
    # Answer format
    ans = repaired.get("正确答案", "")
    if question_type == "判断题":
        a = str(ans).strip()
        if a in ["正确", "A", "a"]:
            repaired["正确答案"] = "A"
        elif a in ["错误", "B", "b"]:
            repaired["正确答案"] = "B"
        elif not re.fullmatch(r"[ABab]", a):
            repaired["正确答案"] = "A"
    elif question_type == "单选题":
        a = str(ans).strip().upper()
        if not re.fullmatch(r"[A-H]", a):
            repaired["正确答案"] = "A"
        else:
            repaired["正确答案"] = a
    elif question_type == "多选题":
        a = re.sub(r"[^A-Ha-h]", "", str(ans).strip()).upper()
        if len(a) < 2:
            repaired["正确答案"] = "AC"
        else:
            repaired["正确答案"] = a
    # Code-side fixes: single quote -> double quote; numeric options ascending
    repaired = replace_single_quotes_in_final_json(repaired)
    repaired = apply_numeric_options_ascending(repaired)
    # Enforce 1、2、3、 三段式 (顿号) for 解析
    repaired["解析"] = normalize_explanation_three_stage(str(repaired.get("解析", "") or ""))
    return repaired

def prepare_draft_for_writer(draft: Dict[str, Any], target_type: str) -> Dict[str, Any]:
    if not isinstance(draft, dict):
        return draft
    cleaned = dict(draft)
    cleaned_q = enforce_question_bracket_and_punct(str(cleaned.get("question", "")), target_type)
    cleaned["question"] = cleaned_q
    options = cleaned.get("options", [])
    if target_type == "判断题":
        cleaned["options"] = ["正确", "错误"]
    else:
        fixed_opts = []
        for opt in options if isinstance(options, list) else []:
            val = str(opt)
            val = re.sub(r'^[A-HＡ-Ｈa-h][\.\、:：\s\)）]+', '', val, flags=re.IGNORECASE)
            val = normalize_blank_brackets(val.strip())
            # Strip trailing punctuation (full-width and ASCII)
            val = re.sub(r"[。！？；;：:，,、]+$", "", val)
            fixed_opts.append(val)
        cleaned["options"] = fixed_opts
    # Normalize option count for choice questions
    if target_type in ["单选题", "多选题"]:
        fixed_opts = cleaned.get("options", [])
        if isinstance(fixed_opts, list):
            if len(fixed_opts) > 4:
                cleaned["options"] = fixed_opts[:4]
            elif len(fixed_opts) < 4:
                cleaned["options"] = fixed_opts + ["待补充选项"] * (4 - len(fixed_opts))
    # Normalize answer format by type
    ans = cleaned.get("answer", "")
    if target_type == "判断题":
        if isinstance(ans, str):
            a = ans.strip()
            if a in ["正确", "A", "a"]:
                cleaned["answer"] = "A"
            elif a in ["错误", "B", "b"]:
                cleaned["answer"] = "B"
        elif isinstance(ans, list) and ans:
            cleaned["answer"] = str(ans[0]).strip().upper()
    elif target_type == "单选题":
        if isinstance(ans, list) and ans:
            cleaned["answer"] = str(ans[0]).strip().upper()
        elif isinstance(ans, str):
            cleaned["answer"] = ans.strip().upper()[:1]
    elif target_type == "多选题":
        if isinstance(ans, list):
            cleaned["answer"] = [str(x).strip().upper() for x in ans if str(x).strip()]
        elif isinstance(ans, str):
            cleaned["answer"] = re.sub(r"[^A-Ha-h]", "", ans.strip()).upper()
    return cleaned


def _infer_draft_type_for_writer(draft: Dict[str, Any]) -> str:
    options = draft.get("options", []) if isinstance(draft, dict) else []
    answer = draft.get("answer", "") if isinstance(draft, dict) else ""
    if isinstance(options, list) and len(options) == 2:
        opt_set = {str(options[0]).strip(), str(options[1]).strip()}
        if opt_set == {"正确", "错误"}:
            return "判断题"
    if isinstance(answer, list):
        return "多选题"
    if isinstance(answer, str):
        ans = answer.strip().upper()
        if len(ans) > 1 and all(c in "ABCDE" for c in ans):
            return "多选题"
    return "单选题"


def _infer_final_json_question_type(final_json: Optional[Dict[str, Any]]) -> str:
    data = final_json if isinstance(final_json, dict) else {}
    opt1 = str(data.get("选项1", "") or "").strip()
    opt2 = str(data.get("选项2", "") or "").strip()
    opt3 = str(data.get("选项3", "") or "").strip()
    opt4 = str(data.get("选项4", "") or "").strip()
    ans = str(data.get("正确答案", "") or "").strip().upper()
    if {opt1, opt2} == {"正确", "错误"} and not opt3 and not opt4:
        return "判断题"
    letters = re.sub(r"[^A-H]", "", ans)
    if len(letters) > 1:
        return "多选题"
    return "单选题"


def _infer_multiselect_labels_from_explanation(explanation: str, option_count: int = 4) -> List[str]:
    text = str(explanation or "")
    labels: List[str] = []
    # e.g. "选项A正确" / "A正确"
    for m in re.findall(r"(?:选项)?([A-H])(?:项)?\s*正确", text, flags=re.IGNORECASE):
        lab = str(m).upper()
        if lab not in labels:
            labels.append(lab)
    if len(labels) >= 2:
        return labels
    # e.g. "本题答案为ACD"
    ans_match = re.search(r"本题答案为\s*([A-H]{2,8})", text, flags=re.IGNORECASE)
    if ans_match:
        for ch in str(ans_match.group(1)).upper():
            if ch not in labels:
                labels.append(ch)
    valid = [chr(ord("A") + i) for i in range(max(0, min(option_count, 8)))]
    labels = [x for x in labels if x in valid]
    return labels


def _resolve_writer_target_type(
    draft: Optional[Dict[str, Any]],
    configured_question_type: str,
    router_recommended_type: str,
) -> str:
    draft_type = _infer_draft_type_for_writer(draft) if isinstance(draft, dict) else None
    if configured_question_type == "随机":
        return draft_type if draft_type else router_recommended_type
    if configured_question_type in ["单选题", "多选题", "判断题"]:
        return configured_question_type
    return draft_type if draft_type else router_recommended_type


def _build_validation_issue(message: str) -> "ValidationIssue":
    msg = str(message or "")
    issue_code = "WRITER_RULE"
    field = "global"
    if "题干" in msg:
        field = "question"
    elif "选项" in msg:
        field = "options"
    elif "答案" in msg:
        field = "answer"
    elif "解析" in msg:
        field = "explanation"
    if "括号" in msg:
        issue_code = "FMT_BRACKET"
    elif "答案格式" in msg or "答案" in msg:
        issue_code = "ANS_FORMAT"
    elif "选项末尾" in msg:
        issue_code = "FMT_OPTION_END_PUNCT"
    elif "称谓" in msg or "人名" in msg:
        issue_code = "NAME_STYLE"
    elif "锁词" in msg or "术语" in msg:
        issue_code = "TERM_LOCK"
    elif "图片" in msg:
        issue_code = "HARD_IMAGE"
    elif "表格" in msg:
        issue_code = "HARD_TABLE"
    elif "题干缺少标准占位括号" in msg:
        issue_code = "FMT_STEM_BLANK"
    elif "设问不规范" in msg or "结论锚点" in msg:
        issue_code = "FMT_ASK_TEMPLATE"
    return {
        "issue_code": issue_code,
        "severity": "error",
        "field": field,
        "message": msg,
        "fix_hint": f"请修复问题：{msg}",
    }


def _writer_normalize_phase(draft: Dict[str, Any], target_type: str) -> "QuestionIR":
    normalized = prepare_draft_for_writer(draft, target_type)
    # Deterministic name cleanup before media/format normalization.
    force_named = _is_judgement_style_stem(str(normalized.get("question", "") or ""))
    normalized["question"] = _repair_name_style(str(normalized.get("question", "") or ""), force_named=force_named)
    normalized["options"] = [
        _repair_name_style(str(opt or ""), force_named=force_named)
        for opt in (normalized.get("options", []) or [])
    ]
    normalized["explanation"] = _repair_name_style(str(normalized.get("explanation", "") or ""), force_named=force_named)
    q_text, opt_list, exp_text, _changed = sanitize_media_payload(
        normalized.get("question", ""),
        normalized.get("options", []),
        normalized.get("explanation", ""),
    )
    # Enforce 1、2、3、 三段式 (顿号) for 定稿解析
    exp_text = normalize_explanation_three_stage(str(exp_text or ""))
    return {
        "question": str(q_text or ""),
        "options": list(opt_list or []),
        "answer": normalized.get("answer", ""),
        "explanation": exp_text,
    }


def _writer_validate_phase(
    question_ir: "QuestionIR",
    target_type: str,
    term_locks: Optional[List[str]] = None,
    kb_context: Optional[str] = None,
    focus_contract: Optional[Dict[str, Any]] = None,
    is_calculation: Optional[bool] = None,
    expected_calc_target: Optional[str] = None,
    expected_calc_unit: Optional[str] = None,
) -> "ValidationReport":
    q = str(question_ir.get("question", "") or "")
    opts = list(question_ir.get("options", []) or [])
    ans = question_ir.get("answer")
    exp = str(question_ir.get("explanation", "") or "")
    term_locks = term_locks or []

    issues: List[str] = []
    if is_calculation and expected_calc_target:
        current_target = _extract_calc_target_signature(q)
        if current_target and current_target != str(expected_calc_target):
            issues.append(
                f"计算题设问目标发生漂移（期望: {expected_calc_target}，当前: {current_target}）"
            )
    if is_calculation and expected_calc_unit:
        current_unit = _infer_calc_unit_hint(q, opts, exp)
        if current_unit and current_unit != str(expected_calc_unit):
            issues.append(
                f"计算题单位口径发生漂移（期望: {expected_calc_unit}，当前: {current_unit}）"
            )
    issues.extend(validate_writer_format(q, opts, ans, target_type))
    issues.extend(validate_name_usage(q, opts, exp))
    payload = {
        "question": q,
        "options": opts,
        "answer": ans,
        "explanation": exp,
    }
    issues.extend(detect_term_lock_violations(term_locks, payload))
    unique_issues = list(dict.fromkeys([str(x) for x in issues if str(x).strip()]))
    structured = [_build_validation_issue(x) for x in unique_issues]
    hard_rule_issues = validate_hard_rules(
        q, opts, exp,
        kb_context=kb_context,
        target_type=target_type,
        answer=ans,
        is_calculation=is_calculation,
    )
    structured.extend([i for i in hard_rule_issues if isinstance(i, dict)])
    unique_answer_risk_issues = validate_light_unique_answer_risk(
        question_ir,
        target_type=target_type,
        kb_context=kb_context,
    )
    structured.extend([i for i in unique_answer_risk_issues if isinstance(i, dict)])
    numeric_distractor_issues = validate_numeric_distractor_path_quality(
        question_ir,
        target_type=target_type,
        is_calculation=is_calculation,
    )
    structured.extend([i for i in numeric_distractor_issues if isinstance(i, dict)])
    focus_alignment_issues = validate_focus_alignment(
        question_ir,
        focus_contract=focus_contract,
    )
    structured.extend([i for i in focus_alignment_issues if isinstance(i, dict)])
    deduped = []
    seen = set()
    for issue in structured:
        key = (
            issue.get("issue_code"),
            issue.get("field"),
            issue.get("message"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    summary = "通过" if not deduped else f"命中{len(deduped)}个问题"
    return {
        "passed": len(deduped) == 0,
        "issues": deduped,
        "summary": summary,
    }


def _final_json_to_question_ir(final_json: Dict[str, Any]) -> "QuestionIR":
    if not isinstance(final_json, dict):
        return {"question": "", "options": [], "answer": "", "explanation": ""}
    options: List[str] = []
    for i in range(1, 9):
        val = str(final_json.get(f"选项{i}", "") or "").strip()
        if val:
            options.append(val)
    return {
        "question": str(final_json.get("题干", "") or ""),
        "options": options,
        "answer": final_json.get("正确答案", ""),
        "explanation": str(final_json.get("解析", "") or ""),
    }


def _sync_downstream_state_from_final_json(
    final_json: Dict[str, Any],
    target_type: str,
    *,
    term_locks: Optional[List[str]] = None,
    kb_context: Optional[str] = None,
    focus_contract: Optional[Dict[str, Any]] = None,
    is_calculation: bool = False,
    expected_calc_target: str = "",
    expected_calc_unit: str = "",
) -> Dict[str, Any]:
    question_ir = _final_json_to_question_ir(final_json)
    report = _writer_validate_phase(
        question_ir,
        target_type,
        term_locks=term_locks or [],
        kb_context=kb_context,
        focus_contract=focus_contract,
        is_calculation=is_calculation,
        expected_calc_target=expected_calc_target,
        expected_calc_unit=expected_calc_unit,
    )
    stem = str(question_ir.get("question", "") or "")
    options = list(question_ir.get("options", []) or [])
    candidate_sentences = []
    if target_type in ["单选题", "多选题", "判断题"]:
        try:
            candidate_sentences = build_candidate_sentences(stem, options)
        except Exception:
            candidate_sentences = []
    return {
        "writer_format_issues": [
            str(i.get("message", "")).strip()
            for i in (report.get("issues") or [])
            if str(i.get("message", "")).strip()
        ],
        "writer_validation_report": report,
        "writer_retry_exhausted": False,
        "candidate_sentences": candidate_sentences,
    }


def _legacy_writer_precheck(
    draft: Dict[str, Any],
    target_type: str,
    term_locks: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    term_locks = term_locks or []
    draft_for_prompt = prepare_draft_for_writer(draft, target_type)
    q_text, opt_list, exp_text, _changed = sanitize_media_payload(
        draft_for_prompt.get("question", ""),
        draft_for_prompt.get("options", []),
        draft_for_prompt.get("explanation", ""),
    )
    draft_for_prompt["question"] = q_text
    draft_for_prompt["options"] = opt_list
    draft_for_prompt["explanation"] = exp_text
    issues = validate_writer_format(
        draft_for_prompt.get("question", ""),
        draft_for_prompt.get("options", []),
        draft_for_prompt.get("answer"),
        target_type,
    )
    issues += validate_name_usage(
        draft_for_prompt.get("question", ""),
        draft_for_prompt.get("options", []),
        draft_for_prompt.get("explanation", ""),
    )
    hard_issues = validate_hard_rules(
        draft_for_prompt.get("question", ""),
        draft_for_prompt.get("options", []),
        draft_for_prompt.get("explanation", ""),
        target_type=target_type,
        answer=draft_for_prompt.get("answer"),
    )
    issues += [str(i.get("message", "")) for i in hard_issues if str(i.get("message", "")).strip()]
    issues += detect_term_lock_violations(term_locks, draft_for_prompt)
    return draft_for_prompt, list(dict.fromkeys([str(x) for x in issues if str(x).strip()]))


def _refactored_writer_precheck(
    draft: Dict[str, Any],
    target_type: str,
    term_locks: Optional[List[str]] = None,
    kb_context: Optional[str] = None,
    focus_contract: Optional[Dict[str, Any]] = None,
) -> Tuple["QuestionIR", "ValidationReport"]:
    question_ir = _writer_normalize_phase(draft, target_type)
    report = _writer_validate_phase(
        question_ir,
        target_type,
        term_locks=term_locks,
        kb_context=kb_context,
        focus_contract=focus_contract,
    )
    return question_ir, report


def _build_writer_polish_prompt_issue_only(
    *,
    target_type: str,
    draft_for_prompt: Dict[str, Any],
    kb_context: str,
    examples_text: str,
    term_lock_text: str,
    router_focus_text: str,
    difficulty_instruction_writer: str,
    self_check_text: str,
    issue_messages: List[str],
) -> str:
    issue_lines = "\n".join([f"- {x}" for x in issue_messages[:20]]) if issue_messages else "- 无（仅做轻量润色）"
    return f"""
# 任务
你是最终编辑，仅针对“问题清单”进行定向修复，不要重新定义规则。

# 目标题型
{target_type}

{difficulty_instruction_writer}
{term_lock_text}
{router_focus_text}

# 必须修复的问题（按优先级）
{issue_lines}

{self_check_text}

# 修复要求
1. 仅修复上述问题，不做无关改写。
2. 不得引入题干外新前提，不得改动考点方向。
3. 若涉及答案修正，解析必须同步修正并保持一致。
4. 输出严格 JSON，不要输出额外说明文字。

初稿（已做代码归一化）: {json.dumps(draft_for_prompt, ensure_ascii=False)}
参考教材: {kb_context}
{examples_text}

# 输出格式 (JSON)
{{
    "question": "题干内容...",
    "options": ["第一项正文（勿写A.或A、等序号）", "第二项正文", "第三项正文", "第四项正文"],
    "answer": "A" 或 ["A", "C"],
    "explanation": "解析须严格按试题解析三段论：1、教材原文：（路由前三个标题即目标题内容+分级+教材原文，≤400字，不要写「目标题：」字样）2、试题分析：（用自己的话解释每个选项，多选须覆盖全部选项，不得粘贴教材原文）3、结论：（判断题写本题答案为正确/错误，选择题写本题答案为A/B/C/D/AB/AC...）。严禁省略号与省略段落。",
    "difficulty": 0.64
}}
"""

def format_kb_chunk_full(kb_chunk: Dict[str, Any]) -> str:
    data = {
        "完整路径": kb_chunk.get("完整路径", ""),
        "掌握程度": kb_chunk.get("掌握程度", ""),
        "核心内容": kb_chunk.get("核心内容", ""),
        "结构化内容": kb_chunk.get("结构化内容", {}),
        "metadata": kb_chunk.get("metadata", {}),
    }
    return json.dumps(data, ensure_ascii=False, indent=2)

def _get_parent_path(path: str) -> str:
    if not path or " > " not in path:
        return ""
    return " > ".join(path.split(" > ")[:-1]).strip()

def build_extended_kb_context(kb_chunk: Dict[str, Any], retriever: Optional[KnowledgeRetriever], examples: List[Dict]) -> Tuple[str, List[Dict], List[Dict]]:
    current_path = kb_chunk.get("完整路径", "")
    parent_slices = []
    related_slices = []
    if retriever:
        parent_slices = retriever.get_parent_slices(kb_chunk)
        # Related slices by current slice content
        current_query = f"{kb_chunk.get('完整路径','')} {kb_chunk.get('核心内容','')}".strip()
        related_slices.extend(
            retriever.get_related_kb_chunks(current_query, k=5, exclude_paths=[current_path])
        )
        # Related slices by examples (题干+解析)
        if examples:
            for ex in examples[:5]:
                if isinstance(ex, dict):
                    q = ex.get("题干", "") or ex.get("question", "")
                    exp = ex.get("解析", "") or ex.get("explanation", "")
                    query_text = f"{q}\n{exp}".strip()
                else:
                    query_text = str(ex)
                related_slices.extend(
                    retriever.get_related_kb_chunks(query_text, k=5, exclude_paths=[current_path])
                )
    # Deduplicate by path
    def _dedup(chunks):
        seen = set()
        out = []
        for c in chunks or []:
            path = c.get("完整路径", "")
            if not path or path in seen:
                continue
            seen.add(path)
            out.append(c)
        return out
    parent_slices = _dedup(parent_slices)
    related_slices = _dedup(related_slices)
    data = {
        "当前切片": json.loads(format_kb_chunk_full(kb_chunk)),
        "上一级切片全集": [json.loads(format_kb_chunk_full(c)) for c in parent_slices],
        "相似切片": [json.loads(format_kb_chunk_full(c)) for c in related_slices],
        "metadata": {
            "当前路径": current_path,
            "上一级路径": _get_parent_path(current_path),
        }
    }
    return json.dumps(data, ensure_ascii=False, indent=2), parent_slices, related_slices


# All selectable question types when user chooses "随机" (must match frontend options)
ALL_QUESTION_TYPES = ["单选题", "多选题", "判断题"]


def resolve_target_question_type(
    configured_question_type: Optional[str],
    recommended_type: str,
    kb_chunk: Dict[str, Any],
    retriever: Optional[KnowledgeRetriever],
) -> Tuple[str, List[str]]:
    """
    决定本次出题的最终题型。
    规则：
    1) 指定题型（单选/多选/判断）直接使用指定题型；
    2) 随机题型：在「筛选中的各种类型」中真正随机选一；若有当前切片关联母题的题型集合，
       则在该集合内随机；否则在全部三种题型（单选/多选/判断）中随机，保证批次中各类都会出现。
    3) 返回 (本题选定题型, 候选题型列表) 便于日志展示“随机出来的是什么”。
    """
    cfg = str(configured_question_type or "").strip()
    rec = recommended_type if recommended_type in {"单选题", "多选题", "判断题"} else "单选题"
    if cfg in {"单选题", "多选题", "判断题"}:
        return cfg, []
    if cfg == "随机":
        preferred_types: List[str] = []
        if retriever and hasattr(retriever, "get_preferred_question_types_by_knowledge_point"):
            try:
                preferred_types = retriever.get_preferred_question_types_by_knowledge_point(kb_chunk) or []
            except Exception:
                preferred_types = []
        # Candidate set: slice-mapped types if any, else all three types so 随机 yields variety
        candidates = preferred_types if preferred_types else ALL_QUESTION_TYPES
        # 优先消费 router 推荐题型，避免“路由已推荐但后续随机改题型”
        chosen = rec if rec in candidates else random.choice(candidates)
        return chosen, candidates
    return rec, []


def normalize_generation_mode(raw_mode: Optional[str]) -> str:
    """
    统一出题筛选条件取值，并兼容历史配置。
    可选值：
    - 基础概念/理解记忆
    - 实战应用/推演
    - 随机
    """
    mode = str(raw_mode or "").strip()
    if mode in {"基础概念/理解记忆", "实战应用/推演", "随机"}:
        return mode
    # 兼容历史模式
    if mode == "灵活":
        return "实战应用/推演"
    if mode == "严谨":
        return "基础概念/理解记忆"
    return "随机"


def resolve_effective_generation_mode(raw_mode: Optional[str], state: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """
    返回 (effective_mode, normalized_mode)：
    - normalized_mode: 规范化后的用户筛选条件
    - effective_mode: 本题实际执行条件（随机模式下在两类中选一）
    """
    normalized = normalize_generation_mode(raw_mode)
    if normalized != "随机":
        return normalized, normalized
    # 随机模式下做轻量轮转，保证两类都能覆盖
    seed = int(time.time() * 1000)
    if isinstance(state, dict):
        seed += int(state.get("retry_count", 0) or 0)
    effective = "基础概念/理解记忆" if seed % 2 == 0 else "实战应用/推演"
    return effective, normalized


def build_mode_instruction(effective_mode: str, normalized_mode: str) -> str:
    """构建出题筛选条件提示词。"""
    if effective_mode == "基础概念/理解记忆":
        random_note = "（来自随机模式自动选择）" if normalized_mode == "随机" else ""
        return f"""
# 出题筛选条件：基础概念/理解记忆{random_note}
要求：
1. **聚焦知识点本体**：重点考察定义、规则、条件、结构与关键边界。
2. **不强制业务场景**：可直接围绕教材切片命题，不要求绑定客户或交易情境。
3. **忠实教材原文**：不得引入材料外结论；题干、选项、解析必须可回溯到切片。
4. **解析要说明依据**：清晰给出教材依据与推理链路。
"""
    random_note = "（来自随机模式自动选择）" if normalized_mode == "随机" else ""
    return f"""
# 出题筛选条件：实战应用/推演{random_note}
要求：
1. **必须关联业务场景**：题干需出现可识别的经纪业务情境（客户咨询、交易流程、签约、合规、税费或贷款决策等）。
2. **强调应用与推演**：通过场景条件推导结论，避免只考纯记忆点。
3. **忠实教材原文**：场景可重构但规则依据必须来自切片，不得超纲。
4. **解析要体现应用链路**：明确“场景条件 -> 规则套用 -> 结论”。
"""


def build_mode_instruction_repair(effective_mode: str, normalized_mode: str) -> str:
    """修复阶段的简版筛选条件提示。"""
    if effective_mode == "基础概念/理解记忆":
        suffix = "（随机模式本题选中）" if normalized_mode == "随机" else ""
        return f"出题筛选条件：基础概念/理解记忆{suffix}。本题不强制业务场景，直接考察切片知识点，禁止偏离教材依据。"
    suffix = "（随机模式本题选中）" if normalized_mode == "随机" else ""
    return f"出题筛选条件：实战应用/推演{suffix}。本题必须关联业务场景，并体现条件推演过程。"


def has_business_context(
    text: str,
    kb_context: str = "",
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    provider: Optional[str] = None,
    trace_id: Optional[str] = None,
    question_id: Optional[str] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """使用大模型语义判定题干是否属于房产经纪业务场景（不做关键词硬编码）。"""
    stem = str(text or "").strip()
    if not stem:
        return False, "题干为空，无法判定业务场景", None

    semantic_prompt = f"""
你是出题审计员。请仅根据语义判断这道题题干是否属于“房产经纪人的真实业务场景”。

判定标准：
1. 若题干描述的是经纪业务中的真实工作情境（含交易流程、客户沟通、合规执行、门店运营管理、团队管理、房贷税费办理、投诉处理、渠道管理等与经纪岗位相关的实践活动），返回 true。
2. 若题干只是抽象概念背诵、定义记忆、口号/价值观复述，且没有可执行业务情境，返回 false。
3. 不要做关键词匹配，请做整体语义理解。

教材上下文（供参考）：
{kb_context[:2500]}

题干：
{stem}

只输出 JSON：
{{
  "is_business_context": true/false,
  "reason": "一句话说明依据"
}}
"""

    content, _, llm_record = call_llm(
        node_name="critic.scene_semantic",
        prompt=semantic_prompt,
        model_name=(model_name or CRITIC_MODEL or MODEL_NAME),
        api_key=(api_key or CRITIC_API_KEY or API_KEY),
        base_url=(base_url or CRITIC_BASE_URL or BASE_URL),
        provider=(provider or CRITIC_PROVIDER),
        trace_id=trace_id,
        question_id=question_id,
        temperature=0.0,
        max_tokens=300,
        timeout=60,
    )
    if not str(content or "").strip():
        return True, "场景语义判定服务空响应，已降级放行", llm_record
    try:
        parsed = parse_json_from_response(content)
        is_business_context = bool(parsed.get("is_business_context", False))
        reason = str(parsed.get("reason", "") or "").strip() or "未提供判定依据"
        return is_business_context, reason, llm_record
    except Exception as e:
        return True, f"场景语义判定解析失败，已降级放行: {e}", llm_record

# --- State Definition ---
# Contract: Any node that modifies question content MUST return it in its state update so the next
# node receives the latest version. Specialist/Calculator return "draft"; Writer/Fixer return
# "final_json". After Fixer or Router(reroute), execution_result/generated_code/tool_usage must be
# cleared so Critic never uses stale calculator state for the new question.
class AgentState(TypedDict, total=False):
    # Core routing / generation
    kb_chunk: Dict[str, Any]
    examples: List[Dict[str, Any]]
    agent_name: str
    retry_count: int
    draft: Optional[Dict[str, Any]]
    final_json: Optional[Dict[str, Any]]
    self_check_issues: Optional[List[str]]
    current_question_type: Optional[str]
    locked_question_type: Optional[str]
    current_generation_mode: Optional[str]
    term_locks: Optional[List[str]]
    locked_focus_contract: Optional[Dict[str, Any]]
    router_details: Optional[Dict[str, Any]]
    router_round: Optional[int]
    is_reroute_round: Optional[bool]

    # Critic / fixer loop
    critic_feedback: Optional[str]
    critic_details: Optional[str]
    critic_result: Optional[Dict[str, Any]]
    critic_tool_usage: Optional[Dict[str, Any]]
    critic_rules_context: Optional[str]
    critic_related_rules: Optional[List[str]]
    critic_basis_source: Optional[str]
    critic_basis_paths: Optional[List[str]]
    critic_non_current_basis: Optional[bool]
    critic_required_fixes: Optional[List[str]]
    fix_required_unmet: Optional[bool]
    was_fixed: Optional[bool]
    fix_summary: Optional[Dict[str, Any]]
    fix_no_change: Optional[bool]
    fix_attempted_regen: Optional[bool]

    # Previous round snapshot (for reroute repair prompts)
    prev_final_json: Optional[Dict[str, Any]]
    prev_critic_feedback: Optional[str]
    prev_critic_details: Optional[str]
    prev_critic_result: Optional[Dict[str, Any]]
    prev_critic_tool_usage: Optional[Dict[str, Any]]
    prev_critic_rules_context: Optional[str]
    prev_critic_related_rules: Optional[List[str]]
    prev_critic_basis_source: Optional[str]
    prev_critic_basis_paths: Optional[List[str]]
    prev_critic_non_current_basis: Optional[bool]
    reroute_basis_context: Optional[str]

    # Calculator/code execution
    generated_code: Optional[str]
    execution_result: Optional[Any]
    code_status: Optional[str]
    tool_usage: Optional[Dict[str, Any]]
    calc_target_signature: Optional[str]
    calc_unit_hint: Optional[str]
    calc_required_slots: Optional[List[str]]
    calc_missing_slots: Optional[List[str]]
    solver_commentary: Optional[str]

    # Writer validation artifacts
    candidate_sentences: Optional[List[Dict[str, Any]]]
    writer_format_issues: Optional[List[str]]
    writer_validation_report: Optional[Dict[str, Any]]
    writer_retry_exhausted: Optional[bool]

    # Runtime/UI/observability
    logs: Annotated[List[str], operator.add]
    llm_trace: Annotated[List[Dict[str, Any]], operator.add]
    llm_summary: Optional[Dict[str, Any]]
    trace_id: Optional[str]
    question_id: Optional[str]
    unstable_flags: Optional[List[str]]
    debug_force_fail_once: Optional[bool]

    # Model bookkeeping
    critic_model_used: Optional[str]
    calculator_model_used: Optional[str]

    # FR1.7: Specialist can refuse to generate; company-culture slices relax practice-oriented checks
    refuse_to_generate: Optional[bool]
    refuse_reason: Optional[str]
    is_company_culture: Optional[bool]


class DraftV1(TypedDict, total=False):
    question: str
    options: List[str]
    answer: Any
    explanation: str


class QuestionIR(TypedDict, total=False):
    question: str
    options: List[str]
    answer: Any
    explanation: str


class ValidationIssue(TypedDict):
    issue_code: str
    severity: str
    field: str
    message: str
    fix_hint: str


class ValidationReport(TypedDict):
    passed: bool
    issues: List[ValidationIssue]
    summary: str


# Section header pattern for 三段论: "1、教材原文：" "2、试题分析：" "3、结论："
_EXPL_SECTION_HEADER_RE = re.compile(
    r"^\s*(\d+)\s*、\s*(教材原文|试题分析|结论)\s*[:：、\s]",
    re.UNICODE | re.MULTILINE,
)


def _merge_duplicate_expl_sections(text: str) -> str:
    """Ensure each of 教材原文/试题分析/结论 appears exactly once; merge duplicates."""
    if not (text or text.strip()):
        return text
    matches = list(_EXPL_SECTION_HEADER_RE.finditer(text))
    if not matches:
        return text
    # Build (section_name, start, end) for each block; end = next section start or end of text
    blocks = []
    for i, m in enumerate(matches):
        num, name = m.group(1), m.group(2)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        # Strip the header line from body so we have content only
        first_nl = body.find("\n")
        if first_nl >= 0:
            body = body[first_nl + 1 :].strip()
        else:
            body = ""
        blocks.append((name, body))
    # Count by section name
    counts = Counter(b[0] for b in blocks)
    if all(c == 1 for c in counts.values()):
        return text
    # Keep first 教材原文, first 试题分析, last 结论 (so 本题答案为 is preserved)
    first_textbook = next((b[1] for b in blocks if b[0] == "教材原文"), "")
    first_analysis = next((b[1] for b in blocks if b[0] == "试题分析"), "")
    last_conclusion_blocks = [b[1] for b in blocks if b[0] == "结论"]
    last_conclusion = last_conclusion_blocks[-1] if last_conclusion_blocks else ""
    return (
        "1、教材原文：\n"
        + first_textbook
        + "\n\n2、试题分析：\n"
        + first_analysis
        + "\n\n3、结论：\n"
        + last_conclusion
    ).strip()


def _strip_target_title_label(explanation: str) -> str:
    """Remove literal '目标题：' from explanation so display shows only the content (routing first three titles)."""
    if not explanation:
        return explanation
    return re.sub(r"目标题\s*[：:]\s*", "", explanation)


def normalize_explanation_three_stage(text: str) -> str:
    """Normalize explanation to 1、2、3、 format (顿号) per 试题解析三段论: 1、教材原文 2、试题分析 3、结论. Each section must appear exactly once."""
    s = str(text or "").strip()
    if not s:
        return s
    # Already starts with 1、 or 1. → unify to 1、2、3、 and return
    if s.startswith("1.") or s.startswith("1、") or "1. 教材原文" in s[:25] or "1、教材原文" in s[:25]:
        s = re.sub(r"^(1\.)\s*", "1、", s, count=1)
        s = re.sub(r"(\n)(2\.)\s*", r"\n2、", s, count=1)
        s = re.sub(r"(\n)(3\.)\s*", r"\n3、", s, count=1)
        s = _merge_duplicate_expl_sections(s)
        return _strip_target_title_label(s)
    # 【】 style
    s = s.replace("【教材原文】", "1、教材原文：", 1)
    s = s.replace("【试题分析】", "\n2、试题分析：", 1)
    if "【本题答案为" in s and "3、" not in s and "3." not in s:
        s = s.replace("【本题答案为", "\n3、结论：【本题答案为", 1)
    # 教材原文(了解) / 教材原文（了解）→ 1、教材原文：
    s = re.sub(r"^(教材原文\s*[（(][^）)]+[）)]\s*)", "1、教材原文：", s, count=1, flags=re.MULTILINE)
    # Plain 教材原文 / 教材原文：
    s = re.sub(r"^(教材原文[：:]\s*)", "1、教材原文：", s, count=1, flags=re.MULTILINE)
    if not s.startswith("1、") and not s.startswith("1."):
        s = re.sub(r"^(教材原文)\s*(\n)?", r"1、教材原文：\2", s, count=1)
    # 试题分析 / 结论 at line start
    s = re.sub(r"(\n)(试题分析[：:]\s*)", r"\n2、试题分析：", s, count=1)
    s = re.sub(r"(\n)(结论[：:]\s*)", r"\n3、结论：", s, count=1)
    if "本题答案为" in s and "3、结论：" not in s and "3. 结论：" not in s:
        s = re.sub(r"(\n)(本题答案为)", r"\n3、结论：\2", s, count=1)
    if not s.startswith("1.") and not s.startswith("1、"):
        s = "1、教材原文：" + s
    s = _merge_duplicate_expl_sections(s)
    return _strip_target_title_label(s)


def _ensure_draft_v1(payload: Dict[str, Any]) -> DraftV1:
    """Keep only DraftV1 contract fields for generator outputs."""
    if not isinstance(payload, dict):
        return {"question": "", "options": [], "answer": "", "explanation": ""}
    # Accept both EN/中文 keys from LLM outputs; otherwise stem can be dropped.
    question = str(payload.get("question", "") or payload.get("题干", "") or "")

    options_raw = payload.get("options", [])
    if not isinstance(options_raw, list) or not options_raw:
        zh_opts: List[str] = []
        for i in range(1, 9):
            v = str(payload.get(f"选项{i}", "") or "").strip()
            if v:
                zh_opts.append(v)
        options_raw = zh_opts
    options = [str(x) for x in options_raw] if isinstance(options_raw, list) else []

    answer = payload.get("answer", "")
    if answer in (None, "", []):
        answer = payload.get("正确答案", "")

    explanation_raw = str(payload.get("explanation", "") or payload.get("解析", "") or "")
    explanation = normalize_explanation_three_stage(explanation_raw)
    return {
        "question": question,
        "options": options,
        "answer": answer,
        "explanation": explanation,
    }

# NOTE:
# The installed `langgraph` version in this repo does NOT support `config_schema`
# in `StateGraph.compile()`, so Studio cannot auto-render a configurable UI form
# from a schema here. We keep runtime defaults and rely on env/config file for
# model/api_key/base_url.

# --- Helper Functions ---
_DEFAULT_RETRIEVER: Optional[KnowledgeRetriever] = None
_GLOSSARY_CACHE: Optional[Dict[str, Any]] = None


def get_default_retriever() -> Optional[KnowledgeRetriever]:
    """Lazily initialize a local retriever for Studio/HTTP runs.

    This enables `examples` retrieval even when callers cannot pass a Python
    object via `configurable.retriever` (e.g., LangGraph Studio).
    """

    global _DEFAULT_RETRIEVER
    if _DEFAULT_RETRIEVER is None:
        try:
            _DEFAULT_RETRIEVER = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
        except Exception:
            _DEFAULT_RETRIEVER = None
    return _DEFAULT_RETRIEVER


def _normalize_term_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = re.sub(r"\s+", "", text)
    cleaned = re.sub(r"[“”\"'`·•,，。！？；;：:（）()【】\\[\\]<>《》/\\\\-]", "", cleaned)
    return cleaned.strip()


def _build_glossary_cache() -> Dict[str, Any]:
    global _GLOSSARY_CACHE
    if _GLOSSARY_CACHE is not None:
        return _GLOSSARY_CACHE

    glossary_path = Path("房地产行业专有名词新.xlsx")
    terms_by_category: Dict[str, List[str]] = defaultdict(list)
    all_terms: List[str] = []

    if glossary_path.exists():
        try:
            import pandas as pd  # Lazy import to avoid hard dependency at module import time
            xls = pd.ExcelFile(glossary_path)
            generic_headers = {"核心名词", "专有名词"}
            for sheet in xls.sheet_names:
                df = pd.read_excel(glossary_path, sheet_name=sheet)
                col_names = [str(c).strip() for c in df.columns if str(c).strip()]
                # Some sheets use a concrete term as the only header (e.g. 商业贷款), keep it.
                for col in col_names:
                    norm_col = _normalize_term_text(col)
                    if len(norm_col) >= 2 and col not in generic_headers:
                        terms_by_category[sheet].append(norm_col)
                        all_terms.append(norm_col)
                for col in df.columns:
                    series = df[col].dropna()
                    for value in series:
                        term = _normalize_term_text(str(value))
                        if len(term) >= 2:
                            terms_by_category[sheet].append(term)
                            all_terms.append(term)
        except Exception as e:
            print(f"⚠️ 专有名词库加载失败（xlsx）: {e}")

    # Fallback to txt cache if xlsx unavailable
    txt_path = Path("教材提取专有名词.txt")
    if (not all_terms) and txt_path.exists():
        try:
            for line in txt_path.read_text(encoding="utf-8").splitlines():
                term = _normalize_term_text(line)
                if len(term) >= 2:
                    terms_by_category["fallback_txt"].append(term)
                    all_terms.append(term)
        except Exception as e:
            print(f"⚠️ 专有名词库加载失败（txt）: {e}")

    dedup_terms: List[str] = []
    seen = set()
    for t in all_terms:
        if t in seen:
            continue
        seen.add(t)
        dedup_terms.append(t)

    dedup_terms.sort(key=len, reverse=True)
    for cat, terms in list(terms_by_category.items()):
        u = []
        s = set()
        for t in terms:
            if t and t not in s:
                s.add(t)
                u.append(t)
        terms_by_category[cat] = u

    term_to_categories: Dict[str, List[str]] = defaultdict(list)
    for cat, ts in terms_by_category.items():
        for t in ts:
            term_to_categories[t].append(cat)

    _GLOSSARY_CACHE = {
        "terms": dedup_terms,
        "terms_by_category": dict(terms_by_category),
        "term_to_categories": dict(term_to_categories),
    }
    return _GLOSSARY_CACHE


def _build_kb_term_context(kb_chunk: Dict[str, Any]) -> str:
    parts: List[str] = []
    if not isinstance(kb_chunk, dict):
        return ""
    parts.append(str(kb_chunk.get("完整路径", "") or ""))
    parts.append(str(kb_chunk.get("核心内容", "") or ""))
    struct = kb_chunk.get("结构化内容", {}) or {}
    parts.append(json.dumps(struct, ensure_ascii=False))
    return "\n".join([p for p in parts if p])


def _semantic_term_match(term: str, context_text: str, category: str, category_terms: List[str], path_text: str) -> bool:
    if term in path_text:
        return True
    if len(term) >= 3:
        return True
    # For very short terms, require stronger contextual evidence.
    for kw in re.split(r"[、与和/]", category or ""):
        kw = _normalize_term_text(kw)
        if len(kw) >= 2 and kw in context_text:
            return True
    sibling_hits = 0
    for t in category_terms[:30]:
        if t != term and t in context_text:
            sibling_hits += 1
            if sibling_hits >= 1:
                return True
    # Fallback: must appear multiple times if no category evidence.
    return context_text.count(term) >= 2


def detect_term_locks_from_kb(kb_chunk: Dict[str, Any]) -> List[str]:
    glossary = _build_glossary_cache()
    terms = glossary.get("terms", []) or []
    terms_by_category = glossary.get("terms_by_category", {}) or {}
    if not terms:
        return []

    context_raw = _build_kb_term_context(kb_chunk)
    context_text = _normalize_term_text(context_raw)
    path_text = _normalize_term_text(str((kb_chunk or {}).get("完整路径", "") or ""))
    locks: List[str] = []

    for term in terms:
        if term not in context_text:
            continue
        matched = False
        for category, cat_terms in terms_by_category.items():
            if term in cat_terms:
                if _semantic_term_match(term, context_text, category, cat_terms, path_text):
                    matched = True
                    break
        if matched or term in path_text:
            locks.append(term)

    # Dedup and keep longer terms first to avoid shorter overlapping aliases.
    uniq = []
    seen = set()
    for t in sorted(locks, key=len, reverse=True):
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _question_text_for_term_check(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    fields = []
    for key in ["题干", "解析", "question", "explanation"]:
        if key in payload:
            fields.append(str(payload.get(key, "") or ""))
    if isinstance(payload.get("options"), list):
        fields.extend([str(x) for x in payload.get("options", []) if x is not None])
    for i in range(1, 9):
        v = payload.get(f"选项{i}")
        if v is not None:
            fields.append(str(v))
    return _normalize_term_text(" ".join(fields))


def detect_term_lock_violations(term_locks: List[str], payload: Dict[str, Any]) -> List[str]:
    if not term_locks:
        return []
    lock_set = set(term_locks or [])
    raw_text_parts = []
    if isinstance(payload, dict):
        for key in ["题干", "解析", "question", "explanation"]:
            if key in payload:
                raw_text_parts.append(str(payload.get(key, "") or ""))
        if isinstance(payload.get("options"), list):
            raw_text_parts.extend([str(x) for x in payload.get("options", []) if x is not None])
        for i in range(1, 9):
            v = payload.get(f"选项{i}") if isinstance(payload, dict) else None
            if v is not None:
                raw_text_parts.append(str(v))
    raw_text = " ".join(raw_text_parts)
    text = _question_text_for_term_check(payload)
    if not text:
        return []
    glossary = _build_glossary_cache()
    all_terms = glossary.get("terms", []) or []
    present_terms = [t for t in all_terms if t in text]

    def _looks_like_substitution(lock: str, cand: str) -> bool:
        if cand == lock:
            return False
        def _is_subseq(shorter: str, longer: str) -> bool:
            it = iter(longer)
            return all(ch in it for ch in shorter)
        # Abbreviation-like pattern: same first/last char and candidate is shorter.
        if len(cand) >= 2 and len(cand) < len(lock):
            if lock[0] == cand[0] and lock[-1] == cand[-1]:
                return True
            # Abbreviation-like subsequence (e.g., 商业贷款 -> 商贷)
            if lock[0] == cand[0] and _is_subseq(cand, lock):
                return True
        # Prefix/suffix containment relation (e.g., 全称/简称 variants).
        if lock.startswith(cand) or cand.startswith(lock) or lock.endswith(cand) or cand.endswith(lock):
            return True
        return False

    def _is_explanatory_usage(lock: str, cand: str, source_text: str) -> bool:
        if not source_text:
            return False
        explain_keywords = ["简称", "又称", "也称", "俗称", "即", "是指", "指的是", "全称"]
        # Sentence-level relaxation: same sentence contains both terms + explanation keyword.
        for sentence in re.split(r"[。！？；;!\n]", source_text):
            if lock in sentence and cand in sentence and any(k in sentence for k in explain_keywords):
                return True
        # Allow explicit terminology explanation forms.
        patterns = [
            rf"{re.escape(lock)}\s*(?:（[^）]{{0,30}}）)?\s*(?:简称|又称|也称|俗称|即|是指|指的是|全称)\s*{re.escape(cand)}",
            rf"{re.escape(cand)}\s*(?:（[^）]{{0,30}}）)?\s*(?:简称|又称|也称|俗称|即|是指|指的是|全称)\s*{re.escape(lock)}",
            rf"{re.escape(lock)}\s*[:：]\s*{re.escape(cand)}",
            rf"{re.escape(cand)}\s*[:：]\s*{re.escape(lock)}",
        ]
        return any(re.search(p, source_text) for p in patterns)

    violations: List[str] = []
    for lock in term_locks:
        similar_hits = []
        for t in present_terms:
            if t == lock:
                continue
            # If candidate itself is also a locked term for this chunk, treat as coexisting
            # mandatory terminology instead of replacement.
            if t in lock_set:
                continue
            if not _looks_like_substitution(lock, t):
                continue
            if _is_explanatory_usage(lock, t, raw_text):
                continue
            if t not in similar_hits:
                similar_hits.append(t)
            if len(similar_hits) >= 3:
                break
        if similar_hits:
            violations.append(f"术语疑似改词：应为“{lock}”，检测到近似词“{'/'.join(similar_hits)}”")
    return violations


def enforce_term_locks(term_locks: List[str], payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict) or not term_locks:
        return payload
    glossary = _build_glossary_cache()
    all_terms = glossary.get("terms", []) or []
    fixed = dict(payload)

    def _replace_in_text(text: str, lock: str) -> str:
        if not text or lock in _normalize_term_text(text):
            return text
        lock_chars = set(lock)
        for t in all_terms[:500]:
            if t == lock or len(t) < 2:
                continue
            t_chars = set(t)
            overlap = len(lock_chars & t_chars)
            if overlap == 0:
                continue
            jaccard = overlap / max(1, len(lock_chars | t_chars))
            if (jaccard >= 0.5 or lock in t or t in lock) and t in _normalize_term_text(text):
                text = text.replace(t, lock)
        return text

    # Free-form fields
    for key in ["题干", "解析", "question", "explanation"]:
        if key in fixed and isinstance(fixed.get(key), str):
            value = fixed.get(key) or ""
            for lock in term_locks:
                value = _replace_in_text(value, lock)
            fixed[key] = value

    # Options list
    if isinstance(fixed.get("options"), list):
        new_opts = []
        for opt in fixed["options"]:
            opt_text = str(opt)
            for lock in term_locks:
                opt_text = _replace_in_text(opt_text, lock)
            new_opts.append(opt_text)
        fixed["options"] = new_opts

    # Flat options
    for i in range(1, 9):
        key = f"选项{i}"
        if key in fixed and isinstance(fixed.get(key), str):
            value = fixed.get(key) or ""
            for lock in term_locks:
                value = _replace_in_text(value, lock)
            fixed[key] = value
    return fixed


def parse_json_from_response(text: str) -> Dict:
    """
    Robustly extracts and parses JSON from LLM response text.
    Handles markdown code blocks, plain JSON, and common formatting issues.
    """
    if not text:
        raise ValueError("Empty response from LLM")

    # Some providers return list content; normalize to string.
    if isinstance(text, list):
        text = "\n".join([str(item) for item in text if item is not None])
    elif not isinstance(text, str):
        text = str(text)

    text = text.strip()
    
    # 1. Try to find JSON within markdown code blocks
    # Matches ```json { ... } ``` or ``` { ... } ```
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        # 2. Try to find the first '{' and last '}'
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            json_str = text[start:end+1]
        else:
            # 3. Assume the whole text is JSON
            json_str = text
            
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        # Provide a snippet of the failed text for debugging
        snippet = json_str[:200] + "..." if len(json_str) > 200 else json_str
        raise ValueError(f"Failed to parse JSON: {e}. Content snippet: {snippet}")

# --- LLM Factory ---

def _extract_usage_dict(usage: Any) -> Dict[str, Optional[int]]:
    if not usage:
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
    if isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
            try:
                total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
            except Exception:
                total_tokens = None
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
        try:
            total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
        except Exception:
            total_tokens = None
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def summarize_llm_trace(trace: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_node: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "latency_ms": 0.0,
        "errors": 0,
    })
    by_model: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "calls": 0,
        "total_tokens": 0,
        "latency_ms": 0.0,
        "errors": 0,
    })

    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    total_latency_ms = 0.0
    error_calls = 0
    critic_calls = 0

    for item in trace or []:
        node = str(item.get("node", "unknown"))
        root_node = node.split(".", 1)[0] if node else "unknown"
        model = str(item.get("model", "unknown"))
        prompt_tokens = int(item.get("prompt_tokens") or 0)
        completion_tokens = int(item.get("completion_tokens") or 0)
        call_tokens = int(item.get("total_tokens") or (prompt_tokens + completion_tokens) or 0)
        latency_ms = float(item.get("latency_ms") or 0.0)
        success = bool(item.get("success", False))

        total_prompt += prompt_tokens
        total_completion += completion_tokens
        total_tokens += call_tokens
        total_latency_ms += latency_ms
        if not success:
            error_calls += 1
        if root_node == "critic":
            critic_calls += 1

        node_bucket = by_node[root_node]
        node_bucket["calls"] += 1
        node_bucket["prompt_tokens"] += prompt_tokens
        node_bucket["completion_tokens"] += completion_tokens
        node_bucket["total_tokens"] += call_tokens
        node_bucket["latency_ms"] += latency_ms
        if not success:
            node_bucket["errors"] += 1

        model_bucket = by_model[model]
        model_bucket["calls"] += 1
        model_bucket["total_tokens"] += call_tokens
        model_bucket["latency_ms"] += latency_ms
        if not success:
            model_bucket["errors"] += 1

    return {
        "total_llm_calls": len(trace or []),
        "error_calls": error_calls,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "total_latency_ms": round(total_latency_ms, 2),
        "critic_calls": critic_calls,
        "by_node": dict(by_node),
        "by_model": dict(by_model),
    }


def mark_unstable(summary: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    if int(summary.get("total_tokens") or 0) > 3000:
        flags.append("high_total_tokens")
    if int(summary.get("critic_calls") or 0) > 3:
        flags.append("too_many_critic_calls")
    if float(summary.get("total_latency_ms") or 0.0) > 10000:
        flags.append("high_total_latency")
    if int(summary.get("error_calls") or 0) > 0:
        flags.append("llm_errors_present")
    return flags


def call_llm(
    node_name: str,
    prompt: str,
    model_name: str,
    api_key: str = None,
    base_url: str = None,
    provider: str = None,
    trace_id: Optional[str] = None,
    question_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    timeout: int = 300,
) -> Tuple[str, str, Dict[str, Any]]:
    # NOTE: In Studio UI, users might omit config; provide safe defaults.
    if not model_name:
        model_name = MODEL_NAME or "deepseek-chat"

    provider = str(provider or "").lower()
    request_timeout_cap = int(str(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "90")).strip() or 90)
    total_timeout_cap = int(str(os.getenv("LLM_TOTAL_TIMEOUT_SECONDS", "240")).strip() or 240)
    effective_timeout = max(10, min(int(timeout or 300), request_timeout_cap, total_timeout_cap))
    model_lower = model_name.lower()
    base_url_lower = str(base_url or "").lower()
    if provider:
        is_ark = provider == "ark"
    else:
        is_ark = ("volces.com" in base_url_lower) or ("ark.cn" in base_url_lower)

    def is_retryable_error(err: Exception) -> bool:
        err_str = str(err)
        err_lower = err_str.lower()
        return (
            "429" in err_str
            or "rate" in err_lower
            or "too many" in err_lower
            or "500" in err_str
            or "502" in err_str
            or "503" in err_str
            or "504" in err_str
            or "timeout" in err_lower
            or "timed out" in err_lower
            or "connection" in err_lower
        )

    def build_record(
        *,
        success: bool,
        used_model: str,
        provider_used: str,
        started_at: float,
        retries: int,
        usage_obj: Any = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        ended_at = time.time()
        usage = _extract_usage_dict(usage_obj)
        return {
            "call_id": uuid.uuid4().hex,
            "trace_id": trace_id,
            "question_id": question_id,
            "node": node_name,
            "provider": provider_used,
            "model": used_model,
            "prompt_version": prompt_version,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "latency_ms": round((ended_at - started_at) * 1000, 2),
            "retries": retries,
            "success": success,
            "error": error,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended_at)),
            "ts_ms": int(ended_at * 1000),
        }

    if is_ark:
        started = time.time()
        ark_backoff_seconds = [2, 5, 10]
        for attempt in range(len(ark_backoff_seconds) + 1):
            if time.time() - started > total_timeout_cap:
                record = build_record(
                    success=False,
                    used_model=model_name,
                    provider_used="ark",
                    started_at=started,
                    retries=attempt,
                    usage_obj=None,
                    error=f"LLM total timeout exceeded ({total_timeout_cap}s)",
                )
                return "", model_name, record
            try:
                ark_key = ARK_API_KEY or api_key
                if ark_key:
                    client = Ark(
                        api_key=ark_key,
                        base_url=(base_url or ARK_BASE_URL),
                    )
                else:
                    if not (VOLC_ACCESS_KEY_ID and VOLC_SECRET_ACCESS_KEY):
                        raise ValueError("ARK_API_KEY is required for Ark chain, or provide VOLC_ACCESS_KEY_ID / VOLC_SECRET_ACCESS_KEY")
                    client = Ark(
                        ak=VOLC_ACCESS_KEY_ID,
                        sk=VOLC_SECRET_ACCESS_KEY,
                        base_url=(base_url or ARK_BASE_URL),
                    )
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=effective_timeout,
                    extra_headers=({"X-Project-Name": ARK_PROJECT_NAME} if ARK_PROJECT_NAME else None),
                )
                content = resp.choices[0].message.content if resp.choices else ""
                record = build_record(
                    success=True,
                    used_model=model_name,
                    provider_used="ark",
                    started_at=started,
                    retries=attempt,
                    usage_obj=getattr(resp, "usage", None),
                )
                return content, model_name, record
            except Exception as e:
                if is_retryable_error(e) and attempt < len(ark_backoff_seconds):
                    wait_time = ark_backoff_seconds[attempt]
                    print(f"⚠️ Ark 限流/服务错误，等待 {wait_time}s 后重试 (第 {attempt+1} 次)")
                    time.sleep(wait_time)
                    continue
                print(f"❌ Ark 调用失败: {e}")
                record = build_record(
                    success=False,
                    used_model=model_name,
                    provider_used="ark",
                    started_at=started,
                    retries=attempt,
                    usage_obj=None,
                    error=str(e),
                )
                return "", model_name, record

    key = api_key or API_KEY
    url = base_url or BASE_URL
    used_model = model_name
    backoff_seconds = [2, 5, 10]
    started = time.time()
    url_candidates: List[str] = []
    base_u = str(url or "").rstrip("/")
    if base_u:
        url_candidates.append(base_u)
        if not base_u.endswith("/v1"):
            url_candidates.append(f"{base_u}/v1")
    else:
        url_candidates.append(base_u)
    # de-duplicate while preserving order
    seen_url = set()
    url_candidates = [u for u in url_candidates if not (u in seen_url or seen_url.add(u))]

    for attempt in range(len(backoff_seconds) + 1):
        if time.time() - started > total_timeout_cap:
            record = build_record(
                success=False,
                used_model=used_model,
                provider_used=(provider or "ait"),
                started_at=started,
                retries=attempt,
                usage_obj=None,
                error=f"LLM total timeout exceeded ({total_timeout_cap}s)",
            )
            return "", used_model, record
        try:
            last_non_retryable: Optional[Exception] = None
            for candidate_url in url_candidates:
                try:
                    client = OpenAI(api_key=key, base_url=candidate_url)
                    resp = client.chat.completions.create(
                        model=used_model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=effective_timeout,
                    )
                    content = resp.choices[0].message.content if resp.choices else ""
                    if isinstance(content, list):
                        content = "\n".join(
                            str(item.get("text", "")) if isinstance(item, dict) else str(item)
                            for item in content
                        ).strip()
                    elif not isinstance(content, str):
                        content = str(content or "")
                    if not content.strip():
                        raise ValueError(f"Empty response (attempt {attempt + 1})")
                    record = build_record(
                        success=True,
                        used_model=used_model,
                        provider_used=(provider or "ait"),
                        started_at=started,
                        retries=attempt,
                        usage_obj=getattr(resp, "usage", None),
                    )
                    return content, used_model, record
                except Exception as inner:
                    if is_retryable_error(inner):
                        raise
                    last_non_retryable = inner
            if last_non_retryable is not None:
                raise last_non_retryable
            raise RuntimeError("No valid base_url candidate for OpenAI-compatible call")
        except Exception as e:
            err_str = str(e)
            is_retryable = is_retryable_error(e)
            if is_retryable and attempt < len(backoff_seconds):
                wait_time = backoff_seconds[attempt]
                print(f"⚠️ OpenAI-compatible 限流/服务错误，等待 {wait_time}s 后重试 (第 {attempt+1} 次)")
                time.sleep(wait_time)
                continue
            record = build_record(
                success=False,
                used_model=used_model,
                provider_used=(provider or "ait"),
                started_at=started,
                retries=attempt,
                usage_obj=None,
                error=err_str,
            )
            return "", used_model, record


def generate_content(model_name: str, prompt: str, api_key: str = None, base_url: str = None, provider: str = None, return_model: bool = False):
    # Backward-compatible wrapper for legacy scripts.
    content, used_model, _ = call_llm(
        node_name="legacy.generate_content",
        prompt=prompt,
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        provider=provider,
    )
    return (content, used_model) if return_model else content


def resolve_code_gen_provider(model_name: str, provider: Optional[str], fallback_provider: Optional[str] = None):
    if provider:
        return provider
    if model_name:
        model_lower = model_name.lower()
        if model_lower.startswith("gpt") or "doubao" in model_lower:
            return "ait"
    return fallback_provider


def resolve_provider(model_name: str, base_url: Optional[str], fallback_provider: Optional[str] = None) -> str:
    """Resolve provider for generic LLM calls. Current policy: always use AIT."""
    if fallback_provider and str(fallback_provider).strip().lower() == "ait":
        return "ait"
    return "ait"


def _resolve_specialist_writer_model(state: AgentState, default_model: str) -> Tuple[str, str]:
    """
    Specialist/Writer model policy:
    1) After critic decides reroute (router re-entered), force gpt-5.2.
    2) In non-reroute rounds, if router judged calculation-needed, force gpt-5.2.
    """
    target_model = "gpt-5.2"
    is_reroute_round = bool(state.get("is_reroute_round"))
    router_details = state.get("router_details") or {}
    need_calc_raw = router_details.get("need_calculation")
    if isinstance(need_calc_raw, bool):
        need_calculation = need_calc_raw
    elif isinstance(need_calc_raw, str):
        need_calculation = need_calc_raw.strip().lower() in {"1", "true", "yes", "y", "on"}
    else:
        need_calculation = bool(need_calc_raw)
    if is_reroute_round:
        return target_model, "reroute_round"
    if need_calculation:
        return target_model, "router_need_calculation"
    return default_model, "default"

# --- Nodes ---

def router_node(state: AgentState, config):
    kb_chunk = state['kb_chunk']
    retriever = config['configurable'].get('retriever') or get_default_retriever()
    examples = state.get('examples', [])
    kb_context, parent_slices, related_slices = build_extended_kb_context(kb_chunk, retriever, examples)

    self_check_issues = state.get("self_check_issues") or []
    if not isinstance(self_check_issues, list):
        self_check_issues = []
    router_round = int(state.get("retry_count", 0) or 0)
    is_reroute_round = router_round > 0
    qid = str(state.get("question_id", "") or "").strip() or "-"
    print(f"🧭 Router Enter: question_id={qid} router_round={router_round}")
    retriever = config['configurable'].get('retriever')
    retriever = config['configurable'].get('retriever')
    retriever = config['configurable'].get('retriever')
    retriever = config['configurable'].get('retriever')
    retriever = config['configurable'].get('retriever')
    retriever = config['configurable'].get('retriever')
    retriever = config['configurable'].get('retriever')
    # 1. Analyze Content Features for Question Type Recommendation
    content = kb_chunk['核心内容']
    path = kb_chunk['完整路径']
    mastery = kb_chunk.get('掌握程度', '未知')
    struct = kb_chunk.get('结构化内容', {})
    
    # Feature Detection
    has_formulas = len(struct.get('formulas', [])) > 0
    has_tables = len(struct.get('tables', [])) > 0
    # Simple heuristic for list: text contains multiple numbered items like (1) (2) or 1. 2.
    has_list = bool(re.search(r'（\d+）|\d+\.', content)) and content.count('\n') > 3
    has_calc_keywords = bool(
        re.search(
            r"(计算|金额|税额|税费|补交|超标款|贷款|利率|成数|比例|面积|年限|公式|分润|指数)",
            f"{path}\n{content}",
        )
    )
    has_calc_operands = bool(re.search(r"(×|/|=|％|%|㎡|平方米|元|万元|\d+\.\d+|\d+)", content))
    has_calc_signal = has_formulas or (has_calc_keywords and has_calc_operands)
    term_locks = detect_term_locks_from_kb(kb_chunk)
    high_risk_profile = detect_router_high_risk_slice(content, path)
    formula_ambiguity_risk = detect_router_formula_ambiguity_risk(content, path)
    rule_precondition_profile = detect_router_rule_precondition_profile(content, path)
    
    recommended_type = "单选题" # Default
    if has_calc_signal:
        recommended_type = "单选题" # Calculation usually single choice
    elif has_list:
        recommended_type = "多选题" # Lists are perfect for multi-select
    elif has_tables:
        recommended_type = "判断题" # Tables are good for True/False checks on details
    if high_risk_profile.get("prohibit_single_choice"):
        recommended_type = "多选题"
        
    prompt = f"""
# 角色
你是路由代理 (Router Agent)。
你的任务是根据【参考材料】的内容，判断最佳的出题专家和题型策略。

# 好立意标准（考什么内容）
1. **聚焦贴业务**：命题必须聚焦房地产经纪人实际工作场景，考察实用常见的业务知识。
2. **直接不拐弯**：考点要直接明确，不绕弯子，让学员能清晰理解考察重点。
3. **适纲性强**：必须基于教材知识切片，不超纲，不引入外部条件。

# 参考材料
【路径】: {path}
【掌握程度】: {mastery}
【内容】:
{content}
【完整切片】:
{kb_context}
【特征】: 包含公式={has_formulas}, 包含计算信号={has_calc_signal}, 包含列表={has_list}, 包含表格={has_tables}

# 专家列表
1. **CalculatorAgent (计算专家)**: 专门处理需要**数值计算**的题目。
   - **触发条件**: 知识点包含明确的计算公式(formulas)、或者需要逻辑推演计算。
   
2. **LegalAgent (法律专家)**: 擅长法律法规、违规处罚、纠纷处理。
   - **触发条件**: 涉及法律条文、罚则、年限规定。
   
3. **GeneralAgent (综合专家)**: 默认选项，处理概念、流程、业务常识。

# 决策逻辑
1. **优先判断计算**: 如果包含公式或需要计算 -> CalculatorAgent
2. **其次判断法律**: 如果是纯法规/年限/罚款 -> LegalAgent
3. **否则**: GeneralAgent

# 输出格式
请严格按照 JSON 格式输出:
- "agent": "CalculatorAgent" / "LegalAgent" / "GeneralAgent"
- "score_calculation": 0-10
- "score_legal": 0-10
- "need_calculation": true/false
- "recommended_type": "单选题" / "多选题" / "判断题"
- "question_type_reason": "为什么更适合这个题型"
- "core_focus": "当前切片最重要、最值得优先考察的核心考点，必须具体"
- "secondary_focuses": ["次要考点1", "次要考点2"]
- "minor_focuses": ["再次要考点1", "再次要考点2"]
- "focus_rule": "可判定的主规则句（不要只写片段标题）"
- "focus_variables": ["本题必须显式出现的关键变量，如户籍/区域/时间/套数/口径"]
- "focus_task": "规则判定/数值计算/流程判定/规则理解（四选一）"
- "reasoning": "决策理由"
"""
    
    model_to_use = ROUTER_MODEL or MODEL_NAME
    llm_records: List[Dict[str, Any]] = []
    response_text, _, llm_record = call_llm(
        node_name="router.route",
        prompt=prompt,
        model_name=model_to_use,
        api_key=API_KEY,
        base_url=BASE_URL,
        trace_id=state.get("trace_id"),
        question_id=state.get("question_id"),
    )
    llm_records.append(llm_record)
    
    try:
        result = parse_json_from_response(response_text)
        agent = result.get("agent", "GeneralAgent")
        score_calculation = result.get("score_calculation", 0)
        score_legal = result.get("score_legal", 0)
        need_calculation = result.get("need_calculation", False)
        reasoning = result.get("reasoning", "")
        llm_recommended_type = str(result.get("recommended_type", "") or "").strip()
        if llm_recommended_type in ["单选题", "多选题", "判断题"]:
            recommended_type = llm_recommended_type
        question_type_reason = str(result.get("question_type_reason", "") or "").strip()
        core_focus = str(result.get("core_focus", "") or "").strip()
        llm_focus_rule = str(result.get("focus_rule", "") or "").strip()
        llm_focus_variables = result.get("focus_variables") if isinstance(result.get("focus_variables"), list) else []
        llm_focus_task = str(result.get("focus_task", "") or "").strip()
        secondary_focuses = [
            str(x).strip()
            for x in (result.get("secondary_focuses") or [])
            if str(x).strip()
        ][:3]
        minor_focuses = [
            str(x).strip()
            for x in (result.get("minor_focuses") or [])
            if str(x).strip()
        ][:3]
        
        # Override agent based on rigid features if LLM missed it
        if has_calc_signal and agent != "CalculatorAgent":
             agent = "CalculatorAgent"
             need_calculation = True
             reasoning += " (强制修正: 检测到计算信号)"
        if formula_ambiguity_risk.get("has_ranking_formula_ambiguity"):
            agent = "GeneralAgent"
            need_calculation = False
            recommended_type = "判断题"
            question_type_reason = "切片公式存在歧义/易漂移风险，优先出判断/理解题，避免计算题多解。"
            reasoning += " (公式歧义保护: 禁止计算路由)"
        if formula_ambiguity_risk.get("has_parallel_formula_without_merge"):
            agent = "GeneralAgent"
            need_calculation = False
            recommended_type = "多选题"
            question_type_reason = "切片存在“可同时使用”但未给合并公式，优先出规则判定题，避免计算多解。"
            reasoning += " (并行公式歧义保护: 禁止计算路由)"
        if formula_ambiguity_risk.get("has_coeff_lookup_dependency"):
            # 通用“系数查表依赖”风险：不禁止计算，但强制后续补齐可解性槽位
            question_type_reason = "切片存在系数查表依赖，题干必须补齐系数来源与触发条件，避免多解。"
            reasoning += " (系数依赖保护: 需补齐前置条件)"
        if high_risk_profile.get("prohibit_single_choice"):
            recommended_type = "多选题"
            extra = []
            if high_risk_profile.get("has_material_checklist"):
                extra.append("切片包含多个必备材料")
            if high_risk_profile.get("has_parallel_rules"):
                extra.append("切片包含多个并列规则/条件/情形")
            question_type_reason = f"当前切片{ '，'.join(extra) }，默认禁出单选，更适合用多选题完整覆盖核心要点。"
        
    except Exception as e:
        print(f"⚠️ Router JSON parsing failed: {e}. Defaulting to GeneralAgent.")
        agent = "GeneralAgent"
        score_calculation = 0
        score_legal = 0
        need_calculation = False
        reasoning = f"Parsing Error: {str(e)}"
        question_type_reason = ""
        core_focus = ""
        llm_focus_rule = ""
        llm_focus_variables = []
        llm_focus_task = ""
        secondary_focuses = []
        minor_focuses = []

    if not core_focus:
        core_focus = str(path.split(" > ")[-1] or "").strip() or "当前切片核心规则"
    if not question_type_reason:
        question_type_reason = f"当前切片更适合使用【{recommended_type}】考察核心规则。"
    focus_contract = _derive_focus_contract(
        path=path,
        content=content,
        core_focus=core_focus,
        has_calc_signal=has_calc_signal,
        has_list=has_list,
        llm_focus_rule=llm_focus_rule,
        llm_focus_variables=llm_focus_variables,
        llm_focus_task=llm_focus_task,
    )
    prev_locked_focus = state.get("locked_focus_contract") or {}
    if is_reroute_round and isinstance(prev_locked_focus, dict) and prev_locked_focus.get("focus_rule"):
        # reroute rounds must not drift away from already-locked core focus
        focus_contract = dict(prev_locked_focus)
        core_focus = str(focus_contract.get("focus_rule", "") or core_focus)
        question_type_reason = f"沿用已锁定主考点：{core_focus}"

    # Basic validation
    if agent not in ["CalculatorAgent", "FinanceAgent", "LegalAgent", "GeneralAgent"]:
        agent = "GeneralAgent"
    if agent == "FinanceAgent": agent = "CalculatorAgent"

    # 清理旧状态（如果是 reroute）
    state_updates = {
        "agent_name": agent,
        "router_details": {
            "path": path,
            "content": content,
            "struct_content": struct,
            "mastery": mastery,
            "score_calculation": score_calculation,
            "score_legal": score_legal,
            "need_calculation": need_calculation,
            "agent": agent,
            "reasoning": reasoning,
            "recommended_type": recommended_type, # Pass recommendation to next node
            "question_type_reason": question_type_reason,
            "core_focus": core_focus,
            "focus_contract": focus_contract,
            "secondary_focuses": secondary_focuses,
            "minor_focuses": minor_focuses,
            "high_risk_profile": high_risk_profile,
            "formula_ambiguity_risk": formula_ambiguity_risk,
            "rule_precondition_profile": rule_precondition_profile,
            "term_locks": term_locks,
        },
        "router_round": router_round,
        "is_reroute_round": is_reroute_round,
        "term_locks": term_locks,
        "locked_focus_contract": focus_contract,
        "logs": [
            f"🧭 Router Enter: question_id={qid} router_round={router_round}",
            f"🤖 路由: 派发给 **{agent}** (特征: 公式={has_formulas}, 计算信号={has_calc_signal}, 列表={has_list}, 表格={has_tables}). 建议题型: {recommended_type}。核心考点: {core_focus}。考核任务: {focus_contract.get('focus_task','规则理解')}"
        ]
        ,
        "llm_trace": llm_records,
    }
    if term_locks:
        state_updates["logs"].append(f"🔒 Router 术语锁定: {', '.join(term_locks[:12])}")
    if rule_precondition_profile.get("enabled"):
        slots = ", ".join(rule_precondition_profile.get("required_slots") or [])
        state_updates["logs"].append(f"🧩 Router 前提槽位: {slots}")
    
    # 如果是重新路由（retry_count > 0），清理旧的生成结果与计算状态，确保下一轮全部基于新题目
    if state.get('retry_count', 0) > 0:
        # Preserve previous question and critic feedback for repair-mode prompts
        state_updates["prev_final_json"] = state.get("final_json")
        state_updates["prev_critic_feedback"] = state.get("critic_feedback")
        state_updates["prev_critic_details"] = state.get("critic_details")
        state_updates["prev_critic_result"] = state.get("critic_result")
        state_updates["prev_critic_tool_usage"] = state.get("critic_tool_usage")
        state_updates["prev_critic_rules_context"] = state.get("critic_rules_context")
        state_updates["prev_critic_related_rules"] = state.get("critic_related_rules")
        state_updates["prev_critic_basis_source"] = state.get("critic_basis_source")
        state_updates["prev_critic_basis_paths"] = state.get("critic_basis_paths")
        state_updates["prev_critic_non_current_basis"] = state.get("critic_non_current_basis")
        state_updates["reroute_basis_context"] = state.get("critic_rules_context")
        state_updates["draft"] = None
        state_updates["self_check_issues"] = None
        state_updates["final_json"] = None
        state_updates["critic_feedback"] = None
        state_updates["critic_details"] = None
        state_updates["critic_result"] = None
        state_updates["critic_tool_usage"] = None
        state_updates["critic_rules_context"] = None
        state_updates["critic_related_rules"] = None
        state_updates["critic_basis_source"] = None
        state_updates["critic_basis_paths"] = None
        state_updates["critic_non_current_basis"] = None
        state_updates["execution_result"] = None
        state_updates["generated_code"] = None
        state_updates["tool_usage"] = None
        state_updates["calc_target_signature"] = None
        state_updates["calc_unit_hint"] = None
        state_updates["calc_required_slots"] = None
        state_updates["calc_missing_slots"] = None
        state_updates["code_status"] = None
        state_updates["candidate_sentences"] = None
        # Reroute should re-select question type from latest routing context, not pin previous round type.
        state_updates["current_question_type"] = None
        state_updates["locked_question_type"] = None
        state_updates["writer_format_issues"] = None
        state_updates["writer_validation_report"] = None
        state_updates["writer_retry_exhausted"] = None
        state_updates["fix_summary"] = None
        state_updates["fix_no_change"] = None
        state_updates["fix_attempted_regen"] = None
        state_updates["fix_required_unmet"] = None
        state_updates["was_fixed"] = None
        state_updates["logs"].append(f"🔄 检测到重新路由 (retry #{state['retry_count']})，已清理旧状态")
    
    return state_updates

def specialist_node(state: AgentState, config):
    agent_name = state['agent_name']
    kb_chunk = state['kb_chunk']
    term_locks = state.get("term_locks") or []
    llm_records: List[Dict[str, Any]] = []
    retriever = config['configurable'].get('retriever') or get_default_retriever()
    examples = state.get('examples', [])
    kb_context, parent_slices, related_slices = build_extended_kb_context(kb_chunk, retriever, examples)
    reroute_basis_context = state.get("reroute_basis_context") or state.get("prev_critic_rules_context")
    generation_kb_context = kb_context
    if state.get("retry_count", 0) > 0 and isinstance(reroute_basis_context, str) and reroute_basis_context.strip():
        generation_kb_context = reroute_basis_context
    term_lock_text = ""
    if term_locks:
        term_lock_text = f"""
# 专有名词锁词约束（必须执行）
以下术语若在题干/选项/解析中使用，必须保持原词，不得同义替换、缩写替换或解释性改写：
{json.dumps(term_locks, ensure_ascii=False)}
"""
    
    # Fetch examples AFTER routing, based on knowledge point and question type
    retriever = config['configurable'].get('retriever')
    question_type = config['configurable'].get('question_type')
    generation_mode = config['configurable'].get('generation_mode', '随机')
    effective_generation_mode, normalized_generation_mode = resolve_effective_generation_mode(generation_mode, state)
    uniqueness_note = ""
    avoid_superlative = "   - **避免“最XX”考法**：禁止用“最重要/最关键/重点/主要”等表述设计题干或选项，重点考察完整流程、条件、责任边界或操作要点。"
    # Set uniqueness constraint for single-choice questions
    if question_type == "单选题":
        uniqueness_note = "   - **唯一正确性**：确保只有一个选项严格符合教材原文及其含义，其他选项须有明确错误点，不能出现 A/B 都似乎正确的歧义。"
    
    # Get difficulty range from config
    difficulty_range = config['configurable'].get('difficulty_range')
    
    # Get mastery level from kb_chunk (FR7.5)
    mastery = kb_chunk.get('掌握程度', '未知')
    
    # 题型决策：指定题型直接使用；随机题型优先用映射母题题型
    router_details = state.get('router_details', {})
    rec_type = router_details.get('recommended_type', '单选题')
    core_focus = str(router_details.get('core_focus', '') or '').strip()
    secondary_focuses = [str(x).strip() for x in (router_details.get('secondary_focuses') or []) if str(x).strip()]
    minor_focuses = [str(x).strip() for x in (router_details.get('minor_focuses') or []) if str(x).strip()]
    question_type_reason = str(router_details.get('question_type_reason', '') or '').strip()
    rule_precondition_profile = router_details.get("rule_precondition_profile") or {}
    focus_contract = state.get("locked_focus_contract") or router_details.get("focus_contract") or {}
    focus_rule = str(focus_contract.get("focus_rule", "") or core_focus).strip()
    focus_task = str(focus_contract.get("focus_task", "") or "").strip()
    focus_variables = [str(x).strip() for x in (focus_contract.get("focus_variables") or []) if str(x).strip()]
    target_type, preferred_types = resolve_target_question_type(
        configured_question_type=question_type,
        recommended_type=rec_type,
        kb_chunk=kb_chunk,
        retriever=retriever,
    )
    # 计算题随机题型保护：默认禁出多选，避免“多选单答案”格式性失败
    if question_type == "随机" and target_type == "多选题":
        target_type = "单选题"
    # 公式歧义兜底：遇到明显歧义公式时，降级为判断题
    if re.search(r"最中国式排名|排名赋分\s*=\s*（?1-最中国式排名-1", kb_context):
        target_type = "判断题"
    high_risk_profile = router_details.get("high_risk_profile") or {}
    if high_risk_profile.get("prohibit_single_choice") and target_type == "单选题":
        target_type = "多选题"
    specialist_model_to_use, model_reason = _resolve_specialist_writer_model(
        state,
        SPECIALIST_MODEL or MODEL_NAME,
    )

    # Fetch examples logic updated:
    # 1. First priority: Structural examples from the slice itself (100% match)
    # 2. Second priority: Retrieved examples from vector DB (Reference)
    
    slice_struct = kb_chunk.get('结构化内容', {})
    builtin_examples = slice_struct.get('examples', [])
    retrieved_examples = []
    
    # Only retrieve if no builtin examples? Or always retrieve as supplement?
    # User said: "教材中找到的跟母题库关联的不要冲突，一起作为例子参考" 
    # But usually builtin is better. Let's use builtin first.
    
    if retriever:
        retrieved_examples = retriever.get_examples_by_knowledge_point(kb_chunk, k=3, question_type=target_type)

    # Combine examples for passing to state (normalize to dict)
    def _normalize_examples(ex_list):
        out = []
        for ex in ex_list or []:
            if isinstance(ex, dict):
                out.append(ex)
            else:
                txt = str(ex).strip()
                if txt:
                    out.append({"题干": txt, "解析": ""})
        return out
    examples = _normalize_examples(builtin_examples) + _normalize_examples(retrieved_examples)

    # Build extended KB context (current + parent slices + related slices by examples)
    kb_context, parent_slices, related_slices = build_extended_kb_context(kb_chunk, retriever, examples)

    # 构建 Prompt 中的范例部分
    examples_text = ""
    
    if builtin_examples:
        examples_text += "\n## 教材原题 (最高优先级参考)\n"
        for i, ex in enumerate(builtin_examples, 1):
            examples_text += f"原题 {i}:\n{ex}\n"
        examples_text += "\n**注意**：请优先仿照【教材原题】的出题逻辑、计算方式和陷阱设置，但需要更换具体的数值或场景，不要完全抄袭。\n"
            
    if retrieved_examples:
        examples_text += "\n## 外部母题参考 (仅作补充)\n"
        for i, ex in enumerate(retrieved_examples, 1):
            title = ex.get('题干', '')
            # If retrieved example is very similar to builtin, maybe skip? 
            # For now just list them.
            examples_text += f"参考题 {i}: {title}\n"
    
    mode_instructions = build_mode_instruction(effective_generation_mode, normalized_generation_mode)
    
    # Feature Injection
    struct = kb_chunk.get('结构化内容', {})
    formulas = struct.get('formulas', [])
    tables = struct.get('tables', [])
    key_params = struct.get('key_params', [])
    
    struct_instruction = ""
    if formulas:
        struct_instruction += f"\n## 核心公式 (必须基于此计算，严禁编造)\n" + "\n".join([f"- {f}" for f in formulas])
    if tables:
        struct_instruction += f"\n## 核心表格数据 (请基于此对比出题)\n" + "\n".join(tables)
    if key_params:
        struct_instruction += f"\n## 关键参数 (建议作为选项干扰项)\n" + ", ".join(key_params)

    router_focus_instruction = f"""
## 路由判定的考点优先级（必须优先遵守）⚠️
- 核心考点：{core_focus or '默认聚焦当前切片最核心规则'}
- 次要考点：{', '.join(secondary_focuses) if secondary_focuses else '无'}
- 再次要考点：{', '.join(minor_focuses) if minor_focuses else '无'}
- 路由建议题型：{rec_type}
- 题型理由：{question_type_reason or '未提供'}
{f"- 规则前提槽位：{', '.join(rule_precondition_profile.get('required_slots') or [])}" if rule_precondition_profile.get("enabled") else ""}
{f"- 主规则句：{focus_rule}" if focus_rule else ""}
{f"- 主任务：{focus_task}" if focus_task else ""}
{f"- 关键变量：{', '.join(focus_variables)}" if focus_variables else ""}

执行要求：
1. 优先围绕【核心考点】命题，题干、正确答案和解析主线都必须服务于核心考点。
2. 次要考点只允许作为背景条件、补充限制或干扰项来源，不得压过核心考点。
3. 再次要考点只能弱化出现，不能把多个并列要点混成一道单选题。
4. 若当前切片中有多个并列规则或多个正确项，必须优先选择最稳定、最适合当前题型的核心知识点来出题。
5. 若存在“规则前提槽位”，题干必须显式给出这些前提后再设问；缺少前提时优先改为规则判定题，不要硬出计算结果题。
6. 严禁退化为“年份/名称”纯记忆题；必须围绕主规则句完成可判定考核。
7. 单题测点必须收敛：主测点最多2个；不得在同一题里同时堆叠“主体资格+时间口径+地域口径+数值计算+例外边界”等多维考核。
"""
    precondition_template_block = build_precondition_template_block(rule_precondition_profile)

    # Question type control (strict)
    cfg_type = question_type
    if target_type == "判断题":
        type_instruction = (
            "题型要求：判断题。\n"
            "选项必须固定为：['正确','错误']。\n"
            "答案必须是 'A' 或 'B'。\n"
            "括号格式：题干末尾必须精确写成“（　）”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后不能再加句号。"
        )
    elif target_type == "多选题":
        type_instruction = (
            "题型要求：多选题。\n"
            "至少4个选项。\n"
            "答案必须是列表形式，如 ['A','C','D']。\n"
            "括号格式：题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。"
        )
    else:
        type_instruction = (
            "题型要求：单选题。\n"
            "4个选项且只有一个正确。\n"
            "答案必须是单个字母，如 'A'。\n"
            "括号格式：题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。"
        )

    mapped_type_hint = ""
    if cfg_type == "随机" and preferred_types:
        mapped_type_hint = f"\n# 随机题型优先规则\n当前切片关联母题题型优先集合：{preferred_types}。\n本题请按已选定题型【{target_type}】生成。"

    # Repair mode for reroute: inject critic feedback and previous question
    if state.get("retry_count", 0) > 0 and state.get("prev_final_json"):
        prev_question = state.get("prev_final_json")
        critic_reason = state.get("prev_critic_feedback") or state.get("prev_critic_details") or ""
        
        # Get constraints from config for repair mode
        question_type = config['configurable'].get('question_type')
        generation_mode = state.get("current_generation_mode") or config['configurable'].get('generation_mode', '随机')
        effective_generation_mode, normalized_generation_mode = resolve_effective_generation_mode(generation_mode, state)
        difficulty_range = config['configurable'].get('difficulty_range')
        if question_type == "随机":
            question_type = state.get("locked_question_type") or state.get("current_question_type") or rec_type
        
        # Build type instruction for repair
        if question_type == "判断题":
            type_instruction_repair = "题型要求：判断题。选项必须固定为：['正确','错误']。答案必须是 'A' 或 'B'。括号格式：题干末尾必须精确写成“（　）”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后不能再加句号。"
        elif question_type == "多选题":
            type_instruction_repair = "题型要求：多选题。至少4个选项。答案必须是列表形式，如 ['A','C','D']。括号格式：题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。"
        else:
            type_instruction_repair = "题型要求：单选题。4个选项且只有一个正确。答案必须是单个字母，如 'A'。括号格式：题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。"
        
        # Build mode instruction for repair
        mode_instruction_repair = build_mode_instruction_repair(effective_generation_mode, normalized_generation_mode)
        
        # Build difficulty instruction for repair
        difficulty_instruction_repair = ""
        if difficulty_range:
            min_diff, max_diff = difficulty_range
            difficulty_instruction_repair = f"难度要求：题目难度值必须在 {min_diff:.1f} 到 {max_diff:.1f} 之间。"
        
        # Build mastery instruction for repair (FR7.5)
        mastery_instruction_repair = ""
        mastery = kb_chunk.get('掌握程度', '未知')
        if mastery and mastery != "未知":
            mastery_instruction_repair = f"掌握程度要求：当前知识点的掌握程度要求为【{mastery}】。请根据掌握程度调整题目复杂度。"
        
        prompt = f"""
# 角色
你现在不是出题人，而是**逻辑修复工程师**。

# 当前问题
上一轮题目被驳回，请基于【参考材料】修复。

【上一轮题目】
{json.dumps(prev_question, ensure_ascii=False)}

【驳回原因】
{critic_reason}

# 必须遵守的约束
{type_instruction_repair}

{mode_instruction_repair}

{mastery_instruction_repair}

{difficulty_instruction_repair}
{term_lock_text}
{mapped_type_hint}
{router_focus_instruction}
{precondition_template_block}

# 人名规范（必须遵守）
1. **非必要不取名**：能不出现人名就不要出现。
2. **通俗姓名**：如需人名，使用常见姓氏+常见名的两字通俗姓名。
3. **负面事件**：涉及事故、违法违规等负面问题时，用“某某”指代（如张某）。但若题目需要判断行为是否合法/正确与否，则不适用“某某”规则。
4. **禁止恶搞**：姓名不得含恶搞或戏谑成分（如张漂亮、甄真钱、贾董事、张三、刘二等）。
5. **伦理合理**：姓名组合需符合日常伦理与常识（如父亲刘大伟、儿子刘二伟不可以；父亲张勇强、儿子张强勇不可以）。
6. **简洁易懂**：姓名尽可能简洁、通俗易懂，不使用生僻词。
7. **禁止小名**：不得使用小名/乳名（如小宝、贝贝）。
8. **禁止称谓**：不得使用“姓+女士/先生”，也不得使用“小李/小张”等称谓。

# 修复要求（三选一或都改）
1) **改题干/选项/答案**：使题目与知识片段一致，且能唯一推导正确答案。
2) **改解析**：如果题目与答案正确，只修正解析使其与答案一致。
3) **题目与解析都改**：如果两者都错，需同时修正题干/选项/答案与解析。

# 禁止
- 禁止解释出题过程
- 禁止辩解
- 禁止直接照搬原文案例中的具体人名、金额、日期、房产面积（必须做数据重构）
- 出题筛选条件必须严格执行：基础概念/理解记忆可非场景化；实战应用/推演必须场景化

# 参考材料
{generation_kb_context}

# 输出
返回 JSON：options 只填选项正文，不要写 A/B/C/D 或 A. B. 等序号。判断题: {{"question": "...", "options": ["正确", "错误"], "answer": "A 或 B", "explanation": "..."}}；单选题/多选题: {{"question": "...", "options": ["第一项正文", "第二项正文", "第三项正文", "第四项正文"], "answer": "A 或 A/B/C 等", "explanation": "..."}}
"""
        content, _, llm_record = call_llm(
            node_name="specialist.repair",
            prompt=prompt,
            model_name=specialist_model_to_use,
            api_key=API_KEY,
            base_url=BASE_URL,
            trace_id=state.get("trace_id"),
            question_id=state.get("question_id"),
        )
        llm_records.append(llm_record)
        try:
            draft = _ensure_draft_v1(parse_json_from_response(content))
            return {
                "draft": draft,
                "examples": examples,
                "current_generation_mode": effective_generation_mode,
                "current_question_type": question_type,
                "locked_question_type": question_type,
                "llm_trace": llm_records,
                "logs": [f"🛠️ {agent_name}: 已进入修复模式（模型={specialist_model_to_use}，原因={model_reason}）"]
            }
        except Exception as e:
            return {"llm_trace": llm_records, "logs": [f"❌ {agent_name} 修复模式失败: {str(e)}"]}

    # Build difficulty instruction
    difficulty_instruction = ""
    if difficulty_range:
        min_diff, max_diff = difficulty_range
        difficulty_instruction = f"""
# 难度要求（必须严格遵守）⚠️
**题目难度值必须在 {min_diff:.1f} 到 {max_diff:.1f} 之间**。
并且必须用数值填写难度字段（禁止“易/中/难”文本标签）。

难度控制方法：
- **简单题 (0.3-0.5)**：直接考察知识点定义、基础概念，干扰项仍需同维度且贴近常见误判
- **中等题 (0.5-0.7)**：需要理解知识点含义并应用到场景，干扰项似是而非，需要仔细分析
- **困难题 (0.7-0.9)**：需要综合多个知识点、复杂计算或多步推理，干扰项高度相似

**重要**：生成的题目难度值必须落在指定范围内，否则会被拒绝。请根据难度要求调整：
- 题干复杂度（简单题用直接表述，困难题用复杂场景）
- 干扰项相似度（简单题也应“看起来合理但错误”，困难题可进一步提高相似度）
- 所需推理步骤（简单题直接答案，困难题需要多步推理）
"""
    
    # Build mastery instruction (FR7.5)
    mastery_instruction = ""
    if mastery and mastery != "未知":
        mastery_instruction = f"""
# 掌握程度要求（影响题目复杂度设计）⚠️
当前知识点的掌握程度要求为: 【{mastery}】。

请根据掌握程度调整题目复杂度：
- **了解**：考察基础概念和基本定义，题目相对简单直接
- **熟悉**：需要理解知识点含义并应用到场景，题目难度适中
- **掌握**：需要深入理解并能综合运用，题目可以更复杂，需要多步推理

**注意**：掌握程度要求应与难度范围配合使用，共同控制题目复杂度。
"""
    
    # Call LLM
    prompt = f"""
# 角色
你是 {agent_name}。
请严格基于【参考材料】创作一道高质量的房地产经纪人考试题。

# 好题标准（必须遵守）
## 好情境（用什么材料考）
1. **聚焦考点**：围绕教材切片核心知识点命题；是否使用业务场景由筛选条件决定。
2. **真诚说人话**：情境描述要通俗易懂，避免生僻词和专业黑话，使用自然的日常表达。
3. **简洁不啰嗦**：情境表述要简洁清晰，避免冗余信息，突出核心要点。

## 好方法（用什么方法）
1. **直接不拐弯**：考点直接，不设置复杂陷阱，让学员能清晰理解要考察的知识点。
2. **按筛选条件决定场景化**：基础概念/理解记忆可直接考知识点；实战应用/推演必须使用业务场景案例。
3. **数据重构**：严禁直接照搬原文案例中的具体人名、金额、日期、房产面积。

# 题型要求（必须遵守）
{type_instruction}

{mode_instructions}

{mastery_instruction}

{difficulty_instruction}
{term_lock_text}
{CALC_PARAMETER_GROUNDING_GUIDE}
{router_focus_instruction}
{precondition_template_block}

# 适纲性 / 对工作有帮助 / 导向性（必须满足）
1. **适纲性**：命题内容必须来自当前知识切片或本教材切片，不得超纲出题；超纲题属于错题。
2. **对经纪人工作有帮助**：题目应对经纪人工作有正向作用（可为实操判断/流程/风险，也可为理解规则、合规要点、公司文化等）。允许出对工作有指导意义的记忆题，尤其是公司制度、合规红线、禁止性规定、时效阈值、标准口径、企业文化与价值观口径等需要记忆执行的知识点；仅禁止对工作无帮助的死记硬背题（如脱离业务语义的孤立数量/年代、仅考概念归类或教材措辞“核心/主要”）。
3. **导向性**：试题应有引导和启发作用，帮助经纪人理解公司文化、熟悉新业务、热爱行业。

# 聚焦核心业务，避开特殊考点（必须遵守）⚠️
1. **避免歧义考点**：题目答案必须唯一明确，不能有争议或模糊空间。
   - ❌ 错误示例：问"房价上涨主要体现了房地产的哪个特性"，答案可能是"保值增值"也可能是"相互影响"。
   - ✅ 正确做法：题干提供的条件必须能唯一确定答案，不能让考生在两个看似都对的答案中纠结。
2. **避免偏辟考点**：不考察过于细节、不常用的知识点。
   - ❌ 错误示例：家装产品的详细报价规格（如B3产品每增加1㎡增加999元）。
   - ✅ 正确做法：聚焦经纪人日常高频业务场景（如房源核验、客户接待、合同签订、税费计算等）。
3. **避免无关考点**：不考察与房地产经纪业务无关的内容。
   - ❌ 错误示例：监护权判定、植物人法律问题等民法细节。
   - ✅ 正确做法：只考察与房地产经纪、交易、服务直接相关的知识点。
4. **避免模糊考点（必须严格执行）**：
   - ❌ 禁止考察无明确对错的内容：
     * 带看的顺序、面谈的内容、空看的时间等流程细节
     * 经纪人在拍摄实勘时"与业主充分沟通、树立专业形象是否正确"（过于主观）
   - ❌ 禁止考察教材与实际不符的内容：
     * 教材要求备件但实际业务中不需要的
     * 政策规定与实际操作脱节的内容
5. **题目要有考察意义（必须严格执行）**：
   - ❌ 禁止考察过于简单或无意义的判断：
     * "经纪人做得好是否正确"（废话题）
     * "客户想买某区房，经纪人无需推荐新房项目"（过于绝对，无意义）
     * "老客户找经纪人A，值班经纪人B可以说A离职并私自接待"（明显错误，无考察价值）
     * "物业交割时经纪人不需要准备，只需提醒签字"（明显错误，无考察价值）
   - ✅ 正确做法：考察有实际业务意义的知识点，能帮助经纪人解决实际问题或避免实际错误。

# 简化场景，符合实际（必须遵守）⚠️
1. **无意义的场景铺垫不要**：
   - ❌ 错误示例："师傅告诉徐薇：经纪人在培训时了解到..."、"经纪人刘铭在新人训时学习了..."
   - ✅ 正确做法：直接陈述事实，去掉"某某告诉某某"、"在培训时了解到"等冗余铺垫。
2. **和题目无关联的句子不要**：
   - ❌ 错误示例："客户张美通过经纪人邱好购买了一套毛坯二手房。因张美工作比较繁忙无暇装修..."（"通过经纪人邱好购买"与题目考点无关）
   - ✅ 正确做法：只保留与解题相关的关键信息，去掉对答案没有影响的背景描述。
   - 避免「新人培训」「通过中介买了房」等冗余场景套话。
3. **题干较长时重点注意**：剔除与本题**毫无关系**的表达，不要让题干变得没必要的复杂、逻辑没必要的绕；只保留与解题/考点直接相关的信息。
4. **太长的句子不要**：
   - ❌ 错误示例："2023年5月5日，经纪人刘卓在门店接受了业主刘伟对其名下一套住宅的出售委托。在交流过程中得知刘伟着急出售该住宅。"
   - ✅ 正确做法："业主刘伟委托出售一套房源，经纪人刘卓得知其着急出售。"（简化表述，突出核心条件）
5. **简化数字，方便计算（必须遵守）**：
   - ❌ 错误示例：总户数328户，车位100个，车位配比1:3.28（复杂小数）
   - ✅ 正确做法：总户数400户，车位100个，车位配比1:4（整数，易于口算）
   - **原则**：数字尽量使用整数或简单小数（如0.5、1.5），避免使用1.328、2.876等复杂小数。
6. **非必要不起名（必须遵守）**：
   - ❌ 错误示例："客户杨帆，欲通过经纪人黄燕购买一套金碧花园的住宅..."（"欲通过经纪人黄燕购买"冗余）
   - ✅ 正确做法："客户杨帆因出差外地，无法到场签约，在获得其授权后，经纪人可以在房屋买卖合同上代其签字。"
   - **原则**：如果经纪人的名字对题目考点无关，就不要提及；只保留必要的角色（如客户）。

# 人名规范（必须遵守）
1. **非必要不取名**：能不出现人名就不要出现。
2. **通俗姓名**：如需人名，使用常见姓氏+常见名的两字通俗姓名。
3. **负面事件**：涉及事故、违法违规等负面问题时，用“某某”指代（如张某）。但若题目需要判断行为是否合法/正确与否，则不适用“某某”规则。
4. **禁止恶搞**：姓名不得含恶搞或戏谑成分（如张漂亮、甄真钱、贾董事、张三、刘二等）。
5. **伦理合理**：姓名组合需符合日常伦理与常识（如父亲刘大伟、儿子刘二伟不可以；父亲张勇强、儿子张强勇不可以）。
6. **简洁易懂**：姓名尽可能简洁、通俗易懂，不使用生僻词。
7. **禁止小名**：不得使用小名/乳名（如小宝、贝贝）。
8. **禁止称谓**：不得使用“姓+女士/先生”，也不得使用“小李/小张”等称谓。

# 题干/设问规范（必须遵守）
1. **题干括号位置**：
   - 题干中的括号不能在句首，可放在句中或句末。
   - 选择题题干句末要有句号，句号在最后；判断题题干句子完结后加一个括号，括号在最后。
2. **括号格式**：
   - 使用中文括号，括号内部有且仅有一个全角空格（不能多）：`（　）`
   - 括号前后不允许空格
3. **设问表达**：
   - 设问须用陈述句，禁止使用问号（？）；不得以疑问句形式设问。
   - 少用否定句，禁止使用双重否定句；禁止“不是不”“并非不”等易歧义表述。
   - **遣词造句与指代一致**：题干注意主谓搭配与指代一致，避免指代对象错误导致语义偏差。
   - **前提（必须遵守）**：题干必须是**肯定陈述句**，不得写成疑问句，不得依赖“是否正确/对不对/是不是”这类问法。
   - **判断题要求**：判断题只要求语义是肯定陈述句，并且题干中能明确出现“正确”或“错误”这一判断锚点；不要强制固定某一种模板句式。
   - **选择题设问表述**：选择题同样只要求题干是陈述句、以（　）作答占位结尾（句号在括号后），不强制固定使用某一类“以下表述正确的是/有/包括”模板。
4. **标准用语**：禁止使用“外接”“上交”等易与规范用语混淆的表述，应使用“买方/受让方”“缴纳”等标准用语。

# 选项规范（必须遵守）
0. **选项输出格式（严禁违反，否则会出现 A. A 网签… 双重序号）**：
   - **options 数组中只填选项正文**，禁止在每项前写 A./B./C./D. 或 A、B、等序号；系统会按 A/B/C/D 自动显示，写序号会导致展示时出现双重序号。
   - 正确示例（单选题）：options 填四句正文，如 ["网签合同信息一旦录入系统便无法修改，可能导致过户失败", "线上过户无法调取网签合同，可能影响客户提取公积金", ...]；判断题：["正确", "错误"]。
   - 错误示例：不要写 ["A. 网签...", "B. 线上..."] 或 ["A", "B", "C", "D"]，否则展示会变成 A. A 网签… 双重序号。
1. **选项数量与正确性**：
   - 选择题每题4个选项；单选题仅1个正确，多选题≥2个正确。
   - 多选题中正确选项数量要合理，不要多道题都只有1个答案。
2. **标点与语义**：
   - 每个选项末尾不添加标点符号。
   - 选项必须与题干组成完整语句（将选项代入题干括号处语义完整）。
   - **选项单位**：选项中有单位时，**必须**将单位提到题干中，**不得**在选项中反复出现单位。选项不得包含数值单位（如元、万元、平方米、年、%等）；单位应写在题干设问处（如「……额度为（　）万元」则选项只写 6、8、10、12）。
3. **一致性与干扰项**：
   - 选项中的姓名与题干中的姓名保持一致，不出现题目未涉及的姓名。
   - 干扰项必须具有干扰性，选项本身应是存在或相关的内容，不能无意义。
   - **禁止明显常识性错误/极端值**，干扰项要“看起来可能对但实际上不对”。
   - **禁止明显常识性错误/极端值**（如与材料明显不符、过低/过高层数等），干扰项要“看起来可能对但实际上不对”。
4. **数值型选项**：
   - 选项为数字时按从小到大顺序排列。
   - 计算题尽量简单（能口算优先），确需保留小数时注明保留位数（一般1-2位）。
   - 非计算题若解题过程涉及运算（如比例、折算、阈值比较），同样执行“简算优先”：避免复杂小数与冗长多步计算，不应依赖计算器；若必须保留小数，题干须明确“保留到X位小数”（一般1-2位）。
   - 未被选中的数值选项也必须有计算依据，不可胡编乱造。

# 输出解析格式（试题解析三段论，必须遵守）
解析须带段首序号 1、2、3、，三段分别对应：教材原文、试题分析、结论。
1. **教材原文**：路由前三个标题（即目标题内容，不要写「目标题：」字样）+ 分级（掌握/了解/熟悉）+ 教材原文要点；可只复制主题句/关键句，须保持完整；不可复制表格/图片（可改文字）。总字数尽量≤400字。
2. **试题分析**：必须用自己的话清晰解释每个选项与答案，不可直接粘贴教材原文；多选题须解释所有正确选项及每个错误选项。
3. **结论**：判断题写【本题答案为正确/错误】，禁止写【本题答案为A/B】；选择题写【本题答案为A/B/C/D/AB/AC...】。
4. **严禁**：直接粘贴教材原文表格或图片；试题分析段不得整段粘贴教材原文。
5. **一致性**：答案与解析必须一致，计算题须与计算过程一致。
**正确示例**：
- 1、教材原文：常见的身份证明(掌握) 普通居民: 第二代身份证; 军人(武警): 军(警)官证、军(警)身份证、身份证与军(警)官证一致证明; 香港/澳门居民: 港澳居民来往内地通行证、港澳居民身份证; 台湾居民: 台湾居民来往大陆通行证; 外国居民: 护照。
- 2、试题分析：选项ACD都是正确的身份证明；选项B，香港/澳门居民不可以提供护照，故错误。
- 3、结论：本题答案为ACD。
6. **典型错题规避**：
   - 题干/选项/解析出现多字、少字、错字，影响作答。
   - 题干与选项/解析前后不一致。
   - 计算题无正确答案或答案与计算过程不一致。
   - 题目超纲或概念过时（如旧业务名/过期协议）。
   - 场景严重脱离经纪业务实际。
   - 干扰选项存在争议或与正确答案同样成立。

# 质量标准 (必须达成):
1. **逻辑忠实与数据重构 (40%)**:
   - **核心逻辑**：必须严格遵循原文的判定规则（如时间点、税率、认定标准）。
   - **数据重构（反抄袭）**：严禁直接照搬原文案例中的具体人名、金额、日期、房产面积。
     - ❌ 错误：原文是"2010年张三买房"，题目也写"2010年张三买房"。
     - ✅ 正确：将"张三"改为"李女士"，将"2010年"改为"2011年"（前提是仍在规则适用的同一时间段内），将"180万"改为"200万"。
2. **干扰项质量 (25%)**: 错误选项必须似是而非，利用常见误区，不要一眼假。除非必要，避免使用"以上皆是"。
   - **干扰项设计技巧**：利用**"相近的数字"**（如正确答案是3年，干扰项用2年或4年）或**"错误的参照物"**（如混淆不同概念、用类似但不正确的表述）
{uniqueness_note}
   - **避免“最XX”考法**：禁止用“最重要/最关键/重点/主要”等表述设计题干或选项，重点考察完整流程、条件、责任边界或操作要点。
3. **唯一答案强制校验 (One Truth Rule)**：
   - 逐条假设每个错误选项为真，验证在当前题干条件下是否“必错”。
   - 如果某个干扰项只是“题干没提到”而非逻辑必错，必须补充题干条件把它排除。
   - 若答案是 A，但 B 也是必需材料/条件，则必须在题干中明确写出“已提供 B”，避免双答案。
4. **相关性 (15%)**: 考察核心概念在经纪业务/合规/客户服务中的应用，避免纯背诵性琐碎记忆。
5. **格式 (10%)**: 严格的 JSON 输出。
{struct_instruction}
6. **筛选条件强约束（必须执行）**:
   - 若筛选条件为【基础概念/理解记忆】：可直接考察定义、条件、规则，不强制业务场景。
   - 若筛选条件为【实战应用/推演】：题干必须描述具体业务场景，并体现推演过程。
   - ❌ 禁止无效题：题干问“A是什么”，选项说“A是A”。
# 参考材料
{generation_kb_context}

# 范例参考
{examples_text}

# 题干一致性自检（必须执行）
1. 基于【当前切片 + 上一级切片全集 + 相似切片】检查题干与解析是否存在冲突或不一致。
2. 若发现不一致，必须输出“问题清单”，说明冲突维度、冲突点、修复建议。

# 任务
返回 JSON（options 只填选项正文，不要写 A/B/C/D 或 A. B. 等序号）:
- 判断题: {{"question": "...", "options": ["正确", "错误"], "answer": "A 或 B", "explanation": "...", "self_check_issues": [...]}}
- 单选题/多选题: {{"question": "...", "options": ["第一项正文内容", "第二项正文内容", "第三项正文内容", "第四项正文内容"], "answer": "A 或 A/B/C 等", "explanation": "...", "self_check_issues": [...]}}
约束: 题干中**禁止**出现"根据材料"、"依据参考资料"等字眼。题目必须是独立的。

# 题目质量硬性约束（违反会被 Critic 驳回）⚠️
## 1. 禁止使用模糊的日常用语：题干中**禁止**使用"实实在在的特点"、"重要的信息"、"关键因素"等模糊表述，这类词在汉语中可能指向多个维度，会导致歧义。应使用明确、可操作的表述。
## 2. 选项维度一致性：所有选项必须在同一维度内做区分（如考实物信息则选项都是户型/面积/朝向/装修等）；**禁止**跨维度（如A法律、B实物、C位置、D价格），否则无法真正考察专业知识。干扰项应与正确答案同维度但略有不同。
## 3. 对经纪人工作有帮助：题目须对经纪人工作有正向作用（实操题、规则理解、合规、文化等均可）。公司制度/合规红线/禁止性规定/时效阈值/标准口径/企业文化与价值观口径等“要求背诵并执行”的知识点允许直接命题，不因“偏记忆”被否决。**禁止**：（1）仅考「定义 vs 目的 vs 方式」等概念归类、对工作无帮助的题；（2）仅考「教材把哪一条称为核心/主要/关键」的刁钻题（实务上多选项都重要、选对只靠记教材措辞）；（3）**常识与切片表述易冲突的题**（如常人理解“新建”=未交易过、而教材有专门口径，易导致按常识选错或觉得没写清楚）——此类题不出，或须在题干/解析中明确教材口径与日常用语区别。（4）**流程/步骤类主体或视角歧义**：若切片流程未明确每一步的执行主体或视角（谁来做、从谁的角度），则不出因主体/视角不同会产生歧义的题或选项（如“最后一步”在流程顺序 vs 当事人操作角度可能不同）。（5）**选项与题干条件相悖**：题干已设定某事实成立时，选项中不得出现与该事实在逻辑上矛盾的表述。（6）**规则要素缺失或绝对化**：教材规则中的触发条件、适用范围、约束主体、作用对象、角色边界、时间/流程时点等要素不得缺失或被改写为无条件绝对命题。

# 自检清单（必须逐条核对）
1. **题干与选项逻辑一致**：任一选项不得与题干中已明确给出的条件、前提或设定相悖（题干已设定某事实成立时，选项不得出现与该事实矛盾的表述）。
2. **规则要素完整**：若教材规则包含触发条件、适用范围、约束主体、作用对象、角色边界、时间/流程时点，题干与正确项不得遗漏或偷换这些要素。
3. **正确项完整覆盖考点**：若切片对考点明确了多个并列要点，正确选项须覆盖这些关键要素，不得遗漏。
4. **唯一答案**：题干条件足以排除其他选项，不能出现两条合理路径。
5. **解析规范（试题解析三段论）**：1、教材原文（路由前三个标题即目标题内容+分级+原文≤400字，不要写「目标题：」字样）2、试题分析（用自己的话解释各选项，多选覆盖全部）3、结论（判断题写本题答案为正确/错误，选择题写本题答案为A/B/C/D/AB/AC...）。
6. **一致性**：题干/选项/答案/解析前后一致，计算题与计算过程一致。
7. **适纲性**：不超纲，不引入材料外条件或结论。
8. **人名与措辞**：人名规范、无生造词、无模糊词。
9. **维度一致**：选项同维度，干扰项有理有据。
10. **干扰项质量**：避免“明显错误/常识级错误/极端值”，干扰项应合理但错误。
11. **禁用兜底选项**：选项不得出现「以上都对」「以上都错」「以上选项全对/全错」「皆是」「皆非」等表述；若命中须改写为同维度干扰项，保持考点不变。
12. **长度限制**：题干不超过400字、单选项不超过200字；解析仅要求“教材原文”段尽量≤400字，整段解析不设硬性上限。超长时仅删减非核心句，并剔除与解题无关的表述，保持考点。
13. **隐含计算复杂度**：即使题型为非计算题，只要作答依赖运算，也必须做到“口算/简单笔算可完成”；若需复杂小数或明显依赖计算器，应重构数字与设问。
"""
    content, _, llm_record = call_llm(
        node_name="specialist.draft",
        prompt=prompt,
        model_name=specialist_model_to_use,
        api_key=API_KEY,
        base_url=BASE_URL,
        trace_id=state.get("trace_id"),
        question_id=state.get("question_id"),
    )
    llm_records.append(llm_record)
    
    try:
        # Log raw content for debugging
        print(f"DEBUG RAW CONTENT: {content}")
        
        parsed = parse_json_from_response(content)
        draft = _ensure_draft_v1(parsed if isinstance(parsed, dict) else {})
        self_check_issues = parsed.get("self_check_issues") if isinstance(parsed, dict) else None
        if not isinstance(self_check_issues, list):
            self_check_issues = []
        planner_logs: List[str] = []
        if cfg_type == "随机":
            planner_logs.append(f"🎲 随机题型：本题已选定【{target_type}】")
        planner_logs.append(f"🧠 Specialist模型={specialist_model_to_use}（原因={model_reason}）")
        planner_logs.append(f"👨‍💻 {agent_name}: 初稿已生成（题型={target_type}，筛选条件={effective_generation_mode}）")
        return {
            "draft": draft,
            "examples": examples,  # Pass examples to UI
            "self_check_issues": self_check_issues,
            "current_generation_mode": effective_generation_mode,
            "current_question_type": target_type,  # So repair/critic use same type when 随机
            "locked_question_type": target_type,
            "llm_trace": llm_records,
            "logs": planner_logs,
        }
    except Exception as e:
        return {"llm_trace": llm_records, "logs": [f"❌ {agent_name} 错误: {str(e)}"]}


def writer_node(state: AgentState, config):
    draft = state.get('draft')
    llm_records: List[Dict[str, Any]] = []
    # If draft is missing (e.g. previous step failed), skip writer
    if not draft:
        return {"llm_trace": llm_records, "logs": ["❌ 作家: 未收到有效初稿，跳过润色。"]}

    kb_chunk = state['kb_chunk']
    term_locks = state.get("term_locks") or []
    # Get examples for reference
    examples = state.get('examples', [])
    retriever = config['configurable'].get('retriever') or get_default_retriever()
    kb_context, parent_slices, related_slices = build_extended_kb_context(kb_chunk, retriever, examples)
    self_check_issues = state.get("self_check_issues") or []
    if not isinstance(self_check_issues, list):
        self_check_issues = []
    self_check_text = ""
    if self_check_issues:
        self_check_text = f"""
# 出题节点自检问题清单（必须逐条修复）
{json.dumps(self_check_issues, ensure_ascii=False)}
"""
    term_lock_text = ""
    if term_locks:
        term_lock_text = f"""
# 专有名词锁词约束（必须执行）
以下术语来自知识切片，若在题干/选项/解析中使用，必须保持**原词**，不得同义替换、缩写替换或解释性改写：
{json.dumps(term_locks, ensure_ascii=False)}
"""
    
    # Get difficulty range from config
    difficulty_range = config['configurable'].get('difficulty_range')
    
    # ✅ Question type modification strategy based on user settings
    # Get Router's recommended type (fallback)
    router_details = state.get('router_details', {})
    rec_type = router_details.get('recommended_type', '单选题')
    core_focus = str(router_details.get('core_focus', '') or '').strip()
    secondary_focuses = [str(x).strip() for x in (router_details.get('secondary_focuses') or []) if str(x).strip()]
    minor_focuses = [str(x).strip() for x in (router_details.get('minor_focuses') or []) if str(x).strip()]
    question_type_reason = str(router_details.get('question_type_reason', '') or '').strip()
    rule_precondition_profile = router_details.get("rule_precondition_profile") or {}
    focus_contract = state.get("locked_focus_contract") or router_details.get("focus_contract") or {}
    focus_rule = str(focus_contract.get("focus_rule", "") or core_focus).strip()
    focus_task = str(focus_contract.get("focus_task", "") or "").strip()
    focus_variables = [str(x).strip() for x in (focus_contract.get("focus_variables") or []) if str(x).strip()]
    
    # Get configured question type
    cfg_type = config['configurable'].get('question_type', '自动')
    locked_question_type = state.get("locked_question_type")
    
    draft_type = _infer_draft_type_for_writer(draft) if isinstance(draft, dict) else None
    target_type = _resolve_writer_target_type(draft if isinstance(draft, dict) else None, cfg_type, rec_type)
    if locked_question_type in ["单选题", "多选题", "判断题"] and target_type != locked_question_type:
        print(f"⚠️ Writer 题型锁定生效: [{target_type}] -> [{locked_question_type}]")
        target_type = str(locked_question_type)
    high_risk_profile = router_details.get("high_risk_profile") or {}
    if high_risk_profile.get("prohibit_single_choice") and target_type == "单选题":
        target_type = "多选题"
    # In 随机 mode, planner already chose the question type for this run; use it so critic gets the same type.
    # Otherwise writer may overwrite state with draft-inferred type (e.g. 单选题 when draft answer is single letter) and critic sees wrong type.
    if (
        locked_question_type not in ["单选题", "多选题", "判断题"]
        and cfg_type == "随机"
        and state.get("current_question_type") in ["单选题", "多选题", "判断题"]
    ):
        target_type = state.get("current_question_type")
    if cfg_type == "随机":
        print(f"📌 随机模式：保持专家节点生成的题型 [{target_type}]")
    elif cfg_type in ["单选题", "多选题", "判断题"] and draft_type and draft_type != cfg_type:
        print(f"📌 指定题型模式：强制修改题型 [{draft_type}] → [{cfg_type}]")

    # Whether current question is from calculator (for hard_rules is_calculation)
    is_calculation = state.get("code_status") in ("success", "success_no_result") or bool(state.get("generated_code"))
    expected_calc_target = str(state.get("calc_target_signature", "") or "").strip()
    expected_calc_unit = str(state.get("calc_unit_hint", "") or "").strip()

    pre_writer_logs: List[str] = []
    draft_for_prompt = draft
    pre_hard_issues: List[str] = []
    if isinstance(draft, dict):
        pre_question_ir = _writer_normalize_phase(draft, target_type)
        pre_report = _writer_validate_phase(
            pre_question_ir,
            target_type,
            term_locks=term_locks,
            kb_context=kb_context,
            focus_contract=focus_contract,
            is_calculation=is_calculation,
            expected_calc_target=expected_calc_target,
            expected_calc_unit=expected_calc_unit,
        )
        draft_for_prompt = dict(pre_question_ir)
        try:
            _q, _o, _e, media_changed = sanitize_media_payload(
                draft.get("question", ""),
                draft.get("options", []),
                draft.get("explanation", ""),
            )
        except Exception:
            media_changed = False
        pre_hard_issues = []
        for i in (pre_report.get("issues") or []):
            msg = str(i.get("message", "") or "").strip()
            if not msg:
                continue
            fix = i.get("suggested_fix") or i.get("fix_hint")
            pre_hard_issues.append(f"{msg}；修复建议：{fix}" if fix else msg)
        # Judge 4.6: option content must not start with A./B./C./D. (check raw draft before normalize)
        prefix_issues = _detect_option_prefix_in_draft(draft)
        pre_hard_issues.extend(prefix_issues)
        if pre_hard_issues:
            pre_writer_logs.append(
                f"⚠️ 作家: 预清洗后仍有硬约束风险（将优先修复）: {', '.join(pre_hard_issues)}"
            )
        else:
            pre_writer_logs.append("⚠️ 作家: 已在润色前完成一次格式硬预清洗")
        if media_changed:
            pre_writer_logs.append("⚠️ 作家: 已执行图片/表格最小修复")

    # ------------------------------------------------------------------
    # 1. 动态构建 Prompt (Type-Aware)
    # ------------------------------------------------------------------
    type_specific_instruction = ""
    if target_type == "判断题":
        type_specific_instruction = """
- **题型要求**: 判断题。
- **选项设置**: 必须固定为两个选项：["正确", "错误"]。
- **答案格式**: 必须是 "A" (代表正确) 或 "B" (代表错误)。
- **括号格式**: 题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。
"""
    elif target_type == "多选题":
        type_specific_instruction = """
- **题型要求**: 多项选择题。
- **选项设置**: 至少 4 个选项，干扰项要具有迷惑性。
- **答案格式**: 必须包含所有正确选项的列表，例如 ["A", "C", "D"]。
- **逻辑**: 确保有 2 个或以上的选项是正确的。
- **括号格式**: 题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。
"""
    else: # 单选题
        type_specific_instruction = """
- **题型要求**: 单项选择题。
- **选项设置**: 4 个选项，只有一个正确。
- **答案格式**: 必须是单个字母，例如 "A"。
- **括号格式**: 题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。
"""

    router_focus_text = f"""
# 路由优先级（润色时必须服从）
- 核心考点：{core_focus or '默认聚焦当前切片最核心规则'}
- 次要考点：{', '.join(secondary_focuses) if secondary_focuses else '无'}
- 再次要考点：{', '.join(minor_focuses) if minor_focuses else '无'}
- 路由建议题型：{rec_type}
- 题型理由：{question_type_reason or '未提供'}
{f"- 规则前提槽位：{', '.join(rule_precondition_profile.get('required_slots') or [])}" if rule_precondition_profile.get("enabled") else ""}
{f"- 主规则句：{focus_rule}" if focus_rule else ""}
{f"- 主任务：{focus_task}" if focus_task else ""}
{f"- 关键变量：{', '.join(focus_variables)}" if focus_variables else ""}

润色要求：
1. 优先保证题干、答案、解析都围绕【核心考点】闭环。
2. 若草稿把次要考点写成了主考点，必须收敛回核心考点。
3. 若草稿在单选题里混入多个并列正确项，优先通过收缩到核心考点消除歧义。
4. 若存在“规则前提槽位”，必须在题干中显式写明，不得在解析里补前提。
5. 题干不得退化为“年份/名称”纯记忆，必须匹配主任务与关键变量。
6. 题干不得测点过载：主测点最多2个；若草稿同时考核资格/时间/地域/计算/边界等多维，请删减为聚焦版题干。
"""
    precondition_template_block_writer = build_precondition_template_block(rule_precondition_profile)

    # Build examples reference text
    examples_text = ""
    if examples:
        examples_text = "\n# 参考母题（仅用于参考出题风格，严禁照搬数据）\n"
        for i, ex in enumerate(examples[:3], 1):
            examples_text += f"母题 {i}: {ex.get('题干', '')}\n"
    
    # Build difficulty instruction for writer
    difficulty_instruction_writer = ""
    if difficulty_range:
        min_diff, max_diff = difficulty_range
        difficulty_instruction_writer = f"""
# 难度要求（必须严格遵守）⚠️
**题目难度值必须在 {min_diff:.1f} 到 {max_diff:.1f} 之间**。
并且必须用数值填写难度字段（禁止“易/中/难”文本标签）。

请根据难度范围设置 difficulty 字段：
- 必须输出数值（float），例如 0.34、0.58、0.76。
- 禁止输出“易/中/难”等中文难度标签。
- 输出值必须落在区间 [{min_diff:.1f}, {max_diff:.1f}] 内。

**重要**：必须确保生成的难度值在指定范围内！
"""

    model_to_use, writer_model_reason = _resolve_specialist_writer_model(
        state,
        WRITER_MODEL or MODEL_NAME,
    )
    # 允许对问题清单触发一次整体改写（不直接替换为“某某”）
    extra_self_check_issues = list(self_check_issues)
    if pre_hard_issues:
        extra_self_check_issues.extend([f"格式或规则残留: {x}" for x in pre_hard_issues])
    last_exception = None
    final_dict = None
    writer_logs = list(pre_writer_logs)
    writer_logs.append(f"🖋️ Writer模型={model_to_use}（原因={writer_model_reason}）")
    final_report: ValidationReport = {"passed": False, "issues": [], "summary": "未执行"}
    writer_retry_exhausted = False
    for attempt in range(2):
        self_check_text = ""
        if extra_self_check_issues:
            self_check_text = f"""
# 出题节点自检问题清单（必须逐条修复）
{json.dumps(extra_self_check_issues, ensure_ascii=False)}
"""
        issue_messages_for_prompt = [str(x) for x in extra_self_check_issues if str(x).strip()]
        if not issue_messages_for_prompt:
            issue_messages_for_prompt = pre_hard_issues[:]
        issue_only_prompt = _build_writer_polish_prompt_issue_only(
            target_type=target_type,
            draft_for_prompt=draft_for_prompt if isinstance(draft_for_prompt, dict) else {},
            kb_context=kb_context,
            examples_text=examples_text,
            term_lock_text=term_lock_text,
            router_focus_text=router_focus_text,
            difficulty_instruction_writer=difficulty_instruction_writer,
            self_check_text=self_check_text,
            issue_messages=issue_messages_for_prompt,
        )
        legacy_prompt = f"""
# 任务
你是最终编辑。请将以下初稿转化为严格的 JSON 格式。

# 好题标准（必须遵守）
## 好设问（问什么问题）
1. **直接不拐弯**：设问要直接明确，避免绕弯子和双重否定。
2. **简洁不啰嗦**：设问表述要简洁清晰，用陈述方式而非疑问句。
3. **真诚说人话**：设问用词要通俗易懂，符合日常表达习惯。

## 好选项（如何设置选项）
1. **维度一致**：所有选项必须在同一维度内做区分，禁止跨维度设置（如A法律、B实物、C位置、D价格）。
2. **干扰有据**：干扰项必须似是而非，有理有据，利用常见误区，不能一眼假。
3. **简洁清晰**：选项表述要简洁，避免冗余，末尾不添加标点符号。

## 四大核心要求
1. **聚焦贴业务**：实用常见，贴近经纪人实际工作。
2. **直接不拐弯**：考点直接，不绕弯子。
3. **简洁不啰嗦**：表述简洁，突出要点。
4. **真诚说人话**：通俗易懂，自然表达。

# 目标题型: 【{target_type}】
{type_specific_instruction}

{difficulty_instruction_writer}
{term_lock_text}
{router_focus_text}
{precondition_template_block_writer}

# 简化场景，符合实际（必须遵守）⚠️
1. **无意义的场景铺垫不要**：
   - ❌ 错误示例："师傅告诉徐薇：经纪人在培训时了解到..."、"经纪人刘铭在新人训时学习了..."
   - ✅ 正确做法：直接陈述事实，去掉"某某告诉某某"、"在培训时了解到"等冗余铺垫。
2. **和题目无关联的句子不要**：
   - ❌ 错误示例:"客户张美通过经纪人邱好购买了一套毛坯二手房。因张美工作比较繁忙无暇装修..."（"通过经纪人邱好购买"与题目考点无关）
   - ✅ 正确做法：只保留与解题相关的关键信息，去掉对答案没有影响的背景描述。
3. **题干较长时重点注意**：剔除与本题**毫无关系**的表达，不要让题干变得没必要的复杂、逻辑没必要的绕；只保留与解题/考点直接相关的信息。
4. **太长的句子不要**：
   - ❌ 错误示例："2023年5月5日，经纪人刘卓在门店接受了业主刘伟对其名下一套住宅的出售委托。在交流过程中得知刘伟着急出售该住宅。"
   - ✅ 正确做法："业主刘伟委托出售一套房源，经纪人刘卓得知其着急出售。"（简化表述，突出核心条件）
5. **简化数字，方便计算**：
   - ❌ 错误示例：车位配比1:3.28（复杂小数）
   - ✅ 正确做法：车位配比1:4（整数，易于口算）
6. **非必要不起名**：
   - ❌ 错误示例："欲通过经纪人黄燕购买..."
   - ✅ 正确做法：经纪人名字对考点无关时不要提及

# 人名规范（必须遵守）
1. **非必要不取名**：能不出现人名就不要出现。
2. **通俗姓名**：如需人名，使用常见姓氏+常见名的两字通俗姓名。
3. **负面事件**：涉及事故、违法违规等负面问题时，用“某某”指代（如张某）。但若题目需要判断行为是否合法/正确与否，则不适用“某某”规则。
4. **禁止恶搞**：姓名不得含恶搞或戏谑成分（如张漂亮、甄真钱、贾董事、张三、刘二等）。
5. **伦理合理**：姓名组合需符合日常伦理与常识（如父亲刘大伟、儿子刘二伟不可以；父亲张勇强、儿子张强勇不可以）。
6. **简洁易懂**：姓名尽可能简洁、通俗易懂，不使用生僻词。
7. **禁止小名**：不得使用小名/乳名（如小宝、贝贝）。
8. **禁止称谓**：不得使用“姓+女士/先生”，也不得使用“小李/小张”等称谓。

# 题干/设问规范（必须遵守）
1. **题干括号位置**：
   - 题干中的括号不能在句首，可放在句中或句末。
   - 选择题题干句末要有句号，句号在最后；判断题题干句子完结后加一个括号，括号在最后。
2. **括号格式**：
   - 使用中文括号，括号内部有且仅有一个全角空格（不能多）：`（　）`
   - 括号前后不允许空格
3. **题干简练**：题干建议120字以内；避免连接词堆叠（如并且、且、同时、另外、此外等过多）。题干较长时重点剔除与解题无关的表述，避免逻辑绕弯。
4. **设问表达**：
   - 设问须用陈述句，禁止使用问号（？）；不得以疑问句形式设问。
   - 少用否定句，禁止使用双重否定句。
   - **遣词造句与指代一致**：题干注意主谓搭配与指代一致，避免指代对象错误导致语义偏差。
   - **前提（必须遵守）**：题干必须是**肯定陈述句**，不得写成疑问句，不得依赖“是否正确/对不对/是不是”这类问法。
   - **判断题要求**：判断题只要求语义是肯定陈述句，并且题干中能明确出现“正确”或“错误”这一判断锚点；不要强制固定某一种模板句式。
   - **选择题设问表述**：选择题同样只要求题干是陈述句、以（　）作答占位结尾（句号在括号后），不强制固定使用某一类“以下表述正确的是/有/包括”模板。

# 选项规范（必须遵守）
1. **选项内容**：只填选项正文，禁止在内容前写 A./B./C./D. 标签。
2. **选项数量与正确性**：
   - 选择题每题4个选项；单选题仅1个正确，多选题≥2个正确。
   - 多选题中正确选项数量要合理，不要多道题都只有1个答案。
3. **标点与语义**：
   - 每个选项末尾不添加标点符号。
   - 选项必须与题干组成完整语句（将选项代入题干括号处语义完整）。
4. **一致性与干扰项**：
   - 选项中的姓名与题干中的姓名保持一致，不出现题目未涉及的姓名。
   - 干扰项必须具有干扰性，选项本身应是存在或相关的内容，不能无意义。
5. **数值型选项**：
   - 选项为数字时按从小到大顺序排列。
   - 计算题尽量简单（能口算优先），确需保留小数时注明保留位数（一般1-2位）。
   - 非计算题若解题过程涉及运算（如比例、折算、阈值比较），同样执行“简算优先”：避免复杂小数与冗长多步计算，不应依赖计算器；若必须保留小数，题干须明确“保留到X位小数”（一般1-2位）。
   - 未被选中的数值选项也必须有计算依据，不可胡编乱造。
6. **选项单位**：选项中有单位时，**必须**将单位提到题干中，**不得**在选项中反复出现单位。选项不得包含数值单位（如元、万元、平方米、年、%等）；单位应写在题干设问处（如「……额度为（　）万元」则选项只写 6、8、10、12），不得仅在选项中带单位。

# 输出解析格式要求（试题解析三段论，必须严格遵守）⚠️
解析必须带段首序号 **1、2、3、**，三段分别对应：**教材原文**、**试题分析**、**结论**。每段独立成段，不得合并或省略。

## 1、教材原文
- **内容构成**：必须包含「路由前三个标题（即目标题内容）+ 分级 + 教材原文」。**不要写「目标题：」这几个字**，直接写路由前三个标题的内容即可。
  - 目标题内容：即教材路径/路由的前三个标题（知识点层级），直接写出该内容即可。
  - 分级：掌握/了解/熟悉/识记，写在括号内，如（掌握）、（了解）。
  - 教材原文：可只复制知识点的主题句或关键句，但须保持语义完整；不可复制表格和图片，可改为文字描述。
- **字数**：总字数尽量控制在 **400 字以内**（贝经堂强制，大考题建议）。
- **示例**：`1、教材原文：线上过户优势及风险点(掌握) 风险点1：过户前网签合同信息...`

## 2、试题分析
- **内容要求**：必须**用自己的话**清晰解释每个选项与答案，**不可直接粘贴教材原文**。
- **覆盖范围**：单选题须说明正确选项依据及错误选项为何错；**多选题须解释所有正确选项及每个错误选项**，不得遗漏。
- **示例**：`2、试题分析：选项ACD都是正确的身份证明; 选项B, 香港/澳门居民不可以提供护照, 故错误。`

## 3、结论
- **判断题**：必须写 **本题答案为正确** 或 **本题答案为错误**；**禁止**写成本题答案为A/B。
- **选择题**：必须写 **本题答案为A/B/C/D/AB/AC...**（按实际正确选项组合）。
- **示例**：`3、结论：本题答案为ACD。`

## 其他约束
- **严禁**：直接粘贴教材原文表格或图片（可改成文字描述）；试题分析段不得整段粘贴教材原文。
- **一致性**：解析结论与答案字段必须一致，计算题须与计算过程一致。

6. **典型错题规避**：
   - 题干/选项/解析出现多字、少字、错字，影响作答。
   - 题干与选项/解析前后不一致。
   - 计算题无正确答案或答案与计算过程不一致。
   - 题目超纲或概念过时（如旧业务名/过期协议）。
   - 场景严重脱离经纪业务实际。
   - 干扰选项存在争议或与正确答案同样成立。

# 核心原则
1. **讲原理**: 解析要解释“为什么”，不要讲生成过程或机制。
2. **情境绑定**: 必须结合题干中的具体人物与情境进行解释。
3. **口语清晰**: 用清晰自然的口语解释，但避免“大家注意/这里有个陷阱/你可能以为”等口头禅。
4. **错误引导**: 对每个错误选项，直接指出学员可能的错误思路（例如“你可能以为……但……”）。
5. **格式规范**: 严格遵守要求的 JSON 结构。

# 润色约束（必须遵守）
1. **禁止元认知**：解析中不得出现“我遵循了规则/我没有引入/根据生成机制”等自我证明。
2. **禁止辩论体**：不要写“虽然…但…其实…”。只给出规则与结论。
3. **解析结构**：先摆事实，再引规则，最后结论；可选补充错误选项为什么错。
4. **错字修复**：发现明显错别字、乱码或奇怪词语，必须直接改正。

# ⚠️ 关键约束
1. **地理继承 (必须执行)**: 
   - 检查【参考教材】原文：
   - 如果原文明确限定了城市（如"北京市"），题干场景 **必须** 设定在该城市（或其下辖区县）。
   - 如果原文是通用规则（未提及特定城市），题干**不得**写具体城市，可用"某市"或不提及地点。
2. **严禁无关城市**:
   - **绝对禁止**出现原文未提及的其他具体城市名（如上海、深圳、广州等）。
   - 即使【参考母题】中写的是上海，你必须将其自动替换为原文指定的城市（如北京）或通用化。
   - 考生不需要掌握跨城市政策对比，不要制造这种干扰。
3. **时间逻辑**:
   - 如果原文未给出具体时间，题干与解析**不得**添加具体年份/日期；仅保留相对时间（如"满5年"）。
   - 若原文明示时间，才可使用对应年份/日期；判定逻辑必须严格遵循教材规则（如"满5年"的计算）。
   - 允许设计关于时间的干扰项（如设置一个时间未满的情景作为错误选项），但解析必须清晰指出不符合哪条时间规则。

# 自检清单（必须逐条核对）⚠️
1. **唯一答案**：题干条件足以排除其他选项，不能出现两条合理路径。
2. **解析规范（试题解析三段论，重点检查）**：
   - ✅ 第一段：以"1、教材原文："开头，含路由前三个标题（目标题内容）+分级+教材原文，≤400字；不要写「目标题：」字样
   - ✅ 第二段：以"2、试题分析："开头，用自己的话解释各选项，多选须覆盖全部选项
   - ✅ 第三段：以"3、结论："开头；判断题写本题答案为正确/错误，选择题写本题答案为A/B/C/D/AB/AC...
   - ❌ 不能省略任何一段，不能合并段落，必须用数字序号标识；严禁试题分析段直接粘贴教材原文
3. **一致性**：题干/选项/答案/解析前后一致，计算题与计算过程一致。
4. **适纲性**：不超纲，不引入材料外条件或结论。
5. **人名与措辞**：人名规范、无生造词、无模糊词。
6. **维度一致**：选项同维度，干扰项有理有据。

{self_check_text}

初稿（已执行一次机器硬预清洗，请在此基础上做语义与规范润色）: {json.dumps(draft_for_prompt, ensure_ascii=False)}
参考教材: {kb_context}
{examples_text}

# 输出格式 (JSON)
{{
    "question": "题干内容...",
    "options": ["第一项正文（勿写A.或A、等序号）", "第二项正文", "第三项正文", "第四项正文"],
    "answer": "A" 或 ["A", "C"],
    "explanation": "解析须严格按试题解析三段论：1、教材原文：（路由前三个标题即目标题内容+分级+教材原文，≤400字，不要写「目标题：」字样）2、试题分析：（用自己的话解释每个选项，多选须覆盖全部选项，不得粘贴教材原文）3、结论：（判断题写本题答案为正确/错误，选择题写本题答案为A/B/C/D/AB/AC...）。严禁省略号与省略段落。",
    "difficulty": 0.64
}}
"""
        prompt = issue_only_prompt
        response_text, _, llm_record = call_llm(
            node_name="writer.finalize",
            prompt=prompt,
            model_name=model_to_use,
            api_key=API_KEY,
            base_url=BASE_URL,
            trace_id=state.get("trace_id"),
            question_id=state.get("question_id"),
        )
        llm_records.append(llm_record)
        try:
            final_dict = parse_json_from_response(response_text)
            writer_logs = list(pre_writer_logs)
            print(f"DEBUG WRITER FINAL_JSON: {final_dict}")
            # Post-LLM hard format repair
            if isinstance(final_dict, dict):
                final_ir = _writer_normalize_phase(final_dict, target_type)
                final_dict = dict(final_ir)
                writer_logs.append("⚠️ 作家: 已在润色后执行格式硬修复")
                final_report = _writer_validate_phase(
                    final_ir,
                    target_type,
                    term_locks=term_locks,
                    kb_context=kb_context,
                    focus_contract=focus_contract,
                    is_calculation=is_calculation,
                    expected_calc_target=expected_calc_target,
                    expected_calc_unit=expected_calc_unit,
                )
                post_issue_lines = []
                for i in (final_report.get("issues") or []):
                    msg = str(i.get("message", "") or "").strip()
                    if not msg:
                        continue
                    fix = i.get("suggested_fix") or i.get("fix_hint")
                    post_issue_lines.append((msg, f"{msg}；修复建议：{fix}" if fix else msg))
                post_issues = [t[0] for t in post_issue_lines]
                post_issue_with_fix = [t[1] for t in post_issue_lines]
                if post_issue_with_fix and attempt == 0:
                    extra_self_check_issues.extend([f"定向修复项: {x}" for x in post_issue_with_fix])
                    writer_logs.append(
                        f"⚠️ 作家: 仍存在待修复问题，发起二次润色（{'; '.join(post_issues[:6])}）"
                    )
                    continue
        except Exception as e:
            last_exception = e
            continue
        break
    if final_dict is not None and isinstance(final_dict, dict):
        final_ir = _writer_normalize_phase(final_dict, target_type)
        final_report = _writer_validate_phase(
            final_ir,
            target_type,
            term_locks=term_locks,
            kb_context=kb_context,
            focus_contract=focus_contract,
            is_calculation=is_calculation,
            expected_calc_target=expected_calc_target,
            expected_calc_unit=expected_calc_unit,
        )
    if final_dict is None and last_exception is not None:
        return {
            "final_json": None,
            "writer_validation_report": final_report,
            "writer_retry_exhausted": True,
            "llm_trace": llm_records,
            "logs": [f"❌ 作家格式化失败: {str(last_exception)}"]
        }

    try:
        # Validate and optional repair loop: fix explanation/conclusion then re-validate (max 2 rounds)
        payload = final_dict if isinstance(final_dict, dict) else {"question": "", "options": [], "answer": "", "explanation": ""}
        report = _writer_validate_phase(
            payload,
            target_type,
            term_locks=term_locks,
            kb_context=kb_context,
            focus_contract=focus_contract,
            is_calculation=is_calculation,
            expected_calc_target=expected_calc_target,
            expected_calc_unit=expected_calc_unit,
        )
        MAX_WRITER_FIX_LOOPS = 2
        for fix_round in range(MAX_WRITER_FIX_LOOPS):
            if report.get("passed"):
                break
            issues_list = report.get("issues") or []
            expl_related = [
                i for i in issues_list
                if i.get("field") == "explanation" or (str(i.get("issue_code") or "").startswith("HARD_EXPL"))
            ]
            if not expl_related:
                break
            # Re-normalize explanation (three-stage structure + conclusion) and re-validate
            final_ir = _writer_normalize_phase(payload, target_type)
            payload = dict(final_ir)
            if isinstance(final_dict, dict):
                final_dict["explanation"] = payload.get("explanation", final_dict.get("explanation", ""))
            report = _writer_validate_phase(
                payload,
                target_type,
                term_locks=term_locks,
                kb_context=kb_context,
                focus_contract=focus_contract,
                is_calculation=is_calculation,
                expected_calc_target=expected_calc_target,
                expected_calc_unit=expected_calc_unit,
            )
            writer_logs.append(f"⚠️ 作家: 解析校验未通过，已执行解析规范化修复并重验（第 {fix_round + 1} 轮）")
        final_report = report
        issues = [str(i.get("message", "")) for i in (report.get("issues") or []) if str(i.get("message", "")).strip()]
        has_multiselect_answer_contract_violation = (
            target_type == "多选题"
            and any("多选题答案格式应为多个字母" in m for m in issues)
        )
        has_calc_target_drift = any("计算题设问目标发生漂移" in m for m in issues)
        if has_calc_target_drift:
            writer_logs.append("❌ 作家: 计算题设问目标发生漂移，终止本轮并要求重生")
            return {
                "final_json": None,
                "writer_format_issues": issues,
                "writer_validation_report": final_report,
                "writer_retry_exhausted": True,
                "candidate_sentences": [],
                "llm_trace": llm_records,
                "logs": writer_logs,
            }
        if has_multiselect_answer_contract_violation:
            writer_logs.append("⚠️ 作家: 多选题答案契约失败（答案少于2个），将执行答案纠偏后继续。")
            if isinstance(final_dict, dict):
                inferred = _infer_multiselect_labels_from_explanation(
                    str(final_dict.get("explanation", "") or ""),
                    option_count=len(final_dict.get("options") or []),
                )
                if len(inferred) < 2:
                    inferred = ["A", "B"]
                final_dict["answer"] = inferred[:4]
        if issues:
            writer_logs.append(f"⚠️ 作家: 格式校验发现问题（继续送审）: {', '.join(issues)}")
            writer_retry_exhausted = True
        # Keep final_dict in sync for downstream 格式大清洗
        if isinstance(final_dict, dict) and isinstance(payload, dict):
            final_dict["explanation"] = payload.get("explanation", final_dict.get("explanation", ""))

        # ------------------------------------------------------------------
        # 2. 格式大清洗 (Convert to Flat Excel Structure)
        # ------------------------------------------------------------------
        # 准备 Excel 要求的扁平化字段
        excel_row = {}
        
        # A. 题干
        question_text = final_dict.get('question', '')
        if target_type in ["判断题", "单选题", "多选题"]:
            question_text = normalize_blank_brackets(question_text)
        excel_row['题干'] = question_text
        
        # B. 选项拆解 (Option 1-8)
        options = final_dict.get('options', [])
        # 如果是判断题，强制修正选项
        if target_type == "判断题":
            options = ["正确", "错误"]
            
        labels = ['1', '2', '3', '4', '5', '6', '7', '8']
        for i, label in enumerate(labels):
            key = f"选项{label}"
            if i < len(options):
                # Robust cleanup
                val = str(options[i])
                # Only strip alphabetic option labels like A./A、/A: .
                # Never strip numeric prefixes here, otherwise decimals such as 2.7 / 3.3 get corrupted.
                val = re.sub(r'^[A-HＡ-Ｈa-h][\.\、:：\s\)）]+', '', val, flags=re.IGNORECASE)
                # Strip leading single A-H when followed by CJK (avoids "A网签" -> display "A. A网签...")
                val = re.sub(r'^[A-HＡ-Ｈa-h](?=[\u4e00-\u9fff])', '', val, flags=re.IGNORECASE)
                val = val.strip()
                if target_type in ["判断题", "单选题", "多选题"]:
                    val = normalize_blank_brackets(val)
                excel_row[key] = val
            else:
                excel_row[key] = "" 
        
        # C. 答案选项（统一解析多种格式）
        raw_ans = final_dict.get('answer')
        answer_labels = _parse_answer_labels(raw_ans)
        final_ans = ""
        if target_type == "多选题":
            if len(answer_labels) < 2:
                inferred_labels = _infer_multiselect_labels_from_explanation(
                    str(final_dict.get("explanation", "") or ""),
                    option_count=len(options or []),
                )
                for x in inferred_labels:
                    if x not in answer_labels:
                        answer_labels.append(x)
                if len(answer_labels) < 2:
                    answer_labels = [x for x in ["A", "B"] if x in {chr(ord("A") + i) for i in range(max(1, len(options or [])))}]
                    if len(answer_labels) < 2:
                        answer_labels = ["A", "B"]
            final_ans = "".join(sorted(dict.fromkeys(answer_labels)))
        else:
            if not answer_labels:
                final_ans = str(raw_ans).upper().strip()[:1]
            else:
                final_ans = answer_labels[0]
            
        excel_row['正确答案'] = final_ans
        
        # D. 知识点拆解
        path_parts = [p.strip() for p in kb_chunk.get('完整路径', '').split(' > ') if p.strip()]
        excel_row['一级知识点'] = path_parts[0] if len(path_parts) > 0 else ""
        excel_row['二级知识点'] = path_parts[1] if len(path_parts) > 1 else ""
        excel_row['三级知识点'] = path_parts[2] if len(path_parts) > 2 else ""
        excel_row['四级知识点'] = path_parts[3] if len(path_parts) > 3 else ""
        
        # E. 其他字段
        excel_row['解析'] = final_dict.get('explanation', '')
        excel_row['掌握程度'] = str(kb_chunk.get('掌握程度', '') or '').strip()
        
        # 难度转换：优先数值；若仍返回“易/中/难”，按当前难度区间映射为区间内数值
        raw_diff = final_dict.get('difficulty', '中')
        diff_map = {"易": 0.3, "中": 0.5, "难": 0.8}
        if isinstance(raw_diff, str):
            raw_diff_norm = raw_diff.strip()
            if raw_diff_norm in diff_map:
                if difficulty_range:
                    min_diff, max_diff = difficulty_range
                    if raw_diff_norm == "易":
                        difficulty_value = min_diff
                    elif raw_diff_norm == "中":
                        difficulty_value = (min_diff + max_diff) / 2
                    else:
                        difficulty_value = max_diff
                else:
                    difficulty_value = diff_map.get(raw_diff_norm, 0.5)
            else:
                try:
                    difficulty_value = float(raw_diff_norm)
                except Exception:
                    difficulty_value = 0.5
        else:
            try:
                difficulty_value = float(raw_diff)
            except Exception:
                difficulty_value = 0.5
        
        # 如果指定了难度范围，验证并调整难度值
        if difficulty_range:
            min_diff, max_diff = difficulty_range
            if difficulty_value < min_diff or difficulty_value > max_diff:
                # 如果不在范围内，调整到范围中点
                difficulty_value = (min_diff + max_diff) / 2
                print(f"⚠️ 警告: 生成的难度值不在指定范围内，已调整为 {difficulty_value:.2f}")

        # 掌握程度兜底：熟悉/了解不允许过高难度
        mastery = str(kb_chunk.get('掌握程度', '') or '').strip()
        if mastery == "了解" and difficulty_value > 0.5:
            difficulty_value = 0.5
        elif mastery == "熟悉" and difficulty_value > 0.6:
            difficulty_value = 0.6
        
        excel_row['难度值'] = difficulty_value

        inferred_final_type = _infer_final_json_question_type(excel_row)
        if locked_question_type in ["单选题", "多选题", "判断题"] and inferred_final_type != locked_question_type:
            writer_logs.append(
                f"⚠️ 作家: 最终题型漂移（locked={locked_question_type}, inferred={inferred_final_type}），执行就地纠偏"
            )
            if locked_question_type == "多选题":
                labels = _parse_answer_labels(excel_row.get("正确答案", ""))
                if len(labels) < 2:
                    labels = _infer_multiselect_labels_from_explanation(
                        str(excel_row.get("解析", "") or ""),
                        option_count=len([x for x in [excel_row.get(f'选项{i}', '') for i in range(1, 9)] if str(x).strip()]),
                    )
                if len(labels) < 2:
                    labels = ["A", "B"]
                excel_row["正确答案"] = "".join(sorted(dict.fromkeys(labels)))
            elif locked_question_type == "单选题":
                labels = _parse_answer_labels(excel_row.get("正确答案", ""))
                excel_row["正确答案"] = labels[0] if labels else "A"
            elif locked_question_type == "判断题":
                labels = _parse_answer_labels(excel_row.get("正确答案", ""))
                excel_row["正确答案"] = labels[0] if labels and labels[0] in {"A", "B"} else "A"
            inferred_final_type = _infer_final_json_question_type(excel_row)
            if inferred_final_type != locked_question_type:
                writer_logs.append(
                    f"❌ 作家: 题型纠偏失败（locked={locked_question_type}, inferred={inferred_final_type}），本轮重生"
                )
                return {
                    "final_json": None,
                    "current_question_type": str(locked_question_type),
                    "writer_format_issues": [f"题型漂移: locked={locked_question_type}, inferred={inferred_final_type}"],
                    "writer_validation_report": final_report,
                    "writer_retry_exhausted": True,
                    "candidate_sentences": [],
                    "llm_trace": llm_records,
                    "logs": writer_logs,
                }

        calc_align_msg = ""
        if is_calculation:
            excel_row, calc_aligned, calc_align_msg = _enforce_calc_answer_alignment_on_final_json(
                excel_row,
                execution_result=state.get("execution_result"),
                code_status=str(state.get("code_status", "") or ""),
            )
            if calc_aligned:
                final_ans = str(excel_row.get("正确答案", final_ans) or final_ans)

        # 构造题干+选项组合的候选句，用于后续 Critic 可读性复核
        candidate_sentences = []
        try:
            if target_type in ["单选题", "多选题", "判断题"]:
                candidate_sentences = build_candidate_sentences(question_text, options)
        except Exception as _e:
            # 可读性候选句构建失败不应阻断流程，仅打印调试信息
            print(f"⚠️ build_candidate_sentences 失败: {_e}")

        return {
            "final_json": excel_row,  # Now strictly matches Excel template & ExamQuestion model
            "current_question_type": target_type,  # Pass actual question type to downstream nodes
            "locked_question_type": locked_question_type or target_type,
            "writer_format_issues": issues,
            "writer_validation_report": final_report,
            "writer_retry_exhausted": writer_retry_exhausted,
            "candidate_sentences": candidate_sentences,
            "llm_trace": llm_records,
            "logs": writer_logs + ([f"🧮 作家: 计算题答案对齐完成（{calc_align_msg}）"] if calc_align_msg else []) + [f"✍️ 作家: 已格式化为【{target_type}】 (答案: {final_ans})"]
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "final_json": None,
            "writer_validation_report": final_report,
            "writer_retry_exhausted": True,
            "llm_trace": llm_records,
            "logs": [f"❌ 作家格式化失败: {str(e)}"]
        }

def critic_node(state: AgentState, config):
    llm_records: List[Dict[str, Any]] = []
    # Structured option hierarchy conflict detection defaults
    option_hierarchy_conflict_flag: bool = False
    option_hierarchy_conflict_pairs: List[Dict[str, Any]] = []
    option_hierarchy_conflict_message: str = ""
    # Debug/testing hook: force one "minor" failure to demonstrate the fixer loop.
    if state.get("debug_force_fail_once") and state.get("retry_count", 0) == 0:
        return {
            "critic_feedback": "FORCED_FAIL",
            "critic_details": "Forced minor failure for loop demo (will go to Fixer).",
            "critic_result": {"passed": False, "issue_type": "minor", "reason": "forced", "fail_types": ["debug_forced"]},
            "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
            "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
            "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
            "retry_count": 1,
            "llm_trace": llm_records,
            "logs": ["🧪 批评家: 已强制驳回一次，用于演示 Fixer 闭环"]
        }
    final_json = state.get('final_json')
    if not final_json:
        return {
            "critic_feedback": "FAIL",
            "critic_details": "No question generated to verify.",
            "critic_result": {
                "passed": False,
                "issue_type": "major",
                "reason": "No question generated to verify.",
                "fail_types": ["no_question"],
            },
            "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
            "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
            "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
            "retry_count": state.get("retry_count", 0) + 1,
            "llm_trace": llm_records,
            "logs": ["🕵️ 批评家: 无法审核，未生成题目。"]
        }
    # Log when we are re-reviewing after Fixer so we confirm we got the updated question
    is_post_fixer = isinstance(final_json, dict) and final_json.get("_was_fixed") is True
    if is_post_fixer:
        print("DEBUG CRITIC: 收到 Fixer 后的题目 (final_json._was_fixed=True)，将基于最新题目审核")
    print(f"DEBUG CRITIC INPUT FINAL_JSON: {final_json}")

    kb_chunk = state['kb_chunk']
    term_locks = state.get("term_locks") or []
    retriever = config['configurable'].get('retriever') or get_default_retriever()
    examples = state.get('examples', [])
    kb_context, parent_slices, related_slices = build_extended_kb_context(kb_chunk, retriever, examples)
    
    # Get difficulty range from config
    difficulty_range = config['configurable'].get('difficulty_range')
    writer_validation_report = state.get("writer_validation_report") or {}
    writer_retry_exhausted = bool(state.get("writer_retry_exhausted"))
    
    # ✅ 信息不对称校验：Critic 拥有全量教材逻辑
    # 获取相关的全量规则（不仅仅是当前知识点）
    full_rules_context = kb_context  # 当前 + 上一级 + 相似切片
    
    # 相关规则集合（上一级切片全集 + 相似切片）
    related_rules = []
    for chunk in (parent_slices + related_slices):
        chunk_path = chunk.get('完整路径', '')
        if chunk_path and chunk_path != kb_chunk.get('完整路径', ''):
            related_rules.append({
                "路径": chunk_path,
                "内容": format_kb_chunk_full(chunk)
            })
    
    # 构建全量规则上下文
    full_rules_text = f"# 当前知识点规则\n{kb_context}\n"
    if related_rules:
        full_rules_text += "\n# 相关知识点规则（用于完整判定）\n"
        for rule in related_rules[:5]:  # 最多5个相关规则
            full_rules_text += f"【{rule['路径']}】\n{rule['内容']}\n\n"
    
    # 批评家固定使用审计模型（GPT-5.2）
    agent_name = state.get('agent_name', '')
    # ✅ Prioritize reading question type from state (set by Writer), fallback to config
    locked_question_type = state.get("locked_question_type")
    question_type = (
        locked_question_type
        or state.get('current_question_type')
        or config['configurable'].get('question_type', '单选题')
    )
    cfg_question_type = config['configurable'].get('question_type', '单选题')

    # 选项父子类层级冲突结构化检测（单选/多选题）
    try:
        if isinstance(final_json, dict):
            (
                option_hierarchy_conflict_flag,
                option_hierarchy_conflict_pairs,
                option_hierarchy_conflict_message,
            ) = detect_option_hierarchy_conflict(final_json, kb_context, question_type)
    except Exception as e:
        print(f"⚠️ Critic 选项层级冲突检测失败: {e}")
        option_hierarchy_conflict_flag = False
        option_hierarchy_conflict_pairs = []
        option_hierarchy_conflict_message = ""
    
    # ✅ Question type consistency validation (only for specific type mode)
    # If config is "随机", skip type validation
    # If config is specific type (单选/多选/判断), validate consistency with state
    print(f"🔍 Critic 开始执行 - cfg题型:[{cfg_question_type}], state题型:[{question_type}]")
    inferred_final_type = _infer_final_json_question_type(final_json)
    if locked_question_type in ["单选题", "多选题", "判断题"] and inferred_final_type != locked_question_type:
        reason = f"题型锁定不一致：specialist锁定[{locked_question_type}]，最终题目推断为[{inferred_final_type}]"
        print(f"❌ {reason}")
        return {
            "critic_feedback": "FAIL",
            "critic_rules_context": full_rules_text,
            "critic_related_rules": related_rules,
            "critic_result": {
                "passed": False,
                "issue_type": "major",
                "reason": reason,
                "fix_strategy": "regenerate",
                "fail_types": ["locked_question_type_mismatch"],
            },
            "critic_details": reason,
            "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
            "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
            "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
            "critic_model_used": "rule-based",
            "retry_count": state.get("retry_count", 0) + 1,
            "llm_trace": llm_records,
            "logs": [f"🔍 批评家: ❌ {reason}"],
        }
    if cfg_question_type != "随机" and cfg_question_type in ["单选题", "多选题", "判断题"]:
        if question_type != cfg_question_type:
            high_risk_profile = (state.get("router_details") or {}).get("high_risk_profile") or {}
            if (
                cfg_question_type == "单选题"
                and question_type == "多选题"
                and bool(high_risk_profile.get("prohibit_single_choice"))
            ):
                reason = "配置题型冲突：当前切片命中“禁出单选”规则（并列规则/材料清单），但任务配置为单选题。请改为“随机”或“多选题”。"
                print(f"❌ {reason}")
                return {
                    "critic_feedback": "FAIL",
                    "critic_rules_context": full_rules_text,
                    "critic_related_rules": related_rules,
                    "critic_result": {
                        "passed": False,
                        "issue_type": "major",
                        "reason": reason,
                        "fix_strategy": "regenerate",
                        "fail_types": ["question_type_config_conflict", "prohibit_single_choice_conflict"],
                    },
                    "critic_details": reason,
                    "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
                    "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
                    "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
                    "critic_model_used": "rule-based",
                    "retry_count": state.get("retry_count", 0) + 1,
                    "llm_trace": llm_records,
                    "logs": [f"🔍 批评家: ❌ {reason}"],
                }
            print(f"❌ 题型不一致: 要求[{cfg_question_type}]，实际[{question_type}]")
            return {
                "critic_feedback": "FAIL",
                "critic_rules_context": full_rules_text,
                "critic_related_rules": related_rules,
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "reason": f"题型不一致：要求生成{cfg_question_type}，但实际生成了{question_type}",
                    "fix_strategy": "regenerate",
                    "fail_types": ["question_type_mismatch"],
                },
                "critic_details": f"题型校验失败：要求{cfg_question_type}，实际{question_type}",
                "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
                "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
                "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
                "critic_model_used": "rule-based",
                "retry_count": state.get("retry_count", 0) + 1,
                "llm_trace": llm_records,
                "logs": [f"🔍 批评家: ❌ 题型不一致（要求{cfg_question_type}，实际{question_type}）→ 重新生成"]
            }
    else:
        print(f"✅ 跳过题型校验（随机模式或已匹配）")

    configured_mode = config['configurable'].get('generation_mode', '随机')
    effective_generation_mode = state.get("current_generation_mode") or resolve_effective_generation_mode(configured_mode, state)[0]

    # ✅ 模式强约束：实战应用/推演必须体现业务场景
    if effective_generation_mode == "实战应用/推演":
        stem_text = str(final_json.get("题干", "")) if isinstance(final_json, dict) else ""
        structural_ok, structural_reason = _is_business_context_structural(stem_text)
        has_context, semantic_reason, semantic_record = has_business_context(
            stem_text,
            kb_context=kb_context,
            model_name=CRITIC_MODEL,
            api_key=CRITIC_API_KEY,
            base_url=CRITIC_BASE_URL,
            provider=CRITIC_PROVIDER,
            trace_id=state.get("trace_id"),
            question_id=state.get("question_id"),
        )
        if semantic_record:
            llm_records.append(semantic_record)
        # 双判定策略（稳态）：语义判定为主，结构判定作为补充防护，避免结构误判导致误杀
        if not has_context:
            reason = (
                "筛选条件不符合：当前为【实战应用/推演】，题干未满足业务场景双判定"
                f"（结构判定: {structural_reason}；语义判定: {semantic_reason}）"
            )
            return {
                "critic_feedback": "FAIL",
                "critic_rules_context": full_rules_text,
                "critic_related_rules": related_rules,
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "reason": reason,
                    "fix_strategy": "regenerate",
                    "fail_types": ["generation_mode"],
                },
                "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
                "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
                "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
                "critic_details": reason,
                "critic_model_used": "rule-based",
                "retry_count": state.get("retry_count", 0) + 1,
                "llm_trace": llm_records,
                "logs": [f"🔍 批评家: ❌ {reason} → 重新生成"]
            }
        if not structural_ok:
            llm_records.append(
                {
                    "node": "critic.scene_structural",
                    "provider": "rule-based",
                    "model": "rule-based",
                    "success": True,
                    "error": None,
                    "latency_ms": 0.0,
                    "total_tokens": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "detail": structural_reason,
                }
            )

    # 从 Writer 或当前 final_json 构造题干+选项组合候选句，供可读性审计使用
    candidate_sentences = state.get("candidate_sentences") or []
    if (not candidate_sentences) and isinstance(final_json, dict) and question_type in ["单选题", "多选题", "判断题"]:
        stem_for_read = str(final_json.get("题干", "") or "")
        option_values: List[str] = []
        for i in range(1, 9):
            val = str(final_json.get(f"选项{i}", "") or "").strip()
            if val:
                option_values.append(val)
        try:
            candidate_sentences = build_candidate_sentences(stem_for_read, option_values)
        except Exception as _e:
            print(f"⚠️ Critic 构造 candidate_sentences 失败: {_e}")

    # ✅ Bracket format: only stem ending bracket must have full-width space; options use full check
    if question_type in ["单选题", "多选题", "判断题"]:
        invalid_fields = []
        if isinstance(final_json, dict):
            stem_text = str(final_json.get("题干", ""))
            if has_invalid_ending_blank_bracket(stem_text):
                invalid_fields.append("题干")
            for i in range(1, 9):
                key = f"选项{i}"
                if key in final_json and final_json.get(key):
                    if has_invalid_blank_bracket(str(final_json.get(key, ""))):
                        invalid_fields.append(key)
        if invalid_fields:
            reason = "题干结尾括号中间必须有且仅有一个全角空格（不能多）；选项若有占位括号须为全角括号且括号内有且仅有一个全角空格（不能多）"
            return {
                "critic_feedback": "FAIL",
                "critic_rules_context": full_rules_text,
                "critic_related_rules": related_rules,
                "critic_result": {
                    "passed": False,
                    "issue_type": "minor",
                    "reason": reason,
                    "fix_strategy": "fix_question",
                    "fail_types": ["format_bracket"],
                },
                "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
                "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
                "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
                "critic_details": f"{reason}（字段：{', '.join(invalid_fields)}）",
                "critic_model_used": "rule-based",
                "retry_count": state.get("retry_count", 0) + 1,
                "llm_trace": llm_records,
                "logs": [f"🔍 批评家: ❌ {reason} → 进入修复"]
            }

    # ✅ Material missing check: multiple missing required materials -> Fail
    has_material_issue, missing_materials = material_missing_check(final_json, kb_context)
    if has_material_issue:
        reason = f"材料缺失项不唯一：缺失 {', '.join(missing_materials)}"
        return {
            "critic_feedback": "FAIL",
            "critic_rules_context": full_rules_text,
            "critic_related_rules": related_rules,
            "critic_result": {
                "passed": False,
                "issue_type": "major",
                "reason": reason,
                "fix_strategy": "fix_question",
                "fail_types": ["material_missing"],
            },
            "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
            "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
            "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
            "critic_details": reason,
            "critic_model_used": "rule-based",
            "retry_count": state.get("retry_count", 0) + 1,
            "llm_trace": llm_records,
            "logs": [f"🔍 批评家: ❌ {reason} → 进入修复"]
        }

    material_coverage_issue = validate_material_coverage_rule(
        final_json,
        kb_context=kb_context,
        question_type=question_type,
    )
    if material_coverage_issue:
        reason = str(material_coverage_issue.get("reason", "") or "材料清单题校验失败")
        return {
            "critic_feedback": "FAIL",
            "critic_rules_context": full_rules_text,
            "critic_related_rules": related_rules,
            "critic_result": {
                "passed": False,
                "issue_type": str(material_coverage_issue.get("issue_type", "major") or "major"),
                "reason": reason,
                "fix_strategy": str(material_coverage_issue.get("fix_strategy", "fix_both") or "fix_both"),
                "required_fixes": list(material_coverage_issue.get("required_fixes") or []),
                "fail_types": list(material_coverage_issue.get("fail_types") or ["material_rule_fail"]),
            },
            "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
            "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
            "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
            "critic_details": reason,
            "critic_model_used": "rule-based",
            "retry_count": state.get("retry_count", 0) + 1,
            "llm_trace": llm_records,
            "logs": [f"🔍 批评家: ❌ {reason} → 进入修复"]
        }

    # 规则前提槽位验收：仅保留语义校验（不再使用词面/正则命中判定）。
    rule_precondition_profile = (state.get("router_details") or {}).get("rule_precondition_profile") or {}
    precondition_labels: List[str] = []
    if isinstance(rule_precondition_profile, dict) and rule_precondition_profile.get("enabled"):
        specs = rule_precondition_profile.get("required_slot_specs")
        if isinstance(specs, list):
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                label = str(spec.get("label", "") or "").strip()
                if label and label not in precondition_labels:
                    precondition_labels.append(label)
        if not precondition_labels:
            label_map = {
                "适用地域": "政策适用地域",
                "主体身份": "购房/交易主体身份",
                "时间条件": "关键时间口径",
                "适用边界": "规则触发边界",
            }
            for slot in (rule_precondition_profile.get("required_slots") or []):
                slot_text = str(slot).strip()
                if not slot_text:
                    continue
                label = label_map.get(slot_text, slot_text)
                if label not in precondition_labels:
                    precondition_labels.append(label)
    precondition_labels = precondition_labels[:2]

    if precondition_labels:
        stem_text = str(final_json.get("题干", "") or "")
        options_for_slot = []
        for i in range(1, 9):
            opt_val = str(final_json.get(f"选项{i}", "") or "").strip()
            if opt_val:
                options_for_slot.append(f"{chr(64+i)}. {opt_val}")
        options_text = "\n".join(options_for_slot)
        semantic_prompt = f"""
你是“前提槽位语义验收器”。请仅基于语义判断【题干+选项整体】是否覆盖必需槽位（禁止按关键词机械匹配）。
说明：槽位可以在题干中出现，也可以通过选项中的明确条件共同完成判定。

必需槽位：
{json.dumps(precondition_labels, ensure_ascii=False)}

题干：
{stem_text}

选项：
{options_text}

规则上下文：
{full_rules_text}

只返回 JSON：
{{
  "passed": true/false,
  "missing_slots": ["缺失槽位1","缺失槽位2"],
  "reason": "一句话",
  "evidence": ["证据片段1","证据片段2"]
}}
"""
        try:
            sem_content, _, sem_record = call_llm(
                node_name="critic.precondition_semantic",
                prompt=semantic_prompt,
                model_name=CRITIC_MODEL,
                api_key=CRITIC_API_KEY,
                base_url=CRITIC_BASE_URL,
                provider=CRITIC_PROVIDER,
                trace_id=state.get("trace_id"),
                question_id=state.get("question_id"),
                temperature=0.0,
            )
            llm_records.append(sem_record)
            sem_json = parse_json_from_response(sem_content)
            if isinstance(sem_json, dict):
                missing_rule_preconditions = [
                    str(x).strip()
                    for x in (sem_json.get("missing_slots") or [])
                    if str(x).strip()
                ]
                sem_passed = bool(sem_json.get("passed", False)) and not missing_rule_preconditions
                if not sem_passed:
                    sem_reason = str(sem_json.get("reason", "") or "").strip()
                    if not sem_reason:
                        sem_reason = "题干/选项缺少关键规则前提，无法稳定判定唯一答案"
                    reason = f"{sem_reason}；缺失：{', '.join(missing_rule_preconditions)}"
                    return {
                        "critic_feedback": "FAIL",
                        "critic_rules_context": full_rules_text,
                        "critic_related_rules": related_rules,
                        "critic_result": {
                            "passed": False,
                            "issue_type": "major",
                            "reason": reason,
                            "fix_strategy": "fix_question",
                            "required_fixes": ["logic:missing_conditions", "logic:precondition_slots"],
                            "fail_types": ["reverse_solve_fail", "missing_preconditions"],
                            "missing_conditions": missing_rule_preconditions,
                        },
                        "critic_required_fixes": ["logic:missing_conditions", "logic:precondition_slots"],
                        "critic_details": reason,
                        "critic_model_used": "llm-semantic",
                        "retry_count": state.get("retry_count", 0) + 1,
                        "llm_trace": llm_records,
                        "logs": [f"🔍 批评家: ❌ {reason} → 进入修复"],
                    }
        except Exception as e:
            # 语义检查异常时不在此处直接失败，交由后续 Critic 主审链路兜底。
            llm_records.append(
                {
                    "node": "critic.precondition_semantic",
                    "provider": "llm",
                    "model": CRITIC_MODEL,
                    "success": False,
                    "error": str(e),
                    "latency_ms": 0.0,
                    "total_tokens": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "detail": "slot semantic check error",
                }
            )

    focus_overload_issue = detect_focus_overload_issue(
        final_json,
        focus_contract=state.get("locked_focus_contract") or (state.get("router_details") or {}).get("focus_contract") or {},
        rule_precondition_profile=rule_precondition_profile,
    )
    if focus_overload_issue:
        reason = str(focus_overload_issue.get("reason", "") or "题干测点过载")
        return {
            "critic_feedback": "FAIL",
            "critic_rules_context": full_rules_text,
            "critic_related_rules": related_rules,
            "critic_result": {
                "passed": False,
                "issue_type": str(focus_overload_issue.get("issue_type", "major") or "major"),
                "reason": reason,
                "fix_strategy": str(focus_overload_issue.get("fix_strategy", "fix_question") or "fix_question"),
                "required_fixes": list(focus_overload_issue.get("required_fixes") or ["quality:focus_slimming"]),
                "fail_types": list(focus_overload_issue.get("fail_types") or ["quality_fail"]),
            },
            "critic_required_fixes": list(focus_overload_issue.get("required_fixes") or ["quality:focus_slimming"]),
            "critic_details": reason,
            "critic_model_used": "rule-based",
            "retry_count": state.get("retry_count", 0) + 1,
            "llm_trace": llm_records,
            "logs": [f"🔍 批评家: ❌ {reason} → 进入修复"],
        }

    calculation_closure_issue = None
    if state.get("agent_name") == "CalculatorAgent":
        # 通用可解性契约：计算题必须具备公式所需关键输入槽位（缺失则直接重生，避免送入语义审计反复失败）
        required_slots = state.get("calc_required_slots") or _extract_required_calc_slots(kb_chunk)
        stem_for_slots = str(final_json.get("题干", "") or "")
        missing_slots = _detect_missing_calc_slots(stem_for_slots, required_slots)
        if required_slots and len(missing_slots) >= 2:
            reason = f"计算题前置条件缺失，无法唯一求解；缺少关键槽位：{', '.join(missing_slots[:6])}"
            return {
                "critic_feedback": "FAIL",
                "critic_rules_context": full_rules_text,
                "critic_related_rules": related_rules,
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "reason": reason,
                    "fix_strategy": "regenerate",
                    "required_fixes": ["logic:missing_conditions", "calc:solvability_contract"],
                    "fail_types": ["reverse_solve_fail", "calc_missing_preconditions"],
                    "missing_conditions": missing_slots[:6],
                },
                "critic_required_fixes": ["logic:missing_conditions", "calc:solvability_contract"],
                "critic_details": reason,
                "critic_model_used": "rule-based",
                "retry_count": state.get("retry_count", 0) + 1,
                "llm_trace": llm_records,
                "logs": [f"🔍 批评家: ❌ {reason} → 重新生成"],
                "calc_missing_slots": missing_slots,
            }

        calculation_closure_issue = validate_calculation_closure(
            final_json,
            question_type=question_type,
            execution_result=state.get("execution_result"),
            code_status=str(state.get("code_status", "") or ""),
            expected_calc_target=str(state.get("calc_target_signature", "") or ""),
            expected_unit_hint=str(state.get("calc_unit_hint", "") or ""),
        )
    if calculation_closure_issue:
        reason = str(calculation_closure_issue.get("reason", "") or "计算题数值闭环不成立")
        issue_type = str(calculation_closure_issue.get("issue_type", "") or "major")
        fix_strategy = str(calculation_closure_issue.get("fix_strategy", "") or "regenerate")
        fail_types = calculation_closure_issue.get("fail_types") or ["calculation_closure_fail"]
        required_fixes = calculation_closure_issue.get("required_fixes") or ["calc:closure"]
        level = "重新生成" if fix_strategy == "regenerate" else "进入修复"
        return {
            "critic_feedback": "FAIL",
            "critic_rules_context": full_rules_text,
            "critic_related_rules": related_rules,
            "critic_result": {
                "passed": False,
                "issue_type": issue_type,
                "reason": reason,
                "fix_strategy": fix_strategy,
                "fail_types": fail_types,
            },
            "critic_required_fixes": required_fixes,
            "critic_details": reason,
            "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
            "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
            "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
            "critic_model_used": "rule-based",
            "retry_count": state.get("retry_count", 0) + 1,
            "llm_trace": llm_records,
            "logs": [f"🔍 批评家: ❌ {reason} → {level}"],
        }

    # ✅ Smart model switching: Check GPT rate limit and switch to Deepseek if needed
    critic_model = CRITIC_MODEL
    critic_model_used = critic_model
    critic_api_key = CRITIC_API_KEY
    critic_base_url = CRITIC_BASE_URL
    critic_provider = CRITIC_PROVIDER
    
    # Check if GPT model is rate-limited
    if critic_model and critic_model.lower().startswith("gpt") and "api.deepseek.com" in (critic_base_url or ""):
        throttle_path = Path(".gpt_rate_limit.txt")
        if throttle_path.exists():
            try:
                last_ts = float(throttle_path.read_text(encoding="utf-8").strip() or "0")
                now = time.time()
                elapsed = now - last_ts
                wait_needed = max(0, 12 - elapsed)
                
                # If need to wait > 5 seconds, switch to Deepseek
                if wait_needed > 5:
                    print(f"⚠️ GPT-5.2 限流中（需等待 {int(wait_needed)}s），切换到 Deepseek Reasoner")
                    critic_model = "deepseek-reasoner"
                    critic_api_key = API_KEY  # Use default OpenAI-compatible key
                    critic_base_url = BASE_URL  # Use default base URL
                    critic_provider = "ait"
            except Exception as e:
                print(f"⚠️ 限流检测失败: {e}，使用默认模型")
    
    if critic_model and "deepseek" in critic_model.lower():
        log_prefix = f"🔍 批评家 (Deepseek):"
    else:
        log_prefix = f"🔍 批评家 ({critic_model}):"

    # 题干与选项组合可读性复核（仅对单选/多选启用；判断题“正确/错误”代入易产生表面重复误判）
    if question_type in ["单选题", "多选题"] and candidate_sentences:
        try:
            readability_prompt = f"""
# 角色
你是中文试题的可读性审稿人，专门检查“题干+选项代入”后的句子在中文里是否自然顺畅。

# 说明
- 已给出题干模板中的括号占位符句子，以及把不同选项内容代入括号（　）后形成的完整句子列表。每项带有 index、option_label（选项A/B/C/D）、option 原文和代入后的 sentence。
- 你只关心语法和表达是否自然，不需要考虑答案对错或业务规则。
- 错误选项（干扰项）在业务上本来就是错的，可读性检查不得因为“选项里的公式/事实是错的”而判为不自然。例如：题干问“正确的计算公式是()”，某选项为“未结算值=实抄数字+结清数字”（错误公式），只要代入后句子通顺、无语病，应判 is_natural=true；不要因该公式在业务上错误而判为不自然。
- “含义残缺”仅指句子本身表述不完整、歧义或语病导致读者看不懂在说什么，不包括“选项内容与教材/常识不符”这类逻辑正确性。
- 重要：index 与 option_label 一一对应（1=选项A, 2=选项B, 3=选项C, 4=选项D）。你返回的每一条必须严格对应该 index 的那一句；reason 里描述的内容必须属于该条目的 option/sentence，不得把其他选项、解析或题目外内容张冠李戴。若引用具体表述，请用引号标出，且只能引用本条候选句中的原文。

# 候选句列表（JSON 数组）
{json.dumps(candidate_sentences, ensure_ascii=False)}

# 输出要求（必须是单个 JSON 对象）
{{
  "per_sentence": [
    {{"index": 1, "option_label": "A", "is_natural": true, "reason": "一句话说明是否自然、如果不自然说明哪里别扭（仅限语法/搭配/断句问题）；必须针对本 index 对应选项的内容"}}
  ],
  "overall_ok": true
}}
- 每条必须包含与输入一致的 index 和 option_label，且 is_natural/reason 只针对该条对应的那一句。
- is_natural 为 false 仅在存在明显语法错误、搭配错误、断句异常或表述残缺（非逻辑错误）时使用。
- overall_ok 为 false 当存在任意一句 is_natural = false 且该问题可能影响考生理解或造成误解时。
"""
            readability_response, _, readability_record = call_llm(
                node_name="critic.readability",
                prompt=readability_prompt,
                model_name=critic_model,
                api_key=critic_api_key or CRITIC_API_KEY,
                base_url=critic_base_url or CRITIC_BASE_URL,
                provider=critic_provider or CRITIC_PROVIDER,
                trace_id=state.get("trace_id"),
                question_id=state.get("question_id"),
                temperature=0.1,
                max_tokens=800,
            )
            llm_records.append(readability_record)
            parsed_readability = parse_json_from_response(readability_response)
            per_list = parsed_readability.get("per_sentence") or []
            overall_ok = bool(parsed_readability.get("overall_ok", True))
            bad_items = [item for item in per_list if not bool(item.get("is_natural", True))]
            # Drop items whose reason cites content not in the corresponding candidate (no 张冠李戴/hallucination)
            bad_items = [
                item for item in bad_items
                if _readability_reason_grounded_in_candidate(item, candidate_sentences)
            ]
            if bad_items:
                def _opt_label(item: Dict[str, Any]) -> str:
                    label = item.get("option_label")
                    if not label and isinstance(item.get("index"), (int, float)):
                        idx = int(item.get("index", 1))
                        label = chr(ord("A") + idx - 1) if 1 <= idx <= 26 else str(idx)
                    return f"选项{label or item.get('index', '?')}"

                bad_desc = "; ".join(
                    [
                        f"{_opt_label(item)}: {str(item.get('reason') or '读起来不自然').strip()}"
                        for item in bad_items
                    ]
                )
                reason = f"题干与选项组合读起来不自然：{bad_desc}"
                severity = "major" if not overall_ok else "minor"
                return {
                    "critic_feedback": "FAIL",
                    "critic_rules_context": full_rules_text,
                    "critic_related_rules": related_rules,
                    "critic_result": {
                        "passed": False,
                        "issue_type": severity,
                        "reason": reason,
                        "fix_strategy": "fix_question",
                        "fail_types": ["readability_fail"],
                    },
                    "critic_details": reason,
                    "critic_model_used": critic_model,
                    "retry_count": state.get("retry_count", 0) + 1,
                    "llm_trace": llm_records,
                    "logs": [f"{log_prefix} ❌ {reason} → 进入修复"],
                }
        except Exception as e:
            # 可读性审计失败不应阻断整体 Critic 流程，仅记录日志
            print(f"⚠️ Critic 可读性检查失败: {e}")
    
    # Create a blind copy of the question (remove answer and explanation)
    blind_question = {k: v for k, v in final_json.items() if k not in ['正确答案', '解析', 'answer', 'explanation']}
    
    # --- Critic Code Generation Step ---
    # 1. Decide if calculation is needed to verify this question, and generate Python code
    prompt_plan = f"""
# 角色
你是批评家 (Critic)。
你需要验证以下题目是否正确。请分析【题目】和【参考材料】，判断是否需要进行数值计算来验证答案。

# 计算验证约束（必须遵守）
1. **时间/日期题必须用 datetime 精确到天**：禁止用年份直接相减。
2. **先锚定政策阈值**：在代码前用常量声明，例如 `REQUIRED_YEARS = 2`。
3. **注释仅说明变量含义**：严禁在注释里辩论或解释“为什么”。
4. **验证逻辑体现在 if/else 或比较表达式**。

# 重要提示：参数提取和计算步骤分析
**计算可能只是解决整个问题的一个步骤，而不是整个问题！**

在验证题目时，请仔细分析：
1. **题目问的是什么？**（最终答案是什么）
2. **需要计算什么？**（能解决哪个步骤）
3. **如何从题目中提取参数？**（题干和选项中可能包含计算所需的数据）

**参数提取规则：**
- 必须从题目中提取**具体的数值**（如：80平方米、1560元、2025年、1993年）
- **不能使用描述性文字**（如："成本价"、"建筑面积"、"建成年代"）
- 如果题目中没有明确数值，需要根据参考材料推断合理的数值
- 注意单位的统一（平方米、元、年等）

**计算步骤分析：**
- 如果题目问的是最终结果，可能需要多步计算
- 计算可能只解决其中一个步骤
- 需要验证：计算结果 + 其他步骤 = 题目答案

例如：
- 题目问"土地出让金是多少"，如果题干给出"建筑面积80平方米，成本价1560元/平方米"
  → 生成代码：`result = 80 * 1560 * 0.01`
  
- 题目问"最长贷款年限是多少"，题干给出"建成年代1993年，当前2025年"
  → 先计算房龄：`house_age = 2025 - 1993`
  → 再根据"房龄+贷款年限≤50年"计算：`max_loan_years = 50 - house_age`
  → 可能还需要考虑借款人年龄等其他因素

# 题目
{json.dumps(blind_question, ensure_ascii=False)}

{CALCULATION_GUIDE}
{CALC_PARAMETER_GROUNDING_GUIDE}

# 参考材料
{kb_context}

# 任务
如果需要计算，返回 JSON: {{"need_calculation": true, "python_code": "result = ..."}}
如果不需要计算，返回 {{"need_calculation": false, "python_code": null}}
"""
    # Use code generation model (qwen3-coder-plus) for code generation in critic
    # When verifying calculation questions, use specialized code generation model
    use_code_gen_model = agent_name in ['CalculatorAgent', 'FinanceAgent']
    
    if use_code_gen_model:
        # Use code generation model for better code generation
        plan_model = CODE_GEN_MODEL
        plan_api_key = CODE_GEN_API_KEY or critic_api_key
        plan_base_url = CODE_GEN_BASE_URL or critic_base_url
        plan_provider = resolve_code_gen_provider(plan_model, CODE_GEN_PROVIDER, None)
    else:
        # Use regular critic model for non-calculation questions
        plan_model = critic_model
        plan_api_key = critic_api_key
        plan_base_url = critic_base_url
        plan_provider = critic_provider
    
    print(f"🔍 Critic Step 1: 开始调用 LLM 生成验证计划（模型: {plan_model}）")
    plan_content, _, llm_record = call_llm(
        node_name="critic.plan",
        prompt=prompt_plan,
        model_name=plan_model,
        api_key=plan_api_key,
        base_url=plan_base_url,
        provider=plan_provider,
        trace_id=state.get("trace_id"),
        question_id=state.get("question_id"),
    )
    llm_records.append(llm_record)
    print(f"🔍 Critic Step 1: 验证计划生成完成")
    # Normalize potential list responses to string
    if isinstance(plan_content, list):
        plan_content = "\n".join([str(item) for item in plan_content if item is not None])
    elif plan_content is not None and not isinstance(plan_content, str):
        plan_content = str(plan_content)
    
    calc_result = None
    tool_used = "None"
    tool_params = {}
    code_check_passed = True
    code_check_reason = ""
    calc_code_warning = ""
    
    # ✅ 检查空响应
    if not plan_content or not plan_content.strip():
        print(f"DEBUG CRITIC PLAN ERROR: Empty response from LLM")
        # 如果 LLM 返回空响应，优先从 calculator_node 的结果中获取
        if agent_name in ['CalculatorAgent', 'FinanceAgent']:
            execution_result = state.get('execution_result')
            tool_usage = state.get('tool_usage', {})
            
            # 优先使用 execution_result (来自 calculator_node 的执行结果)
            if execution_result is not None:
                calc_result = execution_result
                tool_used = tool_usage.get('method', 'dynamic_code_generation')
                tool_params = tool_usage.get('extracted_params', {})
                print(f"DEBUG CRITIC: 使用 calculator_node 的执行结果: {calc_result}")
            # 其次尝试从 tool_usage 中获取
            elif tool_usage.get('result') is not None:
                calc_result = tool_usage['result']
                tool_used = tool_usage.get('method', 'dynamic_code_generation')
                tool_params = tool_usage.get('extracted_params', {})
                print(f"DEBUG CRITIC: 使用 calculator_node 的 tool_usage 结果: {calc_result}")
            # 最后尝试执行生成的计算代码
            else:
                generated_code = state.get('generated_code')
                if generated_code:
                    try:
                        result_value, stdout_str, stderr_str = execute_python_code(generated_code)
                        if result_value is not None:
                            calc_result = result_value
                            tool_used = "generated_code"
                            print(f"DEBUG CRITIC: 使用 calculator_node 生成的计算代码，结果={calc_result}")
                        elif stderr_str:
                            print(f"DEBUG CRITIC: 执行生成代码失败: {stderr_str}")
                    except Exception as e:
                        print(f"DEBUG CRITIC: 执行生成代码失败: {e}")
    
    try:
        if plan_content and plan_content.strip():
            plan = parse_json_from_response(plan_content)
            
            # Check if calculation is needed
            if plan.get("need_calculation") and plan.get("python_code"):
                generated_code = plan.get("python_code", "").strip()
                if generated_code:
                    code_check_prompt = f"""
# 角色
你是严厉的审计人 (Critic)。请检查【计算代码】是否严格符合【教材规则】与【题干条件】。

# 教材规则
{kb_context}

# 题目
{json.dumps(blind_question, ensure_ascii=False)}

# 计算代码
{generated_code}

# 要求
1. 判断代码是否严格遵守教材公式与判定条件。
2. 若不符合，指出关键错误点（例如漏判定条件、用错计税基础、用错阈值）。

# 输出 JSON
{{
  "code_valid": true/false,
  "code_reason": "不超过80字，说明是否符合规则"
}}
"""
                    try:
                        code_check_text, _, llm_record = call_llm(
                            node_name="critic.code_check",
                            prompt=code_check_prompt,
                            model_name=critic_model,
                            api_key=critic_api_key,
                            base_url=critic_base_url,
                            provider=critic_provider,
                            trace_id=state.get("trace_id"),
                            question_id=state.get("question_id"),
                        )
                        llm_records.append(llm_record)
                        code_check = parse_json_from_response(code_check_text)
                        code_check_passed = bool(code_check.get("code_valid", True))
                        code_check_reason = str(code_check.get("code_reason", "")).strip()
                    except Exception as e:
                        code_check_passed = True
                        code_check_reason = f"代码校验解析失败: {e}"
                
                # If LLM didn't generate code but needs calculation, try to regenerate with code generation model
                if not generated_code:
                    # Re-generate code using code generation model
                    print(f"DEBUG CRITIC: LLM didn't generate code, using code generation model to regenerate...")
                    code_gen_response, _, llm_record = call_llm(
                        node_name="critic.codegen_retry",
                        prompt=prompt_plan + "\n\n请重新分析并生成Python代码。",
                        model_name=CODE_GEN_MODEL,
                        api_key=CODE_GEN_API_KEY,
                        base_url=CODE_GEN_BASE_URL,
                        provider=resolve_code_gen_provider(CODE_GEN_MODEL, CODE_GEN_PROVIDER, None),
                        trace_id=state.get("trace_id"),
                        question_id=state.get("question_id"),
                    )
                    llm_records.append(llm_record)
                    try:
                        code_plan = parse_json_from_response(code_gen_response)
                        generated_code = code_plan.get("python_code", "").strip()
                    except Exception as e:
                        print(f"DEBUG CRITIC: Failed to regenerate code: {e}")
                
                if generated_code and code_check_passed:
                    # Execute the generated Python code
                    result_value, stdout_str, stderr_str = execute_python_code(generated_code)
                elif generated_code and not code_check_passed:
                    result_value, stdout_str, stderr_str = None, "", ""
                    tool_used = "code_validation_failed"
                    tool_params = {"code": generated_code}
                
                if stderr_str:
                    calc_result = f"Execution Error: {stderr_str}"
                    tool_used = "error"
                    print(f"DEBUG CRITIC CODE EXECUTION ERROR: {stderr_str}")
                elif result_value is not None:
                    calc_result = result_value
                    tool_used = "generated_code"
                    tool_params = {"code": generated_code}
                    print(f"DEBUG CRITIC: 成功执行动态生成的代码，结果={calc_result}")
                else:
                    calc_result = stdout_str.strip() if stdout_str.strip() else None
                    tool_used = "generated_code"
                    tool_params = {"code": generated_code}
            else:
                # No calculation needed
                tool_used = "None"
                calc_result = None
    except json.JSONDecodeError as je:
        print(f"DEBUG CRITIC PLAN JSON ERROR: {je}")
        # 如果 JSON 解析失败，尝试从 calculator_node 的结果中获取
        if agent_name in ['CalculatorAgent', 'FinanceAgent']:
            execution_result = state.get('execution_result')
            if execution_result is not None:
                calc_result = execution_result
                tool_used = "from_calculator_node"
                print(f"DEBUG CRITIC: 使用 calculator_node 的执行结果: {calc_result}")
    except Exception as e:
        print(f"DEBUG CRITIC PLAN ERROR: {e}")
        # 如果出现其他错误，尝试从 calculator_node 的结果中获取
        if agent_name in ['CalculatorAgent', 'FinanceAgent']:
            execution_result = state.get('execution_result')
            if execution_result is not None:
                calc_result = execution_result
                tool_used = "from_calculator_node"
                print(f"DEBUG CRITIC: 使用 calculator_node 的执行结果: {calc_result}")

    if (
        not code_check_passed
        and state.get("agent_name") == "CalculatorAgent"
        and state.get("code_status") in ("success", "success_no_result")
        and _calc_result_grounded_in_output(calc_result, final_json)
    ):
        calc_code_warning = (
            f"代码校验LLM给出负面结论，但动态执行结果 {calc_result} 已被题干/解析一致引用，"
            f"本轮降级为警告。原始提示: {code_check_reason}"
        )
        print(f"⚠️ Critic 代码校验已降级为警告: {calc_code_warning}")
        code_check_passed = True

    # --- Verification Step: 信息不对称校验 + 反向解题 ---
    options_text = (
        f"A.{final_json.get('选项1', '')} B.{final_json.get('选项2', '')}"
        if question_type == "判断题"
        else f"A.{final_json.get('选项1', '')} B.{final_json.get('选项2', '')} C.{final_json.get('选项3', '')} D.{final_json.get('选项4', '')}"
    )
    raw_difficulty = final_json.get("难度值", 0.5)
    try:
        difficulty_value = float(raw_difficulty)
    except Exception:
        difficulty_value = 0.5
    if difficulty_value <= 0.5:
        difficulty_level = "低"
    elif difficulty_value >= 0.7:
        difficulty_level = "高"
    else:
        difficulty_level = "中"

    writer_format_issues = state.get("writer_format_issues") or []
    writer_issue_text = ""
    if writer_format_issues:
        writer_issue_text = " / ".join([str(x) for x in writer_format_issues if x])

    critic_format_issues = validate_critic_format(final_json, question_type)
    term_lock_issues = detect_term_lock_violations(term_locks, final_json)
    if term_lock_issues:
        critic_format_issues.extend(term_lock_issues)
    # Explanation three-part structure (三段论) from hard_rules: merge into format issues so Critic evaluates it
    _q = str(final_json.get("题干") or "")
    _opts = [
        str(final_json.get(k) or "").strip()
        for k in ["选项1", "选项2", "选项3", "选项4", "选项5", "选项6", "选项7", "选项8"]
        if str(final_json.get(k) or "").strip()
    ]
    _exp = str(final_json.get("解析") or "")
    _ans = str(final_json.get("正确答案") or "").strip()
    _is_calc = state.get("agent_name") == "CalculatorAgent"
    try:
        hard_rule_issues = validate_hard_rules(
            _q,
            _opts,
            _exp,
            kb_context=kb_context,
            target_type=question_type,
            answer=_ans,
            is_calculation=_is_calc,
        )
        for hi in hard_rule_issues:
            if hi.get("field") == "explanation" or (str(hi.get("issue_code") or "").startswith("HARD_EXPL")):
                msg = str(hi.get("message") or "").strip()
                if msg:
                    critic_format_issues.append(msg)
    except Exception as e:
        print(f"⚠️ Critic 解析三段式硬规则校验失败: {e}")
    # 年份约束：切片无年份时禁止题干/选项/解析出现公历年份
    kb_text = kb_context if isinstance(kb_context, str) else str(kb_context)
    if not _has_year(kb_text):
        year_violations = [t for t in _collect_text_fields(final_json) if _has_year(t)]
        if year_violations:
            critic_format_issues.append("题干/选项/解析出现公历年份（原文未提及）")
    critic_format_text = " / ".join([str(x) for x in critic_format_issues if x]) if critic_format_issues else ""
    prompt = f"""
你是【严厉的审计人（Critic）】，不是教师、不是解释者、不是建议者。
**重要**：即使发现格式问题，也必须继续完成所有检查并输出完整问题清单，不得只返回格式问题。

你的目标只有一个：
【判断该题是否可以直接进入正式题库】。

⚠️ 审计裁决铁律：
- 只要命中任意“Fail 条件”，必须判定为【审计不通过】。
- 不允许进行“整体权衡”“酌情放行”“大体正确”的判断。
- 即使最终数值正确，只要推导路径、条件或解析存在问题，也必须 Fail。


# 全量教材规则（你拥有的完整信息）
{full_rules_text}

# 计算辅助
计算结果: {calc_result} (仅供参考)

# 待审核题目
题型: {question_type}
难度: {difficulty_value:.2f}（{difficulty_level}）
题干: {final_json['题干']}
选项: {options_text}

# 生成者声称的答案 (Proposed Answer)
{final_json.get('正确答案', '未知')}

# 生成者提供的解析
{final_json.get('解析', '（无解析）')}

{f"# Writer 格式自检结果（仅供参考）\\n{writer_issue_text}\\n" if writer_issue_text else ""}
{f"# Critic 代码格式校验结果（必须纳入汇总）\\n{critic_format_text}\\n" if critic_format_text else ""}

**注意**: 虽然你能看到生成者的答案，但请先**掩盖它**，进行独立推导，最后再比对。

# 核心审计任务 (Audit Tasks) ⚠️

## 0. 适纲性 / 对工作有帮助 / 导向性
- **适纲性**: 命题内容必须来自当前知识切片或本教材切片，不得超纲出题。
- **对经纪人工作有帮助**: 题目应对经纪人工作有正向作用（可为实操判断/流程/风险，也可为理解规则、合规、公司文化等）。公司制度/合规红线/禁止性规定/时效阈值/标准口径/企业文化与价值观口径等需要记忆并执行的知识点，允许直接命题，不得仅因“偏记忆”判 Fail。**禁止**：仅考数量/年代等脱离业务语义的死记硬背点；仅考概念归类/概念辨析、对工作无指导的题（见下）；仅考教材措辞“核心/主要/关键”的刁钻题。
- **导向性**: 试题应有引导和启发作用，帮助经纪人理解公司文化、熟悉新业务、热爱行业。
- **Fail条件**:
  - 题目超出当前知识切片或教材范围（超纲）。
  - 题目仅考察数量/年代等脱离业务语义的纯记忆点，对工作无帮助（但公司制度/红线/禁止性规定/时效阈值/标准口径/企业文化与价值观口径等要求记忆执行的知识点不在此限）。
  - **仅考「定义 vs 目的 vs 方式/形式」等概念辨析**：若考点只是把教材里的“定义”“目的”“方式”做归类区分，选对答案对经纪人工作没有直接帮助（如问“组织集中空看的主要目的是？”正确项仅因教材写的是“目的”、错误项是“定义”或“方式”），则判为对工作无帮助，quality_check_passed=false，fix_reason 建议改为对工作有指导意义的考法（如给定场景判断是否该做、或步骤/注意点）。
  - **仅考「教材把哪一条称为核心/主要/关键」的刁钻题**：若题干问“核心基础/主要目的/关键环节”等且实务上多个选项都重要、选对只能靠记教材标签（如“房客匹配的核心基础是？”仅 A 正确、B 在实务也重要但被排除），则判为对工作无帮助，quality_check_passed=false，fix_reason 建议改为对工作有区分的考法。
  - **常识与切片表述易冲突**：若考点在常识理解上与切片原文容易产生偏差（如常人认为“新建”=未交易过，而教材有专门口径如“再次上市即属二手房”），导致考生按常识易选错或觉得题目/教材没写清楚，则判为不合格，quality_check_passed=false，fix_reason 建议删除或改为在题干/解析中明确限定教材口径并说明与日常用语区别。
  - **流程/步骤类：主体或视角歧义**：若切片中的流程、步骤**未明确每一步的执行主体或视角**（如谁来做、从谁的角度），则不得出因「主体或视角不同会产生歧义」的题目或选项。例如：流程列“A→B→C”但未区分当事人操作与部门操作，则不宜出“最后一步是？”或依赖“当事人角度的最后一步”与“流程顺序最后一步”区分的选项；否则判为不合格，quality_check_passed=false，fix_reason 建议删除或限定题干视角（如明确“按流程顺序”或“买方完成的最后一步”）。
  - **选项与题干条件相悖**：任一选项不得与题干中已明确给出的条件、前提或设定在逻辑上矛盾；若题干已设定某事实成立，选项中不得出现与该事实相悖的表述，否则判为不合格，quality_check_passed=false，fix_reason 建议修改或删除相悖选项。
  - **规则要素缺失或绝对化**：若教材原规则包含触发条件、适用范围、约束主体、作用对象、角色边界、时间/流程时点中的任意关键要素，题干/正确项/解析却遗漏、偷换或改写为无条件绝对命题（把“在X条件下成立”写成“任何情况下都成立”），判为不合格，quality_check_passed=false，fix_reason 建议补全关键要素并重写题干与解析。
  - **正确选项须完整覆盖考点关键要素**：若教材/切片对某概念、流程或规则明确了多个并列要点，正确选项不得只表述其中部分要点而遗漏其他关键要素，否则判为正确项不完整，quality_check_passed=false，fix_reason 建议补全正确项。

## 1. 地理与范围审计 (Geo-Consistency)
- **规则**: 如果教材明确限定了城市（如"北京市"），题干必须严格遵守。
- **Fail条件**: 
  - 教材=北京，题干=上海/深圳/其他具体城市。
  - 教材=北京，题干=无（若规则具特殊性）。
- **新增约束**:
  - 教材未提及具体城市/时间，题干或解析却出现具体城市名或具体年份/日期。
- **特例**: 干扰项中允许出现其他城市作为错误选项，但题干场景和正确答案必须基于教材指定城市。

## 2. 逻辑自洽性审计 (Logic Validity)
- **规则**: 不要机械比对数字，要比对**判定结果**。
- **Fail条件**: 
  - 题目场景中条件（如"不满2年"）推导出的结论与正确答案冲突。
  - **严重错误案例**: 题目说"北京换房退税"，但并未满足"先卖后买"或"1年内"的核心条件，正确答案通过。

## 3. 反向解题（Reverse Solving，最高裁决权）

⚠️ 本维度拥有最高裁决优先级，高于所有其他审计维度。

任务：
- 在【完全忽略生成者声称的答案】的前提下，
- 仅基于题干条件 + 教材规则，
- 推导是否能得到【唯一且确定的答案】。

判断题专项规则（必须遵守）：
- 判断题的作答本质是“判断题干表述是否符合教材规则”。只要题干语义清晰、可判真伪，即可视为可反向解题。
- **严禁**因“题干与教材原文一致/高度相似/改写幅度小”“可通过对照教材记忆作答”而将判断题判定为反向解题失败。
- 对公司制度、合规红线、禁止性规定、企业文化与价值观口径等“背诵执行型”判断题，同样适用上述放行规则。

Fail 条件（任一即 Fail）：
- 无法计算（缺关键数值 / 条件）
- 存在两条及以上合理推导路径
- 不同规则分支在题干中未被明确排除
- 需要考生“猜规则”“默认前提”才能算出答案

⚠️ **严禁给出空泛理由**：
- 禁止只因为“教材/切片里有多条相近规则”或“有两个相关切片”就写成“需要学员猜测/信息不足”这一类抽象描述。
- 当你判定“存在多解 / 需要学员猜测”时，必须：
  1. 至少点名 **2 处具体文本**：一处来自教材/切片中的规则原文，一处来自题干或选项中的表述（都要给出原句或关键短语，而不是“规则一/规则二”这种代号）。
  2. 说明这两处文本是如何在含义上产生冲突、互斥或模糊的（例如：一个按建筑面积，一个按套数；一个按“新建”日常含义，一个按教材专门口径）。
  3. 明确写出：在完全只依赖【题干 + 这些切片】的前提下，学员可能会分别依据哪些理解选出哪几个不同答案，从而**无法在不靠主观猜测的情况下锁定唯一答案**。
- 若你无法给出上述“具体文本 + 冲突关系 + 误导路径”三点，请不要使用“需要学员猜测/信息不足”这一类总结性话术，而应改为其它更精确的问题描述（如“题干缺少 ×× 条件”“规则 A/B 的适用范围未在题干中区分”等)。

**注意**：题干已给出规则所需的某数值时，直接使用即可；不得以「缺少可推导或验证该数值的其他信息」为由判缺条件或反向解题失败。

⚠️ 一旦 Reverse Solving 判定失败：
- reverse_solve_success = false
- can_deduce_unique_answer = false
- fix_strategy 至少为 fix_question


## 4. 质量把关 (Quality Control) - 核心拦截项：题目傻瓜化直给答案 (Fail) ⚠️

**【判定核心视点：仅限题干 VS 选项】**
本考试为**闭卷**。AI 审核时请务必注意：考生**看不到**教材原文。本项要拦截的是“题目过分傻瓜化、几乎不用思考和学习教材就能直接选对”。判定依据必须是：仅凭【题干】文字，考生是否可以不调动业务知识、只做文本比对就锁定正确项。**绝对禁止**将“题干与教材原文重合度高”作为判 Fail 的理由。

**【严格触发条件（仅当满足以下情况才判 Fail）】**
- **直给答案效应**：题干中已经**完整包含了正确选项的具体内容**，导致考生完全不需要任何业务知识，仅通过“比对题干文字和选项文字”就能唯一锁定正确答案。
- *Fail 示例*：题干问“网签的作用包含A、B、C，以下关于网签作用说法正确的是？”，而正确选项恰好是“A、B、C”。（题干直接把答案喂到了嘴边）。

**【绝对豁免清单（命中以下任一情况，一律判 Pass，严禁判 Fail！）】**
1. **判断题一律豁免**：判断题的选项仅为“正确/错误”，没有实质性文本。从物理结构上讲，判断题不适用“题干与选项文本直给匹配”这一类 Fail 规则。无论命题真假，无论题干是否直接摘抄教材，**严禁据此对判断题判 Fail**。
2. **考点名称/业务术语豁免**：题干仅给出“概念名称”（如“关于【建筑密度】以下说法正确的是”），或者使用了教材中的专有名词（如“办理商业贷款时”）。只要正确选项的完整定义没有被写在题干里，这就属于正常的知识考查，**严禁判 Fail**。
3. **教材原文一致性豁免**：即使题干的表述与教材原文 100% 一致，只要它没有在题干中直接揭示正确选项的具体内容，就证明需要考生调用大脑记忆来作答。**严禁因“题干和教材长得一样”而判 Fail**。

- **Fail条件 2 (基础质量)**:
  - 题目表述使用了模糊词汇（如"实实在在"）。
  - 选项跨维度（如A法律 B实物 C位置 D价格）。
  - 干扰项过于幼稚，无需专业知识即可排除（所有难度均可判 Fail）。
  - **判断题特例**：判断题只允许两个选项（正确/错误），C/D为空不构成质量问题。
- **Fail条件 3 (AI幻觉/非人话)**:
  - 出现了不符合中国房地产业务习惯的生造词。
  - *典型案例*：使用“外接”代替“买方/受让方”；使用“上交”代替“缴纳”。

## 5. 题干/设问规范审计
- **前提（必须写入提示词）**：仅当题干意思为“判断表述正确与否”或“判断XX做法正确与否”时，才强制下列表述标准；题干意思中没有这些表述的不强制要求。
- **规则**:
  - 题干括号不得在句首，可在句中或句末；选择题句末必须有句号且句号在最后；判断题句子完结后加括号且在最后。
  - 括号必须为中文 `（　）`，括号内有且仅有一个全角空格（不能多），括号前后无空格。
  - 设问使用陈述方式，避免否定句与双重否定；判断题必须是肯定陈述句，并明确出现“正确”或“错误”这一判断锚点；不要写成“是否正确/是不是/对不对”等疑问式。
  - 选择题同样只要求题干为陈述句，并以（　）作答占位结尾（句号在括号后），不强制固定某一种模板化结尾。
- **Fail条件**:
  - 括号格式不符合 `（　）` 或位置/句号不符合要求。
  - 设问为疑问句/双重否定；或判断题缺少“正确/错误”锚点；或选择题题干非陈述句/缺少（　）占位/句号位置不规范。

## 6. 选项规范审计
- **规则**:
  - 选项末尾不加标点；选项与题干合成语义完整。
  - 选项姓名与题干姓名一致，不能出现题目未涉及的姓名。
  - 数值型选项需按从小到大排序，未被选中的数值也必须有计算依据。
  - **选项单位**：选项中有单位时，**必须**将单位提到题干中，**不得**在选项中反复出现单位（如选项写「6万元」「8万元」为违规；应题干写「……为（　）万元」，选项只写 6、8、10、12）。
  - **隐含计算复杂度控制（跨题型）**：即使是非计算题，只要解题依赖运算（比例/折算/阈值比较等），也必须满足简算优先；避免复杂小数与冗长多步计算，不应依赖计算器；若答案含小数，题干需明确保留位数（一般1-2位）。
- **Fail条件**:
  - 选项末尾有标点、姓名不一致、选项无法与题干组成完整语义。
  - 选项包含数值单位（元、万元、平方米、年、%等）而未将单位写在题干中。
  - 数值选项无依据或乱序。
  - 非计算题存在隐含计算但计算复杂度过高（复杂小数/步骤过多/明显依赖计算器），且题干未给出必要的保留位数说明。

## 7. 解析规范审计
- **规则**:
  - 解析需采用三段式：教材原文 + 试题分析 + 结论。
  - 判断题结论写本题答案为正确/错误，不得写成本题答案为A/B。
  - 选择题结论写本题答案为A/B/C/D/AB/AC...。
  - 禁止直接粘贴教材原文表格或图片（可改成文字描述）。
  - 解析必须与答案一致，计算题必须与计算过程一致。
  - **解析可读性**：需单独判断解析是否通顺、有无拗口或歧义；若有问题可列入 quality_issues。不得与题干/选项可读性混淆，不要引用题干或选项中的句子作为解析问题（不要张冠李戴）。
- **Fail条件**:
  - 解析缺少三段式或结论格式不规范。
  - 解析与答案/计算过程不一致。
  - 直接复制表格/图片。

## 8. 典型错题审计
- **Fail条件**:
  - 多字、少字、错字影响作答或造成歧义。
  - 题干与选项/解析前后不一致。
  - 计算题无正确答案或答案与计算过程不一致。
  - 题目超纲或概念过时（如旧业务名/过期协议）。
  - 场景严重脱离经纪业务实际。
  - 干扰选项存在争议或与正确答案同样成立。
  - *整改建议*：修正为标准业务术语。

## 9. 选项逻辑（干扰项审计）
- **Fail条件**:
  - 干扰项是纯粹的随机数字，没有考察到易错点。
  - *优质干扰项标准*：应该是“遗漏了某一步计算”或“误用了另一个税率”得出的错误结果。
  - **难度差异化要求**：
    - 低/中/高难度都必须满足“同维度、似是而非、可解释”的干扰项质量要求；不达标可判 Fail。

    请基于以上标准，输出审核结果。

    # 输出格式 (必须为 JSON 块)
⚠️ JSON 输出强一致性规则（必须遵守）：

- 若 can_deduce_unique_answer = false：
  → reverse_solve_success 必须为 false
  → fix_strategy 不得为 fix_explanation

    - 若 explanation_valid = false：
      → grounding_check_passed 必须为 false

    - 若 quality_check_passed = false：
      → quality_issues 不得为空数组

    - 不允许出现：
      “问题存在但仍判通过”的组合

    - 对解析质量维度，还需输出以下结构化字段（若不适用按合理默认值填写）：
      - 多选题逐项覆盖：`multi_option_coverage_rate`（0.0–1.0，小数保留两位）、`missing_options`（如 `["B","E"]`）
      - 解析首段结构：`first_part_missing_target_title`（第1段缺少目标题内容即路由前三个标题时置 true；不要求解析中出现「目标题：」字样）、`first_part_missing_level`、`first_part_missing_textbook_raw`、`first_part_structured_issues`
      - 解析重写充分性：`analysis_rewrite_sufficient`、`analysis_rewrite_issues`
      - 判据来源：`basis_source` / `basis_paths` / `basis_reason`
        * 若判不通过依据仅来自当前切片，`basis_source` 必须写 `current`
        * 若判不通过依据来自上一级或相似切片，`basis_source` 必须写 `non_current` 或 `mixed`，并在 `basis_paths` 写明对应切片路径
        * 严禁把“当前切片可独立判定”的问题误标为 `non_current`

```json
{{
    "reverse_solve_success": true/false,
    "critic_answer": "A/B/C/D",
    "can_deduce_unique_answer": true/false,
    "missing_conditions": ["遗漏的条件1", "遗漏的条件2"] 或 [],
    "deduction_process": "你的推导过程：1. 提取条件... 2. 匹配规则... 3. 计算结果...",
    "explanation_valid": true/false,
    "grounding_check_passed": true/false,
    "example_conflict": true/false,
    "quality_check_passed": true/false,
    "quality_issues": ["问题1", "问题2"] 或 [],
    "context_strength": "强/中/弱",
    "option_dimension_consistency": true/false,
    "multi_option_coverage_rate": 1.0,
    "missing_options": [],
    "first_part_missing_target_title": true/false,
    "first_part_missing_level": true/false,
    "first_part_missing_textbook_raw": true/false,
    "first_part_structured_issues": [],
    "analysis_rewrite_sufficient": true/false,
    "analysis_rewrite_issues": [],
    "basis_source": "current / non_current / mixed / unknown",
    "basis_paths": ["触发判定时引用的切片路径1", "切片路径2"] 或 [],
    "basis_reason": "一句话说明为何判定依据属于当前或非当前切片",
    "fix_strategy": "fix_explanation / fix_question / fix_both / regenerate",
    "fix_reason": "用一句话给出修复建议（必要时给出要补充的具体条件/选项）",
    "reason": "详细说明审核结论"
}}
"""

    print(f"🔍 Critic Step 2: 开始调用 LLM 执行质量验证（模型: {critic_model}）")
    response_text, used_model, llm_record = call_llm(
        node_name="critic.review",
        prompt=prompt,
        model_name=critic_model,
        api_key=critic_api_key,
        base_url=critic_base_url,
        provider=critic_provider,
        trace_id=state.get("trace_id"),
        question_id=state.get("question_id"),
    )
    llm_records.append(llm_record)
    if not str(response_text or "").strip():
        print("⚠️ Critic.review 返回空响应，使用更保守参数重试一次")
        response_text_retry, used_model_retry, llm_record_retry = call_llm(
            node_name="critic.review.retry",
            prompt=prompt,
            model_name=critic_model,
            api_key=critic_api_key,
            base_url=critic_base_url,
            provider=critic_provider,
            trace_id=state.get("trace_id"),
            question_id=state.get("question_id"),
            temperature=0.1,
            max_tokens=2500,
        )
        llm_records.append(llm_record_retry)
        if str(response_text_retry or "").strip():
            response_text = response_text_retry
            used_model = used_model_retry or used_model
    critic_model_used = used_model or critic_model
    print(f"🔍 Critic Step 2: 质量验证完成，开始解析结果")
    
    # Initialize variables with defaults
    critic_answer = "UNKNOWN"
    explanation_valid = False
    reverse_solve_success = False
    can_deduce_unique_answer = False
    deduction_process = ""
    grounding_check_passed = False
    missing_conditions = []
    example_conflict = False
    quality_check_passed = False
    quality_issues = []
    context_strength = "弱"
    option_dimension_consistency = False
    reason = "Parsing Failed"
    fix_strategy = "fix_both"
    fix_reason = ""
    critic_schema_incomplete = False
    critic_schema_missing_fields: List[str] = []
    # 解析质量细粒度默认值（与离线 Judge 对齐）
    multi_option_coverage_rate = 1.0
    missing_options: List[str] = []
    first_part_missing_target_title = False
    first_part_missing_level = False
    first_part_missing_textbook_raw = False
    first_part_structured_issues: List[str] = []
    analysis_rewrite_sufficient = True
    analysis_rewrite_issues: List[str] = []
    basis_source = "unknown"
    basis_paths: List[str] = []
    basis_reason = ""
    
    try:
        review_result = parse_json_from_response(response_text)
        if not isinstance(review_result, dict):
            raise ValueError("critic.review 输出不是 JSON 对象")

        required_schema_fields = [
            "reverse_solve_success",
            "can_deduce_unique_answer",
            "critic_answer",
            "deduction_process",
            "grounding_check_passed",
            "missing_conditions",
            "quality_check_passed",
            "quality_issues",
            "context_strength",
            "option_dimension_consistency",
            "explanation_valid",
            "reason",
            "fix_strategy",
        ]
        critic_schema_missing_fields = [k for k in required_schema_fields if k not in review_result]
        if critic_schema_missing_fields:
            critic_schema_incomplete = True
        
        # 反向解题结果（核心校验）
        reverse_solve_success = review_result.get("reverse_solve_success", False)
        can_deduce_unique_answer = bool(review_result.get("can_deduce_unique_answer", False))
        deduction_process = str(review_result.get("deduction_process", "") or "")
        
        # 答案一致性
        critic_answer = str(review_result.get("critic_answer", "UNKNOWN") or "UNKNOWN").strip().upper()
        
        # 信息不对称校验
        grounding_check_passed = bool(review_result.get("grounding_check_passed", False))
        missing_conditions = review_result.get("missing_conditions", [])
        if not isinstance(missing_conditions, list):
            missing_conditions = []
        example_conflict = bool(review_result.get("example_conflict", False))
        has_example_refs = bool(state.get("examples")) or bool(kb_chunk.get("结构化内容", {}).get("examples"))
        if not has_example_refs:
            example_conflict = False
        
        # 题目质量检查
        quality_check_passed = bool(review_result.get("quality_check_passed", False))
        quality_issues = review_result.get("quality_issues", [])
        if not isinstance(quality_issues, list):
            quality_issues = []
        context_strength = str(review_result.get("context_strength", "弱") or "弱")
        option_dimension_consistency = bool(review_result.get("option_dimension_consistency", False))
        # 解析质量细粒度字段（新增加，与离线 Judge 对齐）
        try:
            multi_option_coverage_rate = float(review_result.get("multi_option_coverage_rate", 1.0) or 1.0)
        except Exception:
            multi_option_coverage_rate = 1.0
        missing_options = [str(x) for x in (review_result.get("missing_options") or [])]
        first_part_missing_target_title = bool(review_result.get("first_part_missing_target_title", False))
        first_part_missing_level = bool(review_result.get("first_part_missing_level", False))
        first_part_missing_textbook_raw = bool(review_result.get("first_part_missing_textbook_raw", False))
        first_part_structured_issues = [str(x) for x in (review_result.get("first_part_structured_issues") or [])]
        analysis_rewrite_sufficient = bool(review_result.get("analysis_rewrite_sufficient", True))
        analysis_rewrite_issues = [str(x) for x in (review_result.get("analysis_rewrite_issues") or [])]
        
        # ✅ 如果语境强度为"弱"或选项维度不一致，强制判定为质量不合格
        if context_strength == "弱" or not option_dimension_consistency:
            quality_check_passed = False
            if context_strength == "弱":
                quality_issues.append("题干语境模糊，表述不够明确")
            if not option_dimension_consistency:
                quality_issues.append("选项跨多个维度，干扰项设计不合理")
        # ✅ 将代码格式校验结果纳入质量问题（确保汇总输出）
        if critic_format_issues:
            quality_check_passed = False
            for item in critic_format_issues:
                if item and item not in quality_issues:
                    quality_issues.append(item)

        # 多选题解析逐项覆盖率：覆盖不足或缺失选项时，作为质量问题输出
        if question_type == "多选题":
            if multi_option_coverage_rate < 1.0:
                quality_check_passed = False
                msg = f"多选解析逐项覆盖率不足：{multi_option_coverage_rate:.2f}"
                if msg not in quality_issues:
                    quality_issues.append(msg)
                # 覆盖率不足意味着解析不合格
                explanation_valid = False
            if missing_options:
                quality_check_passed = False
                miss_msg = f"多选解析未覆盖选项：{','.join(sorted(set(missing_options)))}"
                if miss_msg not in quality_issues:
                    quality_issues.append(miss_msg)
                explanation_valid = False

        # 解析首段结构三要素：缺任一要素或首段结构问题列表非空时，判为解析结构不合格
        if first_part_missing_target_title:
            quality_check_passed = False
            msg = "解析第1段缺少目标题内容（路由前三个标题）"
            if msg not in quality_issues:
                quality_issues.append(msg)
            explanation_valid = False
        if first_part_missing_level:
            quality_check_passed = False
            msg = "解析第1段缺少“分级（了解/掌握/应用/熟悉）”"
            if msg not in quality_issues:
                quality_issues.append(msg)
            explanation_valid = False
        if first_part_missing_textbook_raw:
            quality_check_passed = False
            msg = "解析第1段缺少“教材原文”内容"
            if msg not in quality_issues:
                quality_issues.append(msg)
            explanation_valid = False
        if first_part_structured_issues:
            quality_check_passed = False
            for item in first_part_structured_issues:
                wrapped = f"第1段结构问题：{str(item)}"
                if wrapped not in quality_issues:
                    quality_issues.append(wrapped)
            explanation_valid = False

        # 解析“必须重写”充分性：不充分时视为重大解析问题
        if not analysis_rewrite_sufficient:
            quality_check_passed = False
            explanation_valid = False
            if analysis_rewrite_issues:
                for item in analysis_rewrite_issues:
                    msg = str(item)
                    if msg and msg not in quality_issues:
                        quality_issues.append(msg)
            else:
                default_msg = "解析第2段未充分转述：术语可保留，但句式与推理表达需重写"
                if default_msg not in quality_issues:
                    quality_issues.append(default_msg)
            # 解析重写不足按严重问题处理
            issue_type = "major"
        
        # 解析审查
        explanation_valid = bool(review_result.get("explanation_valid", False))
        # Code-level gate: if hard_rules detect explanation three-part structure issues, force explanation_valid = False
        try:
            _exp = str(final_json.get("解析") or "")
            if _exp.strip():
                _q = str(final_json.get("题干") or "")
                _opts = [
                    str(final_json.get(k) or "").strip()
                    for k in ["选项1", "选项2", "选项3", "选项4", "选项5", "选项6", "选项7", "选项8"]
                    if str(final_json.get(k) or "").strip()
                ]
                expl_hard = validate_hard_rules(
                    _q,
                    _opts,
                    _exp,
                    kb_context=kb_context,
                    target_type=question_type,
                    answer=str(final_json.get("正确答案") or "").strip(),
                    is_calculation=(state.get("agent_name") == "CalculatorAgent"),
                )
                for hi in expl_hard:
                    if hi.get("field") == "explanation" or (str(hi.get("issue_code") or "").startswith("HARD_EXPL")):
                        explanation_valid = False
                        break
        except Exception:
            pass

        reason = review_result.get("reason", "")
        fix_strategy = review_result.get("fix_strategy", "fix_both")
        fix_reason = review_result.get("fix_reason", "")
        basis_source = str(review_result.get("basis_source", "unknown") or "unknown").strip().lower()
        basis_paths = [str(x).strip() for x in (review_result.get("basis_paths") or []) if str(x).strip()]
        basis_reason = str(review_result.get("basis_reason", "") or "").strip()

        # 如果 can_deduce_unique_answer 为 False，强制 reverse_solve_success = False
        if not can_deduce_unique_answer:
            reverse_solve_success = False

        if critic_schema_incomplete:
            grounding_check_passed = False
            quality_check_passed = False
            reverse_solve_success = False
            can_deduce_unique_answer = False
            explanation_valid = False
            issue_type = "major"
            missing_str = ", ".join(critic_schema_missing_fields)
            schema_msg = f"critic审计JSON缺少关键字段: {missing_str}"
            if schema_msg not in quality_issues:
                quality_issues.append(schema_msg)
            reason = schema_msg
            fix_strategy = "regenerate"
            if not fix_reason:
                fix_reason = "审计输出结构不完整，无法可靠验收"
    except Exception as e:
        print(f"DEBUG CRITIC PARSE ERROR: {e}")
        # Fallback: try to find answer in text if JSON fails
        import re
        match = re.search(r'[ABCD]', response_text)
        if match:
            critic_answer = match.group(0)
        
        # Set defaults for missing fields
        reverse_solve_success = False
        can_deduce_unique_answer = False
        deduction_process = ""
        grounding_check_passed = False
        missing_conditions = []
        example_conflict = False
        quality_check_passed = False
        quality_issues = ["JSON解析失败"]
        context_strength = "弱"
        option_dimension_consistency = False
        explanation_valid = False
        multi_option_coverage_rate = 1.0
        missing_options = []
        first_part_missing_target_title = False
        first_part_missing_level = False
        first_part_missing_textbook_raw = False
        first_part_structured_issues = []
        analysis_rewrite_sufficient = False
        analysis_rewrite_issues = ["解析 JSON 结构失败，无法检查解析质量细节"]
        reason = f"JSON解析失败: {str(e)}"
        fix_strategy = "regenerate"
        fix_reason = "审计输出解析失败"
        basis_source = "unknown"
        basis_paths = []
        basis_reason = ""
        if (
            state.get("agent_name") == "CalculatorAgent"
            and state.get("code_status") in ("success", "success_no_result")
            and code_check_passed
            and _calc_result_grounded_in_output(calc_result, final_json)
            and _judgment_answer_consistent(final_json)
        ):
            reverse_solve_success = True
            can_deduce_unique_answer = True
            grounding_check_passed = True
            quality_check_passed = True
            quality_issues = []
            context_strength = "中"
            option_dimension_consistency = True
            explanation_valid = True
            critic_answer = str(final_json.get("正确答案", "") or "").strip().upper() or "UNKNOWN"
            reason = "审计输出解析失败，但计算链确定性校验已通过"
            fix_strategy = "fix_explanation"
            fix_reason = "审计输出解析失败，已走确定性兜底"

    current_slice_path = str(kb_chunk.get("完整路径", "") or "").strip()
    related_slice_paths = set()
    for chunk in (parent_slices + related_slices):
        p = str(chunk.get("完整路径", "") or "").strip()
        if p and p != current_slice_path:
            related_slice_paths.add(p)
    explicit_non_current_paths = [p for p in basis_paths if p and p != current_slice_path]
    if basis_source in {"non_current", "mixed"}:
        non_current_slice_basis = True
    elif basis_source == "current":
        non_current_slice_basis = False
    else:
        non_current_slice_basis = any(p in related_slice_paths for p in explicit_non_current_paths)

    # 通用证据绑定契约：审计结论必须绑定当前切片依据，避免“来源漂移”
    has_current_basis = (
        basis_source == "current"
        or current_slice_path in set(basis_paths)
        or basis_source == "mixed"
    )
    if not has_current_basis:
        grounding_check_passed = False
        if "审计依据未绑定当前切片" not in quality_issues:
            quality_issues.append("审计依据未绑定当前切片")
        if fix_strategy == "fix_explanation":
            fix_strategy = "fix_both"
        if not fix_reason:
            fix_reason = "请显式绑定当前切片作为判定依据"

    gen_answer = final_json['正确答案'].strip().upper()
    question_text = final_json.get("题干", "")

    # Writer deterministic issues are blocking: Critic pass gate must honor them.
    writer_issue_objs = writer_validation_report.get("issues") if isinstance(writer_validation_report, dict) else []
    blocking_writer_issues: List[str] = []
    if isinstance(writer_issue_objs, list):
        for it in writer_issue_objs:
            if not isinstance(it, dict):
                continue
            severity = str(it.get("severity", "error")).lower()
            code = str(it.get("issue_code", "")).strip()
            msg = str(it.get("message", "")).strip()
            if severity == "error" or code.startswith("HARD_") or code in {"NAME_STYLE", "FORMAT_ISSUE"}:
                blocking_writer_issues.append(msg or code or "writer_issue")
    if blocking_writer_issues:
        quality_check_passed = False
        for m in blocking_writer_issues:
            mark = f"Writer硬校验未通过: {m}"
            if mark not in quality_issues:
                quality_issues.append(mark)
        if not fix_reason:
            fix_reason = "Writer硬校验存在阻断项，需先修复命名/格式等基础问题"
        if fix_strategy == "fix_explanation":
            fix_strategy = "fix_both"

    critic_tool_usage = {
        "tool": tool_used,
        "params": tool_params,
        "result": calc_result
    }

    # Near-duplicate guard: avoid generating questions that already exist in history
    if retriever:
        is_dup, dup_score, dup_text = retriever.is_similar_to_history(question_text, threshold=0.9)
        if is_dup:
            return {
                "critic_feedback": "FAIL",
                "critic_details": f"疑似重复题干，相似度 {dup_score:.2f}，已存在题目: {dup_text[:120]}",
                "critic_tool_usage": critic_tool_usage,
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "reason": "高相似度重复题目",
                    "fail_types": ["duplicate_stem"],
                    "basis_source": basis_source,
                    "basis_paths": basis_paths,
                    "basis_reason": basis_reason,
                    "non_current_slice_basis": non_current_slice_basis,
                },
                "critic_basis_source": basis_source,
                "critic_basis_paths": basis_paths,
                "critic_non_current_basis": non_current_slice_basis,
                "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
                "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
                "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
                "critic_model_used": critic_model,
                "final_json": None,
                "retry_count": state.get("retry_count", 0) + 1,
                "llm_trace": llm_records,
                "logs": [f"🛑 {log_prefix} 发现高相似度题目（{dup_score:.2f}），已丢弃以避免重复出题。"]
            }

    # Pass Condition: 核心是"反向解题成功"（能推导出唯一答案）
    # 1. 反向解题校验（核心）
    fail_reason = ""
    reverse_fail = False
    answer_mismatch = False
    grounding_fail = False
    difficulty_out_of_range = False
    explanation_fail = False
    issue_type = "minor"  # 默认轻微问题
    term_lock_fail = bool(term_lock_issues)
    
    if not reverse_solve_success or not can_deduce_unique_answer:
        reverse_fail = True
        fail_reason += f"反向解题失败：无法根据题目条件推导出唯一答案; "
        if missing_conditions:
            fail_reason += f"遗漏条件: {', '.join(missing_conditions)}; "
        if deduction_process:
            fail_reason += f"推导过程: {deduction_process[:100]}...; "
        issue_type = "major"  # 无法推导唯一答案是严重问题
    
    # 2. 答案一致性验证
    if critic_answer != gen_answer and critic_answer != "UNKNOWN":
        answer_mismatch = True
        fail_reason += f"答案不一致 (审计人推导: {critic_answer} vs 生成者: {gen_answer}); "
        issue_type = "major"  # 答案错误是严重问题
    
    # 3. 信息不对称校验
    if not grounding_check_passed:
        grounding_fail = True
        if missing_conditions:
            fail_reason += f"遗漏判定条件: {', '.join(missing_conditions)}; "
        if example_conflict:
            fail_reason += f"误带入母题中的陈旧逻辑或错误数据; "
        issue_type = "major"
    
    # 4. 难度范围验证
    if difficulty_range:
        min_diff, max_diff = difficulty_range
        current_difficulty = final_json.get('难度值', 0.5)
        try:
            current_difficulty = float(current_difficulty)
        except:
            current_difficulty = 0.5
        
        if current_difficulty < min_diff or current_difficulty > max_diff:
            difficulty_out_of_range = True
            fail_reason += f"难度值 {current_difficulty:.2f} 不在指定范围内 ({min_diff:.1f}-{max_diff:.1f}); "
            issue_type = "major"  # 难度不符合要求是严重问题
    
    # 5. 题目质量检查
    if not quality_check_passed:
        fail_reason += f"题目质量不合格; "
        if quality_issues:
            fail_reason += f"质量问题: {', '.join(quality_issues)}; "
        
        # ✅ 语境模糊或维度不一致是严重问题，必须重新生成
        if context_strength == "弱" or not option_dimension_consistency:
            issue_type = "major"
            if context_strength == "弱":
                fail_reason += f"【严重】题干语境模糊，表述不够明确，可能导致歧义; "
            if not option_dimension_consistency:
                fail_reason += f"【严重】选项跨多个维度，干扰项设计不合理，无法真正考察专业知识; "
        else:
            # 仅格式类问题→轻微；其他质量问题默认轻微（可修复）
            if issue_type != "major":
                if critic_format_issues and all(i in critic_format_issues for i in quality_issues):
                    issue_type = "minor"
                else:
                    issue_type = "minor"
    if term_lock_fail:
        fail_reason += f"专有名词锁词违规: {'; '.join(term_lock_issues)}; "
        issue_type = "major"
    
    # 5. 计算代码校验
    if not code_check_passed:
        fail_reason += f"计算代码不符合教材规则 ({code_check_reason}); "
        issue_type = "major"

    # 5. 解析审查
    if not explanation_valid:
        explanation_fail = True
        fail_reason += f"解析不合格 ({reason}); "
        # 解析问题通常可以修复，保持 minor

    # Decide fix strategy when failing (按优先级决策)
    # 优先级1：反向解题失败 → 必须修复题目（题干条件不足或有歧义）
    if not reverse_solve_success or not can_deduce_unique_answer:
        if critic_answer != gen_answer and critic_answer != "UNKNOWN":
            fix_strategy = "fix_both"
            if not fix_reason:
                fix_reason = "反向解题失败且答案不一致，需同时调整题干/选项/答案与解析"
        else:
            fix_strategy = "fix_question"
            if not fix_reason:
                fix_reason = "反向解题失败（无法推导唯一答案），需补充题干条件或调整选项"
    # 优先级2：答案不一致（但反向解题成功）
    elif critic_answer != gen_answer and critic_answer != "UNKNOWN":
        fix_strategy = "fix_both"
        if not fix_reason:
            fix_reason = "答案与解析不一致或答案错误，需同时调整题干/选项/答案与解析"
    # 优先级3：仅解析无效（题目本身可解）
    elif not explanation_valid:
        fix_strategy = "fix_explanation"
        if not fix_reason:
            fix_reason = "解析错误或未能支撑答案，仅修正解析"
    # 优先级4：质量或依据问题
    elif not quality_check_passed or not grounding_check_passed:
        fix_strategy = "regenerate"
        if not fix_reason:
            fix_reason = "题目质量或依据不足，建议重写题目"
    
    # 通过条件：反向解题成功 + 答案一致 + 解析合理 + 信息完整 + 题目质量合格
    if (reverse_solve_success and can_deduce_unique_answer and 
        critic_answer == gen_answer and 
        explanation_valid and 
        grounding_check_passed and
        quality_check_passed):
        critic_payload = {
            "critic_feedback": "PASS", 
            "critic_details": f"✅ 审核通过 (反向解题成功，能推导出唯一答案: {critic_answer})",
            "critic_tool_usage": critic_tool_usage,
            "critic_result": {
                "passed": True,
                "deduction_process": deduction_process,
                "multi_option_coverage_rate": multi_option_coverage_rate,
                "missing_options": missing_options,
                "first_part_missing_target_title": first_part_missing_target_title,
                "first_part_missing_level": first_part_missing_level,
                "first_part_missing_textbook_raw": first_part_missing_textbook_raw,
                "first_part_structured_issues": first_part_structured_issues,
                "analysis_rewrite_sufficient": analysis_rewrite_sufficient,
                "analysis_rewrite_issues": analysis_rewrite_issues,
                "basis_source": basis_source,
                "basis_paths": basis_paths,
                "basis_reason": basis_reason,
                "non_current_slice_basis": non_current_slice_basis,
            },
            "critic_basis_source": basis_source,
            "critic_basis_paths": basis_paths,
            "critic_non_current_basis": non_current_slice_basis,
            "critic_format_issues": critic_format_issues,
            "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
            "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
            "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
            "critic_model_used": critic_model_used,
            "llm_trace": llm_records,
            "logs": [f"{log_prefix} 审核通过（反向解题成功，能推导出唯一答案）"]
        }
        print(f"DEBUG CRITIC RESULT: {critic_payload['critic_result']}")
        return critic_payload
    else:
        required_fixes = []
        all_issues = []
        writer_issues = writer_validation_report.get("issues") if isinstance(writer_validation_report, dict) else []
        if isinstance(writer_issues, list):
            for item in writer_issues:
                if not isinstance(item, dict):
                    continue
                issue_code = str(item.get("issue_code", "")).strip()
                if issue_code:
                    all_issues.append(f"writer:{issue_code}")
                    required_fixes.append(f"writer:{issue_code}")
        if writer_retry_exhausted:
            all_issues.append("writer:retry_exhausted")
            required_fixes.append("writer:retry_exhausted")
        if critic_format_issues:
            for item in critic_format_issues:
                required_fixes.append(f"format:{item}")
                all_issues.append(f"format:{item}")
        if not reverse_solve_success or not can_deduce_unique_answer:
            required_fixes.append("logic:cannot_deduce_unique_answer")
            all_issues.append("logic:cannot_deduce_unique_answer")
        if critic_answer != gen_answer and critic_answer != "UNKNOWN":
            required_fixes.append("logic:answer_mismatch")
            all_issues.append("logic:answer_mismatch")
        if missing_conditions:
            for mc in missing_conditions:
                all_issues.append(f"logic:missing_condition:{mc}")
            required_fixes.append("logic:missing_conditions")
        if example_conflict:
            required_fixes.append("logic:example_conflict")
            all_issues.append("logic:example_conflict")
        if option_dimension_consistency is False:
            required_fixes.append("logic:option_dimension")
            all_issues.append("logic:option_dimension")
        if not grounding_check_passed:
            required_fixes.append("logic:grounding")
            all_issues.append("logic:grounding")
        if not quality_check_passed:
            required_fixes.append("quality:issues")
            if quality_issues:
                for qi in quality_issues:
                    all_issues.append(f"quality:{qi}")
            else:
                all_issues.append("quality:issues")
        if critic_schema_incomplete:
            required_fixes.append("critic:schema_incomplete")
            all_issues.append("critic:schema_incomplete")
        if calc_code_warning:
            all_issues.append(f"calc:warning:{calc_code_warning}")
        if difficulty_out_of_range:
            all_issues.append("difficulty:out_of_range")
        if explanation_fail:
            all_issues.append("explanation:invalid")
        if not code_check_passed:
            all_issues.append(f"calc:code_check:{code_check_reason}")
        if term_lock_fail:
            required_fixes.append("term_lock:violation")
            all_issues.append("term_lock:violation")

        # Build canonical fail_types for QA aggregation (critic rejection reason stats)
        fail_types: List[str] = []
        if reverse_fail:
            fail_types.append("reverse_solve_fail")
        if answer_mismatch:
            fail_types.append("answer_mismatch")
        if grounding_fail:
            fail_types.append("grounding_fail")
        if difficulty_out_of_range:
            fail_types.append("difficulty_out_of_range")
        if not quality_check_passed:
            fail_types.append("quality_fail")
        if term_lock_fail:
            fail_types.append("term_lock_fail")
        if not code_check_passed:
            fail_types.append("code_check_fail")
        if explanation_fail:
            fail_types.append("explanation_fail")
        if critic_schema_incomplete:
            fail_types.append("critic_schema_incomplete")
        if critic_format_issues:
            fail_types.append("format_fail")
        if any(str(x).startswith("writer:") for x in required_fixes):
            fail_types.append("writer_issue")
        if not fail_types:
            fail_types.append("unknown")

        critic_payload = {
            "critic_feedback": fail_reason if fail_reason else "反向解题失败",
            "critic_details": f"❌ 审计不通过（触发Fail条件）: {fail_reason if fail_reason else '无法根据题目条件推导出唯一答案'}",
            "critic_tool_usage": critic_tool_usage,
            "critic_rules_context": full_rules_text,
            "critic_related_rules": related_rules,
            "critic_result": {
                "passed": False,
                "issue_type": issue_type,  # minor: 可修复 / major: 需重新路由
                # Prefer LLM's detailed "reason" so Fixer gets full review conclusion; never leave reason empty for UI
                "reason": (reason if reason else fail_reason or "无法根据题目条件推导出唯一答案"),
                "fix_strategy": fix_strategy,
                "fix_reason": fix_reason,
                "missing_conditions": missing_conditions,
                "example_conflict": example_conflict,
                "quality_check_passed": quality_check_passed,
                "quality_issues": quality_issues,
                "term_lock_issues": term_lock_issues,
                "context_strength": context_strength,
                "option_dimension_consistency": option_dimension_consistency,
                "deduction_process": deduction_process,
                "can_deduce_unique_answer": can_deduce_unique_answer,
                "all_issues": all_issues,
                "multi_option_coverage_rate": multi_option_coverage_rate,
                "missing_options": missing_options,
                "first_part_missing_target_title": first_part_missing_target_title,
                "first_part_missing_level": first_part_missing_level,
                "first_part_missing_textbook_raw": first_part_missing_textbook_raw,
                "first_part_structured_issues": first_part_structured_issues,
                "analysis_rewrite_sufficient": analysis_rewrite_sufficient,
                "analysis_rewrite_issues": analysis_rewrite_issues,
                "fail_types": fail_types,
                "basis_source": basis_source,
                "basis_paths": basis_paths,
                "basis_reason": basis_reason,
                "non_current_slice_basis": non_current_slice_basis,
            },
            "critic_required_fixes": required_fixes,
            "critic_basis_source": basis_source,
            "critic_basis_paths": basis_paths,
            "critic_non_current_basis": non_current_slice_basis,
            "critic_format_issues": critic_format_issues,
            "option_hierarchy_conflict_flag": option_hierarchy_conflict_flag,
            "option_hierarchy_conflict_pairs": option_hierarchy_conflict_pairs,
            "option_hierarchy_conflict_message": option_hierarchy_conflict_message,
            "critic_model_used": critic_model_used,
            "llm_trace": llm_records,
            "retry_count": state['retry_count'] + 1, 
            "logs": [f"{log_prefix} 审计不通过 (第 {state['retry_count']+1} 次). 严重程度: {issue_type}. 原因: {fail_reason if fail_reason else '反向解题失败'}"]
        }
        print(f"DEBUG CRITIC RESULT: {critic_payload['critic_result']}")
        return critic_payload

def fixer_node(state: AgentState, config):
    llm_records: List[Dict[str, Any]] = []
    # This node runs if Critic fails
    fixer_model = WRITER_MODEL or MODEL_NAME
    fixer_api_key = API_KEY
    fixer_base_url = BASE_URL
    fixer_provider = "ait"
    # It takes the feedback and asks Writer (or Specialist) to fix it.
    def build_fix_summary(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
        fields = [
            "题干", "选项1", "选项2", "选项3", "选项4",
            "正确答案", "解析", "难度值", "考点"
        ]
        changed = []
        before_vals = {}
        after_vals = {}
        for key in fields:
            before_val = before.get(key)
            after_val = after.get(key)
            if str(before_val) != str(after_val):
                changed.append(key)
                before_vals[key] = before_val
                after_vals[key] = after_val
        return {
            "changed_fields": changed,
            "before": before_vals,
            "after": after_vals
        }

    def detect_unmet_required_fixes(
        required_fixes: List[str],
        after: Dict[str, Any],
        changed_fields: List[str],
        question_type: str,
        term_locks: List[str],
        rule_precondition_profile: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        unmet: List[str] = []
        required_set = {str(x).strip() for x in (required_fixes or []) if str(x).strip()}
        changed_set = set(changed_fields or [])

        question_fields = {"题干", "选项1", "选项2", "选项3", "选项4", "正确答案"}
        option_fields = {"选项1", "选项2", "选项3", "选项4"}
        question_changed = bool(changed_set & question_fields)
        options_changed = bool(changed_set & option_fields)
        answer_changed = "正确答案" in changed_set
        analysis_changed = "解析" in changed_set
        stem_changed = "题干" in changed_set

        # format:* can be deterministically checked locally
        if any(item.startswith("format:") for item in required_set):
            post_format_issues = validate_critic_format(after, question_type)
            if post_format_issues:
                unmet.append("format")

        # logic:* uses structural minimum-change gates to block superficial "fixes"
        if "logic:cannot_deduce_unique_answer" in required_set and not question_changed:
            unmet.append("logic:cannot_deduce_unique_answer")
        if "logic:answer_mismatch" in required_set and not (answer_changed or analysis_changed):
            unmet.append("logic:answer_mismatch")
        if ("logic:missing_conditions" in required_set or "logic:precondition_slots" in required_set) and not stem_changed:
            # Fixer 不做槽位验收，仅要求有实质修复动作；最终验收交给 Critic。
            if "logic:missing_conditions" in required_set:
                unmet.append("logic:missing_conditions")
            if "logic:precondition_slots" in required_set:
                unmet.append("logic:precondition_slots")
        if "logic:option_dimension" in required_set and not options_changed:
            unmet.append("logic:option_dimension")
        if "logic:example_conflict" in required_set and not (stem_changed or options_changed):
            unmet.append("logic:example_conflict")
        if "logic:grounding" in required_set and not (question_changed or analysis_changed):
            unmet.append("logic:grounding")
        if "calc:closure" in required_set and not (options_changed or answer_changed or analysis_changed):
            unmet.append("calc:closure")
        if "calc:explanation" in required_set and not analysis_changed:
            unmet.append("calc:explanation")
        if "calc:missing_execution" in required_set and not question_changed:
            unmet.append("calc:missing_execution")
        if "calc:unit_lock" in required_set and not (stem_changed or options_changed or analysis_changed):
            unmet.append("calc:unit_lock")
        if "calc:solvability_contract" in required_set and not stem_changed:
            unmet.append("calc:solvability_contract")

        # quality / writer issues cannot be proven solved without another Critic pass,
        # but at least require changing core content, not only metadata.
        if "quality:issues" in required_set and not (question_changed or analysis_changed):
            unmet.append("quality:issues")
        if "quality:focus_slimming" in required_set and not stem_changed:
            unmet.append("quality:focus_slimming")
        if any(item.startswith("writer:") for item in required_set) and not (question_changed or analysis_changed):
            unmet.append("writer")

        if "term_lock:violation" in required_set:
            post_term_lock_issues = detect_term_lock_violations(term_locks or [], after)
            if post_term_lock_issues:
                unmet.append("term_lock:violation")

        # keep stable order and avoid duplicates
        deduped: List[str] = []
        seen = set()
        for item in unmet:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped
    
    final_json = state.get('final_json')
    feedback = state.get('critic_feedback', 'Unknown Error')
    critic_details = state.get('critic_details', '')
    critic_result = state.get('critic_result', {})
    critic_required_fixes = state.get('critic_required_fixes') or []
    fix_strategy = critic_result.get('fix_strategy', 'fix_question')
    fix_reason = critic_result.get('fix_reason', '')
    critic_tool_usage = state.get('critic_tool_usage', {})
    critic_rules_context = state.get('critic_rules_context', '')
    critic_related_rules = state.get('critic_related_rules', [])
    kb_chunk = state['kb_chunk']
    term_locks = state.get("term_locks") or []
    kb_context = format_kb_chunk_full(kb_chunk)
    term_lock_text = ""
    if term_locks:
        term_lock_text = f"""
# 专有名词锁词约束（必须执行）
以下术语若在题干/选项/解析中出现，必须保持原词，不得改写：
{json.dumps(term_locks, ensure_ascii=False)}
"""
    
    # ✅ 输出修复策略和原因，让用户明确看到修复过程
    strategy_map = {
        "fix_explanation": "仅修复解析",
        "fix_question": "修复题目（题干/选项）",
        "fix_both": "同时修复题目和解析",
        "regenerate": "重新生成"
    }
    strategy_label = strategy_map.get(fix_strategy, fix_strategy)
    print(f"\n{'='*60}")
    print(f"🔧 FIXER 开始修复")
    print(f"{'='*60}")
    print(f"📋 修复策略: {strategy_label}")
    print(f"💡 修复原因: {fix_reason}")
    print(f"❌ Critic反馈: {feedback[:100]}...")
    print(f"{'='*60}\n")
    
    # Get constraints from config
    # ✅ Prioritize locked type from specialist/calculator; fallback to current state/config
    locked_question_type = state.get("locked_question_type")
    question_type = (
        locked_question_type
        or state.get('current_question_type')
        or config['configurable'].get('question_type', '单选题')
    )
    generation_mode = state.get("current_generation_mode") or config['configurable'].get('generation_mode', '随机')
    effective_generation_mode, normalized_generation_mode = resolve_effective_generation_mode(generation_mode, state)
    difficulty_range = config['configurable'].get('difficulty_range')
    focus_contract = state.get("locked_focus_contract") or (state.get("router_details") or {}).get("focus_contract") or {}
    rule_precondition_profile = (state.get("router_details") or {}).get("rule_precondition_profile") or {}
    focus_rule = str(focus_contract.get("focus_rule", "") or "").strip()
    focus_task = str(focus_contract.get("focus_task", "") or "").strip()
    focus_variables = [str(x).strip() for x in (focus_contract.get("focus_variables") or []) if str(x).strip()]
    focus_lock_text = ""
    if focus_rule:
        focus_lock_text = f"""
# 主考点锁定（必须保持，不得漂移）
- 主规则句：{focus_rule}
- 主任务：{focus_task or '规则理解'}
- 关键变量：{', '.join(focus_variables) if focus_variables else '无'}
- 要求：修复后题干、正确答案、解析必须继续围绕该主规则，不得退化为年份/名称等纯记忆题。
"""
    
    def _rebuild_calc_execution_state(latest_final_json: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        is_calc_question = bool(state.get("generated_code")) or state.get("agent_name") == "CalculatorAgent"
        if not is_calc_question:
            return {}

        tool_usage = state.get("tool_usage") if isinstance(state.get("tool_usage"), dict) else {}
        generated_code = state.get("generated_code")
        if not generated_code and isinstance(tool_usage, dict):
            generated_code = tool_usage.get("generated_code")
        generated_code = str(generated_code or "").strip() or None

        execution_result = state.get("execution_result")
        code_status = str(state.get("code_status", "") or (tool_usage.get("code_status") if isinstance(tool_usage, dict) else "") or "").strip()

        if generated_code and (execution_result in (None, "") or code_status not in {"success", "success_no_result"}):
            try:
                result_value, stdout_str, stderr_str = execute_python_code(generated_code)
                if stderr_str:
                    code_status = "error"
                    execution_result = f"Execution Error: {stderr_str}"
                elif result_value is not None:
                    code_status = "success"
                    execution_result = result_value
                else:
                    code_status = "success_no_result"
                    execution_result = stdout_str.strip() if stdout_str.strip() else None
            except Exception as e:
                code_status = "error"
                execution_result = f"Execution Error: {type(e).__name__}: {e}"

        merged_tool_usage = dict(tool_usage or {})
        if generated_code:
            merged_tool_usage["generated_code"] = generated_code
        if code_status:
            merged_tool_usage["code_status"] = code_status
        if execution_result not in (None, ""):
            merged_tool_usage["result"] = execution_result

        stem = ""
        if isinstance(latest_final_json, dict):
            stem = str(latest_final_json.get("题干", "") or "")
        calc_target_signature = _extract_calc_target_signature(stem) if stem else str(state.get("calc_target_signature", "") or "")

        return {
            "execution_result": execution_result,
            "generated_code": generated_code,
            "tool_usage": merged_tool_usage if merged_tool_usage else None,
            "calc_target_signature": calc_target_signature or None,
            "code_status": code_status or None,
        }

    # Build type instruction
    if question_type == "判断题":
        type_instruction = "题型要求：判断题。选项必须固定为：['正确','错误']。答案必须是 'A' 或 'B'。括号格式：题干末尾必须精确写成“（　）”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后不能再加句号。"
    elif question_type == "多选题":
        type_instruction = "题型要求：多选题。至少4个选项。答案必须是列表形式，如 ['A','C','D']。括号格式：题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。"
    else:
        type_instruction = "题型要求：单选题。4个选项且只有一个正确。答案必须是单个字母，如 'A'。括号格式：题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。"
    
    # Build mode instruction
    mode_instruction = build_mode_instruction(effective_generation_mode, normalized_generation_mode)
    
    # Build difficulty instruction
    difficulty_instruction = ""
    if difficulty_range:
        min_diff, max_diff = difficulty_range
        difficulty_instruction = f"""
# 难度要求（必须严格遵守）⚠️
**题目难度值必须在 {min_diff:.1f} 到 {max_diff:.1f} 之间**。
并且必须用数值填写难度字段（禁止“易/中/难”文本标签）。

难度控制方法：
- **简单题 (0.3-0.5)**：直接考察知识点定义、基础概念，干扰项仍需同维度且贴近常见误判
- **中等题 (0.5-0.7)**：需要理解知识点含义并应用到场景，干扰项似是而非，需要仔细分析
- **困难题 (0.7-0.9)**：需要综合多个知识点、复杂计算或多步推理，干扰项高度相似

**重要**：生成的题目难度值必须落在指定范围内，否则会被拒绝。请根据难度要求调整：
- 题干复杂度（简单题用直接表述，困难题用复杂场景）
- 干扰项相似度（简单题也应“看起来合理但错误”，困难题可进一步提高相似度）
- 所需推理步骤（简单题直接答案，困难题需要多步推理）
	"""

    required_fix_directives: List[str] = []
    required_fix_set = {str(x) for x in critic_required_fixes if str(x)}
    if "logic:cannot_deduce_unique_answer" in required_fix_set:
        required_fix_directives.append(
            "针对 logic:cannot_deduce_unique_answer：必须重写题干约束和至少一个选项，使单选题只能推出一个答案，禁止“角色边界未定义”导致多选项都成立。"
        )
    if "logic:answer_mismatch" in required_fix_set:
        required_fix_directives.append(
            "针对 logic:answer_mismatch：必须同步修正“正确答案+解析结论”，确保两者一致且结论句只指向最终答案。"
        )
    if "logic:missing_conditions" in required_fix_set:
        required_fix_directives.append(
            "针对 logic:missing_conditions：必须在题干补齐判题条件，不得依赖隐含常识或教材外前提。"
        )
    if "logic:precondition_slots" in required_fix_set:
        required_fix_directives.append(
            "针对 logic:precondition_slots：必须在题干或用于判定的关键选项中补齐规则前提槽位（如地域/主体身份/时间条件/适用边界），不得仅在解析补充。"
        )
    if focus_rule:
        required_fix_directives.append(
            f"针对 focus:locked：必须围绕主规则“{focus_rule}”命题，保持主任务“{focus_task or '规则理解'}”，不得改成片段事实记忆题。"
        )
    if "logic:option_dimension" in required_fix_set:
        required_fix_directives.append(
            "针对 logic:option_dimension：选项必须保持同一维度（同类角色/同类行为/同类规则），不能混维度。"
        )
    if "logic:grounding" in required_fix_set:
        required_fix_directives.append(
            "针对 logic:grounding：题干、选项、解析都必须可由给定参考材料直接支撑，禁止材料外扩展。"
        )
    if "calc:closure" in required_fix_set:
        required_fix_directives.append(
            "针对 calc:closure：必须同步修正正确选项文本、正确答案和解析中的最终数值，保证三者完全一致，不能只改答案字母。"
        )
    if "calc:explanation" in required_fix_set:
        required_fix_directives.append(
            "针对 calc:explanation：解析必须明确写出完整计算过程，并算出与正确选项一致的最终结果。"
        )
    if "calc:missing_execution" in required_fix_set:
        required_fix_directives.append(
            "针对 calc:missing_execution：必须把题目改成可验证的计算题，避免依赖模糊口径或无法落地的中间结果。"
        )
    if "calc:unit_lock" in required_fix_set:
        required_fix_directives.append(
            "针对 calc:unit_lock：题干、选项、解析必须保持同一单位口径（例如元/万元不能混用），不得发生单位漂移。"
        )
    if "calc:solvability_contract" in required_fix_set:
        required_fix_directives.append(
            "针对 calc:solvability_contract：必须在题干补齐计算所需关键输入槽位，不得留空让学员猜测前置条件。"
        )
    if "quality:issues" in required_fix_set:
        required_fix_directives.append(
            "针对 quality:issues：必须消除歧义并提升可读性，避免靠措辞猜答案。"
        )
    if "quality:focus_slimming" in required_fix_set:
        required_fix_directives.append(
            "针对 quality:focus_slimming：必须压缩题干测点，主测点最多2个；删除与主考核无关的资格/时间/地域/计算/边界叠加条件。"
        )
    if any(x.startswith("writer:HARD_EXPL") for x in required_fix_set):
        required_fix_directives.append(
            "针对 writer:HARD_EXPL*：解析必须严格三段式，第一段含目标题与分级，第二段覆盖全部正确/错误选项，第三段结论格式规范。"
        )
    if "writer:retry_exhausted" in required_fix_set:
        required_fix_directives.append(
            "针对 writer:retry_exhausted：禁止仅改元数据，必须实质重写题干或解析主干并重新闭环答案。"
        )
    if any(x.startswith("format:教材原文段须包含分级") for x in required_fix_set):
        required_fix_directives.append(
            "针对 format:教材原文段须包含分级：解析第1段必须包含“掌握/熟悉/了解”等分级标识。"
        )
    if any(x.startswith("format:多选题试题分析须对每个正确选项都解释") for x in required_fix_set):
        required_fix_directives.append(
            "针对 format:多选题试题分析覆盖不足：解析第2段必须逐一解释所有正确选项及主要错误选项。"
        )
    required_fix_directive_block = "\n".join(f"- {item}" for item in required_fix_directives) if required_fix_directives else "- 无额外定向指令，按必改项全量修复。"
    
    def normalize_question_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        if '题干' in payload:
            payload['题干'] = normalize_blank_brackets(str(payload.get('题干', '')))
        if 'question' in payload:
            payload['question'] = normalize_blank_brackets(str(payload.get('question', '')))
        for i in range(1, 9):
            key = f"选项{i}"
            if key in payload and payload.get(key):
                payload[key] = normalize_blank_brackets(str(payload[key]))
        return payload

    def patch_stem_for_missing_slots(
        current_json: Dict[str, Any],
        missing_slots: List[str],
    ) -> Dict[str, Any]:
        slots = [str(x).strip() for x in (missing_slots or []) if str(x).strip()][:2]
        if not isinstance(current_json, dict) or not slots:
            return current_json
        patch_prompt = f"""
你是修复器。目标：最小重写补齐槽位，并减少无关背景噪音；不得把题目越改越重。

当前题目：
{json.dumps(current_json, ensure_ascii=False)}

必须补齐的槽位：
{json.dumps(slots, ensure_ascii=False)}

约束：
1. 优先最小改动：先精简题干冗余背景，再补缺槽位；必要时可把槽位放入关键选项，不要求全部塞进题干。
2. 补槽位要“语义明确可判定”，不要空话，不要新增主测点。
3. 保持与参考规则一致，不引入材料外条件，不无故改题型/正确答案。
4. 返回完整 JSON（题干/选项1-4/正确答案/解析/难度值/考点）。

参考规则上下文：
{critic_rules_context if critic_rules_context else kb_context}
"""
        patched_content, _, patch_record = call_llm(
            node_name="fixer.slot_patch",
            prompt=patch_prompt,
            model_name=fixer_model,
            api_key=fixer_api_key,
            base_url=fixer_base_url,
            provider=fixer_provider,
            trace_id=state.get("trace_id"),
            question_id=state.get("question_id"),
        )
        llm_records.append(patch_record)
        patched_json = parse_json_from_response(patched_content)
        if not isinstance(patched_json, dict):
            return current_json
        patched_json = normalize_question_fields(patched_json)
        patched_json = repair_final_json_format(patched_json, question_type)
        patched_json.setdefault('题干', current_json.get('题干', ''))
        for i in range(1, 5):
            patched_json.setdefault(f'选项{i}', current_json.get(f'选项{i}', ''))
        patched_json.setdefault('正确答案', current_json.get('正确答案', 'A'))
        patched_json.setdefault('解析', current_json.get('解析', ''))
        patched_json.setdefault('难度值', current_json.get('难度值', 0.5))
        patched_json.setdefault('考点', current_json.get('考点', ''))
        return patched_json

    def patch_focus_slimming(
        current_json: Dict[str, Any],
        *,
        must_keep_slots: List[str],
    ) -> Dict[str, Any]:
        if not isinstance(current_json, dict):
            return current_json
        keep_slots = [str(x).strip() for x in (must_keep_slots or []) if str(x).strip()]
        slim_prompt = f"""
你是修复器。目标：把题干测点收敛到最多2个主测点，同时保留必要前提槽位。

当前题目：
{json.dumps(current_json, ensure_ascii=False)}

必须保留的前提槽位（不得删除）：
{json.dumps(keep_slots, ensure_ascii=False)}

要求：
1. 删除与主考点无关的次要测点，避免同题同时堆叠资格/地域/时间/计算/边界多维考核。
2. 保留题型、正确答案与核心考点方向；优先精简题干和无关选项表述。
3. 不得删除上面列出的必要前提槽位。
4. 返回完整 JSON（题干/选项1-4/正确答案/解析/难度值/考点）。

参考规则上下文：
{critic_rules_context if critic_rules_context else kb_context}
"""
        slim_content, _, slim_record = call_llm(
            node_name="fixer.focus_slimming",
            prompt=slim_prompt,
            model_name=fixer_model,
            api_key=fixer_api_key,
            base_url=fixer_base_url,
            provider=fixer_provider,
            trace_id=state.get("trace_id"),
            question_id=state.get("question_id"),
        )
        llm_records.append(slim_record)
        slim_json = parse_json_from_response(slim_content)
        if not isinstance(slim_json, dict):
            return current_json
        slim_json = normalize_question_fields(slim_json)
        slim_json = repair_final_json_format(slim_json, question_type)
        slim_json.setdefault('题干', current_json.get('题干', ''))
        for i in range(1, 5):
            slim_json.setdefault(f'选项{i}', current_json.get(f'选项{i}', ''))
        slim_json.setdefault('正确答案', current_json.get('正确答案', 'A'))
        slim_json.setdefault('解析', current_json.get('解析', ''))
        slim_json.setdefault('难度值', current_json.get('难度值', 0.5))
        slim_json.setdefault('考点', current_json.get('考点', ''))
        return slim_json

    # CASE 1: Critical Failure (No question generated) -> Regenerate from scratch
    if not final_json:
        prompt = f"""
# 任务
之前的生成流程失败了，未生成有效题目。
原因: {feedback}
参考: {kb_context}

# 补救任务
请重新根据参考材料创作一道{question_type}。

{type_instruction}

{mode_instruction}

{difficulty_instruction}
{CALC_PARAMETER_GROUNDING_GUIDE}
{term_lock_text}
{focus_lock_text}

# 质量标准:
1. **准确性**: 100% 忠实于原文。
2. **格式**: 严格的 JSON 输出。

# 自检清单（必须逐条核对）
1. **唯一答案**：题干条件足以排除其他选项，不能出现两条合理路径。
2. **解析规范**：三段式完整，结论以“本题答案为X”收束。
3. **一致性**：题干/选项/答案/解析前后一致。
4. **适纲性**：不超纲，不引入材料外条件或结论。
5. **人名与措辞**：人名规范、无生造词、无模糊词。
6. **维度一致**：选项同维度，干扰项有理有据。
7. **禁用兜底选项**：选项不得出现「以上都对」「以上都错」「皆是」「皆非」等；若命中须改写为同维度干扰项。
8. **长度限制**：题干≤400字、单选项≤200字；解析仅要求“教材原文”段尽量≤400字，整段解析不设硬性上限。超长时仅删减非核心句，并剔除与解题无关的表述。

# 输出格式 (JSON)
{{
    "题干": "...",
    "选项1": "...", "选项2": "...", "选项3": "...", "选项4": "...",
    "正确答案": "A/B/C/D",
    "解析": "...",
    "难度值": 0.5,
    "考点": "..."
}}
"""
        content, _, llm_record = call_llm(
            node_name="fixer.regenerate_initial",
            prompt=prompt,
            model_name=fixer_model,
            api_key=fixer_api_key,
            base_url=fixer_base_url,
            provider=fixer_provider,
            trace_id=state.get("trace_id"),
            question_id=state.get("question_id"),
        )
        llm_records.append(llm_record)
        
        try:
            fixed_json = parse_json_from_response(content)
            # Ensure required fields with safe defaults
            difficulty_value = fixed_json.get('难度值', 0.5)
            try:
                difficulty_value = float(difficulty_value)
            except:
                difficulty_value = 0.5
            
            # 如果指定了难度范围，验证并调整难度值
            if difficulty_range:
                min_diff, max_diff = difficulty_range
                if difficulty_value < min_diff or difficulty_value > max_diff:
                    # 如果不在范围内，调整到范围中点
                    difficulty_value = (min_diff + max_diff) / 2
                    print(f"⚠️ 修复者警告: 生成的难度值不在指定范围内，已调整为 {difficulty_value:.2f}")
            
            fixed_json['难度值'] = difficulty_value
            fixed_json['掌握程度'] = str(kb_chunk.get('掌握程度', '') or '').strip()
            fixed_json.setdefault('考点', kb_chunk.get('完整路径', '未知'))
            fixed_json.setdefault('题干', '')
            fixed_json.setdefault('选项1', '')
            fixed_json.setdefault('选项2', '')
            fixed_json.setdefault('选项3', '')
            fixed_json.setdefault('选项4', '')
            fixed_json.setdefault('正确答案', 'A')
            fixed_json.setdefault('解析', '')
            fixed_json = normalize_question_fields(fixed_json)
            fixed_json = repair_final_json_format(fixed_json, question_type)
            # Keep decision contract consistent: critical_decision checks final_json._was_fixed.
            fixed_json['_was_fixed'] = True
            calc_state_updates = _rebuild_calc_execution_state(fixed_json)
            fixed_json, _, _ = _enforce_calc_answer_alignment_on_final_json(
                fixed_json,
                execution_result=calc_state_updates.get("execution_result", state.get("execution_result")),
                code_status=str(calc_state_updates.get("code_status", state.get("code_status", "")) or ""),
            )
            derived_state = _sync_downstream_state_from_final_json(
                fixed_json,
                question_type,
                term_locks=term_locks,
                kb_context=critic_rules_context or kb_context,
                focus_contract=state.get("locked_focus_contract") or (state.get("router_details") or {}).get("focus_contract"),
                is_calculation=bool(state.get("generated_code")) or state.get("agent_name") == "CalculatorAgent",
                expected_calc_target=str(state.get("calc_target_signature", "") or ""),
                expected_calc_unit=str(state.get("calc_unit_hint", "") or ""),
            )
            
            return {
                "final_json": fixed_json,
                **derived_state,
                "llm_trace": llm_records,
                "logs": ["🔧 修复者: 检测到生成失败，已重新生成题目"],
                "was_fixed": True,
                **calc_state_updates,
            }
        except Exception as e:
            return {"llm_trace": llm_records, "logs": [f"❌ 修复者重试失败: {str(e)}"]}

    # CASE 2: Normal Fix (Question exists but rejected)
    # Ensure Fixer gets Critic's full review conclusion (LLM "reason"), not just summary
    critic_reason_full = critic_result.get("reason") or critic_details or feedback
    prompt = f"""
# 任务
上一道题被批评家驳回了。
原因: {feedback}
审计详情: {critic_details}
修复策略: {fix_strategy}（{fix_reason}）

## 驳回详细说明（批评家完整审核结论，必须按此修改）⚠️
{critic_reason_full}

必须修复项（来自批评家）：{json.dumps(critic_required_fixes, ensure_ascii=False)}
定向修复指令：
{required_fix_directive_block}
审计补充信息:
- missing_conditions: {critic_result.get('missing_conditions')}
- example_conflict: {critic_result.get('example_conflict')}
- quality_issues: {critic_result.get('quality_issues')}
- all_issues: {critic_result.get('all_issues')}
- option_dimension_consistency: {critic_result.get('option_dimension_consistency')}
- deduction_process: {critic_result.get('deduction_process')}
- can_deduce_unique_answer: {critic_result.get('can_deduce_unique_answer')}
- context_strength: {critic_result.get('context_strength')}
审计工具/计算痕迹: {json.dumps(critic_tool_usage, ensure_ascii=False)}
相关规则列表: {json.dumps(critic_related_rules, ensure_ascii=False)}
参考: {kb_context}
补充规则（如有）：{critic_rules_context if critic_rules_context else "(无)"} 
题目: {json.dumps(final_json, ensure_ascii=False)}
{term_lock_text}
{focus_lock_text}

# 好题标准（修复时必须遵守）
## 四大核心要求
1. **聚焦贴业务**：题目必须聚焦房地产经纪人实际工作场景，实用常见。
2. **直接不拐弯**：考点直接明确，不绕弯子，避免复杂陷阱。
3. **简洁不啰嗦**：题干、设问、选项表述简洁清晰，突出核心要点。
4. **真诚说人话**：用通俗易懂的日常表达，避免生僻词和专业黑话。

## 避开特殊考点（必须检查）⚠️
1. **避免歧义考点**：答案必须唯一明确，不能有争议。
2. **避免偏辟考点**：不考察过于细节、不常用的知识点（如家装报价详细规格）。
3. **避免无关考点**：不考察与房地产经纪业务无关的内容（如监护权等民法细节）。
4. **避免模糊考点**：不考察无明确对错的内容（带看顺序、面谈内容等）、教材与实际不符的内容。
5. **题目要有意义**：避免过于简单或无考察价值的判断题（如"做得好是否正确"等废话题）。

## 简化场景（必须检查）⚠️
1. **去掉无意义铺垫**："某某告诉某某"、"在培训时了解到"等冗余表述。
2. **去掉无关句子**：只保留与解题相关的关键信息；题干较长时重点剔除与本题毫无关系的表达，避免题干没必要的复杂、逻辑没必要的绕。
3. **简化长句子**：突出核心条件，避免冗长描述。
4. **简化数字**：优先使用整数或简单小数（如1:4而非1:3.28），方便口算。
5. **非必要不起名**：经纪人名字对考点无关时不要提及。

# 必须遵守的约束
{type_instruction}

# 计算题修复硬约束（如适用，必须遵守）
1. 如果这是计算题，禁止只修改“正确答案”字母而不修改选项数值或解析计算过程。
2. 正确选项文本中的最终数值，必须与解析中算出的最终数值一致。
3. 若当前题目数值闭环无法自洽，优先整体重写题干/选项/解析，不要做表面修补。

{mode_instruction}

{difficulty_instruction}

# 人名规范（必须遵守）
1. **非必要不取名**：能不出现人名就不要出现。
2. **通俗姓名**：如需人名，使用常见姓氏+常见名的两字通俗姓名。
3. **负面事件**：涉及事故、违法违规等负面问题时，用“某某”指代（如张某）。但若题目需要判断行为是否合法/正确与否，则不适用“某某”规则。
4. **禁止恶搞**：姓名不得含恶搞或戏谑成分（如张漂亮、甄真钱、贾董事、张三、刘二等）。
5. **伦理合理**：姓名组合需符合日常伦理与常识（如父亲刘大伟、儿子刘二伟不可以；父亲张勇强、儿子张强勇不可以）。
6. **简洁易懂**：姓名尽可能简洁、通俗易懂，不使用生僻词。
7. **禁止小名**：不得使用小名/乳名（如小宝、贝贝）。
8. **禁止称谓**：不得使用“姓+女士/先生”，也不得使用“小李/小张”等称谓。

# 题干/设问/选项/解析规范（必须遵守）
1. **题干括号位置**：
   - 题干中的括号不能在句首，可放在句中或句末。
   - 选择题题干句末要有句号，句号在最后；判断题题干句子完结后加一个括号，括号在最后。
2. **括号格式**：
   - 使用中文括号，括号内部有且仅有一个全角空格（不能多）：`（　）`
   - 括号前后不允许空格
3. **设问表达**：
   - 设问要用陈述方式，不使用疑问句。
   - 少用否定句，禁止使用双重否定句。
   - **判断题要求**：判断题必须是肯定陈述句，并明确出现“正确”或“错误”锚点；不要写成“是否正确/对不对/是不是”这类疑问式。
4. **选择题设问表述**：题干须为陈述句，以（　）作答占位结尾（句号在括号后）。不强制固定使用某一种模板化结尾。
5. **选项规范**：
   - 选择题每题4个选项；单选题仅1个正确，多选题≥2个正确。
   - 每个选项末尾不添加标点符号。
   - 选项必须与题干组成完整语句（将选项代入题干括号处语义完整）。
   - 选项中的姓名与题干中的姓名保持一致，不出现题目未涉及的姓名。
   - 干扰项必须具有干扰性，选项本身应是存在或相关的内容，不能无意义。
   - 多选题中正确选项数量要合理，不要多道题都只有1个答案。
6. **判断题比例提醒**（仅适用于批量场景）：正确/错误数量比例应接近1:1（最低要求约15:25），避免明显偏高。
7. **数值型选项**：
   - 选项为数字时按从小到大顺序排列。
   - 计算题尽量简单（能口算优先），确需保留小数时注明保留位数（一般1-2位）。
   - 非计算题若解题过程涉及运算（如比例、折算、阈值比较），同样执行“简算优先”：避免复杂小数与冗长多步计算，不应依赖计算器；若必须保留小数，题干须明确“保留到X位小数”（一般1-2位）。
   - 未被选中的数值选项也必须有计算依据，不可胡编乱造。

# 自检清单（必须逐条核对）
1. **唯一答案**：题干条件足以排除其他选项，不能出现两条合理路径。
2. **解析规范**：三段式完整，结论以“本题答案为X”收束。
3. **一致性**：题干/选项/答案/解析前后一致，计算题与计算过程一致。
4. **适纲性**：不超纲，不引入材料外条件或结论。
5. **人名与措辞**：人名规范、无生造词、无模糊词。
6. **维度一致**：选项同维度，干扰项有理有据。
7. **禁用兜底选项**：选项不得出现「以上都对」「以上都错」「皆是」「皆非」等；若命中须改写为同维度干扰项。
8. **长度限制**：题干≤400字、单选项≤200字；解析仅要求“教材原文”段尽量≤400字，整段解析不设硬性上限。超长时仅删减非核心句，并剔除与解题无关的表述。

# 修复要求:
1. **准确性**: 确保答案与解析完全一致，且有知识片段支持。
2. **干扰项**: 确保错误选项似是而非但绝对错误。利用**"相近的数字"**或**"错误的参照物"**设计干扰项。
3. **清晰度**: 消除导致批评家困惑的歧义。
4. **完整性**: 必须包含 "难度值" (0.0-1.0) 和 "考点"。
5. **题型一致性**: 修复后的题目必须符合指定的题型要求（{question_type}）。

# 必改项定向修复指令（必须逐条执行）
{required_fix_directive_block}

请按修复策略执行：
- 如果策略是 fix_explanation：只修改解析，使其与现有答案/题干一致，不改题干与选项。
- 如果策略是 fix_question：基于知识片段修改题干/选项/答案，使题目与解析一致。
- 如果策略是 fix_both：同时修正题干/选项/答案与解析。
- 如果策略是 regenerate：重写题干与选项，确保可唯一推导出正确答案。

请修复这道题，使其正确且无歧义。
**强制要求**：必须覆盖所有“必须修复项”，至少修改一个字段。
约束: 题干中**禁止**出现"根据材料"或"依据参考资料"。
返回修复后的 JSON (包含 题干, 选项1-4, 正确答案, 解析, 难度值, 考点)。
"""
    content, _, llm_record = call_llm(
        node_name="fixer.apply_fix",
        prompt=prompt,
        model_name=fixer_model,
        api_key=fixer_api_key,
        base_url=fixer_base_url,
        provider=fixer_provider,
        trace_id=state.get("trace_id"),
        question_id=state.get("question_id"),
    )
    llm_records.append(llm_record)
    
    try:
        fixed_json = parse_json_from_response(content)
        fixed_json = normalize_question_fields(fixed_json)
        
        # Robust fallback for all required fields
        difficulty_value = fixed_json.get('难度值', final_json.get('难度值', 0.5))
        try:
            difficulty_value = float(difficulty_value)
        except:
            difficulty_value = 0.5
        
        # 如果指定了难度范围，验证并调整难度值
        if difficulty_range:
            min_diff, max_diff = difficulty_range
            if difficulty_value < min_diff or difficulty_value > max_diff:
                # 如果不在范围内，调整到范围中点
                difficulty_value = (min_diff + max_diff) / 2
                print(f"⚠️ 修复者警告: 修复后的难度值不在指定范围内，已调整为 {difficulty_value:.2f}")
        
        fixed_json['难度值'] = difficulty_value
        fixed_json['掌握程度'] = str(kb_chunk.get('掌握程度', '') or '').strip()
        fixed_json.setdefault('考点', final_json.get('考点', kb_chunk.get('完整路径', '').split('>')[-1].strip() or "综合考点"))
        fixed_json.setdefault('题干', final_json.get('题干', ''))
        fixed_json.setdefault('选项1', final_json.get('选项1', ''))
        fixed_json.setdefault('选项2', final_json.get('选项2', ''))
        fixed_json.setdefault('选项3', final_json.get('选项3', ''))
        fixed_json.setdefault('选项4', final_json.get('选项4', ''))
        fixed_json.setdefault('正确答案', final_json.get('正确答案', 'A'))
        fixed_json.setdefault('解析', final_json.get('解析', ''))
        fixed_json = repair_final_json_format(fixed_json, question_type)
        inferred_fixed_type = _infer_final_json_question_type(fixed_json)
        if locked_question_type in ["单选题", "多选题", "判断题"] and inferred_fixed_type != locked_question_type:
            return {
                "final_json": final_json,
                "current_question_type": str(locked_question_type),
                "locked_question_type": str(locked_question_type),
                "fix_summary": {"changed_fields": [], "rollback_reason": f"题型漂移: locked={locked_question_type}, inferred={inferred_fixed_type}"},
                "fix_no_change": True,
                "fix_attempted_regen": False,
                "fix_required_unmet": True,
                "llm_trace": llm_records,
                "logs": [f"❌ 修复者: 检测到题型漂移（locked={locked_question_type}, inferred={inferred_fixed_type}），已回退"],
                "was_fixed": False,
            }
        
        # Mark this question as having been fixed (for UI highlighting)
        fixed_json['_was_fixed'] = True
        fix_summary = build_fix_summary(final_json or {}, fixed_json or {})
        # Fixer 只负责“修复产出”，不负责“验收裁决”。
        # required_fixes 的验收与通过/驳回一律由 Critic 统一判定，避免职责混杂。
        fix_summary["required_fixes"] = critic_required_fixes
        fix_summary["unmet_required_fixes"] = []
        fix_required_unmet = False

        # If no changes, force a second pass with regenerate instruction
        fix_no_change = False
        force_regen_used = False
        changed_fields = fix_summary.get("changed_fields", [])
        if not changed_fields:
            force_regen_used = True
            force_prompt = f"""
# 任务
你上一次修复没有产生任何改动，这会导致问题无法解决。
请基于批评家反馈，**必须修改至少一个字段**（题干/选项/答案/解析/难度/考点），确保题目可唯一推导答案并解决驳回原因。
如果无法修复，请直接**重写整题**。

原因: {feedback}
审计详情: {critic_details}

## 驳回详细说明（批评家完整审核结论，必须按此修改）⚠️
{critic_reason_full}

参考: {kb_context}
原题: {json.dumps(final_json, ensure_ascii=False)}

{type_instruction}
{mode_instruction}
{difficulty_instruction}

# 强制要求
1. 必须修改至少一个字段（题干/选项/答案/解析/难度/考点）。
2. 输出严格 JSON。

# 输出格式 (JSON)
{{
    "题干": "...",
    "选项1": "...", "选项2": "...", "选项3": "...", "选项4": "...",
    "正确答案": "A/B/C/D",
    "解析": "...",
    "难度值": 0.5,
    "考点": "..."
}}
"""
            try:
                content_force, _, llm_record = call_llm(
                    node_name="fixer.force_regenerate",
                    prompt=force_prompt,
                    model_name=fixer_model,
                    api_key=fixer_api_key,
                    base_url=fixer_base_url,
                    provider=fixer_provider,
                    trace_id=state.get("trace_id"),
                    question_id=state.get("question_id"),
                )
                llm_records.append(llm_record)
                forced_json = parse_json_from_response(content_force)
                forced_json = normalize_question_fields(forced_json)

                difficulty_value = forced_json.get('难度值', final_json.get('难度值', 0.5))
                try:
                    difficulty_value = float(difficulty_value)
                except:
                    difficulty_value = 0.5
                if difficulty_range:
                    min_diff, max_diff = difficulty_range
                    if difficulty_value < min_diff or difficulty_value > max_diff:
                        difficulty_value = (min_diff + max_diff) / 2
                        print(f"⚠️ 修复者警告: 强制重写难度值不在范围内，已调整为 {difficulty_value:.2f}")
                forced_json['难度值'] = difficulty_value
                forced_json['掌握程度'] = str(kb_chunk.get('掌握程度', '') or '').strip()
                forced_json.setdefault('考点', final_json.get('考点', kb_chunk.get('完整路径', '').split('>')[-1].strip() or "综合考点"))
                forced_json.setdefault('题干', final_json.get('题干', ''))
                forced_json.setdefault('选项1', final_json.get('选项1', ''))
                forced_json.setdefault('选项2', final_json.get('选项2', ''))
                forced_json.setdefault('选项3', final_json.get('选项3', ''))
                forced_json.setdefault('选项4', final_json.get('选项4', ''))
                forced_json.setdefault('正确答案', final_json.get('正确答案', 'A'))
                forced_json.setdefault('解析', final_json.get('解析', ''))
                forced_json = repair_final_json_format(forced_json, question_type)
                inferred_forced_type = _infer_final_json_question_type(forced_json)
                if locked_question_type in ["单选题", "多选题", "判断题"] and inferred_forced_type != locked_question_type:
                    return {
                        "final_json": final_json,
                        "current_question_type": str(locked_question_type),
                        "locked_question_type": str(locked_question_type),
                        "fix_summary": {"changed_fields": [], "rollback_reason": f"题型漂移: locked={locked_question_type}, inferred={inferred_forced_type}"},
                        "fix_no_change": True,
                        "fix_attempted_regen": True,
                        "fix_required_unmet": True,
                        "llm_trace": llm_records,
                        "logs": [f"❌ 修复者: 强制重写后仍题型漂移（locked={locked_question_type}, inferred={inferred_forced_type}），已回退"],
                        "was_fixed": False,
                    }
                forced_json['_was_fixed'] = True

                forced_summary = build_fix_summary(final_json or {}, forced_json or {})
                forced_changed = forced_summary.get("changed_fields", [])
                if forced_changed:
                    fixed_json = forced_json
                    fix_summary = forced_summary
                    changed_fields = forced_changed
                    fixed_json['_was_fixed'] = True
                else:
                    fix_no_change = True
            except Exception as e:
                print(f"⚠️ 修复者警告: 强制重写失败: {str(e)}")
                fix_no_change = True

        # ✅ 详细的修复日志
        strategy_label = strategy_map.get(fix_strategy, fix_strategy)
        log_msg = f"🔧 修复者: 已完成修复 (策略: {strategy_label})"
        if changed_fields:
            log_msg += f" | 修改字段: {', '.join(changed_fields)}"
        elif force_regen_used:
            log_msg += " | 强制重写后仍无变化"
        
        print(f"\n{'='*60}")
        print(f"✅ FIXER 修复完成")
        print(f"{'='*60}")
        print(f"📝 修改字段: {', '.join(changed_fields) if changed_fields else '无变化'}")
        print(f"{'='*60}\n")
        # Rebuild calculator/code state so Critic can verify fixed question with executable evidence.
        calc_state_updates = _rebuild_calc_execution_state(fixed_json)
        fixed_json, calc_aligned, calc_align_msg = _enforce_calc_answer_alignment_on_final_json(
            fixed_json,
            execution_result=calc_state_updates.get("execution_result", state.get("execution_result")),
            code_status=str(calc_state_updates.get("code_status", state.get("code_status", "")) or ""),
        )
        if calc_aligned:
            fix_summary = build_fix_summary(final_json or {}, fixed_json or {})
            changed_fields = fix_summary.get("changed_fields", changed_fields)
        derived_state = _sync_downstream_state_from_final_json(
            fixed_json,
            question_type,
            term_locks=term_locks,
            kb_context=critic_rules_context or kb_context,
            focus_contract=state.get("locked_focus_contract") or (state.get("router_details") or {}).get("focus_contract"),
            is_calculation=bool(state.get("generated_code")) or state.get("agent_name") == "CalculatorAgent",
            expected_calc_target=str(state.get("calc_target_signature", "") or ""),
            expected_calc_unit=str(state.get("calc_unit_hint", "") or ""),
        )
        if calc_aligned and calc_align_msg:
            log_msg += f" | {calc_align_msg}"

        # 槽位修复：仅根据 Critic 给出的 missing_conditions 执行修复，不在 Fixer 节点做验收。
        semantic_slot_required = (
            ("logic:missing_conditions" in required_fix_set or "logic:precondition_slots" in required_fix_set)
            and isinstance(critic_result, dict)
            and isinstance(critic_result.get("missing_conditions"), list)
            and len([x for x in critic_result.get("missing_conditions") if str(x).strip()]) > 0
        )
        if semantic_slot_required:
            expected_slots = [str(x).strip() for x in critic_result.get("missing_conditions", []) if str(x).strip()]
            fixed_json = patch_stem_for_missing_slots(fixed_json, expected_slots)
            fix_summary = build_fix_summary(final_json or {}, fixed_json or {})
            changed_fields = fix_summary.get("changed_fields", changed_fields)
            if changed_fields:
                log_msg += " | 已按Critic缺失槽位执行修复"

        # 第二阶段：测点收敛（在补齐前提槽位之后执行），并强制保留必要槽位。
        if "quality:focus_slimming" in required_fix_set:
            keep_slots: List[str] = []
            if isinstance(critic_result, dict):
                keep_slots.extend([str(x).strip() for x in (critic_result.get("missing_conditions") or []) if str(x).strip()])
            if isinstance(rule_precondition_profile, dict) and rule_precondition_profile.get("enabled"):
                specs = rule_precondition_profile.get("required_slot_specs")
                if isinstance(specs, list):
                    for spec in specs:
                        if not isinstance(spec, dict):
                            continue
                        label = str(spec.get("label", "") or "").strip()
                        if label and label not in keep_slots:
                            keep_slots.append(label)
            keep_slots = keep_slots[:2]
            fixed_json = patch_focus_slimming(fixed_json, must_keep_slots=keep_slots)
            fix_summary = build_fix_summary(final_json or {}, fixed_json or {})
            changed_fields = fix_summary.get("changed_fields", changed_fields)
            if changed_fields:
                log_msg += " | 已执行测点收敛并保留前提槽位"

        return {
            "final_json": fixed_json,
            **derived_state,
            "current_question_type": question_type,
            "locked_question_type": locked_question_type or question_type,
            "fix_summary": fix_summary,
            "fix_no_change": fix_no_change,
            "fix_attempted_regen": force_regen_used,
            "fix_required_unmet": fix_required_unmet,
            "llm_trace": llm_records,
            "logs": [log_msg],
            "was_fixed": True,
            **calc_state_updates,
        }
    except Exception as e:
        return {"llm_trace": llm_records, "logs": [f"❌ 修复者错误: {str(e)}"]}

# --- Edges ---
def critical_decision(state: AgentState):
    """
    智能决策函数：根据 Critic 结果决定下一步
    - pass: 审核通过 → END
    - fix: 轻微问题 → Fixer 修复
    - reroute: 严重问题 → Router 重新路由
    - self_heal: 超限 → 自愈输出
    """
    critic_result = state.get('critic_result', {})
    retry_count = int(state.get('retry_count', 0) or 0)
    # 轮次计数口径：MAX_QUESTION_RETRY_ROUNDS 作用于“当前路由轮次(run)”内的 critic->fixer 循环，
    # 而不是整道题的全局累计次数。router_round 在每次进入 router 时写入当时的 retry_count。
    router_round_base = int(state.get("router_round", 0) or 0)
    round_retry_count = max(0, retry_count - router_round_base)
    
    # 通过
    if critic_result.get('passed'):
        return "pass"

    # 若 Critic 判不通过依据来自非当前切片：直接 reroute（不走 Fixer）
    non_current_basis = bool(
        critic_result.get("non_current_slice_basis")
        or state.get("critic_non_current_basis")
    )
    if non_current_basis:
        return "reroute"

    # 配置冲突（如“禁出单选”切片 + 强制单选配置）直接自愈返回，避免无效循环
    fail_types = critic_result.get("fail_types") if isinstance(critic_result, dict) else []
    if isinstance(fail_types, list) and "question_type_config_conflict" in fail_types:
        return "self_heal"
    if isinstance(fail_types, list) and "no_question" in fail_types:
        return "reroute"

    # 超限自愈（按当前轮次计数，不跨 reroute 叠加）
    if round_retry_count >= MAX_QUESTION_RETRY_ROUNDS:
        return "self_heal"

    # 判断题特例：反向解题失败一律继续走 Fixer，不走 reroute
    # 触发条件：
    # 1) can_deduce_unique_answer 明确为 False；或
    # 2) fail_types 含 reverse_solve_fail（兼容历史/不同分支）
    question_type = state.get("current_question_type")
    reverse_solve_failed = (critic_result.get("can_deduce_unique_answer") is False) or (
        isinstance(fail_types, list) and "reverse_solve_fail" in fail_types
    )
    if question_type == "判断题" and reverse_solve_failed:
        return "fix"

    # Fixer 未满足必修项 → 强制重路由
    if state.get("fix_required_unmet"):
        return "reroute"
    
    # 判断问题严重程度
    issue_type = critic_result.get('issue_type', 'minor')
    final_json = state.get('final_json', {})
    was_fixed = isinstance(final_json, dict) and final_json.get('_was_fixed') is True
    
    # 失败一律先走 Fixer，确保真正修复
    if not was_fixed:
        return "fix"
    
    # 修复后若仅剩写作/格式类问题，继续走 Fixer；避免格式问题频繁 reroute
    soft_fail_types = {"format_fail", "explanation_fail", "writer_issue", "quality_fail"}
    hard_fail_types = {
        "reverse_solve_fail",
        "grounding_fail",
        "answer_mismatch",
        "code_check_fail",
        "calculation_answer_mismatch",
        "calc_missing_preconditions",
        "missing_preconditions",
    }
    if issue_type == "major" and isinstance(fail_types, list):
        fail_set = {str(x) for x in fail_types}
        if fail_set and fail_set.issubset(soft_fail_types) and fail_set.isdisjoint(hard_fail_types):
            return "fix"

    # 修复后仍为严重问题 → 重新路由
    if issue_type == 'major':
        return "reroute"
    
    # 轻微问题 → 继续修复
    return "fix"

# --- Graph Construction ---
# --- Code Execution (Safe Sandbox) ---
import sys
import io
import contextlib
from types import ModuleType

def execute_python_code(code: str, max_execution_time: float = 5.0) -> Tuple[Any, str, str]:
    """
    Safely execute dynamically generated Python code in a restricted environment.
    
    Args:
        code: Python code string to execute
        max_execution_time: Maximum execution time in seconds (default 5.0)
    
    Returns:
        tuple: (result_value, stdout_output, stderr_output)
    """
    # Create a restricted execution environment
    import builtins
    allowed_modules = {"math", "datetime", "decimal", "time", "_strptime"}

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if root not in allowed_modules:
            raise ImportError(f"Module '{name}' is not allowed")
        return builtins.__import__(name, globals, locals, fromlist, level)

    restricted_globals = {
        '__builtins__': {
            # Only allow safe built-in functions
            'abs': abs, 'round': round, 'min': min, 'max': max,
            'sum': sum, 'len': len, 'int': int, 'float': float, 'str': str,
            'bool': bool, 'type': type, 'isinstance': isinstance,
            'range': range, 'enumerate': enumerate, 'zip': zip,
            'print': print,  # For debugging
            '__import__': safe_import,
        },
        '__name__': '__main__',
        '__doc__': None,
    }
    
    restricted_locals = {}
    
    # Capture stdout and stderr
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    
    result_value = None
    
    try:
        with contextlib.redirect_stdout(stdout_capture), \
             contextlib.redirect_stderr(stderr_capture):
            # Execute the code
            exec(code, restricted_globals, restricted_locals)
            
            # Try to get the result (look for common result variable names)
            if 'result' in restricted_locals:
                result_value = restricted_locals['result']
            elif 'answer' in restricted_locals:
                result_value = restricted_locals['answer']
            elif 'value' in restricted_locals:
                result_value = restricted_locals['value']
            # If code ends with an expression, it won't be captured, but that's OK
    
    except Exception as e:
        error_msg = f"Execution error: {type(e).__name__}: {str(e)}"
        stderr_capture.write(error_msg)
        result_value = None
    
    stdout_str = stdout_capture.getvalue()
    stderr_str = stderr_capture.getvalue()
    
    return result_value, stdout_str, stderr_str


def calculator_node(state: AgentState, config):
    llm_records: List[Dict[str, Any]] = []
    agent_name = "CalculatorAgent"
    kb_chunk = state['kb_chunk']
    term_locks = state.get("term_locks") or []
    kb_context = format_kb_chunk_full(kb_chunk)
    reroute_basis_context = state.get("reroute_basis_context") or state.get("prev_critic_rules_context")
    if state.get("retry_count", 0) > 0 and isinstance(reroute_basis_context, str) and reroute_basis_context.strip():
        kb_context = reroute_basis_context
    mastery = kb_chunk.get('掌握程度', '未知')
    term_lock_text = ""
    if term_locks:
        term_lock_text = f"""
# 专有名词锁词约束（必须执行）
以下术语若在题干/选项/解析中使用，必须保持原词，不得同义替换、缩写替换或解释性改写：
{json.dumps(term_locks, ensure_ascii=False)}
"""
    
    # ✅ Smart model switching: Check GPT rate limit and switch to Deepseek if needed
    calc_model = CALC_MODEL or MODEL_NAME
    calc_api_key = API_KEY
    calc_base_url = BASE_URL
    calc_provider = "ait"  # Default provider
    
    # Check if GPT model is rate-limited
    if calc_model and calc_model.lower().startswith("gpt") and "api.deepseek.com" in (calc_base_url or ""):
        throttle_path = Path(".gpt_rate_limit.txt")
        if throttle_path.exists():
            try:
                last_ts = float(throttle_path.read_text(encoding="utf-8").strip() or "0")
                now = time.time()
                elapsed = now - last_ts
                wait_needed = max(0, 12 - elapsed)
                
                # If need to wait > 5 seconds, switch to Deepseek
                if wait_needed > 5:
                    print(f"⚠️ GPT 限流中（需等待 {int(wait_needed)}s），切换到 Deepseek")
                    calc_model = "deepseek-chat"
                    # Keep using default API_KEY and BASE_URL
            except Exception as e:
                print(f"⚠️ 限流检测失败: {e}，使用默认模型")
    
    # Step 1: Fetch examples FIRST (照猫画虎)
    retriever = config['configurable'].get('retriever') or get_default_retriever()
    question_type = config['configurable'].get('question_type')
    router_details = state.get('router_details', {})
    rec_type = router_details.get('recommended_type', '单选题')
    target_type, preferred_types = resolve_target_question_type(
        configured_question_type=question_type,
        recommended_type=rec_type,
        kb_chunk=kb_chunk,
        retriever=retriever,
    )
    
    examples = []
    if retriever:
        examples = retriever.get_examples_by_knowledge_point(kb_chunk, k=3, question_type=target_type)
    
    # Step 2: Decide if calculation is needed based on examples and material
    # If examples contain calculation questions, we should also do calculation
    examples_have_calculations = False
    if examples:
        # Check if any example's explanation mentions numbers or calculations
        for ex in examples:
            explanation = str(ex.get('解析', ''))
            # Simple heuristic: if explanation contains digits or common calc keywords
            if any(keyword in explanation for keyword in ['计算', '公式', '=', '×', '÷', '%', '元', '平方米', '年']):
                examples_have_calculations = True
                break
    
    # Step 2: Generate Python code dynamically
    prompt_code_gen = f"""
# 角色
你是计算专家 (CalculatorAgent)。
你的任务是根据参考材料中的计算规则，动态生成Python代码来计算结果。
当前知识点的掌握程度要求为: 【{mastery}】。
{term_lock_text}

# 参考材料
{kb_context}

# 参考范例分析
范例中{'包含' if examples_have_calculations else '不包含'}计算题。你应该{'优先生成计算代码' if examples_have_calculations else '分析是否需要生成计算代码'}。

# 重要提示：计算步骤分析
**计算可能只是解决整个问题的一个步骤，而不是整个问题！**

在分析需要生成什么代码时，请仔细思考：
1. **题目问的是什么？**（最终答案是什么）
2. **需要计算什么？**（能解决哪个步骤）
3. **是否需要多步计算？**（计算结果是否需要进一步处理）

例如：
- 如果题目问"房龄是多少年"，代码可以是：`result = current_year - completion_year`
- 如果题目问"最长贷款年限是多少年"，可能需要：
  ① 先计算房龄：`house_age = current_year - completion_year`
  ② 再根据"房龄+贷款年限≤50年"计算：`max_loan_years = 50 - house_age`
  ③ 可能还需要考虑借款人年龄等其他因素，取最小值

# 任务
1. **仔细分析**：题目最终问的是什么？需要计算什么？
2. **提取参数与变量重构 (Variable Refactoring)**：
   - **识别变量**：从原文中识别出计算所需的关键变量（如：原值、网签价、税率阈值）。
   - **参数重置**：如果原文提供的是一个具体案例（如"原价180万"），**你必须修改这个数值**，以便生成一道全新的题目。
     - ⚠️ 约束：修改后的数值必须符合业务逻辑（例如：网签价通常高于原值，日期必须在政策有效期内）。
     - 例子：原文 `price = 180`, `guidance_price = 218`。你应该设定 `price = 200`, `guidance_price = 230`（保持大小关系不变）。
   - **常量保留**：政策规定的固定数值（如税率5%、年限5年）不能修改。
3. **生成Python代码**：根据计算规则，生成完整的Python代码来计算结果
4. **如果不包含可计算的数值逻辑**，返回无需计算

{CALCULATION_GUIDE}
{CALC_PARAMETER_GROUNDING_GUIDE}

# 计算代码硬约束（必须遵守）
1. 只要当前切片存在公式、比例、阈值、年限、税率、金额、面积、套数等可运算规则，就必须输出 `need_calculation=true` 并生成代码。
2. 代码必须算出“最终要放进正确选项里的数值结果”，不能只停留在无用中间值；若确实有中间步骤，也必须继续算到最终答案。
3. 代码最后必须把最终结果赋给变量 `result`，且该结果后续将直接用于校验正确选项。
4. 如果你无法把最终结果算清楚，就不要输出模糊题目；应优先重构数据后再算。

# 输出 JSON
{{
    "need_calculation": true/false,
    "python_code": "result = ...",  // 完整的Python代码，最后将结果赋值给result变量
    "extracted_params": {{  // 从材料中提取的参数（用于说明）
        "param1": "value1的说明",
        ...
    }},
    "reason": "为什么需要这个计算..."
}}

注意：
- `python_code` 必须是有效的Python代码
- 代码最后必须将结果赋值给变量 `result`
- 代码应该是独立的，不依赖外部函数（除了内置函数）
- 处理边界情况（如除零检查）
"""
    # 计算代码生成节点固定使用 CODE_GEN_MODEL（默认 gpt-5.3-codex）
    code_gen_model = CODE_GEN_MODEL or "gpt-5.3-codex"
    code_gen_api_key = CODE_GEN_API_KEY or calc_api_key or API_KEY
    code_gen_base_url = CODE_GEN_BASE_URL or calc_base_url or BASE_URL
    code_gen_provider = resolve_code_gen_provider(code_gen_model, CODE_GEN_PROVIDER or calc_provider, None)
    
    print(f"🧮 计算专家: 使用模型 {code_gen_model} 生成计算代码")
    code_gen_content, _, llm_record = call_llm(
        node_name="calculator.codegen",
        prompt=prompt_code_gen,
        model_name=code_gen_model,
        api_key=code_gen_api_key,
        base_url=code_gen_base_url,
        provider=code_gen_provider,
        trace_id=state.get("trace_id"),
        question_id=state.get("question_id"),
    )
    llm_records.append(llm_record)
    
    calc_result = None
    generated_code_str = None
    code_status = "no_calculation"
    plan = {}
    
    try:
        plan = parse_json_from_response(code_gen_content)
        
        if plan.get("need_calculation") and plan.get("python_code"):
            generated_code_str = plan.get("python_code", "").strip()
            
            # Execute the generated Python code
            result_value, stdout_str, stderr_str = execute_python_code(generated_code_str)
            
            if stderr_str:
                # Execution error
                code_status = "error"
                calc_result = f"Execution Error: {stderr_str}"
                print(f"Code Execution Error: {stderr_str}")
            elif result_value is not None:
                # Success
                code_status = "success"
                calc_result = result_value
                print(f"Code Execution Success: result = {calc_result}")
            else:
                # No result variable found, try to parse from stdout
                code_status = "success_no_result"
                calc_result = stdout_str.strip() if stdout_str.strip() else None
                
    except Exception as e:
        print(f"Code Generation Error: {e}")
        code_status = "error"
        calc_result = f"Error: {str(e)}"
        
    # Step 3: Generate Question (with calculation result and examples)
    
    # 根据模式与题型调整提示词
    generation_mode = state.get("current_generation_mode") or config['configurable'].get('generation_mode', '随机')
    effective_generation_mode, normalized_generation_mode = resolve_effective_generation_mode(generation_mode, state)
    question_type = config['configurable'].get('question_type')
    difficulty_range = config['configurable'].get('difficulty_range')
    if question_type not in ["随机", "单选题", "多选题", "判断题"]:
        question_type = "随机"
    
    loan_formula_parentheses_sensitive = bool(
        re.search(
            r"较小值（评估值、网签价）\s*×\s*商业贷款成数\s*-\s*公积金贷款部分额度",
            kb_context,
        )
    )
    calc_disambiguation_instruction = ""
    if loan_formula_parentheses_sensitive:
        calc_disambiguation_instruction = """
# 公式歧义消解硬约束（必须遵守）⚠️
- 涉及“商业贷款部分额度=较小值（评估值、网签价）×商业贷款成数-公积金贷款部分额度”时，
  题干或解析必须显式写成“（较小值×商业贷款成数）-公积金贷款部分额度”，并明确“先乘后减”。
- 禁止写成可能被理解为“较小值×（成数-公积金额度）”的表达。
"""

    uniqueness_note = ""
    avoid_superlative = "   - **避免\"最XX\"考法**：禁止用\"最重要/最关键/重点/主要\"等表述设计题干或选项，重点考察完整流程、条件、责任边界或操作要点。"
    if target_type == "单选题":
        uniqueness_note = "   - **唯一正确性**：确保只有一个选项严格符合教材原文或计算结论，其他选项须有明确错误点，避免 A/B 都似乎正确的歧义。"

    if target_type == "判断题":
        type_instruction = "题型要求：判断题。选项必须固定为：['正确','错误']。答案必须是 'A' 或 'B'。括号格式：题干末尾必须精确写成“（　）”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后不能再加句号。"
    elif target_type == "多选题":
        type_instruction = "题型要求：多选题。至少4个选项。答案必须是列表形式，如 ['A','C','D']。括号格式：题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。"
    else:
        type_instruction = "题型要求：单选题。4个选项且只有一个正确。答案必须是单个字母，如 'A'。括号格式：题干末尾必须精确写成“（　）。”；括号必须是中文全角括号，括号内有且仅有一个全角空格，括号前不能有任何符号或空格，括号后一律紧跟中文句号。"
    
    mapped_type_hint = ""
    if question_type == "随机" and preferred_types:
        mapped_type_hint = f"""
# 随机题型优先规则
当前切片关联母题题型优先集合：{preferred_types}。
本题请按已选定题型【{target_type}】生成。
"""

    # Build difficulty instruction
    difficulty_instruction = ""
    if difficulty_range:
        min_diff, max_diff = difficulty_range
        difficulty_instruction = f"""
# 难度要求（必须严格遵守）⚠️
**题目难度值必须在 {min_diff:.1f} 到 {max_diff:.1f} 之间**。

难度控制方法：
- **简单题 (0.3-0.5)**：直接考察知识点定义、基础概念，干扰项仍需同维度且贴近常见误判
- **中等题 (0.5-0.7)**：需要理解知识点含义并应用到场景，干扰项似是而非，需要仔细分析
- **困难题 (0.7-0.9)**：需要综合多个知识点、复杂计算或多步推理，干扰项高度相似

**重要**：生成的题目难度值必须落在指定范围内，否则会被拒绝。请根据难度要求调整：
- 题干复杂度（简单题用直接表述，困难题用复杂场景）
- 干扰项相似度（简单题也应“看起来合理但错误”，困难题可进一步提高相似度）
- 所需推理步骤（简单题直接答案，困难题需要多步推理）
"""
    
    mode_instructions = build_mode_instruction(effective_generation_mode, normalized_generation_mode)
    
    prompt_gen = f"""
# 角色
你是计算专家 (CalculatorAgent)。
请基于【参考材料】创作一道**需要数值计算**的{target_type}。
当前知识点的掌握程度要求为: 【{mastery}】。
{term_lock_text}

# 好题标准（必须遵守）
## 四大核心要求
1. **聚焦考点**：计算题必须围绕教材切片的计算规则，可直接知识点考察或业务场景考察（由筛选条件决定）。
2. **直接不拐弯**：计算考点直接明确，避免复杂陷阱，让学员清楚知道要计算什么。
3. **简洁不啰嗦**：题干提供计算所需条件即可，避免冗余信息干扰。
4. **真诚说人话**：用通俗易懂的表达，避免生僻词，数值设置符合常识（如房价不能是1元或1亿元）。

{type_instruction}
{mapped_type_hint}

# 简化场景，符合实际（必须遵守）⚠️
1. **无意义的场景铺垫不要**：直接陈述计算场景，去掉"某某告诉某某"、"在培训时了解到"等冗余铺垫。
2. **和题目无关联的句子不要**：只保留与计算相关的关键数据，去掉对计算没有影响的背景描述。
3. **太长的句子不要**：简化表述，突出计算所需的核心条件和数据。
4. **简化数字（计算题必须严格遵守）**：
   - ✅ 优先使用整数：总户数400户，车位100个，配比1:4
   - ✅ 可用简单小数：0.5、1.5、2.5等
   - ❌ 禁止复杂小数：1.328、2.876、3.14159等
   - **原则**：让考生能够口算或简单笔算，不需要计算器才能算出答案。

# 题干/设问规范（必须遵守）
1. **题干括号位置**：
   - 题干中的括号不能在句首，可放在句中或句末。
   - 选择题题干句末要有句号，句号在最后；判断题题干句子完结后加一个括号，括号在最后。
2. **括号格式**：
   - 使用中文括号，括号内部有且仅有一个全角空格（不能多）：`（　）`
   - 括号前后不允许空格
3. **设问表达**：
   - 设问须用陈述句，禁止使用问号（？）；不得以疑问句形式设问。
   - 少用否定句，禁止使用双重否定句；禁止“不是不”“并非不”等易歧义表述。
   - **遣词造句与指代一致**：题干注意主谓搭配与指代一致，避免指代对象错误导致语义偏差。
   - **判断题要求**：判断题必须是肯定陈述句，并明确出现“正确”或“错误”锚点；不要写成“是否正确/对不对/是不是”这类疑问式。
4. **选择题设问表述**：题干须为陈述句，以（　）作答占位结尾（句号在括号后）。不强制固定使用某一种模板化结尾。

# 选项规范（必须遵守）
0. **选项输出格式（严禁违反，否则会出现 A. A 网签… 双重序号）**：
   - **options 数组中只填选项正文**，禁止在每项前写 A./B./C./D. 或 A、B、等序号；系统会按 A/B/C/D 自动显示，写序号会导致展示时出现双重序号。
   - 正确示例（单选题）：options 填四句正文，如 ["网签合同信息一旦录入系统便无法修改，可能导致过户失败", "线上过户无法调取网签合同，可能影响客户提取公积金", ...]；判断题：["正确", "错误"]。
   - 错误示例：不要写 ["A. 网签...", "B. 线上..."] 或 ["A", "B", "C", "D"]，否则展示会变成 A. A 网签… 双重序号。
1. **选项数量与正确性**：
   - 选择题每题4个选项；单选题仅1个正确，多选题≥2个正确。
   - 多选题中正确选项数量要合理，不要多道题都只有1个答案。
2. **标点与语义**：
   - 每个选项末尾不添加标点符号。
   - 选项必须与题干组成完整语句（将选项代入题干括号处语义完整）。
   - **选项单位**：选项中有单位时，**必须**将单位提到题干中，**不得**在选项中反复出现单位。选项不得包含数值单位（如元、万元、平方米、年、%等）；单位应写在题干设问处（如「……额度为（　）万元」则选项只写 6、8、10、12）。
3. **一致性与干扰项**：
   - 选项中的姓名与题干中的姓名保持一致，不出现题目未涉及的姓名。
   - 干扰项必须具有干扰性，选项本身应是存在或相关的内容，不能无意义。
4. **数值型选项**：
   - 选项为数字时按从小到大顺序排列。
   - 计算题尽量简单（能口算优先），确需保留小数时注明保留位数（一般1-2位）。
   - 非计算题若解题过程涉及运算（如比例、折算、阈值比较），同样执行“简算优先”：避免复杂小数与冗长多步计算，不应依赖计算器。
   - **保留位数说明（必须）**：当答案或选项含小数时，题干中必须包含“保留到X位小数”或“精确到X位小数”的说明。
   - 未被选中的数值选项也必须有计算依据，不可胡编乱造。

# 解析规范（必须遵守）
1. **三段式（必须带段首序号 1、2、3、）**：第一段以"1、教材原文："开头，须包含路由前三个标题（目标题内容）与分级及教材要点，不要写「目标题：」字样；第二段以"2、试题分析："开头，用自己的话解释；第三段以"3、结论："开头，以"本题答案为X"收束。
2. **结论写法**：
   - 判断题写本题答案为正确/错误，不得写成本题答案为A/B。
   - 选择题写本题答案为A/B/C/D/AB/AC...。
3. **严禁**：直接粘贴教材原文表格或图片（可改成文字描述）；试题分析段不得整段粘贴教材原文。
4. **一致性**：答案与解析必须一致，计算题必须与计算过程一致。
5. **典型错题规避**：
   - 题干/选项/解析出现多字、少字、错字，影响作答。
   - 题干与选项/解析前后不一致。
   - 计算题无正确答案或答案与计算过程不一致。
   - 题目超纲或概念过时（如旧业务名/过期协议）。
   - 场景严重脱离经纪业务实际。
   - 干扰选项存在争议或与正确答案同样成立。

{mode_instructions}

{difficulty_instruction}
{calc_disambiguation_instruction}

# 计算上下文
生成的Python代码: {generated_code_str if generated_code_str else "无需计算"}
执行结果: {calc_result}
执行状态: {code_status}

**重要提示：理解计算步骤**
- 计算器可能只是解决整个问题的一个步骤，而不是整个问题
- 如果题目问的是最终结果，可能需要多步计算：
  ① 计算器结果（如：房龄）
  ② 基于计算器结果进一步计算（如：贷款年限 = 50 - 房龄）
  ③ 可能还需要考虑其他因素（如：借款人年龄），取最小值
  
**生成题目时的要求：**
- 如果计算器结果就是最终答案：直接使用计算结果作为正确答案
- 如果计算器结果只是中间步骤：需要在题干中提供完整信息，让答题者能够完成所有计算步骤
- 在解析中必须说明完整的计算过程，包括：
  ① 第一步：使用计算器计算什么（如：房龄 = 50 - (2025-1993) = 18年）
  ② 第二步：基于第一步结果计算什么（如：贷款年限上限 = 50 - 18 = 32年）
  ③ 第三步：考虑其他因素（如：借款人年龄限制），取最小值
  ④ 最终答案

(如果结果不为 None，你**必须**使用该计算结果，但需要理解它可能是中间步骤还是最终答案。{'可不构建业务场景，直接围绕知识点计算规则命题。' if effective_generation_mode == '基础概念/理解记忆' else '需要构建业务场景并匹配使用的参数。'})

# 计算题数值闭环硬约束（违反会被直接驳回）⚠️
1. 正确答案对应的选项，必须与最终计算结果一致。
2. 解析中必须写出清晰的计算链路，并明确算出与正确选项一致的最终数值。
3. 禁止出现“解析算出一个数，正确选项却是另一个数”。
4. 禁止出现“正确答案字母正确，但该选项文本数值不等于计算结果”。
5. 若当前代码执行失败或没有最终结果，不得硬编数值题；应改写为可验证的计算题。
6. 必须在题干与解析中锁定“参数来源/统计口径/时间口径”，避免同题多解。
7. 涉及“上浮一个职级/浮动范围”时，题干必须明确“不能分割退回”作为触发条件，并明确本题级别的上浮路径；禁止“所有级别均可上浮”这类泛化写法。

# 质量标准 (必须达成):
1. **准确性 (40%)**: 100% 事实准确。如果有计算结果 {calc_result}，必须使用。
2. **干扰项质量 (25%)**: 错误选项必须似是而非。
   - **干扰项设计技巧**：利用**"相近的数字"**（如正确答案是某个数值，干扰项用相近的数值，如正确答案是30万元，干扰项用25万元或35万元）或**"错误的参照物"**（如混淆不同概念、用类似但不正确的表述，如混淆"评估价"和"成交价"）
{uniqueness_note}
{avoid_superlative}
3. **相关性 (15%)**: 考察核心概念在经纪业务、合规或客户服务中的应用，避免纯记忆性细节。
4. **格式 (10%)**: 严格的 JSON 输出。

# 自检清单（必须逐条核对）
1. **唯一答案**：题干条件足以排除其他选项，不能出现两条合理路径。
2. **解析规范**：三段式完整，结论以“本题答案为X”收束。
3. **一致性**：题干/选项/答案/解析前后一致，计算题与计算过程一致。
4. **适纲性**：不超纲，不引入材料外条件或结论。
5. **人名与措辞**：人名规范、无生造词、无模糊词。
6. **维度一致**：选项同维度，干扰项有理有据。
7. **禁用兜底选项**：选项不得出现「以上都对」「以上都错」「皆是」「皆非」等；若命中须改写为同维度干扰项。
8. **长度限制**：题干≤400字、单选项≤200字；解析仅要求“教材原文”段尽量≤400字，整段解析不设硬性上限。超长时仅删减非核心句，并剔除与解题无关的表述。

# 参考材料
{kb_context}

# 范例 (请模仿以下题目的出题风格)
"""
    for i, ex in enumerate(examples, 1):
        prompt_gen += f"例 {i}: {ex['题干']}\n"

    prompt_gen += """
# 题干一致性自检（必须执行）
1. 基于【当前切片 + 上一级切片全集 + 相似切片】检查题干与解析是否存在冲突或不一致。
2. 若发现不一致，必须输出“问题清单”，说明冲突维度、冲突点、修复建议。

# 任务
返回 JSON（options 只填选项正文，不要写 A/B/C/D 或 A. B. 等序号）: 判断题 {{"options": ["正确", "错误"], ...}}；选择题 {{"options": ["第一项正文", "第二项正文", "第三项正文", "第四项正文"], "answer": "A 或 A/B/C", "explanation": "...", "self_check_issues": [...]}}
约束: 题干中**禁止**出现"根据材料"或"依据参考资料"。

# 题目质量硬性约束（违反会被 Critic 驳回）⚠️
## 1. 禁止使用模糊的日常用语：题干中**禁止**使用"实实在在的特点"、"重要的信息"、"关键因素"等模糊表述，这类词在汉语中可能指向多个维度，会导致歧义。应使用明确、可操作的表述。
## 2. 选项维度一致性：所有选项必须在同一维度内做区分（如考实物信息则选项都是户型/面积/朝向/装修等）；**禁止**跨维度（如A法律、B实物、C位置、D价格），否则无法真正考察专业知识。干扰项应与正确答案同维度但略有不同。
## 3. 对经纪人工作有帮助：题目须对经纪人工作有正向作用（实操题、规则理解、合规、文化等均可）。公司制度/合规红线/禁止性规定/时效阈值/标准口径/企业文化与价值观口径等“要求背诵并执行”的知识点允许直接命题，不因“偏记忆”被否决。**禁止**：（1）仅考「定义 vs 目的 vs 方式」等概念归类、对工作无帮助的题；（2）仅考「教材把哪一条称为核心/主要/关键」的刁钻题；（3）常识与切片表述易冲突的题；（4）流程/步骤类未明确主体或视角时，不出因主体/视角不同会产生歧义的题或选项；（5）选项与题干条件相悖（题干已设定某事实成立时，选项不得出现与该事实矛盾的表述）；（6）教材规则中的触发条件、适用范围、约束主体、作用对象、角色边界、时间/流程时点不得缺失或被改写为无条件绝对命题。可出场景化题，也可出对工作有帮助的理解/记忆题。
"""
    # 计算题初稿仍沿用 calculator 的选模逻辑，不与 codegen 强绑定
    draft_model = calc_model if calc_model else MODEL_NAME
    draft_api_key = calc_api_key
    draft_base_url = calc_base_url
    draft_provider = resolve_provider(draft_model, draft_base_url, calc_provider)
    print(f"🧮 计算专家: 使用模型 {draft_model} 生成初稿")
    content, _, llm_record = call_llm(
        node_name="calculator.draft",
        prompt=prompt_gen,
        model_name=draft_model,
        api_key=draft_api_key,
        base_url=draft_base_url,
        provider=draft_provider,
        trace_id=state.get("trace_id"),
        question_id=state.get("question_id"),
    )
    llm_records.append(llm_record)
    
    try:
        parsed = parse_json_from_response(content)
        draft = _ensure_draft_v1(parsed if isinstance(parsed, dict) else {})
        def _needs_city6_lock(d: Dict[str, Any]) -> bool:
            if not isinstance(d, dict):
                return False
            q_txt = str(d.get("question", "") or "")
            all_txt = "\n".join(
                [
                    q_txt,
                    *(str(x or "") for x in (d.get("options") or [])),
                    str(d.get("explanation", "") or ""),
                ]
            )
            uses_city6_pricing = bool(re.search(r"(1560|4000)\s*元", all_txt))
            return uses_city6_pricing and ("城六区" not in q_txt)
        def _needs_non_split_lock(d: Dict[str, Any]) -> bool:
            if not isinstance(d, dict):
                return False
            q_txt = str(d.get("question", "") or "")
            all_txt = "\n".join(
                [
                    q_txt,
                    *(str(x or "") for x in (d.get("options") or [])),
                    str(d.get("explanation", "") or ""),
                ]
            )
            uses_float_rule = bool(re.search(r"(上浮一个职级|浮动范围|1560|4000)", all_txt))
            return uses_float_rule and ("不能分割退回" not in q_txt)

        if target_type == "多选题":
            ans_labels = _parse_answer_labels(draft.get("answer", ""))
            if len(ans_labels) < 2 or _needs_city6_lock(draft) or _needs_non_split_lock(draft):
                force_multi_prompt = f"""
# 任务
你上一次计算题初稿不满足【多选题契约】或【区域口径契约】，必须重写。

# 必须满足的硬约束（任何一条不满足都算失败）
1. 题型必须是多选题，`answer` 必须为至少2个字母（如 "AB" 或 ["A","C"]）。
2. 禁止产出“单答案金额题”模板（即只问一个金额然后四个数字里选一个）；多选题应改为“以下说法正确的有（　）。”这类多判断项结构。
3. 若题干/选项/解析使用了 `1560元/㎡` 或 `4000元/㎡`，题干必须显式写明“城六区”这一口径；否则不要使用该单价。
4. 若题干/选项/解析使用了“上浮一个职级”或“浮动范围”规则，题干必须显式写明“不能分割退回”这一触发条件。
5. 禁止写“所有级别均可上浮”这类泛化表述；请只在本题具体级别上给出确定计算路径。
6. 题干、正确答案、解析三者必须一致，且可唯一推导。
7. 只返回 JSON：question/options/answer/explanation/difficulty/self_check_issues。

# 当前切片
{kb_context}

# 当前草稿（不合格）
{json.dumps(draft, ensure_ascii=False)}

# 计算上下文（可用于构造干扰项）
执行结果: {calc_result}
生成代码: {generated_code_str if generated_code_str else "(无)"}
"""
                retry_content, _, retry_llm_record = call_llm(
                    node_name="calculator.redraft_multiselect",
                    prompt=force_multi_prompt,
                    model_name=draft_model,
                    api_key=draft_api_key,
                    base_url=draft_base_url,
                    provider=draft_provider,
                    trace_id=state.get("trace_id"),
                    question_id=state.get("question_id"),
                )
                llm_records.append(retry_llm_record)
                try:
                    retry_parsed = parse_json_from_response(retry_content)
                    retry_draft = _ensure_draft_v1(retry_parsed if isinstance(retry_parsed, dict) else {})
                    retry_labels = _parse_answer_labels(retry_draft.get("answer", ""))
                    if (
                        len(retry_labels) >= 2
                        and not _needs_city6_lock(retry_draft)
                        and not _needs_non_split_lock(retry_draft)
                        and ("所有级别" not in str(retry_draft.get("question", "") or ""))
                    ):
                        draft = retry_draft
                        parsed = retry_parsed if isinstance(retry_parsed, dict) else parsed
                    else:
                        self_check_retry_issue = "多选题契约重生后仍未满足（答案数量或城六区口径）"
                        if isinstance(parsed, dict):
                            raw_issues = parsed.get("self_check_issues") if isinstance(parsed.get("self_check_issues"), list) else []
                            parsed["self_check_issues"] = list(raw_issues) + [self_check_retry_issue]
                except Exception:
                    if isinstance(parsed, dict):
                        raw_issues = parsed.get("self_check_issues") if isinstance(parsed.get("self_check_issues"), list) else []
                        parsed["self_check_issues"] = list(raw_issues) + ["多选题契约重生解析失败"]

        draft_question_text = str(draft.get("question", "") or "")
        draft_options = [str(x or "") for x in (draft.get("options") or [])]
        calc_target_signature = _extract_calc_target_signature(draft_question_text)
        calc_unit_hint = _infer_calc_unit_hint(
            draft_question_text,
            draft_options,
            str(draft.get("explanation", "") or ""),
        )
        calc_required_slots = _extract_required_calc_slots(kb_chunk)
        calc_missing_slots = _detect_missing_calc_slots(draft_question_text, calc_required_slots)
        self_check_issues = parsed.get("self_check_issues") if isinstance(parsed, dict) else []
        if not isinstance(self_check_issues, list):
            self_check_issues = []
        
        log_msg = f"🧮 计算专家: 初稿已生成"
        if calc_result is not None:
            log_msg += f" (已执行动态代码, 结果={calc_result})"
        elif generated_code_str:
            log_msg += f" (已生成代码，但执行失败: {code_status})"
        
        # Ensure random question type is visible in logs when 随机
        calc_logs: List[str] = []
        if question_type == "随机":
            calc_logs.append(f"🎲 随机题型：本题已选定【{target_type}】")
        calc_logs.append(f"{log_msg}（筛选条件={effective_generation_mode}）")
        # Pass current_question_type so writer/critic use same type when 随机
        return {
            "draft": draft,
            "tool_usage": {
                "method": "dynamic_code_generation",
                "generated_code": generated_code_str,
                "extracted_params": plan.get("extracted_params", {}),
                "result": calc_result,
                "code_status": code_status
            },
            "execution_result": calc_result,  # Pass to critic_node
            "calculator_model_used": draft_model,
            "generated_code": generated_code_str,
            "code_status": code_status,
            "calc_target_signature": calc_target_signature,
            "calc_unit_hint": calc_unit_hint or None,
            "calc_required_slots": calc_required_slots or [],
            "calc_missing_slots": calc_missing_slots or [],
            "examples": examples,
            "current_generation_mode": effective_generation_mode,
            "current_question_type": target_type,
            "locked_question_type": target_type,
            "self_check_issues": self_check_issues,
            "llm_trace": llm_records,
            "logs": calc_logs,
        }
    except Exception as e:
        return {
            "calculator_model_used": draft_model,
            "calc_target_signature": "",
            "calc_unit_hint": None,
            "calc_required_slots": [],
            "calc_missing_slots": [],
            "llm_trace": llm_records,
            "logs": [f"❌ 计算专家错误: {str(e)} \nContent: {content}"]
        }

# --- Graph Construction ---
workflow = StateGraph(AgentState)

workflow.add_node("router", router_node)
workflow.add_node("specialist", specialist_node)
workflow.add_node("calculator", calculator_node)  # 计算专家节点
workflow.add_node("writer", writer_node)
workflow.add_node("critic", critic_node)
workflow.add_node("fixer", fixer_node)

workflow.set_entry_point("router")

# Conditional Edge for Router
def route_agent(state):
    agent_name = state.get('agent_name', 'GeneralAgent')
    # 支持新旧名称兼容
    if agent_name in ["CalculatorAgent", "FinanceAgent"]:
        return "calculator"
    else:
        return "specialist"

workflow.add_conditional_edges(
    "router",
    route_agent,
    {
        "calculator": "calculator",
        "specialist": "specialist"
    }
)

workflow.add_edge("specialist", "writer")
workflow.add_edge("calculator", "writer")  # Calculator also goes to Writer
workflow.add_edge("writer", "critic")

# Critic 的智能决策：支持多路径
workflow.add_conditional_edges(
    "critic",
    critical_decision,
    {
        "pass": END,              # 通过 → 结束
        "fix": "fixer",          # 轻微问题 → Fixer 修复
        "reroute": "router",     # ✅ 严重问题 → 回到 Router 重新路由
        "self_heal": END          # 超限自愈 → 结束
    }
)

# Fixer 修复后回到 Critic 验证
workflow.add_edge("fixer", "critic")  # ✅ Fixer → Critic 循环

app = workflow.compile()
