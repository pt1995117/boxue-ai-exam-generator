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
import os
from typing import Tuple

# Paths
KB_PATH = "bot_knowledge_base.jsonl"
HISTORY_PATH = "存量房买卖母卷ABCD.xls"
OUTPUT_PATH = "question_knowledge_mapping.json"
MODEL_NAME = "deepseek-reasoner"
TOP_N_CANDIDATES = 8
TOP_K_RETRIEVAL = 30
CONF_THRESHOLD = 0.3
AUTO_PASS_TARGET = 0.8
MIN_AUTO_PASS_TFIDF = 0.3
MIN_AUTO_PASS_COVERAGE = 0.3
AUTO_PASS_WEIGHT_TFIDF = 0.7
AUTO_PASS_WEIGHT_COVERAGE = 0.3

def load_config():
    """Load API keys from config file."""
    config = {}
    cfg_path = os.path.join(os.path.dirname(__file__) or '.', '填写您的Key.txt')
    if os.path.isfile(cfg_path):
        with open(cfg_path, 'r', encoding='utf-8') as f:
            for line in f:
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.split('=', 1)
                    config[k.strip()] = v.strip()
    return config


def _usable_key(v: str) -> bool:
    return bool(v) and "请将您的Key" not in v


def _normalize_base_url(url: str) -> str:
    u = str(url or "").strip().rstrip("/")
    if not u:
        return "https://openapi-ait.ke.com/v1"
    if u.endswith("/v1") or u.endswith("/api/v1"):
        return u
    return f"{u}/v1"


def resolve_llm_config(config: dict) -> Tuple[str, str, str]:
    """
    Resolve API_KEY/BASE_URL/MODEL as one consistent triplet.
    Priority: AIT -> OPENAI -> DEEPSEEK -> CRITIC
    """
    default_base = _normalize_base_url(
        config.get("AIT_BASE_URL")
        or config.get("OPENAI_BASE_URL")
        or config.get("DEEPSEEK_BASE_URL")
        or "https://openapi-ait.ke.com/v1"
    )
    default_model = (
        config.get("MAPPING_MODEL")
        or config.get("AIT_MODEL")
        or config.get("DEEPSEEK_MODEL")
        or config.get("OPENAI_MODEL")
        or MODEL_NAME
    )
    for prefix in ("AIT", "OPENAI", "DEEPSEEK", "CRITIC"):
        key = str(config.get(f"{prefix}_API_KEY", "")).strip()
        if not _usable_key(key):
            continue
        base = _normalize_base_url(config.get(f"{prefix}_BASE_URL") or default_base)
        model = (
            config.get(f"{prefix}_MODEL")
            or config.get("MAPPING_MODEL")
            or default_model
        )
        return key, base, model
    return "", default_base, default_model

def normalize_text(text):
    """Normalize text for comparison."""
    if not isinstance(text, str):
        return ""
    # Remove punctuation and whitespace
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip().lower()

def extract_keywords(text):
    if not isinstance(text, str):
        return []
    tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+", text)
    stop = {"根据", "以下", "关于", "正确", "错误", "属于", "下列", "不属于", "可以", "应该", "不得", "是否", "哪项", "哪个", "哪些", "哪些项"}
    return [t for t in tokens if t and t not in stop]

def keyword_coverage(keywords, text):
    if not keywords:
        return 0.0, []
    hits = [k for k in keywords if k in text]
    return len(hits) / max(len(keywords), 1), hits

