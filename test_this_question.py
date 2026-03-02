#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直接调用 Critic 审核指定题目"""
import os
import json
import sys

print("="*80)
print("直接调用 Critic 审核题目")
print("="*80)

# 读取配置
config = {}
with open("填写您的Key.txt", 'r', encoding='utf-8') as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()

# 测试题目（从截图）
test_question = {
    "题干": "王女士在咨询一套房源时,对经纪人说:\"我想先了解房子本身那些实实在在的特点。\"作为经纪人,你应该以下列哪项为例向她说明房屋的实物信息？",
    "选项1": "房屋的产权是否清晰无纠纷",
    "选项2": "房屋的户型为三室两厅",
    "选项3": "房屋所在学区的排名情况",
    "选项4": "房屋近半年的市场成交价格",
    "正确答案": "B",
    "解析": "1、教材原文：房源实物信息是指房地产可见、可触摸的部分，包括房屋面积、户型、朝向、楼层高度、装修状况等具体物理特征。2、试题分析：选项B"房屋的户型为三室两厅"直接属于户型信息，是典型的实物信息，符合专业描述。选项A的产权状况属于法律权利信息。选项C的学区排名属于位置配套信息。选项D的市场成交价格属于交易价格信息。这些都不是实物信息。3、结论：在实际业务中，经纪人应准确区分实物信息与非实物信息，优先用实物特征回应客户对房屋本身的询问，避免概念混淆，从而体现专业性和合规意识。"
}

print(f"\n待审核题目:")
print(f"题干: {test_question['题干']}")
print(f"\n选项:")
print(f"  A. {test_question['选项1']}")
print(f"  B. {test_question['选项2']}")
print(f"  C. {test_question['选项3']}")
print(f"  D. {test_question['选项4']}")
print(f"\n生成者的答案: {test_question['正确答案']}")

# 导入必要的模块
from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
from exam_graph import generate_content, CRITIC_API_KEY, CRITIC_BASE_URL, CRITIC_MODEL, CRITIC_PROVIDER

# 获取知识点内容（房源实物信息）
retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)

# 查找相关知识点
kb_chunk = None
for chunk in retriever.kb_data[:500]:
    content = chunk.get('核心内容', '') + chunk.get('完整路径', '')
    if '实物信息' in content or '房源实物' in content:
        kb_chunk = chunk
        break

if not kb_chunk:
    # 使用默认知识点内容
    kb_chunk = {
        "完整路径": "第二篇签前服务 > 第一章房屋信息 > 第一节房源信息概述 > 二、房源实物信息",
        "核心内容": """房源实物信息是指房地产可见、可触摸的部分，包括房屋面积、户型、朝向、楼层高度、装修状况等具体物理特征。

实物信息的特点：
1. 可见性：能够通过视觉观察到的特征
2. 可触摸性：能够通过触觉感知的特征
3. 客观性：不涉及主观判断，是客观存在的物理属性

实物信息包括：
- 房屋面积（建筑面积、使用面积、套内面积）
- 户型（几室几厅、房间布局）
- 朝向（南北、东西等）
- 楼层高度
- 装修状况（精装、简装、毛坯）
- 建筑结构
- 房屋年代
- 配套设施（电梯、停车位等）

非实物信息包括：
- 产权信息（法律权利）
- 位置配套信息（学区、交通、商业等）
- 交易价格信息
- 市场行情信息"""
    }

print(f"\n知识点: {kb_chunk.get('完整路径', '')}")

# 构建全量规则上下文
full_rules_text = f"# 当前知识点规则\n{kb_chunk['核心内容']}\n"

# 确定使用的模型
critic_model = CRITIC_MODEL or config.get("OPENAI_MODEL", "deepseek-reasoner")
critic_api_key = CRITIC_API_KEY or config.get("OPENAI_API_KEY", "")
critic_base_url = CRITIC_BASE_URL or config.get("OPENAI_BASE_URL", "https://openapi-ait.ke.com")
critic_provider = CRITIC_PROVIDER or None

print(f"\n使用 Critic 模型: {critic_model}")
print(f"Provider: {critic_provider or 'default'}")

