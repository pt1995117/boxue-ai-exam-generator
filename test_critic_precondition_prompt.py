import json

import exam_graph


def test_precondition_audit_does_not_require_memorized_rules_in_stem(monkeypatch):
    prompts = []

    def fake_call_llm(**kwargs):
        prompts.append(kwargs["prompt"])
        node_name = kwargs["node_name"]
        if node_name == "critic.precondition_current":
            return (
                json.dumps(
                    {
                        "passed": False,
                        "materially_affects_answer": True,
                        "missing_conditions": ["贷款年限+房龄≤70年", "贷款年限+借款人年龄≤70周岁"],
                        "reason": "题干未写贷款年限限制规则",
                        "evidence": ["规则未出现在题干"],
                        "conflicting_options": ["B", "C"],
                    },
                    ensure_ascii=False,
                ),
                "",
                {"node": node_name},
            )
        if node_name == "critic.precondition_current.availability":
            return (
                json.dumps(
                    {
                        "truly_missing": [],
                        "dismissed": ["贷款年限+房龄≤70年", "贷款年限+借款人年龄≤70周岁"],
                        "reason": "这些是教材规则，不是题干必须补充的个案事实",
                    },
                    ensure_ascii=False,
                ),
                "",
                {"node": node_name},
            )
        raise AssertionError(f"unexpected node: {node_name}")

    monkeypatch.setattr(exam_graph, "call_llm", fake_call_llm)

    passed, missing, reason, _record = exam_graph.assess_preconditions_current_only(
        final_json={
            "题干": "客户王强今年46周岁，计划申请商业贷款购买一套竣工于2015年的住宅，可申请的最长贷款年限是（　）。",
            "选项1": "11年",
            "选项2": "24年",
            "选项3": "30年",
            "选项4": "46年",
            "正确答案": "B",
        },
        kb_context="商业贷款年限应同时满足贷款年限+房龄≤70年、贷款年限+借款人年龄≤70周岁。",
        question_type="单选题",
        model_name="gpt-5.2",
        api_key="test-key",
        base_url="https://example.com/v1",
        provider="ait",
        trace_id="trace-1",
        question_id="q-1",
        node_name="critic.precondition_current",
    )

    assert passed is True
    assert missing == []
    assert "教材规则" in reason
    assert "不应强制复述进题干" in prompts[0]
    assert "当前题目的设问、选项、正确答案和唯一作答链路动态推导" in prompts[0]
    assert "不应强制写进题干的信息" in prompts[0]
    assert "禁止按预置字段清单" in prompts[0]
    assert "教材规则、岗位应记忆公式" in prompts[1]


def test_minimal_conditions_prompt_treats_rule_disclosure_as_overload(monkeypatch):
    seen_prompt = {}

    def fake_call_llm(**kwargs):
        seen_prompt["prompt"] = kwargs["prompt"]
        return (
            json.dumps(
                {
                    "passed": False,
                    "overloaded": True,
                    "minimal_conditions": ["当前题目唯一作答所需的个案输入"],
                    "redundant_conditions": ["贷款年限+房龄≤70年", "贷款年限+借款人年龄≤70周岁"],
                    "reason": "题干直接写出应由学员记忆并套用的贷款年限规则",
                    "fix_hint": "删除题干中的规则提示，只保留本题唯一作答所需的个案输入",
                },
                ensure_ascii=False,
            ),
            "",
            {"node": kwargs["node_name"]},
        )

    monkeypatch.setattr(exam_graph, "call_llm", fake_call_llm)

    passed, redundant, reason, _record = exam_graph.assess_minimal_sufficient_conditions_current_only(
        final_json={
            "题干": "客户王强今年46周岁，计划申请商业贷款购买一套竣工于2015年的住宅，银行规定商业贷款年限最长不超过30年，且需同时满足贷款年限+房龄≤70年和贷款年限+借款人年龄≤70周岁的条件，可申请的最长贷款年限是（　）。",
            "选项1": "11年",
            "选项2": "24年",
            "选项3": "30年",
            "选项4": "46年",
            "正确答案": "B",
        },
        kb_context="商业贷款年限规则。",
        question_type="单选题",
        model_name="gpt-5.2",
        api_key="test-key",
        base_url="https://example.com/v1",
        provider="ait",
        trace_id="trace-1",
        question_id="q-1",
        node_name="critic.minimal_conditions_current",
    )

    assert passed is False
    assert "贷款年限+房龄≤70年" in redundant
    assert "提示过强" in seen_prompt["prompt"]
    assert "当前题目动态推导出的必要个案输入" in seen_prompt["prompt"]
    assert "禁止按预置字段清单" in seen_prompt["prompt"]
    assert "删除题干中的规则提示" in reason
