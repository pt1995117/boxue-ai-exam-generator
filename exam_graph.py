import os
import json
import operator
import re
from typing import Annotated, List, Dict, Optional, TypedDict, Union, Any
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, END
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

# Reuse existing config loading
from exam_factory import API_KEY, GEMINI_KEY, BASE_URL, MODEL_NAME

# --- State Definition ---
class AgentState(TypedDict):
    kb_chunk: Dict
    examples: List[Dict]
    agent_name: Optional[str]
    draft: Optional[Dict]
    final_json: Optional[Dict]
    critic_feedback: Optional[str]
    critic_result: Optional[Dict]  # ✅ 新增：Critic 验证结果 (passed, issue_type, reason)
    retry_count: int
    logs: Annotated[List[str], operator.add] # Append-only logs for UI
    router_details: Optional[Dict]
    tool_usage: Optional[Dict]
    critic_tool_usage: Optional[Dict]
    critic_details: Optional[str]

# --- Helper Functions ---
def parse_json_from_response(text: str) -> Dict:
    """
    Robustly extracts and parses JSON from LLM response text.
    Handles markdown code blocks, plain JSON, and common formatting issues.
    """
    if not text:
        raise ValueError("Empty response from LLM")
    
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
from google import genai
from google.genai import types

def generate_content(model_name: str, prompt: str, api_key: str = None, base_url: str = None):
    is_gemini = "gemini" in model_name.lower() or "flash" in model_name.lower()
    
    import time
    
    if is_gemini:
        key = api_key or GEMINI_KEY
        client = genai.Client(api_key=key)
        
        max_retries = 5  # 增加重试次数
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=0.3)
                )
                if response.text:
                    return response.text
                else:
                    # Try to get more info on why it's empty (e.g. safety)
                    reason = "Unknown"
                    try:
                        if hasattr(response, 'candidates') and response.candidates:
                            reason = response.candidates[0].finish_reason
                    except:
                        pass
                    print(f"⚠️ Warning: Gemini returned None (Attempt {attempt+1}/{max_retries}). Reason: {reason}")
            except Exception as e:
                error_str = str(e)
                print(f"⚠️ Gemini Error (Attempt {attempt+1}/{max_retries}): {e}")
                
                # 检测可重试的错误：配额限制、服务器错误、网络/SSL错误
                is_retriable = any([
                    "503" in error_str,
                    "429" in error_str,
                    "RESOURCE_EXHAUSTED" in error_str,
                    "SSL" in error_str,  # ✅ SSL 连接错误
                    "EOF" in error_str,  # ✅ 连接中断
                    "timeout" in error_str.lower(),  # ✅ 超时
                    "connection" in error_str.lower()  # ✅ 连接问题
                ])
                
                if is_retriable:
                    # 智能等待：逐渐增加等待时间
                    wait_times = [10, 30, 60, 90, 120]  # 10秒, 30秒, 60秒, 90秒, 120秒
                    wait_time = wait_times[min(attempt, len(wait_times)-1)]
                    
                    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                        print(f"⏳ 检测到 API 配额限制，等待 {wait_time} 秒后重试...")
                    else:
                        print(f"⏳ 检测到网络/连接问题，等待 {wait_time} 秒后重试...")
                    print(f"   (这是正常的重试策略，请耐心等待)")
                    time.sleep(wait_time)
                    continue
                else:
                    # 其他错误（非网络/配额问题），不重试
                    print(f"❌ 不可重试的错误，停止重试: {error_str}")
                    return ""
        
        print(f"❌ 尝试 {max_retries} 次后仍然失败，请稍后再试或检查 API 配置")
        return ""
    else:
        # OpenAI compatible API (including DeepSeek)
        key = api_key or API_KEY
        url = base_url or BASE_URL
        # 添加超时设置：DeepSeek Reasoner 需要更长的推理时间
        client = ChatOpenAI(
            model=model_name, 
            api_key=key, 
            base_url=url, 
            temperature=0.3,
            timeout=120.0,  # 120秒超时（Reasoner模型需要更长时间推理）
            max_retries=1   # 失败后重试1次（避免重复等待）
        )
        return client.invoke(prompt).content

# --- Nodes ---

