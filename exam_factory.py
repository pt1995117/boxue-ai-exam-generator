import os
import json
import pandas as pd
import random
from typing import List, Dict, Optional, Tuple, Any
from pydantic import BaseModel, Field, ValidationError
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from openai import OpenAI
from google import genai
from dotenv import load_dotenv

# Load environment variables
config_path = "填写您的Key.txt"
config = {}
if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()

# Fallback to .env or system env
API_KEY = config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
GEMINI_KEY = config.get("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
BASE_URL = config.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
MODEL_NAME = config.get("OPENAI_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-reasoner")

# Paths
KB_PATH = "bot_knowledge_base.jsonl"
HISTORY_PATH = "存量房买卖母卷ABCD.xls"
OUTPUT_PATH = "generated_exam_questions.xlsx"

# --- Pydantic Models ---
class ExamQuestion(BaseModel):
    题干: str = Field(..., description="The question stem")
    选项1: str = Field(..., description="Option A")
    选项2: str = Field(..., description="Option B")
    选项3: str = Field(..., description="Option C")
    选项4: str = Field(..., description="Option D")
    正确答案: str = Field(..., pattern="^[ABCD]$", description="Correct answer (A/B/C/D)")
    解析: str = Field(..., description="Structured explanation: 1. Source 2. Analysis 3. Conclusion")
    难度值: float = Field(..., ge=0, le=1, description="Difficulty 0.0-1.0")
    考点: str = Field(..., description="The specific knowledge point tested")

# --- Knowledge Retriever (Unchanged) ---
class KnowledgeRetriever:
    def __init__(self, kb_path, history_path):
        print("Loading Knowledge Base...")
        self.kb_data = []
        with open(kb_path, 'r', encoding='utf-8') as f:
            for line in f:
                self.kb_data.append(json.loads(line))
        
        print("Loading Historical Questions...")
        self.history_df = pd.read_excel(history_path)
        
        print("Indexing Historical Questions...")
        self.vectorizer = TfidfVectorizer()
        self.history_corpus = (self.history_df['题干'].astype(str) + " " + self.history_df['考点'].astype(str)).tolist()
        self.tfidf_matrix = self.vectorizer.fit_transform(self.history_corpus)
        
        # Load question-knowledge mapping
        print("Loading Question-Knowledge Mapping...")
        self.kb_to_questions = {}  # Reverse index: kb_path -> [question_indices]
        try:
            with open('question_knowledge_mapping.json', 'r', encoding='utf-8') as f:
                self.mapping = json.load(f)
                # Build reverse index
                for q_idx_str, mapping_info in self.mapping.items():
                    q_idx = int(q_idx_str)
                    kb_path = mapping_info['matched_kb_path']
                    if kb_path not in self.kb_to_questions:
                        self.kb_to_questions[kb_path] = []
                    self.kb_to_questions[kb_path].append(q_idx)
                print(f"Mapped {len(self.mapping)} questions to {len(self.kb_to_questions)} KB paths")
        except FileNotFoundError:
            print("Warning: question_knowledge_mapping.json not found. Using TF-IDF fallback only.")
            self.mapping = {}
            self.kb_to_questions = {}

    def get_random_kb_chunk(self):
        valid_chunks = [c for c in self.kb_data if c['核心内容'] and "（章节标题" not in c['Bot专用切片']]
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
        return self._get_question_type(row) == question_type

    def get_similar_examples(self, query_text, k=3, question_type=None):
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
            if question_type and not self._matches_question_type(row, question_type):
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
    
    def get_examples_by_knowledge_point(self, kb_chunk, k=3, question_type=None):
        """Get examples that match this knowledge point."""
        path = kb_chunk['完整路径']
        
        # Find questions mapped to this KB path
        if path in self.kb_to_questions:
            indices = self.kb_to_questions[path]
            # Try to get more candidates to account for filtering
            max_tries = min(len(indices), k * 5)  # Increased from k*3 to k*5 for type filtering
            candidates = random.sample(indices, max_tries)
            
            examples = []
            for idx in candidates:
                if len(examples) >= k:
                    break
                row = self.history_df.iloc[idx]
                # Skip invalid examples
                if not self._is_valid_example(row):
                    continue
                # Skip if question type doesn't match
                if question_type and not self._matches_question_type(row, question_type):
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
            # Fallback to TF-IDF if no mapping found
            return self.get_similar_examples(kb_chunk['核心内容'], k, question_type)

# --- Multi-Agent System ---

class BaseAgent:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.model_name = model
        self.is_gemini = "gemini" in model.lower() or "flash" in model.lower()
        
        if self.is_gemini:
            key_to_use = api_key or GEMINI_KEY
            if not key_to_use:
                 raise ValueError("Gemini API Key is missing.")
            self.client = genai.Client(api_key=key_to_use)
        else:
            if not api_key:
                raise ValueError("OpenAI API Key is missing.")
            self.client = OpenAI(api_key=api_key, base_url=base_url)

    def call_llm(self, prompt: str, json_mode: bool = False) -> str:
        try:
            if self.is_gemini:
                config = {"response_mime_type": "application/json"} if json_mode else None
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=config
                )
                return response.text
            else:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"} if json_mode else None,
                    temperature=0.3
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
            if 'final_json' in event:
                result = event['final_json']
            if 'error' in event: # Custom error handling if needed
                pass
        return result, error

    def generate_events(self, kb_chunk, examples):
        """Yields state updates from LangGraph"""
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
    if (not API_KEY or "请将您的Key粘贴在这里" in API_KEY) and (not GEMINI_KEY or "请将您的Key粘贴在这里" in GEMINI_KEY):
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
