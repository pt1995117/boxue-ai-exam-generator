# 离线 Judge 产品需求文档

> 基于当前代码实现整理，版本 v2.1

---

## 1. 产品概述

离线 Judge 是房地产搏学考试的**自动评估引擎**，对题目进行多闸门、多维度质检，输出 `PASS` / `REVIEW` / `REJECT` 决策及结构化报告。

### 1.1 核心能力

- **Phase 1** 硬规则校验：格式、字数、括号、标点、违禁词、图片/表格、年份约束等
- **Phase 2** 多 Agent 语义审查：盲答、知识匹配、基础规则复核、表层质量、教学复盘、计算题专项
- **Phase 3** 聚合裁决：致命信号直接 REJECT，其余按维度通过状态与风险等级决定 PASS / REVIEW / REJECT

### 1.2 支持题型

- 单选题（single_choice）
- 多选题（multiple_choice）
- 判断题（true_false）

### 1.3 评估类型

- 基础概念/理解记忆
- 实战应用/推演

---

## 2. 输入数据模型（QuestionInput）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| question_id | str | 是 | 题目唯一标识 |
| stem | str | 是 | 题干 |
| options | list[str] | 是 | 选项列表 |
| correct_answer | str | 是 | 正确答案（A/B/C/D 或 正确/错误） |
| explanation | str | 是 | 题目解析 |
| textbook_slice | str | 是 | 教材主切片原文 |
| related_slices | list[str] | 否 | 关联切片 |
| reference_slices | list[str] | 否 | 参考切片 |
| mother_question | str | 否 | 关联母题 |
| examples | list[dict] | 否 | 范例（含题干、解析） |
| term_locks | list[str] | 否 | 锁词/术语锁词 |
| mastery | str | 否 | 掌握程度 |
| question_type | str | 否 | 题型，默认 single_choice |
| is_calculation | bool | 否 | 是否计算题，默认 False |
| assessment_type | str | 否 | 评估类型，默认 基础概念/理解记忆 |

---

## 3. 流程架构（四层解耦）

**状态对象**：JudgeState（TypedDict），含 question、llm、trace_id、hard_rule_errors/warnings、gate_recheck_data、solver_validation、各节点输出（realism_data、rigor_data、distractor_data、explanation_data、teaching_data、knowledge_data、calculation_data）、ran_* 标志等。

```
node_layer1_blind_solver（盲答）
    ├─ ambiguity_flag=true → node_aggregate（短路）
    └─ ambiguity_flag=false → node_layer2_knowledge_gate（知识门）

node_layer2_knowledge_gate（知识匹配）
    ├─ knowledge_gate_reject=true → node_aggregate（短路）
    └─ 否则 → 并行：
        ├─ node_layer3_basic_rules_gate（基础规则复核）
        ├─ node_layer3_surface_a（表层质量：业务真实性 + 严谨合规 + 干扰项）
        ├─ node_layer3_teaching_b（教学复盘：解析质量 + 教学价值）
        └─ node_layer3_calc_branch（计算题专项，仅 is_calculation=true 时）

所有并行节点 → node_aggregate（聚合裁决）
```

补充：当 `llm` 未配置时，盲答节点会直接产出 `ambiguity_flag=true`（reasoning_path=`LLM 未配置`），因此会按短路路径进入聚合并触发 REJECT。

### 3.1 聚合节点触发与 state 合并（LangGraph）

- **触发时机**：聚合节点在所有指向它的并行节点（basic_rules_gate、surface_a、teaching_b、calc_branch）全部执行完毕后触发。
- **state 合并**：各并行节点写入不同 state key，无 reducer 时采用覆盖策略；本图各节点 key 不重叠，无并发写冲突。详见 `src/pipeline/builder.py` 模块注释。

### 3.2 Agent 接线矩阵（当前实现）

- **主流程已接线（执行生效）**：
  - `layer1_blind_solver_agent`
  - `layer2_knowledge_gate_agent`
  - `node_layer3_surface_a`（统一提示词返回 `business_realism + rigor + distractor`）
  - `node_layer3_teaching_b`（统一提示词返回 `explanation_quality + teaching_value`）
  - `node_layer3_calc_branch`（统一提示词返回 `code_evaluator + complexity`）
