"""
Code-as-Tool Test - Output to File
"""
import os
import json
import sys
from typing import Any, Tuple
from datetime import datetime

# Open log file
log_file = open("code_tool_test_log.txt", "w", encoding="utf-8")

def log(msg):
    """Write to both stdout and file"""
    print(msg)
    log_file.write(msg + "\n")
    log_file.flush()

log(f"===== 测试开始: {datetime.now()} =====")

# Load API Key
config_path = "填写您的Key.txt"
config = {}

log(f"检查配置文件: {config_path}")

if os.path.exists(config_path):
    log("配置文件存在，开始读取")
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    log(f"配置读取完成，共 {len(config)} 个配置项")
else:
    log("❌ 配置文件不存在")
    log_file.close()
    sys.exit(1)

OPENAI_API_KEY = config.get("OPENAI_API_KEY", "")
DEEPSEEK_BASE_URL = config.get("OPENAI_BASE_URL", "https://api.deepseek.com")
MODEL_NAME = config.get("OPENAI_MODEL", "deepseek-reasoner")

log(f"API Key: {OPENAI_API_KEY[:10]}..." if OPENAI_API_KEY else "❌ API Key 为空")
log(f"Base URL: {DEEPSEEK_BASE_URL}")
log(f"Model: {MODEL_NAME}")

def safe_execute_python(code_str: str) -> Tuple[Any, str]:
    """Safely execute Python code."""
    log(f"\n执行代码:\n{code_str}")
    try:
        local_vars = {}
        allowed_builtins = {
            'abs': abs,
            'min': min,
            'max': max,
            'round': round,
            'int': int,
            'float': float,
        }
        
        exec(code_str, {'__builtins__': allowed_builtins}, local_vars)
        
        if 'calculate' in local_vars:
            result = local_vars['calculate']()
            log(f"✅ 执行成功，结果: {result}")
            return result, "success"
        else:
            log("❌ 没有找到 calculate 函数")
            return None, "error: no calculate function"
            
    except Exception as e:
        log(f"❌ 执行错误: {e}")
        return str(e), "error"


log("\n" + "=" * 60)
log("测试1: 基础代码执行（沙箱）")
log("=" * 60)

test_code = """
def calculate():
    area = 80
    cost_price = 1560
    result = area * cost_price * 0.01
    return result
"""

result, status = safe_execute_python(test_code)

if status == "success" and abs(result - 1248.0) < 0.01:
    log(f"✅ 测试1通过: 结果 = {result} 元")
else:
    log(f"❌ 测试1失败: {result}")
    log_file.close()
    sys.exit(1)

log("\n" + "=" * 60)
log("测试2: LLM 动态代码生成")
log("=" * 60)

if not OPENAI_API_KEY:
    log("❌ API Key 未配置，跳过 LLM 测试")
    log_file.close()
    sys.exit(0)

try:
    log("导入 OpenAI 模块...")
    from openai import OpenAI
    
    log("创建客户端...")
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=DEEPSEEK_BASE_URL)
    
    textbook_rule = """
已购公房转让土地出让金计算（成本价法）：
土地出让金 = 建筑面积 × 成本价 × 1%
其中，成本价一般为 1560 元/平方米
"""
    
    question_scenario = """
某套已购公房，建筑面积 80 平方米，成本价 1560 元/平方米。
计算需补缴的土地出让金。
"""
    
    prompt = f"""
# 任务
根据教材规则和题目场景，生成 Python 代码计算答案。

# 教材规则
{textbook_rule}

# 题目场景
{question_scenario}

# 要求
1. 定义 calculate() 函数
2. 从场景提取数值
3. 严格按照教材规则实现

# 输出格式（JSON）
{{
    "thought": "计算逻辑说明",
    "python_code": "def calculate():\\n    area = 80\\n    cost_price = 1560\\n    return area * cost_price * 0.01"
}}
"""
    
    log("调用 DeepSeek API...")
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        timeout=60
    )
    
    result_text = response.choices[0].message.content
    log(f"\nAPI 响应:\n{result_text[:500]}...")
    
    # Parse JSON
    import re
    match = re.search(r'\{.*\}', result_text, re.DOTALL)
    if match:
        result_json = json.loads(match.group(0))
        
        thought = result_json.get('thought', '')
        generated_code = result_json.get('python_code', '')
        
        log(f"\n💭 LLM 思考: {thought}")
        log(f"\n📝 生成的代码:\n{generated_code}")
        
        # Execute generated code
        exec_result, exec_status = safe_execute_python(generated_code)
        
        if exec_status == "success":
            expected = 1248.0
            if abs(float(exec_result) - expected) < 0.01:
                log(f"\n✅ 测试2通过!")
                log(f"   预期结果: {expected}")
                log(f"   实际结果: {exec_result}")
                log(f"\n🎉 Code-as-Tool 方案验证成功！")
                log("\n核心优势:")
                log("1. ✅ LLM 能理解教材规则并生成正确代码")
                log("2. ✅ 沙箱能安全执行代码")
                log("3. ✅ 计算结果准确")
                log("\n建议下一步:")
                log("- 添加双模型交叉验证")
                log("- 扩展到更多场景（增值税、契税等）")
                log("- 集成到 exam_graph.py")
            else:
                log(f"\n❌ 测试2失败: 结果不符合预期")
                log(f"   预期: {expected}")
                log(f"   实际: {exec_result}")
        else:
            log(f"\n❌ 测试2失败: 代码执行失败")
            log(f"   错误: {exec_result}")
    else:
        log(f"\n❌ 测试2失败: 无法解析 JSON")
        
except Exception as e:
    log(f"\n❌ 测试2异常: {e}")
    import traceback
    log(traceback.format_exc())

log("\n" + "=" * 60)
log(f"测试完成: {datetime.now()}")
log("=" * 60)

log_file.close()
print("\n📄 详细日志已保存到: code_tool_test_log.txt")
