import json
import os

input_file = '/Users/panting/Desktop/搏学考试/AI出题/test_knowledge_slices.jsonl'
output_file = '/Users/panting/Desktop/搏学考试/AI出题/knowledge_slices_preview.md'

print(f"Reading {input_file}...")

if not os.path.exists(input_file):
    print(f"Error: {input_file} does not exist.")
    exit(1)

slices = []
with open(input_file, 'r', encoding='utf-8') as f:
    for line in f:
        if line.strip():
            try:
                slices.append(json.loads(line))
            except json.JSONDecodeError:
                print("Skipping invalid JSON line")

print(f"Found {len(slices)} slices. Generating Markdown...")

with open(output_file, 'w', encoding='utf-8') as f:
    f.write("# 知识切片预览 (Markdown版)\n\n")
    f.write(f"> 总计: {len(slices)} 条\n\n")
    
    for i, data in enumerate(slices, 1):
        f.write(f"## 切片 {i}\n\n")
        
        # 基础信息
        f.write(f"- **完整路径**: `{data.get('完整路径', 'N/A')}`\n")
        f.write(f"- **掌握程度**: {data.get('掌握程度', 'N/A')}\n")
        
        # 结构化内容
        content = data.get('结构化内容', {})
        f.write("\n### 结构化内容详情\n\n")
        
        # 1. 关键参数
        key_params = content.get('key_params', [])
        if key_params:
            f.write(f"**关键词**: {', '.join(key_params)}\n\n")
            
        # 2. 规则
        rules = content.get('rules', [])
        if rules:
            f.write("**规则/要点**:\n")
            for rule in rules:
                f.write(f"- {rule}\n")
            f.write("\n")
            
        # 3. 公式
        formulas = content.get('formulas', [])
        if formulas:
            f.write("**包含公式**:\n")
            for formula in formulas:
                f.write(f"> 📐 `{formula}`\n")
            f.write("\n")
            
        # 3b. 例题 (新增)
        examples = content.get('examples', [])
        if examples:
            f.write("**包含例题**:\n")
            for idx, ex in enumerate(examples, 1):
                f.write(f"> **Example {idx}**:\n")
                f.write("```text\n")
                f.write(ex.strip())
                f.write("\n```\n")
            f.write("\n")
            
        # 4. 上下文 (Before)
        context_before = content.get('context_before', '')
        if context_before:
            f.write("**前置文本**:\n")
            f.write("```text\n")
            f.write(context_before.strip())
            f.write("\n```\n\n")
            
        # 5. 表格
        tables = content.get('tables', [])
        if tables:
            f.write("**包含表格**:\n")
            for idx, table in enumerate(tables, 1):
                f.write(f"**Table {idx}**:\n\n")
                f.write(table + "\n\n")
        
        # 6. 图片
        images = content.get('images', [])
        if images:
            f.write(f"**包含图片**: {len(images)} 张\n\n")
            for idx, img in enumerate(images, 1):
                img_path = img.get('image_path', 'N/A')
                img_id = img.get('image_id', 'N/A')
                analysis = img.get('analysis', '')
                
                f.write(f"**Image {idx} ({img_id})**:\n")
                f.write(f"![{img_id}]({img_path})\n\n")
                if analysis:
                    f.write(f"> **Analysis**: {analysis}\n\n")
                
                # Metadata tags
                tags = []
                if img.get('contains_table'): tags.append("Contains Table")
                if img.get('contains_chart'): tags.append("Contains Chart")
                if tags:
                    f.write(f"> Tags: {', '.join(tags)}\n\n")

        # 7. 上下文 (After)
        context_after = content.get('context_after', '')
        if context_after:
            f.write("**后置文本**:\n")
            f.write("```text\n")
            f.write(context_after.strip())
            f.write("\n```\n\n")

        #元数据
        if data.get('metadata'):
            f.write("\n> Metadata: " + json.dumps(data.get('metadata', {}), ensure_ascii=False) + "\n")
        f.write("\n---\n\n")

print(f"✅ 已生成 Markdown 预览文件: {output_file}")