- **独立 agent 文件当前未接入主流程（定义存在，但默认不执行）**：
  - `src/agents/distractor_quality.py`
  - `src/agents/explanation_quality.py`
  - `src/agents/teaching_value.py`
  - `src/agents/code_evaluator.py`
  - `src/agents/calculation_complexity.py`

---

## 4. 硬规则（代码级）

**实现位置**：`_basic_rules_code_checks` 在 `node_layer3_basic_rules_gate` 内执行，与 LLM 基础规则复核合并。**不是** DeterministicFilter（见第 15 节）。

### 4.1 字数与格式

| 规则 | 类型 | 阈值/条件 | 说明 |
|------|------|----------|------|
| 题干字数 | error | >400 字 | 超限直接 REJECT |
| 题干简练 | warning | >120 字 | 建议控制在 120 字以内 |
| 题干冗余 | warning | 连接词≥4 且密度≥0.04 | 连接词堆叠 |
| 选项字数 | error | 任一选项 >200 字 | 超限直接 REJECT |
| 解析字数 | warning | >400 字 | 建议控制在 400 字以内 |

### 4.2 年份约束

- **条件**：教材主切片 + 关联切片不含公历年份（19xx年/20xx年）
- **禁止**：题干、选项、解析中出现公历年份
- **违反**：error

### 4.3 设问句式

| 题型 | 结尾模板 |
|------|----------|
| 单选题 | “以下表述正确的是（ ）。。”或“以下表述错误的是（ ）。。” |
| 多选题 | “以下表述正确/错误的有/包括（ ）。。” |
| 判断题 | 以“。（ ）”形式结尾 |

- 括号必须为全角中文括号，括号内必须有空格
- 题干括号不能在句首

### 4.4 标点与符号

- 题干/选项/解析禁止单引号（'、‘、’），应使用双引号
- 选项结尾禁止标点（。，、；：！？等）

### 4.5 图片与表格

- **题干、选项、解析**均禁止出现：
  - 图片：`![...](...)`、`<img>`、`.png/.jpg/.jpeg/.gif`
  - 表格：Markdown 表格行、`<table>`

### 4.6 选项规范

- 判断题：2 个选项（A/B）
- 选择题：4 个选项（A/B/C/D）
- 选项内容前禁止再写 A/B/C/D 标签
- 禁止“以上都对/都错/皆是/皆非/以上选项全对/以上选项全错”
- 数值选项必须按从小到大升序排列
- 选项长度均衡：最长与最短字数差≥15 时 warning

### 4.7 答案字段

- 单选题：必须且仅能一个答案，A/B/C/D 之一
- 多选题：由 A/B/C/D 组成（如 A,B）
- 判断题：A 或 B

### 4.8 解析结构

- 必须包含三段：`1.教材原文`、`2.试题分析`、`3.结论`
- 结论必须包含“本题答案为...”
- 判断题结论必须写“正确/错误”，不能写 A/B
- 解析结论与正确答案字段必须一致

### 4.9 计算题特殊规则

- 若选项出现小数，题干需标注“保留到几位小数”或“精确到几位小数”

---

## 5. 基础规则复核（LLM，node_layer3_basic_rules_gate）

**仅做语义仲裁，不做改写。** 禁止输出格式类问题。

### 5.1 语义校验项

| 校验项 | issue_key | 级别 | 说明 |
|--------|-----------|------|------|
| 设问语义 | ask_pattern_still_invalid | error | 设问是否语义上为陈述句 |
| 选项代入 | substitution_still_invalid | error | 选项代入题干是否形成完整自然句 |
| 姓名规范 | name_rule_still_invalid | error | 恶搞名、伦理冲突、称谓式等 |
| 非必要命名 | name_unnecessary_but_used | warning | 题目不需要姓名但硬命名 |
| 姓名长度 | name_length_nonideal | warning | 非 2~3 字通俗名 |
| 生僻字风险 | rare_character_name_risk | warning | 疑似生僻字命名 |
| 否定语义 | negation_semantic_invalid | error | 双重否定导致歧义 |
| 语义冗余 | redundancy_semantic_warning | warning | 题干信息重复、连接词堆叠 |
| 判断题句式 | tf_definition_style_valid | error | 定义类/行为类模板使用不当 |
| 选项单位 | option_unit_still_invalid | error | 选项包含数值单位（应上提题干） |
| 遣词造句 | wording_semantic_invalid | error | 主谓搭配不当、指代错误等 |

### 5.2 教材一致性

