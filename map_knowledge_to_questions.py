#!/usr/bin/env python3
"""
Map knowledge slices to historical questions (reverse mapping).
Creates a JSON file that links each knowledge slice to its relevant questions.
Target: 80% auto-mapping rate.
"""
import json
import os
import re
import sys
import argparse
import numpy as np
import pandas as pd
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from tenants_config import resolve_tenant_kb_path, resolve_tenant_history_path, tenant_mapping_path

# BGE embedding model - required, no fallback
try:
    from sentence_transformers import SentenceTransformer
    BGE_AVAILABLE = True
except ImportError:
    BGE_AVAILABLE = False
    print("ERROR: sentence-transformers is required but not installed!")
    print("Please install it with: pip install sentence-transformers")
    sys.exit(1)

# Paths
KB_PATH = "bot_knowledge_base.jsonl"
HISTORY_PATH = "存量房买卖母卷ABCD.xls"
MAPPING_PATH = "question_knowledge_mapping.json"
OUTPUT_PATH = "knowledge_question_mapping.json"

# BGE semantic matching thresholds (per PRD FR2.1.1)
TOP_K_RETRIEVAL = 20  # Number of candidates for BGE fallback retrieval
BGE_AUTO_PASS_THRESHOLD = 0.8  # Score > 0.8: auto pass
BGE_LLM_REVIEW_THRESHOLD = 0.5  # 0.5 < Score <= 0.8: LLM review
BGE_MODEL_NAME = "BAAI/bge-small-zh-v1.5"  # BGE model name

# LLM Rerank debug output (PRD FR2.1.1, TP12.13: fail loud, expose raw)
LLM_RERANK_DEBUG_DIR = os.path.dirname(os.path.abspath(__file__))


class LLMRerankParseError(Exception):
    """Raised when LLM Rerank JSON parse fails. Raw response is written to debug file."""

    def __init__(self, message: str, debug_path: str):
        super().__init__(message)
        self.debug_path = debug_path


def _parse_llm_rerank_json(extracted: str, raw_content: str, debug_dir: Optional[str] = None) -> dict:
    """
    Parse LLM rerank JSON with robust error handling.
    On failure: write debug file and raise LLMRerankParseError (TP12.13).
    """
    try:
        return json.loads(extracted)
    except json.JSONDecodeError:
        d = debug_dir if debug_dir is not None else LLM_RERANK_DEBUG_DIR
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(d, f"llm_rerank_debug_{ts}.txt")
        try:
            os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Raw LLM response (pre-extraction)\n")
                f.write(raw_content)
                f.write("\n\n# Extracted JSON (failed to parse)\n")
                f.write(extracted)
        except Exception:
            pass
        raise LLMRerankParseError("LLM rerank JSON parse failed", path)


# Synonym mapping dictionary (per PRD FR2.1.1)
SYNONYM_MAP = {
    '个税': '个人所得税',
    '个人所得税': '个税',
    '贝壳': '贝壳找房',
    '贝壳找房': '贝壳',
    'BEIKE': '贝壳',
    '贝壳': 'BEIKE',
}

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
        or "deepseek-chat"
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

def normalize_path_dehydration(text):
    """
    Normalize path with "dehydration" according to PRD FR2.1.1.
    - Remove keywords: "第X篇", "第X章", "第X节", "（了解/掌握/熟悉）", "-无需修改"
    - Normalize symbols: convert all punctuation/spaces/slashes to standard separator '/'
    - Apply synonym mapping
    """
    if not isinstance(text, str):
        return ""
    
    # Step 1: Remove descriptive keywords
    # Remove "第X篇"、"第X章"、"第X节"
    text = re.sub(r'第[一二三四五六七八九十\d]+[篇章节]', '', text)
    # Remove "（了解/掌握/熟悉）" and similar patterns
    text = re.sub(r'[（(]了解[）)]', '', text)
    text = re.sub(r'[（(]掌握[）)]', '', text)
    text = re.sub(r'[（(]熟悉[）)]', '', text)
    text = re.sub(r'[（(].*?了解.*?[）)]', '', text)
    text = re.sub(r'[（(].*?掌握.*?[）)]', '', text)
    text = re.sub(r'[（(].*?熟悉.*?[）)]', '', text)
    # Remove "-无需修改"
    text = re.sub(r'-无需修改', '', text)
    text = re.sub(r'无需修改', '', text)
    
    # Step 2: Normalize symbols - convert to standard separator '/'
    # Replace various separators with '/'
    text = re.sub(r'[>\s]+', '/', text)  # Replace '>' and spaces with '/'
    text = re.sub(r'[、，。：；？！（）().,:;?!]', '/', text)  # Replace punctuation with '/'
    text = re.sub(r'/+', '/', text)  # Normalize multiple '/' to single '/'
    text = text.strip('/')  # Remove leading/trailing '/'
    
    # Step 3: Apply synonym mapping
    for synonym, standard in SYNONYM_MAP.items():
        text = text.replace(synonym, standard)
    
    return text


def strip_title_prefix(s: str) -> str:
    """
    Remove structural prefixes from slice title before strategy ops (PRD FR2.1.1, TDD TP12.6c).
    Strips leading: （一）（二）..., 一、二、..., 1、2、...
    """
    if not isinstance(s, str):
        return ""
    s = s.strip()
    # （一）（二）...
    s = re.sub(r"^[（(][一二三四五六七八九十\d]+[）)]", "", s)
    s = s.strip()
    # 一、二、... 十、十一、...
    s = re.sub(r"^[一二三四五六七八九十百千\d]+、", "", s)
    s = s.strip()
    # 1、2、...
    s = re.sub(r"^\d+、", "", s)
    return s.strip()


def normalize_text(text):
    """Legacy normalize function for backward compatibility."""
    if not isinstance(text, str):
        return ""
    # Remove punctuation and whitespace
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip().lower()

def extract_keywords(text):
    """Extract keywords from text using jieba."""
    try:
        import jieba
    except ImportError:
        print("ERROR: jieba is required but not installed!")
        print("Please install it with: pip install jieba")
        sys.exit(1)
    
    if not isinstance(text, str):
        return []
    # Use jieba for better Chinese word segmentation
    words = jieba.cut(text, cut_all=False)
    stop = {"根据", "以下", "关于", "正确", "错误", "属于", "下列", "不属于", "可以", "应该", "不得", 
            "是否", "哪项", "哪个", "哪些", "哪些项", "的", "和", "与", "或", "是", "有", "在", "为"}
    keywords = [w.strip() for w in words if w.strip() and w.strip() not in stop and len(w.strip()) > 1]
    return keywords

def extract_legal_references(text):
    """Extract legal references like 《民法典》第XX条 from text."""
    if not isinstance(text, str):
        return []
    # Pattern: 《法律名称》第XX条 or 《法律名称》第XX条、第YY条
    pattern = r'《([^》]+)》第(\d+)条'
    matches = re.findall(pattern, text)
    return matches  # Returns list of (law_name, article_number) tuples

def segment_text(text):
    """Segment text using jieba for keyword extraction."""
    try:
        import jieba
    except ImportError:
        print("ERROR: jieba is required but not installed!")
        sys.exit(1)
    
    if not isinstance(text, str):
        return []
    words = jieba.cut(text, cut_all=False)
    return [w.strip() for w in words if w.strip() and len(w.strip()) > 1]

def get_kb_content(kb_entry):
    """Extract full content from knowledge slice."""
    parts = []
    
    # Add path
    parts.append(kb_entry.get('完整路径', ''))
    
    # Add structured content
    if '结构化内容' in kb_entry:
        struct = kb_entry['结构化内容']
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
    
    # Add core content if exists
    if '核心内容' in kb_entry:
        parts.append(kb_entry['核心内容'])
    
    return "\n".join(parts)

def get_kb_content_for_embedding(kb_entry):
    """Get knowledge slice content for BGE embedding: 完整切片内容（含路径与结构化文本）。"""
    full_text = get_kb_content(kb_entry)
    path = kb_entry.get('完整路径', '')
    # Keep path at the front as a strong anchor, and include full slice text for completeness.
    return f"{path} {full_text}".strip()