# 构建 Critic 的完整 prompt（与 exam_graph.py 中的格式一致）
prompt = f"""
# 角色
你是审计人 (Critic/Solver)，拥有**全量教材逻辑**。
你的任务是进行"反向解题"验证：**仅凭出题人给的这几个条件，你能根据全量规则推导出唯一的答案吗？**

# 信息不对称设置
- **出题人 (Writer)**: 只拿到了教材的一小块（信息少）
- **你 (审计人)**: 拿着全量教材逻辑（信息多）
- **校验点**: 仅凭出题人给的这几个条件，你能根据全量规则推导出唯一的答案吗？

# 全量教材规则（你拥有的完整信息）
{full_rules_text}

# 待审核题目（你只能看到这些条件）
题干: {test_question['题干']}
选项:
A. {test_question['选项1']}
B. {test_question['选项2']}
C. {test_question['选项3']}
D. {test_question['选项4']}

**注意**: 你**不能看**生成者的答案和解析，必须独立推导。

# 审核任务：反向解题验证

## 1. **反向解题（核心校验）** ⚠️ **最重要**

**任务**: 仅凭题目给出的条件，根据全量教材规则，尝试推导出唯一答案。

**步骤**:
1. **提取题目条件**: 从题干和选项中提取所有数值、条件、前提
2. **匹配全量规则**: 在全量教材规则中查找匹配的逻辑和判定条件
3. **检查条件完整性**: 
   - 题目给出的条件是否足够推导出唯一答案？
   - 是否遗漏了教材规则中的关键判定条件？
4. **尝试推导答案**:
   - 如果条件完整，根据全量规则推导，得到唯一答案
   - 如果条件不完整，无法推导出唯一答案 → **判定为失败**

**判定标准**:
- ✅ **通过**: 能够根据题目条件 + 全量规则推导出唯一答案
- ❌ **失败**: 无法推导出唯一答案

## 2. **答案一致性验证**

**任务**: 独立推导出答案后，与生成者的答案对比（但不看生成者的解析）

## 3. **信息不对称校验 (Grounding Check)**

- **检查题目是否遗漏了教材中的判定条件**
- **检查是否误带入了母题中的陈旧逻辑或错误数据**

## 4. **题目质量深度检查** ⚠️ **重要**

**必须严格检查以下方面，任何一项不合格都应判定为失败：**

### 4.1 题目表述质量
- **题干是否清晰明确？** 是否存在歧义、模糊表述？
- **题目是否真正考察了知识点？** 还是只是简单的记忆题？
- **场景是否合理？** 是否符合实际业务场景？
- **题目是否有实际意义？** 是否有助于提升业务能力？

### 4.2 选项设计质量
- **干扰项是否合理？** 是否真正具有干扰性，还是明显错误？
- **选项之间是否有明显区分度？** 是否存在两个选项都似乎正确的情况？
- **选项是否与题干匹配？** 是否存在选项与题干不相关的情况？
- **选项表述是否专业？** 是否符合行业术语和规范？

### 4.3 逻辑严谨性
- **正确答案是否唯一？** 是否存在多个选项都可以解释为正确的情况？
- **题目逻辑是否自洽？** 题干、选项、答案之间是否一致？
- **是否考察了核心知识点？** 还是只考察了边缘信息？

### 4.4 实际业务价值
- **题目是否有助于提升业务能力？** 是否真正帮助经纪人避免业务失误？
- **是否考察了关键判断点？** 是否考察了容易出错的地方？
- **是否符合"教材为实，母题为样"原则？** 是否严格遵循教材，而非照搬母题？

**如果发现以下问题，必须判定为失败：**
- ❌ 题目表述模糊，存在歧义
- ❌ 选项设计不合理，干扰项过于明显或过于相似
- ❌ 正确答案不唯一，存在多个合理答案
- ❌ 题目过于简单，只是记忆题，没有考察理解
- ❌ 题目与实际业务脱节，没有实际价值
- ❌ 题目逻辑不严谨，存在矛盾

## 5. **解析审查**

- 解析是否逻辑清晰？
- 解析是否有力解释了为何选该答案？
- 解析是否说明了其他选项为何错误？

# 输出格式 (JSON)
{{
    "reverse_solve_success": true/false,
    "critic_answer": "A/B/C/D",
    "can_deduce_unique_answer": true/false,
    "missing_conditions": ["遗漏的条件1", "遗漏的条件2"] 或 [],
    "deduction_process": "你的推导过程：1. 提取条件... 2. 匹配规则... 3. 推导答案...",
    "explanation_valid": true/false,
    "grounding_check_passed": true/false,
    "example_conflict": true/false,
    "quality_check_passed": true/false,
    "quality_issues": ["问题1", "问题2"] 或 [],
    "reason": "详细说明：如果能推导出唯一答案，说明推导过程；如果不能，说明缺少什么条件。如果题目质量有问题，详细说明问题所在"
}}
"""

