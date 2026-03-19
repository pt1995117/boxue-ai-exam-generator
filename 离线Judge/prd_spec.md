# PRD Spec（基于当前代码初始化）

版本：init-2026-03-10+realism-fatal-explain+compliance-risk-gate+competing-truth-block+kg-prompt-guard+fatal-tighten+kg-evidence-threshold+code-evidence-arbitration
范围：`src/pipeline/graph.py`、`src/pipeline/routes.py`、`src/pipeline/builder.py`、`src/pipeline/runner.py`、`src/schemas/evaluation.py`、`src/agents/*.py`、`tests/test_pipeline.py`

## 1. 产品目标

离线 Judge 对单题进行自动评估，输出结构化 `JudgeReport`，核心决策为：

- `pass`
- `review`
- `reject`

## 2. 输入输出模型

### 2.1 输入（`QuestionInput`）

关键字段：

- `question_id`
- `stem`
- `options`
- `correct_answer`
- `explanation`
- `textbook_slice`
- `related_slices`
- `reference_slices`
- `question_type`（`single_choice`/`multiple_choice`/`true_false`）
- `is_calculation`
- `assessment_type`

### 2.2 输出（`JudgeReport`）

关键字段：

- `decision`（`pass`/`review`/`reject`）
- `hard_pass`
- `overall_score`
- `reasons`
- `hard_gate`
- `solver_validation`
- `dimension_results`
- `knowledge_match`

## 3. 流程与路由（当前实现）

### 3.1 节点流程

1. `node_layer1_blind_solver`
2. 若 `solver_validation.ambiguity_flag == True`，直接到 `node_aggregate`（短路）
3. 否则进入 `node_layer2_knowledge_gate`
4. 若 `knowledge_gate_reject == True`，直接到 `node_aggregate`（短路）
5. 否则并行执行：
   - `node_layer3_basic_rules_gate`
   - `node_layer3_surface_a`
   - `node_layer3_teaching_b`
   - `node_layer3_calc_branch`（仅 `is_calculation=True`）
6. 并行完成后进入 `node_aggregate`

### 3.2 状态合并

- 并行节点写入不同 key，无显式 reducer 冲突。
- 聚合节点在上游边都到达后触发。

## 4. Phase 1 硬规则（代码真值）

实现入口：`_basic_rules_code_checks`（`node_layer3_basic_rules_gate` 内调用）

包含（非穷尽）：

- 题干字数上限与简练提醒
- 年份约束：若教材主切片+关联切片无年份且题干出现公历年份，给出“需证据支持”的复核提醒（warning）
- 题型设问模板（单选/多选/判断，当前模板偏差降级为warning）
- 括号位置与格式（全角、空格）
- 单引号禁用
- 图片/表格禁用
- 选项末尾标点禁用
- 违禁兜底项（如“以上都对”）
- 选项数量与空值检查
- 选项前缀标签重复禁用（A/B/C/D）
- 数值选项升序（当前降级为warning）
- 解析三段结构检查与“结论-答案字段一致”

## 5. 聚合裁决规则（当前实现）

### 5.1 硬门禁

- `structure_legal = len(hard_rule_errors) == 0`
- `expression_standard = len(hard_rule_errors) == 0`
- `solvability_baseline = not solver_validation.ambiguity_flag`
- `hard_pass = 三者与`

### 5.2 致命信号（任一命中即 `reject`）

- `solver_validation.ambiguity_flag == True`
- `knowledge_gate_reject == True`（仅当知识门命中且证据链充分）
- `rigor_data.legal_math_closure_invalid == True`
- 计算题代码证据硬冲突：`calculation_data.code_evidence_status == "HARD"`（代码与标准答案不一致或条件缺失/多解）

### 5.2.1 致命门槛收紧（P0）

- 仅以下四类进入致命拒绝：
  - 无解/多解/不可判定（盲答短路）
  - 知识超纲或边界冲突（知识门短路）
  - 法理/数学闭环失败（严谨合规硬失败）
  - 计算题代码节点硬冲突（HARD：代码与标准答案不一致或未映射到选项/多解）
- 其余问题默认降级为 `review`（优化建议），不直接 `reject`：
  - 业务真实性不足（含教条主义/真理对抗/合规动作不足等）
  - 泄题、题干过长、选项过长
  - 解析结构不足、教学价值一般、题型建议偏差

