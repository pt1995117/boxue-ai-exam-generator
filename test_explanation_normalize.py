"""
Test explanation normalization (1、2、3、 三段式) in _ensure_draft_v1.
"""
import sys
sys.path.insert(0, ".")

from exam_graph import _ensure_draft_v1, normalize_explanation_three_stage, _writer_normalize_phase


def _has_numbered_three_stage(explanation: str) -> bool:
    """Check that explanation contains 1、2、3、 (or 1. 2. 3.) segment markers."""
    s = explanation or ""
    has1 = "1、" in s or (s.strip().startswith("1.") or "1. 教材原文" in s[:30])
    has2 = "2、" in s or "2. 试题分析" in s
    has3 = "3、" in s or "3. 结论" in s
    return has1 and has2 and has3


def test_bracket_style():
    """【教材原文】【试题分析】【本题答案为】→ 1、2、3、"""
    raw = "【教材原文】\n(第一篇行业与贝壳>第一节房地产行业,了解) 大城市房价高。\n【试题分析】\n选项B与教材不符。\n【本题答案为A。】"
    d = _ensure_draft_v1({"question": "q", "options": ["A", "B"], "answer": "A", "explanation": raw})
    exp = d.get("explanation", "")
    assert _has_numbered_three_stage(exp), f"Expected 1、2、3、 in: {exp!r}"
    assert "1、教材原文：" in exp
    assert "2、试题分析：" in exp
    assert "3、" in exp and "结论" in exp
    print("OK test_bracket_style:", exp[:120], "...")


def test_plain_headers():
    """教材原文： / 试题分析： / 结论： → 1、2、3、"""
    raw = "教材原文：\n(常见身份证明,掌握) 居民身份证等。\n试题分析：\n选项ACD正确；选项B错误。\n结论：\n本题答案为ACD。"
    d = _ensure_draft_v1({"question": "q", "options": ["A", "B", "C", "D"], "answer": "ACD", "explanation": raw})
    exp = d.get("explanation", "")
    assert _has_numbered_three_stage(exp), f"Expected 1、2、3、 in: {exp!r}"
    print("OK test_plain_headers:", exp[:120], "...")


def test_no_headers():
    """No segment headers → prepend 1、教材原文： and add 3、结论： before 本题答案为"""
    raw = "根据教材，一线城市二手房流通率更高。\n选项B错误。\n本题答案为A。"
    d = _ensure_draft_v1({"question": "q", "options": ["A", "B"], "answer": "A", "explanation": raw})
    exp = d.get("explanation", "")
    assert exp.strip().startswith("1、教材原文："), f"Expected leading 1、教材原文：: {exp[:80]!r}"
    assert "3、结论：" in exp and "本题答案为" in exp, f"Expected 3、结论： and 本题答案为: {exp!r}"
    print("OK test_no_headers:", exp[:120], "...")


def test_dot_style_unified():
    """1. 2. 3. style → unified to 1、2、3、"""
    raw = "1. 教材原文：\n(目标题,掌握) 内容。\n2. 试题分析：\n分析。\n3. 结论：本题答案为B。"
    d = _ensure_draft_v1({"question": "q", "options": ["A", "B"], "answer": "B", "explanation": raw})
    exp = d.get("explanation", "")
    assert "1、" in exp, f"Expected 1、 after unified: {exp!r}"
    assert "2、" in exp
    assert "3、" in exp
    print("OK test_dot_style_unified:", exp[:120], "...")


def test_jiaocai_yuanwen_with_level():
    """教材原文(了解) / 教材原文（掌握）→ 1、教材原文："""
    raw = "教材原文(了解)\n房地产市场发展与城镇化相关。\n试题分析：\nA正确。\n结论：本题答案为A。"
    out = normalize_explanation_three_stage(raw)
    assert out.startswith("1、教材原文："), f"Expected 1、教材原文： at start: {out[:80]!r}"
    assert "2、试题分析：" in out and "3、" in out
    print("OK test_jiaocai_yuanwen_with_level:", out[:100], "...")


def test_writer_normalize_phase_applies_123():
    """_writer_normalize_phase output must have 1、2、3、 in explanation."""
    draft = {
        "question": "关于规律，以下表述正确的是（　）。",
        "options": ["A", "B", "C", "D"],
        "answer": "A",
        "explanation": "教材原文(了解)\n大城市房价高。\n试题分析：选项B错。\n本题答案为A。",
    }
    ir = _writer_normalize_phase(draft, "单选题")
    exp = ir.get("explanation", "")
    assert "1、教材原文：" in exp, f"Writer phase should normalize to 1、: {exp[:120]!r}"
    assert "2、" in exp and "3、" in exp
    print("OK test_writer_normalize_phase_applies_123:", exp[:100], "...")


if __name__ == "__main__":
    test_bracket_style()
    test_plain_headers()
    test_no_headers()
    test_dot_style_unified()
    test_jiaocai_yuanwen_with_level()
    test_writer_normalize_phase_applies_123()
    print("\nAll explanation normalization tests passed.")
