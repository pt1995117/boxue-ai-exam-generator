# 离线 Judge 评分接入质量评估 — 具体方案

> 目标：将「离线 Judge」的评分结果接入现有「质量评估」模块，在总览、单题评估、批量指标中展示 Judge 的 decision、overall_score、维度分及 reasons，并可选用 Judge 结果参与告警与发布评估。

---

## 一、现状简述

### 1.1 质量评估（当前）

- **数据来源**：生成任务完成后，`admin_api._build_qa_run_payload()` 根据 `process_trace` 构建 qa_run。
- **题目级评分**：`_score_question_from_trace(question_trace)` 从 **exam_graph 的 critic 节点结果**（`critic_result`）解析得到：
  - `quality`: logic_score, distractor_score, knowledge_match_score, teaching_value_score（规则/启发式折算）
  - `risk`: level (low/medium/high), tags
  - `hard_gate`: pass, failed_rules
  - `stability`: critic_loops, llm_calls, tokens, latency_ms 等
- **批量指标**：由上述题目级数据聚合（hard_pass_rate、quality_score_avg、logic_pass_rate、out_of_scope_rate 等），写入 `batch_metrics`，并用于告警与发布评估。
- **前端**：`QualityEvaluationPage.jsx` 展示总览、单题评估表、批量指标、趋势、漂移、发布评估、告警、阈值配置等。

### 1.2 离线 Judge

- **入口**：`离线Judge/src/pipeline/runner.run_judge(question: QuestionInput, llm) -> JudgeReport`
- **输入**：`QuestionInput`（question_id, stem, options, correct_answer, explanation, textbook_slice, assessment_type, question_type, is_calculation 等）。
- **输出**：`JudgeReport`：
  - `decision`: PASS / REVIEW / REJECT
  - `overall_score`: 0–100
  - `scores`: logic, knowledge, distractor, teaching, risk (各 1–10)，confidence
  - `dimension_results`: 各维度 PASS/FAIL/SKIP 及 issues
  - `reasons`, `actionable_feedback`
  - `hard_gate`, `semantic_drift`, `solver_validation`, `distractor_quality`, `knowledge_match`, `teaching_value`, `risk_assessment` 等结构化维度。

---

## 二、接入策略（推荐）

- **并行双轨**：保留现有「基于 critic_result 的 _score_question_from_trace」逻辑不变；新增「离线 Judge 评分」作为**附加数据**。
- **触发时机**：在 `_build_qa_run_payload` 构建 qa_run 时，对 `process_trace` 中每题**同步或异步**调用 Judge，将结果写入每题及 batch_metrics。
- **可选开关**：通过租户/任务配置控制是否启用 Judge 评分（如 `config.enable_offline_judge: true`），便于灰度与降级。

---

## 三、数据流与接口

### 3.1 Trace → QuestionInput 映射

`process_trace` 中每条题目已包含（batch 与 stream 均已写入）：

| 来源字段 | QuestionInput 字段 | 说明 |
|----------|--------------------|------|
| `question_trace["question_id"]` | question_id | 直接映射 |
| `final_json["题干"]` | stem | 题干 |
| `final_json["选项1"]`…`选项4`（或 2 选项判断） | options | list[str]，按顺序 |
| `final_json["正确答案"]` | correct_answer | A/B/C/D 或 AB/AC… |
| `final_json["解析"]` | explanation | 解析全文 |
| `question_trace["slice_content"]` | textbook_slice | 已有，见 admin_api 4209–4214（batch）、4595–4610（stream） |
| 空或从 config 取 | related_slices, reference_slices | 可选，trace 无则 [] |
| `config_payload["generation_mode"]` | assessment_type | "基础概念/理解记忆" / "实战应用/推演" / 默认"基础概念/理解记忆" |
| `config_payload["question_type"]` | question_type | 单选题→single_choice，多选题→multi_choice，判断题→true_false |
| 可选 | is_calculation | 可由题型/题干启发式或 config 传入 |

实现位置：在 `admin_api.py` 中新增 `_trace_to_question_input(question_trace, config_payload) -> QuestionInput`（或返回 dict 再构造 QuestionInput），供 Judge 调用前使用。

### 3.2 调用 Judge 的时机与方式

- **推荐**：在 `_build_qa_run_payload` 内，对 `process_trace` 逐题调用 Judge（或批量调用，见下），再将 Judge 结果合并进「题目级」与「batch_metrics」。
- **LLM**：Judge 需要 `llm`（Runnable）。可从 `离线Judge/src/llm/factory.build_llm(...)` 按环境变量（如 OPENAI_API_KEY）构建；若与主项目共用同一套 key，需约定 provider/model。建议在 admin_api 内 lazy import Judge 的 `run_judge` 与 `build_llm`，失败时可降级为「不写 judge 字段」。
- **性能**：每题一次 Judge 会多轮 LLM 调用，耗时会明显增加。可选方案：
  - **同步**：先跑 Judge 再写 qa_run（实现简单，时延高）。
  - **异步**：写 qa_run 时先不跑 Judge，由后台任务或单独接口「按 run_id 补算 Judge」并更新 qa_runs.jsonl 中对应 run 的 questions/batch_metrics（需要支持单 run 的更新写入，见 3.4）。
