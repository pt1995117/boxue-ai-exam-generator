# Trace 接入完整性检查报告

## 1. 检查范围

- 每个**大模型节点**是否都有：**模型名称**、**响应时长**、**消耗 token** 的记录。
- **修复策略 (Fixer)**、**Rerouter 后重新走 Specialist/Writer/Critic** 等全流程是否全部进入同一题的 `llm_trace`。

---

## 2. 单次 LLM 调用记录结构（call_llm 产出）

`exam_graph.call_llm` 在每次调用后通过 `build_record` 产出一条记录，字段如下：

| 字段 | 含义 | 说明 |
|------|------|------|
| `trace_id` | 链路 ID | 来自 state |
| `question_id` | 题目 ID | 来自 state |
| `node` | 节点名（含子步骤） | 如 `router.route`, `critic.review` |
| `provider` | 实际使用的 provider | 如 `ait`, `ark` |
| `model` | 实际使用的模型名 | 如 `gpt-4o`, `deepseek-chat` |
| `prompt_tokens` | 输入 token 数 | 成功时有值，失败可为 None |
| `completion_tokens` | 输出 token 数 | 同上 |
| `total_tokens` | 总 token | 同上 |
| `latency_ms` | 本次调用耗时（毫秒） | 每次都有 |
| `retries` | 重试次数 | 每次都有 |
| `success` | 是否成功 | 每次都有 |
| `error` | 错误信息（失败时） | 可选 |
| `ts` | 时间戳 | ISO 格式 |

结论：**每个大模型节点单次调用都具备「模型名称、响应时长、token 消耗」记录**（失败时 token 可能为 None，成本计算中按 0 处理）。

---

## 3. 各节点 LLM 调用与 llm_trace 接入情况

### 3.1 Router

| 子步骤 | node_name | 是否 call_llm | 是否写入 llm_trace |
|--------|------------|----------------|--------------------|
| 路由推荐 | `router.route` | 是 | 是（返回 `llm_trace`） |

- 仅 1 处 `call_llm`，记录追加到 `llm_records`，节点返回 `state_updates` 中含 `llm_trace`。

### 3.2 Specialist（初稿 / 修复模式）

| 子步骤 | node_name | 是否 call_llm | 是否写入 llm_trace |
|--------|------------|----------------|--------------------|
| 修复模式重写 | `specialist.repair` | 是 | 是 |
| 初稿生成 | `specialist.draft` | 是 | 是 |

- Rerouter 后再次进入 Specialist 时，会走 `specialist.repair` 或 `specialist.draft`，两者都经 `call_llm` 并返回 `llm_trace`，与主流程一致。

### 3.3 Writer

| 子步骤 | node_name | 是否 call_llm | 是否写入 llm_trace |
|--------|------------|----------------|--------------------|
| 润色定稿 | `writer.finalize` | 是 | 是 |

- 所有异常/提前返回路径均带 `llm_trace: llm_records`（可为空列表）。

### 3.4 Critic

| 子步骤 | node_name | 是否 call_llm | 是否写入 llm_trace |
|--------|------------|----------------|--------------------|
| 可读性检查 | `critic.readability` | 是 | 是 |
| 计算题规划 | `critic.plan` | 是 | 是 |
| 计算题代码检查 | `critic.code_check` | 是 | 是 |
| 计算题代码重试 | `critic.codegen_retry` | 是 | 是 |
| 主审核 LLM | `critic.review` | 是 | 是 |

- 规则类提前返回（题型不一致、模式强约束、括号格式、材料缺失、重复题、可读性失败等）均显式带 `"llm_trace": llm_records`（可能为空）。
- 通过/不通过的主路径返回也均含 `llm_trace`。
- **多次走到 Critic**（如 Fixer 修复后再审）：每次 Critic 执行都会产生新的 `critic.*` 记录，并通过 state 的 `llm_trace` 累加，最终在 admin_api 侧被完整同步到该题的 `question_trace.llm_trace`。

