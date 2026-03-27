# TDD Spec（基于当前代码初始化）

版本：init-2026-03-10+realism-fatal-explain+compliance-risk-gate+competing-truth-block+kg-prompt-guard+fatal-tighten+kg-evidence-threshold+code-evidence-arbitration
目标：把当前实现与预期行为映射为可执行测试点，作为后续“完整测试”清单。

## 1. 测试执行约定

- 推荐命令：`python -m pytest -q`
- 定向命令：`python -m pytest -q tests/test_pipeline.py`
- 若用户要求“完整测试”，按本文件从 TDD-001 顺序执行到末尾。

## 2. 测试点列表

### TDD-001 结构化报告可生成

- 类型：回归
- 入口：`run_judge`
- 输入：合法单选题 + mock llm
- 断言：
  - 返回 `JudgeReport`
  - `decision` 属于 `pass/review/reject`
  - `solver_validation.ambiguity_flag == False`
- 参考：`tests/test_pipeline.py::test_pipeline_returns_structured_report`

### TDD-002 Golden 评测输出指标

- 类型：回归
- 入口：`evaluate_golden`
- 输入：1 条 `GoldenRecord`
- 断言：
  - `metrics.total == 1`
  - 输出中存在 `accuracy`
- 参考：`tests/test_pipeline.py::test_pipeline_computes_golden_metrics`

### TDD-003 盲答短路生效并封顶分

- 类型：门禁
- 入口：`run_judge`（`llm=None`）
- 断言：
  - `decision == reject`
  - `overall_score <= 59.0`
  - 关键维度状态为 `SKIP`
- 参考：`tests/test_pipeline.py::test_short_circuit_on_solver_marks_skip_and_caps_score`

### TDD-004 知识门短路生效并封顶分

- 类型：门禁
- 入口：`run_judge` + mock 知识门拒绝
- 断言：
  - `decision == reject`
  - `overall_score <= 59.0`
  - 知识匹配维度为 `FAIL`
  - 下游部分维度为 `SKIP`
- 参考：`tests/test_pipeline.py::test_short_circuit_on_knowledge_marks_skip_and_caps_score`

### TDD-005 维度状态枚举合法

- 类型：契约
- 入口：`run_judge`
- 断言：
  - 每个 `dimension_results[*].status` ∈ `{PASS, FAIL, SKIP}`
- 参考：`tests/test_pipeline.py::test_dimension_results_use_strong_status_enum`

### TDD-006 年份约束硬校验（教材无年份）

- 类型：复核信号
- 入口：`_basic_rules_code_checks`
- 输入：教材主切片 + 关联切片均无年份；题干/选项/解析包含 `20xx年`
- 断言：
  - `warnings` 包含“【年份约束复核】...需提供教材证据支持”
  - 不作为 `errors` 直接拦截

### TDD-007 年份约束放行（教材有年份）

- 类型：硬规则
- 入口：`_basic_rules_code_checks`
- 输入：教材或关联切片包含年份；题干/选项/解析出现年份
- 断言：
  - 不触发 TDD-006 的年份复核 warning

### TDD-008 解析结论与答案一致性

- 类型：硬规则
- 入口：`_basic_rules_code_checks`
- 输入：解析结论与 `correct_answer` 不一致
- 断言：
  - `errors` 命中“解析结论与正确答案字段不一致”

### TDD-009（待实现）盲答答案一致性硬失败

- 类型：缺口驱动
- 入口：`node_aggregate`
- 输入：`solver_validation.predicted_answer != correct_answer` 且 `ambiguity_flag=False`
- 预期（目标态）：
  - 至少 `review`，推荐 `reject`（需产品决策）
  - `reasons` 含一致性失败原因
- 当前状态：**预期失败（代码未实现）**

### TDD-010（待实现）skip_phase1 参数生效

- 类型：契约
- 入口：`run_judge(..., skip_phase1=True)`
- 预期（目标态）：
  - Phase 1 硬规则节点可被跳过或降权（按产品定义）
- 当前状态：**预期失败（参数暂未生效）**

### TDD-011 情景题背书式正确项拦截

- 类型：后置质量门禁
- 入口：`run_judge`（前置两层均通过）
- 输入：题干含“客户异议/担忧”；正确选项为背书式定义，且无探需/安抚/方案动作
- 断言：
  - `dimension_results["业务真实性"].status == FAIL`
  - `dimension_results["业务真实性"].details.contains_business_action == False`

### TDD-012 情景题业务动作放行

