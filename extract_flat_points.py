import re
import pandas as pd
import os

def extract_flat_knowledge_points(input_file, output_file):
    print(f"Reading {input_file}...")
    with open(input_file, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f.readlines()]

    # Regex patterns
    part_pattern = re.compile(r'^(第[一二三四五六七八九十]+篇)\s+(.*)')
    l1_pattern = re.compile(r'^(第[一二三四五六七八九十]+章)\s+(.*)')
    l2_pattern = re.compile(r'^(第[一二三四五六七八九十]+节)\s+(.*)')
    
    # Point patterns (L3, L4, L5)
    # We will treat any of these as a potential "Point" if it has content
    l3_pattern = re.compile(r'^([一二三四五六七八九十]+)、(.*)')
    l4_pattern = re.compile(r'^（([一二三四五六七八九十]+)）(.*)')
    l5_pattern = re.compile(r'^（(\d+)）(.*)')
    l5_pattern_alt = re.compile(r'^(\d+)[、\.](.*)')

    rows = []
    
    current_part = ""
    current_l1 = ""
    current_l2 = ""
    
    # We need to buffer content for the "current point"
    # But points can be nested.
    # The user wants "All knowledge points".
    # If L3 has content, it's a point. If L3 has L4 children, L3 might be a category.
    # Strategy:
    # 1. Identify the "Current Header" (could be L3, L4, L5).
    # 2. Accumulate text.
    # 3. When a NEW header starts, flush the previous header's content.
    # 4. If the previous header had content, add it to rows.
    
    current_point_title = ""
    content_buffer = []
    
    # Helper to flush
    def flush():
        nonlocal current_point_title
        if current_point_title and content_buffer:
            content = "\n".join(content_buffer).strip()
            if content:
                # Add to rows
                rows.append([
                    current_part,
                    current_l1,
                    current_l2,
                    current_point_title,
                    content
                ])
        # Reset
        content_buffer.clear()
        # Note: We don't reset current_point_title here because we set it immediately after flush

    for line in lines:
        if not line:
            continue

        # Check for structure headers (Part, Chapter, Section)
        match_part = part_pattern.match(line)
        match_l1 = l1_pattern.match(line)
        match_l2 = l2_pattern.match(line)
        
        if match_part:
            flush()
            current_part = f"{match_part.group(1)} {match_part.group(2)}"
            current_l1 = ""
            current_l2 = ""
            current_point_title = "" # Reset point context
        elif match_l1:
            flush()
            current_l1 = f"{match_l1.group(1)} {match_l1.group(2)}"
            current_l2 = ""
            current_point_title = ""
        elif match_l2:
            flush()
            current_l2 = f"{match_l2.group(1)} {match_l2.group(2)}"
            current_point_title = ""
        
        else:
            # Check for Point headers
            match_l3 = l3_pattern.match(line)
            match_l4 = l4_pattern.match(line)
            match_l5 = l5_pattern.match(line)
            match_l5_alt = l5_pattern_alt.match(line)
            
            # Determine if it's a new point
            new_point_title = None
            remaining_text = ""
            
            if match_l3:
                new_point_title = f"{match_l3.group(1)}、{match_l3.group(2)}"
                # If there is text in the title group(2), it's part of the title.
                # But sometimes content follows on the same line?
                # The regex captures everything after "、" as group 2.
                # Usually titles are short. If it's very long, it might be title + content?
                # For now, assume it's the title.
            elif match_l4:
                new_point_title = f"（{match_l4.group(1)}）{match_l4.group(2)}"
            elif match_l5:
                new_point_title = f"（{match_l5.group(1)}）{match_l5.group(2)}"
            elif match_l5_alt:
                new_point_title = f"{match_l5_alt.group(1)}、{match_l5_alt.group(2)}"
            
            if new_point_title:
                flush()
                
            if new_point_title:
                flush()
                
                # Get raw text after the number/bullet
                raw_text = ""
                if match_l3: raw_text = match_l3.group(2)
                elif match_l4: raw_text = match_l4.group(2)
                elif match_l5: raw_text = match_l5.group(2)
                elif match_l5_alt: raw_text = match_l5_alt.group(2)
                
                # 1. Remove proficiency labels
                raw_text = re.sub(r'（(了解|熟悉|掌握)）', '', raw_text).strip()
                
                # 2. Split Title and Content
                # Priority: "：" > "。" > "，" (if long)
                
                split_index = -1
                if "：" in raw_text:
                    split_index = raw_text.find("：")
                elif "。" in raw_text:
                    split_index = raw_text.find("。")
                elif "，" in raw_text and len(raw_text) > 15:
                    # Only split by comma if the text is somewhat long, 
                    # assuming the first part is a phrase/keyword.
                    split_index = raw_text.find("，")
                
                if split_index != -1 and split_index < len(raw_text) - 1:
                    # Found separator
                    title_text = raw_text[:split_index+1]
                    # Remove the separator from the title if it is a comma or period, 
                    # but maybe keep colon? 
                    # User wants "words/phrases". "Title：" is not a word. "Title" is.
                    # Let's strip the separator from the title for display.
                    clean_title_text = title_text.rstrip("：。，")
                    
                    # Re-construct prefix
                    prefix = ""
                    if match_l3: prefix = f"{match_l3.group(1)}、"
                    elif match_l4: prefix = f"（{match_l4.group(1)}）"
                    elif match_l5: prefix = f"（{match_l5.group(1)}）"
                    elif match_l5_alt: prefix = f"{match_l5_alt.group(1)}、"
                    
                    current_point_title = prefix + clean_title_text
                    
                    # Content is the rest
                    content_part = raw_text[split_index+1:]
                    content_buffer.append(content_part)
                else:
                    # No split, use full text but cleaned
                    prefix = ""
                    if match_l3: prefix = f"{match_l3.group(1)}、"
                    elif match_l4: prefix = f"（{match_l4.group(1)}）"
                    elif match_l5: prefix = f"（{match_l5.group(1)}）"
                    elif match_l5_alt: prefix = f"{match_l5_alt.group(1)}、"
                    
                    current_point_title = prefix + raw_text
            else:
                # It's content for the current point
                content_buffer.append(line)

    flush() # Flush last

    # Create DataFrame
    headers = ["篇", "章", "节", "知识点", "内容详情"]
    df = pd.DataFrame(rows, columns=headers)
    
    # Save to Excel
    excel_file = output_file.replace('.md', '.xlsx')
    df.to_excel(excel_file, index=False)
    print(f"Generated {excel_file} with {len(rows)} rows.")

if __name__ == "__main__":
    input_path = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/textbook_content.txt"
    output_path = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/concise_knowledge_points.md"
    extract_flat_knowledge_points(input_path, output_path)
