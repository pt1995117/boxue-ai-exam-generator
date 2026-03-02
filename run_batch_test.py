#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch test - generates multiple questions"""
import os
import json
import random
from datetime import datetime

# Load config
config = {}
with open("填写您的Key.txt", 'r', encoding='utf-8') as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()

from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
from exam_graph import app as graph_app

print("="*80)
print("批量题目生成测试")
print("="*80)

retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)

# Find chunks
calc_chunks = []
non_calc_chunks = []

for chunk in retriever.kb_data[:200]:
    content = chunk.get('核心内容', '') + chunk.get('完整路径', '')
    if any(kw in content for kw in ['土地出让金', '房龄', '贷款年限', '建筑面积', '成本价', '容积率']):
        if len(calc_chunks) < 5:
            calc_chunks.append(chunk)
    elif any(kw in content for kw in ['责任', '义务', '权利', '流程', '规定', '要求']):
        if len(non_calc_chunks) < 5:
            non_calc_chunks.append(chunk)
    
    if len(calc_chunks) >= 5 and len(non_calc_chunks) >= 5:
        break

# Fill with random
while len(calc_chunks) < 3:
    calc_chunks.append(retriever.get_random_kb_chunk())
while len(non_calc_chunks) < 3:
    chunk = retriever.get_random_kb_chunk()
    if chunk not in calc_chunks:
        non_calc_chunks.append(chunk)

print(f"\n将生成 {len(calc_chunks)} 道计算题")
print(f"将生成 {len(non_calc_chunks)} 道非计算题\n")

config_dict = {
    "configurable": {
        "model": config.get("OPENAI_MODEL", "deepseek-reasoner"),
        "api_key": config.get("OPENAI_API_KEY", ""),
        "base_url": config.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
        "retriever": retriever,
        "question_type": "单选题",
        "generation_mode": "灵活"
    }
}

all_results = []

def generate_one(chunk, qtype):
    print(f"\n{'='*70}")
    print(f"生成 {qtype}: {chunk.get('完整路径', '')[:50]}...")
    print(f"{'='*70}")
    
    inputs = {
        "kb_chunk": chunk,
        "examples": [],
        "retry_count": 0,
        "logs": []
    }
    
    q_json = None
    agent_used = None
    was_fixed = False
    
    try:
        for event in graph_app.stream(inputs, config=config_dict):
            for node_name, state_update in event.items():
                if 'logs' in state_update:
                    for log in state_update['logs']:
                        print(f"  {log}")
                
                if 'agent_name' in state_update:
                    agent_used = state_update['agent_name']
                
                if 'final_json' in state_update:
                    q_json = state_update['final_json']
                    if state_update.get('was_fixed'):
                        was_fixed = True
        
        if q_json:
            print(f"\n✅ 成功！代理: {agent_used or 'Unknown'}")
            print(f"题干: {q_json.get('题干', '')[:80]}...")
            all_results.append({
                "type": qtype,
                "agent": agent_used,
                "was_fixed": was_fixed,
                "question": q_json
            })
            return True
        else:
            print(f"\n❌ 失败：未返回题目")
            return False
            
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        return False

# Generate calculation questions
for i, chunk in enumerate(calc_chunks[:3], 1):
    print(f"\n[计算题 {i}/3]")
    generate_one(chunk, "计算题")

# Generate non-calculation questions  
for i, chunk in enumerate(non_calc_chunks[:3], 1):
    print(f"\n[非计算题 {i}/3]")
    generate_one(chunk, "非计算题")

# Summary
print("\n" + "="*80)
print("生成结果汇总")
print("="*80)
print(f"\n总计: {len(all_results)} 道题目")
calc_count = sum(1 for r in all_results if r['type'] == '计算题')
non_calc_count = sum(1 for r in all_results if r['type'] == '非计算题')
print(f"计算题: {calc_count} 道")
print(f"非计算题: {non_calc_count} 道")

# Display all
print("\n" + "="*80)
print("题目详情")
print("="*80)

for i, result in enumerate(all_results, 1):
    q = result['question']
    print(f"\n{'='*70}")
    print(f"题目 {i}: {result['type']} - {result['agent']}")
    if result['was_fixed']:
        print("⚠️  此题目经过修复")
    print(f"{'='*70}")
    print(f"\n题干: {q.get('题干', '')}")
    print(f"\n选项:")
    for j in range(1, 5):
        opt = q.get(f'选项{j}', '') or '(空)'
        marker = "✓" if chr(64+j) == q.get('正确答案', '') else " "
        print(f"  {marker} {chr(64+j)}. {opt}")
    print(f"\n正确答案: {q.get('正确答案', '')}")
    print(f"\n解析:\n{q.get('解析', '')}")
    print(f"\n难度值: {q.get('难度值', 'N/A')}")

print("\n测试完成！")
