#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试语境模糊和维度不一致问题的识别"""
import json
import sys

print("="*80)
print("测试：Critic 是否能识别语境模糊和维度不一致问题")
print("="*80)

# 问题题目（用户指出的问题题目）
problem_question = {
    "题干": "王女士在咨询一套房源时,对经纪人说:\"我想先了解房子本身那些实实在在的特点。\"作为经纪人,你应该以下列哪项为例向她说明房屋的实物信息？",
    "选项1": "房屋的产权是否清晰无纠纷",
    "选项2": "房屋的户型为三室两厅",
    "选项3": "房屋所在学区的排名情况",
    "选项4": "房屋近半年的市场成交价格",
    "正确答案": "B"
}

print(f"\n问题题目:")
print(f"题干: {problem_question['题干']}")
print(f"A. {problem_question['选项1']}")
print(f"B. {problem_question['选项2']}")
print(f"C. {problem_question['选项3']}")
print(f"D. {problem_question['选项4']}")

print(f"\n问题分析:")
print(f"1. 语境模糊: '实实在在的特点'在汉语语境下，产权和地段也可以算'实实在在'的")
print(f"2. 维度不一致: 选项跨多个维度（A=法律，B=实物，C=位置，D=价格）")
print(f"3. 干扰项无效: 考生无需专业知识就能通过常识判断")

# 读取配置
config = {}
with open("填写您的Key.txt", 'r', encoding='utf-8') as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            config[k.strip()] = v.strip()

from exam_graph import generate_content, CRITIC_API_KEY, CRITIC_BASE_URL, CRITIC_MODEL, CRITIC_PROVIDER

model = CRITIC_MODEL or config.get("OPENAI_MODEL", "deepseek-reasoner")
api_key = CRITIC_API_KEY or config.get("OPENAI_API_KEY", "")
base_url = CRITIC_BASE_URL or config.get("OPENAI_BASE_URL", "https://openapi-ait.ke.com")
provider = CRITIC_PROVIDER or None

kb_content = """房源实物信息是指房地产可见、可触摸的部分，包括房屋面积、户型、朝向、楼层高度、装修状况等具体物理特征。

实物信息包括：房屋面积、户型、朝向、楼层高度、装修状况、建筑结构等。
非实物信息包括：产权信息、位置配套信息、交易价格信息等。"""

prompt = f"""你是审计人，需要审核以下题目。

# 教材规则
{kb_content}

# 题目
题干: {problem_question['题干']}
A. {problem_question['选项1']}
B. {problem_question['选项2']}
C. {problem_question['选项3']}
D. {problem_question['选项4']}

# 任务
**特别注意检查以下问题：**

1. **语境强度检查**：
   - 题干中的"实实在在的特点"是否语境明确？
   - 在汉语语境下，"实实在在"是否可能指向多个维度（产权、地段等）？
   - 如果语境模糊，应判定 context_strength = "弱"

2. **选项维度一致性检查**：
   - 选项A（产权）属于什么维度？
   - 选项B（户型）属于什么维度？
   - 选项C（学区）属于什么维度？
   - 选项D（价格）属于什么维度？
   - 这些选项是否在同一维度内？如果跨多个维度，应判定 option_dimension_consistency = false

3. **干扰项有效性**：
   - 如果选项跨多个维度，考生是否无需专业知识就能通过常识判断？
   - 这样的干扰项是否真正"干扰"？

返回 JSON:
{{
    "critic_answer": "A/B/C/D",
    "reverse_solve_success": true/false,
    "quality_check_passed": true/false,
    "quality_issues": [],
    "context_strength": "强/中/弱",
    "option_dimension_consistency": true/false,
    "reason": "详细评价，必须说明语境是否模糊、选项是否跨维度"
}}
"""

print(f"\n正在调用 Critic...")
response = generate_content(model, prompt, api_key, base_url, provider)

print(f"\n" + "="*80)
print("Critic 响应:")
print("="*80)
print(response if response else "(空响应)")

if not response:
    print("\n❌ Critic 返回空响应！")
    sys.exit(1)

try:
    from exam_graph import parse_json_from_response
    result = parse_json_from_response(response)
    
    print(f"\n" + "="*80)
    print("解析结果:")
    print("="*80)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    print(f"\n" + "="*80)
    print("审核结论:")
    print("="*80)
    
    quality_passed = result.get("quality_check_passed", True)
    context_strength = result.get("context_strength", "中")
    dimension_consistent = result.get("option_dimension_consistency", True)
    quality_issues = result.get("quality_issues", [])
    
    print(f"题目质量合格: {'✅' if quality_passed else '❌'}")
    print(f"语境强度: {context_strength}")
    print(f"选项维度一致性: {'✅' if dimension_consistent else '❌'}")
    
    if quality_issues:
        print(f"\n质量问题:")
        for issue in quality_issues:
            print(f"  - {issue}")
    
    print(f"\n详细评价:\n{result.get('reason', '')}")
    
    # 判断是否符合预期
    print(f"\n" + "="*80)
    print("是否符合预期:")
    print("="*80)
    
    expected_fail = (context_strength == "弱" or not dimension_consistent)
    actual_fail = not quality_passed
    
    if expected_fail and actual_fail:
        print("✅✅✅ 符合预期！Critic 正确识别了问题并判定为失败")
    elif expected_fail and not actual_fail:
        print("❌ 不符合预期！Critic 应该判定为失败，但判定为通过")
    elif not expected_fail and actual_fail:
        print("⚠️ 可能误判：Critic 判定为失败，但预期应该通过")
    else:
        print("⚠️ 可能有问题：Critic 判定为通过，但题目确实存在语境模糊和维度不一致问题")
        
except Exception as e:
    print(f"\n❌ 解析失败: {e}")
    import traceback
    traceback.print_exc()

print(f"\n" + "="*80)
