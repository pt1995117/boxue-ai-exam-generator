#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate one question - can be run multiple times"""
import os
import json
import random
import sys
from runtime_paths import load_primary_key_config

# Force UTF-8 output
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("="*80)
print("单题生成测试（可多次运行生成不同题目）")
print("="*80)

# Load config
config = load_primary_key_config()

from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
from exam_graph import app as graph_app

retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)

# Ask user for question type
print("\n请选择题目类型:")
print("1. 计算题（土地出让金、房龄、贷款年限等）")
print("2. 非计算题（责任、义务、流程等）")
print("3. 随机")

choice = input("\n请输入选择 (1/2/3，默认3): ").strip() or "3"

if choice == "1":
    # Find calculation-related chunks
    calc_chunks = []
    for chunk in retriever.kb_data[:200]:
        content = chunk.get('核心内容', '') + chunk.get('完整路径', '')
        if any(kw in content for kw in ['土地出让金', '房龄', '贷款年限', '建筑面积', '成本价', '容积率']):
            calc_chunks.append(chunk)
    if calc_chunks:
        target_chunk = random.choice(calc_chunks)
        print(f"\n✅ 选中计算题知识点: {target_chunk.get('完整路径', '')[:60]}...")
    else:
        target_chunk = retriever.get_random_kb_chunk()
        print(f"\n⚠️  未找到计算题知识点，使用随机知识点")
elif choice == "2":
    # Find non-calculation chunks
    non_calc_chunks = []
    for chunk in retriever.kb_data[:200]:
        content = chunk.get('核心内容', '') + chunk.get('完整路径', '')
        if any(kw in content for kw in ['责任', '义务', '权利', '流程', '规定', '要求']) and \
           not any(kw in content for kw in ['土地出让金', '房龄', '贷款年限', '建筑面积', '成本价']):
            non_calc_chunks.append(chunk)
    if non_calc_chunks:
        target_chunk = random.choice(non_calc_chunks)
        print(f"\n✅ 选中非计算题知识点: {target_chunk.get('完整路径', '')[:60]}...")
    else:
        target_chunk = retriever.get_random_kb_chunk()
        print(f"\n⚠️  未找到非计算题知识点，使用随机知识点")
else:
    target_chunk = retriever.get_random_kb_chunk()
    print(f"\n✅ 随机选中知识点: {target_chunk.get('完整路径', '')[:60]}...")

inputs = {
    "kb_chunk": target_chunk,
    "examples": [],
    "retry_count": 0,
    "logs": []
}

config_dict = {
    "configurable": {
        "model": config.get("OPENAI_MODEL", "deepseek-reasoner"),
        "api_key": config.get("OPENAI_API_KEY", ""),
        "base_url": config.get("OPENAI_BASE_URL", "https://openapi-ait.ke.com"),
        "retriever": retriever,
        "question_type": "单选题",
        "generation_mode": "灵活"
    }
}

print("\n正在生成题目...\n")

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
        print("\n" + "="*80)
        print("✅ 题目生成成功！")
        print("="*80)
        
        if was_fixed:
            print("\n⚠️  此题目经过修复")
        
        print(f"\n代理: {agent_used or 'Unknown'}")
        print(f"\n知识点: {target_chunk.get('完整路径', '')}")
        print(f"\n题干:\n{q_json.get('题干', '')}")
        print(f"\n选项:")
        for i in range(1, 5):
            opt = q_json.get(f'选项{i}', '') or '(空)'
            marker = "✓" if chr(64+i) == q_json.get('正确答案', '') else " "
            print(f"  {marker} {chr(64+i)}. {opt}")
        print(f"\n正确答案: {q_json.get('正确答案', '')}")
        print(f"\n解析:\n{q_json.get('解析', '')}")
        print(f"\n难度值: {q_json.get('难度值', 'N/A')}")
        print(f"考点: {q_json.get('考点', 'N/A')}")
        
        # Check for None values
        none_fields = [k for k, v in q_json.items() if v is None]
        if none_fields:
            print(f"\n⚠️  发现 None 值: {none_fields}")
        else:
            print("\n✅ 所有字段都有值")
            
    else:
        print("\n❌ 生成失败：未返回题目")
        
except Exception as e:
    print(f"\n❌ 错误: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "="*80)
print("生成完成！")
print("="*80)
