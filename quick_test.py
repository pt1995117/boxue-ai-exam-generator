#!/usr/bin/env python3
import sys

print("测试核心依赖安装...")
try:
    import openai  # noqa: F401
    import volcenginesdkarkruntime  # noqa: F401
    print("✅ openai / volcenginesdkarkruntime 已安装")
    sys.exit(0)
except ImportError as e:
    print(f"❌ 依赖未安装: {e}")
    print("\n请运行以下命令安装:")
    print("  pip install -r requirements.txt")
    print("或")
    print("  python -m pip install -r requirements.txt")
    sys.exit(1)