def router_node(state: AgentState, config):
    kb_chunk = state['kb_chunk']
    # 1. Analyze Content
    content = kb_chunk['核心内容']
    path = kb_chunk['完整路径']
    mastery = kb_chunk.get('掌握程度', '未知')
    
    prompt = f"""
# 角色
你是路由代理 (Router Agent)。
你的任务是根据【参考材料】的内容，决定由哪位专家代理来生成题目。

# 参考材料
【路径】: {path}
【掌握程度】: {mastery}
【内容】:
{content}

# 专家列表
1. **FinanceAgent (金融专家)**: 擅长计算、数值、公式、税费、贷款、面积计算等。
   - 关键词: 计算, 税费, 贷款, 首付, 利率, 金额, 比例, 公式, 年限, 面积, 单价, 总价.
2. **LegalAgent (法律专家)**: 擅长法律法规、政策条例、违规处罚、纠纷处理等。
   - 关键词: 法律, 法规, 条例, 规定, 违法, 违规, 处罚, 责任, 纠纷, 合同, 权利, 义务.
3. **GeneralAgent (综合专家)**: 擅长概念定义、流程步骤、业务常识等非计算非法律类内容。
   - 关键词: 流程, 步骤, 定义, 概念, 特点, 优势, 劣势, 含义, 职能.

# 决策逻辑
1. 如果内容包含具体的数值计算、公式应用或财务相关概念，优先选择 **FinanceAgent**。
2. 如果内容主要涉及法律条文、合规性判断或权责界定，选择 **LegalAgent**。
3. 其他情况，或者内容较为基础、偏向记忆理解的，选择 **GeneralAgent**。

# 输出格式
请严格按照 JSON 格式输出，包含以下字段:
- "agent": "FinanceAgent", "LegalAgent", 或 "GeneralAgent"
- "score_finance": 0-10 (整数，表示内容与金融计算的相关度)
- "score_legal": 0-10 (整数，表示内容与法律法规的相关度)
- "reasoning": "简短的决策理由"

示例:
```json
{{
    "agent": "FinanceAgent",
    "score_finance": 9,
    "score_legal": 2,
    "reasoning": "内容涉及具体的税费计算公式"
}}
```
"""
    
    # Router 使用统一配置的模型
    model_to_use = config['configurable'].get('model')
    response_text = generate_content(
        model_to_use, 
        prompt, 
        config['configurable'].get('api_key'),
        config['configurable'].get('base_url')
    )
    
    try:
        result = parse_json_from_response(response_text)
        agent = result.get("agent", "GeneralAgent")
        score_finance = result.get("score_finance", 0)
        score_legal = result.get("score_legal", 0)
        reasoning = result.get("reasoning", "")
        
    except Exception as e:
        print(f"⚠️ Router JSON parsing failed: {e}. Defaulting to GeneralAgent.")
        agent = "GeneralAgent"
        score_finance = 0
        score_legal = 0
        reasoning = f"Parsing Error: {str(e)}"

    # Basic validation for the agent name
    if agent not in ["FinanceAgent", "LegalAgent", "GeneralAgent"]:
        print(f"⚠️ Router returned an unexpected agent name: {agent}. Defaulting to GeneralAgent.")
        agent = "GeneralAgent"

    # 清理旧状态（如果是 reroute）
    state_updates = {
        "agent_name": agent,
        "router_details": {
            "path": path,
            "content": content,
            "mastery": mastery,
            "score_finance": score_finance,
            "score_legal": score_legal,
            "agent": agent,
            "reasoning": reasoning
        },
        "logs": [f"🤖 路由: 金融分={score_finance}, 法律分={score_legal}. 决策: **{agent}** ({reasoning})"]
    }
    
    # 如果是重新路由（retry_count > 0），清理旧的生成结果
    if state.get('retry_count', 0) > 0:
        state_updates["draft"] = None
        state_updates["final_json"] = None
        state_updates["logs"].append(f"🔄 检测到重新路由 (retry #{state['retry_count']})，已清理旧状态")
    
    return state_updates

