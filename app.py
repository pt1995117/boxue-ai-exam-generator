import streamlit as st
import pandas as pd
import os
import time
import re
import numpy as np
import random
import mimetypes
import base64
from collections import Counter
from exam_factory import KnowledgeRetriever, ExamQuestion, set_active_tenant
from exam_graph import app as graph_app
from pydantic import ValidationError
import json
from io import BytesIO
import pathlib
from tenants_config import (
    list_tenants,
    resolve_tenant_kb_path,
    resolve_tenant_history_path,
    tenant_mapping_path,
    tenant_bank_path,
)
from tenant_context import (
    get_accessible_tenants,
    get_user_profile,
    assert_tenant_access,
)
from mapping_review_store import load_mapping_review, upsert_mapping_review
from slice_review_store import load_slice_review, upsert_slice_review
from audit_log import write_audit_log

# --- Utils ---
def filter_json_display(d):
    """Filters out empty strings and None values for a cleaner JSON display in UI."""
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if v != "" and v is not None}

def _stringify_structured_value(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _normalize_calc_label(raw_value):
    text = str(raw_value or "").strip().lower()
    if not text:
        return ""
    if text in {"计算题", "计算", "calc", "calculation", "true", "1", "yes", "y", "是"}:
        return "计算题"
    if text in {"非计算题", "非计算", "false", "0", "no", "n", "否"}:
        return "非计算题"
    if "非计算" in text:
        return "非计算题"
    if "计算" in text:
        return "计算题"
    return ""


def _resolve_calc_question_type(item):
    for key in (
        "题型标签",
        "题目类型",
        "是否计算题",
        "计算题标签",
        "calc_type",
        "question_calc_type",
        "is_calculation",
        "need_calculation",
    ):
        label = _normalize_calc_label(item.get(key))
        if label:
            return label

    text_parts = [
        str(item.get("题干", "") or ""),
        str(item.get("解析", "") or ""),
    ]
    for idx in range(1, 9):
        text_parts.append(str(item.get(f"选项{idx}", "") or ""))
    text = " ".join([x for x in text_parts if x]).lower()

    has_digit = bool(re.search(r"\d", text))
    has_operator = bool(re.search(r"[+\-*/=×÷%]", text))
    calc_keywords = (
        "计算",
        "税",
        "税费",
        "贷款",
        "月供",
        "利率",
        "利息",
        "首付",
        "还款",
        "金额",
        "总价",
        "单价",
        "面积",
        "比例",
        "百分比",
        "合计",
        "折扣",
        "公式",
    )
    keyword_hits = sum(1 for kw in calc_keywords if kw in text)
    if (has_digit and (has_operator or keyword_hits >= 1)) or keyword_hits >= 2:
        return "计算题"
    return "非计算题"


def build_slice_text_from_struct(struct):
    if not isinstance(struct, dict):
        return ""
    parts = []
    context_before = struct.get("context_before")
    if context_before:
        parts.append(str(context_before).strip())
    for table in struct.get("tables") or []:
        parts.append(str(table).strip())
    context_after = struct.get("context_after")
    if context_after:
        parts.append(str(context_after).strip())
    for example in struct.get("examples") or []:
        parts.append(str(example).strip())
    for formula in struct.get("formulas") or []:
        parts.append(str(formula).strip())
    for image in struct.get("images") or []:
        parts.append(str(image).strip())
    return "\n".join([p for p in parts if p])

def build_slice_raw_text(kb_chunk):
    if not isinstance(kb_chunk, dict):
        return ""
    core = kb_chunk.get("核心内容")
    if core:
        return str(core).strip()
    struct = kb_chunk.get("结构化内容") or {}
    return build_slice_text_from_struct(struct)

def _normalize_image_item(image):
    if isinstance(image, dict):
        return {
            "image_id": str(image.get("image_id", "")).strip(),
            "image_path": str(image.get("image_path", "")).strip(),
            "analysis": str(image.get("analysis", "")).strip(),
        }
    if isinstance(image, str):
        return {"image_id": "", "image_path": "", "analysis": image.strip()}
    return {"image_id": "", "image_path": "", "analysis": ""}

def _resolve_image_path(image_path: str) -> str:
    if not image_path:
        return ""
    p = pathlib.Path(image_path)
    if p.is_file():
        return str(p)
    workspace_path = pathlib.Path(__file__).parent / image_path
    if workspace_path.is_file():
        return str(workspace_path.resolve())
    filename = p.name
    tenant_id = os.getenv("TENANT_ID", "").strip()
    if tenant_id and filename:
        images_root = pathlib.Path(__file__).parent / "data" / tenant_id / "slices" / "images"
        if images_root.exists():
            for candidate in images_root.rglob(filename):
                if candidate.is_file():
                    return str(candidate.resolve())
    return ""

def _build_image_href(local_path: str) -> str:
    if not local_path:
        return ""
    try:
        content = pathlib.Path(local_path).read_bytes()
        mime = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    except Exception:
        return f"file://{local_path}"

def _render_image_items(images, scope_key: str):
    normalized = [_normalize_image_item(img) for img in (images or [])]
    normalized = [x for x in normalized if x.get("image_path") or x.get("analysis")]
    if not normalized:
        st.info("（该切片暂无可展示图片）")
        return
    for idx, image in enumerate(normalized, 1):
        image_id = image.get("image_id") or f"图{idx}"
        image_path = image.get("image_path", "")
        analysis = image.get("analysis", "")
        resolved_path = _resolve_image_path(image_path)
        col_a, col_b = st.columns([2, 3])
        with col_a:
            st.markdown(f"**{image_id}**")
            if resolved_path:
                href = _build_image_href(resolved_path)
                st.markdown(
                    f'<a href="{href}" target="_blank" rel="noopener noreferrer">🔗 点击在新页面查看图片</a>',
                    unsafe_allow_html=True,
                )
                st.caption(f"路径：`{resolved_path}`")
                st.image(resolved_path, use_container_width=True)
            elif image_path:
                st.warning("图片文件未找到")
                st.caption(f"原始路径：`{image_path}`")
            else:
                st.caption("无图片路径")
        with col_b:
            st.markdown("**图片解析**")
            if analysis:
                st.markdown(analysis)
            else:
                st.caption("（暂无解析）")
        st.divider()

def render_structured_slice(struct):
    if not isinstance(struct, dict) or not struct:
        st.info("（未提供结构化内容）")
        return
    context_before = struct.get("context_before")
    if context_before:
        st.markdown(str(context_before))
    tables = struct.get("tables") or []
    images = struct.get("images") or []
    for table in tables:
        st.markdown(str(table))
    if tables:
        st.markdown("**表格关联图片（可点击新页查看）**")
        _render_image_items(images, scope_key="table_images")
    context_after = struct.get("context_after")
    if context_after:
        st.markdown(str(context_after))
    examples = struct.get("examples") or []
    if examples:
        st.markdown("**例题/示例**")
        for idx, ex in enumerate(examples, 1):
            if isinstance(ex, dict):
                st.markdown(f"**例 {idx}**")
                if ex.get("题干"):
                    st.markdown(str(ex.get("题干")))
                if ex.get("解析"):
                    st.markdown(str(ex.get("解析")))
            else:
                st.markdown(f"**例 {idx}**：{ex}")
    formulas = struct.get("formulas") or []
    if formulas:
        st.markdown("**公式**")
        for idx, formula in enumerate(formulas, 1):
            st.markdown(f"{idx}. {formula}")
    if images and not tables:
        st.markdown("**图片**")
        _render_image_items(images, scope_key="images_only")

# Page Config
st.set_page_config(page_title="搏学大考出题工厂", page_icon="📝", layout="wide")

# ====================================
# 🔄 自动检测服务重启并清除缓存
# ====================================
def get_server_process_id():
    """
    获取服务器进程 ID。
    每次服务重启，PID 都会变化，通过检测 PID 变化来判断是否重启。
    """
    return str(os.getpid())

# 获取当前服务器的进程 ID
current_server_pid = get_server_process_id()

# 检查浏览器端存储的进程 ID
if "server_pid" not in st.session_state:
    # 第一次访问，存储当前进程 ID
    st.session_state.server_pid = current_server_pid
    st.session_state.session_initialized = True
elif st.session_state.server_pid != current_server_pid:
    # 进程 ID 不匹配，说明服务重启了，清除所有缓存
    st.info("🔄 检测到服务已重启，正在自动清除缓存...")
    time.sleep(0.3)
    # 清除所有 session_state
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    # 存储新的进程 ID
    st.session_state.server_pid = current_server_pid
    st.session_state.session_initialized = True
    # 强制刷新页面
    st.rerun()

# Title
st.title("📝 搏学大考 AI 出题工厂")
st.markdown("基于 **LangGraph 多智能体协同 + 自适应反馈循环** 的智能出题系统")

# 工作区切换
mode_main = st.radio("工作区", ["出题", "题库", "切片核对", "映射确认"], index=0, horizontal=True)

# Tenant / user context
tenant_defs = list_tenants()
tenant_name_map = {x["tenant_id"]: x["name"] for x in tenant_defs}
system_user = st.session_state.get("system_user", "admin")
allowed_tenants = get_accessible_tenants(system_user)
if not allowed_tenants:
    allowed_tenants = [tenant_defs[0]["tenant_id"]] if tenant_defs else ["hz"]
current_tenant = st.session_state.get("tenant_id", allowed_tenants[0])
if current_tenant not in allowed_tenants:
    current_tenant = allowed_tenants[0]

kb_path = str(resolve_tenant_kb_path(current_tenant))
history_path = str(resolve_tenant_history_path(current_tenant))
mapping_path = str(tenant_mapping_path(current_tenant))
bank_path = tenant_bank_path(current_tenant)
bank_state_key = f"question_bank_{current_tenant}"
set_active_tenant(current_tenant)
os.environ["TENANT_ID"] = current_tenant

# --- Sidebar: Configuration (folded) ---
with st.sidebar:
    st.subheader("👤 系统号与城市")
    user_input = st.text_input("系统号", value=system_user, help="示例：admin / teacher_hz")
    profile = get_user_profile(user_input)
    if profile:
        st.session_state["system_user"] = user_input
        system_user = user_input
        allowed_tenants = profile.get("tenants", [])
        tenant_options = allowed_tenants or [tenant_defs[0]["tenant_id"]]
        idx = tenant_options.index(current_tenant) if current_tenant in tenant_options else 0
        chosen_tenant = st.selectbox(
            "城市",
            options=tenant_options,
            index=idx,
            format_func=lambda x: f"{tenant_name_map.get(x, x)} ({x})",
        )
        st.caption(f"角色：`{profile.get('role', 'city_viewer')}`")
        st.session_state["tenant_id"] = chosen_tenant
        current_tenant = chosen_tenant
        kb_path = str(resolve_tenant_kb_path(current_tenant))
        history_path = str(resolve_tenant_history_path(current_tenant))
        mapping_path = str(tenant_mapping_path(current_tenant))
        bank_path = tenant_bank_path(current_tenant)
        bank_state_key = f"question_bank_{current_tenant}"
        set_active_tenant(current_tenant)
        os.environ["TENANT_ID"] = current_tenant
    else:
        st.error("系统号未配置，请在 tenant_users.json 中增加账号配置。")
        st.stop()

    with st.expander("⚙️ 配置 / API Key（默认收起）", expanded=False):
        # Load API Key from file
        config_path = "填写您的Key.txt"
        default_openai_key = ""
        default_deepseek_key = ""
        
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if "OPENAI_API_KEY=" in line and "请将您的Key粘贴在这里" not in line:
                        default_openai_key = line.split("=", 1)[1].strip()
                    if "DEEPSEEK_API_KEY=" in line and "请将您的Key粘贴在这里" not in line:
                        default_deepseek_key = line.split("=", 1)[1].strip()
        
        gpt_api_key = st.text_input("GPT API Key", value=default_openai_key, type="password")
        deepseek_api_key = st.text_input("DeepSeek API Key", value=default_deepseek_key, type="password")
        base_url = st.text_input("Base URL", value="https://openapi-ait.ke.com")
        model_name = st.text_input("模型名称", value="doubao-seed-1.8", help="所有节点统一使用此模型，当前默认 doubao-seed-1.8")
        # Select key based on model family
        if "deepseek" in model_name.lower():
            api_key = deepseek_api_key
        else:
            api_key = gpt_api_key
        
        st.divider()
        proxy = st.text_input("代理地址 (可选)", placeholder="http://127.0.0.1:7890")
        if proxy:
            os.environ["HTTP_PROXY"] = proxy
            os.environ["HTTPS_PROXY"] = proxy
        
        if not api_key:
            st.warning("请在左侧填入 API Key 或修改 '填写您的Key.txt'")

# --- Main Area ---

# 1. Initialize Retriever (Cached)
@st.cache_resource
def get_retriever(kb_file: str, history_file: str, mapping_file: str, tenant_id: str):
    return KnowledgeRetriever(kb_file, history_file, mapping_file)

# --- Question Bank Helpers ---
def load_bank(path: pathlib.Path):
    if path.exists():
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return []

def save_bank(items, path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items), encoding="utf-8")


