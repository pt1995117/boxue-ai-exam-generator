#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify core dependencies for current provider stack."""

import sys


def check_import(name: str) -> None:
    try:
        __import__(name)
        print(f"   ✅ {name} 已安装")
    except Exception as e:
        print(f"   ❌ {name} 导入失败: {e}")
        sys.exit(1)


print("=" * 60)
print("验证依赖安装")
print("=" * 60)

print("\n1. 测试 openai...")
check_import("openai")

print("\n2. 测试 volcenginesdkarkruntime...")
check_import("volcenginesdkarkruntime")

print("\n3. 测试 numpy...")
check_import("numpy")

print("\n4. 测试 pandas...")
check_import("pandas")

print("\n5. 测试项目核心模块...")
try:
    from exam_factory import CRITIC_API_KEY, CRITIC_MODEL, CRITIC_PROVIDER

    print("   ✅ exam_factory 导入成功")
    print(f"   CRITIC_API_KEY: {CRITIC_API_KEY[:20] if CRITIC_API_KEY else 'None'}...")
    print(f"   CRITIC_MODEL: {CRITIC_MODEL}")
    print(f"   CRITIC_PROVIDER: {CRITIC_PROVIDER}")
except Exception as e:
    print(f"   ❌ exam_factory 导入失败: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("✅ 所有依赖验证完成")
print("=" * 60)
