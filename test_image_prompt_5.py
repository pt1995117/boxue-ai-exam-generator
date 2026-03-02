#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run image-analysis prompt smoke test on 5 images."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List

from process_textbook_images import analyze_image_with_qwen_vl, load_config


def _natural_key(path: Path):
    parts = re.split(r"(\d+)", path.name)
    out = []
    for p in parts:
        if p.isdigit():
            out.append(int(p))
        else:
            out.append(p.lower())
    return out


def _collect_images(image_dir: Path) -> List[Path]:
    exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
    files = [p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]
    return sorted(files, key=_natural_key)


def _load_runtime_config():
    cfg = load_config()
    api_key = (cfg.get("CRITIC_API_KEY") or cfg.get("OPENAI_API_KEY") or "").strip()
    model_name = (cfg.get("IMAGE_MODEL") or "doubao-seed-1.8").strip()
    provider = (cfg.get("IMAGE_PROVIDER") or "").strip().lower()
    base_url = (cfg.get("IMAGE_BASE_URL") or cfg.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3").strip()
    ark_api_key = (cfg.get("ARK_API_KEY") or "").strip()
    volc_ak = (cfg.get("VOLC_ACCESS_KEY_ID") or "").strip()
    volc_sk = (cfg.get("VOLC_SECRET_ACCESS_KEY") or "").strip()
    ark_project_name = (cfg.get("ARK_PROJECT_NAME") or "").strip()

    if provider == "ark":
        if not (ark_api_key or (volc_ak and volc_sk)):
            raise RuntimeError("Ark 图片链路缺少认证信息：请配置 ARK_API_KEY 或 VOLC_ACCESS_KEY_ID/VOLC_SECRET_ACCESS_KEY")
    elif not api_key:
        raise RuntimeError("未找到 API Key，请在 填写您的Key.txt 配置 CRITIC_API_KEY / OPENAI_API_KEY")

    return {
        "api_key": api_key,
        "model_name": model_name,
        "provider": provider,
        "base_url": base_url,
        "ark_api_key": ark_api_key,
        "volc_ak": volc_ak,
        "volc_sk": volc_sk,
        "ark_project_name": ark_project_name,
    }


def main():
    parser = argparse.ArgumentParser(description="Use latest image prompt to test 5 images")
    parser.add_argument("--image-dir", default="data/wh/slices/images/v20260224_131517", help="Directory that contains images")
    parser.add_argument("--count", type=int, default=5, help="How many images to test")
    parser.add_argument(
        "--images",
        default="",
        help="Comma-separated image names or stems, e.g. image7,image11,image20",
    )
    parser.add_argument("--out", default="", help="Output JSONL path")
    args = parser.parse_args()

    image_dir = Path(args.image_dir).expanduser().resolve()
    if not image_dir.is_dir():
        raise SystemExit(f"❌ 目录不存在: {image_dir}")

    images = _collect_images(image_dir)
    if not images:
        raise SystemExit(f"❌ 未找到图片: {image_dir}")

    if args.images.strip():
        wanted = [x.strip() for x in args.images.split(",") if x.strip()]
        selected = []
        image_by_name = {p.name: p for p in images}
        image_by_stem = {p.stem: p for p in images}
        for token in wanted:
            hit = image_by_name.get(token) or image_by_stem.get(token)
            if hit:
                selected.append(hit)
            else:
                print(f"⚠️ 未找到图片: {token}")
        if not selected:
            raise SystemExit("❌ 指定图片均未找到")
    else:
        selected = images[: max(1, args.count)]
    cfg = _load_runtime_config()

    out_path = Path(args.out).expanduser().resolve() if args.out else Path("tmp") / f"image_prompt_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Using model: {cfg['model_name']} (provider={cfg['provider'] or 'openai_compat'})")
    print(f"Testing images: {len(selected)}")
    print(f"Output: {out_path}")

    ok = 0
    with out_path.open("w", encoding="utf-8") as f:
        for idx, img in enumerate(selected, start=1):
            print(f"[{idx}/{len(selected)}] {img.name} ...", end=" ", flush=True)
            text = analyze_image_with_qwen_vl(
                str(img),
                cfg["api_key"],
                model_name=cfg["model_name"],
                base_url=cfg["base_url"],
                provider=cfg["provider"],
                ark_api_key=cfg["ark_api_key"],
                volc_ak=cfg["volc_ak"],
                volc_sk=cfg["volc_sk"],
                ark_project_name=cfg["ark_project_name"],
            )
            err = str(getattr(analyze_image_with_qwen_vl, "last_error", "") or "")
            rec = {
                "index": idx,
                "image": str(img),
                "success": bool(text),
                "error": err if not text else "",
                "output_preview": (text or "")[:500],
                "output": text or "",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if text:
                ok += 1
                print("OK")
            else:
                print(f"FAIL ({err or 'unknown'})")

    print("-" * 70)
    print(f"Done. success={ok}/{len(selected)}")
    print(f"Result file: {out_path}")


if __name__ == "__main__":
    main()
