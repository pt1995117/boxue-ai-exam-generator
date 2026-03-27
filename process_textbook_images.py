#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量处理教材图片页面：使用 qwen3-vl-plus 多模态分析
- 识别图片内容
- 提取坐标轴/趋势图的曲线变化趋势和逻辑关系
- 还原对比表格为 Markdown 格式
- 输出 JSONL 格式
"""
import os
import json
import base64
import glob
import tempfile
import re
from pathlib import Path
from typing import List, Dict, Optional
from openai import OpenAI
from volcenginesdkarkruntime import Ark

def load_config():
    """加载配置文件"""
    config = {}
    cfg_path = os.path.join(os.path.dirname(__file__) or '.', '填写您的Key.txt')
    if os.path.isfile(cfg_path):
        with open(cfg_path, 'r', encoding='utf-8') as f:
            for line in f:
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.split('=', 1)
                    config[k.strip()] = v.strip()
    return config

def image_to_base64(image_path: str) -> str:
    """将图片转换为 base64 编码"""
    with open(image_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')

def _guess_mime_type(image_path: str) -> str:
    ext = Path(image_path).suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    if ext == ".bmp":
        return "image/bmp"
    return "image/png"


def _extract_openai_content(resp_obj: dict) -> Optional[str]:
    choices = resp_obj.get("choices") if isinstance(resp_obj, dict) else None
    if not isinstance(choices, list) or not choices:
        return None
    msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                txt = str(item.get("text", "")).strip()
                if txt:
                    parts.append(txt)
            elif isinstance(item, str):
                txt = item.strip()
                if txt:
                    parts.append(txt)
        return "\n".join(parts).strip() or None
    return None


def _is_markdown_table_delimiter(line: str) -> bool:
    compact = str(line or "").replace(" ", "")
    if "|" not in compact:
        return False
    compact = compact.strip("|")
    return bool(compact) and all(ch in ":-" for ch in compact)


def _split_md_row(line: str) -> list[str]:
    raw = str(line or "").strip().strip("|")
    return [x.strip() for x in raw.split("|")]


def _strip_table_judgement_lines(content: str) -> str:
    """Remove table/non-table judgement lines from image analysis text."""
    text = str(content or "").strip()
    if not text:
        return ""
    banned_patterns = [
        r"^\s*该图(?:片)?不[是为]表格[。！!？?]?\s*$",
        r"^\s*这张图(?:片)?不[是为]表格[。！!？?]?\s*$",
        r"^\s*此图不[是为]表格[。！!？?]?\s*$",
        r"^\s*图像不[是为]表格[。！!？?]?\s*$",
        r"^\s*该图(?:片)?为表格[。！!？?]?\s*$",
        r"^\s*这张图(?:片)?为表格[。！!？?]?\s*$",
        r"^\s*先判断.*表格.*$\s*",
    ]
    kept_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept_lines.append(line)
            continue
        if any(re.match(pat, stripped, flags=re.IGNORECASE) for pat in banned_patterns):
            continue
        kept_lines.append(line)
    cleaned = "\n".join(kept_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def normalize_image_analysis_content(content: str, contains_table: bool) -> str:
    """
    规范图片解析输出。
    - contains_table=True: 保留原有表格表达；
    - contains_table=False: 统一转为纯文本结构化文案（不保留 Markdown 表格/代码块符号）。
    """
    text = _strip_table_judgement_lines(content)
    if not text:
        return ""
    if contains_table:
        return text

    lines = text.splitlines()
    out_lines: list[str] = []
    i = 0
    in_code_fence = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            i += 1
            continue

        if in_code_fence:
            plain = stripped
            if plain:
                out_lines.append(plain)
            i += 1
            continue

        if (
            "|" in stripped
            and i + 1 < len(lines)
            and _is_markdown_table_delimiter(lines[i + 1])
        ):
            header_cells = _split_md_row(stripped)
            i += 2
            while i < len(lines) and "|" in lines[i]:
                row_cells = _split_md_row(lines[i])
                pairs = []
                for idx, key in enumerate(header_cells):
                    if not key:
                        continue
                    val = row_cells[idx] if idx < len(row_cells) else ""
                    pairs.append(f"{key}：{val}")
                if pairs:
                    out_lines.append("；".join(pairs))
                i += 1
            continue

        plain = stripped
        plain = re.sub(r"^\s{0,3}#{1,6}\s*", "", plain)
        plain = re.sub(r"^\s*[-*+]\s+", "", plain)
        plain = plain.replace("**", "").replace("__", "")
        if plain:
            out_lines.append(plain)
        i += 1

    merged = "\n".join(out_lines)
    merged = re.sub(r"\n{3,}", "\n\n", merged).strip()
    return merged


def analyze_image_with_qwen_vl(
    image_path: str,
    api_key: str,
    model_name: str = "doubao-seed-1.8",
    base_url: str = "https://openapi-ait.ke.com",
    provider: str = "",
    ark_api_key: str = "",
    volc_ak: str = "",
    volc_sk: str = "",
    ark_project_name: str = "",
) -> Optional[str]:
    """
    使用 qwen-vl-plus 或 qwen3-vl-plus 分析图片内容
    返回分析结果文本
    """
    try:
        # Flatten transparent images to improve OCR readability
        prepared_path = prepare_image_for_ocr(image_path)
        prompt = "请完整识别这张图的可见信息并结构化输出，禁止编造；无法确认请写“识别不清”。输出中禁止出现“该图片不是表格/这是表格”等判定句。若图片本体不是表格，输出纯文本结构化文案（可分段，禁止使用 Markdown 表格、代码块、项目符号）；仅当图片本体确实是表格时，输出标准 Markdown 表格（包含表头和分隔行，列数一致，合并单元格按列补齐）。"

        img_b64 = image_to_base64(prepared_path)
        mime = _guess_mime_type(prepared_path)
        lower_provider = str(provider or "").lower()
        lower_model = str(model_name or "").lower()
        base_url_lower = str(base_url or "").lower()
        if lower_provider:
            use_ark = lower_provider == "ark"
        else:
            use_ark = "volces.com" in base_url_lower or "ark.cn" in base_url_lower
        if use_ark and "deepseek" not in lower_model:
            if ark_api_key:
                client = Ark(api_key=ark_api_key, base_url=base_url or "https://ark.cn-beijing.volces.com/api/v3")
            else:
                if not (volc_ak and volc_sk):
                    raise ValueError("ARK_API_KEY is required for Ark image chain, or provide VOLC_ACCESS_KEY_ID / VOLC_SECRET_ACCESS_KEY")
                client = Ark(ak=volc_ak, sk=volc_sk, base_url=base_url or "https://ark.cn-beijing.volces.com/api/v3")
            resp = client.chat.completions.create(
                model=model_name or "doubao-seed-1.8",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                        ],
                    }
                ],
                temperature=0,
                max_tokens=4000,
                timeout=120,
                extra_headers=({"X-Project-Name": ark_project_name} if ark_project_name else None),
            )
            content = resp.choices[0].message.content if resp.choices else ""
            analyze_image_with_qwen_vl.last_error = ""
            return (content or "").strip()

        payload_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                ],
            }
        ]
        base_candidates = []
        primary = str(base_url or "").strip().rstrip("/")
        fallback_root = "https://openapi-ait.ke.com"
        for root in [primary, fallback_root]:
            val = str(root or "").strip().rstrip("/")
            if not val:
                continue
            if val not in base_candidates:
                base_candidates.append(val)
            if not val.endswith("/v1"):
                v1 = f"{val}/v1"
                if v1 not in base_candidates:
                    base_candidates.append(v1)
            if not val.endswith("/api/v1"):
                apiv1 = f"{val}/api/v1"
                if apiv1 not in base_candidates:
                    base_candidates.append(apiv1)

        last_err = None
        for api_root in base_candidates:
            try:
                client = OpenAI(api_key=api_key, base_url=api_root)
                resp = client.chat.completions.create(
                    model=model_name or "deepseek-chat",
                    temperature=0,
                    messages=payload_messages,
                    max_tokens=4000,
                    timeout=120,
                )
                content = resp.choices[0].message.content if resp.choices else ""
                if content:
                    analyze_image_with_qwen_vl.last_error = ""
                    return str(content).strip()
                last_err = "empty_content"
            except Exception as inner_e:
                err = str(inner_e).strip()
                low = err.lower()
                if "429" in err or "rate" in low or "too many" in low:
                    last_err = f"RATE_LIMIT: 图片模型触发限流。{err}"
                elif "nodename nor servname" in low or "name or service not known" in low:
                    last_err = f"NETWORK_DNS: 域名解析/网络失败。{err}"
                elif "timeout" in low or "timed out" in low:
                    last_err = f"NETWORK_TIMEOUT: 图片模型请求超时。{err}"
                else:
                    last_err = f"OPENAI_COMPAT_ERROR: {err}"
                continue

        analyze_image_with_qwen_vl.last_error = str(last_err)
        print(f"  ⚠️ 图片模型返回失败（{model_name or 'unknown_model'}）: {last_err}")
        return None
    except Exception as e:
        analyze_image_with_qwen_vl.last_error = str(e)
        print(f"  ❌ 分析失败: {e}")
        return None

def cleanup_mermaid_duplicates(text: str) -> str:
    """Remove duplicated node lines and sanitize invalid Mermaid formula lines."""
    if "```mermaid" not in text:
        return text

    import re

    OP_LABELS = {"=", "×", "x", "X", "*", "÷", "/", "+", "-"}
    # Mermaid IDs can contain more than alnum/underscore in model outputs (e.g. A+/B+).
    # Keep parser permissive here, then rewrite invalid operator patterns deterministically.
    NODE_ID_RE = r"[A-Za-z0-9_\u4e00-\u9fa5\+\-\.]+"

    def _parse_node_token(token: str) -> tuple[str, str | None]:
        token = token.strip()
        m = re.match(rf"^({NODE_ID_RE})\[(.+?)\]$", token)
        if m:
            return m.group(1), m.group(2).strip()
        return token, None

    def _normalize_node_ids(block_text: str) -> str:
        """Normalize potentially invalid Mermaid node IDs (e.g. A+/B+) to safe IDs."""
        node_decl_pat = re.compile(rf"({NODE_ID_RE})\[(.+?)\]")

        used_ids: set[str] = set()
        id_map: dict[str, str] = {}

        def _safe_id(raw_id: str) -> str:
            cleaned = re.sub(r"[^A-Za-z0-9_\u4e00-\u9fa5]", "_", raw_id).strip("_")
            if not cleaned:
                cleaned = "N"
            if re.match(r"^\d", cleaned):
                cleaned = f"N_{cleaned}"
            candidate = cleaned
            idx = 2
            while candidate in used_ids and id_map.get(raw_id) != candidate:
                candidate = f"{cleaned}_{idx}"
                idx += 1
            used_ids.add(candidate)
            return candidate

        # 1) Collect all declared IDs and build mapping.
        for m in node_decl_pat.finditer(block_text):
            raw_id = m.group(1)
            if raw_id in id_map:
                continue
            id_map[raw_id] = _safe_id(raw_id)

        if not id_map:
            return block_text

        def _replace_outside_brackets(line: str) -> str:
            segments = re.split(r"(\[[^\]]*\])", line)
            for i, seg in enumerate(segments):
                if i % 2 == 1:
                    # Keep label text untouched.
                    continue
                for raw_id, safe_id in sorted(id_map.items(), key=lambda x: -len(x[0])):
                    if raw_id == safe_id:
                        continue
                    seg = re.sub(
                        rf"(?<![\w\u4e00-\u9fa5]){re.escape(raw_id)}(?![\w\u4e00-\u9fa5])",
                        safe_id,
                        seg,
                    )
                segments[i] = seg
            return "".join(segments)

        # 2) Replace declarations and standalone references (outside labels).
        out_lines: list[str] = []
        for line in block_text.splitlines():
            replaced = node_decl_pat.sub(lambda m: f"{id_map.get(m.group(1), m.group(1))}[{m.group(2)}]", line)
            replaced = _replace_outside_brackets(replaced)
            out_lines.append(replaced)
        return "\n".join(out_lines)

    def _rewire_operator_nodes(lines: list[str]) -> list[str]:
        edge_lines = []
        other_lines = []
        labels: dict[str, str] = {}
        edges: list[tuple[str, str]] = []
        edge_text: dict[tuple[str, str], str] = {}

        for line in lines:
            stripped = line.strip()
            if "-->" not in stripped:
                # capture standalone label declarations like A[xxx]
                m = re.match(rf"^\s*({NODE_ID_RE})\[(.+?)\]\s*$", stripped)
                if m:
                    labels[m.group(1)] = m.group(2).strip()
                other_lines.append(line)
                continue

            parts = stripped.split("-->")
            if len(parts) != 2:
                other_lines.append(line)
                continue
            l_id, l_label = _parse_node_token(parts[0].strip())
            r_id, r_label = _parse_node_token(parts[1].strip())
            if l_label:
                labels[l_id] = l_label
            if r_label:
                labels[r_id] = r_label
            edges.append((l_id, r_id))
            edge_text[(l_id, r_id)] = line
            edge_lines.append(line)

        if not edges:
            return lines

        def _is_op(node_id: str) -> bool:
            lbl = labels.get(node_id, "").strip()
            return lbl in OP_LABELS

        def _normalize_op(op: str) -> str:
            op = (op or "").strip()
            if op in {"x", "X", "*"}:
                return "×"
            if op == "/":
                return "÷"
            return op

        def _node_text(node_id: str) -> str:
            return str(labels.get(node_id, node_id) or "").strip()

        def _outgoing(node_id: str) -> list[str]:
            return [b for (a, b) in edges if a == node_id]

        def _incoming(node_id: str) -> list[str]:
            return [a for (a, b) in edges if b == node_id]

        def _build_equation_rhs(eq_id: str) -> str:
            terms: list[str] = []
            ops: list[str] = []
            cur = eq_id
            seen: set[str] = set()
            while cur and cur not in seen:
                seen.add(cur)
                outs = _outgoing(cur)
                if not outs:
                    break
                non_ops = [n for n in outs if not _is_op(n)]
                for n in non_ops:
                    txt = _node_text(n)
                    if txt:
                        terms.append(txt)
                op_child = next((n for n in outs if _is_op(n)), None)
                if not op_child:
                    break
                op_txt = _normalize_op(_node_text(op_child))
                if op_txt and op_txt != "=":
                    ops.append(op_txt)
                cur = op_child

            if not terms:
                return ""
            if not ops or len(terms) == 1:
                return " ".join(terms).strip()
            out = [terms[0]]
            for i in range(1, len(terms)):
                op = ops[i - 1] if i - 1 < len(ops) else ops[-1]
                out.extend([op, terms[i]])
            return " ".join(out).strip()

        eq_nodes = [n for n, lbl in labels.items() if lbl.strip() == "="]
        for eq in eq_nodes:
            rhs = _build_equation_rhs(eq)
            if not rhs:
                continue
            lhs_nodes = [n for n in _incoming(eq) if not _is_op(n)]
            for lhs in lhs_nodes:
                lhs_txt = _node_text(lhs)
                if not lhs_txt:
                    continue
                labels[lhs] = f"{lhs_txt} = {rhs}"

        changed = True
        while changed:
            changed = False
            current_ops = {n for n in labels.keys() if _is_op(n)}
            if not current_ops:
                break
            for op in list(current_ops):
                incoming = [(a, b) for (a, b) in edges if b == op]
                outgoing = [(a, b) for (a, b) in edges if a == op]
                if not incoming and not outgoing:
                    continue
                new_edges = []
                if incoming and outgoing:
                    for (p, _) in incoming:
                        for (_, c) in outgoing:
                            if p != c:
                                new_edges.append((p, c))
                # Remove all edges touching operator node
                edges = [(a, b) for (a, b) in edges if a != op and b != op]
                for e in new_edges:
                    if e not in edges:
                        edges.append(e)
                changed = True

        edges = [(a, b) for (a, b) in edges if not _is_op(a) and not _is_op(b)]

        # Rebuild edge lines with labels
        rebuilt = []
        for line in other_lines:
            # drop standalone operator node declarations
            stripped = line.strip()
            m = re.match(rf"^\s*({NODE_ID_RE})\[(.+?)\]\s*$", stripped)
            if m and m.group(2).strip() in OP_LABELS:
                continue
            rebuilt.append(line)

        edge_lines_out = []
        for a, b in edges:
            left = f"{a}[{labels[a]}]" if a in labels and labels[a] else a
            right = f"{b}[{labels[b]}]" if b in labels and labels[b] else b
            edge_lines_out.append(f"  {left} --> {right}")

        # Keep edges inside mermaid fence block (before the closing ```).
        close_idx = None
        for idx, line in enumerate(rebuilt):
            if line.strip() == "```":
                close_idx = idx
        if close_idx is None:
            rebuilt.extend(edge_lines_out)
        else:
            rebuilt = rebuilt[:close_idx] + edge_lines_out + rebuilt[close_idx:]
        return rebuilt

    def sanitize_block(block: str) -> str:
        lines = block.splitlines()
        out = []
        eq_idx = 1
        tmp_idx = 1

        def _tmp_id(prefix: str) -> str:
            nonlocal tmp_idx
            node_id = f"{prefix}_{tmp_idx}"
            tmp_idx += 1
            return node_id

        def _normalize_formula_line(raw_line: str) -> list[str] | None:
            s = raw_line.strip()
            if not s or "-->" in s or "<--" in s or "---" in s:
                return None
            m = re.match(rf"^({NODE_ID_RE}\[(.+?)\])\s*=\s*(.+)$", s)
            if not m:
                return None

            l_id, l_label = _parse_node_token(m.group(1))
            if not l_id:
                return None
            rhs = m.group(3).strip()
            segs = [x for x in re.split(r"\s*([×xX*÷/+\-])\s*", rhs) if str(x).strip()]
            term_tokens = [segs[i] for i in range(0, len(segs), 2)]
            op_tokens = [segs[i] for i in range(1, len(segs), 2)]
            if not term_tokens:
                return None

            term_nodes: list[tuple[str, str]] = []
            for idx, tok in enumerate(term_tokens):
                t_id, t_label = _parse_node_token(tok)
                if t_id and t_label is not None:
                    term_nodes.append((t_id, t_label))
                else:
                    term_nodes.append((_tmp_id(f"FTERM{idx+1}"), tok.strip()))

            eq_id = _tmp_id("OP_EQ")
            out_lines = [f"  {l_id}[{l_label or l_id}] --> {eq_id}[=]"]
            out_lines.append(f"  {eq_id}[=] --> {term_nodes[0][0]}[{term_nodes[0][1]}]")
            prev_op = eq_id
            for i, op in enumerate(op_tokens):
                op_id = _tmp_id(f"OP_{i+1}")
                prev_op_label = "=" if i == 0 else op_tokens[i - 1]
                out_lines.append(f"  {prev_op}[{prev_op_label}] --> {op_id}[{op}]")
                if i + 1 < len(term_nodes):
                    t_id, t_lbl = term_nodes[i + 1]
                    out_lines.append(f"  {op_id}[{op}] --> {t_id}[{t_lbl}]")
                prev_op = op_id
            return out_lines

        for line in lines:
            raw = line.strip()
            if not raw or raw.startswith("```"):
                out.append(line)
                continue
            # Normalize edge label form to the most parser-stable syntax.
            # Example: A -- × --> B  =>  A -->|×| B
            line = re.sub(
                r"--\s*([^>-][^>]*)\s*-->",
                lambda m: f" -->|{m.group(1).strip()}| ",
                line,
            )
            normalized_formula = _normalize_formula_line(line)
            if normalized_formula:
                out.extend(normalized_formula)
                continue
            # Mermaid does not allow '=' or '×' as standalone operators in lines.
            if ("=" in raw or "×" in raw) and ("-->" not in raw and "<--" not in raw and "---" not in raw):
                parts = re.findall(r"\[(.+?)\]", raw)
                if parts:
                    if "×" in raw and len(parts) >= 3:
                        formula = f"{parts[0]} = {parts[1]} × {parts[2]}"
                    elif len(parts) >= 2:
                        formula = f"{parts[0]} = {parts[1]}"
                    else:
                        formula = parts[0]
                else:
                    formula = raw
                out.append(f"  EQ{eq_idx}[{formula}]")
                eq_idx += 1
                continue
            out.append(line)
        out = _rewire_operator_nodes(out)
        return _normalize_node_ids("\n".join(out))

    mermaid_matches = list(re.finditer(r"```mermaid[\s\S]*?```", text))
    if not mermaid_matches:
        return text

    rebuilt = text
    for mermaid_match in reversed(mermaid_matches):
        mermaid_block = mermaid_match.group(0)
        sanitized_block = sanitize_block(mermaid_block)
        rebuilt = rebuilt[:mermaid_match.start()] + sanitized_block + rebuilt[mermaid_match.end():]
    return rebuilt

def prepare_image_for_ocr(image_path: str) -> str:
    """Flatten images with alpha to white background for OCR."""
    try:
        from PIL import Image
    except Exception:
        return image_path

    try:
        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                base = Image.new("RGB", img.size, (255, 255, 255))
                base.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                fd, tmp_path = tempfile.mkstemp(prefix="ocr_flat_", suffix=".png")
                os.close(fd)
                base.save(tmp_path, format="PNG")
                return tmp_path
    except Exception:
        return image_path

    return image_path

def extract_table_from_content(content: str) -> tuple[str, str]:
    """
    从分析结果中提取表格（Markdown 格式）和逻辑描述
    返回: (逻辑描述, 表格内容)
    """
    # 查找 Markdown 表格（以 | 开头）
    lines = content.split('\n')
    table_start = -1
    table_end = -1
    
    for i, line in enumerate(lines):
        if '|' in line and '---' not in line and table_start == -1:
            # 可能是表头
            if i + 1 < len(lines) and '---' in lines[i + 1]:
                table_start = i
        elif table_start >= 0 and '|' in line:
            table_end = i
        elif table_start >= 0 and '|' not in line and line.strip():
            # 表格结束
            if table_end == -1:
                table_end = i - 1
            break
    
    if table_start >= 0 and table_end >= table_start:
        table_content = '\n'.join(lines[table_start:table_end + 1])
        logic_desc = '\n'.join(lines[:table_start]) + '\n' + '\n'.join(lines[table_end + 1:])
        return logic_desc.strip(), table_content.strip()
    else:
        # 没有表格，全部作为逻辑描述
        return content.strip(), ""

def process_image(
    image_path: str,
    api_key: str,
    output_file: str,
    model_name: str,
    base_url: str,
    provider: str,
    ark_api_key: str,
    volc_ak: str,
    volc_sk: str,
    ark_project_name: str,
):
    """处理单张图片"""
    print(f"处理: {os.path.basename(image_path)}...", end='', flush=True)
    
    # 分析图片
    analysis_result = analyze_image_with_qwen_vl(
        image_path,
        api_key,
        model_name=model_name,
        base_url=base_url,
        provider=provider,
        ark_api_key=ark_api_key,
        volc_ak=volc_ak,
        volc_sk=volc_sk,
        ark_project_name=ark_project_name,
    )
    
    if not analysis_result:
        print(" ✗ (分析失败)")
        return None
    
    # 提取表格和逻辑描述
    logic_desc, table_content = extract_table_from_content(analysis_result)
    has_table = bool(table_content)
    logic_desc = normalize_image_analysis_content(logic_desc, contains_table=has_table)
    
    # 构建 content（逻辑描述 + 表格）
    content_parts = []
    if logic_desc:
        content_parts.append(logic_desc)
    if table_content:
        content_parts.append("\n\n" + table_content)
    content = '\n'.join(content_parts)
    
    # 构建 metadata
    has_chart = any(kw in analysis_result.lower() for kw in ['坐标', '曲线', '趋势', '图表', '图', 'axis', 'chart'])
    metadata = {
        "标注": "含有图表说明" if (has_chart or has_table) else "图片内容",
        "包含图表": has_chart,
        "包含表格": has_table,
        "图片路径": os.path.basename(image_path)
    }
    
    # 构建 JSONL 条目
    result = {
        "content": content,
        "metadata": metadata
    }
    
    # 写入 JSONL
    with open(output_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(result, ensure_ascii=False) + '\n')
    
    print(" ✓")
    return result

def main():
    import sys
    
    print("="*70)
    print("教材图片批量处理（qwen3-vl-plus 多模态分析）")
    print("="*70)
    print()
    
    # 加载配置
    config = load_config()
    provider = (config.get("IMAGE_PROVIDER") or "").lower()
    if provider == "ark":
        api_key = (
            config.get("IMAGE_API_KEY")
            or config.get("ARK_API_KEY")
            or config.get("OPENAI_API_KEY")
            or ""
        )
    else:
        api_key = (
            config.get("AIT_API_KEY")
            or config.get("IMAGE_API_KEY")
            or config.get("OPENAI_API_KEY")
            or config.get("CRITIC_API_KEY")
            or ""
        )
    model_name = config.get("IMAGE_MODEL") or "doubao-seed-1.8"
    if provider == "ark":
        base_url = config.get("IMAGE_BASE_URL") or config.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"
    else:
        base_url = (
            config.get("IMAGE_BASE_URL")
            or config.get("AIT_BASE_URL")
            or config.get("OPENAI_BASE_URL")
            or "https://openapi-ait.ke.com/v1"
        )
    ark_api_key = config.get("ARK_API_KEY") or ""
    volc_ak = config.get("VOLC_ACCESS_KEY_ID") or ""
    volc_sk = config.get("VOLC_SECRET_ACCESS_KEY") or ""
    ark_project_name = config.get("ARK_PROJECT_NAME") or ""
    
    if provider == "ark":
        if not (ark_api_key or (volc_ak and volc_sk)):
            print("❌ Ark 图片链路缺少认证信息，请配置 ARK_API_KEY（推荐）或 VOLC_ACCESS_KEY_ID/VOLC_SECRET_ACCESS_KEY")
            return
    elif not api_key:
        print("❌ 未找到 API Key，请在 填写您的Key.txt 中配置 AIT_API_KEY（推荐）或 OPENAI_API_KEY")
        return
    
    # 获取图片目录
    if len(sys.argv) > 1:
        image_dir = sys.argv[1]
    else:
        image_dir = input("请输入图片目录路径（或按回车使用当前目录）: ").strip()
        if not image_dir:
            image_dir = '.'
    
    if not os.path.isdir(image_dir):
        print(f"❌ 目录不存在: {image_dir}")
        return
    
    # 查找图片文件
    image_extensions = ['*.png', '*.jpg', '*.jpeg', '*.gif', '*.bmp', '*.webp']
    image_files = []
    for ext in image_extensions:
        image_files.extend(glob.glob(os.path.join(image_dir, ext)))
        image_files.extend(glob.glob(os.path.join(image_dir, ext.upper())))
    
    if not image_files:
        print(f"❌ 在 {image_dir} 中未找到图片文件")
        return
    
    print(f"找到 {len(image_files)} 张图片")
    print()
    
    # 输出文件
    output_file = os.path.join(image_dir, 'textbook_images_analysis.jsonl')
    if os.path.isfile(output_file):
        backup = output_file + '.bak'
        import shutil
        shutil.copy(output_file, backup)
        print(f"已备份原文件 -> {backup}")
        # 清空文件
        with open(output_file, 'w', encoding='utf-8') as f:
            pass
    
    print(f"输出文件: {output_file}")
    print()
    
    # 处理每张图片
    success_count = 0
    for i, image_path in enumerate(sorted(image_files), 1):
        print(f"[{i}/{len(image_files)}] ", end='', flush=True)
        result = process_image(
            image_path,
            api_key,
            output_file,
            model_name=model_name,
            base_url=base_url,
            provider=provider,
            ark_api_key=ark_api_key,
            volc_ak=volc_ak,
            volc_sk=volc_sk,
            ark_project_name=ark_project_name,
        )
        if result:
            success_count += 1
    
    print()
    print("="*70)
    print(f"处理完成: {success_count}/{len(image_files)} 张图片成功")
    print(f"结果已保存至: {output_file}")
    print("="*70)

if __name__ == '__main__':
    main()
