# critic 100题复核后的修复清单（2026-03-31）

## 结论概览
- 样本范围：最近100道“过程出现过critic驳回”的题。
- 人工独立复核结果：`true_issue=30`、`misjudge=54`、`uncertain=16`。
- 当前主要问题不是单一题库质量，而是“状态聚合/口径冲突 + 过严阻断策略”。

## P0（本周必须修）
- **状态一致性强校验**
  - 规则：若最终 `critic_result.passed=true` 且 `fail_types` 为空，统计层不得继续记为“critic失败题”。
  - 规则：若 `reason` 含“审计通过/审核通过”，则必须与 `passed=true` 保持一致；否则打 `state_conflict` 并报警。
- **no_question误判拦截**
  - 规则：命中 `no_question` 前，先检查题目载荷是否已有完整 `stem/options/correct_answer`；若完整则禁止标 `no_question`。
  - 预期：直接消除本轮复核中的 `false_no_question` 类误判。

## P1（下周完成）
- **质量类失败降级策略**
  - 将纯“命题风格/干扰项成熟度”类 `quality_fail` 从阻断改为 `review_tag`（不直接判失败）。
  - 仅保留“可唯一性/答案闭环/切片冲突/计算硬冲突”为阻断。
- **process驳回与final结果分层统计**
  - 新增两个指标：`process_reject_count` 与 `final_reject_count`，避免把“过程驳回后已修复通过”重复记为失败。

## P2（持续优化）
- **uncertain样本复核池**
  - 将16条 uncertain 进入人工复核池，沉淀为固定回归集（每次规则变更后回放）。
- **误判日报**
  - 每日报告增加：`state_conflict_rate`、`false_no_question_rate`、`misjudge_rate`（人工复核样本）。

## 代码落点建议
- `admin_api.py`
  - QA聚合与失败分类处：增加 `reason/passed/fail_types` 一致性守门。
  - `no_question` 归因前置校验：先看 question payload 是否完整。
- `exam_graph.py`
  - critic 输出落地前：增加“通过文案 vs passed值”一致性断言。
  - `quality_fail` 细分硬/软失败标签，软失败降级为 review。

## 验收标准
- 连续3天：`state_conflict_rate == 0`。
- `false_no_question_rate` 降为 0。
- 同口径抽样100题，`misjudge` 比例较本次（54%）显著下降。