def get_question_content_for_embedding(row):
    """Get question content for BGE embedding: 标准化路径 + 题干 + 选项 + 解析."""
    gps = build_gps_path(row)
    stem = str(row.get('题干', '')).strip()
    options = []
    for i in range(1, 9):
        opt = str(row.get(f'选项{i}', '')).strip()
        if opt and pd.notna(row.get(f'选项{i}', '')):
            options.append(opt)
    options_text = " ".join(options)
    explanation = str(row.get('解析', '')).strip()
    return f"{gps} {stem} {options_text} {explanation}".strip()

# Global BGE model instance
_bge_model = None

def get_bge_model():
    """Get or initialize BGE model from a fixed local cache directory.

    This version avoids any online download at runtime by always pointing
    SentenceTransformer to ./models/bge-small-zh-v1.5 under the project root.
    """
    global _bge_model
    if _bge_model is None:
        if not BGE_AVAILABLE:
            raise RuntimeError(
                "BGE embedding model is required but sentence-transformers is not installed!"
            )
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__)) or "."
            local_cache = os.path.join(base_dir, "models", "bge-small-zh-v1.5")
            print(f"Loading BGE model from local cache: {local_cache} ...")
            _bge_model = SentenceTransformer(
                BGE_MODEL_NAME,
                cache_folder=local_cache,
            )
            print("BGE model loaded successfully")
        except Exception as e:
            raise RuntimeError(
                "Failed to load BGE model from local cache. "
                "Please ensure you have pre-downloaded 'BAAI/bge-small-zh-v1.5' "
                "into ./models/bge-small-zh-v1.5. "
                f"Original error: {e}"
            )
    return _bge_model

def compute_bge_similarity(text1, text2):
    """Compute BGE embedding similarity between two texts."""
    model = get_bge_model()
    try:
        embeddings = model.encode([text1, text2], normalize_embeddings=True)
        emb1, emb2 = embeddings[0], embeddings[1]
        return float(np.dot(emb1, emb2))
    except Exception as e:
        raise RuntimeError(f"BGE encoding failed: {e}")


def encode_batch(texts, batch_size=64):
    """Encode a list of texts with BGE; returns (N, dim) float array, normalized."""
    model = get_bge_model()
    embs = model.encode(texts, batch_size=batch_size, normalize_embeddings=True)
    return np.asarray(embs, dtype=np.float32)

def llm_semantic_match(kb_entry, questions_df, api_key, base_url, model_name="deepseek-chat", top_k=20):
    """Use LLM to find semantically related questions from all questions."""
    try:
        from openai import OpenAI
    except Exception:
        return None

    kb_path = kb_entry.get('完整路径', '')
    kb_content = get_kb_content(kb_entry)
    
    # Extract key concepts from knowledge slice
    keywords = extract_keywords(f"{kb_path} {kb_content}")
    key_concepts = ", ".join(keywords[:10]) if keywords else "无"
    
    # Get all questions (or a sample if too many)
    all_questions = []
    for idx, row in questions_df.iterrows():
        q_stem = str(row.get('题干', '')).strip()
        q_point = str(row.get('考点', '')).strip()
        if q_stem and len(q_stem) > 5:  # Filter empty questions
            all_questions.append({
                'index': int(idx),
                'kaodian': q_point,
                'stem': q_stem[:150] + "..." if len(q_stem) > 150 else q_stem
            })
    
    # If too many, use BGE embedding to get top candidates first
    if len(all_questions) > 50:
        kb_embedding_text = get_kb_content_for_embedding(kb_entry)
        # Compute BGE similarity for all questions
        question_scores = []
        for q in all_questions:
            q_embedding_text = f"{q['kaodian']} {q['stem']}"
            try:
                bge_score = compute_bge_similarity(kb_embedding_text, q_embedding_text)
                question_scores.append((q, bge_score))
            except Exception as e:
                print(f"    [BGE] Error computing similarity: {e}", flush=True)
                continue
        
        # Sort by BGE score and take top_k
        question_scores.sort(key=lambda x: x[1], reverse=True)
        candidate_questions = [q for q, score in question_scores[:top_k]]
    else:
        candidate_questions = all_questions
    
    if not candidate_questions:
        return None
    
    # Format for LLM
    lines = []
    for i, q in enumerate(candidate_questions, 1):
        lines.append(f"{i}. [题目{i}] 考点: {q['kaodian']}\n   题干: {q['stem']}")
    
    prompt = (
        "你是知识点与题目语义匹配专家。请分析知识点内容，从候选题目中找出语义相关的题目。\n"
        "即使表述方式不同，只要语义相关、知识点相关，就应该匹配。\n"
        "请返回 JSON：{relevant_question_indices: [1, 3, 5], key_concepts_matched: [\"关键词1\", \"关键词2\"]}。\n"
        "relevant_question_indices 为候选编号列表（从1开始），可以是空列表。\n\n"
        f"知识点路径: {kb_path}\n"
        f"知识点内容: {kb_content[:800]}{'...' if len(kb_content) > 800 else ''}\n"
        f"提取的关键概念: {key_concepts}\n\n"
        "候选题目:\n" + "\n".join(lines)
    )

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,  # Slightly higher for semantic understanding
            max_tokens=500,
            timeout=30  # Add timeout
        )
        content = resp.choices[0].message.content if resp.choices and resp.choices[0].message else ""
        if not content:
            print(f"    [LLM] Warning: Empty response (finish_reason: {resp.choices[0].finish_reason if resp.choices else 'N/A'})", flush=True)
            return None
        
        # Extract JSON from markdown code blocks if present
        import re
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
        if json_match:
            content = json_match.group(1)
        else:
            # Try to find JSON object directly
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                content = json_match.group(0)
        
        try:
            data = json.loads(content)
            indices = data.get("relevant_question_indices", [])
            if not isinstance(indices, list):
                print(f"    [LLM] Warning: Invalid indices format: {indices}", flush=True)
                return None
            # Convert 1-based to 0-based, then to actual question indices
            result = []
            for idx in indices:
                if isinstance(idx, int) and 1 <= idx <= len(candidate_questions):
                    result.append(candidate_questions[idx - 1]['index'])
            if result:
                print(f"    [LLM] Success: Found {len(result)} matches", flush=True)
            else:
                # LLM returned empty list - no matches found (this is valid, not an error)
                print(f"    [LLM] Info: LLM found no matches (returned empty list)", flush=True)
            return result, "", data.get("key_concepts_matched", [])
        except json.JSONDecodeError as e:
            # Try to extract numbers from text if JSON parsing fails
            import re
            numbers = re.findall(r'\d+', content)
            if numbers:
                result = []
                for num_str in numbers[:5]:  # Limit to 5 matches
                    num = int(num_str)
                    if 1 <= num <= len(candidate_questions):
                        result.append(candidate_questions[num - 1]['index'])
                if result:
                    print(f"    [LLM] Success (extracted): Found {len(result)} matches", flush=True)
                    return result, "", []
            print(f"    [LLM] Warning: JSON parse error: {str(e)[:100]}", flush=True)
            return None
    except Exception as e:
        error_msg = str(e)
        # Check for common error types
        if "timeout" in error_msg.lower():
            print(f"    [LLM] Error: API timeout - {error_msg[:80]}", flush=True)
        elif "rate limit" in error_msg.lower():
            print(f"    [LLM] Error: Rate limit exceeded - {error_msg[:80]}", flush=True)
        elif "authentication" in error_msg.lower() or "401" in error_msg or "403" in error_msg:
            print(f"    [LLM] Error: Authentication failed - {error_msg[:80]}", flush=True)
        else:
            print(f"    [LLM] Error: {error_msg[:100]}", flush=True)
        return None

