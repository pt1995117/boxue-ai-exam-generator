#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
计算节点微测：动态代码/工具执行率 (Dynamic Code Audit)
针对 CalculatorNode：只检验「计划步骤」输出的 (tool, params) 在 RealEstateCalculator 中能否跑通。
目标 KPI：执行成功率 > 95%；若低于 95% 则按 幻觉/语法/逻辑 归因。
"""
import os
import sys

# 计算相关关键词，用于从知识库筛选用以跑测的 chunk
CALC_KEYWORDS = [
    '契税', '土地出让金', '房龄', '贷款', '增值税', '建筑面积', '容积率',
    '得房率', '价差率', '土地年限', '经适房', '公房', '土地出让', '公积金',
    '商业贷款', '层高', '净高', '面积误差', '绿化率', '评估价', '成本价',
]

def get_calculation_chunks(kb_data, limit=20):
    out = []
    seen_paths = set()
    for c in kb_data:
        path = c.get('完整路径', '') or ''
        content = (c.get('核心内容', '') or '') + path
        if not c.get('核心内容') or '（章节标题' in c.get('Bot专用切片', ''):
            continue
        if path in seen_paths:
            continue
        if any(k in content for k in CALC_KEYWORDS):
            seen_paths.add(path)
            out.append(c)
            if len(out) >= limit:
                break
    return out

def main():
    print("="*70)
    print("计算节点微测：工具执行率 (CalculatorNode Plan → RealEstateCalculator)")
    print("="*70)

    # 配置
    config = {}
    cfg_path = os.path.join(os.path.dirname(__file__) or '.', '填写您的Key.txt')
    if os.path.isfile(cfg_path):
        with open(cfg_path, 'r', encoding='utf-8') as f:
            for line in f:
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    config[k.strip()] = v.strip()

    from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
    from exam_graph import generate_content, parse_json_from_response, CALCULATION_GUIDE
    from calculation_logic import RealEstateCalculator

    model = config.get('OPENAI_MODEL', 'deepseek-reasoner')
    api_key = config.get('OPENAI_API_KEY', '')
    base_url = config.get('OPENAI_BASE_URL', 'https://openapi-ait.ke.com')

    retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
    limit = 20
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            pass
    chunks = get_calculation_chunks(retriever.kb_data, limit=limit)

    print(f"参与跑测的计算相关知识点数量: {len(chunks)} (限 cap={limit}，可传参: python run_calculation_execution_rate.py 5)")
    print()

    success = 0
    failed = []  # ( path, tool, params, error_type, message )
    skipped = 0  # need_calculation=False 或 tool=None

    for i, c in enumerate(chunks):
        path = (c.get('完整路径', '') or '')[:60]
        mastery = c.get('掌握程度', '未知')

        prompt = f"""# 角色
你是计算专家。请根据【参考材料】判断是否需要调用计算函数；若需要，选出函数名并从材料中提取**具体数值**作为 params。

# 参考材料
{c.get('核心内容','')}

{CALCULATION_GUIDE}

# 输出 JSON（仅此）
{{"need_calculation": true/false, "tool": "函数名或None", "params": {{"key": 数值}}, "reason": "一句话"}}
"""

        try:
            raw = generate_content(model, prompt, api_key, base_url, None)
            if not raw or not raw.strip():
                failed.append((path, None, None, "Empty", "LLM 返回空"))
                continue
            plan = parse_json_from_response(raw)
        except Exception as e:
            failed.append((path, None, None, "Parse", str(e)))
            continue

        need = plan.get('need_calculation', False)
        tool = plan.get('tool') or 'None'
        params = plan.get('params') or {}

        if not need or not tool or tool == 'None':
            skipped += 1
            continue

        if not hasattr(RealEstateCalculator, tool):
            failed.append((path, tool, params, "幻觉", f"Tool not found: {tool}"))
            continue

        func = getattr(RealEstateCalculator, tool)
        try:
            func(**params)
            success += 1
        except TypeError as e:
            failed.append((path, tool, params, "语法/参数", str(e)))
        except Exception as e:
            failed.append((path, tool, params, "逻辑/其它", str(e)))

    # 统计
    attempted = success + len(failed)
    rate = (success / attempted * 100) if attempted else 0

    print("--- 指标 ---")
    print(f"  参与计划: {len(chunks)}, 尝试执行: {attempted}, 跳过: {skipped}")
    print(f"  执行成功率 = {success}/{attempted} = {rate:.1f}%  (目标 >95%)")
    print()

    if failed:
        print("--- 失败归因（便于优化 Prompt / 工具设计）---")
        for path, tool, params, err_type, msg in failed:
            print(f"  [{err_type}] {path}")
            print(f"      tool={tool}, params={params}")
            print(f"      {msg}")
        print()
        print("归因建议:")
        print("  幻觉: 模型捏造不存在的函数名 -> 在 Prompt 中强化 CALCULATION_GUIDE 与「仅从给定函数中选取」")
        print("  语法/参数: 参数名错误、类型非数值 -> 强制 JSON 中 params 的 value 为数字，并做 schema 校验")
        print("  逻辑: 能跑通但结果与教材不符 -> 需人工核对教材规则与 calculation_logic 实现")

    print("="*70)

if __name__ == "__main__":
    main()
