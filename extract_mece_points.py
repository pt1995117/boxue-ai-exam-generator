import re
import pandas as pd
import os

def clean_title(text):
    # 1. Extract proficiency labels
    mastery = ""
    match = re.search(r'（(了解|熟悉|掌握)）', text)
    if match:
        mastery = match.group(1)
        text = re.sub(r'（(了解|熟悉|掌握)）', '', text).strip()
    
    # 2. Split Title and Content (for concise titles)
    # Priority: "：" > "。" > "，" (if long) > "——"
    
    split_index = -1
    if "：" in text:
        split_index = text.find("：")
    elif "。" in text:
        split_index = text.find("。")
    elif "，" in text and len(text) > 15:
        split_index = text.find("，")
    elif "——" in text:
        split_index = text.find("——")
    
    if split_index != -1 and split_index < len(text) - 1:
        # Found separator
        title_part = text[:split_index+1]
        clean_title_part = title_part.rstrip("：。，")
        return clean_title_part, text[split_index+1:], mastery
    else:
        return text, "", mastery

def extract_mece_points(input_file, output_file):
    print(f"Reading {input_file}...")
    with open(input_file, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f.readlines()]

    # Regex patterns
    part_pattern = re.compile(r'^(第[一二三四五六七八九十]+篇)\s+(.*)')
    l1_pattern = re.compile(r'^(第[一二三四五六七八九十]+章)\s+(.*)')
    l2_pattern = re.compile(r'^(第[一二三四五六七八九十]+节)\s+(.*)')
    
    l3_pattern = re.compile(r'^([一二三四五六七八九十]+)、(.*)')
    l4_pattern = re.compile(r'^（([一二三四五六七八九十]+)）(.*)')
    l5_pattern = re.compile(r'^（(\d+)）(.*)')
    l5_pattern_alt = re.compile(r'^(\d+)[、\.](.*)')

    rows = []
    
    # State
    current_part = ""
    current_l1 = ""
    current_l2 = ""
    current_l3 = ""
    current_l4 = ""
    current_l5 = ""
    
    # Track mastery for each level
    l3_mastery = ""
    l4_mastery = ""
    l5_mastery = ""
    
    # We need to track which level is "active" to attach content to it.
    # But wait, content usually follows the header immediately.
    # So if we hit a header, we update state.
    # If we hit content, we append to buffer.
    # When do we flush?
    # We flush when we hit a NEW header.
    # But which level do we associate the flushed content with?
    # The "deepest" active level that was just set.
    
    # Let's track `active_level` (3, 4, or 5).
    active_level = 0 
    content_buffer = []
    
    # Also, if splitting title/content, we might have "immediate content" from the title line.
    # We should add that to buffer immediately.

    def flush():
        nonlocal active_level
        # Flush if we have an active level OR if we have content at a higher level (Intro text)
        if active_level > 0 or (content_buffer and (current_part or current_l1 or current_l2)):
            content = ""
            if content_buffer:
                content = "\n".join(content_buffer).strip()
            
            # Construct row
            # Columns: Part, Chapter, Section, L3, L4, L5, Content, Mastery
            row = [current_part, current_l1, current_l2, "", "", "", "", ""]
            
            current_mastery = ""
            
            if active_level >= 3: 
                row[3] = current_l3
                if active_level == 3: current_mastery = l3_mastery
            if active_level >= 4: 
                row[4] = current_l4
                if active_level == 4: current_mastery = l4_mastery
            if active_level >= 5: 
                row[5] = current_l5
                if active_level == 5: current_mastery = l5_mastery
            
            # Fallback: if current level has no mastery, maybe inherit from parent?
            # User request: "mark... according to... written after each chapter... understand, master..."
            # Usually these are on L3. If L4 is inside L3, it should probably inherit L3's mastery if L4 doesn't have one.
            if not current_mastery:
                if active_level == 4 and l3_mastery: current_mastery = l3_mastery
                elif active_level == 5:
                    if l4_mastery: current_mastery = l4_mastery
                    elif l3_mastery: current_mastery = l3_mastery
            
            row[6] = content
            row[7] = current_mastery
            rows.append(row)
            
        content_buffer.clear()

    for line in lines:
        if not line:
            continue

        # Check structure
        match_part = part_pattern.match(line)
        match_l1 = l1_pattern.match(line)
        match_l2 = l2_pattern.match(line)
        
        if match_part:
            flush()
            current_part = f"{match_part.group(1)} {match_part.group(2)}"
            current_l1 = ""
            current_l2 = ""
            current_l3 = ""
            current_l4 = ""
            current_l5 = ""
            l3_mastery = ""
            l4_mastery = ""
            l5_mastery = ""
            active_level = 0
        elif match_l1:
            flush()
            current_l1 = f"{match_l1.group(1)} {match_l1.group(2)}"
            current_l2 = ""
            current_l3 = ""
            current_l4 = ""
            current_l5 = ""
            l3_mastery = ""
            l4_mastery = ""
            l5_mastery = ""
            active_level = 0
        elif match_l2:
            flush()
            current_l2 = f"{match_l2.group(1)} {match_l2.group(2)}"
            current_l3 = ""
            current_l4 = ""
            current_l5 = ""
            l3_mastery = ""
            l4_mastery = ""
            l5_mastery = ""
            active_level = 0
        else:
            # Check points
            match_l3 = l3_pattern.match(line)
            match_l4 = l4_pattern.match(line)
            match_l5 = l5_pattern.match(line)
            match_l5_alt = l5_pattern_alt.match(line)
            
            new_level = 0
            raw_title = ""
            prefix = ""
            
            if match_l3:
                new_level = 3
                raw_title = match_l3.group(2)
                prefix = f"{match_l3.group(1)}、"
            elif match_l4:
                new_level = 4
                raw_title = match_l4.group(2)
                prefix = f"（{match_l4.group(1)}）"
            elif match_l5:
                new_level = 5
                raw_title = match_l5.group(2)
                prefix = f"（{match_l5.group(1)}）"
            elif match_l5_alt:
                new_level = 5
                raw_title = match_l5_alt.group(2)
                prefix = f"{match_l5_alt.group(1)}、"
            
            if new_level > 0:
                flush()
                active_level = new_level
                
                # Clean and split
                title_text, extra_content, mastery = clean_title(raw_title)
                full_title = prefix + title_text
                
                # Update state
                if new_level == 3:
                    current_l3 = full_title
                    l3_mastery = mastery
                    current_l4 = ""
                    l4_mastery = ""
                    current_l5 = ""
                    l5_mastery = ""
                elif new_level == 4:
                    current_l4 = full_title
                    l4_mastery = mastery
                    current_l5 = ""
                    l5_mastery = ""
                elif new_level == 5:
                    current_l5 = full_title
                    l5_mastery = mastery
                
                if extra_content:
                    content_buffer.append(extra_content)
            else:
                # Check for Implicit Points (e.g. "小贴士：", "情况1：", "Term：")
                # ... (Implicit logic remains similar but needs to handle mastery inheritance)
                
                implicit_match = None
                implicit_title = ""
                implicit_content = ""
                
                # Pattern 1: Sidebar "小贴士"
                if line.startswith("小贴士"):
                    implicit_title = "小贴士"
                    implicit_content = line[3:].lstrip("：: ")
                    implicit_match = True
                
                # Pattern 2: Case "情况X"
                elif re.match(r'^情况\d+[：:]', line):
                    # Split by colon
                    parts = re.split(r'[：:]', line, 1)
                    implicit_title = parts[0]
                    implicit_content = parts[1] if len(parts) > 1 else ""
                    implicit_match = True
                    
                # Pattern 3: Short term definition "Term："
                elif re.match(r'^[^：:]{2,15}[：:]', line):
                    if not re.search(r'[，。,.!?;]', line.split('：')[0].split(':')[0]):
                         parts = re.split(r'[：:]', line, 1)
                         implicit_title = parts[0]
                         implicit_content = parts[1] if len(parts) > 1 else ""
                         implicit_match = True

                if implicit_match:
                    target_level = active_level + 1
                    if target_level > 5: target_level = 5 
                    if target_level < 4: target_level = 4 
                    if active_level == 0: target_level = 3
                    
                    flush()
                    active_level = target_level
                    
                    if target_level == 3:
                        current_l3 = implicit_title
                        current_l4 = ""
                        current_l5 = ""
                        # Implicit points usually don't have their own mastery label, 
                        # so they inherit or have none.
                        # l3_mastery remains whatever it was? No, new L3 means reset.
                        l3_mastery = "" 
                    elif target_level == 4:
                        current_l4 = implicit_title
                        current_l5 = ""
                        l4_mastery = ""
                    elif target_level == 5:
                        current_l5 = implicit_title
                        l5_mastery = ""
                    
                    if implicit_content:
                        content_buffer.append(implicit_content)
                
                else:
                    # Content
                    if current_part or current_l1 or current_l2 or active_level > 0:
                        content_buffer.append(line)

    flush()

    # Create DataFrame
    headers = ["篇", "章", "节", "一级知识点", "二级知识点", "三级知识点", "内容详情", "掌握程度"]
    df = pd.DataFrame(rows, columns=headers)
    
    # Save to Excel
    df.to_excel(output_file, index=False)
    print(f"Generated {output_file} with {len(rows)} rows.")

if __name__ == "__main__":
    input_path = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/textbook_content.txt"
    output_path = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/mece_knowledge_points.xlsx"
    extract_mece_points(input_path, output_path)