def llm_semantic_analysis(kb_entry, questions_df, api_key, base_url, model_name="deepseek-chat", top_k=30):
    """
    Use LLM to deeply analyze whether a knowledge slice can be associated with any questions.
    Returns: (can_associate: bool, matched_question_indices: list, candidate_questions: list)
    If can_associate is False, returns None for matched_question_indices and provides candidates.
    """
    try:
        from openai import OpenAI
    except Exception:
        return None

    kb_path = kb_entry.get('完整路径', '')
    kb_content = get_kb_content(kb_entry)
    
    # Get full knowledge slice information
    kb_mastery = kb_entry.get('掌握程度', '未知')
    kb_structured = kb_entry.get('结构化内容', {})
    kb_examples = kb_structured.get('examples', [])
    
    # Format knowledge slice content for LLM
    kb_info = f"""知识点路径: {kb_path}
掌握程度: {kb_mastery}
核心内容: {kb_content[:1000]}{'...' if len(kb_content) > 1000 else ''}"""
    
    if kb_examples:
        examples_text = "\n".join([f"  - {ex}" for ex in kb_examples[:3]])
        kb_info += f"\n教材原题示例:\n{examples_text}"
    
    # Get candidate questions using BGE embedding
    all_questions = []
    for idx, row in questions_df.iterrows():
        q_stem = str(row.get('题干', '')).strip()
        q_point = str(row.get('考点', '')).strip()
        q_options = [str(row.get(f'选项{i}', '')).strip() for i in range(1, 5) if pd.notna(row.get(f'选项{i}', ''))]
        q_answer = str(row.get('正确答案', '')).strip()
        q_explanation = str(row.get('解析', '')).strip()
        
        if q_stem and len(q_stem) > 5:
            all_questions.append({
                'index': int(idx),
                'kaodian': q_point,
                'stem': q_stem,
                'options': q_options,
                'answer': q_answer,
                'explanation': q_explanation[:200] if q_explanation else ''
            })
    
    if not all_questions:
        return None
    
    # Use BGE to get top candidates
    if len(all_questions) > top_k:
        kb_embedding_text = get_kb_content_for_embedding(kb_entry)
        question_scores = []
        for q in all_questions:
            q_embedding_text = get_question_content_for_embedding(questions_df.iloc[q['index']])
            try:
                bge_score = compute_bge_similarity(kb_embedding_text, q_embedding_text)
                question_scores.append((q, bge_score))
            except Exception as e:
                continue
        
        question_scores.sort(key=lambda x: x[1], reverse=True)
        candidate_questions = [q for q, score in question_scores[:top_k]]
    else:
        candidate_questions = all_questions
    
    # Format candidate questions for LLM
    candidate_lines = []
    for i, q in enumerate(candidate_questions, 1):
        options_text = "\n    ".join([f"{chr(64+j)}. {opt}" for j, opt in enumerate(q['options'], 1)])
        candidate_lines.append(
            f"{i}. [题目{i}] 考点: {q['kaodian']}\n"
            f"   题干: {q['stem']}\n"
            f"   选项: {options_text}\n"
            f"   答案: {q['answer']}\n"
            f"   解析: {q['explanation']}"
        )
    
    prompt = (
        "你是一位房产交易专家。请判断给定的知识切片是否能为候选题目提供逻辑支撑。\n\n"
        "任务：分析知识切片内容与母题内容之间的关联性，判断是否存在实质性关联。\n\n"
        "关联性判断标准：\n"
        "1. 知识点内容是否能够支撑该题目的出题逻辑\n"
        "2. 即使表述方式不同，只要语义相关、知识点相关，就应该判断为可关联\n"
        "3. 特别关注隐含关联：例如题目考的是'未成年人售房'，知识切片讲的是'监护人公证'，字面上可能一个重复词都没有，但逻辑上必须关联\n"
        "4. 题目的考点是否与知识点内容相关\n"
        "5. 题目的解析是否引用了知识点中的规则或概念\n\n"
        "输出要求：仅输出 JSON 格式，包含以下字段：\n"
        "{\n"
        '  "is_related": true/false,  // 是否可以关联（布尔值）\n'
        '  "matched_question_indices": [1, 3, 5]  // 可关联的题目编号列表（从1开始），如果is_related为false则为空列表\n'
        "}\n\n"
        f"知识切片内容：\n{kb_info}\n\n"
        f"候选母题（共{len(candidate_questions)}道）:\n" + "\n".join(candidate_lines)
    )
    
    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=800,
            timeout=60
        )
        content = resp.choices[0].message.content if resp.choices and resp.choices[0].message else ""
        if not content:
            print(f"    [LLM深度分析] Warning: Empty response", flush=True)
            return None
        
        # Extract JSON from markdown code blocks if present
        import re
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
        if json_match:
            content = json_match.group(1)
        else:
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                content = json_match.group(0)
        
        try:
            data = json.loads(content)
            # Support both old format (can_associate) and new format (is_related)
            can_associate = data.get("is_related", data.get("can_associate", False))
            matched_indices = data.get("matched_question_indices", [])
            analysis = data.get("analysis", "")
            
            # Convert 1-based to 0-based, then to actual question indices
            matched_question_indices = []
            if can_associate and matched_indices:
                for idx in matched_indices:
                    if isinstance(idx, int) and 1 <= idx <= len(candidate_questions):
                        matched_question_indices.append(candidate_questions[idx - 1]['index'])
            
            # Prepare candidate questions info for report
            candidate_info = []
            for q in candidate_questions:
                candidate_info.append({
                    'question_index': q['index'],
                    'kaodian': q['kaodian'],
                    'stem': q['stem'][:200]
                })
            
            if can_associate and matched_question_indices:
                print(f"    [LLM深度分析] 可关联: 找到{len(matched_question_indices)}道相关题目", flush=True)
                return {
                    'can_associate': True,
                    'is_related': True,
                    'matched_question_indices': matched_question_indices,
                    'analysis': analysis,
                    'confidence': 0.6  # LLM深度分析的置信度
                }
            else:
                print("    [LLM深度分析] 不可关联: 无匹配题目", flush=True)
                return {
                    'can_associate': False,
                    'is_related': False,
                    'analysis': analysis,
                    'candidate_questions': candidate_info
                }
        except json.JSONDecodeError as e:
            print(f"    [LLM深度分析] Warning: JSON parse error: {str(e)[:100]}", flush=True)
            return None
    except Exception as e:
        error_msg = str(e)
        if "timeout" in error_msg.lower():
            print(f"    [LLM深度分析] Error: API timeout", flush=True)
        elif "rate limit" in error_msg.lower():
            print(f"    [LLM深度分析] Error: Rate limit exceeded", flush=True)
        else:
            print(f"    [LLM深度分析] Error: {error_msg[:100]}", flush=True)
        return None

def llm_rerank_questions(kb_entry, candidate_indices, questions_df, api_key, base_url, model_name="deepseek-chat"):
    """Use LLM to select the best matching questions from candidates."""
    try:
        from openai import OpenAI
    except Exception:
        return None

    kb_path = kb_entry.get('完整路径', '')
    kb_content = get_kb_content(kb_entry)
    if len(kb_content) > 500:
        kb_content = kb_content[:500] + "..."

    lines = []
    for i, q_idx in enumerate(candidate_indices, 1):
        row = questions_df.iloc[q_idx]
        q_stem = str(row.get('题干', '')).strip()
        q_point = str(row.get('考点', '')).strip()
        if len(q_stem) > 100:
            q_stem = q_stem[:100] + "..."
        lines.append(f"{i}. 考点: {q_point}\n   题干: {q_stem}")

    prompt = (
        "你是知识点与题目匹配助手。给定一个知识点切片，从候选题目中选出相关的题目。\n"
        "即使表述方式不同，只要语义相关就应该匹配。\n"
        "如果没有任何候选明显相关，请返回空列表 []。\n"
        "请返回 JSON：{relevant_question_indices: [1, 3, 5]}。\n"
        "relevant_question_indices 为候选编号列表（从1开始），可以是空列表。\n\n"
        f"知识点路径: {kb_path}\n"
        f"知识点内容: {kb_content}\n\n"
        "候选题目:\n" + "\n".join(lines)
    )
    
    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content if resp.choices else ""
        if not content:
            return None
        try:
            data = json.loads(content)
            indices = data.get("relevant_question_indices", [])
            if not isinstance(indices, list):
                return None
            # Convert 1-based to 0-based, then to actual question indices
            result = []
            for idx in indices:
                if isinstance(idx, int) and 1 <= idx <= len(candidate_indices):
                    result.append(candidate_indices[idx - 1])
            return result, ""
        except Exception:
            return None
    except Exception:
        return None

