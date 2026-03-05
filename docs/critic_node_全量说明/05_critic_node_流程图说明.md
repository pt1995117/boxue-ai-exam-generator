# Critic 节点流程图说明

已基于当前代码逻辑生成 `jpg` 流程图：

- 文件名：`critic_node_流程图.jpg`
- 当前生成路径：`/Users/panting/.cursor/projects/Users-panting-Desktop-AI/assets/critic_node_流程图.jpg`

## 流程图覆盖范围

1. `critic_node` 入口短路逻辑  
2. rule-based 前置失败分支  
3. Step1 计算计划、代码校验与执行回退  
4. Step2 主审计与 JSON 解析  
5. 代码侧纠偏与重复题拦截  
6. 失败原因聚合与 `fix_strategy` 优先级决策  
7. PASS / FAIL 最终输出结构

## 与文档对应关系

- 前半段源码：`02_critic_node_源码_前半段.md`
- 后半段源码：`03_critic_node_源码_后半段.md`
- 依赖函数源码：`04_critic_node_依赖函数源码.md`

