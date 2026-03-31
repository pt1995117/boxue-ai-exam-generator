# 上线前真实 LLM 门禁

这套门禁不是本地 mock，也不是只测条件函数。

它会真实调用 `填写您的Key.txt` 中配置的大模型，执行 LangGraph 的关键路径，并把每个节点拿到的关键 state 落到报告里，重点检查：

- 题目是否在节点间串题
- `final_json` 是否被后续节点正确继承
- `current_question_type/current_generation_mode` 是否正确传递
- `candidate_sentences/writer_validation_report` 是否跟随最新题目刷新
- 计算链路里的 `generated_code/execution_result/code_status` 是否正确流转
- `fixer -> critic -> reroute` 后旧状态是否被清理

## 必跑命令

```bash
python /Users/panting/Desktop/搏学考试/AI出题/run_release_gate_real_llm.py
```

如果当前默认租户不是正式教材库，必须显式指定租户或知识库路径，例如：

```bash
python /Users/panting/Desktop/搏学考试/AI出题/run_release_gate_real_llm.py --tenant-id wh
```

或

```bash
python /Users/panting/Desktop/搏学考试/AI出题/run_release_gate_real_llm.py --kb-path /绝对路径/knowledge_slices.jsonl --history-path /绝对路径/母题.xlsx
```

## 报告位置

```text
/Users/panting/Desktop/搏学考试/AI出题/tmp/release_gate_real_llm_report.json
```

## 当前固定场景

1. `real_non_calc_pass`
2. `real_calc_pass`
3. `real_non_calc_fix_once`
4. `real_calc_fix_once`
5. `real_non_calc_reroute_to_calculator`
6. `real_calc_reroute_to_specialist`

## 使用说明

- 这套门禁依赖真实模型服务可用。
- 若 provider 限流、服务异常或 key 无效，脚本会直接失败，并把错误写入报告。
- 发布前应至少查看一次报告中的 `path` 和 `snapshots`，确认节点路径与关键 state 没有串题或丢字段。
