# 智能出题系统 - 技术设计文档 (TDD)

## 1. 文档信息

- **文档版本**: v1.0
- **创建日期**: 2025-01-27
- **基于需求文档**: prd.md v1.0
- **维护者**: 搏学考试团队

## 2. 系统概述

### 2.1 系统定位

智能出题系统是一个基于 LangGraph 多智能体协同架构的自动化出题平台，采用"照猫画虎"（Few-Shot Learning）策略，通过分析知识点和母题库，自动生成高质量的房地产经纪人培训考试题目。

### 2.2 核心能力

- **智能出题**: 基于知识库自动生成题目，支持批量生成（1-200题）
- **多智能体协同**: 6个智能体节点分工协作，确保题目质量
- **计算器集成**: 支持16种房地产专业计算（税费、贷款、面积等，当前实现）
- **质量保障**: 多轮验证机制，确保题目准确率 ≥ 95%
- **风格统一**: 参考历史母题，确保题目风格一致

### 2.3 技术特点

- **多模型支持**: 支持 OpenAI/DeepSeek/Ark
- **流式输出**: 实时展示生成过程，提升用户体验
- **自适应反馈**: 智能错误修复和重试机制
- **安全执行**: 沙箱环境执行计算代码，确保系统安全

## 3. 技术架构

### 3.1 技术栈

```
前端展示层: Streamlit (Python Web框架)
智能体编排: LangGraph (LangChain生态)
LLM 接口: 
  - Ark/OpenAI兼容
  - OpenAI API (兼容DeepSeek)
  - OpenAI兼容API
数据处理: 
  - Pandas (Excel/CSV处理)
  - Scikit-learn (TF-IDF向量化)
  - JSON/JSONL (知识库存储)
知识检索: 
  - 知识点映射文件 (question_knowledge_mapping.json)
  - BGE语义向量检索 (回退机制)
计算工具: 自定义Python计算器 (RealEstateCalculator)
数据验证: Pydantic (数据模型验证)
```

### 3.2 系统分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                    用户交互层 (UI Layer)                      │
│  File: app.py                                                │
│  - Streamlit Web界面                                         │
│  - API配置管理                                               │
│  - 章节选择（多选/全选/仅计算类）                            │
│  - 出题参数设置（数量/难度/题型/模式）                        │
│  - 实时生成状态展示                                          │
│  - 流式事件处理与UI渲染                                      │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                  应用编排层 (Orchestration Layer)             │
│  File: app.py                                                │
│  - 初始化 KnowledgeRetriever                                 │
│  - 构建 LangGraph inputs                                     │
│  - 调用 exam_graph.app 执行工作流                            │
│  - 处理流式输出事件                                          │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                  智能体编排层 (Agent Layer)                   │
│  File: exam_graph.py                                         │
│  - LangGraph StateGraph 工作流定义                           │
│  - 6个智能体节点实现                                         │
│  - 状态管理和传递                                            │
│  - 条件路由逻辑                                              │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                  知识检索层 (Retrieval Layer)                 │
│  File: exam_factory.py (KnowledgeRetriever)                 │
│  - 知识库加载 (bot_knowledge_base.jsonl)                    │
│  - 母题库加载 (存量房买卖母卷ABCD.xls)                       │
│  - 知识点映射文件加载 (question_knowledge_mapping.json)      │
│  - 母题检索（映射优先，BGE回退）                             │
│  - TF-IDF向量化（回退机制）                                  │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                  计算工具层 (Calculator Layer)                │
│  File: calculation_logic.py (RealEstateCalculator)           │
│  - 16种房地产专业计算函数（当前实现）                        │
│  - 统一函数接口和参数规范                                    │
│  - 代码执行安全机制（沙箱环境）                              │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 核心组件

#### 3.3.1 用户交互层 (app.py)

**职责**:
- 提供 Streamlit Web 界面
- 管理用户配置（API Key、模型选择、代理设置）
- 处理用户输入（章节选择、出题参数）
- 实时展示生成过程和结果
- 处理流式输出事件

**关键函数**:
- `get_retriever()`: 初始化并缓存 KnowledgeRetriever
- `generate_questions()`: 调用 LangGraph 工作流生成题目
- `display_question_generation()`: 展示生成过程和结果

**章节选择功能** (FR1.1):
- **章节范围选择**:
  - 全部章节（默认）：选择所有知识点
  - 自定义章节（支持多选）：用户可多选特定章节
  - 仅计算类章节（自动筛选）：根据关键词（计算、税费、贷款、建筑指标、面积）自动筛选
- **掌握程度筛选**:
  - 全部掌握程度（默认）：不限制掌握程度
  - 自定义掌握程度（支持多选）：用户可选择"了解"/"熟悉"/"掌握"
  - 显示选中范围的知识点数量和掌握程度分布

**测试点**:
- **TP23.1 章节选择**: 应正确筛选知识点
  - 断言: 选择"仅计算类章节"时，筛选的知识点包含"计算"、"税费"、"贷款"等关键词
  - 测试数据: 选择"仅计算类章节"
- **TP23.2 掌握程度筛选**: 应正确筛选知识点
  - 断言: 选择"掌握"时，筛选的知识点掌握程度为"掌握"
  - 测试数据: 选择掌握程度"掌握"

**批量生成功能** (FR1.2):
- **出题范围模式**:
  - **每个知识点各出一题模式**（默认）：
    - 自动匹配知识点数量
    - 题型/难度/模式采用默认值（题型单选题，难度随机，模式灵活）
  - **自定义模式**：
    - 用户指定数量（1-200题）
    - 可自定义题型、难度、模式

#### 3.3.2 智能体编排层 (exam_graph.py)

**职责**:
- 定义 LangGraph 工作流
- 实现6个智能体节点
- 管理状态传递和条件路由
- 处理反馈循环机制

**核心节点**:
1. **router_node**: 路由决策节点
2. **specialist_node**: 非计算专家节点
3. **calculator_node**: 计算专家节点
4. **writer_node**: 格式化标准化节点
5. **critic_node**: 质量验证节点
6. **fixer_node**: 错误修复节点

#### 3.3.3 知识检索层 (exam_factory.py)

**职责**:
- 加载知识库和母题库
- 实现母题检索逻辑
- 支持知识点映射和语义检索
- 数据质量过滤

**核心类**: `KnowledgeRetriever`

**关键方法**:
- `get_examples_by_knowledge_point()`: 基于知识点检索母题
- `_is_valid_example()`: 数据质量过滤
- `_get_question_type()`: 题型识别
- `_matches_question_type()`: 题型匹配

#### 3.3.4 计算工具层 (calculation_logic.py)

**职责**:
- 提供16种房地产专业计算函数（当前实现）
- 确保计算逻辑准确
- 提供统一的函数接口

**核心类**: `RealEstateCalculator`

**计算函数列表**:
1. `calculate_loan_amount()`: 商业贷款金额计算
2. `calculate_provident_fund_loan()`: 公积金贷款计算
3. `calculate_vat()`: 增值税及附加计算（税率5.3%）
4. `calculate_deed_tax()`: 契税计算
5. `calculate_income_tax()`: 个人所得税计算
6. `calculate_loan_payment()`: 贷款月供计算
7. `calculate_building_area()`: 建筑面积计算
8. `calculate_floor_area_ratio()`: 容积率计算
9. `calculate_house_age()`: 房龄计算（支持两种模式）
10. `calculate_land_transfer_fee()`: 土地出让金计算
11. 等共16种工具

## 4. 数据模型

### 4.1 状态模型 (AgentState)

**定义位置**: `exam_graph.py`

```python
class AgentState(TypedDict):
    kb_chunk: Dict[str, Any]  # 知识点切片
    question_type: str  # 题型：single/multi/judge（config初始值）
    current_question_type: Optional[str]  # 当前实际题型（Writer确定后传递给后续节点）
    difficulty_range: Tuple[float, float]  # 难度范围：(min, max)
    mode: str  # 出题筛选条件：基础概念/理解记忆、实战应用/推演、随机
    examples: List[Dict[str, Any]]  # 母题范例列表
    agent_name: str  # 派发的专家：CalculatorAgent/LegalAgent/GeneralAgent
    draft: Optional[Dict[str, Any]]  # 初稿（JSON格式）
    final_json: Optional[Dict[str, Any]]  # 最终格式化后的题目
    critic_feedback: Optional[Dict[str, Any]]  # Critic评审结果
    critic_model_used: Optional[str]  # Critic实际使用的模型（用于UI显示）
    calculator_model_used: Optional[str]  # Calculator实际使用的模型（用于UI显示）
    prev_final_json: Optional[Dict[str, Any]]  # 上一次的final_json（用于重路由）
    prev_critic_feedback: Optional[Dict[str, Any]]  # 上一次的critic_feedback（用于重路由）
    retry_count: int  # 重试次数
    logs: List[str]  # 日志列表
    term_locks: List[str]  # 术语锁定清单（关键词召回+语义语境一致）
    calculation_code: Optional[str]  # 计算代码（calculator_node生成）
    calculation_result: Optional[Any]  # 计算结果
    calculation_function: Optional[str]  # 使用的计算函数名
    calculation_params: Optional[Dict[str, Any]]  # 计算参数
```

### 4.2 题目模型 (ExamQuestion)

**定义位置**: `exam_factory.py`

```python
class ExamQuestion(BaseModel):
    题干: str = Field(..., description="The question stem")
    选项1: str = Field(..., description="Option A")
    选项2: str = Field(..., description="Option B")
    选项3: str = Field("", description="Option C")
    选项4: str = Field("", description="Option D")
    选项5: str = Field("", description="Option E")
    选项6: str = Field("", description="Option F")
    选项7: str = Field("", description="Option G")
    选项8: str = Field("", description="Option H")
    正确答案: str = Field(..., pattern="^[ABCDEFGH]+$", description="Correct answer")
    解析: str = Field(..., description="Structured explanation")
    难度值: float = Field(..., ge=0, le=1, description="Difficulty 0.0-1.0")
```

**自动生成字段**（在app.py中补充）:
- `考点`: 从 `kb_chunk['完整路径']` 获取
- `一级知识点`、`二级知识点`、`三级知识点`、`四级知识点`: 从 `完整路径` 按 `>` 分割后提取
- `来源路径`: 用于展示和导出
- `_was_fixed`: 布尔值，标记是否经过Fixer修复
- `是否修复`: 字符串（"是"/"否"），用于UI展示

### 4.3 知识库数据模型

**文件**: `bot_knowledge_base.jsonl` (1712条)

**必需字段**:
- `完整路径`: 字符串，知识点层级路径（如"第一篇 > 第一章 > 第一节 > ..."）
- `核心内容`: 字符串，知识点核心文本内容
  - 如果缺失，从 `结构化内容` 自动构建

**可选字段**:
- `掌握程度`: 字符串，如"了解"/"熟悉"/"掌握"或"未知"
- `结构化内容`: 字典，包含：
  - `context_before`: 上下文前置文本
  - `context_after`: 上下文后置文本
  - `tables`: 核心表格数据列表
  - `formulas`: 核心公式列表
  - `examples`: 教材原题列表（知识点切片内置的题目示例，最高优先级参考）
  - `key_params`: 关键参数列表（可选）
- `Bot专用切片`: 字符串，格式化后的文本（可选）

**切片展示规则**:
- UI 展示顺序：`context_before` → `tables` → `context_after` → `examples` → `formulas` → `images`
- `examples` 必须独立区块展示，不得嵌入表格

### 4.4 母题库数据模型

**文件**: `存量房买卖母卷ABCD.xls` (408道)

**必需字段**（用于数据质量过滤）:
- `题干`: 字符串，题目题干
- `选项1`: 字符串，选项A
- `选项2`: 字符串，选项B
- `正确答案`: 字符串，正确答案（A-H或组合）
- `解析`: 字符串，题目解析

**可选字段**:
- `选项3-8`: 字符串，选项C-H（可选）
- `考点`: 字符串，知识点路径
- `难度值`: 浮点数，题目难度（0.0-1.0）

**数据质量过滤**: 自动过滤NaN值和空字符串

### 4.5 知识点映射数据模型

**文件**: `question_knowledge_mapping.json`

**结构**:
```json
{
  "0": {
    "完整路径": "第一篇 > 第一章 > ...",
    "掌握程度": "掌握",
    "matched_questions": [
      {
        "question_index": 3,
        "confidence": 0.95,
        "method": "GPS_FullPath",
        "evidence": {
          "reason": "路径完全匹配：第四篇/第四章/第二节/个人所得税计算"
        }
      }
    ],
    "total_matches": 2,
    "methods_used": ["GPS_FullPath", "LLM_Logic"]
  }
}
```

**用途**: 优先使用映射文件匹配母题，无映射时回退到BGE语义向量检索

### 动态意图约束（仅特定题型触发）
- 仅当题干被识别为 `ENUMERATION`（完整清单/资料包括）或 `EXCLUSION`（不需要/不包括）时触发强校验
- 允许无标签题；无标签题不触发动态约束
- 禁止 “SELECTION（单点选择/最XX/关键）” 类题型
- 语义覆盖允许改写，但专有名词必须原文一致
- 专有名词命中规则：关键词召回 + 语义语境一致（双条件）
- 专有名词库主文件：`房地产行业专有名词新.xlsx`（可预处理缓存为 `教材提取专有名词.txt`）

## 5. 工作流设计

### 5.1 LangGraph 工作流架构

**工作流定义**: `exam_graph.py` 中的 `create_exam_graph()`

**节点列表**（6个节点）:
1. `router_node`: 路由决策
2. `specialist_node`: 非计算专家节点
3. `calculator_node`: 计算专家节点
4. `writer_node`: 格式化标准化
5. `critic_node`: 质量验证
6. `fixer_node`: 错误修复

**工作流路径**:
```
入口: router_node
  ↓
router_node → calculator_node (如果检测到计算需求)
  OR
router_node → specialist_node (如果非计算类)
  ↓
calculator_node/specialist_node → writer_node
  ↓
writer_node → critic_node
  ↓
critic_node → critical_decision (条件路由)
  ├── pass → END (通过，结束)
  ├── fix → fixer_node (轻微问题，修复)
  ├── reroute → router_node (严重问题，重新路由)
  └── self_heal → END (重试≥3次，自愈输出)
  ↓
fixer_node → critic_node (形成循环)
```

**测试点**:
- **TP30.1 完整工作流路径** (FR3.6): 应能正确执行完整工作流
  - 断言: 工作流从router_node开始，按正确路径执行到END，所有节点按顺序执行
  - 测试数据: 有效的kb_chunk和config
- **TP30.2 条件路由**: 应根据条件正确路由
  - 断言: 计算类知识点路由到calculator_node，非计算类路由到specialist_node
  - 测试数据: 包含公式的知识点和纯文本知识点
- **TP30.3 反馈循环**: 应正确执行反馈循环
  - 断言: Critic失败后能正确触发Fixer或Router重路由
  - 测试数据: Critic标记为问题的情况

### 5.2 节点详细设计

#### 5.2.1 Router Node (路由节点)

**职责**:
- 分析知识点类型（计算类/法律类/综合类）
- 提取掌握程度
- 决定派发的专家（CalculatorAgent/LegalAgent/GeneralAgent）
- 检测重路由并清理旧状态

**输入**: `AgentState` (包含 `kb_chunk`)

**输出**: 更新 `AgentState` 的 `agent_name` 字段

**特征检测**:
- 检测是否包含公式（formulas）
- 检测是否包含表格（tables）
- 检测是否包含列表（编号列表如（1）（2）或1. 2.）
- 检测切片中的专有名词候选（关键词召回）并做语义语境判定，输出 `term_locks`

**路由决策（实际架构：两个专家节点）**:
- **计算类** → `CalculatorAgent`/`FinanceAgent` → `calculator_node`（计算专家节点）
  - 触发条件：包含公式或需要数值计算
- **非计算类** → `LegalAgent`/`GeneralAgent` → `specialist_node`（非计算专家节点）
  - LegalAgent：涉及法律条文、罚则、年限规定
  - GeneralAgent：处理概念、流程、业务常识
  - 注意：虽然Router会区分LegalAgent和GeneralAgent，但实际都走specialist_node处理，根据agent_name调整提示词风格