def specialist_node(state: AgentState, config):
    agent_name = state['agent_name']
    kb_chunk = state['kb_chunk']
    
    # Fetch examples AFTER routing, based on knowledge point and question type
    retriever = config['configurable'].get('retriever')
    question_type = config['configurable'].get('question_type')
    generation_mode = config['configurable'].get('generation_mode', '灵活')
    
    examples = []
    if retriever:
        examples = retriever.get_examples_by_knowledge_point(kb_chunk, k=3, question_type=question_type)
    
    # 根据模式调整提示词
    if generation_mode == "严谨":
        mode_instructions = """
# 出题模式：严谨模式（用于标准化考试）
要求：
1. **严格忠实原文**：题目必须严格按照参考材料的内容，不得添加任何材料外的信息或推理。
2. **标准化表述**：使用标准的考试题目表述方式，避免口语化或场景化描述。
3. **直接考察知识点**：直接考察知识点本身，不进行场景化包装。
4. **标准化选项**：选项表述简洁、准确，符合标准化考试风格。干扰项设计利用**"相近的数字"**或**"错误的参照物"**。
5. **严谨的解析**：解析必须严格按照"1、教材原文 2、试题分析 3、结论"的结构，直接引用原文。

禁止：
- 禁止添加假设性场景（如"客户咨询..."、"在交易中..."）
- 禁止使用口语化表达
- 禁止在题干中添加材料外的信息
"""
    else:  # 灵活模式
        mode_instructions = """
# 出题模式：灵活模式（适合日常练习）
要求：
1. **场景化表达**：将题目融入实际工作场景（例如"客户咨询..."、"在交易中..."），增强实用性。
2. **灵活表述**：可以使用更自然、更贴近实际工作的表述方式。
3. **创意干扰项**：错误选项可以更灵活，利用常见误区。利用**"相近的数字"**或**"错误的参照物"**设计干扰项。
4. **生动解析**：解析可以更生动，但必须保持准确性。
"""
    
    # Call LLM
    prompt = f"""
# 角色
你是 {agent_name}。
请严格基于【参考材料】创作一道高质量的单项选择题。

{mode_instructions}

# 质量标准 (必须达成):
1. **准确性 (40%)**: 100% 忠实于原文，绝无幻觉。
2. **干扰项质量 (25%)**: 错误选项必须似是而非，利用常见误区，不要一眼假。除非必要，避免使用"以上皆是"。
   - **干扰项设计技巧**：利用**"相近的数字"**（如正确答案是3年，干扰项用2年或4年）或**"错误的参照物"**（如混淆不同概念、用类似但不正确的表述）
3. **相关性 (15%)**: 考察核心概念或逻辑，不要考细枝末节。
4. **格式 (10%)**: 严格的 JSON 输出。

# 参考材料
{kb_chunk['核心内容']}

# 范例
"""
    for i, ex in enumerate(examples, 1):
        prompt += f"例 {i}: {ex['题干']}\n"
        
    prompt += """
# 任务
返回 JSON: {"question": "...", "options": ["A", "B", "C", "D"], "answer": "A/B/C/D", "explanation": "..."}
约束: 题干中**禁止**出现"根据材料"、"依据参考资料"等字眼。题目必须是独立的。
"""
    content = generate_content(
        config['configurable'].get('model'), 
        prompt, 
        config['configurable'].get('api_key'),
        config['configurable'].get('base_url')
    )
    
    try:
        # Log raw content for debugging
        print(f"DEBUG RAW CONTENT: {content}")
        
        draft = parse_json_from_response(content)
        return {
            "draft": draft,
            "examples": examples,  # Pass examples to UI
            "logs": [f"👨‍💻 {agent_name}: 初稿已生成"]
        }
    except Exception as e:
        return {"logs": [f"❌ {agent_name} 错误: {str(e)}"]}

def writer_node(state: AgentState, config):
    draft = state.get('draft')
    # If draft is missing (e.g. previous step failed), skip writer
    if not draft:
        return {"logs": ["❌ 作家: 未收到有效初稿，跳过润色。"]}

    kb_chunk = state['kb_chunk']
    
    prompt = f"""

# 任务
你是最终编辑。请将以下初稿转化为严格的输出格式。
初稿: {json.dumps(draft, ensure_ascii=False)}
参考: {kb_chunk['核心内容']}

# 输出格式 (JSON)
{{
    "题干": "...",
    "选项1": "...", "选项2": "...", "选项3": "...", "选项4": "...",
    "正确答案": "A/B/C/D",
    "解析": "1、教材原文... 2、试题分析... 3、结论...",
    "难度值": 0.5,
    "考点": "..."
}}
约束: "题干"中**禁止**出现"根据材料"、"依据参考资料"等字眼。
"""
    # Writer 使用统一配置的模型
    model_to_use = config['configurable'].get('model')
    content = generate_content(
        model_to_use, 
        prompt, 
        config['configurable'].get('api_key'),
        config['configurable'].get('base_url')
    )
    
    try:
        final_json = parse_json_from_response(content)
        return {
            "final_json": final_json,
            "logs": ["✍️ 作家: 格式已优化"]
        }
    except Exception as e:
        return {"logs": [f"❌ 作家错误: {str(e)}"]}

