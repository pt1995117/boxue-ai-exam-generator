# QA 指标口径说明

本文档说明质量评估页面与 QA 接口中常见指标的定义、计算方式和解读建议。

## 1. 指标来源

系统的 QA 指标主要来自单次 `run` 的 `batch_metrics`，由出题任务的 `process_trace`、LLM 调用记录、离线 Judge 结果汇总生成。

代码参考：

- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L9736)

## 2. 批次级核心计数

| 指标 | 含义 |
| --- | --- |
| `question_count` | 本次纳入统计的问题条数 |
| `generated_count` | 任务尝试生成的问题数 |
| `saved_count` | 最终成功落库的问题数 |
| `error_count` | 运行级错误数 |
| `error_calls` | LLM 调用出错次数 |
| `total_llm_calls` | LLM 总调用次数 |

说明：

- `generated_count` 不等于 `saved_count`
- `saved_count` 才是后续发布、CPVQ 等指标的关键基数

## 3. 质量通过类指标

### 3.1 `hard_pass_rate`

定义：

- `通过硬门槛的问题数 / question_count`

这里的“硬门槛”本质上来自 critic 最终结论。

### 3.2 `logic_pass_rate`

定义：

- `logic_score >= 80` 的题目数 / `question_count`

当前在线出题链路里 `logic_score` 兼容保留，很多 run 中该值可能长期偏低或为兼容字段，解读时要结合离线 Judge 一起看。

### 3.3 `quality_score_avg`

定义：

- 若题目已跑离线 Judge 且存在 `offline_judge.quality_score`，则取 Judge 质量分均值
- 否则退回到兼容公式：
  - `avg_logic_score * 0.5`
  - `avg_distractor_score * 10 * 0.15`
  - `knowledge_match_rate * 100 * 0.2`
  - `avg_teaching_value_score * 10 * 0.15`

结论：

- 对已经执行过离线 Judge 的 run，应优先把 `quality_score_avg` 视为 Judge 口径
- 对未执行 Judge 的 run，它只是一个兼容估算值

## 4. 风险类指标

### 4.1 `out_of_scope_rate`

定义：

- 带 `out_of_scope` 风险标签的题目数 / `question_count`

### 4.2 `duplicate_rate`

定义：

- 带 `duplicate` 风险标签的题目数 / `question_count`

### 4.3 `risk_high_rate`

定义：

- 过程里出现过“critic 驳回且有明确 reason”的题目数 / `question_count`

注意：

- 这个字段当前更接近“critic fail rate”，不是传统意义上的高风险题比例
- 即便题目最终修复通过，只要过程里出现过 reject，也可能计入

### 4.4 `unstable_rate`

定义：

- 被标记为 `unstable_question` 的题目数 / `question_count`

## 5. 平均分数类指标

| 指标 | 定义 |
| --- | --- |
| `avg_distractor_score` | 干扰项质量平均分 |
| `knowledge_match_rate` | 知识点匹配均值 |
| `avg_logic_score` | 逻辑分均值 |
| `avg_teaching_value_score` | 教学价值分均值 |
| `avg_critic_loops` | 平均 critic 循环次数 |

说明：

- 这些指标保留了完整结构，但在当前在线链路里，有些字段更多是兼容老口径
- 当 run 已执行离线 Judge 时，更建议结合 `judge_*` 系列指标一起看

## 6. 成本与性能指标

### 6.1 `avg_tokens_per_question`

定义：

- 所有题目的 `tokens` 总和 / `question_count`

### 6.2 `avg_latency_ms_per_question`

定义：

- 所有题目的耗时毫秒总和 / `question_count`

### 6.3 `avg_cost_per_question`

定义：

- 所有题目的成本估算总和 / `question_count`

### 6.4 `avg_cost_per_call`

定义：

- `total_cost / total_llm_calls`

### 6.5 `total_cost`

定义：

- 基于定价配置，把每次 LLM 调用的 prompt/completion token 成本累加得到

### 6.6 `error_call_rate`

定义：

- `error_calls / total_llm_calls`

代码参考：

- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L9786)
- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L9884)

## 7. `cpvq` 口径

`cpvq` = `Cost Per Valid Question`

定义：

- `total_cost / saved_count`
- 仅当 `saved_count > 0` 时才有值
- 若 `saved_count == 0`，则 `cpvq = null`

这也是为什么系统不会在“没有任何有效落库题”的批次上触发 `cpvq` 阈值告警。

代码参考：

- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L9812)
- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L9952)

## 8. Judge 指标

当 run 已执行离线 Judge 后，`batch_metrics` 还会附带：

- `judge_pass_count`
- `judge_review_count`
- `judge_reject_count`
- `judge_pass_rate`
- `judge_reject_rate`
- `judge_overall_score_avg`
- `judge_baseline_score_avg`
- `judge_total_llm_calls`
- `judge_total_tokens`
- `judge_total_latency_ms`
- `judge_total_cost_usd`
- `judge_avg_tokens_per_question`
- `judge_avg_latency_ms_per_question`
- `judge_avg_cost_usd_per_question`

说明：

- 这些字段直接基于题目上的 `offline_judge` 结果聚合
- 发布版本时，系统至少要求 run 已经具备 Judge 结果

## 9. 默认阈值

默认 QA 阈值如下：

| 阈值键 | 默认值 |
| --- | --- |
| `hard_pass_rate_min` | `1.0` |
| `logic_pass_rate_min` | `0.95` |
| `out_of_scope_rate_max` | `0.02` |
| `duplicate_rate_max` | `0.03` |
| `avg_distractor_score_min` | `3.5` |
| `avg_critic_loops_max` | `2.0` |
| `risk_high_rate_max` | `0.03` |
| `avg_tokens_per_question_max` | `3000` |
| `avg_latency_ms_per_question_max` | `10000` |
| `avg_cost_per_question_max` | `1.5` |
| `cpvq_max` | `2.0` |

代码参考：

- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L8510)

## 10. 告警触发规则

批次级告警会按“低于最小值”或“高于最大值”触发，例如：

- `hard_pass_rate < hard_pass_rate_min`
- `duplicate_rate > duplicate_rate_max`
- `cpvq > cpvq_max`

特殊规则：

- `cpvq` 只有在 `saved_count > 0` 且值为数值时才参与告警
- 如果 `generated_count > 0` 但 `saved_count == 0`，系统会额外触发“无有效题目”高优先级告警

代码参考：

- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L9927)

## 11. 常见解读建议

- 看发布可用性时，优先看 `saved_count`、`judge_pass_rate`、`quality_score_avg`
- 看成本效率时，优先看 `cpvq`、`avg_cost_per_question`、`avg_tokens_per_question`
- 看稳定性时，优先看 `risk_high_rate`、`avg_critic_loops`、`error_call_rate`
- 对未跑 Judge 的 run，不要过度依赖 `quality_score_avg`，它可能只是兼容分