def build_question_indices(questions_df):
    """Build indices for fast lookup."""
    # Index by 篇/章/节
    path_index = {}  # (pian, zhang, jie) -> [question_indices]
    # Index by 考点
    kaodian_index = {}  # kaodian_clean -> [question_indices]
    
    for idx, row in questions_df.iterrows():
        # Path index
        q_pian = str(row.get('篇', '')).strip()
        q_zhang = str(row.get('章', '')).strip()
        q_jie = str(row.get('节', '')).strip()
        if q_pian or q_zhang or q_jie:
            key = (q_pian, q_zhang, q_jie)
            path_index.setdefault(key, []).append(idx)
        
        # Kaodian index
        q_point = str(row.get('考点', '')).strip()
        if q_point:
            q_point_clean = re.sub(r'[（(].*?[）)]', '', q_point).strip()
            if q_point_clean:
                kaodian_index.setdefault(q_point_clean, []).append(idx)
    
    return path_index, kaodian_index

def load_data():
    """Load knowledge base, historical questions, and existing mapping."""
    print("Loading knowledge base...")
    kb_data = []
    with open(KB_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            item = json.loads(line)
            kb_data.append(item)
    print(f"Loaded {len(kb_data)} KB entries")
    
    print("Loading historical questions...")
    questions_df = pd.read_excel(HISTORY_PATH)
    print(f"Loaded {len(questions_df)} historical questions")
    
    print("Building question indices...")
    path_index, kaodian_index = build_question_indices(questions_df)
    print(f"Built path index: {len(path_index)} entries")
    print(f"Built kaodian index: {len(kaodian_index)} entries")
    
    print("Loading existing question-to-knowledge mapping...")
    reverse_index = {}  # kb_index -> [question_indices]
    if os.path.exists(MAPPING_PATH):
        with open(MAPPING_PATH, 'r', encoding='utf-8') as f:
            mapping = json.load(f)
            for q_idx_str, mapping_info in mapping.items():
                kb_idx = mapping_info.get('matched_kb_index', -1)
                if kb_idx >= 0:
                    reverse_index.setdefault(kb_idx, []).append(int(q_idx_str))
        print(f"Built reverse index: {len(reverse_index)} KB entries have mapped questions")
    else:
        print("Warning: question_knowledge_mapping.json not found, skipping reverse index")
    
    return kb_data, questions_df, reverse_index, path_index, kaodian_index

def build_gps_path(row):
    """
    Build GPS path coordinate from question: 标准化(篇/章/节/考点)
    Returns normalized path string like: "交易服务/不动产交易税费/个人所得税/个税计算"
    """
    q_pian = str(row.get('篇', '')).strip()
    q_zhang = str(row.get('章', '')).strip()
    q_jie = str(row.get('节', '')).strip()
    q_kaodian = str(row.get('考点', '')).strip()
    
    # Clean kaodian
    q_kaodian_clean = re.sub(r'[（(].*?[）)]', '', q_kaodian).strip()
    q_kaodian_clean = re.sub(r'-无需修改', '', q_kaodian_clean).strip()
    
    # Build GPS path components
    components = []
    if q_pian:
        components.append(normalize_path_dehydration(q_pian))
    if q_zhang:
        components.append(normalize_path_dehydration(q_zhang))
    if q_jie:
        components.append(normalize_path_dehydration(q_jie))
    if q_kaodian_clean:
        components.append(normalize_path_dehydration(q_kaodian_clean))
    
    return '/'.join([c for c in components if c])


def _normalize_for_compare(text: str) -> str:
    if not isinstance(text, str):
        return ""
    s = normalize_path_dehydration(text)
    return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff/]", "", s).lower()


def detect_question_meta_conflict(q_row):
    """
    Detect mismatch between question path fields and question text content.
    Returns dict:
      {
        "meta_conflict": bool,
        "matched_count": int,
        "total_count": int,
        "matched_tokens": [...],
        "missing_tokens": [...],
        "detail": str,
      }
    """
    q_pian = str(q_row.get("篇", "")).strip()
    q_zhang = str(q_row.get("章", "")).strip()
    q_jie = str(q_row.get("节", "")).strip()
    q_kaodian = str(q_row.get("考点", "")).strip()
    q_kaodian_clean = re.sub(r"[（(].*?[）)]", "", q_kaodian).strip()
    q_kaodian_clean = re.sub(r"-无需修改", "", q_kaodian_clean).strip()

    stem = str(q_row.get("题干", "")).strip()
    explanation = str(q_row.get("解析", "")).strip()
    text_norm = _normalize_for_compare(f"{stem} {explanation}")

    raw_tokens = [q_pian, q_zhang, q_jie, q_kaodian_clean]
    tokens = []
    for t in raw_tokens:
        tn = _normalize_for_compare(t)
        if tn:
            tokens.append(tn)

    if not tokens:
        return {
            "meta_conflict": False,
            "matched_count": 0,
            "total_count": 0,
            "matched_tokens": [],
            "missing_tokens": [],
            "detail": "",
        }

    matched = [t for t in tokens if t and t in text_norm]
    missing = [t for t in tokens if t and t not in text_norm]
    total = len(tokens)
    matched_count = len(matched)

    # Conservative rules:
    # - 3+ tokens: less than 2 matches means conflict.
    # - 2 tokens: 0 match means conflict.
    # - 1 token: do not mark conflict by this rule.
    conflict = False
    if total >= 3 and matched_count < 2:
        conflict = True
    elif total == 2 and matched_count == 0:
        conflict = True

    detail = ""
    if conflict:
        detail = f"路径字段与题干/解析可能不一致：matched={matched_count}/{total}"

    return {
        "meta_conflict": conflict,
        "matched_count": matched_count,
        "total_count": total,
        "matched_tokens": matched,
        "missing_tokens": missing,
        "detail": detail,
    }

def llm_rerank_candidates(kb_entry, candidate_questions, api_key, base_url, model_name="deepseek-chat"):
    """
    LLM Reranking according to PRD FR2.1.1 Strategy 5.
    Returns tuple: (list of question indices that are related, empty reason string).
    On JSON parse failure: writes debug file and raises LLMRerankParseError (no silent ignore).
    """
    from openai import OpenAI

    if not candidate_questions or not api_key:
        return [], ""
    
    kb_path = kb_entry.get('完整路径', '')
    kb_content = get_kb_content(kb_entry)
    kb_gps = normalize_path_dehydration(kb_path)
    
    # Format candidate questions for LLM
    candidate_texts = []
    for q_idx, q_data in enumerate(candidate_questions[:5], 1):  # Top 3-5 candidates
        row = q_data['row']
        q_gps = q_data.get('gps_path', '')
        q_stem = str(row.get('题干', '')).strip()
        candidate_texts.append(f"{q_idx}. GPS路径: {q_gps}\n   题干: {q_stem[:200]}")
    
    prompt = (
        f"你是一个房产交易专家。\n"
        f"这道题考的是【{kb_gps}】，知识点路径是【{kb_path}】。\n"
        f"知识点内容: {kb_content[:500]}{'...' if len(kb_content) > 500 else ''}\n\n"
        f"请从以下 {len(candidate_texts)} 个知识切片中选出能支撑解题的项。如果不相关，请输出 False。\n\n"
        f"候选题目:\n" + "\n".join(candidate_texts) + "\n\n"
        f"请返回**合法JSON**，且仅此一段，格式：{{\"is_related\": true或false, \"related_indices\": [1,3]}}。\n"
        f"related_indices 为候选编号（从1开始），可空列表。"
    )

    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=500,
        timeout=30,
        response_format={"type": "json_object"},
    )
    raw_content = resp.choices[0].message.content if resp.choices and resp.choices[0].message else ""
    if not raw_content:
        return [], ""

    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_content, re.DOTALL)
    if json_match:
        extracted = json_match.group(1)
    else:
        m = re.search(r"\{.*\}", raw_content, re.DOTALL)
        extracted = m.group(0) if m else raw_content

    try:
        data = _parse_llm_rerank_json(extracted, raw_content)
    except LLMRerankParseError as exc:
        print(f"    [LLM Rerank] JSON parse failed, skip. Debug: {exc.debug_path}", flush=True)
        return [], f"parse_error:{exc.debug_path}"
    is_related = data.get("is_related", False)
    related_indices = data.get("related_indices", [])
    if is_related and related_indices:
        result = []
        for idx in related_indices:
            if isinstance(idx, int) and 1 <= idx <= len(candidate_questions):
                result.append(candidate_questions[idx - 1]["question_index"])
        return result, ""
    return [], ""

