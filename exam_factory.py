import os
import json
import pandas as pd
import random
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from pydantic import BaseModel, Field, ValidationError
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from openai import OpenAI
from volcenginesdkarkruntime import Ark
from tenants_config import (
    resolve_tenant_kb_path,
    resolve_tenant_history_path,
    tenant_mapping_path,
    tenant_mapping_review_path,
    resolve_tenant_from_env,
)
from reference_loader import load_reference_questions

# Load environment variables from the single primary key file.
config_path = str(Path(__file__).resolve().parent / "填写您的Key.txt")
config = {}
if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()

# Fallback to .env or system env
DEEPSEEK_API_KEY = config.get("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = config.get("DEEPSEEK_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL", "https://openapi-ait.ke.com")
DEEPSEEK_MODEL = config.get("DEEPSEEK_MODEL") or os.getenv("DEEPSEEK_MODEL", "deepseek-reasoner")

AIT_API_KEY = config.get("AIT_API_KEY") or os.getenv("AIT_API_KEY")
AIT_BASE_URL = config.get("AIT_BASE_URL") or os.getenv("AIT_BASE_URL")
AIT_MODEL = config.get("AIT_MODEL") or os.getenv("AIT_MODEL")

API_KEY = AIT_API_KEY or DEEPSEEK_API_KEY or config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
BASE_URL = AIT_BASE_URL or DEEPSEEK_BASE_URL or config.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://openapi-ait.ke.com")
MODEL_NAME = AIT_MODEL or DEEPSEEK_MODEL or config.get("OPENAI_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-reasoner")
ROUTER_MODEL = config.get("ROUTER_MODEL") or os.getenv("ROUTER_MODEL") or MODEL_NAME
SPECIALIST_MODEL = config.get("SPECIALIST_MODEL") or os.getenv("SPECIALIST_MODEL") or MODEL_NAME
WRITER_MODEL = config.get("WRITER_MODEL") or os.getenv("WRITER_MODEL") or MODEL_NAME
CALC_MODEL = config.get("CALC_MODEL") or os.getenv("CALC_MODEL") or MODEL_NAME

# Critic-specific configuration for finance question validation
CRITIC_API_KEY = config.get("CRITIC_API_KEY") or os.getenv("CRITIC_API_KEY") or API_KEY
CRITIC_BASE_URL = config.get("CRITIC_BASE_URL") or os.getenv("CRITIC_BASE_URL") or BASE_URL
CRITIC_MODEL = config.get("CRITIC_MODEL") or os.getenv("CRITIC_MODEL") or MODEL_NAME
CRITIC_PROVIDER = config.get("CRITIC_PROVIDER", "ait").lower() or os.getenv("CRITIC_PROVIDER", "ait").lower()

# Code generation model configuration (for dynamic code generation)
CODE_GEN_MODEL = config.get("CODE_GEN_MODEL") or os.getenv("CODE_GEN_MODEL", "gpt-5.3-codex")
CODE_GEN_API_KEY = config.get("CODE_GEN_API_KEY") or os.getenv("CODE_GEN_API_KEY") or API_KEY
CODE_GEN_BASE_URL = config.get("CODE_GEN_BASE_URL") or os.getenv("CODE_GEN_BASE_URL") or BASE_URL
CODE_GEN_PROVIDER = config.get("CODE_GEN_PROVIDER", "ait").lower() or os.getenv("CODE_GEN_PROVIDER", "ait").lower()
IMAGE_PROVIDER = config.get("IMAGE_PROVIDER", "ait").lower() or os.getenv("IMAGE_PROVIDER", "ait").lower()

# Volcano Ark config (API Key preferred; AK/SK fallback)
ARK_API_KEY = config.get("ARK_API_KEY") or os.getenv("ARK_API_KEY", "")
VOLC_ACCESS_KEY_ID = config.get("VOLC_ACCESS_KEY_ID") or os.getenv("VOLC_ACCESS_KEY_ID", "")
VOLC_SECRET_ACCESS_KEY = config.get("VOLC_SECRET_ACCESS_KEY") or os.getenv("VOLC_SECRET_ACCESS_KEY", "")
ARK_BASE_URL = config.get("ARK_BASE_URL") or os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
ARK_PROJECT_NAME = config.get("ARK_PROJECT_NAME") or os.getenv("ARK_PROJECT_NAME", "")

# Paths (tenant-aware; fallback to existing single-city files)
def set_active_tenant(tenant_id: str) -> None:
    global TENANT_ID, KB_PATH, HISTORY_PATH, MAPPING_PATH
    TENANT_ID = tenant_id
    KB_PATH = str(resolve_tenant_kb_path(tenant_id))
    HISTORY_PATH = str(resolve_tenant_history_path(tenant_id))
    MAPPING_PATH = str(tenant_mapping_path(tenant_id))


TENANT_ID = resolve_tenant_from_env()
KB_PATH = "bot_knowledge_base.jsonl"
HISTORY_PATH = "存量房买卖母卷ABCD.xls"
MAPPING_PATH = "knowledge_question_mapping.json"
set_active_tenant(TENANT_ID)
OUTPUT_PATH = "generated_exam_questions.xlsx"

# --- Pydantic Models ---
class ExamQuestion(BaseModel):
    题干: str = Field(..., description="The question stem")
    选项1: str = Field(..., description="Option A")
    选项2: str = Field(..., description="Option B")
    选项3: str = Field("", description="Option C")
    选项4: str = Field("", description="Option D")
    选项5: str = Field("", description="Option E")
    选项6: str = Field("", description="Option F")
    选项7: str = Field("", description="Option G")
    选项8: str = Field("", description="Option H")
    正确答案: str = Field(..., pattern="^[ABCDEFGH]+$", description="Correct answer (A-H or combination for multi-choice)")
    解析: str = Field(..., description="Structured explanation: 1. Source 2. Analysis 3. Conclusion")
    难度值: float = Field(..., ge=0, le=1, description="Difficulty 0.0-1.0")

# --- Knowledge Retriever (Unchanged) ---
class KnowledgeRetriever:
    def __init__(self, kb_path, history_path, mapping_path: Optional[str] = None):
        print("Loading Knowledge Base...")
        self.kb_data = []
        self.mapping_path = mapping_path or MAPPING_PATH
        self.mapping_review = {}
        review_path = tenant_mapping_review_path(TENANT_ID)
        if os.path.exists(review_path):
            try:
                with open(review_path, "r", encoding="utf-8") as rf:
                    raw_review = json.load(rf)
                if isinstance(raw_review, dict):
                    self.mapping_review = raw_review
            except Exception:
                self.mapping_review = {}
        with open(kb_path, 'r', encoding='utf-8') as f:
            for line in f:
                item = json.loads(line)
                # Adapter for new slice format: Construct '核心内容' if missing
                if '核心内容' not in item and '结构化内容' in item:
                    struct = item['结构化内容']
                    parts = []
                    if struct.get('context_before'):
                        parts.append(struct['context_before'])
                    if struct.get('tables'):
                        parts.extend([str(t) for t in struct['tables']])
                    if struct.get('context_after'):
                        parts.append(struct['context_after'])
                    if struct.get('formulas'):
                        parts.extend(struct['formulas'])
                    if struct.get('examples'):
                        parts.extend(struct['examples'])
                    
                    item['核心内容'] = "\n".join(parts)
                
                self.kb_data.append(item)
        
        print("Loading Historical Questions...")
        self.history_df = load_reference_questions(history_path)
        self.vectorizer: Optional[TfidfVectorizer] = None
        self.history_corpus: List[str] = []
        self.tfidf_matrix = None
        if not self.history_df.empty:
            print("Indexing Historical Questions...")
            self.vectorizer = TfidfVectorizer()
            self.history_corpus = (
                self.history_df['题干'].astype(str) + " " + self.history_df['考点'].astype(str)
            ).tolist()
            self.tfidf_matrix = self.vectorizer.fit_transform(self.history_corpus)
        else:
            print(f"Warning: no usable reference questions loaded from {history_path}. Generation will continue without mother questions.")

        # Index KB slices for related-slice retrieval
        self.kb_vectorizer = TfidfVectorizer()
        self.kb_corpus = [
            f"{item.get('完整路径','')} {item.get('核心内容','')}"
            for item in self.kb_data
        ]
        self.kb_tfidf_matrix = self.kb_vectorizer.fit_transform(self.kb_corpus)
        
        # Load knowledge-question mapping
        print("Loading Question-Knowledge Mapping...")
        self.kb_to_questions = {}  # Reverse index: kb_path -> list of mapping entries
        self.mapping = {}
        kb_path_by_id = {i: item.get("完整路径") for i, item in enumerate(self.kb_data)}
        mapping_loaded = False

        mapping_candidates = [self.mapping_path, "knowledge_question_mapping.json"]
        mapping_candidates = [p for i, p in enumerate(mapping_candidates) if p and p not in mapping_candidates[:i]]

        selected_mapping_path = next((p for p in mapping_candidates if os.path.exists(p)), None)
        if selected_mapping_path:
            with open(selected_mapping_path, "r", encoding="utf-8") as f:
                self.mapping = json.load(f)
            total_candidates = 0
            kept_candidates = 0
            for slice_id, entry in self.mapping.items():
                try:
                    slice_idx = int(slice_id)
                except Exception:
                    slice_idx = None
                kb_path = entry.get("完整路径") or (kb_path_by_id.get(slice_idx) if slice_idx is not None else None)
                if not kb_path:
                    continue
                for m in entry.get("matched_questions", []):
                    total_candidates += 1
                    q_idx = m.get("question_index")
                    if q_idx is None:
                        continue
                    map_key = f"{slice_id}:{q_idx}"
                    review = self.mapping_review.get(map_key, {})
                    # Enforce: only confirmed mapping can be used for generation examples
                    if review.get("confirm_status") != "confirmed":
                        continue
                    self.kb_to_questions.setdefault(kb_path, []).append({
                        "q_idx": int(q_idx),
                        "confidence": float(m.get("confidence", 0.0)),
                        "method": m.get("method", "")
                    })
                    kept_candidates += 1
            print(f"Mapped {len(self.mapping)} slices to {len(self.kb_to_questions)} KB paths ({selected_mapping_path})")
            print(f"Confirmed mapping usage: kept {kept_candidates}/{total_candidates} entries")
            mapping_loaded = True

        if not mapping_loaded:
            print("Warning: confirmed mapping file not found. No mapped examples will be used.")
            self.mapping = {}
            self.kb_to_questions = {}

    def get_random_kb_chunk(self):
        # New format is already filtered for empty headers, so just check for content.
        valid_chunks = [c for c in self.kb_data if c.get('核心内容')]
        if not valid_chunks:
            return random.choice(self.kb_data) # Fallback
        return random.choice(valid_chunks)
    
    def _is_valid_example(self, row):
        """Check if a row has all required fields without NaN values."""
        required_fields = ['题干', '选项1', '选项2', '正确答案', '解析']
        for field in required_fields:
            value = row.get(field)
            # Check for NaN or empty string
            if pd.isna(value) or (isinstance(value, str) and value.strip() == ''):
                return False
        return True
    
    def _get_question_type(self, row):
        """判断题目类型。"""
        # 判断题：选项1是"正确"，选项2是"错误"
        if str(row.get('选项1', '')).strip() == '正确' and str(row.get('选项2', '')).strip() == '错误':
            return '判断题'
        # 多选题：答案包含多个字母（如 AB, ABC）
        answer = str(row.get('正确答案', '')).strip()
        if len(answer) > 1 and all(c in 'ABCDE' for c in answer):
            return '多选题'
        # 单选题：默认
        return '单选题'
    
    def _matches_question_type(self, row, question_type):
        """检查题目是否匹配指定类型。"""
        if not question_type or str(question_type).strip() in {"随机", "auto", "AUTO"}:
            return True
        return self._get_question_type(row) == question_type

    def get_preferred_question_types_by_knowledge_point(self, kb_chunk):
        """
        获取某知识切片关联母题的题型优先列表（按出现频次降序）。
        仅统计已确认映射中的可用母题。
        """
        if not isinstance(kb_chunk, dict):
            return []
        path = kb_chunk.get('完整路径')
        if not path or path not in self.kb_to_questions:
            return []
        counts = {"单选题": 0, "多选题": 0, "判断题": 0}
        for entry in self.kb_to_questions.get(path, []):
            try:
                q_idx = int(entry.get("q_idx"))
            except (TypeError, ValueError):
                continue
            if q_idx < 0 or q_idx >= len(self.history_df):
                continue
            row = self.history_df.iloc[q_idx]
            if not self._is_valid_example(row):
                continue
            q_type = self._get_question_type(row)
            if q_type in counts:
                counts[q_type] += 1
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        return [q_type for q_type, cnt in ranked if cnt > 0]

    def get_similar_examples(self, query_text, k=3, question_type=None):
        if self.history_df.empty or self.vectorizer is None or self.tfidf_matrix is None:
            return []
        query_vec = self.vectorizer.transform([query_text])
        similarities = cosine_similarity(query_vec, self.tfidf_matrix).flatten()
        # Get more candidates to account for filtering
        top_candidates = similarities.argsort()[-(k*5):][::-1]  # Increased from k*3 to k*5 for type filtering
        
        examples = []
        for idx in top_candidates:
            if len(examples) >= k:
                break
            row = self.history_df.iloc[idx]
            # Skip invalid examples
            if not self._is_valid_example(row):
                continue
            # Skip if question type doesn't match
            if not self._matches_question_type(row, question_type):
                continue
            examples.append({
                "题干": row['题干'],
                "选项": {
                    "A": row['选项1'], "B": row['选项2'], "C": row['选项3'], "D": row['选项4']
                },
                "正确答案": row['正确答案'],
                "解析": row['解析'],
                "难度": row['难度值']
            })
        return examples

    def is_similar_to_history(self, text: str, threshold: float = 0.9, top_k: int = 3):
        """Check if text is highly similar to existing history questions."""
        if not text:
            return False, None, None
        if self.history_df.empty or self.vectorizer is None or self.tfidf_matrix is None:
            return False, None, None
        try:
            query_vec = self.vectorizer.transform([text])
            similarities = cosine_similarity(query_vec, self.tfidf_matrix).flatten()
            top_idx = similarities.argsort()[::-1]
            if not len(top_idx):
                return False, None, None
            best_idx = top_idx[0]
            best_score = similarities[best_idx]
            if best_score >= threshold:
                row = self.history_df.iloc[best_idx]
                return True, best_score, str(row.get('题干', ''))
            return False, best_score, None
        except Exception:
            return False, None, None
    
    def get_examples_by_knowledge_point(self, kb_chunk, k=3, question_type=None):
        """Get examples that match this knowledge point."""
        if self.history_df.empty:
            return []
        path = kb_chunk['完整路径']

        # Find questions mapped to this KB path
        if path in self.kb_to_questions:
            entries = self.kb_to_questions[path]
            strong_methods = {"exact_path_match", "fuzzy_path_match"}
            filtered = [
                e for e in entries
                if e.get("method") in strong_methods or e.get("confidence", 0.0) >= 0.3
            ]
            if not filtered:
                return []
            # Try to get more candidates to account for filtering
            max_tries = min(len(filtered), k * 5)  # Increased from k*3 to k*5 for type filtering
            candidates = random.sample(filtered, max_tries)
            
            examples = []
            for entry in candidates:
                if len(examples) >= k:
                    break
                row = self.history_df.iloc[entry["q_idx"]]
                # Skip invalid examples
                if not self._is_valid_example(row):
                    continue
                # Skip if question type doesn't match
                if not self._matches_question_type(row, question_type):
                    continue
                examples.append({
                    "题干": row['题干'],
                    "选项": {
                        "A": row['选项1'], "B": row['选项2'], "C": row['选项3'], "D": row['选项4']
                    },
                    "正确答案": row['正确答案'],
                    "解析": row['解析'],
                    "难度": row['难度值']
                })
            return examples
        else:
            # No mapping -> no examples
            return []

    def get_parent_slices(self, kb_chunk):
        path = kb_chunk.get("完整路径", "")
        if not path or " > " not in path:
            return []
        parent_path = " > ".join(path.split(" > ")[:-1]).strip()
        if not parent_path:
            return []
        prefix = parent_path + " > "
        return [
            c for c in self.kb_data
            if isinstance(c, dict) and str(c.get("完整路径", "")).startswith(prefix)
        ]

    def get_related_kb_chunks(self, query_text: str, k: int = 5, exclude_paths=None):
        if not query_text:
            return []
        if exclude_paths is None:
            exclude_paths = set()
        else:
            exclude_paths = set(exclude_paths)
        try:
            query_vec = self.kb_vectorizer.transform([query_text])
            sims = cosine_similarity(query_vec, self.kb_tfidf_matrix).flatten()
            top_idx = sims.argsort()[::-1]
            results = []
            for idx in top_idx:
                if len(results) >= k:
                    break
                chunk = self.kb_data[idx]
                path = chunk.get("完整路径", "")
                if path in exclude_paths:
                    continue
                results.append(chunk)
            return results
        except Exception:
            return []

# --- Multi-Agent System ---

class BaseAgent:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.model_name = model
        self.ark_project_name = ARK_PROJECT_NAME

        base_url_lower = str(base_url or "").lower()
        use_ark = ("volces.com" in base_url_lower) or ("ark.cn" in base_url_lower)
        is_deepseek = "deepseek" in (self.model_name or "").lower()
        if is_deepseek and not use_ark:
            if not api_key:
                raise ValueError("DeepSeek API Key is missing.")
            self.client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            ark_key = ARK_API_KEY or api_key
            if ark_key:
                self.client = Ark(
                    api_key=ark_key,
                    base_url=ARK_BASE_URL,
                )
            else:
                if not (VOLC_ACCESS_KEY_ID and VOLC_SECRET_ACCESS_KEY):
                    raise ValueError("ARK_API_KEY is missing, and VOLC_ACCESS_KEY_ID / VOLC_SECRET_ACCESS_KEY is also missing.")
                self.client = Ark(
                    ak=VOLC_ACCESS_KEY_ID,
                    sk=VOLC_SECRET_ACCESS_KEY,
                    base_url=ARK_BASE_URL,
                )

    def call_llm(self, prompt: str, json_mode: bool = False) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"} if json_mode else None,
                temperature=0.3,
                extra_headers=({"X-Project-Name": self.ark_project_name} if self.ark_project_name else None),
            )
            return response.choices[0].message.content
        except Exception as e:
            raise e

