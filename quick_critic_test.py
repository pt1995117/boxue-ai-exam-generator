#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速测试 Critic"""
import json
import sys

# 测试题目
test_q = {
    "题干": "王女士在咨询一套房源时,对经纪人说:\"我想先了解房子本身那些实实在在的特点。\"作为经纪人,你应该以下列哪项为例向她说明房屋的实物信息?",
    "选项1": "房屋的产权是否清晰无纠纷",
    "选项2": "房屋的户型为三室两厅",
    "选项3": "房屋所在学区的排名情况",
    "选项4": "房屋近半年的市场成交价格",
    "正确答案": "B"
}

kb_content = """房源实物信息是指房地产可见、可触摸的部分，包括房屋面积、户型、朝向、楼层高度、装修状况等具体物理特征。

实物信息包括：房屋面积、户型、朝向、楼层高度、装修状况、建筑结构等。
非实物信息包括：产权信息、位置配套信息、交易价格信息等。"""

print("="*60)
print("测试题目:")
print(f"题干: {test_q['题干']}")
print(f"选项:")
print(f"  A. {test_q['选项1']}")
print(f"  B. {test_q['选项2']}")
print(f"  C. {test_q['选项3']}")
print(f"  D. {test_q['选项4']}")
print(f"正确答案: {test_q['正确答案']}")

# 读取配置
config = {}
with open("填写您的Key.txt", 'r', encoding='utf-8') as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            config[k.strip()] = v.strip()

from exam_graph import generate_content, parse_json_from_response, CRITIC_API_KEY, CRITIC_BASE_URL, CRITIC_MODEL, CRITIC_PROVIDER

critic_model = CRITIC_MODEL or config.get("OPENAI_MODEL", "deepseek-reasoner")
critic_api_key = CRITIC_API_KEY or config.get("OPENAI_API_KEY", "")
critic_base_url = CRITIC_BASE_URL or config.get("OPENAI_BASE_URL", "https://openapi-ait.ke.com")
critic_provider = CRITIC_PROVIDER or None

print(f"\n使用模型: {critic_model}")

prompt = f"""你是审计人，需要审核以下题目。

# 教材规则
{kb_content}

# 题目
题干: {test_q['题干']}
A. {test_q['选项1']}
B. {test_q['选项2']}
C. {test_q['选项3']}
D. {test_q['选项4']}

# 任务
1. 反向解题：仅凭题目条件，能否推导出唯一答案？
2. **题目质量深度检查**（必须严格检查）：
   - **语境强度**：题干中"实实在在的特点"是否模糊？在汉语中"实实在在"可能指向产权、地段等多维度，若语境不明确则 context_strength="弱"
   - **选项维度一致性**：A=产权(法律)、B=户型(实物)、C=学区(位置)、D=价格，是否跨多维度？若跨维度则 option_dimension_consistency=false
   - 若语境模糊或选项跨维度，必须 quality_check_passed=false
3. 给出你的答案和详细评价

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

print("\n正在调用 Critic...")
response = generate_content(critic_model, prompt, critic_api_key, critic_base_url, critic_provider)

# 若返回空，则用主模型（DeepSeek）再试一次，便于本地测试
if not response or not response.strip():
    print("(首次调用未返回内容，改用主模型 DeepSeek 进行 Critic 测试)")
    main_model = config.get("OPENAI_MODEL", "deepseek-reasoner")
    main_key = config.get("OPENAI_API_KEY", "")
    main_url = config.get("OPENAI_BASE_URL", "https://openapi-ait.ke.com")
    response = generate_content(main_model, prompt, main_key, main_url, None)

print("\n" + "="*60)
print("Critic 响应:")
print("="*60)
print(response if response else "(空)")

try:
    result = parse_json_from_response(response)
    print("\n" + "="*60)
    print("解析结果:")
    print("="*60)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    print("\n" + "="*60)
    print("审核结论:")
    print("="*60)
    print(f"Critic 答案: {result.get('critic_answer', 'UNKNOWN')}")
    print(f"生成者答案: {test_q['正确答案']}")
    print(f"答案一致: {'✅' if result.get('critic_answer') == test_q['正确答案'] else '❌'}")
    print(f"反向解题成功: {'✅' if result.get('reverse_solve_success') else '❌'}")
    print(f"题目质量合格: {'✅' if result.get('quality_check_passed', True) else '❌'}")
    print(f"语境强度: {result.get('context_strength', '未提供')}")
    print(f"选项维度一致性: {'✅' if result.get('option_dimension_consistency', True) else '❌'}")
    if result.get('quality_issues'):
        print(f"质量问题: {', '.join(result['quality_issues'])}")
    print(f"\n详细评价:\n{result.get('reason', '')}")
    
    # 综合判定
    q = result.get('quality_check_passed', True)
    cs = result.get('context_strength', '中')
    odc = result.get('option_dimension_consistency', True)
    print("\n" + "="*60)
    if not q or cs == "弱" or not odc:
        print("❌❌❌ Critic 判定：此题不符合条件，不应通过")
    else:
        print("✅✅✅ Critic 判定：此题符合条件")
except Exception as e:
    print(f"\n⚠️ JSON 解析失败: {e}")
    if response:
        print("原始响应:", response[:400])
