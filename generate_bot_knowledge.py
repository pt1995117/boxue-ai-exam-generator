import pandas as pd
import json

def generate_bot_knowledge(input_file, output_excel, output_jsonl):
    print(f"Reading {input_file}...")
    df = pd.read_excel(input_file)
    
    bot_rows = []
    
    for index, row in df.iterrows():
        # 1. Build Context Path
        path_parts = []
        if pd.notna(row['篇']): path_parts.append(str(row['篇']))
        if pd.notna(row['章']): path_parts.append(str(row['章']))
        if pd.notna(row['节']): path_parts.append(str(row['节']))
        if pd.notna(row['一级知识点']): path_parts.append(str(row['一级知识点']))
        if pd.notna(row['二级知识点']): path_parts.append(str(row['二级知识点']))
        if pd.notna(row['三级知识点']): path_parts.append(str(row['三级知识点']))
        
        context_path = " > ".join(path_parts)
        
        # 2. Get Content
        content = ""
        if pd.notna(row['内容详情']):
            content = str(row['内容详情']).strip()
            
        # 3. Construct Full Text Chunk (Optimized for RAG)
        # Format: "【Path】\nContent"
        # If content is empty, maybe the path itself is the knowledge (e.g. a header)?
        # But for a bot, a header without content is less useful unless it implies structure.
        # However, we want "Complete Output".
        
        full_text = f"【归属】：{context_path}\n"
        
        # Add Mastery Level if present
        mastery = ""
        if '掌握程度' in row and pd.notna(row['掌握程度']):
            mastery = str(row['掌握程度']).strip()
            full_text += f"【掌握程度】：{mastery}\n"
            
        if content:
            full_text += f"【内容】：{content}"
        else:
            # If no content, it might be a structural node.
            # We can label it as a structural node.
            full_text += "【内容】：（章节标题/结构节点）"
            
        bot_rows.append({
            "完整路径": context_path,
            "掌握程度": mastery,
            "核心内容": content,
            "Bot专用切片": full_text
        })
        
    # Create DataFrame
    bot_df = pd.DataFrame(bot_rows)
    
    # Save to Excel
    print(f"Saving to {output_excel}...")
    bot_df.to_excel(output_excel, index=False)
    
    # Save to JSONL (for potential fine-tuning or vector DB)
    print(f"Saving to {output_jsonl}...")
    with open(output_jsonl, 'w', encoding='utf-8') as f:
        for _, row in bot_df.iterrows():
            # Standard JSONL format often used: {"text": ...} or {"messages": ...}
            # Let's just dump the dict.
            json.dump(row.to_dict(), f, ensure_ascii=False)
            f.write('\n')
            
    print("Done.")

if __name__ == "__main__":
    input_path = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/mece_knowledge_points.xlsx"
    output_excel = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/bot_knowledge_base.xlsx"
    output_jsonl = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/bot_knowledge_base.jsonl"
    generate_bot_knowledge(input_path, output_excel, output_jsonl)
