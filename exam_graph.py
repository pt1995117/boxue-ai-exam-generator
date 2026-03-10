import os
import json
import operator
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Annotated, List, Dict, Optional, TypedDict, Union, Any, Tuple
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field
from openai import OpenAI
from volcenginesdkarkruntime import Ark

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

def normalize_blank_brackets(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    # Normalize empty brackets to Chinese format with inner space and no outer spaces
    return re.sub(r"\s*[(（]\s*[)）]\s*", "（ ）", text)

def has_invalid_blank_bracket(text: str) -> bool:
    if not isinstance(text, str) or not text:
        return False
    # Any empty bracket not exactly "（ ）" is invalid
    for match in re.finditer(r"[(（]\s*[)）]", text):
        if match.group(0) != "（ ）":
            return True
    # No spaces allowed around the bracket
    if re.search(r"\s（ ）|（ ）\s", text):
        return True
    return False

def enforce_question_bracket_and_punct(text: str, target_type: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    t = normalize_blank_brackets(text.strip())
    # Remove leading bracket if present
    t = re.sub(r"^（ ）", "", t).lstrip()
    if target_type in ["单选题", "多选题"]:
        # Ensure a single blank bracket exists before the ending period
        if "（ ）" not in t:
            t = f"{t}（ ）"
        # Ensure ends with period, and bracket before period
        if t.endswith("（ ）"):
            t = f"{t}。"
        elif t.endswith("）") and not t.endswith("）。"):
            t = f"{t}。"
        if t.endswith("）。") is False and "（ ）" in t:
            # If period comes before bracket, move it to the end
            t = re.sub(r"。\s*（ ）", "（ ）。", t)
    elif target_type == "判断题":
        # Ensure ends with blank bracket and no trailing period
        if "（ ）" not in t:
            t = f"{t}（ ）"
        t = re.sub(r"（ ）\s*。$", "（ ）", t)
        if not t.endswith("（ ）"):
            t = f"{t}（ ）"
    return t

def validate_writer_format(question: str, options: List[str], answer, target_type: str) -> List[str]:
    issues = []
    q = question or ""
    if has_invalid_blank_bracket(q):
        issues.append("题干括号格式不规范")
    if target_type in ["单选题", "多选题", "判断题"]:
        if "（ ）" not in q:
            issues.append("题干缺少标准占位括号")
    if target_type in ["单选题", "多选题"]:
        if not q.endswith("。"):
            issues.append("选择题题干未以句号结尾")
        if "（ ）" in q and not q.endswith("）。"):
            issues.append("选择题括号与句号位置不规范")
    if target_type == "判断题":
        if not q.endswith("（ ）"):
            issues.append("判断题题干未以括号结尾")
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

COMMON_SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁荀羊於惠甄麴家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公万俟司马上官欧阳夏侯诸葛闻人东方赫连皇甫尉迟公羊澹台公冶宗政濮阳淳于单于太叔申屠公孙仲孙轩辕令狐钟离宇文长孙慕容鲜于闾丘司徒司空丌官司寇子车颛孙端木巫马公西漆雕乐正壤驷公良拓跋夹谷宰父谷梁晋楚闫法汝鄢涂钦段干百里东郭南门呼延归海羊舌微生岳帅缑亢况后有琴梁丘左丘东门西门商牟佘佴伯赏南宫墨哈谯笪年爱阳佟"

def _name_violations_in_text(text: str) -> List[str]:
    if not text:
        return []
    issues = []
    # 姓+女士/先生
    if re.search(rf"[{COMMON_SURNAMES}][\u4e00-\u9fff]{{0,1}}(女士|先生)", text):
        issues.append("使用了“姓+女士/先生”")
    # 小+常见姓氏（如小张/小李）——避免误伤“小区/小镇/小路”等非人名
    if re.search(rf"小[{COMMON_SURNAMES}](?:[\u4e00-\u9fff])?(?=$|[，。；：、\s])", text):
        issues.append("使用了“小+姓氏”称谓")
    return issues

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
    # Reuse writer format checks where applicable
    if has_invalid_blank_bracket(q):
        issues.append("题干括号格式不规范")
    if question_type in ["单选题", "多选题", "判断题"]:
        if "（ ）" not in q:
            issues.append("题干缺少标准占位括号")
    if question_type in ["单选题", "多选题"]:
        if not q.endswith("。"):
            issues.append("选择题题干未以句号结尾")
        if "（ ）" in q and not q.endswith("）。"):
            issues.append("选择题括号与句号位置不规范")
    if question_type == "判断题" and not q.endswith("（ ）"):
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
    return issues

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
            val = re.sub(r'^[A-HＡ-Ｈa-h][\.\、:：\s\)）]+', '', val, flags=re.IGNORECASE)
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
    2) 随机题型优先使用“当前切片已关联母题”的题型集合；
    3) 若无可用映射题型，回退 Router 推荐题型。
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
        if preferred_types:
            # 轻量轮转，避免长期只命中一种题型。
            idx = int(time.time() * 1000) % len(preferred_types)
            return preferred_types[idx], preferred_types
        return rec, []
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


def has_business_context(text: str) -> bool:
    """轻量判定题干是否包含业务场景语义。"""
    content = str(text or "")
    if not content.strip():
        return False
    keywords = [
        "客户", "业主", "经纪人", "门店", "带看", "签约", "过户", "交易",
        "税费", "贷款", "公积金", "合同", "房源", "咨询", "看房", "收佣", "服务",
    ]
    return any(k in content for k in keywords)

# --- State Definition ---
class AgentState(TypedDict):
    kb_chunk: Dict
    examples: List[Dict]
    agent_name: Optional[str]
    draft: Optional[Dict]
    final_json: Optional[Dict]
    critic_feedback: Optional[str]
    critic_result: Optional[Dict]  # ✅ Critic 验证结果 (passed, issue_type, reason)
    retry_count: int
    logs: Annotated[List[str], operator.add] # Append-only logs for UI
    term_locks: Optional[List[str]]  # Locked domain terms detected from kb chunk
    router_details: Optional[Dict]
    tool_usage: Optional[Dict]
    critic_tool_usage: Optional[Dict]
    critic_details: Optional[str]
    # Debug flag: force a single fix loop for Studio testing.
    debug_force_fail_once: Optional[bool]
    # ✅ Code-as-Tool: Dynamic code generation fields
    generated_code: Optional[str]  # Python code generated by LLM
    execution_result: Optional[Any]  # Result from executing the code
    code_status: Optional[str]  # 'success' or 'error'
    solver_commentary: Optional[str]  # Critic's independent solving explanation
    # ✅ Question type state transfer: Writer passes actual question type to downstream nodes
    current_question_type: Optional[str]  # Actual question type determined by Writer node
    current_generation_mode: Optional[str]  # Actual mode chosen for this question
    # ✅ Model switching: Track which model was actually used
    critic_model_used: Optional[str]  # Actual model used by Critic (for UI display)
    calculator_model_used: Optional[str]  # Actual model used by Calculator (for UI display)
    # LLM trace fields
    trace_id: Optional[str]
    question_id: Optional[str]
    llm_trace: Annotated[List[Dict[str, Any]], operator.add]
    llm_summary: Optional[Dict[str, Any]]
    unstable_flags: Optional[List[str]]

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
        return {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
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
        call_tokens = int(item.get("total_tokens") or 0)
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
        }

    if is_ark:
        started = time.time()
        ark_backoff_seconds = [2, 5, 10]
        for attempt in range(len(ark_backoff_seconds) + 1):
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
                    timeout=timeout,
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
    backoff_seconds = [5, 10, 20, 30, 45, 60, 60, 60, 60, 60]
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
                        timeout=timeout,
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

# --- Nodes ---