### 3.5 Fixer

| 子步骤 | node_name | 是否 call_llm | 是否写入 llm_trace |
|--------|------------|----------------|--------------------|
| 无题时重新生成 | `fixer.regenerate_initial` | 是 | 是 |
| 按反馈修复 | `fixer.apply_fix` | 是 | 是 |
| 无改动时强制重写 | `fixer.force_regenerate` | 是 | 是 |

- 所有正常/异常返回路径均带 `llm_trace: llm_records`。
- **多次修复**（Critic → Fixer → Critic → …）：每次 Fixer 的 1～2 次 LLM 调用都会追加到同一 state 的 `llm_trace`，全流程可见。

### 3.6 Calculator（计算专家）

| 子步骤 | node_name | 是否 call_llm | 是否写入 llm_trace |
|--------|------------|----------------|--------------------|
| 代码生成 | `calculator.codegen` | 是 | 是 |
| 初稿生成 | `calculator.draft` | 是 | 是 |

- 正常返回与异常返回均含 `llm_trace`。

---

## 4. 全流程与 Rerouter / 多轮修复 的 trace 完整性

### 4.1 State 累加方式

- `AgentState` 中：`llm_trace: Annotated[List[Dict], operator.add]`。
- 每个节点返回的 `llm_trace` 会与当前 state 的 `llm_trace` **按列表相加**，因此：
  - 同一题在 **Router → Specialist → Writer → Critic** 的首次链路；
  - **Critic 不通过 → Fixer → 再 Critic** 的多轮修复；
  - **Rerouter → Specialist（repair/draft）→ Writer → Critic** 的重新生成；
  - 计算题路径下的 **Calculator → Writer → Critic**；
  
  上述所有 LLM 调用都会按顺序累加在同一题的 state `llm_trace` 中。

### 4.2 Admin API 侧如何写入 question_trace

- 题目维度在内存中维护 `question_llm_trace`，在 **每步 stream 事件** 后：
  - 从该步的 **完整 state** 中读取 `state_update.get("llm_trace")`；
  - 使用 **整段覆盖**：`question_llm_trace[:] = [x for x in llm_records if isinstance(x, dict)]`，避免因「每步都是全量 state」而重复 append 导致重复记录。
- 题目结束时将 `question_llm_trace` 写入 `question_trace["llm_trace"]`，并据此生成 `llm_summary`、成本等。

因此：**修复、Rerouter 后的全流程都会落在同一题的 `llm_trace` 中，且不重复**。

---

## 5. 小结与结论

| 检查项 | 结果 |
|--------|------|
| 每个大模型节点是否有「模型名称」 | 是，`call_llm` 的 record 中必有 `model`（及 `provider`） |
| 每个大模型节点是否有「响应时长」 | 是，`latency_ms` 每次都有 |
| 每个大模型节点是否有「token 消耗」 | 是，`prompt_tokens` / `completion_tokens` / `total_tokens` 成功时有，失败为 None（成本按 0） |
| 修复策略多轮 Fixer/Critic 是否全记录 | 是，state 用 `operator.add` 累加，每题一条链全包含 |
| Rerouter 后 Specialist/Writer/Critic 是否全记录 | 是，同上 |
| 是否可能重复同一条调用 | 否，admin_api 按「当前步全量 state 的 llm_trace」覆盖式同步，不重复 |

**结论：当前 trace 接入完整，每个大模型节点都有模型名、响应时长和 token 记录，且修复、Rerouter 后的全流程均完整进入该题的 llm_trace。**

---

## 6. 已做代码修正（与本检查相关）

- **admin_api.py**：在同步/流式两种调用路径中，对 `question_llm_trace` 的更新由「对 `state_update["llm_trace"]` 做 extend」改为「用当前步的完整 `llm_trace` 覆盖 `question_llm_trace`」，避免 LangGraph stream 每步返回全量 state 时造成同一条调用被重复追加。
