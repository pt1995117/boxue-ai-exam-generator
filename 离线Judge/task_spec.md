# Task Spec（基于当前代码初始化）

版本：init-2026-03-10+realism-fatal-explain+compliance-risk-gate+kg-prompt-guard+fatal-tighten+kg-evidence-threshold+code-evidence-arbitration
说明：每项任务均绑定 TDD ID；仅在真实测试通过后勾选。

[x] TASK-001 | 建立并维护 PRD 代码真值映射（流程、门禁、决策信号） | 关联 TDD ID: TDD-001,TDD-003,TDD-004,TDD-005 | 验收点: [文档描述与现网代码一致，抽样检查无冲突]

[x] TASK-002 | 建立回归基线并固化现有 tests/test_pipeline.py 通过性 | 关联 TDD ID: TDD-001,TDD-002,TDD-003,TDD-004,TDD-005 | 验收点: [执行 python -m pytest -q tests/test_pipeline.py 全部通过]

[ ] TASK-003 | 新增年份约束硬校验单测（教材无年份时拦截） | 关联 TDD ID: TDD-006 | 验收点: [命中“题干/选项/解析出现公历年份（原文未提及）”错误]

[ ] TASK-004 | 新增年份约束放行单测（教材有年份时不拦截） | 关联 TDD ID: TDD-007 | 验收点: [不出现年份误杀，且其余断言保持稳定]

[ ] TASK-005 | 新增解析结论与答案一致性单测 | 关联 TDD ID: TDD-008 | 验收点: [不一致时稳定报错“一致性”类错误]

[ ] TASK-006 | 补齐“盲答推导答案与标准答案一致性”聚合门禁规则 | 关联 TDD ID: TDD-009 | 验收点: [predicted_answer != correct_answer 时触发显式失败条件并产出原因]

[ ] TASK-007 | 明确并实现 skip_phase1 参数行为（跳过或降权） | 关联 TDD ID: TDD-010 | 验收点: [run_judge(skip_phase1=True) 与 False 行为差异符合 PRD 定义]

[ ] TASK-008 | 执行完整测试并按结果更新任务勾选状态 | 关联 TDD ID: TDD-001,TDD-002,TDD-003,TDD-004,TDD-005,TDD-006,TDD-007,TDD-008,TDD-009,TDD-010 | 验收点: [已执行 python -m pytest -q tests/test_pipeline.py（17/17）与 python -m pytest -q（32/32）；TDD-006~010 尚缺对应自动化或目标态未实现，保持未完成]

[x] TASK-009 | 补齐代码真值文档缺口（LLM缺失短路、ambiguity判定、推荐题型冲突、证据/成本/观测细节） | 关联 TDD ID: TDD-001,TDD-003,TDD-004,TDD-005 | 验收点: [REQUIREMENTS.md 与 prd_spec.md 对上述逻辑描述一致且可在代码中逐条映射]

[x] TASK-010 | 新增“实操拟真度”后置评估证据字段（仅上报不裁决） | 关联 TDD ID: TDD-011,TDD-012 | 验收点: [情景题可稳定识别是否包含探需/安抚/方案动作，并写入业务真实性 details]

[x] TASK-011 | 新增“教条主义”致命拦截（仅在聚合节点生效） | 关联 TDD ID: TDD-013,TDD-014 | 验收点: [命中负面情绪+放大缺点+无补救时直接 reject；有补救时不触发该致命信号]

[x] TASK-012 | 新增“解析双支撑”评估（理论支撑+业务支撑） | 关联 TDD ID: TDD-015,TDD-016 | 验收点: [任一支撑缺失时解析质量 FAIL 并触发 review 信号]

[x] TASK-013 | 执行新增后置质量门禁测试并按结果回填任务状态 | 关联 TDD ID: TDD-011,TDD-012,TDD-013,TDD-014,TDD-015,TDD-016 | 验收点: [相关测试真实通过后再勾选；失败项保留未完成并注明原因]