def guard_tenant_access() -> None:
    try:
        assert_tenant_access(system_user, current_tenant)
    except PermissionError as e:
        st.error(f"权限校验失败：{e}")
        st.stop()

# If in 题库模式，直接展示题库并返回
if mode_main == "题库":
    guard_tenant_access()
    st.subheader("📚 当前题库")
    st.caption(f"当前城市：`{tenant_name_map.get(current_tenant, current_tenant)}`")
    bank = st.session_state.get(bank_state_key, load_bank(bank_path))
    st.caption(f"题库条目：{len(bank)}")
    if bank:
        df_bank = pd.DataFrame(bank)
        cols_show = [c for c in ["题干","选项1","选项2","选项3","选项4","正确答案","解析","来源路径"] if c in df_bank.columns]
        st.dataframe(df_bank[cols_show] if cols_show else df_bank)
        st.divider()
        st.subheader("🗑️ 删除题库题目")
        st.caption("点击删除后立即生效（不可撤销）。")
        for idx, item in enumerate(bank):
            stem = str(item.get("题干", "")).strip()
            stem_display = stem if len(stem) <= 80 else stem[:80] + "…"
            col1, col2 = st.columns([6, 1])
            with col1:
                st.markdown(f"{idx+1}. {stem_display}")
            with col2:
                if st.button("删除", key=f"delete_bank_{idx}"):
                    bank.pop(idx)
                    save_bank(bank, bank_path)
                    st.session_state[bank_state_key] = bank
                    write_audit_log(current_tenant, system_user, "bank.delete", "question_bank", str(idx))
                    st.success("已删除")
                    st.rerun()
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df_bank.to_excel(writer, index=False)
        st.download_button("📥 下载题库 Excel", data=buf.getvalue(), file_name="question_bank.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.info("当前题库为空。请在“出题”工作区生成并点击“加入题库”。")
    st.stop()

if mode_main == "切片核对":
    guard_tenant_access()
    st.subheader("📄 城市教材切片核对")
    st.caption(f"当前城市：`{tenant_name_map.get(current_tenant, current_tenant)}`")
    if not os.path.exists(kb_path):
        st.warning(f"当前城市暂无切片文件：`{kb_path}`")
        st.stop()
    slices = []
    with open(kb_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if not line.strip():
                continue
            item = json.loads(line)
            item["_slice_id"] = i
            slices.append(item)
    slice_review_store = load_slice_review(current_tenant)
    if not slices:
        st.info("暂无切片数据")
        st.stop()
    # Toolbar
    tool1, tool2 = st.columns([2, 1])
    with tool1:
        status_filter = st.selectbox(
            "审核状态筛选",
            options=["全部", "pending", "approved", "rejected", "revised"],
            index=0,
            key="slice_status_filter",
        )
    with tool2:
        keyword = st.text_input("路径关键词", value="", key="slice_keyword_filter")

    df_all = pd.DataFrame(
        [
            {
                "slice_id": s["_slice_id"],
                "完整路径": s.get("完整路径", ""),
                "掌握程度": s.get("掌握程度", ""),
                "review_status": slice_review_store.get(str(s["_slice_id"]), {}).get("review_status", "pending"),
                "review_comment": slice_review_store.get(str(s["_slice_id"]), {}).get("comment", ""),
                "核心内容预览": build_slice_raw_text(s)[:80],
            }
            for s in slices
        ]
    )

    # Metrics
    total_cnt = len(df_all)
    pending_cnt = int((df_all["review_status"] == "pending").sum()) if total_cnt else 0
    approved_cnt = int((df_all["review_status"] == "approved").sum()) if total_cnt else 0
    rejected_cnt = int((df_all["review_status"] == "rejected").sum()) if total_cnt else 0
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("总切片", total_cnt)
    m2.metric("待审核", pending_cnt)
    m3.metric("已通过", approved_cnt)
    m4.metric("已驳回", rejected_cnt)

    df = df_all.copy()
    if status_filter != "全部":
        df = df[df["review_status"] == status_filter]
    if keyword.strip():
        kw = keyword.strip()
        df = df[df["完整路径"].astype(str).str.contains(kw, na=False)]

    if df.empty:
        st.info("当前筛选条件下暂无切片")
        st.stop()

    # Batch-first editor
    select_all_slices = st.checkbox("全选当前筛选结果", value=False, key="slice_select_all")
    df_edit = df.copy()
    df_edit.insert(0, "selected", select_all_slices)
    edited_df = st.data_editor(
        df_edit,
        use_container_width=True,
        hide_index=True,
        disabled=["slice_id", "完整路径", "掌握程度", "review_status", "review_comment", "核心内容预览"],
        column_config={
            "selected": st.column_config.CheckboxColumn("选择"),
            "slice_id": st.column_config.NumberColumn("ID"),
        },
        key="slice_review_editor",
    )
    slice_options = [int(x) for x in df["slice_id"].tolist()]
    selected_slice_batch = [int(x) for x in edited_df.loc[edited_df["selected"] == True, "slice_id"].tolist()]

    st.markdown("### 批量审核")
    batch_review_status = st.selectbox(
        "批量审核结果",
        options=["approved", "rejected", "revised", "pending"],
        index=0,
        key="slice_batch_status",
    )
    batch_review_comment = st.text_input("批量审核备注", value="", key="slice_batch_comment")
    if st.button("批量保存审核", key="save_slice_batch"):
        if not selected_slice_batch:
            st.warning("请先选择至少一条切片")
        else:
            for sid in selected_slice_batch:
                upsert_slice_review(
                    tenant_id=current_tenant,
                    slice_id=int(sid),
                    review_status=batch_review_status,
                    reviewer=system_user,
                    comment=batch_review_comment,
                )
                write_audit_log(
                    current_tenant,
                    system_user,
                    "slice.review.batch",
                    "slice_item",
                    str(sid),
                    after={"review_status": batch_review_status, "review_comment": batch_review_comment},
                )
            st.success(f"已批量更新 {len(selected_slice_batch)} 条切片")
            st.rerun()

    st.divider()
    st.markdown("### 详情与单条审核")
    selected_slice = st.selectbox("选择切片", options=slice_options, key="slice_single_select")
    selected_obj = next((s for s in slices if s["_slice_id"] == selected_slice), None)
    if selected_obj:
        left, right = st.columns([2, 1])
        with left:
            st.markdown(f"**路径**：`{selected_obj.get('完整路径', '')}`")
            render_structured_slice(selected_obj.get("结构化内容") or {})
        with right:
            current_review = slice_review_store.get(str(selected_slice), {})
            review_status = st.selectbox(
                "审核结果",
                options=["approved", "rejected", "revised", "pending"],
                index=["approved", "rejected", "revised", "pending"].index(
                    current_review.get("review_status", "pending")
                ),
                key="slice_single_status",
            )
            review_comment = st.text_input("审核备注", value=current_review.get("comment", ""), key="slice_single_comment")
            if st.button("保存切片审核"):
                upsert_slice_review(
                    tenant_id=current_tenant,
                    slice_id=selected_slice,
                    review_status=review_status,
                    reviewer=system_user,
                    comment=review_comment,
                )
                write_audit_log(
                    current_tenant,
                    system_user,
                    "slice.review",
                    "slice_item",
                    str(selected_slice),
                    after={"review_status": review_status, "review_comment": review_comment},
                )
                st.success("切片审核已保存")
    st.stop()

if mode_main == "映射确认":
    guard_tenant_access()
    st.subheader("🔗 切片与母题映射确认")
    st.caption(f"当前城市：`{tenant_name_map.get(current_tenant, current_tenant)}`")
    if not os.path.exists(mapping_path):
        st.warning(f"当前城市暂无映射文件：`{mapping_path}`")
        st.stop()
    mapping = json.loads(pathlib.Path(mapping_path).read_text(encoding="utf-8"))
    review_store = load_mapping_review(current_tenant)
    rows = []
    for slice_id, payload in mapping.items():
        for m in payload.get("matched_questions", [])[:3]:
            map_key = f"{slice_id}:{m.get('question_index')}"
            review = review_store.get(map_key, {})
            rows.append(
                {
                    "map_key": map_key,
                    "slice_id": slice_id,
                    "完整路径": payload.get("完整路径", ""),
                    "question_index": m.get("question_index"),
                    "confidence": m.get("confidence"),
                    "method": m.get("method"),
                    "confirm_status": review.get("confirm_status", "auto_pending"),
                    "review_comment": review.get("comment", ""),
                }
            )
    if not rows:
        st.info("暂无可确认映射")
        st.stop()
    map_df_all = pd.DataFrame(rows)

    # Toolbar
    mt1, mt2 = st.columns([2, 1])
    with mt1:
        map_status_filter = st.selectbox(
            "映射状态筛选",
            options=["全部", "auto_pending", "confirmed", "rejected", "remapped"],
            index=0,
            key="map_status_filter",
        )
    with mt2:
        map_keyword = st.text_input("路径关键词", value="", key="map_keyword_filter")

    # Metrics
    m_total = len(map_df_all)
    m_pending = int((map_df_all["confirm_status"] == "auto_pending").sum()) if m_total else 0
    m_confirm = int((map_df_all["confirm_status"] == "confirmed").sum()) if m_total else 0
    m_reject = int((map_df_all["confirm_status"] == "rejected").sum()) if m_total else 0
    mm1, mm2, mm3, mm4 = st.columns(4)
    mm1.metric("映射总数", m_total)
    mm2.metric("待确认", m_pending)
    mm3.metric("已确认", m_confirm)
    mm4.metric("已驳回", m_reject)

    map_df = map_df_all.copy()
    if map_status_filter != "全部":
        map_df = map_df[map_df["confirm_status"] == map_status_filter]
    if map_keyword.strip():
        map_df = map_df[map_df["完整路径"].astype(str).str.contains(map_keyword.strip(), na=False)]
    if map_df.empty:
        st.info("当前筛选条件下暂无映射记录")
        st.stop()

    select_all_maps = st.checkbox("全选当前筛选结果", value=False, key="map_select_all")
    map_edit = map_df.copy()
    map_edit.insert(0, "selected", select_all_maps)
    map_edited = st.data_editor(
        map_edit,
        use_container_width=True,
        hide_index=True,
        disabled=["map_key", "slice_id", "完整路径", "question_index", "confidence", "method", "confirm_status", "review_comment"],
        column_config={"selected": st.column_config.CheckboxColumn("选择")},
        key="map_review_editor",
    )
    map_options = map_df["map_key"].tolist()
    st.divider()
    st.markdown("### 批量确认")
    selected_map_batch = map_edited.loc[map_edited["selected"] == True, "map_key"].tolist()
    batch_map_status = st.selectbox("批量确认状态", options=["confirmed", "rejected", "remapped"], key="map_batch_status")
    batch_map_comment = st.text_input("批量备注", value="", key="map_batch_comment")
    batch_remap_target = st.text_input("批量重映射母题ID（仅 remapped 时可填）", value="", key="map_batch_target")
    if st.button("批量保存映射确认", key="save_map_batch"):
        if not selected_map_batch:
            st.warning("请先选择至少一条映射记录")
        else:
            for mk in selected_map_batch:
                upsert_mapping_review(
                    tenant_id=current_tenant,
                    map_key=mk,
                    confirm_status=batch_map_status,
                    reviewer=system_user,
                    comment=batch_map_comment,
                    target_mother_question_id=batch_remap_target if batch_map_status == "remapped" else "",
                )
                write_audit_log(current_tenant, system_user, "map.confirm.batch", "slice_question_map", mk)
            st.success(f"已批量更新 {len(selected_map_batch)} 条映射")
            st.rerun()

    st.divider()
    st.markdown("### 单条确认")
    selected_key = st.selectbox("选择映射记录", options=map_options, key="map_single_select")
    status = st.selectbox("确认状态", options=["confirmed", "rejected", "remapped"], key="map_single_status")
    comment = st.text_input("备注", value="", key="map_single_comment")
    remap_target = st.text_input("重映射母题ID（仅 remapped 时可填）", value="", key="map_single_target")
    if st.button("保存确认结果", key="save_map_single"):
        upsert_mapping_review(
            tenant_id=current_tenant,
            map_key=selected_key,
            confirm_status=status,
            reviewer=system_user,
            comment=comment,
            target_mother_question_id=remap_target,
        )
        write_audit_log(current_tenant, system_user, "map.confirm", "slice_question_map", selected_key)
        st.success("已保存")
    st.stop()

try:
    guard_tenant_access()
    retriever = get_retriever(kb_path, history_path, mapping_path, current_tenant)
    st.success(f"✅ 知识库已加载 ({len(retriever.kb_data)} 条知识点)")
except Exception as e:
    st.error(f"❌ 知识库加载失败: {e}")
    st.stop()

# 2. Chapter Selection
st.subheader("1. 选择出题范围")

# Extract all unique chapters/sections from KB
def normalize_mastery(val):
    """Normalize mastery label."""
    if val is None:
        return "未标注"
    txt = str(val).strip()
    return txt if txt else "未标注"

def is_similar_in_batch(text, prior_texts, vectorizer, threshold=0.9):
    """Check if text is highly similar to any prior_texts using TF-IDF cosine."""
    if not prior_texts:
        return False, 0.0, None
    try:
        vec_new = vectorizer.transform([text])
        vec_prev = vectorizer.transform(prior_texts)
        sims = (vec_prev @ vec_new.T).toarray().ravel()
        best_idx = int(np.argmax(sims))
        best_score = float(sims[best_idx])
        if best_score >= threshold:
            return True, best_score, prior_texts[best_idx]
        return False, best_score, None
    except Exception:
        return False, 0.0, None

all_paths = [item['完整路径'] for item in retriever.kb_data if item['核心内容']]
split_paths = [p.split(" > ") for p in all_paths]
max_levels = max((len(p) for p in split_paths), default=0)

chapter_mode = st.radio(
    "章节范围",
    ["全部章节", "自定义章节", "仅计算类章节"],
    index=0,
    horizontal=True,
    key="chapter_mode"
)

selected_levels = []
level_options = []

def _path_matches_level(path_parts, level_selections):
    for idx, selections in enumerate(level_selections):
        if selections and (idx >= len(path_parts) or path_parts[idx] not in selections):
            return False
    return True

def _level_options_for(level_idx, level_selections):
    opts = set()
    for parts in split_paths:
        if len(parts) <= level_idx:
            continue
        # Match only previous levels
        if _path_matches_level(parts, level_selections[:level_idx]):
            opts.add(parts[level_idx])
    return sorted(opts)

if chapter_mode == "自定义章节":
    st.caption("按层级筛选知识切片路径（多级联动）")
    for level_idx in range(max_levels):
        opts = _level_options_for(level_idx, selected_levels)
        if not opts:
            break
        sig_key = f"level_opts_sig_{level_idx}"
        opts_sig = "|".join(opts)
        if st.session_state.get(sig_key) != opts_sig:
            st.session_state[sig_key] = opts_sig
            # Clear deeper selections when upstream options change
            for deeper in range(level_idx, max_levels):
                st.session_state.pop(f"path_level_{deeper+1}", None)
        selected = st.multiselect(f"第{level_idx+1}级筛选", opts, key=f"path_level_{level_idx+1}")
        selected_levels.append(selected)
        level_options.append(opts)
elif chapter_mode == "仅计算类章节":
    calc_keywords = ["计算", "税费", "贷款", "建筑指标", "面积"]
    # Only keep paths that match calc keywords
    calc_paths = [p for p in all_paths if any(k in p for k in calc_keywords)]
    split_paths = [p.split(" > ") for p in calc_paths]
    max_levels = max((len(p) for p in split_paths), default=0)
    st.caption(f"已自动筛选计算类章节：{len(set(calc_paths))} 条路径")
else:
    # 全部章节：不做层级选择
    selected_levels = []

# Filter KB based on selection (by path levels)
if chapter_mode == "自定义章节":
    chapter_chunks = [
        c for c in retriever.kb_data
        if c['核心内容'] and _path_matches_level(c['完整路径'].split(" > "), selected_levels)
    ]
elif chapter_mode == "仅计算类章节":
    chapter_chunks = [
        c for c in retriever.kb_data
        if c['核心内容'] and any(k in c['完整路径'] for k in calc_keywords)
    ]
else:
    chapter_chunks = [c for c in retriever.kb_data if c['核心内容']]

# Mastery options should be derived from chapter selection only (stable)
mastery_levels = sorted(list({normalize_mastery(c.get('掌握程度')) for c in chapter_chunks}))
mastery_mode = st.radio(
    "掌握程度范围",
    ["全部掌握程度", "自定义"],
    index=0,
    horizontal=True,
    key="mastery_mode"
)

if mastery_mode == "自定义":
    mastery_sig = "|".join(mastery_levels)
    if st.session_state.get("mastery_levels_sig") != mastery_sig:
        st.session_state["mastery_levels_sig"] = mastery_sig
        st.session_state.pop("mastery_select", None)
    selected_mastery = st.multiselect("选择掌握程度 (可多选)", mastery_levels, key="mastery_select")
else:
    selected_mastery = mastery_levels

if selected_mastery:
    target_chunks = [
        c for c in chapter_chunks
        if normalize_mastery(c.get('掌握程度')) in selected_mastery
    ]
else:
    target_chunks = chapter_chunks

# Enforce: only approved slices are eligible for generation
slice_review_store_for_gen = load_slice_review(current_tenant)
approved_slice_ids = {
    int(k) for k, v in slice_review_store_for_gen.items()
    if isinstance(v, dict) and v.get("review_status") == "approved" and str(k).isdigit()
}
if approved_slice_ids:
    selected_paths = {str(c.get("完整路径", "")) for c in target_chunks}
    target_chunks = [
        c for idx, c in enumerate(retriever.kb_data)
        if idx in approved_slice_ids and str(c.get("完整路径", "")) in selected_paths
    ]
    st.caption(f"✅ 已按城市审核状态过滤：仅使用 approved 切片（{len(target_chunks)} 条）")
else:
    st.warning("当前城市暂无 approved 切片，出题前请先在“切片核对”中完成审核。")
    st.stop()

# Show counts & distribution
mastery_counter = Counter([normalize_mastery(c.get('掌握程度')) for c in target_chunks])
dist_text = "，".join([f"{k}:{v}" for k, v in mastery_counter.items()]) if mastery_counter else "无"
st.write(f"🎯 选中范围包含 **{len(target_chunks)}** 个知识点；掌握程度分布：{dist_text}")

if not target_chunks:
    st.error("当前章节 + 掌握程度筛选为空，请调整选择。")
    st.stop()

# 3. Generation Settings
st.subheader("2. 出题设置")
mode_choice = st.radio("出题范围模式", ["每个知识点各出一题", "自定义"], index=0, horizontal=True)
per_kb = mode_choice == "每个知识点各出一题"

# defaults
num_questions = 5
difficulty = "随机"
question_type = "单选题"
generation_mode = "灵活"

if mode_choice == "自定义":
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        num_questions = st.number_input(
            "生成题目数量",
            min_value=1,
            max_value=200,
            value=5,
            help="自定义模式生效"
        )
    with col2:
        difficulty = st.selectbox("难度偏好", ["随机", "简单 (0.3-0.5)", "中等 (0.5-0.7)", "困难 (0.7-0.9)"])
    with col3:
        question_type = st.selectbox("题目类型", ["单选题", "多选题", "判断题", "随机"])
    with col4:
        generation_mode = st.selectbox(
            "出题模式", 
            ["灵活", "严谨"], 
            index=0,
            help="灵活模式：场景化、灵活表达，适合日常练习。严谨模式：严格按照知识点输出，适合标准化考试。"
        )
else:
    st.caption(f"本次题量 = 已筛选知识点数 ({len(target_chunks)})；题型/难度/模式采用默认值：题型单选题，难度随机，模式灵活。")

# 4. Generate Button
def _build_run_signature(chapter_mode, selected_levels, selected_mastery, per_kb, num_questions, difficulty, question_type, generation_mode):
    return {
        "chapter_mode": str(chapter_mode),
        "selected_levels": list(selected_levels) if selected_levels else [],
        "selected_mastery": list(selected_mastery) if selected_mastery else [],
        "per_kb": bool(per_kb),
        "num_questions": int(num_questions),
        "difficulty": str(difficulty),
        "question_type": str(question_type),
        "generation_mode": str(generation_mode),
    }

run_signature = _build_run_signature(
    chapter_mode, selected_levels, selected_mastery, per_kb, num_questions, difficulty, question_type, generation_mode
)

if st.button("🚀 开始出题", type="primary", disabled=not api_key):
    # Clear previous run UI blocks
    if "run_ui_container" in st.session_state:
        st.session_state["run_ui_container"].empty()
    run_ui_container = st.empty()
    st.session_state["run_ui_container"] = run_ui_container
    st.session_state["run_attempted"] = True
    with run_ui_container.container():
        # Unique run id for widget keys
        run_id = int(time.time() * 1000)
        st.session_state["run_id"] = run_id
        st.session_state["active_run_signature"] = run_signature
        st.session_state["is_generating"] = True
        progress_bar = st.progress(0)
        status_text = st.empty()
        results = []
        all_results = []
        bank = st.session_state.get(bank_state_key, load_bank(bank_path))
    
    # ✅ Keep "随机" as-is, let specialist nodes decide the question type
    # Don't call pick_question_type here - pass "随机" directly to config
    # Writer will respect the draft's question type when config is "随机"
    
    # Select chunks
    if per_kb:
        selected_chunks_for_gen = target_chunks
        num_questions = len(target_chunks)
    else:
        selected_chunks_for_gen = [random.choice(target_chunks) for _ in range(num_questions)]
        num_questions = len(selected_chunks_for_gen)
    run_total = len(selected_chunks_for_gen)
    st.session_state["run_total"] = run_total
    
    prior_stems = []
    
    # ✅ Random difficulty balanced distribution: when difficulty is "随机", ensure even distribution of easy/medium/hard
    difficulty_levels = ["简单 (0.3-0.5)", "中等 (0.5-0.7)", "困难 (0.7-0.9)"]
    if difficulty == "随机":
        # Cyclic distribution to ensure balanced ratio (1:1:1)
        random.shuffle(difficulty_levels)  # Randomize the initial order
        difficulty_pool = (difficulty_levels * ((num_questions // 3) + 1))[:num_questions]

    run_block = run_ui_container.container()
    with run_block:
        run_seq = 0
        for i, chunk in enumerate(selected_chunks_for_gen):
            run_seq += 1
            seq = run_seq
            # Generate with Visuals
            # If user changed filters mid-run, abort and ask to restart
            if st.session_state.get("active_run_signature") != run_signature:
                st.warning("⚠️ 出题范围/设置已更改，本轮生成已停止，请重新开始出题。")
                break
            with st.status(f"🤖 第 {seq}/{run_total} 题: 智能体协同中 (LangGraph)...", expanded=True) as status:
                q_json = None
                error_msg = None
                started = False
                finished = False
                state = {}  # Track state for reroute detection
                
                # Initial State (examples will be fetched inside graph after routing)
                inputs = {
                    "kb_chunk": chunk, 
                    "examples": [],  # Will be populated by specialist/finance nodes
                    "term_locks": [],
                    "retry_count": 0,
                    "logs": []
                }
                
                # Parse difficulty range
                difficulty_range = None
                current_difficulty = difficulty
                if difficulty == "随机":
                    # Assign difficulty from pool to ensure balanced distribution
                    current_difficulty = difficulty_pool[i]
                
                if current_difficulty != "随机":
                    # Extract difficulty range from string like "中等 (0.5-0.7)"
                    import re
                    match = re.search(r'\(([\d.]+)-([\d.]+)\)', str(current_difficulty))
                    if match:
                        difficulty_range = (float(match.group(1)), float(match.group(2)))
                
                # Config for LLM (now includes retriever and question_type)
                config = {
                    "configurable": {
                        "model": model_name,  # 所有节点统一使用此模型
                        "api_key": api_key, 
                        "base_url": base_url,
                        "retriever": retriever,
                        "question_type": question_type,  # ✅ Pass "随机" as-is, don't convert to specific type
                        "generation_mode": generation_mode,  # 灵活/严谨模式
                        "difficulty_range": difficulty_range  # 难度范围，如 (0.5, 0.7) 或 None
                    }
                }
                
            try:
                # 显示当前题目的配置（如果是随机难度，显示分配的难度）
                if difficulty == "随机":
                    st.caption(f"📊 本题难度：{current_difficulty}")
                
                # 添加初始提示，让用户知道系统正在工作
                st.info("🔄 正在初始化... 首次调用可能需要30-60秒，复杂计算题可能需要2-5分钟，请耐心等待")
                
                # Stream events from LangGraph
                event_count = 0
                fixed_in_run = False
                current_state = {}  # Track current state for reroute detection
                last_critic_result = {}
                for event in graph_app.stream(inputs, config=config):
                    started = True
                    event_count += 1
                    # 清除初始提示（在第一次事件后）
                    if event_count == 1:
                        st.empty()  # 清除初始提示
                    
                    # event is a dict like {'node_name': {'key': 'value'}}
                    for node_name, state_update in event.items():
                        # Update current state for reroute detection
                        current_state.update(state_update)
                        if node_name == "critic" and isinstance(state_update.get("critic_result"), dict):
                            last_critic_result = state_update.get("critic_result") or {}

                        if 'logs' in state_update:
                            for log in state_update['logs']:
                                st.write(log)

                        # Show Router Decision
                        if node_name == "router":
                            # Check if this is a reroute
                            retry_count = state_update.get('retry_count', 0)
                            is_reroute = retry_count > 0
                            
                            reroute_label = " (重新路由)" if is_reroute else ""
                            with st.expander(f"🧠 路由决策 (Router Decision){reroute_label}", expanded=True):
                                if 'router_details' in state_update:
                                    details = state_update['router_details']
                                    
                                    # Show reroute info if applicable (FR9.9)
                                    if is_reroute:
                                        prev_critic_result = current_state.get('critic_result', {})
                                        prev_fail_reason = prev_critic_result.get('reason', '')
                                        prev_issue_type = prev_critic_result.get('issue_type', '')
                                        st.warning(f"🔄 **重新路由** (第 {retry_count} 次重试)")
                                        if prev_fail_reason:
                                            st.caption(f"**重路由原因**: {prev_fail_reason}")
                                        if prev_issue_type:
                                            issue_type_label = "🔴 严重问题" if prev_issue_type == "major" else "🟡 轻微问题"
                                            st.caption(f"**问题类型**: {issue_type_label}")
                                        st.divider()
                                    
                                    cols = st.columns([2, 1])
                                    with cols[0]:
                                        st.markdown(f"**选中知识点**: `{details.get('path', 'N/A')}`")
                                        st.markdown(f"**掌握程度**: `{details.get('mastery', '未知')}`")
                                        struct_content = details.get("struct_content")
                                        if struct_content:
                                            st.info("**核心内容片段**")
                                            render_structured_slice(struct_content)
                                        else:
                                            st.info(f"**核心内容片段**: \n\n{details.get('content', '')}")
                                    with cols[1]:
                                        # 兼容新旧字段名
                                        calc_score = details.get('score_calculation') or details.get('score_finance', 0)
                                        need_calc = details.get('need_calculation', False)
                                        st.metric("计算相关度", calc_score)
                                        st.metric("法律相关度", details.get('score_legal', 0))
                                        if need_calc:
                                            st.info(f"📊 需要计算题: {need_calc}")
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
                                st.json(filter_json_display(state_update['draft']))

                        # Show Calculator Calculation & Draft
                        if node_name == "calculator":
                            # ✅ Display which Calculator model was actually used
                            calculator_model_used = state_update.get('calculator_model_used', 'Unknown')
                            if calculator_model_used and calculator_model_used != 'Unknown':
                                model_display = "GPT" if "gpt" in calculator_model_used.lower() else "Deepseek"
                                st.caption(f"🤖 Calculator 模型: **{model_display}**")
                            
                            if 'tool_usage' in state_update:
                                usage = state_update['tool_usage']
                                tool_name = usage.get('tool', 'None')
                                
                                if tool_name and tool_name != "None":
                                    with st.expander("🧮 计算器调用详情（生成阶段）", expanded=True):
                                        st.info(f"调用函数: `{tool_name}`")
                                        st.write("输入参数:", usage['params'])
                                        st.success(f"计算结果: {usage['result']}")
                                else:
                                    with st.expander("🧮 计算器分析（生成阶段）", expanded=False):
                                        st.caption("ℹ️ 生成阶段判断：无需数值计算，仅进行概念/逻辑生成。")
                            
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
                                with st.expander("📄 查看计算专家初稿"):
                                    st.json(filter_json_display(state_update['draft']))

                        # Show Writer Output
                        if node_name == "writer" and 'final_json' in state_update:
                            with st.expander("✍️ 查看作家润色后内容 (待审核)"):
                                st.json(filter_json_display(state_update['final_json']))
                                writer_issues = state_update.get("writer_format_issues") or []
                                if writer_issues:
                                    st.warning(f"⚠️ Writer 格式自检问题: {', '.join(writer_issues)}")
                                # Show last char of each option for debugging hidden punctuation
                                q_json = state_update.get('final_json', {})
                                if isinstance(q_json, dict):
                                    option_ends = {}
                                    for i in range(1, 9):
                                        key = f"选项{i}"
                                        val = q_json.get(key, "")
                                        if val:
                                            last_char = str(val)[-1:]
                                            option_ends[key] = last_char
                                    if option_ends:
                                        st.caption(f"🔎 选项末尾字符: {option_ends}")

                        # Show Critic Review (Pass or Fail)
                        if node_name == "critic":
                            feedback = state_update.get('critic_feedback', 'Unknown')
                            details = state_update.get('critic_details', '')
                            critic_result = state_update.get('critic_result', {})
                            writer_issues = current_state.get("writer_format_issues") or []
                            critic_format_issues = current_state.get("critic_format_issues") or []
                            
                            # Get retry count for display (default to 0 if not present)
                            retry_count = state_update.get('retry_count', 0)
                            round_label = f" (Round {retry_count + 1})" if retry_count > 0 else ""
                            
                            # Get issue type and fix strategy (FR9.6)
                            issue_type = critic_result.get('issue_type', 'unknown')
                            fix_strategy = critic_result.get('fix_strategy', '')
                            fix_reason = critic_result.get('fix_reason', '')
                            
                            # ✅ Display which Critic model was actually used
                            critic_model_used = state_update.get('critic_model_used', 'Unknown')
                            if critic_model_used and critic_model_used != 'Unknown':
                                if critic_model_used == "rule-based":
                                    model_display = "Rule-based"
                                else:
                                    model_display = "GPT-5.2" if "gpt" in critic_model_used.lower() else "Deepseek-Reasoner"
                                st.caption(f"🤖 Critic 模型: **{model_display}**")

                            # Display Critic Tool Usage
                            if 'critic_tool_usage' in state_update:
                                usage = state_update['critic_tool_usage']
                                tool_name = usage.get('tool', 'None')
                                
                                if tool_name and tool_name != "None":
                                    with st.expander(f"🕵️ 批评家验证计算（审计阶段）{round_label}", expanded=True):
                                        st.info(f"验证调用: `{tool_name}`")
                                        st.write("验证参数:", usage['params'])
                                        st.success(f"验证结果: {usage['result']}")
                                else:
                                    with st.expander(f"🕵️ 批评家验证分析（审计阶段）{round_label}", expanded=False):
                                        st.caption("ℹ️ 审计阶段判断：无需进行数值验证。")

                            if feedback == "PASS":
                                st.success(f"🕵️ 批评家: 审核通过{round_label}")
                            else:
                                # Display issue type and fix strategy
                                issue_type_label = ""
                                if issue_type == "major":
                                    issue_type_label = "🔴 严重问题"
                                elif issue_type == "minor":
                                    issue_type_label = "🟡 轻微问题"
                                
                                fix_strategy_label = ""
                                if fix_strategy:
                                    strategy_map = {
                                        "fix_explanation": "仅修复解析",
                                        "fix_question": "修复题目",
                                        "fix_both": "同时修复题目和解析",
                                        "regenerate": "重新生成"
                                    }
                                    fix_strategy_label = f" | 修复策略: {strategy_map.get(fix_strategy, fix_strategy)}"
                                
                                st.error(f"🕵️ 批评家: 驳回{round_label} {issue_type_label}{fix_strategy_label}")
                                st.write(f"**驳回原因**: {details}")
                                if fix_reason:
                                    st.caption(f"💡 修复建议: {fix_reason}")
                                if writer_issues:
                                    st.caption(f"🧾 Writer 自检问题（参考）: {', '.join(writer_issues)}")
                                if critic_format_issues:
                                    st.caption(f"🧾 Critic 代码格式校验（参考）: {', '.join(critic_format_issues)}")
                                
                                # Show next step hint
                                if issue_type == "major" and retry_count < 2:
                                    st.caption("🔄 严重问题，将重新路由到Router...")
                                elif issue_type == "major" and retry_count >= 2:
                                    st.caption("🔧 严重问题，连续失败，将强制修复...")
                                else:
                                    st.caption("🔧 轻微问题，将进入Fixer修复流程...")
                                    
                        # Show Fixer Result (FR9.7)
                        if node_name == "fixer":
                            # ✅ 先显示 fixer 执行信息
                            st.info("🔧 修复者已执行")
                            
                            # Get fix strategy from previous critic result if available
                            prev_critic_result = current_state.get('critic_result', {})
                            fix_strategy = prev_critic_result.get('fix_strategy', '')
                            fix_reason = prev_critic_result.get('fix_reason', '')
                            fix_summary = state_update.get('fix_summary', {})
                            
                            strategy_map = {
                                "fix_explanation": "仅修复解析",
                                "fix_question": "修复题目",
                                "fix_both": "同时修复题目和解析",
                                "regenerate": "重新生成"
                            }
                            strategy_label = strategy_map.get(fix_strategy, fix_strategy) if fix_strategy else "修复"
                            
                            # Show fix strategy info
                            if fix_strategy:
                                st.caption(f"📋 修复策略: **{strategy_label}**")
                            if fix_reason:
                                st.caption(f"💡 修复原因: {fix_reason}")
                            required_fixes = state_update.get("critic_required_fixes") or current_state.get("critic_required_fixes") or []
                            if required_fixes:
                                st.caption(f"📌 必须修复项: {', '.join(required_fixes)}")
                            
                            # Show changed fields summary
                            if fix_summary:
                                changed_fields = fix_summary.get("changed_fields") or []
                                if changed_fields:
                                    st.success(f"✅ 已修复字段: {', '.join(changed_fields)}")
                                    fixed_in_run = True
                                    with st.expander("📊 修改前后对比", expanded=False):
                                        st.json({
                                            "before": fix_summary.get("before", {}),
                                            "after": fix_summary.get("after", {})
                                        })
                                else:
                                    if state_update.get("fix_attempted_regen"):
                                        st.warning("⚠️ 修复无变化，已触发强制重写但仍未改动")
                                    else:
                                        st.warning("⚠️ 修复无变化，未产生任何改动")
                                unmet_required = fix_summary.get("unmet_required_fixes") or []
                                if unmet_required:
                                    st.error(f"❌ 未满足必修项: {', '.join(unmet_required)}")
                            
                            # Show fixed content
                            if 'final_json' in state_update:
                                with st.expander(f"🔧 修复后完整内容", expanded=True):
                                    st.json(filter_json_display(state_update['final_json']))
                            else:
                                st.warning("⚠️ Fixer 执行完成但未返回 final_json")

                        if 'final_json' in state_update:
                            q_json = state_update['final_json']
                            finished = True
                                
                    # Check final state
                    if q_json:
                        # Validate Schema
                        try:
                            ExamQuestion(**q_json)
                            slice_raw = build_slice_raw_text(chunk)
                            slice_struct = chunk.get("结构化内容") or {}
                            q_json["切片原文"] = slice_raw
                            q_json["结构化内容"] = slice_struct
                            # Only evaluate pass/fail after critic has emitted a result
                            critic_result_available = isinstance(last_critic_result, dict) and ("passed" in last_critic_result)
                            if critic_result_available:
                                passed = bool(last_critic_result.get("passed"))
                                if not passed:
                                    q_json['来源路径'] = chunk['完整路径']
                                    q_json['是否修复'] = "是" if fixed_in_run else "否"
                                    q_json['是否通过'] = "否"
                                    all_results.append(q_json)
                                    status.update(label=f"❌ 第 {seq} 题审核未通过，未保存", state="error", expanded=True)
                                    continue
                            else:
                                status.update(label=f"⏳ 第 {seq} 题等待审核结果", state="running", expanded=True)
                                continue
                            # Check against existing question bank to avoid duplicates
                            is_dup_bank, dup_score_bank, _ = is_similar_in_batch(
                                q_json.get('题干', ''), [item.get("题干","") for item in bank], retriever.vectorizer, threshold=0.9
                            )
                            if is_dup_bank:
                                status.update(label=f"⚠️ 第 {seq} 题与题库相似度 {dup_score_bank:.2f}，已丢弃", state="error", expanded=True)
                                continue
                            # In-batch near-duplicate check (avoid同轮次高度重复)
                            is_dup, dup_score, dup_text = is_similar_in_batch(
                                q_json.get('题干', ''), prior_stems, retriever.vectorizer, threshold=0.9
                            )
                            if is_dup:
                                status.update(label=f"⚠️ 第 {seq} 题与本轮已有题相似度 {dup_score:.2f}，已丢弃并需重试", state="error", expanded=True)
                                continue
                            status.update(label=f"✅ 第 {seq} 题生成成功", state="complete", expanded=False)
                            q_json['来源路径'] = chunk['完整路径']
                            q_json['是否修复'] = "是" if fixed_in_run else "否"
                            q_json['是否通过'] = "是"
                            results.append(q_json)
                            all_results.append(q_json)
                            prior_stems.append(q_json.get('题干', ''))
                        except ValidationError as e:
                            st.write(f"❌ Validation Error: {e}")
                            status.update(label=f"❌ 第 {seq} 题格式错误", state="error", expanded=True)
                    else:
                        if not started:
                            status.update(label=f"⏳ 第 {seq} 题初始化中，请稍候", state="running", expanded=True)
                        elif finished and not q_json:
                            status.update(label=f"❌ 第 {seq} 题生成失败 (Max Retries)", state="error", expanded=True)
                        else:
                            # still running, don't mark as failure
                            status.update(label=f"⏳ 第 {seq} 题处理中，请稍候", state="running", expanded=True)
                         
            except Exception as e:
                error_msg = str(e)
                # Handle timeout errors with friendly message
                if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                    st.error(f"⏱️ **请求超时**：模型响应时间过长（已设置300秒超时）。")
                    st.info("💡 **建议**：\n"
                           "- 复杂计算题可能需要更长时间，请稍后重试\n"
                           "- 可以尝试切换到更快的模型（如 qwen-plus-latest）\n"
                           "- 检查网络连接是否稳定")
                    status.update(label=f"⏱️ 第 {seq} 题超时（请重试）", state="error", expanded=True)
                else:
                    st.error(f"Graph Error: {e}")
                    status.update(label=f"❌ 第 {seq} 题运行出错", state="error", expanded=True)
            
                progress_bar.progress(min((i + 1) / max(run_total, 1), 1.0))
        
        status_text.text("✅ 出题完成！")
        st.session_state["is_generating"] = False
        st.session_state["last_results"] = results
        st.session_state["last_results_all"] = all_results
        st.session_state["last_run_id"] = run_id

# Show last results (persisted) after rerun so "加入题库" works reliably
last_results = st.session_state.get("last_results") or []
last_results_all = st.session_state.get("last_results_all") or []
if (last_results or last_results_all) and not st.session_state.get("is_generating", False):
    # Auto-add passed results to question bank (once per run)
    last_run_id = st.session_state.get("last_run_id", 0)
    if st.session_state.get("last_added_run_id") != last_run_id:
        bank = st.session_state.get(bank_state_key, load_bank(bank_path))
        bank.extend([r for r in last_results])
        save_bank(bank, bank_path)
        st.session_state[bank_state_key] = bank
        st.session_state["last_added_run_id"] = last_run_id
        write_audit_log(current_tenant, system_user, "bank.auto_add", "question_bank", str(last_run_id))
        st.toast("✅ 审核通过题目已自动加入题库", icon="✅")

    df = pd.DataFrame(last_results_all or last_results)
    preview_cols = ["题干", "选项1", "选项2", "选项3", "选项4", "正确答案", "解析", "难度值", "来源路径", "是否修复", "是否通过"]
    final_preview_cols = [c for c in preview_cols if c in df.columns]
    df_preview = df[final_preview_cols]

    st.subheader("3. 结果预览")
    # Highlight fixed questions in dataframe
    def highlight_fixed(row):
        if row.get('是否修复') == '是':
            return ['background-color: #fff3cd'] * len(row)
        return [''] * len(row)
    
    styled_df = df_preview.style.apply(highlight_fixed, axis=1)
    st.dataframe(styled_df)

    # 不展示题目回顾列表（避免回放/重复显示）

    # 不展示“出题过程回放”

    # Download
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    file_name = f"exam_questions_{timestamp}.xlsx"
    
    # Convert to Excel in memory (match required template header)
    import io
    buffer = io.BytesIO()
    export_rows = []
    for item in last_results:
        # Robust path splitting with fallback
        path = str(item.get("来源路径", ""))
        parts = [p.strip() for p in path.split(" > ") if p.strip()]
        
        # Safe get with type conversion for answer
        raw_answer = item.get("正确答案", "")
        answer = str(raw_answer).strip().upper() if raw_answer else ""
        
        # Safe get with fallback for difficulty
        raw_diff = item.get("难度值", 0.5)
        try:
            difficulty = float(raw_diff) if raw_diff not in [None, "", "未知"] else 0.5
        except (ValueError, TypeError):
            difficulty = 0.5
        
        # Safe string conversion with None handling
        def safe_str(val, default=""):
            if val is None:
                return default
            return str(val).strip() if val else default
        
        export_rows.append({
            "题干(必填)": safe_str(item.get("题干", "")),
            "选项A(必填)": safe_str(item.get("选项1", "")),
            "选项B(必填)": safe_str(item.get("选项2", "")),
            "选项C": safe_str(item.get("选项3", "")),
            "选项D": safe_str(item.get("选项4", "")),
            "选项E": safe_str(item.get("选项5", "")),
            "选项F": safe_str(item.get("选项6", "")),
            "选项G": safe_str(item.get("选项7", "")),
            "选项H": safe_str(item.get("选项8", "")),
            "答案选项(必填)": answer,
            "难度": difficulty,
            "题型": _resolve_calc_question_type(item),
            "一级知识点": parts[0] if len(parts) > 0 else "",
            "二级知识点": parts[1] if len(parts) > 1 else "",
            "三级知识点": parts[2] if len(parts) > 2 else "",
            "四级知识点": parts[3] if len(parts) > 3 else "",
            "题目解析": safe_str(item.get("解析", "")),
            "切片原文": safe_str(item.get("切片原文", "")),
            "结构化内容": _stringify_structured_value(item.get("结构化内容", "")),
        })
    export_df = pd.DataFrame(export_rows, columns=[
        "题干(必填)",
        "选项A(必填)",
        "选项B(必填)",
        "选项C",
        "选项D",
        "选项E",
        "选项F",
        "选项G",
        "选项H",
        "答案选项(必填)",
        "难度",
        "题型",
        "一级知识点",
        "二级知识点",
        "三级知识点",
        "四级知识点",
        "题目解析",
        "切片原文",
        "结构化内容",
    ])
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        export_df.to_excel(writer, index=False)
    
    st.download_button(
        label="📥 下载 Excel 文件",
        data=buffer.getvalue(),
        file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
elif st.session_state.get("run_attempted"):
    st.error("生成失败，未能生成有效题目。请检查 API Key 或网络连接。")
