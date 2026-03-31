# 商业 ROI 控制与数据闭环 (Data Flywheel) 演进方案

## 1. 目标概述

围绕「**极具商业算账意识**」与「**打通线上线下数据流**」，本方案给出四块可落地的建设路径：

| 方向 | 核心产出 |
|------|----------|
| **CPVQ 精细化管控** | 单题有效生产成本的指标定义、采集、监控与告警 |
| **大模型单位经济模型 (Unit Economics)** | 以「单题」为单位的成本/收益结构可观测、可优化 |
| **数据飞轮 (Data Flywheel)** | 用生成与质检数据反哺策略，持续降本提效 |
| **线上线下数据流打通** | 线下出题/成本/质量与线上考试/使用数据统一口径、可关联分析 |

---

## 2. CPVQ：单题有效生产成本

### 2.1 定义

- **CPVQ = Cost Per Valid Question**
- **公式**：`CPVQ = 当次任务总成本 (total_cost) / 有效题目数 (saved_count)`
- **有效题目**：Critic 通过且成功入库的题目（当前代码中 `saved_count`、`question_trace.saved === true`）。

与现有「**avg_cost_per_question**」（总成本/生成题数）的区别：

- **avg_cost_per_question**：含未通过、未入库的题目，反映「毛成本」。
- **CPVQ**：只算「有效产出」对应的成本，反映**单题有效生产成本**，更贴近商业算账。

### 2.2 现状与缺口

- **已有**（`admin_api.py`）：
  - `total_cost`、`by_model`/`by_node`/`by_question` 成本汇总；
  - `batch_metrics.saved_count`、`batch_metrics.avg_cost_per_question`；
  - QA 告警阈值：`avg_cost_per_question_max`、`avg_tokens_per_question_max`。
- **缺口**：
  - 未显式定义并输出 **CPVQ**；
  - 未对 CPVQ 设阈值与告警（如 CPVQ 过高或 saved_count=0 导致无意义 CPVQ）；
  - 报表/看板未突出 CPVQ 趋势。

### 2.3 实施要点

1. **指标计算**（在 `_build_qa_run_payload` 或等价位置）  
   - 在 `batch_metrics` 中新增：
     - `cpvq`: 当 `saved_count > 0` 时为 `total_cost / saved_count`，否则为 `null` 或置为「N/A」；
     - 可选：`cpvq_currency` 与现有 `currency` 一致。
2. **阈值与告警**  
   - 在 `_load_qa_thresholds` 中支持 `cpvq_max`（可选）；  
   - 在 `_build_alerts_for_run` 中：若 `saved_count > 0` 且 `cpvq > cpvq_max`，则生成一条 batch_metric 告警；  
   - 可选：`saved_count == 0` 且 `generated_count > 0` 时，生成「本批无有效题目」类告警。
3. **展示**  
   - 管理端「任务详情 / QA Run 详情」中展示 CPVQ；  
   - 与 `avg_cost_per_question`、`saved_count`、`hard_pass_rate` 并列，便于对比「毛成本 vs 有效成本」。

### 2.4 验收标准

- 单次 QA Run 的 `batch_metrics` 中包含 `cpvq`（saved_count>0 时为数值，否则为 null 或明确标注）。
- 配置 `cpvq_max` 后，超标时产生告警并在 qa_alerts 中可查。
- 管理端至少一处（如任务详情/运行汇总）展示 CPVQ。

---

## 3. 大模型单位经济模型 (Unit Economics)

### 3.1 单位定义

- **主单位**：**单题**（一道最终入库的题目）。
- **扩展单位**：单次生成任务（run）、单知识点/章节（若后续按章节聚合）。

### 3.2 成本侧（当前已具备，需结构化呈现）

| 维度 | 含义 | 当前数据来源 |
|------|------|--------------|
| 单题总成本 | 某题从生成到通过 Critic 的 LLM 成本 | `cost_summary.by_question[qid]` |
| 单题 token | 该题对应的总 token 消耗 | `stability.tokens`（per question） |
| 按节点成本 | Writer/Critic/Fixer/Router 等 | `cost_summary.by_node` |
| 按模型成本 | 不同模型各自成本 | `cost_summary.by_model` |

建议在「单位经济」报表或导出中固定输出：

- 每题：`question_id`、`cost_estimate`、`tokens`、`critic_loops`、`hard_pass`、`saved`。
- 每 run：`total_cost`、`saved_count`、`cpvq`、`avg_cost_per_question`、`by_node`、`by_model`。

