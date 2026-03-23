# 单题有效生产成本 (CPVQ) 评估方案

## 1. 背景与目标

基于「**商业 ROI 控制与数据闭环 (Data Flywheel) 演进**」中的要求：

- **极具商业算账意识**：建立可量化、可监控的成本指标。
- **精细化管控单题有效生产成本 (CPVQ)**：对「单题有效生产成本」进行定义、采集、评估与管控。
- **大模型单位经济模型 (Unit Economics)**：以单题为单位观测成本结构，支撑后续 ROI 与优化决策。
- **打通线上线下数据流**：为后续「成本 × 使用量」关联分析提供统一口径。

本方案给出 **CPVQ 的评估体系**：指标定义、测算方法、基线/目标、监控告警与报表，便于落地精细化管控。

---

## 2. CPVQ 指标定义

### 2.1 核心公式

| 指标 | 含义 | 公式 |
|------|------|------|
| **CPVQ** | Cost Per Valid Question，单题有效生产成本 | `CPVQ = 当次任务总成本 (total_cost) / 有效题目数 (saved_count)` |
| **有效题目** | 通过 Critic 且成功入库的题目 | 代码口径：`question_trace.saved === true`，汇总为 `saved_count` |

### 2.2 与现有指标区分

| 指标 | 分子 | 分母 | 含义 |
|------|------|------|------|
| **avg_cost_per_question** | total_cost | 生成题数 (generated_count) | 毛成本：含未通过、未入库题目 |
| **CPVQ** | total_cost | saved_count | 有效成本：仅算「有效产出」对应的成本 |

CPVQ 更贴近商业算账，用于评估「每道真正入库的题目花了多少钱」。

### 2.3 边界约定

- **saved_count = 0** 时：CPVQ 无意义，记为 `null` 或「N/A」，不参与汇总与趋势计算。
- **成本口径**：当前以 LLM API 成本（按 token 或按次计费）为主；若后续纳入人工复核、算力等，需在公式中显式说明口径。
- **统计粒度**：单次 QA Run（任务维度）；可扩展至按章节/题型/模式的聚合 CPVQ。

---

## 3. 评估维度

从「**能否算清、能否管住、能否优化**」三个层次设计评估维度。

### 3.1 可观测性（能否算清）

| 维度 | 评估内容 | 验收标准 |
|------|----------|----------|
| 指标落库 | 每次 QA Run 的 batch_metrics 是否包含 cpvq | saved_count>0 时为数值，否则为 null/标注 |
| 成本分解 | 是否可按 by_node、by_model、by_question 查看成本 | 与现有 cost_summary 一致，可关联到单题 |
| 有效产出 | saved_count、generated_count、hard_pass_rate 是否同步可查 | 便于对比「毛成本 vs 有效成本」 |

### 3.2 可控性（能否管住）

| 维度 | 评估内容 | 验收标准 |
|------|----------|----------|
| 阈值配置 | 是否支持 cpvq_max 等阈值配置 | 配置后写入 qa 阈值表/配置 |
| 告警触发 | CPVQ 超标时是否生成告警 | 告警写入 qa_alerts，可查询与展示 |
| 异常场景 | saved_count=0 且 generated_count>0 时是否有提示 | 可选「本批无有效题目」类告警 |

### 3.3 可优化性（能否优化）

| 维度 | 评估内容 | 验收标准 |
|------|----------|----------|
| 趋势对比 | 任务/周期维度 CPVQ 趋势是否可见 | 管理端或报表至少一处展示 CPVQ |
| 与毛成本对比 | CPVQ 与 avg_cost_per_question 是否并列展示 | 便于分析「通过率对有效成本的影响」 |
| 单位经济扩展 | 是否可导出「单题成本 + 单题收益（使用量）」结构 | 为后续 ROI、数据飞轮提供数据基础 |

---

## 4. Trace 与数据结构说明（是否需要调整）

- **question_trace（单题过程 trace）**：**无需调整**。现有结构已支持 CPVQ 计算：
  - 每道题有 `saved: boolean`（Critic 通过且入库为 true），用于统计 `saved_count`。
  - 成本按题汇总在 `cost_summary.by_question` 和每题的 `stability.cost_estimate`，均来自现有 `llm_trace`。
- **batch_metrics（Run 级）**：在现有 `total_cost`、`saved_count` 基础上**仅新增字段**：
  - `cpvq`：saved_count > 0 时为 total_cost / saved_count，否则为 null。
  - `cpvq_currency`：与 currency 一致（仅当 cpvq 有值时存在）。
- **任务详情接口**：为便于页面展示，在返回任务时若存在 `run_id`，会从 `qa_runs.jsonl` 拉取对应 run，将 `batch_metrics` 与 `cost_summary` 挂到 task 上返回，**不改变** task 或 trace 的落库结构。

因此：**trace 不需要新增字段或改结构**，只需在 Run 级指标（batch_metrics）中增加 cpvq 计算与落库，并在管理端读取展示即可。

---

## 5. 测算与数据采集

### 5.1 数据来源（当前已有）

- **admin_api.py**：`total_cost`，`by_model` / `by_node` / `by_question` 成本汇总；`batch_metrics.saved_count`、`batch_metrics.avg_cost_per_question`。
- **qa_runs.jsonl**：每 run 的题目列表、critic 结果、cost、tokens、saved 状态。
- **gen_tasks.jsonl**：任务配置与结果摘要。

