# 长任务 trace 字段说明

本文档说明 AI 出题任务详情页里常见 trace 字段的含义，以及这些字段在排障时应该怎么读。

## 1. trace 的定位

任务详情页展示的 `process_trace` 不是“最终题目列表”，而是“尝试过程记录”。

它会保留：

- 首次尝试
- critic 驳回
- 修复重试
- 换切片重试
- 子任务补题
- 模板修复轮次

因此同一个题位可能出现多行，不应把 trace 行数理解成最终成题数。

代码参考：

- [admin-web/src/utils/generateTrace.js](/Users/panting/Desktop/搏学考试/AI出题/admin-web/src/utils/generateTrace.js#L1)

## 2. 常见主字段

| 字段 | 含义 |
| --- | --- |
| `index` | 尝试序号或本地序号 |
| `target_index` | 目标题位编号 |
| `trace_id` | 单次尝试唯一标识 |
| `question_id` | 题目 ID |
| `slice_id` | 使用的切片 ID |
| `slice_path` | 使用的切片路径 |
| `elapsed_ms` | 当前尝试耗时 |
| `saved` | 是否正常落库 |
| `saved_with_issues` | 是否带问题入库 |
| `snapshot_stage` | 当前 trace 快照阶段 |
| `final_json` | 最终定稿题目 JSON |
| `steps` | 本次尝试的节点日志 |
| `critic_result` | critic 结构化结果 |

## 3. `target_index`

这是最重要的字段之一。

含义：

- 普通任务里，可近似理解为“第几道目标题”
- 模板任务里，表示“模板位次”

因此：

- 看最终成功数时，应按 `target_index` 去重
- 不能简单按 trace 行数统计成题数

前端的“过程成功数”也是按题位去重统计，而不是按 trace 行数统计。

代码参考：

- [admin-web/src/utils/generateTrace.js](/Users/panting/Desktop/搏学考试/AI出题/admin-web/src/utils/generateTrace.js#L1)
- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L7474)

## 4. `trace_id`

`trace_id` 对应一次具体尝试。

用途：

- 前端合并父任务和子任务 trace 时，用它做去重
- 排查“同一题位为什么出现多行”时，用它区分不同尝试

如果父任务快照和活跃子任务快照里出现完全相同的 `trace_id`，前端只保留一条。

## 5. `snapshot_stage`

常见值：

- `live`
- `final`

含义：

- `live` 表示该条记录仍处于执行中或中间态
- `final` 表示该轮尝试已经收尾，当前展示的是最终快照

排障建议：

- 看最终结论时，优先看 `snapshot_stage=final`
- 看运行中的节点状态时，再结合 `live` 行与 `current_node`

## 6. `saved` 与 `saved_with_issues`

### 6.1 `saved`

表示题目正常落库。

### 6.2 `saved_with_issues`

表示题目虽然存在问题，但命中了白名单或软通过策略，仍被允许入库。

前端在详情页中会把它显示为：

- `通过（白名单）`

因此：

- `saved=true` 是标准成功
- `saved_with_issues=true` 是“非标准成功”
- 两者在“是否已入库”上都算成功，但质量语义不同

代码参考：

- [admin-web/src/pages/AIGenerateTaskDetailPage.jsx](/Users/panting/Desktop/搏学考试/AI出题/admin-web/src/pages/AIGenerateTaskDetailPage.jsx#L639)

## 7. `critic_result`

这是 trace 中最关键的诊断字段之一。

常见子字段：

- `passed`
- `reason`
- `fix_reason`
- `issue_type`
- `fix_strategy`
- `fail_types`
- `missing_conditions`
- `all_issues`
- `basis_paths`

使用建议：

- 先看 `passed`
- 未通过时再看 `reason`
- 需要判断问题类别时看 `fail_types`
- 需要判断修复方向时看 `fix_strategy`

## 8. `final_json_expired`

这个字段表示：

- 某一轮曾经生成过定稿 JSON
- 但因为进入了新一轮修复或重试，这份旧定稿已失效

相关字段：

- `final_json_expired`
- `final_json_expired_at`
- `final_json_expired_run_id`

解读建议：

- 如果某条 trace 有旧定稿，但又被标记为 expired，不要再把它当当前有效结果

代码参考：

- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L6800)

## 9. `current_node`

这是任务级字段，不是单条 trace 字段。

它表示当前活跃执行节点，例如：

- `writer`
- `critic`
- `resume_subrun`
- `system`

配套字段：

- `current_node_updated_at`

用途：

- 判断任务当前卡在哪个节点
- 区分“真卡死”还是“仍在某节点执行”

## 10. `live_subtask_traces`

当父任务在执行模板修复、补题或其他内部子任务时，详情页会把活跃子任务 trace 合并展示。

这就是为什么你会看到：

- 父任务 `process_trace`
- 以及活跃子任务的 `process_trace`

前端会为来自子任务的行附加：

- `_subtask_id`
- `_subtask_name`
- `_subtask_local_index`

## 11. `repair_rounds`

这是模板任务特有的重要诊断字段。

用途：

- 记录每一轮模板修复
- 展示本轮修复补了哪些位次
- 展示修复后是否仍有缺口

如果模板任务明明“跑完了”但目标题量没补齐，优先看：

- `repair_rounds`
- `missing_target_indexes`
- `invalid_targets`

## 12. 详情页阅读顺序建议

建议按下面顺序看：

1. 先看任务级 `status / current_node / progress`
2. 再看 `generated_count / saved_count / error_count`
3. 然后看 trace 列表里同一 `target_index` 是否有多次尝试
4. 未通过的行重点看 `critic_result.reason`
5. 模板任务再额外看 `repair_rounds`

## 13. 常见误区

- trace 行数不等于最终题数
- `index` 不是最稳定的业务位次，模板任务优先看 `target_index`
- `saved_with_issues` 也属于已入库，不应误判为失败
- 旧的 `final_json` 如果被标记为 expired，就不再代表当前有效结果

