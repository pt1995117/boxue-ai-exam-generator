#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动应用信息不对称校验的修改
"""
import re

file_path = "exam_graph.py"

print("正在读取文件...")
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix 1: Writer Node 约束部分
print("\n1. 修改 Writer Node 约束部分...")
old_constraint = r'约束: "题干"中\*\*禁止\*\*出现"根据材料"、"依据参考资料"等字眼；题目应体现对房地产经纪业务专业性、合规要求、客户服务或计算准确性的考察，避免纯记忆性条款或投诉级别数量；禁止用"最重要/最关键/重点/主要"等表述设计题干或选项，优先考察完整流程、条件、责任边界或操作要点\{writer_uniqueness\}。选项文本内\*\*不要写 A\./B\./C\./D\. 前缀\*\*，只保留选项内容本身。'

new_constraint = '''约束: 
1. "题干"中**禁止**出现"根据材料"、"依据参考资料"等字眼
2. 题目应体现对房地产经纪业务专业性、合规要求、客户服务或计算准确性的考察，避免纯记忆性条款或投诉级别数量
3. 禁止用"最重要/最关键/重点/主要"等表述设计题干或选项，优先考察完整流程、条件、责任边界或操作要点
4. **严禁照搬母题中的数值、条件或逻辑**：必须根据教材规则重新设计题目，使用新的数值和场景
5. **必须包含教材中的所有判定条件**：不得遗漏任何条件（如"容积率 > 1.0"、"房龄+贷款年限≤50年"等），确保题目完整还原教材规则
6. 选项文本内**不要写 A./B./C./D. 前缀**，只保留选项内容本身
{writer_uniqueness}'''

if re.search(old_constraint, content):
    content = re.sub(old_constraint, new_constraint, content)
    print("   ✅ Writer Node 约束已更新")
else:
    print("   ⚠️  未找到匹配的约束文本，可能需要手动修改")

# Fix 2: Calculator Node 任务部分
print("\n2. 修改 Calculator Node 任务部分...")
old_task = r'prompt_gen \+= """\n# 任务\n返回 JSON: \{\{"question": "\.\.\.", "options": \["A", "B", "C", "D"\], "answer": "A/B/C/D", "explanation": "\.\.\."\}\}\n约束: 题干中\*\*禁止\*\*出现"根据材料"或"依据参考资料"。\n"""'

new_task = '''prompt_gen += """
# 任务
**重要：计算类题目必须同步生成 Python 计算代码**

返回 JSON 格式:
{{
    "thought": "根据教材规则，计算逻辑是...",
    "python_code": "def calculate():\\n    # 从题干提取数值\\n    area = 80\\n    cost_price = 1560\\n    # 按照教材规则计算（必须包含所有判定条件）\\n    result = area * cost_price * 0.01\\n    return result",
    "question_data": {{
        "question": "...",
        "options": ["A", "B", "C", "D"],
        "answer": "A/B/C/D",
        "explanation": "..."
    }}
}}

**代码生成要求：**
1. 必须定义 `calculate()` 函数并返回计算结果
2. 从题目场景中提取具体数值（硬编码到函数内部）
3. **严格按照教材规则实现计算逻辑，不得遗漏任何判定条件**（如"容积率 > 1.0"、"房龄+贷款年限≤50年"等）
4. 如果涉及多步计算，代码必须体现完整流程

约束: 题干中**禁止**出现"根据材料"或"依据参考资料"。
"""'''

if 'prompt_gen += """' in content and '# 任务' in content:
    # Find the section and replace
    pattern = r'(prompt_gen \+= """\n# 任务\n返回 JSON:.*?约束: 题干中.*?"""\n)'
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, new_task + '\n', content, flags=re.DOTALL)
        print("   ✅ Calculator Node 任务部分已更新")
    else:
        print("   ⚠️  未找到匹配的任务文本，可能需要手动修改")
else:
    print("   ⚠️  未找到 Calculator Node 任务部分")

# Fix 3: Calculator Node 解析部分
print("\n3. 修改 Calculator Node 解析部分...")
old_parse = r'try:\n\s+draft = parse_json_from_response\(content\)\n\s+\n\s+log_msg = f"🧮 计算专家: 初稿已生成"\n\s+if calc_result is not None:\n\s+log_msg \+= f" \(已调用 \{tool_used\}, 结果=\{calc_result\}\)"\n\s+\n\s+return \{\n\s+"draft": draft,'

new_parse = '''try:
        response_json = parse_json_from_response(content)
        
        # Extract python_code and question_data
        python_code = response_json.get('python_code', None)
        thought = response_json.get('thought', '')
        
        # Get question_data (may be nested or flat)
        if 'question_data' in response_json:
            draft = response_json['question_data']
        else:
            # Fallback: assume flat structure
            draft = {k: v for k, v in response_json.items() if k not in ['python_code', 'thought']}
        
        # Store generated code in state for critic to use
        log_msg = f"🧮 计算专家: 初稿已生成"
        if calc_result is not None:
            log_msg += f" (已调用 {tool_used}, 结果={calc_result})"
        if python_code:
            log_msg += f" (已生成 Python 代码)"
            
        return {
            "draft": draft,
            "generated_code": python_code,  # Store for critic'''

# Find and replace the parse section
pattern = r'(try:\s+draft = parse_json_from_response\(content\).*?"draft": draft,)'
if re.search(pattern, content, re.DOTALL):
    content = re.sub(pattern, new_parse, content, flags=re.DOTALL)
    print("   ✅ Calculator Node 解析部分已更新")
else:
    print("   ⚠️  未找到匹配的解析文本，可能需要手动修改")

# Also need to update the return statement to include generated_code
if '"generated_code": python_code' not in content:
    # Find the return statement and add generated_code
    pattern = r'("draft": draft,\n\s+"tool_usage":)'
    replacement = r'"draft": draft,\n            "generated_code": python_code,  # Store for critic\n            \1'
    if re.search(pattern, content):
        content = re.sub(pattern, replacement, content)
        print("   ✅ 已添加 generated_code 字段")

# Save
print("\n正在保存文件...")
with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("\n✅ 修改完成！")
print("\n请检查以下内容：")
print("1. Writer Node 约束部分（第 605 行附近）")
print("2. Calculator Node 任务部分（第 1272 行附近）")
print("3. Calculator Node 解析部分（第 1284 行附近）")
print("\n如果自动修改不完整，请参考 手动修改指南.md")
