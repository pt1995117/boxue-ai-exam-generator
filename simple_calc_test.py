#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simple calculation question test"""
import os
import json

print("开始测试...")

# Load config
config = {}
with open("填写您的Key.txt", 'r', encoding='utf-8') as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()

from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
from exam_graph import app as graph_app

retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)

# Find a chunk about "土地出让金" or "房龄"
target_chunk = None
for chunk in retriever.kb_data[:100]:  # Check first 100
    content = chunk.get('核心内容', '') + chunk.get('完整路径', '')
    if '土地出让金' in content or '房龄' in content or '贷款年限' in content:
        target_chunk = chunk
        break

if not target_chunk:
    target_chunk = retriever.get_random_kb_chunk()

print(f"选中知识点: {target_chunk.get('完整路径', '')[:50]}...")

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
        "base_url": config.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
        "retriever": retriever,
        "question_type": "单选题",
        "generation_mode": "灵活"
    }
}

print("正在生成题目...")

q_json = None
try:
    for event in graph_app.stream(inputs, config=config_dict):
        for node_name, state_update in event.items():
            if 'logs' in state_update:
                for log in state_update['logs']:
                    print(f"  [{node_name}] {log}")
            
            if 'final_json' in state_update:
                q_json = state_update['final_json']
    
    if q_json:
        print("\n" + "="*60)
        print("✅ 题目生成成功！")
        print("="*60)
        print(f"\n题干: {q_json.get('题干', '')}")
        print(f"\n选项:")
        for i in range(1, 5):
            opt = q_json.get(f'选项{i}', '') or '(空)'
            print(f"  {chr(64+i)}. {opt}")
        print(f"\n正确答案: {q_json.get('正确答案', '')}")
        print(f"\n解析:\n{q_json.get('解析', '')}")
        
        # Check None
        none_fields = [k for k, v in q_json.items() if v is None]
        if none_fields:
            print(f"\n⚠️ 发现 None 值: {none_fields}")
        else:
            print("\n✅ 所有字段都有值")
            
except Exception as e:
    print(f"\n❌ 错误: {e}")
    import traceback
    traceback.print_exc()

print("\n测试完成")
