"""离线 Judge 主入口。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from src.llm import build_llm
from src.pipeline.graph import run_judge
from src.schemas.evaluation import JudgeReport, QuestionInput


def _progress(message: str) -> None:
    print(f"[src.main] {message}", file=sys.stderr, flush=True)


def judge_single(
    question: QuestionInput | dict,
    llm=None,
    *,
    skip_phase1: bool = False,
) -> JudgeReport:
    if isinstance(question, dict):
        question = QuestionInput(**question)
    if llm is None:
        load_dotenv()
        llm = build_llm(provider="openai")
    return run_judge(question, llm, skip_phase1=skip_phase1)


def judge_batch(
    questions: list[QuestionInput] | list[dict],
    llm=None,
    *,
    skip_phase1: bool = False,
) -> list[JudgeReport]:
    if llm is None:
        load_dotenv()
        llm = build_llm(provider="openai")
    total = len(questions)
    results: list[JudgeReport] = []
    for idx, q in enumerate(questions, start=1):
        question = q if isinstance(q, QuestionInput) else QuestionInput(**q)
        started = time.perf_counter()
        _progress(f"start {idx}/{total} question_id={question.question_id}")
        result = judge_single(question, llm, skip_phase1=skip_phase1)
        elapsed = time.perf_counter() - started
        _progress(f"done {idx}/{total} question_id={question.question_id} decision={result.decision} elapsed={elapsed:.1f}s")
        results.append(result)
    return results


def main():
    load_dotenv()

    if len(sys.argv) < 2:
        print("用法: python -m src.main <input.json> [output.json]")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    data = json.loads(input_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        _progress(f"loaded {len(data)} question(s) from {input_path}")
        results = [r.model_dump() for r in judge_batch(data)]
    else:
        _progress(f"loaded 1 question from {input_path}")
        started = time.perf_counter()
        results = judge_single(data).model_dump()
        elapsed = time.perf_counter() - started
        _progress(f"done 1/1 question_id={results.get('question_id')} decision={results.get('decision')} elapsed={elapsed:.1f}s")

    out = json.dumps(results, ensure_ascii=False, indent=2)
    if output_path:
        output_path.write_text(out, encoding="utf-8")
        print(f"已写入 {output_path}")
    else:
        print(out)


if __name__ == "__main__":
    main()