### 5.3 Review 信号（非致命时命中则 `review`）

- `not hard_pass`
- 风险中/高
- 严谨合规失败
- 干扰项显式闸门触发
- 解析质量失败
- 教学价值失败
- 知识匹配失败
- 计算维度失败（计算题）
- 计算题代码证据 SOFT/TOOL_FAIL：`code_evidence_status in ("SOFT", "TOOL_FAIL")`（答案冲突待复核或工具执行失败/系统异常）

### 5.4 否则 `pass`

## 6. 当前已确认行为

- 支持短路时维度标记 `SKIP`（如盲答短路后知识匹配/业务真实性/教学价值可能 `SKIP`）。
- `overall_score` 在 `reject` 时封顶 59，在 `review` 时封顶 79。
- 当 `llm` 未配置时，盲答节点会直接产出 `ambiguity_flag=true`，并按短路路径进入聚合，最终触发 `reject`。
- `solver_validation.ambiguity_flag` 的代码判定为：`score==0` 或 `predicted_answer in {"", "NONE"}` 或 `fatal_logic_issues` 非空。
- 知识门中，`recommended_question_types` 与当前题型不一致且 `recommendation_confidence >= 0.75` 时会追加 issue，但不触发 `knowledge_gate_reject`。
- 判断题 `single_knowledge_point_invalid` 不触发知识门短路，仅在聚合阶段影响 `knowledge_pass`（进入 REVIEW 信号链）。
- `dimension_results["干扰项质量"].details` 额外输出 `explicit_gate_triggered`、`gate_signals_count`。
- `Evidence` 当前实现为：`slice_id="slice_001"`、`quotes=[]`、`uniqueness_evidence` 合并后最多保留前 8 条。
- `Observability` 当前实现为：`critic_loops=0`，`unstable_flag` 条件是 `failed_calls>0` 或 `latency_ms>60000`。
- `Costs` 当前实现为固定估算：`per_question_usd = round((total_tokens/1000)*0.0002, 6)`，`per_node_usd/per_model_usd` 为空，`cost_alert=false`。
- 主流程“接线矩阵”现状：`surface_a`/`teaching_b`/`calc_branch` 使用统一提示词 JSON 聚合路径；独立文件 `distractor_quality.py`、`explanation_quality.py`、`teaching_value.py`、`code_evaluator.py`、`calculation_complexity.py` 默认未接入主链执行。
- 基础规则复核采用 schema/narrative 双轨：schema 用于决策，narrative 仅展示且先过滤格式类幻觉描述。
- 知识门 recommendation 子字段里，仅“推荐题型 + 置信度阈值”参与 issue；其余 recommendation 字段默认仅透传展示。
- 解析维度门禁白名单：仅 `analysis_rewrite_sufficient`、`three_part_semantic_invalid`、`first_part_missing_*`、`first_part_structured_issues` 参与 `explanation_pass`；`has_forbidden_media/multi_option_coverage_rate/missing_options` 当前仅展示。
- 解析维度门禁还包含“双支撑”硬条件：`theory_support_present`、`business_support_present` 任一为 `false` 会导致 `explanation_pass=false`。
- 当 `has_assessment_value=false` 时，系统会强制写回 `discrimination="低"` 且 `estimated_pass_rate>=0.9`。
- 盲答/知识门输入存在裁剪策略：`related_slices` 前 8、`reference_slices` 前 8、`examples` 前 5，且 `mother_question` 会先注入 examples 再统一裁剪。
- 计算模型回退细则：读取 `.gpt_rate_limit.txt`，按 12 秒窗口估算等待，若等待超过 5 秒切换 `CALC_FALLBACK_MODEL`。
- 业务真实性含后置门禁信号：`scenario_dialogue_or_objection=true` 且 `contains_business_action=false` 会拉低 `realism_pass`；`negative_emotion_detected=true` 且 `amplifies_defect_without_remedy=true` 触发 `fatal_doctrinaire_gate`（致命拒绝）。
- `dimension_results.details` 为字段级透传：各维度落不同 key（如 realism/rigor/explanation/teaching/knowledge/calculation），文档需按 key 维护映射，不可仅写“details”泛化描述。