- 所有“与教材一致性”判断需同时参考**教材主切片与关联切片**

### 5.3 判断题例外

- 定义类判断题（含“属于/是指/定义/概念”）：可不强制“XX做法正确/错误”模板
- 行为/说法类判断题：应满足“XX做法（说法）正确/错误”语义

### 5.4 严谨性考察例外

- 若题目意图是考察“识别不严谨表达”，题干中的待辨识错误表述可作为命题素材，不判错

### 5.5 schema/narrative 双轨策略（当前实现）

- 决策主链仅依赖 schema 字段（结构化布尔/列表）。
- narrative 文本仅用于展示提醒，不直接驱动门禁。
- narrative 中的格式类描述（如“括号/标点/A/B/C/D/单引号”等）会先过滤，避免格式幻觉污染语义结论。

---

## 6. 知识匹配（LLM，node_layer2_knowledge_gate）

### 6.1 输出字段

| 字段 | 说明 |
|------|------|
| out_of_scope | 超纲/跑偏 |
| constraint_drift | 限定词或边界词漂移 |
| single_knowledge_point_invalid | 判断题单知识点原则不满足 |
| recommendation | 推荐题型、评估类型、考察焦点、难度 |

### 6.2 短路逻辑

- `out_of_scope=true` 或 `constraint_drift=true` → **knowledge_gate_reject**，直接进入聚合，不再跑后续并行节点

### 6.3 切片核验

- 需同时参考“教材切片 + 关联切片”
- 主切片与关联切片冲突时，以更具体且与题干直接相关的切片为准

### 6.4 推荐题型冲突提示（非短路）

- 若 `recommendation.recommended_question_types` 非空，且不包含当前 `question_type`，并且 `recommendation_confidence >= 0.75`，生成建议：
  - “教材切片更推荐题型为...，当前为...（推荐置信度=...）”
- 该建议写入 `knowledge_match.details.recommendation_suggestions`，**不进入 reasons**。
- 该规则用于提示，不会单独触发 `knowledge_gate_reject`。

### 6.5 知识门未执行时的展示

- **场景**：盲答歧义短路时，知识门未执行，`knowledge_semantic_drift` 为空
- **展示规则**：未执行即「未检测」，不造默认“通过”
- **实现**：`dimension_results["知识匹配"].status=SKIP`，`knowledge_match.skipped=True`；Word 报告输出“未检测”

### 6.6 判断题单知识点规则的决策链路

- `single_knowledge_point_invalid=true` 不触发知识门短路。
- 该信号仅在聚合阶段参与 `knowledge_pass` 计算（仅判断题），命中时进入 REVIEW 信号链路。

### 6.7 recommendation 子字段参与性

- 参与 suggestion 触发：`recommended_question_types` + `recommendation_confidence`（见 6.4）。
- 仅透传展示（默认不直接参与 `knowledge_gate_reject/knowledge_pass`）：
  - `recommended_assessment_type`
  - `recommended_focus`
  - `recommended_difficulty`
  - `recommendation_rationale`

---

## 7. 表层质量（LLM，node_layer3_surface_a）

### 7.1 业务真实性（business_realism）

- slice_conflict_invalid：与教材切片冲突
- scene_binding_required_violation：场景绑定缺失（实战题）
- workflow_sequence_violation：流程顺序错误
- scenario_dialogue_or_objection：是否属于“情景对话/客户异议”场景
- contains_business_action：正确选项是否包含业务动作（探需/安抚/方案等）
- negative_emotion_detected：是否识别到客户负面情绪/担忧
- amplifies_defect_without_remedy：是否“放大问题且无补救动作”

补充：

- 当 `scenario_dialogue_or_objection=true` 且 `contains_business_action=false` 时，`realism_pass=false`。
- 当 `negative_emotion_detected=true` 且 `amplifies_defect_without_remedy=true` 时，触发教条主义致命拦截（见 10.1）。

### 7.2 严谨合规性（rigor）

| 字段 | 说明 |
|------|------|
| leakage_still_invalid | **泄题**：正确答案（或其完整核心表述）已在题干中被原样给出 |
| explanation_conflict_still_invalid | 解析与教材/题干冲突 |
| name_consistency_still_invalid | 人名一致性 |
| legal_math_closure_invalid | 法理/数学闭环不成立 |
| term_mismatch_issues | 用词不规范（口语 vs 标准术语）；判定时需同时参考“教材切片+关联切片”，冲突时以更具体且与题干直接相关的切片为准 |

