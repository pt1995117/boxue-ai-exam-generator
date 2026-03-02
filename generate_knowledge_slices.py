#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate Knowledge Slices from Word Document
Strictly following 'Final Complete Slice Style Specifications'
"""
import os
import json
import re
import sys
import argparse
import zipfile
import shutil
from typing import List, Dict, Optional, Tuple
from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.text.paragraph import Paragraph
from tenants_config import tenant_slices_dir

# Import helper for image analysis if available
try:
    from process_textbook_images import analyze_image_with_qwen_vl, extract_table_from_content
except ImportError:
    analyze_image_with_qwen_vl = None
    extract_table_from_content = None

# Import exam_graph for splitting
try:
    from exam_graph import generate_content
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    try:
        from exam_graph import generate_content
    except ImportError:
        generate_content = None

def load_config():
    config = {}
    cfg_path = os.path.join(os.path.dirname(__file__) or '.', '填写您的Key.txt')
    if os.path.isfile(cfg_path):
        with open(cfg_path, 'r', encoding='utf-8') as f:
            for line in f:
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.split('=', 1)
                    config[k.strip()] = v.strip()
    return config

# --- Image Handling ---

def extract_images_from_docx(docx_path: str, output_dir: str) -> Dict[str, str]:
    """
    Extract all images from docx to output_dir.
    Returns a map: { 'image_part_name': 'local_file_path' }
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    image_map = {}
    with zipfile.ZipFile(docx_path, 'r') as z:
        for name in z.namelist():
            if name.startswith('word/media/') and not name.endswith('/'):
                filename = os.path.basename(name)
                if not filename: continue
                target_path = os.path.join(output_dir, filename)
                with z.open(name) as source, open(target_path, 'wb') as dest:
                    dest.write(source.read())
                image_map[filename] = target_path
    return image_map

def get_image_rels(doc_part):
    """
    Get relationship map for a document part (e.g. document.part)
    Returns: { rId: target_part }
    """
    return doc_part.rels

def find_images_in_paragraph(paragraph, rels) -> List[Dict]:
    """
    Find images in a paragraph by looking for blip/drawing elements and mapping rId.
    Returns list of dicts: { 'rId': ..., 'filename': ... }
    """
    images = []
    # This is a simplified search. It works for many standard docx images.
    # Namespace for 'r' is usually http://schemas.openxmlformats.org/officeDocument/2006/relationships
    # Look for <a:blip r:embed="rIdX">
    
    # We iterate over the xml to find all blip elements
    blips = paragraph._element.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip')
    
    # Also check VML for older formats
    vml_imagedata = paragraph._element.findall('.//{urn:schemas-microsoft-com:vml}imagedata')
    
    all_imgs = blips + vml_imagedata
    
    for img_xml in all_imgs:
        embed_attr = img_xml.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
        if not embed_attr:
            embed_attr = img_xml.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        
        if embed_attr and embed_attr in rels:
            rel = rels[embed_attr]
            # Verify it's an image
            if "image" in rel.target_ref:
                # The target_ref is usually like "media/image1.png" relative to the document part
                filename = os.path.basename(rel.target_ref)
                images.append({
                    'rId': embed_attr,
                    'filename': filename
                })
    return images

# --- Structure Extraction ---