def llm_rerank(question_row, candidate_indices, kb_data, api_key, base_url, model_name=MODEL_NAME):
    """Use DeepSeek (OpenAI-compatible) to select the best matching KB entry from candidates."""
    try:
        from openai import OpenAI
    except Exception:
        return None

    q_point = str(question_row.get('考点', '')).strip()
    q_stem = str(question_row.get('题干', '')).strip()

    lines = []
    for i, kb_idx in enumerate(candidate_indices, 1):
        kb_entry = kb_data[kb_idx]
        snippet = str(kb_entry.get('核心内容', '')).replace('\n', ' ').strip()
        if len(snippet) > 160:
            snippet = snippet[:160] + "..."
        lines.append(f"{i}. {kb_entry.get('完整路径','')} | {snippet}")

    prompt = (
        "你是教材知识点匹配助手。给定题干与考点，从候选知识点中选出最相关的一项。\n"
        "如果没有任何候选明显匹配，请返回 0。\n"
        "请返回 JSON：{\"choice\": 1, \"evidence_keywords\": []}。\n"
        f"choice 为 0-{len(candidate_indices)}。\n\n"
        f"考点: {q_point}\n"
        f"题干: {q_stem}\n\n"
        "候选:\n" + "\n".join(lines)
    )

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200
        )
        content = resp.choices[0].message.content if resp.choices else ""
        if not content:
            return None
        try:
            data = json.loads(content)
            idx = int(data.get("choice", -1))
            if idx == 0:
                return None
            return idx, data
        except Exception:
            m = re.search(r'\d+', content)
            if not m:
                return None
            idx = int(m.group(0))
            if idx == 0:
                return None
            return idx, {"choice": idx, "evidence_keywords": []}
    except Exception:
        return None