### 7.3 泄题判定定义（必须遵守）

- **从严定义**：仅当“正确答案（或其完整核心表述）已在题干中被原样给出”，导致无需推理即可直接作答，才判 `leakage_still_invalid=true`
- **禁止**：仅凭关键词重合判定泄题
- **证据要求**：若判泄题，必须在 issues 中给出两段证据：题干原文片段、被原样给出的正确答案片段；证据不足一律判 false

### 7.4 严谨性考察例外

- 若题目意图是考察“识别不严谨/不合规表述”，题干中的待辨识说法可视为命题素材，不直接判错

### 7.5 干扰项质量（distractor）

- real_but_inapplicable、format_aligned、logic_homogenous、balance_strength
- overlap_pairs、stem_option_conflicts、mutual_exclusivity_fail
- 选项中的“干扰性词语/教材口径不一致”判断：需同时参考“教材切片+关联切片”，冲突时以更具体且与题干直接相关的切片为准

---

## 8. 教学复盘（LLM，node_layer3_teaching_b）

### 8.1 解析质量（explanation_quality）

| 字段 | 说明 |
|------|------|
| first_part_missing_target_title | 第 1 段缺目标题内容（路由前三个标题）；解析中不要求出现「目标题：」字样 |
| first_part_missing_level | 第 1 段缺“分级” |
| first_part_missing_textbook_raw | 第 1 段缺“教材原文内容” |
| first_part_structured_issues | 第 1 段结构/顺序/完整性问题 |
| analysis_rewrite_sufficient | 第 2 段是否充分转述（非直接粘贴教材） |
| three_part_semantic_invalid | 三段语义冲突/结论不自洽 |
| multi_option_coverage_rate | 多选是否覆盖所有选项 |

### 8.2 解析基本规范

- 第 1 段需包含：**目标题内容（路由前三个标题）+ 分级 + 教材原文**（不要求写「目标题：」字样）
- 第 2 段必须用自己的话解释，不能直接粘贴教材
- 第 3 段结论需与第 2 段分析语义一致

### 8.3 教学价值（teaching_value）

- cognitive_level、business_relevance、discrimination、estimated_pass_rate
- has_assessment_value
- **考核点分析（必须）**：先识别题目主要考核点（main_assessment_points），再结合**教材切片+关联切片**与**经纪人日常工作**分析，判断考核点是否找偏（assessment_point_aligned）；若考核点与切片内容或实务场景脱节，写入 assessment_point_issues，并可在 issues 中体现、酌情降低 has_assessment_value

### 8.4 解析质量字段门禁矩阵（当前实现）

- 参与 `explanation_pass` 判定（命中任一则 FAIL）：
  - `analysis_rewrite_sufficient == false`
  - `three_part_semantic_invalid == true`
  - `first_part_missing_target_title == true`
  - `first_part_missing_level == true`
  - `first_part_missing_textbook_raw == true`
  - `first_part_structured_issues` 非空
  - `theory_support_present == false`
  - `business_support_present == false`
- 当前只做展示/记录，不直接触发 `explanation_pass` 失败：
  - `has_forbidden_media`
  - `multi_option_coverage_rate`
  - `missing_options`

### 8.5 has_assessment_value 的字段改写

- 当 `has_assessment_value=false` 时，系统会强制：
  - `teaching_value.discrimination = "低"`
  - `teaching_value.estimated_pass_rate = max(原值, 0.9)`
- 该改写用于统一“低考核价值”题目的可解释统计表现。

---

## 9. 计算题专项（LLM，node_layer3_calc_branch）

**仅当 is_calculation=true 时执行。**

### 9.1 输出

- code_evaluator：逆向计算与错误路径可解释性
- complexity：digit_complexity_too_high、step_count_too_high、complex_decimal_present、mental_math_level

### 9.2 计算题不通过条件

- digit_complexity_too_high
- step_count_too_high
- complex_decimal_present
- mental_math_level == "明显需计算器"

### 9.3 code_evaluator 字段契约（主流程 JSON 口径）

- 主流程按 `layer3_calc_branch` 提示词返回的 `code_evaluator` 字段消费，不调用独立文件 `src/agents/code_evaluator.py`。
- 当前主流程期望字段：
  - `issues`（列表）
  - `evidence`（列表）
  - `wrong_path_count`（整数）
  - `mapped_to_options`（布尔）
