from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.runnables import RunnableLambda

from src.llm import build_llm
from src.pipeline.graph import run_judge
from src.schemas.evaluation import QuestionInput


def _mock_llm_response(input_value):
    text = str(input_value)
    if "QUALITY_SCORE" in text or "quality_score" in text:
        return "<QUALITY_SCORE>8.8</QUALITY_SCORE>"
    if "solver_validation" in text or "Cognitive Auditor" in text:
        return '{"solver_validation": {"passed": true, "predicted_answer": "A", "reasoning_path": "可唯一推出", "ambiguity_found": false}, "semantic_drift": {"passed": true, "evidence_quotes": ["证据句"], "drift_issues": []}}'
    if "distractor_quality" in text or "Value Assessor" in text:
        return '{"distractor_quality": {"score": 4, "homogeneity_issues": [], "weakness_issues": []}, "pedagogical_value": {"cognitive_level": "应用", "estimated_pass_rate": 0.62, "teaching_issues": []}, "risk_assessment": {"risk_level": "LOW", "risk_issues": []}}'
    if "generate_possible_answers(context)" in text or "只输出纯 Python 代码" in text:
        return (
            "def generate_possible_answers(context):\n"
            "    total = 100.0\n"
            "    # 健壮的除零检查示例\n"
            "    divisor = 1.0\n"
            "    if divisor == 0:\n"
            "        correct = 0.0\n"
            "    else:\n"
            "        correct = total / divisor\n"
            "    return [\n"
            "        {'type': 'correct', 'value': 100.0},\n"
            "        {'type': 'error_used_wrong_tax_rate_3_percent', 'value': 90.0},\n"
            "        {'type': 'error_forgot_to_deduct_vat', 'value': 80.0},\n"
            "    ]\n"
        )
    return '{"passed": true, "issues": []}'


def _parse_list_like(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return []
        if txt.startswith("[") and txt.endswith("]"):
            try:
                arr = json.loads(txt)
                if isinstance(arr, list):
                    return [str(x).strip() for x in arr if str(x).strip()]
            except Exception:
                pass
        parts = re.split(r"[;\n；,，|]+", txt)
        return [p.strip() for p in parts if p.strip()]
    return []


def _normalize_input(row: dict) -> dict:
    if "correct_answer" not in row and "answer" in row:
        row["correct_answer"] = row["answer"]
    if "textbook_slice" not in row and "textbook_excerpt" in row:
        row["textbook_slice"] = row["textbook_excerpt"]
    if "related_slices" not in row:
        for alias in ("related_textbook_slices", "associated_slices", "关联切片"):
            if alias in row and isinstance(row[alias], list):
                row["related_slices"] = row[alias]
                break
    for src, targets in [
        ("assessment_type", ("题目类型标签", "assessment_type")),
        ("city_name", ("命题城市", "城市", "tenant_display_name")),
        ("reference_slices", ("参考切片", "参考切片原文", "reference_textbook_slices")),
        ("mother_question", ("母题", "母题题干", "关联母题", "parent_question")),
        ("examples", ("范例", "examples")),
        ("term_locks", ("锁词", "术语锁词", "term_locks")),
        ("mastery", ("掌握程度", "mastery")),
    ]:
        if src in row:
            continue
        for alias in targets:
            if alias in row:
                row[src] = row[alias]
                break
    row["related_slices"] = _parse_list_like(row.get("related_slices", []))
    row["reference_slices"] = _parse_list_like(row.get("reference_slices", []))
    row["term_locks"] = _parse_list_like(row.get("term_locks", []))
    if isinstance(row.get("examples"), str):
        txt = row.get("examples", "").strip()
        if txt.startswith("[") and txt.endswith("]"):
            try:
                parsed = json.loads(txt)
                if isinstance(parsed, list):
                    row["examples"] = parsed
            except Exception:
                row["examples"] = []
        else:
            row["examples"] = []
    return row


def load_items(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = [payload]
    for row in payload:
        yield QuestionInput(**_normalize_input(row))


def _progress(message: str) -> None:
    print(f"[judge_cli] {message}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Offline Judge CLI")
    parser.add_argument("input", type=Path, help="JSON file containing one question or a list")
    parser.add_argument("--output", type=Path, help="Output JSON path")
    parser.add_argument("--mock-llm", action="store_true", help="Run fully offline with built-in mock LLM")
    parser.add_argument("--provider", default="openai", choices=["openai", "anthropic", "ait"], help="LLM provider")
    parser.add_argument("--model", default=None, help="Model name")
    parser.add_argument("--temperature", type=float, default=0)
    args = parser.parse_args()

    load_dotenv()
    if args.mock_llm:
        llm = RunnableLambda(_mock_llm_response)
    else:
        llm = build_llm(provider=args.provider, model=args.model, temperature=args.temperature)
    items = list(load_items(args.input))
    _progress(
        f"loaded {len(items)} question(s), provider={'mock' if args.mock_llm else args.provider}, input={args.input}"
    )

    reports = []
    for idx, item in enumerate(items, start=1):
        started = time.perf_counter()
        _progress(f"start {idx}/{len(items)} question_id={item.question_id}")
        report = run_judge(item, llm).model_dump()
        reports.append(report)
        elapsed = time.perf_counter() - started
        _progress(
            f"done {idx}/{len(items)} question_id={item.question_id} decision={report.get('decision')} elapsed={elapsed:.1f}s"
        )

    out = json.dumps(reports[0] if len(reports) == 1 else reports, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(out, encoding="utf-8")
        _progress(f"wrote output to {args.output}")
    else:
        print(out)


if __name__ == "__main__":
    main()
