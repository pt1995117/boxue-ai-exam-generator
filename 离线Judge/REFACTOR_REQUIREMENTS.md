# 重构需求说明

## 目标
- 将 `src/pipeline/graph.py` 的建图与运行职责拆分到独立模块。
- 保持 LangGraph 拓扑、节点语义与外部接口行为不变。
- 提升可维护性，降低单文件复杂度与改动冲突风险。

## 范围
- `src/pipeline/state.py`
- `src/pipeline/routes.py`
- `src/pipeline/builder.py`
- `src/pipeline/runner.py`
- `src/pipeline/graph.py`（保留兼容门面）
- `tests` 中增加拓扑一致性与回归验证

## 非目标
- 不调整评分规则与裁决逻辑。
- 不重写 prompt 语义。
- 不修改 CLI 参数与调用方式。

## 验收标准
- `pytest` 全量通过。
- 三入口 mock 冒烟可运行（`judge_cli.py`、`src.main`、`batch_runner`）。
- `tests/test_graph_topology.py` 通过，证明拓扑未变。