### 5.2 测算逻辑

1. **单 Run 级**  
   - 在 `_build_qa_run_payload` 或等价位置计算：  
     - `cpvq = saved_count > 0 ? total_cost / saved_count : null`  
   - 写入 `batch_metrics.cpvq`（及可选 `cpvq_currency`）。

2. **聚合级（可选）**  
   - 按时间窗口（日/周/月）或按章节/题型：  
     - 聚合 `total_cost_sum`、`saved_count_sum`；  
     - `CPVQ_agg = total_cost_sum / saved_count_sum`（仅当 saved_count_sum > 0 时有效）。

### 5.3 基线与目标（建议）

- **基线**：取最近 N 次有效 Run 的 CPVQ 中位数或均值，作为当前「常态水平」。  
- **目标**：结合业务对「单题成本」的预期，设定 cpvq_max（如 P95 或业务给定值）。  
- **健康度**：CPVQ 在阈值内且趋势稳定或下降视为健康；持续上升或频繁告警需触发归因与优化（见数据飞轮）。

---

## 6. 监控与告警

### 6.1 告警规则

| 规则 | 条件 | 动作 |
|------|------|------|
| CPVQ 超标 | saved_count > 0 且 cpvq > cpvq_max | 生成 batch_metric 类告警，写入 qa_alerts |
| 无有效题目 | saved_count == 0 且 generated_count > 0 | 可选：生成「本批无有效题目」告警，便于排查 |

### 6.2 阈值配置

- 在 `_load_qa_thresholds` 中支持 `cpvq_max`（单位与 total_cost 一致，如 CNY 或 USD）。  
- 未配置 cpvq_max 时，仅计算并展示 CPVQ，不触发告警。

### 6.3 展示要求与页面呈现

- **任务详情页**（`AIGenerateTaskDetailPage`）：当任务有关联的 QA Run（有 `run_id`）时，接口会附带该 run 的 `batch_metrics` 与 `cost_summary`。页面在 Descriptions 中展示：
  - **总成本**：`batch_metrics.total_cost` + currency
  - **平均成本/题（毛）**：`batch_metrics.avg_cost_per_question` + currency
  - **CPVQ（单题有效成本）**：`batch_metrics.cpvq`（saved_count=0 时显示为「—」）+ cpvq_currency
- **质检评估页**（`QualityEvaluationPage`）：
  - **总览卡片**：增加「CPVQ（单题有效成本）」卡片；「平均成本/题」标注为「平均成本/题（毛）」以区分。
  - **金额成本 Tab**：在 Descriptions 中增加 `cpvq` 行（数值或「—」）。
  - **批量指标 Tab**：`batch_metrics` 全量展示中已包含 `cpvq`（null 时展示为「—」）。
  - **趋势表**：增加 `cpvq` 列，便于按 run 查看 CPVQ 趋势。
- 便于对比毛成本与有效成本：CPVQ 与 avg_cost_per_question、saved_count、hard_pass_rate 并列展示。

---

## 7. 报表与复盘

### 7.1 最小报表内容

- 每 Run：`run_id`、`total_cost`、`saved_count`、`cpvq`、`avg_cost_per_question`、`hard_pass_rate`。  
- 可选：按 node、model 的成本占比，用于定位高成本环节。

### 7.2 与单位经济、数据飞轮的衔接

- **单位经济**：CPVQ 即「单题成本」侧的核心指标；配合「单题收益」（如题目使用次数，后续由业务赋值），可算单题 ROI。  
- **数据飞轮**：用 CPVQ 与通过率、失败原因、节点成本等做归因分析，反哺提示词、模型选择与重试策略，形成「评估 → 优化 → 再评估」闭环。

---

## 8. 验收清单（评估方案落地）

| 序号 | 验收项 | 说明 |
|------|--------|------|
| 1 | 指标计算 | 单次 QA Run 的 batch_metrics 包含 cpvq（saved_count>0 为数值，否则 null/标注） |
| 2 | 阈值与告警 | 配置 cpvq_max 后，超标可产生告警并写入 qa_alerts |
| 3 | 无有效题目提示 | 可选：saved_count=0 且 generated_count>0 时产生相应告警或提示 |
| 4 | 管理端展示 | 至少一处（如任务详情/运行汇总）展示 CPVQ，并与 avg_cost_per_question、saved_count、hard_pass_rate 并列 |
| 5 | 文档与口径 | 本评估方案与 prd_spec/商业ROI方案中 CPVQ 口径一致，便于审计与扩展 |

---

## 9. 总结

本方案将「单题有效生产成本 (CPVQ)」的评估归纳为：

1. **定义清**：CPVQ = total_cost / saved_count，与 avg_cost_per_question 区分明确。  
2. **算得准**：在现有 total_cost、saved_count 上增加 cpvq 计算与落库，并约定 saved_count=0 时的处理。  
3. **管得住**：通过 cpvq_max 与告警、无有效题目提示，实现阈值化管控。  
4. **看得见**：在管理端与报表中展示 CPVQ 及对比指标，支撑趋势与复盘。  
5. **可进化**：与单位经济模型、数据飞轮及线上线下数据流衔接，为后续 ROI 与精细化优化提供基础。

落地时优先完成「计算 + 告警 + 展示」全链路，再扩展聚合报表与飞轮分析。
