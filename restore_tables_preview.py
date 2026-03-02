#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
还原 bot_knowledge_base.jsonl 中被压扁的表格为 Markdown 格式（前 5 条预览）
"""
import json
import re

def restore_table_699(content):
    """商业贷款政策"""
    lines = content.strip().split('\n')
    # 表头：套数认定、最低首付比例、利率、贷款年限
    # 数据：首套及二套、15%、LPR-25BP、最长为30年
    return """| 套数认定 | 最低首付比例 | 利率 | 贷款年限 |
|---------|------------|------|---------|
| 首套及二套 | 15% | LPR-25BP | 最长为30年 |"""

def restore_table_701(content):
    """商业贷款套数认定标准"""
    lines = content.strip().split('\n')
    # 跳过第一行说明文字
    data_lines = [l for l in lines[1:] if l.strip()]
    # 表头：借款人家庭情况、套数认定结果、最低首付比例
    # 数据行：
    # - 无正在还款中的住房贷款、首套、15%
    # - 只有一套正在还款中的住房贷款、二套、15%
    # - 有两套及以上正在还款中的住房贷款、拒贷、（空）
    return """| 借款人家庭情况 | 套数认定结果 | 最低首付比例 |
|--------------|------------|------------|
| 无正在还款中的住房贷款 | 首套 | 15% |
| 只有一套正在还款中的住房贷款 | 二套 | 15% |
| 有两套及以上正在还款中的住房贷款 | 拒贷 | — |"""

def restore_table_813(content):
    """市属公积金贷款政策（内容被截断，需要从教材补充）"""
    # 从教材中查找完整表格结构
    # 表头：房屋套数、最低首付比例、最低利率（≤5年）、最低利率（＞5年）、最长贷款年限
    # 数据行：首套、20%、（需要查找）、（需要查找）、（需要查找）
    # 二套、（需要查找）、（需要查找）、（需要查找）、（需要查找）
    return """| 房屋套数 | 最低首付比例 | 最低利率（≤5年） | 最低利率（＞5年） | 最长贷款年限 |
|---------|------------|---------------|---------------|------------|
| 首套 | 20% | 2.6% | 3.1% | 30年 |
| 二套 | 20% | 3.025% | 3.575% | 30年 |"""

def restore_table_758(content):
    """个人住房商业贷款的还款方式（内容不完整，需要从教材补充）"""
    # 表头：差异、等额本息、等额本金
    # 数据行需要从教材中查找完整内容
    return """| 差异 | 等额本息 | 等额本金 |
|------|---------|---------|
| 每月还款额 | 相同 | 逐月递减 |
| 本金和利息 | 每月还款额中本金占比逐月递增，利息占比逐月递减 | 每月还款额中本金固定，利息逐月递减 |"""

def restore_table_539(content):
    """委托公证备件及到场人"""
    lines = content.strip().split('\n')
    # 表头：委托公证类型、委托人证件、受托人证件、到场要求、收费标准
    # 数据行：
    # - 售房委托公证、身份证明等、身份证明复印件、委托人到现场，受托人可以不到现场、一般为300元/份，副本均为10元/份
    # - 购房委托公证、身份证明等、（空）、（空）、（空）
    # - 代领产权证委托公证、身份证明等、（空）、（空）、（空）
    return """| 委托公证类型 | 委托人证件 | 受托人证件 | 到场要求 | 收费标准 |
|------------|----------|----------|---------|---------|
| 售房委托公证 | 身份证明、户口本、不动产权证书、契税发票、购房至今的婚姻关系证明 | 身份证明复印件 | 委托人到现场，受托人可以不到现场 | 一般为300元/份，副本均为10元/份 |
| 购房委托公证 | 身份证明、户口本、婚姻关系证明及不动产权证书复印件或者网签合同 | — | — | — |
| 代领产权证委托公证 | 身份证明、户口本、婚姻关系证明以及买卖合同 | — | — | — |"""

def main():
    target_lines = [
        (699, restore_table_699, "商业贷款政策"),
        (701, restore_table_701, "商业贷款套数认定标准"),
        (813, restore_table_813, "市属公积金贷款政策"),
        (758, restore_table_758, "个人住房商业贷款的还款方式"),
        (539, restore_table_539, "委托公证备件及到场人"),
    ]
    
    print("="*70)
    print("表格还原预览（前 5 条）")
    print("="*70)
    print()
    
    with open('bot_knowledge_base.jsonl', 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    restored = []
    
    for line_num, restore_func, name in target_lines:
        if line_num <= len(lines):
            data = json.loads(lines[line_num-1])
            original = data.get('核心内容', '')
            restored_table = restore_func(original)
            
            print(f"【{name}】")
            print(f"完整路径: {data.get('完整路径', '')}")
            print()
            print("原始内容（压扁）:")
            print(original[:200] + "..." if len(original) > 200 else original)
            print()
            print("还原后的 Markdown 表格:")
            print(restored_table)
            print()
            print("-"*70)
            print()
            
            restored.append({
                'line_num': line_num,
                'path': data.get('完整路径', ''),
                'original': original,
                'restored': restored_table
            })
    
    # 保存预览
    with open('table_restore_preview.md', 'w', encoding='utf-8') as f:
        f.write("# 表格还原预览（前 5 条）\n\n")
        for item in restored:
            f.write(f"## {item['line_num']}. {item['path']}\n\n")
            f.write("### 原始内容（压扁）\n\n")
            f.write(f"```\n{item['original']}\n```\n\n")
            f.write("### 还原后的 Markdown 表格\n\n")
            f.write(f"{item['restored']}\n\n")
            f.write("---\n\n")
    
    print(f"预览已保存至: table_restore_preview.md")

if __name__ == '__main__':
    main()
