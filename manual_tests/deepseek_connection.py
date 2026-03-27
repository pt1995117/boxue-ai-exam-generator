import os
from openai import OpenAI

# 1. Load Config
config_path = "填写您的Key.txt"
api_key = ""
base_url = "https://openapi-ait.ke.com"
model = "deepseek-reasoner"

if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key == "OPENAI_API_KEY" and value and "请将您的Key粘贴在这里" not in value:
                    api_key = value
                elif key == "OPENAI_BASE_URL" and value:
                    base_url = value
                elif key == "OPENAI_MODEL" and value:
                    model = value

if not api_key:
    print("❌ 未找到 API Key，请检查配置文件。")
    exit(1)

print(f"✅ 找到 API Key: {api_key[:10]}******")
print(f"📍 Base URL: {base_url}")
print(f"🤖 Model: {model}")
print("=" * 60)

# 2. Test Connection
try:
    print("\n🧪 测试 API 连接...")
    client = OpenAI(api_key=api_key, base_url=base_url)
    
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是一个有用的助手。"},
            {"role": "user", "content": "请回答：1+1等于几？只用数字回答即可。"}
        ],
        temperature=0.3
    )
    
    answer = response.choices[0].message.content
    print(f"✅ 连接成功！")
    print(f"📝 模型回复: {answer}")
    
    print("\n" + "=" * 60)
    print("🎉 DeepSeek Reasoner 配置正确，可以正常使用！")
    
except Exception as e:
    print(f"\n❌ 连接失败: {e}")
    print("\n请检查：")
    print("1. API Key 是否正确")
    print("2. 网络连接是否正常")
    print("3. Base URL 是否正确 (应为 https://openapi-ait.ke.com)")
    exit(1)

