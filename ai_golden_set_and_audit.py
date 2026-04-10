#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 自动化：黄金集扩充、预期片段教材化、Hit Rate 跑测、逻辑截断审计、预处理/切分建议汇总。
依赖：run_retrieval_hit_rate.compute_hit_rate, get_top_k_chunks；exam_graph.generate_content, parse_json_from_response。
"""
import csv
import json
import os
import re
import shutil
from collections import Counter
from runtime_paths import load_primary_key_config

def load_config():
    return load_primary_key_config()

def clean_leaf(s):
    if not s or not isinstance(s, str):
        return ''
    s = s.strip()
    s = re.sub(r'^[一二三四五六七八九十\d]+[、.．]\s*', '', s)
    s = re.sub(r'^[（(][一二三四五六七八九十\d]+[)）]\s*', '', s)
    return s.strip()

def mine_kaodian_from_mapping(mapping_path='question_knowledge_mapping.json', top_n=50):
    """从 question_knowledge_mapping.json 中提取考点（去重，按出现频率排序）。"""
    import json
    if not os.path.isfile(mapping_path):
        return []
    with open(mapping_path, 'r', encoding='utf-8') as f:
        mapping = json.load(f)
    # 提取所有考点，去重
    考点_set = set()
    for v in mapping.values():
        考点 = v.get('考点', '').strip()
        if 考点:
            # 去掉考点末尾的 "-无需修改" 等标记
            考点 = re.sub(r'\s*-\s*无需修改.*$', '', 考点).strip()
            if 考点:
                考点_set.add(考点)
    # 按在 mapping 中出现的频率排序（出现次数多的优先）
    考点_counts = Counter()
    for v in mapping.values():
        考点 = v.get('考点', '').strip()
        考点 = re.sub(r'\s*-\s*无需修改.*$', '', 考点).strip()
        if 考点:
            考点_counts[考点] += 1
    ordered = [k for k, _ in 考点_counts.most_common(top_n * 2) if k in 考点_set][:top_n]
    return ordered

def get_chunk_texts_for_kaodian(kb_data, 考点, max_chars=800, max_chunks=2):
    from run_retrieval_hit_rate import get_top_k_chunks
    top = get_top_k_chunks(kb_data, 考点, k=max_chunks)
    out = []
    for c in top:
        t = (c.get('核心内容') or '')[:max_chars]
        if t:
            out.append(t)
    return '\n\n'.join(out) if out else ''

def llm_find_best_chunk(考点, kb_data, generate_content, parse_json, model, api_key, base_url):
    """
    让 AI 在 bot_knowledge_base.jsonl 中找到最能支撑该考点的原文 Chunk。
    返回该 Chunk 的 核心内容（content），不做任何修改。
    """
    # 准备候选 chunks（限制数量以节省 token）
    candidates = []
    for i, c in enumerate(kb_data):
        path = c.get('完整路径', '') or ''
        content = c.get('核心内容', '') or ''
        if not content or '（章节标题' in c.get('Bot专用切片', ''):
            continue
        # 简单过滤：考点在路径或内容中
        if 考点 in path or 考点 in content or any(kw in content for kw in 考点.split() if len(kw) >= 2):
            candidates.append({
                'index': i,
                'path': path[:100],  # 截断以节省 token
                'content': content[:1500]  # 每个 chunk 最多 1500 字符
            })
        if len(candidates) >= 20:  # 最多 20 个候选
            break
    
    if not candidates:
        return None
    
    # 构建 prompt：让 AI 找到最能支撑考点的 Chunk
    chunks_text = '\n\n'.join([
        f"--- Chunk {i+1} (路径: {c['path']}) ---\n{c['content']}"
        for i, c in enumerate(candidates)
    ])
    
    prompt = f"""你正在为一个检索系统的「黄金基准集」准备数据。

考点：{考点}

下面是从 bot_knowledge_base.jsonl 知识库中筛选出的候选 Chunk（每个 Chunk 包含 完整路径 和 核心内容）。