def critic_node(state: AgentState, config):
    final_json = state.get('final_json')
    if not final_json:
        return {
            "critic_feedback": "FAIL", 
            "critic_details": "No question generated to verify.",
            "logs": ["🕵️ 批评家: 无法审核，未生成题目。"]
        }

    kb_chunk = state['kb_chunk']
    
    # Create a blind copy of the question (remove answer and explanation)
    blind_question = {k: v for k, v in final_json.items() if k not in ['正确答案', '解析', 'answer', 'explanation']}
    
    # --- Critic Tool Step ---
    # 1. Decide if calculation is needed to verify this question
    prompt_plan = f"""
# 角色
你是批评家 (Critic)。
你需要验证以下题目是否正确。请分析【题目】和【参考材料】，判断是否需要进行数值计算来验证答案。

# 重要提示：参数提取和计算步骤分析
**计算器可能只是解决整个问题的一个步骤，而不是整个问题！**

在验证题目时，请仔细分析：
1. **题目问的是什么？**（最终答案是什么）
2. **计算器能计算什么？**（计算器能解决哪个步骤）
3. **如何从题目中提取参数？**（题干和选项中可能包含计算所需的数据）

**参数提取规则：**
- 必须从题目中提取**具体的数值**（如：80平方米、1560元、2025年、1993年）
- **不能使用描述性文字**（如："成本价"、"建筑面积"、"建成年代"）
- 如果题目中没有明确数值，需要根据参考材料推断合理的数值
- 注意单位的统一（平方米、元、年等）

**计算步骤分析：**
- 如果题目问的是最终结果，可能需要多步计算
- 计算器可能只解决其中一个步骤
- 需要验证：计算器结果 + 其他步骤 = 题目答案

例如：
- 题目问"土地出让金是多少"，如果题干给出"建筑面积80平方米，成本价1560元/平方米"
  → 调用 `calculate_land_grant_fee_public_housing(area=80, cost_price=1560)`
  
- 题目问"最长贷款年限是多少"，题干给出"建成年代1993年，当前2025年"
  → 先调用 `calculate_house_age(2025, 1993, for_loan=True)` 计算房龄
  → 再根据"房龄+贷款年限≤50年"计算：50-房龄=贷款年限上限
  → 可能还需要考虑借款人年龄等其他因素

# 题目
{json.dumps(blind_question, ensure_ascii=False)}

# 工具列表 (必须提供所有参数)
- calculate_loan_amount(evaluation_price, loan_ratio)
- calculate_provident_fund_loan(balance_applicant, balance_co_applicant, multiple, year_coefficient)
- calculate_vat(price, original_price, years_held, is_ordinary, is_residential)
- calculate_deed_tax(price, area, is_first_home, is_second_home, is_residential)
- calculate_land_grant_fee_economical(price, original_price, buy_date_is_before_2008_4_11)
- calculate_land_grant_fee_managed_economical(price)
- calculate_land_grant_fee_public_housing(area, cost_price=1560)
  * area: 建筑面积（平方米，必须是数字）
  * cost_price: 当年成本价格（元/平方米，必须是数字，默认1560）
  * 注意：cost_price 参数必须是数字（如1560），不能是字符串（如"成本价"）
- calculate_land_remaining_years(total_years, current_year, grant_year)
- calculate_house_age(current_year, completion_year, for_loan=False)
  * 通用房龄（for_loan=False）: 房龄 = 截止年份 - 房屋竣工年份
  * 贷款计算用房龄（for_loan=True）: 房龄 = 50 - (当前年份 - 建成年代)
  * 注意：公积金/商业贷款题目应使用 for_loan=True
- calculate_indoor_height(floor_height, slab_thickness)
- calculate_building_area(inner_area, shared_area)
- calculate_efficiency_rate(inner_use_area, building_area)
- calculate_area_error_ratio(registered_area, contract_area)
- calculate_price_diff_ratio(listing_price, deal_price)
- calculate_plot_ratio(total_building_area, total_land_area)
- calculate_green_rate(green_area, total_land_area)

# 参考材料
{kb_chunk['核心内容']}

# 任务
返回 JSON: {{"tool": "function_name", "params": {{...}}}}
如果不需要计算，返回 {{"tool": "None"}}
"""
    plan_content = generate_content(
        config['configurable'].get('model'), 
        prompt_plan, 
        config['configurable'].get('api_key'),
        config['configurable'].get('base_url')
    )
    
    calc_result = None
    tool_used = "None"
    tool_params = {}
    
    try:
        plan = parse_json_from_response(plan_content)
        tool_used = plan.get("tool")
        tool_params = plan.get("params", {})
        
        if tool_used and tool_used != "None" and hasattr(RealEstateCalculator, tool_used):
            method = getattr(RealEstateCalculator, tool_used)
            # Execute Calculation
            calc_result = method(**tool_params)
            print(f"DEBUG CRITIC CALC: {tool_used}({tool_params}) = {calc_result}")
    except Exception as e:
        print(f"DEBUG CRITIC CALC ERROR: {e}")

    # --- Verification Step ---
    prompt = f"""
# 角色
你是批评家 (Critic)。
你需要严格审核以下题目，确保其准确性、逻辑性和清晰度。

# 参考材料
{kb_chunk['核心内容']}

# 计算辅助
批评家使用了工具: {tool_used}
工具参数: {tool_params}
计算结果: {calc_result}

**重要提示：理解计算步骤**
- 计算器可能只是解决整个问题的一个步骤，而不是整个问题
- 如果题目问的是最终结果，可能需要多步计算：
  ① 计算器结果（如：房龄 = 18年）
  ② 基于计算器结果进一步计算（如：贷款年限上限 = 50 - 18 = 32年）
  ③ 可能还需要考虑其他因素（如：借款人年龄限制），取最小值
  
**验证时的要求：**
- 如果计算器结果就是最终答案：直接对比计算结果和题目答案
- 如果计算器结果只是中间步骤：需要验证完整的计算过程
  - 检查解析中是否说明了所有计算步骤
  - 验证最终答案是否基于计算器结果正确计算得出
  - 验证是否考虑了所有相关因素（如：取最小值）

(如果结果有效，请优先依据此结果进行判断，但需要理解它可能是中间步骤还是最终答案)

# 待审核题目
题干: {final_json['题干']}
选项:
A. {final_json['选项1']}
B. {final_json['选项2']}
C. {final_json['选项3']}
D. {final_json['选项4']}
正确答案: {final_json['正确答案']}
解析: {final_json['解析']}

# 审核任务
1. **答案验证**: 
   - 如果题目涉及计算，使用计算结果验证答案
   - 如果计算器结果是中间步骤，验证完整计算过程：
     * 第一步计算是否正确（计算器结果）
     * 后续步骤是否正确（基于第一步结果的计算）
     * 是否考虑了所有相关因素（如：取最小值）
   - 独立做题，判断【正确答案】是否与参考材料（及计算结果）一致
   
2. **解析审查**: 
   - 解析是否说明了完整的计算过程？（如果涉及多步计算）
   - 解析是否逻辑清晰？
   - 是否有力地解释了为何选该答案？
   - 是否说明了其他选项为何错误？
   - 是否存在与材料矛盾的说法？
   - 如果计算器结果是中间步骤，解析中是否说明了所有步骤？

# 输出格式 (JSON)
{{
    "critic_answer": "A/B/C/D",
    "explanation_valid": true/false,
    "reason": "详细说明驳回原因（如果通过则简述理由）"
}}
"""
    response_text = generate_content(
        config['configurable'].get('model'), 
        prompt, 
        config['configurable'].get('api_key'),
        config['configurable'].get('base_url')
    )
    
    critic_answer = "UNKNOWN"
    explanation_valid = False
    reason = "Parsing Failed"
    
    try:
        review_result = parse_json_from_response(response_text)
        critic_answer = review_result.get("critic_answer", "UNKNOWN").strip().upper()
        explanation_valid = review_result.get("explanation_valid", False)
        reason = review_result.get("reason", "")
    except Exception as e:
        print(f"DEBUG CRITIC PARSE ERROR: {e}")
        # Fallback: try to find answer in text if JSON fails
        import re
        match = re.search(r'[ABCD]', response_text)
        if match:
            critic_answer = match.group(0)
    
    gen_answer = final_json['正确答案'].strip().upper()
    
    critic_tool_usage = {
        "tool": tool_used,
        "params": tool_params,
        "result": calc_result
    }

    # Pass Condition: Answer matches AND Explanation is valid
    if critic_answer == gen_answer and explanation_valid:
        return {
            "critic_feedback": "PASS", 
            "critic_details": f"✅ 审核通过 (答案一致且解析合理)",
            "critic_tool_usage": critic_tool_usage,
            "critic_result": {"passed": True},
            "logs": ["🕵️ 批评家: 审核通过"]
        }
    else:
        fail_reason = ""
        issue_type = "minor"  # 默认轻微问题
        
        if critic_answer != gen_answer:
            fail_reason += f"答案不一致 (批评家: {critic_answer} vs 生成者: {gen_answer}); "
            issue_type = "major"  # 答案错误是严重问题
        if not explanation_valid:
            fail_reason += f"解析不合格 ({reason}); "
            # 解析问题通常可以修复，保持 minor
            
        return {
            "critic_feedback": fail_reason,
            "critic_details": f"❌ 审核驳回: {fail_reason}",
            "critic_tool_usage": critic_tool_usage,
            "critic_result": {
                "passed": False,
                "issue_type": issue_type,  # minor: 可修复 / major: 需重新路由
                "reason": fail_reason
            },
            "retry_count": state['retry_count'] + 1, 
            "logs": [f"🕵️ 批评家: 驳回 (第 {state['retry_count']+1} 次). 严重程度: {issue_type}. 原因: {fail_reason}"]
        }

