# Critic 依赖函数源码

来源：`exam_graph.py`

## `validate_critic_format`

```python
def validate_critic_format(final_json: Dict[str, Any], question_type: str) -> List[str]:
    issues = []
    if not isinstance(final_json, dict):
        return ["题目结构非字典"]
    q = str(final_json.get("题干", "") or "")
    options = []
    for i in range(1, 9):
        key = f"选项{i}"
        val = final_json.get(key)
        if val is not None and str(val) != "":
            options.append(str(val))
    answer = final_json.get("正确答案", "")
    # Reuse writer format checks where applicable
    if has_invalid_blank_bracket(q):
        issues.append("题干括号格式不规范")
    if question_type in ["单选题", "多选题", "判断题"]:
        if "（ ）" not in q:
            issues.append("题干缺少标准占位括号")
    if question_type in ["单选题", "多选题"]:
        if not q.endswith("。"):
            issues.append("选择题题干未以句号结尾")
        if "（ ）" in q and not q.endswith("）。"):
            issues.append("选择题括号与句号位置不规范")
    if question_type == "判断题" and not q.endswith("（ ）"):
        issues.append("判断题题干未以括号结尾")
    for opt in options:
        if has_invalid_blank_bracket(opt):
            issues.append("选项括号格式不规范")
            break
    for opt in options:
        if re.search(r"[。！？；;：:，,、]\s*$", opt):
            issues.append("选项末尾含标点")
            break
    if question_type == "判断题":
        if not (isinstance(answer, str) and re.fullmatch(r"[ABab]", str(answer).strip())):
            issues.append("判断题答案格式应为A/B")
    elif question_type == "单选题":
        if not (isinstance(answer, str) and re.fullmatch(r"[A-Ha-h]", str(answer).strip())):
            issues.append("单选题答案格式应为单个字母")
    elif question_type == "多选题":
        if not (isinstance(answer, str) and re.fullmatch(r"[A-Ha-h]{2,}", str(answer).strip())):
            issues.append("多选题答案格式应为多个字母")
    # Name usage checks (no 姓+女士/先生 or 小+姓氏)
    q_text = str(final_json.get("题干", "") or "")
    exp_text = str(final_json.get("解析", "") or "")
    name_issues = validate_name_usage(q_text, options, exp_text)
    if name_issues:
        issues.append("人名不规范（禁止称谓/小名）")
    return issues
```

## `material_missing_check`

```python
def material_missing_check(final_json: Dict[str, Any], kb_context: str) -> Tuple[bool, List[str]]:
    if not isinstance(final_json, dict):
        return False, []
    q = str(final_json.get("题干", "") or "")
    # Only apply to "supplement materials" questions
    if not re.search(r"(补充|还需|需要|应当|应需).*(材料|证|证明|证件)", q):
        return False, []
    kb_text = _extract_text_from_kb_context(kb_context)
    material_terms = [
        "身份证", "户口本", "结婚证", "婚姻关系证明", "出生医学证明", "独生子女证",
        "子女关系证明", "不动产权证书", "权属证明", "购房合同", "委托书", "完税证明"
    ]
    required = {m for m in material_terms if m in kb_text}
    if not required:
        return False, []
    provided = set()
    for m in required:
        if re.search(rf"(已提供|已提交|已出示|已准备|已递交|已交).{{0,6}}{re.escape(m)}", q):
            provided.add(m)
    missing = sorted(list(required - provided))
    # If more than one missing item, question is ambiguous for single-answer
    if len(missing) > 1:
        return True, missing
    return False, missing
```

## `_has_year`

```python
def _has_year(text: str) -> bool:
    return bool(re.search(r'(19|20)\d{2}年', text or ""))
```

## `_collect_text_fields`

```python
def _collect_text_fields(final_json: Dict[str, Any]) -> List[str]:
    fields = []
    if not isinstance(final_json, dict):
        return fields
    fields.append(str(final_json.get("题干", "")))
    fields.append(str(final_json.get("解析", "")))
    for i in range(1, 9):
        key = f"选项{i}"
        if key in final_json:
            fields.append(str(final_json.get(key, "")))
    return fields
```

## `build_extended_kb_context`

