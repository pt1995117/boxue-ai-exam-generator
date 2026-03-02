#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调用 Critic 审核三道题目"""
import json
import sys

# 三道题目
questions = [
    {
        "题干": "张先生购买了一套建筑面积为85m²的二手普通住宅，成交价格为400万元。请问张先生应缴纳的契税金额是多少？",
        "选项1": "4万元",
        "选项2": "6万元",
        "选项3": "12万元",
        "选项4": "无法确定",
        "正确答案": "?",  # Critic 需独立推导
        "知识点": "契税"
    },
    {
        "题干": "在带客户看房的过程中，经纪人向客户介绍说：「这房子的地段特别好，下楼走两步就是地铁站，周边配套非常成熟。」作为经纪人，你这是在向客户说明房屋的哪类信息？",
        "选项1": "实物信息",
        "选项2": "权益信息",
        "选项3": "区位信息",
        "选项4": "价格信息",
        "正确答案": "C",
        "知识点": "房源信息分类"
    },
    {
        "题干": "王某出售了一套购买时间为3年的普通住宅。根据现行政策，王某在转让该房产时，关于增值税的缴纳说法正确的是：",
        "选项1": "满两年免征增值税",
        "选项2": "满五年免征增值税",
        "选项3": "无论几年都要全额缴纳",
        "选项4": "满两年减半缴纳",
        "正确答案": "A",
        "知识点": "增值税"
    }
]

# 各题对应的教材规则（供 Critic 反向解题与质量检查）
kb_contents = {
    "契税": """契税：个人购买住房，根据面积、是否首套等适用不同税率。如 90m² 以下首套 1%、90m² 以上 1.5%，二套 3% 等。具体需结合当地政策与房屋面积、套数判定。若条件不足无法确定唯一税率，则答案为「无法确定」。""",
    "房源信息分类": """房源信息分类：实物信息（面积、户型、朝向、装修等可见可触部分）；权益信息（产权、抵押、共有人等）；区位信息（地段、交通、配套、学区等）；价格信息（挂牌价、成交价、税费等）。「地段、地铁、周边配套」属于区位信息。""",
    "增值税": """个人转让住房增值税：北上广深以外地区，满2年免征，未满2年按全额或差额计征；北上广深，满2年普通住宅免征。购买满3年即已满2年，普通住宅满2年免征。"""
}

# 读取配置
config = {}
with open("填写您的Key.txt", 'r', encoding='utf-8') as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            config[k.strip()] = v.strip()

from exam_graph import generate_content, parse_json_from_response
from exam_graph import CRITIC_API_KEY, CRITIC_BASE_URL, CRITIC_MODEL, CRITIC_PROVIDER

model = CRITIC_MODEL or config.get("OPENAI_MODEL", "deepseek-reasoner")
api_key = CRITIC_API_KEY or config.get("OPENAI_API_KEY", "")
base_url = CRITIC_BASE_URL or config.get("OPENAI_BASE_URL", "https://api.deepseek.com")
provider = CRITIC_PROVIDER or None

def run_critic(q, kb):
    prompt = f"""你是审计人 (Critic)。请审核以下题目。

# 教材规则
{kb}

# 题目
题干: {q['题干']}
A. {q['选项1']}
B. {q['选项2']}
C. {q['选项3']}
D. {q['选项4']}

# 任务
1. 反向解题：仅凭题干与选项，能否根据教材规则推导出唯一答案？
2. 题目质量：语境是否明确、选项是否同维度、干扰项是否合理？
3. 若题干用「实实在在」「重要」等模糊用语，或选项跨多维度（如 A 法律 B 实物 C 区位 D 价格），须 quality_check_passed=false。

返回 JSON:
{{"critic_answer":"A/B/C/D","reverse_solve_success":true/false,"quality_check_passed":true/false,"quality_issues":[],"context_strength":"强/中/弱","option_dimension_consistency":true/false,"reason":"简要说明"}}
"""
    return generate_content(model, prompt, api_key, base_url, provider)

print("="*80)
print("Critic 审核三道题目")
print("="*80)

for i, q in enumerate(questions, 1):
    kb = kb_contents.get(q["知识点"], "")
    print(f"\n{'='*80}")
    print(f"【题目 {i}】{q['知识点']}")
    print(f"{'='*80}")
    print(f"题干: {q['题干'][:80]}...")
    print(f"A. {q['选项1']}  B. {q['选项2']}  C. {q['选项3']}  D. {q['选项4']}")
    
    resp = run_critic(q, kb)
    
    if not resp or not resp.strip():
        print("Critic 响应: (空)")
        continue
    
    print(f"\nCritic 原始响应:\n{resp[:600]}{'...' if len(resp)>600 else ''}")
    
    try:
        r = parse_json_from_response(resp)
        ans = r.get("critic_answer","?")
        rev = r.get("reverse_solve_success", False)
        qua = r.get("quality_check_passed", True)
        issues = r.get("quality_issues", [])
        ctx = r.get("context_strength", "?")
        dim = r.get("option_dimension_consistency", True)
        reason = r.get("reason", "")
        
        print(f"\n--- 解析结果 ---")
        print(f"Critic 答案: {ans}  |  反向解题成功: {'是' if rev else '否'}  |  质量合格: {'是' if qua else '否'}")
        print(f"语境强度: {ctx}  |  选项同维度: {'是' if dim else '否'}")
        if issues:
            print(f"质量问题: {issues}")
        print(f"说明: {reason[:300]}{'...' if len(reason)>300 else ''}")
        
        if not qua or ctx == "弱" or not dim:
            print(f"结论: 不符合条件，不应通过")
        else:
            print(f"结论: 符合条件")
    except Exception as e:
        print(f"解析异常: {e}\n原始: {resp[:300]}")

print("\n" + "="*80)
print("审核完成")
print("="*80)