def find_matching_questions(kb_entry, kb_idx, questions_df, 
                           reverse_index, path_index, kaodian_index, 
                           api_key=None, base_url=None, model_name="deepseek-chat"):
    """
    Find matching questions for a knowledge slice using PRD FR2.1.1 five-tier strategy.
    
    Strategy 1: Reverse Index (P0) - confidence 1.0
    Strategy 2: GPS Path-Based Match (P1) - confidence 0.95/0.90/0.85
    Strategy 3: Statute Collision (P2) - confidence 0.88
    Strategy 4: BGE Vector Retrieval (P3) - confidence = score
    Strategy 5: LLM Reranking (P4) - confidence 0.80
    """
    kb_path = kb_entry.get('完整路径', '')
    kb_content = get_kb_content(kb_entry)
    segs = [x.strip() for x in kb_path.split(">")] if kb_path else []
    raw_title = segs[-1] if segs else kb_path
    stripped_title = strip_title_prefix(raw_title)
    if segs:
        mod_path = " > ".join(segs[:-1] + [stripped_title])
    else:
        mod_path = kb_path
    kb_gps = normalize_path_dehydration(mod_path)
    kb_title = stripped_title

    matched_questions = []
    methods_used = []
    matched_set = set()  # Use set for O(1) lookup
    
    # Strategy 1: Reverse Index (P0) - Priority
    if kb_idx in reverse_index:
        for q_idx in reverse_index[kb_idx]:
            matched_set.add(q_idx)
            matched_questions.append({
                'question_index': int(q_idx),
                'confidence': 1.0,
                'method': 'Reverse_Index',
                'evidence': {
                    'reason': '反向索引复用：直接碰撞现有映射文件',
                    'source': 'existing_mapping',
                    'kb_path': kb_path
                }
            })
        if matched_questions:
            methods_used.append('Reverse_Index')
    
    # If we have matches from reverse index, return early (PRD: priority)
    if matched_questions:
        matched_questions.sort(key=lambda x: x['confidence'], reverse=True)
        return matched_questions, methods_used, [], []
    
    # Strategy 2: GPS Path-Based Match (P1)
    # Build GPS paths for all questions and match
    # Only keep the highest confidence matches (if multiple same confidence, keep all)
    strategy2_matches = []
    for idx, row in questions_df.iterrows():
        if idx in matched_set:
            continue
        
        q_gps = build_gps_path(row)
        if not q_gps:
            continue
        
        q_pian = str(row.get('篇', '')).strip()
        q_zhang = str(row.get('章', '')).strip()
        q_jie = str(row.get('节', '')).strip()
        q_kaodian = str(row.get('考点', '')).strip()
        q_kaodian_clean = re.sub(r'[（(].*?[）)]', '', q_kaodian).strip()
        q_kaodian_clean = re.sub(r'-无需修改', '', q_kaodian_clean).strip()
        
        # Full path match: require sufficient depth to avoid over-broad matches
        if q_pian and q_zhang and q_jie and q_kaodian_clean:
            full_path = normalize_path_dehydration(f"{q_pian}/{q_zhang}/{q_jie}/{q_kaodian_clean}")
            kb_gps_depth = len([s for s in kb_gps.split('/') if s])
            full_depth = len([s for s in full_path.split('/') if s])
            if kb_gps_depth >= 3 and full_depth >= 3 and (full_path in kb_gps or kb_gps in full_path):
                strategy2_matches.append({
                    'question_index': int(idx),
                    'confidence': 0.95,
                    'method': 'GPS_FullPath',
                    'evidence': {
                        'reason': f'全路径包含匹配：{full_path}',
                        'match_type': 'full_path',
                        'kb_gps': kb_gps,
                        'q_gps': full_path
                    }
                })
                continue
        
        # No partial-path strategy: fall through to later strategies
    
    # Strategy 2 outcome: full_path return early
    if strategy2_matches:
        full = [m for m in strategy2_matches if m['evidence']['match_type'] == 'full_path']
        if full:
            # Full path only: return early, keep best (no BGE)
            full.sort(key=lambda x: x['confidence'], reverse=True)
            max_c = full[0]['confidence']
            best = [m for m in full if abs(m['confidence'] - max_c) < 0.05]
            for match in best:
                matched_set.add(match['question_index'])
                matched_questions.append({k: v for k, v in match.items()})
            if 'GPS_FullPath' not in methods_used:
                methods_used.append('GPS_FullPath')
            matched_questions.sort(key=lambda x: x['confidence'], reverse=True)
            return matched_questions, methods_used, [], []
    
    # Strategy 3: Statute Collision (P2)
    # Extract legal references from KB content
    kb_legal_refs = extract_legal_references(kb_content)
    kb_legal_refs.extend(extract_legal_references(kb_path))
    
    # Only keep the highest confidence matches (if multiple same confidence, keep all)
    strategy3_matches = []
    if kb_legal_refs:
        for idx, row in questions_df.iterrows():
            if idx in matched_set:
                continue
            
            q_stem = str(row.get('题干', '')).strip()
            q_explanation = str(row.get('解析', '')).strip()
            q_legal_refs = extract_legal_references(q_stem)
            q_legal_refs.extend(extract_legal_references(q_explanation))
            
            # Check if any legal reference matches
            for kb_law, kb_article in kb_legal_refs:
                for q_law, q_article in q_legal_refs:
                    if kb_law == q_law and kb_article == q_article:
                        strategy3_matches.append({
                            'question_index': int(idx),
                            'confidence': 0.88,
                            'method': 'Statute_Collision',
                            'evidence': {
                                'reason': f'法条硬碰撞：《{kb_law}》第{kb_article}条',
                                'law_name': kb_law,
                                'article_number': kb_article
                            },
                            'row': row
                        })
                        break
    
    # Strategy 3: BGE refinement then keep best
    if strategy3_matches:
        kb_emb_text = get_kb_content_for_embedding(kb_entry)
        for m in strategy3_matches:
            q_emb_text = get_question_content_for_embedding(m['row'])
            try:
                bge_score = compute_bge_similarity(kb_emb_text, q_emb_text)
            except Exception:
                bge_score = 0.5
            conf = 0.88 + 0.07 * (bge_score - 0.5)
            conf = min(0.95, max(conf, 0.0))
            m['confidence'] = round(conf, 3)
            m['evidence']['bge_score'] = round(bge_score, 3)
            m['evidence']['bge_refined'] = True
        strategy3_matches.sort(key=lambda x: x['confidence'], reverse=True)
        max_confidence = strategy3_matches[0]['confidence']
        best_matches = [m for m in strategy3_matches if abs(m['confidence'] - max_confidence) < 0.05]
        for match in best_matches:
            matched_set.add(match['question_index'])
            out = {k: v for k, v in match.items() if k != 'row'}
            matched_questions.append(out)
        if 'Statute_Collision' not in methods_used:
            methods_used.append('Statute_Collision')
        matched_questions.sort(key=lambda x: x['confidence'], reverse=True)
        return matched_questions, methods_used, [], []
    
    # Strategy 4 & 5: BGE Vector Retrieval + LLM Reranking
    # Only use if no matches found from previous strategies
    if not matched_questions:
        kb_embedding_text = get_kb_content_for_embedding(kb_entry)
        
        # Build candidate pool: compute BGE similarity for all questions
        # Limit to first 20 questions for performance when testing
        candidate_scores = []
        max_questions_to_check = min(len(questions_df), TOP_K_RETRIEVAL * 2)  # Check more candidates
        for idx, row in questions_df.head(max_questions_to_check).iterrows():
            if idx in matched_set:
                continue
            q_embedding_text = get_question_content_for_embedding(row)
            bge_score = compute_bge_similarity(kb_embedding_text, q_embedding_text)
            candidate_scores.append({
                'question_index': int(idx),
                'row': row,
                'bge_score': bge_score,
                'gps_path': build_gps_path(row)
            })
        
        # Sort by BGE score
        candidate_scores.sort(key=lambda x: x['bge_score'], reverse=True)
        
        # Process candidates based on score thresholds
        # Only keep the highest score matches (if multiple same score, keep all)
        strategy4_matches = []
        llm_candidates = []
        for candidate in candidate_scores:
            q_idx = candidate['question_index']
            bge_score = candidate['bge_score']
            
            if bge_score > BGE_AUTO_PASS_THRESHOLD:  # Score > 0.8: auto pass
                strategy4_matches.append({
                    'question_index': q_idx,
                    'confidence': round(bge_score, 3),
                    'method': 'BGE_Vector',
                    'evidence': {
                        'reason': f'BGE语义向量检索自动通过（Score={round(bge_score, 3)}）',
                        'bge_score': round(bge_score, 3)
                    },
                    'bge_score': bge_score
                })
            elif BGE_LLM_REVIEW_THRESHOLD < bge_score <= BGE_AUTO_PASS_THRESHOLD:  # 0.5 < Score <= 0.8: LLM review
                llm_candidates.append(candidate)
        
        # Only keep the highest score matches (N≤3, if multiple same score, keep all)
        if strategy4_matches:
            strategy4_matches.sort(key=lambda x: x['bge_score'], reverse=True)
            max_score = strategy4_matches[0]['bge_score']
            # Keep top N (N≤3) matches with score >= max_score (or within 0.05 if close)
            best_matches = [m for m in strategy4_matches if abs(m['bge_score'] - max_score) < 0.05][:3]
            for match in best_matches:
                matched_set.add(match['question_index'])
                # Remove bge_score from match before appending (it's not part of the final structure)
                match_copy = {k: v for k, v in match.items() if k != 'bge_score'}
                matched_questions.append(match_copy)
            if 'BGE_Vector' not in methods_used:
                methods_used.append('BGE_Vector')
            
            # If we have matches from strategy 4, return early (only keep most relevant)
            if matched_questions:
                matched_questions.sort(key=lambda x: x['confidence'], reverse=True)
                return matched_questions, methods_used, [], []
        
        # Strategy 5: LLM Reranking for candidates with 0.5 < Score <= 0.75
        if llm_candidates and api_key:
            llm_results, _ = llm_rerank_candidates(
                kb_entry, llm_candidates[:5], api_key, base_url, model_name
            )
            
            for q_idx in llm_results:
                if q_idx not in matched_set:
                    matched_set.add(q_idx)
                    candidate = next((c for c in llm_candidates if c['question_index'] == q_idx), None)
                    bge_score = candidate['bge_score'] if candidate else 0.65
                    conf = 0.80 + 0.10 * (bge_score - 0.5)
                    conf = min(0.90, max(conf, 0.0))
                    matched_questions.append({
                        'question_index': q_idx,
                        'confidence': round(conf, 3),
                        'method': 'LLM_Logic',
                        'evidence': {
                            'reason': 'LLM专家逻辑重排序',
                            'bge_score': round(bge_score, 3),
                            'bge_refined': True
                        }
                    })
                    if 'LLM_Logic' not in methods_used:
                        methods_used.append('LLM_Logic')

        # LLM fallback: no matches from strategies 1-4
        if not matched_questions and api_key and candidate_scores:
            fallback_candidates = candidate_scores[:5]
            llm_results, _ = llm_rerank_candidates(
                kb_entry, fallback_candidates, api_key, base_url, model_name
            )
            for q_idx in llm_results:
                if q_idx not in matched_set:
                    matched_set.add(q_idx)
                    candidate = next((c for c in fallback_candidates if c['question_index'] == q_idx), None)
                    bge_score = candidate['bge_score'] if candidate else 0.5
                    conf = 0.80 + 0.10 * (bge_score - 0.5)
                    conf = min(0.90, max(conf, 0.0))
                    matched_questions.append({
                        'question_index': q_idx,
                        'confidence': round(conf, 3),
                        'method': 'LLM_Logic',
                        'evidence': {
                            'reason': 'LLM专家逻辑重排序',
                            'bge_score': round(bge_score, 3),
                            'bge_refined': True,
                            'llm_fallback': True
                        }
                    })
                    if 'LLM_Logic' not in methods_used:
                        methods_used.append('LLM_Logic')
    
    # Sort matched questions by confidence (descending) for multi-mapping support
    matched_questions.sort(key=lambda x: x['confidence'], reverse=True)
    
    return matched_questions, methods_used, [], []