**题型推荐**:
- 包含公式 → 推荐单选题
- 包含列表 → 推荐多选题
- 包含表格 → 推荐判断题

**重路由检测**:
- 检测 `prev_final_json` 和 `prev_critic_feedback` 是否存在
- 如果存在，清理旧状态，保留必要的上下文
- 同步保留并透传 `term_locks`

**测试点**:
- **TP1.1 特征检测**: 包含公式的知识点应派发到CalculatorAgent
  - 断言: `agent_name == "CalculatorAgent"` 当 `kb_chunk['结构化内容']['formulas']` 存在
  - 测试数据: kb_chunk包含formulas字段
- **TP1.2 题型推荐**: 包含表格的知识点应推荐判断题
  - 断言: `router_details['recommended_type'] == "判断题"` 当 `kb_chunk['结构化内容']['tables']` 存在
  - 测试数据: kb_chunk包含tables字段
- **TP1.3 重路由检测**: 重路由时应清理旧状态但保留prev_final_json
  - 断言: `state['draft'] is None` 且 `state['prev_final_json'] is not None` 当 `retry_count > 0`
  - 测试数据: retry_count > 0 且存在prev_final_json
- **TP1.4 掌握程度提取**: 应从kb_chunk正确提取掌握程度
  - 断言: `router_details['mastery'] == kb_chunk.get('掌握程度', '未知')`
  - 测试数据: kb_chunk包含`掌握程度`字段（了解/熟悉/掌握）
- **TP1.5 计算需求自动判断** (FR4.1): 应自动判断是否需要计算
  - 断言: 如果知识点包含公式或需要数值计算，则`agent_name == "CalculatorAgent"`
  - 测试数据: 知识点包含`formulas`字段或需要数值计算
- **TP1.6 专有名词语义命中**: 仅“关键词命中+语义一致”才进入 `term_locks`
  - 断言: `term_locks` 不包含同词不同义的误命中项
  - 测试数据: 构造同词不同义/同词同义样本

#### 5.2.2 Specialist Node (非计算专家节点)

**职责**:
- 处理法律、政策、综合知识类题目
- 根据知识点和母题范例生成初稿
- 根据 `agent_name`（LegalAgent/GeneralAgent）调整提示词风格

**输入**: `AgentState` (包含 `kb_chunk`, `examples`, `question_type`, `difficulty_range`, `mode`)

**输出**: 更新 `AgentState` 的 `draft` 字段

**关键约束**:
- 严格遵循题型、模式、难度约束
- 数据重构：禁止直接照搬原文案例中的具体数据
- 按筛选条件执行：基础概念/理解记忆可非场景化，实战应用/推演必须场景化，随机自动选择
- `term_locks` 命中术语必须原词输出，不得同义改写或缩写替换
 - **题型格式规范**：
   - 判断题：选项固定为“正确/错误”，答案仅 A/B
   - 单选题：4个选项且仅1个正确答案
   - 多选题：至少4个选项且至少2个正确答案
   - 括号规范（判断/选择题统一）：中文括号“（ ）”、括号前后无空格、括号内为全角空格

**测试点**:
- **TP2.1 教材原题优先**: 应优先使用builtin_examples
  - 断言: `examples[0]` 来自 `kb_chunk['结构化内容']['examples']`（如果存在）
  - 测试数据: kb_chunk包含`结构化内容.examples`
- **TP2.2 掌握程度约束**: 提示词中应包含掌握程度信息
  - 断言: 生成的prompt包含`掌握程度要求为: 【{mastery}】`
  - 测试数据: kb_chunk包含`掌握程度`字段（了解/熟悉/掌握）
- **TP2.3 题型约束**: 生成的题目应符合指定题型
  - 断言: 单选题有4个选项且答案长度为1，多选题答案长度>1，判断题选项为["正确","错误"]
  - 测试数据: `question_type = "单选题"/"多选题"/"判断题"`
- **TP2.4 筛选条件约束**: 三档筛选条件应被严格执行
  - 断言: 基础概念/理解记忆包含"不强制业务场景"；实战应用/推演包含"必须关联业务场景"；随机模式包含"自动选择两者之一"
  - 测试数据: `generation_mode = "基础概念/理解记忆"/"实战应用/推演"/"随机"`
 - **TP2.5 括号规范**: 判断/选择题的括号格式必须为中文括号且内部有空格
   - 断言: 题干中出现占位括号时，必须为 `（ ）` 且括号前后无空格
   - 测试数据: 含答案占位括号的题干
- **TP2.6 专有名词原词锁定**: 命中术语必须原词出现
  - 断言: `draft` 中命中术语字符串与 `term_locks` 一致
  - 测试数据: 含可替换同义词的术语场景

#### 5.2.3 Calculator Node (计算专家节点)

**职责**:
- **模型智能切换策略**（与 Critic 相同）：
  - 限流检测：读取 `.gpt_rate_limit.txt`，检测 GPT 限流状态
  - 切换决策：如果距离上次调用 < 10 秒且需等待 > 5 秒，切换到 Deepseek
  - 状态传递：将实际使用的模型存入 `state['calculator_model_used']`
  - 日志输出：切换时打印提示信息
- 动态生成Python计算代码
- 执行计算获取结果
- 根据计算结果和母题范例生成初稿
- **题型与括号格式**：遵循判断/单选/多选结构规范，并使用中文括号“（ ）”且括号内为全角空格
- **术语锁定**：`term_locks` 命中术语必须按原词写入题干/选项/解析

**输入**: `AgentState` (包含 `kb_chunk`, `examples`, `question_type`, `difficulty_range`, `mode`)

**输出**: 更新 `AgentState` 的 `draft`, `calculation_code`, `calculation_result`, `calculation_function`, `calculation_params`, `calculator_model_used` 字段

**计算代码生成**:
- 从题干或参考材料中提取具体数值（必须是数字，不能是描述性文字）
- 严格按照教材规则编写计算逻辑
- 处理边界情况（如除零检查、条件判断）
- 最后将结果赋值给变量 `result`

**代码执行**:
- 在沙箱环境中执行
- 限制可导入的模块（仅允许数学和时间相关模块）
- 执行时间参数（默认5秒，当前未实现强制中断）
- 异常捕获和错误处理

**测试点**:
- **TP3.1 代码生成**: 应生成有效的Python代码
  - 断言: `calculation_code` 是有效的Python代码字符串，包含`result =`赋值
  - 测试数据: 知识点包含计算规则（如契税计算）
- **TP3.2 沙箱执行安全**: 应限制可导入的模块
  - 断言: 尝试导入`os`模块应抛出ImportError
  - 测试数据: 代码包含`import os`
- **TP3.3 执行时间参数传递**: 代码执行函数应接收并使用 max_execution_time 参数
  - 断言: 调用代码执行函数时默认传入`max_execution_time=5.0`
  - 测试数据: 计算类知识点触发代码执行
- **TP3.4 参数提取**: 应提取具体数值而非描述性文字
  - 断言: 提取的参数是数字类型（int/float），不是字符串描述
  - 测试数据: 题干包含"80平方米"、"1560元"等具体数值
- **TP3.5 掌握程度约束**: 提示词中应包含掌握程度信息
  - 断言: 生成的prompt包含`掌握程度要求为: 【{mastery}】`
  - 测试数据: kb_chunk包含`掌握程度`字段
- **TP3.6 计算结果嵌入题目** (FR4.3): 计算结果应正确嵌入题目
  - 断言: 生成的题目中包含`calculation_result`的值，且该值作为正确答案或中间步骤
  - 测试数据: 计算类知识点，已执行计算代码
- **TP3.10 括号规范**: 判断/选择题括号格式统一为中文括号且内部有空格
  - 断言: 题干出现占位括号时为 `（ ）`，括号前后无空格
  - 测试数据: 含答案占位括号的题干
- **TP3.7 限流检测逻辑**: 应正确检测 GPT 限流状态
  - 断言: 如果 `.gpt_rate_limit.txt` 文件存在且 `now - last_ts < 10`，则检测为限流中
  - 测试数据: 创建限流文件，写入 5 秒前的时间戳
- **TP3.8 模型自动切换**: 限流等待时间 > 5 秒时，应切换到 Deepseek
  - 断言: 如果 `wait > 5`，则 `calculator_model_used == "deepseek-chat"`
  - 测试数据: 限流文件显示需等待 8 秒
- **TP3.9 模型状态传递**: 应将实际使用的模型存入 state
  - 断言: Calculator 执行后，`state['calculator_model_used']` 不为空
  - 测试数据: 任意 Calculator 执行
- **TP3.11 专有名词原词锁定**: 命中术语不得被改词
  - 断言: `draft` 内术语与 `term_locks` 原词一致
  - 测试数据: 计算题含专有名词的场景

#### 5.2.4 Writer Node (格式化标准化节点)

**职责**:
- 将初稿转化为标准JSON格式
- **题型修改策略**：
  - **随机模式**：如果 `config['question_type'] == "随机"`，Writer **不允许**修改专家节点生成的题型，必须保持原样
  - **指定题型模式**：如果 `config['question_type']` 为具体题型（单选/多选/判断），Writer 才需要校验并强制修改为指定题型
- **题型状态传递**：将确定的题型保存到 `state['current_question_type']`，供后续节点使用
- 难度验证：检查并调整难度值到指定范围
- 格式清洗：去除选项前缀（A./A、/A:等）

**输入**: `AgentState` (包含 `draft`, `question_type`, `difficulty_range`)

**输出**: 更新 `AgentState` 的 `final_json`, `current_question_type` 字段

**分层实现（新增）**:
- Writer 节点内部必须拆分为三个阶段：
  1. `Normalize`（代码）：`DraftV1 -> QuestionIR`
  2. `Validate`（混合）：`QuestionIR -> ValidationReport`
  3. `Polish`（LLM）：`QuestionIR + ValidationReport.issues -> QuestionIR`
- `Normalize` 阶段禁止调用 LLM。
- `Validate` 阶段必须统一输出结构化问题码，不允许仅输出自由文本。
- `Polish` 阶段仅允许修复 `ValidationReport` 中声明的问题，不允许重新定义校验规则。

**契约对象（新增）**:
- `DraftV1`：`question/options/answer/explanation`（生成节点输出）
- `QuestionIR`：统一字段、统一题型、统一答案口径（Writer Normalize 输出）
- `ValidationReport`：
  - `passed: bool`
  - `issues: list[Issue]`
  - `summary: str`
- `Issue`：
  - `issue_code: str`
  - `severity: error|warning`
  - `field: question|options|answer|explanation|global`
  - `message: str`
  - `fix_hint: str`

**核心原则**:
- 讲原理：解析要解释"为什么"，不要讲生成过程或机制
- 情境绑定：必须结合题干中的具体人物与情境进行解释
- 口语清晰：用清晰自然的口语解释，但避免"大家注意/这里有个陷阱/你可能以为"等口头禅
- 错误引导：对每个错误选项，直接指出学员可能的错误思路
- **遣词造句与指代一致**：润色时注意主谓搭配与指代一致，避免指代对象错误
- **判断题定义类例外**：定义类判断题（题干含“属于/是指/定义/概念”等）可不强制【XX做法正确/错误】模板；行为/说法类仍应使用该模板

**润色约束**:
- 禁止元认知：解析中不得出现"我遵循了规则/我没有引入/根据生成机制"等自我证明
- 禁止辩论体：不要写"虽然…但…其实…"。只给出规则与结论
- 解析结构：先摆事实，再引规则，最后结论；可选补充错误选项为什么错
- 错字修复：发现明显错别字、乱码或奇怪词语，必须直接改正
- 专有名词锁定：对 `term_locks` 命中术语禁止改词；可重写句式但术语字面必须一致
 - **题型格式与括号规范**：
   - 判断题：选项固定为“正确/错误”，答案仅 A/B
   - 单选题：4个选项且仅1个正确答案
   - 多选题：至少4个选项且至少2个正确答案
   - 括号规范（判断/选择题统一）：中文括号“（ ）”、括号前后无空格、括号内为全角空格
 - **题干与选项组合可读性自检（Writer 可读性维度）**：
   - 仅在题目使用“题干中包含占位括号（　），选项为补全括号内容”这一结构时启用（典型如“……的做法是（　）。”）
   - 在 Normalize/Polish 完成后，Writer 需要基于形成的最终题干模板，逐一将 `选项1-选项N` 的文本代入括号位置，构造完整句子序列 `candidate_sentences`
   - 通过代码+LLM 结合方式，对 `candidate_sentences` 做语法与语义可读性检测：
     - 代码侧至少检查：是否出现明显残缺（如连续标点、空括号）、括号替换后是否仍有孤立量词/助词
     - LLM 侧提示词需显式要求判断“这句话是否在中文里读起来自然、没有明显病句或别扭感”
   - 若任意一个选项代入后被判定为“明显别扭/病句”，Writer **必须**：
     - 优先通过重写题干句式来统一适配所有选项（例如从“……的做法是（　）。”改写为“下列关于……的说法，哪项/哪些是正确的？”）
     - 或在保持考点不变的前提下，改写该选项文本使其与题干搭配自然
   - Writer 不得在可读性明显不合格的情况下直接放行题目进入 Critic；若多轮尝试后仍无法修复，应通过 ValidationReport 标记专门的 issue_code（例如 `READABILITY_STEM_OPTION_MISMATCH`）并作为未通过状态透传

**地理和时间约束**:
- 地理继承：如果教材明确限定了城市，题干场景必须设定在该城市
- 严禁无关城市：绝对禁止出现原文未提及的其他具体城市名
- 时间继承：如果原文未给出具体时间，题干与解析不得添加具体年份/日期
- **硬规则预检（Writer Validate）**：年份/表格/图片/单引号/兜底选项/数值升序/字数阈值必须在 Writer 侧提前输出结构化问题码

**测试点**:
- **TP4.1 难度值验证和调整**: 不在范围内的难度值应调整到范围中点
  - 断言: 如果`difficulty_value = 0.3`且`difficulty_range = (0.5, 0.7)`，则调整后应为`0.6`
  - 测试数据: 初稿难度值为0.3，难度范围为(0.5, 0.7)
- **TP4.2 选项前缀清洗**: 应去除所有选项前缀
  - 断言: `选项1`不包含"A."、"/A:"等前缀
  - 测试数据: 初稿选项为"A. 选项内容"、"/A: 选项内容"
- **TP4.3 题型锁定**: 判断题选项必须为["正确","错误"]
  - 断言: 如果`question_type == "判断题"`，则`选项1 == "正确"`且`选项2 == "错误"`
  - 测试数据: `question_type = "判断题"`
- **TP4.4 数据验证**: 输出的final_json应通过ExamQuestion验证
  - 断言: `ExamQuestion(**final_json)` 不抛出ValidationError
  - 测试数据: 有效的初稿JSON
- **TP4.5 完整题目信息** (FR1.6): 应包含所有必需字段
  - 断言: final_json包含`题干`、`选项1`、`选项2`、`正确答案`、`解析`、`难度值`、`考点`、`一级知识点`、`二级知识点`、`三级知识点`、`四级知识点`
  - 测试数据: 生成的完整题目
- **TP4.6 题型状态传递**: 应将确定的题型保存到state
  - 断言: `state['current_question_type']` 不为空且与实际生成的题型一致
  - 测试数据: Writer 根据 draft 推断题型为"判断题"
- **TP4.7 随机模式保持题型**: 当 `question_type == "随机"` 时，Writer 不修改 draft 的题型
  - 断言: 如果 `config['question_type'] == "随机"` 且 draft 为多选题，则 `current_question_type == "多选题"`
  - 测试数据: config 题型为"随机"，draft 为多选题（答案为列表）
- **TP4.8 指定题型强制修改**: 当 `question_type` 为具体题型时，Writer 强制修改为指定题型
  - 断言: 如果 `config['question_type'] == "单选题"` 且 draft 为多选题，则 `current_question_type == "单选题"`
  - 测试数据: config 题型为"单选题"，draft 为多选题
