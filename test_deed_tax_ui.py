"""
契税计算 - 代码生成 vs 硬编码对比 (Streamlit UI)
运行: streamlit run test_deed_tax_ui.py --server.port 8502
"""
import streamlit as st
import json
import os
from calculation_logic import RealEstateCalculator

st.set_page_config(page_title="契税代码生成测试", page_icon="🧪", layout="wide")

st.title("🧪 契税计算 - 代码生成 vs 硬编码对比")
st.markdown("测试动态代码生成方案的可行性")

# Load API Key
config_path = "填写您的Key.txt"
API_KEY = ""
BASE_URL = "https://api.deepseek.com"

if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            if "OPENAI_API_KEY=" in line and "请将您的Key粘贴在这里" not in line:
                API_KEY = line.split("=", 1)[1].strip()
                break

if not API_KEY:
    st.error("❌ 请先配置 API Key")
    st.stop()

st.success(f"✅ API Key 已加载: {API_KEY[:10]}...")

# Import libraries
try:
    from openai import OpenAI
    from RestrictedPython import compile_restricted
    from RestrictedPython.Guards import guarded_iter_unpack_sequence
    import re
    st.success("✅ 依赖库加载成功")
except ImportError as e:
    st.error(f"❌ 依赖库导入失败: {e}")
    st.stop()

# Test scenario configuration
st.subheader("1. 配置测试场景")

col1, col2 = st.columns(2)

with col1:
    price = st.number_input("房价（万元）", min_value=1, max_value=10000, value=100)
    area = st.number_input("建筑面积（平方米）", min_value=1, max_value=1000, value=90)

with col2:
    is_first_home = st.checkbox("首套房", value=True)
    is_second_home = st.checkbox("二套房", value=False)
    is_residential = st.checkbox("住宅", value=True)

# Deed tax rules
kb_content = """
契税计算规则：
1. 住宅：
   - 首套房：面积≤140平，税率1%；面积>140平，税率1.5%
   - 二套房：面积≤140平，税率1%；面积>140平，税率2%
   - 三套及以上：税率3%
2. 非住宅：统一税率3%
"""

with st.expander("📖 教材规则", expanded=False):
    st.code(kb_content, language="text")

