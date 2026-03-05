# Critic 节点依赖函数源码（按代码调用链）

源文件：`exam_graph.py`

## 1) 格式与规则校验

- `validate_critic_format`（305-353）
- `material_missing_check`（367-390）
- `_has_year`（392-393）
- `_collect_text_fields`（395-405）
- `has_business_context`（671-680）
- `detect_term_lock_violations`（910-988）

## 2) 上下文构建与模式决议

- `build_extended_kb_context`（523-568）
- `resolve_effective_generation_mode`（622-636）

## 3) LLM与解析链路

- `parse_json_from_response`（1043-1079）
- `call_llm`（1187-...）
- `execute_python_code`（4081-4147）

## 4) Critic出口路由决策

- `critical_decision`（4035-4072）

```python
def critical_decision(state: AgentState):
    """
    智能决策函数：根据 Critic 结果决定下一步
    - pass: 审核通过 → END
    - fix: 轻微问题 → Fixer 修复
    - reroute: 严重问题 → Router 重新路由
    - self_heal: 超限 → 自愈输出
    """
    critic_result = state.get('critic_result', {})
    retry_count = state.get('retry_count', 0)
    
    # 通过
    if critic_result.get('passed'):
        return "pass"

    # Fixer 未满足必修项 → 强制重路由
    if state.get("fix_required_unmet"):
        return "reroute"
    
    # 超限自愈
    if retry_count >= 3:
        return "self_heal"
    
    # 判断问题严重程度
    issue_type = critic_result.get('issue_type', 'minor')
    final_json = state.get('final_json', {})
    was_fixed = isinstance(final_json, dict) and final_json.get('_was_fixed') is True
    
    # 失败一律先走 Fixer，确保真正修复
    if not was_fixed:
        return "fix"
    
    # 修复后仍为严重问题 → 重新路由
    if issue_type == 'major':
        return "reroute"
    
    # 轻微问题 → 继续修复
    return "fix"
```

> 注：以上函数均为 Critic 实际运行会触发的依赖链，建议结合 `02` 和 `03` 文档联读。