- 类型：后置质量门禁
- 入口：`run_judge`（前置两层均通过）
- 输入：题干含“客户异议/担忧”；正确选项包含探需/安抚/方案动作之一
- 断言：
  - `dimension_results["业务真实性"].status == PASS`
  - `dimension_results["业务真实性"].details.contains_business_action == True`

### TDD-013 教条主义致命拦截命中

- 类型：致命门禁
- 入口：`node_aggregate`（通过 `run_judge` 触发）
- 输入：`negative_emotion_detected=True` 且 `amplifies_defect_without_remedy=True`
- 断言：
  - `decision == reject`
  - `overall_score <= 59.0`
  - `reasons` 包含“教条主义”拦截原因

### TDD-014 教条主义拦截豁免（有补救）

- 类型：致命门禁豁免
- 入口：`run_judge`
- 输入：客户有负面情绪，但正确选项包含补救方案（`amplifies_defect_without_remedy=False`）
- 断言：
  - 不因 TDD-013 规则直接 `reject`

### TDD-015 解析缺理论支撑

- 类型：解析质量
- 入口：`run_judge`
- 输入：`theory_support_present=False`，`business_support_present=True`
- 断言：
  - `dimension_results["解析质量"].status == FAIL`
  - 决策至少 `review`

### TDD-016 解析缺业务支撑

- 类型：解析质量
- 入口：`run_judge`
- 输入：`theory_support_present=True`，`business_support_present=False`
- 断言：
  - `dimension_results["解析质量"].status == FAIL`
  - 决策至少 `review`

### TDD-017 高危域+主观替代客观触发致命拦截

- 类型：致命门禁
- 入口：`run_judge`（前置两层通过后进入聚合）
- 输入：`high_risk_domain_triggered=True` 且 `subjective_replaces_objective=True`
- 断言：
  - `decision == reject`
  - `reasons` 包含“合规风控拦截”关键字

### TDD-018 高危域+口头替代书面触发致命拦截

- 类型：致命门禁
- 入口：`run_judge`
- 输入：`high_risk_domain_triggered=True` 且 `oral_replaces_written=True`
- 断言：
  - `decision == reject`

### TDD-019 高危域+越权定论触发致命拦截

- 类型：致命门禁
- 入口：`run_judge`
- 输入：`high_risk_domain_triggered=True` 且 `over_authority_conclusion=True`
- 断言：
  - `decision == reject`

### TDD-020 高危域+绕流程触发致命拦截

- 类型：致命门禁
- 入口：`run_judge`
- 输入：`high_risk_domain_triggered=True` 且 `bypass_compliance_process=True`
- 断言：
  - `decision == reject`

### TDD-021 高危域但采用凭证+第三方+SOP不触发致命拦截

- 类型：放行校验
- 入口：`run_judge`
- 输入：`high_risk_domain_triggered=True`，且 `uses_authoritative_evidence=True`、`introduces_professional_third_party=True`、`follows_compliance_sop=True`，四类危险行为均为 `False`
- 断言：
  - 不因合规风控门禁直接 `reject`

### TDD-022 错误选项优于正确选项触发真理对抗风险

- 类型：后置质量门禁
- 入口：`run_judge`
- 输入：`competing_truth_violation=True`
- 断言：
  - 决策至少 `review`
  - `reasons` 包含“真理对抗”关键字

### TDD-023 题干空泛无判别性触发致命拦截

- 类型：致命门禁
- 入口：`run_judge`
- 输入：`non_discriminative_stem_risk=True`
- 断言：
  - `decision == reject`
  - `overall_score <= 59.0`

### TDD-024 真理对抗未命中时不触发该门禁

- 类型：放行校验
- 入口：`run_judge`
- 输入：`competing_truth_violation=False` 且 `non_discriminative_stem_risk=False`
- 断言：
  - 不因“真理对抗拦截”导致 `review/reject`

### TDD-028 教条主义风险降级为 Review（不再直接 Reject）

- 类型：门槛调整回归
- 入口：`run_judge`
- 输入：`fatal_doctrinaire_gate=True`，且未命中三类致命门槛
- 断言：
  - `decision == review`

### TDD-029 合规风控危险行为降级为 Review（不再直接 Reject）

- 类型：门槛调整回归
- 入口：`run_judge`
- 输入：`fatal_compliance_risk_gate=True`，且未命中三类致命门槛
- 断言：
  - `decision == review`

### TDD-030 真理对抗“题干无判别性”降级为 Review（不再直接 Reject）

