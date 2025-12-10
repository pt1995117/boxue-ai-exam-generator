import streamlit as st
import pandas as pd
import os
import time
from exam_factory import KnowledgeRetriever, ExamQuestion, KB_PATH, HISTORY_PATH
from exam_graph import app as graph_app
from pydantic import ValidationError

# Page Config
st.set_page_config(page_title="搏学大考出题工厂", page_icon="📝", layout="wide")

# Title
st.title("📝 搏学大考 AI 出题工厂")
st.markdown("基于 **LangGraph 多智能体协同 + 自适应反馈循环** 的智能出题系统")

# --- Sidebar: Configuration ---
with st.sidebar:
    st.header("⚙️ 配置")
    
    # Load API Key from Streamlit Secrets (for cloud deployment) or file (for local)
    default_openai_key = ""
    default_gemini_key = ""
    default_base_url = "https://api.deepseek.com"
    default_model = "deepseek-chat"
    
    # Try to load from Streamlit Secrets first (for Streamlit Cloud)
    try:
        if hasattr(st, 'secrets') and st.secrets:
            default_openai_key = st.secrets.get("OPENAI_API_KEY", "")
            default_gemini_key = st.secrets.get("GEMINI_API_KEY", "")
            default_base_url = st.secrets.get("OPENAI_BASE_URL", "https://api.deepseek.com")
            default_model = st.secrets.get("OPENAI_MODEL", "deepseek-chat")
    except Exception:
        pass
    
    # Fallback to file if secrets not available
    if not default_openai_key and not default_gemini_key:
        config_path = "填写您的Key.txt"
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if "OPENAI_API_KEY=" in line and "请将您的Key粘贴在这里" not in line:
                        default_openai_key = line.split("=", 1)[1].strip()
                    if "GEMINI_API_KEY=" in line and "请将您的Key粘贴在这里" not in line:
                        default_gemini_key = line.split("=", 1)[1].strip()
                    if "OPENAI_BASE_URL=" in line:
                        default_base_url = line.split("=", 1)[1].strip()
                    if "OPENAI_MODEL=" in line:
                        default_model = line.split("=", 1)[1].strip()
    
    provider = st.radio("选择模型提供商", ["OpenAI / DeepSeek", "Google Gemini"], index=0)  # 默认选中 DeepSeek
    
    api_key = ""
    if provider == "Google Gemini":
        api_key = st.text_input("Gemini API Key", value=default_gemini_key, type="password")
        # Use a dropdown for known working models
        model_name = st.selectbox(
            "Model Name", 
            ["gemini-2.0-flash-exp", "gemini-1.5-flash-001", "gemini-1.5-pro"],
            index=0,
            help="如果遇到 404 错误，请尝试切换不同模型"
        )
        base_url = "" # Not needed for Gemini
    else:
        api_key = st.text_input("OpenAI API Key", value=default_openai_key, type="password")
        base_url = st.text_input("Base URL", value=default_base_url)  # DeepSeek API
        model_name = st.text_input("模型名称", value=default_model, help="所有节点统一使用此模型，推荐使用 deepseek-chat 速度更快")
    
    # Proxy Config
    st.divider()
    proxy = st.text_input("代理地址 (可选)", placeholder="http://127.0.0.1:7890", help="如果您在中国大陆使用 Gemini，可能需要配置代理")
    if proxy:
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
    
    if not api_key:
        st.warning("请在左侧填入 API Key 或修改 '填写您的Key.txt'")
    
    st.divider()
    st.info("💡 提示：推荐使用 DeepSeek Reasoner（中国可直连，无需代理），或 GPT-4o / Gemini 2.0 Flash。")

# --- Main Area ---

# 1. Initialize Retriever (Cached)
@st.cache_resource
def get_retriever():
    return KnowledgeRetriever(KB_PATH, HISTORY_PATH)

try:
    retriever = get_retriever()
    st.success(f"✅ 知识库已加载 ({len(retriever.kb_data)} 条知识点)")
except Exception as e:
    st.error(f"❌ 知识库加载失败: {e}")
    st.stop()

# 2. Chapter Selection
st.subheader("1. 选择出题范围")

# Extract all unique chapters/sections from KB
all_paths = [item['完整路径'] for item in retriever.kb_data if item['核心内容']]
# Let's group by "Part > Chapter"
chapters = sorted(list(set([" > ".join(p.split(" > ")[:2]) for p in all_paths])))

selected_chapters = st.multiselect("选择章节 (支持多选)", chapters)

col_sel1, col_sel2 = st.columns(2)
with col_sel1:
    select_all = st.checkbox("全选所有章节")
with col_sel2:
    calc_preset = st.checkbox("🧮 仅选中计算类章节")

if select_all:
    selected_chapters = chapters
elif calc_preset:
    # Define calculation keywords/chapters
    calc_keywords = ["计算", "税费", "贷款", "建筑指标", "面积"]
    selected_chapters = [c for c in chapters if any(k in c for k in calc_keywords)]

if not selected_chapters:
    st.warning("请至少选择一个章节。")
    st.stop()