def fixer_node(state: AgentState, config):
    # This node runs if Critic fails
    # It takes the feedback and asks Writer (or Specialist) to fix it.
    
    final_json = state.get('final_json')
    feedback = state.get('critic_feedback', 'Unknown Error')
    kb_chunk = state['kb_chunk']
    
    # CASE 1: Critical Failure (No question generated) -> Regenerate from scratch
    if not final_json:
        prompt = f"""
# 任务
之前的生成流程失败了，未生成有效题目。
原因: {feedback}
参考: {kb_chunk['核心内容']}

# 补救任务
请重新根据参考材料创作一道单项选择题。

# 质量标准:
1. **准确性**: 100% 忠实于原文。
2. **格式**: 严格的 JSON 输出。

# 输出格式 (JSON)
{{
    "题干": "...",
    "选项1": "...", "选项2": "...", "选项3": "...", "选项4": "...",
    "正确答案": "A/B/C/D",
    "解析": "...",
    "难度值": 0.5,
    "考点": "..."
}}
"""
        content = generate_content(
            config['configurable'].get('model'), 
            prompt, 
            config['configurable'].get('api_key'),
            config['configurable'].get('base_url')
        )
        
        try:
            fixed_json = parse_json_from_response(content)
            # Ensure defaults
            if '难度值' not in fixed_json: fixed_json['难度值'] = 0.5
            if '考点' not in fixed_json: fixed_json['考点'] = "补救考点"
            
            return {
                "final_json": fixed_json,
                "logs": ["🔧 修复者: 检测到生成失败，已重新生成题目"]
            }
        except Exception as e:
            return {"logs": [f"❌ 修复者重试失败: {str(e)}"]}

    # CASE 2: Normal Fix (Question exists but rejected)
    prompt = f"""
# 任务
上一道题被批评家驳回了。
原因: {feedback}
参考: {kb_chunk['核心内容']}
题目: {json.dumps(final_json, ensure_ascii=False)}

# 修复要求:
1. **准确性**: 确保答案 100% 有原文支持。
2. **干扰项**: 确保错误选项似是而非但绝对错误。利用**"相近的数字"**或**"错误的参照物"**设计干扰项。
3. **清晰度**: 消除导致批评家困惑的歧义。
4. **完整性**: 必须包含 "难度值" (0.0-1.0) 和 "考点"。

请修复这道题（修改答案、选项或解析），使其正确且无歧义。
约束: 题干中**禁止**出现“根据材料”或“依据参考资料”。
返回修复后的 JSON (包含 题干, 选项1-4, 正确答案, 解析, 难度值, 考点)。
"""
    content = generate_content(
        config['configurable'].get('model'), 
        prompt, 
        config['configurable'].get('api_key'),
        config['configurable'].get('base_url')
    )
    
    try:
        fixed_json = parse_json_from_response(content)
        
        # Fallback for required fields
        if '难度值' not in fixed_json:
            fixed_json['难度值'] = final_json.get('难度值', 0.5)
            
        if '考点' not in fixed_json:
            fixed_json['考点'] = final_json.get('考点', kb_chunk.get('完整路径', '').split('>')[-1].strip() or "综合考点")
            
        return {
            "final_json": fixed_json,
            "logs": ["🔧 修复者: 已修正题目"]
        }
    except Exception as e:
        return {"logs": [f"❌ 修复者错误: {str(e)}"]}

