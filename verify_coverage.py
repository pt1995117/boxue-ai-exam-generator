import pandas as pd
import re

def normalize(text):
    # Remove whitespace and punctuation for comparison
    if not isinstance(text, str):
        return ""
    return re.sub(r'\s+|[、，。：；？！（）().,:;?!]', '', text)

def verify_coverage(source_file, excel_file):
    print(f"Reading source: {source_file}")
    with open(source_file, 'r', encoding='utf-8') as f:
        source_lines = [line.strip() for line in f.readlines() if line.strip()]

    print(f"Reading excel: {excel_file}")
    df = pd.read_excel(excel_file)
    
    # Create a giant string or set of extracted content for searching
    # We concatenate all relevant columns
    extracted_texts = []
    for index, row in df.iterrows():
        # Headers
        if pd.notna(row['篇']): extracted_texts.append(str(row['篇']))
        if pd.notna(row['章']): extracted_texts.append(str(row['章']))
        if pd.notna(row['节']): extracted_texts.append(str(row['节']))
        if pd.notna(row['一级知识点']): extracted_texts.append(str(row['一级知识点']))
        if pd.notna(row['二级知识点']): extracted_texts.append(str(row['二级知识点']))
        if pd.notna(row['三级知识点']): extracted_texts.append(str(row['三级知识点']))
        # Content
        if pd.notna(row['内容详情']): extracted_texts.append(str(row['内容详情']))
    
    # Normalize extracted texts
    # We use a set of normalized lines/fragments for faster lookup?
    # But content in Excel might be split differently than source lines.
    # E.g. Source line: "Title: Content"
    # Excel: Row 1 "Title", Row 1 "Content"
    # So we should check if source line is *contained* in the extracted text?
    # Or if the source line's normalized content exists in the normalized extracted content.
    
    # Let's build a single giant normalized string for extraction?
    # No, that might be too messy.
    # Let's try: For each source line, check if its normalized version exists in 
    # ANY of the normalized extracted fields OR if it is a substring of them.
    
    # Optimization: Join all extracted normalized text into one big string?
    # Source lines are sequential.
    # Let's try to match source lines against the dataframe.
    
    print("Normalizing extracted content...")
    # We will keep a list of normalized strings from the excel
    normalized_extracted = [normalize(t) for t in extracted_texts if t]
    
    # To handle "Title: Content" split, we might want to join row content?
    # Actually, if we just check if source_line is in the "bag of words", it's safer.
    # But "bag of words" loses order.
    # Let's try exact match first.
    
    # Better approach:
    # Iterate source lines.
    # Try to find this line in the Excel data.
    # If not found, report it.
    
    # Issue: Source line "（1）Point" might be in Excel as "Point" (Title) and "（1）" (implicit in hierarchy).
    # So we need to be flexible.
    
    missing_lines = []
    
    # Speed up: Create a big joined string of all extracted content
    full_extracted_text = "".join(normalized_extracted)
    
    print("Checking lines...")
    for i, line in enumerate(source_lines):
        norm_line = normalize(line)
        if not norm_line:
            continue
            
        # Check if this normalized line exists in the full extracted text
        if norm_line not in full_extracted_text:
            # Maybe it's page numbers? "1", "10"
            if re.match(r'^\d+$', norm_line):
                continue
            # Maybe it's "目 录"?
            if "目录" in norm_line:
                continue
            
            missing_lines.append((i+1, line))

    print(f"Found {len(missing_lines)} potentially missing lines.")
    
    if missing_lines:
        print("First 20 missing lines:")
        for ln, text in missing_lines[:20]:
            print(f"Line {ln}: {text}")
            
    # Save missing report
    with open("missing_lines_report.txt", "w") as f:
        for ln, text in missing_lines:
            f.write(f"Line {ln}: {text}\n")
            
    print("Report saved to missing_lines_report.txt")

if __name__ == "__main__":
    source = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/textbook_content.txt"
    excel = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/mece_knowledge_points.xlsx"
    verify_coverage(source, excel)