- **建议首版**：同步、可选开关（如 `enable_offline_judge`），便于验证端到端；后续再做异步补算与队列。

### 3.3 题目级：合并 Judge 结果到 questions[i]

在现有 `_score_question_from_trace` 返回结构上**增加**（不替换原有字段）：

- `offline_judge`: 仅当启用且调用成功时存在，结构建议：
  - `decision`: "pass" | "review" | "reject"
  - `overall_score`: 0–100
  - `scores`: { logic, knowledge, distractor, teaching, risk, confidence }
  - `dimension_results`: { "逻辑可解性": { status, issues, details }, … }
  - `reasons`: list[str]
  - `actionable_feedback`: str
  - `hard_gate`, `semantic_drift`, `solver_validation` 等（可按需只存顶层，避免体量过大）

这样前端「单题评估」可同时展示：现有 quality/risk/hard_gate + 新增 offline_judge（decision、overall_score、reasons 等）。

### 3.4 批量指标：batch_metrics 新增 Judge 维度

在 `_build_qa_run_payload` 的 `batch_metrics` 中新增（仅当启用 Judge 且至少一题有结果时写入）：

- `judge_pass_count` / `judge_review_count` / `judge_reject_count`
- `judge_pass_rate` = judge_pass_count / n
- `judge_reject_rate` = judge_reject_count / n
- `judge_overall_score_avg`：overall_score 平均
- 可选：`judge_logic_avg`, `judge_knowledge_avg` 等各维度均分（从 scores 聚合）

告警与发布评估若需按 Judge 结果做阈值（如 judge_pass_rate_min），可在现有 `_build_alerts_for_run` 与 `_default_qa_thresholds` 中增加对应项。

### 3.5 持久化与更新

- 当前 qa_run 是**整条 append** 到 `qa_runs.jsonl`，没有按 run_id 更新单条的现成逻辑。
- **方案 A（首版）**：Judge 在写 qa_run **之前**算完，整条 qa_run 已含 `questions[].offline_judge` 与 `batch_metrics.judge_*`，一次 persist，无需改存储格式。
- **方案 B（异步补算）**：若后续做「先落盘再补算」，需要：
  - 要么支持「按 run_id 找到对应行并覆写该行」（需读全文件再写回或索引），
  - 要么新增 `qa_judge_results.jsonl` 按 run_id + question_id 存 Judge 结果，前端/overview 在展示时合并查询（复杂度较高）。建议首版不做 B，只做 A。

---

## 四、后端实现要点（admin_api.py）

1. **依赖与路径**  
   - 在 `admin_api.py` 中通过 `sys.path` 或项目根把 `离线Judge` 加入路径，lazy import：
     - `from 离线Judge.src.pipeline.runner import run_judge`
     - `from 离线Judge.src.schemas.evaluation import QuestionInput`
     - `from 离线Judge.src.llm.factory import build_llm`（或等价入口）
   - 若 Judge 包名带中文不便，可考虑在项目根增加 `offline_judge_runner.py` 薄封装，仅做 `run_judge` + `build_llm` 的封装，admin_api 只依赖该封装。

2. **_trace_to_question_input(question_trace, config_payload)**  
   - 从 `question_trace` 与 `config_payload` 拼出 QuestionInput 所需字段；options 从 final_json 的 选项1…选项4（或 2）按序组成 list；缺的字段用默认值（如 textbook_slice 为空时用 ""，Judge 内部会按缺省处理）。

3. **_build_qa_run_payload 内**  
   - 若 `config_payload.get("enable_offline_judge")` 为 True：
     - 构建 LLM 一次（或按租户/环境缓存）。
     - 对 `process_trace` 中每条题目：`qin = _trace_to_question_input(...)`，`report = run_judge(qin, llm)`，将 `report.model_dump()`（或精简版）写入该题对应的 `questions[i]["offline_judge"]`。
     - 根据所有题的 Judge 结果聚合 `judge_pass_count` 等，写入 `batch_metrics`。
   - 若 Judge 某题抛错：可记录到该题 `offline_judge_error`，不阻塞整次 qa_run 写入；batch_metrics 中 Judge 相关只统计成功题。

4. **配置**  
   - 生成请求 body 中增加可选 `enable_offline_judge: true`，传入 `config_payload`，这样 batch 与 stream 生成在构建 qa_run 时都能根据配置决定是否调 Judge。

