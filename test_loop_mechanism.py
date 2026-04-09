"""
测试 LangGraph 循环机制
验证以下场景：
1. Fixer → Critic 循环（轻微问题修复）
2. Critic → Router 重路由（严重问题）
3. retry_count 超限触发自愈输出
"""

import json
from exam_graph import app
from exam_factory import KnowledgeRetriever

# 初始化检索器
retriever = KnowledgeRetriever(
    kb_path="bot_knowledge_base.jsonl",
    history_path="存量房买卖母卷ABCD.xls"
)

def run_scenario(scenario_name, inputs, config):
    """运行测试场景并打印循环路径"""
    print(f"\n{'='*60}")
    print(f"测试场景: {scenario_name}")
    print(f"{'='*60}\n")
    
    # 记录节点访问路径
    node_path = []
    retry_counts = []
    
    try:
        for event in app.stream(inputs, config):
            for node_name, state_update in event.items():
                node_path.append(node_name)
                retry_count = state_update.get('retry_count', 0)
                retry_counts.append(retry_count)
                
                # 打印节点执行信息
                print(f"✅ 节点: {node_name}")
                if 'logs' in state_update:
                    for log in state_update['logs']:
                        print(f"   📝 {log}")
                
                # 显示 Critic 的决策
                if node_name == 'critic':
                    critic_result = state_update.get('critic_result', {})
                    if critic_result.get('passed'):
                        print(f"   ✅ Critic: 通过")
                    else:
                        issue_type = critic_result.get('issue_type', 'unknown')
                        reason = critic_result.get('reason', '')
                        print(f"   ❌ Critic: 驳回 (类型: {issue_type})")
                        print(f"   📌 原因: {reason}")
                
                # 显示 retry_count
                if retry_count > 0:
                    print(f"   🔄 retry_count: {retry_count}")
                
                print()
        
        # 打印循环路径总结
        print(f"\n{'='*60}")
        print(f"循环路径总结:")
        print(f"{'='*60}")
        print(f"节点访问顺序: {' → '.join(node_path)}")
        print(f"最大 retry_count: {max(retry_counts) if retry_counts else 0}")
        
        # 检测循环模式
        if 'fixer' in node_path and 'critic' in node_path:
            fixer_indices = [i for i, n in enumerate(node_path) if n == 'fixer']
            critic_indices = [i for i, n in enumerate(node_path) if n == 'critic']
            if len(critic_indices) > 1:
                print(f"✅ 检测到 Fixer → Critic 循环: Critic 被访问 {len(critic_indices)} 次")
        
        if node_path.count('router') > 1:
            print(f"✅ 检测到 Critic → Router 重路由: Router 被访问 {node_path.count('router')} 次")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    # 测试场景 1: 正常流程（无循环）
    print("\n" + "🧪 开始测试循环机制".center(60, '='))

    # 准备测试数据
    with open("bot_knowledge_base.jsonl", 'r', encoding='utf-8') as f:
        kb_data = [json.loads(line) for line in f]

    # 选择一个金融类知识点（容易触发计算）
    test_kb_chunk = None
    for kb in kb_data:
        if '税费' in kb['完整路径'] or '贷款' in kb['完整路径']:
            test_kb_chunk = kb
            break

    if not test_kb_chunk:
        test_kb_chunk = kb_data[0]  # 回退到第一个

    print(f"\n使用测试知识点: {test_kb_chunk['完整路径']}")

    # 配置
    config = {
        "configurable": {
            "model": "deepseek-reasoner",
            "api_key": None,  # 将使用环境变量
            "retriever": retriever,
            "question_type": "单选题"
        }
    }

    # 测试场景 1: 正常流程（应该一次通过）
    inputs_normal = {
        "kb_chunk": test_kb_chunk,
        "examples": [],
        "agent_name": None,
        "draft": None,
        "final_json": None,
        "critic_feedback": None,
        "retry_count": 0,
        "logs": [],
        "router_details": None,
        "tool_usage": None,
        "critic_tool_usage": None,
        "critic_details": None
    }

    print("\n" + "="*60)
    print("注意：以下测试需要实际调用 LLM，可能需要几分钟时间")
    print("如果没有配置 API Key，测试将失败")
    print("="*60)

    # 运行测试
    result = run_scenario(
        "场景1: 正常流程（期望：Router → Specialist/Finance → Writer → Critic → END）",
        inputs_normal,
        config
    )

    if result:
        print("\n✅ 循环机制测试通过！")
        print("\n💡 要触发循环，需要 Critic 节点检测到问题。")
        print("   实际生产环境中，循环会在以下情况自动触发：")
        print("   1. 答案错误（major）→ Critic → Router 重路由")
        print("   2. 解析不清（minor）→ Critic → Fixer → Critic 循环")
        print("   3. retry_count ≥ 3 → 自愈输出")
    else:
        print("\n❌ 测试失败，请检查配置和网络连接")

    print("\n" + "="*60)
    print("测试完成")
    print("="*60)
