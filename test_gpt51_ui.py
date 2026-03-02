"""
GPT-5.1 Configuration Test UI
"""
import streamlit as st
import os

st.set_page_config(page_title="GPT-5.1 测试", page_icon="🧪")

st.title("🧪 GPT-5.1 配置测试")

# Load config
config = {}
config_path = "填写您的Key.txt"

if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()

# Display config
st.header("📋 当前配置")

col1, col2 = st.columns(2)

with col1:
    st.subheader("DeepSeek (生成端)")
    deepseek_key = config.get("OPENAI_API_KEY", "")
    deepseek_model = config.get("OPENAI_MODEL", "")
    
    if deepseek_key:
        st.success(f"✅ API Key: {deepseek_key[:20]}...")
        st.info(f"Model: {deepseek_model}")
    else:
        st.error("❌ 未配置")

with col2:
    st.subheader("GPT-5.1 (审计端)")
    gpt_key = config.get("CRITIC_API_KEY", "")
    gpt_model = config.get("CRITIC_MODEL", "gpt-5.1")
    
    if gpt_key and gpt_key != deepseek_key:
        st.success(f"✅ API Key: {gpt_key[:20]}...")
        st.info(f"Model: {gpt_model}")
    else:
        st.warning("⚠️ 未单独配置 (将使用 DeepSeek)")

# Test GPT-5.1
st.header("🔬 测试 GPT-5.1 连接")

if st.button("▶️ 测试 API 连接", type="primary"):
    if not gpt_key:
        st.error("❌ CRITIC_API_KEY 未配置")
    else:
        with st.spinner(f"正在连接 {gpt_model}..."):
            try:
                from openai import OpenAI
                
                client = OpenAI(
                    api_key=gpt_key,
                    base_url="https://api.openai.com/v1"
                )
                
                # Test calculation
                test_prompt = """
计算题目：建筑面积 80 平方米，成本价 1560 元/平方米
公式：土地出让金 = 面积 × 成本价 × 1%
请计算结果，只返回数字。
"""
                
                response = client.chat.completions.create(
                    model=gpt_model,
                    messages=[
                        {"role": "user", "content": test_prompt}
                    ],
                    temperature=0,
                    max_tokens=100
                )
                
                result = response.choices[0].message.content
                
                st.success("✅ GPT-5.1 连接成功！")
                
                col1, col2 = st.columns(2)
                col1.metric("API 响应", result)
                col2.metric("预期结果", "1248")
                
                # Validate
                if "1248" in result:
                    st.balloons()
                    st.success("🎉 计算准确性验证通过！")
                else:
                    st.warning(f"⚠️ 结果可能不准确，请人工检查")
                
                st.info("""
**✅ 配置成功！现在你可以：**
1. 运行 `streamlit run app.py` 生成题目
2. 金融计算题会自动使用 GPT-5.1 审核
3. 日志会显示 "🔍 批评家 (GPT-5.1)"
                """)
                
            except Exception as e:
                error_msg = str(e)
                st.error(f"❌ 连接失败")
                
                with st.expander("查看错误详情"):
                    st.code(error_msg)
                
                # Check common errors
                if "429" in error_msg:
                    st.warning("""
**可能原因：配额不足**
- 检查 OpenAI 账户余额
- 或等待配额重置
                    """)
                elif "401" in error_msg:
                    st.warning("""
**可能原因：API Key 无效**
- 检查 Key 是否正确
- 检查 Key 是否有 GPT-5.1 权限
                    """)
                elif "404" in error_msg:
                    st.warning("""
**可能原因：模型不存在**
- GPT-5.1 可能不是正确的模型名称
- 尝试：gpt-4o, gpt-4o-mini, o1-preview, o1-mini
                    """)

# Cost estimation
st.header("💰 成本估算")

st.markdown("""
**架构：DeepSeek + GPT-5.1**

| 环节 | 模型 | 单题成本 |
|------|------|---------|
| 路由 | DeepSeek | ¥0.0005 |
| 生成 | DeepSeek | ¥0.002 |
| 写作 | DeepSeek | ¥0.0005 |
| **审计** | **GPT-5.1** | **¥0.05-0.08** |
| **总计** | - | **¥0.053-0.083** |

**1000 道金融题总成本：¥53-83**
""")

st.caption("注：非金融题使用 DeepSeek 审计，成本仅 ¥5/千题")
