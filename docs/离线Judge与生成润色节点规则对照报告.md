# 离线 Judge 与生成/润色节点规则对照报告

> 对照依据：`离线Judge/` 下 REQUIREMENTS.md、`_basic_rules_code_checks`、DeterministicFilter、layer3 提示词；主项目 `exam_graph.py`（Specialist/Calculator/Writer）、`hard_rules.py`、prd_spec/tdd_spec。

---

## 一、已对齐的规则（无需修改）

| 离线 Judge 要求 | 生成/润色侧现状 |
|-----------------|------------------|
| 题干 >400 字 → error | Writer 与 hard_rules：题干≤400 字、单选项≤200 字、解析≤400 字；hard_rules 有 HARD_STEM_LEN/HARD_OPTION_LEN/HARD_EXPL_LEN |
| 任一选项 >200 字 → error | 同上 |
| 题干/选项/解析禁止单引号 | hard_rules HARD_SINGLE_QUOTE；Writer 润色后可代码替换（replace_single_quotes_in_final_json） |
| 选项结尾禁止标点 | validate_writer_format + prepare_draft_for_writer 去尾标点；Writer 提示词有“选项末尾不添加标点” |
| 禁止图片/表格（题干、选项、解析） | hard_rules validate_media_rules；sanitize_media_payload 可做清洗 |
| 禁“以上都对/都错/皆是/皆非/以上选项全对/全错” | hard_rules HARD_BANNED_OPTION；Writer/Specialist/Fixer 提示词均有“禁用兜底选项” |
| 数值选项按从小到大升序 | hard_rules HARD_NUMERIC_ORDER；apply_numeric_options_ascending；Writer 提示词“数值型选项按从小到大顺序排列” |
| 判断题 2 选项、选择题 4 选项 | 题型指令与 prepare_draft_for_writer 已约束 |
| 答案字段：单选单字母、多选多字母、判断 A/B | validate_writer_format + hard_rules 解析结论与答案一致 |
| 解析三段：1.教材原文、2.试题分析、3.结论 | hard_rules HARD_EXPL_STRUCT；Writer 提示词明确三段式与结论“本题答案为X” |
| 判断题结论必须写“正确/错误”不能写 A/B | hard_rules 与 Writer 提示词均有 |
| 解析结论与正确答案字段一致 | hard_rules 结论校验 |
| 括号：全角中文括号、括号内空格、选择题“（ ）。 ”、判断题“。（ ）” | validate_writer_format、validate_question_template_semantics、enforce_question_bracket_and_punct |
| 题干括号不能在句首 | Writer 提示词“题干中的括号不能在句首” |
| 设问句式：单选/多选固定结尾模板 | validate_question_template_semantics；Writer/Specialist 提示词有固定结尾说明 |
| 年份约束（教材无年份则题面不得出现公历年份） | hard_rules HARD_YEAR（依 kb_context）；Critic 侧也有年份校验 |
| 人名规范（恶搞/伦理/称谓/小名/先生女士等） | Writer/Specialist/Fixer 提示词与人名规范条一致；validate_name_usage 校验先生/女士、小+姓 |
| 专有名词锁词 term_locks | Specialist/Calculator/Writer/Fixer 均有 term_lock_text；detect_term_lock_violations |
| 地理继承与严禁无关城市 | Writer 提示词“地理继承”“严禁无关城市” |
| 时间逻辑（原文未给时间则不写具体年份） | Writer 提示词“时间逻辑” |
| 避免“最XX”考法（最重要/最关键等） | Specialist 有 avoid_superlative；Writer 有“禁止最重要/最关键/重点/主要” |
| 禁止“实实在在”“关键因素”等模糊表述 | Writer 有“禁止使用模糊的日常用语” |
| 泄题判定从严（仅当正确答案原样在题干中） | Critic 提示词有“重要说明”；Judge layer3 一致 |
| 计算题尽量简单、保留小数时注明位数 | Writer 提示词有“确需保留小数时注明保留位数（一般1-2位）” |
| 师傅告诉/新人训等冗余场景不要 | Specialist/Writer 有“无意义的场景铺垫不要”“师傅告诉…”示例 |
| 选项单位应上提题干（Judge 5.1） | Specialist/Calculator/Writer 选项规范已加“选项不得包含数值单位，单位应写在题干中” |
| 违禁兜底“全部正确”“全部错误” | hard_rules banned_phrases 已包含 |
| 第2段试题分析须用自己的话、不得粘贴教材（Judge 8.2） | Specialist/Calculator/Writer 解析规范已明确 |
| 设问禁止问号（陈述句） | Specialist/Calculator/Writer 设问表达已加“禁止使用问号（？）” |
| 选择题题干作答占位括号（ ）只能出现一次（DeterministicFilter） | validate_writer_format 已校验：单选题/多选题下题干中占位括号出现次数须≤1 |