# Run test button
if st.button("🚀 开始测试", type="primary"):
    st.divider()
    
    # Step 1: Hardcoded calculation
    st.subheader("2. 硬编码计算（Ground Truth）")
    
    with st.spinner("计算中..."):
        hardcoded_result = RealEstateCalculator.calculate_deed_tax(
            price=price,
            area=area,
            is_first_home=is_first_home,
            is_second_home=is_second_home,
            is_residential=is_residential
        )
    
    st.metric("硬编码结果", f"{hardcoded_result}万元")
    
    # Step 2: Generate code
    st.divider()
    st.subheader("3. LLM 代码生成")
    
    prompt = f"""你是一个Python代码生成器。根据下面的规则生成契税计算代码。

教材规则：
{kb_content}

场景：
- 房价: {price}万元
- 建筑面积: {area}平方米
- 是否首套: {"是" if is_first_home else "否"}
- 是否二套: {"是" if is_second_home else "否"}
- 是否住宅: {"是" if is_residential else "否"}

请输出JSON格式（不要markdown代码块）：
{{
  "reasoning": "根据教材分析...",
  "formula": "数学公式",
  "python_code": "price = {price}\\nrate = ...\\nresult = price * rate"
}}

注意：
1. python_code 中必须定义 result 变量
2. 只能用基本运算，不能用 import
3. 直接输出JSON，不要```标记
"""
    
    with st.spinner("调用 DeepSeek 生成代码..."):
        try:
            client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
            
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            
            llm_output = response.choices[0].message.content
            
            # Parse JSON
            json_match = re.search(r'\{.*\}', llm_output, re.DOTALL)
            if json_match:
                generated_data = json.loads(json_match.group(0))
            else:
                st.error("❌ 无法解析 JSON 响应")
                st.code(llm_output)
                st.stop()
            
            st.success("✅ 代码生成成功")
            
            col_a, col_b = st.columns(2)
            
            with col_a:
                st.markdown("**推理过程**")
                st.info(generated_data['reasoning'])
                
                st.markdown("**数学公式**")
                st.code(generated_data['formula'])
            
            with col_b:
                st.markdown("**生成的 Python 代码**")
                st.code(generated_data['python_code'], language="python")
            
            # Step 3: Execute code
            st.divider()
            st.subheader("4. 安全执行生成的代码")
            
            with st.spinner("执行中..."):
                try:
                    code_str = generated_data['python_code']
                    
                    safe_env = {
                        "__builtins__": {
                            "abs": abs,
                            "min": min,
                            "max": max,
                            "round": round,
                            "float": float,
                            "int": int,
                        },
                        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
                    }
                    
                    byte_code = compile_restricted(
                        code_str,
                        filename='<generated>',
                        mode='exec'
                    )
                    
                    if byte_code.errors:
                        st.error(f"❌ 编译错误: {byte_code.errors}")
                        st.stop()
                    
                    exec(byte_code, safe_env)
                    
                    if 'result' in safe_env:
                        code_result = safe_env['result']
                        st.success("✅ 代码执行成功")
                        st.metric("代码执行结果", f"{code_result}万元")
                    else:
                        st.error("❌ 代码中未定义 result 变量")
                        st.stop()
                    
                except Exception as e:
                    st.error(f"❌ 执行错误: {str(e)}")
                    st.stop()
            
            # Step 4: Compare
            st.divider()
            st.subheader("5. 结果对比")
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("硬编码", f"{hardcoded_result}万元")
            
            with col2:
                st.metric("代码生成", f"{code_result}万元")
            
            with col3:
                diff = abs(hardcoded_result - code_result)
                st.metric("误差", f"{diff:.6f}万元")
            
            if diff < 0.01:
                st.success("🎉 **结果一致！** 代码生成方案可行！")
            else:
                st.error(f"⚠️ **结果不一致！** 误差: {diff:.6f}万元")
                st.warning("可能原因：模型理解错误、规则描述不清、或提示词需要优化")
            
            # Save result
            result = {
                "scenario": {
                    "price": price,
                    "area": area,
                    "is_first_home": is_first_home,
                    "is_second_home": is_second_home,
                    "is_residential": is_residential,
                },
                "hardcoded": hardcoded_result,
                "generated": code_result,
                "match": diff < 0.01,
                "reasoning": generated_data['reasoning'],
                "code": generated_data['python_code'],
                "formula": generated_data['formula'],
            }
            
            st.divider()
            st.markdown("**完整结果 (JSON)**")
            st.json(result)
            
            # Download button
            st.download_button(
                label="📥 下载测试结果",
                data=json.dumps(result, ensure_ascii=False, indent=2),
                file_name="deed_tax_test_result.json",
                mime="application/json"
            )
            
        except Exception as e:
            st.error(f"❌ 发生错误: {type(e).__name__}: {str(e)}")
            import traceback
            st.code(traceback.format_exc())

# Sidebar info
with st.sidebar:
    st.markdown("### 📊 测试说明")
    st.markdown("""
    这个测试对比两种方式：
    
    1. **硬编码函数**  
       `calculate_deed_tax()` - 当前系统
    
    2. **动态代码生成**  
       让 LLM 根据教材规则生成 Python 代码
    
    #### 安全措施
    - ✅ RestrictedPython 沙箱
    - ✅ 禁止 import/open 等危险操作
    - ✅ 只允许基本数学运算
    
    #### 评估标准
    - 结果一致性（误差 <0.01万元）
    - 代码可读性
    - 执行稳定性
    """)
    
    st.divider()
    
    st.markdown("### 🎯 快速测试场景")
    if st.button("首套小户型 (90平)"):
        st.rerun()
    if st.button("首套大户型 (150平)"):
        st.rerun()
    if st.button("二套房 (100平)"):
        st.rerun()