## 7. 当前已知缺口（基于代码现状）

1. 未实现“盲答推导答案与标准答案一致性硬校验”  
   - 当前仅依赖 `ambiguity_flag`，未将 `predicted_answer != correct_answer` 作为显式失败条件。
2. `skip_phase1` 参数在 `runner.py` 中当前未生效（被赋值后未参与分支逻辑）。

## 8. 术语约束（用于后续实现）

- 代码与文档统一使用：`solver_validation`、`predicted_answer`、`correct_answer`、`knowledge_gate_reject`、`hard_rule_errors`、`dimension_results`。  
- 新增逻辑不得引入与本规格冲突的同义字段名。

## 9. 后置质量评估扩展（低耦合实现）

### 9.1 结构约束（必须保留）

- 保留前置短路：`node_layer1_blind_solver` 或 `node_layer2_knowledge_gate` 任一触发拒绝时，直接进入 `node_aggregate`，不执行后续质量节点。
- 新增规则仅在“前置短路未命中”时生效（第3层并行 + 聚合裁决）。
- 第3层节点仅负责“上报证据字段”，不直接输出最终裁决。

### 9.2 业务情境（Business Scenario）实操拟真度

- 适用场景：题干属于“情景对话”或“客户异议处理”。
- 判定规则：正确选项不得是纯背书式定义，必须体现至少一种业务动作：
  - 探寻需求
  - 安抚情绪
  - 提供解决方案
- 证据归属：`realism_data`（由 `node_layer3_surface_a` 产生并标准化）。

### 9.3 致命逻辑拦截（Fatal Logic Block）

- 规则定义（Hard Gate）：
  - IF 题干包含客户负面情绪/担忧
  - AND 正确选项直接承认或放大该缺点
  - AND 未给出任何补救措施
  - THEN 触发致命拦截（0分路径，`decision=reject`）
- 该规则只允许在 `node_aggregate` 生效；上游节点只提供布尔证据，不执行一票否决。

### 9.4 解析规范（Explanation Standard）双支撑

- 解析必须同时包含：
  - 理论支撑：来自教材章节/知识点依据
  - 业务支撑：实际沟通中为何优于其他选项、能解决客户何种问题
- 任一缺失：触发解析质量失败信号，至少进入 `review`。
- 证据归属：`explanation_data`（由 `node_layer3_teaching_b` 产生并标准化）。

## 10. 通用“合规与风控门禁”（Compliance & Risk Control Gate）

### 10.1 设计原则

- 适用于高风险业务节点：严禁以主观经验、口头承诺、感官判断替代客观凭证、法定流程、专业核验。
- 仍保持低耦合：`node_layer3_surface_a` 仅上报证据，`node_aggregate` 统一裁决。
- 不改变前置短路结构：盲答短路与知识门短路优先级高于本规则。

### 10.2 触发域（高危业务域）

- `structure_safety`：结构与安全（承重墙、漏水隐患、燃气改造等）
- `ownership_qualification`：权属与资质（产权、共有权人同意、购房资格等）
- `fund_tax`：资金与税费（定金、首付监管、税费承担/核定等）
- `law_contract`：法律与合同（违约责任、补充协议、口头承诺兑现等）

### 10.3 致命拦截（Hard Fatal）

- IF 命中任一高危业务域
- AND 正确选项存在任一危险行为：
  - 主观替代客观
  - 口头替代书面
  - 越俎代庖（代替专业机构给绝对结论）
  - 规避合规流程（绕过 SOP/法定程序）
- THEN 触发致命拦截（`decision=reject`，0分路径）

### 10.4 通过标准（Pass Requirements）

- 高危业务域下，正确选项应体现：
  - 依靠权威凭证（产调、证照、完税/核税依据等）
  - 引入专业第三方（法务、鉴定、主管部门核验等）
  - 严守流程规范（书面留痕、合同流程、资金监管等）

### 10.5 证据字段（上报，不直接裁决）

- 证据归属：`realism_data`
- 字段约定：
  - `high_risk_domain_triggered: bool`
  - `high_risk_domains: list[str]`
  - `subjective_replaces_objective: bool`
  - `oral_replaces_written: bool`
  - `over_authority_conclusion: bool`
  - `bypass_compliance_process: bool`
  - `uses_authoritative_evidence: bool`
  - `introduces_professional_third_party: bool`
  - `follows_compliance_sop: bool`