```python
def build_extended_kb_context(kb_chunk: Dict[str, Any], retriever: Optional[KnowledgeRetriever], examples: List[Dict]) -> Tuple[str, List[Dict], List[Dict]]:
    current_path = kb_chunk.get("完整路径", "")
    parent_slices = []
    related_slices = []
    if retriever:
        parent_slices = retriever.get_parent_slices(kb_chunk)
        # Related slices by current slice content
        current_query = f"{kb_chunk.get('完整路径','')} {kb_chunk.get('核心内容','')}".strip()
        related_slices.extend(
            retriever.get_related_kb_chunks(current_query, k=5, exclude_paths=[current_path])
        )
        # Related slices by examples (题干+解析)
        if examples:
            for ex in examples[:5]:
                if isinstance(ex, dict):
                    q = ex.get("题干", "") or ex.get("question", "")
                    exp = ex.get("解析", "") or ex.get("explanation", "")
                    query_text = f"{q}\n{exp}".strip()
                else:
                    query_text = str(ex)
                related_slices.extend(
                    retriever.get_related_kb_chunks(query_text, k=5, exclude_paths=[current_path])
                )
    # Deduplicate by path
    def _dedup(chunks):
        seen = set()
        out = []
        for c in chunks or []:
            path = c.get("完整路径", "")
            if not path or path in seen:
                continue
            seen.add(path)
            out.append(c)
        return out
    parent_slices = _dedup(parent_slices)
    related_slices = _dedup(related_slices)
    data = {
        "当前切片": json.loads(format_kb_chunk_full(kb_chunk)),
        "上一级切片全集": [json.loads(format_kb_chunk_full(c)) for c in parent_slices],
        "相似切片": [json.loads(format_kb_chunk_full(c)) for c in related_slices],
        "metadata": {
            "当前路径": current_path,
            "上一级路径": _get_parent_path(current_path),
        }
    }
    return json.dumps(data, ensure_ascii=False, indent=2), parent_slices, related_slices
```

## `resolve_effective_generation_mode`

```python
def resolve_effective_generation_mode(raw_mode: Optional[str], state: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """
    返回 (effective_mode, normalized_mode)：
    - normalized_mode: 规范化后的用户筛选条件
    - effective_mode: 本题实际执行条件（随机模式下在两类中选一）
    """
    normalized = normalize_generation_mode(raw_mode)
    if normalized != "随机":
        return normalized, normalized
    # 随机模式下做轻量轮转，保证两类都能覆盖
    seed = int(time.time() * 1000)
    if isinstance(state, dict):
        seed += int(state.get("retry_count", 0) or 0)
    effective = "基础概念/理解记忆" if seed % 2 == 0 else "实战应用/推演"
    return effective, normalized
```

## `has_business_context`

```python
def has_business_context(text: str) -> bool:
    """轻量判定题干是否包含业务场景语义。"""
    content = str(text or "")
    if not content.strip():
        return False
    keywords = [
        "客户", "业主", "经纪人", "门店", "带看", "签约", "过户", "交易",
        "税费", "贷款", "公积金", "合同", "房源", "咨询", "看房", "收佣", "服务",
    ]
    return any(k in content for k in keywords)

# --- State Definition ---
class AgentState(TypedDict):
    kb_chunk: Dict
    examples: List[Dict]
    agent_name: Optional[str]
    draft: Optional[Dict]
    final_json: Optional[Dict]
    critic_feedback: Optional[str]
    critic_result: Optional[Dict]  # ✅ Critic 验证结果 (passed, issue_type, reason)
    retry_count: int
    logs: Annotated[List[str], operator.add] # Append-only logs for UI
    term_locks: Optional[List[str]]  # Locked domain terms detected from kb chunk
    router_details: Optional[Dict]
    tool_usage: Optional[Dict]
    critic_tool_usage: Optional[Dict]
    critic_details: Optional[str]
    # Debug flag: force a single fix loop for Studio testing.
    debug_force_fail_once: Optional[bool]
    # ✅ Code-as-Tool: Dynamic code generation fields
    generated_code: Optional[str]  # Python code generated by LLM
    execution_result: Optional[Any]  # Result from executing the code
    code_status: Optional[str]  # 'success' or 'error'
    solver_commentary: Optional[str]  # Critic's independent solving explanation
    # ✅ Question type state transfer: Writer passes actual question type to downstream nodes
    current_question_type: Optional[str]  # Actual question type determined by Writer node
    current_generation_mode: Optional[str]  # Actual mode chosen for this question
    # ✅ Model switching: Track which model was actually used
    critic_model_used: Optional[str]  # Actual model used by Critic (for UI display)
    calculator_model_used: Optional[str]  # Actual model used by Calculator (for UI display)
    # LLM trace fields
    trace_id: Optional[str]
    question_id: Optional[str]
    llm_trace: Annotated[List[Dict[str, Any]], operator.add]
    llm_summary: Optional[Dict[str, Any]]
    unstable_flags: Optional[List[str]]

# NOTE:
# The installed `langgraph` version in this repo does NOT support `config_schema`
# in `StateGraph.compile()`, so Studio cannot auto-render a configurable UI form
# from a schema here. We keep runtime defaults and rely on env/config file for
# model/api_key/base_url.

# --- Helper Functions ---
_DEFAULT_RETRIEVER: Optional[KnowledgeRetriever] = None
_GLOSSARY_CACHE: Optional[Dict[str, Any]] = None
```