class Router(BaseAgent):
    def route(self, kb_chunk: Dict) -> str:
        """Decides which specialist agent should handle the chunk."""
        # Simple keyword-based routing for speed and reliability
        content = kb_chunk['核心内容'] + kb_chunk['完整路径']
        
        finance_keywords = ["计算", "税费", "贷款", "首付", "利率", "金额", "比例", "公式", "年限"]
        legal_keywords = ["法律", "法规", "条例", "规定", "违法", "违规", "处罚", "责任", "纠纷"]
        
        score_finance = sum(1 for k in finance_keywords if k in content)
        score_legal = sum(1 for k in legal_keywords if k in content)
        
        if score_finance > 0 and score_finance >= score_legal:
            return "FinanceAgent"
        elif score_legal > 0:
            return "LegalAgent"
        else:
            return "GeneralAgent"

class SpecialistAgent(BaseAgent):
    def generate_draft(self, kb_chunk: Dict, examples: List[Dict]) -> Dict:
        raise NotImplementedError

class FinanceAgent(SpecialistAgent):
    def generate_draft(self, kb_chunk: Dict, examples: List[Dict]) -> Dict:
        prompt = self._build_prompt(kb_chunk, examples, "Finance Specialist")
        prompt += "\n\nIMPORTANT: Focus on numerical accuracy, calculation logic, and correct application of formulas/rates."
        response = self.call_llm(prompt, json_mode=True)
        return json.loads(response)

    def _build_prompt(self, kb_chunk, examples, role_name):
        # Shared prompt builder (simplified for brevity, can be customized per agent)
        prompt = f"""
# Role
You are the {role_name} for the Real Estate Exam.
Your goal is to create a high-quality exam question based *strictly* on the provided [Reference Material].

# Reference Material
【Path】: {kb_chunk['完整路径']}
【Content】:
{kb_chunk['核心内容']}

# Style Reference
"""
        for i, ex in enumerate(examples, 1):
            prompt += f"Example {i}: {ex['题干']}\n"
        
        prompt += """
# Task
Generate 1 single-choice question.
Return JSON: {"question": "...", "options": ["A", "B", "C", "D"], "answer": "A/B/C/D", "explanation": "..."}
"""
        return prompt