## 11. “真理对抗拦截”（Competing Truth Block）

### 11.1 规则目标

- 在“最合适/最专业建议”类题目中，错误选项不得比正确选项更专业、更全面或更可执行。
- 防止“政治正确但无可判别性”的空泛题干进入通过链路。

### 11.2 触发条件

- 题干存在“最合适/最专业/最佳建议”类设问语义，且题型允许单一最优解（如单选题）。

### 11.3 证据字段（由 `surface_a` 上报）

- `competing_truth_violation: bool`
- `competing_truth_issues: list[str]`
- `non_discriminative_stem_risk: bool`
- `non_discriminative_stem_issues: list[str]`

### 11.4 裁决约束（由 `aggregate` 执行）

- 若 `competing_truth_violation=true`：至少进入 `review`。
- 若 `non_discriminative_stem_risk=true`：触发致命拦截（`reject`，打回重写）。
- 与前置短路关系：盲答短路/知识门短路优先级更高，本规则仅在后置质量链执行后生效。

## 12. 知识门提示词防误杀约束（Prompt Guardrails）

### 12.1 目标

- 降低 `node_layer2_knowledge_gate` 在灰区样本上的过度拦截，避免把“可复核不严谨”误判为“超纲/教材冲突”并触发短路。

### 12.2 提示词约束

- 显式断言优先：当题干同时包含“可推导条件”和“直接断言条件”时，以直接断言为准，不再反向推翻。
- 条件关系守恒：并列补充条件不得自动推导为因果条件；背景条件不得自动推导为触发条件。
- 证据门槛：若判 `out_of_scope=true` 或 `constraint_drift=true`，必须提供“教材片段+题面片段+冲突说明”三段证据；证据不足时改为 `false` 并在 `issues` 说明“需复核”。
- 解析从属：`explanation` 仅作辅助，不得单独触发知识短路结论。
- 不确定降级：无法确定时返回 `false`，并输出复核建议，禁止“疑似”触发短路。

### 12.3 知识门“延迟阈值”与证据链输出

- `node_layer2_knowledge_gate` 短路条件从“命中布尔”升级为“命中布尔 + 证据充分”。
- 证据充分定义：对应证据数组至少 3 条，覆盖“教材片段、题面片段、冲突说明”。
- 若命中风险但证据不足：降级为 `review` 并继续执行第3层并行节点，不在第2层短路。
- 若触发短路：必须在 `knowledge_gate_reasons` 中输出证据链条目，保证短路时刻可追溯。

## 13. 计算题代码节点证据仲裁（Code Evidence Arbitration）

### 13.1 目标

- 计算分支不只“算出答案”，而是提供参数提取、公式路径、错误路径映射、可执行性等证据；最终判定用“证据仲裁”：证据 A = 代码节点结果（含日志），证据 B = 盲答/知识门结论，证据 C = 标准答案与解析一致性；三者一致→高置信，不一致→触发复核、不直接判死（除非硬冲突）。

### 13.2 calculation_data 证据字段（由 `node_layer3_calc_branch` 产出）

- `code_evidence_status`: `"OK"` | `"SOFT"` | `"HARD"` | `"TOOL_FAIL"`
  - OK：无关键问题，映射到选项且错误路径可解释。
  - SOFT：存在答案冲突或部分问题但可人工闭环（答案冲突待复核）。
  - HARD：代码与标准答案不一致或条件缺失/多解（题目硬冲突）。
  - TOOL_FAIL：工具执行失败（未返回可执行代码、返回值非法等），不判题错，仅系统异常。
- `code_evidence_chain`: `list[str]`，证据链（code_evaluator evidence + 关键 issues），用于 reject/review 时写入 `reasons`。

### 13.3 判定矩阵（由 `node_aggregate` 执行）

- 工具=标准答案且知识门通过 → 正常判（无额外惩罚）。
- 工具≠标准答案但题面可人工闭环且知识门通过 → REVIEW（答案冲突待复核）。
- 工具≠标准答案且存在题目硬冲突（条件缺失/多解） → REJECT。
- 工具执行失败 → 不判题错，REVIEW（系统异常）；reasons 含【代码节点】工具执行失败及证据链。

