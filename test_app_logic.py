import os
import json
import random
from exam_factory import KnowledgeRetriever, QuestionGenerator, Critic, ExamQuestion, KB_PATH, HISTORY_PATH
from pydantic import ValidationError

# Mock Streamlit functions
class MockStreamlit:
    def write(self, msg):
        print(f"   [UI] {msg}")
    def error(self, msg):
        print(f"   [UI ERROR] {msg}")
    def toast(self, msg, icon=""):
        print(f"   [UI TOAST] {icon} {msg}")

st = MockStreamlit()

def run_test():
    print("=== Starting Comprehensive Integration Test ===")
    
    # 1. Load Config
    config_path = "填写您的Key.txt"
    api_key = ""
    base_url = "https://api.deepseek.com"
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            for line in f:
                if "OPENAI_API_KEY=" in line and "请将您的Key粘贴在这里" not in line:
                    api_key = line.split("=", 1)[1].strip()
                if "OPENAI_BASE_URL=" in line and not line.strip().startswith("#"):
                    base_url = line.split("=", 1)[1].strip()
    
    if not api_key:
        print("❌ API Key not found")
        return

    # 2. Initialize
    print("1. Initializing Components...")
    retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
    generator = QuestionGenerator(api_key, base_url, "deepseek-reasoner")
    critic = Critic(api_key, base_url, "deepseek-reasoner")

    # 3. Select Chunk
    chunk = retriever.get_random_kb_chunk()
    print(f"2. Selected Topic: {chunk['完整路径'].split('>')[-1]}")

    # 4. Get Examples
    examples = retriever.get_similar_examples(chunk['核心内容'])
    print(f"3. Retrieved {len(examples)} examples")

    # 5. Generate with Events (Mimic App Loop)
    print("4. Running Generation Loop...")
    q_json = None
    error_msg = None
    
    for event in generator.generate_events(chunk, examples):
        if event['type'] == 'log':
            st.write(event['message'])
        elif event['type'] == 'result':
            q_json = event['data']
        elif event['type'] == 'error':
            error_msg = event['message']
    
    if error_msg:
        st.error(error_msg)
        return

    if q_json:
        print("5. Validating Result...")
        try:
            # Pydantic Validation
            ExamQuestion(**q_json)
            print("   ✅ Schema Validated")
            
            # Critic Verification
            st.write("🕵️ Critic: Verifying answer logic...")
            is_valid, reason = critic.verify(q_json, chunk)
            
            if is_valid:
                st.write("✅ Critic: Passed.")
                print("\n=== FINAL OUTPUT ===")
                print(json.dumps(q_json, indent=2, ensure_ascii=False))
            else:
                st.write(f"❌ Critic: Rejected. {reason}")
        except ValidationError as e:
            st.write(f"❌ Validation Error: {e}")

if __name__ == "__main__":
    run_test()
