"""
Code-as-Tool Pilot Test
测试动态代码生成方案的可行性
"""
import os
import json
from typing import Any, Tuple

# Load API Key
config_path = "填写您的Key.txt"
config = {}
if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()

OPENAI_API_KEY = config.get("OPENAI_API_KEY", "")
DEEPSEEK_BASE_URL = config.get("OPENAI_BASE_URL", "https://openapi-ait.ke.com")
MODEL_NAME = config.get("OPENAI_MODEL", "deepseek-reasoner")


def safe_execute_python(code_str: str) -> Tuple[Any, str]:
    """
    Safely execute Python code in a restricted environment.
    
    Args:
        code_str: Python code string containing a calculate() function
        
    Returns:
        (result, status): result from calculate() and 'success' or 'error'
    """
    try:
        # Create a restricted namespace
        local_vars = {}
        allowed_builtins = {
            'abs': abs,
            'min': min,
            'max': max,
            'round': round,
            'int': int,
            'float': float,
        }
        
        # Execute code with restricted builtins
        exec(code_str, {'__builtins__': allowed_builtins}, local_vars)
        
        # Call the required calculate() function
        if 'calculate' in local_vars:
            result = local_vars['calculate']()
            return result, "success"
        else:
            return None, "error: no calculate function"
            
    except Exception as e:
        return str(e), "error"


def generate_calculation_code(textbook_rule: str, question_scenario: str, api_key: str) -> dict:
    """
    Use LLM to generate Python calculation code based on textbook rules.
    
    Args:
        textbook_rule: The rule/formula from textbook
        question_scenario: The specific scenario with numbers
        api_key: DeepSeek/OpenAI API key
        
    Returns:
        dict with 'thought', 'python_code', 'expected_answer' fields
    """
    prompt = f"""
# 任务
你是一位金融计算专家。请根据【教材规则】和【题目场景】，生成Python代码来计算答案。

# 教材规则
{textbook_rule}

# 题目场景
{question_scenario}

# 代码生成要求
1. **定义 calculate() 函数**：必须包含一个 `calculate()` 函数并返回计算结果
2. **从场景提取数值**：将题目中的具体数值硬编码到函数内部
3. **遵循教材规则**：代码逻辑必须严格按照教材规则实现
4. **只使用基础运算**：+ - * / ** () 及 min/max/abs/round 等基础函数

# 输出格式
返回 JSON：
```json
{{
    "thought": "根据教材规则，需要先计算X，再计算Y...",
    "python_code": "def calculate():\\n    # 提取数值\\n    price = 1000000\\n    # 计算逻辑\\n    result = price * 0.01\\n    return result",
    "expected_answer": 10000
}}
```

严格按照 JSON 格式返回，不要有其他文字。
"""
    
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        result_text = response.choices[0].message.content
        
        # Parse JSON from response
        import re
        match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if match:
            result_json = json.loads(match.group(0))
            return result_json
        else:
            return {"error": "Failed to parse JSON", "raw": result_text}
            
    except Exception as e:
        return {"error": str(e)}


def independent_solver(textbook_rule: str, question_scenario: str, api_key: str) -> dict:
    """
    Independent solver: another model solves the problem independently for cross-validation.
    
    Args:
        textbook_rule: The rule/formula from textbook
        question_scenario: The specific scenario with numbers
        api_key: API key
        
    Returns:
        dict with 'python_code', 'result' fields
    """
    prompt = f"""
# 任务
你是一位独立审计员。请**独立解题**，不看任何答案，只根据教材规则编写 Python 代码来计算答案。

# 教材规则
{textbook_rule}

# 题目场景
{question_scenario}

# 代码生成要求
1. 定义 `calculate()` 函数
2. 从题目场景中提取具体数值
3. 严格按照教材规则实现计算逻辑

# 输出格式
返回 JSON：
```json
{{
    "python_code": "def calculate():\\n    ...",
    "reasoning": "我的计算逻辑是..."
}}
```
"""
    
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        result_text = response.choices[0].message.content
        
        import re
        match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        else:
            return {"error": "Failed to parse JSON"}
            
    except Exception as e:
        return {"error": str(e)}


