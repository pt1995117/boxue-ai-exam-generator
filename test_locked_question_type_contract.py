import exam_graph
import inspect


def _base_state():
    return {
        "final_json": {
            "题干": "某规则下，正确选项有（　）。",
            "选项1": "说法一",
            "选项2": "说法二",
            "选项3": "说法三",
            "选项4": "说法四",
            "正确答案": "AC",
            "解析": "1、教材原文：...\n2、试题分析：...\n3、结论：本题答案为AC。",
            "难度值": 0.5,
            "考点": "测试考点",
        },
        "kb_chunk": {
            "完整路径": "第一篇 > 第一章 > 第一节 > 测试点",
            "掌握程度": "掌握",
            "核心内容": "测试内容",
            "结构化内容": {},
            "metadata": {},
        },
        "term_locks": [],
        "examples": [],
        "router_details": {},
        "current_question_type": "单选题",
        "locked_question_type": "单选题",
        "current_generation_mode": "随机",
        "writer_validation_report": {},
        "writer_retry_exhausted": False,
    }


def _base_config():
    return {"configurable": {"question_type": "随机", "generation_mode": "随机", "difficulty_range": (0.3, 0.7)}}


def test_infer_final_json_question_type_multiselect():
    row = {
        "选项1": "A",
        "选项2": "B",
        "选项3": "C",
        "选项4": "D",
        "正确答案": "AC",
    }
    assert exam_graph._infer_final_json_question_type(row) == "多选题"


def test_critic_uses_actual_type_and_records_alignment_issue():
    source = inspect.getsource(exam_graph.critic_node)
    assert 'state_question_type' in source
    assert 'inferred_final_type = _infer_final_json_question_type(final_json)' in source
    assert 'question_type = inferred_final_type or state_question_type' in source
    assert 'question_type_alignment_issue' in source


def test_sync_helpers_do_not_write_locked_question_type():
    source = inspect.getsource(exam_graph._sync_downstream_state_from_final_json)
    assert '"locked_question_type"' not in source
