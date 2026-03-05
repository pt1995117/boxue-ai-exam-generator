# Critic 节点源码分段索引（基于当前代码）

源文件：`exam_graph.py`

## 1. 节点主体源码（完整）

- `critic_node`：`def critic_node(state: AgentState, config):`
- 行区间：`2549-3548`

说明：
- 该函数源码过长，已按“前半段 / 后半段”拆分到：
  - `02_critic_node_源码_前半段.md`
  - `03_critic_node_源码_后半段.md`

## 2. Critic 依赖的关键函数源码

见文档：`04_critic_node_依赖函数源码.md`

包含：
- `validate_critic_format`
- `material_missing_check`
- `_has_year`
- `_collect_text_fields`
- `build_extended_kb_context`
- `resolve_effective_generation_mode`
- `has_business_context`
- `detect_term_lock_violations`
- `parse_json_from_response`
- `call_llm`
- `execute_python_code`
- `critical_decision`（critic 出口路由决策）

## 3. 流程图

见文档：`05_critic_node_流程图说明.md`

配图文件：
- `critic_node_流程图.jpg`