### 3.3 收益侧（与业务对齐）

- **短期可落地**：  
  - **有效题目数**：`saved_count`（已入库题目数），即「产量」；  
  - **通过率**：`hard_pass_rate`、`logic_pass_rate` 等，反映「一次通过率」与质量，间接影响重试成本。
- **中期扩展**（若线上有数据）：  
  - 某题被**考试使用次数**、**参与场次**；  
  - 按题目或按章节的「使用量 × 单题成本」对比，识别高成本低使用题目，优化出题策略。

单位经济模型可概括为：

- **单题成本** = CPVQ（或 per-question cost_estimate）；  
- **单题收益** = 业务定义的「题目价值」（如：是否进入考试、使用次数等，后续由业务赋值）；  
- **ROI/健康度** = 收益/成本 或 成本/有效题数 的监控与趋势。

### 3.4 实施要点

1. 在现有 `cost_summary` 与 `batch_metrics` 基础上，**不新增存储**，仅在展示与导出层增加「单位经济」视图。
2. 定义一份**单位经济报表**结构（如 JSON/CSV 导出）：run 维度 + 题目维度，包含上述成本与收益字段，便于后续接 BI 或 Excel 分析。
3. 若 prd_spec 中增加「非功能需求：单位经济可观测」，可在 tdd_spec/task_spec 中增加「导出/API 包含 CPVQ 及 by_question cost」类验收点。

---

## 4. 数据飞轮 (Data Flywheel)

### 4.1 飞轮逻辑

```
生成题目 → 质检(Critic/Fixer) → 入库/丢弃
     ↑                              ↓
     └── 反馈：失败原因、token/成本、通过率 ──┘
         用于：提示词优化、模型选择、重试策略、规则收紧
```

目标：用**历史生成与质检结果**持续改进**提示词、模型选择、重试与规则**，从而在保证质量前提下降低 **CPVQ** 与 **avg_cost_per_question**。

### 4.2 数据来源（现有与可扩展）

- **已有**：  
  - `gen_tasks.jsonl`：任务配置、结果摘要；  
  - `qa_runs.jsonl`：每 run 的题目列表、critic 结果、cost、tokens、issues、fix_strategy；  
  - `qa_alerts.jsonl`：超标与风险告警。
- **可扩展**：  
  - 离线 Judge 与线上 Critic 的**规则对齐报告**（如已有「离线Judge与生成润色节点规则对照报告」），用于统一「通过/不通过」口径；  
  - 按「题型/难度/模式/节点」聚合的通过率、平均 token、平均 cost，用于发现高成本节点或题型。

### 4.3 飞轮动作（可分批实施）

| 动作 | 说明 | 优先级 |
|------|------|--------|
| 失败原因聚合 | 按 `critic_result.reason`、`fix_strategy`、`quality_issues` 聚合，找出高频失败类型 | P0 |
| 节点/模型成本排序 | 按 `by_node`、`by_model` 识别高成本节点与模型，评估是否替换模型或精简 prompt | P0 |
| Writer 内层优化 | 用「首段结构」「多选解析覆盖」等高频 issue 反哺 Writer 的 Normalize/Validate/Polish 提示词 | P1 |
| 重试策略调参 | 根据「fix 成功率 vs reroute 成功率」调整 max_loops、何时 reroute | P1 |
| 母题与切片使用分析 | 统计「带母题 vs 不带母题」的通过率与 token，优化母题选取与切片长度 | P2 |

### 4.4 实施要点

1. **分析层**：从 `qa_runs.jsonl` 定期（如每周）产出汇总：  
   - 按 fail reason、fix_strategy、node、model 的通过率与平均 cost/token；  
   - 输出为报表或 JSON，供产品/运营决策。
2. **策略层**：将上述结论转化为具体改动（提示词、阈值、模型配置），通过现有配置或 A/B 任务对比验证「CPVQ 或 avg_cost 是否下降」。
3. **闭环验证**：同一章节/题型在策略调整前后的 CPVQ、hard_pass_rate 对比，形成飞轮效果记录。

---

## 5. 打通线上线下数据流

### 5.1 概念划分

- **线下**：当前 AI 出题与审核环境；  
  - 数据：`gen_tasks.jsonl`、`qa_runs.jsonl`、`audit_log`、`local_question_bank.jsonl`、切片与映射等。  
