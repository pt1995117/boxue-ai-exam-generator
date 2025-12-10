#!/usr/bin/env python3
"""
Map historical questions to knowledge points.
Creates a JSON file that links each question to its most relevant KB entry.
"""
import json
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import re

# Paths
KB_PATH = "bot_knowledge_base.jsonl"
HISTORY_PATH = "存量房买卖母卷ABCD.xls"
OUTPUT_PATH = "question_knowledge_mapping.json"

def normalize_text(text):
    """Normalize text for comparison."""
    if not isinstance(text, str):
        return ""
    # Remove punctuation and whitespace
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip().lower()

def load_data():
    """Load knowledge base and historical questions."""
    print("Loading knowledge base...")
    kb_data = []
    with open(KB_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            kb_data.append(json.loads(line))
    
    print(f"Loaded {len(kb_data)} KB entries")
    
    print("Loading historical questions...")
    history_df = pd.read_excel(HISTORY_PATH)
    print(f"Loaded {len(history_df)} historical questions")
    
    return kb_data, history_df

def find_best_match(question_row, kb_data, vectorizer, kb_tfidf_matrix):
    """Find the best matching KB entry for a question."""
    question_kp = str(question_row.get('考点', '')).strip()
    question_stem = str(question_row.get('题干', '')).strip()
    
    # Strategy 1: Exact match on path
    for idx, kb_entry in enumerate(kb_data):
        kb_path = kb_entry['完整路径']
        # Check if the question's 考点 appears in the KB path
        if question_kp and question_kp in kb_path:
            return idx, kb_path, 1.0, "exact_path_match"
    
    # Strategy 2: Fuzzy match on normalized 考点
    norm_qkp = normalize_text(question_kp)
    if norm_qkp:
        for idx, kb_entry in enumerate(kb_data):
            kb_path = kb_entry['完整路径']
            norm_path = normalize_text(kb_path)
            if norm_qkp in norm_path or norm_path in norm_qkp:
                return idx, kb_path, 0.8, "fuzzy_path_match"
    
    # Strategy 3: TF-IDF similarity on content
    query_text = f"{question_kp} {question_stem}"
    query_vec = vectorizer.transform([query_text])
    similarities = cosine_similarity(query_vec, kb_tfidf_matrix).flatten()
    best_idx = similarities.argmax()
    best_score = similarities[best_idx]
    
    return best_idx, kb_data[best_idx]['完整路径'], float(best_score), "tfidf_similarity"

def create_mapping():
    """Create the question-to-knowledge mapping."""
    kb_data, history_df = load_data()
    
    # Build TF-IDF index for KB
    print("Building TF-IDF index...")
    kb_corpus = [f"{entry['完整路径']} {entry['核心内容']}" for entry in kb_data]
    vectorizer = TfidfVectorizer()
    kb_tfidf_matrix = vectorizer.fit_transform(kb_corpus)
    
    # Map each question
    print("Mapping questions to knowledge points...")
    mapping = {}
    
    for idx, row in history_df.iterrows():
        kb_idx, kb_path, confidence, method = find_best_match(row, kb_data, vectorizer, kb_tfidf_matrix)
        
        mapping[idx] = {
            "题干": str(row.get('题干', ''))[:100] + "...",  # Truncate for readability
            "考点": str(row.get('考点', '')),
            "matched_kb_path": kb_path,
            "matched_kb_index": int(kb_idx),  # Convert to Python int
            "confidence": confidence,
            "method": method
        }
        
        if (idx + 1) % 100 == 0:
            print(f"  Processed {idx + 1}/{len(history_df)} questions")
    
    # Save mapping
    print(f"Saving mapping to {OUTPUT_PATH}...")
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    
    # Print statistics
    print("\nMapping Statistics:")
    methods = {}
    for entry in mapping.values():
        method = entry['method']
        methods[method] = methods.get(method, 0) + 1
    
    for method, count in sorted(methods.items()):
        print(f"  {method}: {count} questions ({count/len(mapping)*100:.1f}%)")
    
    print(f"\nMapping complete! Saved to {OUTPUT_PATH}")

if __name__ == "__main__":
    create_mapping()