class LegalAgent(SpecialistAgent):
    def generate_draft(self, kb_chunk: Dict, examples: List[Dict]) -> Dict:
        prompt = self._build_prompt(kb_chunk, examples, "Legal Specialist")
        prompt += "\n\nIMPORTANT: Focus on precise legal terminology, specific regulations, and distinguishing between similar legal concepts."
        response = self.call_llm(prompt, json_mode=True)
        return json.loads(response)
    
    def _build_prompt(self, kb_chunk, examples, role_name):
        return FinanceAgent._build_prompt(self, kb_chunk, examples, role_name) # Reuse for now

class GeneralAgent(SpecialistAgent):
    def generate_draft(self, kb_chunk: Dict, examples: List[Dict]) -> Dict:
        prompt = self._build_prompt(kb_chunk, examples, "General Knowledge Specialist")
        response = self.call_llm(prompt, json_mode=True)
        return json.loads(response)

    def _build_prompt(self, kb_chunk, examples, role_name):
        return FinanceAgent._build_prompt(self, kb_chunk, examples, role_name)

class WriterAgent(BaseAgent):
    def finalize(self, draft: Dict, kb_chunk: Dict) -> Dict:
        """Formats the draft into the strict final JSON schema."""
        prompt = f"""
# Task
You are the Final Editor. Convert the draft question below into the strict output format.
Ensure the explanation follows the "1. Source 2. Analysis 3. Conclusion" structure.

# Draft
{json.dumps(draft, ensure_ascii=False)}

# Reference Material
{kb_chunk['核心内容']}

# Output Schema (JSON)
{{
    "题干": "...",
    "选项1": "...", "选项2": "...", "选项3": "...", "选项4": "...",
    "正确答案": "A/B/C/D",
    "解析": "1、教材原文... 2、试题分析... 3、结论...",
    "难度值": 0.5,
    "考点": "..."
}}
"""
        response = self.call_llm(prompt, json_mode=True)
        return json.loads(response)



