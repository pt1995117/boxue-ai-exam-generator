#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查找知识库中与计算相关的知识点"""
import json

# 计算相关的关键词
calculation_keywords = [
    '契税', '土地出让金', '房龄', '贷款年限', '建筑面积', '成本价', 
    '容积率', '计算', '公式', '税率', '税费', '面积', '年限', 
    '比例', '百分比', '金额', '单价', '总价', '等于', '结果',
    '土地出让', '税费计算', '税费缴纳', '税费标准'
]

print("="*80)
print("查找计算相关的知识点")
print("="*80)

calculation_chunks = []

with open('bot_knowledge_base.jsonl', 'r', encoding='utf-8') as f:
    for line_num, line in enumerate(f, 1):
        try:
            chunk = json.loads(line)
            content = chunk.get('核心内容', '') + chunk.get('完整路径', '')
            
            # 检查是否包含计算关键词
            for keyword in calculation_keywords:
                if keyword in content:
                    calculation_chunks.append({
                        'line': line_num,
                        'path': chunk.get('完整路径', ''),
                        'content': chunk.get('核心内容', '')[:200],  # 只取前200字符
                        'keyword': keyword
                    })
                    break  # 找到一个关键词就够了
        except json.JSONDecodeError:
            continue

print(f"\n找到 {len(calculation_chunks)} 个计算相关的知识点\n")

# 按关键词分组显示
keyword_groups = {}
for chunk in calculation_chunks:
    kw = chunk['keyword']
    if kw not in keyword_groups:
        keyword_groups[kw] = []
    keyword_groups[kw].append(chunk)

# 显示前10个最相关的
print("="*80)
print("计算相关知识点列表（按关键词分组）")
print("="*80)

# 优先显示：契税、土地出让金、房龄、贷款年限
priority_keywords = ['契税', '土地出让金', '房龄', '贷款年限', '建筑面积', '成本价', '容积率']

for kw in priority_keywords:
    if kw in keyword_groups:
        chunks = keyword_groups[kw]
        print(f"\n【{kw}】相关知识点 ({len(chunks)} 个):")
        for i, chunk in enumerate(chunks[:3], 1):  # 只显示前3个
            print(f"  {i}. {chunk['path']}")
            print(f"     内容: {chunk['content'][:100]}...")
            print()

# 显示其他关键词
print("\n" + "="*80)
print("其他计算相关知识点")
print("="*80)

for kw in sorted(keyword_groups.keys()):
    if kw not in priority_keywords:
        chunks = keyword_groups[kw]
        print(f"\n【{kw}】: {len(chunks)} 个知识点")
        for chunk in chunks[:2]:  # 只显示前2个
            print(f"  - {chunk['path']}")

# 保存到文件
output_file = 'calculation_topics.json'
with open(output_file, 'w', encoding='utf-8') as f:
    json.dump(calculation_chunks[:50], f, ensure_ascii=False, indent=2)

print(f"\n\n已保存前50个知识点到: {output_file}")