def _filter_mapping_by_max_confidence_per_question(mapping):
    """
    Per-question strict Top1 filter: keep exactly one slice with highest score
    for each question.
    Tie-breaker:
      1) Higher confidence
      2) Higher method priority
      3) Smaller slice id (deterministic)
    Mutates mapping in place; removes slices with zero matches.
    """
    if not mapping:
        return
    method_priority = {
        "Reverse_Index": 100,
        "GPS_FullPath": 90,
        "Statute_Collision": 80,
        "LLM_Logic": 70,
        "BGE_Vector": 60,
        "MetaConflict_Pending": 10,
    }

    def _sid_num(sid):
        s = str(sid)
        return int(s) if s.isdigit() else 10**12

    best_by_question = {}
    for sid, entry in list(mapping.items()):
        for m in entry["matched_questions"]:
            qid = int(m["question_index"])
            conf = float(m.get("confidence", 0.0))
            method = str(m.get("method", ""))
            pri = method_priority.get(method, 0)
            score = (conf, pri, -_sid_num(sid))
            prev = best_by_question.get(qid)
            if prev is None or score > prev["score"]:
                best_by_question[qid] = {"sid": sid, "score": score}

    kept = {(v["sid"], qid) for qid, v in best_by_question.items()}
    to_drop = []
    for sid, entry in mapping.items():
        new_matches = [
            m for m in entry["matched_questions"]
            if (sid, m["question_index"]) in kept
        ]
        if not new_matches:
            to_drop.append(sid)
            continue
        entry["matched_questions"] = new_matches
        entry["total_matches"] = len(new_matches)
        entry["methods_used"] = list(set(m["method"] for m in new_matches))
    for sid in to_drop:
        del mapping[sid]


def build_slice_meta(kb_data):
    """Build per-slice metadata: kb_idx, kb_gps, kb_title, legal_refs, embedding text.
    kb_title and kb_gps use stripped title (TP12.6c) for strategy matching."""
    meta = []
    for kb_idx, entry in enumerate(kb_data):
        path = entry.get("完整路径", "")
        content = get_kb_content(entry)
        segs = [x.strip() for x in path.split(">")] if path else []
        raw_title = segs[-1] if segs else path
        stripped_title = strip_title_prefix(raw_title)
        # Use stripped last segment for path so full_path / end_node match correctly
        if segs:
            mod_path = " > ".join(segs[:-1] + [stripped_title])
        else:
            mod_path = path
        gps = normalize_path_dehydration(mod_path)
        refs = extract_legal_references(content)
        refs.extend(extract_legal_references(path))
        emb_text = get_kb_content_for_embedding(entry)
        meta.append({
            "kb_idx": kb_idx,
            "kb_entry": entry,
            "kb_gps": gps,
            "kb_title": stripped_title,
            "kb_legal_refs": refs,
            "emb_text": emb_text,
        })
    return meta


def precompute_slice_embeddings(slice_meta, batch_size=64):
    """Precompute BGE embeddings for all slices. Returns (N, dim) array."""
    texts = [m["emb_text"] for m in slice_meta]
    return encode_batch(texts, batch_size=batch_size)


