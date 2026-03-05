# Critic 节点源码（前半段）

来源：`exam_graph.py` 第 `2549-2983` 行（原函数前半段）

```python
def critic_node(state: AgentState, config):
    llm_records: List[Dict[str, Any]] = []
    # Debug/testing hook: force one "minor" failure to demonstrate the fixer loop.
    if state.get("debug_force_fail_once") and state.get("retry_count", 0) == 0:
        return {
            "critic_feedback": "FORCED_FAIL",
            "critic_details": "Forced minor failure for loop demo (will go to Fixer).",
            "critic_result": {"passed": False, "issue_type": "minor", "reason": "forced"},
            "retry_count": 1,
            "llm_trace": llm_records,
            "logs": ["🧪 批评家: 已强制驳回一次，用于演示 Fixer 闭环"]
        }
    final_json = state.get('final_json')
    if not final_json:
        return {
            "critic_feedback": "FAIL", 
            "critic_details": "No question generated to verify.",
            "llm_trace": llm_records,
            "logs": ["🕵️ 批评家: 无法审核，未生成题目。"]
        }
    print(f"DEBUG CRITIC INPUT FINAL_JSON: {final_json}")

    kb_chunk = state['kb_chunk']
    term_locks = state.get("term_locks") or []
    retriever = config['configurable'].get('retriever') or get_default_retriever()
    examples = state.get('examples', [])
    kb_context, parent_slices, related_slices = build_extended_kb_context(kb_chunk, retriever, examples)
    
    # Get difficulty range from config
    difficulty_range = config['configurable'].get('difficulty_range')
    
    # ✅ 信息不对称校验：Critic 拥有全量教材逻辑
    # 获取相关的全量规则（不仅仅是当前知识点）
    full_rules_context = kb_context  # 当前 + 上一级 + 相似切片
    
    # 相关规则集合（上一级切片全集 + 相似切片）
    related_rules = []
    for chunk in (parent_slices + related_slices):
        chunk_path = chunk.get('完整路径', '')
        if chunk_path and chunk_path != kb_chunk.get('完整路径', ''):
            related_rules.append({
                "路径": chunk_path,
                "内容": format_kb_chunk_full(chunk)
            })
    
    # 构建全量规则上下文
    full_rules_text = f"# 当前知识点规则\n{kb_context}\n"
    if related_rules:
        full_rules_text += "\n# 相关知识点规则（用于完整判定）\n"
        for rule in related_rules[:5]:  # 最多5个相关规则
            full_rules_text += f"【{rule['路径']}】\n{rule['内容']}\n\n"
    
    # 批评家固定使用审计模型（GPT-5.2）
    agent_name = state.get('agent_name', '')
    # ✅ Prioritize reading question type from state (set by Writer), fallback to config
    question_type = state.get('current_question_type') or config['configurable'].get('question_type', '单选题')
    cfg_question_type = config['configurable'].get('question_type', '单选题')
    
    # ✅ Question type consistency validation (only for specific type mode)
    # If config is "随机", skip type validation
    # If config is specific type (单选/多选/判断), validate consistency with state
    print(f"🔍 Critic 开始执行 - cfg题型:[{cfg_question_type}], state题型:[{question_type}]")
    if cfg_question_type != "随机" and cfg_question_type in ["单选题", "多选题", "判断题"]:
        if question_type != cfg_question_type:
            print(f"❌ 题型不一致: 要求[{cfg_question_type}]，实际[{question_type}]")
            return {
                "critic_feedback": "FAIL",
                "critic_rules_context": full_rules_text,
                "critic_related_rules": related_rules,
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "reason": f"题型不一致：要求生成{cfg_question_type}，但实际生成了{question_type}",
                    "fix_strategy": "regenerate"
                },
                "critic_details": f"题型校验失败：要求{cfg_question_type}，实际{question_type}",
                "critic_model_used": "rule-based",
                "llm_trace": llm_records,
                "logs": [f"🔍 批评家: ❌ 题型不一致（要求{cfg_question_type}，实际{question_type}）→ 重新生成"]
            }
    else:
        print(f"✅ 跳过题型校验（随机模式或已匹配）")

    configured_mode = config['configurable'].get('generation_mode', '随机')
    effective_generation_mode = state.get("current_generation_mode") or resolve_effective_generation_mode(configured_mode, state)[0]

    # ✅ 模式强约束：实战应用/推演必须体现业务场景
    if effective_generation_mode == "实战应用/推演":
        stem_text = str(final_json.get("题干", "")) if isinstance(final_json, dict) else ""
        if not has_business_context(stem_text):
            reason = "筛选条件不符合：当前为【实战应用/推演】，题干未体现业务场景语义"
            return {
                "critic_feedback": "FAIL",
                "critic_rules_context": full_rules_text,
                "critic_related_rules": related_rules,
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "reason": reason,
                    "fix_strategy": "regenerate"
                },
                "critic_details": reason,
                "critic_model_used": "rule-based",
                "llm_trace": llm_records,
                "logs": [f"🔍 批评家: ❌ {reason} → 重新生成"]
            }

    # ...（中间包含括号格式校验、材料缺失校验、模型切换、盲题构建、计算计划生成与执行回退）
    # 该段完整原文请直接对照 exam_graph.py 2549-2983
```

> 说明：前半段包含了 Critic 的全部“前置硬校验 + 计算计划阶段”逻辑。  
> 为保持文档可读性，中间长 prompt 与重复容错分支在此处省略，未改动原代码。