def is_heading(paragraph) -> Tuple[bool, int]:
    """Determine if paragraph is a heading and return level."""
    style_name = paragraph.style.name.lower() if paragraph.style else ""
    if 'heading' in style_name or '标题' in style_name:
        match = re.search(r'heading\s*(\d+)|标题\s*(\d+)', style_name, re.IGNORECASE)
        if match:
            return True, int(match.group(1) or match.group(2))
    
    # if hasattr(paragraph, 'paragraph_format') and paragraph.paragraph_format.level is not None:
    #      return True, paragraph.paragraph_format.level
         
    text = paragraph.text.strip()
    
    # 长度和标点校验：避免将长列表项误判为标题
    # 1. 标点符号结尾的通常是列表项，但短标题允许保留
    if text.endswith(('；', '。', '，', ';', '.', ',')):
        if len(text) > 15:
            return False, 0
        
    # 2. Level 5 标题通常较短，放宽长度阈值避免误判
    # 特别针对 （1） 这种格式
    if re.match(r'^（\d+）|^\d+[、.]', text):
        if len(text) > 30:
             return False, 0
    
    # 3. 如果文本包含冒号后跟描述，通常是正文（但短标题允许）
    if '：' in text or ':' in text:
        parts = re.split(r'[:：]', text, 1)
        if len(parts) > 1 and len(parts[1]) > 15: # 冒号后内容较长即视为正文
            return False, 0

    if re.match(r'^第[一二三四五六七八九十\d]+篇', text): return True, 1
    if re.match(r'^第[一二三四五六七八九十\d]+章', text): return True, 2
    if re.match(r'^第[一二三四五六七八九十\d]+节', text): return True, 3
    # 中文大序号（如 一、二、三、）与中文小序号（如 （一）（二））拆成不同层级，
    # 避免（一）覆盖掉上一级“三、...”路径。
    if re.match(r'^[一二三四五六七八九十]+[、.]', text): return True, 4
    if re.match(r'^（[一二三四五六七八九十]+）', text): return True, 5
    if re.match(r'^（\d+）|^\d+[、.]', text): return True, 6
    
    return False, 0