- 类型：门槛调整回归
- 入口：`run_judge`
- 输入：`non_discriminative_stem_risk=True`，且未命中三类致命门槛
- 断言：
  - `decision == review`

### TDD-031 题干/选项超长降级为 Review（不再直接 Reject）

- 类型：门槛调整回归
- 入口：`run_judge`
- 输入：题干 > 400 或任一选项 > 200，且未命中三类致命门槛
- 断言：
  - `decision == review`

### TDD-032 题型固定模板降级为 Warning

- 类型：格式降级回归
- 入口：`_basic_rules_code_checks`
- 输入：单选/多选题干不满足固定模板但可判定
- 断言：
  - 命中 `warnings`
  - 不进入 `errors`

### TDD-033 数值选项升序降级为 Warning

- 类型：格式降级回归
- 入口：`_basic_rules_code_checks`
- 输入：4个数值选项非升序
- 断言：
  - 命中“数值选项建议按从小到大升序排列” warning
  - 不进入 `errors`

### TDD-025 显式断言优先于反向推导（知识门）

- 类型：提示词回归
- 入口：`layer2_knowledge_gate_agent`
- 输入：题面同时出现年份推导信息与直接断言（如“持有满5年”）
- 断言：
  - 不因“年份反推不足”直接判 `constraint_drift=true`

### TDD-026 并列条件不推导为因果（知识门）

- 类型：提示词回归
- 入口：`layer2_knowledge_gate_agent`
- 输入：题面给出并列条件A/B，非因果链
- 断言：
  - 不把 B 自动推导为 A 的因果前提导致 `constraint_drift=true`

### TDD-027 证据不足不得触发知识短路（知识门）

- 类型：提示词回归
- 入口：`run_judge`
- 输入：知识门输出“疑似/可能”但缺少教材-题面对照证据
- 断言：
  - 不应触发短路拒绝（降级 review 并继续后链路）

### TDD-034 证据充分时知识门允许短路且输出证据链

- 类型：知识门短路契约
- 入口：`run_judge`
- 输入：`out_of_scope=true`（或 `constraint_drift=true`）且对应 evidence 至少3条
- 断言：
  - `knowledge_gate_reject == true`
  - 第3层维度为 `SKIP`
  - `reasons` 包含“知识边界短路-证据链”条目

### TDD-035 证据不足时知识门降级 Review并继续后链路

- 类型：知识门延迟阈值
- 入口：`run_judge`
- 输入：`out_of_scope=true`（或 `constraint_drift=true`）但 evidence 不足3条
- 断言：
  - `knowledge_gate_reject == false`
  - 第3层维度非 `SKIP`（继续执行）
  - 决策至少 `review`

### TDD-036 计算题代码证据 HARD 触发 REJECT 并输出证据链

- 类型：代码证据仲裁
- 入口：`run_judge`（计算题 + mock calc_branch 返回 `code_evidence_status=HARD`）
- 断言：
  - `decision == reject`
  - `reasons` 含「代码节点」题目硬冲突及「代码证据链」条目

### TDD-037 计算题代码证据 SOFT/TOOL_FAIL 触发 REVIEW 不 REJECT

- 类型：代码证据仲裁
- 入口：`run_judge`（计算题 + mock 返回 `code_evidence_status=SOFT` 或 `TOOL_FAIL`）
- 断言：
  - `decision` 非 `reject`，至少 `review`
  - `reasons` 含「代码节点」答案冲突待复核或工具执行失败

## 3. “完整测试”执行顺序

1. `python -m pytest -q tests/test_pipeline.py`
2. 新增并执行 Phase 1 定向单测（覆盖 TDD-006/007/008）
3. 新增并执行聚合一致性测试（覆盖 TDD-009）
4. 新增并执行参数契约测试（覆盖 TDD-010）
5. 新增并执行后置质量门禁测试（覆盖 TDD-011/012/013/014/015/016）
6. 新增并执行合规风控门禁测试（覆盖 TDD-017/018/019/020/021）
7. 新增并执行真理对抗门禁测试（覆盖 TDD-022/023/024）
8. 新增并执行知识门防误杀回归（覆盖 TDD-025/026/027）
9. 新增并执行知识门证据阈值回归（覆盖 TDD-034/035）

## 4. 结果回填规范（供 task_spec 联动）

- 仅在对应测试真实跑通后，才能把关联任务标记为完成。
- 任一失败必须在 `task_spec.md` 中回填“未达标验收点”。

