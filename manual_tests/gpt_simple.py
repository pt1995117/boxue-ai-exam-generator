"""
Simple GPT-5.1 Connection Test
"""
import os

# Read config
config = {}
with open("填写您的Key.txt", 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()

gpt_key = config.get("CRITIC_API_KEY", "")
gpt_model = config.get("CRITIC_MODEL", "")

print(f"Testing GPT-5.1...")
print(f"Key: {gpt_key[:20]}...")
print(f"Model: {gpt_model}")

try:
    from openai import OpenAI
    client = OpenAI(api_key=gpt_key, base_url="https://api.openai.com/v1")
    
    response = client.chat.completions.create(
        model=gpt_model,
        messages=[{"role": "user", "content": "计算 80 * 1560 * 0.01，只返回数字"}],
        temperature=0,
        max_tokens=50
    )
    
    result = response.choices[0].message.content
    print(f"✅ Success! Response: {result}")
    
    with open("gpt_test_result.txt", "w") as f:
        f.write(f"Success!\nResponse: {result}")
    
except Exception as e:
    print(f"❌ Error: {e}")
    with open("gpt_test_result.txt", "w") as f:
        f.write(f"Error: {e}")
