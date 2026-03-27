"""Layer1 blind solver deterministic fallback tests."""

from src.agents.layer1_blind_solver import _deterministic_solve_from_raw, _parse_solver_evaluation_text
from src.schemas.evaluation import QuestionInput


def test_deterministic_solver_recovers_true_false_from_natural_language():
    q = QuestionInput(
        question_id="Q-TF-001",
        stem="以上说法是否正确？（ ）",
        options=["A. 正确", "B. 错误"],
        correct_answer="B",
        explanation="3、结论：本题答案为B。",
        textbook_slice="教材",
        question_type="true_false",
    )
    answer, reasoning = _deterministic_solve_from_raw(
        q,
        "错误。\n\n依据教材，该说法不成立。",
    )
    assert answer == "B"
    assert "错误" in reasoning


def test_deterministic_solver_recovers_single_choice_from_explicit_letter():
    q = QuestionInput(
        question_id="Q-SC-001",
        stem="以下表述正确的是（ ）。",
        options=["A. 甲", "B. 乙", "C. 丙", "D. 丁"],
        correct_answer="C",
        explanation="3、结论：本题答案为C。",
        textbook_slice="教材",
        question_type="single_choice",
    )
    answer, _ = _deterministic_solve_from_raw(q, "本题答案为C。因为只有选项C符合教材。")
    assert answer == "C"


def test_deterministic_solver_accepts_multi_choice_alias():
    q = QuestionInput(
        question_id="Q-MC-001",
        stem="以下表述正确的有（ ）。",
        options=["A. 甲", "B. 乙", "C. 丙", "D. 丁"],
        correct_answer="ABC",
        explanation="3、结论：本题答案为ABC。",
        textbook_slice="教材",
        question_type="multi_choice",
    )
    answer, _ = _deterministic_solve_from_raw(q, "答案为ABC。")
    assert answer == "ABC"


def test_deterministic_solver_accepts_conclusion_protocol():
    q = QuestionInput(
        question_id="Q-SC-002",
        stem="以下表述正确的是（ ）。",
        options=["A. 甲", "B. 乙", "C. 丙", "D. 丁"],
        correct_answer="D",
        explanation="3、结论：本题答案为D。",
        textbook_slice="教材",
        question_type="single_choice",
    )
    answer, _ = _deterministic_solve_from_raw(q, "CONCLUSION=D")
    assert answer == "D"


def test_parse_solver_evaluation_text_with_plain_protocol():
    parsed = _parse_solver_evaluation_text(
        "SCORE=4\n"
        "PREDICTED_ANSWER=B\n"
        "REASONING_PATH=依据题干与教材，唯一可选 B。\n"
        "FATAL_LOGIC_ISSUES=无\n"
    )
    assert parsed["score"] == 4
    assert parsed["predicted_answer"] == "B"
    assert "唯一可选" in parsed["reasoning_path"]
    assert parsed["fatal_logic_issues"] == []


def test_parse_solver_evaluation_text_with_cn_keys_and_multiline():
    parsed = _parse_solver_evaluation_text(
        "评分：0\n"
        "答案：NONE\n"
        "推理链：题干条件不足，无法唯一确定。\n"
        "仍存在多解风险。\n"
        "致命逻辑缺陷：条件缺失；无法从原始输出中可靠抽取答案\n"
    )
    assert parsed["score"] == 0
    assert parsed["predicted_answer"] == "NONE"
    assert "多解风险" in parsed["reasoning_path"]
    assert len(parsed["fatal_logic_issues"]) >= 2
