#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试配置加载是否正确
"""
import os
from exam_factory import API_KEY, BASE_URL, MODEL_NAME

def test_config_loading():
    print("=" * 60)
    print("🧪 测试配置加载")
    print("=" * 60)
    
    print(f"\n📋 从 exam_factory 加载的配置：")
    print(f"   API_KEY: {API_KEY[:10]}******" if API_KEY and len(API_KEY) > 10 else f"   API_KEY: {API_KEY}")
    print(f"   BASE_URL: {BASE_URL}")
    print(f"   MODEL_NAME: {MODEL_NAME}")
    
    # 验证配置
    expected_api_key = "sk-7a55cd2a6f9d4f6ab5a7badff99e979f"
    expected_base_url = "https://openapi-ait.ke.com"
    expected_model = "deepseek-reasoner"
    
    print(f"\n🔍 验证配置：")
    
    checks = []
    
    if API_KEY == expected_api_key:
        print(f"   ✅ API_KEY 正确")
        checks.append(True)
    else:
        print(f"   ❌ API_KEY 不匹配")
        print(f"      期望: {expected_api_key[:10]}******")
        print(f"      实际: {API_KEY[:10]}******" if API_KEY and len(API_KEY) > 10 else f"      实际: {API_KEY}")
        checks.append(False)
    
    if BASE_URL == expected_base_url:
        print(f"   ✅ BASE_URL 正确: {BASE_URL}")
        checks.append(True)
    else:
        print(f"   ❌ BASE_URL 不匹配")
        print(f"      期望: {expected_base_url}")
        print(f"      实际: {BASE_URL}")
        checks.append(False)
    
    if MODEL_NAME == expected_model:
        print(f"   ✅ MODEL_NAME 正确: {MODEL_NAME}")
        checks.append(True)
    else:
        print(f"   ❌ MODEL_NAME 不匹配")
        print(f"      期望: {expected_model}")
        print(f"      实际: {MODEL_NAME}")
        checks.append(False)
    
    if all(checks):
        print(f"\n🎉 所有配置加载正确！")
        return True
    else:
        print(f"\n⚠️  部分配置不匹配，请检查配置文件")
        return False

if __name__ == "__main__":
    success = test_config_loading()
    exit(0 if success else 1)