- **TP4.10 出题一致性问题清单传递**:
  - 断言: 出题节点输出 `self_check_issues`（含冲突维度/冲突点/修复建议）
  - 断言: Writer 提示词包含 `self_check_issues` 并据此修订题干与解析
  - 测试数据: 构造时间/政策冲突的题干场景
- **TP4.12 遣词造句与指代一致（提示词约束）**:
  - 断言: Writer 提示词包含“主谓搭配与指代一致/避免指代对象错误”等约束文字
  - 测试数据: 任意 Writer 执行场景
- **TP4.13 定义类判断题例外（提示词约束）**:
  - 断言: Writer 提示词包含“定义类判断题（属于/是指/定义/概念）可不强制【XX做法正确/错误】模板”的说明
  - 测试数据: 题型为判断题的 Writer 执行场景
- **TP4.11 Normalize 阶段纯代码约束（新增）**:
  - 断言: Writer 的 Normalize 阶段不发生任何 LLM 调用
  - 断言: Normalize 输出对象类型为 `QuestionIR`
  - 测试数据: 含脏字段和混合答案格式的 draft
- **TP4.12 Validate 结构化输出（新增）**:
  - 断言: Validate 输出 `ValidationReport`，且 `issues` 中每条都包含 `issue_code/severity/field/message/fix_hint`
  - 测试数据: 构造括号错误、答案格式错误、语义歧义的样例题
- **TP4.13 Polish 定向修复（新增）**:
  - 断言: Polish 提示词仅包含问题码列表与修复提示，不包含全量规范清单
  - 断言: 生产链路不允许回退 legacy prompt（全量规范长提示词）
  - 断言: 修复后再次进入 Normalize->Validate，且问题数量下降
  - 测试数据: 含 2-3 个可修复 issue 的样例题
- **TP4.14 Writer 最大修复轮次（新增）**:
  - 断言: Writer 内部修复超过 2 轮后停止并透传 `ValidationReport` 给 Critic
  - 测试数据: 构造持续失败的样例题
- **TP4.15 统一问题码协议（新增）**:
  - 断言: Writer Validate 产出的 `issue_code` 能被 Critic/Fixer 直接复用（无二次映射）
  - 断言: Critic 的 `all_issues` 包含来自 Writer 的 `issue_code`（可追踪来源）
  - 测试数据: 任意包含格式+语义混合问题的样例题
- **TP4.16 Writer 硬规则校验**:
  - 断言: 命中年份/表格/图片/单引号/兜底选项/数值升序/字数阈值时，`ValidationReport.issues` 含对应 issue_code
  - 测试数据: 构造含年份/表格/图片/单引号/兜底选项/乱序数值/超长文本的样例题
- **TP4.17 Writer 硬规则最小修复**:
  - 断言: 修复后不改变考点与题意，仅移除违规细节
  - 测试数据: 含年份或兜底选项的样例题
- **TP5.14 Critic 全量问题输出**:
  - 断言: Critic 即使发现格式问题也继续完成所有检查
  - 断言: `critic_result.all_issues` 包含格式/逻辑/缺失条件/质量/难度/解析/计算代码
- **TP5.15 年份约束校验**:
  - 断言: 当知识切片未包含年份时，题干/选项/解析出现年份应判Fail
  - 测试数据: 切片无年份，题干含“2024年”
- **TP4.9 括号规范**: 判断/选择题括号格式必须为中文括号且内部有空格
  - 断言: 题干中的占位括号为 `（ ）`，括号前后无空格
  - 测试数据: 含答案占位括号的题干
- **TP4.18 题干与选项组合可读性自检（Writer）**:
  - 断言: 对于使用括号占位结构的题目，Writer 在输出 final_json 前已对每个选项执行“代入题干形成完整句子”的可读性检查；若存在明显别扭句子，final_json 对应的 ValidationReport 中包含 `READABILITY_STEM_OPTION_MISMATCH` 或同类 issue_code，且题干/选项已被改写为自然表达或标记为未通过
  - 测试数据: 人为构造一个题干为“下列选项中最（　）的说法是。”且选项为完整句子的样例题，要求 Writer 识别并改写

#### 5.2.5 Critic Node (质量验证节点)

**职责**:
- **模型智能切换策略**：
  - **限流检测逻辑**：
    1. 读取 `.gpt_rate_limit.txt` 文件，获取上次 GPT 调用的时间戳
    2. 计算距离当前时间的间隔 `elapsed = now - last_ts`
    3. 如果 `elapsed < 10` 秒，说明限流中，需要等待 `wait = 12 - elapsed` 秒
  - **切换决策**：
    - 如果 `wait > 5` 秒，自动切换到 Deepseek Reasoner
    - 否则使用配置的 CRITIC_MODEL（默认 GPT-5.2）
  - **状态传递**：将实际使用的模型名称存入 `state['critic_model_used']`
  - **日志输出**：切换时打印 `⚠️ GPT-5.2 限流中（需等待 {wait}s），切换到 Deepseek Reasoner`
- **题型读取策略**：优先从 `state['current_question_type']` 读取上游节点确定的题型，若无则从 `config['question_type']` 读取
- **题型校验策略**：
  - **随机模式**：如果 `config['question_type'] == "随机"`，Critic **不校验**题型一致性，保留 state 的题型
  - **指定题型模式**：如果 `config['question_type']` 为具体类型（单选/多选/判断），Critic **必须校验** state 的题型是否与 config 一致，不一致则标记为 **major 问题（failed）**
- 反向解题验证：能根据题目条件推导出唯一答案（最高裁决优先级）
- 答案一致性验证：Critic推导的答案与生成答案一致
- 信息不对称校验：检查是否遗漏判定条件、母题冲突检查
- 计算验证：对计算题自动调用计算器验证
- 难度验证：检查难度值是否在指定范围内
- 质量检查：语境强度、选项维度一致性、解析有效性、同义反复/信息泄露检测、AI幻觉/非人话检测
- 地理与范围审计：检查城市一致性、时间逻辑
- 逻辑自洽性审计：比对判定结果而非机械比对数字
- 选项逻辑审计：干扰项应考察易错点，而非纯粹随机数字
- **题型格式与括号规范校验**：判断/单选/多选结构与“（ ）”格式必须一致，否则判为不通过
- **术语锁词审计**：`term_locks` 命中术语若被替换为近义词/解释词，判为 major
 - **题干与选项组合可读性复核**：
   - 在 Writer 已做基础自检的前提下，Critic 需要将“题干+占位括号+选项代入”的可读性作为独立审计维度执行复核
   - 具体实现要求：
     - 由代码侧预先生成所有 `candidate_sentences`（将选项内容逐一填入括号中的全句字符串），传入 LLM 作为结构化字段
     - LLM 必须针对每个候选句输出 `is_natural`（bool）和 `reason`，并给出整体是否可接受的结论
   - 判定规则：
     - 若存在任意一个选项在代入后被判定为 `is_natural = False` 且理由涉及语法错误、主谓搭配错误、指代错乱等严重问题，则至少应将该题标记为 `quality_check_passed = False`
     - 若该可读性问题可能影响考生理解或造成误判，应设置 `issue_type = "major"` 并推荐 `fix_strategy = "fix_question"` 或 `fix_both`
     - 仅在问题为轻微措辞偏好且不影响理解时，才可将该维度记为 `minor`，并交由 Fixer 进行非必须修复
   - 若未执行此维度检查（例如 candidate_sentences 为空、LLM 未被要求逐句判断），应在开发阶段视为实现不达标

**输入**: `AgentState` (包含 `final_json`, `current_question_type`, `question_type`, `difficulty_range`, `calculation_function`, `calculation_params`)

**输出**: 更新 `AgentState` 的 `critic_feedback` 字段

**验证逻辑**:
0. **题型一致性验证**（仅指定题型模式）:
   - 如果 `config['question_type'] == "随机"`：跳过题型校验
   - 如果 `config['question_type']` 为具体类型：校验 `state['current_question_type']` 是否与 `config['question_type']` 一致
   - 不一致 → 标记为 **major 问题**，返回 `{"passed": False, "issue_type": "major", "reason": "题型不一致"}`

1. **反向解题验证**（最高优先级）:
   - 在完全忽略生成者声称答案的前提下，仅基于题干条件+教材规则推导
   - Fail条件：无法计算（缺关键数值/条件）、存在多条合理推导路径、需要考生"猜规则"

2. **答案一致性验证**:
   - Critic推导的答案与生成答案一致

3. **计算验证**（如适用）:
   - 自动提取参数
   - 调用计算器验证
   - 支持多步计算

4. **难度验证**:
   - 检查难度值是否在指定范围内

5. **质量检查**:
   - 语境强度（强/中/弱）
   - 选项维度一致性
   - 解析有效性
   - 题干直接给出答案检测（题干中是否直接包含正确答案的关键词，导致无需理解即可选出）
   - AI幻觉/非人话检测（生造词检测）

6. **地理与范围审计**:
   - 检查城市一致性、时间逻辑

7. **逻辑自洽性审计**:
   - 比对判定结果而非机械比对数字

8. **选项逻辑审计**:
   - 干扰项应考察易错点，而非纯粹随机数字
   - 低难度题不强制要求干扰项“优秀程度”，高难度题必须满足高质量干扰项要求
9. **题型格式与括号规范校验**:
   - 判断题仅“正确/错误”且答案仅 A/B
   - 单选题4选项且仅1个正确
   - 多选题至少4选项且至少2个正确
   - 题干占位括号必须为 `（　）`，括号前后无空格、括号内为全角空格
10. **术语锁词校验**:
   - 比对 `term_locks` 命中术语在题干/选项/解析中的字面一致性
   - 命中术语被改词 → `passed = False`, `issue_type = "major"`
12. **第三层泄题判定（rigor.leakage_still_invalid）**：
   - Critic（或专门的 rigor critic 子模块）需要输出：
     - `leakage_still_invalid: bool`
     - `leakage_evidence: { "stem_snippet": str, "option_snippet": str }`
   - 判定规则（需同时满足）：
     1. 题干中的关键术语 / 情景短语 / 数值组合，与正确选项中的关键表述高度一致或同义；
     2. 在当前题型、出题模式和教材语境下，考生几乎不需要理解教材内容或业务规则，仅凭该关键表述就能唯一锁定正确选项。
   - 强制约束：
     - 若任一条件不满足，或 `stem_snippet` / `option_snippet` 任一为空或无法指向具体短句，必须输出 `leakage_still_invalid = false`；
     - 禁止仅因题干与正确选项有高文本相似度、共享通用业务词（如“贷款、利率、税费、合同、客户、经纪人、门店、服务”等）就将 `leakage_still_invalid` 置为 `true`；
     - 当题干涉及的概念在多个选项中以类似方式出现、仍需理解 / 推理才能选出时，亦不得判泄题。
   - 题型语义变体：
     - `current_mode == "基础概念/理解记忆"` 时，在提示词中使用“关键概念术语 / 定义短语”表述上述规则；
     - `current_mode == "实战应用/推演"` 时，在提示词中使用“关键业务情景 / 术语 / 数值组合”表述上述规则。
   - **在线链路联动要求（与决策逻辑绑定）**：
     - 当 `leakage_still_invalid == True` 且 `leakage_evidence.stem_snippet` / `option_snippet` 两段证据均为非空、可对齐到具体短语时，Critic 必须：
       - 将本题整体严重程度至少提升为 `issue_type = "major"`；
       - 强制设置 `critic_result.passed = False`，即视为“绝对不通过”；
       - 将该泄题信号透传给 `critical_decision`，在决策函数中**无条件选择 `"reroute"`**，直接回到 Router 重新出题，不允许走 `"fix"` 路径在当前题目上做修修补补。

11. **多选题解析逐项覆盖率（新增，与离线 Judge 对齐）**:
   - 适用范围：`current_question_type == "多选题"`。
   - 代码侧需从解析文本中抽取“选项级别”的解释覆盖情况，生成：
     - `multi_option_coverage_rate: float`（0.0–1.0，小数保留两位），含义为“被逐项解释到的选项数量 / 实际存在的选项数量（非空选项1-8）”。
     - `missing_options: list[str]`，未被逐项解释的选项字母列表（如 `["B", "E"]`），按字母升序。
   - 逐项覆盖规则：
     - 只有当解析中对某一选项进行了**显式点名或可唯一映射**（如“选项B……”“B项属于……”等），才视为该选项已被覆盖。
     - 单纯一句“其他选项都是错误的”或“错误选项都是因为条件不满足”不计入任何具体选项的覆盖。
   - 放行条件：`multi_option_coverage_rate == 1.0` 且 `missing_options == []`；否则必须在 `quality_issues` 中给出固定模式中文提示：
     - `"多选解析逐项覆盖率不足：X.xx"`（用格式化后的覆盖率替换 X.xx，例如 0.75 → `0.75`）
     - 如 `missing_options` 非空，再追加 `"多选解析未覆盖选项：B,E"`（缺失字母按升序用英文逗号拼接）。

12. **解析首段结构三要素（新增）**:
   - 解析第一段需显式包含以下三类信息：
     - 目标题内容：即路由/教材路径的前三个标题；解析中须有该内容，不要写「目标题：」字样。
     - 掌握程度分级：`了解/熟悉/掌握/应用` 中的一种，优先与 `kb_chunk['掌握程度']` 对齐。
     - 教材原文引用：至少一句贴近教材原文的句子，可通过启发式/相似度比对识别。
   - Critic 在结构化输出中必须新增字段：
     - `first_part_missing_target_title: bool`
     - `first_part_missing_level: bool`
     - `first_part_missing_textbook_raw: bool`
     - `first_part_structured_issues: list[str]`（内容如 `"缺少目标题内容（路由前三个标题）"`, `"缺少掌握程度分级"`, `"缺少教材原文引用"`, `"首段结构顺序混乱"` 等）
   - 验证规则：
     - 只要任意一个 `first_part_missing_* == True`，需要在 `first_part_structured_issues` 和 `quality_issues` 中体现对应问题，且 `explanation_valid` 不得简单置为 True。
     - 所有 `first_part_missing_*` 为 False 且 `first_part_structured_issues == []`，才视为首段结构完全合格。

13. **解析“必须重写”充分性检查（新增）**:
   - 目标：区分“直接抄教材原文”与“在忠实原文基础上，由模型自己讲清楚推理过程”的解析。
   - Critic 输出需新增：
     - `analysis_rewrite_sufficient: bool`
     - `analysis_rewrite_issues: list[str]`
   - 判定逻辑（建议实现为“教材原文片段 vs 解析文本”的相似度与差异度混合规则）：
     - 允许保留条文名、政策名、专有名词等术语级短语，但不允许长句逐字或大段结构完全照搬。
     - 如检测到以下任一情形，需置 `analysis_rewrite_sufficient = False`，并在 `analysis_rewrite_issues` 中写入对应原因：
       - “大段解析文本与教材原文高度重合，仅做少量词汇替换或顺序调整。”
       - “解析只机械复述教材规定，缺少对‘为什么答案如此’的解释性推理。”
       - “解析主要以‘见教材原文’、‘同教材表述’等方式代替展开说明。”
   - 约束：当 `analysis_rewrite_sufficient == False` 时，该问题在 `critic_result` 中至少应归类为 **major** 质量问题，并影响 `explanation_valid` 与总体 `passed` 判定。
11. **选项父子类层级冲突检测（疑似多解）**:
   - 基于教材切片和内部知识图谱/分类信息，对单选/多选题选项进行“父类/子类/上下位”结构识别（例如“住宅”“存量住宅”“二手房”“普通住宅”）
   - 当同一题目的选项集合中，存在被判定为父子类或上下位关系的一对或多对选项，且不属于“已被题干条件完全消解的情形”（如题干已限定某一子类的前提下仅对该子类内部作区分）时，生成结构化检测结果：
     - `option_hierarchy_conflict_flag: bool`
     - `option_hierarchy_conflict_pairs: List[(option_a, option_b, relation)]`
     - `option_hierarchy_conflict_message: str`，推荐固定前缀：“单选/多选题选项疑似层级冲突（父/子类或上下位关系），需结合题干判断是否存在多解风险”
   - 该检测逻辑应复用现有的“选项层级冲突”判断能力（如 DeterministicFilter 内部的层级冲突检测工具函数），但以“工具函数/服务”的形式在 Critic 内独立调用，不强制要求与离线 Judge 的最终裁决结果做一致性比对
   - 在 `critic_feedback` 中，必须透出上述三个字段，供前端质检维度与审核 UI 使用；此维度默认作为质量报告中的“风险提示”，由人工决定是否据此打回题目

