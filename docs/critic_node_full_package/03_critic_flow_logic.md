# Critic 节点流程（基于代码）

## 主流程
1. 入口检查（调试强制失败、`final_json` 缺失）。
2. 构建扩展规则上下文（当前切片 + 父级切片 + 相似切片）。
3. 规则短路校验（题型一致性、实战模式业务语义、括号格式、材料缺失项不唯一）。
4. 计算验证计划（`critic.plan`）：判断是否需要计算并生成代码。
5. 计算代码校验（`critic.code_check`）与执行（沙箱执行）。
6. 主审计（`critic.review`）：反向解题、答案一致性、解析质量、适纲性、干扰项质量等。
7. 聚合失败原因并计算严重程度（`issue_type`）。
8. 生成修复策略（`fix_strategy`：`fix_question`/`fix_explanation`/`fix_both`/`regenerate`）。
9. 输出 `PASS` 或 `FAIL` 载荷（含 `critic_result`、`critic_required_fixes`、`critic_rules_context`、`critic_related_rules`）。

## 路由流程（`critical_decision`）
- 通过：`pass -> END`
- 未通过且未修复：`fix -> fixer`
- 修复后仍严重：`reroute -> router`
- 重试超限：`self_heal -> END`

## 输出关键字段
- `critic_feedback`
- `critic_details`
- `critic_result`
- `critic_tool_usage`
- `critic_required_fixes`
- `critic_rules_context`
- `critic_related_rules`
- `critic_format_issues`
- `retry_count`
- `llm_trace`
