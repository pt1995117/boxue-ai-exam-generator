from exam_graph import (
    detect_term_locks_from_kb,
    detect_term_lock_violations,
)


def _mock_glossary():
    term_to_categories = {
        "商业贷款": ["贷款与金融"],
        "公积金贷款": ["贷款与金融"],
        "商贷": ["贷款与金融"],
        "定金": ["资金税费与价格"],
        "订金": ["资金税费与价格"],
    }
    return {
        "terms": ["商业贷款", "公积金贷款", "商贷", "定金", "订金"],
        "terms_by_category": {
            "贷款与金融": ["商业贷款", "公积金贷款", "商贷"],
            "资金税费与价格": ["定金", "订金"],
        },
        "term_to_categories": term_to_categories,
    }


def test_detect_term_locks_keyword_plus_semantic(monkeypatch):
    import exam_graph

    monkeypatch.setattr(exam_graph, "_GLOSSARY_CACHE", _mock_glossary())
    kb_chunk = {
        "完整路径": "第一篇 > 贷款与金融 > 商业贷款",
        "核心内容": "商业贷款与公积金贷款在首付比例和贷款年限上存在差异。",
        "结构化内容": {"examples": []},
    }
    locks = detect_term_locks_from_kb(kb_chunk)
    assert "商业贷款" in locks
    assert "公积金贷款" in locks
    # Not in context, should not be locked.
    assert "定金" not in locks


def test_detect_term_lock_violations_when_rewritten(monkeypatch):
    import exam_graph

    monkeypatch.setattr(exam_graph, "_GLOSSARY_CACHE", _mock_glossary())
    payload = {
        "题干": "客户咨询商贷办理流程（ ）。",
        "选项1": "商贷审批更快",
        "选项2": "公积金贷款审批更快",
        "解析": "本题重点在于商贷与公积金贷款差异。",
    }
    violations = detect_term_lock_violations(["商业贷款"], payload)
    assert violations, "Expected lock-term rewrite violations"
    assert "商业贷款" in violations[0]


def test_detect_term_lock_violations_even_when_lock_appears(monkeypatch):
    import exam_graph

    monkeypatch.setattr(exam_graph, "_GLOSSARY_CACHE", _mock_glossary())
    payload = {
        "题干": "商业贷款办理流程中，常见说法是商贷审批更快（ ）。",
        "选项1": "商业贷款审批更快",
        "选项2": "公积金贷款审批更快",
        "解析": "该题中同时出现商业贷款和商贷，但并非术语解释语句。",
    }
    violations = detect_term_lock_violations(["商业贷款"], payload)
    assert violations, "Should trigger even if lock term appears"


def test_explanatory_usage_allowed(monkeypatch):
    import exam_graph

    monkeypatch.setattr(exam_graph, "_GLOSSARY_CACHE", _mock_glossary())
    payload = {
        "题干": "术语理解题：商业贷款（简称商贷）是指由商业银行发放的贷款（ ）。",
        "选项1": "正确",
        "选项2": "错误",
        "解析": "这里是在解释术语，不是替换术语。",
    }
    violations = detect_term_lock_violations(["商业贷款"], payload)
    assert not violations


def test_no_false_positive_cross_category(monkeypatch):
    import exam_graph

    monkeypatch.setattr(exam_graph, "_GLOSSARY_CACHE", _mock_glossary())
    payload = {
        "题干": "客户支付了定金后咨询贷款流程（ ）。",
        "选项1": "商贷审批更快",
        "选项2": "公积金贷款审批更快",
        "解析": "本题重点是定金规则。",
    }
    # lock is from loan category, but text mainly keeps another category term
    violations = detect_term_lock_violations(["定金"], payload)
    assert not violations


def test_no_false_positive_when_both_locked_terms_present(monkeypatch):
    import exam_graph

    glossary = {
        "terms": ["套内建筑面积", "建筑面积"],
        "terms_by_category": {
            "房屋信息": ["套内建筑面积", "建筑面积"],
        },
        "term_to_categories": {
            "套内建筑面积": ["房屋信息"],
            "建筑面积": ["房屋信息"],
        },
    }
    monkeypatch.setattr(exam_graph, "_GLOSSARY_CACHE", glossary)
    payload = {
        "题干": "客户咨询建筑面积与套内建筑面积的区别（ ）。",
        "选项1": "建筑面积包含套内建筑面积与公摊面积",
        "选项2": "套内建筑面积大于建筑面积",
        "解析": "本题同时讨论两个术语，均按原词出现。",
    }
    violations = detect_term_lock_violations(["套内建筑面积", "建筑面积"], payload)
    assert not violations
