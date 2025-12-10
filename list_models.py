import os
from google import genai

# 1. Load Key
config_path = "填写您的Key.txt"
gemini_key = ""
if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            if "GEMINI_API_KEY=" in line and "请将您的Key粘贴在这里" not in line:
                gemini_key = line.split("=", 1)[1].strip()

if not gemini_key:
    print("❌ 未找到 Gemini Key")
    exit(1)

print(f"✅ Key: {gemini_key[:5]}******")

# 2. List Models
try:
    client = genai.Client(api_key=gemini_key)
    print("正在获取可用模型列表...")
    # Pager object, iterate to get models
    for m in client.models.list():
        print(f"- {m.name} (Display: {m.display_name})")

except Exception as e:
    print(f"❌ 获取模型列表失败: {e}")
