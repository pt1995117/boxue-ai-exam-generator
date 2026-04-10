#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "leadership_main_flow_20260409.png"

W = 3200
H = 1850
BG = (248, 249, 252)
TEXT = (32, 45, 64)
LINE = (110, 130, 158)
BOX_FILL = (255, 255, 255)


def pick_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = []
    if bold:
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    for p in candidates:
        if Path(p).exists():
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


FONT_TITLE = pick_font(64, bold=True)
FONT_COL = pick_font(34, bold=True)
FONT_FOOT = pick_font(18, bold=False)


def wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    out: list[str] = []
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
        out.append(line or "")
    return out


def round_box(draw: ImageDraw.ImageDraw, xy, fill, outline, radius=26, width=3):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def draw_multiline_center(
    draw: ImageDraw.ImageDraw,
    box,
    title: str,
    body: str,
    title_color=TEXT,
    body_color=(82, 96, 120),
):
    x1, y1, x2, y2 = box
    inner_w = x2 - x1 - 36
    inner_h = y2 - y1 - 30

    best = None
    for body_size in range(18, 13, -1):
        title_font = pick_font(24, bold=True)
        body_font = pick_font(body_size, bold=False)
        title_lines = wrap(draw, title, title_font, inner_w)
        body_lines = wrap(draw, body, body_font, inner_w)
        title_gap = 34
        body_gap = 24 if body_size >= 17 else 22
        total_h = len(title_lines) * title_gap + 8 + len(body_lines) * body_gap
        if total_h <= inner_h:
            best = (title_font, body_font, title_lines, body_lines, title_gap, body_gap, total_h)
            break
    if best is None:
        title_font = pick_font(22, bold=True)
        body_font = pick_font(14, bold=False)
        title_lines = wrap(draw, title, title_font, inner_w)
        body_lines = wrap(draw, body, body_font, inner_w)
        title_gap = 30
        body_gap = 20
        total_h = len(title_lines) * title_gap + 6 + len(body_lines) * body_gap
    else:
        title_font, body_font, title_lines, body_lines, title_gap, body_gap, total_h = best

    y = y1 + (y2 - y1 - total_h) / 2
    for line in title_lines:
        tw = draw.textbbox((0, 0), line, font=title_font)[2]
        draw.text((x1 + (x2 - x1 - tw) / 2, y), line, fill=title_color, font=title_font)
        y += title_gap
    y += 6
    for line in body_lines:
        tw = draw.textbbox((0, 0), line, font=body_font)[2]
        draw.text((x1 + (x2 - x1 - tw) / 2, y), line, fill=body_color, font=body_font)
        y += body_gap


def arrow(draw: ImageDraw.ImageDraw, start, end, color=LINE, width=4):
    draw.line((start[0], start[1], end[0], end[1]), fill=color, width=width)
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    norm = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / norm, dy / norm
    px, py = -uy, ux
    size = 14
    p1 = (bx, by)
    p2 = (bx - ux * size - px * 7, by - uy * size - py * 7)
    p3 = (bx - ux * size + px * 7, by - uy * size + py * 7)
    draw.polygon([p1, p2, p3], fill=color)


def draw_column(draw: ImageDraw.ImageDraw, rect, title: str, fill, outline):
    round_box(draw, rect, fill=fill, outline=outline, radius=28, width=3)
    tw = draw.textbbox((0, 0), title, font=FONT_COL)[2]
    draw.text((rect[0] + (rect[2] - rect[0] - tw) / 2, rect[1] + 18), title, fill=outline, font=FONT_COL)


def add_boxes(draw: ImageDraw.ImageDraw, x1: int, boxes: Iterable[tuple[int, int, str, str]], width: int):
    coords = []
    for top, height, title, body in boxes:
        rect = (x1, top, x1 + width, top + height)
        round_box(draw, rect, fill=BOX_FILL, outline=(150, 166, 191), radius=18, width=2)
        draw_multiline_center(draw, rect, title, body)
        coords.append(rect)
    return coords


