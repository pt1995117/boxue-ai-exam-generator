import json

import exam_graph


def _base_state():
    return {
        "final_json": {
            "题干": "经纪人王明录入房源后，后续可参与该业务角色的是（　）。",
            "选项1": "房源录入人",
            "选项2": "钥匙人",
            "选项3": "实勘人",
            "选项4": "带看人",
            "正确答案": "A",
            "解析": "本题答案为A。",
            "难度值": 0.5,
            "考点": "角色权限",
        },
        "critic_feedback": "FAIL",
        "critic_details": "无法唯一推导答案",
        "critic_result": {
            "passed": False,
            "fix_strategy": "fix_question",
            "fix_reason": "存在多解风险",
        },
        "critic_required_fixes": ["logic:cannot_deduce_unique_answer"],
        "critic_tool_usage": {},
        "critic_rules_context": "",
        "critic_related_rules": [],
        "kb_chunk": {
            "完整路径": "第一篇 > 角色权限 > 录入规则",
            "掌握程度": "了解",
            "核心内容": "录入人与后续业务角色边界",
            "结构化内容": {},
            "metadata": {},
        },
        "term_locks": [],
        "current_question_type": "单选题",
        "current_generation_mode": "随机",
    }


def _base_config():
    return {"configurable": {"question_type": "单选题", "generation_mode": "随机"}}


def test_fixer_does_not_perform_required_fix_acceptance(monkeypatch):
    state = _base_state()
    mutated = dict(state["final_json"])
    mutated["难度值"] = 0.6

    def fake_call_llm(*args, **kwargs):
        return json.dumps(mutated, ensure_ascii=False), None, {"node": kwargs.get("node_name", "fake")}

    monkeypatch.setattr(exam_graph, "call_llm", fake_call_llm)
    result = exam_graph.fixer_node(state, _base_config())

    assert result.get("fix_required_unmet") is False
    fix_summary = result.get("fix_summary") or {}
    unmet = fix_summary.get("unmet_required_fixes") or []
    assert unmet == []


def test_fixer_clears_unmet_when_logic_required_and_question_is_rewritten(monkeypatch):
    state = _base_state()
    rewritten = dict(state["final_json"])
    rewritten["题干"] = "经纪人王明录入房源后，仅可继续担任的角色是（　）。"
    rewritten["选项1"] = "房源录入人"
    rewritten["选项2"] = "钥匙人（独立服务）"
    rewritten["选项3"] = "实勘人（独立服务）"
    rewritten["选项4"] = "带看人（独立服务）"

    def fake_call_llm(*args, **kwargs):
        return json.dumps(rewritten, ensure_ascii=False), None, {"node": kwargs.get("node_name", "fake")}

    monkeypatch.setattr(exam_graph, "call_llm", fake_call_llm)
    result = exam_graph.fixer_node(state, _base_config())

    assert result.get("fix_required_unmet") is False
    fix_summary = result.get("fix_summary") or {}
    unmet = fix_summary.get("unmet_required_fixes") or []
    assert unmet == []
