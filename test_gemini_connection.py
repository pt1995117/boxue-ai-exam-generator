import os
import json
from exam_factory import KnowledgeRetriever, QuestionGenerator

# 1. Load Key
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
    print("❌ 未找到 API Key，请检查配置文件。")
    exit(1)

print(f"✅ 找到 Key: {api_key[:5]}******")

# 2. Initialize Components
try:
    print("1. 初始化检索器...")
    retriever = KnowledgeRetriever("bot_knowledge_base.jsonl", "存量房买卖母卷ABCD.xls")
    
    print("2. 初始化生成器 (使用 deepseek-reasoner)...")
    # We manually set the proxy if needed, but here we assume user env or direct connection
    # If you need a proxy, uncomment and set:
    # os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
    
    generator = QuestionGenerator(api_key=api_key, base_url=base_url, model="deepseek-reasoner")
    
    # 3. Pick a chunk
    chunk = retriever.get_random_kb_chunk()
    print(f"   选中知识点: {chunk['完整路径'].split('>')[-1]}")
    
    # 4. Generate with Events
    print("3. 正在尝试生成题目 (Event Stream)...")
    examples = retriever.get_similar_examples(chunk['核心内容'])
    
    q_json = None
    for event in generator.generate_events(chunk, examples):
        if event['type'] == 'log':
            print(f"   [LOG] {event['message']}")
        elif event['type'] == 'result':
            q_json = event['data']
        elif event['type'] == 'error':
            print(f"❌ Error Event: {event['message']}")

    if not q_json:
        print("❌ 生成失败 (No Result)")
    else:
        print("✅ 生成成功！")
        print(json.dumps(q_json, indent=2, ensure_ascii=False))

except Exception as e:
    print(f"❌ 发生异常: {e}")
