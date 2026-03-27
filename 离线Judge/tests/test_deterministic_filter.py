"""Phase 1 确定性过滤器单元测试。"""

from src.filters.deterministic_filter import DeterministicFilter
from src.schemas.evaluation import QuestionInput


def test_filter_passes_valid_question():
    q = QuestionInput(
        question_id="Q-001",
        stem="张某购买首套房，以下表述正确的是（　）。",
        options=["A. 1%", "B. 1.5%", "C. 2%", "D. 3%"],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="教材",
        question_type="single_choice",
    )
    r = DeterministicFilter().run(q)
    assert r.passed is True
    assert len(r.errors) == 0


def test_filter_rejects_forbidden_option():
    q = QuestionInput(
        question_id="Q-002",
        stem="张某购买首套房，以下表述正确的是（ ）。",
        options=["A. 选项A", "B. 选项B", "C. 以上皆是", "D. 选项D"],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="教材",
        question_type="single_choice",
    )
    r = DeterministicFilter().run(q)
    assert r.passed is False
    assert any("以上皆是" in e for e in r.errors)


def test_filter_rejects_invalid_explanation_sections():
    q = QuestionInput(
        question_id="Q-003",
        stem="张某购买首套房，以下表述正确的是（ ）。",
        options=["A. 选项A", "B. 选项B", "C. 选项C", "D. 选项D"],
        correct_answer="A",
        explanation="2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="教材",
        question_type="single_choice",
    )
    r = DeterministicFilter().run(q)
    assert r.passed is False
    assert any("1.教材原文" in e for e in r.errors)


def test_filter_accepts_declarative_blank_style_stem():
    q = QuestionInput(
        question_id="Q-004",
        stem="经纪人询问业主“房屋是否满五唯一”，是在收集（　）。",
        options=["A. 房源实物信息", "B. 房源区位信息", "C. 房源交易条件信息", "D. 房源交易信息"],
        correct_answer="C",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为C",
        textbook_slice="教材",
        question_type="single_choice",
    )
    r = DeterministicFilter().run(q)
    assert r.passed is True
    assert not any("单选题句式建议" in w for w in r.warnings)


def test_filter_rejects_parent_child_options_in_single_choice():
    q = QuestionInput(
        question_id="Q-005",
        stem="业主出售一套四年前购买的新建商品住宅，从流通属性角度应归类为（ ）。",
        options=["A. 增量住宅", "B. 存量住宅", "C. 二手房", "D. 次新住房"],
        correct_answer="C",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为C",
        textbook_slice="存量住宅中购买后再次上市交易的住宅，称为“二手房”。",
        question_type="single_choice",
    )
    r = DeterministicFilter().run(q)
    assert any("疑似层级冲突" in w for w in r.warnings)


def test_filter_rejects_blank_at_sentence_start():
    q = QuestionInput(
        question_id="Q-006",
        stem="（ ）是房源实物信息的组成部分。",
        options=["A. 户型", "B. 税费", "C. 产权年限", "D. 交易进度"],
        correct_answer="A",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为A",
        textbook_slice="教材",
        question_type="single_choice",
    )
    r = DeterministicFilter().run(q)
    assert r.passed is False
    assert any("不能放在句首" in e for e in r.errors)


def test_filter_rejects_multiple_answer_blanks():
    q = QuestionInput(
        question_id="Q-007",
        stem="经纪人（ ）向客户说明（ ）。",
        options=["A. 需要", "B. 可以", "C. 不得", "D. 应当"],
        correct_answer="B",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为B",
        textbook_slice="教材",
        question_type="single_choice",
    )
    r = DeterministicFilter().run(q)
    assert r.passed is False
    assert any("只能出现一次" in e for e in r.errors)


def test_filter_rejects_unreasonable_floor_height_consistency():
    q = QuestionInput(
        question_id="Q-008",
        stem="某住宅楼地上实际总层数为11层，建筑高度为28米。根据相关标准，该栋住宅楼应属于（ ）。",
        options=["A. 低层住宅", "B. 多层住宅", "C. 高层住宅", "D. 超高层住宅"],
        correct_answer="C",
        explanation="1.教材原文\n2.试题分析\n3.结论\n本题答案为C",
        textbook_slice="按层数与建筑高度分类可判定住宅类型。",
        question_type="single_choice",
    )
    r = DeterministicFilter().run(q)
    assert r.passed is False
    assert any("层数与建筑高度不符合常识" in e for e in r.errors)
