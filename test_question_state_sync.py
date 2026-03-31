#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""State sync helpers should rebuild downstream review inputs from the latest final_json."""

import sys

import pytest

sys.path.insert(0, ".")

from exam_graph import (
    BLANK_BRACKET,
    enforce_question_bracket_and_punct,
    has_forbidden_symbol_before_ending_blank_bracket,
    repair_final_json_format,
    _sync_question_type_from_draft,
    _sync_downstream_state_from_final_json,
    validate_critic_format,
    validate_writer_format,
)
from admin_api import _merge_llm_trace_records

pytestmark = pytest.mark.release_gate


def test_sync_downstream_state_rebuilds_candidate_sentences_from_fixed_question():
    final_json = {
        "题干": "经纪人应当核验房源信息（　）。",
        "选项1": "后再发布",
        "选项2": "无需核验直接发布",
        "正确答案": "A",
        "解析": "1、教材原文：教材。\n2、试题分析：分析。\n3、结论：本题答案为A。",
    }
    state = _sync_downstream_state_from_final_json(final_json, "单选题")
    sentences = state.get("candidate_sentences") or []
    assert sentences, "candidate_sentences should be rebuilt from the latest fixed final_json"
    assert any("经纪人应当核验房源信息后再发布" in str(x.get("sentence", "")) for x in sentences)
    assert state.get("writer_validation_report", {}).get("passed") in (True, False)


def test_sync_question_type_from_draft_uses_latest_draft_shape():
    draft = {
        "question": "以下说法正确的有（　）。",
        "options": ["说法一", "说法二", "说法三", "说法四"],
        "answer": "AC",
        "explanation": "1、教材原文：教材。\n2、试题分析：分析。\n3、结论：本题答案为AC。",
    }
    state = _sync_question_type_from_draft(draft, "单选题")
    assert state["current_question_type"] == "单选题"


def test_sync_downstream_state_preserves_provided_question_type():
    final_json = {
        "题干": "以下说法正确的有（　）。",
        "选项1": "说法一",
        "选项2": "说法二",
        "选项3": "说法三",
        "选项4": "说法四",
        "正确答案": "AC",
        "解析": "1、教材原文：教材。\n2、试题分析：分析。\n3、结论：本题答案为AC。",
    }
    state = _sync_downstream_state_from_final_json(final_json, "单选题")
    assert state["current_question_type"] == "单选题"


def test_merge_llm_trace_records_preserves_history_without_duplicates():
    existing = [{"trace_id": "t1", "node": "router", "ts": "1", "model": "m1"}]
    incoming = [
        {"trace_id": "t1", "node": "router", "ts": "1", "model": "m1"},
        {"trace_id": "t2", "node": "writer", "ts": "2", "model": "m2"},
    ]
    merged = _merge_llm_trace_records(existing, incoming)
    assert len(merged) == 2
    assert [x["trace_id"] for x in merged] == ["t1", "t2"]


def test_enforce_question_bracket_and_punct_rewrites_noncanonical_tail():
    text = "经纪人应核验房源信息？（ ）"
    normalized = enforce_question_bracket_and_punct(text, "单选题")
    assert normalized == f"经纪人应核验房源信息{BLANK_BRACKET}。"
    assert not has_forbidden_symbol_before_ending_blank_bracket(normalized)


def test_enforce_judgment_question_rewrites_interrogative_tail_to_affirmative():
    text = "在贝壳省心租业务中，经纪人作为房源推荐人，其收房业绩的提报条件是否正确。（ ）"
    normalized = enforce_question_bracket_and_punct(text, "判断题")
    assert normalized == f"在贝壳省心租业务中，经纪人作为房源推荐人，其收房业绩的提报条件正确{BLANK_BRACKET}"


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        ("经纪人应核验房源信息？（ ）", f"经纪人应核验房源信息{BLANK_BRACKET}。"),
        ("经纪人应核验房源信息；（　）", f"经纪人应核验房源信息{BLANK_BRACKET}。"),
        ("经纪人应核验房源信息， （　）", f"经纪人应核验房源信息{BLANK_BRACKET}。"),
        ("经纪人应核验房源信息 ( )", f"经纪人应核验房源信息{BLANK_BRACKET}。"),
        ("经纪人应核验房源信息（　）", f"经纪人应核验房源信息{BLANK_BRACKET}。"),
        ("经纪人应核验房源信息。（　）", f"经纪人应核验房源信息{BLANK_BRACKET}。"),
        ("经纪人应核验房源信息（  ）", f"经纪人应核验房源信息{BLANK_BRACKET}。"),
        ("经纪人应核验房源信息（　　）", f"经纪人应核验房源信息{BLANK_BRACKET}。"),
        ("客户家庭此次申请公积金贷款应认定为（　）套。", f"客户家庭此次申请公积金贷款应认定为套数{BLANK_BRACKET}。"),
        ("该笔业务的应纳税额为（　）元。", f"该笔业务的应纳税额为金额（元）{BLANK_BRACKET}。"),
    ],
    ids=[
        "question_mark_before_bracket",
        "semicolon_before_bracket",
        "comma_space_before_bracket",
        "ascii_parentheses",
        "missing_period_after_bracket",
        "period_before_bracket",
        "half_width_spaces_in_bracket",
        "multiple_full_width_spaces_in_bracket",
        "unit_moves_into_stem_set_count",
        "unit_moves_into_stem_amount",
    ],
)
def test_choice_question_tail_normalization_matrix(raw_text, expected):
    normalized = enforce_question_bracket_and_punct(raw_text, "单选题")
    assert normalized == expected
    assert not has_forbidden_symbol_before_ending_blank_bracket(normalized)


