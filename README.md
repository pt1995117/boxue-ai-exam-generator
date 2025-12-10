# 📝 搏学AI出题生成器

基于 **LangGraph 多智能体协同 + 自适应反馈循环** 的智能出题系统

## ✨ 功能特点

- 🤖 **多智能体协同**：Router、Finance、Specialist、Writer、Critic、Fixer 六大智能体分工协作
- 🔄 **自适应反馈循环**：自动质量检测、错误修复、智能重试机制
- 🧮 **智能计算验证**：Finance节点支持17种计算器工具，Critic节点自动验证计算准确性
- 📚 **知识库驱动**：基于MECE知识点体系，支持章节筛选和难度控制
- 🎯 **照猫画虎**：参考历史母题范例，确保题目质量稳定
- 🌐 **多模型支持**：支持 DeepSeek、OpenAI GPT、Google Gemini 等多种大模型

## 🎬 系统架构

![系统流程图](docs/系统流程图.png)

### 智能体工作流程

1. **Router Node（路由节点）** 📊
   - 分析知识点类型
   - 金融类 → Finance Node
   - 法律/综合类 → Specialist Node

2. **Finance Node（金融专家）** 💰
   - 支持17种计算工具（税费、贷款、面积等）
   - 自动识别计算需求并调用相应工具
   - 生成带详细计算步骤的初稿

3. **Specialist Node（专家节点）** ⚖️
   - 处理法律、政策、综合知识类题目
   - 参考历史母题进行生成

4. **Writer Node（写作节点）** ✍️
   - 格式化标准化
   - 统一题目输出格式

5. **Critic Node（评审节点）** 🕵️
   - 质量验证（参数提取、计算验证）
   - 自动调用计算器验证答案准确性
   - 决策：通过/需要修复/重新生成

6. **Fixer Node（修复节点）** 🔧
   - 针对性修复轻微问题
   - 避免完整重新生成

### 容错机制

- ✅ **轻微问题** → Fixer本地修复
- ❌ **严重问题** → 返回Router重新路由
- 🔄 **重试≥3次** → 自愈机制（bypass Router直接重新生成）

## 🚀 快速开始

### 1. 环境要求

- Python 3.8+
- 依赖包见 `requirements.txt`

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置API Key

复制并编辑 `填写您的Key.txt` 文件：

```
OPENAI_API_KEY=你的密钥
# 或使用 Gemini
GEMINI_API_KEY=你的密钥

# DeepSeek 配置（推荐，国内可直连）
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-chat
```

### 4. 运行应用

```bash
streamlit run app.py
```

应用将在浏览器中打开 `http://localhost:8501`

## 📖 使用说明

1. **选择章节**：在界面中选择出题范围（支持多选）
2. **配置参数**：设置题目数量、难度、题型
3. **选择模式**：
   - **灵活模式**：场景化、灵活表达，适合日常练习
   - **严谨模式**：严格按照知识点，适合标准化考试
4. **开始出题**：点击按钮，实时查看智能体协同过程
5. **下载结果**：导出为Excel文件

## 📁 项目结构

```
├── app.py                          # Streamlit 主应用
├── exam_factory.py                 # 知识库检索和数据模型
├── exam_graph.py                   # LangGraph 智能体定义
├── calculation_logic.py            # 17种计算器工具
├── bot_knowledge_base.jsonl        # 知识库数据
├── docs/                           # 文档和流程图
│   ├── 系统流程图.png
│   ├── 技术文档.md
│   └── 架构文档.md
└── requirements.txt                # Python依赖
```

## 🛠 技术栈

- **LangGraph**：智能体编排框架
- **Streamlit**：Web界面
- **LangChain**：大模型调用封装
- **Pandas**：数据处理
- **Pydantic**：数据验证

## 🔧 支持的计算工具

Finance Node 和 Critic Node 共享17种计算器：

| 计算器 | 功能 |
|--------|------|
| `calc_deed_tax` | 契税计算 |
| `calc_vat` | 增值税计算 |
| `calc_income_tax` | 个人所得税 |
| `calc_loan_payment` | 贷款月供 |
| `calc_provident_fund_loan` | 公积金贷款 |
| `calc_building_area` | 建筑面积 |
| `calc_floor_area_ratio` | 容积率 |
| ... | 共17种工具 |

## 📊 知识库说明

项目包含完整的房地产经纪考试知识库：

- **bot_knowledge_base.jsonl**：MECE结构化知识点
- **存量房买卖母卷ABCD.xls**：历史母题范例（照猫画虎用）

## 🤝 贡献

欢迎提交Issue和Pull Request！

## 📄 许可证

MIT License

## 👨‍💻 作者

搏学考试团队

---

⭐ 如果这个项目对您有帮助，请给我们一个星标！
