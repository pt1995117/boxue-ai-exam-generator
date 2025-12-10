import pandas as pd
import json
import random

def load_few_shot_examples(excel_path):
    df = pd.read_excel(excel_path)
    # Pick 3 examples: 1 Easy, 1 Medium, 1 Hard (if possible, or just first 3)
    # For now, let's just take the first 3 valid ones.
    examples = []
    for _, row in df.head(3).iterrows():
        ex = {
            "题干": row['题干'],
            "选项": {
                "A": row['选项1'],
                "B": row['选项2'],
                "C": row['选项3'],
                "D": row['选项4']
            },
            "正确答案": row['正确答案'],
            "解析": row['解析'],
            "难度": row['难度值']
        }
        examples.append(ex)
    return examples

def create_prompt(knowledge_point, examples):
    # knowledge_point is a dict from the JSONL
    
    prompt = f"""
# Role
You are an expert exam question setter for the "Real Estate Broker Professional Exam".
Your goal is to create a high-quality exam question based *strictly* on the provided [Reference Material].
The question must match the style, format, and logic of the [Few-Shot Examples].

# Reference Material
【Path】: {knowledge_point['完整路径']}
【Content】:
{knowledge_point['核心内容']}

# Few-Shot Examples (Follow this style!)
"""
    for i, ex in enumerate(examples, 1):
        prompt += f"""
### Example {i}
**Question**: {ex['题干']}
**Options**:
A. {ex['选项']['A']}
B. {ex['选项']['B']}
C. {ex['选项']['C']}
D. {ex['选项']['D']}
**Answer**: {ex['正确答案']}
**Explanation**: {ex['解析']}
**Difficulty**: {ex['难度']}
---
"""

    prompt += """
# Task
Generate 1 new single-choice question based on the [Reference Material].

# Requirements
1.  **Accuracy**: The question must be directly answerable from the Reference Material. Do not use outside knowledge.
2.  **Distractors**: The wrong options must be plausible but clearly incorrect based on the text. Design them to test common misunderstandings (e.g., swapping conditions, wrong numbers).
3.  **Explanation Format**: You MUST strictly follow this format:
    1、教材原文。 [Quote the exact sentence]
    2、试题分析。 [Explain why the answer is right and why others are wrong]
    3、结论。 [State the final answer]
4.  **Output Format**: Provide the output as a JSON object with keys: "题干", "选项1", "选项2", "选项3", "选项4", "正确答案", "解析", "难度值", "考点".

# Output
"""
    return prompt

def main():
    kb_path = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/bot_knowledge_base.jsonl"
    history_path = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/存量房买卖母卷ABCD.xls"
    
    # Load KB
    with open(kb_path, 'r') as f:
        kb_data = [json.loads(line) for line in f]
    
    # Pick a random knowledge point that has content
    valid_kbs = [k for k in kb_data if k['核心内容'] and "（章节标题" not in k['Bot专用切片']]
    target_kb = random.choice(valid_kbs)
    
    # Load Examples
    examples = load_few_shot_examples(history_path)
    
    # Generate Prompt
    prompt = create_prompt(target_kb, examples)
    
    print("Generated Prompt for Knowledge Point:", target_kb['完整路径'])
    
    with open("question_generation_prompt.txt", "w") as f:
        f.write(prompt)
    
    print("Prompt saved to question_generation_prompt.txt")

if __name__ == "__main__":
    main()
