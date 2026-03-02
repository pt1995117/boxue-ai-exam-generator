"""
Simplified Code-as-Tool Test with Debugging
"""
import sys
print("===== 测试开始 =====", flush=True)
sys.stdout.flush()

import os
import json
from typing import Any, Tuple

print("导入模块成功", flush=True)

# Load API Key
config_path = "填写您的Key.txt"
config = {}

print(f"检查配置文件: {config_path}", flush=True)

if os.path.exists(config_path):
    print("配置文件存在，开始读取", flush=True)
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    print(f"配置读取完成，共 {len(config)} 个配置项", flush=True)
else:
    print("❌ 配置文件不存在", flush=True)
    sys.exit(1)

OPENAI_API_KEY = config.get("OPENAI_API_KEY", "")
DEEPSEEK_BASE_URL = config.get("OPENAI_BASE_URL", "https://openapi-ait.ke.com")
MODEL_NAME = config.get("OPENAI_MODEL", "deepseek-reasoner")

print(f"API Key: {OPENAI_API_KEY[:10]}..." if OPENAI_API_KEY else "❌ API Key 为空", flush=True)
print(f"Base URL: {DEEPSEEK_BASE_URL}", flush=True)
print(f"Model: {MODEL_NAME}", flush=True)

def safe_execute_python(code_str: str) -> Tuple[Any, str]:
    """Safely execute Python code."""
    print(f"\n执行代码:\n{code_str}", flush=True)
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
            print(f"✅ 执行成功，结果: {result}", flush=True)
            return result, "success"
        else:
            print("❌ 没有找到 calculate 函数", flush=True)
            return None, "error: no calculate function"
            
    except Exception as e:
        print(f"❌ 执行错误: {e}", flush=True)
        return str(e), "error"


def test_basic_execution():
    """Test basic code execution."""
    print("\n" + "=" * 60, flush=True)
    print("测试1: 基础代码执行", flush=True)
    print("=" * 60, flush=True)
    
    test_code = """
def calculate():
    area = 80
    cost_price = 1560
    result = area * cost_price * 0.01
    return result
"""
    
    result, status = safe_execute_python(test_code)
    
    if status == "success":
        print(f"✅ 测试1通过: {result}", flush=True)
        assert result == 1248.0, f"预期 1248.0, 实际 {result}"
    else:
        print(f"❌ 测试1失败: {result}", flush=True)
        return False
    
    return True


def test_llm_generation():
    """Test LLM code generation."""
    print("\n" + "=" * 60, flush=True)
    print("测试2: LLM 代码生成", flush=True)
    print("=" * 60, flush=True)
    
    if not OPENAI_API_KEY:
        print("❌ API Key 未配置，跳过测试2", flush=True)
        return False
    
    try:
        print("导入 OpenAI 模块...", flush=True)
        from openai import OpenAI
        
        print("创建 OpenAI 客户端...", flush=True)
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=DEEPSEEK_BASE_URL)
        
        prompt = """
请生成一个 Python 函数 calculate()，计算：
建筑面积 80 平方米，成本价 1560 元/平方米，土地出让金 = 面积 × 成本价 × 1%

返回 JSON:
{
    "python_code": "def calculate():\\n    area = 80\\n    cost_price = 1560\\n    return area * cost_price * 0.01"
}
"""
        
        print("调用 API...", flush=True)
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            timeout=60
        )
        
        result_text = response.choices[0].message.content
        print(f"API 响应:\n{result_text}", flush=True)
        
        # Parse JSON
        import re
        match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if match:
            result_json = json.loads(match.group(0))
            generated_code = result_json.get('python_code', '')
            
            print(f"生成的代码:\n{generated_code}", flush=True)
            
            # Execute generated code
            exec_result, exec_status = safe_execute_python(generated_code)
            
            if exec_status == "success":
                print(f"✅ 测试2通过: LLM 生成的代码正确执行，结果 = {exec_result}", flush=True)
                return True
            else:
                print(f"❌ 测试2失败: 代码执行失败 - {exec_result}", flush=True)
                return False
        else:
            print(f"❌ 测试2失败: 无法解析 JSON", flush=True)
            return False
            
    except Exception as e:
        print(f"❌ 测试2异常: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("\n" + "=" * 60, flush=True)
    print("Code-as-Tool 简化测试")
    print("=" * 60, flush=True)
    
    # Test 1: Basic execution
    if not test_basic_execution():
        print("\n❌ 基础测试失败，终止", flush=True)
        sys.exit(1)
    
    # Test 2: LLM generation
    if test_llm_generation():
        print("\n✅ 所有测试通过！", flush=True)
        print("\n🎉 Code-as-Tool 方案可行！", flush=True)
        print("\n建议下一步：扩展到更多场景（增值税、契税等）", flush=True)
    else:
        print("\n⚠️  LLM 测试失败，请检查 API 配置", flush=True)
    
    print("\n" + "=" * 60, flush=True)
    print("测试完成", flush=True)
    print("=" * 60, flush=True)
