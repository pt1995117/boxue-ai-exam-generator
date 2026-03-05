# Critic 节点完整源码

来源：`exam_graph.py`

## `critic_node`

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

    # ✅ Bracket format validation for judgment/choice questions
    if question_type in ["单选题", "多选题", "判断题"]:
        fields_to_check = []
        if isinstance(final_json, dict):
            fields_to_check.append(("题干", final_json.get("题干", "")))
            for i in range(1, 9):
                key = f"选项{i}"
                if key in final_json and final_json.get(key):
                    fields_to_check.append((key, final_json.get(key, "")))
        invalid_fields = [name for name, text in fields_to_check if has_invalid_blank_bracket(str(text))]
        if invalid_fields:
            reason = "括号格式错误：必须使用中文括号“（ ）”，括号前后无空格，括号内有空格"
            return {
                "critic_feedback": "FAIL",
                "critic_rules_context": full_rules_text,
                "critic_related_rules": related_rules,
                "critic_result": {
                    "passed": False,
                    "issue_type": "minor",
                    "reason": reason,
                    "fix_strategy": "fix_question"
                },
                "critic_details": f"{reason}（字段：{', '.join(invalid_fields)}）",
                "critic_model_used": "rule-based",
                "llm_trace": llm_records,
                "logs": [f"🔍 批评家: ❌ {reason} → 进入修复"]
            }

    # ✅ Material missing check: multiple missing required materials -> Fail
    has_material_issue, missing_materials = material_missing_check(final_json, kb_context)
    if has_material_issue:
        reason = f"材料缺失项不唯一：缺失 {', '.join(missing_materials)}"
        return {
            "critic_feedback": "FAIL",
            "critic_rules_context": full_rules_text,
            "critic_related_rules": related_rules,
            "critic_result": {
                "passed": False,
                "issue_type": "major",
                "reason": reason,
                "fix_strategy": "fix_question"
            },
            "critic_details": reason,
            "critic_model_used": "rule-based",
            "llm_trace": llm_records,
            "logs": [f"🔍 批评家: ❌ {reason} → 进入修复"]
        }
    
    # ✅ Smart model switching: Check GPT rate limit and switch to Deepseek if needed
    critic_model = CRITIC_MODEL
    critic_model_used = critic_model
    critic_api_key = CRITIC_API_KEY
    critic_base_url = CRITIC_BASE_URL
    critic_provider = CRITIC_PROVIDER
    
    # Check if GPT model is rate-limited
    if critic_model and critic_model.lower().startswith("gpt") and "api.deepseek.com" in (critic_base_url or ""):
        throttle_path = Path(".gpt_rate_limit.txt")
        if throttle_path.exists():
            try:
                last_ts = float(throttle_path.read_text(encoding="utf-8").strip() or "0")
                now = time.time()
                elapsed = now - last_ts
                wait_needed = max(0, 12 - elapsed)
                
                # If need to wait > 5 seconds, switch to Deepseek
                if wait_needed > 5:
                    print(f"⚠️ GPT-5.2 限流中（需等待 {int(wait_needed)}s），切换到 Deepseek Reasoner")
                    critic_model = "deepseek-reasoner"
                    critic_api_key = API_KEY  # Use default OpenAI-compatible key
                    critic_base_url = BASE_URL  # Use default base URL
                    critic_provider = "ait"
            except Exception as e:
                print(f"⚠️ 限流检测失败: {e}，使用默认模型")
    
    if critic_model and "deepseek" in critic_model.lower():
        log_prefix = f"🔍 批评家 (Deepseek):"
    else:
        log_prefix = f"🔍 批评家 ({critic_model}):"
    
    # Create a blind copy of the question (remove answer and explanation)
    blind_question = {k: v for k, v in final_json.items() if k not in ['正确答案', '解析', 'answer', 'explanation']}
    
    # --- Critic Code Generation Step ---
    # 1. Decide if calculation is needed to verify this question, and generate Python code
    prompt_plan = f"""
# 角色
你是批评家 (Critic)。
你需要验证以下题目是否正确。请分析【题目】和【参考材料】，判断是否需要进行数值计算来验证答案。

# 计算验证约束（必须遵守）
1. **时间/日期题必须用 datetime 精确到天**：禁止用年份直接相减。
2. **先锚定政策阈值**：在代码前用常量声明，例如 `REQUIRED_YEARS = 2`。
3. **注释仅说明变量含义**：严禁在注释里辩论或解释“为什么”。
4. **验证逻辑体现在 if/else 或比较表达式**。

# 重要提示：参数提取和计算步骤分析
**计算可能只是解决整个问题的一个步骤，而不是整个问题！**

在验证题目时，请仔细分析：
1. **题目问的是什么？**（最终答案是什么）
2. **需要计算什么？**（能解决哪个步骤）
3. **如何从题目中提取参数？**（题干和选项中可能包含计算所需的数据）

**参数提取规则：**
- 必须从题目中提取**具体的数值**（如：80平方米、1560元、2025年、1993年）
- **不能使用描述性文字**（如："成本价"、"建筑面积"、"建成年代"）
- 如果题目中没有明确数值，需要根据参考材料推断合理的数值
- 注意单位的统一（平方米、元、年等）

**计算步骤分析：**
- 如果题目问的是最终结果，可能需要多步计算
- 计算可能只解决其中一个步骤
- 需要验证：计算结果 + 其他步骤 = 题目答案

例如：
- 题目问"土地出让金是多少"，如果题干给出"建筑面积80平方米，成本价1560元/平方米"
  → 生成代码：`result = 80 * 1560 * 0.01`
  
- 题目问"最长贷款年限是多少"，题干给出"建成年代1993年，当前2025年"
  → 先计算房龄：`house_age = 2025 - 1993`
  → 再根据"房龄+贷款年限≤50年"计算：`max_loan_years = 50 - house_age`
  → 可能还需要考虑借款人年龄等其他因素

# 题目
{json.dumps(blind_question, ensure_ascii=False)}

{CALCULATION_GUIDE}

# 参考材料
{kb_context}

# 任务
如果需要计算，返回 JSON: {{"need_calculation": true, "python_code": "result = ..."}}
如果不需要计算，返回 {{"need_calculation": false, "python_code": null}}
"""
    # Use code generation model (qwen3-coder-plus) for code generation in critic
    # When verifying calculation questions, use specialized code generation model
    use_code_gen_model = agent_name in ['CalculatorAgent', 'FinanceAgent']
    
    if use_code_gen_model:
        # Use code generation model for better code generation
        plan_model = CODE_GEN_MODEL
        plan_api_key = CODE_GEN_API_KEY or critic_api_key
        plan_base_url = CODE_GEN_BASE_URL or critic_base_url
        plan_provider = resolve_code_gen_provider(plan_model, CODE_GEN_PROVIDER, None)
    else:
        # Use regular critic model for non-calculation questions
        plan_model = critic_model
        plan_api_key = critic_api_key
        plan_base_url = critic_base_url
        plan_provider = critic_provider
    
    print(f"🔍 Critic Step 1: 开始调用 LLM 生成验证计划（模型: {plan_model}）")
    plan_content, _, llm_record = call_llm(
        node_name="critic.plan",
        prompt=prompt_plan,
        model_name=plan_model,
        api_key=plan_api_key,
        base_url=plan_base_url,
        provider=plan_provider,
        trace_id=state.get("trace_id"),
        question_id=state.get("question_id"),
    )
    llm_records.append(llm_record)
    print(f"🔍 Critic Step 1: 验证计划生成完成")
    # Normalize potential list responses to string
    if isinstance(plan_content, list):
        plan_content = "\n".join([str(item) for item in plan_content if item is not None])
    elif plan_content is not None and not isinstance(plan_content, str):
        plan_content = str(plan_content)
    
    calc_result = None
    tool_used = "None"
    tool_params = {}
    code_check_passed = True
    code_check_reason = ""
    
    # ✅ 检查空响应
    if not plan_content or not plan_content.strip():
        print(f"DEBUG CRITIC PLAN ERROR: Empty response from LLM")
        # 如果 LLM 返回空响应，优先从 calculator_node 的结果中获取
        if agent_name in ['CalculatorAgent', 'FinanceAgent']:
            execution_result = state.get('execution_result')
            tool_usage = state.get('tool_usage', {})
            
            # 优先使用 execution_result (来自 calculator_node 的执行结果)
            if execution_result is not None:
                calc_result = execution_result
                tool_used = tool_usage.get('method', 'dynamic_code_generation')
                tool_params = tool_usage.get('extracted_params', {})
                print(f"DEBUG CRITIC: 使用 calculator_node 的执行结果: {calc_result}")
            # 其次尝试从 tool_usage 中获取
            elif tool_usage.get('result') is not None:
                calc_result = tool_usage['result']
                tool_used = tool_usage.get('method', 'dynamic_code_generation')
                tool_params = tool_usage.get('extracted_params', {})
                print(f"DEBUG CRITIC: 使用 calculator_node 的 tool_usage 结果: {calc_result}")
            # 最后尝试执行生成的计算代码
            else:
                generated_code = state.get('generated_code')
                if generated_code:
                    try:
                        result_value, stdout_str, stderr_str = execute_python_code(generated_code)
                        if result_value is not None:
                            calc_result = result_value
                            tool_used = "generated_code"
                            print(f"DEBUG CRITIC: 使用 calculator_node 生成的计算代码，结果={calc_result}")
                        elif stderr_str:
                            print(f"DEBUG CRITIC: 执行生成代码失败: {stderr_str}")
                    except Exception as e:
                        print(f"DEBUG CRITIC: 执行生成代码失败: {e}")
    
    try:
        if plan_content and plan_content.strip():
            plan = parse_json_from_response(plan_content)
            
            # Check if calculation is needed
            if plan.get("need_calculation") and plan.get("python_code"):
                generated_code = plan.get("python_code", "").strip()
                if generated_code:
                    code_check_prompt = f"""
# 角色
你是严厉的审计人 (Critic)。请检查【计算代码】是否严格符合【教材规则】与【题干条件】。

# 教材规则
{kb_context}

# 题目
{json.dumps(blind_question, ensure_ascii=False)}

# 计算代码
{generated_code}

# 要求
1. 判断代码是否严格遵守教材公式与判定条件。
2. 若不符合，指出关键错误点（例如漏判定条件、用错计税基础、用错阈值）。

# 输出 JSON
{{
  "code_valid": true/false,
  "code_reason": "不超过80字，说明是否符合规则"
}}
"""
                    try:
                        code_check_text, _, llm_record = call_llm(
                            node_name="critic.code_check",
                            prompt=code_check_prompt,
                            model_name=critic_model,
                            api_key=critic_api_key,
                            base_url=critic_base_url,
                            provider=critic_provider,
                            trace_id=state.get("trace_id"),
                            question_id=state.get("question_id"),
                        )
                        llm_records.append(llm_record)
                        code_check = parse_json_from_response(code_check_text)
                        code_check_passed = bool(code_check.get("code_valid", True))
                        code_check_reason = str(code_check.get("code_reason", "")).strip()
                    except Exception as e:
                        code_check_passed = True
                        code_check_reason = f"代码校验解析失败: {e}"
                
                # If LLM didn't generate code but needs calculation, try to regenerate with code generation model
                if not generated_code:
                    # Re-generate code using code generation model
                    print(f"DEBUG CRITIC: LLM didn't generate code, using code generation model to regenerate...")
                    code_gen_response, _, llm_record = call_llm(
                        node_name="critic.codegen_retry",
                        prompt=prompt_plan + "\n\n请重新分析并生成Python代码。",
                        model_name=CODE_GEN_MODEL,
                        api_key=CODE_GEN_API_KEY,
                        base_url=CODE_GEN_BASE_URL,
                        provider=resolve_code_gen_provider(CODE_GEN_MODEL, CODE_GEN_PROVIDER, None),
                        trace_id=state.get("trace_id"),
                        question_id=state.get("question_id"),
                    )
                    llm_records.append(llm_record)
                    try:
                        code_plan = parse_json_from_response(code_gen_response)
                        generated_code = code_plan.get("python_code", "").strip()
                    except Exception as e:
                        print(f"DEBUG CRITIC: Failed to regenerate code: {e}")
                
                if generated_code and code_check_passed:
                    # Execute the generated Python code
                    result_value, stdout_str, stderr_str = execute_python_code(generated_code)
                elif generated_code and not code_check_passed:
                    result_value, stdout_str, stderr_str = None, "", ""
                    tool_used = "code_validation_failed"
                    tool_params = {"code": generated_code}
                
                if stderr_str:
                    calc_result = f"Execution Error: {stderr_str}"
                    tool_used = "error"
                    print(f"DEBUG CRITIC CODE EXECUTION ERROR: {stderr_str}")
                elif result_value is not None:
                    calc_result = result_value
                    tool_used = "generated_code"
                    tool_params = {"code": generated_code}
                    print(f"DEBUG CRITIC: 成功执行动态生成的代码，结果={calc_result}")
                else:
                    calc_result = stdout_str.strip() if stdout_str.strip() else None
                    tool_used = "generated_code"
                    tool_params = {"code": generated_code}
            else:
                # No calculation needed
                tool_used = "None"
                calc_result = None
    except json.JSONDecodeError as je:
        print(f"DEBUG CRITIC PLAN JSON ERROR: {je}")
        # 如果 JSON 解析失败，尝试从 calculator_node 的结果中获取
        if agent_name in ['CalculatorAgent', 'FinanceAgent']:
            execution_result = state.get('execution_result')
            if execution_result is not None:
                calc_result = execution_result
                tool_used = "from_calculator_node"
                print(f"DEBUG CRITIC: 使用 calculator_node 的执行结果: {calc_result}")
    except Exception as e:
        print(f"DEBUG CRITIC PLAN ERROR: {e}")
        # 如果出现其他错误，尝试从 calculator_node 的结果中获取
        if agent_name in ['CalculatorAgent', 'FinanceAgent']:
            execution_result = state.get('execution_result')
            if execution_result is not None:
                calc_result = execution_result
                tool_used = "from_calculator_node"
                print(f"DEBUG CRITIC: 使用 calculator_node 的执行结果: {calc_result}")

    # --- Verification Step: 信息不对称校验 + 反向解题 ---
    options_text = (
        f"A.{final_json.get('选项1', '')} B.{final_json.get('选项2', '')}"
        if question_type == "判断题"
        else f"A.{final_json.get('选项1', '')} B.{final_json.get('选项2', '')} C.{final_json.get('选项3', '')} D.{final_json.get('选项4', '')}"
    )
    raw_difficulty = final_json.get("难度值", 0.5)
    try:
        difficulty_value = float(raw_difficulty)
    except Exception:
        difficulty_value = 0.5
    if difficulty_value <= 0.5:
        difficulty_level = "低"
    elif difficulty_value >= 0.7:
        difficulty_level = "高"
    else:
        difficulty_level = "中"

    writer_format_issues = state.get("writer_format_issues") or []
    writer_issue_text = ""
    if writer_format_issues:
        writer_issue_text = " / ".join([str(x) for x in writer_format_issues if x])

    critic_format_issues = validate_critic_format(final_json, question_type)
    term_lock_issues = detect_term_lock_violations(term_locks, final_json)
    if term_lock_issues:
        critic_format_issues.extend(term_lock_issues)
    # 年份约束：切片无年份时禁止题干/选项/解析出现公历年份
    kb_text = kb_context if isinstance(kb_context, str) else str(kb_context)
    if not _has_year(kb_text):
        year_violations = [t for t in _collect_text_fields(final_json) if _has_year(t)]
        if year_violations:
            critic_format_issues.append("题干/选项/解析出现公历年份（原文未提及）")
    critic_format_text = " / ".join([str(x) for x in critic_format_issues if x]) if critic_format_issues else ""
    prompt = f"""
你是【严厉的审计人（Critic）】，不是教师、不是解释者、不是建议者。
**重要**：即使发现格式问题，也必须继续完成所有检查并输出完整问题清单，不得只返回格式问题。

你的目标只有一个：
【判断该题是否可以直接进入正式题库】。

⚠️ 审计裁决铁律：
- 只要命中任意“Fail 条件”，必须判定为【审计不通过】。
- 不允许进行“整体权衡”“酌情放行”“大体正确”的判断。
- 即使最终数值正确，只要推导路径、条件或解析存在问题，也必须 Fail。


# 全量教材规则（你拥有的完整信息）
{full_rules_text}

# 计算辅助
计算结果: {calc_result} (仅供参考)

# 待审核题目
题型: {question_type}
难度: {difficulty_value:.2f}（{difficulty_level}）
题干: {final_json['题干']}
选项: {options_text}

# 生成者声称的答案 (Proposed Answer)
{final_json.get('正确答案', '未知')}

# 生成者提供的解析
{final_json.get('解析', '（无解析）')}

{f"# Writer 格式自检结果（仅供参考）\\n{writer_issue_text}\\n" if writer_issue_text else ""}
{f"# Critic 代码格式校验结果（必须纳入汇总）\\n{critic_format_text}\\n" if critic_format_text else ""}

**注意**: 虽然你能看到生成者的答案，但请先**掩盖它**，进行独立推导，最后再比对。

# 核心审计任务 (Audit Tasks) ⚠️

## 0. 适纲性 / 实用性 / 导向性
- **适纲性**: 命题内容必须来自当前知识切片或本教材切片，不得超纲出题。
- **实用性**: 试题必须贴近经纪人作业或规则/法律理解，禁止仅考察数量、年代等死记硬背点。
- **导向性**: 试题应有引导和启发作用，帮助经纪人理解公司文化、熟悉新业务、热爱行业。
- **Fail条件**:
  - 题目超出当前知识切片或教材范围（超纲）。
  - 题目仅考察数量/年代等纯记忆点，缺少实务价值。

## 1. 地理与范围审计 (Geo-Consistency)
- **规则**: 如果教材明确限定了城市（如"北京市"），题干必须严格遵守。
- **Fail条件**: 
  - 教材=北京，题干=上海/深圳/其他具体城市。
  - 教材=北京，题干=无（若规则具特殊性）。
- **新增约束**:
  - 教材未提及具体城市/时间，题干或解析却出现具体城市名或具体年份/日期。
- **特例**: 干扰项中允许出现其他城市作为错误选项，但题干场景和正确答案必须基于教材指定城市。

## 2. 逻辑自洽性审计 (Logic Validity)
- **规则**: 不要机械比对数字，要比对**判定结果**。
- **Fail条件**: 
  - 题目场景中条件（如"不满2年"）推导出的结论与正确答案冲突。
  - **严重错误案例**: 题目说"北京换房退税"，但并未满足"先卖后买"或"1年内"的核心条件，正确答案通过。

## 3. 反向解题（Reverse Solving，最高裁决权）

⚠️ 本维度拥有最高裁决优先级，高于所有其他审计维度。

任务：
- 在【完全忽略生成者声称的答案】的前提下，
- 仅基于题干条件 + 教材规则，
- 推导是否能得到【唯一且确定的答案】。

Fail 条件（任一即 Fail）：
- 无法计算（缺关键数值 / 条件）
- 存在两条及以上合理推导路径
- 不同规则分支在题干中未被明确排除
- 需要考生“猜规则”“默认前提”才能算出答案

⚠️ 一旦 Reverse Solving 判定失败：
- reverse_solve_success = false
- can_deduce_unique_answer = false
- fix_strategy 至少为 fix_question


## 4. 质量把关 (Quality Control) - 新增核心拦截项 ⚠️
- **Fail条件 1 (题干直接给出答案)**:
  - 题干中直接包含了正确答案的关键词，导致考生无需理解专业知识，仅通过文字匹配即可选出答案。
  - *典型案例*：题干说"经纪人向她介绍了商业贷款"，然后问"商业贷款最核心的特征或定义是什么"，此时正确答案选项A如果直接复述教材定义，考生无需理解即可通过题干中的"商业贷款"关键词匹配到答案。
  - *整改建议*：要求改为"给定场景->判断结论"的模式，题干不应直接给出答案关键词。
  - **重要说明**：允许正确答案选项与教材原文定义一致，这是正常的考察方式。检测的重点是题干是否直接给出了答案关键词，而不是答案选项是否与教材原文一致。
- **Fail条件 2 (基础质量)**:
  - 题目表述使用了模糊词汇（如"实实在在"）。
  - 选项跨维度（如A法律 B实物 C位置 D价格）。
  - 干扰项过于幼稚，无需专业知识即可排除（仅对中/高难度强制）。
  - **判断题特例**：判断题只允许两个选项（正确/错误），C/D为空不构成质量问题。
- **Fail条件 3 (AI幻觉/非人话)**:
  - 出现了不符合中国房地产业务习惯的生造词。
  - *典型案例*：使用“外接”代替“买方/受让方”；使用“上交”代替“缴纳”。

## 5. 题干/设问规范审计
- **规则**:
  - 题干括号不得在句首，可在句中或句末；选择题句末必须有句号且句号在最后；判断题句子完结后加括号且在最后。
  - 括号必须为中文 `（ ）`，括号内有空格，括号前后无空格。
  - 设问使用陈述方式，避免否定句与双重否定；判断题写法需为【XX做法正确/错误】。
  - 单选题设问用“以下表述正确的是（ ）。”；多选题用“以下表述正确的有（ ）/包括（ ）。”。
- **Fail条件**:
  - 括号格式不符合 `（ ）` 或位置/句号不符合要求。
  - 设问为疑问句/双重否定，或判断题格式不符合【XX做法正确/错误】。

## 6. 选项规范审计
- **规则**:
  - 选项末尾不加标点；选项与题干合成语义完整。
  - 选项姓名与题干姓名一致，不能出现题目未涉及的姓名。
  - 数值型选项需按从小到大排序，未被选中的数值也必须有计算依据。
- **Fail条件**:
  - 选项末尾有标点、姓名不一致、选项无法与题干组成完整语义。
  - 数值选项无依据或乱序。

## 7. 解析规范审计
- **规则**:
  - 解析需采用三段式：教材原文 + 试题分析 + 结论。
  - 判断题结论写【本题答案为正确/错误】，不得写成【本题答案为A/B】。
  - 选择题结论写【本题答案为A/B/C/D/AB/AC...】。
  - 禁止直接粘贴教材原文表格或图片（可改成文字描述）。
  - 解析必须与答案一致，计算题必须与计算过程一致。
- **Fail条件**:
  - 解析缺少三段式或结论格式不规范。
  - 解析与答案/计算过程不一致。
  - 直接复制表格/图片。

## 8. 典型错题审计
- **Fail条件**:
  - 多字、少字、错字影响作答或造成歧义。
  - 题干与选项/解析前后不一致。
  - 计算题无正确答案或答案与计算过程不一致。
  - 题目超纲或概念过时（如旧业务名/过期协议）。
  - 场景严重脱离经纪业务实际。
  - 干扰选项存在争议或与正确答案同样成立。
  - *整改建议*：修正为标准业务术语。

## 9. 选项逻辑（干扰项审计）
- **Fail条件**:
  - 干扰项是纯粹的随机数字，没有考察到易错点（仅对高难度强制）。
  - *优质干扰项标准*：应该是“遗漏了某一步计算”或“误用了另一个税率”得出的错误结果。
  - **难度差异化要求**：
    - 低难度（<=0.5）：干扰项质量只记录建议，不作为 Fail 条件。
    - 中等（0.5-0.7）：可提示，但不要仅因干扰项质量判 Fail。
    - 高难度（>=0.7）：必须满足高质量干扰项要求，不达标即 Fail。

请基于以上标准，输出审核结果。

# 输出格式 (必须为 JSON 块)
⚠️ JSON 输出强一致性规则（必须遵守）：

- 若 can_deduce_unique_answer = false：
  → reverse_solve_success 必须为 false
  → fix_strategy 不得为 fix_explanation

- 若 explanation_valid = false：
  → grounding_check_passed 必须为 false

- 若 quality_check_passed = false：
  → quality_issues 不得为空数组

- 不允许出现：
  “问题存在但仍判通过”的组合

```json
{{
    "reverse_solve_success": true/false,
    "critic_answer": "A/B/C/D",
    "can_deduce_unique_answer": true/false,
    "missing_conditions": ["遗漏的条件1", "遗漏的条件2"] 或 [],
    "deduction_process": "你的推导过程：1. 提取条件... 2. 匹配规则... 3. 计算结果...",
    "explanation_valid": true/false,
    "grounding_check_passed": true/false,
    "example_conflict": true/false,
    "quality_check_passed": true/false,
    "quality_issues": ["问题1", "问题2"] 或 [],
    "context_strength": "强/中/弱",
    "option_dimension_consistency": true/false,
    "fix_strategy": "fix_explanation / fix_question / fix_both / regenerate",
    "fix_reason": "用一句话给出修复建议（必要时给出要补充的具体条件/选项）",
    "reason": "详细说明审核结论"
}}
```
"""

    print(f"🔍 Critic Step 2: 开始调用 LLM 执行质量验证（模型: {critic_model}）")
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
    print(f"🔍 Critic Step 2: 质量验证完成，开始解析结果")
    
    # Initialize variables with defaults
    critic_answer = "UNKNOWN"
    explanation_valid = False
    reverse_solve_success = False
    can_deduce_unique_answer = False
    deduction_process = ""
    grounding_check_passed = True  # Default to True for backward compatibility
    missing_conditions = []
    example_conflict = False
    quality_check_passed = True  # Default to True for backward compatibility
    quality_issues = []
    context_strength = "中"  # Default to medium
    option_dimension_consistency = True  # Default to True
    reason = "Parsing Failed"
    fix_strategy = "fix_both"
    fix_reason = ""
    
    try:
        review_result = parse_json_from_response(response_text)
        
        # 反向解题结果（核心校验）
        reverse_solve_success = review_result.get("reverse_solve_success", False)
        can_deduce_unique_answer = review_result.get("can_deduce_unique_answer", True)  # Default to True for backward compatibility
        deduction_process = review_result.get("deduction_process", "")
        
        # 答案一致性
        critic_answer = review_result.get("critic_answer", "UNKNOWN").strip().upper()
        
        # 信息不对称校验
        grounding_check_passed = review_result.get("grounding_check_passed", True)
        missing_conditions = review_result.get("missing_conditions", [])
        example_conflict = review_result.get("example_conflict", False)
        has_example_refs = bool(state.get("examples")) or bool(kb_chunk.get("结构化内容", {}).get("examples"))
        if not has_example_refs:
            example_conflict = False
        
        # 题目质量检查
        quality_check_passed = review_result.get("quality_check_passed", True)  # Default to True for backward compatibility
        quality_issues = review_result.get("quality_issues", [])
        context_strength = review_result.get("context_strength", "中")
        option_dimension_consistency = review_result.get("option_dimension_consistency", True)
        
        # ✅ 如果语境强度为"弱"或选项维度不一致，强制判定为质量不合格
        if context_strength == "弱" or not option_dimension_consistency:
            quality_check_passed = False
            if context_strength == "弱":
                quality_issues.append("题干语境模糊，表述不够明确")
            if not option_dimension_consistency:
                quality_issues.append("选项跨多个维度，干扰项设计不合理")
        # ✅ 将代码格式校验结果纳入质量问题（确保汇总输出）
        if critic_format_issues:
            quality_check_passed = False
            for item in critic_format_issues:
                if item and item not in quality_issues:
                    quality_issues.append(item)
        
        # 低难度题：干扰项优秀程度不作为强制 Fail 条件
        if difficulty_value <= 0.5:
            filtered_issues = [i for i in quality_issues if "干扰项" not in str(i)]
            if len(filtered_issues) != len(quality_issues):
                quality_issues = filtered_issues
                if not quality_issues:
                    quality_check_passed = True
        
        # 解析审查
        explanation_valid = review_result.get("explanation_valid", False)
        
        reason = review_result.get("reason", "")
        fix_strategy = review_result.get("fix_strategy", "fix_both")
        fix_reason = review_result.get("fix_reason", "")
        
        # 如果 can_deduce_unique_answer 为 False，强制 reverse_solve_success = False
        if not can_deduce_unique_answer:
            reverse_solve_success = False
    except Exception as e:
        print(f"DEBUG CRITIC PARSE ERROR: {e}")
        # Fallback: try to find answer in text if JSON fails
        import re
        match = re.search(r'[ABCD]', response_text)
        if match:
            critic_answer = match.group(0)
        
        # Set defaults for missing fields
        reverse_solve_success = False
        can_deduce_unique_answer = False
        deduction_process = ""
        grounding_check_passed = False
        missing_conditions = []
        example_conflict = False
        quality_check_passed = False
        quality_issues = ["JSON解析失败"]
        context_strength = "弱"
        option_dimension_consistency = False
        explanation_valid = False
        reason = f"JSON解析失败: {str(e)}"
        fix_strategy = "regenerate"
        fix_reason = "审计输出解析失败"
    
    gen_answer = final_json['正确答案'].strip().upper()
    question_text = final_json.get("题干", "")
    
    critic_tool_usage = {
        "tool": tool_used,
        "params": tool_params,
        "result": calc_result
    }

    # Near-duplicate guard: avoid generating questions that already exist in history
    if retriever:
        is_dup, dup_score, dup_text = retriever.is_similar_to_history(question_text, threshold=0.9)
        if is_dup:
            return {
                "critic_feedback": "FAIL",
                "critic_details": f"疑似重复题干，相似度 {dup_score:.2f}，已存在题目: {dup_text[:120]}",
                "critic_tool_usage": critic_tool_usage,
                "critic_result": {
                    "passed": False,
                    "issue_type": "major",
                    "reason": "高相似度重复题目"
                },
                "critic_model_used": critic_model,
                "final_json": None,
                "llm_trace": llm_records,
                "logs": [f"🛑 {log_prefix} 发现高相似度题目（{dup_score:.2f}），已丢弃以避免重复出题。"]
            }

    # Pass Condition: 核心是"反向解题成功"（能推导出唯一答案）
    # 1. 反向解题校验（核心）
    fail_reason = ""
    reverse_fail = False
    answer_mismatch = False
    grounding_fail = False
    difficulty_out_of_range = False
    explanation_fail = False
    issue_type = "minor"  # 默认轻微问题
    term_lock_fail = bool(term_lock_issues)
    
    if not reverse_solve_success or not can_deduce_unique_answer:
        reverse_fail = True
        fail_reason += f"反向解题失败：无法根据题目条件推导出唯一答案; "
        if missing_conditions:
            fail_reason += f"遗漏条件: {', '.join(missing_conditions)}; "
        if deduction_process:
            fail_reason += f"推导过程: {deduction_process[:100]}...; "
        issue_type = "major"  # 无法推导唯一答案是严重问题
    
    # 2. 答案一致性验证
    if critic_answer != gen_answer and critic_answer != "UNKNOWN":
        answer_mismatch = True
        fail_reason += f"答案不一致 (审计人推导: {critic_answer} vs 生成者: {gen_answer}); "
        issue_type = "major"  # 答案错误是严重问题
    
    # 3. 信息不对称校验
    if not grounding_check_passed:
        grounding_fail = True
        if missing_conditions:
            fail_reason += f"遗漏判定条件: {', '.join(missing_conditions)}; "
        if example_conflict:
            fail_reason += f"误带入母题中的陈旧逻辑或错误数据; "
        issue_type = "major"
    
    # 4. 难度范围验证
    if difficulty_range:
        min_diff, max_diff = difficulty_range
        current_difficulty = final_json.get('难度值', 0.5)
        try:
            current_difficulty = float(current_difficulty)
        except:
            current_difficulty = 0.5
        
        if current_difficulty < min_diff or current_difficulty > max_diff:
            difficulty_out_of_range = True
            fail_reason += f"难度值 {current_difficulty:.2f} 不在指定范围内 ({min_diff:.1f}-{max_diff:.1f}); "
            issue_type = "major"  # 难度不符合要求是严重问题
    
    # 5. 题目质量检查
    if not quality_check_passed:
        fail_reason += f"题目质量不合格; "
        if quality_issues:
            fail_reason += f"质量问题: {', '.join(quality_issues)}; "
        
        # ✅ 语境模糊或维度不一致是严重问题，必须重新生成
        if context_strength == "弱" or not option_dimension_consistency:
            issue_type = "major"
            if context_strength == "弱":
                fail_reason += f"【严重】题干语境模糊，表述不够明确，可能导致歧义; "
            if not option_dimension_consistency:
                fail_reason += f"【严重】选项跨多个维度，干扰项设计不合理，无法真正考察专业知识; "
        else:
            # 仅格式类问题→轻微；其他质量问题默认轻微（可修复）
            if issue_type != "major":
                if critic_format_issues and all(i in critic_format_issues for i in quality_issues):
                    issue_type = "minor"
                else:
                    issue_type = "minor"
    if term_lock_fail:
        fail_reason += f"专有名词锁词违规: {'; '.join(term_lock_issues)}; "
        issue_type = "major"
    
    # 5. 计算代码校验
    if not code_check_passed:
        fail_reason += f"计算代码不符合教材规则 ({code_check_reason}); "
        issue_type = "major"

    # 5. 解析审查
    if not explanation_valid:
        explanation_fail = True
        fail_reason += f"解析不合格 ({reason}); "
        # 解析问题通常可以修复，保持 minor

    # Decide fix strategy when failing (按优先级决策)
    # 优先级1：反向解题失败 → 必须修复题目（题干条件不足或有歧义）
    if not reverse_solve_success or not can_deduce_unique_answer:
        if critic_answer != gen_answer and critic_answer != "UNKNOWN":
            fix_strategy = "fix_both"
            if not fix_reason:
                fix_reason = "反向解题失败且答案不一致，需同时调整题干/选项/答案与解析"
        else:
            fix_strategy = "fix_question"
            if not fix_reason:
                fix_reason = "反向解题失败（无法推导唯一答案），需补充题干条件或调整选项"
    # 优先级2：答案不一致（但反向解题成功）
    elif critic_answer != gen_answer and critic_answer != "UNKNOWN":
        fix_strategy = "fix_both"
        if not fix_reason:
            fix_reason = "答案与解析不一致或答案错误，需同时调整题干/选项/答案与解析"
    # 优先级3：仅解析无效（题目本身可解）
    elif not explanation_valid:
        fix_strategy = "fix_explanation"
        if not fix_reason:
            fix_reason = "解析错误或未能支撑答案，仅修正解析"
    # 优先级4：质量或依据问题
    elif not quality_check_passed or not grounding_check_passed:
        fix_strategy = "regenerate"
        if not fix_reason:
            fix_reason = "题目质量或依据不足，建议重写题目"
    
    # 通过条件：反向解题成功 + 答案一致 + 解析合理 + 信息完整 + 题目质量合格
    if (reverse_solve_success and can_deduce_unique_answer and 
        critic_answer == gen_answer and 
        explanation_valid and 
        grounding_check_passed and
        quality_check_passed):
        critic_payload = {
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
        print(f"DEBUG CRITIC RESULT: {critic_payload['critic_result']}")
        return critic_payload
    else:
        required_fixes = []
        all_issues = []
        if critic_format_issues:
            for item in critic_format_issues:
                required_fixes.append(f"format:{item}")
                all_issues.append(f"format:{item}")
        if not reverse_solve_success or not can_deduce_unique_answer:
            required_fixes.append("logic:cannot_deduce_unique_answer")
            all_issues.append("logic:cannot_deduce_unique_answer")
        if critic_answer != gen_answer and critic_answer != "UNKNOWN":
            required_fixes.append("logic:answer_mismatch")
            all_issues.append("logic:answer_mismatch")
        if missing_conditions:
            for mc in missing_conditions:
                all_issues.append(f"logic:missing_condition:{mc}")
            required_fixes.append("logic:missing_conditions")
        if example_conflict:
            required_fixes.append("logic:example_conflict")
            all_issues.append("logic:example_conflict")
        if option_dimension_consistency is False:
            required_fixes.append("logic:option_dimension")
            all_issues.append("logic:option_dimension")
        if not grounding_check_passed:
            required_fixes.append("logic:grounding")
            all_issues.append("logic:grounding")
        if not quality_check_passed:
            required_fixes.append("quality:issues")
            if quality_issues:
                for qi in quality_issues:
                    all_issues.append(f"quality:{qi}")
            else:
                all_issues.append("quality:issues")
        if difficulty_out_of_range:
            all_issues.append("difficulty:out_of_range")
        if explanation_fail:
            all_issues.append("explanation:invalid")
        if not code_check_passed:
            all_issues.append(f"calc:code_check:{code_check_reason}")

        critic_payload = {
            "critic_feedback": fail_reason if fail_reason else "反向解题失败",
            "critic_details": f"❌ 审计不通过（触发Fail条件）: {fail_reason if fail_reason else '无法根据题目条件推导出唯一答案'}",
            "critic_tool_usage": critic_tool_usage,
            "critic_rules_context": full_rules_text,
            "critic_related_rules": related_rules,
            "critic_result": {
                "passed": False,
                "issue_type": issue_type,  # minor: 可修复 / major: 需重新路由
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
        print(f"DEBUG CRITIC RESULT: {critic_payload['critic_result']}")
        return critic_payload
```