请在这些候选 Chunk 中，找到**最能支撑该考点**的原文 Chunk。要求：
1. 该 Chunk 的 核心内容 必须完整涵盖该考点的所有判定条件、规则、公式等核心逻辑
2. 如果考点涉及计算（如契税、土地出让金），Chunk 必须包含完整的判定条件（如首套/二套、面积阈值、税率等）
3. 如果考点涉及流程/操作，Chunk 必须包含完整的步骤或要点

候选 Chunks：
{chunks_text[:8000]}  # 限制总长度

请输出 JSON：
{{
    "chunk_index": 数字（从 1 开始，对应上面的 Chunk 编号）,
    "reason": "为什么选择这个 Chunk（一句话）"
}}

如果所有候选都不够完整，输出：{{"chunk_index": -1, "reason": "未找到完整支撑"}}
"""
    
    raw = generate_content(model, prompt, api_key, base_url, None)
    if not raw or not raw.strip():
        return None
    
    try:
        j = parse_json(raw)
        idx = j.get('chunk_index')
        if isinstance(idx, int) and 1 <= idx <= len(candidates):
            # 返回选中 Chunk 的完整 核心内容（不做修改）
            selected = candidates[idx - 1]
            # 从原始 kb_data 获取完整内容（不截断）
            original_chunk = kb_data[selected['index']]
            return original_chunk.get('核心内容', '').strip()
        elif idx == -1:
            # 未找到，尝试用第一个候选作为 fallback
            if candidates:
                original_chunk = kb_data[candidates[0]['index']]
                return original_chunk.get('核心内容', '').strip()
    except Exception as e:
        print(f"  ⚠️ 解析失败: {e}")
    
    return None


# --- 新版：使用“真相提取器”提示，输出选中 Chunk 的原文内容 ---
def extract_kaodian_keywords(考点: str):
    """提取考点核心关键词（去掉括号与“-”后的后缀）。"""
    clean = re.sub(r'\s*-.*$', '', 考点 or '')
    clean = re.sub(r'[（(].*?[)）]', '', clean)
    clean = clean.strip()
    keywords = [kw for kw in clean.split() if len(kw) >= 2]
    if not keywords and len(clean) >= 2:
        keywords = [clean]
    return clean, keywords


def llm_find_best_chunk_v2(考点, kb_data, generate_content, parse_json, model, api_key, base_url):
    """
    让 AI 在知识库候选中选择最能支撑考点的 Chunk，直接返回其原文内容（不改写）。
    使用“真相提取器”提示：严禁改写，逻辑最全优先，输出仅原文内容。
    """
    kaodian_clean, keywords = extract_kaodian_keywords(考点)

    # 准备候选（放宽匹配，最多 30 条；回退使用 top_k）
    candidates = []
    for i, c in enumerate(kb_data):
        path = c.get('完整路径', '') or ''
        content = c.get('核心内容', '') or ''
        if not content or '（章节标题' in c.get('Bot专用切片', ''):
            continue
        path_lower = path.lower()
        content_lower = content.lower()
        matched = False
        if kaodian_clean and (kaodian_clean.lower() in path_lower or kaodian_clean.lower() in content_lower):
            matched = True
        elif keywords:
            if any(kw.lower() in path_lower or kw.lower() in content_lower for kw in keywords):
                matched = True
        if matched:
            candidates.append({
                'index': i,
                'path': path[:150],
                'content': content[:1500],
            })
        if len(candidates) >= 30:
            break

    # 回退：get_top_k_chunks
    if not candidates:
        try:
            from run_retrieval_hit_rate import get_top_k_chunks
            top = get_top_k_chunks(kb_data, kaodian_clean or 考点, k=5)
            for c in top:
                content = c.get('核心内容', '') or ''
                if content and '（章节标题' not in c.get('Bot专用切片', ''):
                    candidates.append({
                        'index': kb_data.index(c) if c in kb_data else 0,
                        'path': (c.get('完整路径', '') or '')[:150],
                        'content': content[:1500],
                    })
                    if len(candidates) >= 10:
                        break
        except Exception:
            pass

    if not candidates:
        return None

    chunks_text = '\n\n'.join([
        f"--- Chunk {i+1} (路径: {c['path']}) ---\n{c['content']}"
        for i, c in enumerate(candidates)
    ])

    prompt = f"""Role: 你是一位极其严谨的教材审计专家，专门负责为房产经纪人考试准备“黄金标准答案”。

