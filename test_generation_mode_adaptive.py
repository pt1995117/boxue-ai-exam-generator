import exam_graph


def _state(path: str, content: str, focus_task: str = "", focus_rule: str = "") -> dict:
    return {
        "kb_chunk": {
            "完整路径": path,
            "核心内容": content,
        },
        "router_details": {
            "core_focus": focus_rule,
            "focus_contract": {
                "focus_task": focus_task,
                "focus_rule": focus_rule,
            },
        },
    }


def test_conceptual_slice_downgrades_practical_mode():
    state = _state(
        "第一篇 > 第二章 > 公司文化理念 > 链家的使命",
        "链家的使命是有尊严的服务者，更美好的居住。",
        focus_task="规则理解",
        focus_rule="链家使命内涵理解",
    )
    effective, normalized = exam_graph.resolve_effective_generation_mode("实战应用/推演", state)
    assert normalized == "实战应用/推演"
    assert effective == "基础概念/理解记忆"


def test_actionable_slice_keeps_practical_mode():
    state = _state(
        "第三篇 > 第一节 > 契税 > 免征或者减征情形",
        "新购入住房网签日期需在动迁协议签订之后。契税=（核定价-补偿价-增值税）×税率。",
        focus_task="规则判定",
        focus_rule="拆迁补偿款购房契税减免规则",
    )
    effective, normalized = exam_graph.resolve_effective_generation_mode("实战应用/推演", state)
    assert normalized == "实战应用/推演"
    assert effective == "实战应用/推演"


def test_random_mode_prefers_memory_for_conceptual_slice():
    state = _state(
        "第一篇 > 第二章 > 公司文化理念 > 链家的使命",
        "企业文化概念与价值观内涵。",
        focus_task="规则理解",
        focus_rule="公司文化使命理解",
    )
    effective, normalized = exam_graph.resolve_effective_generation_mode("随机", state)
    assert normalized == "随机"
    assert effective == "基础概念/理解记忆"