## `detect_term_lock_violations`

```python
def detect_term_lock_violations(term_locks: List[str], payload: Dict[str, Any]) -> List[str]:
    if not term_locks:
        return []
    lock_set = set(term_locks or [])
    raw_text_parts = []
    if isinstance(payload, dict):
        for key in ["题干", "解析", "question", "explanation"]:
            if key in payload:
                raw_text_parts.append(str(payload.get(key, "") or ""))
        if isinstance(payload.get("options"), list):
            raw_text_parts.extend([str(x) for x in payload.get("options", []) if x is not None])
        for i in range(1, 9):
            v = payload.get(f"选项{i}") if isinstance(payload, dict) else None
            if v is not None:
                raw_text_parts.append(str(v))
    raw_text = " ".join(raw_text_parts)
    text = _question_text_for_term_check(payload)
    if not text:
        return []
    glossary = _build_glossary_cache()
    all_terms = glossary.get("terms", []) or []
    present_terms = [t for t in all_terms if t in text]

    def _looks_like_substitution(lock: str, cand: str) -> bool:
        if cand == lock:
            return False
        def _is_subseq(shorter: str, longer: str) -> bool:
            it = iter(longer)
            return all(ch in it for ch in shorter)
        # Abbreviation-like pattern: same first/last char and candidate is shorter.
        if len(cand) >= 2 and len(cand) < len(lock):
            if lock[0] == cand[0] and lock[-1] == cand[-1]:
                return True
            # Abbreviation-like subsequence (e.g., 商业贷款 -> 商贷)
            if lock[0] == cand[0] and _is_subseq(cand, lock):
                return True
        # Prefix/suffix containment relation (e.g., 全称/简称 variants).
        if lock.startswith(cand) or cand.startswith(lock) or lock.endswith(cand) or cand.endswith(lock):
            return True
        return False

    def _is_explanatory_usage(lock: str, cand: str, source_text: str) -> bool:
        if not source_text:
            return False
        explain_keywords = ["简称", "又称", "也称", "俗称", "即", "是指", "指的是", "全称"]
        # Sentence-level relaxation: same sentence contains both terms + explanation keyword.
        for sentence in re.split(r"[。！？；;!\n]", source_text):
            if lock in sentence and cand in sentence and any(k in sentence for k in explain_keywords):
                return True
        # Allow explicit terminology explanation forms.
        patterns = [
            rf"{re.escape(lock)}\s*(?:（[^）]{{0,30}}）)?\s*(?:简称|又称|也称|俗称|即|是指|指的是|全称)\s*{re.escape(cand)}",
            rf"{re.escape(cand)}\s*(?:（[^）]{{0,30}}）)?\s*(?:简称|又称|也称|俗称|即|是指|指的是|全称)\s*{re.escape(lock)}",
            rf"{re.escape(lock)}\s*[:：]\s*{re.escape(cand)}",
            rf"{re.escape(cand)}\s*[:：]\s*{re.escape(lock)}",
        ]
        return any(re.search(p, source_text) for p in patterns)

    violations: List[str] = []
    for lock in term_locks:
        similar_hits = []
        for t in present_terms:
            if t == lock:
                continue
            # If candidate itself is also a locked term for this chunk, treat as coexisting
            # mandatory terminology instead of replacement.
            if t in lock_set:
                continue
            if not _looks_like_substitution(lock, t):
                continue
            if _is_explanatory_usage(lock, t, raw_text):
                continue
            if t not in similar_hits:
                similar_hits.append(t)
            if len(similar_hits) >= 3:
                break
        if similar_hits:
            violations.append(f"术语疑似改词：应为“{lock}”，检测到近似词“{'/'.join(similar_hits)}”")
    return violations
```

## `parse_json_from_response`

