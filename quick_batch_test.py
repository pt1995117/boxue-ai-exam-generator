#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quick batch test"""
import sys
import os

# Force output
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

print("开始批量测试...", flush=True)

# Load config
config = {}
try:
    with open("填写您的Key.txt", 'r', encoding='utf-8') as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    print("配置加载成功", flush=True)
except Exception as e:
    print(f"配置加载失败: {e}", flush=True)
    sys.exit(1)

from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
from exam_graph import app as graph_app

print("正在初始化知识库...", flush=True)
retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
print(f"知识库加载完成，共 {len(retriever.kb_data)} 个知识点", flush=True)

# Select 3 calculation chunks and 3 non-calculation chunks
calc_chunks = []
non_calc_chunks = []

for chunk in retriever.kb_data[:150]:
    content = chunk.get('核心内容', '') + chunk.get('完整路径', '')
    if any(kw in content for kw in ['土地出让金', '房龄', '贷款年限', '建筑面积', '成本价']):
        if len(calc_chunks) < 3:
            calc_chunks.append(chunk)
    elif any(kw in content for kw in ['责任', '义务', '权利', '流程', '规定']):
        if len(non_calc_chunks) < 3:
            non_calc_chunks.append(chunk)
    
    if len(calc_chunks) >= 3 and len(non_calc_chunks) >= 3:
        break

# Fill with random if not enough
while len(calc_chunks) < 3:
    calc_chunks.append(retriever.get_random_kb_chunk())
while len(non_calc_chunks) < 3:
    chunk = retriever.get_random_kb_chunk()
    if chunk not in calc_chunks:
        non_calc_chunks.append(chunk)

print(f"\n将生成 {len(calc_chunks)} 道计算题", flush=True)
print(f"将生成 {len(non_calc_chunks)} 道非计算题\n", flush=True)

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

all_chunks = [("计算题", c) for c in calc_chunks] + [("非计算题", c) for c in non_calc_chunks]

for idx, (qtype, chunk) in enumerate(all_chunks, 1):
    print(f"\n{'='*70}", flush=True)
    print(f"[{idx}/{len(all_chunks)}] 生成 {qtype}", flush=True)
    print(f"知识点: {chunk.get('完整路径', '')[:50]}...", flush=True)
    print(f"{'='*70}\n", flush=True)
    
    inputs = {
        "kb_chunk": chunk,
        "examples": [],
        "retry_count": 0,
        "logs": []
    }
    
    try:
        q_json = None
        agent_used = None
        
        for event in graph_app.stream(inputs, config=config_dict):
            for node_name, state_update in event.items():
                if 'logs' in state_update:
                    for log in state_update['logs']:
                        print(f"  {log}", flush=True)
                
                if 'agent_name' in state_update:
                    agent_used = state_update['agent_name']
                
                if 'final_json' in state_update:
                    q_json = state_update['final_json']
        
        if q_json:
            print(f"\n✅ 生成成功！代理: {agent_used or 'Unknown'}", flush=True)
            print(f"\n题干: {q_json.get('题干', '')}", flush=True)
            print(f"\n选项:", flush=True)
            for i in range(1, 5):
                opt = q_json.get(f'选项{i}', '') or '(空)'
                marker = "✓" if chr(64+i) == q_json.get('正确答案', '') else " "
                print(f"  {marker} {chr(64+i)}. {opt}", flush=True)
            print(f"\n正确答案: {q_json.get('正确答案', '')}", flush=True)
            print(f"\n解析: {q_json.get('解析', '')[:200]}...", flush=True)
        else:
            print(f"\n❌ 生成失败：未返回题目", flush=True)
            
    except Exception as e:
        print(f"\n❌ 错误: {e}", flush=True)
        import traceback
        traceback.print_exc()

print(f"\n\n{'='*70}", flush=True)
print("批量测试完成！", flush=True)
print(f"{'='*70}", flush=True)
