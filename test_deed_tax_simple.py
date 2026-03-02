"""
契税计算简化测试 - 输出到文件
"""
import json
import os
import sys
from calculation_logic import RealEstateCalculator

# Redirect all output to file
output_file = open("test_output.txt", "w", encoding="utf-8")
sys.stdout = output_file
sys.stderr = output_file

try:
    print("开始契税计算测试...")
    print("=" * 80)
    
    # Load API Key
    config_path = "填写您的Key.txt"
    API_KEY = ""
    BASE_URL = "https://openapi-ait.ke.com"
    
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            for line in f:
                if "OPENAI_API_KEY=" in line and "请将您的Key粘贴在这里" not in line:
                    API_KEY = line.split("=", 1)[1].strip()
                    break
    
    if not API_KEY:
        print("错误: 未找到 API Key")
        sys.exit(1)
    
    print(f"API Key 已加载: {API_KEY[:10]}...")
    print()
    
    # Import OpenAI after confirming API key
    from openai import OpenAI
    from RestrictedPython import compile_restricted
    from RestrictedPython.Guards import guarded_iter_unpack_sequence
    
    print("依赖库导入成功")
    print()
    
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    
    # Test scenario
    scenario = {
        "name": "首套小户型",
        "price": 100,  # 万元
        "area": 90,
        "is_first_home": True,
        "is_second_home": False,
        "is_residential": True,
    }
    
    print(f"测试场景: {scenario['name']}")
    print(f"  房价: {scenario['price']}万元")
    print(f"  面积: {scenario['area']}平方米")
    print()
    
    # 1. Hardcoded result
    hardcoded_result = RealEstateCalculator.calculate_deed_tax(
        price=scenario['price'],
        area=scenario['area'],
        is_first_home=scenario['is_first_home'],
        is_second_home=scenario['is_second_home'],
        is_residential=scenario['is_residential']
    )
    print(f"硬编码结果: {hardcoded_result}万元")
    print()
    
    # 2. Generate code via LLM
    kb_content = """
契税计算规则：
1. 住宅：
   - 首套房：面积≤140平，税率1%；面积>140平，税率1.5%
   - 二套房：面积≤140平，税率1%；面积>140平，税率2%
   - 三套及以上：税率3%
2. 非住宅：统一税率3%
    """
    
    prompt = f"""你是一个Python代码生成器。根据下面的规则生成契税计算代码。

教材规则：
{kb_content}

场景：
- 房价: {scenario['price']}万元
- 建筑面积: {scenario['area']}平方米
- 是否首套: {"是" if scenario['is_first_home'] else "否"}
- 是否二套: {"是" if scenario['is_second_home'] else "否"}

请输出JSON格式：
{{
  "reasoning": "根据教材，首套房90平≤140平，税率1%",
  "formula": "100 × 0.01 = 1.0",
  "python_code": "price = 100\\nrate = 0.01\\nresult = price * rate"
}}

只输出JSON，不要其他内容。
"""
    
    print("正在调用 DeepSeek 生成代码...")
    
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )
    
    llm_output = response.choices[0].message.content
    print("LLM 响应:")
    print(llm_output)
    print()
    
    # Parse JSON
    import re
    json_match = re.search(r'\{.*\}', llm_output, re.DOTALL)
    if json_match:
        generated_data = json.loads(json_match.group(0))
    else:
        print("错误: 无法解析 JSON")
        sys.exit(1)
    
    print("解析结果:")
    print(f"  推理: {generated_data['reasoning']}")
    print(f"  公式: {generated_data['formula']}")
    print(f"  代码:")
    for line in generated_data['python_code'].split('\\n'):
        print(f"    {line}")
    print()
    
    # Execute code safely
    print("执行生成的代码...")
    code_str = generated_data['python_code']
    
    safe_env = {
        "__builtins__": {
            "abs": abs,
            "min": min,
            "max": max,
            "round": round,
            "float": float,
            "int": int,
        },
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
    }
    
    byte_code = compile_restricted(
        code_str,
        filename='<generated>',
        mode='exec'
    )
    
    if byte_code.errors:
        print(f"编译错误: {byte_code.errors}")
        sys.exit(1)
    
    exec(byte_code, safe_env)
    
    if 'result' in safe_env:
        code_result = safe_env['result']
        print(f"代码执行结果: {code_result}万元")
    else:
        print("错误: 代码中未定义 result 变量")
        sys.exit(1)
    
    print()
    print("=" * 80)
    print("对比结果")
    print("=" * 80)
    print(f"硬编码: {hardcoded_result}万元")
    print(f"代码生成: {code_result}万元")
    
    diff = abs(hardcoded_result - code_result)
    if diff < 0.01:
        print(f"✅ 结果一致！(误差: {diff:.6f}万元)")
    else:
        print(f"❌ 结果不一致！(误差: {diff:.6f}万元)")
    
    # Save result
    result = {
        "scenario": scenario['name'],
        "hardcoded": hardcoded_result,
        "generated": code_result,
        "match": diff < 0.01,
        "reasoning": generated_data['reasoning'],
        "code": generated_data['python_code']
    }
    
    with open("deed_tax_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print()
    print("测试完成！结果已保存到 deed_tax_result.json")

except Exception as e:
    print(f"发生错误: {type(e).__name__}: {str(e)}")
    import traceback
    traceback.print_exc()

finally:
    output_file.close()