def router_node(state: AgentState, config):
    kb_chunk = state['kb_chunk']
    retriever = config['configurable'].get('retriever') or get_default_retriever()
    examples = state.get('examples', [])
    kb_context, parent_slices, related_slices = build_extended_kb_context(kb_chunk, retriever, examples)

    self_check_issues = state.get("self_check_issues") or []
    if not isinstance(self_check_issues, list):
        self_check_issues = []
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
    term_locks = detect_term_locks_from_kb(kb_chunk)
    
    recommended_type = "单选题" # Default
    if has_formulas:
        recommended_type = "单选题" # Calculation usually single choice
    elif has_list:
        recommended_type = "多选题" # Lists are perfect for multi-select
    elif has_tables:
        recommended_type = "判断题" # Tables are good for True/False checks on details
        
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
【特征】: 包含公式={has_formulas}, 包含列表={has_list}, 包含表格={has_tables}

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
        
        # Override agent based on rigid features if LLM missed it
        if has_formulas and agent != "CalculatorAgent":
             agent = "CalculatorAgent"
             reasoning += " (强制修正: 检测到公式)"
        
    except Exception as e:
        print(f"⚠️ Router JSON parsing failed: {e}. Defaulting to GeneralAgent.")
        agent = "GeneralAgent"
        score_calculation = 0
        score_legal = 0
        need_calculation = False
        reasoning = f"Parsing Error: {str(e)}"

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
            "term_locks": term_locks,
        },
        "term_locks": term_locks,
        "logs": [f"🤖 路由: 派发给 **{agent}** (特征: 公式={has_formulas}, 列表={has_list}, 表格={has_tables}). 建议题型: {recommended_type}"]
        ,
        "llm_trace": llm_records,
    }
    if term_locks:
        state_updates["logs"].append(f"🔒 Router 术语锁定: {', '.join(term_locks[:12])}")
    
    # 如果是重新路由（retry_count > 0），清理旧的生成结果
    if state.get('retry_count', 0) > 0:
        # Preserve previous question and critic feedback for repair-mode prompts
        state_updates["prev_final_json"] = state.get("final_json")
        state_updates["prev_critic_feedback"] = state.get("critic_feedback")
        state_updates["prev_critic_details"] = state.get("critic_details")
        state_updates["prev_critic_result"] = state.get("critic_result")
        state_updates["prev_critic_tool_usage"] = state.get("critic_tool_usage")
        state_updates["prev_critic_rules_context"] = state.get("critic_rules_context")
        state_updates["prev_critic_related_rules"] = state.get("critic_related_rules")
        state_updates["draft"] = None
        state_updates["self_check_issues"] = None
        state_updates["final_json"] = None
        state_updates["critic_feedback"] = None
        state_updates["critic_details"] = None
        state_updates["critic_result"] = None
        state_updates["critic_tool_usage"] = None
        state_updates["critic_rules_context"] = None
        state_updates["critic_related_rules"] = None
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
    target_type, preferred_types = resolve_target_question_type(
        configured_question_type=question_type,
        recommended_type=rec_type,
        kb_chunk=kb_chunk,
        retriever=retriever,
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

    # Question type control (strict)
    cfg_type = question_type
    if target_type == "判断题":
        type_instruction = (
            "题型要求：判断题。\n"
            "选项必须固定为：['正确','错误']。\n"
            "答案必须是 'A' 或 'B'。\n"
            "括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
        )
    elif target_type == "多选题":
        type_instruction = (
            "题型要求：多选题。\n"
            "至少4个选项。\n"
            "答案必须是列表形式，如 ['A','C','D']。\n"
            "括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
        )
    else:
        type_instruction = (
            "题型要求：单选题。\n"
            "4个选项且只有一个正确。\n"
            "答案必须是单个字母，如 'A'。\n"
            "括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
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
            question_type = state.get("current_question_type") or rec_type
        
        # Build type instruction for repair
        if question_type == "判断题":
            type_instruction_repair = "题型要求：判断题。选项必须固定为：['正确','错误']。答案必须是 'A' 或 'B'。括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
        elif question_type == "多选题":
            type_instruction_repair = "题型要求：多选题。至少4个选项。答案必须是列表形式，如 ['A','C','D']。括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
        else:
            type_instruction_repair = "题型要求：单选题。4个选项且只有一个正确。答案必须是单个字母，如 'A'。括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
        
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
{kb_context}

# 输出
返回 JSON: {{"question": "...", "options": ["A","B","C","D"], "answer": "A/B/C/D" 或 ["A","C"], "explanation": "..."}}
"""
        content, _, llm_record = call_llm(
            node_name="specialist.repair",
            prompt=prompt,
            model_name=SPECIALIST_MODEL or MODEL_NAME,
            api_key=API_KEY,
            base_url=BASE_URL,
            trace_id=state.get("trace_id"),
            question_id=state.get("question_id"),
        )
        llm_records.append(llm_record)
        try:
            draft = parse_json_from_response(content)
            return {
                "draft": draft,
                "examples": examples,
                "current_generation_mode": effective_generation_mode,
                "llm_trace": llm_records,
                "logs": [f"🛠️ {agent_name}: 已进入修复模式"]
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

难度控制方法：
- **简单题 (0.3-0.5)**：直接考察知识点定义、基础概念，干扰项明显错误
- **中等题 (0.5-0.7)**：需要理解知识点含义并应用到场景，干扰项似是而非，需要仔细分析
- **困难题 (0.7-0.9)**：需要综合多个知识点、复杂计算或多步推理，干扰项高度相似

**重要**：生成的题目难度值必须落在指定范围内，否则会被拒绝。请根据难度要求调整：
- 题干复杂度（简单题用直接表述，困难题用复杂场景）
- 干扰项相似度（简单题干扰项明显错误，困难题干扰项高度相似）
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

# 适纲性 / 实用性 / 导向性（必须满足）
1. **适纲性**：命题内容必须来自当前知识切片或本教材切片，不得超纲出题；超纲题属于错题。
2. **实用性**：试题要贴近经纪人作业或规则/法律理解，禁止仅考察数量、年代等死记硬背点。
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
3. **太长的句子不要**：
   - ❌ 错误示例："2023年5月5日，经纪人刘卓在门店接受了业主刘伟对其名下一套住宅的出售委托。在交流过程中得知刘伟着急出售该住宅。"
   - ✅ 正确做法："业主刘伟委托出售一套房源，经纪人刘卓得知其着急出售。"（简化表述，突出核心条件）
4. **简化数字，方便计算（必须遵守）**：
   - ❌ 错误示例：总户数328户，车位100个，车位配比1:3.28（复杂小数）
   - ✅ 正确做法：总户数400户，车位100个，车位配比1:4（整数，易于口算）
   - **原则**：数字尽量使用整数或简单小数（如0.5、1.5），避免使用1.328、2.876等复杂小数。
5. **非必要不起名（必须遵守）**：
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
   - 使用中文括号，括号内部加一个空格：`（ ）`
   - 括号前后不允许空格
3. **设问表达**：
   - 设问要用陈述方式，不使用疑问句。
   - 少用否定句，禁止使用双重否定句。
   - 判断题写法用【XX做法正确/错误】，不要写成【XX的做法是正确的】。
4. **选择题设问用词**：
   - 单选题：以下表述正确的是（ ）。
   - 多选题：以下表述正确的有（ ） / 以下表述正确的包括（ ）。

# 选项规范（必须遵守）
1. **选项数量与正确性**：
   - 选择题每题4个选项；单选题仅1个正确，多选题≥2个正确。
   - 多选题中正确选项数量要合理，不要多道题都只有1个答案。
2. **标点与语义**：
   - 每个选项末尾不添加标点符号。
   - 选项必须与题干组成完整语句（将选项代入题干括号处语义完整）。
3. **一致性与干扰项**：
   - 选项中的姓名与题干中的姓名保持一致，不出现题目未涉及的姓名。
   - 干扰项必须具有干扰性，选项本身应是存在或相关的内容，不能无意义。
   - **禁止明显常识性错误/极端值**，干扰项要“看起来可能对但实际上不对”。
   - **禁止明显常识性错误/极端值**（如与材料明显不符、过低/过高层数等），干扰项要“看起来可能对但实际上不对”。
4. **数值型选项**：
   - 选项为数字时按从小到大顺序排列。
   - 计算题尽量简单（能口算优先），确需保留小数时注明保留位数（一般1-2位）。
   - 未被选中的数值选项也必须有计算依据，不可胡编乱造。

# 解析规范（必须遵守）
1. **三段式（必须显式分段）**：
   - **教材原文**：先陈述教材规则或原文要点（可改写，不得直接贴表格/图片）。
   - **试题分析**：基于题干条件进行逐步分析与推导。
   - **结论**：必须以“本题答案为X”收束。
2. **结论写法（必须严格执行）**：
   - 判断题写【本题答案为正确/错误】，不得写成【本题答案为A/B】。
   - 选择题写【本题答案为A/B/C/D/AB/AC...】。
3. **严禁**：直接粘贴教材原文表格或图片（可改成文字描述）。
4. **一致性**：答案与解析必须一致，计算题必须与计算过程一致。
5. **典型错题规避**：
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
{kb_context}

# 范例参考
{examples_text}

# 题干一致性自检（必须执行）
1. 基于【当前切片 + 上一级切片全集 + 相似切片】检查题干与解析是否存在冲突或不一致。
2. 若发现不一致，必须输出“问题清单”，说明冲突维度、冲突点、修复建议。

# 任务
返回 JSON: {{"question": "...", "options": ["A", "B", "C", "D"], "answer": "A/B/C/D", "explanation": "...", "self_check_issues": [{{"dimension": "...", "issue": "...", "suggestion": "..."}}]}}
约束: 题干中**禁止**出现"根据材料"、"依据参考资料"等字眼。题目必须是独立的。

# 题目质量硬性约束（违反会被 Critic 驳回）⚠️
## 1. 禁止使用模糊的日常用语：题干中**禁止**使用"实实在在的特点"、"重要的信息"、"关键因素"等模糊表述，这类词在汉语中可能指向多个维度，会导致歧义。应使用明确、可操作的表述。
## 2. 选项维度一致性：所有选项必须在同一维度内做区分（如考实物信息则选项都是户型/面积/朝向/装修等）；**禁止**跨维度（如A法律、B实物、C位置、D价格），否则无法真正考察专业知识。干扰项应与正确答案同维度但略有不同。

# 自检清单（必须逐条核对）
1. **唯一答案**：题干条件足以排除其他选项，不能出现两条合理路径。
2. **解析规范**：三段式完整，结论以“本题答案为X”收束。
3. **一致性**：题干/选项/答案/解析前后一致，计算题与计算过程一致。
4. **适纲性**：不超纲，不引入材料外条件或结论。
5. **人名与措辞**：人名规范、无生造词、无模糊词。
6. **维度一致**：选项同维度，干扰项有理有据。
7. **干扰项质量**：避免“明显错误/常识级错误/极端值”，干扰项应合理但错误。
"""
    content, _, llm_record = call_llm(
        node_name="specialist.draft",
        prompt=prompt,
        model_name=SPECIALIST_MODEL or MODEL_NAME,
        api_key=API_KEY,
        base_url=BASE_URL,
        trace_id=state.get("trace_id"),
        question_id=state.get("question_id"),
    )
    llm_records.append(llm_record)
    
    try:
        # Log raw content for debugging
        print(f"DEBUG RAW CONTENT: {content}")
        
        draft = parse_json_from_response(content)
        self_check_issues = draft.get("self_check_issues") if isinstance(draft, dict) else None
        if not isinstance(self_check_issues, list):
            self_check_issues = []
        self_check_issues = draft.get("self_check_issues") if isinstance(draft, dict) else None
        if not isinstance(self_check_issues, list):
            self_check_issues = []
        return {
            "draft": draft,
            "examples": examples,  # Pass examples to UI
            "self_check_issues": self_check_issues,
            "current_generation_mode": effective_generation_mode,
            "llm_trace": llm_records,
            "logs": [f"👨‍💻 {agent_name}: 初稿已生成（题型={target_type}，筛选条件={effective_generation_mode}）"]
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
    
    # Get configured question type
    cfg_type = config['configurable'].get('question_type', '自动')
    
    # Infer draft's actual question type
    def infer_draft_type(d):
        options = d.get('options', []) if isinstance(d, dict) else []
        answer = d.get('answer', '') if isinstance(d, dict) else ''
        # 判断题：两项且为正确/错误
        if isinstance(options, list) and len(options) == 2:
            opt_set = {str(options[0]).strip(), str(options[1]).strip()}
            if opt_set == {"正确", "错误"}:
                return "判断题"
        # 多选题：答案为列表或包含多个选项字母
        if isinstance(answer, list):
            return "多选题"
        if isinstance(answer, str):
            ans = answer.strip().upper()
            if len(ans) > 1 and all(c in "ABCDE" for c in ans):
                return "多选题"
        # 默认单选
        return "单选题"
    
    # Determine target type based on strategy
    draft_type = None
    if isinstance(draft, dict):
        draft_type = infer_draft_type(draft)
    
    # ✅ Strategy:
    # 1. If cfg_type is "随机" (random mode): MUST keep draft's type, DO NOT modify
    # 2. If cfg_type is specific type: force modify to cfg_type
    # 3. If cfg_type is "自动" (auto): use router's recommendation or draft's type
    if cfg_type == "随机":
        # Random mode: keep draft's type, don't modify
        target_type = draft_type if draft_type else rec_type
        print(f"📌 随机模式：保持专家节点生成的题型 [{target_type}]")
    elif cfg_type in ["单选题", "多选题", "判断题"]:
        # Specific type: force modify to cfg_type
        target_type = cfg_type
        if draft_type and draft_type != cfg_type:
            print(f"📌 指定题型模式：强制修改题型 [{draft_type}] → [{cfg_type}]")
    else:
        # Auto mode: use router's recommendation or draft's type
        target_type = draft_type if draft_type else rec_type

    
    pre_writer_logs: List[str] = []
    draft_for_prompt = draft
    pre_hard_issues: List[str] = []
    if isinstance(draft, dict):
        draft_for_prompt = prepare_draft_for_writer(draft, target_type)
        pre_hard_issues = validate_writer_format(
            draft_for_prompt.get("question", ""),
            draft_for_prompt.get("options", []),
            draft_for_prompt.get("answer"),
            target_type,
        )
        if pre_hard_issues:
            pre_writer_logs.append(
                f"⚠️ 作家: 预清洗后仍有硬约束风险（将优先修复）: {', '.join(pre_hard_issues)}"
            )
        else:
            pre_writer_logs.append("⚠️ 作家: 已在润色前完成一次格式硬预清洗")

    # ------------------------------------------------------------------
    # 1. 动态构建 Prompt (Type-Aware)
    # ------------------------------------------------------------------
    type_specific_instruction = ""
    if target_type == "判断题":
        type_specific_instruction = """
- **题型要求**: 判断题。
- **选项设置**: 必须固定为两个选项：["正确", "错误"]。
- **答案格式**: 必须是 "A" (代表正确) 或 "B" (代表错误)。
- **括号格式**: 题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。
"""
    elif target_type == "多选题":
        type_specific_instruction = """
- **题型要求**: 多项选择题。
- **选项设置**: 至少 4 个选项，干扰项要具有迷惑性。
- **答案格式**: 必须包含所有正确选项的列表，例如 ["A", "C", "D"]。
- **逻辑**: 确保有 2 个或以上的选项是正确的。
- **括号格式**: 题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。
"""
    else: # 单选题
        type_specific_instruction = """
- **题型要求**: 单项选择题。
- **选项设置**: 4 个选项，只有一个正确。
- **答案格式**: 必须是单个字母，例如 "A"。
- **括号格式**: 题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。
"""

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

请根据难度范围设置 difficulty 字段：
- 如果范围是 0.3-0.5：使用 "易" 或数值 0.3-0.5
- 如果范围是 0.5-0.7：使用 "中" 或数值 0.5-0.7  
- 如果范围是 0.7-0.9：使用 "难" 或数值 0.7-0.9

**重要**：必须确保生成的难度值在指定范围内！
"""

    model_to_use = WRITER_MODEL or MODEL_NAME
    # 允许对“人名不规范”触发一次整体改写（不直接替换为“某某”）
    extra_self_check_issues = list(self_check_issues)
    if pre_hard_issues:
        extra_self_check_issues.extend([f"格式硬约束残留: {x}" for x in pre_hard_issues])
    last_exception = None
    final_dict = None
    writer_logs = list(pre_writer_logs)
    for attempt in range(2):
        self_check_text = ""
        if extra_self_check_issues:
            self_check_text = f"""
# 出题节点自检问题清单（必须逐条修复）
{json.dumps(extra_self_check_issues, ensure_ascii=False)}
"""
        prompt = f"""
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

# 简化场景，符合实际（必须遵守）⚠️
1. **无意义的场景铺垫不要**：
   - ❌ 错误示例："师傅告诉徐薇：经纪人在培训时了解到..."、"经纪人刘铭在新人训时学习了..."
   - ✅ 正确做法：直接陈述事实，去掉"某某告诉某某"、"在培训时了解到"等冗余铺垫。
2. **和题目无关联的句子不要**：
   - ❌ 错误示例:"客户张美通过经纪人邱好购买了一套毛坯二手房。因张美工作比较繁忙无暇装修..."（"通过经纪人邱好购买"与题目考点无关）
   - ✅ 正确做法：只保留与解题相关的关键信息，去掉对答案没有影响的背景描述。
3. **太长的句子不要**：
   - ❌ 错误示例："2023年5月5日，经纪人刘卓在门店接受了业主刘伟对其名下一套住宅的出售委托。在交流过程中得知刘伟着急出售该住宅。"
   - ✅ 正确做法："业主刘伟委托出售一套房源，经纪人刘卓得知其着急出售。"（简化表述，突出核心条件）
4. **简化数字，方便计算**：
   - ❌ 错误示例：车位配比1:3.28（复杂小数）
   - ✅ 正确做法：车位配比1:4（整数，易于口算）
5. **非必要不起名**：
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
   - 使用中文括号，括号内部加一个空格：`（ ）`
   - 括号前后不允许空格
3. **设问表达**：
   - 设问要用陈述方式，不使用疑问句。
   - 少用否定句，禁止使用双重否定句。
   - 判断题写法用【XX做法正确/错误】，不要写成【XX的做法是正确的】。
4. **选择题设问用词**：
   - 单选题：以下表述正确的是（ ）。
   - 多选题：以下表述正确的有（ ） / 以下表述正确的包括（ ）。

# 选项规范（必须遵守）
1. **选项数量与正确性**：
   - 选择题每题4个选项；单选题仅1个正确，多选题≥2个正确。
   - 多选题中正确选项数量要合理，不要多道题都只有1个答案。
2. **标点与语义**：
   - 每个选项末尾不添加标点符号。
   - 选项必须与题干组成完整语句（将选项代入题干括号处语义完整）。
3. **一致性与干扰项**：
   - 选项中的姓名与题干中的姓名保持一致，不出现题目未涉及的姓名。
   - 干扰项必须具有干扰性，选项本身应是存在或相关的内容，不能无意义。
4. **数值型选项**：
   - 选项为数字时按从小到大顺序排列。
   - 计算题尽量简单（能口算优先），确需保留小数时注明保留位数（一般1-2位）。
   - 未被选中的数值选项也必须有计算依据，不可胡编乱造。

# 解析规范（必须遵守）⚠️
1. **三段式（必须显式分段，每段必须独立成段）**：
   - **第一段 - 教材原文**：必须以"1. 教材原文："或类似标识开头，先陈述教材规则或原文要点（可改写，不得直接贴表格/图片）。
   - **第二段 - 试题分析**：必须以"2. 试题分析："或类似标识开头，基于题干条件进行逐步分析与推导。
   - **第三段 - 结论**：必须以"3. 结论："或类似标识开头，必须以"本题答案为X"收束。
   - **重要**：三个段落必须分别独立，不能合并或省略任何一段！必须用明确的数字序号标识每一段！
2. **结论写法（必须严格执行）**：
   - 判断题写【本题答案为正确/错误】，不得写成【本题答案为A/B】。
   - 选择题写【本题答案为A/B/C/D/AB/AC...】。
3. **严禁**：直接粘贴教材原文表格或图片（可改成文字描述）。
4. **一致性**：答案与解析必须一致，计算题必须与计算过程一致。
5. **典型错题规避**：
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
2. **解析规范（重点检查）**：
   - ✅ 必须有"1. 教材原文："开头的第一段
   - ✅ 必须有"2. 试题分析："开头的第二段
   - ✅ 必须有"3. 结论："开头的第三段，且以"本题答案为X"收束
   - ❌ 不能省略任何一段，不能合并段落，必须用数字序号标识！
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
    "options": ["选项A内容", "选项B内容", "选项C内容", "选项D内容"],
    "answer": "A" 或 ["A", "C"],
    "explanation": "解析内容（讲原理、结合情境、口语化，逐项指出错误选项的误区）",
    "difficulty": "易/中/难"
}}
"""
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
                final_dict = prepare_draft_for_writer(final_dict, target_type)
                writer_logs.append("⚠️ 作家: 已在润色后执行格式硬修复")
                # Name usage check (no auto replace)
                q_text = final_dict.get("question", "")
                opts = final_dict.get("options", []) or []
                exp_text = final_dict.get("explanation", "")
                name_issues = validate_name_usage(q_text, opts, exp_text)
                term_issues = detect_term_lock_violations(term_locks, final_dict)
                if name_issues and attempt == 0:
                    extra_self_check_issues.append(
                        "人名不规范：出现“小+姓氏”或“姓+女士/先生”等称谓，必须整体改写，禁止直接替换为“某某”。"
                    )
                    writer_logs.append(
                        f"⚠️ 作家: 人名不规范（{', '.join(name_issues)}），已要求整体改写"
                    )
                    continue
                if term_issues and attempt == 0:
                    extra_self_check_issues.append(
                        f"术语锁词违规：{'; '.join(term_issues)}。命中术语必须保持原词。"
                    )
                    writer_logs.append(
                        f"⚠️ 作家: 检测到术语疑似改词，已要求整体改写（{'; '.join(term_issues)}）"
                    )
                    continue
        except Exception as e:
            last_exception = e
            continue
        break
    if final_dict is None and last_exception is not None:
        return {
            "final_json": None,
            "llm_trace": llm_records,
            "logs": [f"❌ 作家格式化失败: {str(last_exception)}"]
        }

    try:
        # Validate key format points and surface in logs
        issues = validate_writer_format(
            final_dict.get("question", "") if isinstance(final_dict, dict) else "",
            final_dict.get("options", []) if isinstance(final_dict, dict) else [],
            final_dict.get("answer") if isinstance(final_dict, dict) else "",
            target_type
        )
        if isinstance(final_dict, dict):
            issues += validate_name_usage(
                final_dict.get("question", ""),
                final_dict.get("options", []),
                final_dict.get("explanation", "")
            )
            term_issues = detect_term_lock_violations(term_locks, final_dict)
            issues += term_issues
        if issues:
            writer_logs.append(f"⚠️ 作家: 格式校验发现问题（继续送审）: {', '.join(issues)}")
        
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
                # Remove A./A、/A:/A）/A) and variants (case-insensitive, full-width)
                val = re.sub(r'^[A-H１-８Ａ-Ｈa-h0-8][\.\、:：\s\)）]+', '', val, flags=re.IGNORECASE)
                val = val.strip()
                if target_type in ["判断题", "单选题", "多选题"]:
                    val = normalize_blank_brackets(val)
                excel_row[key] = val
            else:
                excel_row[key] = "" 
        
        # C. 答案选项 (格式: "ABC" or "A")
        raw_ans = final_dict.get('answer')
        final_ans = ""
        if isinstance(raw_ans, list):
            # 多选题列表 ["A", "C"] -> "AC"
            final_ans = "".join(sorted([str(x).upper() for x in raw_ans]))
        else:
            # 单选/判断 "A" -> "A"
            final_ans = str(raw_ans).upper().strip()
            
        excel_row['正确答案'] = final_ans
        
        # D. 知识点拆解
        path_parts = [p.strip() for p in kb_chunk.get('完整路径', '').split(' > ') if p.strip()]
        excel_row['一级知识点'] = path_parts[0] if len(path_parts) > 0 else ""
        excel_row['二级知识点'] = path_parts[1] if len(path_parts) > 1 else ""
        excel_row['三级知识点'] = path_parts[2] if len(path_parts) > 2 else ""
        excel_row['四级知识点'] = path_parts[3] if len(path_parts) > 3 else ""
        
        # E. 其他字段
        excel_row['解析'] = final_dict.get('explanation', '')
        
        # 难度转换: 易/中/难 -> 0.3/0.5/0.8，并验证是否在指定范围内
        raw_diff = final_dict.get('difficulty', '中')
        diff_map = {"易": 0.3, "中": 0.5, "难": 0.8}
        if isinstance(raw_diff, str):
            difficulty_value = diff_map.get(raw_diff, 0.5)
        else:
            try:
                difficulty_value = float(raw_diff)
            except:
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

        return {
            "final_json": excel_row, # Now strictly matches Excel template & ExamQuestion model
            "current_question_type": target_type,  # Pass actual question type to downstream nodes
            "writer_format_issues": issues,
            "llm_trace": llm_records,
            "logs": writer_logs + [f"✍️ 作家: 已格式化为【{target_type}】 (答案: {final_ans})"]
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "final_json": None,
            "llm_trace": llm_records,
            "logs": [f"❌ 作家格式化失败: {str(e)}"]
        }

def critic_node(state: AgentState, config):
    llm_records: List[Dict[str, Any]] = []
    # Debug/testing hook: force one "minor" failure to demonstrate the fixer loop.
    if state.get("debug_force_fail_once") and state.get("retry_count", 0) == 0:
        return {
            "critic_feedback": "FORCED_FAIL",
            "critic_details": "Forced minor failure for loop demo (will go to Fixer).",
            "critic_result": {"passed": False, "issue_type": "minor", "reason": "forced"},
            "retry_count": 1,
            "llm_trace": llm_records,
            "logs": ["🧪 批评家: 已强制驳回一次，用于演示 Fixer 闭环"]
        }
    final_json = state.get('final_json')
    if not final_json:
        return {
            "critic_feedback": "FAIL", 
            "critic_details": "No question generated to verify.",
            "llm_trace": llm_records,
            "logs": ["🕵️ 批评家: 无法审核，未生成题目。"]
        }
    print(f"DEBUG CRITIC INPUT FINAL_JSON: {final_json}")

    kb_chunk = state['kb_chunk']
    term_locks = state.get("term_locks") or []
    retriever = config['configurable'].get('retriever') or get_default_retriever()
    examples = state.get('examples', [])
    kb_context, parent_slices, related_slices = build_extended_kb_context(kb_chunk, retriever, examples)
    
    # Get difficulty range from config
    difficulty_range = config['configurable'].get('difficulty_range')
    
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
    question_type = state.get('current_question_type') or config['configurable'].get('question_type', '单选题')
    cfg_question_type = config['configurable'].get('question_type', '单选题')
    
    # ✅ Question type consistency validation (only for specific type mode)
    # If config is "随机", skip type validation
    # If config is specific type (单选/多选/判断), validate consistency with state
    print(f"🔍 Critic 开始执行 - cfg题型:[{cfg_question_type}], state题型:[{question_type}]")
    if cfg_question_type != "随机" and cfg_question_type in ["单选题", "多选题", "判断题"]:
        if question_type != cfg_question_type:
            print(f"❌ 题型不一致: 要求[{cfg_question_type}]，实际[{question_type}]")
            return {
                "critic_feedback": "FAIL",
                "critic_rules_context": full_rules_text,
                "critic_related_rules": related_rules,
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "reason": f"题型不一致：要求生成{cfg_question_type}，但实际生成了{question_type}",
                    "fix_strategy": "regenerate"
                },
                "critic_details": f"题型校验失败：要求{cfg_question_type}，实际{question_type}",
                "critic_model_used": "rule-based",
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
        if not has_business_context(stem_text):
            reason = "筛选条件不符合：当前为【实战应用/推演】，题干未体现业务场景语义"
            return {
                "critic_feedback": "FAIL",
                "critic_rules_context": full_rules_text,
                "critic_related_rules": related_rules,
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "reason": reason,
                    "fix_strategy": "regenerate"
                },
                "critic_details": reason,
                "critic_model_used": "rule-based",
                "llm_trace": llm_records,
                "logs": [f"🔍 批评家: ❌ {reason} → 重新生成"]
            }

    # ✅ Bracket format validation for judgment/choice questions
    if question_type in ["单选题", "多选题", "判断题"]:
        fields_to_check = []
        if isinstance(final_json, dict):
            fields_to_check.append(("题干", final_json.get("题干", "")))
            for i in range(1, 9):
                key = f"选项{i}"
                if key in final_json and final_json.get(key):
                    fields_to_check.append((key, final_json.get(key, "")))
        invalid_fields = [name for name, text in fields_to_check if has_invalid_blank_bracket(str(text))]
        if invalid_fields:
            reason = "括号格式错误：必须使用中文括号“（ ）”，括号前后无空格，括号内有空格"
            return {
                "critic_feedback": "FAIL",
                "critic_rules_context": full_rules_text,
                "critic_related_rules": related_rules,
                "critic_result": {
                    "passed": False,
                    "issue_type": "minor",
                    "reason": reason,
                    "fix_strategy": "fix_question"
                },
                "critic_details": f"{reason}（字段：{', '.join(invalid_fields)}）",
                "critic_model_used": "rule-based",
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
                "fix_strategy": "fix_question"
            },
            "critic_details": reason,
            "critic_model_used": "rule-based",
            "llm_trace": llm_records,
            "logs": [f"🔍 批评家: ❌ {reason} → 进入修复"]
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

## 0. 适纲性 / 实用性 / 导向性
- **适纲性**: 命题内容必须来自当前知识切片或本教材切片，不得超纲出题。
- **实用性**: 试题必须贴近经纪人作业或规则/法律理解，禁止仅考察数量、年代等死记硬背点。
- **导向性**: 试题应有引导和启发作用，帮助经纪人理解公司文化、熟悉新业务、热爱行业。
- **Fail条件**:
  - 题目超出当前知识切片或教材范围（超纲）。
  - 题目仅考察数量/年代等纯记忆点，缺少实务价值。

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

Fail 条件（任一即 Fail）：
- 无法计算（缺关键数值 / 条件）
- 存在两条及以上合理推导路径
- 不同规则分支在题干中未被明确排除
- 需要考生“猜规则”“默认前提”才能算出答案

⚠️ 一旦 Reverse Solving 判定失败：
- reverse_solve_success = false
- can_deduce_unique_answer = false
- fix_strategy 至少为 fix_question


## 4. 质量把关 (Quality Control) - 新增核心拦截项 ⚠️
- **Fail条件 1 (题干直接给出答案)**:
  - 题干中直接包含了正确答案的关键词，导致考生无需理解专业知识，仅通过文字匹配即可选出答案。
  - *典型案例*：题干说"经纪人向她介绍了商业贷款"，然后问"商业贷款最核心的特征或定义是什么"，此时正确答案选项A如果直接复述教材定义，考生无需理解即可通过题干中的"商业贷款"关键词匹配到答案。
  - *整改建议*：要求改为"给定场景->判断结论"的模式，题干不应直接给出答案关键词。
  - **重要说明**：允许正确答案选项与教材原文定义一致，这是正常的考察方式。检测的重点是题干是否直接给出了答案关键词，而不是答案选项是否与教材原文一致。
- **Fail条件 2 (基础质量)**:
  - 题目表述使用了模糊词汇（如"实实在在"）。
  - 选项跨维度（如A法律 B实物 C位置 D价格）。
  - 干扰项过于幼稚，无需专业知识即可排除（仅对中/高难度强制）。
  - **判断题特例**：判断题只允许两个选项（正确/错误），C/D为空不构成质量问题。
- **Fail条件 3 (AI幻觉/非人话)**:
  - 出现了不符合中国房地产业务习惯的生造词。
  - *典型案例*：使用“外接”代替“买方/受让方”；使用“上交”代替“缴纳”。

## 5. 题干/设问规范审计
- **规则**:
  - 题干括号不得在句首，可在句中或句末；选择题句末必须有句号且句号在最后；判断题句子完结后加括号且在最后。
  - 括号必须为中文 `（ ）`，括号内有空格，括号前后无空格。
  - 设问使用陈述方式，避免否定句与双重否定；判断题写法需为【XX做法正确/错误】。
  - 单选题设问用“以下表述正确的是（ ）。”；多选题用“以下表述正确的有（ ）/包括（ ）。”。
- **Fail条件**:
  - 括号格式不符合 `（ ）` 或位置/句号不符合要求。
  - 设问为疑问句/双重否定，或判断题格式不符合【XX做法正确/错误】。

## 6. 选项规范审计
- **规则**:
  - 选项末尾不加标点；选项与题干合成语义完整。
  - 选项姓名与题干姓名一致，不能出现题目未涉及的姓名。
  - 数值型选项需按从小到大排序，未被选中的数值也必须有计算依据。
- **Fail条件**:
  - 选项末尾有标点、姓名不一致、选项无法与题干组成完整语义。
  - 数值选项无依据或乱序。

## 7. 解析规范审计
- **规则**:
  - 解析需采用三段式：教材原文 + 试题分析 + 结论。
  - 判断题结论写【本题答案为正确/错误】，不得写成【本题答案为A/B】。
  - 选择题结论写【本题答案为A/B/C/D/AB/AC...】。
  - 禁止直接粘贴教材原文表格或图片（可改成文字描述）。
  - 解析必须与答案一致，计算题必须与计算过程一致。
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
  - 干扰项是纯粹的随机数字，没有考察到易错点（仅对高难度强制）。
  - *优质干扰项标准*：应该是“遗漏了某一步计算”或“误用了另一个税率”得出的错误结果。
  - **难度差异化要求**：
    - 低难度（<=0.5）：干扰项质量只记录建议，不作为 Fail 条件。
    - 中等（0.5-0.7）：可提示，但不要仅因干扰项质量判 Fail。
    - 高难度（>=0.7）：必须满足高质量干扰项要求，不达标即 Fail。

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
    "fix_strategy": "fix_explanation / fix_question / fix_both / regenerate",
    "fix_reason": "用一句话给出修复建议（必要时给出要补充的具体条件/选项）",
    "reason": "详细说明审核结论"
}}
```
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
    critic_model_used = used_model or critic_model
    print(f"🔍 Critic Step 2: 质量验证完成，开始解析结果")
    
    # Initialize variables with defaults
    critic_answer = "UNKNOWN"
    explanation_valid = False
    reverse_solve_success = False
    can_deduce_unique_answer = False
    deduction_process = ""
    grounding_check_passed = True  # Default to True for backward compatibility
    missing_conditions = []
    example_conflict = False
    quality_check_passed = True  # Default to True for backward compatibility
    quality_issues = []
    context_strength = "中"  # Default to medium
    option_dimension_consistency = True  # Default to True
    reason = "Parsing Failed"
    fix_strategy = "fix_both"
    fix_reason = ""
    
    try:
        review_result = parse_json_from_response(response_text)
        
        # 反向解题结果（核心校验）
        reverse_solve_success = review_result.get("reverse_solve_success", False)
        can_deduce_unique_answer = review_result.get("can_deduce_unique_answer", True)  # Default to True for backward compatibility
        deduction_process = review_result.get("deduction_process", "")
        
        # 答案一致性
        critic_answer = review_result.get("critic_answer", "UNKNOWN").strip().upper()
        
        # 信息不对称校验
        grounding_check_passed = review_result.get("grounding_check_passed", True)
        missing_conditions = review_result.get("missing_conditions", [])
        example_conflict = review_result.get("example_conflict", False)
        has_example_refs = bool(state.get("examples")) or bool(kb_chunk.get("结构化内容", {}).get("examples"))
        if not has_example_refs:
            example_conflict = False
        
        # 题目质量检查
        quality_check_passed = review_result.get("quality_check_passed", True)  # Default to True for backward compatibility
        quality_issues = review_result.get("quality_issues", [])
        context_strength = review_result.get("context_strength", "中")
        option_dimension_consistency = review_result.get("option_dimension_consistency", True)
        
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
        
        # 低难度题：干扰项优秀程度不作为强制 Fail 条件
        if difficulty_value <= 0.5:
            filtered_issues = [i for i in quality_issues if "干扰项" not in str(i)]
            if len(filtered_issues) != len(quality_issues):
                quality_issues = filtered_issues
                if not quality_issues:
                    quality_check_passed = True
        
        # 解析审查
        explanation_valid = review_result.get("explanation_valid", False)
        
        reason = review_result.get("reason", "")
        fix_strategy = review_result.get("fix_strategy", "fix_both")
        fix_reason = review_result.get("fix_reason", "")
        
        # 如果 can_deduce_unique_answer 为 False，强制 reverse_solve_success = False
        if not can_deduce_unique_answer:
            reverse_solve_success = False
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
        reason = f"JSON解析失败: {str(e)}"
        fix_strategy = "regenerate"
        fix_reason = "审计输出解析失败"
    
    gen_answer = final_json['正确答案'].strip().upper()
    question_text = final_json.get("题干", "")
    
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
                    "reason": "高相似度重复题目"
                },
                "critic_model_used": critic_model,
                "final_json": None,
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
                "deduction_process": deduction_process
            },
            "critic_format_issues": critic_format_issues,
            "critic_model_used": critic_model_used,
            "llm_trace": llm_records,
            "logs": [f"{log_prefix} 审核通过（反向解题成功，能推导出唯一答案）"]
        }
        print(f"DEBUG CRITIC RESULT: {critic_payload['critic_result']}")
        return critic_payload
    else:
        required_fixes = []
        all_issues = []
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
        if difficulty_out_of_range:
            all_issues.append("difficulty:out_of_range")
        if explanation_fail:
            all_issues.append("explanation:invalid")
        if not code_check_passed:
            all_issues.append(f"calc:code_check:{code_check_reason}")

        critic_payload = {
            "critic_feedback": fail_reason if fail_reason else "反向解题失败",
            "critic_details": f"❌ 审计不通过（触发Fail条件）: {fail_reason if fail_reason else '无法根据题目条件推导出唯一答案'}",
            "critic_tool_usage": critic_tool_usage,
            "critic_rules_context": full_rules_text,
            "critic_related_rules": related_rules,
            "critic_result": {
                "passed": False,
                "issue_type": issue_type,  # minor: 可修复 / major: 需重新路由
                "reason": fail_reason if fail_reason else reason,
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
                "all_issues": all_issues
            },
            "critic_required_fixes": required_fixes,
            "critic_format_issues": critic_format_issues,
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
    # ✅ Prioritize reading question type from state (set by Writer), fallback to config
    question_type = state.get('current_question_type') or config['configurable'].get('question_type', '单选题')
    generation_mode = state.get("current_generation_mode") or config['configurable'].get('generation_mode', '随机')
    effective_generation_mode, normalized_generation_mode = resolve_effective_generation_mode(generation_mode, state)
    difficulty_range = config['configurable'].get('difficulty_range')
    
    # Build type instruction
    if question_type == "判断题":
        type_instruction = "题型要求：判断题。选项必须固定为：['正确','错误']。答案必须是 'A' 或 'B'。括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
    elif question_type == "多选题":
        type_instruction = "题型要求：多选题。至少4个选项。答案必须是列表形式，如 ['A','C','D']。括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
    else:
        type_instruction = "题型要求：单选题。4个选项且只有一个正确。答案必须是单个字母，如 'A'。括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
    
    # Build mode instruction
    mode_instruction = build_mode_instruction(effective_generation_mode, normalized_generation_mode)
    
    # Build difficulty instruction
    difficulty_instruction = ""
    if difficulty_range:
        min_diff, max_diff = difficulty_range
        difficulty_instruction = f"""
# 难度要求（必须严格遵守）⚠️
**题目难度值必须在 {min_diff:.1f} 到 {max_diff:.1f} 之间**。

难度控制方法：
- **简单题 (0.3-0.5)**：直接考察知识点定义、基础概念，干扰项明显错误
- **中等题 (0.5-0.7)**：需要理解知识点含义并应用到场景，干扰项似是而非，需要仔细分析
- **困难题 (0.7-0.9)**：需要综合多个知识点、复杂计算或多步推理，干扰项高度相似

**重要**：生成的题目难度值必须落在指定范围内，否则会被拒绝。请根据难度要求调整：
- 题干复杂度（简单题用直接表述，困难题用复杂场景）
- 干扰项相似度（简单题干扰项明显错误，困难题干扰项高度相似）
- 所需推理步骤（简单题直接答案，困难题需要多步推理）
"""
    
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
{term_lock_text}

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
            model_name=CRITIC_MODEL,
            api_key=CRITIC_API_KEY,
            base_url=CRITIC_BASE_URL,
            provider=CRITIC_PROVIDER,
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
            
            return {
                "final_json": fixed_json,
                "llm_trace": llm_records,
                "logs": ["🔧 修复者: 检测到生成失败，已重新生成题目"],
                "was_fixed": True  # Mark as fixed for UI highlighting
            }
        except Exception as e:
            return {"llm_trace": llm_records, "logs": [f"❌ 修复者重试失败: {str(e)}"]}

    # CASE 2: Normal Fix (Question exists but rejected)
    prompt = f"""
# 任务
上一道题被批评家驳回了。
原因: {feedback}
审计详情: {critic_details}
修复策略: {fix_strategy}（{fix_reason}）
必须修复项（来自批评家）：{json.dumps(critic_required_fixes, ensure_ascii=False)}
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
2. **去掉无关句子**：只保留与解题相关的关键信息。
3. **简化长句子**：突出核心条件，避免冗长描述。
4. **简化数字**：优先使用整数或简单小数（如1:4而非1:3.28），方便口算。
5. **非必要不起名**：经纪人名字对考点无关时不要提及。

# 必须遵守的约束
{type_instruction}

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
   - 使用中文括号，括号内部加一个空格：`（ ）`
   - 括号前后不允许空格
3. **设问表达**：
   - 设问要用陈述方式，不使用疑问句。
   - 少用否定句，禁止使用双重否定句。
   - 判断题写法用【XX做法正确/错误】，不要写成【XX的做法是正确的】。
4. **选择题设问用词**：
   - 单选题：以下表述正确的是（ ）。
   - 多选题：以下表述正确的有（ ） / 以下表述正确的包括（ ）。
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
   - 未被选中的数值选项也必须有计算依据，不可胡编乱造。

# 自检清单（必须逐条核对）
1. **唯一答案**：题干条件足以排除其他选项，不能出现两条合理路径。
2. **解析规范**：三段式完整，结论以“本题答案为X”收束。
3. **一致性**：题干/选项/答案/解析前后一致，计算题与计算过程一致。
4. **适纲性**：不超纲，不引入材料外条件或结论。
5. **人名与措辞**：人名规范、无生造词、无模糊词。
6. **维度一致**：选项同维度，干扰项有理有据。

# 修复要求:
1. **准确性**: 确保答案与解析完全一致，且有知识片段支持。
2. **干扰项**: 确保错误选项似是而非但绝对错误。利用**"相近的数字"**或**"错误的参照物"**设计干扰项。
3. **清晰度**: 消除导致批评家困惑的歧义。
4. **完整性**: 必须包含 "难度值" (0.0-1.0) 和 "考点"。
5. **题型一致性**: 修复后的题目必须符合指定的题型要求（{question_type}）。

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
        model_name=CRITIC_MODEL,
        api_key=CRITIC_API_KEY,
        base_url=CRITIC_BASE_URL,
        provider=CRITIC_PROVIDER,
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
        fixed_json.setdefault('考点', final_json.get('考点', kb_chunk.get('完整路径', '').split('>')[-1].strip() or "综合考点"))
        fixed_json.setdefault('题干', final_json.get('题干', ''))
        fixed_json.setdefault('选项1', final_json.get('选项1', ''))
        fixed_json.setdefault('选项2', final_json.get('选项2', ''))
        fixed_json.setdefault('选项3', final_json.get('选项3', ''))
        fixed_json.setdefault('选项4', final_json.get('选项4', ''))
        fixed_json.setdefault('正确答案', final_json.get('正确答案', 'A'))
        fixed_json.setdefault('解析', final_json.get('解析', ''))
        fixed_json = repair_final_json_format(fixed_json, question_type)
        
        # Mark this question as having been fixed (for UI highlighting)
        fixed_json['_was_fixed'] = True
        fix_summary = build_fix_summary(final_json or {}, fixed_json or {})
        unmet_required = []
        if critic_required_fixes:
            # Only enforce format checks deterministically here
            if any(item.startswith("format:") for item in critic_required_fixes):
                post_format_issues = validate_critic_format(fixed_json, question_type)
                if post_format_issues:
                    unmet_required.append("format")
        fix_summary["required_fixes"] = critic_required_fixes
        fix_summary["unmet_required_fixes"] = unmet_required
        fix_required_unmet = True if unmet_required else False

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
                    model_name=CRITIC_MODEL,
                    api_key=CRITIC_API_KEY,
                    base_url=CRITIC_BASE_URL,
                    provider=CRITIC_PROVIDER,
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
                forced_json.setdefault('考点', final_json.get('考点', kb_chunk.get('完整路径', '').split('>')[-1].strip() or "综合考点"))
                forced_json.setdefault('题干', final_json.get('题干', ''))
                forced_json.setdefault('选项1', final_json.get('选项1', ''))
                forced_json.setdefault('选项2', final_json.get('选项2', ''))
                forced_json.setdefault('选项3', final_json.get('选项3', ''))
                forced_json.setdefault('选项4', final_json.get('选项4', ''))
                forced_json.setdefault('正确答案', final_json.get('正确答案', 'A'))
                forced_json.setdefault('解析', final_json.get('解析', ''))
                forced_json = repair_final_json_format(forced_json, question_type)
                forced_json['_was_fixed'] = True

                forced_summary = build_fix_summary(final_json or {}, forced_json or {})
                forced_changed = forced_summary.get("changed_fields", [])
                if forced_changed:
                    fixed_json = forced_json
                    fix_summary = forced_summary
                    changed_fields = forced_changed
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
            
        return {
            "final_json": fixed_json,
            "fix_summary": fix_summary,
            "fix_no_change": fix_no_change,
            "fix_attempted_regen": force_regen_used,
            "fix_required_unmet": fix_required_unmet,
            "llm_trace": llm_records,
            "logs": [log_msg],
            "was_fixed": True
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
    retry_count = state.get('retry_count', 0)
    
    # 通过
    if critic_result.get('passed'):
        return "pass"

    # Fixer 未满足必修项 → 强制重路由
    if state.get("fix_required_unmet"):
        return "reroute"
    
    # 超限自愈
    if retry_count >= 3:
        return "self_heal"
    
    # 判断问题严重程度
    issue_type = critic_result.get('issue_type', 'minor')
    final_json = state.get('final_json', {})
    was_fixed = isinstance(final_json, dict) and final_json.get('_was_fixed') is True
    
    # 失败一律先走 Fixer，确保真正修复
    if not was_fixed:
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
    # ✅ Use smart-switched model for code generation
    # If GPT is rate-limited, calc_model will already be "deepseek-chat"
    code_gen_model = calc_model if calc_model else CODE_GEN_MODEL
    code_gen_api_key = calc_api_key if calc_api_key else (CODE_GEN_API_KEY or API_KEY)
    code_gen_base_url = calc_base_url if calc_base_url else (CODE_GEN_BASE_URL or BASE_URL)
    code_gen_provider = resolve_code_gen_provider(code_gen_model, calc_provider, None)
    
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
    
    uniqueness_note = ""
    avoid_superlative = "   - **避免\"最XX\"考法**：禁止用\"最重要/最关键/重点/主要\"等表述设计题干或选项，重点考察完整流程、条件、责任边界或操作要点。"
    if target_type == "单选题":
        uniqueness_note = "   - **唯一正确性**：确保只有一个选项严格符合教材原文或计算结论，其他选项须有明确错误点，避免 A/B 都似乎正确的歧义。"

    if target_type == "判断题":
        type_instruction = "题型要求：判断题。选项必须固定为：['正确','错误']。答案必须是 'A' 或 'B'。括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
    elif target_type == "多选题":
        type_instruction = "题型要求：多选题。至少4个选项。答案必须是列表形式，如 ['A','C','D']。括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
    else:
        type_instruction = "题型要求：单选题。4个选项且只有一个正确。答案必须是单个字母，如 'A'。括号格式：题干占位括号必须为中文括号“（ ）”，括号前后无空格，括号内有空格。"
    
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
- **简单题 (0.3-0.5)**：直接考察知识点定义、基础概念，干扰项明显错误
- **中等题 (0.5-0.7)**：需要理解知识点含义并应用到场景，干扰项似是而非，需要仔细分析
- **困难题 (0.7-0.9)**：需要综合多个知识点、复杂计算或多步推理，干扰项高度相似

**重要**：生成的题目难度值必须落在指定范围内，否则会被拒绝。请根据难度要求调整：
- 题干复杂度（简单题用直接表述，困难题用复杂场景）
- 干扰项相似度（简单题干扰项明显错误，困难题干扰项高度相似）
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
   - 使用中文括号，括号内部加一个空格：`（ ）`
   - 括号前后不允许空格
3. **设问表达**：
   - 设问要用陈述方式，不使用疑问句。
   - 少用否定句，禁止使用双重否定句。
   - 判断题写法用【XX做法正确/错误】，不要写成【XX的做法是正确的】。
4. **选择题设问用词**：
   - 单选题：以下表述正确的是（ ）。
   - 多选题：以下表述正确的有（ ） / 以下表述正确的包括（ ）。

# 选项规范（必须遵守）
1. **选项数量与正确性**：
   - 选择题每题4个选项；单选题仅1个正确，多选题≥2个正确。
   - 多选题中正确选项数量要合理，不要多道题都只有1个答案。
2. **标点与语义**：
   - 每个选项末尾不添加标点符号。
   - 选项必须与题干组成完整语句（将选项代入题干括号处语义完整）。
3. **一致性与干扰项**：
   - 选项中的姓名与题干中的姓名保持一致，不出现题目未涉及的姓名。
   - 干扰项必须具有干扰性，选项本身应是存在或相关的内容，不能无意义。
4. **数值型选项**：
   - 选项为数字时按从小到大顺序排列。
   - 计算题尽量简单（能口算优先），确需保留小数时注明保留位数（一般1-2位）。
   - 未被选中的数值选项也必须有计算依据，不可胡编乱造。

# 解析规范（必须遵守）
1. **三段式**：教材原文 + 试题分析 + 结论。
2. **结论写法**：
   - 判断题写【本题答案为正确/错误】，不得写成【本题答案为A/B】。
   - 选择题写【本题答案为A/B/C/D/AB/AC...】。
3. **严禁**：直接粘贴教材原文表格或图片（可改成文字描述）。
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
返回 JSON: {{"question": "...", "options": ["A", "B", "C", "D"], "answer": "A/B/C/D", "explanation": "...", "self_check_issues": [{{"dimension": "...", "issue": "...", "suggestion": "..."}}]}}
约束: 题干中**禁止**出现"根据材料"或"依据参考资料"。

# 题目质量硬性约束（违反会被 Critic 驳回）⚠️
## 1. 禁止使用模糊的日常用语：题干中**禁止**使用"实实在在的特点"、"重要的信息"、"关键因素"等模糊表述，这类词在汉语中可能指向多个维度，会导致歧义。应使用明确、可操作的表述。
## 2. 选项维度一致性：所有选项必须在同一维度内做区分（如考实物信息则选项都是户型/面积/朝向/装修等）；**禁止**跨维度（如A法律、B实物、C位置、D价格），否则无法真正考察专业知识。干扰项应与正确答案同维度但略有不同。
"""
    # ✅ Use smart-switched model for draft generation
    print(f"🧮 计算专家: 使用模型 {code_gen_model} 生成初稿")
    content, _, llm_record = call_llm(
        node_name="calculator.draft",
        prompt=prompt_gen,
        model_name=code_gen_model,
        api_key=code_gen_api_key,
        base_url=code_gen_base_url,
        provider=code_gen_provider,
        trace_id=state.get("trace_id"),
        question_id=state.get("question_id"),
    )
    llm_records.append(llm_record)
    
    try:
        draft = parse_json_from_response(content)
        
        log_msg = f"🧮 计算专家: 初稿已生成"
        if calc_result is not None:
            log_msg += f" (已执行动态代码, 结果={calc_result})"
        elif generated_code_str:
            log_msg += f" (已生成代码，但执行失败: {code_status})"
        
        # ✅ 确保计算结果传递到 critic_node
        return {
            "draft": draft,
            "tool_usage": {
                "method": "dynamic_code_generation",
                "generated_code": generated_code_str,
                "extracted_params": plan.get("extracted_params", {}),
                "result": calc_result,
                "code_status": code_status
            },
            "execution_result": calc_result,  # ✅ 传递给 critic_node
            "calculator_model_used": code_gen_model,  # Track which model was used
            "generated_code": generated_code_str,  # ✅ 动态生成的Python代码
            "code_status": code_status,  # success / error / no_calculation
            "examples": examples,  # Pass examples to UI
            "current_generation_mode": effective_generation_mode,
            "self_check_issues": [],
            "llm_trace": llm_records,
            "logs": [f"{log_msg}（筛选条件={effective_generation_mode}）"]
        }
    except Exception as e:
        return {
            "calculator_model_used": code_gen_model,
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