# Filter KB based on selection
target_chunks = [
    c for c in retriever.kb_data 
    if c['核心内容'] and any(c['完整路径'].startswith(ch) for ch in selected_chapters)
]
st.write(f"🎯 选中范围包含 **{len(target_chunks)}** 个知识点")

# 3. Generation Settings
st.subheader("2. 出题设置")
col1, col2, col3, col4 = st.columns(4)
with col1:
    num_questions = st.number_input("生成题目数量", min_value=1, max_value=100, value=5)
with col2:
    difficulty = st.selectbox("难度偏好", ["随机", "简单 (0.3-0.5)", "中等 (0.5-0.7)", "困难 (0.7-0.9)"])
with col3:
    question_type = st.selectbox("题目类型", ["单选题", "多选题", "判断题"])
with col4:
    generation_mode = st.selectbox(
        "出题模式", 
        ["灵活", "严谨"], 
        index=0,
        help="灵活模式：场景化、灵活表达，适合日常练习。严谨模式：严格按照知识点输出，适合标准化考试。"
    )


# 4. Generate Button
if st.button("🚀 开始出题", type="primary", disabled=not api_key):
    progress_bar = st.progress(0)
    status_text = st.empty()
    results = []
    
    # Randomly select chunks for the requested number of questions
    # If num_questions > len(target_chunks), we might repeat or just cap it.
    # Let's sample with replacement if needed, or just cycle.
    import random
    selected_chunks_for_gen = [random.choice(target_chunks) for _ in range(num_questions)]
    
    for i, chunk in enumerate(selected_chunks_for_gen):
    # Generate with Visuals
        with st.status(f"🤖 第 {i+1} 题: 智能体协同中 (LangGraph)...", expanded=True) as status:
            q_json = None
            error_msg = None
            
            # Initial State (examples will be fetched inside graph after routing)
            inputs = {
                "kb_chunk": chunk, 
                "examples": [],  # Will be populated by specialist/finance nodes
                "retry_count": 0,
                "logs": []
            }
            
            # Config for LLM (now includes retriever and question_type)
            config = {
                "configurable": {
                    "model": model_name,  # 所有节点统一使用此模型
                    "api_key": api_key, 
                    "base_url": base_url,
                    "retriever": retriever,
                    "question_type": question_type,
                    "generation_mode": generation_mode  # 灵活/严谨模式
                }
            }
            
            try:
                # 添加初始提示，让用户知道系统正在工作
                st.info("🔄 正在初始化... 首次调用可能需要10-30秒，请耐心等待")
                
                # Stream events from LangGraph
                event_count = 0
                for event in graph_app.stream(inputs, config=config):
                    event_count += 1
                    # 清除初始提示（在第一次事件后）
                    if event_count == 1:
                        st.empty()  # 清除初始提示
                    
                    # event is a dict like {'node_name': {'key': 'value'}}
                    for node_name, state_update in event.items():
                        if 'logs' in state_update:
                            for log in state_update['logs']:
                                st.write(log)
                        
                        # Show Router Decision
                        if node_name == "router":
                            with st.expander("🧠 路由决策 (Router Decision)", expanded=True):
                                if 'router_details' in state_update:
                                    details = state_update['router_details']
                                    cols = st.columns([2, 1])
                                    with cols[0]:
                                        st.markdown(f"**选中知识点**: `{details.get('path', 'N/A')}`")
                                        st.markdown(f"**掌握程度**: `{details.get('mastery', '未知')}`") # Added Mastery Display
                                        st.info(f"**核心内容片段**: \n\n{details.get('content', '')}")
                                    with cols[1]:
                                        st.metric("金融相关度", details.get('score_finance', 0))
                                        st.metric("法律相关度", details.get('score_legal', 0))
                                        st.success(f"➡️ 派发给: **{details.get('agent', 'Unknown')}**")
                        if node_name == "specialist" and 'draft' in state_update:
                            # Show examples used (fetched after routing)
                            if 'examples' in state_update and state_update['examples']:
                                examples = state_update['examples']
                                with st.expander(f"🐯 照猫画虎：参考的 {len(examples)} 道母题范例", expanded=False):
                                    for idx, ex in enumerate(examples, 1):
                                        st.markdown(f"### 范例 {idx}")
                                        st.markdown(f"**题干**：{ex['题干']}")
                                        
                                        # Display Options
                                        if '选项' in ex and isinstance(ex['选项'], dict):
                                            st.markdown("**选项**：")
                                            for k, v in ex['选项'].items():
                                                if v and str(v) != 'nan':
                                                    st.markdown(f"- {k}. {v}")
                                            
                                        st.markdown(f"**答案**：{ex['正确答案']}")
                                        st.markdown(f"**解析**：{ex['解析']}")
                                        st.divider()
                            
                            with st.expander("📄 查看初稿内容"):
                                st.json(state_update['draft'])

                        # Show Finance Calculation & Draft
                        if node_name == "finance":
                            if 'tool_usage' in state_update:
                                usage = state_update['tool_usage']
                                tool_name = usage.get('tool', 'None')
                                
                                if tool_name and tool_name != "None":
                                    with st.expander("🧮 计算器调用详情", expanded=True):
                                        st.info(f"调用函数: `{tool_name}`")
                                        st.write("输入参数:", usage['params'])
                                        st.success(f"计算结果: {usage['result']}")
                                else:
                                    with st.expander("🧮 计算器分析", expanded=False):
                                        st.caption("ℹ️ 智能体分析后认为：本题为概念/逻辑题，无需进行数值计算。")
                            
                            
                            # Show examples used (fetched after routing)
                            if 'examples' in state_update and state_update['examples']:
                                examples = state_update['examples']
                                with st.expander(f"🐯 照猫画虎：参考的 {len(examples)} 道母题范例", expanded=False):
                                    for idx, ex in enumerate(examples, 1):
                                        st.markdown(f"### 范例 {idx}")
                                        st.markdown(f"**题干**：{ex['题干']}")
                                        
                                        # Display Options
                                        if '选项' in ex and isinstance(ex['选项'], dict):
                                            st.markdown("**选项**：")
                                            for k, v in ex['选项'].items():
                                                if v and str(v) != 'nan':
                                                    st.markdown(f"- {k}. {v}")
                                            
                                        st.markdown(f"**答案**：{ex['正确答案']}")
                                        st.markdown(f"**解析**：{ex['解析']}")
                                        st.divider()
                            
                            if 'draft' in state_update:
                                with st.expander("📄 查看金融专家初稿"):
                                    st.json(state_update['draft'])
                                
                        # Show Writer Output
                        if node_name == "writer" and 'final_json' in state_update:
                            with st.expander("✍️ 查看作家润色后内容 (待审核)"):
                                st.json(state_update['final_json'])
                                
                        # Show Critic Review (Pass or Fail)
                        if node_name == "critic":
                            feedback = state_update.get('critic_feedback', 'Unknown')
                            details = state_update.get('critic_details', '')
                            
                            # Get retry count for display (default to 0 if not present)
                            retry_count = state_update.get('retry_count', 0)
                            round_label = f" (Round {retry_count + 1})" if retry_count > 0 else ""

                            # Display Critic Tool Usage
                            if 'critic_tool_usage' in state_update:
                                usage = state_update['critic_tool_usage']
                                tool_name = usage.get('tool', 'None')
                                
                                if tool_name and tool_name != "None":
                                    with st.expander(f"🕵️ 批评家验证计算{round_label}", expanded=True):
                                        st.info(f"验证调用: `{tool_name}`")
                                        st.write("验证参数:", usage['params'])
                                        st.success(f"验证结果: {usage['result']}")
                                else:
                                    with st.expander(f"🕵️ 批评家验证分析{round_label}", expanded=False):
                                        st.caption("ℹ️ 批评家认为无需进行数值验证。")

                            if feedback == "PASS":
                                st.success(f"🕵️ 批评家: 审核通过{round_label}")
                            else:
                                st.error(f"🕵️ 批评家: 驳回{round_label} -> {details}")
                                st.caption("即将进入 Fixer 修复流程...")
                                    
                        # Show Fixer Result
                        if node_name == "fixer" and 'final_json' in state_update:
                            with st.expander(f"🔧 修复后内容 (Fix Round)", expanded=True):
                                st.json(state_update['final_json'])

                        if 'final_json' in state_update:
                            q_json = state_update['final_json']
                            
                # Check final state
                if q_json:
                    # Validate Schema
                    try:
                        ExamQuestion(**q_json)
                        status.update(label=f"✅ 第 {i+1} 题生成成功", state="complete", expanded=False)
                        q_json['来源路径'] = chunk['完整路径']
                        results.append(q_json)
                    except ValidationError as e:
                        st.write(f"❌ Validation Error: {e}")
                        status.update(label=f"❌ 第 {i+1} 题格式错误", state="error", expanded=True)
                else:
                     status.update(label=f"❌ 第 {i+1} 题生成失败 (Max Retries)", state="error", expanded=True)
                     
            except Exception as e:
                st.error(f"Graph Error: {e}")
                status.update(label=f"❌ 第 {i+1} 题运行出错", state="error", expanded=True)
        
        progress_bar.progress((i + 1) / num_questions)
    
    status_text.text("✅ 出题完成！")
    
    if results:
        df = pd.DataFrame(results)
        cols = ["题干", "选项1", "选项2", "选项3", "选项4", "正确答案", "解析", "难度值", "考点", "来源路径"]
        # Ensure cols exist
        final_cols = [c for c in cols if c in df.columns]
        df = df[final_cols]
        
        st.subheader("3. 结果预览")
        st.dataframe(df)
        
        # Download
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        file_name = f"exam_questions_{timestamp}.xlsx"
        
        # Convert to Excel in memory
        import io
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
            
        st.download_button(
            label="📥 下载 Excel 文件",
            data=buffer.getvalue(),
            file_name=file_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.error("生成失败，未能生成有效题目。请检查 API Key 或网络连接。")
