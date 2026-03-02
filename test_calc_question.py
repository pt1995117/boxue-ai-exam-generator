#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直接测试生成一道金融计算题
快速验证系统是否正常工作
"""
import os
import json
from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
from exam_graph import app as graph_app

print("=" * 60)
print("快速测试：生成一道金融计算题")
print("=" * 60)

# Load config
config_path = "填写您的Key.txt"
config = {}
if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()

api_key = config.get("OPENAI_API_KEY", "")
base_url = config.get("OPENAI_BASE_URL", "https://api.deepseek.com")
model_name = config.get("OPENAI_MODEL", "deepseek-reasoner")

print(f"\n配置:")
print(f"  Model: {model_name}")
print(f"  Base URL: {base_url}")
print(f"  API Key: {api_key[:20]}...")

# Initialize retriever
print("\n正在加载知识库...")
retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)

# Find a finance-related knowledge point
print("\n正在查找金融计算类知识点...")
all_chunks = retriever.kb_data
finance_keywords = ["计算", "税费", "贷款", "土地出让金", "契税", "增值税", "房龄", "面积", "年限"]

finance_chunks = []
for chunk in all_chunks:
    content = chunk.get('核心内容', '')
    path = chunk.get('完整路径', '')
    combined = content + path
    
    # Check if it's a finance-related chunk
    if any(keyword in combined for keyword in finance_keywords):
        # Prefer chunks with specific calculation rules
        if any(calc_word in combined for calc_word in ["土地出让金", "契税", "房龄", "贷款年限"]):
            finance_chunks.append(chunk)

if not finance_chunks:
    print("⚠️ 未找到金融计算类知识点，使用随机知识点")
    import random
    valid_chunks = [c for c in all_chunks if c.get('核心内容')]
    test_chunk = random.choice(valid_chunks)
else:
    # Pick one with calculation rules
    import random
    test_chunk = random.choice(finance_chunks)

print(f"\n✅ 选中知识点:")
print(f"  路径: {test_chunk.get('完整路径', 'N/A')}")
print(f"  内容片段: {test_chunk.get('核心内容', '')[:100]}...")

# Prepare inputs
inputs = {
    "kb_chunk": test_chunk,
    "examples": [],  # Will be fetched by nodes
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

print("\n" + "=" * 60)
print("开始生成题目...")
print("=" * 60)

try:
    q_json = None
    logs = []
    
    # Stream events
    for event in graph_app.stream(inputs, config=config_dict):
        for node_name, state_update in event.items():
            # Collect logs
            if 'logs' in state_update:
                for log in state_update['logs']:
                    logs.append(f"[{node_name}] {log}")
                    print(f"  {log}")
            
            # Check for final result
            if 'final_json' in state_update:
                q_json = state_update['final_json']
            
            # Show progress
            if node_name == "router":
                if 'router_details' in state_update:
                    details = state_update['router_details']
                    print(f"\n  🧠 路由决策: {details.get('agent', 'Unknown')}")
                    print(f"     金融分: {details.get('score_finance', 0)}, 法律分: {details.get('score_legal', 0)}")
            
            if node_name == "finance":
                if 'tool_usage' in state_update:
                    usage = state_update['tool_usage']
                    tool = usage.get('tool', 'None')
                    if tool != "None":
                        print(f"\n  🧮 计算器调用: {tool}")
                        print(f"     参数: {usage.get('params', {})}")
                        print(f"     结果: {usage.get('result', 'N/A')}")
            
            if node_name == "critic":
                feedback = state_update.get('critic_feedback', '')
                if feedback == "PASS":
                    print(f"\n  ✅ 批评家审核通过")
                else:
                    print(f"\n  ⚠️ 批评家反馈: {feedback}")
    
    print("\n" + "=" * 60)
    
    if q_json:
        print("✅ 题目生成成功！")
        print("\n生成的题目:")
        print("-" * 60)
        print(f"题干: {q_json.get('题干', 'N/A')}")
        print(f"\n选项:")
        for i in range(1, 5):
            opt_key = f"选项{i}"
            opt_val = q_json.get(opt_key, '')
            opt_label = chr(64 + i)  # A, B, C, D
            print(f"  {opt_label}. {opt_val if opt_val else '(空)'}")
        print(f"\n正确答案: {q_json.get('正确答案', 'N/A')}")
        print(f"难度值: {q_json.get('难度值', 'N/A')}")
        print(f"考点: {q_json.get('考点', 'N/A')}")
        print(f"\n解析:")
        print(f"{q_json.get('解析', 'N/A')}")
        
        # Check for None values
        print("\n" + "=" * 60)
        print("数据完整性检查:")
        print("-" * 60)
        has_none = False
        for key, value in q_json.items():
            if value is None:
                print(f"  ❌ {key}: None")
                has_none = True
            elif value == "":
                print(f"  ⚠️  {key}: 空字符串")
        
        if not has_none:
            print("  ✅ 所有字段都有值，无 None")
        
        # Save to file
        output_file = "test_question_output.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(q_json, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 题目已保存到: {output_file}")
        
    else:
        print("❌ 题目生成失败")
        print("\n日志:")
        for log in logs:
            print(f"  {log}")
    
except Exception as e:
    print(f"\n❌ 错误: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