def process_document(
    docx_path: str,
    api_key: str,
    extract_dir: str = "extracted_images",
    image_model: str = "doubao-seed-1.8",
    image_base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
    image_provider: str = "",
    ark_api_key: str = "",
    volc_ak: str = "",
    volc_sk: str = "",
    ark_project_name: str = "",
):
    print(f"Reading {docx_path}...")
    doc = Document(docx_path)
    
    # 1. Extract all images first to a temp dir
    extracted_image_map = extract_images_from_docx(docx_path, extract_dir) # filename -> path
    print(f"Extracted {len(extracted_image_map)} images to {extract_dir}")

    # Prepare logic for processing
    elements = []
    # path_stack 使用 1-based 层级索引，预留到 6 级标题
    path_stack = [None] * 7
    last_mastery = ""
    
    # We iterate over document body elements
    # doc.element.body gives us the order needed
    
    rels = doc.part.rels # Relationship map for the main document part
    
    table_index_global = 0
    image_index_global = 0
    
    for element in doc.element.body:
        if isinstance(element, CT_P):
            para = Paragraph(element, doc)
            text = para.text.strip()
            
            # Check for images in this paragraph
            # We want to attach images to the content flow
            para_images = find_images_in_paragraph(para, rels)
            
            processed_images = []
            if para_images:
                for img_info in para_images:
                    fname = img_info['filename']
                    fpath = extracted_image_map.get(fname)
                    if fpath:
                        image_index_global += 1
                        # Perform analysis if API key is present and analyzer is available
                        analysis = ""
                        contains_table = False
                        contains_chart = False
                        if api_key and analyze_image_with_qwen_vl:
                            analysis_result = analyze_image_with_qwen_vl(
                                fpath,
                                api_key,
                                model_name=image_model,
                                base_url=image_base_url,
                                provider=image_provider,
                                ark_api_key=ark_api_key,
                                volc_ak=volc_ak,
                                volc_sk=volc_sk,
                                ark_project_name=ark_project_name,
                            )
                            if analysis_result:
                                analysis = analysis_result.strip()
                                if extract_table_from_content:
                                    _, table_content = extract_table_from_content(analysis_result)
                                    contains_table = bool(table_content)
                                contains_chart = any(
                                    kw in analysis_result.lower()
                                    for kw in ['坐标', '曲线', '趋势', '图表', '图', 'axis', 'chart']
                                )
                            else:
                                detail = ""
                                if analyze_image_with_qwen_vl:
                                    detail = str(getattr(analyze_image_with_qwen_vl, "last_error", "") or "")
                                detail = detail[:300].strip()
                                analysis = (
                                    f"(分析失败：{detail})"
                                    if detail
                                    else "(分析失败：图片模型调用失败，请检查 ARK_API_KEY 或 IMAGE_API_KEY / IMAGE_BASE_URL 配置)"
                                )
                        elif not api_key:
                            analysis = "(分析失败：未配置图片模型 KEY，请在填写您的Key.txt设置 IMAGE_API_KEY 或 OPENAI_API_KEY)"
                        else:
                            analysis = "(分析失败：图片分析模块未加载)"

                        img_obj = {
                           "image_id": fname,
                           "image_path": fpath,
                           "analysis": analysis,
                           "contains_table": contains_table,
                           "contains_chart": contains_chart
                        }
                        processed_images.append(img_obj)
            
            # Heading Login
            is_head, level = is_heading(para)
            if is_head and level > 0:
                # Extract mastery
                m = re.search(r'[（(](掌握|熟悉|了解)[)）]', text)
                if m:
                    last_mastery = m.group(1)
                    text = re.sub(r'[（(](掌握|熟悉|了解)[)）]', '', text).strip()
                text = _clean_path_seg(text)
                if not text:
                    # Skip empty headings to prevent blank path levels.
                    continue

                path_stack[level] = text
                for i in range(level + 1, 7): path_stack[i] = None
                
                elements.append({
                    "type": "heading",
                    "level": level,
                    "text": text,
                    "path": _clean_joined_path(" > ".join([p for p in path_stack[1:level+1] if p])),
                    "mastery": last_mastery
                })
            else:
                if text or processed_images:
                    current_path = _clean_joined_path(" > ".join([p for p in path_stack[1:] if p]))
                    elements.append({
                        "type": "paragraph",
                        "text": text,
                        "path": current_path,
                        "mastery": last_mastery,
                        "images": processed_images
                    })

        elif isinstance(element, CT_Tbl):
            # Find the index of this table in the doc.tables list
            # But iterating body elements sequentially matches doc.tables sequentially usually?
            # Actually, `element` IS the table element.
            # We can wrap it in a Table object if needed, or parse XML directly.
            # To be safe, we parse XML or assume sequentiality.
            # doc.tables is a list of all tables.
            # We can try to match by identity? No, `doc.tables` creates new proxy objects.
            # Let's iterate `doc.tables` and pop? No.
            
            # Simple Markdown extraction
            import docx.table
            table = docx.table.Table(element, doc)
            
            rows_md = []
            try:
                # Basic MD Table
                r0_cells = [c.text.strip().replace('\n', ' ') for c in table.rows[0].cells]
                rows_md.append(f"| {' | '.join(r0_cells)} |")
                rows_md.append(f"| {' | '.join(['---']*len(r0_cells))} |")
                for row in table.rows[1:]:
                    cells = [c.text.strip().replace('\n', ' ') for c in row.cells]
                    rows_md.append(f"| {' | '.join(cells)} |")
            except:
                pass
            
            table_md = "\n".join(rows_md)
            current_path = _clean_joined_path(" > ".join([p for p in path_stack[1:] if p]))
            table_index_global += 1
            
            elements.append({
                "type": "table",
                "text": table_md,
                "path": current_path,
                "mastery": last_mastery,
                "table_index": table_index_global
            })

    return elements

