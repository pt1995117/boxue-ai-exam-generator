# 离线 Judge 输入数据说明

本文档说明：**离线 Judge 需要哪些输入**、**这些数据从哪里来**、**当前是怎么组装的**。

---

## 一、Judge 实际接收的数据结构（QuestionInput）

离线 Judge 模块入口是 `离线Judge/src/pipeline/runner.run_judge(question: QuestionInput, llm)`，其中 `QuestionInput` 定义在 `离线Judge/src/schemas/evaluation.py`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question_id` | str | 是 | 题目唯一 ID |
| `stem` | str | 是 | 题干 |
| `options` | list[str] | 是 | 选项列表（至少 1 项） |
| `correct_answer` | str | 是 | 正确答案（如 "A"） |
| `explanation` | str | 是 | 解析 |
| `textbook_slice` | str | 是 | 教材切片原文（无则传 "(无切片原文)"） |
| `question_type` | str | 否 | 默认 "single_choice"；可选 "multi_choice" / "true_false" |
| `assessment_type` | str | 否 | 默认 "基础概念/理解记忆"；或 "实战应用/推演" |
| `related_slices` / `reference_slices` / `mother_question` / `examples` 等 | - | 否 | 有默认值，当前 admin 侧未传 |

也就是说，**给离线 Judge 的“输入数据”就是上述字段**，核心是：题干、选项、正确答案、解析、教材切片，以及题型/考查类型。

---

## 二、当前有哪些入口会“准备数据并调用 Judge”

有两类入口会准备这些数据并调用 Judge：

1. **按 process_trace 单题跑 Judge**（基于“单题生成过程”的一条 trace）
2. **按已落库的 run 题目跑 Judge**（单题测评阶段 / 补跑 Judge）

下面分别说：**给了哪些数据、数据来源是什么**。

---

## 三、入口 1：按 process_trace 跑 Judge（生成阶段用）

- **调用链**：`_run_offline_judge_for_trace(question_trace, config_payload, llm)`  
  → `_trace_to_question_input(question_trace, config_payload)` 得到 dict  
  → 转成 `QuestionInput` 后 `run_judge(qin, llm)`。

- **数据来源**：全部来自**当次生成的单题 process_trace 一条** + **run 的 config**。

  - **question_trace**：来自出题图/生成流水线里该题的“过程记录”，包含：
    - `question_trace["final_json"]`：当前题目最终产出
      - `题干` → Judge 的 `stem`
      - `正确答案` → `correct_answer`
      - `解析` → `explanation`
      - `选项1`～`选项4` → `options` 列表
    - `question_trace["slice_content"]` → Judge 的 `textbook_slice`（无则 "(无切片原文)"）
    - `question_trace["question_id"]` → `question_id`
  - **config_payload**：来自 run 的 config（请求参数）
    - `question_type`（单选题/多选题/判断题）→ `question_type`（single_choice / multi_choice / true_false）
    - `generation_mode`（是否「实战应用/推演」）→ `assessment_type`

- **总结**：  
  - **给了 Judge 的数据**：题干、选项 1～4、正确答案、解析、教材切片、题型、考查类型、question_id。  
  - **数据来源**：当题 `process_trace` 的 `final_json` + `slice_content`，以及 run 的 `config`。

---

## 四、入口 2：按已落库 run 的题目跑 Judge（单题测评 / 补跑）

- **调用链**：  
  `POST /api/<tenant_id>/qa/runs/<run_id>/run-judge`  
  → 对每个选中的题目调用 `_run_offline_judge_for_question(q, config_payload, judge_llm)`  
  → 内部用 `_build_judge_input_from_question(question)` 得到 `judge_input` 字典  
  → 再转成 `QuestionInput`，调用 `run_judge(qin, llm)`。

- **数据来源**：**当前 run 在 qa_runs.jsonl 里存的那条题目 `question`** + **该 run 的 `config`**。

  1. **优先用题目里已有的 `judge_input`**  
     - 若 `question["judge_input"]` 存在且题干非空，则直接用这里的：
       - `stem`, `options`, `correct_answer`, `explanation`, `textbook_slice`
     - 这些 `judge_input` 最初是从哪来的：  
        落库时由 `_score_question_from_trace(question_trace)` 从**同一条题的 process_trace** 里算出来的（见下一节「落库时 judge_input 的生成」）。

  2. **若没有可用的 `judge_input`**（例如历史 run、或当时没存），则用题目其它字段“拼”出一份：
     - `question["question_text"]` 或 `question["final_json"]["题干"]` → `stem`（都没有则 "(题干缺失)"）
     - `question["answer"]` 或 `question["final_json"]["正确答案"]` → `correct_answer`
     - `question["options"]` 或从 `question["final_json"]["选项1"～"选项4"]` 解析 → `options`
     - `question["explanation"]` 或 `question["final_json"]["解析"]` → `explanation`
     - `question["slice_content"]` 或 `question["textbook_slice"]` 或 `judge_input["textbook_slice"]` → `textbook_slice`（无则 "(无切片原文)"）

  **config_payload** 同样来自该 run 的 `config`：  
  - `question_type` → Judge 的 `question_type`  
  - `generation_mode` → `assessment_type`

- **总结**：  
  - **给了 Judge 的数据**：同上（题干、选项、正确答案、解析、教材切片、题型、考查类型、question_id）。  
  - **数据来源**：  
    - 有 `judge_input` 时：来自**落库时由 process_trace 生成的 judge_input**（见下）；  
    - 没有时：来自**当前题目在 run 里的字段**（question_text / answer / final_json / slice_content 等）。

---

## 五、落库时 judge_input 的生成（和“数据来源”的闭环）

run 落库时，每条题目是由 `_score_question_from_trace(question_trace)` 生成的。其中 **judge_input** 的生成方式为：

- **数据来源**：同样是**该题的 process_trace 一条**。
- **字段映射**：
  - `question_trace["final_json"]["题干"]` → `judge_input["stem"]`
  - `question_trace["final_json"]["选项1"～"选项4"]` → `judge_input["options"]`
  - `question_trace["final_json"]["正确答案"]` → `judge_input["correct_answer"]`
  - `question_trace["final_json"]["解析"]` → `judge_input["explanation"]`
  - `question_trace["slice_content"]` → `judge_input["textbook_slice"]`（无则 "(无切片原文)"）

因此：  
- **生成阶段**若在写 run 时带了 `judge_input`，那单题测评阶段用的就是这份“当时从 process_trace 算出来的”数据。  
- **若当时没存 judge_input**（或 run 是旧格式），单题测评阶段就靠 `_build_judge_input_from_question` 用题目上的 `question_text` / `answer` / `final_json` / `slice_content` 等再拼一份给 Judge。

---

## 六、汇总表：给离线 Judge 的数据与来源

| Judge 输入字段 | 入口1（trace）来源 | 入口2（run 题目）来源 |
|----------------|--------------------|------------------------|
| question_id | question_trace["question_id"] | question["question_id"] |
| stem | final_json["题干"] | judge_input["stem"] 或 question_text / final_json["题干"] |
| options | final_json["选项1"～"选项4"] | judge_input["options"] 或 question["options"] / final_json |
| correct_answer | final_json["正确答案"] | judge_input["correct_answer"] 或 question["answer"] / final_json |
| explanation | final_json["解析"] | judge_input["explanation"] 或 question["explanation"] / final_json |
| textbook_slice | question_trace["slice_content"] | judge_input["textbook_slice"] 或 slice_content / textbook_slice |
| question_type | config["question_type"] | config["question_type"] |
| assessment_type | config["generation_mode"] | config["generation_mode"] |

**结论**：  
- **Judge 做输入数据处理时**，实际拿到的就是上面这些字段；  
- **数据来源**要么是**当题的 process_trace（final_json + slice_content）+ run config**，要么是**已落库 run 里该题的 judge_input / question_text / answer / final_json / slice_content 等 + run config**。  
若你后续要在 Judge 侧做“输入数据清洗/归一化”，只要针对上述字段和来源做即可。
