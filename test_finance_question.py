#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试金融计算类题目的生成（FinanceAgent + 计算工具）
"""
import os
import json
from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
from exam_graph import app as graph_app

def test_finance_question():
    print("=" * 80)
    print("🧪 测试金融计算类题目生成")
    print("=" * 80)
    
    # 1. 加载配置
    print("\n[步骤 1] 📋 加载配置...")
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
    
    if not api_key:
        print("❌ 错误：未找到 API Key")
        return False
    
    print(f"   ✅ 配置加载完成")
    
    # 2. 初始化知识检索器
    print("\n[步骤 2] 📚 初始化知识库...")
    try:
        retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
        print(f"   ✅ 知识库加载成功")
    except Exception as e:
        print(f"   ❌ 知识库加载失败: {e}")
        return False
    
    # 3. 查找金融/计算类知识点
    print("\n[步骤 3] 🔍 查找金融/计算类知识点...")
    finance_keywords = ["税费", "贷款", "计算", "首付", "利率", "金额"]
    
    finance_chunk = None
    for chunk in retriever.kb_data:
        if chunk.get('核心内容'):
            content = chunk['核心内容'] + chunk['完整路径']
            if any(keyword in content for keyword in finance_keywords):
                finance_chunk = chunk
                break
    
    if not finance_chunk:
        print("   ⚠️  未找到金融类知识点，使用随机知识点")
        finance_chunk = retriever.get_random_kb_chunk()
    
    print(f"   ✅ 选中知识点: {finance_chunk['完整路径']}")
    print(f"   📝 内容预览: {finance_chunk['核心内容'][:150]}...")
    
    # 4. 配置并运行
    print("\n[步骤 4] 🚀 运行生成流程...")
    config = {
        "configurable": {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "retriever": retriever,
            "question_type": "单选题"
        }
    }
    
    inputs = {
        "kb_chunk": finance_chunk,
        "examples": [],
        "retry_count": 0,
        "logs": []
    }
    
    q_json = None
    tool_used = False
    
    try:
        for event in graph_app.stream(inputs, config=config):
            for node_name, state_update in event.items():
                print(f"\n   📍 {node_name}")
                
                if 'logs' in state_update:
                    for log in state_update['logs']:
                        if '工具' in log or '计算' in log or 'tool' in log.lower():
                            print(f"      ✨ {log}")
                        else:
                            print(f"      {log}")
                
                if node_name == "router" and 'router_details' in state_update:
                    details = state_update['router_details']
                    agent = details.get('agent', 'Unknown')
                    print(f"      ➡️  路由到: {agent}")
                
                if 'tool_usage' in state_update:
                    tool_info = state_update['tool_usage']
                    if tool_info.get('tool') and tool_info.get('tool') != 'None':
                        tool_used = True
                        print(f"      🧮 使用计算工具: {tool_info['tool']}")
                        print(f"         参数: {tool_info.get('params', {})}")
                        print(f"         结果: {tool_info.get('result', 'N/A')}")
                
                if 'final_json' in state_update:
                    q_json = state_update['final_json']
                    
                if node_name == "critic":
                    feedback = state_update.get('critic_feedback', 'Unknown')
                    if feedback == "PASS":
                        print(f"      ✅ 审核通过")
        
        print(f"\n   ✅ 流程完成")
        
    except Exception as e:
        print(f"\n   ❌ 流程失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # 5. 显示结果
    print("\n" + "=" * 80)
    print("📊 生成结果")
    print("=" * 80)
    
    if q_json:
        print("\n✅ 题目生成成功！")
        print(f"\n题干: {q_json.get('题干', 'N/A')}")
        print(f"\n选项:")
        for i in range(1, 5):
            opt = q_json.get(f'选项{i}', '')
            if opt:
                print(f"  {opt}")
        print(f"\n正确答案: {q_json.get('正确答案', 'N/A')}")
        print(f"难度值: {q_json.get('难度值', 'N/A')}")
        print(f"考点: {q_json.get('考点', 'N/A')}")
        
        if tool_used:
            print(f"\n🧮 使用了计算工具（金融题目）")
        else:
            print(f"\n💡 未使用计算工具（可能是概念类题目）")
        
        return True
    else:
        print("\n❌ 题目生成失败")
        return False

if __name__ == "__main__":
    success = test_finance_question()
    print("\n" + "=" * 80)
    if success:
        print("🎉 金融题目测试通过！")
    else:
        print("❌ 金融题目测试失败")
    print("=" * 80)
    exit(0 if success else 1)