def build_question_to_kb(reverse_index, question_indices):
    """Build question -> [(kb_idx, 1.0, evidence)] from reverse_index for Strategy 1."""
    q2kb = {}
    for kb_idx, q_list in reverse_index.items():
        for q_idx in q_list:
            if q_idx not in question_indices:
                continue
            ev = {
                "reason": "反向索引复用：直接碰撞现有映射文件",
                "source": "existing_mapping",
            }
            q2kb.setdefault(q_idx, []).append((kb_idx, 1.0, "Reverse_Index", ev))
    return q2kb


def llm_rerank_slices_for_question(q_row, candidate_slices, api_key, base_url, model_name="deepseek-chat"):
    """
    Strategy 5 inverted: for this question, which of the candidate slices are relevant?
    candidate_slices: list of {kb_idx, kb_entry, bge_score, ...}
    Returns: (list of kb_idx that are related, empty reason string).
    On JSON parse failure: writes debug file and raises LLMRerankParseError (no silent ignore).
    """
    from openai import OpenAI

    if not candidate_slices or not api_key:
        return [], ""

    q_gps = build_gps_path(q_row)
    q_stem = str(q_row.get("题干", "")).strip()[:300]

    lines = []
    for i, c in enumerate(candidate_slices[:5], 1):
        path = c["kb_entry"].get("完整路径", "")
        content = get_kb_content(c["kb_entry"])[:400]
        lines.append(f"{i}. 路径: {path}\n   内容: {content}...")

    prompt = (
        f"你是一个房产交易专家。\n"
        f"这道题考的是【{q_gps}】，题干是：{q_stem}\n\n"
        f"请从以下 {len(lines)} 个知识切片中选出能支撑解题的项。如果不相关，请输出 False。\n\n"
        f"候选切片:\n" + "\n".join(lines) + "\n\n"
        f"请返回**合法JSON**，且仅此一段，格式：{{\"is_related\": true或false, \"related_indices\": [1,3]}}。\n"
        f"related_indices 为候选编号（从1开始），可空列表。"
    )

    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=500,
        timeout=30,
        response_format={"type": "json_object"},
    )
    raw_content = resp.choices[0].message.content if resp.choices and resp.choices[0].message else ""
    if not raw_content:
        return [], ""

    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_content, re.DOTALL)
    if json_match:
        extracted = json_match.group(1)
    else:
        m = re.search(r"\{.*\}", raw_content, re.DOTALL)
        extracted = m.group(0) if m else raw_content

    try:
        data = _parse_llm_rerank_json(extracted, raw_content)
    except LLMRerankParseError as exc:
        print(f"    [LLM Rerank] JSON parse failed, skip. Debug: {exc.debug_path}", flush=True)
        return [], f"parse_error:{exc.debug_path}"
    is_related = data.get("is_related", False)
    indices = data.get("related_indices", [])
    if is_related and indices:
        out = []
        for idx in indices:
            if isinstance(idx, int) and 1 <= idx <= len(candidate_slices):
                out.append(candidate_slices[idx - 1]["kb_idx"])
        return out, ""
    return [], ""


def find_matching_slices_for_question(
    q_row, q_idx, slice_meta, slice_embeddings, question_to_kb, kb_data, api_key, base_url, model_name
):
    """
    Question-centric: find which slices match this question. Returns list of
    (kb_idx, confidence, method, evidence).
    """
    q_gps = build_gps_path(q_row)
    if not q_gps:
        return []
    meta_check = detect_question_meta_conflict(q_row)
    is_meta_conflict = bool(meta_check.get("meta_conflict"))

    q_pian = str(q_row.get("篇", "")).strip()
    q_zhang = str(q_row.get("章", "")).strip()
    q_jie = str(q_row.get("节", "")).strip()
    q_kaodian = str(q_row.get("考点", "")).strip()
    q_kaodian_clean = re.sub(r"[（(].*?[）)]", "", q_kaodian).strip()
    q_kaodian_clean = re.sub(r"-无需修改", "", q_kaodian_clean).strip()

    # Strategy 1: Reverse index
    if q_idx in question_to_kb and not is_meta_conflict:
        return [(kb_idx, 1.0, "Reverse_Index", ev) for kb_idx, _, _, ev in question_to_kb[q_idx]]

    # Strategy 2: GPS path
    full_path = None
    if q_pian and q_zhang and q_jie and q_kaodian_clean:
        full_path = normalize_path_dehydration(f"{q_pian}/{q_zhang}/{q_jie}/{q_kaodian_clean}")

    full_matches = []
    for m in slice_meta:
        kb_idx = m["kb_idx"]
        kb_gps = m["kb_gps"]

        if full_path:
            kb_gps_depth = len([s for s in kb_gps.split('/') if s])
            full_depth = len([s for s in full_path.split('/') if s])
            if kb_gps_depth >= 3 and full_depth >= 3 and (full_path in kb_gps or kb_gps in full_path):
                full_matches.append((kb_idx, 0.95, "GPS_FullPath", {
                    "reason": f"全路径包含匹配：{full_path}",
                    "match_type": "full_path",
                    "kb_gps": kb_gps,
                    "q_gps": full_path,
                }))
                continue

    if full_matches and not is_meta_conflict:
        return full_matches

    # Partial-path strategy removed; fall through to later strategies

    # Strategy 3: Statute collision
    q_emb_text = get_question_content_for_embedding(q_row)
    q_emb = encode_batch([q_emb_text])[0]
    q_stem = str(q_row.get("题干", "")).strip()
    q_expl = str(q_row.get("解析", "")).strip()
    q_refs = extract_legal_references(q_stem)
    q_refs.extend(extract_legal_references(q_expl))

    stat_matches = []
    for m in slice_meta:
        for kb_law, kb_art in m["kb_legal_refs"]:
            for q_law, q_art in q_refs:
                if kb_law == q_law and kb_art == q_art:
                    stat_matches.append((m["kb_idx"], m, kb_law, kb_art))
                    break
            else:
                continue
            break

    if stat_matches and not is_meta_conflict:
        refined = []
        for kb_idx, sm, law, art in stat_matches:
            emb = slice_embeddings[sm["kb_idx"]]
            sim = float(np.dot(q_emb, emb))
            conf = 0.88 + 0.07 * (sim - 0.5)
            conf = min(0.95, max(0.0, conf))
            refined.append((kb_idx, round(conf, 3), "Statute_Collision", {
                "reason": f"法条硬碰撞：《{law}》第{art}条",
                "bge_score": round(sim, 3),
                "bge_refined": True,
            }))
        refined.sort(key=lambda x: x[1], reverse=True)
        best = refined[0][1]
        return [r for r in refined if abs(r[1] - best) < 0.05]

    # Strategy 4 & 5: BGE + LLM
    sims = np.dot(slice_embeddings, q_emb)
    candidates = []
    for i, s in enumerate(sims):
        candidates.append({
            "kb_idx": i,
            "kb_entry": slice_meta[i]["kb_entry"],
            "bge_score": float(s),
        })
    candidates.sort(key=lambda x: x["bge_score"], reverse=True)

    auto = [c for c in candidates if c["bge_score"] > BGE_AUTO_PASS_THRESHOLD]

    # High-score short-circuit: skip LLM when BGE is confidently high.
    if auto and not is_meta_conflict:
        best = auto[0]["bge_score"]
        keep = [c for c in auto if abs(c["bge_score"] - best) < 0.05][:3]
        return [
            (c["kb_idx"], round(c["bge_score"], 3), "BGE_Vector", {
                "reason": f"BGE语义向量检索自动通过（Score={round(c['bge_score'], 3)}）",
                "bge_score": round(c["bge_score"], 3),
                "meta_conflict": False,
                "llm_skipped_by_high_score": True,
            })
            for c in keep
        ]

    llm_cands = candidates[:5]
    if llm_cands and api_key:
        related_kb, _ = llm_rerank_slices_for_question(q_row, llm_cands, api_key, base_url, model_name)
        out = []
        for kb_idx in related_kb:
            c = next((x for x in llm_cands if x["kb_idx"] == kb_idx), None)
            if not c:
                continue
            bge = c["bge_score"]
            conf = 0.80 + 0.10 * (bge - 0.5)
            conf = min(0.90, max(0.0, conf))
            out.append((kb_idx, round(conf, 3), "LLM_Logic", {
                "reason": "LLM专家逻辑重排序（含元数据一致性门禁）" if is_meta_conflict else "LLM专家逻辑重排序",
                "bge_score": round(bge, 3),
                "bge_refined": True,
                "meta_conflict": is_meta_conflict,
                "meta_conflict_detail": meta_check.get("detail", ""),
            }))
        if out:
            return out

    # Meta-conflict fallback: keep one low-confidence candidate for manual review queue.
    if is_meta_conflict and candidates:
        top = candidates[0]
        return [(
            top["kb_idx"],
            round(min(top["bge_score"], 0.6), 3),
            "MetaConflict_Pending",
            {
                "reason": "元数据冲突题：禁止BGE自动通过，需人工复核",
                "bge_score": round(top["bge_score"], 3),
                "meta_conflict": True,
                "meta_conflict_detail": meta_check.get("detail", ""),
                "matched_count": meta_check.get("matched_count", 0),
                "total_count": meta_check.get("total_count", 0),
            },
        )]

    return []