# --- Edges ---
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
    
    # 超限自愈
    if retry_count >= 3:
        return "self_heal"
    
    # 判断问题严重程度
    issue_type = critic_result.get('issue_type', 'minor')
    
    if issue_type == 'major':
        # 严重问题（答案错误）→ 回到 Router 重新路由
        return "reroute"
    else:
        # 轻微问题（解析不清等）→ Fixer 修复
        return "fix"

# --- Graph Construction ---
# --- Tool Integration ---
from calculation_logic import RealEstateCalculator


def finance_node(state: AgentState, config):
    agent_name = "FinanceAgent"
    kb_chunk = state['kb_chunk']
    mastery = kb_chunk.get('掌握程度', '未知')
    
    # Step 1: Fetch examples FIRST (照猫画虎)
    retriever = config['configurable'].get('retriever')
    question_type = config['configurable'].get('question_type')
    
    examples = []
    if retriever:
        examples = retriever.get_examples_by_knowledge_point(kb_chunk, k=3, question_type=question_type)
    
    # Step 2: Decide if calculation is needed based on examples and material
    # If examples contain calculation questions, we should also do calculation
    examples_have_calculations = False
    if examples:
        # Check if any example's explanation mentions numbers or calculations
        for ex in examples:
            explanation = str(ex.get('解析', ''))
            # Simple heuristic: if explanation contains digits or common calc keywords
            if any(keyword in explanation for keyword in ['计算', '公式', '=', '×', '÷', '%', '元', '平方米', '年']):
                examples_have_calculations = True
                break
    
    # Identify Calculation Scenario
    prompt_plan = f"""
# 角色
你是金融专家 (FinanceAgent)。
你需要根据【参考材料】和【参考范例】设计一道单项选择题。
当前知识点的掌握程度要求为: 【{mastery}】。

# 参考材料
{kb_chunk['核心内容']}

# 参考范例分析
范例中{'包含' if examples_have_calculations else '不包含'}计算题。你应该{'优先' if examples_have_calculations else '不必'}使用计算工具。

# 重要提示：计算步骤分析
**计算器可能只是解决整个问题的一个步骤，而不是整个问题！**

在分析需要调用哪个计算器时，请仔细思考：
1. **题目问的是什么？**（最终答案是什么）
2. **计算器能计算什么？**（计算器能解决哪个步骤）
3. **是否需要多步计算？**（计算器结果是否需要进一步处理）

例如：
- 如果题目问"房龄是多少年"，计算器 `calculate_house_age` 可以直接给出答案
- 如果题目问"最长贷款年限是多少年"，可能需要：
  ① 先计算房龄（使用 `calculate_house_age`，for_loan=True）
  ② 再根据"房龄+贷款年限≤50年"计算贷款年限（50-房龄）
  ③ 可能还需要考虑借款人年龄等其他因素，取最小值

**在这种情况下，你应该：**
- 调用计算器计算房龄（这是其中一个步骤）
- 在生成题目时，明确说明这是计算过程中的一个步骤
- 确保题目和解析中体现完整的计算逻辑

# 任务
1. **仔细分析**：题目最终问的是什么？计算器能解决哪个步骤？
2. **选择工具**：如果计算器能直接或间接解决题目，选择合适的计算工具
3. **提取参数**：从参考材料中提取计算所需的**具体数值**（必须是数字，不能是描述性文字）
4. **如果不包含可计算的数值逻辑**，直接返回无需计算

# 工具列表 (必须提供所有参数)
- calculate_loan_amount(evaluation_price, loan_ratio)
- calculate_provident_fund_loan(balance_applicant, balance_co_applicant, multiple, year_coefficient)
- calculate_vat(price, original_price, years_held, is_ordinary, is_residential)
- calculate_deed_tax(price, area, is_first_home, is_second_home, is_residential)
- calculate_land_grant_fee_economical(price, original_price, buy_date_is_before_2008_4_11)
- calculate_land_grant_fee_managed_economical(price)
- calculate_land_grant_fee_public_housing(area, cost_price=1560)
  * area: 建筑面积（平方米，必须是数字）
  * cost_price: 当年成本价格（元/平方米，必须是数字，默认1560）
  * 注意：cost_price 参数必须是数字（如1560），不能是字符串（如"成本价"）
- calculate_land_remaining_years(total_years, current_year, grant_year)
- calculate_house_age(current_year, completion_year, for_loan=False)
  * 通用房龄（for_loan=False）: 房龄 = 截止年份 - 房屋竣工年份
  * 贷款计算用房龄（for_loan=True）: 房龄 = 50 - (当前年份 - 建成年代)
  * 注意：公积金/商业贷款题目应使用 for_loan=True
- calculate_indoor_height(floor_height, slab_thickness)
- calculate_building_area(inner_area, shared_area)
- calculate_efficiency_rate(inner_use_area, building_area)
- calculate_area_error_ratio(registered_area, contract_area)
- calculate_price_diff_ratio(listing_price, deal_price)
- calculate_plot_ratio(total_building_area, total_land_area)
- calculate_green_rate(green_area, total_land_area)

# 输出 JSON
{{
    "need_calculation": true/false,
    "tool": "tool_name",
    "params": {{ "param1": value1, ... }},
    "reason": "..."
}}
"""
    plan_content = generate_content(
        config['configurable'].get('model'), 
        prompt_plan, 
        config['configurable'].get('api_key'),
        config['configurable'].get('base_url')
    )
    
    calc_result = None
    tool_used = "None"
    plan = {}
    
    try:
        plan = parse_json_from_response(plan_content)
        tool_used = plan.get("tool")
        
        if plan.get("need_calculation") and tool_used and tool_used != "None":
            # Execute Tool
            params = plan.get("params", {})
            # Use getattr to find the function in RealEstateCalculator
            if hasattr(RealEstateCalculator, tool_used):
                func = getattr(RealEstateCalculator, tool_used)
                try:
                    # Call the function with unpacked params
                    calc_result = func(**params)
                except Exception as e:
                    calc_result = f"Error: {str(e)}"
            else:
                calc_result = "Error: Tool not found"
    except Exception as e:
        print(f"Finance Planning Error: {e}")
        
    # Step 3: Generate Question (with calculation result and examples)
    
    # 根据模式调整提示词
    generation_mode = config['configurable'].get('generation_mode', '灵活')
    if generation_mode == "严谨":
        mode_instructions = """
# 出题模式：严谨模式（用于标准化考试）
要求：
1. **严格忠实原文**：严格按照参考材料，不得添加材料外的信息。
2. **标准化表述**：使用标准考试题目表述，避免场景化包装。
3. **直接考察计算**：直接考察计算知识点，不添加假设性场景。
4. **标准化选项**：选项表述简洁、准确，符合标准化考试风格。干扰项设计利用**"相近的数字"**或**"错误的参照物"**。
5. **严谨的解析**：解析必须严格按照"1、教材原文 2、试题分析 3、结论"的结构。

禁止：
- 禁止添加假设性场景（如"客户咨询..."、"在交易中..."）
- 禁止使用口语化表达
"""
    else:  # 灵活模式
        mode_instructions = """
# 出题模式：灵活模式（适合日常练习）
要求：
1. **场景化表达**：将题目融入实际工作场景，增强实用性。
2. **灵活表述**：可以使用更自然、更贴近实际工作的表述。
3. **创意干扰项**：错误选项可以更灵活，利用常见误区。利用**"相近的数字"**或**"错误的参照物"**设计干扰项。
4. **生动解析**：解析可以更生动，但必须保持准确性。
"""
    
    prompt_gen = f"""
# 角色
你是金融专家 (FinanceAgent)。
请基于【参考材料】创作一道高质量的单项选择题。
当前知识点的掌握程度要求为: 【{mastery}】。

{mode_instructions}

# 计算上下文
使用的工具: {tool_used}
工具参数: {plan.get('params', {}) if plan else {}}
计算结果: {calc_result}

**重要提示：理解计算步骤**
- 计算器可能只是解决整个问题的一个步骤，而不是整个问题
- 如果题目问的是最终结果，可能需要多步计算：
  ① 计算器结果（如：房龄）
  ② 基于计算器结果进一步计算（如：贷款年限 = 50 - 房龄）
  ③ 可能还需要考虑其他因素（如：借款人年龄），取最小值
  
**生成题目时的要求：**
- 如果计算器结果就是最终答案：直接使用计算结果作为正确答案
- 如果计算器结果只是中间步骤：需要在题干中提供完整信息，让答题者能够完成所有计算步骤
- 在解析中必须说明完整的计算过程，包括：
  ① 第一步：使用计算器计算什么（如：房龄 = 50 - (2025-1993) = 18年）
  ② 第二步：基于第一步结果计算什么（如：贷款年限上限 = 50 - 18 = 32年）
  ③ 第三步：考虑其他因素（如：借款人年龄限制），取最小值
  ④ 最终答案

(如果结果不为 None，你**必须**使用该计算结果，但需要理解它可能是中间步骤还是最终答案。{'构建标准化题目场景以匹配使用的参数。' if generation_mode == '严谨' else '构建题目场景以匹配使用的参数。'})

# 质量标准 (必须达成):
1. **准确性 (40%)**: 100% 事实准确。如果有计算结果 {calc_result}，必须使用。
2. **干扰项质量 (25%)**: 错误选项必须似是而非。
   - **干扰项设计技巧**：利用**"相近的数字"**（如正确答案是某个数值，干扰项用相近的数值，如正确答案是30万元，干扰项用25万元或35万元）或**"错误的参照物"**（如混淆不同概念、用类似但不正确的表述，如混淆"评估价"和"成交价"）
3. **相关性 (15%)**: 考察核心概念。
4. **格式 (10%)**: 严格的 JSON 输出。

# 参考材料
{kb_chunk['核心内容']}

# 范例 (请模仿以下题目的出题风格)
"""
    for i, ex in enumerate(examples, 1):
        prompt_gen += f"例 {i}: {ex['题干']}\n"

    prompt_gen += """
# 任务
返回 JSON: {{"question": "...", "options": ["A", "B", "C", "D"], "answer": "A/B/C/D", "explanation": "..."}}
约束: 题干中**禁止**出现“根据材料”或“依据参考资料”。
"""
    content = generate_content(
        config['configurable'].get('model'), 
        prompt_gen, 
        config['configurable'].get('api_key'),
        config['configurable'].get('base_url')
    )
    
    try:
        draft = parse_json_from_response(content)
        
        log_msg = f"👨‍💻 金融专家: 初稿已生成"
        if calc_result is not None:
            log_msg += f" (已调用 {tool_used}, 结果={calc_result})"
            
        return {
            "draft": draft,
            "tool_usage": {
                "tool": tool_used,
                "params": plan.get("params", {}),
                "result": calc_result
            },
            "examples": examples,  # Pass examples to UI
            "logs": [log_msg]
        }
    except Exception as e:
        return {"logs": [f"❌ 金融专家错误: {str(e)} \nContent: {content}"]}

