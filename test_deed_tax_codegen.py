"""
契税计算 - 代码生成 vs 硬编码对比测试
Safe code execution with RestrictedPython
"""
import json
import os
from typing import Dict, Any, Tuple
from RestrictedPython import compile_restricted, safe_globals
from RestrictedPython.Guards import guarded_iter_unpack_sequence
from calculation_logic import RealEstateCalculator
from pydantic import BaseModel

# Load API config
config_path = "填写您的Key.txt"
API_KEY = ""
BASE_URL = "https://api.deepseek.com"

if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            if "OPENAI_API_KEY=" in line:
                API_KEY = line.split("=", 1)[1].strip()
                break

# Schema for code generation output
class DeedTaxCalculation(BaseModel):
    reasoning: str  # 推理过程（如：首套房90平以下，税率1%）
    formula: str    # 数学公式（如：100万 × 1%）
    python_code: str  # 可执行的Python代码
    expected_result: float  # 预期结果

def safe_execute_python(code_str: str) -> Tuple[Any, str]:
    """
    Safe execution using RestrictedPython
    Only allows basic arithmetic and variable assignment
    """
    try:
        # Prepare safe environment
        safe_env = {
            "__builtins__": {
                "abs": abs,
                "min": min,
                "max": max,
                "round": round,
                "float": float,
                "int": int,
                "True": True,
                "False": False,
            },
            "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        }
        
        # Compile with restrictions
        compile_result = compile_restricted(
            code_str,
            filename='<generated>',
            mode='exec'
        )
        
        # Check if compilation was successful
        if hasattr(compile_result, 'errors') and compile_result.errors:
            return None, f"Compilation errors: {compile_result.errors}"
        
        # Get the actual bytecode
        if hasattr(compile_result, 'code'):
            byte_code = compile_result.code
        else:
            byte_code = compile_result
        
        # Execute in restricted environment
        exec(byte_code, safe_env)
        
        # Require a 'result' variable in the generated code
        if 'result' in safe_env:
            return safe_env['result'], "success"
        else:
            return None, "error: no 'result' variable defined"
            
    except Exception as e:
        return None, f"execution_error: {str(e)}"

def generate_deed_tax_code(kb_content: str, scenario: Dict[str, Any]) -> str:
    """
    Call LLM to generate Python code for deed tax calculation
    """
    from openai import OpenAI
    
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    
    prompt = f"""你是一个Python代码生成器，专门根据教材规则生成契税计算代码。

# 教材规则
{kb_content}

# 测试场景
- 房价: {scenario['price']}万元
- 建筑面积: {scenario['area']}平方米
- 是否首套: {"是" if scenario['is_first_home'] else "否"}
- 是否二套: {"是" if scenario['is_second_home'] else "否"}
- 是否住宅: {"是" if scenario['is_residential'] else "否"}

# 任务
请根据教材规则，生成计算这个场景契税的Python代码。

# 输出要求（严格JSON格式）
{{
  "reasoning": "根据教材，首套房90平以下税率1%，因为...",
  "formula": "100万 × 1% = 1万",
  "python_code": "price = 100\\nrate = 0.01\\nresult = price * rate",
  "expected_result": 1.0
}}

# 代码规范
1. 代码中必须定义一个 `result` 变量存储最终结果
2. 只能使用基本运算（+、-、*、/）和 if-else
3. 不能使用 import、函数定义、循环
4. 变量名清晰（price、area、rate等）
5. 结果单位：万元

现在请输出JSON：
"""
    
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,  # 低温度保证稳定性
    )
    
    return response.choices[0].message.content

def parse_json_from_response(text: str) -> Dict:
    """Extract JSON from markdown code blocks or plain text"""
    import re
    
    # Try to find JSON in code blocks
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if json_match:
        return json.loads(json_match.group(1))
    
    # Try to find standalone JSON
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if json_match:
        return json.loads(json_match.group(0))
    
    raise ValueError(f"No valid JSON found in response: {text[:200]}")

