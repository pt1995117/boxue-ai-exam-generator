#!/usr/bin/env python3
"""
基于当前管理后台代码生成业务流程图（JPG）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFont


CANVAS_W = 3600
CANVAS_H = 2100
BG = (248, 250, 252)


def pick_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """选择可用中文字体，保证流程图中文可读。"""
    candidates = []
    if bold:
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    for p in candidates:
        if Path(p).exists():
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


FONT_TITLE = pick_font(62, bold=True)
FONT_GROUP = pick_font(44, bold=True)
FONT_BOX_TITLE = pick_font(30, bold=True)
FONT_BOX_BODY = pick_font(24, bold=False)
FONT_NOTE = pick_font(22, bold=False)


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    """按像素宽度自动换行，避免中文文本溢出。"""
    out: List[str] = []
    for para in str(text).split("\n"):
        line = ""
        for ch in para:
            test = line + ch
            width = draw.textbbox((0, 0), test, font=font)[2]
            if width <= max_width or not line:
                line = test
            else:
                out.append(line)
                line = ch
        out.append(line if line else "")
    return out


def draw_round_box(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int, int, int],
    fill: Tuple[int, int, int],
    outline: Tuple[int, int, int] = (140, 156, 178),
    radius: int = 18,
    width: int = 3,
) -> None:
    """绘制圆角矩形。"""
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def draw_text_center(
    draw: ImageDraw.ImageDraw,
    box: Tuple[int, int, int, int],
    title: str,
    body: str = "",
) -> None:
    """在卡片内绘制标题和正文，自动换行。"""
    x1, y1, x2, y2 = box
    pad = 16
    content_w = x2 - x1 - pad * 2
    y = y1 + pad

    title_lines = wrap_text(draw, title, FONT_BOX_TITLE, content_w)
    for line in title_lines:
        tw = draw.textbbox((0, 0), line, font=FONT_BOX_TITLE)[2]
        draw.text((x1 + (x2 - x1 - tw) / 2, y), line, fill=(24, 39, 57), font=FONT_BOX_TITLE)
        y += 40

    if body:
        y += 4
        body_lines = wrap_text(draw, body, FONT_BOX_BODY, content_w)
        for line in body_lines:
            tw = draw.textbbox((0, 0), line, font=FONT_BOX_BODY)[2]
            draw.text((x1 + (x2 - x1 - tw) / 2, y), line, fill=(59, 79, 107), font=FONT_BOX_BODY)
            y += 33


def _draw_arrow_head(
    draw: ImageDraw.ImageDraw,
    a: Tuple[int, int],
    b: Tuple[int, int],
    color=(90, 110, 130),
) -> None:
    """绘制箭头头部。"""
    ax, ay = a
    bx, by = b
    vx = bx - ax
    vy = by - ay
    norm = (vx * vx + vy * vy) ** 0.5 or 1.0
    ux, uy = vx / norm, vy / norm
    px, py = -uy, ux
    size = 16
    p1 = (bx, by)
    p2 = (bx - ux * size - px * (size * 0.6), by - uy * size - py * (size * 0.6))
    p3 = (bx - ux * size + px * (size * 0.6), by - uy * size + py * (size * 0.6))
    draw.polygon([p1, p2, p3], fill=color)


def arrow(draw: ImageDraw.ImageDraw, a: Tuple[int, int], b: Tuple[int, int], color=(90, 110, 130), width=4) -> None:
    """绘制直线箭头。"""
    draw.line((a[0], a[1], b[0], b[1]), fill=color, width=width)
    _draw_arrow_head(draw, a, b, color=color)


def arrow_polyline(
    draw: ImageDraw.ImageDraw,
    points: List[Tuple[int, int]],
    color=(90, 110, 130),
    width=4,
) -> None:
    """绘制折线箭头（最后一段带箭头），用于绕开文本区域。"""
    if len(points) < 2:
        return
    for i in range(len(points) - 1):
        a = points[i]
        b = points[i + 1]
        draw.line((a[0], a[1], b[0], b[1]), fill=color, width=width)
    _draw_arrow_head(draw, points[-2], points[-1], color=color)


def main() -> None:
    """生成管理后台主流程 JPG。"""
    image = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(image)

    title = "AI出题管理后台主流程（基于当前代码）"
    tw = draw.textbbox((0, 0), title, font=FONT_TITLE)[2]
    draw.text(((CANVAS_W - tw) // 2, 30), title, fill=(17, 24, 39), font=FONT_TITLE)

    # 左侧：权限与租户
    left_col = (70, 180, 590, 1960)
    draw_round_box(draw, left_col, fill=(234, 240, 255), outline=(154, 177, 217), radius=24)
    gtw = draw.textbbox((0, 0), "权限与租户", font=FONT_GROUP)[2]
    draw.text((left_col[0] + (left_col[2] - left_col[0] - gtw) / 2, left_col[1] + 22), "权限与租户", fill=(31, 54, 112), font=FONT_GROUP)

    auth_boxes = [
        ((110, 300, 550, 450), "登录 / 系统号鉴权", "X-System-User + Bearer\n角色ACL + 城市权限"),
        ((110, 490, 550, 670), "租户（城市）范围", "/api/tenants\n过滤可访问城市"),
        ((110, 710, 550, 980), "平台管理", "/api/admin/cities\n/api/admin/users\n新增/停用/绑定权限"),
        ((110, 1020, 550, 1250), "仪表盘入口", "/dashboard\n汇总切片/映射/题库/近7天出题"),
    ]
    for b, t, d in auth_boxes:
        draw_round_box(draw, b, fill=(248, 251, 255))
        draw_text_center(draw, b, t, d)

    # 主体6列
    groups = [
        ("教材与切片", (640, 170, 1100, 1960), (224, 242, 255), (64, 131, 190)),
        ("切片核对", (1140, 170, 1600, 1960), (255, 244, 214), (191, 145, 52)),
        ("映射确认", (1640, 170, 2100, 1960), (255, 240, 216), (185, 120, 43)),
        ("AI出题", (2140, 170, 2620, 1960), (237, 230, 255), (122, 83, 190)),
        ("题库管理", (2660, 170, 3080, 1960), (228, 247, 232), (68, 137, 90)),
        ("质量与运营", (3120, 170, 3530, 1960), (227, 243, 255), (52, 120, 179)),
    ]

    for name, rect, fill, text_color in groups:
        draw_round_box(draw, rect, fill=fill, outline=(180, 196, 215), radius=24)
        gtw = draw.textbbox((0, 0), name, font=FONT_GROUP)[2]
        draw.text((rect[0] + (rect[2] - rect[0] - gtw) / 2, rect[1] + 22), name, fill=text_color, font=FONT_GROUP)

    # 教材与切片
    m_boxes = [
        ((670, 300, 1070, 500), "上传教材", "/materials/upload\ndocx/txt -> material_version_id"),
        ((670, 540, 1070, 770), "自动切片 + 图片解析", "generate_knowledge_slices.py\nprocess_textbook_images.py"),
        ((670, 810, 1070, 1010), "教材版本列表", "/materials\nslice_status / mapping_status"),
        ((670, 1050, 1070, 1310), "参考题上传并映射", "/materials/{mid}/reference/upload\nmap_knowledge_to_questions.py"),
        ((670, 1350, 1070, 1600), "维护动作", "生效 / 下线 / 删除\n重新切片 / 重新映射"),
    ]
    for b, t, d in m_boxes:
        draw_round_box(draw, b, fill=(244, 251, 255))
        draw_text_center(draw, b, t, d)

    # 切片核对
    s_boxes = [
        ((1170, 300, 1570, 520), "切片列表与目录树", "/slices + /slices/path-tree\nstatus/keyword/path筛选"),
        ((1170, 560, 1570, 820), "审核与编辑", "通过/驳回、批量通过\n内容编辑后自动置pending"),
        ((1170, 860, 1570, 1120), "图片解析维护", "查看图片/编辑analysis\n可用Mermaid脑图修改器"),
        ((1170, 1160, 1570, 1410), "结构优化", "新增切片/一键合并/拖拽排序\n同三级目录约束"),
        ((1170, 1450, 1570, 1660), "导出与审计", "/slices/export\naudit_log 写入"),
    ]
    for b, t, d in s_boxes:
        draw_round_box(draw, b, fill=(255, 251, 240))
        draw_text_center(draw, b, t, d)

    # 映射确认
    map_boxes = [
        ((1670, 300, 2070, 560), "加载映射结果", "/mappings\n关联切片内容+历史题目"),
        ((1670, 600, 2070, 880), "确认与修订", "通过/待审核、批量提交\n可改目标母题ID与备注"),
        ((1670, 920, 2070, 1130), "冲突处理", "meta_conflict筛选\n按目录树批量处理"),
        ((1670, 1170, 2070, 1360), "映射状态沉淀", "mapping_review_by_material.json\n供出题与回溯"),
    ]
    for b, t, d in map_boxes:
        draw_round_box(draw, b, fill=(255, 248, 236))
        draw_text_center(draw, b, t, d)

    # AI出题
    g_boxes = [
        ((2170, 300, 2590, 540), "创建出题任务", "/generate/tasks\n任务化异步执行"),
        ((2170, 580, 2590, 870), "异步执行任务", "_run_generate_task_worker\n内部调用 /generate/stream"),
        ((2170, 910, 2590, 1220), "LangGraph 生成链", "router -> writer -> critic -> fixer\n记录每题trace与llm_call"),
        ((2170, 1260, 2590, 1490), "结果判定", "critic通过才算生成成功\n可自动入库或手动入库"),
        ((2170, 1530, 2590, 1720), "任务与QA落盘", "gen_tasks.jsonl + qa_runs.jsonl\n失败任务也补记QA"),
    ]
    for b, t, d in g_boxes:
        draw_round_box(draw, b, fill=(248, 244, 255))
        draw_text_center(draw, b, t, d)

    # 准入门槛节点（跨切片核对+映射确认 -> AI出题）
    gate_box = (2170, 180, 2590, 280)
    draw_round_box(draw, gate_box, fill=(254, 240, 245), outline=(191, 92, 122), radius=20, width=4)
    draw_text_center(
        draw,
        gate_box,
        "出题准入门槛",
        "切片审核完成 + 映射确认完成",
    )

    # 题库
    bank_boxes = [
        ((2690, 340, 3050, 610), "题库存储", "/bank/add /bank\n按教材版本过滤"),
        ((2690, 650, 3050, 880), "题库运营", "查询/查看详情\n批量删除 / 批量导出"),
    ]
    for b, t, d in bank_boxes:
        draw_round_box(draw, b, fill=(240, 252, 243))
        draw_text_center(draw, b, t, d)

    # 质量与运营
    qa_boxes = [
        ((3150, 300, 3500, 570), "质量总览", "/qa/overview /qa/runs\n入库率/质量分/风险率"),
        ((3150, 610, 3500, 850), "调用与成本", "/qa/llm-calls /qa/pricing\ntokens/时延/成本"),
        ((3150, 890, 3500, 1140), "趋势与漂移", "/qa/trends /qa/drift\n跨run对比"),
        ((3150, 1180, 3500, 1460), "发布评估与告警", "/qa/release-report /qa/alerts\n阈值+SLA闭环"),
        ((3150, 1500, 3500, 1720), "运营周报", "/qa/ops-weekly\nowner维度处理效率"),
    ]
    for b, t, d in qa_boxes:
        draw_round_box(draw, b, fill=(240, 249, 255))
        draw_text_center(draw, b, t, d)

    # 关键连线（跨分区）
    arrow(draw, (550, 380), (640, 380))
    arrow(draw, (1070, 1110), (1140, 1110))
    arrow(draw, (1570, 730), (1640, 730))
    # 映射确认 -> 准入门槛（走分区走廊，避免压字）
    arrow_polyline(draw, [(2070, 1030), (2110, 1030), (2110, 230), (2170, 230)])
    # 切片核对完成 -> 准入门槛（先上提到顶部空白，再横向进入）
    arrow_polyline(draw, [(1570, 1550), (1610, 1550), (1610, 200), (2170, 200), (2170, 255)])
    # 准入门槛 -> AI出题开始
    arrow(draw, (2380, 280), (2380, 300))
    arrow(draw, (2590, 1380), (2660, 1380))
    arrow(draw, (2590, 1620), (3120, 1620))
    arrow(draw, (3050, 760), (3120, 760))
    # 切片产出后即可进入切片核对
    arrow(draw, (1070, 700), (1140, 700))
    # 映射产出后即可进入映射确认（走卡片间隙）
    arrow(draw, (1070, 1150), (1640, 1150))

    # 区内顺序箭头
    arrow(draw, (870, 500), (870, 540))
    arrow(draw, (870, 770), (870, 810))
    arrow(draw, (870, 1010), (870, 1050))
    arrow(draw, (870, 1310), (870, 1350))

    arrow(draw, (1370, 520), (1370, 560))
    arrow(draw, (1370, 820), (1370, 860))
    arrow(draw, (1370, 1120), (1370, 1160))
    arrow(draw, (1370, 1410), (1370, 1450))

    arrow(draw, (1870, 560), (1870, 600))
    arrow(draw, (1870, 880), (1870, 920))
    arrow(draw, (1870, 1130), (1870, 1170))

    arrow(draw, (2380, 540), (2380, 580))
    arrow(draw, (2380, 870), (2380, 910))
    arrow(draw, (2380, 1220), (2380, 1260))
    arrow(draw, (2380, 1490), (2380, 1530))

    note = "说明：图中准入门槛为业务期望流程；当前代码中 AI出题后端强校验的是“approved切片”，映射确认暂未做硬校验。"
    nw = draw.textbbox((0, 0), note, font=FONT_NOTE)[2]
    draw.text(((CANVAS_W - nw) // 2, 2025), note, fill=(71, 85, 105), font=FONT_NOTE)

    out = Path("/Users/panting/Desktop/搏学考试/AI出题/管理后台主流程图_基于当前代码.jpg")
    image.save(out, format="JPEG", quality=95, optimize=True)
    print(str(out))


if __name__ == "__main__":
    main()