class QuestionGenerator:
    def __init__(self, api_key, base_url, model):
        # Config is handled inside exam_graph via global config or passed in config
        self.model = model
        self.api_key = api_key

    def generate(self, kb_chunk, examples):
        # Backward compatibility wrapper
        result = None
        error = None
        for event in self.generate_events(kb_chunk, examples):
            # event is like {"writer": {"final_json": ...}}
            for node_name, state_update in event.items():
                if isinstance(state_update, dict):
                    if 'final_json' in state_update and state_update['final_json']:
                        result = state_update['final_json']
                    if 'type' in state_update and state_update['type'] == 'error':
                        error = state_update.get('message', 'Unknown error')
            
            # Support result event type
            if event.get('type') == 'result':
                result = event.get('data')
            if event.get('type') == 'error':
                error = event.get('message')

        return result, error

    def generate_events(self, kb_chunk, examples):
        """Yields state updates from LangGraph"""
        # Local import to avoid circular dependency
        from exam_graph import app as graph_app
        
        inputs = {
            "kb_chunk": kb_chunk,
            "examples": examples,
            "retry_count": 0,
            "logs": []
        }
        
        config = {"configurable": {"model": self.model, "api_key": self.api_key}}
        
        try:
            for output in graph_app.stream(inputs, config=config):
                # output is a dict where key is node name, value is state update
                for node_name, state_update in output.items():
                    print(f"DEBUG EVENT: {node_name} -> {state_update.keys()}")
                    if 'logs' in state_update:
                        print(f"LOGS: {state_update['logs']}")
                    # Yield the node name and the update for the UI
                    yield {node_name: state_update}
                    
                    # Also yield a 'result' type if we hit the end, for backward compatibility
                    if node_name == "fixer" or (node_name == "critic" and state_update.get("critic_feedback") == "PASS"):
                        if "final_json" in state_update:
                             yield {"type": "result", "data": state_update["final_json"]}

        except Exception as e:
            yield {"type": "error", "message": str(e)}

