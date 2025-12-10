import os
import json
import random
from exam_factory import KnowledgeRetriever, ExamQuestion, KB_PATH, HISTORY_PATH
from exam_graph import app as graph_app
from pydantic import ValidationError

def run_test():
    print("=== Starting LangGraph Integration Test ===")
    
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

    # 2. Initialize Retriever
    print("1. Initializing Retriever...")
    retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)

    # 3. Select Chunk
    chunk = retriever.get_random_kb_chunk()
    print(f"2. Selected Topic: {chunk['完整路径'].split('>')[-1]}")

    # 4. Get Examples
    examples = retriever.get_similar_examples(chunk['核心内容'])
    print(f"3. Retrieved {len(examples)} examples")

    # 5. Run Graph
    print("4. Running LangGraph...")
    
    inputs = {
        "kb_chunk": chunk, 
        "examples": examples, 
        "retry_count": 0,
        "logs": []
    }
    
    config = {"configurable": {"model": "deepseek-reasoner", "api_key": api_key, "base_url": base_url}}
    
    q_json = None
    
    try:
        for event in graph_app.stream(inputs, config=config):
            for node_name, state_update in event.items():
                print(f"   [Node: {node_name}]")
                if 'logs' in state_update:
                    for log in state_update['logs']:
                        print(f"     {log}")
                
                if 'final_json' in state_update:
                    q_json = state_update['final_json']
                    
    except Exception as e:
        print(f"❌ Graph Error: {e}")
        return

    if q_json:
        print("5. Validating Result...")
        try:
            # Pydantic Validation
            ExamQuestion(**q_json)
            print("   ✅ Schema Validated")
            print("\n=== FINAL OUTPUT ===")
            print(json.dumps(q_json, indent=2, ensure_ascii=False))
        except ValidationError as e:
            print(f"❌ Validation Error: {e}")
    else:
        print("❌ No result generated.")

if __name__ == "__main__":
    run_test()
