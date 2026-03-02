#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检索端微测：Hit Rate @ Top 3 与逻辑完整度
验证 KnowledgeRetriever 能否在 Top 3 片段中命中「预期片段」。
"""
import csv
import os
import sys

def get_top_k_chunks(kb_data, 考点, k=3):
    """
    根据考点从 kb_data 中检索 Top-K 片段。
    评分：考点 in 完整路径 权重 2，考点 in 核心内容 权重 1；取前 k 个。
    """
    scored = []
    for c in kb_data:
        path = c.get('完整路径', '') or ''
        content = c.get('核心内容', '') or ''
        if not content or '（章节标题' in c.get('Bot专用切片', ''):
            continue
        s = 0
        if 考点 in path:
            s += 2
        if 考点 in content:
            s += 1
        if s > 0:
            scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:k]]

def compute_hit_rate(golden_path, kb_data=None):
    """
    计算 Hit Rate @ Top 3，并返回 Miss 及对应的 Top3 核心内容（供逻辑截断审计）。
    Returns: dict with hit_rate, hits, total, misses, miss_contexts, details
    """
    if kb_data is None:
        from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
        retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
        kb_data = retriever.kb_data

    rows = []
    with open(golden_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get('考点') and r.get('预期片段'):
                rows.append(r)

    hits = 0
    misses = []
    miss_contexts = {}  # (考点, 预期) -> [content1, content2, content3] (truncated)
    detail = []

    for r in rows:
        考点 = r['考点'].strip()
        预期 = r['预期片段'].strip()
        top3 = get_top_k_chunks(kb_data, 考点, k=3)
        contents = [c.get('核心内容', '') or '' for c in top3]
        hit = any(预期 in t for t in contents)
        if hit:
            hits += 1
            detail.append((考点, 预期, "Hit", top3[0].get('完整路径', '')[:50] if top3 else ''))
        else:
            misses.append((考点, 预期))
            detail.append((考点, 预期, "Miss", top3[0].get('完整路径', '')[:50] if top3 else ''))
            # Store top3 contents truncated for LLM audit (e.g. 400 chars each)
            miss_contexts[(考点, 预期)] = [t[:400] for t in contents]

    total = len(rows)
    hit_rate = (hits / total * 100) if total else 0
    return {
        "hit_rate": hit_rate,
        "hits": hits,
        "total": total,
        "misses": misses,
        "miss_contexts": miss_contexts,
        "details": detail,
    }


def main():
    print("="*70)
    print("检索端微测：Hit Rate @ Top 3")
    print("="*70)

    from exam_factory import KnowledgeRetriever, KB_PATH, HISTORY_PATH
    retriever = KnowledgeRetriever(KB_PATH, HISTORY_PATH)
    kb_data = retriever.kb_data

    golden_path = os.path.join(os.path.dirname(__file__) or '.', 'golden_set_retrieval.csv')
    if not os.path.isfile(golden_path):
        print(f"未找到黄金集: {golden_path}")
        return

    res = compute_hit_rate(golden_path, kb_data)
    hits, total = res["hits"], res["total"]
    hit_rate = res["hit_rate"]
    misses, detail = res["misses"], res["details"]

    print(f"黄金集样本数: {total}")
    print()

    print("--- 逐条结果 ---")
    for 考点, 预期, 状态, 路径 in detail:
        print(f"  [{状态}] 考点={考点}, 预期含「{预期}」 -> {路径}...")

    print()
    print("--- 指标 ---")
    print(f"  Hit Rate @ Top 3 = {hits}/{total} = {hit_rate:.1f}%")
    print()

    if misses:
        print("--- 未命中（建议做逻辑完整度审计）---")
        for 考点, 预期 in misses:
            print(f"  考点={考点}, 预期片段含「{预期}」")
        print()
        print("审计建议：")
        print("  1) 逻辑截断：Top3 片段是否因 Chunking 只有「税率」而无「判定条件」？")
        print("  2) 预处理：若截断多，需调整教材的「逻辑语义分割」与切片策略。")
        print()

    print("="*70)


if __name__ == "__main__":
    main()