- **线上**：考试/练习等生产使用环境；  
  - 数据：题目被使用次数、场次、通过率、答题正确率等（若现有系统已采集）。

### 5.2 打通目标

- **统一题目标识**：一道题在「生成 → 入库 → 考试」全链路使用**同一 question_id**（或 题目ID + 版本），便于关联。
- **成本与使用关联**：能回答「某题线下成本多少、是否被线上使用、使用了几次」，用于评估「单题 ROI」和淘汰低效题目。
- **质量闭环**：线上答题正确率/通过率若可回传，可与线下 Critic 通过率、hard_pass 做对比，校准质检标准。

### 5.3 实施要点（分阶段）

| 阶段 | 内容 | 说明 |
|------|------|------|
| 1 | 线下统一 ID 与成本落库 | 题目入库时必带 question_id；qa_runs 中 by_question 已具备，确保与题库记录可关联 | 当前大部分已具备，需核对题库表结构是否含 question_id |
| 2 | 线下数据模型标准化 | 定义「题目成本视图」：question_id、run_id、cost、tokens、saved_at、material_version_id 等，便于导出与 API | 可先以 JSON/CSV 导出或只读 API 提供 |
| 3 | 线上使用数据格式约定 | 若线上有使用埋点，约定「题目使用表」字段：question_id、exam_session_id、使用次数等 | 依赖业务系统是否有现成表或埋点 |
| 4 | 关联分析/报表 | 线下成本 + 线上使用量 联合查询或报表，输出「单题 ROI」或「高成本低使用题目」清单 | 可先做离线报表，再考虑管理端看板 |

### 5.4 数据流示意图（目标态）

```
[ 线下 ]                     [ 线上 ]
gen_tasks / qa_runs          exam_sessions / question_usage
     |                              |
     |  question_id (统一)           |
     +-------------> 题目库 <--------+
                        |
                        v
              关联分析：成本 × 使用量 → ROI / 优化清单
```

---

## 6. 与现有规格的衔接

- **prd_spec.md**：本方案属于「商业与运营侧」增强，不改变现有功能需求；建议在 PRD 中增加一节「非功能需求：成本与单位经济可观测」（CPVQ、单位经济视图、数据飞轮与线上线下打通目标），以便后续 task 与 TDD 对齐。
- **tdd_spec.md / task_spec.md**：  
  - 新增任务：CPVQ 计算与落库、CPVQ 告警、管理端展示；  
  - 可选：单位经济导出结构、飞轮分析脚本的输入输出约定；  
  - 每个任务关联明确验收点（如「batch_metrics 含 cpvq」「配置 cpvq_max 后告警写入 qa_alerts」）。

---

## 7. 实施顺序建议

| 顺序 | 项目 | 依赖 | 预估 |
|------|------|------|------|
| 1 | CPVQ 指标与告警 + 管理端展示 | 无 | 小 |
| 2 | 单位经济视图（报表/导出） | CPVQ 已存在 | 小 |
| 3 | 飞轮：失败原因聚合 + 节点/模型成本分析 | 现有 qa_runs | 中 |
| 4 | 飞轮：策略优化（提示词/模型/重试）与效果对比 | 3 | 中 |
| 5 | 线上线下 ID 与成本视图统一 | 无 | 小 |
| 6 | 线上使用数据对接与 ROI 报表 | 业务侧有数据 | 中 |

建议先做 **CPVQ 全链路（计算 + 告警 + 展示）**，再补单位经济视图与飞轮分析，最后在业务侧具备使用数据后打通线上线下并做 ROI 报表。

---

## 8. 总结

- **CPVQ**：在现有 `total_cost` 与 `saved_count` 上增加「单题有效生产成本」指标与告警，并呈现在管理端。  
- **单位经济模型**：以单题为单位，结构化呈现成本（含 CPVQ、by_node、by_model），并预留「题目价值/使用量」接口，便于后续算 ROI。  
- **数据飞轮**：用 qa_runs 与 gen_tasks 做失败原因与成本聚合，反哺提示词、模型与重试策略，形成闭环。  
- **线上线下打通**：统一 question_id、标准化「题目成本视图」、约定线上使用数据格式，最终实现「成本 × 使用量」的关联分析。

按上述顺序落地后，即可在「不改变现有出题与质检逻辑」的前提下，建立商业 ROI 控制与数据闭环的可观测、可优化体系。
