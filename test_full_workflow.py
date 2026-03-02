#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整测试整个系统的题目生成流程
"""
import os
import json
from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
from exam_graph import app as graph_app

def test_full_workflow():
    print("=" * 80)
    print("🧪 完整系统测试 - 题目生成流程")
    print("=" * 80)
    
    # 1. 加载配置
    print("\n[步骤 1/6] 📋 加载配置...")
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
        print("❌ 错误：未找到 API Key")
        return False
    
    print(f"   ✅ API Key: {api_key[:10]}******")
    print(f"   ✅ Base URL: {base_url}")
    print(f"   ✅ Model: {model}")
    
    # 2. 初始化知识检索器
    print("\n[步骤 2/6] 📚 初始化知识库...")
    try:
        retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
        print(f"   ✅ 知识库加载成功 ({len(retriever.kb_data)} 条知识点)")
        print(f"   ✅ 历史题目加载成功 ({len(retriever.history_df)} 道题目)")
    except Exception as e:
        print(f"   ❌ 知识库加载失败: {e}")
        return False
    
    # 3. 选择一个知识点
    print("\n[步骤 3/6] 🎯 选择测试知识点...")
    try:
        chunk = retriever.get_random_kb_chunk()
        print(f"   ✅ 选中知识点: {chunk['完整路径']}")
        print(f"   📝 内容预览: {chunk['核心内容'][:100]}...")
    except Exception as e:
        print(f"   ❌ 选择知识点失败: {e}")
        return False
    
    # 4. 获取相似示例
    print("\n[步骤 4/6] 🔍 检索相似题目示例...")
    try:
        examples = retriever.get_similar_examples(chunk['核心内容'], k=3, question_type="单选题")
        print(f"   ✅ 找到 {len(examples)} 个相似示例")
        if examples:
            print(f"   📝 示例题目: {examples[0]['题干'][:50]}...")
    except Exception as e:
        print(f"   ⚠️  获取示例失败: {e}，继续使用空示例")
        examples = []
    
    # 5. 配置 LangGraph
    print("\n[步骤 5/6] ⚙️  配置 LangGraph 工作流...")
    config = {
        "configurable": {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "retriever": retriever,
            "question_type": "单选题"
        }
    }
    print("   ✅ 配置完成")
    
    # 6. 运行完整的生成流程
    print("\n[步骤 6/6] 🚀 运行完整生成流程...")
    print("   (这可能需要一些时间，请耐心等待...)")
    
    inputs = {
        "kb_chunk": chunk,
        "examples": [],
        "retry_count": 0,
        "logs": []
    }
    
    q_json = None
    node_count = 0
    
    try:
        for event in graph_app.stream(inputs, config=config):
            for node_name, state_update in event.items():
                node_count += 1
                print(f"\n   📍 节点: {node_name}")
                
                # 显示日志
                if 'logs' in state_update:
                    for log in state_update['logs']:
                        print(f"      {log}")
                
                # 显示路由决策
                if node_name == "router" and 'router_details' in state_update:
                    details = state_update['router_details']
                    print(f"      ➡️  路由决策: {details.get('agent', 'Unknown')}")
                
                # 显示工具使用
                if 'tool_usage' in state_update:
                    tool_info = state_update['tool_usage']
                    if tool_info.get('tool') and tool_info.get('tool') != 'None':
                        print(f"      🧮 使用工具: {tool_info['tool']}")
                
                # 检查最终结果
                if 'final_json' in state_update:
                    q_json = state_update['final_json']
                    
                # 显示审核结果
                if node_name == "critic":
                    feedback = state_update.get('critic_feedback', 'Unknown')
                    if feedback == "PASS":
                        print(f"      ✅ 审核通过")
                    else:
                        print(f"      ⚠️  审核反馈: {feedback[:50]}...")
        
        print(f"\n   ✅ 流程完成 (共经过 {node_count} 个节点)")
        
    except Exception as e:
        print(f"\n   ❌ 流程执行失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # 7. 验证结果
    print("\n" + "=" * 80)
    print("📊 生成结果验证")
    print("=" * 80)
    
    if q_json:
        print("\n✅ 题目生成成功！")
        print("\n生成的题目：")
        print(json.dumps(q_json, indent=2, ensure_ascii=False))
        
        # 验证必要字段
        required_fields = ['题干', '选项1', '选项2', '选项3', '选项4', '正确答案', '解析', '难度值']
        missing_fields = [f for f in required_fields if f not in q_json]
        
        if missing_fields:
            print(f"\n⚠️  缺少字段: {missing_fields}")
        else:
            print("\n✅ 所有必要字段都存在")
        
        return True
    else:
        print("\n❌ 题目生成失败，未获得最终结果")
        return False

if __name__ == "__main__":
    success = test_full_workflow()
    print("\n" + "=" * 80)
    if success:
        print("🎉 完整系统测试通过！")
    else:
        print("❌ 完整系统测试失败")
    print("=" * 80)
    exit(0 if success else 1)

