#!/usr/bin/env python3
"""
测试 LangGraph Loop 功能
"""
from exam_graph import app as graph_app
from exam_factory import KnowledgeRetriever

# 初始化
retriever = KnowledgeRetriever("bot_knowledge_base.jsonl", "存量房买卖母卷ABCD.xls")

# 获取一个知识点
kb_chunk = retriever.get_random_kb_chunk()

print("=" * 80)
print("测试知识点:", kb_chunk['完整路径'])
print("=" * 80)

# 模拟配置
config = {
    "configurable": {
        "model": "deepseek-reasoner",
        "api_key": "YOUR_API_KEY_HERE",  # 需要替换
        "retriever": retriever,
        "question_type": "单选题"
    }
}

# 初始状态
inputs = {
    "kb_chunk": kb_chunk,
    "examples": [],
    "retry_count": 0,
    "logs": []
}

print("\n开始执行 Graph...")
print("-" * 80)

# 执行 Graph
node_count = 0
for event in graph_app.stream(inputs, config):
    node_count += 1
    for node_name, state_update in event.items():
        print(f"\n[{node_count}] Node: {node_name}")
        
        # 打印关键信息
        if 'agent_name' in state_update:
            print(f"  → Agent: {state_update['agent_name']}")
        
        if 'critic_result' in state_update:
            result = state_update['critic_result']
            print(f"  → Critic Result: passed={result.get('passed')}, issue_type={result.get('issue_type')}")
        
        if 'retry_count' in state_update:
            print(f"  → Retry Count: {state_update['retry_count']}")
        
        if 'logs' in state_update:
            for log in state_update['logs']:
                print(f"  → Log: {log}")

print("\n" + "=" * 80)
print(f"总共执行了 {node_count} 个节点")
print("=" * 80)

# 检查是否有循环发生
if node_count > 6:  # Router + Specialist/Finance + Writer + Critic = 4-5 nodes
    print("✅ 检测到循环！节点数超过基础流程")
else:
    print("ℹ️  未检测到循环（可能题目一次通过）")