def group_and_slice(elements: List[Dict], api_key: str):
    # Grouping logic (simplified)
    # Collect all P and Table under the last Heading
    
    slices = []
    current_slice = None
    
    EXCLUDE_KEYWORDS = ["前言", "目录", "相关说明"]
    
    for el in elements:
        if el["type"] == "heading" and el["level"] >= 1: # Group by smallest heading
            # Start new slice
            if current_slice:
                slices.append(current_slice)
            
            current_slice = {
                "完整路径": el["path"],
                "掌握程度": el["mastery"],
                "结构化内容": {
                    "key_params": [],
                    "rules": [],
                    "context_before": "",
                    "tables": [],
                    "context_after": "",
                    "images": [],
                    "formulas": [],
                    "examples": [] # New field for examples
                },
                "metadata": {
                    "类型": "自动组装"
                },
                "_lines_before": [], 
                "_lines_after": [],
                "_hit_visual": False,
                "_in_example": False # Track if we are processing an example block
            }
        
        elif current_slice: # Content
            # Skip if path match excluded
            if any(k in el["path"] for k in EXCLUDE_KEYWORDS):
                continue
            
            if el["type"] == "paragraph":
                text_content = el["text"]
                
                # Check for Example start
                if text_content.startswith("【例】"):
                    current_slice["_in_example"] = True
                    current_slice["结构化内容"]["examples"].append(text_content)
                elif current_slice["_in_example"]:
                    # If we are in an example block, heuristics to decide if we are still in it
                    # Usually example includes question + options + solution (【解】)
                    # For now, simplistic: if it starts with 【解】 or options A/B/C/D or is short/continuation, keep in example
                    # If it looks like a new Heading (handled by outer loop), we exit naturally.
                    current_slice["结构化内容"]["examples"][-1] += "\n" + text_content
                else:
                    # Normal text processing
                    # Check for images FIRST, as they might be inline
                    has_images = False
                    if el.get("images"):
                        current_slice["结构化内容"]["images"].extend(el["images"])
                        current_slice["_hit_visual"] = True
                        has_images = True
                    
                    if text_content:
                        # Check for formulas in text
                        if "=" in text_content and any(x in text_content for x in "+-*/"):
                             current_slice["结构化内容"]["formulas"].append(text_content)
                        
                        # Distribute text
                        if current_slice["_hit_visual"]:
                            current_slice["_lines_after"].append(text_content)
                        else:
                            current_slice["_lines_before"].append(text_content)
            
            elif el["type"] == "table":
                # If we encounter a table, does it belong to the example?
                # If _in_example is True, maybe?
                # For safety, let's reset example mode on visuals for now, or keep separate.
                current_slice["_in_example"] = False 
                current_slice["结构化内容"]["tables"].append(el["text"])
                current_slice["_hit_visual"] = True
    
    if current_slice:
        slices.append(current_slice)
        
    # Post-process: Fill context fields
    final_slices = []
    for s in slices:
        s["结构化内容"]["context_before"] = "\n".join(s["_lines_before"])
        s["结构化内容"]["context_after"] = "\n".join(s["_lines_after"])
        # Cleanup temp fields
        del s["_lines_before"]
        del s["_lines_after"]
        del s["_hit_visual"]
        
        # Metadata updates
        s["metadata"]["表格索引"] = len(s["结构化内容"]["tables"])
        s["metadata"]["图片索引"] = len(s["结构化内容"]["images"])
        s["metadata"]["包含计算公式"] = len(s["结构化内容"]["formulas"]) > 0
        
        final_slices.append(s)
        
    return final_slices


_PATH_L4_RE = re.compile(r"^(?:[一二三四五六七八九十百千万]+[、.]|\d+[、.])")
_PATH_CHILD_RE = re.compile(r"^(?:（[一二三四五六七八九十百千万]+）|（\d+）)")
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")


def _clean_path_seg(seg: str) -> str:
    s = str(seg or "")
    s = _INVISIBLE_RE.sub("", s)
    return s.strip()


def _clean_joined_path(path: str) -> str:
    segs = [_clean_path_seg(x) for x in str(path or "").split(" > ")]
    segs = [x for x in segs if x]
    return " > ".join(segs)


def repair_flattened_paths(slices: List[Dict]) -> List[Dict]:
    """
    Repair flattened paths like:
    ... > （一）周期影响因素
    to:
    ... > 三、房地产市场周期波动 > （一）周期影响因素

    This runs as a source-side safeguard before writing JSONL.
    """
    latest_l4_by_p3: Dict[str, str] = {}
    out: List[Dict] = []
    for s in slices:
        if not isinstance(s, dict):
            out.append(s)
            continue
        path = _clean_joined_path(str(s.get("完整路径", "") or ""))
        segs = [_clean_path_seg(x) for x in path.split(" > ") if _clean_path_seg(x)]
        if len(segs) >= 4:
            p3 = " > ".join(segs[:3])
            seg4 = segs[3]
            if _PATH_L4_RE.match(seg4):
                latest_l4_by_p3[p3] = seg4
            elif _PATH_CHILD_RE.match(seg4):
                parent = latest_l4_by_p3.get(p3)
                if parent:
                    segs = [*segs[:3], parent, *segs[3:]]
        fixed_path = _clean_joined_path(" > ".join(segs) if segs else path)
        if fixed_path != path:
            patched = dict(s)
            patched["完整路径"] = fixed_path
            out.append(patched)
        else:
            out.append(s)
    return out

