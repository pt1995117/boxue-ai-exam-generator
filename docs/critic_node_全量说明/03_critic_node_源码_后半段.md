# Critic 节点源码（后半段）

来源：`exam_graph.py` 第 `2984-3548` 行（原函数后半段）

```python
# --- Verification Step: 信息不对称校验 + 反向解题 ---
options_text = (
    f"A.{final_json.get('选项1', '')} B.{final_json.get('选项2', '')}"
    if question_type == "判断题"
    else f"A.{final_json.get('选项1', '')} B.{final_json.get('选项2', '')} C.{final_json.get('选项3', '')} D.{final_json.get('选项4', '')}"
)

# ...（主审计 prompt 构造：包含全量规则、题干、选项、答案、解析、审计任务与JSON输出约束）

response_text, used_model, llm_record = call_llm(
    node_name="critic.review",
    prompt=prompt,
    model_name=critic_model,
    api_key=critic_api_key,
    base_url=critic_base_url,
    provider=critic_provider,
    trace_id=state.get("trace_id"),
    question_id=state.get("question_id"),
)
llm_records.append(llm_record)
critic_model_used = used_model or critic_model

# 解析 LLM 输出 JSON
review_result = parse_json_from_response(response_text)
reverse_solve_success = review_result.get("reverse_solve_success", False)
can_deduce_unique_answer = review_result.get("can_deduce_unique_answer", True)
deduction_process = review_result.get("deduction_process", "")
critic_answer = review_result.get("critic_answer", "UNKNOWN").strip().upper()
grounding_check_passed = review_result.get("grounding_check_passed", True)
missing_conditions = review_result.get("missing_conditions", [])
example_conflict = review_result.get("example_conflict", False)
quality_check_passed = review_result.get("quality_check_passed", True)
quality_issues = review_result.get("quality_issues", [])
context_strength = review_result.get("context_strength", "中")
option_dimension_consistency = review_result.get("option_dimension_consistency", True)
explanation_valid = review_result.get("explanation_valid", False)
reason = review_result.get("reason", "")
fix_strategy = review_result.get("fix_strategy", "fix_both")
fix_reason = review_result.get("fix_reason", "")

# ...（本地纠偏：无母题时去掉 example_conflict、弱语境/跨维强制质量不合格、低难度干扰项放宽等）

gen_answer = final_json['正确答案'].strip().upper()
critic_tool_usage = {
    "tool": tool_used,
    "params": tool_params,
    "result": calc_result
}

# 近重复题拦截
is_dup, dup_score, dup_text = retriever.is_similar_to_history(question_text, threshold=0.9)

# fail_reason 聚合（反向解题、答案一致性、grounding、难度、质量、锁词、代码校验、解析）
# issue_type 计算（minor / major）

# 修复策略优先级
if not reverse_solve_success or not can_deduce_unique_answer:
    if critic_answer != gen_answer and critic_answer != "UNKNOWN":
        fix_strategy = "fix_both"
    else:
        fix_strategy = "fix_question"
elif critic_answer != gen_answer and critic_answer != "UNKNOWN":
    fix_strategy = "fix_both"
elif not explanation_valid:
    fix_strategy = "fix_explanation"
elif not quality_check_passed or not grounding_check_passed:
    fix_strategy = "regenerate"

# PASS 条件
if (reverse_solve_success and can_deduce_unique_answer and 
    critic_answer == gen_answer and 
    explanation_valid and 
    grounding_check_passed and
    quality_check_passed):
    return {
        "critic_feedback": "PASS", 
        "critic_details": f"✅ 审核通过 (反向解题成功，能推导出唯一答案: {critic_answer})",
        "critic_tool_usage": critic_tool_usage,
        "critic_result": {
            "passed": True,
            "deduction_process": deduction_process
        },
        "critic_format_issues": critic_format_issues,
        "critic_model_used": critic_model_used,
        "llm_trace": llm_records,
        "logs": [f"{log_prefix} 审核通过（反向解题成功，能推导出唯一答案）"]
    }
else:
    return {
        "critic_feedback": fail_reason if fail_reason else "反向解题失败",
        "critic_details": f"❌ 审计不通过（触发Fail条件）: {fail_reason if fail_reason else '无法根据题目条件推导出唯一答案'}",
        "critic_tool_usage": critic_tool_usage,
        "critic_rules_context": full_rules_text,
        "critic_related_rules": related_rules,
        "critic_result": {
            "passed": False,
            "issue_type": issue_type,
            "reason": fail_reason if fail_reason else reason,
            "fix_strategy": fix_strategy,
            "fix_reason": fix_reason,
            "missing_conditions": missing_conditions,
            "example_conflict": example_conflict,
            "quality_check_passed": quality_check_passed,
            "quality_issues": quality_issues,
            "term_lock_issues": term_lock_issues,
            "context_strength": context_strength,
            "option_dimension_consistency": option_dimension_consistency,
            "deduction_process": deduction_process,
            "can_deduce_unique_answer": can_deduce_unique_answer,
            "all_issues": all_issues
        },
        "critic_required_fixes": required_fixes,
        "critic_format_issues": critic_format_issues,
        "critic_model_used": critic_model_used,
        "llm_trace": llm_records,
        "retry_count": state['retry_count'] + 1, 
        "logs": [f"{log_prefix} 审计不通过 (第 {state['retry_count']+1} 次). 严重程度: {issue_type}. 原因: {fail_reason if fail_reason else '反向解题失败'}"]
    }
```

> 说明：后半段包含主审计、结果解析、失败归因、修复策略、PASS/FAIL 输出。  
> 完整逐行版本请对照 `exam_graph.py` 2984-3548。

