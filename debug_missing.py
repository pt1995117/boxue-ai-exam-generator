import pandas as pd
import re

def normalize(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r'\s+|[、，。：；？！（）().,:;?!]', '', text)

def debug_missing(excel_file):
    df = pd.read_excel(excel_file)
    print("Columns:", df.columns)
    
    # Target strings to look for
    targets = [
        "保障内容",
        "2个机制"
    ]
    
    print("\nSearching for targets in DataFrame...")
    for t in targets:
        found = False
        norm_t = normalize(t)
        print(f"\nTarget: '{t}' (Norm: '{norm_t}')")
        
        for idx, row in df.iterrows():
            # Construct row string
            row_vals = [str(x) for x in row.values if pd.notna(x)]
            row_str = "".join(row_vals)
            norm_row = normalize(row_str)
            
            if norm_t in norm_row:
                print(f"  FOUND in Row {idx}: {row_vals}")
                found = True
                break
        
        if not found:
            print("  NOT FOUND")

if __name__ == "__main__":
    excel = "/Users/panting/Desktop/搏学考试/生成文字版教学内容/mece_knowledge_points.xlsx"
    debug_missing(excel)