def _parse_formula_table(table_text: str) -> List[str]:
    """Parse a Markdown table for formulas (编号/公式). Returns list of formula strings."""
    formulas = []
    lines = [ln.strip() for ln in (table_text or "").splitlines() if ln.strip()]
    if not lines:
        return formulas
    # Expect header row containing 编号 and 公式
    header = lines[0]
    if "编号" not in header or "公式" not in header:
        return formulas
    # Skip header + separator
    for ln in lines[2:]:
        if "|" not in ln:
            continue
        parts = [p.strip() for p in ln.strip("|").split("|")]
        if len(parts) < 2:
            continue
        formula = parts[1]
        if formula:
            formulas.append(formula)
    return formulas

def _assign_formulas_to_slices(final_slices: List[Dict], appendix_slice: Dict, formulas: List[str]) -> None:
    """Assign formulas to best-matching slices based on left-side keyword."""
    if not formulas or not final_slices:
        return
    # Build searchable content for each slice
    slice_index = []
    for s in final_slices:
        content = s.get("结构化内容", {})
        text = " ".join([
            s.get("完整路径", ""),
            content.get("context_before", ""),
            content.get("context_after", ""),
            "\n".join(content.get("tables", []) or []),
            "\n".join(content.get("examples", []) or []),
        ])
        slice_index.append((s, text))

    appendix_text = ""
    if appendix_slice:
        appendix_text = " ".join([
            appendix_slice.get("完整路径", ""),
            appendix_slice.get("结构化内容", {}).get("context_before", ""),
            appendix_slice.get("结构化内容", {}).get("context_after", ""),
        ])

    for fml in formulas:
        left = fml.split("=", 1)[0].strip()
        if not left:
            continue
        best = None
        best_score = 0
        for s, text in slice_index:
            score = 0
            if left in text:
                score += 2
            if "附录" in text:
                score += 1
            if score > best_score:
                best_score = score
                best = s
        target = best if best_score > 0 else appendix_slice
        if not target:
            continue
        target.setdefault("结构化内容", {}).setdefault("formulas", [])
        if fml not in target["结构化内容"]["formulas"]:
            target["结构化内容"]["formulas"].append(fml)

