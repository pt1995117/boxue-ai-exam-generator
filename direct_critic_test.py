#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import sys

# 强制输出
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

print("="*80, flush=True)
print("直接调用 Critic 审核题目", flush=True)
print("="*80, flush=True)

# 题目
q = {
    "题干": "王女士在咨询一套房源时,对经纪人说:\"我想先了解房子本身那些实实在在的特点。\"作为经纪人,你应该以下列哪项为例向她说明房屋的实物信息？",
    "选项1": "房屋的产权是否清晰无纠纷",
    "选项2": "房屋的户型为三室两厅",
    "选项3": "房屋所在学区的排名情况",
    "选项4": "房屋近半年的市场成交价格",
    "正确答案": "B"
}

print(f"\n题目:", flush=True)
print(f"题干: {q['题干']}", flush=True)
print(f"A. {q['选项1']}", flush=True)
print(f"B. {q['选项2']}", flush=True)
print(f"C. {q['选项3']}", flush=True)
print(f"D. {q['选项4']}", flush=True)
print(f"答案: {q['正确答案']}", flush=True)

# 配置
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

print(f"\n模型: {model}", flush=True)
print(f"Provider: {provider}", flush=True)

kb_content = """房源实物信息是指房地产可见、可触摸的部分，包括房屋面积、户型、朝向、楼层高度、装修状况等具体物理特征。

实物信息包括：房屋面积、户型、朝向、楼层高度、装修状况、建筑结构等。
非实物信息包括：产权信息、位置配套信息、交易价格信息等。"""

prompt = f"""你是审计人，需要审核以下题目。

# 教材规则
{kb_content}

# 题目
题干: {q['题干']}
A. {q['选项1']}
B. {q['选项2']}
C. {q['选项3']}
D. {q['选项4']}

# 任务
1. 反向解题：仅凭题目条件，能否推导出唯一答案？
2. 题目质量检查：题目表述、选项设计、逻辑严谨性是否合格？
3. 给出你的答案和详细评价

返回 JSON:
{{
    "critic_answer": "A/B/C/D",
    "reverse_solve_success": true/false,
    "quality_check_passed": true/false,
    "quality_issues": [],
    "reason": "详细评价"
}}
"""

print(f"\n正在调用 Critic...", flush=True)
response = generate_content(model, prompt, api_key, base_url, provider)

print(f"\n" + "="*80, flush=True)
print("Critic 响应:", flush=True)
print("="*80, flush=True)

if response:
    print(response, flush=True)
    
    try:
        result = json.loads(response)
        print(f"\n" + "="*80, flush=True)
        print("解析结果:", flush=True)
        print("="*80, flush=True)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        
        print(f"\n" + "="*80, flush=True)
        print("审核结论:", flush=True)
        print("="*80, flush=True)
        print(f"Critic 答案: {result.get('critic_answer', 'UNKNOWN')}", flush=True)
        print(f"生成者答案: {q['正确答案']}", flush=True)
        print(f"答案一致: {'✅' if result.get('critic_answer') == q['正确答案'] else '❌'}", flush=True)
        print(f"反向解题成功: {'✅' if result.get('reverse_solve_success') else '❌'}", flush=True)
        print(f"题目质量合格: {'✅' if result.get('quality_check_passed', True) else '❌'}", flush=True)
        if result.get('quality_issues'):
            print(f"质量问题:", flush=True)
            for issue in result['quality_issues']:
                print(f"  - {issue}", flush=True)
        print(f"\n详细评价:\n{result.get('reason', '')}", flush=True)
    except Exception as e:
        print(f"\n⚠️ JSON 解析失败: {e}", flush=True)
        print(f"原始响应:", flush=True)
        print(response[:500], flush=True)
else:
    print("❌ Critic 返回空响应！", flush=True)

print(f"\n" + "="*80, flush=True)
