#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速测试 DeepSeek Reasoner 配置是否正确
"""
import os
from openai import OpenAI

def test_deepseek_config():
    print("=" * 60)
    print("🧪 测试 DeepSeek Reasoner 配置")
    print("=" * 60)
    
    # 1. 读取配置文件
    config_path = "填写您的Key.txt"
    api_key = ""
    base_url = "https://api.deepseek.com"
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
    
    # 2. 验证配置
    print(f"\n📋 配置信息：")
    print(f"   API Key: {api_key[:10]}******" if api_key else "   ❌ API Key 未找到")
    print(f"   Base URL: {base_url}")
    print(f"   Model: {model}")
    
    if not api_key:
        print("\n❌ 错误：未找到 API Key，请检查配置文件")
        return False
    
    # 3. 测试 API 连接
    print(f"\n🔌 测试 API 连接...")
    try:
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
        print(f"✅ API 连接成功！")
        print(f"📝 模型回复: {answer.strip()}")
        
        # 4. 验证答案是否正确
        if "2" in answer or "二" in answer:
            print(f"\n🎉 配置完全正确！DeepSeek Reasoner 可以正常使用！")
            return True
        else:
            print(f"\n⚠️  API 连接成功，但回复异常：{answer}")
            return False
            
    except Exception as e:
        print(f"\n❌ API 连接失败: {e}")
        print("\n请检查：")
        print("1. API Key 是否正确")
        print("2. 网络连接是否正常")
        print("3. Base URL 是否正确 (应为 https://api.deepseek.com)")
        return False

if __name__ == "__main__":
    success = test_deepseek_config()
    exit(0 if success else 1)