- 上述字段用于：
  - 组装 `calculation_issues`
  - 写入 `calculation_data.code_evaluator_*`
  - 驱动“计算可执行性与复杂度”维度解释

---

## 10. 决策逻辑（node_aggregate）

### 10.1 致命信号（任一命中 → REJECT）

| 信号 | 来源 |
|------|------|
| solver.ambiguity_flag | 盲答不可判定（score=0，或 predicted_answer 为空/NONE，或 fatal_logic_issues 非空） |
| knowledge_gate_reject | 知识门短路 |
| not realism_pass | 业务真实性不通过 |
| slice_conflict_invalid | 与教材切片冲突 |
| out_of_scope | 超纲 |
| constraint_drift | 限定词漂移 |
| leakage_still_invalid | 泄题 |
| legal_math_closure_invalid | 法理/数学闭环不成立 |
| fatal_doctrinaire_gate | 教条主义拦截（负面情绪/担忧 + 放大问题且无补救） |
| stem_over_400 | 题干 >400 字 |
| any_option_over_200 | 任一选项 >200 字 |

### 10.2 REVIEW 信号（任一命中 → REVIEW）

- not hard_pass（结构/表达硬错，从 REJECT 降级）
- risk_level == HIGH
- not rigor_pass（排除致命项后的严谨性问题）
- explicit_distractor_gate_triggered（干扰项冲突）
- not explanation_pass
- not teaching_pass
- not knowledge_pass
- 计算题且 not calc_pass
- risk_level == MEDIUM

### 10.3 其余 → PASS

### 10.4 分数与置信度

- overall_score：逻辑+知识+干扰项+教学+风险 五维加权，REJECT 时上限 59，REVIEW 时上限 79
- confidence：PASS 时 0.95，否则 0.9

---

## 11. 维度结果（dimension_results）

| 维度 | 说明 |
|------|------|
| 业务真实性 | 来自 node_layer3_surface_a |
| 严谨合规性 | 含 leakage、term_mismatch 等 |
| 干扰项质量 | 含 overlap、stem_option_conflicts 等 |
| 解析质量 | 来自 node_layer3_teaching_b |
| 教学价值 | 来自 node_layer3_teaching_b |
| 知识匹配 | 来自 node_layer2_knowledge_gate |
| 计算可执行性与复杂度 | 仅计算题，来自 node_layer3_calc_branch |

**SKIP 说明**：节点未执行时 status=SKIP，展示时应输出“未检测”而非“无问题”。

**details 补充**：`干扰项质量` 维度的 details 额外包含：

- `explicit_gate_triggered`
- `gate_signals_count`

### 11.1 details 字段对齐（关键键）

- `业务真实性.details`：`passed`、`issues`、`score`、`slice_conflict_invalid`、`slice_conflict_issues`、`scene_binding_required_violation`、`workflow_sequence_violation`、`scenario_dialogue_or_objection`、`contains_business_action`、`negative_emotion_detected`、`amplifies_defect_without_remedy`。
- `严谨合规性.details`：`leakage_still_invalid`、`explanation_conflict_still_invalid`、`name_consistency_still_invalid`、`legal_math_closure_invalid`、`term_mismatch_issues`、`issues`、`warnings`。
- `解析质量.details`：`analysis_rewrite_sufficient`、`three_part_semantic_invalid`、`first_part_missing_*`、`first_part_structured_issues`、`theory_support_present`、`business_support_present`（以及其他解释性字段）。
- `教学价值.details`：`has_assessment_value`、`main_assessment_points`、`assessment_point_aligned`、`assessment_point_issues`、`assessment_value_issues`。
- `知识匹配.details`：`out_of_scope`、`constraint_drift`、`single_knowledge_point_invalid`、`*_evidence`、`recommendation`。
- `计算可执行性与复杂度.details`：`enabled`、`digit_complexity_too_high`、`step_count_too_high`、`complex_decimal_present`、`mental_math_level`、`complexity_level`、`code_evaluator_issues`、`code_evaluator_evidence`、`wrong_path_count`、`mapped_to_options`。

---

## 12. Excel 输入规范

### 12.1 列名映射