def create_mapping():
    """
    Create the knowledge-to-questions mapping according to PRD requirements.
    
    PRD FR2.1 requirements:
    1. Priority: Use reverse index from question_knowledge_mapping.json
    2. Fallback: Use BGE semantic vector retrieval when no mapping exists
    """
    kb_data, questions_df, reverse_index, path_index, kaodian_index = load_data()
    
    # Initialize BGE model (required for fallback mechanism)
    # According to PRD: 回退机制：无映射时使用BGE语义向量检索
    print("Initializing BGE embedding model (for fallback mechanism)...")
    get_bge_model()  # This will raise error if BGE is not available
    
    print("Mapping knowledge slices to questions according to PRD requirements...")
    TEST_MODE = False   # True: 10 questions vs all slices; False: full run
    if TEST_MODE:
        QUESTION_LIMIT = 20
        RANDOM_SEED = 20260128
        questions_df_work = questions_df.sample(n=QUESTION_LIMIT, random_state=RANDOM_SEED).copy()
        kb_data_work = kb_data  # all slices
        print(f"⚠️  TEST MODE: Random {len(questions_df_work)} questions (seed={RANDOM_SEED}) × full {len(kb_data_work)} knowledge slices")
    else:
        questions_df_work = questions_df
        kb_data_work = kb_data
        print(f"Full run: {len(questions_df_work)} questions, {len(kb_data_work)} knowledge slices")

    # Question-centric flow (PRD FR2.1.1, TP12.12): one question × full slices; precompute slice embeddings
    print("Building slice metadata...")
    slice_meta = build_slice_meta(kb_data_work)
    print("Precomputing BGE embeddings for all slices...")
    slice_embeddings = precompute_slice_embeddings(slice_meta)
    question_indices = set(questions_df_work.index.tolist())
    question_to_kb = build_question_to_kb(reverse_index, question_indices)

    config = load_config()
    api_key, base_url, model_name = resolve_llm_config(config)

    mapping = {}
    nq = len(questions_df_work)
    for q_ord, (q_idx, q_row) in enumerate(questions_df_work.iterrows()):
        matches = find_matching_slices_for_question(
            q_row, q_idx, slice_meta, slice_embeddings, question_to_kb, kb_data_work,
            api_key=api_key, base_url=base_url, model_name=model_name
        )
        for kb_idx, conf, method, ev in matches:
            if kb_idx not in mapping:
                e = kb_data_work[kb_idx]
                mapping[kb_idx] = {
                    '完整路径': e.get('完整路径', ''),
                    '掌握程度': e.get('掌握程度', ''),
                    'matched_questions': [],
                }
            mapping[kb_idx]['matched_questions'].append({
                'question_index': int(q_idx),
                'confidence': conf,
                'method': method,
                'evidence': ev,
            })
        if (q_ord + 1) % 5 == 0 or q_ord == nq - 1:
            print(f"  Processed {q_ord + 1}/{nq} questions ({(q_ord + 1) / nq * 100:.1f}%)", flush=True)

    for entry in mapping.values():
        entry['matched_questions'].sort(key=lambda m: m['confidence'], reverse=True)
        entry['total_matches'] = len(entry['matched_questions'])
        entry['methods_used'] = list(set(m['method'] for m in entry['matched_questions']))

    # Per-question filter: keep only highest-confidence slice(s) per question (PRD FR2.1.1, TP12.11)
    _filter_mapping_by_max_confidence_per_question(mapping)

    # Save mapping
    print(f"\nSaving mapping to {OUTPUT_PATH}...")
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    
    # Print statistics
    print("\nMapping Statistics:")
    total_slices = len(mapping)
    if total_slices == 0:
        print("  No slices with matches after per-question filter.")
        print(f"\nMapping complete! Saved to {OUTPUT_PATH}")
        return
    slices_with_matches = sum(1 for entry in mapping.values() if entry["total_matches"] > 0)
    total_matches = sum(entry["total_matches"] for entry in mapping.values())
    print(f"  Total knowledge slices: {total_slices}")
    print(f"  Slices with matches: {slices_with_matches} ({slices_with_matches / total_slices * 100:.1f}%)")
    print(f"  Total question matches: {total_matches}")
    print(f"  Average matches per slice: {total_matches / total_slices:.2f}")
    
    # Method statistics
    method_counts = {}
    for entry in mapping.values():
        for method in entry["methods_used"]:
            method_counts[method] = method_counts.get(method, 0) + 1
    print("\nMethod Statistics:")
    for method, count in sorted(method_counts.items(), key=lambda x: -x[1]):
        print(f"  {method}: {count} slices ({count / total_slices * 100:.1f}%)")
    # Auto-mapping rate (all matches are auto, no LLM used per PRD)
    auto_mapped = sum(1 for entry in mapping.values() if entry["total_matches"] > 0)
    auto_rate = auto_mapped / total_slices if total_slices > 0 else 0.0
    print(f"\nAuto-mapping rate: {auto_mapped}/{total_slices} = {auto_rate*100:.1f}%")
    
    # Confidence distribution
    all_confidences = []
    for entry in mapping.values():
        for m in entry["matched_questions"]:
            all_confidences.append(m["confidence"])
    
    if all_confidences:
        print(f"\nConfidence Statistics:")
        print(f"  Average: {sum(all_confidences)/len(all_confidences):.3f}")
        print(f"  Min: {min(all_confidences):.3f}")
        print(f"  Max: {max(all_confidences):.3f}")
        print(f"  High confidence (>=0.7): {sum(1 for c in all_confidences if c >= 0.7)} ({sum(1 for c in all_confidences if c >= 0.7)/len(all_confidences)*100:.1f}%)")
        print(f"  Medium confidence (0.3-0.7): {sum(1 for c in all_confidences if 0.3 <= c < 0.7)} ({sum(1 for c in all_confidences if 0.3 <= c < 0.7)/len(all_confidences)*100:.1f}%)")
        print(f"  Low confidence (<0.3): {sum(1 for c in all_confidences if c < 0.3)} ({sum(1 for c in all_confidences if c < 0.3)/len(all_confidences)*100:.1f}%)")
    
    print(f"\nMapping complete! Saved to {OUTPUT_PATH}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Map knowledge slices to historical questions")
    parser.add_argument("--tenant-id", default="", help="城市租户ID，例如 hz/bj/sh")
    parser.add_argument("--kb-path", default="", help="覆盖知识切片路径")
    parser.add_argument("--history-path", default="", help="覆盖母题路径")
    parser.add_argument("--output", default="", help="覆盖输出映射路径")
    args = parser.parse_args()

    if args.tenant_id:
        KB_PATH = str(resolve_tenant_kb_path(args.tenant_id, fallback=KB_PATH))
        HISTORY_PATH = str(resolve_tenant_history_path(args.tenant_id, fallback=HISTORY_PATH))
        OUTPUT_PATH = str(tenant_mapping_path(args.tenant_id))
    if args.kb_path:
        KB_PATH = args.kb_path
    if args.history_path:
        HISTORY_PATH = args.history_path
    if args.output:
        OUTPUT_PATH = args.output

    create_mapping()