@pytest.mark.parametrize(
    ("question", "target_type", "expected_issue"),
    [
        ("经纪人应核验房源信息？（　）。", "单选题", "题干结尾作答括号前不能有任何符号或空格"),
        ("经纪人应核验房源信息（ ）", "单选题", "题干结尾括号中间必须有且仅有一个全角空格（不能多）"),
        ("经纪人应核验房源信息（　）", "单选题", "选择题题干未以句号结尾"),
        ("经纪人应核验房源信息（　）。（　）。", "单选题", "选择题题干作答占位括号（ ）只能出现一次"),
        ("经纪人应核验房源信息。", "单选题", "题干缺少标准占位括号（须为全角括号且括号内有且仅有一个全角空格）"),
    ],
    ids=[
        "punctuation_before_bracket_rejected",
        "half_width_blank_rejected",
        "missing_period_rejected",
        "duplicate_blank_bracket_rejected",
        "missing_blank_bracket_rejected",
    ],
)
def test_writer_format_validation_matrix(question, target_type, expected_issue):
    issues = validate_writer_format(question, ["核验后发布", "直接发布", "委托他人核验", "口头说明即可"], "A", target_type)
    assert expected_issue in issues


def test_writer_format_rejects_interrogative_true_false_stem():
    issues = validate_writer_format(
        "经纪人作为房源推荐人，其提报条件是否正确（　）",
        ["正确", "错误"],
        "A",
        "判断题",
    )
    assert "判断题题干应使用肯定陈述句，避免“是否正确/对不对”等疑问句" in issues


def test_critic_format_validation_matrix_for_choice_question_tail():
    final_json = {
        "题干": "经纪人应核验房源信息？（　）。",
        "选项1": "核验后发布",
        "选项2": "直接发布",
        "选项3": "委托他人核验",
        "选项4": "口头说明即可",
        "正确答案": "A",
        "解析": "1、教材原文：教材。\n2、试题分析：分析。\n3、结论：本题答案为A。",
    }
    issues = validate_critic_format(final_json, "单选题")
    assert "题干结尾作答括号前不能有任何符号或空格" in issues


def test_repair_final_json_format_repairs_choice_question_tail_and_options():
    raw = {
        "题干": "经纪人应核验房源信息？（ ）",
        "选项1": "A. 核验后发布。",
        "选项2": "B、直接发布",
        "选项3": "C: 委托他人核验",
        "选项4": "D）口头说明即可",
        "正确答案": "a",
        "解析": "1.教材原文：教材 2.试题分析：分析 3.结论：本题答案为A",
    }
    repaired = repair_final_json_format(raw, "单选题")
    assert repaired["题干"] == f"经纪人应核验房源信息{BLANK_BRACKET}。"
    assert repaired["选项1"] == "核验后发布"
    assert repaired["选项2"] == "直接发布"
    assert repaired["选项3"] == "委托他人核验"
    assert repaired["选项4"] == "口头说明即可"
    assert repaired["正确答案"] == "a"
    assert "本题答案为A" in repaired["解析"]


def test_repair_final_json_format_moves_trailing_unit_into_stem():
    raw = {
        "题干": "客户家庭此次申请公积金贷款应认定为（　）套。",
        "选项1": "首套",
        "选项2": "二套",
        "正确答案": "A",
        "解析": "1.教材原文：教材 2.试题分析：分析 3.结论：本题答案为A",
    }
    repaired = repair_final_json_format(raw, "单选题")
    assert repaired["题干"] == f"客户家庭此次申请公积金贷款应认定为套数{BLANK_BRACKET}。"