| 系统字段 | Excel 列名（优先） |
|----------|-------------------|
| 题干 | 题干(必填) |
| 选项 | 选项A(必填)、选项B(必填)、选项C、选项D |
| 答案 | 答案选项(必填) |
| 解析 | 题目解析 |
| 教材切片 | 切片原文 |
| 关联切片 | 关联切片、关联切片原文 |
| 参考切片 | 参考切片、参考切片原文 |
| 母题 | 母题、母题题干、关联母题 |
| 锁词 | 术语锁词、锁词 |
| 掌握程度 | 掌握程度 |
| 题目类型 | 题目类型标签（精确值：基础概念/理解记忆、实战应用/推演） |
| 计算题 | 题目计算题标签、是否计算题、计算题标记等 |

### 12.2 题型推断

- 答案含“正确/错误” → 判断题
- 答案 A/B 且前两项为正确/错误 → 判断题
- 答案含逗号/多选格式 → 多选题
- 默认 → 单选题

### 12.3 计算题标记

- 仅按表格显式标记判定，不做关键词猜测
- 支持列：题目计算题标签、是否计算题、计算题标记等

### 12.4 excel_to_word_report CLI 参数

- `--input-excel`、`--output-docx`、`--output-json`、`--progress-json`
- `--sheet`（默认 AI题目，实际常用 Sheet1）
- `--header-row`（0-based，单行表头用 0）
- `--limit`（仅评测前 N 题，0 表示全部）
- `--provider`、`--model`、`--temperature`、`--mock-llm`

---

## 13. 入口与配置

### 13.1 入口

| 入口 | 用途 |
|------|------|
| judge_cli.py | JSON 单题/多题 |
| src.evaluation.batch_runner | Golden JSON 数据集 |
| src.evaluation.excel_to_word_report | Excel 题库 → Word 报告 + JSON |

补充：`run_judge(..., skip_phase1=...)` 参数当前在执行器中未生效（现状为 no-op）。

### 13.2 JSON 输入列名映射（judge_cli、batch_runner）

- correct_answer ← answer
- textbook_slice ← textbook_excerpt
- related_slices ← related_textbook_slices、associated_slices、关联切片
- assessment_type ← 题目类型标签、assessment_type
- reference_slices ← 参考切片、参考切片原文、reference_textbook_slices
- mother_question ← 母题、母题题干、关联母题、parent_question
- examples ← 范例、examples
- term_locks ← 锁词、术语锁词、term_locks
- mastery ← 掌握程度、mastery

### 13.3 配置

- ARK_CONFIG.txt / AIT_CONFIG.txt：LLM 相关（含 CALC_MODEL、CALC_PROVIDER、CALC_FALLBACK_MODEL）
- 环境变量：MIN_RESIDENTIAL_FLOOR_HEIGHT_M、MAX_RESIDENTIAL_FLOOR_HEIGHT_M（层高常识校验）

---

## 14. 盲答与计算辅助（node_layer1_blind_solver）

### 14.1 盲答

- 基于教材主切片 + 关联切片 + 参考切片 + 母题/范例进行推理
- 输出：predicted_answer、reasoning_path、ambiguity_flag
- `ambiguity_flag` 判定：`score==0` 或 `predicted_answer in {"", "NONE"}` 或 `fatal_logic_issues` 非空
- 当 `llm` 未配置时，默认输出 `predicted_answer=""`、`ambiguity_flag=true`，并记入 `【LLM未配置】` 问题

### 14.2 计算题辅助（仅 is_calculation=true）

1. **规划阶段**（`_plan_calculation`）：LLM 判断 need_calculation，若 true 则生成 python_code
2. **执行阶段**（`_execute_calculation_code`）：调用 safe_python_runner，超时 2.5s
3. **失败处理**：need_calculation=true 但无有效代码、或执行失败 → calc_hard_fail，score=0，predicted_answer="NONE"，ambiguity_flag=true
4. **模型切换**：CALC_MODEL 独立配置，GPT 限流时可切换 CALC_FALLBACK_MODEL（如 DeepSeek）
   - 触发细则（当前实现）：读取 `.gpt_rate_limit.txt` 最近时间戳，按 12s 窗口估算等待；若预计等待 >5s，则切换到 `CALC_FALLBACK_MODEL`。

### 14.3 计算辅助输入

- mastery、term_locks、kb_context（主切片+关联+参考+母题）、examples_text、examples_have_calc

### 14.4 输入裁剪策略（当前实现）