```python
def parse_json_from_response(text: str) -> Dict:
    """
    Robustly extracts and parses JSON from LLM response text.
    Handles markdown code blocks, plain JSON, and common formatting issues.
    """
    if not text:
        raise ValueError("Empty response from LLM")

    # Some providers return list content; normalize to string.
    if isinstance(text, list):
        text = "\n".join([str(item) for item in text if item is not None])
    elif not isinstance(text, str):
        text = str(text)

    text = text.strip()
    
    # 1. Try to find JSON within markdown code blocks
    # Matches ```json { ... } ``` or ``` { ... } ```
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        # 2. Try to find the first '{' and last '}'
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            json_str = text[start:end+1]
        else:
            # 3. Assume the whole text is JSON
            json_str = text
            
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        # Provide a snippet of the failed text for debugging
        snippet = json_str[:200] + "..." if len(json_str) > 200 else json_str
        raise ValueError(f"Failed to parse JSON: {e}. Content snippet: {snippet}")

# --- LLM Factory ---
```

## `call_llm`

```python
def call_llm(
    node_name: str,
    prompt: str,
    model_name: str,
    api_key: str = None,
    base_url: str = None,
    provider: str = None,
    trace_id: Optional[str] = None,
    question_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    timeout: int = 300,
) -> Tuple[str, str, Dict[str, Any]]:
    # NOTE: In Studio UI, users might omit config; provide safe defaults.
    if not model_name:
        model_name = MODEL_NAME or "deepseek-chat"

    provider = str(provider or "").lower()
    model_lower = model_name.lower()
    base_url_lower = str(base_url or "").lower()
    if provider:
        is_ark = provider == "ark"
    else:
        is_ark = ("volces.com" in base_url_lower) or ("ark.cn" in base_url_lower)

    def is_retryable_error(err: Exception) -> bool:
        err_str = str(err)
        err_lower = err_str.lower()
        return (
            "429" in err_str
            or "rate" in err_lower
            or "too many" in err_lower
            or "500" in err_str
            or "502" in err_str
            or "503" in err_str
            or "504" in err_str
            or "timeout" in err_lower
            or "timed out" in err_lower
            or "connection" in err_lower
        )

    def build_record(
        *,
        success: bool,
        used_model: str,
        provider_used: str,
        started_at: float,
        retries: int,
        usage_obj: Any = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        ended_at = time.time()
        usage = _extract_usage_dict(usage_obj)
        return {
            "trace_id": trace_id,
            "question_id": question_id,
            "node": node_name,
            "provider": provider_used,
            "model": used_model,
            "prompt_version": prompt_version,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "latency_ms": round((ended_at - started_at) * 1000, 2),
            "retries": retries,
            "success": success,
            "error": error,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended_at)),
        }

    if is_ark:
        started = time.time()
        ark_backoff_seconds = [2, 5, 10]
        for attempt in range(len(ark_backoff_seconds) + 1):
            try:
                ark_key = ARK_API_KEY or api_key
                if ark_key:
                    client = Ark(
                        api_key=ark_key,
                        base_url=(base_url or ARK_BASE_URL),
                    )
                else:
                    if not (VOLC_ACCESS_KEY_ID and VOLC_SECRET_ACCESS_KEY):
                        raise ValueError("ARK_API_KEY is required for Ark chain, or provide VOLC_ACCESS_KEY_ID / VOLC_SECRET_ACCESS_KEY")
                    client = Ark(
                        ak=VOLC_ACCESS_KEY_ID,
                        sk=VOLC_SECRET_ACCESS_KEY,
                        base_url=(base_url or ARK_BASE_URL),
                    )
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    extra_headers=({"X-Project-Name": ARK_PROJECT_NAME} if ARK_PROJECT_NAME else None),
                )
                content = resp.choices[0].message.content if resp.choices else ""
                record = build_record(
                    success=True,
                    used_model=model_name,
                    provider_used="ark",
                    started_at=started,
                    retries=attempt,
                    usage_obj=getattr(resp, "usage", None),
                )
                return content, model_name, record
            except Exception as e:
                if is_retryable_error(e) and attempt < len(ark_backoff_seconds):
                    wait_time = ark_backoff_seconds[attempt]
                    print(f"⚠️ Ark 限流/服务错误，等待 {wait_time}s 后重试 (第 {attempt+1} 次)")
                    time.sleep(wait_time)
                    continue
                print(f"❌ Ark 调用失败: {e}")
                record = build_record(
                    success=False,
                    used_model=model_name,
                    provider_used="ark",
                    started_at=started,
                    retries=attempt,
                    usage_obj=None,
                    error=str(e),
                )
                return "", model_name, record

    key = api_key or API_KEY
    url = base_url or BASE_URL
    used_model = model_name
    backoff_seconds = [5, 10, 20, 30, 45, 60, 60, 60, 60, 60]
    started = time.time()
    url_candidates: List[str] = []
    base_u = str(url or "").rstrip("/")
    if base_u:
        url_candidates.append(base_u)
        if not base_u.endswith("/v1"):
            url_candidates.append(f"{base_u}/v1")
    else:
        url_candidates.append(base_u)
    # de-duplicate while preserving order
    seen_url = set()
    url_candidates = [u for u in url_candidates if not (u in seen_url or seen_url.add(u))]

    for attempt in range(len(backoff_seconds) + 1):
        try:
            last_non_retryable: Optional[Exception] = None
            for candidate_url in url_candidates:
                try:
                    client = OpenAI(api_key=key, base_url=candidate_url)
                    resp = client.chat.completions.create(
                        model=used_model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=timeout,
                    )
                    content = resp.choices[0].message.content if resp.choices else ""
                    if isinstance(content, list):
                        content = "\n".join(
                            str(item.get("text", "")) if isinstance(item, dict) else str(item)
                            for item in content
                        ).strip()
                    elif not isinstance(content, str):
                        content = str(content or "")
                    if not content.strip():
                        raise ValueError(f"Empty response (attempt {attempt + 1})")
                    record = build_record(
                        success=True,
                        used_model=used_model,
                        provider_used=(provider or "ait"),
                        started_at=started,
                        retries=attempt,
                        usage_obj=getattr(resp, "usage", None),
                    )
                    return content, used_model, record
                except Exception as inner:
                    if is_retryable_error(inner):
                        raise
                    last_non_retryable = inner
            if last_non_retryable is not None:
                raise last_non_retryable
            raise RuntimeError("No valid base_url candidate for OpenAI-compatible call")
        except Exception as e:
            err_str = str(e)
            is_retryable = is_retryable_error(e)
            if is_retryable and attempt < len(backoff_seconds):
                wait_time = backoff_seconds[attempt]
                print(f"⚠️ OpenAI-compatible 限流/服务错误，等待 {wait_time}s 后重试 (第 {attempt+1} 次)")
                time.sleep(wait_time)
                continue
            record = build_record(
                success=False,
                used_model=used_model,
                provider_used=(provider or "ait"),
                started_at=started,
                retries=attempt,
                usage_obj=None,
                error=err_str,
            )
            return "", used_model, record
```