[x] TASK-014 | 第二轮补齐代码真值文档缺口（接线矩阵、字段门禁矩阵、输入裁剪、计算回退阈值） | 关联 TDD ID: TDD-001,TDD-003,TDD-004,TDD-005 | 验收点: [REQUIREMENTS.md 与 prd_spec.md 新增条目可在 src/agents/*.py 与 src/pipeline/graph.py 逐条定位]

[x] TASK-015 | 第三轮补齐聚合输出字段映射（fatal_doctrinaire_gate、双支撑门禁、dimension_results.details 键级定义） | 关联 TDD ID: TDD-001,TDD-003,TDD-004,TDD-005 | 验收点: [REQUIREMENTS.md 第10/11节与 prd_spec.md 可覆盖 node_aggregate 关键出参与判定键]

[x] TASK-016 | 新增“合规与风控门禁”证据字段（高危域+危险行为+合规动作） | 关联 TDD ID: TDD-017,TDD-018,TDD-019,TDD-020,TDD-021 | 验收点: [surface_a 可稳定上报高危域与四类危险行为、三类合规动作证据]

[x] TASK-017 | 在聚合节点新增通用合规风控致命拦截（仅聚合层裁决） | 关联 TDD ID: TDD-017,TDD-018,TDD-019,TDD-020 | 验收点: [高危域命中且任一危险行为为真时直接 reject，并追加可追溯原因]

[x] TASK-018 | 新增合规风控门禁测试并回填状态 | 关联 TDD ID: TDD-017,TDD-018,TDD-019,TDD-020,TDD-021 | 验收点: [相关测试真实通过后再勾选，失败项不得误勾]

[x] TASK-019 | 新增“真理对抗”证据字段（错项优于正项、题干无判别性） | 关联 TDD ID: TDD-022,TDD-023,TDD-024 | 验收点: [surface_a 可稳定上报 competing_truth 与 non_discriminative_stem 风险证据]

[x] TASK-020 | 在聚合节点新增“真理对抗拦截”规则 | 关联 TDD ID: TDD-022,TDD-023 | 验收点: [错项优于正项至少 review；题干空泛无判别性触发 reject]

[x] TASK-021 | 执行真理对抗门禁测试并按结果回填状态 | 关联 TDD ID: TDD-022,TDD-023,TDD-024 | 验收点: [相关测试真实通过后再勾选，失败项不得误勾]

[x] TASK-022 | 调整知识门提示词：显式断言优先、并列≠因果、证据不足不短路 | 关联 TDD ID: TDD-025,TDD-026,TDD-027 | 验收点: [layer2_knowledge_gate Prompt 明确新增三类防误杀约束]

[ ] TASK-023 | 执行知识门防误杀回归测试并回填状态 | 关联 TDD ID: TDD-025,TDD-026,TDD-027 | 验收点: [相关测试真实通过后再勾选；未实现项保留未完成（当前仅完成通用回归：tests/test_pipeline.py）]

[x] TASK-024 | 收紧致命门槛：仅保留“无解/多解、知识超纲/边界冲突、法理数学闭环失败” | 关联 TDD ID: TDD-028,TDD-029,TDD-030,TDD-031 | 验收点: [node_aggregate 的 fatal_reject_signals 仅包含上述三类]

[x] TASK-025 | 将教条主义/合规风控/真理对抗/超长文本由致命降级为 Review | 关联 TDD ID: TDD-028,TDD-029,TDD-030,TDD-031 | 验收点: [对应场景不再直接 reject，且 reasons 保留风险提示]

[x] TASK-026 | 将题型固定模板与数值升序由 error 降级为 warning | 关联 TDD ID: TDD-032,TDD-033 | 验收点: [_basic_rules_code_checks 对应规则只产出 warning，不进入 errors]

[x] TASK-027 | 将年份约束由 error 调整为“需证据支持”的 review 信号 | 关联 TDD ID: TDD-006,TDD-007 | 验收点: [教材无年份+题面有年份时仅 warning，并在聚合进入 review 信号]

[x] TASK-028 | 知识门短路新增证据阈值（布尔命中+证据充分才短路） | 关联 TDD ID: TDD-034,TDD-035 | 验收点: [evidence不足时降级review并继续后链路；evidence充分时才knowledge_gate_reject=true]

[x] TASK-029 | 知识门短路时输出证据链到 reasons | 关联 TDD ID: TDD-034 | 验收点: [短路场景下reasons包含“知识边界短路-证据链”条目]

[x] TASK-030 | 计算题代码节点证据仲裁（code_evidence_status/chain + 聚合 HARD→reject, SOFT/TOOL_FAIL→review） | 关联 TDD ID: TDD-036,TDD-037 | 验收点: [HARD 致命拒绝且 reasons 含证据链；SOFT/TOOL_FAIL 仅 review 且 reasons 含对应说明]