class Critic(BaseAgent):
    def verify(self, question_json, kb_chunk):
        prompt = f"""
# Task
Solve this question based ONLY on the text.

# Text
{kb_chunk['核心内容']}

# Question
{question_json['题干']}
A. {question_json['选项1']}
B. {question_json['选项2']}
C. {question_json['选项3']}
D. {question_json['选项4']}

Output: Just the letter (A/B/C/D) or INVALID.
"""
        try:
            response = self.call_llm(prompt)
            critic_answer = response.strip().upper().replace(".", "").replace(" ", "")
            gen_answer = question_json['正确答案'].strip().upper()
            
            if critic_answer == gen_answer:
                return True, "Pass"
            else:
                return False, f"Critic answered {critic_answer}, Generator said {gen_answer}"
        except Exception as e:
            return False, f"Critic Error: {e}"

# --- Main Execution ---
def main():
    print("=== Exam Factory (Multi-Agent) Starting ===")
    
    # Check API Key
    if not API_KEY or "请将您的Key粘贴在这里" in API_KEY:
        print("ERROR: API Key not found.")
        return

    # Initialize Components
    retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
    generator = QuestionGenerator(API_KEY, BASE_URL, MODEL_NAME)
    critic = Critic(API_KEY, BASE_URL, MODEL_NAME)
    
    # Test Generation
    chunk = retriever.get_random_kb_chunk()
    print(f"\nTopic: {chunk['完整路径'].split('>')[-1]}")
    examples = retriever.get_similar_examples(chunk['核心内容'])
    
    q_json, error = generator.generate(chunk, examples)
    if error:
        print(f"Error: {error}")
    else:
        print("Generated Question:")
        print(json.dumps(q_json, indent=2, ensure_ascii=False))
        
        valid, reason = critic.verify(q_json, chunk)
        print(f"Verification: {reason}")

if __name__ == "__main__":
    main()
