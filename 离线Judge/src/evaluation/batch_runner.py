from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.runnables import RunnableLambda

from src.llm import build_llm
from src.pipeline.graph import run_judge
from src.schemas.evaluation import Decision, QuestionInput


@dataclass
class GoldenRecord:
    item: QuestionInput
    expected_decision: Decision | None = None


def _mock_llm_response(input_value):
    text = str(input_value)
    if "solver_validation" in text or "Cognitive Auditor" in text:
        return '{"solver_validation": {"passed": true, "predicted_answer": "A", "reasoning_path": "可唯一推出", "ambiguity_found": false}, "semantic_drift": {"passed": true, "evidence_quotes": ["证据句"], "drift_issues": []}}'
    if "distractor_quality" in text or "Value Assessor" in text:
        return '{"distractor_quality": {"score": 4, "homogeneity_issues": [], "weakness_issues": []}, "pedagogical_value": {"cognitive_level": "应用", "estimated_pass_rate": 0.62, "teaching_issues": []}, "risk_assessment": {"risk_level": "LOW", "risk_issues": []}}'
    if "generate_possible_answers(context)" in text or "只输出纯 Python 代码" in text:
        return (
            "def generate_possible_answers(context):\n"
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


def _normalize(row: dict) -> dict:
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


def load_golden(path: Path) -> list[GoldenRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Golden dataset 必须是 JSON 数组")

    out: list[GoldenRecord] = []
    for row in payload:
        expected_raw = row.pop("expected_decision", None)
        item = QuestionInput(**_normalize(row))
        expected = None
        if expected_raw:
            norm = str(expected_raw).lower()
            if norm in {"pass", "review", "reject"}:
                expected = Decision(norm)
            elif norm in {"passes", "passed"}:
                expected = Decision.PASS
            elif norm in {"needs_minor_fix", "reviewed"}:
                expected = Decision.REVIEW
            else:
                expected = Decision.REJECT
        out.append(GoldenRecord(item=item, expected_decision=expected))
    return out


def _progress(message: str) -> None:
    print(f"[batch_runner] {message}", file=sys.stderr, flush=True)


def evaluate_golden(records: list[GoldenRecord], llm) -> dict:
    reports = []
    correct = 0
    with_expected = 0

    tp = tn = fp = fn = 0
    total = len(records)
    for idx, rec in enumerate(records, start=1):
        started = time.perf_counter()
        _progress(f"start {idx}/{total} question_id={rec.item.question_id}")
        report = run_judge(rec.item, llm)
        pred = report.decision
        reports.append(report.model_dump())
        elapsed = time.perf_counter() - started
        _progress(f"done {idx}/{total} question_id={rec.item.question_id} decision={pred} elapsed={elapsed:.1f}s")

        if rec.expected_decision is not None:
            with_expected += 1
            if pred == rec.expected_decision:
                correct += 1

            pred_pass = pred == Decision.PASS
            exp_pass = rec.expected_decision == Decision.PASS
            if pred_pass and exp_pass:
                tp += 1
            elif (not pred_pass) and (not exp_pass):
                tn += 1
            elif pred_pass and (not exp_pass):
                fp += 1
            else:
                fn += 1

    metrics = {
        "total": len(records),
        "labeled": with_expected,
        "accuracy": round(correct / with_expected, 4) if with_expected else None,
        "pass_confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        "false_accept_rate": round(fp / (fp + tn), 4) if (fp + tn) else None,
        "false_reject_rate": round(fn / (fn + tp), 4) if (fn + tp) else None,
    }

    return {"metrics": metrics, "reports": reports}


def main():
    parser = argparse.ArgumentParser(description="Run golden dataset regression for Offline Judge")
    parser.add_argument("input", type=Path, help="golden dataset json")
    parser.add_argument("--output-dir", type=Path, default=Path("./outputs"))
    parser.add_argument("--mock-llm", action="store_true")
    parser.add_argument("--provider", choices=["openai", "anthropic", "ait"], default="openai")
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=0)
    args = parser.parse_args()

    load_dotenv()
    records = load_golden(args.input)
    if args.mock_llm:
        llm = RunnableLambda(_mock_llm_response)
    else:
        llm = build_llm(provider=args.provider, model=args.model, temperature=args.temperature)
    _progress(
        f"loaded {len(records)} record(s), provider={'mock' if args.mock_llm else args.provider}, input={args.input}"
    )

    result = evaluate_golden(records, llm)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    (args.output_dir / "reports.json").write_text(
        json.dumps(result["reports"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / "metrics.json").write_text(
        json.dumps(result["metrics"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _progress(f"wrote outputs to {args.output_dir}")

    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
