"""
复杂计算场景对比测试 - 代码生成 vs 硬编码
真正能测出差距的难题
"""
import json
import os
from calculation_logic import RealEstateCalculator

# Load API Key
config_path = "填写您的Key.txt"
API_KEY = ""
BASE_URL = "https://api.deepseek.com"

if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            if "OPENAI_API_KEY=" in line and "请将您的Key粘贴在这里" not in line:
                API_KEY = line.split("=", 1)[1].strip()
                break

if not API_KEY:
    print("❌ 未找到 API Key")
    exit(1)

from openai import OpenAI
from RestrictedPython import compile_restricted
from RestrictedPython.Guards import guarded_iter_unpack_sequence
import re

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

def safe_execute_python(code_str: str):
    """安全执行生成的代码"""
    try:
        safe_env = {
            "__builtins__": {
                "abs": abs, "min": min, "max": max, "round": round,
                "float": float, "int": int, "True": True, "False": False,
            },
            "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        }
        
        compile_result = compile_restricted(code_str, filename='<generated>', mode='exec')
        
        if hasattr(compile_result, 'errors') and compile_result.errors:
            return None, f"编译错误: {compile_result.errors}"
        
        byte_code = compile_result.code if hasattr(compile_result, 'code') else compile_result
        exec(byte_code, safe_env)
        
        if 'result' in safe_env:
            return safe_env['result'], "success"
        else:
            return None, "错误: 代码中未定义 result 变量"
    except Exception as e:
        return None, f"执行错误: {str(e)}"

def generate_code(kb_content: str, scenario: dict, function_name: str) -> dict:
    """调用 LLM 生成代码"""
    prompt = f"""你是一个Python代码生成器。根据教材规则生成{function_name}的计算代码。

教材规则：
{kb_content}

测试场景：
{json.dumps(scenario, ensure_ascii=False, indent=2)}

输出JSON格式（不要markdown代码块）：
{{
  "reasoning": "根据教材分析...",
  "formula": "数学公式",
  "python_code": "variable1 = ...\\nresult = ..."
}}

要求：
1. python_code 中必须定义 result 变量存储最终结果
2. 只能用基本运算（+、-、*、/）和 if-else
3. 不能用 import、函数定义、循环
4. 直接输出JSON，不要```标记
"""
    
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )
    
    llm_output = response.choices[0].message.content
    json_match = re.search(r'\{.*\}', llm_output, re.DOTALL)
    
    if json_match:
        return json.loads(json_match.group(0))
    else:
        raise ValueError(f"无法解析JSON: {llm_output[:200]}")

# ============================================================================
# 难题场景定义
# ============================================================================

