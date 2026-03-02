#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch test: Generate multiple questions (calculation and non-calculation)"""
import os
import json
import random
from datetime import datetime

print("="*80)
print("批量题目生成测试")
print("="*80)

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

# Find chunks for calculation questions (土地出让金, 房龄, 贷款年限, etc.)
calc_keywords = ['土地出让金', '房龄', '贷款年限', '建筑面积', '成本价', '容积率', '土地出让', '计算']
non_calc_keywords = ['责任', '义务', '权利', '流程', '规定', '要求', '禁止', '应当', '必须']

calc_chunks = []
non_calc_chunks = []

print("\n正在筛选知识点...")
for chunk in retriever.kb_data[:200]:  # Check first 200 chunks
    content = chunk.get('核心内容', '') + chunk.get('完整路径', '')
    
    # Check for calculation-related keywords
    if any(kw in content for kw in calc_keywords):
        calc_chunks.append(chunk)
    
    # Check for non-calculation keywords (but not calculation-related)
    if any(kw in content for kw in non_calc_keywords) and not any(kw in content for kw in calc_keywords):
        non_calc_chunks.append(chunk)

print(f"找到 {len(calc_chunks)} 个计算相关知识点")
print(f"找到 {len(non_calc_chunks)} 个非计算相关知识点")

# Select chunks for testing
selected_calc_chunks = random.sample(calc_chunks, min(5, len(calc_chunks))) if calc_chunks else []
selected_non_calc_chunks = random.sample(non_calc_chunks, min(5, len(non_calc_chunks))) if non_calc_chunks else []

# If not enough found, use random chunks
all_chunks = retriever.kb_data[:100]
if len(selected_calc_chunks) < 3:
    selected_calc_chunks.extend(random.sample(all_chunks, min(3, len(all_chunks))))
if len(selected_non_calc_chunks) < 3:
    selected_non_calc_chunks.extend(random.sample(all_chunks, min(3, len(all_chunks))))

print(f"\n将生成 {len(selected_calc_chunks)} 道计算题")
print(f"将生成 {len(selected_non_calc_chunks)} 道非计算题")
print(f"总计: {len(selected_calc_chunks) + len(selected_non_calc_chunks)} 道题目\n")

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

results = []
failed_count = 0

def generate_question(chunk, question_type_name):
    """Generate a single question"""
    global failed_count
    
    print(f"\n{'='*80}")
    print(f"生成 {question_type_name}: {chunk.get('完整路径', '')[:60]}...")
    print(f"{'='*80}")
    
    inputs = {
        "kb_chunk": chunk,
        "examples": [],
        "retry_count": 0,
        "logs": []
    }
    
    q_json = None
    agent_used = None
    was_fixed = False
    logs = []
    
    try:
        for event in graph_app.stream(inputs, config=config_dict):
            for node_name, state_update in event.items():
                if 'logs' in state_update:
                    for log in state_update['logs']:
                        logs.append(f"[{node_name}] {log}")
                        print(f"  [{node_name}] {log}")
                
                if 'agent_name' in state_update:
                    agent_used = state_update['agent_name']
                
                if 'final_json' in state_update:
                    q_json = state_update['final_json']
                    if state_update.get('was_fixed'):
                        was_fixed = True
        
        if q_json:
            result = {
                "question_type": question_type_name,
                "knowledge_point": chunk.get('完整路径', ''),
                "agent_used": agent_used or "Unknown",
                "was_fixed": was_fixed,
                "question": q_json,
                "logs": logs,
                "status": "success"
            }
            results.append(result)
            
            print(f"\n✅ 生成成功！")
            print(f"   代理: {agent_used or 'Unknown'}")
            print(f"   是否修复: {'是' if was_fixed else '否'}")
            print(f"   题干: {q_json.get('题干', '')[:80]}...")
            return result
        else:
            failed_count += 1
            print(f"\n❌ 生成失败：未返回题目")
            return {
                "question_type": question_type_name,
                "knowledge_point": chunk.get('完整路径', ''),
                "status": "failed",
                "error": "未返回题目",
                "logs": logs
            }
            
    except Exception as e:
        failed_count += 1
        error_msg = str(e)
        print(f"\n❌ 生成失败：{error_msg}")
        import traceback
        traceback.print_exc()
        return {
            "question_type": question_type_name,
            "knowledge_point": chunk.get('完整路径', ''),
            "status": "failed",
            "error": error_msg,
            "logs": logs
        }

# Generate calculation questions
print("\n" + "="*80)
print("开始生成计算题...")
print("="*80)
for i, chunk in enumerate(selected_calc_chunks, 1):
    print(f"\n[计算题 {i}/{len(selected_calc_chunks)}]")
    generate_question(chunk, "计算题")

# Generate non-calculation questions
print("\n" + "="*80)
print("开始生成非计算题...")
print("="*80)
for i, chunk in enumerate(selected_non_calc_chunks, 1):
    print(f"\n[非计算题 {i}/{len(selected_non_calc_chunks)}]")
    generate_question(chunk, "非计算题")

# Summary
print("\n" + "="*80)
print("生成结果汇总")
print("="*80)

successful_results = [r for r in results if r.get('status') == 'success']
calc_results = [r for r in successful_results if r.get('question_type') == '计算题']
non_calc_results = [r for r in successful_results if r.get('question_type') == '非计算题']

print(f"\n总计生成: {len(results)} 道题目")
print(f"  成功: {len(successful_results)} 道")
print(f"  失败: {failed_count} 道")
print(f"\n计算题: {len(calc_results)} 道")
print(f"非计算题: {len(non_calc_results)} 道")

# Agent statistics
agent_stats = {}
for r in successful_results:
    agent = r.get('agent_used', 'Unknown')
    agent_stats[agent] = agent_stats.get(agent, 0) + 1

if agent_stats:
    print(f"\n代理使用统计:")
    for agent, count in agent_stats.items():
        print(f"  {agent}: {count} 道")

# Fixed questions
fixed_count = sum(1 for r in successful_results if r.get('was_fixed'))
if fixed_count > 0:
    print(f"\n修复的题目: {fixed_count} 道")

# Display all successful questions
print("\n" + "="*80)
print("题目详情")
print("="*80)

for i, result in enumerate(successful_results, 1):
    q = result['question']
    print(f"\n{'='*80}")
    print(f"题目 {i}: {result['question_type']} - {result['agent_used']}")
    print(f"知识点: {result['knowledge_point'][:60]}...")
    if result.get('was_fixed'):
        print("⚠️  此题目经过修复")
    print(f"{'='*80}")
    print(f"\n题干: {q.get('题干', '')}")
    print(f"\n选项:")
    for j in range(1, 5):
        opt = q.get(f'选项{j}', '') or '(空)'
        marker = "✓" if chr(64+j) == q.get('正确答案', '') else " "
        print(f"  {marker} {chr(64+j)}. {opt}")
    print(f"\n正确答案: {q.get('正确答案', '')}")
    print(f"\n解析:\n{q.get('解析', '')}")
    print(f"\n难度值: {q.get('难度值', 'N/A')}")
    print(f"考点: {q.get('考点', 'N/A')}")
    
    # Check for None values
    none_fields = [k for k, v in q.items() if v is None]
    if none_fields:
        print(f"\n⚠️  发现 None 值: {none_fields}")

# Save results to JSON
output_file = f"batch_test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
with open(output_file, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2, default=str)

print(f"\n\n结果已保存到: {output_file}")
print("\n测试完成！")
