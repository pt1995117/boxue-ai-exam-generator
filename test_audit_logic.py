
import os
import json
import sys
from exam_graph import generate_content

# Load Config
config = {}
try:
    with open("填写您的Key.txt", 'r', encoding='utf-8') as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
except Exception as e:
    print(f"Config load error: {e}")

# Model Config
CRITIC_MODEL = config.get("OPENAI_MODEL", "deepseek-reasoner") # Or qwen-max
CRITIC_API_KEY = config.get("OPENAI_API_KEY", "")
CRITIC_BASE_URL = config.get("OPENAI_BASE_URL", "https://openapi-ait.ke.com")

print(f"Using Model: {CRITIC_MODEL}")

# ==========================================
# TEST CASE 1: Geo-Consistency Failure
# ==========================================
print("\n" + "="*50)
print("TEST 1: Geo-Consistency Audit (Beijing vs Shanghai)")
print("="*50)

# Mock Context (Beijing Policy)
kb_content_beijing = """
【北京市】个人购买住房不满2年的，全额征收增值税；满2年的，免征增值税。
适用范围：北京市行政区域内。
"""

# Mock Question (Shanghai - Hallucination)
q_bad_geo = {
    "题干": "王先生在上海市浦东新区购买了一套普通住宅，持有1年后出售。请问他需要缴纳多少增值税？",
    "选项1": "免征",
    "选项2": "全额征收",
    "选项3": "差额征收",
    "选项4": "减半征收",
    "正确答案": "B",
    "解析": "根据规定，不满2年的全额征收。"
}

# The EXACT prompt from exam_graph.py (reconstructed)
prompt_geo = f"""
# 角色
你是严厉的**审计人 (Critic)**，拥有**全量教材逻辑**。
你的任务是进行"反向解题"验证，并执行严格的**全链路防幻觉审计**。

# 全量教材规则（你拥有的完整信息）
{kb_content_beijing}

# 计算辅助
计算结果: None (仅供参考)

# 待审核题目
题干: {q_bad_geo['题干']}
选项: A.{q_bad_geo['选项1']} B.{q_bad_geo['选项2']} C.{q_bad_geo['选项3']} D.{q_bad_geo['选项4']}

**注意**: 你不能看生成者的答案和解析，必须独立推导。

# 核心审计任务 (Audit Tasks) ⚠️

## 1. 地理与范围审计 (Geo-Consistency)
- **规则**: 如果教材明确限定了城市（如"北京市"），题干必须严格遵守。
- **Fail条件**: 
  - 教材=北京，题干=上海/深圳/其他具体城市。
  - 教材=北京，题干=无（若规则具特殊性）。
- **特例**: 干扰项中允许出现其他城市作为错误选项，但题干场景和正确答案必须基于教材指定城市。

## 2. 逻辑自洽性审计 (Logic Validity)
- **规则**: 不要机械比对数字，要比对**判定结果**。
- **Fail条件**: 
  - 题目场景中条件（如"不满2年"）推导出的结论与正确答案冲突。
  - **严重错误案例**: 题目说"北京换房退税"，但并未满足"先卖后买"或"1年内"的核心条件，正确答案通过。

## 3. 反向解题 (Reverse Solving)
- **任务**: 仅凭题目给出的条件，能否根据教材规则推导出**唯一**正确答案？
- **Fail条件**: 
  - 题目条件缺失（如计算个税没给原值）。
  - 存在歧义，有多个正确答案。

## 4. 质量把关
- **Fail条件**: 
  - 题目表述使用了模糊词汇（如"实实在在"）。
  - 选项跨维度（如A法律 B实物 C位置 D价格）。
  - 干扰项过于幼稚，无需专业知识即可排除。

请基于以上标准，输出审核结果。
"""

print("Running Audit on Question 1...")
resp_geo = generate_content(CRITIC_MODEL, prompt_geo, CRITIC_API_KEY, CRITIC_BASE_URL)
print(f"Critic Output:\n{resp_geo}")


# ==========================================
# TEST CASE 2: Logic Validity Failure
# ==========================================
print("\n" + "="*50)
print("TEST 2: Logic Validity Audit (Wrong Deduction)")
print("="*50)

# Mock Question (Bad Logic: < 2 years but says Tax Free)
q_bad_logic = {
    "题干": "李女士在北京市朝阳区购买了一套住房（满1年），现在将其出售。请问增值税如何缴纳？",
    "选项1": "免征增值税",
    "选项2": "全额征收",
    "选项3": "减半",
    "选项4": "不确定",
    "正确答案": "A", 
    "解析": "虽然不满2年，但也免征。（错！教材说不满2年必须全额）"
}

prompt_logic = f"""
# 角色
你是严厉的**审计人 (Critic)**，拥有**全量教材逻辑**。
你的任务是进行"反向解题"验证，并执行严格的**全链路防幻觉审计**。

# 全量教材规则（你拥有的完整信息）
{kb_content_beijing}

# 计算辅助
计算结果: None (仅供参考)

# 待审核题目
题干: {q_bad_logic['题干']}
选项: A.{q_bad_logic['选项1']} B.{q_bad_logic['选项2']} C.{q_bad_logic['选项3']} D.{q_bad_logic['选项4']}

# 生成者声称的答案 (Proposed Answer)
{q_bad_logic['正确答案']}

**注意**: 虽然你能看到生成者的答案，但请先**掩盖它**，进行独立推导，最后再比对。

# 核心审计任务 (Audit Tasks) ⚠️

## 1. 地理与范围审计 (Geo-Consistency)
- **规则**: 如果教材明确限定了城市（如"北京市"），题干必须严格遵守。
- **Fail条件**: 
  - 教材=北京，题干=上海/深圳/其他具体城市。
  - 教材=北京，题干=无（若规则具特殊性）。
- **特例**: 干扰项中允许出现其他城市作为错误选项，但题干场景和正确答案必须基于教材指定城市。

## 2. 逻辑自洽性审计 (Logic Validity)
- **规则**: 不要机械比对数字，要比对**判定结果**。
- **Fail条件**: 
  - 题目场景中条件（如"不满2年"）推导出的结论与正确答案冲突。
  - **严重错误案例**: 题目说"北京换房退税"，但并未满足"先卖后买"或"1年内"的核心条件，正确答案通过。

## 3. 反向解题 (Reverse Solving)
- **任务**: 仅凭题目给出的条件，能否根据教材规则推导出**唯一**正确答案？
- **Fail条件**: 
  - 题目条件缺失（如计算个税没给原值）。
  - 存在歧义，有多个正确答案。

## 4. 质量把关
- **Fail条件**: 
  - 题目表述使用了模糊词汇（如"实实在在"）。
  - 选项跨维度（如A法律 B实物 C位置 D价格）。
  - 干扰项过于幼稚，无需专业知识即可排除。

请基于以上标准，输出审核结果。
"""

print("Running Audit on Question 2...")
resp_logic = generate_content(CRITIC_MODEL, prompt_logic, CRITIC_API_KEY, CRITIC_BASE_URL)
print(f"Critic Output:\n{resp_logic}")