def test_deed_tax_scenarios():
    """
    Test deed tax calculation with multiple scenarios
    Compare generated code vs hardcoded function
    """
    
    # 教材契税规则（简化版）
    kb_content = """
契税计算规则：
1. 住宅：
   - 首套房：面积≤140平，税率1%；面积>140平，税率1.5%
   - 二套房：面积≤140平，税率1%；面积>140平，税率2%
   - 三套及以上：税率3%
2. 非住宅：统一税率3%
    """
    
    # Test scenarios
    scenarios = [
        {
            "name": "首套小户型",
            "price": 100,  # 万元
            "area": 90,
            "is_first_home": True,
            "is_second_home": False,
            "is_residential": True,
        },
        {
            "name": "首套大户型",
            "price": 200,
            "area": 150,
            "is_first_home": True,
            "is_second_home": False,
            "is_residential": True,
        },
        {
            "name": "二套小户型",
            "price": 150,
            "area": 100,
            "is_first_home": False,
            "is_second_home": True,
            "is_residential": True,
        },
        {
            "name": "三套房",
            "price": 180,
            "area": 120,
            "is_first_home": False,
            "is_second_home": False,
            "is_residential": True,
        },
        {
            "name": "商业地产",
            "price": 500,
            "area": 200,
            "is_first_home": False,
            "is_second_home": False,
            "is_residential": False,
        },
    ]
    
    print("=" * 80)
    print("契税计算 - 代码生成 vs 硬编码对比测试")
    print("=" * 80)
    
    results = []
    
    for scenario in scenarios:
        print(f"\n【场景】{scenario['name']}")
        print(f"  房价: {scenario['price']}万  面积: {scenario['area']}平")
        
        # 1. 硬编码计算（Ground Truth）
        hardcoded_result = RealEstateCalculator.calculate_deed_tax(
            price=scenario['price'],
            area=scenario['area'],
            is_first_home=scenario['is_first_home'],
            is_second_home=scenario['is_second_home'],
            is_residential=scenario['is_residential']
        )
        print(f"  硬编码结果: {hardcoded_result}万元")
        
        # 2. 生成代码并执行
        try:
            llm_response = generate_deed_tax_code(kb_content, scenario)
            generated_data = parse_json_from_response(llm_response)
            
            print(f"  模型推理: {generated_data['reasoning'][:50]}...")
            print(f"  生成公式: {generated_data['formula']}")
            print(f"  生成代码:")
            for line in generated_data['python_code'].split('\n'):
                print(f"    {line}")
            
            # Execute generated code
            code_result, status = safe_execute_python(generated_data['python_code'])
            
            if status == "success":
                print(f"  代码结果: {code_result}万元")
                
                # Compare
                diff = abs(hardcoded_result - code_result)
                match = diff < 0.01  # 允许0.01万元（100元）的误差
                
                print(f"  【对比】{'✅ 一致' if match else '❌ 不一致'} (误差: {diff:.4f}万元)")
                
                results.append({
                    "scenario": scenario['name'],
                    "hardcoded": hardcoded_result,
                    "generated": code_result,
                    "match": match,
                    "reasoning": generated_data['reasoning']
                })
            else:
                print(f"  ❌ 代码执行失败: {status}")
                results.append({
                    "scenario": scenario['name'],
                    "hardcoded": hardcoded_result,
                    "generated": None,
                    "match": False,
                    "error": status
                })
                
        except Exception as e:
            print(f"  ❌ 生成失败: {str(e)}")
            results.append({
                "scenario": scenario['name'],
                "hardcoded": hardcoded_result,
                "generated": None,
                "match": False,
                "error": str(e)
            })
    
    # Summary
    print("\n" + "=" * 80)
    print("测试总结")
    print("=" * 80)
    
    total = len(results)
    matched = sum(1 for r in results if r.get('match', False))
    accuracy = (matched / total * 100) if total > 0 else 0
    
    print(f"总场景数: {total}")
    print(f"一致数量: {matched}")
    print(f"准确率: {accuracy:.1f}%")
    
    if matched == total:
        print("\n🎉 完美！所有场景的代码生成结果与硬编码一致！")
    elif accuracy >= 80:
        print(f"\n⚠️  准确率{accuracy:.1f}%，还需优化提示词或规则")
    else:
        print(f"\n❌ 准确率{accuracy:.1f}%偏低，需要重新设计方案")
    
    # Save detailed results
    with open("deed_tax_test_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"\n详细结果已保存至: deed_tax_test_results.json")
    
    return results

if __name__ == "__main__":
    if not API_KEY:
        print("❌ 请先在 填写您的Key.txt 中配置 OPENAI_API_KEY")
    else:
        test_deed_tax_scenarios()
