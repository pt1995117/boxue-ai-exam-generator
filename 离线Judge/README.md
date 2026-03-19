# Offline Judge

高可用、可工程化落地的离线 Judge（自动评估引擎）。

## 当前能力

- `Phase 1` 硬规则校验：
  - 括号/标点/单双引号
  - 设问句式规范（单选/多选/判断）
  - 违禁词与兜底选项
  - 选项数值排序
  - 人名/场景冗余/AI幻觉词常识检查
  - 地理一致性（题干城市 vs 教材城市）
  - 解析三段论与结论一致性
- `Phase 2` 多 Agent 语义审查（LangGraph）：
  - 反向解题器（Fatal 熔断）
  - 业务真实性嗅探器
  - 选项与解析质量检查器
  - 计算题动态代码校验器
- `Phase 3` 聚合裁决：
  - `PASS` / `NEEDS_MINOR_FIX` / `REJECT`
  - 标准化 JSON 报告

## 可靠性增强

- 统一 `ReliableLLMClient`：超时、重试、JSON 提取与 fallback
- 支持 OpenAI / Anthropic 双 provider
- 计算题支持“静态代码审查 + 受限子进程执行”

## 快速使用

1. 安装依赖

```bash
pip install -r requirements.txt
```

2. 离线（Mock）运行

```bash
python judge_cli.py examples/sample_input.json --mock-llm
```

3. 真实模型运行

```bash
export OPENAI_API_KEY=xxx
python judge_cli.py examples/sample_input.json --provider openai --model gpt-4o-mini
```

## Golden Dataset 回归评测

```bash
python -m src.evaluation.batch_runner examples/golden_dataset_sample.json --mock-llm --output-dir outputs
```

输出：
- `outputs/reports.json`：逐题报告
- `outputs/metrics.json`：准确率、误放行率、误拒率、混淆矩阵

## 核心文件

- `src/schemas/evaluation.py`：输入输出 Schema
- `src/filters/deterministic_filter.py`：硬规则校验
- `src/agents/*.py`：多 Agent 节点
- `src/agents/safe_python_runner.py`：计算题代码安全执行
- `src/llm/client.py`：LLM 容错调用
- `src/llm/factory.py`：真实模型构造
- `src/pipeline/graph.py`：LangGraph 编排与裁决
- `src/evaluation/batch_runner.py`：Golden Dataset 批评测