def load_data():
    """Load knowledge base and historical questions."""
    print("Loading knowledge base...")
    kb_data = []
    with open(KB_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            item = json.loads(line)
            if '核心内容' not in item and '结构化内容' in item:
                struct = item['结构化内容']
                parts = []
                if struct.get('context_before'):
                    parts.append(struct['context_before'])
                if struct.get('context_after'):
                    parts.append(struct['context_after'])
                if struct.get('tables'):
                    parts.extend([str(t) for t in struct['tables']])
                if struct.get('formulas'):
                    parts.extend(struct['formulas'])
                if struct.get('examples'):
                    parts.extend(struct['examples'])
                item['核心内容'] = "\n".join(parts)
            kb_data.append(item)
    
    print(f"Loaded {len(kb_data)} KB entries")
    
    print("Loading historical questions...")
    history_df = pd.read_excel(HISTORY_PATH)
    print(f"Loaded {len(history_df)} historical questions")
    
    return kb_data, history_df

def find_best_match(question_row, kb_data, vectorizer, kb_tfidf_matrix):
    """Find the best matching KB entry for a question."""
    question_kp = str(question_row.get('考点', '')).strip()
    question_stem = str(question_row.get('题干', '')).strip()
    keywords = extract_keywords(f"{question_kp} {question_stem}")
    q_pian = str(question_row.get('篇', '')).strip()
    q_zhang = str(question_row.get('章', '')).strip()
    q_jie = str(question_row.get('节', '')).strip()

    # Strategy 1: Match by 篇/章/节, then prefer 考点 in path
    scoped_indices = []
    for idx, kb_entry in enumerate(kb_data):
        kb_path = kb_entry['完整路径']
        if q_pian and q_pian not in kb_path:
            continue
        if q_zhang and q_zhang not in kb_path:
            continue
        if q_jie and q_jie not in kb_path:
            continue
        scoped_indices.append(idx)

    if scoped_indices:
        for idx in scoped_indices:
            kb_path = kb_data[idx]['完整路径']
            if question_kp and question_kp in kb_path:
                return idx, kb_path, 1.0, "exact_path_match_scoped"

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
    top_k_idx = similarities.argsort()[::-1][:TOP_K_RETRIEVAL].tolist()
    candidate_pool = list(dict.fromkeys((scoped_indices or []) + top_k_idx))
    best_idx = max(candidate_pool, key=lambda i: similarities[i])
    best_score = similarities[best_idx]
    method = "tfidf_similarity_scoped" if scoped_indices else "tfidf_similarity"
    best_text = f"{kb_data[best_idx].get('完整路径','')} {kb_data[best_idx].get('核心内容','')}"
    coverage, hits = keyword_coverage(keywords, best_text)
    return best_idx, kb_data[best_idx]['完整路径'], float(best_score), method, similarities, candidate_pool, coverage, hits, keywords

def create_mapping():
    """Create the question-to-knowledge mapping."""
    kb_data, history_df = load_data()
    config = load_config()
    api_key, base_url, model_name = resolve_llm_config(config)
    
    # Build TF-IDF index for KB
    print("Building TF-IDF index...")
    kb_corpus = [f"{entry['完整路径']} {entry['核心内容']}" for entry in kb_data]
    vectorizer = TfidfVectorizer()
    kb_tfidf_matrix = vectorizer.fit_transform(kb_corpus)
    
    print(f"Using LLM rerank: base_url={base_url}, model={model_name}")
    # Precompute matches for threshold calibration
    print("Mapping questions to knowledge points...")
    mapping = {}
    precomputed = []

    for idx, row in history_df.iterrows():
        kb_idx, kb_path, confidence, method, similarities, candidate_pool, coverage, hit_keywords, keywords = find_best_match(
            row, kb_data, vectorizer, kb_tfidf_matrix
        )
        combined_score = (AUTO_PASS_WEIGHT_TFIDF * confidence) + (AUTO_PASS_WEIGHT_COVERAGE * coverage)
        precomputed.append({
            "idx": idx,
            "row": row,
            "kb_idx": kb_idx,
            "kb_path": kb_path,
            "confidence": confidence,
            "method": method,
            "similarities": similarities,
            "candidate_pool": candidate_pool,
            "coverage": coverage,
            "hit_keywords": hit_keywords,
            "keywords": keywords,
            "combined_score": combined_score
        })

        if (idx + 1) % 100 == 0:
            print(f"  Processed {idx + 1}/{len(history_df)} questions")

    # Calibrate auto-pass threshold to target ratio
    scores = sorted(p["combined_score"] for p in precomputed)
    if scores:
        cutoff_index = int(len(scores) * (1 - AUTO_PASS_TARGET))
        cutoff_index = max(0, min(len(scores) - 1, cutoff_index))
        score_threshold = scores[cutoff_index]
    else:
        score_threshold = 1.0

    print(
        "Auto-pass calibration: "
        f"target={AUTO_PASS_TARGET:.0%}, score_threshold={score_threshold:.3f}, "
        f"min_tfidf={MIN_AUTO_PASS_TFIDF}, min_coverage={MIN_AUTO_PASS_COVERAGE}"
    )

    # Final mapping with auto-pass + rerank
    for item in precomputed:
        idx = item["idx"]
        row = item["row"]
        kb_idx = item["kb_idx"]
        kb_path = item["kb_path"]
        confidence = item["confidence"]
        method = item["method"]
        similarities = item["similarities"]
        candidate_pool = item["candidate_pool"]
        coverage = item["coverage"]
        hit_keywords = item["hit_keywords"]
        combined_score = item["combined_score"]

        evidence = {
            "hit_keywords": hit_keywords,
            "keyword_coverage": round(coverage, 3),
        }

        auto_pass_ok = (
            combined_score >= score_threshold
            and confidence >= MIN_AUTO_PASS_TFIDF
            and coverage >= MIN_AUTO_PASS_COVERAGE
        )
        if auto_pass_ok:
            method = "auto_pass"
        elif api_key and confidence < CONF_THRESHOLD and candidate_pool:
            ranked_pool = sorted(candidate_pool, key=lambda i: similarities[i], reverse=True)[:TOP_N_CANDIDATES]
            llm_out = llm_rerank(row, ranked_pool, kb_data, api_key, base_url, model_name=model_name)
            if llm_out is not None:
                choice, meta = llm_out
                if choice is not None and choice > 0:
                    kb_idx = int(ranked_pool[choice - 1])
                    kb_path = kb_data[kb_idx]['完整路径']
                    method = "llm_rerank"
                    confidence = float(similarities[kb_idx])
                    if isinstance(meta, dict):
                        llm_keywords = meta.get("evidence_keywords", [])
                        if isinstance(llm_keywords, list):
                            evidence["llm_keywords"] = llm_keywords
            else:
                if confidence < CONF_THRESHOLD:
                    kb_idx = -1
                    kb_path = ""
                    method = "unmapped"

        mapping[idx] = {
            "题干": str(row.get('题干', ''))[:100] + "...",  # Truncate for readability
            "考点": str(row.get('考点', '')),
            "matched_kb_path": kb_path,
            "matched_kb_index": int(kb_idx),  # Convert to Python int
            "confidence": confidence,
            "method": method,
            "evidence": evidence
        }
    
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