COMPLEX_SCENARIOS = [
    {
        "name": "难题1：增值税 - 满2年非普通住宅（差额计算）",
        "kb_content": """
增值税计算规则：
1. 住宅：
   - 满2年 + 普通住宅：免征
   - 满2年 + 非普通住宅：差额征收 = (网签价-原值)/1.05 × 5.3%
   - 不满2年：全额征收 = 网签价/1.05 × 5.3%
2. 非住宅：差额征收 = (网签价-原值)/1.05 × 5.3%

注意：/1.05 是将含税价转为不含税价
        """,
        "scenario": {
            "网签价": 630,  # 万元
            "原值": 420,
            "持有年限": 3,  # 满2年
            "是否普通住宅": False,  # 非普通（大于140平）
            "是否住宅": True,
        },
        "hardcoded_func": lambda: RealEstateCalculator.calculate_vat(
            price=630, original_price=420, years_held=3,
            is_ordinary=False, is_residential=True
        ),
        "expected_formula": "(630-420)/1.05 × 5.3%",
        "trap": "模型容易忘记 /1.05 这个除法，或者搞混差额/全额条件"
    },
    
    {
        "name": "难题2：公积金贷款 - 多步骤计算+取最小值",
        "kb_content": """
市属公积金贷款额度计算：
1. 公式：(借款人余额 + 共同申请人余额) × 倍数 × 缴存年限系数
2. 倍数：通常为20倍
3. 年限系数：缴存≥1年为1.5，<1年为1.0
4. 最终额度：取以下最小值
   - 公式计算结果
   - 最高额度限制（如120万）
   - 保底额度（如10万）

示例：借款人余额7.5万，无共同申请人，倍数20，年限系数1.5
计算：7.5×20×1.5=225万
如果最高限额120万，则取120万
        """,
        "scenario": {
            "借款人余额": 7.5,  # 万元
            "共同申请人余额": 0,
            "倍数": 20,
            "年限系数": 1.5,
            "最高额度": 120,  # 万元
        },
        "hardcoded_func": lambda: min(
            RealEstateCalculator.calculate_provident_fund_loan(
                balance_applicant=75000,  # 注意单位转换：万元→元
                balance_co_applicant=0,
                multiple=20,
                year_coefficient=1.5
            ) / 10000,  # 结果从元转回万元
            120  # 最高限额
        ),
        "expected_formula": "min((7.5+0)×20×1.5, 120) = 120",
        "trap": "模型可能只算公式，忘记取 min()；或者单位转换出错（万元 vs 元）"
    },
    
    {
        "name": "难题3：经济适用房土地出让金 - 时间节点判断",
        "kb_content": """
经济适用房土地出让金计算：
1. 购买时间在2008年4月11日之前：
   - 公式：网签价（或核定价取较高值）× 10%
2. 购买时间在2008年4月11日之后（含当日）：
   - 公式：(网签价-原购房价) × 70%

示例1：2007年购买，原值50万，现价100万
       → 100 × 10% = 10万（用现价，不管原值）

示例2：2010年购买，原值50万，现价100万
       → (100-50) × 70% = 35万（差额×70%）
        """,
        "scenario": {
            "网签价": 100,  # 万元
            "原购房价": 50,
            "购买年份": 2010,  # 2008年4月11日之后
        },
        "hardcoded_func": lambda: RealEstateCalculator.calculate_land_grant_fee_economical(
            price=100, original_price=50, buy_date_is_before_2008_4_11=False
        ),
        "expected_formula": "(100-50) × 70% = 35",
        "trap": "时间判断容易出错；2008年之前/之后公式完全不同"
    },
    
    {
        "name": "难题4：房龄+贷款年限 - 多步骤中间计算",
        "kb_content": """
贷款年限计算规则：
1. 先计算房龄（贷款专用公式）：
   房龄 = 50 - (当前年份 - 建成年代)
   
2. 再计算最长贷款年限：
   最长贷款年限 = 50 - 房龄
   
3. 还要考虑借款人年龄：
   借款人年龄 + 贷款年限 ≤ 65
   
4. 最终取最小值

示例：建成年代1993年，当前2025年，借款人40岁
步骤1：房龄 = 50 - (2025-1993) = 18年
步骤2：按房龄：50 - 18 = 32年
步骤3：按年龄：65 - 40 = 25年
步骤4：取 min(32, 25) = 25年
        """,
        "scenario": {
            "当前年份": 2025,
            "建成年代": 1993,
            "借款人年龄": 40,
        },
        "hardcoded_func": lambda: min(
            50 - RealEstateCalculator.calculate_house_age(2025, 1993, for_loan=True),
            65 - 40
        ),
        "expected_formula": "min(50-(50-(2025-1993)), 65-40) = min(32, 25) = 25",
        "trap": "多步骤计算，房龄是中间结果；容易漏掉借款人年龄约束"
    },
    
    {
        "name": "难题5：已购公房土地出让金 - 简单但易错的单位问题",
        "kb_content": """
已购公房土地出让金（成本价）：
公式：建筑面积 × 当年成本价 × 1%

当年成本价：城六区默认1560元/平方米

示例：建筑面积80平方米，成本价1560元/㎡
计算：80 × 1560 × 1% = 1248元

注意：结果单位是【元】，不是万元！
        """,
        "scenario": {
            "建筑面积": 80,  # 平方米
            "成本价": 1560,  # 元/平方米
        },
        "hardcoded_func": lambda: RealEstateCalculator.calculate_land_grant_fee_public_housing(
            area=80, cost_price=1560
        ),
        "expected_formula": "80 × 1560 × 1% = 1248元",
        "trap": "单位问题：结果是【元】不是万元；模型可能混淆单位"
    },
]

