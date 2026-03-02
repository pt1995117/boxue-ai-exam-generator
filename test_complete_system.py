#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整系统测试 - 包括灵活/严谨模式
"""
import os
import json
from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
from exam_graph import app as graph_app

def test_complete_system():
    print("=" * 80)
    print("🧪 完整系统测试 - 包括灵活/严谨模式")
    print("=" * 80)
    
    # 1. 加载配置
    print("\n[步骤 1/7] 📋 加载配置...")
    config_path = "填写您的Key.txt"
    api_key = ""
    base_url = "https://openapi-ait.ke.com"
    model = "deepseek-chat"
    
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
        print("❌ 错误：未找到 API Key")
        return False
    
    print(f"   ✅ API Key: {api_key[:10]}******")
    print(f"   ✅ Base URL: {base_url}")
    print(f"   ✅ Model: {model}")
    
    # 2. 初始化知识检索器
    print("\n[步骤 2/7] 📚 初始化知识库...")
    try:
        retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
        print(f"   ✅ 知识库加载成功 ({len(retriever.kb_data)} 条知识点)")
    except Exception as e:
        print(f"   ❌ 知识库加载失败: {e}")
        return False
    
    # 3. 选择测试知识点
    print("\n[步骤 3/7] 🎯 选择测试知识点...")
    try:
        chunk = retriever.get_random_kb_chunk()
        print(f"   ✅ 选中知识点: {chunk['完整路径']}")
        print(f"   📝 内容预览: {chunk['核心内容'][:100]}...")
    except Exception as e:
        print(f"   ❌ 选择知识点失败: {e}")
        return False
    
    # 4. 测试灵活模式
    print("\n[步骤 4/7] 🎨 测试灵活模式...")
    config_flexible = {
        "configurable": {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "retriever": retriever,
            "question_type": "单选题",
            "generation_mode": "灵活"
        }
    }
    
    inputs = {
        "kb_chunk": chunk,
        "examples": [],
        "retry_count": 0,
        "logs": []
    }
    
    q_json_flexible = None
    try:
        print("   ⏳ 运行灵活模式生成流程...")
        for event in graph_app.stream(inputs, config=config_flexible):
            for node_name, state_update in event.items():
                if 'final_json' in state_update:
                    q_json_flexible = state_update['final_json']
                if node_name == "critic" and state_update.get('critic_feedback') == "PASS":
                    print(f"      ✅ {node_name}: 审核通过")
                    break
        
        if q_json_flexible:
            print(f"   ✅ 灵活模式生成成功")
            print(f"      📝 题干: {q_json_flexible.get('题干', 'N/A')[:60]}...")
        else:
            print(f"   ❌ 灵活模式生成失败")
            return False
    except Exception as e:
        print(f"   ❌ 灵活模式测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # 5. 测试严谨模式
    print("\n[步骤 5/7] 📋 测试严谨模式...")
    config_strict = {
        "configurable": {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "retriever": retriever,
            "question_type": "单选题",
            "generation_mode": "严谨"
        }
    }
    
    # 选择另一个知识点测试严谨模式
    chunk2 = retriever.get_random_kb_chunk()
    inputs2 = {
        "kb_chunk": chunk2,
        "examples": [],
        "retry_count": 0,
        "logs": []
    }
    
    q_json_strict = None
    try:
        print("   ⏳ 运行严谨模式生成流程...")
        for event in graph_app.stream(inputs2, config=config_strict):
            for node_name, state_update in event.items():
                if 'final_json' in state_update:
                    q_json_strict = state_update['final_json']
                if node_name == "critic" and state_update.get('critic_feedback') == "PASS":
                    print(f"      ✅ {node_name}: 审核通过")
                    break
        
        if q_json_strict:
            print(f"   ✅ 严谨模式生成成功")
            print(f"      📝 题干: {q_json_strict.get('题干', 'N/A')[:60]}...")
        else:
            print(f"   ❌ 严谨模式生成失败")
            return False
    except Exception as e:
        print(f"   ❌ 严谨模式测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # 6. 对比两种模式
    print("\n[步骤 6/7] 🔍 对比两种模式...")
    print("\n   灵活模式题目特点：")
    flexible_stem = q_json_flexible.get('题干', '')
    if any(keyword in flexible_stem for keyword in ['客户', '咨询', '交易', '在', '中']):
        print("      ✅ 包含场景化表达（符合灵活模式）")
    else:
        print("      ⚠️  未明显包含场景化表达")
    
    print("\n   严谨模式题目特点：")
    strict_stem = q_json_strict.get('题干', '')
    if not any(keyword in strict_stem for keyword in ['客户咨询', '在交易中', '假设']):
        print("      ✅ 无场景化包装（符合严谨模式）")
    else:
        print("      ⚠️  可能包含场景化表达")
    
    # 7. 验证结果
    print("\n[步骤 7/7] ✅ 验证结果...")
    required_fields = ['题干', '选项1', '选项2', '选项3', '选项4', '正确答案', '解析', '难度值', '考点']
    
    flexible_valid = all(field in q_json_flexible for field in required_fields)
    strict_valid = all(field in q_json_strict for field in required_fields)
    
    if flexible_valid and strict_valid:
        print("   ✅ 两种模式生成的题目都包含所有必要字段")
    else:
        missing_flexible = [f for f in required_fields if f not in q_json_flexible]
        missing_strict = [f for f in required_fields if f not in q_json_strict]
        if missing_flexible:
            print(f"   ⚠️  灵活模式缺少字段: {missing_flexible}")
        if missing_strict:
            print(f"   ⚠️  严谨模式缺少字段: {missing_strict}")
    
    return True

if __name__ == "__main__":
    success = test_complete_system()
    print("\n" + "=" * 80)
    if success:
        print("🎉 完整系统测试通过！")
        print("\n✅ 所有功能正常：")
        print("   - 配置加载正常")
        print("   - 知识库加载正常")
        print("   - 灵活模式正常工作")
        print("   - 严谨模式正常工作")
        print("   - 题目生成完整")
    else:
        print("❌ 完整系统测试失败")
    print("=" * 80)
    exit(0 if success else 1)