# --- Graph Construction ---
workflow = StateGraph(AgentState)

workflow.add_node("router", router_node)
workflow.add_node("specialist", specialist_node)
workflow.add_node("finance", finance_node) # New Node
workflow.add_node("writer", writer_node)
workflow.add_node("critic", critic_node)
workflow.add_node("fixer", fixer_node)

workflow.set_entry_point("router")

# Conditional Edge for Router
def route_agent(state):
    if state['agent_name'] == "FinanceAgent":
        return "finance"
    else:
        return "specialist"

workflow.add_conditional_edges(
    "router",
    route_agent,
    {
        "finance": "finance",
        "specialist": "specialist"
    }
)

workflow.add_edge("specialist", "writer")
workflow.add_edge("finance", "writer") # Finance also goes to Writer
workflow.add_edge("writer", "critic")

# Critic 的智能决策：支持多路径
workflow.add_conditional_edges(
    "critic",
    critical_decision,
    {
        "pass": END,              # 通过 → 结束
        "fix": "fixer",          # 轻微问题 → Fixer 修复
        "reroute": "router",     # ✅ 严重问题 → 回到 Router 重新路由
        "self_heal": END          # 超限自愈 → 结束
    }
)

# Fixer 修复后回到 Critic 验证
workflow.add_edge("fixer", "critic")  # ✅ Fixer → Critic 循环

app = workflow.compile()