## `execute_python_code`

```python
def execute_python_code(code: str, max_execution_time: float = 5.0) -> Tuple[Any, str, str]:
    """
    Safely execute dynamically generated Python code in a restricted environment.
    
    Args:
        code: Python code string to execute
        max_execution_time: Maximum execution time in seconds (default 5.0)
    
    Returns:
        tuple: (result_value, stdout_output, stderr_output)
    """
    # Create a restricted execution environment
    import builtins
    allowed_modules = {"math", "datetime", "decimal", "time", "_strptime"}

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if root not in allowed_modules:
            raise ImportError(f"Module '{name}' is not allowed")
        return builtins.__import__(name, globals, locals, fromlist, level)

    restricted_globals = {
        '__builtins__': {
            # Only allow safe built-in functions
            'abs': abs, 'round': round, 'min': min, 'max': max,
            'sum': sum, 'len': len, 'int': int, 'float': float, 'str': str,
            'bool': bool, 'type': type, 'isinstance': isinstance,
            'range': range, 'enumerate': enumerate, 'zip': zip,
            'print': print,  # For debugging
            '__import__': safe_import,
        },
        '__name__': '__main__',
        '__doc__': None,
    }
    
    restricted_locals = {}
    
    # Capture stdout and stderr
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    
    result_value = None
    
    try:
        with contextlib.redirect_stdout(stdout_capture), \
             contextlib.redirect_stderr(stderr_capture):
            # Execute the code
            exec(code, restricted_globals, restricted_locals)
            
            # Try to get the result (look for common result variable names)
            if 'result' in restricted_locals:
                result_value = restricted_locals['result']
            elif 'answer' in restricted_locals:
                result_value = restricted_locals['answer']
            elif 'value' in restricted_locals:
                result_value = restricted_locals['value']
            # If code ends with an expression, it won't be captured, but that's OK
    
    except Exception as e:
        error_msg = f"Execution error: {type(e).__name__}: {str(e)}"
        stderr_capture.write(error_msg)
        result_value = None
    
    stdout_str = stdout_capture.getvalue()
    stderr_str = stderr_capture.getvalue()
    
    return result_value, stdout_str, stderr_str
```

## `critical_decision`

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

# --- Graph Construction ---
# --- Code Execution (Safe Sandbox) ---
import sys
import io
import contextlib
from types import ModuleType
```

