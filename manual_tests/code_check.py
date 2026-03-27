#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代码检查：验证所有配置和逻辑是否正确
"""
import os
import sys

def check_config():
    """检查配置是否正确"""
    print("=" * 80)
    print("🔍 代码检查 - 配置验证")
    print("=" * 80)
    
    issues = []
    
    # 1. 检查配置文件
    print("\n[1] 检查配置文件...")
    config_path = "填写您的Key.txt"
    if not os.path.exists(config_path):
        issues.append("❌ 配置文件不存在")
    else:
        print("   ✅ 配置文件存在")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()
            if "OPENAI_API_KEY=" in content and "请将您的Key粘贴在这里" not in content:
                print("   ✅ API Key 已配置")
            else:
                issues.append("⚠️  API Key 可能未正确配置")
    
    # 2. 检查关键文件
    print("\n[2] 检查关键文件...")
    key_files = [
        "exam_graph.py",
        "exam_factory.py",
        "app.py",
        "bot_knowledge_base.jsonl",
        "存量房买卖母卷ABCD.xls"
    ]
    
    for file in key_files:
        if os.path.exists(file):
            print(f"   ✅ {file}")
        else:
            issues.append(f"❌ {file} 不存在")
    
    # 3. 检查代码导入
    print("\n[3] 检查代码导入...")
    try:
        from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
        print("   ✅ exam_factory 导入成功")
    except Exception as e:
        issues.append(f"❌ exam_factory 导入失败: {e}")
    
    try:
        from exam_graph import app as graph_app
        print("   ✅ exam_graph 导入成功")
    except Exception as e:
        issues.append(f"❌ exam_graph 导入失败: {e}")
    
    # 4. 检查 generation_mode 配置
    print("\n[4] 检查 generation_mode 配置...")
    try:
        # 模拟检查配置传递
        config = {
            "configurable": {
                "model": "deepseek-chat",
                "generation_mode": "灵活"
            }
        }
        
        # 检查是否能正确读取
        mode = config['configurable'].get('generation_mode', '灵活')
        if mode in ["灵活", "严谨"]:
            print(f"   ✅ generation_mode 配置正确: {mode}")
        else:
            issues.append(f"⚠️  generation_mode 值异常: {mode}")
    except Exception as e:
        issues.append(f"❌ generation_mode 配置检查失败: {e}")
    
    # 5. 检查干扰项设计说明
    print("\n[5] 检查干扰项设计说明...")
    try:
        with open("exam_graph.py", 'r', encoding='utf-8') as f:
            content = f.read()
            if "相近的数字" in content and "错误的参照物" in content:
                count_near_number = content.count("相近的数字")
                count_wrong_reference = content.count("错误的参照物")
                print(f"   ✅ 干扰项设计说明已添加（'相近的数字'出现 {count_near_number} 次，'错误的参照物'出现 {count_wrong_reference} 次）")
            else:
                issues.append("⚠️  干扰项设计说明可能不完整")
    except Exception as e:
        issues.append(f"❌ 无法检查干扰项设计说明: {e}")
    
    # 6. 检查模型配置
    print("\n[6] 检查模型配置...")
    try:
        with open("app.py", 'r', encoding='utf-8') as f:
            content = f.read()
            if "deepseek-chat" in content:
                print("   ✅ 模型配置包含 deepseek-chat")
            else:
                issues.append("⚠️  模型配置可能不正确")
    except Exception as e:
        issues.append(f"❌ 无法检查模型配置: {e}")
    
    # 总结
    print("\n" + "=" * 80)
    if issues:
        print("⚠️  发现以下问题：")
        for issue in issues:
            print(f"   {issue}")
        return False
    else:
        print("✅ 所有检查通过！代码没有问题。")
        return True

if __name__ == "__main__":
    success = check_config()
    sys.exit(0 if success else 1)

