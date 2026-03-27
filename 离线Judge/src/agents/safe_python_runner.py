from __future__ import annotations

import ast
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

FORBIDDEN_IMPORTS = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "pathlib",
    "shutil",
    "resource",
    "multiprocessing",
    "threading",
    "ctypes",
}

FORBIDDEN_CALLS = {
    "eval",
    "exec",
    "compile",
    "open",
    "__import__",
    "input",
}


def static_check(code: str) -> list[str]:
    issues: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"代码语法错误: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    issues.append(f"禁止导入模块: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in FORBIDDEN_IMPORTS:
                issues.append(f"禁止导入模块: {node.module}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                issues.append(f"禁止调用函数: {node.func.id}")

    return issues


def execute_code(code: str, *, timeout_seconds: float = 2.5) -> dict[str, Any]:
    """Execute code in isolated subprocess.

    Contract:
    - user code should print a JSON object as the last line.
    - we inject a tiny wrapper that catches runtime errors.
    """
    static_issues = static_check(code)
    if static_issues:
        return {"ok": False, "issues": static_issues}

    wrapper = (
        "import json\n"
        "def __judge_emit(payload):\n"
        "    print(json.dumps(payload, ensure_ascii=False))\n"
        "try:\n"
        + "\n".join(f"    {line}" for line in code.splitlines())
        + "\nexcept Exception as e:\n"
        "    __judge_emit({'ok': False, 'error': str(e)})\n"
    )

    with tempfile.TemporaryDirectory(prefix="judge_code_") as td:
        p = Path(td) / "snippet.py"
        p.write_text(wrapper, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["python", "-I", str(p)],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "issues": [f"代码执行超时({timeout_seconds}s)"]}

    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "issues": [f"代码执行失败: {proc.stderr.strip() or proc.returncode}"]}

    lines = [x.strip() for x in proc.stdout.splitlines() if x.strip()]
    if not lines:
        return {"ok": False, "issues": ["代码未输出可解析结果(JSON)"]}

    last = lines[-1]
    try:
        data = json.loads(last)
    except json.JSONDecodeError:
        return {"ok": False, "issues": ["代码输出非 JSON，无法校验"]}

    return {"ok": True, "result": data}


def execute_generate_possible_answers(
    code: str,
    context: dict[str, Any],
    *,
    timeout_seconds: float = 2.5,
) -> dict[str, Any]:
    """Execute user code and call generate_possible_answers(context) in isolation."""
    static_issues = static_check(code)
    if static_issues:
        return {"ok": False, "issues": static_issues}

    wrapper = (
        "import json\n"
        f"CONTEXT = {json.dumps(context, ensure_ascii=False)}\n"
        "def __judge_emit(payload):\n"
        "    print(json.dumps(payload, ensure_ascii=False))\n"
        "try:\n"
        + "\n".join(f"    {line}" for line in code.splitlines())
        + "\n    fn = locals().get('generate_possible_answers')\n"
        "    if not callable(fn):\n"
        "        __judge_emit({'ok': False, 'error': '函数 generate_possible_answers(context) 未定义'})\n"
        "    else:\n"
        "        result = fn(CONTEXT)\n"
        "        __judge_emit({'ok': True, 'result': result})\n"
        "except Exception as e:\n"
        "    __judge_emit({'ok': False, 'error': str(e)})\n"
    )

    with tempfile.TemporaryDirectory(prefix="judge_code_") as td:
        p = Path(td) / "snippet.py"
        p.write_text(wrapper, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["python", "-I", str(p)],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "issues": [f"代码执行超时({timeout_seconds}s)"]}

    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "issues": [f"代码执行失败: {proc.stderr.strip() or proc.returncode}"]}

    lines = [x.strip() for x in proc.stdout.splitlines() if x.strip()]
    if not lines:
        return {"ok": False, "issues": ["代码未输出可解析结果(JSON)"]}

    last = lines[-1]
    try:
        data = json.loads(last)
    except json.JSONDecodeError:
        return {"ok": False, "issues": ["代码输出非 JSON，无法校验"]}

    if not data.get("ok", False):
        return {"ok": False, "issues": [str(data.get("error", "代码执行失败"))]}
    return {"ok": True, "result": data.get("result")}