---

## 二、解耦原则（生成 vs 润色，避免过度耦合）

PRD 明确：**生成节点不承担硬规则细节校验**，年份/表格/图片/单引号/兜底选项/数值升序/**字数阈值**等由 **Writer 侧统一处理**；Specialist/Calculator 只产出 DraftV1，Writer 负责 Normalize → Validate → Polish。

据此做**职责切分**，避免同一规则在生成与润色两侧重复落地导致耦合：

| 类型 | 归属 | 说明 |
|------|------|------|
| **内容/语义/结构** | 生成节点可加 | 题目“写什么”：考点、术语、人名规范、不泄题、解析三段与目标题+分级、计算题题干保留位数（内容层）、违禁词/幻觉词/冗余场景。生成时做对比 Writer 事后改成本低。 |
| **格式/字数/呈现** | 仅 Writer + hard_rules | 题目“长什么样”：字数阈值（含 120 字建议）、连接词密度、选项长度均衡、选项 A/B/C/D 前缀、括号/标点/答案格式。PRD 约定由 Writer 统一校验与修复，生成节点**不再**加这些约束。 |
| **选项前缀 A./B./C./D.** | 仅 Writer | 已由 prepare_draft_for_writer 做 strip；如需与 Judge 口径一致，仅在 Writer 流程内对 strip 前的 draft 做一次检测并写入 issues，**不在生成节点加“禁止”**。 |

**结论**：下面“缺口与建议”已按上述原则修正——**生成节点只加内容/语义/结构类**；**120 字、连接词、选项均衡、选项前缀、计算题保留位数校验**等仅在 Writer/hard_rules 侧补齐，生成节点不重复加。

---

## 三、缺口与建议（按解耦原则修正后）

### 3.1 生成节点（Specialist / Calculator）——只加内容/语义/结构

| # | 离线 Judge 要求 | 建议（生成侧） |
|---|-----------------|----------------|
| 1 | **计算题：选项含小数时题干须标注“保留到几位小数”**（Judge 4.9，**内容**） | **仅 Calculator**：在初稿生成说明中加一条——当答案或选项含小数时，题干必须包含“保留到 X 位小数”或“精确到 X 位小数”。不放在 Specialist。 |
| 2 | **违禁词/双重否定：不是不、并非不**（DeterministicFilter） | **仅 Specialist**：在禁止项中显式加——禁止“不是不”“并非不”等双重否定/易歧义表述。Writer 已有“禁止双重否定句”，不重复写。 |
| 3 | **AI 幻觉词：外接、上交**（DeterministicFilter） | **仅 Specialist**（Calculator 若涉业务表述可带一句）：禁止“外接”“上交”，应使用“买方/受让方”“缴纳”等标准用语。 |
| 4 | **冗余场景：新人培训、通过中介买了房**（DeterministicFilter） | **仅 Specialist**：在“简化场景”中补充——避免“新人培训”“通过中介买了房”等冗余场景套话。Writer 已有“师傅告诉”等，可只在一处列全或 Writer 保留概括表述即可。 |
| 5 | **解析第一段“教材原文”须含目标题 + 分级**（Judge 8.2，**结构**） | **生成节点**（Specialist + Calculator）：在解析规范中加——第一段“教材原文”须包含目标题（知识点/教材路径最后一级标题）和分级（掌握/了解/熟悉）。Writer 提示词可写“须含目标题和分级”，hard_rules 已校验分级，目标题做弱校验或仅提示即可。 |

**不在生成节点加的项（避免与 Writer 耦合）**：

- ~~选项禁止 A/B/C/D 前缀~~ → 由 Writer Normalize strip + 可选 Validate 报 issue。
- ~~题干 120 字建议、连接词堆叠~~ → 属字数/呈现，仅 Writer 提示词 + 如需可做 warning。
- ~~选项长度均衡~~ → 仅 Writer/hard_rules。

### 3.2 润色节点（Writer）+ hard_rules——格式/字数/呈现/校验

| # | 离线 Judge 要求 | 建议（仅润色侧） |
|---|-----------------|------------------|
| 6 | **选项内容前禁止 A/B/C/D 标签**（Judge 4.6） | Writer 选项规范加一句：选项只填内容，禁止在内容前写 A./B./C./D. 标签。校验：在 _writer_validate_phase 前对**原始 draft**（未 strip）的 options 检测，若任一项以 A./B./C./D. 开头则写入 issues（如 HARD_OPTION_PREFIX）。prepare_draft_for_writer 继续做 strip，不改变现有流程。 |
| 7 | **题干简练 120 字、题干冗余连接词**（Judge 4.1，warning） | 仅在 Writer 题干/设问规范中加：题干建议 120 字以内；避免连接词堆叠。不在生成节点加。 |
| 8 | **选项长度均衡：最长与最短差≥15 时 warning**（Judge 4.6） | 仅在 hard_rules（或 Writer validate 链路）加：选项≥2 且 max(长度)−min(长度)≥15 时产出一条 warning。 |
| 9 | **计算题保留位数**（Judge 4.9，**校验**） | 仅在 hard_rules.validate_hard_rules 加：可选参数 `is_calculation`；当 is_calculation=True 且选项含小数且题干无“保留到X位小数/精确到X位小数”时追加 issue。调用处由 state 或上下文传入是否计算题。 |
| 10 | **解析第一段须含目标题 + 分级**（Judge 8.2） | Writer 解析规范显式写：第一段“教材原文”须包含目标题和分级。hard_rules 已有分级校验；目标题可弱校验或仅提示，避免与生成节点重复实现。 |

### 3.3 主项目 hard_rules 与 Judge 对齐（仅 Writer 调用）

| # | 规则 | 建议 |
|---|------|------|
| 11 | 计算题 + 选项含小数 ⇒ 题干须有保留位数说明 | 同 3.2 第 9 条，在 validate_hard_rules 中实现，由 Writer 调用时传入 is_calculation。 |
| 12 | 选项前缀 A/B/C/D | 在 Writer 流程内、prepare_draft_for_writer 之前检测并写 issues，不放入 hard_rules（因 strip 后传入 validate 的已无前缀）。 |

### 3.4 二次对照补充（本次新增补齐）

| # | 离线 Judge 要求 | 处理 |
|---|-----------------|------|
| 13 | **选项单位应上提题干**（Judge 5.1 option_unit_still_invalid） | 已在 Specialist/Calculator/Writer 选项规范中增加：选项不得包含数值单位（如元、平方米、年、%等），单位应写在题干中。 |
| 14 | **违禁兜底选项“全部正确”“全部错误”**（DeterministicFilter + REQUIREMENTS 27） | 已在 hard_rules banned_phrases 中增加“全部正确”“全部错误”。 |
| 15 | **第2段试题分析必须用自己的话，不得直接粘贴教材**（Judge 8.2、layer3_teaching_review） | 已在 Specialist/Calculator/Writer 解析规范中明确：第二段试题分析必须用自己的话解释，不得直接粘贴教材原文；严禁试题分析段整段粘贴教材原文。 |
| 16 | **设问禁止问号**（Judge DeterministicFilter _check_ask_pattern） | 已在 Specialist/Calculator/Writer 设问表达中增加：设问须用陈述句，禁止使用问号（？）；不得以疑问句形式设问。 |

### 3.5 三次对照补充（作答占位括号唯一）

| # | 离线 Judge 要求 | 处理 |
|---|-----------------|------|
| 17 | **选择题题干作答占位括号（ ）只能出现一次**（DeterministicFilter _check_brackets） | 已在 validate_writer_format 中增加：当 target_type 为单选题/多选题且题干中 BLANK_BRACKET 出现次数 >1 时，追加 issue「选择题题干作答占位括号（ ）只能出现一次」。 |

### 3.6 可选提示（Judge 语义项，生成侧可一句带过）

| # | 离线 Judge 要求 | 说明 |
|---|-----------------|------|
| 18 | **遣词造句**（Judge 5.1 wording_semantic_invalid：主谓搭配不当、指代错误） | Judge 由 LLM 语义判定。生成/润色提示词中可加一句「题干注意主谓搭配与指代一致，避免指代错误」以降低被 Judge 判错概率；非硬规则。 |
| 19 | **定义类判断题**（Judge 5.3：含“属于/是指/定义/概念”可不强制“XX做法正确/错误”模板） | Judge 侧为语义例外。当前生成端统一要求「XX做法正确/错误」；若希望与 Judge 完全一致，可在判断题规范中补一句「定义类判断题（含属于/是指/定义/概念）可用其他表述」。 |

### 3.7 DeterministicFilter 中未体现的规则（可选对齐）

| # | 规则 | 说明 |
|---|------|------|
| 20 | 得房率 <70% → warning | 常识类；可考虑在 Writer 或独立校验中作为可选 warning。 |
| 21 | 复式层高 >6 米 → warning | 同上。 |
| 22 | 层数与建筑高度一致性（平均层高 2.8~6.0m） | Judge 用环境变量 MIN/MAX_RESIDENTIAL_FLOOR_HEIGHT_M；主项目未实现，可按需在 hard_rules 或 Critic 侧加。 |
| 23 | 题干城市与教材城市一致（地理一致性） | Writer 已有“地理继承”“严禁无关城市”，与 Judge 意图一致。 |
| 24 | 选项层级冲突（父子类选项） | Judge 为 warning、需结合教材；生成/润色未做代码级检测，可保留由 Judge 负责。 |

---

## 四、规则来源索引（便于回溯）

- **Judge 硬规则**：`离线Judge/src/pipeline/graph.py` 中 `_basic_rules_code_checks`（约 121–332 行）。
- **Judge 选项/设问/解析**：`离线Judge/REQUIREMENTS.md` 第 4 节（字数、年份、设问、标点、图片表格、选项、答案、解析、计算题保留位数）。
- **Judge 语义/基础规则**：`离线Judge/prompts/layer3_basic_rules_gate.md`（设问、选项代入、姓名、否定、判断题句式、选项单位、遣词造句等）。
- **Judge DeterministicFilter**：`离线Judge/src/filters/deterministic_filter.py`（违禁词、幻觉词、冗余场景、称谓、得房率、层高、地理、泄题关键词等）。
- **主项目生成/润色**：`exam_graph.py` 中 specialist_node、calculator_node、writer_node 的 prompt 与 validate/normalize 逻辑；`hard_rules.py` 中 validate_hard_rules、validate_media_rules 等。

---

## 五、建议实施顺序（按解耦原则）

1. **高优先级（与 Judge REJECT/error 相关，且职责清晰）**  
   - **仅 Writer**：选项前缀在 strip 前检测并写 issues（3.2-6）；计算题保留位数在 hard_rules 中校验（3.2-9、3.3-11）。  
   - **仅 Calculator**：计算题题干保留位数在初稿生成时要求（3.1-1）。  

2. **中优先级（Judge warning / 内容结构）**  
   - **仅 Writer**：题干 120 字建议与连接词、选项长度均衡（3.2-7、3.2-8）；解析规范目标题+分级（3.2-10）。  
   - **生成节点**：解析第一段目标题+分级（3.1-5）。  

3. **低优先级（语义/内容，仅生成节点）**  
   - **仅 Specialist**：违禁词“不是不、并非不”、幻觉词“外接/上交”、冗余场景“新人培训、通过中介买了房”（3.1-2、3.1-3、3.1-4）。  

**不做的（避免耦合）**：不在生成节点加 120 字、连接词密度、选项均衡、选项前缀等格式类约束；这些只在 Writer + hard_rules 侧落地。

完成修改后，建议用离线 Judge 对同一批题目再跑一轮，确认 PASS/REVIEW/REJECT 与预期一致，并视情况补充 tdd_spec/task_spec。

---

## 六、再次对照检查（逐条无遗漏）

以下按**离线 Judge 代码与文档**逐条列出，并标注生成节点（Specialist/Calculator）与润色节点（Writer）+ hard_rules 的对应情况。**不得有任何遗漏。**

### 6.1 Judge _basic_rules_code_checks（graph.py 119–334）

| # | Judge 规则 | 类型 | 生成节点 | 润色/hard_rules | 状态 |
|---|------------|------|----------|------------------|------|
| 1 | 题干字数 >400 → error | 硬 | 不要求（解耦） | hard_rules HARD_STEM_LEN | ✅ 已对齐 |
| 2 | 题干 >120 字 → warning 简练提醒 | 硬 | 不要求 | Writer 提示词「题干建议120字以内」 | ✅ 已对齐 |
| 3 | 题干连接词密度（≥4 且密度≥0.04）→ warning | 硬 | 不要求 | Writer 提示词「避免连接词堆叠」 | ✅ 已对齐 |
| 4 | 年份约束：教材无年份则题干/选项/解析不得出现公历年份 → warning | 硬 | 不要求 | hard_rules HARD_YEAR（题干/解析） | ✅ 已对齐 |
| 5 | 单选题设问结尾「以下表述正确/错误的是（ ）。」 | 硬 | Specialist/Calculator 有固定结尾说明 | validate_question_template_semantics + 提示词 | ✅ 已对齐 |
| 6 | 多选题设问结尾「正确/错误的有/包括（ ）。」 | 硬 | 同上 | 同上 | ✅ 已对齐 |
| 7 | 题干括号不能在句首 → error | 硬 | 提示词有 | validate 相关逻辑 | ✅ 已对齐 |
| 8 | 选择题题干必须以「（ ）。」结尾（全角括号+空格） | 硬 | 提示词有 | validate_writer_format | ✅ 已对齐 |
| 9 | 判断题必须以「。（ ）」结尾 | 硬 | 提示词有 | validate_writer_format | ✅ 已对齐 |
| 10 | 题干/选项禁单引号 → error | 硬 | 不要求 | hard_rules HARD_SINGLE_QUOTE；replace_single_quotes_in_final_json | ✅ 已对齐 |
| 11 | 题干/选项禁图片、表格 → error | 硬 | 不要求 | validate_media_rules；sanitize_media_payload | ✅ 已对齐 |
| 12 | 选项字数 >200 → error | 硬 | 不要求 | hard_rules HARD_OPTION_LEN | ✅ 已对齐 |
| 13 | 选项末尾禁标点 → error | 硬 | 提示词「选项末尾不添加标点」 | prepare_draft_for_writer + validate | ✅ 已对齐 |
| 14 | 禁「以上都对/都错/皆是/皆非/以上选项全对/全错」→ error | 硬 | 提示词有禁用兜底选项 | hard_rules banned_phrases | ✅ 已对齐 |
| 15 | 判断题 2 选项、选择题 4 选项 → error | 硬 | 题型指令 | prepare_draft_for_writer + 题型约束 | ✅ 已对齐 |
| 16 | 选项内容前禁止 A/B/C/D 标签 → error | 硬 | 不要求（解耦） | _detect_option_prefix_in_draft + Writer 提示词「只填选项正文，禁止 A./B./C./D.」 | ✅ 已对齐 |
| 17 | 数值选项按从小到大升序 → warning | 硬 | 提示词有 | hard_rules HARD_NUMERIC_ORDER；apply_numeric_options_ascending | ✅ 已对齐 |
| 18 | 选项长度均衡：max−min≥15 → warning | 硬 | 不要求 | hard_rules HARD_OPTION_BALANCE | ✅ 已对齐 |
| 19 | 答案字段：单选单字母、多选多字母、判断 A/B → error | 硬 | 题型约束 | validate_writer_format + hard_rules | ✅ 已对齐 |
| 20 | 解析三段结构（教材原文/试题分析/结论）→ error | 硬 | 解析规范有三段式 | hard_rules HARD_EXPL_STRUCT | ✅ 已对齐 |
| 21 | 解析结论「本题答案为…」→ error | 硬 | 解析规范有 | hard_rules 结论校验 | ✅ 已对齐 |
| 22 | 判断题结论必须写「正确/错误」不能写 A/B → error | 硬 | 解析规范有 | hard_rules 判断题结论 | ✅ 已对齐 |
| 23 | 解析结论与正确答案字段一致 → error | 硬 | 一致性要求 | hard_rules 结论与答案一致 | ✅ 已对齐 |
| 24 | 解析字数 >400 → warning | 硬 | 不要求 | hard_rules HARD_EXPL_LEN | ✅ 已对齐 |
| 25 | 解析禁图片/表格 → error | 硬 | 不要求 | validate_media_rules | ✅ 已对齐 |
| 26 | 计算题 + 选项含小数 ⇒ 题干须有「保留到X位小数/精确到X位小数」→ error | 硬 | **Calculator** 提示词有「保留位数说明（必须）」 | hard_rules HARD_CALC_PRECISION（is_calculation 传入） | ✅ 已对齐 |

### 6.2 Judge DeterministicFilter（未接入主 pipeline，规则对齐即可）

| # | 规则 | 生成节点 | 润色/hard_rules | 状态 |
|---|------|----------|------------------|------|
| 27 | 违禁词：最重要、实实在在、**不是不、并非不** | Specialist/Calculator 均有「禁止“不是不”“并非不”等易歧义表述」 | Writer 有「禁止双重否定句」 | ✅ 已对齐 |
| 28 | AI 幻觉词：外接、上交 | Specialist 有「禁止“外接”“上交”，应使用买方/受让方、缴纳」 | 未重复要求 | ✅ 已对齐 |
| 29 | 冗余场景：师傅告诉徒弟、新人培训、通过中介买了房 | Specialist 有「避免新人培训、通过中介买了房等冗余场景套话」+「师傅告诉」示例 | Writer 有「师傅告诉」「新人训」示例 | ✅ 已对齐 |
| 30 | 违禁兜底：全部正确、全部错误 | 提示词禁用兜底选项 | hard_rules banned_phrases 已含 | ✅ 已对齐 |
| 31 | 选项层级冲突（父子类）→ warning | 无 | 无（报告 3.7：可保留由 Judge 负责） | ✅ 可选不实现 |
| 32 | 选择题题干作答占位括号（ ）只能出现一次 | 不要求 | validate_writer_format 已校验 BLANK_BRACKET 出现次数≤1 | ✅ 已对齐 |
| 33 | 设问禁止问号（？） | Specialist/Calculator/Writer 均有「禁止使用问号（？）；不得以疑问句形式设问」 | 同上 | ✅ 已对齐 |
| 34 | 得房率 <70%、层高 >6 米、层数与建筑高度 | 无 | 无（报告 3.7：可选 warning） | ✅ 可选不实现 |

### 6.3 Judge REQUIREMENTS 4.6 / 4.9 / 8.2

| # | 要求 | 生成节点 | 润色/hard_rules | 状态 |
|---|------|----------|------------------|------|
| 35 | 选项内容前禁止 A/B/C/D 标签（4.6） | 解耦至 Writer | _detect_option_prefix_in_draft + Writer 提示词 | ✅ 已对齐 |
| 36 | 计算题选项含小数时题干须保留位数说明（4.9） | Calculator 有「保留位数说明（必须）」 | hard_rules HARD_CALC_PRECISION | ✅ 已对齐 |
| 37 | 解析第 1 段须含**目标题 + 分级**（8.2） | Specialist/Calculator 解析规范均有「第一段须包含目标题和分级」 | Writer 解析规范 + hard_rules 分级校验（HARD_EXPL_TEXTBOOK）；目标题仅提示未做硬校验 | ✅ 已对齐 |
| 38 | 解析第 2 段必须用自己的话，不得直接粘贴教材（8.2） | Specialist/Calculator/Writer 均有「第二段必须用自己的话…不得直接粘贴教材原文」 | 同上 | ✅ 已对齐 |

### 6.4 Judge layer3_basic_rules_gate（LLM 语义项）

| # | 语义项 | 生成/润色侧 | 状态 |
|---|--------|-------------|------|
| 39 | 选项单位应上提题干（option_unit_still_invalid） | Specialist/Calculator/Writer 选项规范均有「选项不得包含数值单位，单位应写在题干中」 | ✅ 已对齐 |
| 40 | 遣词造句（主谓搭配、指代错误） | 报告 3.6：可一句带过 | 可选：在题干规范中加「注意主谓搭配与指代一致」 |
| 41 | 定义类判断题例外（属于/是指/定义/概念可不强制「XX做法正确/错误」） | 当前统一要求「XX做法正确/错误」 | 可选：判断题规范补一句定义类例外（报告 3.6-19） |

### 6.5 主项目特有（与 Judge 一致）

| # | 项 | 状态 |
|---|-----|------|
| 42 | 专有名词锁词 term_locks | Specialist/Calculator/Writer/Fixer 均有 term_lock_text；detect_term_lock_violations | ✅ 已对齐 |
| 43 | 人名规范（先生/女士、小+姓、恶搞名等） | 提示词 + validate_name_usage | ✅ 已对齐 |
| 44 | 地理继承、严禁无关城市 | Writer 提示词 | ✅ 已对齐 |
| 45 | 时间逻辑（原文未给时间则不写具体年份） | Writer 提示词 | ✅ 已对齐 |
| 46 | 泄题判定从严（仅当正确答案原样在题干） | Critic 提示词 | ✅ 已对齐 |

---

## 七、本次检查结论与剩余缺口

### 7.1 结论

- **绝大部分规则已对齐**：Judge 的 _basic_rules_code_checks、REQUIREMENTS 4/8、DeterministicFilter 中与主 pipeline 相关的项，在生成节点（Specialist/Calculator）与润色节点（Writer）+ hard_rules 中均有对应实现或提示词约束。
- **解耦原则已遵守**：格式/字数/标点/选项前缀/数值升序/计算题保留位数校验等均在 Writer + hard_rules 侧落地；生成节点仅做内容/语义/结构类约束。

### 7.2 剩余缺口与建议

| 优先级 | 缺口 | 建议 |
|--------|------|------|
| ~~**高**~~ | ~~Calculator 未显式禁止「不是不」「并非不」~~ | **已落实**：Calculator 节点「设问表达」已增加「禁止“不是不”“并非不”等易歧义表述」。 |
| 低 | 遣词造句（主谓搭配、指代） | 在题干/设问规范中加一句「题干注意主谓搭配与指代一致，避免指代错误」（可选，降低 Judge wording_semantic_invalid 概率） |
| 低 | 定义类判断题例外 | 在判断题规范中补一句「定义类判断题（含属于/是指/定义/概念）可用其他表述」（可选，与 Judge 5.3 完全一致） |

### 7.3 无需补充的项

- 得房率 <70%、层高 >6 米、层数与建筑高度：Judge 为可选/环境变量；主项目可按需在 Writer 或 Critic 侧加 warning，非必须。
- 选项层级冲突：Judge 为 warning 且需结合教材；主项目可保留由 Judge 负责。
- 目标题硬校验：hard_rules 仅做分级校验；目标题在解析规范中已提示，不做代码级硬校验符合报告 3.2-10。

**当前状态**：生成节点与润色节点与离线 Judge 的规则对齐已**无遗漏**（高优先级项已补齐）。上述低优先级可选项可根据后续 Judge 跑题结果再决定是否补充。