- `related_slices`：最多取前 8 条
- `reference_slices`：最多取前 8 条
- `examples`：最多取前 5 条
- 若 `mother_question` 非空，会先追加为一个范例项，再整体执行“最多 5 条”裁剪

---

## 15. 安全代码执行（safe_python_runner）

| 约束 | 说明 |
|------|------|
| 禁止导入 | os、sys、subprocess、socket、pathlib、shutil、resource、multiprocessing、threading、ctypes |
| 禁止调用 | eval、exec、compile、open、__import__、input |
| 超时 | 默认 2.5s |
| 输出 | 需 `print(json.dumps(...))` 或通过 `__judge_emit` 输出 JSON |

---

## 16. 套卷级规则（batch checks）

**Excel 评测时**在 `_compute_batch_rule_checks` 与 `_compute_batch_llm_negation_check` 中执行。

| 规则 | 阈值 | 说明 |
|------|------|------|
| 判断题正误比例 | 正确占比 0.3~0.7（样本≥6 时）；0.2~0.8（样本<6 时） | 避免正误失衡 |
| 多选 ABCD 全对 | ≤5 题 | 避免全选过多 |
| 答案字母分布 | 最大最小差占比 ≤0.35 | A/B/C/D 均衡 |
| 否定设问占比 | ≤0.25（LLM 语义判定） | 按 layer4_batch_negation_check 提示词 |

---

## 17. 表层质量提示词选择

- `assessment_type == "实战应用/推演"` → `layer3_surface_quality_practical.md`
- 否则 → `layer3_surface_quality_concept.md`

---

## 18. 风险等级与联动

| 条件 | risk_level |
|------|------------|
| leakage_still_invalid | HIGH |
| name_consistency_still_invalid 或 explanation_conflict_still_invalid | MEDIUM |
| realism_issues 存在且当前为 LOW | 按 `_risk_level_from_issues`：≥3 条为 HIGH，否则 MEDIUM |

- `dispute_risk`：leakage 或 explanation_conflict 任一为 true
- `practice_conflict`：realism_issues 非空时

---

## 19. 分数计算（_score_from_state）

| 维度 | 规则 |
|------|------|
| logic | ambiguity_flag 则 2；否则 solvability_baseline 否则 4；否则 10 |
| knowledge | fingerprint_matched 且 rule_constraints_kept 则 10，否则 6 |
| distractor | logic_homogenous/format_aligned 否各减 3；balance_strength 否减 3；范围 1~10 |
| teaching | pass_rate>0.9 或 <0.2 减 2；discrimination 低减 2；范围 1~10 |
| risk | LOW=10，MEDIUM=7，HIGH=3 |
| confidence | PASS 为 0.95，否则 0.9 |

overall_score = (logic+knowledge+distractor+teaching+risk)/50*100，REJECT 时上限 59，REVIEW 时上限 79。

---

## 20. JudgeReport 输出结构

| 字段 | 说明 |
|------|------|
| question_id、assessment_type、trace_id、version、prompt_version | 元数据 |
| decision、hard_pass、scores、overall_score | 决策与分数 |
| evidence | slice_id、quotes、ask_judgement_evidence、substitution_evidence、uniqueness_evidence |
| reasons | 聚合后的所有问题描述 |
| hard_gate | structure_legal、expression_standard、solvability_baseline |
| semantic_drift | limit_words_consistent、rule_constraints_kept、fingerprint_matched |
| solver_validation | predicted_answer、reasoning_path、ambiguity_flag |
| distractor_quality、knowledge_match、teaching_value、risk_assessment | 各维度 |
| observability | critic_loops、llm_calls、failed_calls、tokens、latency_ms、unstable_flag |
| costs | per_question_usd、per_node_usd、per_model_usd、cost_alert |
| dimension_results | 各维度 status、issues、details |
| actionable_feedback | LLM 聚合建议；失败时回退为前 8 条 reasons+warnings+recommendation_suggestions 拼接 |

### 20.4 聚合节点（LLM 聚合）

- 聚合节点在生成 `decision` 后，调用 LLM 对 `reasons` 做“去重+归并+改写”，输出最终 `reasons` 与 `actionable_feedback`。
- 若 LLM 失败或返回不合规，回退为原始 `reasons` 和本地拼接的 `actionable_feedback`。

### 20.1 evidence 生成细节（当前实现）