def test_code_as_tool():
    """Test the Code-as-Tool approach with a sample calculation problem."""
    
    print("=" * 60)
    print("Code-as-Tool 试点测试")
    print("=" * 60)
    
    # Test Case 1: Public Housing Land Grant Fee (已购公房土地出让金)
    textbook_rule = """
已购公房转让土地出让金计算（成本价法）：
土地出让金 = 建筑面积 × 成本价 × 1%
其中，成本价一般为 1560 元/平方米（可根据实际政策调整）
"""
    
    question_scenario = """
某套已购公房，建筑面积 80 平方米，当年成本价为 1560 元/平方米。
请计算该房屋转让时需补缴的土地出让金。
"""
    
    print(f"\n【教材规则】\n{textbook_rule}")
    print(f"\n【题目场景】\n{question_scenario}")
    
    # Step 1: Generator creates code
    print("\n" + "─" * 60)
    print("步骤1: 生成端 - 生成计算代码")
    print("─" * 60)
    
    gen_result = generate_calculation_code(textbook_rule, question_scenario, OPENAI_API_KEY)
    
    if "error" in gen_result:
        print(f"❌ 生成失败: {gen_result['error']}")
        return
    
    print(f"\n💭 思考过程: {gen_result.get('thought', 'N/A')}")
    print(f"\n📝 生成代码:\n{gen_result.get('python_code', 'N/A')}")
    print(f"\n🎯 预期答案: {gen_result.get('expected_answer', 'N/A')}")
    
    # Step 2: Execute generated code
    print("\n" + "─" * 60)
    print("步骤2: 执行端 - 在沙箱中运行代码")
    print("─" * 60)
    
    gen_code = gen_result.get('python_code', '')
    gen_exec_result, gen_status = safe_execute_python(gen_code)
    
    if gen_status == "success":
        print(f"✅ 代码执行成功")
        print(f"📊 计算结果: {gen_exec_result}")
    else:
        print(f"❌ 代码执行失败: {gen_exec_result}")
        return
    
    # Step 3: Independent Solver (Critic)
    print("\n" + "─" * 60)
    print("步骤3: 审计端 - 独立解题验证")
    print("─" * 60)
    
    solver_result = independent_solver(textbook_rule, question_scenario, OPENAI_API_KEY)
    
    if "error" in solver_result:
        print(f"❌ 审计失败: {solver_result['error']}")
        return
    
    print(f"\n🔍 审计推理: {solver_result.get('reasoning', 'N/A')}")
    print(f"\n📝 审计代码:\n{solver_result.get('python_code', 'N/A')}")
    
    solver_code = solver_result.get('python_code', '')
    solver_exec_result, solver_status = safe_execute_python(solver_code)
    
    if solver_status == "success":
        print(f"✅ 审计代码执行成功")
        print(f"📊 审计结果: {solver_exec_result}")
    else:
        print(f"❌ 审计代码执行失败: {solver_exec_result}")
        return
    
    # Step 4: Cross-Validation
    print("\n" + "─" * 60)
    print("步骤4: 交叉验证 - 比对两个模型的结果")
    print("─" * 60)
    
    try:
        gen_val = float(gen_exec_result)
        solver_val = float(solver_exec_result)
        diff = abs(gen_val - solver_val)
        
        if diff < 0.01:  # Allow 0.01 tolerance for floating point
            print(f"✅ 验证通过！")
            print(f"   生成端结果: {gen_val}")
            print(f"   审计端结果: {solver_val}")
            print(f"   差异: {diff}")
            print(f"\n🎉 答案准确性: 99%+ (双模型一致)")
        else:
            print(f"⚠️  验证失败！")
            print(f"   生成端结果: {gen_val}")
            print(f"   审计端结果: {solver_val}")
            print(f"   差异: {diff}")
            print(f"\n❌ 需要人工审核或重新生成")
            
    except ValueError:
        print(f"❌ 无法比对：结果格式不一致")
        print(f"   生成端: {gen_exec_result}")
        print(f"   审计端: {solver_exec_result}")
    
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    test_code_as_tool()
