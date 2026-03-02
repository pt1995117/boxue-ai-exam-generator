#!/usr/bin/env python3
"""Fix numpy import issue"""
import sys
import os

print("=" * 60)
print("诊断 numpy 导入问题")
print("=" * 60)

# Check current directory
print(f"\n当前工作目录: {os.getcwd()}")

# Check if numpy folder exists in current directory
numpy_path = os.path.join(os.getcwd(), "numpy")
if os.path.exists(numpy_path):
    print(f"⚠️  发现本地 numpy 文件夹: {numpy_path}")
    print("   这会导致导入冲突！")
else:
    print("✅ 当前目录没有 numpy 文件夹")

# Check sys.path
print(f"\nPython 搜索路径 (sys.path):")
for i, path in enumerate(sys.path[:5], 1):
    print(f"   {i}. {path}")

# Try to find numpy
print(f"\n尝试导入 numpy...")
try:
    import numpy
    print(f"✅ numpy 导入成功")
    print(f"   位置: {numpy.__file__}")
    print(f"   版本: {numpy.__version__}")
except ImportError as e:
    print(f"❌ numpy 导入失败: {e}")
    print(f"\n解决方案:")
    print(f"1. 确保不在 numpy 源码目录中")
    print(f"2. 运行: pip install numpy")
    print(f"3. 或: conda install numpy")

# Check if we're in a problematic directory
cwd = os.getcwd()
if "numpy" in cwd.lower():
    print(f"\n⚠️  警告: 当前路径包含 'numpy'")
    print(f"   建议切换到其他目录再运行 Python")

print("\n" + "=" * 60)
