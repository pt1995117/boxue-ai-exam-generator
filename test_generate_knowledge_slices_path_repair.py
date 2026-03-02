from generate_knowledge_slices import repair_flattened_paths


def test_repair_flattened_paths_inserts_missing_l4_parent():
    slices = [
        {"完整路径": "第一篇 > 第一章 > 第一节 > 一、总述"},
        {"完整路径": "第一篇 > 第一章 > 第一节 > 三、房地产市场周期波动"},
        {"完整路径": "第一篇 > 第一章 > 第一节 > （一）周期影响因素"},
        {"完整路径": "第一篇 > 第一章 > 第一节 > （二）市场分析指标"},
    ]

    fixed = repair_flattened_paths(slices)

    assert fixed[2]["完整路径"] == "第一篇 > 第一章 > 第一节 > 三、房地产市场周期波动 > （一）周期影响因素"
    assert fixed[3]["完整路径"] == "第一篇 > 第一章 > 第一节 > 三、房地产市场周期波动 > （二）市场分析指标"


def test_repair_flattened_paths_keeps_already_full_paths():
    slices = [
        {"完整路径": "第一篇 > 第一章 > 第一节 > 三、房地产市场周期波动 > （一）周期影响因素"},
    ]

    fixed = repair_flattened_paths(slices)

    assert fixed[0]["完整路径"] == "第一篇 > 第一章 > 第一节 > 三、房地产市场周期波动 > （一）周期影响因素"
