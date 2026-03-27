#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全面测试所有计算公式，对比原文例题
"""
from calculation_logic import RealEstateCalculator

print("=" * 80)
print("🔍 全面测试所有计算公式（对比原文例题）")
print("=" * 80)

issues = []
passed = []

# ========== 1. 已购公房土地出让金 ==========
print("\n[1] 测试已购公房土地出让金计算...")
print("   原文公式：建筑面积 × 当年成本价格 × 1%")
print("   原文例题：80 × 1560 × 1% = ?")
try:
    result = RealEstateCalculator.calculate_land_grant_fee_public_housing(80, 1560)
    expected = 80 * 1560 * 0.01
    print(f"   输入：area=80, cost_price=1560")
    print(f"   计算：80 × 1560 × 1% = {result}")
    print(f"   预期：{expected}")
    if abs(result - expected) < 0.01:
        print("   ✅ 正确")
        passed.append("已购公房土地出让金")
    else:
        print(f"   ❌ 错误！应该是 {expected}")
        issues.append(f"已购公房土地出让金：预期{expected}，实际{result}")
except Exception as e:
    print(f"   ❌ 错误：{e}")
    issues.append(f"已购公房土地出让金：{str(e)}")

# ========== 2. 商业贷款金额 ==========
print("\n[2] 测试商业贷款金额计算...")
print("   原文公式：贷款金额 = 评估价格 × 贷款成数")
print("   原文例题：100 × 85% = 85（万元）")
try:
    result = RealEstateCalculator.calculate_loan_amount(100, 0.85)
    expected = 100 * 0.85
    print(f"   输入：evaluation_price=100, loan_ratio=0.85")
    print(f"   计算：100 × 85% = {result}")
    print(f"   预期：{expected}")
    if abs(result - expected) < 0.01:
        print("   ✅ 正确")
        passed.append("商业贷款金额")
    else:
        print(f"   ❌ 错误！应该是 {expected}")
        issues.append(f"商业贷款金额：预期{expected}，实际{result}")
except Exception as e:
    print(f"   ❌ 错误：{e}")
    issues.append(f"商业贷款金额：{str(e)}")

# ========== 3. 公积金贷款额度 ==========
print("\n[3] 测试公积金贷款额度计算...")
print("   原文公式：(借款申请人余额 + 共同申请人余额) × 倍数 × 缴存年限系数")
print("   原文例题：75000 × 20 × 1.5 = 225（万元）")
try:
    result = RealEstateCalculator.calculate_provident_fund_loan(75000, 0, 20, 1.5)
    expected = 75000 * 20 * 1.5
    print(f"   输入：balance_applicant=75000, balance_co_applicant=0, multiple=20, year_coefficient=1.5")
    print(f"   计算：75000 × 20 × 1.5 = {result}")
    print(f"   预期：{expected}")
    if abs(result - expected) < 0.01:
        print("   ✅ 正确")
        passed.append("公积金贷款额度")
    else:
        print(f"   ❌ 错误！应该是 {expected}")
        issues.append(f"公积金贷款额度：预期{expected}，实际{result}")
except Exception as e:
    print(f"   ❌ 错误：{e}")
    issues.append(f"公积金贷款额度：{str(e)}")

# ========== 4. 增值税及附加 ==========
print("\n[4] 测试增值税及附加计算...")
print("   原文公式：400÷1.05×5.3%=20.19（万元）")
try:
    result = RealEstateCalculator.calculate_vat(400, 0, 1, False, True)
    expected = 400 / 1.05 * 0.053
    print(f"   输入：price=400, original_price=0, years_held=1, is_ordinary=False, is_residential=True")
    print(f"   计算：400÷1.05×5.3% = {result:.2f}")
    print(f"   预期：{expected:.2f}")
    if abs(result - expected) < 0.1:
        print("   ✅ 正确")
        passed.append("增值税及附加（全额）")
    else:
        print(f"   ❌ 错误！应该是 {expected:.2f}")
        issues.append(f"增值税及附加（全额）：预期{expected:.2f}，实际{result:.2f}")
except Exception as e:
    print(f"   ❌ 错误：{e}")
    issues.append(f"增值税及附加（全额）：{str(e)}")

# 测试差额征收
print("\n   测试差额征收：(630-420)÷1.05×5.3%=10.6（万元）")
try:
    result = RealEstateCalculator.calculate_vat(630, 420, 2, False, True)
    expected = (630 - 420) / 1.05 * 0.053
    print(f"   输入：price=630, original_price=420, years_held=2, is_ordinary=False")
    print(f"   计算：(630-420)÷1.05×5.3% = {result:.2f}")
    print(f"   预期：{expected:.2f}")
    if abs(result - expected) < 0.1:
        print("   ✅ 正确")
        passed.append("增值税及附加（差额）")
    else:
        print(f"   ❌ 错误！应该是 {expected:.2f}")
        issues.append(f"增值税及附加（差额）：预期{expected:.2f}，实际{result:.2f}")
except Exception as e:
    print(f"   ❌ 错误：{e}")
    issues.append(f"增值税及附加（差额）：{str(e)}")

# ========== 5. 契税 ==========
print("\n[5] 测试契税计算...")
print("   原文例题：80 × 3% = 2.4（万元）")
try:
    result = RealEstateCalculator.calculate_deed_tax(80, 110, False, False, True)
    expected = 80 * 0.03
    print(f"   输入：price=80, area=110, is_first_home=False, is_second_home=False (三套)")
    print(f"   计算：80 × 3% = {result}")
    print(f"   预期：{expected}")
    if abs(result - expected) < 0.01:
        print("   ✅ 正确")
        passed.append("契税（三套）")
    else:
        print(f"   ❌ 错误！应该是 {expected}")
        issues.append(f"契税（三套）：预期{expected}，实际{result}")
except Exception as e:
    print(f"   ❌ 错误：{e}")
    issues.append(f"契税：{str(e)}")

# ========== 6. 按经适房管理住房土地出让金 ==========
print("\n[6] 测试按经适房管理住房土地出让金...")
print("   原文公式：较高值 × 3%")
try:
    result = RealEstateCalculator.calculate_land_grant_fee_managed_economical(100)
    expected = 100 * 0.03
    print(f"   输入：price=100")
    print(f"   计算：100 × 3% = {result}")
    print(f"   预期：{expected}")
    if abs(result - expected) < 0.01:
        print("   ✅ 正确")
        passed.append("按经适房管理住房土地出让金")
    else:
        print(f"   ❌ 错误！应该是 {expected}")
        issues.append(f"按经适房管理住房土地出让金：预期{expected}，实际{result}")
except Exception as e:
    print(f"   ❌ 错误：{e}")
    issues.append(f"按经适房管理住房土地出让金：{str(e)}")

# ========== 7. 经济适用房土地出让金 ==========
print("\n[7] 测试经济适用房土地出让金...")
print("   原文公式：2008.4.11之前：较高值 × 10%；之后：(较高值-原值) × 70%")
try:
    # 测试2008.4.11之前
    result1 = RealEstateCalculator.calculate_land_grant_fee_economical(100, 50, True)
    expected1 = 100 * 0.10
    print(f"   测试1（2008.4.11之前）：price=100, original_price=50, before_2008=True")
    print(f"   计算：100 × 10% = {result1}")
    print(f"   预期：{expected1}")
    if abs(result1 - expected1) < 0.01:
        print("   ✅ 正确")
        passed.append("经济适用房土地出让金（2008.4.11之前）")
    else:
        print(f"   ❌ 错误！应该是 {expected1}")
        issues.append(f"经济适用房土地出让金（2008.4.11之前）：预期{expected1}，实际{result1}")
    
    # 测试2008.4.11之后
    result2 = RealEstateCalculator.calculate_land_grant_fee_economical(100, 50, False)
    expected2 = (100 - 50) * 0.70
    print(f"   测试2（2008.4.11之后）：price=100, original_price=50, before_2008=False")
    print(f"   计算：(100-50) × 70% = {result2}")
    print(f"   预期：{expected2}")
    if abs(result2 - expected2) < 0.01:
        print("   ✅ 正确")
        passed.append("经济适用房土地出让金（2008.4.11之后）")
    else:
        print(f"   ❌ 错误！应该是 {expected2}")
        issues.append(f"经济适用房土地出让金（2008.4.11之后）：预期{expected2}，实际{result2}")
except Exception as e:
    print(f"   ❌ 错误：{e}")
    issues.append(f"经济适用房土地出让金：{str(e)}")

# ========== 8. 房龄计算 ==========
print("\n[8] 测试房龄计算...")
print("   通用房龄：2025-2010=15年")
try:
    result1 = RealEstateCalculator.calculate_house_age(2025, 2010, False)
    expected1 = 2025 - 2010
    print(f"   测试1（通用房龄）：current_year=2025, completion_year=2010, for_loan=False")
    print(f"   计算：2025-2010 = {result1}")
    print(f"   预期：{expected1}")
    if result1 == expected1:
        print("   ✅ 正确")
        passed.append("房龄（通用）")
    else:
        print(f"   ❌ 错误！应该是 {expected1}")
        issues.append(f"房龄（通用）：预期{expected1}，实际{result1}")
    
    print("\n   贷款用房龄：50-(2025-1993)=18年")
    result2 = RealEstateCalculator.calculate_house_age(2025, 1993, True)
    expected2 = 50 - (2025 - 1993)
    print(f"   测试2（贷款用房龄）：current_year=2025, completion_year=1993, for_loan=True")
    print(f"   计算：50-(2025-1993) = {result2}")
    print(f"   预期：{expected2}")
    if result2 == expected2:
        print("   ✅ 正确")
        passed.append("房龄（贷款用）")
    else:
        print(f"   ❌ 错误！应该是 {expected2}")
        issues.append(f"房龄（贷款用）：预期{expected2}，实际{result2}")
except Exception as e:
    print(f"   ❌ 错误：{e}")
    issues.append(f"房龄：{str(e)}")

# ========== 9. 其他基础公式 ==========
print("\n[9] 测试其他基础公式...")

# 土地剩余使用年限
try:
    result = RealEstateCalculator.calculate_land_remaining_years(70, 2025, 2000)
    expected = 70 - (2025 - 2000)
    if result == expected:
        passed.append("土地剩余使用年限")
    else:
        issues.append(f"土地剩余使用年限：预期{expected}，实际{result}")
except Exception as e:
    issues.append(f"土地剩余使用年限：{str(e)}")

# 室内净高
try:
    result = RealEstateCalculator.calculate_indoor_height(3.0, 0.2)
    expected = 3.0 - 0.2
    if abs(result - expected) < 0.001:
        passed.append("室内净高")
    else:
        issues.append(f"室内净高：预期{expected}，实际{result}")
except Exception as e:
    issues.append(f"室内净高：{str(e)}")

# 建筑面积
try:
    result = RealEstateCalculator.calculate_building_area(80, 20)
    expected = 80 + 20
    if result == expected:
        passed.append("建筑面积")
    else:
        issues.append(f"建筑面积：预期{expected}，实际{result}")
except Exception as e:
    issues.append(f"建筑面积：{str(e)}")

# 得房率
try:
    result = RealEstateCalculator.calculate_efficiency_rate(80, 100)
    expected = (80 / 100) * 100
    if abs(result - expected) < 0.001:
        passed.append("得房率")
    else:
        issues.append(f"得房率：预期{expected}，实际{result}")
except Exception as e:
    issues.append(f"得房率：{str(e)}")

# 面积误差比
try:
    result = RealEstateCalculator.calculate_area_error_ratio(105, 100)
    expected = (105 - 100) / 100 * 100
    if abs(result - expected) < 0.001:
        passed.append("面积误差比")
    else:
        issues.append(f"面积误差比：预期{expected}，实际{result}")
except Exception as e:
    issues.append(f"面积误差比：{str(e)}")

# 价差率
try:
    result = RealEstateCalculator.calculate_price_diff_ratio(120, 100)
    expected = abs((120 - 100) / 100) * 100
    if abs(result - expected) < 0.001:
        passed.append("价差率")
    else:
        issues.append(f"价差率：预期{expected}，实际{result}")
except Exception as e:
    issues.append(f"价差率：{str(e)}")

# 容积率
try:
    result = RealEstateCalculator.calculate_plot_ratio(10000, 5000)
    expected = 10000 / 5000
    if abs(result - expected) < 0.001:
        passed.append("容积率")
    else:
        issues.append(f"容积率：预期{expected}，实际{result}")
except Exception as e:
    issues.append(f"容积率：{str(e)}")

# 绿地率
try:
    result = RealEstateCalculator.calculate_green_rate(1500, 5000)
    expected = (1500 / 5000) * 100
    if abs(result - expected) < 0.001:
        passed.append("绿地率")
    else:
        issues.append(f"绿地率：预期{expected}，实际{result}")
except Exception as e:
    issues.append(f"绿地率：{str(e)}")

# ========== 总结 ==========
print("\n" + "=" * 80)
print("📊 测试总结")
print("=" * 80)
print(f"✅ 通过：{len(passed)} 项")
print(f"❌ 问题：{len(issues)} 项")

if issues:
    print("\n⚠️  发现的问题：")
    for i, issue in enumerate(issues, 1):
        print(f"   {i}. {issue}")
else:
    print("\n🎉 所有测试通过！")

print("=" * 80)