print(f"\n正在调用 Critic 审核...")
print(f"Prompt 长度: {len(prompt)} 字符")

# 调用 Critic
response_text = generate_content(
    critic_model,
    prompt,
    critic_api_key,
    critic_base_url,
    critic_provider
)

print(f"\n" + "="*80)
print("Critic 原始响应:")
print("="*80)
print(response_text if response_text else "(空响应)")

if not response_text:
    print("\n❌ Critic 返回空响应！")
    sys.exit(1)

# 解析 JSON
from exam_graph import parse_json_from_response

try:
    result = parse_json_from_response(response_text)
    
    print(f"\n" + "="*80)
    print("解析后的结果:")
    print("="*80)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 分析结果
    print(f"\n" + "="*80)
    print("审核结果分析:")
    print("="*80)
    
    reverse_solve = result.get("reverse_solve_success", False)
    can_deduce = result.get("can_deduce_unique_answer", False)
    critic_answer = result.get("critic_answer", "UNKNOWN").strip().upper()
    quality_passed = result.get("quality_check_passed", True)
    quality_issues = result.get("quality_issues", [])
    grounding_passed = result.get("grounding_check_passed", True)
    explanation_valid = result.get("explanation_valid", False)
    reason = result.get("reason", "")
    deduction_process = result.get("deduction_process", "")
    
    print(f"\n1. 反向解题:")
    print(f"   成功: {'✅' if reverse_solve else '❌'}")
    print(f"   能推导唯一答案: {'✅' if can_deduce else '❌'}")
    print(f"   Critic 推导的答案: {critic_answer}")
    print(f"   生成者的答案: {test_question['正确答案']}")
    print(f"   答案一致: {'✅' if critic_answer == test_question['正确答案'] else '❌'}")
    if deduction_process:
        print(f"   推导过程: {deduction_process[:200]}...")
    
    print(f"\n2. 题目质量检查:")
    print(f"   通过: {'✅' if quality_passed else '❌'}")
    if quality_issues:
        print(f"   质量问题:")
        for issue in quality_issues:
            print(f"     - {issue}")
    else:
        print(f"   未发现质量问题")
    
    print(f"\n3. 信息不对称校验:")
    print(f"   通过: {'✅' if grounding_passed else '❌'}")
    
    print(f"\n4. 解析审查:")
    print(f"   有效: {'✅' if explanation_valid else '❌'}")
    
    print(f"\n5. 综合判定:")
    all_passed = (reverse_solve and can_deduce and 
                  critic_answer == test_question['正确答案'] and
                  quality_passed and
                  grounding_passed and
                  explanation_valid)
    
    if all_passed:
        print(f"   ✅✅✅ 审核通过！题目符合所有条件")
    else:
        print(f"   ❌❌❌ 审核驳回！题目不符合条件")
        print(f"\n   失败原因:")
        if not reverse_solve or not can_deduce:
            print(f"     - 反向解题失败：无法推导出唯一答案")
        if critic_answer != test_question['正确答案']:
            print(f"     - 答案不一致：Critic 推导为 {critic_answer}，生成者为 {test_question['正确答案']}")
        if not quality_passed:
            print(f"     - 题目质量不合格")
            if quality_issues:
                for issue in quality_issues:
                    print(f"       • {issue}")
        if not grounding_passed:
            print(f"     - 信息不对称校验失败")
        if not explanation_valid:
            print(f"     - 解析不合格")
    
    print(f"\n6. 详细评价:")
    print(f"   {reason}")
    
except Exception as e:
    print(f"\n❌ JSON 解析失败: {e}")
    print(f"尝试从文本中提取 JSON...")
    import re
    json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response_text, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group())
            print(f"✅ 从文本中提取 JSON 成功:")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except:
            print(f"❌ 仍然无法解析 JSON")
            print(f"原始响应前500字符:")
            print(response_text[:500])
    else:
        print(f"❌ 未找到 JSON 格式内容")
        print(f"原始响应前500字符:")
        print(response_text[:500])

print(f"\n" + "="*80)
print("测试完成")
print("="*80)
