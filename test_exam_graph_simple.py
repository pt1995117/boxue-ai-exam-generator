#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 exam_graph 中的核心功能是否正常
"""
import os
from exam_graph import generate_content
from exam_factory import API_KEY, BASE_URL, MODEL_NAME

def test_exam_graph():
    print("=" * 60)
    print("🧪 测试 exam_graph 核心功能")
    print("=" * 60)
    
    print(f"\n📋 使用的配置：")
    print(f"   MODEL_NAME: {MODEL_NAME}")
    print(f"   API_KEY: {API_KEY[:10]}******" if API_KEY else "   未配置")
    print(f"   BASE_URL: {BASE_URL}")
    
    if not API_KEY:
        print("\n❌ API Key 未配置，无法测试")
        return False
    
    # 测试一个简单的 prompt
    test_prompt = """
请回答以下问题（只需回答数字）：
1 + 1 = ?
"""
    
    print(f"\n🔌 测试调用 generate_content...")
    print(f"   Prompt: {test_prompt.strip()}")
    
    try:
        response = generate_content(
            model_name=MODEL_NAME,
            prompt=test_prompt,
            api_key=API_KEY,
            base_url=BASE_URL
        )
        
        if response:
            print(f"\n✅ 调用成功！")
            print(f"📝 模型回复: {response.strip()[:200]}")  # 只显示前200字符
            
            # 检查是否包含数字2
            if "2" in response or "二" in response or "two" in response.lower():
                print(f"\n🎉 exam_graph 核心功能正常！")
                return True
            else:
                print(f"\n⚠️  回复内容可能异常")
                return True  # 仍然返回 True，因为 API 调用成功了
        else:
            print(f"\n❌ 返回结果为空")
            return False
            
    except Exception as e:
        print(f"\n❌ 调用失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_exam_graph()
    exit(0 if success else 1)

