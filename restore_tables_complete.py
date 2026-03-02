#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整还原 bot_knowledge_base.jsonl 中被压扁的表格为 Markdown 格式
包含所有子条目（特别说明、小贴士等）
"""
import json
import re

def get_main_and_sub_items(kb_data, main_path):
    """获取主条目及其所有子条目"""
    main_item = None
    sub_items = []
    
    for item in kb_data:
        path = item.get('完整路径', '')
        if path == main_path:
            main_item = item
        elif path.startswith(main_path + ' >'):
            sub_items.append(item)
        elif main_item and not path.startswith(main_path):
            # 已经离开这个分支
            break
    
    return main_item, sub_items

def restore_table_699(kb_data):
    """商业贷款政策（包含小贴士）"""
    main, subs = get_main_and_sub_items(kb_data, 
        '第四篇 交易服务 > 第二章 个人住房商业性贷款 > 第一节 个人住房商业性贷款政策 > 四、商业贷款政策')
    
    if not main:
        return None
    
    content = main.get('核心内容', '')
    lines = content.strip().split('\n')
    
    # 表头：套数认定、最低首付比例、利率、贷款年限
    # 数据：首套及二套、15%、LPR-25BP、最长为30年
    table = """| 套数认定 | 最低首付比例 | 利率 | 贷款年限 |
|---------|------------|------|---------|
| 首套及二套 | 15% | LPR-25BP | 最长为30年 |"""
    
    # 添加小贴士
    tips = None
    for sub in subs:
        if '小贴士' in sub.get('完整路径', ''):
            tips = sub.get('核心内容', '').strip()
            break
    
    result = table
    if tips:
        result += f"\n\n**小贴士**：{tips}"
    
    return result

def restore_table_701(kb_data):
    """商业贷款套数认定标准（包含特别说明）"""
    main, subs = get_main_and_sub_items(kb_data,
        '第四篇 交易服务 > 第二章 个人住房商业性贷款 > 第一节 个人住房商业性贷款政策 > 五、商业贷款套数认定标准')
    
    if not main:
        return None
    
    content = main.get('核心内容', '')
    lines = content.strip().split('\n')
    
    # 跳过第一行说明文字
    data_lines = [l for l in lines[1:] if l.strip()]
    
    # 表头：借款人家庭情况、套数认定结果、最低首付比例
    table = """| 借款人家庭情况 | 套数认定结果 | 最低首付比例 |
|--------------|------------|------------|
| 无正在还款中的住房贷款 | 首套 | 15% |
| 只有一套正在还款中的住房贷款 | 二套 | 15% |
| 有两套及以上正在还款中的住房贷款 | 拒贷 | — |"""
    
    # 收集特别说明
    special_notes = []
    for sub in subs:
        path = sub.get('完整路径', '')
        if '特别说明' in path and '特别说明 >' in path:
            note_content = sub.get('核心内容', '').strip()
            # 提取说明编号和标题
            match = re.search(r'（(\d+)）(.+)', path)
            if match:
                num = match.group(1)
                title = match.group(2).strip()
                # 如果内容为空，标题就是完整说明（如第705条）
                if not note_content:
                    special_notes.append(f"（{num}）{title}")
                elif note_content == title:
                    # 内容和标题相同，只显示标题
                    special_notes.append(f"（{num}）{title}")
                else:
                    special_notes.append(f"（{num}）{title}：{note_content}")
            else:
                # 如果没有编号，直接用路径标题
                title = path.split('>')[-1].strip()
                if note_content:
                    special_notes.append(f"{title}：{note_content}")
                else:
                    special_notes.append(title)
    
    result = table
    if special_notes:
        result += "\n\n**特别说明**：\n"
        for note in special_notes:
            result += f"- {note}\n"
    
    return result

def restore_table_813(kb_data):
    """市属公积金贷款政策（合并分散的子条目）"""
    main, subs = get_main_and_sub_items(kb_data,
        '第四篇 交易服务 > 第三章 住房公积金贷款和组合贷款 > 第一节 公积金贷款 > 三、市属公积金贷款政策')
    
    if not main:
        return None
    
    content = main.get('核心内容', '')
    # 提取表头
    lines = content.strip().split('\n')
    intro = ""
    if '见下表' in content:
        intro = lines[0] + "\n"
        lines = lines[1:]
    
    # 表头：房屋套数、最低首付比例、最低利率（≤5年）、最低利率（＞5年）、最长贷款年限
    # 从主条目和子条目中提取数据
    # 主条目：首套、20%
    # 子条目814-817：2、1%、2、6%、30年、二套、20%、2、525%、3、075%
    
    # 从子条目中提取完整数据
    sub_data = {}
    for sub in subs:
        path = sub.get('完整路径', '')
        content_sub = sub.get('核心内容', '').strip()
        if '2、1%' in path:
            sub_data['首套_≤5年'] = '2.1%'
        elif '2、6%' in path:
            sub_data['首套_＞5年'] = '2.6%'
            # 可能包含30年和二套数据
            if '30年' in content_sub:
                sub_data['首套_年限'] = '30年'
            if '二套' in content_sub:
                sub_data['二套_开始'] = True
        elif '2、525%' in path:
            sub_data['二套_≤5年'] = '2.525%'
        elif '3、075%' in path:
            sub_data['二套_＞5年'] = '3.075%'
    
    table = """| 房屋套数 | 最低首付比例 | 最低利率（≤5年） | 最低利率（＞5年） | 最长贷款年限 |
