import os
import sys

print("=" * 60)
print("🧪 测试 exam_graph 模块...")
print("=" * 60)

# Test config loading
from exam_factory import API_KEY, BASE_URL, MODEL_NAME

if not API_KEY or API_KEY == "请将您的Key粘贴在这里":
    print("❌ API Key 未配置，请检查配置文件")
    sys.exit(1)

print(f"\n✅ 使用配置:")
print(f"   Model: {MODEL_NAME}")
print(f"   Base URL: {BASE_URL}")
print(f"   API Key: {API_KEY[:10]}******")

# Test generate_content function
print("\n" + "=" * 60)
print("🧪 测试 generate_content 函数...")
print("=" * 60)

from exam_graph import generate_content

test_prompt = "请用一句话回答：什么是房地产？"

print(f"\n📝 测试提示: {test_prompt}")
print("⏳ 正在调用模型...")

try:
    response = generate_content(
        model_name=MODEL_NAME,
        prompt=test_prompt,
        api_key=API_KEY
    )
    
    if response:
        print(f"\n✅ 生成成功！")
        print(f"📄 模型回复:\n{response[:200]}{'...' if len(response) > 200 else ''}")
        print("\n" + "=" * 60)
        print("🎉 exam_graph 模块工作正常！")
    else:
        print("\n❌ 模型返回空响应")
        
except Exception as e:
    print(f"\n❌ 生成失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("=" * 60)

