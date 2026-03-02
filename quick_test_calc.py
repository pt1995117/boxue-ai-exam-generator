#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quick test - generate one calculation question"""
import os
import json
import sys

# Redirect output to file
output_file = open("test_output.txt", "w", encoding="utf-8")
sys.stdout = output_file
sys.stderr = output_file

print("=" * 60)
print("快速测试：生成金融计算题")
print("=" * 60)

try:
    from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
    from exam_graph import app as graph_app
    
    # Load config
    config = {}
    with open("填写您的Key.txt", 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    
    api_key = config.get("OPENAI_API_KEY", "")
    base_url = config.get("OPENAI_BASE_URL", "https://api.deepseek.com")
    model_name = config.get("OPENAI_MODEL", "deepseek-reasoner")
    
    print(f"\n配置: {model_name}")
    
    # Get retriever
    retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
    
    # Find finance chunk
    finance_keywords = ["土地出让金", "契税", "房龄", "贷款年限"]
    finance_chunks = []
    for chunk in retriever.kb_data:
        content = chunk.get('核心内容', '') + chunk.get('完整路径', '')
        if any(kw in content for kw in finance_keywords):
            finance_chunks.append(chunk)
    
    if finance_chunks:
        import random
        test_chunk = random.choice(finance_chunks)
    else:
        test_chunk = retriever.get_random_kb_chunk()
    
    print(f"\n知识点: {test_chunk.get('完整路径', 'N/A')}")
    
    # Generate
    inputs = {
        "kb_chunk": test_chunk,
        "examples": [],
        "retry_count": 0,
        "logs": []
    }
    
    config_dict = {
        "configurable": {
            "model": model_name,
            "api_key": api_key,
            "base_url": base_url,
            "retriever": retriever,
            "question_type": "单选题",
            "generation_mode": "灵活"
        }
    }
    
    print("\n开始生成...")
    
    q_json = None
    for event in graph_app.stream(inputs, config=config_dict):
        for node_name, state_update in event.items():
            if 'logs' in state_update:
                for log in state_update['logs']:
                    print(f"  {log}")
            
            if 'final_json' in state_update:
                q_json = state_update['final_json']
    
    if q_json:
        print("\n✅ 生成成功！")
        print(f"\n题干: {q_json.get('题干', 'N/A')}")
        print(f"选项1: {q_json.get('选项1', 'N/A')}")
        print(f"选项2: {q_json.get('选项2', 'N/A')}")
        print(f"选项3: {q_json.get('选项3', 'N/A')}")
        print(f"选项4: {q_json.get('选项4', 'N/A')}")
        print(f"正确答案: {q_json.get('正确答案', 'N/A')}")
        
        # Check for None
        has_none = False
        for k, v in q_json.items():
            if v is None:
                print(f"\n❌ {k}: None")
                has_none = True
        
        if not has_none:
            print("\n✅ 无 None 值")
        
        # Save JSON
        with open("test_question.json", "w", encoding="utf-8") as f:
            json.dump(q_json, f, ensure_ascii=False, indent=2)
        print("\n✅ 已保存到 test_question.json")
    else:
        print("\n❌ 生成失败")
        
except Exception as e:
    print(f"\n❌ 错误: {e}")
    import traceback
    traceback.print_exc()

output_file.close()
print("\n输出已保存到 test_output.txt")