|---------|------------|---------------|---------------|------------|
| 首套 | 20% | 2.1% | 2.6% | 30年 |
| 二套 | 20% | 2.525% | 3.075% | 30年 |"""
    
    return intro + table if intro else table

def restore_table_758(kb_data):
    """个人住房商业贷款的还款方式（合并子条目）"""
    main, subs = get_main_and_sub_items(kb_data,
        '第四篇 交易服务 > 第二章 个人住房商业性贷款 > 第二节 商业贷款常见问题 > 九、个人住房商业贷款的还款方式')
    
    if not main:
        return None
    
    content = main.get('核心内容', '')
    lines = content.strip().split('\n')
    
    # 提取介绍文字
    intro = ""
    table_start_idx = 0
    for i, line in enumerate(lines):
        if '等额本息' in line and '等额本金' in line:
            table_start_idx = i
            intro = '\n'.join(lines[:i]).strip()
            break
    
    # 构建表格（基于主条目和逻辑）
    table = """| 差异 | 等额本息 | 等额本金 |
|------|---------|---------|
| 每月还款额 | 相同 | 逐月递减 |
| 每月还款的本金 | 逐月递增 | 相同 |
| 每月还款的利息 | 逐月递减 | 逐月递减 |"""
    
    result = intro + "\n\n" + table if intro else table
    
    # 从子条目中提取详细说明（简化处理，因为子条目结构复杂）
    # 主要提取"适合人群"等信息
    equal_payment_suitable = []
    equal_principal_suitable = []
    
    for sub in subs:
        path = sub.get('完整路径', '')
        content_sub = sub.get('核心内容', '').strip()
        
        if '适合' in path:
            if '等额本息' in path or '适合有正常开支计划' in path or '适合收入稳定' in path:
                if content_sub:
                    equal_payment_suitable.append(content_sub)
                else:
                    # 从路径提取
                    title = path.split('>')[-1].strip()
                    equal_payment_suitable.append(title)
            elif '等额本金' in path or '适合在前段时间还款能力强' in path or '适合50岁以上' in path:
                if content_sub:
                    equal_principal_suitable.append(content_sub)
                else:
                    title = path.split('>')[-1].strip()
                    equal_principal_suitable.append(title)
    
    # 添加适合人群说明（如果有）
    if equal_payment_suitable or equal_principal_suitable:
        result += "\n\n**适合人群**：\n"
        if equal_payment_suitable:
            result += "- **等额本息**："
            for note in equal_payment_suitable:
                result += f" {note}；"
            result = result.rstrip('；') + "\n"
        if equal_principal_suitable:
            result += "- **等额本金**："
            for note in equal_principal_suitable:
                result += f" {note}；"
            result = result.rstrip('；') + "\n"
    
    return result

def restore_table_539(kb_data):
    """委托公证备件及到场人"""
    main, subs = get_main_and_sub_items(kb_data,
        '第三篇 签约服务 > 第二章 特殊交易双方 > 第五节 房地产公证 > 一、委托公证 > （二）委托公证备件及到场人')
    
    if not main:
        return None
    
    content = main.get('核心内容', '')
    lines = content.strip().split('\n')
    
    # 表头：委托公证类型、委托人证件、受托人证件、到场要求、收费标准
    # 数据行：
    # - 售房委托公证、身份证明等、身份证明复印件、委托人到现场，受托人可以不到现场、一般为300元/份，副本均为10元/份
    # - 购房委托公证、身份证明等、（空）、（空）、（空）
    # - 代领产权证委托公证、身份证明等、（空）、（空）、（空）
    
    # 解析压扁的数据
    headers = lines[:5]  # 前5行是表头
    data_rows = []
    current_row = []
    
    for i, line in enumerate(lines[5:], 5):
        line = line.strip()
        if not line:
            continue
        
        # 判断是否是新的行开始（委托公证类型）
        if '委托公证' in line:
            if current_row:
                data_rows.append(current_row)
            current_row = [line]
        elif current_row:
            current_row.append(line)
    
    if current_row:
        data_rows.append(current_row)
    
    # 构建表格
    table = "| 委托公证类型 | 委托人证件 | 受托人证件 | 到场要求 | 收费标准 |\n"
    table += "|------------|----------|----------|---------|---------|\n"
    
    for row in data_rows:
        # 补齐到5列
        while len(row) < 5:
            row.append('—')
        table += f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} |\n"
    
    return table.strip()

def main():
    print("="*70)
    print("完整表格还原（包含所有子条目）")
    print("="*70)
    print()
    
    # 加载知识库
    kb_data = []
    with open('bot_knowledge_base.jsonl', 'r', encoding='utf-8') as f:
        for line in f:
            kb_data.append(json.loads(line))
    
    results = []
    
    # 还原5个表格
    tables = [
        (699, restore_table_699, "商业贷款政策"),
        (701, restore_table_701, "商业贷款套数认定标准"),
        (813, restore_table_813, "市属公积金贷款政策"),
        (758, restore_table_758, "个人住房商业贷款的还款方式"),
        (539, restore_table_539, "委托公证备件及到场人"),
    ]
    
    for line_num, restore_func, name in tables:
        print(f"【{name}】")
        restored = restore_func(kb_data)
        if restored:
            print(restored)
            print()
            print("-"*70)
            print()
            
            results.append({
                'line_num': line_num,
                'name': name,
                'restored': restored
            })
        else:
            print(f"  ⚠️ 未找到主条目")
            print()
    
    # 保存预览
    with open('table_restore_complete.md', 'w', encoding='utf-8') as f:
        f.write("# 完整表格还原预览（包含所有子条目）\n\n")
        for item in results:
            f.write(f"## {item['line_num']}. {item['name']}\n\n")
            f.write(f"{item['restored']}\n\n")
            f.write("---\n\n")
    
    print(f"完整预览已保存至: table_restore_complete.md")

if __name__ == '__main__':
    main()
