from admin_api import _build_display_paths


def test_build_display_paths_restores_flattened_parent_heading():
    kb_items = [
        {"完整路径": "第一篇 > 第一章 > 第一节 > 一、总述"},
        {"完整路径": "第一篇 > 第一章 > 第一节 > 三、房地产市场周期波动"},
        {"完整路径": "第一篇 > 第一章 > 第一节 > （一）周期影响因素"},
        {"完整路径": "第一篇 > 第一章 > 第一节 > （二）市场分析指标"},
    ]

    out = _build_display_paths(kb_items)

    assert out[2] == "第一篇 > 第一章 > 第一节 > 三、房地产市场周期波动 > （一）周期影响因素"
    assert out[3] == "第一篇 > 第一章 > 第一节 > 三、房地产市场周期波动 > （二）市场分析指标"


def test_build_display_paths_keeps_existing_full_path_unchanged():
    kb_items = [
        {"完整路径": "第一篇 > 第一章 > 第一节 > 三、房地产市场周期波动 > （一）周期影响因素"},
    ]

    out = _build_display_paths(kb_items)

    assert out[0] == "第一篇 > 第一章 > 第一节 > 三、房地产市场周期波动 > （一）周期影响因素"
