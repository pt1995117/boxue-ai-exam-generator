# 重构技术方案

## 模块划分
- `src/pipeline/state.py`：`JudgeState` 与初始状态构造。
- `src/pipeline/routes.py`：条件路由函数。
- `src/pipeline/builder.py`：LangGraph 拓扑装配。
- `src/pipeline/runner.py`：执行入口与兜底报告。
- `src/pipeline/graph.py`：节点实现与兼容导出。

## 兼容策略
- 对外仍使用 `from src.pipeline.graph import create_judge_graph, run_judge`。
- `graph.py` 通过门面函数转发到 `builder.py` 与 `runner.py`。
- 节点函数名称与图拓扑保持不变。

## 风险控制
- 通过 `tests/test_graph_topology.py` 固定节点与边集合。
- 通过现有 `test_pipeline.py` 保证行为回归。
- 路由函数独立后，避免后续改动误伤节点逻辑。