def main() -> None:
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)

    title = "AI出题管理后台主流程"
    tw = draw.textbbox((0, 0), title, font=FONT_TITLE)[2]
    draw.text(((W - tw) // 2, 30), title, fill=TEXT, font=FONT_TITLE)

    cols = [
        ("平台与权限", (70, 120, 430, 1700), (238, 244, 255), (88, 121, 189)),
        ("教材准备", (470, 120, 830, 1700), (234, 245, 255), (78, 146, 202)),
        ("内容治理", (870, 120, 1230, 1700), (255, 247, 221), (191, 149, 62)),
        ("智能生产", (1270, 120, 1630, 1700), (252, 238, 255), (158, 93, 177)),
        ("题库与发布", (1670, 120, 2030, 1700), (236, 249, 236), (88, 160, 99)),
        ("质量运营", (2070, 120, 2430, 1700), (236, 246, 255), (74, 137, 194)),
        ("平台价值", (2470, 120, 3130, 1700), (244, 246, 250), (98, 112, 132)),
    ]
    for title_text, rect, fill, outline in cols:
        draw_column(draw, rect, title_text, fill, outline)

    col1 = add_boxes(
        draw,
        110,
        [
            (220, 150, "统一入口", "平台管理员\n城市管理员\n教研老师\n按权限进入同一后台"),
            (220, 150, "统一入口", "平台管理员\n城市管理员\n教研老师\n查看角色\n按权限进入统一后台"),
            (420, 160, "城市隔离", "按城市维度隔离数据\n按角色控制页面与操作范围"),
            (630, 180, "平台管理", "城市管理\n用户与角色管理\n统一 Key 配置"),
        ],
        280,
    )
    col2 = add_boxes(
        draw,
        510,
        [
            (220, 150, "上传教材", "形成教材版本\n进入生产准备阶段"),
            (420, 170, "系统自动处理", "自动切片\n图片解析\n沉淀教材结构化内容"),
            (640, 170, "导入参考题", "上传历史题或参考题\n建立映射基础"),
            (860, 150, "版本维护", "重切片\n重映射\n归档或删除旧版本"),
        ],
        280,
    )
    col3 = add_boxes(
        draw,
        910,
        [
            (220, 150, "切片核对", "教研核对切片内容\n修正错误结构与文本"),
            (420, 170, "映射确认", "确认切片与母题关系\n必要时手工补全母题信息"),
            (640, 150, "教材生效", "满足审核条件后\n将教材设为正式可用版本"),
            (840, 170, "治理结果沉淀", "形成可追溯的审核结果\n为出题提供稳定输入"),
        ],
        280,
    )
    col4 = add_boxes(
        draw,
        1310,
        [
            (220, 140, "出题模板", "按题量\n知识路径\n掌握度配置生产规则"),
            (400, 150, "AI 出题任务", "按教材或模板发起出题\n系统异步执行"),
            (600, 200, "智能生成链路", "系统完成题目生成\n校验\n修复\n重试\n续跑与补齐"),
            (840, 170, "结果判定", "合格题进入题库\n异常任务支持续跑"),
            (1060, 150, "过程留痕", "任务详情\n过程 trace\n过程数据可追溯"),
        ],
        280,
    )
    col5 = add_boxes(
        draw,
        1710,
        [
            (220, 150, "题库存储", "沉淀正式题目资产\n按教材版本管理"),
            (420, 150, "版本发布", "基于质量评估结果\n形成版本化交付"),
            (620, 170, "题库运营", "查询\nAI 调优\n人工修改\n批量导出"),
            (840, 150, "交付支撑", "为业务使用\n后续优化\n外部交付提供基础"),
        ],
        280,
    )
    col6 = add_boxes(
        draw,
        2110,
        [
            (220, 150, "质量总览", "查看通过率\n质量分\n风险情况"),
            (420, 150, "Judge 评估", "对已生成题目进行离线质量评估"),
            (620, 150, "趋势与漂移", "对比不同批次质量变化\n支持持续优化"),
            (820, 170, "发布评估与告警", "形成发布判断依据\n异常指标进入告警闭环"),
            (1040, 150, "运营周报", "面向运营与管理层\n沉淀阶段性结果"),
        ],
        280,
    )
    col7 = add_boxes(
        draw,
        2510,
        [
            (260, 150, "提效", "把教材准备\n题目生产\n质量检查纳入统一平台"),
            (460, 150, "提质", "通过审核\nJudge\nQA 指标控制输出质量"),
            (660, 150, "可追溯", "教材版本\n任务过程\n题库结果均可回溯"),
            (860, 150, "可运营", "通过版本发布\n告警\n周报形成持续运营闭环"),
        ],
        580,
    )

    # Main directional arrows between columns
    arrow(draw, (390, 295), (510, 295))
    arrow(draw, (790, 505), (910, 505))
    arrow(draw, (1190, 715), (1310, 715))
    arrow(draw, (1590, 915), (1710, 915))
    arrow(draw, (1990, 1095), (2110, 1095))
    arrow(draw, (2390, 1115), (2510, 1115))

    # Vertical arrows in core columns
    for rects in (col2, col3, col4, col5, col6):
        for upper, lower in zip(rects[:-1], rects[1:]):
            arrow(draw, ((upper[0] + upper[2]) // 2, upper[3]), ((lower[0] + lower[2]) // 2, lower[1]))

    # Loop from quality back to production
    draw.line((2250, 1240, 2250, 1550, 1450, 1550, 1450, 1210), fill=LINE, width=4)
    arrow(draw, (1450, 1550), (1450, 1210))

    foot = "说明：本图按当前系统业务主链路绘制，突出“教材准备—内容治理—智能生产—质量运营—题库与发布”的闭环，不展开代码级实现细节。"
    draw.text((420, 1765), foot, fill=(100, 110, 130), font=FONT_FOOT)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT, quality=95)
    print(str(OUT))


if __name__ == "__main__":
    main()