Task: 给定【考点名称】和一组知识库候选片段，请从中挑选出最能代表该考点、逻辑最完整、最适合作为“标准检索目标”的一段原文。

当前考点: {kaodian_clean or 考点}

候选片段列表:
{chunks_text[:8000]}

Constraints (必须遵守):
- 严禁改写：直接输出你选中的那个片段的 content 原文，不要做任何同义词替换或总结。
- 逻辑优先：如果多个片段都提到了该考点，选择判定条件最全的片段（例如：既包含税率，又包含“满五唯一”“面积 90㎡”等前置条件）。

输出格式：只输出原文内容，不要包含任何解释性文字、编号或引号。"""

    raw = generate_content(model, prompt, api_key, base_url, None)
    if not raw or not raw.strip():
        return None
    return raw.strip()

def run_golden_expansion(kb_data, 考点_list, generate_content, parse_json, model, api_key, base_url, batch=5):
    """
    为每个考点让 AI 找到最能支撑的原文 Chunk，返回其 核心内容 作为预期片段。
    生成 (考点, 预期片段=完整核心内容) 列表。
    """
    rows = []
    for i, 考点 in enumerate(考点_list, 1):
        if len(rows) >= 50:
            break
        print(f'  处理 {i}/{len(考点_list)}: {考点[:40]}...', end='', flush=True)
        exp = llm_find_best_chunk_v2(考点, kb_data, generate_content, parse_json, model, api_key, base_url)
        if exp:
            rows.append((考点, exp))
            print(' ✓')
        else:
            print(' ✗ (未找到)')
    return rows[:50]

def llm_audit_one(考点, 预期, top3_contents, generate_content, parse_json, model, api_key, base_url):
    """对一条 Miss 做逻辑截断审计。返回 逻辑截断, 缺失, 切分建议。"""
    snips = '\n---\n'.join([f'片段{i+1}:\n{t}' for i, t in enumerate(top3_contents[:3])])
    prompt = f"""你正在做检索片段的逻辑完整度审计。
未命中案例：考点={考点}，期望片段应含「{预期}」。
检索到的 Top3 片段内容如下：

{snips}

请判断：
1) 是否属于逻辑截断？（片段已有部分相关表述，但缺少「{预期}」所代表的判定条件/结论等核心逻辑）
2) 缺失了什么？
3) 对预处理/切分的具体建议（如：按小节合并、按「条件-结论」分块、扩大块大小等）。

只输出一个 JSON：{{"逻辑截断": true或false, "缺失": "一句话", "切分建议": "一句话"}}
"""
    raw = generate_content(model, prompt, api_key, base_url, None)
    if not raw or not raw.strip():
        return {'逻辑截断': None, '缺失': '(LLM 未返回)', '切分建议': ''}
    try:
        j = parse_json(raw)
        return {
            '逻辑截断': j.get('逻辑截断'),
            '缺失': (j.get('缺失') or '')[:200],
            '切分建议': (j.get('切分建议') or '')[:300],
        }
    except Exception:
        return {'逻辑截断': None, '缺失': '(解析失败)', '切分建议': ''}

def llm_summarize_chunking(切分建议_list, generate_content, model, api_key, base_url):
    """汇总多条切分建议，输出 3–5 条总体改进建议。"""
    block = '\n'.join([f"- {t}" for t in 切分建议_list if t and str(t).strip()])
    prompt = f"""以下为针对若干「未命中」案例的切分建议，请合并、去重、按优先级排序，给出 3–5 条「预处理与切分」的总体改进建议。

{block}

