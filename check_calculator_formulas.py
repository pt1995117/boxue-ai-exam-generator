#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查所有计算器公式是否正确
"""
from calculation_logic import RealEstateCalculator

print("=" * 80)
print("🔍 检查所有计算器公式")
print("=" * 80)

# 原文公式汇总表
formulas = {
    1: ("土地剩余使用年限", "该土地使用年限-（截止年份-土地出让获取年份）"),
    2: ("房龄", "截止年份-房屋竣工年份"),
    3: ("室内净高", "层高-楼板厚度"),
    4: ("建筑面积", "套内建筑面积+公摊面积"),
    5: ("套内建筑面积", "套内使用面积+套内墙体面积+套内阳台建筑面积"),
    6: ("得房率", "套内使用面积÷建筑面积×100%"),
    7: ("面积误差比", "（产权登记面积-合同约定面积）÷合同约定面积×100%"),
    8: ("价差率", "（挂牌价-成交价）÷成交价×100%，取绝对值"),
    9: ("容积率", "地上建筑总面积÷用地总面积"),
    10: ("绿地率", "各类绿地面积总和÷用地总面积×100%"),
    11: ("绿化率", "绿化覆盖面积总和÷用地总面积×100%"),
    12: ("建筑密度", "建筑基底面积总和÷用地总面积×100%"),
}

print("\n📋 原文公式汇总表：")
for num, (name, formula) in formulas.items():
    print(f"{num:2d}. {name:12s} = {formula}")

print("\n" + "=" * 80)
print("🧪 测试计算器函数")
print("=" * 80)

# 测试用例
test_cases = []

# 1. 土地剩余使用年限
print("\n[1] 测试土地剩余使用年限...")
try:
    result = RealEstateCalculator.calculate_land_remaining_years(70, 2025, 2000)
    expected = 70 - (2025 - 2000)  # 70 - 25 = 45
    print(f"   输入: 总年限=70, 当前年份=2025, 出让年份=2000")
    print(f"   计算: 70 - (2025 - 2000) = {result}")
    print(f"   预期: {expected}")
    if result == expected:
        print("   ✅ 正确")
    else:
        print(f"   ❌ 错误！应该是 {expected}")
        test_cases.append(("土地剩余使用年限", False))
except Exception as e:
    print(f"   ❌ 错误: {e}")
    test_cases.append(("土地剩余使用年限", False))

# 2. 房龄
print("\n[2] 测试房龄...")
try:
    # 测试用例1：原文例子 2025-2010=15
    result1 = RealEstateCalculator.calculate_house_age(2025, 2010)
    expected1 = 2025 - 2010  # 15
    print(f"   测试1: 当前年份=2025, 竣工年份=2010")
    print(f"   计算: 2025 - 2010 = {result1}")
    print(f"   预期: {expected1}")
    if result1 == expected1:
        print("   ✅ 正确")
    else:
        print(f"   ❌ 错误！应该是 {expected1}")
        test_cases.append(("房龄", False))
    
    # 测试用例2：原文例子 2025-1993=32
    result2 = RealEstateCalculator.calculate_house_age(2025, 1993)
    expected2 = 2025 - 1993  # 32
    print(f"   测试2: 当前年份=2025, 竣工年份=1993")
    print(f"   计算: 2025 - 1993 = {result2}")
    print(f"   预期: {expected2}")
    if result2 == expected2:
        print("   ✅ 正确")
    else:
        print(f"   ❌ 错误！应该是 {expected2}")
        test_cases.append(("房龄", False))
except Exception as e:
    print(f"   ❌ 错误: {e}")
    test_cases.append(("房龄", False))

# 3. 室内净高
print("\n[3] 测试室内净高...")
try:
    result = RealEstateCalculator.calculate_indoor_height(3.0, 0.2)
    expected = 3.0 - 0.2  # 2.8
    print(f"   输入: 层高=3.0, 楼板厚度=0.2")
    print(f"   计算: 3.0 - 0.2 = {result}")
    print(f"   预期: {expected}")
    if abs(result - expected) < 0.001:
        print("   ✅ 正确")
    else:
        print(f"   ❌ 错误！应该是 {expected}")
        test_cases.append(("室内净高", False))
except Exception as e:
    print(f"   ❌ 错误: {e}")
    test_cases.append(("室内净高", False))

# 4. 建筑面积
print("\n[4] 测试建筑面积...")
try:
    result = RealEstateCalculator.calculate_building_area(80, 20)
    expected = 80 + 20  # 100
    print(f"   输入: 套内面积=80, 公摊面积=20")
    print(f"   计算: 80 + 20 = {result}")
    print(f"   预期: {expected}")
    if result == expected:
        print("   ✅ 正确")
    else:
        print(f"   ❌ 错误！应该是 {expected}")
        test_cases.append(("建筑面积", False))
except Exception as e:
    print(f"   ❌ 错误: {e}")
    test_cases.append(("建筑面积", False))

# 5. 得房率
print("\n[5] 测试得房率...")
try:
    result = RealEstateCalculator.calculate_efficiency_rate(80, 100)
    expected = (80 / 100) * 100  # 80%
    print(f"   输入: 套内使用面积=80, 建筑面积=100")
    print(f"   计算: (80 / 100) * 100 = {result}%")
    print(f"   预期: {expected}%")
    if abs(result - expected) < 0.001:
        print("   ✅ 正确")
    else:
        print(f"   ❌ 错误！应该是 {expected}%")
        test_cases.append(("得房率", False))
except Exception as e:
    print(f"   ❌ 错误: {e}")
    test_cases.append(("得房率", False))

# 6. 面积误差比
print("\n[6] 测试面积误差比...")
try:
    result = RealEstateCalculator.calculate_area_error_ratio(105, 100)
    expected = (105 - 100) / 100 * 100  # 5%
    print(f"   输入: 产权登记面积=105, 合同约定面积=100")
    print(f"   计算: (105 - 100) / 100 * 100 = {result}%")
    print(f"   预期: {expected}%")
    if abs(result - expected) < 0.001:
        print("   ✅ 正确")
    else:
        print(f"   ❌ 错误！应该是 {expected}%")
        test_cases.append(("面积误差比", False))
except Exception as e:
    print(f"   ❌ 错误: {e}")
    test_cases.append(("面积误差比", False))

# 7. 价差率
print("\n[7] 测试价差率...")
try:
    result = RealEstateCalculator.calculate_price_diff_ratio(120, 100)
    expected = abs((120 - 100) / 100) * 100  # 20%
    print(f"   输入: 挂牌价=120, 成交价=100")
    print(f"   计算: abs((120 - 100) / 100) * 100 = {result}%")
    print(f"   预期: {expected}%")
    if abs(result - expected) < 0.001:
        print("   ✅ 正确")
    else:
        print(f"   ❌ 错误！应该是 {expected}%")
        test_cases.append(("价差率", False))
except Exception as e:
    print(f"   ❌ 错误: {e}")
    test_cases.append(("价差率", False))

# 8. 容积率
print("\n[8] 测试容积率...")
try:
    result = RealEstateCalculator.calculate_plot_ratio(10000, 5000)
    expected = 10000 / 5000  # 2.0
    print(f"   输入: 地上建筑总面积=10000, 用地总面积=5000")
    print(f"   计算: 10000 / 5000 = {result}")
    print(f"   预期: {expected}")
    if abs(result - expected) < 0.001:
        print("   ✅ 正确")
    else:
        print(f"   ❌ 错误！应该是 {expected}")
        test_cases.append(("容积率", False))
except Exception as e:
    print(f"   ❌ 错误: {e}")
    test_cases.append(("容积率", False))

# 9. 绿地率
print("\n[9] 测试绿地率...")
try:
    result = RealEstateCalculator.calculate_green_rate(1500, 5000)
    expected = (1500 / 5000) * 100  # 30%
    print(f"   输入: 各类绿地面积=1500, 用地总面积=5000")
    print(f"   计算: (1500 / 5000) * 100 = {result}%")
    print(f"   预期: {expected}%")
    if abs(result - expected) < 0.001:
        print("   ✅ 正确")
    else:
        print(f"   ❌ 错误！应该是 {expected}%")
        test_cases.append(("绿地率", False))
except Exception as e:
    print(f"   ❌ 错误: {e}")
    test_cases.append(("绿地率", False))

print("\n" + "=" * 80)
if test_cases:
    print("❌ 发现以下问题：")
    for name, status in test_cases:
        if not status:
            print(f"   - {name}")
else:
    print("✅ 所有公式测试通过！")