**题目质量硬性约束** (FR5.7):
- **禁止模糊用语**：题干中禁止使用"实实在在的特点"、"重要的信息"、"关键因素"等模糊表述
- **选项维度一致性**：所有选项必须在同一维度内做区分，禁止跨维度
- **禁止定义题与对工作无帮助题**：题目须对经纪人工作有帮助（可为场景化、规则理解、合规、文化等）；禁止直接问定义；禁止仅考「定义 vs 目的 vs 方式」等概念归类；禁止仅考「教材把哪一条称为核心/主要/关键」、选对只靠记教材措辞的刁钻题（Critic 判对工作无帮助并驳回或要求改写）。
- **禁止常识与切片表述易冲突题**：若考点在常识上与切片易产生偏差（如“新建”常人=未交易、教材有专门口径），导致按常识选错或觉得没写清楚，则不出；若必须考则须在题干/解析中明确教材口径与日常用语区别。
- **禁止流程/步骤类主体或视角歧义题**：若切片流程未明确每一步的执行主体或视角，则不出因「主体或视角不同会产生歧义」的题目或选项（如“最后一步”在流程顺序与当事人操作角度可能不同）。
- **禁止选项与题干条件相悖**：任一选项不得与题干中已明确给出的条件、前提或设定在逻辑上矛盾；题干已设定某事实成立时，选项中不得出现与该事实相悖的表述，否则 Critic 判为不合格。
- **正确选项须完整覆盖考点关键要素**：若教材/切片对某概念、流程或规则明确了多个并列要点，正确选项不得只表述部分要点而遗漏其他关键要素，否则 Critic 判为正确项不完整并驳回或要求补全。
- **FR1.7 例外**：公司文化类不适用上述“概念辨析/核心基础”类 Fail；Specialist 若判断无法形成对工作有帮助的题目可设置 refuse_to_generate 并直接结束，图中 specialist 后条件边：refuse_to_generate 则 END，否则 writer。
- **唯一正确性**：单选题确保只有一个选项严格符合教材原文，其他选项须有明确错误点
- **数据重构**：严禁直接照搬原文案例中的具体人名、金额、日期、房产面积
- **禁止题干直接给出答案**：题干中不得直接包含正确答案的关键词，导致考生无需理解即可通过文字匹配选出答案（如题干说"经纪人介绍了商业贷款"，然后问"商业贷款最核心的特征"）
- **允许答案与教材原文一致**：正确答案选项可以与教材原文定义一致，这是正常的考察方式，不应被视为质量问题
- **禁止AI幻觉/非人话**：禁止出现不符合中国房地产业务习惯的生造词（如"外接"代替"买方/受让方"、"上交"代替"缴纳"）
- **禁止"最XX"考法**：禁止用"最重要/最关键/重点/主要"等表述
- **唯一答案强制校验**：逐条假设每个错误选项为真，验证是否"必错"

**地理一致性约束** (FR5.8):
- **地理继承**：如果教材明确限定了城市（如"北京市"），题干场景必须设定在该城市（或其下辖区县）
- **严禁无关城市**：绝对禁止出现原文未提及的其他具体城市名（如上海、深圳、广州等）
- **母题城市替换**：即使参考母题中写的是其他城市，必须自动替换为原文指定的城市或通用化
- **干扰项特例**：干扰项中允许出现其他城市作为错误选项，但题干场景和正确答案必须基于教材指定城市
- **通用规则处理**：如果原文是通用规则（未提及特定城市），题干不得写具体城市，可用"某市"或不提及地点

**时间逻辑约束** (FR5.9):
- **时间继承**：如果原文未给出具体时间，题干与解析不得添加具体年份/日期；仅保留相对时间（如"满5年"）
- **时间使用规则**：若原文明示时间，才可使用对应年份/日期；判定逻辑必须严格遵循教材规则（如"满5年"的计算）
- **时间干扰项**：允许设计关于时间的干扰项（如设置一个时间未满的情景作为错误选项），但解析必须清晰指出不符合哪条时间规则
- **计算时间精度**：时间/日期题必须用 datetime 精确到天，禁止用年份直接相减

**问题分类**:
- **严重问题 (major)**: 无法推导唯一答案、答案错误、遗漏条件、难度不符合要求
- **轻微问题 (minor)**: 解析不清晰、格式问题

**通过判定与智能决策** (`critical_decision`):
- **零缺陷通过原则**：
  - Critic 在内部聚合各维度检查结果时，必须保证：只要存在任意 error / warning / issue（包括但不限于 `quality_issues`、`missing_conditions`、`analysis_rewrite_issues`、代码格式校验问题、泄题信号等），就不得返回 `critic_result.passed = True`；
  - 只有当所有问题列表为空（即“零缺陷”）时，才允许设定 `critic_result.passed = True` 并进入 `"pass"` 分支。
- **通过** (`pass`): `critic_result.passed = True` → 结束流程
- **轻微问题** (`fix`): `issue_type = 'minor'` → Fixer修复 → 回到Critic验证
- **严重问题** (`reroute`): `issue_type = 'major'` → 
  - `retry_count < 2`: Router重新路由
  - `retry_count >= 2`: Fixer强制修复
- **泄题强制重路由**：
  - 只要 `critic_result.leakage_still_invalid == True`（且满足上文证据完整约束），`critical_decision` 必须**无条件返回 `"reroute"`**，直接回到 Router 重新出题；
  - 在该分支下，不得再进入 `"fix"` 路径尝试在当前题目上修复（即泄题一律视作需要“推倒重来”的致命问题）。
- **超限自愈** (`self_heal`): `retry_count >= 3` → 自愈机制（直接Fixer重新生成，不再经过Router）

**测试点**:
- **TP5.1 反向解题验证**: 无法推导唯一答案应标记为major问题
  - 断言: 如果`can_deduce_unique_answer == False`，则`issue_type == "major"`
  - 测试数据: 题干缺少关键条件，无法唯一推导答案
- **TP5.2 答案一致性验证**: 答案不一致应标记为major问题
  - 断言: 如果`critic_answer != gen_answer`，则`issue_type == "major"`
  - 测试数据: Critic推导的答案与生成答案不一致
- **TP5.3 难度验证**: 难度值不在范围内应标记为major问题
  - 断言: 如果`difficulty_value < min_diff or difficulty_value > max_diff`，则`issue_type == "major"`
  - 测试数据: 难度值为0.3，难度范围为(0.5, 0.7)
- **TP5.4 题干直接给出答案检测**: 应检测并标记为质量问题
  - 断言: 如果题干包含正确答案关键词，则`quality_check_passed == False`
  - 测试数据: 题干说"经纪人介绍了商业贷款"，然后问"商业贷款最核心的特征"
- **TP5.5 计算验证**: 计算题应调用计算器验证
  - 断言: 如果`agent_name == "CalculatorAgent"`，则`critic_tool_usage['tool'] != "None"`
  - 测试数据: 计算类题目
- **TP5.6 问题分类**: 应正确分类major和minor问题
  - 断言: 答案错误/无法推导唯一答案 → major，解析不清 → minor
  - 测试数据: 各种问题场景
- **TP5.7 解析逻辑性验证** (FR5.2): 解析应与答案一致、引用教材原文、解释错误选项
  - 断言: `explanation_valid == True` 当解析与答案一致、包含教材原文引用、解释所有错误选项
  - 测试数据: 有效解析和无效解析的题目
- **TP5.8 判断题选项数量**: 判断题允许仅两个选项，不应因缺少C/D被判为质量问题
  - 断言: `question_type == "判断题"` 且 `选项3`/`选项4` 为空时，`quality_check_passed == True`
  - 测试数据: 判断题仅包含"正确/错误"两个选项
- **TP5.9 低难度干扰项放宽**: 低难度题不因干扰项不够“优秀”而被判为质量问题
  - 断言: `难度值 <= 0.5` 且仅存在“干扰项质量”相关问题时，`quality_check_passed == True`
  - 测试数据: 低难度题干扰项较明显但无其他质量问题
- **TP5.10 高难度干扰项要求**: 高难度题必须满足高质量干扰项标准
  - 断言: `难度值 >= 0.7` 且干扰项为随机数字/明显错误时，`quality_check_passed == False`
  - 测试数据: 高难度题干扰项未体现易错点
- **TP5.11 题型读取优先级**: 应优先从state读取current_question_type
  - 断言: 如果`state['current_question_type'] == "判断题"`，即使`config['question_type'] == "单选题"`，Critic也应按判断题标准验证
  - 测试数据: state 和 config 的题型不一致
- **TP5.12 随机模式不校验题型**: 当 config 题型为"随机"时，不校验题型一致性
  - 断言: 如果`config['question_type'] == "随机"`，即使`state['current_question_type'] != config['question_type']`，也应`passed == True`（不因题型问题failed）
  - 测试数据: config 题型为"随机"，state 为"多选题"
- **TP5.13 指定题型校验一致性**: 当 config 题型为具体类型时，必须校验题型一致性
  - 断言: 如果`config['question_type'] == "单选题"`且`state['current_question_type'] == "多选题"`，则`passed == False`且`issue_type == "major"`且原因包含"题型不一致"
  - 测试数据: config 题型为"单选题"，state 为"多选题"
- **TP5.14 限流检测逻辑**: 应正确检测 GPT 限流状态
  - 断言: 如果 `.gpt_rate_limit.txt` 文件存在且 `now - last_ts < 10`，则检测为限流中
  - 测试数据: 创建限流文件，写入 5 秒前的时间戳
- **TP5.15 模型自动切换**: 限流等待时间 > 5 秒时，应切换到 Deepseek
  - 断言: 如果 `wait > 5`，则 `critic_model_used == "deepseek-reasoner"`
  - 测试数据: 限流文件显示需等待 8 秒
- **TP5.16 模型状态传递**: 应将实际使用的模型存入 state
  - 断言: Critic 执行后，`state['critic_model_used']` 不为空
  - 测试数据: 任意 Critic 执行
 - **TP5.17 题干与选项组合可读性复核（Critic）**:
   - 断言: 对于使用括号占位结构的题目，Critic 会基于代码生成的 `candidate_sentences` 逐句判断可读性；当至少一个选项代入后形成的句子被判定为不自然/病句时，`quality_issues` 中包含对应选项与具体原因，且根据严重程度将 `issue_type` 标记为 `major` 或 `minor`
   - 测试数据: 构造题干“以下情形中，经纪人应当承担（　）。”，并给出若干明显不适合作为谓语的选项，期望 Critic 将该题判为不通过并给出可读性问题说明
- **TP5.18 多选解析逐项覆盖率计算（新增）**:
  - 场景: 多选题，选项 A–D 均存在，解析文本只对 A、C 做了单独解释，未出现任何可以唯一映射到 B、D 的表述。
  - 断言:
    - `multi_option_coverage_rate == 0.50`
    - `missing_options == ["B", "D"]`
    - `quality_issues` 中包含 `"多选解析逐项覆盖率不足：0.50"` 与 `"多选解析未覆盖选项：B,D"`
  - 测试数据: 人工构造一题多选题解析仅逐项说明 A、C。
- **TP5.19 解析首段三要素缺失标记（新增）**:
  - 场景1: 首段只写了“本题考察的是……”，未给掌握程度，也未引用教材原文。
    - 断言:
      - `first_part_missing_target_title == False`
      - `first_part_missing_level == True`
      - `first_part_missing_textbook_raw == True`
      - `first_part_structured_issues` 至少包含 `"缺少掌握程度分级"` 与 `"缺少教材原文引用"`
  - 场景2: 首段同时包含目标题、掌握程度标签（与切片掌握程度一致）以及一条教材原文引用。
    - 断言:
      - 三个 `first_part_missing_*` 标志均为 False
      - `first_part_structured_issues == []`
  - 测试数据: 构造包含/缺失不同要素的解析首段样例。
- **TP5.20 解析重写充分性检测（新增）**:
  - 场景1: 解析几乎整段直接复制教材原文，只改了个别数字或人名。
    - 断言:
      - `analysis_rewrite_sufficient == False`
      - `analysis_rewrite_issues` 中包含类似 `"大段复用教材原文，未充分重写"` 的原因描述
      - `issue_type == "major"`，且 `explanation_valid == False`
  - 场景2: 解析在引用关键术语的同时，以全新句式给出推理链条，对为什么选项正确/错误做了清晰解释。
    - 断言:
      - `analysis_rewrite_sufficient == True`
      - 不因“与教材含义接近”而被误判为抄袭
  - 测试数据: 一组“明显抄教材”的解析文本与一组“充分重写”的解析文本。
- **TP5.21 泄题双条件正例（概念题）**:
  - 场景: 题干中出现“本题考察的是委托买卖合同的概念”，正确选项几乎逐字复现教材定义，其它选项明显无关，考生几乎可以不看教材，仅凭这句话锁定唯一正确项。
  - 断言:
    - `critic_feedback['rigor']['leakage_still_invalid'] == True`
    - `critic_feedback['rigor']['leakage_evidence']['stem_snippet']` 与 `option_snippet` 非空，且分别来自题干与正确选项中的关键短语。
- **TP5.22 高重合但仍需理解的反例**:
  - 场景: 题干描述一个完整购房交易流程，正确选项和错误选项都包含类似的“贷款审批”等通用词汇，需要理解整体流程才能作答。
  - 断言:
    - 即便题干与正确选项文本相似度较高，`critic_feedback['rigor']['leakage_still_invalid'] == False`。
- **TP5.23 实战应用题泄题正例**:
  - 场景: 题干直接给出“客户家庭唯一住房，建筑面积 89㎡，首套普通住房”等关键信息，正确选项几乎逐字复述对应税率条款，且根据这些数字组合即可唯一锁定正确选项。
  - 断言:
    - `critic_feedback['rigor']['leakage_still_invalid'] == True`
    - `leakage_evidence.stem_snippet` / `option_snippet` 两段证据齐全且可读。
- **TP5.24 泄题证据不全强制改判**:
  - 场景: 模型主观认为存在泄题，但只能给出笼统描述，无法截取清晰的题干片段或正确项片段作为证据。
  - 断言:
    - 当 `leakage_evidence.stem_snippet` 或 `option_snippet` 任一为空或为过长的非局部片段时，最终必须输出 `leakage_still_invalid == False`，并在 reason/quality_issues 中说明“证据不足以支撑泄题结论”。
- **TP5.18 选项父子类层级冲突正例检测**:
  - 断言: 当单选题选项中同时包含教材知识图谱中存在上下位关系的组合（如“住宅”“存量住宅”“二手房”），且题干未对考察范围作消解性限定时，`critic_feedback['option_hierarchy_conflict_flag'] == True`，`option_hierarchy_conflict_pairs` 至少包含一对父/子类选项，`option_hierarchy_conflict_message` 文案包含“层级冲突/父子类/疑似多解”等关键词
  - 测试数据: 人工构造一题，题干为一般性描述，选项含“住宅”“存量住宅”“二手房”“办公用房”
- **TP5.19 选项父子类层级冲突反例与边界检测**:
  - 断言: 对于仅包含并列子类（如“新房”“二手房”“租赁住房”“经济适用房”）但在知识图谱中无上下位关系的组合，应有 `option_hierarchy_conflict_flag == False`；对于题干已限定考察对象为某一子类且选项仅在该子类内部作区分的题目（如题干限定“存量住宅买卖”场景），即使出现父/子类名词串联，也应优先通过“题干消解”逻辑，`option_hierarchy_conflict_flag == False` 或仅作为弱提示
  - 测试数据: （1）构造纯并列子类选项；（2）构造题干限定“存量住宅买卖”的场景，选项为不同处理方式

#### 5.2.6 Fixer Node (错误修复节点)