def main():
    parser = argparse.ArgumentParser(description="Generate knowledge slices from textbook docx")
    parser.add_argument("--tenant-id", default="", help="城市租户ID，例如 hz/bj/sh")
    parser.add_argument("--docx", default="第26届存量房教材模板-勘误版0912-干净版.docx")
    parser.add_argument("--output", default="")
    parser.add_argument("--extract-dir", default="extracted_images", help="图片提取目录")
    args = parser.parse_args()

    config = load_config()
    # 图片分析走 AIT OpenAI 兼容接口
    # 优先级：显式图片配置 > AIT配置 > CRITIC配置 > OPENAI配置
    api_key = (
        config.get('IMAGE_API_KEY')
        or config.get('AIT_API_KEY')
        or config.get('CRITIC_API_KEY')
        or config.get('OPENAI_API_KEY')
    )
    image_model = config.get("IMAGE_MODEL") or "doubao-seed-1.8"
    image_provider = (config.get("IMAGE_PROVIDER") or "").lower()
    image_base_url = (
        config.get("IMAGE_BASE_URL")
        or config.get("ARK_BASE_URL")
        or config.get("AIT_BASE_URL")
        or config.get("CRITIC_BASE_URL")
        or config.get("OPENAI_BASE_URL")
        or "https://ark.cn-beijing.volces.com/api/v3"
    )
    ark_api_key = config.get("ARK_API_KEY") or ""
    volc_ak = config.get("VOLC_ACCESS_KEY_ID") or ""
    volc_sk = config.get("VOLC_SECRET_ACCESS_KEY") or ""
    ark_project_name = config.get("ARK_PROJECT_NAME") or ""
    
    docx_path = args.docx
    if args.output:
        output_file = args.output
    elif args.tenant_id:
        output_file = str(tenant_slices_dir(args.tenant_id) / "knowledge_slices.jsonl")
    else:
        output_file = "test_knowledge_slices.jsonl"
    
    print("🚀 Starting Logic Extraction...")
    elements = process_document(
        docx_path,
        api_key,
        args.extract_dir,
        image_model=image_model,
        image_base_url=image_base_url,
        image_provider=image_provider,
        ark_api_key=ark_api_key,
        volc_ak=volc_ak,
        volc_sk=volc_sk,
        ark_project_name=ark_project_name,
    )
    print(f"Stats: {len(elements)} elements found.")
    
    slices = group_and_slice(elements, api_key)

    # Appendix formula table split + formula assignment
    appendix_slice = None
    appendix_formulas = []
    appendix_table = None
    source_slice = None
    for s in slices:
        content = s.get("结构化内容", {})
        tables = content.get("tables", []) or []
        for t in tables:
            if "计算公式汇总表" in t and "编号" in t and "公式" in t:
                appendix_formulas = _parse_formula_table(t)
                appendix_table = t
                source_slice = s
                break
        if appendix_table:
            break

    if appendix_table:
        # Remove table from source slice
        source_tables = source_slice.get("结构化内容", {}).get("tables", [])
        source_slice["结构化内容"]["tables"] = [t for t in source_tables if t != appendix_table]
        # Remove appendix marker line from context_after if present
        ca = source_slice.get("结构化内容", {}).get("context_after", "")
        if "附录" in ca and "计算公式汇总表" in ca:
            source_slice["结构化内容"]["context_after"] = ca.replace("附录  计算公式汇总表", "").replace("附录 计算公式汇总表", "").strip()

        appendix_slice = {
            "完整路径": "附录  计算公式汇总表",
            "掌握程度": "了解",
            "结构化内容": {
                "key_params": [],
                "rules": [],
                "context_before": "",
                "tables": [appendix_table],
                "context_after": "",
                "images": [],
                "formulas": [],
                "examples": []
            },
            "metadata": {
                "类型": "自动组装"
            },
            "_in_example": False
        }
        slices.append(appendix_slice)
        _assign_formulas_to_slices(slices, appendix_slice, appendix_formulas)

    # Source-side guard: do not output flattened child headings.
    slices = repair_flattened_paths(slices)
    
    # Filter out empty slices (often TOC or empty headers)
    valid_slices = []
    for s in slices:
        content = s["结构化内容"]
        has_content = (
            content["context_before"].strip() or 
            content["context_after"].strip() or 
            content["tables"] or 
            content["images"] or 
            content["formulas"] or
            content["examples"]
        )
        if has_content and not is_toc_slice(s):
            valid_slices.append(s)
            
    print(f"Generated {len(valid_slices)} slices (filtered {len(slices) - len(valid_slices)} empty/TOC slices). Saving to {output_file}...")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        for s in valid_slices:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
            
    print("✅ Done.")

def is_toc_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if "目录" in stripped or "目 录" in stripped:
        return True
    if "\t" in line and re.search(r"\d+\s*$", stripped):
        return True
    if re.search(r"(·{2,}|\.{2,}|…{2,})", stripped) and re.search(r"\d+\s*$", stripped):
        return True
    if re.search(r"第.+(章|节|篇|部分|单元).*\d+\s*$", stripped) and len(stripped) <= 40:
        return True
    return False

def is_toc_slice(slice_obj: dict) -> bool:
    content = slice_obj.get("结构化内容", {})
    if content.get("tables") or content.get("images") or content.get("formulas"):
        return False
    path = slice_obj.get("完整路径", "") or ""
    if "目录" in path or "目 录" in path:
        return True
    text = "\n".join([
        content.get("context_before", ""),
        content.get("context_after", ""),
    ])
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    if any("目录" in ln or "目 录" in ln for ln in lines):
        return True
    return all(is_toc_line(ln) for ln in lines)

if __name__ == '__main__':
    main()
