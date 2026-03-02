"""
Test GPT-5.1 Critic Configuration
验证 GPT-5.1 是否正确配置到金融题目审核中
"""
import os

print("=" * 60)
print("GPT-5.1 Critic 配置测试")
print("=" * 60)

# Load configuration
config_path = "填写您的Key.txt"
config = {}

if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()

# Check configurations
print("\n【配置检查】")
print("-" * 60)

# DeepSeek (默认)
deepseek_key = config.get("OPENAI_API_KEY", "")
deepseek_url = config.get("OPENAI_BASE_URL", "")
deepseek_model = config.get("OPENAI_MODEL", "")

print(f"1. DeepSeek (生成端)")
print(f"   API Key: {deepseek_key[:20]}..." if deepseek_key else "   ❌ 未配置")
print(f"   Base URL: {deepseek_url}")
print(f"   Model: {deepseek_model}")

# GPT-5.1 (Critic)
gpt_key = config.get("CRITIC_API_KEY", "")
gpt_url = config.get("CRITIC_BASE_URL", "")
gpt_model = config.get("CRITIC_MODEL", "")

print(f"\n2. GPT-5.1 (审计端)")
if gpt_key:
    print(f"   ✅ API Key: {gpt_key[:20]}...")
    print(f"   Base URL: {gpt_url}")
    print(f"   Model: {gpt_model}")
else:
    print(f"   ⚠️  未单独配置，将使用默认模型（DeepSeek）")

# Import exam_factory to check if configs are loaded
print(f"\n【模块加载检查】")
print("-" * 60)

try:
    from exam_factory import (
        API_KEY, 
        BASE_URL, 
        MODEL_NAME,
        CRITIC_API_KEY,
        CRITIC_BASE_URL,
        CRITIC_MODEL
    )
    
    print("✅ exam_factory 配置加载成功")
    print(f"   默认模型: {MODEL_NAME}")
    print(f"   Critic 模型: {CRITIC_MODEL}")
    
    if CRITIC_API_KEY != API_KEY:
        print(f"\n✅ Critic 使用独立配置（GPT-5.1）")
    else:
        print(f"\n⚠️  Critic 使用默认配置（DeepSeek）")
        
except Exception as e:
    print(f"❌ 配置加载失败: {e}")
    import traceback
    traceback.print_exc()

# Test GPT-5.1 connection
print(f"\n【GPT-5.1 连接测试】")
print("-" * 60)

if gpt_key:
    try:
        from openai import OpenAI
        
        client = OpenAI(
            api_key=gpt_key,
            base_url=gpt_url
        )
        
        print("正在测试 GPT-5.1 API 连接...")
        
        response = client.chat.completions.create(
            model=gpt_model,
            messages=[
                {"role": "user", "content": "计算：建筑面积80平方米，成本价1560元/平方米，土地出让金=面积×成本价×1%。返回数字。"}
            ],
            temperature=0,
            max_tokens=50
        )
        
        result = response.choices[0].message.content
        print(f"✅ GPT-5.1 连接成功")
        print(f"   测试响应: {result}")
        
        # Verify calculation
        expected = 80 * 1560 * 0.01
        if "1248" in result:
            print(f"✅ 计算准确性验证通过 (预期: {expected})")
        else:
            print(f"⚠️  响应中未找到预期结果 {expected}")
            
    except Exception as e:
        print(f"❌ GPT-5.1 连接失败: {e}")
        import traceback
        traceback.print_exc()
else:
    print("⏭️  跳过连接测试（未配置 CRITIC_API_KEY）")

# Summary
print(f"\n【配置总结】")
print("=" * 60)

if gpt_key and gpt_key != deepseek_key:
    print("✅ 配置完成！架构：")
    print(f"   - 🤖 生成端: DeepSeek Reasoner (便宜高效)")
    print(f"   - 🔍 审计端: GPT-5.1 (金融题目专用，准确性最高)")
    print(f"\n成本估算（生成1000道金融题）:")
    print(f"   - DeepSeek: ¥3")
    print(f"   - GPT-5.1: ~¥50-80 (取决于 token 价格)")
    print(f"   - 总计: ~¥53-83")
else:
    print("⚠️  当前配置：全部使用 DeepSeek")
    print(f"   如需启用 GPT-5.1 审计，请在配置文件中填写 CRITIC_API_KEY")

print("=" * 60)