**职责**:
- **题型读取策略**：优先从 `state['current_question_type']` 读取上游节点确定的题型，若无则从 `config['question_type']` 读取
- 分析Critic反馈的问题
- 根据问题类型选择修复策略
- 修复题目和/或解析
- 严格遵循题型、模式、难度约束
 - **题型格式与括号规范**：修复时保持题型结构与括号格式（中文括号“（ ）”）
- **术语锁定约束**：修复时保持 `term_locks` 命中术语原词一致，禁止同义替换

**输入**: `AgentState` (包含 `final_json`, `critic_feedback`, `current_question_type`, `question_type`, `difficulty_range`, `mode`)

**输出**: 更新 `AgentState` 的 `final_json` 字段

**修复策略**:
- `fix_explanation`: 只修改解析
- `fix_question`: 修改题干/选项/答案
- `fix_both`: 同时修正题目和解析
- `regenerate`: 重写题目

**约束遵循**:
- 修复时严格遵循题型、模式、难度约束
- 自动调整难度值到指定范围

**工作流**: Fixer修复后回到Critic重新验证，形成循环

**测试点**:
- **TP6.1 修复策略执行**: 应根据fix_strategy执行相应修复
  - 断言: 如果`fix_strategy == "fix_explanation"`，则只修改解析，不修改题干/选项
  - 测试数据: critic_result包含`fix_strategy = "fix_explanation"`
- **TP6.2 难度值调整**: 修复后的难度值应在指定范围内
  - 断言: 如果`difficulty_value = 0.3`且`difficulty_range = (0.5, 0.7)`，修复后应为`0.6`
  - 测试数据: 修复前的难度值不在范围内
- **TP6.3 题型约束**: 修复后题型应保持不变
  - 断言: 修复后的题目题型与修复前一致
  - 测试数据: `question_type = "单选题"`
- **TP6.4 模式约束**: 修复后模式应保持不变
  - 断言: 修复后的题目模式与修复前一致
  - 测试数据: `generation_mode = "严谨"`
 - **TP6.5 括号规范**: 修复后题干占位括号为中文括号且内部有空格
 - **TP6.6 术语原词保持**: 修复后命中术语不得被改词
   - 断言: `final_json` 中命中术语与 `term_locks` 字面一致
   - 测试数据: Critic 反馈触发 Fixer 改写的术语场景
   - 断言: 题干占位括号为 `（ ）`，括号前后无空格
   - 测试数据: 含答案占位括号的题干

### 5.3 反馈循环机制

#### 5.3.1 Fixer → Critic 循环

**触发条件**: 轻微问题（解析不清、格式问题）

**循环路径**: 
```
Critic (发现问题) → Fixer (修复) → Critic (重新验证)
```

**最大次数**: 3次（`retry_count`）

**测试点**:
- **TP7.1 Fixer→Critic循环**: 修复后应回到Critic验证
  - 断言: Fixer节点执行后，工作流应回到Critic节点
  - 测试数据: Critic标记为轻微问题，触发Fixer修复
- **TP7.2 循环次数限制**: 应最多循环3次
  - 断言: retry_count <= 3，超过3次应触发自愈
  - 测试数据: Critic连续标记为轻微问题

#### 5.3.2 Critic → Router 循环

**触发条件**: 严重问题（答案错误、无法推导唯一答案）

**循环路径**:
```
Critic (答案错误) → Router (重新路由) → 新Agent (重新生成) → Writer → Critic
```

**状态清理**: Router检测重路由并清理旧状态，保留必要的上下文（`prev_final_json`, `prev_critic_feedback`）

**测试点**:
- **TP8.1 Critic→Router重路由**: 严重问题应触发重新路由
  - 断言: Critic标记为严重问题且retry_count < 2时，工作流应回到Router节点
  - 测试数据: Critic标记为major问题，retry_count = 1
- **TP8.2 状态清理**: 重路由时应清理旧状态但保留prev_final_json
  - 断言: 重路由时`state['draft'] is None`且`state['prev_final_json'] is not None`
  - 测试数据: retry_count > 0，存在prev_final_json

#### 5.3.3 自愈机制

**触发条件**: `retry_count >= 3`

**机制**: 直接调用Fixer重新生成，不再经过Router

**输出**: 即使质量可能不完美，也输出结果，避免无限循环

**测试点**:
- **TP9.1 自愈机制触发**: retry_count >= 3时应触发自愈
  - 断言: retry_count >= 3时，critical_decision应返回"self_heal"
  - 测试数据: retry_count = 3
- **TP9.2 retry_count递增**: 每次Critic失败应递增retry_count
  - 断言: Critic失败后`retry_count = previous_retry_count + 1`
  - 测试数据: 初始retry_count = 0，Critic失败

### 5.4 约束条件传递

#### 5.4.1 题型约束传递

- 从UI配置传递到所有节点（Router、Specialist、Calculator、Writer、Fixer）
- 修复模式也严格遵循题型约束

**测试点**:
- **TP28.1 题型约束传递到所有节点** (FR7.1): 所有节点应收到题型约束
  - 断言: Router、Specialist、Calculator、Writer、Fixer节点的config中都包含`question_type`
  - 测试数据: UI选择"单选题"
- **TP28.2 修复模式遵循题型约束**: 修复模式也应遵循题型约束
  - 断言: Fixer节点修复后题型与指定题型一致
  - 测试数据: 指定题型"判断题"，Fixer修复后仍为判断题

#### 5.4.2 出题筛选条件约束传递

- 从UI配置传递到生成节点和修复节点
- 三档筛选条件在提示词中有明确区分

**测试点**:
- **TP29.1 模式约束传递到生成节点** (FR7.2): 生成节点应收到模式约束
  - 断言: Specialist和Calculator节点的config中都包含`generation_mode`
  - 测试数据: UI选择"灵活"或"严谨"模式
- **TP29.2 模式约束传递到修复节点**: 修复节点应收到模式约束
  - 断言: Fixer节点的config中包含`generation_mode`
  - 测试数据: UI选择"严谨"模式
- **TP29.3 筛选条件在提示词中区分**: 三档筛选条件在提示词中应有明确区分
  - 断言: 基础概念/理解记忆包含"不强制业务场景"；实战应用/推演包含"必须关联业务场景"；随机模式包含"自动选择两者之一"
  - 测试数据: `generation_mode = "基础概念/理解记忆"/"实战应用/推演"/"随机"`

#### 5.4.3 难度范围约束传递

- 从UI配置解析难度范围（如"中等 (0.5-0.7)"）
- 传递到所有生成节点（Specialist、Calculator）
- 传递到格式化节点（Writer）
- 传递到修复节点（Fixer）
- 传递到验证节点（Critic）

#### 5.4.4 难度值验证与调整

- Writer节点验证并调整难度值
- Fixer节点验证并调整难度值
- Critic节点验证难度值是否符合范围
- 自动调整：如果不在范围内，调整到范围中点

**测试点**:
- **TP10.1 Writer节点难度调整**: 不在范围内应调整到中点
  - 断言: 如果`difficulty_value = 0.3`且`difficulty_range = (0.5, 0.7)`，调整后应为`0.6`
  - 测试数据: 初稿难度值为0.3，难度范围为(0.5, 0.7)
- **TP10.2 Critic节点难度验证**: 不符合范围应标记为major问题
  - 断言: 如果`difficulty_value = 0.3`且`difficulty_range = (0.5, 0.7)`，则`issue_type == "major"`
  - 测试数据: final_json难度值为0.3，难度范围为(0.5, 0.7)
- **TP10.3 Fixer节点难度调整**: 不在范围内应调整到中点
  - 断言: 如果`difficulty_value = 0.3`且`difficulty_range = (0.5, 0.7)`，调整后应为`0.6`
  - 测试数据: 修复前难度值为0.3，难度范围为(0.5, 0.7)

#### 5.4.5 掌握程度约束 (FR7.5)

- 从知识点切片读取掌握程度（了解/熟悉/掌握）
- 在提示词中说明掌握程度要求
- 影响题目复杂度设计

**测试点**:
- **TP11.1 掌握程度提取**: 应从kb_chunk正确提取
  - 断言: `mastery == kb_chunk.get('掌握程度', '未知')`
  - 测试数据: kb_chunk包含`掌握程度`字段（了解/熟悉/掌握）
- **TP11.2 掌握程度传递**: 应传递到Specialist和Calculator节点
  - 断言: Specialist和Calculator节点的prompt中包含掌握程度信息
  - 测试数据: kb_chunk包含`掌握程度`字段

## 6. 接口设计

### 6.1 LLM 调用接口

**统一接口**: 所有节点使用统一的LLM调用方式

**支持的模型提供商**:
1. **OpenAI / DeepSeek**:
   - 使用 `langchain_openai.ChatOpenAI`
   - Base URL: `https://openapi-ait.ke.com`
   - 模型：`deepseek-reasoner-v3.2`（推荐）

2. **Ark（Doubao/GPT）**:
   - 使用 OpenAI 兼容 `chat.completions.create`
   - Base URL: `https://ark.cn-beijing.volces.com/api/v3`
   - 模型：`doubao-seed-1.8` 等