- `slice_id` 当前固定为 `slice_001`
- `quotes` 当前固定空数组
- `uniqueness_evidence` 由 gate recheck 的 `uniqueness_evidence` 与计算日志合并，最多保留前 8 条

### 20.2 observability 细节（当前实现）

- `unstable_flag=true` 条件：`failed_calls > 0` 或 `latency_ms > 60000`
- `critic_loops` 当前固定为 0
- 当 LLM JSON 解析失败时，`observability.last_raw_response` 记录原始返回（最多 2000 字符），`observability.last_raw_truncated` 标记是否被截断。

### 20.3 costs 细节（当前实现）

- `per_question_usd = round((total_tokens / 1000) * 0.0002, 6)`
- `per_node_usd` 与 `per_model_usd` 当前为空字典
- `cost_alert` 当前固定为 `false`

---

## 21. Word 报告结构（excel_to_word_report）

- **汇总**：总题数、Decision 分布、套卷规则判定
- **高频问题 Top**：基于“根因去重”的 Top 8（同类问题合并计数）
- **逐题结果**：question_id、assessment_type、decision、hard_pass、confidence、scores、reasons（根因去重后按类别聚合）、分维执行结果、整合说明、observability、costs
- **整合说明**：结论（可通过/需小修/不通过）、归因、建议

### 21.1 根因去重规则（Word 报告）

- 仅影响 Word 报告展示，不改变 JudgeReport 的 `reasons` 原始内容。
- 对每题 `reasons` 做“同类问题聚合”，避免同义重复刷屏。
- 分类维度（默认顺序）：`格式` / `知识` / `解析` / `教学` / `其他`
- “高频问题 Top”与“逐题 reasons”均使用该聚合结果。

---

## 22. Golden Dataset 回归（batch_runner）

- 输入：JSON 数组，每项含 `expected_decision`（pass/review/reject）
- 输出：reports.json、metrics.json
- metrics：total、labeled、accuracy、pass_confusion（tp/tn/fp/fn）、false_accept_rate、false_reject_rate

---

## 23. ReliableLLMClient

- 超时、重试、JSON 提取
- fallback：各节点均有默认 JSON，解析失败时使用

---

## 24. Excel Unnamed 列兼容

- 若列名均为 `Unnamed:*`，且首行含「题目序号」「题干(必填)」「答案选项(必填)」，则首行视为业务表头，第二行起为数据

---

## 25. 提示词文件清单

| 文件 | 用途 |
|------|------|
| layer1_blind_solver.md | 盲答 |
| layer2_knowledge_gate.md | 知识匹配 |
| layer3_basic_rules_gate.md | 基础规则复核 |
| layer3_surface_quality_concept.md | 表层质量（概念题） |
| layer3_surface_quality_practical.md | 表层质量（实战题） |
| layer3_teaching_review.md | 教学复盘 |
| layer3_calc_branch.md | 计算题专项 |
| layer5_aggregate.md | 聚合节点（LLM 去重与归因） |
| layer4_batch_negation_check.md | 套卷否定设问占比 |

---

## 26. 异常兜底

- 未生成 final_report 时：decision=REJECT，hard_pass=False，reasons=["系统异常：未生成 final_report"]

---

## 27. DeterministicFilter（历史/备用）

位于 `src/filters/deterministic_filter.py`，**当前未接入主 pipeline**。主 pipeline 硬规则由 `_basic_rules_code_checks` 承担。

其包含的规则（供参考）：

| 规则 | 说明 |
|------|------|
| 括号 | 全角、作答占位、句首禁止 |
| 标点 | 单引号禁用 |
| 选项 | 结尾禁标点 |
| 选项层级冲突 | 父子类选项（需结合教材切片）→ warning |
| 违禁词 | 以上皆是/皆非/都对/都错、全部正确/错误、最重要、实实在在、不是不、并非不 |
| AI 幻觉词 | 外接、上交 |
| 冗余场景 | 师傅告诉徒弟、新人培训、通过中介买了房 |
| 称谓 | 先生/女士、张三、李四、贾董事 |
| 得房率 | <70% → warning |
| 层高 | 复式>6 米 → warning |
| 层数 vs 高度 | 平均层高 2.8~6.0m |
| 地理一致性 | 题干城市 vs 教材城市 |
| 泄题 | 正确选项关键词重合 → warning（仅关键词，非主 pipeline 泄题定义） |
