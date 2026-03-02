# 前10切片 × 题目0/244 映射复核报告

- 数据源: `tmp/remap_first10_q0_q244.json`
- 切片范围: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
- 题目范围: [0, 244]

---

## 题目 0

- `meta_conflict`: True
- `matched_count/total_count`: 1/4
- `matched_tokens`: ['贝壳战略']
- `missing_tokens`: ['行业与贝壳篇', '认识贝壳', '贝壳发展历程']
- `detail`: 路径字段与题干/解析可能不一致：matched=1/4

### 命中切片

1. slice `7` | method `LLM_Logic` | confidence `0.842`
   - path: 第一篇  行业与贝壳 > 第二章  认识贝壳 > 第一节  贝壳发展历程 > 二、贝壳战略
   - evidence.reason: LLM专家逻辑重排序（含元数据一致性门禁）
   - evidence.bge_score: 0.917
   - evidence.meta_conflict: True
   - evidence.meta_conflict_detail: 路径字段与题干/解析可能不一致：matched=1/4

2. slice `9` | method `LLM_Logic` | confidence `0.831`
   - path: 第一篇  行业与贝壳 > 第二章  认识贝壳 > 第一节  贝壳发展历程 > 二、贝壳战略 > （二）第二翼：惠居
   - evidence.reason: LLM专家逻辑重排序（含元数据一致性门禁）
   - evidence.bge_score: 0.812
   - evidence.meta_conflict: True
   - evidence.meta_conflict_detail: 路径字段与题干/解析可能不一致：matched=1/4

3. slice `8` | method `LLM_Logic` | confidence `0.83`
   - path: 第一篇  行业与贝壳 > 第二章  认识贝壳 > 第一节  贝壳发展历程 > 二、贝壳战略 > （一）第一翼：整装
   - evidence.reason: LLM专家逻辑重排序（含元数据一致性门禁）
   - evidence.bge_score: 0.803
   - evidence.meta_conflict: True
   - evidence.meta_conflict_detail: 路径字段与题干/解析可能不一致：matched=1/4

---

## 题目 244

- `meta_conflict`: True
- `matched_count/total_count`: 0/4
- `matched_tokens`: []
- `missing_tokens`: ['行业与贝壳篇', '认识行业', '房地产行业', '贝壳战略']
- `detail`: 路径字段与题干/解析可能不一致：matched=0/4

### 命中切片

1. slice `7` | method `LLM_Logic` | confidence `0.84`
   - path: 第一篇  行业与贝壳 > 第二章  认识贝壳 > 第一节  贝壳发展历程 > 二、贝壳战略
   - evidence.reason: LLM专家逻辑重排序（含元数据一致性门禁）
   - evidence.bge_score: 0.901
   - evidence.meta_conflict: True
   - evidence.meta_conflict_detail: 路径字段与题干/解析可能不一致：matched=0/4

2. slice `9` | method `LLM_Logic` | confidence `0.827`
   - path: 第一篇  行业与贝壳 > 第二章  认识贝壳 > 第一节  贝壳发展历程 > 二、贝壳战略 > （二）第二翼：惠居
   - evidence.reason: LLM专家逻辑重排序（含元数据一致性门禁）
   - evidence.bge_score: 0.771
   - evidence.meta_conflict: True
   - evidence.meta_conflict_detail: 路径字段与题干/解析可能不一致：matched=0/4

3. slice `8` | method `LLM_Logic` | confidence `0.826`
   - path: 第一篇  行业与贝壳 > 第二章  认识贝壳 > 第一节  贝壳发展历程 > 二、贝壳战略 > （一）第一翼：整装
   - evidence.reason: LLM专家逻辑重排序（含元数据一致性门禁）
   - evidence.bge_score: 0.756
   - evidence.meta_conflict: True
   - evidence.meta_conflict_detail: 路径字段与题干/解析可能不一致：matched=0/4