**配置管理**:
- API Key从文件读取：`填写您的Key.txt`
- 支持多个Key：`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `CRITIC_API_KEY`, `ARK_API_KEY`
- 自动读取并填充到UI输入框

**重试机制**:
- **OpenAI兼容API**（包括DeepSeek）:
  - 最多10次重试（`backoff_seconds` 有10个值）
  - 指数退避策略：等待时间 [5, 10, 20, 30, 45, 60, 60, 60, 60, 60] 秒
  - 可重试错误：429限流（rate limit）
  - 超时设置：120秒（Reasoner模型需要更长时间推理）

### 6.2 知识检索接口

**类**: `KnowledgeRetriever` (exam_factory.py)

**核心方法**:
- `get_examples_by_knowledge_point(kb_chunk, question_type, k=3)`: 基于知识点检索母题
  - 优先使用知识点映射文件（`knowledge_question_mapping.json`）
  - 无映射则不返回母题
  - 题型过滤：只返回匹配题型的母题
  - 当 `question_type == "随机"` 时，不做单一题型硬过滤；应优先使用该切片已关联母题的题型集合
    作为“优先题型池”
- `get_preferred_question_types_by_knowledge_point(kb_chunk)`: 返回该切片映射母题中的题型优先列表（按出现频次降序）
  - 数据质量过滤：去除NaN值和空字符串

**测试点**:
- **TP12.1 映射文件优先**: 有映射时应使用映射文件
  - 断言: 如果`slice_id`在`knowledge_question_mapping.json`中存在，则返回的examples来自映射文件
  - 测试数据: kb_chunk对应的slice_id在映射文件中存在
- **TP12.2 题型过滤**: 应只返回匹配题型的母题
  - 断言: 返回的每个example的题型与question_type匹配
  - 测试数据: `question_type = "单选题"`，母题库包含单选题和多选题
- **TP12.3 数据质量过滤**: 应过滤NaN值和空字符串
  - 断言: 返回的每个example的必需字段（题干、选项1、选项2、正确答案、解析）都不为NaN或空字符串
  - 测试数据: 母题库包含NaN值和空字符串
- **TP12.4 数量限制**: 返回的examples数量应不超过k
  - 断言: `len(examples) <= k`
  - 测试数据: k=3
- **TP12.4b 无映射不返回母题**: 无映射时不应返回任何母题
  - 断言: `slice_id`不在`knowledge_question_mapping.json`中时，`examples == []`
  - 测试数据: kb_chunk对应的slice_id在映射文件中不存在
- **TP12.4c 随机题型优先池**: 随机模式应优先遵循映射母题题型
  - 断言: 当 `question_type="随机"` 且映射母题题型集合为{判断题, 单选题}时，返回的优先题型池仅包含这两类
  - 测试数据: 同一切片关联判断题和单选题母题
- **TP12.4d 随机模式回退**: 无映射时随机模式回退 Router 推荐题型
  - 断言: 当优先题型池为空时，系统使用 `recommended_type`
  - 测试数据: 切片无映射或映射数据不可用
- **TP12.5 五级阶梯关联策略** (FR2.1.1): 应按优先级执行五级策略
  - 断言: 策略1（反向索引）优先级最高，匹配到立即返回；策略2-5按顺序执行
  - 测试数据: 不同匹配场景的知识点
- **TP12.6 GPS路径匹配**: 策略2应正确匹配GPS路径
  - 断言: 仅当母题与切片路径深度均≥3时允许**全路径**命中（0.95 固定且立即返回）；路径深度不足时不得命中
  - 测试数据: 不同路径匹配程度的母题
- **TP12.6d GPS方法标记**: 输出 method 字段区分路径匹配类型
  - 断言: 全路径匹配的 method 为 `GPS_FullPath`
  - 测试数据: 同 TP12.6
- **TP12.6c 切片标题前缀剥离**: 策略操作前对切片标题做前缀剥离，剥离后用于 kb_gps 末节
  - 断言: 存在 `strip_title_prefix(s)`，可去除「一、」「二、」「（一）」「1、」等前缀；`build_slice_meta` 中 `kb_gps` 末节使用剥离后标题；全路径 `篇+章+节+考点` 可包含 kb_gps（末节为「贝壳战略」）
  - 测试数据: 切片标题「二、贝壳战略」「（一）第一翼：整装」「1、xxx」
- **TP12.7 法条碰撞匹配**: 策略3应正确匹配法条编号并做 BGE 细化
  - 断言: 法条编号匹配后对该批候选执行 BGE 细化，置信度 `0.88 + 0.07*(bge-0.5)` 上限 0.95
  - 测试数据: 包含法条编号的母题和知识点
- **TP12.8 BGE语义检索**: 策略4应正确进行语义检索
  - 断言: Score > 0.75自动通过，0.5 < Score <= 0.75进入策略5
  - 测试数据: 语义相似度不同的母题
- **TP12.9 LLM重排序**: 策略5应正确进行逻辑重排序并用 BGE 细化
  - 断言: LLM 判定相关时置信度 `0.80 + 0.10*(bge_score-0.5)` 上限 0.90；LLM 调用使用 `response_format={"type":"json_object"}` 强制 JSON 输出
  - 测试数据: 策略4筛选出的候选切片
- **TP12.9b LLM兜底**: 策略1-4均未命中时**必须**触发 LLM 兜底
  - 断言: 无命中时使用 BGE Top 3-5 候选（允许 Score <= 0.5）进入 LLM 判断
  - 测试数据: 低相似度但可能存在隐含关联的题目与切片
- **TP12.10 BGE 输入规范**: 母题输入=标准化路径+题干+选项+解析，知识输入=完整路径+核心内容（全文）
  - 断言: `get_question_content_for_embedding` 含解析且含标准化路径；`get_kb_content_for_embedding` 含完整核心内容未截断
  - 测试数据: 任意母题行、任意 KB 条目
- **TP12.11 每道题只保留最高置信度切片**: 输出前按母题维度过滤，每道题仅保留置信度最高的切片关联
  - 断言: 任意母题在 `knowledge_question_mapping.json` 中仅出现在其置信度最高的切片下；若多切片与该题置信度相同，可同时保留
  - 测试数据: 多切片匹配同一母题且置信度不同/相同
- **TP12.12 按母题遍历与 BGE 预计算**: 批量映射采用一道题 × 全量切片；全量切片 BGE 向量预计算、复用
  - 断言: 主循环按母题遍历；全量切片 embedding 预计算一次；输出格式与既有一致（slice → matched_questions）
  - 测试数据: N 题 × 全量切片（如 10 题 × 536 切片）
- **TP12.13 LLM Rerank 解析失败不得静默忽略**: 策略 5 的 JSON 解析失败时须报错并暴露原始响应
  - 断言: 解析失败时写调试文件、抛错终止；不 catch 后 `return []` 静默跳过。单测：传入非法 JSON 模拟 LLM 输出，须 raise 且调试文件可查。

- **TP-SLICE-1 例题切片保留**: 仅含 `examples` 的切片也应保留
  - 断言: `examples` 非空时，切片不会被 `has_content` 过滤
  - 测试数据: 标题下只有“【例】”与“【解】”文本
- **TP-SLICE-2 冒号与短句号标题识别**: 冒号/短句号标题不应被误判为正文
  - 断言: 冒号后内容较短的标题、以句号/分号/逗号结尾的短标题仍识别为标题
  - 测试数据: “一、办理流程：概述”“二、定义。”等
- **TP-SLICE-3 （1）/1. 标题长度放宽**: （1）/1. 型标题在合理长度下应识别为标题
  - 断言: （1）/1. 标题长度<=30仍识别为标题
  - 测试数据: “（1）办理条件说明”“1.税费计算规则”
- **TP-SLICE-4 附录公式表拆分**: “附录 计算公式汇总表”应拆成独立切片
  - 断言: 原切片不再包含附录公式表；存在独立“附录 计算公式汇总表”切片
  - 测试数据: 含“附录 计算公式汇总表”的原文表格
- **TP-SLICE-5 公式归属**: 公式按等号左侧名词归入最匹配切片
  - 断言: “容积率=…”归入“容积率”切片；无匹配则保留在附录切片
  - 测试数据: 公式表中的“容积率/房龄/价差率”等
- **TP-SLICE-6 路由最多 5 级**: 路径仅取自 path_stack[1:6]（层级 1～5）；第 6 级标题不参与路径
  - 断言: 任意切片的「完整路径」按 ` > ` 分割后最多 5 段；文档中出现（1）/1. 等第 6 级标题时，其内容归入当前 5 级切片，不启新切片
  - 测试数据: 含“第四篇 > … > 五、公积金提取并销户的特殊情况 > (一)退休”及其下（1）子标题的 docx
- **TP-SLICE-7 5 级下 embedding 再切片**: 同一 5 级路径下内容可依 BGE 向量划分子切片
  - 断言: 存在对 5 级路径下正文/表格分块并计算 BGE 的逻辑；子切片路径均为该 5 级路径（可带子标题元数据）
  - 测试数据: 某 5 级路径下多段正文与表格
- **TP-SLICE-8 短且高关联合并为一个切片**: 同一 5 级路径下，总长度低于阈值且块间相似度高于阈值时合并为一切片
  - 断言: 当子块总长度 < 设定长度阈值且块间 BGE 相似度 > 设定相似度阈值时，输出为单一切片而非多个
  - 测试数据: 5 级路径下两段短文本且语义高度相关

**母题检索策略**（FR2.1.1）:

**核心目标**:
- **自动关联率**：目标实现80%以上的自动关联率
- **映射准确率**：通过路径硬对齐锁死上下文，确保高准确率
- **多维输出**：支持"一对多"映射（1个母题可对应多个原子切片）

**预处理：标准化"脱水" (Normalization)**:
在进行匹配前，系统必须对母题和知识库的路径进行标准化处理，消除格式噪音：
- **关键词清洗**：移除"第X篇"、"第X章"、"第X节"、"（了解/掌握/熟悉）"、"-无需修改"等描述性词汇
- **符号归一化**：将所有全角标点、空格、斜杠统一转换为标准分隔符 `/`
- **同义词映射**：建立字典，如 `个税` ↔ `个人所得税`，`贝壳` ↔ `贝壳找房/BEIKE`
- **切片标题前缀剥离**（TP12.6c）：策略操作前对 KB 切片标题（路径最后一节）去除「一、」「二、」「（一）」「1、」等前缀；剥离后用于 kb_gps 末节构建

**五级阶梯关联策略**:

1. **策略 1：反向索引复用 (Reverse Index - P0)**
   - **逻辑**：直接碰撞现有的 `question_knowledge_mapping.json`
   - **置信度**：1.0
   - **用途**：保护存量数据，避免重复计算
   - **优先级**：最高优先级，如果匹配到则立即返回

2. **策略 2：GPS 路径坐标对齐 (Path-Based GPS Match - P1)**
   - **母题端（GPS 坐标）**：`标准化(篇/章/节/考点)`
     - 示例：`交易服务/不动产交易税费/个人所得税/个税计算`
   - **知识端（目标路径）**：`标准化(完整路径)`
   - **匹配规则**：
     - **全路径包含**：若母题的"篇+章+节+考点"完全包含在 KB 的路径中，置信度 0.95，**立即返回**，不执行后续策略（**不做 BGE 细化**）

3. **策略 3：法条与编码硬碰撞 (Statute Collision - P2)**
   - **逻辑**：正则表达式提取题干或解析中的法条编号（如《民法典》第215条）
   - **匹配**：若知识切片标题或正文中出现相同法条编号，基础 0.88；**对该批候选执行 BGE 细化**：`0.88 + 0.07 * (bge_score - 0.5)` 上限 0.95
   - **适用场景**：法律条文相关的题目和知识切片

4. **策略 4：语义向量检索 (BGE Vector Retrieval - P3)**
   - **模型**：`BAAI/bge-small-zh-v1.5`
   - **输入构建（Context Buffer）**：
     - **母题输入** = `[标准化路径]` + `[题干]` + `[选项拼合]` + `[解析]`
     - **知识输入** = `[完整路径]` + `[核心内容]`（全文，不截断）
   - **评分逻辑**：
     - `Score > 0.75`：自动通过，置信度 = Score
     - `0.5 < Score <= 0.75`：进入策略 5（逻辑复核），置信度 = Score
     - `Score <= 0.5`：不匹配

5. **策略 5：LLM 专家逻辑重排序 (LLM Reranking - P4)**
   - **触发条件**：策略 4 筛选出的前 3-5 个候选切片（Score > 0.5 且 <= 0.75）
   - **Prompt 逻辑**：
     > "你是一个房产交易专家。这道题考的是【母题 GPS 路径】，题干是【内容】。请从以下 3 个知识切片中选出能支撑解题的项。如果不相关，请输出 False。"
   - **输出格式**：JSON，包含 `is_related` (bool) 和 `related_indices` (array)
   - **置信度**：LLM 判定相关时，用其 BGE 分数细化：`0.80 + 0.10 * (bge_score - 0.5)` 上限 0.90

**关联原则：只关联最相关的**:
- **核心原则**：只关联最相关的母题，不关联所有相关的母题
- **最相关判断标准**：
  - 按策略优先级：策略1 > 策略2 > 策略3 > 策略4 > 策略5
  - 同策略内按置信度降序：置信度高的优先
  - 如果存在多个最相关的母题（置信度相同或接近，差异<0.05），可以关联多个
- **关联阈值**：
  - 策略1（反向索引）：置信度1.0，匹配到立即返回，不再执行后续策略
  - 策略2（GPS路径）：**全路径**匹配到立即返回
  - 策略3（法条碰撞）：经 BGE 细化后只保留置信度最高的匹配（如果多个相同置信度，保留所有）
  - 策略4（BGE向量）：Score > 0.75自动通过，只保留Score最高的前N个（N≤3，如果Score相同或接近，保留所有）
  - 策略5（LLM重排序）：用 BGE 细化置信度，只保留LLM判定为相关的匹配
- **多匹配处理**：
  - 如果最相关的有多个（置信度相同或接近，差异<0.05），可以关联多个
  - 如果最相关的只有一个，只关联一个
  - 输出时按置信度降序排列
- **每道题维度过滤**：每道母题只保留置信度最高的切片；置信度一致可保留多个。输出前对映射做按题过滤。

**输出格式**: `knowledge_question_mapping.json`
```json
{
  "0": {
    "完整路径": "第一篇 > 第一章 > ...",
    "掌握程度": "掌握",
    "matched_questions": [
      {
        "question_index": 3,
        "confidence": 0.95,
        "method": "GPS_FullPath",
        "evidence": {
          "reason": "路径完全匹配：第四篇/第四章/第二节/个人所得税计算"
        }
      }
    ],
    "total_matches": 2,
    "methods_used": ["GPS_FullPath", "LLM_Logic"]
  }
}
```

**实施步骤**:
1. **第一步**：生成"干净"的映射候选 (Batch Process) - 执行策略 1-3，解决 50% 以上的简单匹配
2. **第二步**：执行语义召回与排序 (Vector & LLM) - 对复杂题型执行向量检索和 LLM 复核
3. **第三步**：输出关联报告 (Validation Report) - 生成 CSV，包含：`母题题干 | 考点路径 | 关联 Slice ID | 关联理由 | 置信度`

**教材原题优先机制** (FR2.3):
- **教材原题优先**：优先仿照教材原题的出题逻辑、计算方式和陷阱设置（知识点切片内置的examples，100%匹配）
- **外部母题补充**：外部母题仅作补充参考（从母题库检索的相似题目）
- **严禁照搬数据**：必须做数据重构（人名、金额、日期、房产面积）
- **注意**：教材原题和外部母题一起作为例子参考，但教材原题优先级更高

**测试点**:
- **TP13.1 教材原题优先**: 应优先使用builtin_examples
  - 断言: `examples[0]` 来自 `kb_chunk['结构化内容']['examples']`（如果存在）
  - 测试数据: kb_chunk包含`结构化内容.examples`

### 6.3 计算器接口

**类**: `RealEstateCalculator` (calculation_logic.py)

**统一接口规范**:
- 所有计算函数都是静态方法（`@staticmethod`）
- 函数名以 `calculate_` 开头
- 参数使用有意义的变量名
- 返回值是计算结果（数值类型）

**代码执行接口**:
- **安全执行环境**:
  - 限制可导入的模块：仅允许数学和时间相关模块（`math`, `datetime`, `decimal`, `time`等）
  - 拦截非法导入
  - 执行时间参数：默认5秒（当前未实现强制超时中断）
  - 异常捕获：捕获执行错误、导入错误
  - 禁止导入危险模块（`os`, `sys`, `subprocess`等）
  - 仅允许安全的内置函数（`abs`, `round`, `min`, `max`, `sum`, `len`, `int`, `float`, `str`, `bool`, `type`, `isinstance`, `range`, `enumerate`, `zip`, `print`等）
  - 禁止访问危险内置函数（`eval`, `exec`等）

**计算函数列表**（16种，当前实现）:
1. `calculate_loan_amount()`: 商业贷款金额计算
2. `calculate_provident_fund_loan()`: 公积金贷款计算
3. `calculate_vat()`: 增值税及附加计算（税率5.3%）
4. `calculate_deed_tax()`: 契税计算
5. `calculate_income_tax()`: 个人所得税计算
6. `calculate_loan_payment()`: 贷款月供计算
7. `calculate_building_area()`: 建筑面积计算
8. `calculate_floor_area_ratio()`: 容积率计算
9. `calculate_house_age()`: 房龄计算（支持两种模式）
   - **通用房龄**：房龄 = 当前年份 - 竣工年份
   - **贷款用房龄**：房龄 = 50 - (当前年份 - 竣工年份)（用于"房龄+贷款年限≤50年"规则）
10. `calculate_land_transfer_fee()`: 土地出让金计算（经济适用房、按经适房管理、公房等）
11. 等共16种工具

**计算器参数提取规则** (FR4.6):
- **必须提取具体数值**：从题干或参考材料中提取具体数值（如80平方米、1560元、2025年、1993年）
- **禁止描述性文字**：不能使用描述性文字（如"成本价"、"建筑面积"、"建成年代"）
- **参数类型验证**：自动处理字符串参数，转换为数值类型
- **单位统一**：注意单位的统一（平方米、元、年等）
- **参数重构**：如果原文提供的是具体案例（如"原价180万"），必须修改这个数值，以便生成全新的题目
- **业务逻辑保持**：修改后的数值必须符合业务逻辑（例如：网签价通常高于原值，日期必须在政策有效期内）
- **常量保留**：政策规定的固定数值（如税率5%、年限5年）不能修改

**测试点**:
- **TP14.1 契税计算**: 应正确计算不同情况下的契税
  - 断言: `calculate_deed_tax(1000000, 120, True, False, True)` == 1000000 * 0.01（首套≤140㎡）
  - 测试数据: 计税价100万，面积120㎡，首套，住宅
- **TP14.2 增值税计算**: 应使用税率5.3%
  - 断言: `calculate_vat(4000000, 2000000, 3, True, True)` == (4000000 - 2000000) / 1.05 * 0.053
  - 测试数据: 计税价400万，原值200万，持有3年，普通住宅，住宅
- **TP14.3 房龄计算两种模式**: 应支持通用房龄和贷款用房龄
  - 断言: `calculate_house_age(2025, 1993, False)` == 32（通用房龄）
  - 断言: `calculate_house_age(2025, 1993, True)` == 18（贷款用房龄：50-32）
  - 测试数据: 当前年份2025，竣工年份1993
- **TP14.4 边界情况处理**: 应正确处理边界值
  - 断言: `calculate_deed_tax(1000000, 140, True, False, True)` == 1000000 * 0.01（面积=140㎡）
  - 测试数据: 面积等于临界值140㎡

### 6.4 数据验证接口

**模型**: `ExamQuestion` (Pydantic BaseModel)

**验证规则**:
- `题干`: 必需，字符串
- `选项1`, `选项2`: 必需，字符串
- `选项3-8`: 可选，默认空字符串
- `正确答案`: 必需，正则表达式 `^[ABCDEFGH]+$`（A-H或组合）
- `解析`: 必需，字符串
- `难度值`: 必需，浮点数，范围 0.0-1.0（`ge=0, le=1`）

**错误处理**:
- 捕获 `ValidationError` 并显示格式错误
- 提供友好的错误提示

**测试点**:
- **TP15.1 必需字段验证**: 缺少必需字段应抛出ValidationError
  - 断言: `ExamQuestion(**{})` 应抛出ValidationError
  - 测试数据: 空字典
- **TP15.2 正确答案格式验证**: 格式不正确应抛出ValidationError
  - 断言: `ExamQuestion(正确答案="XYZ")` 应抛出ValidationError（不在A-H范围内）
  - 测试数据: 正确答案为"XYZ"
- **TP15.3 难度值范围验证**: 超出范围应抛出ValidationError
  - 断言: `ExamQuestion(难度值=1.5)` 应抛出ValidationError（>1.0）
  - 测试数据: 难度值为1.5
- **TP15.4 验证通过**: 有效数据应能创建实例
  - 断言: `ExamQuestion(题干="...", 选项1="A", 选项2="B", 正确答案="A", 解析="...", 难度值=0.5)` 应成功创建
  - 测试数据: 所有必需字段有效

## 7. 安全机制

### 7.1 代码执行安全

**沙箱执行环境**:
- 限制可导入的模块：仅允许数学和时间相关模块
- 拦截非法导入
- 执行时间参数：默认5秒（当前未实现强制超时中断）
- 异常捕获：捕获执行错误、导入错误
- 禁止导入危险模块（`os`, `sys`, `subprocess`等）
- 仅允许安全的内置函数
- 禁止访问危险内置函数（`eval`, `exec`等）

**实现位置**: `exam_graph.py` 中的代码执行函数

**测试点**:
- **TP16.1 模块白名单**: 应只允许导入白名单模块
  - 断言: 尝试导入`os`应抛出ImportError
  - 测试数据: 代码包含`import os`
- **TP16.2 执行时间参数**: 代码执行函数应保留 max_execution_time 入参
  - 断言: `execute_python_code` 默认参数为`max_execution_time=5.0`
  - 测试数据: 调用 `execute_python_code("result = 100")`
- **TP16.3 异常捕获**: 应捕获并返回错误信息
  - 断言: 执行错误代码应返回错误信息，不中断流程
  - 测试数据: 代码包含`1/0`（除零错误）
- **TP16.4 结果提取**: 应正确提取计算结果
  - 断言: 如果代码包含`result = 100`，则返回值为100
  - 测试数据: 代码包含`result = 100`

### 7.2 数据安全

**数据验证**:
- 使用 Pydantic 模型验证所有输入数据
- 捕获 `ValidationError` 并处理

**数据质量过滤**:
- 自动过滤NaN值和空字符串
- 必需字段检查：题干、选项1、选项2、正确答案、解析

**测试点**:
- **TP17.1 NaN值过滤**: 应过滤NaN值
  - 断言: `_is_valid_example(row)` == False 当 `row['题干']` 为NaN
  - 测试数据: 母题包含NaN值
- **TP17.2 空字符串过滤**: 应过滤空字符串
  - 断言: `_is_valid_example(row)` == False 当 `row['题干']` 为空字符串
  - 测试数据: 母题包含空字符串
- **TP17.3 必需字段验证**: 缺少必需字段应返回False
  - 断言: `_is_valid_example(row)` == False 当缺少任一必需字段
  - 测试数据: 母题缺少"解析"字段

### 7.3 API 安全

**API Key 管理**:
- 从文件读取：`填写您的Key.txt`
- 不在代码中硬编码
- UI中支持密码输入（`type="password"`）

**代理配置**:
- 支持HTTP/HTTPS代理（使用OpenAI兼容接口）
- 通过环境变量配置

## 8. 性能优化

### 8.1 缓存机制

**Streamlit 缓存**:
- `@st.cache_resource`: KnowledgeRetriever 单例
- 避免重复加载知识库和母题库

### 8.2 检索优化

**知识点映射文件**:
- 优先使用映射文件（`question_knowledge_mapping.json`）
- 避免每次调用BGE语义向量检索

**TF-IDF 预计算**:
- 在初始化时预计算TF-IDF矩阵
- 避免重复计算

### 8.3 并发控制

**流式输出**:
- 使用 LangGraph 的 `stream()` 方法实现流式输出
- 实时展示每个节点的处理过程和日志
- 提升用户体验

### 8.4 性能指标

**目标性能**:
- 知识库加载时间 < 5 秒
- 单题生成时间 < 30 秒（取决于 LLM 响应速度）
- 母题检索时间 < 1 秒（使用映射文件）
- 支持流式输出，无阻塞

**测试点**:
- **TP18.1 知识库加载性能**: 应 < 5秒
  - 断言: `load_time < 5.0`
  - 测试数据: 完整知识库（1712条）
- **TP18.2 单题生成性能**: 应 < 30秒（平均）
  - 断言: `generate_time < 30.0`（平均）
  - 测试数据: 生成10题，计算平均时间
- **TP18.3 母题检索性能**: 应 < 1秒
  - 断言: `retrieval_time < 1.0`
  - 测试数据: 使用映射文件检索

## 9. 错误处理

### 9.1 LLM 调用失败

**重试机制**:
- 模型调用: 最多5次重试，指数退避
- OpenAI兼容API: 最多10次重试，指数退避
- 可重试错误：503、429、RESOURCE_EXHAUSTED、SSL、EOF、timeout、connection

**错误处理**:
- 捕获异常并显示详细错误信息
- 网络错误时提示检查API Key或网络连接

**测试点**:
- **TP19.1 模型重试机制**: 应最多重试5次
  - 断言: 连续失败时，重试次数 <= 5
  - 测试数据: 模拟503错误
- **TP19.2 指数退避**: 等待时间应递增
  - 断言: 第n次重试的等待时间 = backoff_seconds[n]
  - 测试数据: 连续失败，记录等待时间
- **TP19.3 可重试错误识别**: 应正确识别可重试错误
  - 断言: 503、429错误应触发重试，400错误应立即停止
  - 测试数据: 模拟不同错误码

### 9.2 JSON 解析失败

**自动修复**:
- 尝试多种解析策略（正则提取、代码块提取等）
- 提供默认值和错误处理

**测试点**:
- **TP20.1 标准JSON解析**: 应能解析标准JSON
  - 断言: `parse_json_from_response('{"key": "value"}')` == `{"key": "value"}`
  - 测试数据: 标准JSON字符串
- **TP20.2 Markdown代码块提取**: 应能从代码块中提取JSON
  - 断言: `parse_json_from_response('```json\n{"key": "value"}\n```')` == `{"key": "value"}`
  - 测试数据: JSON在markdown代码块中
- **TP20.3 修复策略**: 应尝试多种策略
  - 断言: 解析失败时，函数尝试至少2种解析策略
  - 测试数据: 格式不规范的JSON字符串

### 9.3 质量验证失败

**自动重试**:
- 最多3次修复循环（`retry_count >= 3` 时触发自愈）
- 超限后自愈机制：`retry_count >= 3` 时返回 `self_heal`，直接输出结果
- 严重问题：连续2次仍失败（`retry_count >= 2`）则交给Fixer强修
- 轻微问题：直接交给Fixer修复

### 9.4 计算器调用失败

**降级处理**:
- 错误捕获和日志记录
- 计算器调用失败时，降级为概念题处理
- 代码执行失败时，返回错误信息但不中断流程

**测试点**:
- **TP21.1 计算器失败降级**: 应降级为概念题
  - 断言: 计算器调用失败时，不中断流程，继续生成概念题
  - 测试数据: 计算器函数抛出异常
- **TP21.2 错误信息传递**: 应记录错误信息
  - 断言: 错误信息记录在logs或tool_usage中
  - 测试数据: 计算器调用失败

## 10. 部署方案

### 10.1 环境要求

**Python 版本**: Python 3.8+

**依赖包**:
- `streamlit`: Web界面框架
- `langgraph`: 智能体编排
- `langchain`: LLM调用封装
- `pandas`: 数据处理
- `openpyxl`: Excel文件处理
- `pydantic`: 数据验证
- `scikit-learn`: TF-IDF向量化
- `openai`: OpenAI兼容API
- 等（见 `requirements.txt`）

### 10.2 配置文件

**API Key 配置**:
- 文件路径：`填写您的Key.txt`
- 格式：
  ```
  DEEPSEEK_API_KEY=your_key_here
  CRITIC_API_KEY=your_key_here
  ARK_API_KEY=your_key_here
  ```

**数据文件**:
- `bot_knowledge_base.jsonl`: 知识库（1712条知识点）
- `存量房买卖母卷ABCD.xls`: 母题库（408道母题）
- `question_knowledge_mapping.json`: 知识点映射文件

### 10.3 启动方式

**本地运行**:
```bash
streamlit run app.py
```

**部署到服务器**:
- 可以使用 Streamlit Cloud、Docker 容器等方式部署
- 确保数据文件可访问
- 配置API Key环境变量或文件

### 10.4 监控和日志

**日志记录**:
- 每个节点都有日志输出（`logs` 字段）
- 使用emoji标识不同节点（🤖路由、🐯照猫画虎、✍️作家、🕵️批评家、🔧修复者）
- 显示重试次数、错误原因、决策依据

**UI 展示**:
- 实时展示每个节点的执行状态
- 使用 `st.status()` 展示每题的生成状态
- 使用 `st.progress()` 展示整体进度

## 11. 测试策略

### 11.1 单元测试

**测试范围**:
- 计算器函数（16种计算函数，当前实现）
- 数据验证（Pydantic模型）
- 知识检索（KnowledgeRetriever）
- 题型识别和匹配

**测试文件**:
- `test_calc_question.py`: 计算题测试
- `test_audit_logic.py`: 审计逻辑测试
- `test_full_workflow.py`: 完整工作流测试

### 11.2 集成测试

**测试范围**:
- LangGraph 工作流集成
- 节点间状态传递
- 反馈循环机制
- 约束条件传递

**测试文件**:
- `test_exam_graph_simple.py`: 简单工作流测试
- `test_loop_mechanism.py`: 循环机制测试
- `test_reroute_logic.py`: 重路由逻辑测试

### 11.3 端到端测试

**测试范围**:
- 完整出题流程（从UI输入到题目输出）
- 不同题型、难度、模式的组合测试
- 计算题完整流程测试

**测试文件**:
- `test_complete_system.py`: 完整系统测试
- `test_complex_scenarios.py`: 复杂场景测试

### 11.4 性能测试

**测试指标**:
- 知识库加载时间
- 单题生成时间
- 母题检索时间
- 流式输出延迟

**测试文件**:
- `run_batch_test.py`: 批量测试
- `run_retrieval_hit_rate.py`: 检索命中率测试

### 11.5 质量验收

**验收标准**:
- 题目准确性 ≥ 95%
- 干扰项合理性（人工抽检）
- 解析清晰度（人工抽检）
- 风格一致性（与母题对比）
- 难度值符合率 100%（在指定范围内）
- 题型符合率 100%
- 模式符合率 100%

**测试点**:
- **TP22.1 难度值符合率**: 应100%符合
  - 断言: 所有题目的难度值都在指定范围内
  - 测试数据: 生成100题，难度范围(0.5, 0.7)
- **TP22.2 题型符合率**: 应100%符合
  - 断言: 所有题目的题型与指定题型一致
  - 测试数据: 生成100题，指定题型"单选题"
- **TP22.3 模式符合率**: 应100%符合
  - 断言: 所有题目的模式与指定模式一致
  - 测试数据: 生成100题，指定模式"严谨"

## 12. 扩展性设计

### 12.1 模块化设计

**分层架构**:
- 用户交互层、应用编排层、智能体编排层、知识检索层、计算工具层解耦
- 易于替换 LLM 后端
- 易于添加新的智能体节点

### 12.2 可配置性

**配置项**:
- API Key 从文件读取
- 模型配置可切换
- 代理配置可选
- 知识库路径可配置

### 12.3 可扩展性

**添加新智能体**:
- 基于 LangGraph 的节点架构，支持动态添加新节点和连接
- 统一的状态接口，所有节点使用统一的状态格式

**添加新计算工具**:
- 计算工具模块化，所有计算函数集中管理
- 统一的调用接口，所有计算函数使用统一的参数格式和返回格式

**切换 LLM 后端**:
- 统一接口，所有模型通过统一接口调用
- 根据配置自动选择调用方式
- 所有节点统一使用同一模型

## 13. 用户交互需求

### 13.1 UI 配置 (F8)

**API 配置** (FR8.1):
- 支持 OpenAI兼容模型（推荐）
- 支持 OpenAI / DeepSeek
- 支持 OpenAI兼容模型
- 支持从文件读取API Key（`填写您的Key.txt`）

**代理设置** (FR8.2):
- 可选配置HTTP/HTTPS代理
- 适使用OpenAI兼容接口

**章节选择** (FR8.3):
- 多选章节
- 全选所有章节
- 仅选中计算类章节（自动筛选）

**出题参数设置** (FR8.4):
- 题目数量（1-200）
- 难度偏好（简单/中等/困难/随机）
  - **随机难度均分策略**：当选择"随机"时，为每道题随机分配难度范围，确保低中高难度比例均衡（1:1:1）
- 题型选择（单选/多选/判断/随机）
- 出题筛选条件（基础概念/理解记忆、实战应用/推演、随机）
  - **当前代码口径说明**：Streamlit 界面仍展示“灵活/严谨”，并在引擎中做语义映射兼容

**测试点**:
- **TP24.1 API Key读取**: 应从文件正确读取API Key
  - 断言: 如果文件包含`OPENAI_API_KEY=xxx`，UI输入框应显示xxx
  - 测试数据: `填写您的Key.txt`包含API Key
- **TP24.2 出题参数设置**: 应正确传递参数到工作流
  - 断言: 设置的难度、题型、模式能正确传递到config
  - 测试数据: 选择难度"中等 (0.5-0.7)"，题型"单选题"，模式"灵活"
- **TP24.3 随机难度均分**: 当选择"随机"难度生成多题时，应确保低中高难度比例接近 1:1:1
  - 断言: 生成30道题，简单题数量应在8-12题，中等题8-12题，困难题8-12题
  - 测试数据: 难度选择"随机"，生成30道题
- **TP24.4 教材生效按钮可用性 ✅**: 仅当存在“双核对完成”知识切片时允许点击生效
  - 断言: 当存在至少一个知识切片同时满足`映射核对完成=True`且`切片核对完成=True`时，教材“生效”按钮为可点击
  - 测试数据: 构造知识切片状态集合，至少1条双核对完成
- **TP24.5 教材生效按钮禁用态 ✅**: 不存在“双核对完成”知识切片时必须禁用生效
  - 断言: 当全部知识切片均不满足双核对完成条件时，教材“生效”按钮为禁用（置灰）
  - 测试数据: 构造知识切片状态集合，0条双核对完成
- **TP24.6 教材生效后端拦截 ✅**: 后端必须与前端同口径校验
  - 断言: 当前端被绕过直接调用生效接口时，若不存在双核对完成切片，后端返回失败并拒绝生效
  - 测试数据: 直接调用教材生效接口，知识切片状态不满足条件
- **TP24.7 教材上传文本入口**: 应支持文本直传并进入切片流程
  - 断言: `materials/upload` 在仅提交 text 时可成功生成切片文件与版本记录
  - 测试数据: multipart 请求仅含 text 字段
- **TP24.8 教材版本管理操作**: 应支持重切片/重映射/下线/删除流程
  - 断言: `reslice`、`remap`、`archive`、`delete` 接口返回状态与物料状态变更一致
  - 测试数据: 存在教材版本的租户数据
- **TP24.9 异步出题任务创建**: 应支持创建任务并返回 task_id
  - 断言: `POST /generate/tasks` 返回任务ID，初始状态为 pending/running
  - 测试数据: 至少1条 approved 切片
- **TP24.10 异步任务查询**: 应支持任务列表与详情查询
  - 断言: `GET /generate/tasks` 与 `GET /generate/tasks/<task_id>` 返回一致的进度字段与结果摘要
  - 测试数据: 已创建的出题任务

- **TP24.11 新建异步任务页面初始状态**  
  - 场景: 在 admin-web 中点击“新建出题任务”按钮，进入出题任务创建页面。  
  - 断言:  
    - 进入页面时，结果区域的标题、题目列表和错误列表均为空，不展示任何历史任务的生成记录或过程日志；  
    - 仅在当前新建任务创建成功并开始执行后，才根据本次任务的执行进度填充结果区域内容。  

### 13.2 过程可视化 (F9)

**实时展示生成进度** (FR9.1):
- 流式输出，实时更新
- 显示当前处理的题目序号

**展示 Router 决策过程** (FR9.2):
- 显示选中知识点
- 显示掌握程度
- 显示核心内容片段
- 显示计算相关度、法律相关度
- 显示派发的专家

**展示照猫画虎的母题范例** (FR9.3):
- 展示参考的母题数量
- 展示每道母题的题干、选项、答案、解析

**展示计算器调用详情** (FR9.4):
- 显示使用的计算函数
- 显示提取的参数
- 显示计算结果
- 显示执行状态

**展示初稿内容** (FR9.5):
- 显示专家节点生成的初稿

**展示 Critic 评审过程** (FR9.6):
- 显示评审结果（通过/不通过）
- 显示评审原因
- 显示问题类型（严重/轻微）

**展示 Fixer 修复过程** (FR9.7):
- 显示修复策略
- 显示修复后的题目

**展示最终生成的题目** (FR9.8):
- 完整题目展示（题干、选项、答案、解析）
- 显示难度值
- 显示知识点层级

**展示重路由过程** (FR9.9):
- 显示重路由原因
- 显示重路由次数

**测试点**:
- **TP25.1 Router决策展示**: 应显示知识点、掌握程度、相关度、派发专家
  - 断言: UI中显示`router_details['path']`、`router_details['mastery']`、`router_details['agent']`
  - 测试数据: Router节点执行后
- **TP25.2 Critic评审展示**: 应显示问题类型和修复策略
  - 断言: UI中显示`critic_result['issue_type']`和`critic_result['fix_strategy']`
  - 测试数据: Critic节点执行后，标记为问题
- **TP25.3 重路由展示**: 应显示重路由原因和重试次数
  - 断言: UI中显示"重新路由"标签和`retry_count`
  - 测试数据: Router节点检测到重路由（retry_count > 0）
- **TP25.4 Fixer修复展示**: 应显示修复策略和修复依据
  - 断言: UI中显示`fix_strategy`和`fix_reason`
  - 测试数据: Fixer节点执行后
- **TP25.5 流式输出**: 应能实时接收事件
  - 断言: `for event in graph_app.stream(...)` 能实时迭代事件
  - 测试数据: 执行完整工作流
- **TP25.6 映射审核状态机与切片一致**: 映射核对状态统一为`pending/approved`
  - 断言: 任意非`pending/approved`状态均被归一为`pending`
  - 测试数据: 录入`auto_pending/confirmed/rejected/remapped`等历史状态
- **TP25.7 映射审核批量口径一致（对齐切片）**: 批量提交语义与切片核对一致
  - 断言: 提交`confirm_status=approved/pending`后，后端展示与前端筛选一致
  - 测试数据: 两端分别执行批量提交
- **TP25.8 映射页切片呈现与切片核对一致**: 同一切片在两页渲染结果一致
  - 断言: 映射页“切片内容（完整）”与切片核对页均支持 Markdown + Mermaid 渲染，图片链接注入规则一致（不污染代码块）
  - 测试数据: 含 Mermaid 代码块、图片引用、表格解析文本的切片
- **TP25.9 映射页图片呈现一致性**: 图片入口与解析展示与切片核对页一致
  - 断言: 映射页可打开切片图片并展示图片解析，表格型解析使用 Markdown 渲染，非表格解析使用文本渲染
  - 测试数据: 含 `images` 字段的映射记录

### 13.3 结果导出 (F10)

**题目展示** (FR10.1):
- 完整题目信息（题干、选项、答案、解析、难度值、考点）
- 支持复制功能

**Excel导出** (FR10.2)（可选）:
- 导出为Excel格式
- 包含所有题目信息
- 导出字段包含“切片原文/结构化内容”（保持原样）

**测试点**:
- **TP26.1 Excel导出**: 应能生成有效的Excel文件
  - 断言: 导出的Excel文件能被pandas.read_excel()正确读取
  - 测试数据: 生成5道题目
- **TP26.2 字段完整性**: Excel应包含所有必需字段
  - 断言: Excel文件包含"题干"、"选项1"、"选项2"、"正确答案"、"解析"、"难度值"等字段
  - 测试数据: 生成的题目包含所有字段
- **TP26.3 切片原文导出**: Excel应包含切片原文字段且内容一致
  - 断言: 导出字段包含“切片原文/结构化内容”，内容与知识库一致
  - 测试数据: 任意包含结构化内容的切片

## 13b. 成本与单位经济可观测（NFR6）

**范围**: 管理端 QA Run 落盘与告警、batch_metrics 中的 CPVQ 及阈值告警、管理端展示。

**测试点**:
- **TP31.1 CPVQ 计算与落库**: 当次 Run 的 batch_metrics 中必须包含 `cpvq`
  - 断言: 当 `saved_count > 0` 时，`cpvq == total_cost / saved_count`（数值，精度与 total_cost 一致）；当 `saved_count == 0` 时，`cpvq` 为 null 或不存在
  - 测试数据: 构造一次 QA Run payload（含 total_cost、saved_count、questions），验证 _build_qa_run_payload 或等价逻辑输出的 batch_metrics.cpvq
- **TP31.2 CPVQ 告警**: 配置了 cpvq_max 且 run 的 cpvq 超标时，应生成 batch_metric 告警
  - 断言: 当 `saved_count > 0` 且 `cpvq > cpvq_max` 时，_build_alerts_for_run 输出中包含 type 为 batch_metric、metric 为 cpvq 的告警项，并写入 qa_alerts
  - 测试数据: 带 cpvq_max 的 thresholds、cpvq 超标的 qa_run
- **TP31.3 单位经济视图**: Run 详情或导出中应包含 cost_summary（by_question / by_node / by_model）及 batch_metrics 中的 avg_cost_per_question、cpvq、saved_count
  - 断言: QA Run 读取接口返回的 payload 中同时存在 cost_summary 与 batch_metrics，且 batch_metrics 含 cpvq、saved_count、total_cost
  - 测试数据: 已落盘的 qa_runs.jsonl 中一条记录

## 14. 用户场景

### 场景 1: 生成中等难度的单选题
1. 用户打开系统
2. 配置 API Key（DeepSeek）
3. 选择章节 "第一篇 > 第一章 > 第一节"
4. 选择题型 "单选题"
5. 选择难度 "中等 (0.5-0.7)"
6. 选择模式 "灵活"
7. 设置数量 "5 题"
8. 点击"开始出题"
9. 系统展示：
   - Router 决策（知识点、掌握程度、派发专家）
   - 母题范例（参考的3道母题）
   - 生成过程（初稿、格式化、验证）
   - 最终题目（难度值在0.5-0.7范围内）
10. 用户复制题目

### 场景 2: 生成计算题（基础概念/理解记忆）
1. 用户选择章节 "第二篇 (金融税费相关)"
2. 勾选"仅选中计算类章节"
3. 选择题型 "单选题"
4. 选择难度 "困难 (0.7-0.9)"
5. 选择模式 "严谨"
6. 点击"开始出题"
7. 系统：
   - Router 识别为计算类 → CalculatorAgent → calculator_node
   - 获取计算类母题范例
   - Calculator 动态生成Python计算代码并执行（展示函数、参数、结果）
   - 生成包含计算的题目（基础概念/理解记忆，可不做场景化包装）
   - Writer 格式化（验证难度值在0.7-0.9范围内）
   - Critic 用计算器验证答案
   - 输出最终题目（难度值符合要求）
8. 用户查看题目和计算详情

### 场景 3: 生成判断题（简单难度）
1. 用户选择章节
2. 选择题型 "判断题"
3. 选择难度 "简单 (0.3-0.5)"
4. 选择模式 "灵活"
5. 点击"开始出题"
6. 系统：
   - 只检索判断题母题（选项1=正确, 选项2=错误）
   - 生成只有"正确/错误"两个选项的题目
   - 难度值在0.3-0.5范围内
7. 输出判断题

### 场景 4: 修复不符合难度要求的题目
1. 用户选择难度 "中等 (0.5-0.7)"
2. 系统生成题目，但难度值为0.3（不符合要求）
3. Critic 检测到难度不符合要求，标记为严重问题
4. 触发 Router 重新路由
5. 重新生成题目，难度值调整为0.6（符合要求）
6. 输出最终题目

## 15. 风险与应对

### 风险 1: LLM 生成不稳定
- **应对**: Critic 验证 + Fixer 修复 + 最多 3 次重试 + 自愈机制

### 风险 2: 母题数量不足
- **应对**: BGE语义向量检索 + 知识点映射文件

### 风险 3: 计算器调用失败
- **应对**: 错误捕获 + 日志记录 + 降级为概念题

### 风险 4: 数据质量问题
- **应对**: 数据验证 + NaN 过滤 + 必需字段检查

### 风险 5: 难度值不符合要求
- **应对**: ✅ 多节点验证（Writer、Critic、Fixer）+ 自动调整机制

### 风险 6: 题型不符合要求
- **应对**: ✅ 所有节点严格遵循题型约束 + 修复模式也遵循

### 风险 7: 模式不符合要求
- **应对**: ✅ 提示词中明确区分三档筛选条件 + 修复模式也遵循

### 风险 8: 代码执行安全问题
- **应对**: ✅ 沙箱执行环境 + 模块白名单 + 执行时间参数（待补强超时中断）+ 内置函数限制

### 风险 9: 地理/时间逻辑错误
- **应对**: ✅ Writer节点检查 + Critic节点验证 + 明确Fail条件

### 风险 10: 题干直接给出答案
- **应对**: ✅ Critic节点检测题干中是否直接包含正确答案的关键词（导致无需理解即可选出即Fail）
- **注意**: 允许正确答案选项与教材原文定义一致，这是正常的考察方式

### 风险 11: AI幻觉/生造词
- **应对**: ✅ Critic节点检测生造词（如"外接"、"上交"等非标准术语）

## 16. 约束条件

### 16.1 技术约束

- **C1**: 必须使用 Streamlit 作为 UI 框架 ✅
- **C2**: 必须使用 LangGraph 进行智能体编排 ✅
- **C3**: LLM 需支持 JSON 格式输出 ✅
  - 所有节点都要求LLM返回JSON格式
  - 支持从markdown代码块中提取JSON
- **C4**: Python 环境 ✅
  - 需要安装相关依赖包（pandas、openpyxl、pydantic、scikit-learn、openai、streamlit、langgraph、langchain等）

### 16.2 业务约束

- **C5**: 题目内容必须 100% 准确，不得出现幻觉
- **C6**: 题干中禁止出现"根据材料"、"依据参考资料"等提示语
- **C7**: 干扰项必须似是而非，不能一眼假
- **C8**: 必须出场景化、对经纪人实操有指导意义的题目；禁止定义题与仅考概念辨析的低实操价值题
- **C9**: 选项维度必须一致，禁止跨维度
- **C10**: 严禁直接照搬原文案例中的具体数据（必须做数据重构）
- **C11**: 难度值必须符合用户指定的难度范围
- **C12**: 题型必须符合用户选择（单选/多选/判断）
- **C13**: 出题筛选条件必须符合用户选择（基础概念/理解记忆、实战应用/推演、随机）
- **C14**: 地理一致性：教材限定城市时，题干必须在该城市；通用规则不得写具体城市
- **C15**: 时间逻辑：原文未给具体时间时，不得添加具体年份/日期；仅保留相对时间
- **C16**: 禁止题干直接给出答案：题干中不得直接包含正确答案的关键词，导致考生无需理解即可通过文字匹配选出答案。但允许正确答案选项与教材原文定义一致。
- **C17**: 禁止生造词：必须使用标准业务术语，禁止AI幻觉词汇
- **C18**: 禁止"最XX"考法：禁止用"最重要/最关键/重点/主要"等表述
- **C19**: 唯一答案强制校验：逐条假设每个错误选项为真，验证是否"必错"

### 16.3 数据约束

- **C20**: 知识库和母题库为静态数据，不可修改 ✅
  - 知识库和母题库文件为只读，系统仅读取，不写入
- **C21**: 母题数据可能存在 NaN，需过滤 ✅
  - 自动过滤NaN值和空字符串
  - 必需字段检查：题干、选项1、选项2、正确答案、解析
  - 无效数据不参与母题检索
- **C22**: 知识点映射文件需预先生成 ✅
  - 如果映射文件不存在，使用BGE语义向量检索
- **C23**: 知识库字段约束 ✅
  - **必需字段**：`完整路径`、`核心内容`（如果缺失，从`结构化内容`自动构建）
  - **可选字段**：`掌握程度`、`结构化内容`、`Bot专用切片`
  - **结构化内容字段**：`context_before`、`context_after`、`tables`、`formulas`、`examples`、`key_params`（可选）
- **C24**: 母题库字段约束 ✅
  - **必需字段**（用于数据质量过滤）：`题干`、`选项1`、`选项2`、`正确答案`、`解析`
  - **可选字段**：`选项3-8`、`考点`、`难度值`
  - **题型识别**：
    - 判断题：选项1为"正确"且选项2为"错误"
    - 多选题：正确答案长度>1且所有字符在A-E中
    - 单选题：默认
- **C25**: 输出题目字段约束 ✅
  - **必需字段**（`ExamQuestion`模型）：`题干`、`选项1`、`选项2`、`正确答案`、`解析`、`难度值`
  - **可选字段**：`选项3-8`（默认空字符串）
  - **正确答案格式**：正则表达式`^[ABCDEFGH]+$`（A-H或组合）
  - **难度值范围**：0.0-1.0（`ge=0, le=1`）
  - **自动生成字段**：`考点`、`一级知识点`、`二级知识点`、`三级知识点`、`四级知识点`、`来源路径`、`_was_fixed`、`是否修复`

## 17. 已知问题和限制

### 13.1 性能限制

- 单题生成时间受 LLM 响应速度影响，首次调用可能需要10-30秒初始化
- 批量生成时，总时间 = 单题时间 × 题目数量

### 13.2 数据限制

- 知识库和母题库为静态数据，不可修改
- 母题数据可能存在 NaN，需过滤
- 知识点映射文件需预先生成

### 13.3 功能限制

- 目前不支持多选题的复杂场景（如"以下哪些选项正确，请选择所有正确选项"）
- 计算器当前支持16种房地产专业计算，其他计算需要扩展

### 13.4 质量限制

- 题目质量受 LLM 生成能力影响
- 某些复杂场景可能需要人工审核
- 自愈机制输出的题目质量可能不完美

## 18. 优先级划分

### P0 - 必须实现 ✅
- 智能出题核心功能 (F1) ✅
- 多智能体协同 (F3) ✅
- 质量保障 (F5) ✅
- 反馈循环机制 (F6) ✅
- 约束条件传递与验证 (F7) ✅
- UI 配置 (F8) ✅

### P1 - 重要 ✅
- 照猫画虎 (F2) ✅
- 计算器集成 (F4) ✅
- 过程可视化 (F9) ✅

### P2 - 可选
- 结果导出增强 (F10)
- 性能优化
- Excel批量导出

## 19. 验收标准

### 19.1 功能验收 ✅
1. ✅ 能够根据选择的章节生成题目
2. ✅ Router 正确识别知识点类型并派发
3. ✅ 计算题能正确调用计算器
4. ✅ 照猫画虎能匹配正确的母题
5. ✅ Critic 能验证答案和解析的正确性
6. ✅ Fixer 能修复错误并回到Critic重新验证
7. ✅ 严重问题能触发Router重新路由
8. ✅ 难度值符合用户指定的难度范围
9. ✅ 题型符合用户选择
10. ✅ 出题筛选条件符合用户选择

### 19.2 质量验收
1. ✅ 题目准确性 ≥ 95%
2. ✅ 干扰项合理性 (人工抽检)
3. ✅ 解析清晰度 (人工抽检)
4. ✅ 风格一致性 (与母题对比)
5. ✅ 难度值符合率 100%（在指定范围内）
6. ✅ 题型符合率 100%
7. ✅ 模式符合率 100%

### 19.3 性能验收
1. ✅ 知识库加载 < 5 秒
2. ✅ 单题生成 < 30 秒 (取决于 LLM)
3. ✅ 无阻塞，流式输出
4. ✅ 母题检索 < 1 秒（使用映射文件）

## 20. 未来改进方向

### 14.1 功能增强

- 支持更多题型（填空题、简答题等）
- 支持题目难度自动调整
- 支持题目批量导出（Excel格式）
- 支持题目编辑和修改

### 14.2 性能优化

- 并行生成多个题目
- 优化知识检索速度
- 缓存常用计算结果

### 14.3 质量提升

- 增强Critic验证逻辑
- 支持人工审核流程
- 建立题目质量评分体系

### 14.4 用户体验

- 优化UI界面
- 支持题目预览和编辑
- 支持题目收藏和管理
- 支持题目分享和导出

## 21. 附录

### 15.1 术语表

- **Few-Shot Learning（照猫画虎）**: 参考历史母题范例生成新题目的策略
- **Agent（智能体）**: LangGraph 工作流中的节点，负责特定任务
- **Router（路由器）**: 分析知识点类型并决定派发到哪个专家的节点
- **Critic（评审家）**: 验证题目质量的节点
- **Fixer（修复者）**: 修复题目错误的节点
- **Knowledge Base（知识库）**: 包含1712条知识点的JSONL文件
- **Mother Questions（母题库）**: 包含408道历史题目的Excel文件
- **Mapping File（映射文件）**: 母题与知识点关联的JSON文件

### 15.2 参考文档

- PRD文档: `prd.md`
- 架构文档: `docs/架构文档.md`
- 流程图: `docs/系统流程图-可视化版.md`
- 技术文档: `docs/技术文档.md`

### 15.3 代码文件清单

**核心文件**:
- `app.py`: Streamlit UI 和主应用逻辑
- `exam_graph.py`: LangGraph 工作流定义和节点实现
- `exam_factory.py`: 知识检索器和数据模型定义
- `calculation_logic.py`: 计算器实现

**配置文件**:
- `填写您的Key.txt`: API Key 配置文件
- `requirements.txt`: Python 依赖包列表
- `bot_knowledge_base.jsonl`: 知识库数据文件
- `存量房买卖母卷ABCD.xls`: 母题库数据文件
- `question_knowledge_mapping.json`: 知识点映射文件

**测试文件**:
- `test_full_workflow.py`: 完整工作流测试
- `test_calc_question.py`: 计算题测试
- `test_audit_logic.py`: 审计逻辑测试
- 等（见项目根目录）

---

**文档版本**: v1.0  
**最后更新**: 2025-01-27  
**维护者**: 搏学考试团队
