# Task 8.6 / TP12.6c: strip_title_prefix and kb_gps full_path match (no BGE deps)
import re


def strip_title_prefix(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.strip()
    s = re.sub(r"^[（(][一二三四五六七八九十\d]+[）)]", "", s)
    s = s.strip()
    s = re.sub(r"^[一二三四五六七八九十百千\d]+、", "", s)
    s = s.strip()
    s = re.sub(r"^\d+、", "", s)
    return s.strip()


def normalize_path_dehydration(text):
    if not isinstance(text, str):
        return ""
    text = re.sub(r"第[一二三四五六七八九十\d]+[篇章节]", "", text)
    text = re.sub(r"[（(]了解[）)]", "", text)
    text = re.sub(r"[（(]掌握[）)]", "", text)
    text = re.sub(r"[（(]熟悉[）)]", "", text)
    text = re.sub(r"[（(].*?了解.*?[）)]", "", text)
    text = re.sub(r"[（(].*?掌握.*?[）)]", "", text)
    text = re.sub(r"[（(].*?熟悉.*?[）)]", "", text)
    text = re.sub(r"-无需修改", "", text)
    text = re.sub(r"无需修改", "", text)
    text = re.sub(r"[>\s]+", "/", text)
    text = re.sub(r"[、，。：；？！（）().,:;?!]", "/", text)
    text = re.sub(r"/+", "/", text)
    return text.strip("/")


def test_strip_title_prefix():
    assert strip_title_prefix("二、贝壳战略") == "贝壳战略"
    assert strip_title_prefix("（一）第一翼：整装") == "第一翼：整装"
    assert strip_title_prefix("1、xxx") == "xxx"
    assert strip_title_prefix("一、从链家到贝壳") == "从链家到贝壳"
    assert strip_title_prefix("（三）第三翼：贝好家") == "第三翼：贝好家"
    print("strip_title_prefix: ok")


def test_kb_gps_full_path_match():
    path = "第一篇  行业与贝壳 > 第二章  认识贝壳 > 第一节  贝壳发展历程 > 二、贝壳战略"
    segs = [x.strip() for x in path.split(">")]
    stripped = strip_title_prefix(segs[-1])
    assert stripped == "贝壳战略"
    mod = " > ".join(segs[:-1] + [stripped])
    gps = normalize_path_dehydration(mod)
    full_path = normalize_path_dehydration(
        "第二章  认识贝壳 / 第一节  贝壳发展历程 / 贝壳战略（了解）-无需修改"
    )
    assert full_path in gps, f"full_path {full_path!r} not in kb_gps {gps!r}"
    print("kb_gps full_path match: ok")


if __name__ == "__main__":
    test_strip_title_prefix()
    test_kb_gps_full_path_match()
    print("all ok")