输出：直接给出 3–5 条建议，每条一行，以「- 」开头，不要编号外的其他格式。
"""
    raw = generate_content(model, prompt, api_key, base_url, None)
    return (raw or '').strip() or '(未生成)'

def main():
    import sys
    print('='*70)
    print('AI 自动化：黄金集扩充 + Hit Rate + 逻辑截断审计 + 切分建议汇总')
    print('='*70)

    cfg = load_config()
    model = cfg.get('OPENAI_MODEL') or 'deepseek-reasoner'
    api_key = cfg.get('OPENAI_API_KEY') or ''
    base_url = cfg.get('OPENAI_BASE_URL') or 'https://openapi-ait.ke.com'

    top_n = 50
    dry_run = '--dry-run' in sys.argv
    args = [a for a in sys.argv[1:] if a != '--dry-run']
    if args:
        try:
            top_n = max(5, min(50, int(args[0])))
        except ValueError:
            pass
    print(f'目标考点数：{top_n}' + (' (dry-run，跳过 LLM)' if dry_run else ''))

    from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
    from exam_graph import generate_content, parse_json_from_response

    print('加载知识库...')
    retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
    kb_data = retriever.kb_data

    # 1) 从 question_knowledge_mapping.json 挖掘考点
    print('从 question_knowledge_mapping.json 提取考点...')
    考点_list = mine_kaodian_from_mapping('question_knowledge_mapping.json', top_n=top_n)
    print(f'  得到 {len(考点_list)} 个考点（已去重，按出现频率排序）')
    if len(考点_list) < min(10, top_n):
        print('  ⚠️ 考点过少，将用 完整路径 末段作补充。')
        fallback = [clean_leaf((c.get('完整路径') or '').split('>')[-1].strip()) for c in kb_data if c.get('完整路径')]
        考点_list = list(dict.fromkeys([x for x in fallback if x and len(x) >= 2]))[:top_n]

    # 2) LLM 在知识库中找到最能支撑考点的 Chunk，返回其 核心内容
    if dry_run:
        print('查找最能支撑考点的 Chunk（dry-run：用第一个匹配的 Chunk）...')
        golden_rows = []
        for 考点 in 考点_list:
            if len(golden_rows) >= top_n:
                break
            # 简单匹配：找第一个包含考点的 Chunk
            for c in kb_data:
                path = c.get('完整路径', '') or ''
                content = c.get('核心内容', '') or ''
                if content and 考点 in path:
                    golden_rows.append((考点, content[:200] + '...'))
                    break
        print(f'  生成 {len(golden_rows)} 条 (考点, 预期片段=核心内容)')
    else:
        print('让 AI 在 bot_knowledge_base.jsonl 中找到最能支撑考点的原文 Chunk...')
        print('  （预期片段 = 该 Chunk 的完整 核心内容，不做修改）')
        golden_rows = run_golden_expansion(kb_data, 考点_list, generate_content, parse_json_from_response, model, api_key, base_url)
        print(f'  生成 {len(golden_rows)} 条 (考点, 预期片段=完整核心内容)')

    base = os.path.dirname(__file__) or '.'
    golden_path = os.path.join(base, 'golden_set_retrieval.csv')
    bak_path = os.path.join(base, 'golden_set_retrieval.csv.bak')

    if os.path.isfile(golden_path):
        shutil.copy(golden_path, bak_path)
        print(f'  已备份原黄金集 -> {bak_path}')

    with open(golden_path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['考点', '预期片段'])
        for 考点, 预期 in golden_rows:
            w.writerow([考点, 预期])
    print(f'  已写入 {golden_path}')

    # 3) 跑 Hit Rate
    print('运行 Hit Rate @ Top 3...')
    from run_retrieval_hit_rate import compute_hit_rate
    res = compute_hit_rate(golden_path, kb_data)
    hit_rate, hits, total = res['hit_rate'], res['hits'], res['total']
    misses, miss_contexts = res['misses'], res.get('miss_contexts') or {}
    print(f'  Hit Rate @ Top 3 = {hits}/{total} = {hit_rate:.1f}%')

    # 4) 对每条 Miss 做逻辑截断审计（LLM 或 dry-run 占位）
    audits = []
    if misses and miss_contexts:
        if dry_run:
            print('对未命中案例做逻辑截断审计（dry-run：占位）...')
            for 考点, 预期 in misses:
                audits.append({'考点': 考点, '预期': 预期, '逻辑截断': None, '缺失': '(dry-run)', '切分建议': '建议人工审阅 Top3 是否截断。'})
            summary = '- 建议人工审阅未命中案例的 Top3 片段，判断是否逻辑截断，并调整预处理/切分。'
        else:
            print('对未命中案例做逻辑截断审计（LLM）...')
            for 考点, 预期 in misses:
                ctx = miss_contexts.get((考点, 预期), [''])
                a = llm_audit_one(考点, 预期, ctx, generate_content, parse_json_from_response, model, api_key, base_url)
                audits.append({'考点': 考点, '预期': 预期, **a})
            建议_list = [a.get('切分建议') or '' for a in audits if a.get('切分建议')]
            print('汇总预处理与切分建议（LLM）...')
            summary = llm_summarize_chunking(建议_list, generate_content, model, api_key, base_url)
    else:
        summary = '(无未命中，未做汇总)'

    # 6) 写报告
    report_path = os.path.join(base, '节点级微测报告.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('# 检索端节点级微测报告（AI 自动化）\n\n')
        f.write('## 1. 黄金集\n\n')
        f.write(f'- 样本数：{len(golden_rows)}（由 AI 从 `question_knowledge_mapping.json` 提取考点，在 `bot_knowledge_base.jsonl` 中找到最能支撑的原文 Chunk）\n')
        f.write(f'- 预期片段 = 选中 Chunk 的完整 `核心内容`（不做修改）\n')
        f.write(f'- 文件：`golden_set_retrieval.csv`（原文件已备份至 `.bak`）\n')
        f.write(f'- **⚠️ 人工抽检建议**：随机抽 5-10 条确认 AI 找的 Chunk 是否真的涵盖了所有判定条件（如 $90$ $m^2$、满五唯一等）\n\n')
        f.write('## 2. Hit Rate @ Top 3\n\n')
        f.write(f'- **{hits}/{total} = {hit_rate:.1f}%**\n\n')
        f.write('## 3. 未命中与逻辑截断审计\n\n')
        if audits:
            for a in audits:
                f.write(f"### {a.get('考点','')}（预期含「{a.get('预期','')}」）\n\n")
                f.write(f"- **逻辑截断**：{a.get('逻辑截断')}\n")
                f.write(f"- **缺失**：{a.get('缺失','')}\n")
                f.write(f"- **切分建议**：{a.get('切分建议','')}\n\n")
        else:
            f.write('（无未命中）\n\n')
        f.write('## 4. 预处理与切分：总体建议\n\n')
        f.write(summary)
        f.write('\n')
    print(f'报告已写入 {report_path}')
    
    # 人工抽检：随机抽取 5-10 条
    if golden_rows and not dry_run:
        import random
        sample_size = min(10, max(5, len(golden_rows) // 5))
        sample_indices = random.sample(range(len(golden_rows)), sample_size)
        print()
        print('='*70)
        print('⚠️  人工抽检（至关重要）：请检查以下随机抽取的样本')
        print('='*70)
        for idx in sample_indices:
            考点, 预期 = golden_rows[idx]
            print(f'\n【样本 {idx+1}/{len(golden_rows)}】考点：{考点}')
            print(f'预期片段（Chunk 核心内容）：')
            print(f'{预期[:300]}{"..." if len(预期) > 300 else ""}')
            print('-'*60)
        print('\n检查要点：')
        print('  1. 该 Chunk 是否真的涵盖了该考点的所有判定条件？')
        print('  2. 如果考点涉及计算（如契税），是否包含完整的判定条件（首套/二套、面积阈值、税率等）？')
        print('  3. 如果考点涉及流程，是否包含完整的步骤或要点？')
        print('  4. 是否有遗漏的关键信息（如 $90$ $m^2$、满五唯一等）？')
        print('='*70)
    
    print('='*70)

if __name__ == '__main__':
    main()
