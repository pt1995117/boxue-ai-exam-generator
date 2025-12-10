#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全面检查所有计算器函数，对比原文公式和例题
"""
from calculation_logic import RealEstateCalculator

print("=" * 80)
print("🔍 全面检查所有计算器函数")
print("=" * 80)

issues = []

# 1. 检查增值税计算
print("\n[1] 检查增值税计算...")
print("   原文例题：400÷1.05×5.3%=20.19（万元）")
print("   代码税率：5.6% (0.056)")
print("   原文税率：5.3% (0.053)")
if abs(0.056 - 0.053) > 0.001:
    print("   ⚠️  税率不一致！原文是5.3%，代码是5.6%")
    issues.append("增值税税率：原文5.3%，代码5.6%")
else:
    print("   ✅ 税率正确")

# 测试增值税计算
test_vat = RealEstateCalculator.calculate_vat(400, 0, 1, False, True)
expected_vat = 400 / 1.05 * 0.053
print(f"   测试：400÷1.05×5.3% = {expected_vat:.2f}万元")
print(f"   代码：400÷1.05×5.6% = {test_vat:.2f}万元")
if abs(test_vat - expected_vat) > 0.1:
    print("   ❌ 计算结果不一致！")
    issues.append(f"增值税计算：预期{expected_vat:.2f}，实际{test_vat:.2f}")

# 2. 检查个人所得税（代码中没有，但原文有）
print("\n[2] 检查个人所得税...")
print("   原文例题：个人所得税计算")
print("   代码中：❌ 没有个人所得税计算函数")
if not hasattr(RealEstateCalculator, 'calculate_personal_income_tax'):
    print("   ⚠️  缺少个人所得税计算函数")
    issues.append("缺少个人所得税计算函数")

# 3. 检查贷款年限计算
print("\n[3] 检查贷款年限计算...")
print("   原文例题：")
print("   - 商业贷款：50-15=35年（房龄15年）")
print("   - 公积金贷款：50-(2025-1993)=18年")
print("   代码中：❌ 没有贷款年限计算函数")
if not hasattr(RealEstateCalculator, 'calculate_loan_years'):
    print("   ⚠️  缺少贷款年限计算函数（但可以通过房龄计算间接得到）")
    # 这个可能不是问题，因为可以通过房龄计算

# 4. 检查契税计算
print("\n[4] 检查契税计算...")
print("   原文例题：80×3%=2.4（万元）")
test_deed = RealEstateCalculator.calculate_deed_tax(80, 110, False, False, True)
expected_deed = 80 * 0.03
print(f"   测试：80×3% = {expected_deed}万元")
print(f"   代码：{test_deed}万元")
if abs(test_deed - expected_deed) < 0.01:
    print("   ✅ 契税计算正确")
else:
    print("   ❌ 计算结果不一致！")
    issues.append(f"契税计算：预期{expected_deed}，实际{test_deed}")

# 5. 检查公积金贷款额度计算
print("\n[5] 检查公积金贷款额度计算...")
print("   原文例题：75000×20×1.5=225万元")
test_provident = RealEstateCalculator.calculate_provident_fund_loan(75000, 0, 20, 1.5)
expected_provident = 75000 * 20 * 1.5
print(f"   测试：75000×20×1.5 = {expected_provident}万元")
print(f"   代码：{test_provident}万元")
if abs(test_provident - expected_provident) < 0.01:
    print("   ✅ 公积金贷款额度计算正确")
else:
    print("   ❌ 计算结果不一致！")
    issues.append(f"公积金贷款额度：预期{expected_provident}，实际{test_provident}")

# 6. 检查商业贷款金额计算
print("\n[6] 检查商业贷款金额计算...")
print("   原文例题：100×85%=85（万元）")
test_loan = RealEstateCalculator.calculate_loan_amount(100, 0.85)
expected_loan = 100 * 0.85
print(f"   测试：100×85% = {expected_loan}万元")
print(f"   代码：{test_loan}万元")
if abs(test_loan - expected_loan) < 0.01:
    print("   ✅ 商业贷款金额计算正确")
else:
    print("   ❌ 计算结果不一致！")
    issues.append(f"商业贷款金额：预期{expected_loan}，实际{test_loan}")

# 7. 检查土地出让金计算
print("\n[7] 检查土地出让金计算...")
print("   原文：按经适房管理住房土地出让金 = 较高值 × 3%")
test_land1 = RealEstateCalculator.calculate_land_grant_fee_managed_economical(100)
expected_land1 = 100 * 0.03
print(f"   测试：100×3% = {expected_land1}万元")
print(f"   代码：{test_land1}万元")
if abs(test_land1 - expected_land1) < 0.01:
    print("   ✅ 按经适房管理住房土地出让金计算正确")
else:
    print("   ❌ 计算结果不一致！")
    issues.append(f"按经适房管理住房土地出让金：预期{expected_land1}，实际{test_land1}")

print("\n" + "=" * 80)
if issues:
    print("⚠️  发现以下问题：")
    for i, issue in enumerate(issues, 1):
        print(f"   {i}. {issue}")
else:
    print("✅ 所有检查通过！")
print("=" * 80)