---

## 五、前端展示（QualityEvaluationPage.jsx）

1. **总览 Tab**  
   - 在现有卡片旁（或单独一行）增加：Judge 通过率、Judge 均分（overall_score_avg）、Judge 拒绝率（若有）。数据来自 `overview`（需后端 api_qa_overview 返回这些字段，从最近 run 或聚合 run 的 batch_metrics 取）。

2. **单题评估 Table**  
   - 列：在现有「硬通过、逻辑分、干扰项、考点匹配、风险…」后增加：
     - Judge 结论：PASS/REVIEW/REJECT（Tag 颜色）
     - Judge 总分：overall_score
     - 可选：Judge 维度简写（如 logic/knowledge 分）
   - 展开行：在现有 issues 等之下增加「Judge reasons / actionable_feedback」或「dimension_results」折叠展示。

3. **批量指标 Tab**  
   - 在现有 batch_metrics 列表中展示 `judge_pass_rate`、`judge_overall_score_avg`、`judge_reject_count` 等。

4. **趋势 / 漂移 / 发布评估**  
   - 若希望按 Judge 指标做趋势或发布对比，在对应接口中把 `judge_*` 纳入 compare 与 verdict 逻辑（可选，二期）。

5. **告警与阈值**  
   - 在「阈值配置」中增加 `judge_pass_rate_min`、`judge_reject_rate_max` 等（与现有 hard_pass_rate_min 类似），并在 `_build_alerts_for_run` 中增加对 Judge 指标的判断，写入 qa_alerts。

---

## 六、配置与开关

- **生成接口**：POST body 增加 `enable_offline_judge?: boolean`（默认 false），避免未准备好时影响现有行为。
- **环境**：Judge 使用的 LLM（OPENAI/ANTHROPIC 等）与主项目可共用同一 env；若希望 Judge 用单独模型，可在 Judge 的 build_llm 或 admin_api 的封装里读单独 env（如 JUDGE_OPENAI_MODEL）。

---

## 七、任务拆解（与 task_spec 对齐）

| 序号 | 任务 | 验收点 |
|------|------|--------|
| 1 | 在 admin_api 中实现 `_trace_to_question_input(question_trace, config_payload)`，能覆盖题干/选项/答案/解析/切片/题型/assessment_type | 单测或手工：给定一条 trace + config，得到合法 QuestionInput，且 options/ stem 与 final_json 一致 |
| 2 | 在 admin_api 中 lazy import Judge 的 run_judge、QuestionInput、build_llm；实现「按题调用 run_judge 并捕获异常」的辅助函数 | 在 REPL 或单测中能对一条 QuestionInput 调用 run_judge 并得到 JudgeReport |
| 3 | 在 `_build_qa_run_payload` 中：当 config 启用时对 process_trace 逐题调 Judge，将结果写入 questions[i].offline_judge，并聚合 judge_* 到 batch_metrics | 一次生成后，对应 qa_run 的 questions 含 offline_judge，batch_metrics 含 judge_pass_rate、judge_overall_score_avg 等 |
| 4 | 生成接口（batch + stream）支持 body 参数 enable_offline_judge，并传入 config_payload | 前端传 true 时 config 中带 enable_offline_judge，且 qa_run 中有 Judge 结果 |
| 5 | api_qa_overview 返回 Judge 相关聚合（如 judge_pass_rate、judge_overall_score_avg），便于总览展示 | getQaOverview 响应含 judge_* 字段 |
| 6 | 质量评估页「总览」展示 Judge 通过率/均分；「单题评估」表增加 Judge 结论与总分列，展开行展示 reasons/actionable_feedback；「批量指标」展示 judge_* | 前端可见且数据与 qa_run 一致 |
| 7 |（可选）阈值与告警：_default_qa_thresholds 与 _build_alerts_for_run 支持 judge_pass_rate_min 等，并写入 qa_alerts | 超过阈值时产生告警，并在告警列表可见 |

---

## 八、风险与注意

- **时延**：同步调用 Judge 会显著增加「生成接口」的响应时间（每题多轮 LLM），建议首版仅对少量题或测试环境开启，或后续改为异步补算。
- **依赖**：离线 Judge 的依赖（langchain、openai 等）需与 admin_api 所在环境一致；若部署上 Judge 与主服务分离，可改为通过 HTTP 或队列调用 Judge 服务，再写回结果。
- **版本**：Judge 的 QuestionInput/JudgeReport schema 若有变更，需同步更新 _trace_to_question_input 与 offline_judge 的写入结构，避免前端或告警解析失败。

以上为「离线 Judge 评分接入质量评估」的完整方案，可按任务拆解逐项落地并与 prd_spec / task_spec 对齐。