# ============================================================================
# 运行测试
# ============================================================================

print("=" * 80)
print("复杂计算场景对比测试 - 代码生成 vs 硬编码")
print("=" * 80)
print()

results = []

for i, test_case in enumerate(COMPLEX_SCENARIOS, 1):
    print(f"\n{'='*80}")
    print(f"【{test_case['name']}】")
    print(f"{'='*80}")
    
    print(f"\n陷阱: {test_case['trap']}")
    print(f"\n场景参数:")
    for key, value in test_case['scenario'].items():
        print(f"  {key}: {value}")
    
    # 1. 硬编码结果
    try:
        hardcoded_result = test_case['hardcoded_func']()
        print(f"\n✅ 硬编码结果: {hardcoded_result}")
    except Exception as e:
        print(f"\n❌ 硬编码执行失败: {e}")
        hardcoded_result = None
    
    # 2. 生成代码
    try:
        print("\n⏳ 调用 LLM 生成代码...")
        generated = generate_code(
            test_case['kb_content'],
            test_case['scenario'],
            test_case['name']
        )
        
        print(f"\n📝 模型推理: {generated['reasoning'][:80]}...")
        print(f"📐 模型公式: {generated['formula']}")
        print(f"\n💻 生成代码:")
        for line in generated['python_code'].split('\n'):
            print(f"    {line}")
        
        # 3. 执行代码
        code_result, status = safe_execute_python(generated['python_code'])
        
        if status == "success":
            print(f"\n✅ 代码执行结果: {code_result}")
            
            # 4. 对比
            if hardcoded_result is not None:
                diff = abs(hardcoded_result - code_result)
                match = diff < 0.01
                
                print(f"\n{'='*80}")
                print(f"对比结果:")
                print(f"  硬编码: {hardcoded_result}")
                print(f"  代码生成: {code_result}")
                print(f"  误差: {diff}")
                print(f"  {'✅ 通过' if match else '❌ 失败'}")
                print(f"{'='*80}")
                
                results.append({
                    "name": test_case['name'],
                    "hardcoded": hardcoded_result,
                    "generated": code_result,
                    "match": match,
                    "trap": test_case['trap'],
                    "reasoning": generated['reasoning'],
                    "code": generated['python_code'],
                })
            else:
                print("\n⚠️  无法对比（硬编码失败）")
        else:
            print(f"\n❌ 代码执行失败: {status}")
            results.append({
                "name": test_case['name'],
                "hardcoded": hardcoded_result,
                "generated": None,
                "match": False,
                "error": status,
                "trap": test_case['trap'],
            })
    
    except Exception as e:
        print(f"\n❌ 生成/执行失败: {str(e)[:200]}")
        results.append({
            "name": test_case['name'],
            "hardcoded": hardcoded_result,
            "generated": None,
            "match": False,
            "error": str(e),
            "trap": test_case['trap'],
        })

# 总结
print("\n\n" + "=" * 80)
print("测试总结")
print("=" * 80)

total = len(results)
matched = sum(1 for r in results if r.get('match', False))
accuracy = (matched / total * 100) if total > 0 else 0

print(f"\n总场景数: {total}")
print(f"通过数量: {matched}")
print(f"准确率: {accuracy:.1f}%")

if accuracy >= 90:
    print("\n🎉 代码生成方案表现优秀！可以考虑推广")
elif accuracy >= 70:
    print("\n⚠️  代码生成方案基本可用，但需要优化提示词")
elif accuracy >= 50:
    print("\n❌ 代码生成方案不稳定，建议保留硬编码")
else:
    print("\n🚫 代码生成方案失败率过高，不推荐使用")

print(f"\n详细结果已保存至: complex_test_results.json")

with open("complex_test_results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
