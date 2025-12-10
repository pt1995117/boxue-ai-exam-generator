# 🎓 搏学AI出题工厂

基于 **LangGraph 多智能体协同 + 自适应反馈循环** 的智能考试题目生成系统

## ✨ 核心特性

### 🤖 多智能体协同系统
- **Router Node (路由节点)**: 智能分析题目类型，将金融类题目派发给Finance节点，法律/综合类派发给Specialist节点
- **Finance Node (金融专家)**: 专门处理金融计算类题目，集成计算器工具，进行照猫画虎、计算步骤分析、决定计算、生成初稿
- **Specialist Node (专家节点)**: 处理法律、综合类专业题目，照猫画虎生成高质量初稿
- **Writer Node (写作节点)**: 格式化标准化输出，统一题目格式
- **Critic Node (评审节点)**: 三重验证机制 - 质量验证、参数提取、计算步骤验证
- **Fixer Node (修复节点)**: 针对轻微问题进行本地修复

### 🔄 自适应反馈循环
- **智能重试机制**: Critic审核不通过时，根据问题严重程度选择修复策略
  - 严重问题 ❌ → 返回Router重新路由 (retry_count < 2)
  - 轻微问题 🔧 → Fixer本地修复
  - 重试≥3次 🔄 → 自愈机制bypass Router重新生成
- **质量保障**: 确保每道题目都经过严格验证才输出

### 🧮 智能计算系统
- 集成17+计算器工具，覆盖金融、税费、面积、建筑指标等多个领域
- Finance节点自动调用计算工具生成精确答案
- Critic节点独立验证计算结果，确保准确性

### 🐯 照猫画虎机制
- 自动从历史母题库中检索相似题目作为范例
- 根据章节路径、知识点类型智能匹配
- 金融类和专家类节点均支持范例学习

## 📊 系统架构

```
开始 → Router Node (路由决策)
         ↓              ↓
    Finance Node   Specialist Node
    (金融专家)      (专家节点)
         ↓              ↓
         → Writer Node (格式化) →
              ↓
         Critic Node (质量验证)
              ↓
         通过 / 修复决策
         ↓           ↓
    输出题目    Fixer/Reroute
```

详细流程图请查看: `docs/系统流程图.png`

## 🚀 快速开始

### 环境要求
- Python 3.8+
- Streamlit
- LangGraph
- OpenAI/DeepSeek/Gemini API Key

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置API Key

编辑 `填写您的Key.txt` 文件，填入您的API密钥：

```
OPENAI_API_KEY=你的OpenAI或DeepSeek密钥
GEMINI_API_KEY=你的Gemini密钥
```

### 运行应用

```bash
streamlit run app.py
```

访问 `http://localhost:8501` 开始使用！

## 📖 使用说明

1. **选择模型提供商**: OpenAI/DeepSeek 或 Google Gemini
2. **选择出题范围**: 支持多章节选择，或使用快捷按钮（全选/仅计算类）
3. **出题设置**: 
   - 生成题目数量 (1-100)
   - 难度偏好 (简单/中等/困难/随机)
   - 题目类型 (单选题/多选题/判断题)
   - 出题模式 (灵活/严谨)
4. **开始出题**: 点击按钮，实时查看各个智能体的工作流程
5. **下载结果**: 生成完成后可下载Excel文件

## 🔧 计算器工具列表

系统集成了以下计算工具：

- `calculator_deed_tax` - 契税计算
- `calculator_personal_income_tax` - 个人所得税计算
- `calculator_vat` - 增值税计算
- `calculator_land_value_added_tax` - 土地增值税计算
- `calculator_stamp_duty` - 印花税计算
- `calculator_equal_principal_loan` - 等额本金贷款计算
- `calculator_equal_installment_loan` - 等额本息贷款计算
- `calculator_building_area` - 建筑面积计算
- `calculator_floor_area_ratio` - 容积率计算
- 以及更多专业计算工具...

## 📁 项目结构

```
.
├── app.py                    # Streamlit主应用
├── exam_factory.py           # 知识库检索器和数据模型
├── exam_graph.py            # LangGraph核心逻辑
├── calculation_logic.py      # 计算器工具集
├── bot_knowledge_base.jsonl  # 知识库
├── 存量房买卖母卷ABCD.xls    # 历史题库
├── requirements.txt          # 依赖包列表
└── docs/                    # 文档和流程图
    ├── 系统流程图.png
    ├── 架构文档.md
    ├── 技术文档.md
    └── 需求文档.md
```

## 🎯 推荐模型

- **DeepSeek Reasoner** - 中国大陆可直连，无需代理，性价比高
- **GPT-4o** - OpenAI最新模型，质量优秀
- **Gemini 2.0 Flash** - Google最新模型，速度快

## 📊 实时可视化

应用运行时会实时展示：
- 🧠 路由决策过程和相关度评分
- 🐯 照猫画虎选取的范例题目
- 🧮 计算器调用详情（输入参数和计算结果）
- 📄 各节点生成的初稿内容
- 🕵️ 批评家验证过程和反馈
- 🔧 修复节点的修正内容

## 📝 测试

项目包含完整的测试套件：

```bash
# 测试完整工作流
python test_full_workflow.py

# 测试循环机制
python test_loop_mechanism.py

# 测试所有计算器
python test_all_formulas_comprehensive.py
```

## 📄 许可证

本项目仅供教育和研究使用。

## 🤝 贡献

欢迎提交Issue和Pull Request！

---

**Made with ❤️ by 搏学大考团队**
