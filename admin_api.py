from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import random
import uuid
from io import BytesIO
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
import threading
from copy import deepcopy

import pandas as pd
from flask import Flask, Response, g, jsonify, request, send_file, stream_with_context
from werkzeug.exceptions import HTTPException

from authn import AccessDenied, Principal, resolve_principal
from audit_log import write_audit_log
from governance import circuit_breaker, rate_limiter, select_release_channel
from mapping_review_store import load_mapping_review
from observability import init_observability, start_span
from slice_registry import (
    archive_material_version,
    delete_material_version,
    list_material_versions,
    register_material_version,
    set_effective_material_version,
    upsert_material_runtime,
)
from slice_review_store import load_slice_review
from tenants_config import (
    delete_tenant,
    list_tenants,
    resolve_tenant_history_path,
    set_tenant_status,
    tenant_audit_log_path,
    tenant_mapping_path,
    tenant_root,
    tenant_slices_dir,
    tenant_bank_path,
    upsert_tenant,
)
from tenant_context import get_accessible_tenants, assert_tenant_access, enforce_permission, load_acl, save_acl
from exam_factory import KnowledgeRetriever, set_active_tenant
from exam_graph import app as graph_app, mark_unstable, summarize_llm_trace

app = Flask(__name__)
init_observability("exam-admin-api")

SLICE_STATUSES = {"pending", "approved"}
MAP_STATUSES = {"pending", "approved"}
QUESTION_TYPES = {"单选题", "多选题", "判断题", "随机"}
GEN_MODES = {"灵活", "严谨"}
ALLOWED_ORIGINS = set(
    x.strip()
    for x in os.getenv(
        "ADMIN_WEB_ORIGINS",
        "http://127.0.0.1:8520,http://localhost:8520,http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:3000,http://localhost:3000",
    ).split(",")
    if x.strip()
)


def _json_response(payload: dict[str, Any], status: int = 200):
    resp = jsonify(payload)
    resp.status_code = status
    req_origin = request.headers.get("Origin", "")
    if req_origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = req_origin
        resp.headers["Vary"] = "Origin"
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-System-User'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    release_channel = getattr(g, "release_channel", "")
    if release_channel:
        resp.headers["X-Release-Channel"] = release_channel
    request_id = getattr(g, "request_id", "")
    if request_id:
        resp.headers["X-Request-Id"] = request_id
    return resp


def _error(code: str, message: str, status: int = 400):
    return _json_response({"error": {"code": code, "message": message}}, status=status)


def _get_principal() -> Principal:
    principal = getattr(g, "principal", None)
    if principal is None:
        principal = resolve_principal(
            authorization_header=(request.headers.get("Authorization") or ""),
            system_user_header=(request.headers.get("X-System-User") or ""),
        )
        g.principal = principal
    return principal


def _get_system_user() -> str:
    return _get_principal().system_user


def _check_tenant_permission(tenant_id: str, perm_code: str) -> None:
    principal = _get_principal()
    if tenant_id not in principal.tenants:
        raise PermissionError("TENANT_FORBIDDEN")
    # OIDC: prefer permissions in token; fall back to role lookup inside tenant_context.
    if principal.auth_mode == "oidc":
        if "*" in principal.permissions or perm_code in principal.permissions:
            return
        if principal.role == "platform_admin":
            return
        if not principal.permissions:
            raise PermissionError("PERMISSION_MISSING_IN_TOKEN")
        if perm_code not in principal.permissions:
            raise PermissionError("PERMISSION_DENIED")
        return
    assert_tenant_access(principal.system_user, tenant_id)
    enforce_permission(principal.system_user, tenant_id, perm_code)


def _require_platform_admin() -> None:
    principal = _get_principal()
    if principal.role != "platform_admin":
        raise PermissionError("PLATFORM_ADMIN_REQUIRED")


def _parse_pagination() -> tuple[int, int]:
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    try:
        page_size = int(request.args.get("page_size", 20))
    except ValueError:
        page_size = 20
    page_size = min(max(page_size, 1), 200)
    return page, page_size


def _parse_bool_arg(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_mapping_status(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in {"approved", "confirmed"}:
        return "approved"
    if s in {"pending", "auto_pending", "rejected", "remapped", "revised"}:
        return "pending"
    return "pending"


def _load_audit_events(tenant_id: str) -> list[dict[str, Any]]:
    path = tenant_audit_log_path(tenant_id)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def _paginate(items: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return {"items": items[start:end], "total": total, "page": page, "page_size": page_size}


def _extract_slice_text(item: dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    def _as_text(value: Any) -> str:
        if value is None or isinstance(value, bool):
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value).strip()
        if isinstance(value, dict):
            # Prefer text-like keys to keep display close to slice source.
            for k in ("text", "content", "analysis", "caption", "title", "name"):
                v = value.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return json.dumps(value, ensure_ascii=False).strip()
        if isinstance(value, list):
            parts = [_as_text(v) for v in value]
            parts = [p for p in parts if p]
            return "\n".join(parts).strip()
        return str(value).strip()

    # 1) Prefer explicit slice text fields (keep as-is, do not de-duplicate).
    for key in ("切片内容", "slice_content", "核心内容", "content", "chunk_text", "text", "正文", "内容"):
        txt = _as_text(item.get(key))
        if txt:
            return txt

    # 2) Fallback to structured content in source order.
    struct = item.get("结构化内容") or {}
    if isinstance(struct, dict):
        parts: list[str] = []
        for key in ("context_before", "tables", "context_after", "examples", "formulas", "rules", "key_params"):
            txt = _as_text(struct.get(key))
            if txt:
                parts.append(txt)
        if parts:
            return "\n".join(parts).strip()

    # 3) Last fallback
    for key in ("标题", "章节", "完整路径"):
        txt = _as_text(item.get(key))
        if txt:
            return txt
    return ""


def _extract_slice_images(item: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        return []
    raw_images = (item.get("结构化内容", {}) or {}).get("images", []) or []
    image_items: list[dict[str, Any]] = []
    if not isinstance(raw_images, list):
        return image_items
    for img in raw_images:
        if not isinstance(img, dict):
            continue
        image_items.append(
            {
                "image_id": str(img.get("image_id", "")),
                "image_path": str(img.get("image_path", "")),
                "analysis": str(img.get("analysis", "")),
                "contains_table": bool(img.get("contains_table", False)),
                "contains_chart": bool(img.get("contains_chart", False)),
            }
        )
    return image_items


def _stringify_structured_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _short_text(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}…"


def _extract_question_parts(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize question payload from draft/final formats."""
    if not isinstance(payload, dict):
        return {"stem": "", "options": [], "answer": "", "explanation": ""}
    stem = str(payload.get("题干", "") or payload.get("question", "")).strip()
    answer = str(payload.get("正确答案", "") or payload.get("answer", "")).strip()
    explanation = str(payload.get("解析", "") or payload.get("explanation", "")).strip()
    options: list[str] = []
    if isinstance(payload.get("options"), list):
        options = [str(x).strip() for x in (payload.get("options") or []) if str(x).strip()]
    if not options:
        for i in range(1, 9):
            v = str(payload.get(f"选项{i}", "")).strip()
            if v:
                options.append(v)
    return {"stem": stem, "options": options[:8], "answer": answer, "explanation": explanation}


def _format_options(options: list[str], limit: int = 4) -> str:
    if not options:
        return "-"
    labels = "ABCDEFGH"
    chunks: list[str] = []
    for i, opt in enumerate(options[:limit]):
        chunks.append(f"{labels[i]}. {_short_text(opt, 80)}")
    return " | ".join(chunks)


def _is_noisy_log(node_name: str, text: str) -> bool:
    low = str(text).strip().lower()
    if not low:
        return True
    # Structured highlights already render these with richer details.
    noisy_prefixes = (
        "作家: 已格式化为",
        "作家: 已在润色后执行格式硬修复",
        "generalagent: 初稿已生成",
        "计算专家: 初稿已生成",
        "审核通过（反向解题成功",
    )
    if any(str(text).strip().startswith(x) for x in noisy_prefixes):
        return True
    # Keep important failures visible.
    if "错误" in str(text) or "失败" in str(text) or "驳回" in str(text):
        return False
    # Router verbose plain-text line is duplicated by structured router step.
    if node_name == "router" and ("agent=" in low or "path=" in low):
        return True
    return False


def _emit_node_highlights(
    node_name: str,
    state_update: dict[str, Any],
    append_step: Callable[..., None],
) -> None:
    final_json = state_update.get("final_json") if isinstance(state_update, dict) else None
    if isinstance(final_json, dict):
        stem = str(final_json.get("题干", "")).strip()
        answer = str(final_json.get("正确答案", "")).strip()
        diff = final_json.get("难度值", "")
        if stem:
            append_step("题干要点", node=node_name, detail=_short_text(stem, 150))
        if answer or diff != "":
            append_step(
                "题目结果",
                node=node_name,
                detail=f"答案={answer or '-'} 难度={diff if diff != '' else '-'}",
            )

    if node_name == "calculator":
        tool_usage = state_update.get("tool_usage")
        if isinstance(tool_usage, dict):
            code_status = str(tool_usage.get("code_status", "")).strip()
            result = tool_usage.get("result")
            if code_status or result not in (None, ""):
                append_step(
                    "计算结果",
                    node=node_name,
                    detail=f"status={code_status or '-'} result={_short_text(result, 80) or '-'}",
                )

    if node_name == "writer":
        format_issues = state_update.get("writer_format_issues")
        if isinstance(format_issues, list) and format_issues:
            top_issues = [str(x) for x in format_issues[:5] if str(x).strip()]
            if top_issues:
                append_step(
                    "格式校验提示",
                    node=node_name,
                    level="warning",
                    detail="; ".join(top_issues),
                )

    # Draft visibility: what specialist/calculator actually produced before writer.
    if node_name in {"specialist", "calculator"}:
        draft = state_update.get("draft")
        if isinstance(draft, dict):
            parts = _extract_question_parts(draft)
            if parts["stem"]:
                append_step("初稿题干", node=node_name, detail=_short_text(parts["stem"], 180))
            if parts["options"]:
                append_step("初稿选项", node=node_name, detail=_format_options(parts["options"], 4))
            if parts["explanation"]:
                append_step("初稿解析", node=node_name, detail=_short_text(parts["explanation"], 200))

    # Final visibility: writer/fixer finalized question snapshot.
    if node_name in {"writer", "fixer"} and isinstance(final_json, dict):
        parts = _extract_question_parts(final_json)
        if parts["stem"]:
            append_step("定稿题干", node=node_name, detail=_short_text(parts["stem"], 180))
        if parts["options"]:
            append_step("定稿选项", node=node_name, detail=_format_options(parts["options"], 4))
        if parts["explanation"]:
            append_step("定稿解析", node=node_name, detail=_short_text(parts["explanation"], 220))

    if node_name == "critic":
        critic_result = state_update.get("critic_result")
        if isinstance(critic_result, dict):
            if "passed" in critic_result:
                passed = bool(critic_result.get("passed"))
                if passed:
                    deduction = _short_text(critic_result.get("deduction_process", ""), 120)
                    if deduction:
                        append_step("审核依据", node=node_name, detail=deduction)
                else:
                    issue_type = str(critic_result.get("issue_type", "")).strip() or "-"
                    strategy = str(critic_result.get("fix_strategy", "")).strip() or "-"
                    reason = _short_text(critic_result.get("reason", ""), 120) or "-"
                    append_step(
                        "审核动作",
                        node=node_name,
                        level="warning",
                        detail=f"issue={issue_type} strategy={strategy} reason={reason}",
                    )
            all_issues = critic_result.get("all_issues")
            if isinstance(all_issues, list) and all_issues:
                top_issues = [str(x) for x in all_issues[:6] if str(x).strip()]
                if top_issues:
                    append_step(
                        "审核问题清单",
                        node=node_name,
                        level="warning",
                        detail="; ".join(top_issues),
                    )
        required = state_update.get("critic_required_fixes")
        if isinstance(required, list) and required:
            top_required = [str(x) for x in required[:6] if str(x).strip()]
            if top_required:
                append_step("必改项", node=node_name, level="warning", detail="; ".join(top_required))

    if node_name == "fixer":
        fix_summary = state_update.get("fix_summary")
        if isinstance(fix_summary, dict):
            changed = fix_summary.get("changed_fields")
            unmet = fix_summary.get("unmet_required_fixes")
            changed_text = ",".join([str(x) for x in (changed or []) if str(x).strip()]) or "-"
            unmet_text = ",".join([str(x) for x in (unmet or []) if str(x).strip()]) or "-"
            append_step("修复摘要", node=node_name, detail=f"changed={changed_text} unmet={unmet_text}")


def _detect_table_from_text(text: str) -> bool:
    if not text:
        return False
    lines = text.splitlines()
    for i, line in enumerate(lines[:-1]):
        if '|' in line and '---' in lines[i + 1]:
            return True
    return False


def _detect_chart_from_text(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    for kw in ('坐标', '曲线', '趋势', '图表', '图', 'axis', 'chart'):
        if kw in low:
            return True
    return False


def _load_kb_items(tenant_id: str) -> list[dict[str, Any]]:
    slices_dir = tenant_slices_dir(tenant_id)
    candidates = sorted(slices_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    kb_path = candidates[0] if candidates else None
    if kb_path is None:
        return []
    if not Path(kb_path).exists():
        return []
    items: list[dict[str, Any]] = []
    with open(kb_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _load_kb_items_from_file(kb_path: Path) -> list[dict[str, Any]]:
    if not kb_path.exists():
        return []
    items: list[dict[str, Any]] = []
    with kb_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _extract_material_version_from_slice_file(path: Path) -> str:
    name = path.name
    m = re.match(r"knowledge_slices_(v\d{8}_\d{6})\.jsonl$", name)
    if m:
        return m.group(1)
    return ""


def _list_slice_files(tenant_id: str) -> list[Path]:
    return sorted(tenant_slices_dir(tenant_id).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def _resolve_material_version_id(tenant_id: str, requested: str = "") -> str:
    requested = str(requested or "").strip()
    materials = list_material_versions(tenant_id)
    material_ids = {str(x.get("material_version_id", "")).strip() for x in materials}
    if requested:
        return requested if requested in material_ids else ""
    effective = next((x for x in materials if str(x.get("status", "")) == "effective"), None)
    if effective and effective.get("material_version_id"):
        return str(effective.get("material_version_id"))
    for p in _list_slice_files(tenant_id):
        mid = _extract_material_version_from_slice_file(p)
        if mid:
            return mid
    return ""


def _resolve_slice_file_for_material(tenant_id: str, material_version_id: str = "") -> Path | None:
    if material_version_id:
        candidate = tenant_slices_dir(tenant_id) / f"knowledge_slices_{material_version_id}.jsonl"
        if candidate.exists():
            return candidate
    files = _list_slice_files(tenant_id)
    if files:
        return files[0]
    return None


def _resolve_mapping_path_for_material(tenant_id: str, material_version_id: str = "") -> Path | None:
    root = tenant_root(tenant_id) / "mapping"
    if material_version_id:
        cands = [
            root / f"knowledge_question_mapping_{material_version_id}.json",
            root / f"knowledge_question_mapping_{material_version_id}.jsonl",
        ]
        for c in cands:
            if c.exists():
                return c
    legacy = Path(tenant_mapping_path(tenant_id))
    if legacy.exists():
        return legacy
    return None


def _slice_review_file_by_material(tenant_id: str) -> Path:
    path = tenant_root(tenant_id) / "slices" / "slice_review_by_material.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _mapping_review_file_by_material(tenant_id: str) -> Path:
    path = tenant_root(tenant_id) / "mapping" / "mapping_review_by_material.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_material_bucket(path: Path, material_version_id: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    bucket = payload.get(material_version_id, {})
    if isinstance(bucket, dict):
        return bucket
    return {}


def _save_material_bucket(path: Path, material_version_id: str, bucket: dict[str, dict[str, Any]]) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                payload = old
        except json.JSONDecodeError:
            payload = {}
    payload[material_version_id] = bucket
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _delete_material_bucket(path: Path, material_version_id: str) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                payload = old
        except json.JSONDecodeError:
            payload = {}
    if material_version_id in payload:
        payload.pop(material_version_id, None)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_slice_review_for_material(tenant_id: str, material_version_id: str) -> dict[str, dict[str, Any]]:
    path = _slice_review_file_by_material(tenant_id)
    bucket = _load_material_bucket(path, material_version_id)
    if bucket:
        return bucket
    # Backward compatibility: first time for effective material, use legacy review as seed.
    legacy = load_slice_review(tenant_id)
    if legacy:
        seeded = {str(k): v for k, v in legacy.items() if str(k).isdigit() and isinstance(v, dict)}
        if seeded:
            _save_material_bucket(path, material_version_id, seeded)
            return seeded
    return {}


def _upsert_slice_review_for_material(
    tenant_id: str,
    material_version_id: str,
    slice_id: int,
    review_status: str,
    reviewer: str,
    comment: str = "",
) -> None:
    path = _slice_review_file_by_material(tenant_id)
    bucket = _load_material_bucket(path, material_version_id)
    bucket[str(slice_id)] = {
        "slice_id": int(slice_id),
        "review_status": review_status,
        "reviewer": reviewer,
        "comment": comment,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "material_version_id": material_version_id,
    }
    _save_material_bucket(path, material_version_id, bucket)


def _load_mapping_review_for_material(tenant_id: str, material_version_id: str) -> dict[str, dict[str, Any]]:
    path = _mapping_review_file_by_material(tenant_id)
    bucket = _load_material_bucket(path, material_version_id)
    if bucket:
        return bucket
    legacy = load_mapping_review(tenant_id)
    if legacy:
        seeded = {str(k): v for k, v in legacy.items() if isinstance(v, dict)}
        if seeded:
            _save_material_bucket(path, material_version_id, seeded)
            return seeded
    return {}


def _upsert_mapping_review_for_material(
    tenant_id: str,
    material_version_id: str,
    map_key: str,
    confirm_status: str,
    reviewer: str,
    comment: str = "",
    target_mother_question_id: str = "",
) -> None:
    path = _mapping_review_file_by_material(tenant_id)
    bucket = _load_material_bucket(path, material_version_id)
    bucket[str(map_key)] = {
        "map_key": str(map_key),
        "confirm_status": confirm_status,
        "reviewer": reviewer,
        "comment": comment,
        "target_mother_question_id": target_mother_question_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "material_version_id": material_version_id,
    }
    _save_material_bucket(path, material_version_id, bucket)


def _load_history_rows(tenant_id: str) -> dict[int, dict[str, Any]]:
    history_path = resolve_tenant_history_path(tenant_id)
    rows: dict[int, dict[str, Any]] = {}
    if Path(history_path).exists():
        try:
            df = pd.read_excel(history_path)
            for idx, row in df.iterrows():
                stem = str(row.get("题干", "")).strip()
                ans = str(row.get("正确答案", "")).strip()
                exp = str(row.get("解析", "")).strip()
                if not stem and "题目" in row:
                    stem = str(row.get("题目", "")).strip()
                if not ans and "答案" in row:
                    ans = str(row.get("答案", "")).strip()
                if not exp and "分析" in row:
                    exp = str(row.get("分析", "")).strip()
                rows[int(idx)] = {
                    "题干": stem,
                    "正确答案": ans,
                    "解析": exp,
                }
        except Exception:
            rows = {}
    if rows:
        return rows
    bank_path = Path("local_question_bank.jsonl")
    if not bank_path.exists():
        return {}
    idx = 0
    with bank_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            stem = str(rec.get("题干") or rec.get("stem") or rec.get("question") or "").strip()
            ans = str(rec.get("正确答案") or rec.get("answer") or rec.get("答案") or "").strip()
            exp = str(rec.get("解析") or rec.get("explanation") or rec.get("analysis") or "").strip()
            rows[idx] = {"题干": stem, "正确答案": ans, "解析": exp}
            idx += 1
    return rows


def _resolve_mapping_path_for_tenant(tenant_id: str) -> Path | None:
    mapping_path_obj = Path(tenant_mapping_path(tenant_id))
    if mapping_path_obj.exists():
        return mapping_path_obj
    return None


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^0-9a-zA-Z._\-\u4e00-\u9fff]+", "_", filename).strip("._")
    return cleaned or "material"


def _material_upload_dir(tenant_id: str) -> Path:
    root = tenant_root(tenant_id) / "materials" / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _material_reference_dir(tenant_id: str) -> Path:
    root = tenant_root(tenant_id) / "materials" / "references"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _material_slice_image_dir(tenant_id: str, material_version_id: str, create: bool = True) -> Path:
    root = tenant_root(tenant_id) / "slices" / "images" / material_version_id
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _material_history_copy_path(tenant_id: str, material_version_id: str, suffix: str = ".xlsx") -> Path:
    return tenant_root(tenant_id) / "materials" / f"history_questions_{material_version_id}{suffix}"


def _resolve_history_path_for_material(tenant_id: str, material_version_id: str) -> Path:
    for ext in (".xlsx", ".xls"):
        p = _material_history_copy_path(tenant_id, material_version_id, ext)
        if p.exists():
            return p
    return resolve_tenant_history_path(tenant_id)


def _find_material_record(tenant_id: str, material_version_id: str) -> dict[str, Any] | None:
    target = str(material_version_id).strip()
    if not target:
        return None
    items = list_material_versions(tenant_id)
    return next((x for x in items if str(x.get("material_version_id", "")).strip() == target), None)


def _resolve_docx_from_material_record(record: dict[str, Any]) -> Path | None:
    raw = str(record.get("file_path", "")).strip()
    source = Path(raw)
    if not source.exists():
        # 兼容从其他工作目录启动服务时的相对路径
        base = Path(__file__).resolve().parent
        alt = base / raw
        if alt.exists():
            source = alt
        else:
            return None
    if source.suffix.lower() == ".docx":
        return source
    if source.suffix.lower() in {".txt", ".md"}:
        candidate = source.with_suffix(".docx")
        if candidate.exists():
            return candidate
        text_data = source.read_text(encoding="utf-8", errors="ignore")
        _text_to_docx(text_data, candidate)
        return candidate
    return None


def _resolve_reference_file_for_material(tenant_id: str, material_version_id: str) -> Path | None:
    for ext in (".xlsx", ".xls"):
        p = _material_history_copy_path(tenant_id, material_version_id, ext)
        if p.exists():
            return p
    ref_dir = _material_reference_dir(tenant_id)
    ref_file = next((p for p in sorted(ref_dir.glob(f"{material_version_id}_*"), reverse=True) if p.is_file()), None)
    return ref_file


def _cleanup_material_artifacts(tenant_id: str, material_version_id: str) -> dict[str, int]:
    deleted_files = 0

    upload_dir = _material_upload_dir(tenant_id)
    for p in upload_dir.glob(f"{material_version_id}_*"):
        if p.is_file():
            p.unlink(missing_ok=True)
            deleted_files += 1

    ref_dir = _material_reference_dir(tenant_id)
    for p in ref_dir.glob(f"{material_version_id}_*"):
        if p.is_file():
            p.unlink(missing_ok=True)
            deleted_files += 1

    slices_file = tenant_slices_dir(tenant_id) / f"knowledge_slices_{material_version_id}.jsonl"
    if slices_file.exists():
        slices_file.unlink(missing_ok=True)
        deleted_files += 1
    img_dir = _material_slice_image_dir(tenant_id, material_version_id, create=False)
    if img_dir.exists():
        shutil.rmtree(img_dir, ignore_errors=True)

    mapping_dir = tenant_root(tenant_id) / "mapping"
    for p in (
        mapping_dir / f"knowledge_question_mapping_{material_version_id}.json",
        mapping_dir / f"knowledge_question_mapping_{material_version_id}.jsonl",
    ):
        if p.exists():
            p.unlink(missing_ok=True)
            deleted_files += 1

    for ext in (".xlsx", ".xls"):
        p = _material_history_copy_path(tenant_id, material_version_id, ext)
        if p.exists():
            p.unlink(missing_ok=True)
            deleted_files += 1

    _delete_material_bucket(_slice_review_file_by_material(tenant_id), material_version_id)
    _delete_material_bucket(_mapping_review_file_by_material(tenant_id), material_version_id)

    bank_removed = 0
    bank_path = tenant_bank_path(tenant_id)
    if bank_path.exists():
        rows = _load_bank(bank_path)
        next_rows = []
        for row in rows:
            if str(row.get("教材版本ID", "")).strip() == material_version_id:
                bank_removed += 1
                continue
            next_rows.append(row)
        if bank_removed > 0:
            _save_bank(bank_path, next_rows)

    return {"deleted_files": deleted_files, "deleted_bank_questions": bank_removed}


def _text_to_docx(text: str, output_docx: Path) -> None:
    from docx import Document  # lazy import

    doc = Document()
    for line in text.splitlines():
        if line.strip():
            doc.add_paragraph(line.rstrip())
    doc.save(str(output_docx))


def _sha256_file(path: Path) -> str:
    h = sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _parse_difficulty_range(label: str):
    if not label or label == "随机":
        return None
    m = re.search(r"\(([\d.]+)-([\d.]+)\)", str(label))
    if not m:
        return None
    return (float(m.group(1)), float(m.group(2)))


def _is_slice_deleted(item: Any) -> bool:
    return isinstance(item, dict) and bool(item.get("__deleted__", False))


_INVISIBLE_SEG_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")


def _clean_path_seg(seg: str) -> str:
    s = str(seg or "")
    s = _INVISIBLE_SEG_RE.sub("", s)
    return s.strip()


def _split_clean_path(path: Any) -> list[str]:
    return [x for x in (_clean_path_seg(p) for p in str(path or "").split(" > ")) if x]


def _path_prefix(path: Any, levels: int = 3) -> str:
    segs = _split_clean_path(path)
    return " > ".join(segs[: max(1, int(levels))])

_L4_PARENT_RE = re.compile(r"^(?:[一二三四五六七八九十百千万]+[、.]|\d+[、.])")
_L5_CHILD_RE = re.compile(r"^（(?:[一二三四五六七八九十百千万]+|\d+)）")


def _is_l4_parent_heading(seg: str) -> bool:
    return bool(_L4_PARENT_RE.match(str(seg or "").strip()))


def _is_l5_child_heading(seg: str) -> bool:
    return bool(_L5_CHILD_RE.match(str(seg or "").strip()))


def _build_display_paths(kb_items: list[dict[str, Any]]) -> list[str]:
    """
    Build display paths without mutating source data.
    If legacy data flattened a level-4 parent (e.g. "三、..."), restore it from nearby context.
    """
    out: list[str] = []
    latest_l4_by_p3: dict[str, str] = {}
    for s in kb_items:
        raw_path = str((s or {}).get("完整路径", "") or "").strip()
        segs = _split_clean_path(raw_path)
        if len(segs) >= 4:
            p3 = " > ".join(segs[:3])
            seg4 = segs[3]
            if _is_l4_parent_heading(seg4):
                latest_l4_by_p3[p3] = seg4
            elif _is_l5_child_heading(seg4):
                parent = latest_l4_by_p3.get(p3)
                if parent:
                    segs = [*segs[:3], parent, *segs[3:]]
        out.append(" > ".join(segs) if segs else _clean_path_seg(raw_path))
    return out


def _slice_order_file_by_material(tenant_id: str) -> Path:
    path = tenant_root(tenant_id) / "slices" / "slice_order_by_material.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_slice_order_for_material(tenant_id: str, material_version_id: str) -> dict[str, list[int]]:
    path = _slice_order_file_by_material(tenant_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    bucket = payload.get(material_version_id, {})
    if not isinstance(bucket, dict):
        return {}
    out: dict[str, list[int]] = {}
    for k, v in bucket.items():
        if not isinstance(v, list):
            continue
        ids = []
        for sid in v:
            try:
                ids.append(int(sid))
            except (TypeError, ValueError):
                continue
        out[str(k)] = ids
    return out


def _save_slice_order_for_material(tenant_id: str, material_version_id: str, bucket: dict[str, list[int]]) -> None:
    path = _slice_order_file_by_material(tenant_id)
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                payload = old
        except json.JSONDecodeError:
            payload = {}
    payload[material_version_id] = bucket
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_bank(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _save_bank(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items), encoding="utf-8")


def _qa_dir(tenant_id: str) -> Path:
    path = tenant_root(tenant_id) / "audit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _qa_runs_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "qa_runs.jsonl"


def _qa_alerts_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "qa_alerts.jsonl"


def _qa_thresholds_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "qa_thresholds.json"


def _qa_pricing_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "qa_pricing.json"


def _qa_gen_tasks_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "gen_tasks.jsonl"


GEN_TASKS: dict[str, dict[str, Any]] = {}
GEN_TASK_LOCK = threading.Lock()
GEN_TASK_KEEP = 200
QA_PERSIST_LOCK = threading.Lock()


def _task_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(task)


def _prune_task_cache() -> None:
    if len(GEN_TASKS) <= GEN_TASK_KEEP:
        return
    rows = sorted(
        GEN_TASKS.values(),
        key=lambda x: str(x.get("updated_at", "") or x.get("created_at", "")),
        reverse=True,
    )
    keep_ids = {str(x.get("task_id", "")) for x in rows[:GEN_TASK_KEEP]}
    for tid in list(GEN_TASKS.keys()):
        if tid not in keep_ids:
            GEN_TASKS.pop(tid, None)


def _make_gen_task(tenant_id: str, system_user: str, body: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    task_id = f"task_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    task = {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "task_name": str(body.get("task_name", "")).strip(),
        "creator": system_user,
        "created_at": now,
        "updated_at": now,
        "started_at": "",
        "ended_at": "",
        "status": "pending",  # pending | running | completed | failed
        "request": {
            "task_name": str(body.get("task_name", "")).strip(),
            "gen_scope_mode": str(body.get("gen_scope_mode", "custom")),
            "num_questions": int(body.get("num_questions", 1) or 1),
            "question_type": str(body.get("question_type", "单选题")),
            "generation_mode": str(body.get("generation_mode", "灵活")),
            "difficulty": str(body.get("difficulty", "随机")),
            "save_to_bank": bool(body.get("save_to_bank", True)),
            "slice_ids": [int(x) for x in (body.get("slice_ids") or []) if str(x).isdigit()],
            "material_version_id": str(body.get("material_version_id", "")).strip(),
        },
        "run_id": "",
        "material_version_id": str(body.get("material_version_id", "")).strip(),
        "process_trace": [],
        "items": [],
        "errors": [],
        "generated_count": 0,
        "saved_count": 0,
        "error_count": 0,
        "progress": {"current": 0, "total": 0},
    }
    with GEN_TASK_LOCK:
        GEN_TASKS[task_id] = task
        _prune_task_cache()
        return _task_snapshot(task)


def _merge_task_trace_by_index(base: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_index: dict[int, dict[str, Any]] = {}
    for item in (base or []):
        idx = int(item.get("index", 0) or 0) if isinstance(item, dict) else 0
        if not idx:
            continue
        row = dict(item)
        row["steps"] = list(item.get("steps") or [])
        by_index[idx] = row
    for item in (incoming or []):
        if not isinstance(item, dict):
            continue
        idx = int(item.get("index", 0) or 0)
        if not idx:
            continue
        prev = by_index.get(idx, {"index": idx, "steps": []})
        merged = {**prev, **item}
        steps = []
        seen: set[str] = set()
        for s in list(prev.get("steps") or []) + list(item.get("steps") or []):
            if not isinstance(s, dict):
                continue
            key = f"{s.get('seq','')}|{s.get('node','')}|{s.get('message','')}|{s.get('detail','')}"
            if key in seen:
                continue
            seen.add(key)
            steps.append(s)
        merged["steps"] = steps
        by_index[idx] = merged
    return [by_index[k] for k in sorted(by_index.keys())]


def _update_task_live(tenant_id: str, task_id: str, patch: dict[str, Any], trace_updates: list[dict[str, Any]] | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with GEN_TASK_LOCK:
        task = GEN_TASKS.get(task_id)
        if not task or task.get("tenant_id") != tenant_id:
            return
        task.update(patch or {})
        if trace_updates:
            task["process_trace"] = _merge_task_trace_by_index(task.get("process_trace") or [], trace_updates)
        task["error_count"] = len(task.get("errors") or [])
        task["updated_at"] = now


def _persist_gen_task(tenant_id: str, task: dict[str, Any]) -> None:
    _append_jsonl(_qa_gen_tasks_path(tenant_id), task)

def _default_qa_thresholds() -> dict[str, Any]:
    return {
        "hard_pass_rate_min": 1.0,
        "logic_pass_rate_min": 0.95,
        "out_of_scope_rate_max": 0.02,
        "duplicate_rate_max": 0.03,
        "avg_distractor_score_min": 3.5,
        "avg_critic_loops_max": 2.0,
        "risk_high_rate_max": 0.03,
        "avg_tokens_per_question_max": 3000,
        "avg_latency_ms_per_question_max": 10000,
        "avg_cost_per_question_max": 1.5,
        "sla_hours_high": 24,
        "sla_hours_medium": 72,
        "sla_hours_low": 168,
    }


def _configured_models_from_key_file() -> list[str]:
    cfg_path = Path("填写您的Key.txt")
    if not cfg_path.exists():
        return []
    models: list[str] = []
    try:
        for line in cfg_path.read_text(encoding="utf-8").splitlines():
            raw = str(line).strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            name = str(key).strip()
            model = str(value).strip()
            if name not in {"OPENAI_MODEL", "DEEPSEEK_MODEL", "CRITIC_MODEL", "CODE_GEN_MODEL", "IMAGE_MODEL"}:
                continue
            if model and model not in models:
                models.append(model)
    except Exception:
        return []
    return models


def _recent_models_from_runs(tenant_id: str, limit: int = 100) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    for run in reversed(_read_jsonl(_qa_runs_path(tenant_id))):
        calls = run.get("llm_calls") if isinstance(run, dict) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            model = str(call.get("model", "")).strip()
            if not model or model in seen:
                continue
            seen.add(model)
            models.append(model)
            if len(models) >= limit:
                return models
    return models


def _default_qa_pricing(tenant_id: str) -> dict[str, Any]:
    model_names: list[str] = []
    for model in _configured_models_from_key_file() + _recent_models_from_runs(tenant_id):
        if model and model not in model_names:
            model_names.append(model)
    if not model_names:
        model_names = ["doubao-seed-1.8"]
    return {
        "currency": "CNY",
        "default_prompt_per_1k": 0.01,
        "default_completion_per_1k": 0.03,
        "models": {
            model: {"prompt_per_1k": 0.01, "completion_per_1k": 0.03}
            for model in model_names
        },
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _safe_div(a: float, b: float) -> float:
    if not b:
        return 0.0
    return float(a) / float(b)


def _load_qa_thresholds(tenant_id: str) -> dict[str, Any]:
    path = _qa_thresholds_path(tenant_id)
    defaults = _default_qa_thresholds()
    if not path.exists():
        return defaults
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return defaults
    if not isinstance(payload, dict):
        return defaults
    out = dict(defaults)
    out.update(payload)
    return out


def _save_qa_thresholds(tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    merged = _default_qa_thresholds()
    merged.update(payload or {})
    _qa_thresholds_path(tenant_id).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return merged


def _load_qa_pricing(tenant_id: str) -> dict[str, Any]:
    path = _qa_pricing_path(tenant_id)
    defaults = _default_qa_pricing(tenant_id)
    active_models = set(defaults.get("models", {}).keys())
    legacy_defaults = {"gpt-4o-mini", "deepseek-chat", "deepseek-reasoner", "qwen-plus"}
    if not path.exists():
        return defaults
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return defaults
    if not isinstance(payload, dict):
        return defaults
    out = dict(defaults)
    out.update(payload)
    models = defaults.get("models", {}).copy()
    if isinstance(payload.get("models"), dict):
        for mk, mv in payload.get("models", {}).items():
            if isinstance(mv, dict):
                model_name = str(mk).strip()
                if not model_name:
                    continue
                if model_name in legacy_defaults and model_name not in active_models:
                    continue
                models[model_name] = {
                    "prompt_per_1k": float(mv.get("prompt_per_1k", models.get(model_name, {}).get("prompt_per_1k", out["default_prompt_per_1k"])) or 0.0),
                    "completion_per_1k": float(mv.get("completion_per_1k", models.get(model_name, {}).get("completion_per_1k", out["default_completion_per_1k"])) or 0.0),
                }
    out["models"] = models
    return out


def _save_qa_pricing(tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    merged = _load_qa_pricing(tenant_id)
    if isinstance(payload, dict):
        for key in ("currency", "default_prompt_per_1k", "default_completion_per_1k"):
            if key in payload:
                merged[key] = payload[key]
        if isinstance(payload.get("models"), dict):
            models = dict(merged.get("models", {}))
            for mk, mv in payload.get("models", {}).items():
                if isinstance(mv, dict):
                    models[str(mk)] = {
                        "prompt_per_1k": float(mv.get("prompt_per_1k", models.get(str(mk), {}).get("prompt_per_1k", merged["default_prompt_per_1k"])) or 0.0),
                        "completion_per_1k": float(mv.get("completion_per_1k", models.get(str(mk), {}).get("completion_per_1k", merged["default_completion_per_1k"])) or 0.0),
                    }
            merged["models"] = models
    _qa_pricing_path(tenant_id).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return merged


def _call_cost(call: dict[str, Any], pricing: dict[str, Any]) -> float:
    model = str(call.get("model", "") or "")
    models = pricing.get("models") if isinstance(pricing.get("models"), dict) else {}
    cfg = models.get(model) if isinstance(models.get(model), dict) else {}
    prompt_rate = float(cfg.get("prompt_per_1k", pricing.get("default_prompt_per_1k", 0.0)) or 0.0)
    completion_rate = float(cfg.get("completion_per_1k", pricing.get("default_completion_per_1k", 0.0)) or 0.0)
    prompt_tokens = float(call.get("prompt_tokens", 0) or 0)
    completion_tokens = float(call.get("completion_tokens", 0) or 0)
    cost = (prompt_tokens / 1000.0) * prompt_rate + (completion_tokens / 1000.0) * completion_rate
    return round(max(0.0, cost), 6)


def _parse_iso_ts(val: str) -> datetime | None:
    s = str(val or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def _score_question_from_trace(question_trace: dict[str, Any]) -> dict[str, Any]:
    critic_result = question_trace.get("critic_result") if isinstance(question_trace.get("critic_result"), dict) else {}
    llm_summary = question_trace.get("llm_summary") if isinstance(question_trace.get("llm_summary"), dict) else {}
    unstable_flags = [str(x) for x in (question_trace.get("unstable_flags") or []) if str(x)]
    all_issues = [str(x) for x in (critic_result.get("all_issues") or []) if str(x)]
    quality_issues = [str(x) for x in (critic_result.get("quality_issues") or []) if str(x)]
    missing_conditions = [str(x) for x in (critic_result.get("missing_conditions") or []) if str(x)]
    can_deduce_unique = bool(critic_result.get("can_deduce_unique_answer", False))
    passed = bool(critic_result.get("passed", False))
    saved = bool(question_trace.get("saved", False))
    hard_pass = bool(passed and saved)

    logic_score = 100
    if not can_deduce_unique:
        logic_score -= 50
    if missing_conditions:
        logic_score -= 20
    if any("answer_mismatch" in x for x in all_issues):
        logic_score -= 20
    if any("grounding" in x for x in all_issues):
        logic_score -= 15
    logic_score = max(0, min(100, logic_score))

    distractor_score = 5.0
    if any("干扰项" in x for x in quality_issues):
        distractor_score = 2.0
    elif any("option_dimension" in x for x in all_issues):
        distractor_score = 2.5
    elif quality_issues:
        distractor_score = 3.5

    knowledge_match_score = 1.0
    if any("超纲" in x for x in all_issues + quality_issues):
        knowledge_match_score = 0.0
    elif any("grounding" in x for x in all_issues):
        knowledge_match_score = 0.5

    teaching_value_score = 4.5
    if any(("实用" in x or "业务" in x) for x in quality_issues):
        teaching_value_score = 2.5
    elif quality_issues:
        teaching_value_score = 3.5

    risk_tags: list[str] = []
    if any("超纲" in x for x in all_issues + quality_issues):
        risk_tags.append("out_of_scope")
    if any("example_conflict" in x for x in all_issues):
        risk_tags.append("example_conflict")
    if any("answer_mismatch" in x for x in all_issues):
        risk_tags.append("answer_mismatch")
    if not can_deduce_unique:
        risk_tags.append("ambiguous")
    if any("重复题目" in str(critic_result.get("reason", "")) for _ in [0]):
        risk_tags.append("duplicate")
    if unstable_flags:
        risk_tags.append("unstable_generation")

    risk_level = "low"
    if any(tag in {"out_of_scope", "answer_mismatch", "ambiguous"} for tag in risk_tags):
        risk_level = "high"
    elif risk_tags:
        risk_level = "medium"

    steps = question_trace.get("steps") or []
    critic_reject_count = 0
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and "审核驳回" in str(step.get("message", "")):
                critic_reject_count += 1

    stability = {
        "critic_loops": critic_reject_count + (1 if passed else 0),
        "llm_calls": int(llm_summary.get("total_llm_calls") or 0),
        "tokens": int(llm_summary.get("total_tokens") or 0),
        "latency_ms": int(question_trace.get("elapsed_ms") or 0),
        "unstable_question": bool(unstable_flags),
        "unstable_flags": unstable_flags,
        "error_calls": int(llm_summary.get("error_calls") or 0),
    }

    final_json = question_trace.get("final_json") if isinstance(question_trace.get("final_json"), dict) else {}
    return {
        "question_id": str(question_trace.get("question_id", "")),
        "index": int(question_trace.get("index", 0) or 0),
        "slice_id": question_trace.get("slice_id"),
        "slice_path": str(question_trace.get("slice_path", "")),
        "hard_gate": {
            "pass": hard_pass,
            "failed_rules": [] if hard_pass else [str(critic_result.get("reason", "hard_gate_failed"))],
        },
        "quality": {
            "logic_score": logic_score,
            "distractor_score": round(distractor_score, 2),
            "knowledge_match_score": round(knowledge_match_score, 2),
            "teaching_value_score": round(teaching_value_score, 2),
        },
        "risk": {
            "level": risk_level,
            "tags": risk_tags,
            "ambiguity": 1 if "ambiguous" in risk_tags else 0,
        },
        "stability": stability,
        "issues": {
            "quality_issues": quality_issues,
            "missing_conditions": missing_conditions,
            "all_issues": all_issues,
            "fix_strategy": str(critic_result.get("fix_strategy", "")),
            "reason": str(critic_result.get("reason", "")),
        },
        "critic_result": critic_result,
        "llm_summary": llm_summary,
        "question_text": str(final_json.get("题干", "")),
        "answer": str(final_json.get("正确答案", "")),
    }


def _build_qa_run_payload(
    *,
    tenant_id: str,
    run_id: str,
    material_version_id: str,
    config_payload: dict[str, Any],
    process_trace: list[dict[str, Any]],
    generated_count: int,
    saved_count: int,
    errors: list[str],
    started_at: str,
    ended_at: str,
) -> dict[str, Any]:
    pricing = _load_qa_pricing(tenant_id)
    currency = str(pricing.get("currency", "CNY"))
    questions = [_score_question_from_trace(x) for x in (process_trace or []) if isinstance(x, dict)]
    llm_calls: list[dict[str, Any]] = []
    for q in process_trace or []:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("question_id", ""))
        for c in (q.get("llm_trace") or []):
            if isinstance(c, dict):
                item = dict(c)
                if not item.get("question_id"):
                    item["question_id"] = qid
                item["cost_estimate"] = _call_cost(item, pricing)
                item["currency"] = currency
                llm_calls.append(item)

    by_model_cost: dict[str, float] = {}
    by_node_cost: dict[str, float] = {}
    by_question_cost: dict[str, float] = {}
    total_cost = 0.0
    for c in llm_calls:
        model = str(c.get("model", "unknown") or "unknown")
        node = str(c.get("node", "unknown") or "unknown")
        qid = str(c.get("question_id", "") or "")
        cost = float(c.get("cost_estimate", 0.0) or 0.0)
        total_cost += cost
        by_model_cost[model] = round(by_model_cost.get(model, 0.0) + cost, 6)
        by_node_cost[node] = round(by_node_cost.get(node, 0.0) + cost, 6)
        if qid:
            by_question_cost[qid] = round(by_question_cost.get(qid, 0.0) + cost, 6)

    for q in questions:
        qid = str(q.get("question_id", "") or "")
        c = float(by_question_cost.get(qid, 0.0) or 0.0)
        stability = q.get("stability") if isinstance(q.get("stability"), dict) else {}
        stability["cost_estimate"] = round(c, 6)
        stability["currency"] = currency
        q["stability"] = stability

    n = len(questions)
    hard_pass_cnt = sum(1 for q in questions if bool(q.get("hard_gate", {}).get("pass")))
    logic_pass_cnt = sum(1 for q in questions if float(q.get("quality", {}).get("logic_score", 0)) >= 80)
    out_of_scope_cnt = sum(1 for q in questions if "out_of_scope" in (q.get("risk", {}).get("tags") or []))
    duplicate_cnt = sum(1 for q in questions if "duplicate" in (q.get("risk", {}).get("tags") or []))
    high_risk_cnt = sum(1 for q in questions if str(q.get("risk", {}).get("level")) == "high")
    unstable_cnt = sum(1 for q in questions if bool(q.get("stability", {}).get("unstable_question")))
    avg_distractor = _safe_div(sum(float(q.get("quality", {}).get("distractor_score", 0) or 0) for q in questions), n)
    avg_knowledge = _safe_div(sum(float(q.get("quality", {}).get("knowledge_match_score", 0) or 0) for q in questions), n)
    avg_logic = _safe_div(sum(float(q.get("quality", {}).get("logic_score", 0) or 0) for q in questions), n)
    avg_teaching = _safe_div(sum(float(q.get("quality", {}).get("teaching_value_score", 0) or 0) for q in questions), n)
    avg_tokens = _safe_div(sum(int(q.get("stability", {}).get("tokens", 0) or 0) for q in questions), n)
    avg_latency_ms = _safe_div(sum(int(q.get("stability", {}).get("latency_ms", 0) or 0) for q in questions), n)
    avg_critic_loops = _safe_div(sum(int(q.get("stability", {}).get("critic_loops", 0) or 0) for q in questions), n)
    avg_cost = _safe_div(sum(float(q.get("stability", {}).get("cost_estimate", 0.0) or 0.0) for q in questions), n)
    error_calls = sum(int((q.get("llm_summary") or {}).get("error_calls", 0) or 0) for q in questions)
    total_calls = sum(int((q.get("llm_summary") or {}).get("total_llm_calls", 0) or 0) for q in questions)

    batch_metrics = {
        "question_count": n,
        "generated_count": int(generated_count or 0),
        "saved_count": int(saved_count or 0),
        "error_count": len(errors or []),
        "hard_pass_rate": round(_safe_div(hard_pass_cnt, n), 4),
        "quality_score_avg": round((avg_logic * 0.5) + (avg_distractor * 10 * 0.15) + (avg_knowledge * 100 * 0.2) + (avg_teaching * 10 * 0.15), 2),
        "logic_pass_rate": round(_safe_div(logic_pass_cnt, n), 4),
        "out_of_scope_rate": round(_safe_div(out_of_scope_cnt, n), 4),
        "duplicate_rate": round(_safe_div(duplicate_cnt, n), 4),
        "risk_high_rate": round(_safe_div(high_risk_cnt, n), 4),
        "unstable_rate": round(_safe_div(unstable_cnt, n), 4),
        "avg_distractor_score": round(avg_distractor, 3),
        "knowledge_match_rate": round(avg_knowledge, 4),
        "avg_logic_score": round(avg_logic, 2),
        "avg_teaching_value_score": round(avg_teaching, 3),
        "avg_tokens_per_question": round(avg_tokens, 2),
        "avg_latency_ms_per_question": round(avg_latency_ms, 2),
        "avg_critic_loops": round(avg_critic_loops, 3),
        "total_cost": round(total_cost, 6),
        "avg_cost_per_question": round(avg_cost, 6),
        "avg_cost_per_call": round(_safe_div(total_cost, len(llm_calls)), 6),
        "currency": currency,
        "error_calls": int(error_calls),
        "total_llm_calls": int(total_calls),
        "error_call_rate": round(_safe_div(error_calls, total_calls), 4),
    }
    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "material_version_id": material_version_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "config": config_payload,
        "batch_metrics": batch_metrics,
        "questions": questions,
        "llm_calls": llm_calls,
        "cost_summary": {
            "currency": currency,
            "total_cost": round(total_cost, 6),
            "by_model": by_model_cost,
            "by_node": by_node_cost,
            "by_question": by_question_cost,
        },
        "errors": errors or [],
        "trace_count": len(process_trace or []),
    }


def _build_alerts_for_run(qa_run: dict[str, Any], thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    run_id = str(qa_run.get("run_id", ""))
    metrics = qa_run.get("batch_metrics") if isinstance(qa_run.get("batch_metrics"), dict) else {}
    ended_at = str(qa_run.get("ended_at", datetime.now(timezone.utc).isoformat()))
    created_dt = _parse_iso_ts(ended_at) or datetime.now(timezone.utc)
    check_pairs = [
        ("hard_pass_rate", "hard_pass_rate_min", "below_min"),
        ("logic_pass_rate", "logic_pass_rate_min", "below_min"),
        ("avg_distractor_score", "avg_distractor_score_min", "below_min"),
        ("out_of_scope_rate", "out_of_scope_rate_max", "above_max"),
        ("duplicate_rate", "duplicate_rate_max", "above_max"),
        ("risk_high_rate", "risk_high_rate_max", "above_max"),
        ("avg_critic_loops", "avg_critic_loops_max", "above_max"),
        ("avg_tokens_per_question", "avg_tokens_per_question_max", "above_max"),
        ("avg_latency_ms_per_question", "avg_latency_ms_per_question_max", "above_max"),
        ("avg_cost_per_question", "avg_cost_per_question_max", "above_max"),
    ]
    for metric_key, threshold_key, mode in check_pairs:
        mv = metrics.get(metric_key)
        tv = thresholds.get(threshold_key)
        if mv is None or tv is None:
            continue
        trigger = (mode == "below_min" and float(mv) < float(tv)) or (mode == "above_max" and float(mv) > float(tv))
        if not trigger:
            continue
        level = "high" if metric_key in {"hard_pass_rate", "logic_pass_rate", "risk_high_rate"} else "medium"
        sla_hours = int(thresholds.get(f"sla_hours_{level}", thresholds.get("sla_hours_medium", 72)) or 72)
        due_at = (created_dt + timedelta(hours=sla_hours)).isoformat()
        alerts.append(
            {
                "alert_id": f"alert_{run_id}_{metric_key}",
                "tenant_id": str(qa_run.get("tenant_id", "")),
                "run_id": run_id,
                "question_id": "",
                "level": level,
                "type": "batch_metric",
                "metric": metric_key,
                "threshold_key": threshold_key,
                "metric_value": mv,
                "threshold_value": tv,
                "message": f"{metric_key}={mv} vs {threshold_key}={tv}",
                "status": "open",
                "owner": "",
                "created_at": ended_at,
                "updated_at": ended_at,
                "sla_hours": sla_hours,
                "sla_due_at": due_at,
                "overdue": False,
                "acked_at": "",
                "resolved_at": "",
            }
        )
    for q in qa_run.get("questions") or []:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("question_id", ""))
        risk = q.get("risk") if isinstance(q.get("risk"), dict) else {}
        stability = q.get("stability") if isinstance(q.get("stability"), dict) else {}
        if str(risk.get("level", "")) == "high":
            level = "high"
            sla_hours = int(thresholds.get(f"sla_hours_{level}", thresholds.get("sla_hours_medium", 72)) or 72)
            due_at = (created_dt + timedelta(hours=sla_hours)).isoformat()
            alerts.append(
                {
                    "alert_id": f"alert_{run_id}_{qid}_risk",
                    "tenant_id": str(qa_run.get("tenant_id", "")),
                    "run_id": run_id,
                    "question_id": qid,
                    "level": "high",
                    "type": "question_risk",
                    "metric": "risk.level",
                    "threshold_key": "risk_high_rate_max",
                    "metric_value": "high",
                    "threshold_value": "low/medium preferred",
                    "message": f"question {qid} risk high",
                    "status": "open",
                    "owner": "",
                    "created_at": ended_at,
                    "updated_at": ended_at,
                    "sla_hours": sla_hours,
                    "sla_due_at": due_at,
                    "overdue": False,
                    "acked_at": "",
                    "resolved_at": "",
                }
            )
        if bool(stability.get("unstable_question", False)):
            level = "medium"
            sla_hours = int(thresholds.get(f"sla_hours_{level}", thresholds.get("sla_hours_medium", 72)) or 72)
            due_at = (created_dt + timedelta(hours=sla_hours)).isoformat()
            alerts.append(
                {
                    "alert_id": f"alert_{run_id}_{qid}_unstable",
                    "tenant_id": str(qa_run.get("tenant_id", "")),
                    "run_id": run_id,
                    "question_id": qid,
                    "level": "medium",
                    "type": "question_unstable",
                    "metric": "stability.unstable_question",
                    "threshold_key": "",
                    "metric_value": True,
                    "threshold_value": False,
                    "message": f"question {qid} unstable",
                    "status": "open",
                    "owner": "",
                    "created_at": ended_at,
                    "updated_at": ended_at,
                    "sla_hours": sla_hours,
                    "sla_due_at": due_at,
                    "overdue": False,
                    "acked_at": "",
                    "resolved_at": "",
                }
            )
    return alerts


def _persist_qa_run(tenant_id: str, qa_run: dict[str, Any]) -> None:
    with QA_PERSIST_LOCK:
        _append_jsonl(_qa_runs_path(tenant_id), qa_run)
        thresholds = _load_qa_thresholds(tenant_id)
        alerts = _build_alerts_for_run(qa_run, thresholds)
        for alert in alerts:
            _append_jsonl(_qa_alerts_path(tenant_id), alert)


def _save_kb_items_to_file(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items), encoding="utf-8")


def _tenant_has_data(tenant_id: str) -> bool:
    root = tenant_root(tenant_id)
    if not root.exists():
        return False
    for p in root.rglob("*"):
        if p.is_file():
            return True
    return False


@app.before_request
def _handle_options():
    if request.method == 'OPTIONS':
        return _json_response({'ok': True}, 200)
    try:
        principal = resolve_principal(
            authorization_header=(request.headers.get("Authorization") or ""),
            system_user_header=(request.headers.get("X-System-User") or ""),
        )
    except AccessDenied as e:
        return _error(str(e), "认证失败，请检查系统号或 OIDC Token", 401)
    g.principal = principal
    g.request_id = f"{principal.system_user}-{os.getpid()}-{int(os.times().elapsed * 1000)}"
    g.release_channel = select_release_channel(
        principal.system_user,
        forced_channel=(request.headers.get("X-Release-Channel") or ""),
    )
    api_key = f"{principal.system_user}:{request.path}"
    allowed, retry_after = rate_limiter.allow(api_key)
    if not allowed:
        resp = _error("RATE_LIMITED", f"请求过于频繁，请 {retry_after}s 后重试", 429)
        resp.headers["Retry-After"] = str(retry_after)
        return resp
    if not circuit_breaker.allow(request.path):
        return _error("CIRCUIT_OPEN", "服务正在自动恢复中，请稍后重试", 503)
    return None


@app.after_request
def _record_success(response):
    if response.status_code < 500:
        circuit_breaker.record_success(request.path)
    return response


@app.errorhandler(Exception)
def _unhandled_error(err: Exception):
    if isinstance(err, HTTPException):
        return err
    circuit_breaker.record_failure(request.path)
    return _error("INTERNAL_ERROR", f"服务异常: {type(err).__name__}", 500)


@app.get('/api/meta')
def api_meta():
    principal = _get_principal()
    return _json_response(
        {
            "auth_mode": os.getenv("AUTH_MODE", "legacy").strip().lower(),
            "system_user": principal.system_user,
            "role": principal.role,
            "tenants": principal.tenants,
            "release_channel": getattr(g, "release_channel", "stable"),
            "features": {
                "oidc": os.getenv("AUTH_MODE", "legacy").strip().lower() == "oidc",
                "rate_limit_rpm": int(os.getenv("ADMIN_API_RATE_LIMIT_RPM", "240")),
                "circuit_breaker": True,
                "otel_enabled": bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()),
            },
        }
    )


@app.get('/api/admin/cities')
@app.get('/api/platform/cities')
def api_admin_cities():
    try:
        _require_platform_admin()
    except PermissionError as e:
        return _error(str(e), "仅平台管理员可访问城市管理", 403)
    q = request.args.get("q", "").strip().lower()
    status = request.args.get("status", "all").strip().lower()
    page, page_size = _parse_pagination()
    items = list_tenants()
    if status == "active":
        items = [x for x in items if bool(x.get("is_active", True))]
    elif status == "inactive":
        items = [x for x in items if not bool(x.get("is_active", True))]
    if q:
        items = [
            x for x in items
            if q in str(x.get("tenant_id", "")).lower() or q in str(x.get("name", "")).lower()
        ]
    items.sort(key=lambda x: str(x.get("tenant_id", "")))
    return _json_response(_paginate(items, page, page_size))


@app.post('/api/admin/cities')
@app.post('/api/platform/cities')
def api_admin_cities_upsert():
    try:
        _require_platform_admin()
    except PermissionError as e:
        return _error(str(e), "仅平台管理员可操作城市管理", 403)
    body = request.get_json(silent=True) or {}
    tenant_id = str(body.get("tenant_id", "")).strip().lower()
    name = str(body.get("name", "")).strip()
    is_active = body.get("is_active")
    if not tenant_id:
        return _error("BAD_REQUEST", "tenant_id is required", 400)
    if not re.fullmatch(r"[a-z0-9_-]{2,32}", tenant_id):
        return _error("BAD_REQUEST", "tenant_id 仅支持小写字母/数字/_/-，长度2-32", 400)
    item = upsert_tenant(tenant_id, name or tenant_id, None if is_active is None else _parse_bool_arg(is_active, True))
    return _json_response({"item": item})


@app.post('/api/admin/cities/<tenant_id>/status')
@app.post('/api/platform/cities/<tenant_id>/status')
def api_admin_cities_status(tenant_id: str):
    try:
        _require_platform_admin()
    except PermissionError as e:
        return _error(str(e), "仅平台管理员可操作城市管理", 403)
    body = request.get_json(silent=True) or {}
    is_active = body.get("is_active")
    if is_active is None:
        return _error("BAD_REQUEST", "is_active is required", 400)
    try:
        item = set_tenant_status(tenant_id, _parse_bool_arg(is_active, True))
    except KeyError:
        return _error("TENANT_NOT_FOUND", "城市不存在", 404)
    return _json_response({"item": item})


@app.delete('/api/admin/cities/<tenant_id>')
@app.delete('/api/platform/cities/<tenant_id>')
def api_admin_cities_delete(tenant_id: str):
    try:
        _require_platform_admin()
    except PermissionError as e:
        return _error(str(e), "仅平台管理员可操作城市管理", 403)
    tid = str(tenant_id).strip().lower()
    if not tid:
        return _error("BAD_REQUEST", "tenant_id is required", 400)
    known_tenants = {x.get("tenant_id") for x in list_tenants()}
    if tid not in known_tenants:
        return _error("TENANT_NOT_FOUND", "城市不存在", 404)
    force = _parse_bool_arg(request.args.get("force"), False)
    acl = load_acl()
    bound_users: list[str] = []
    for system_user, profile in acl.items():
        tenant_ids = [str(x).strip() for x in profile.get("tenants", [])]
        if tid in tenant_ids:
            bound_users.append(system_user)
    if bound_users and not force:
        return _error("TENANT_BOUND", f"城市已绑定系统号: {', '.join(bound_users[:5])}", 400)
    if _tenant_has_data(tid) and not force:
        return _error("TENANT_HAS_DATA", "城市存在业务数据，请传 force=1 后重试", 400)
    delete_tenant(tid)
    root = tenant_root(tid)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    updated_acl: dict[str, dict] = {}
    for system_user, profile in acl.items():
        tenant_ids = [str(x).strip() for x in profile.get("tenants", []) if str(x).strip() != tid]
        role = str(profile.get("role", "city_viewer"))
        if role != "platform_admin" and not tenant_ids:
            continue
        updated_acl[system_user] = {"role": role, "tenants": tenant_ids}
    save_acl(updated_acl)
    return _json_response({"ok": True})


@app.get('/api/admin/users')
@app.get('/api/platform/users')
def api_admin_users():
    try:
        _require_platform_admin()
    except PermissionError as e:
        return _error(str(e), "仅平台管理员可管理系统号", 403)
    q = request.args.get("q", "").strip().lower()
    role_filter = request.args.get("role", "").strip()
    page, page_size = _parse_pagination()
    acl = load_acl()
    users = []
    for system_user, profile in acl.items():
        role = str(profile.get("role", "city_viewer"))
        if role_filter and role_filter != "all" and role != role_filter:
            continue
        if q and q not in system_user.lower():
            continue
        users.append(
            {
                "system_user": system_user,
                "role": role,
                "tenants": list(profile.get("tenants", [])),
            }
        )
    users.sort(key=lambda x: x["system_user"])
    return _json_response(_paginate(users, page, page_size))


@app.post('/api/admin/users/upsert')
@app.post('/api/platform/users/upsert')
def api_admin_users_upsert():
    try:
        _require_platform_admin()
    except PermissionError as e:
        return _error(str(e), "仅平台管理员可管理系统号", 403)
    body = request.get_json(silent=True) or {}
    system_user = str(body.get("system_user", "")).strip()
    role = str(body.get("role", "city_viewer")).strip()
    tenants = body.get("tenants") or []
    if not system_user:
        return _error("BAD_REQUEST", "system_user is required", 400)
    allowed_roles = {"platform_admin", "city_admin", "city_teacher", "city_viewer"}
    if role not in allowed_roles:
        return _error("BAD_REQUEST", "invalid role", 400)
    if not isinstance(tenants, list):
        return _error("BAD_REQUEST", "tenants must be list", 400)
    tenant_ids = {x["tenant_id"] for x in list_tenants() if bool(x.get("is_active", True))}
    valid_tenants = sorted({str(x).strip() for x in tenants if str(x).strip() in tenant_ids})
    if role != "platform_admin" and not valid_tenants:
        return _error("BAD_REQUEST", "非平台管理员必须至少绑定一个城市", 400)
    acl = load_acl()
    acl[system_user] = {"role": role, "tenants": valid_tenants}
    save_acl(acl)
    return _json_response({"item": {"system_user": system_user, "role": role, "tenants": valid_tenants}})


@app.post('/api/admin/users/delete')
@app.post('/api/platform/users/delete')
def api_admin_users_delete():
    try:
        _require_platform_admin()
    except PermissionError as e:
        return _error(str(e), "仅平台管理员可管理系统号", 403)
    body = request.get_json(silent=True) or {}
    system_user = str(body.get("system_user", "")).strip()
    if not system_user:
        return _error("BAD_REQUEST", "system_user is required", 400)
    if system_user == "admin":
        return _error("BAD_REQUEST", "admin 账号不可删除", 400)
    acl = load_acl()
    acl.pop(system_user, None)
    save_acl(acl)
    return _json_response({"ok": True})


@app.post('/api/admin/users/batch-bind')
@app.post('/api/platform/users/batch-bind')
def api_admin_users_batch_bind():
    try:
        _require_platform_admin()
    except PermissionError as e:
        return _error(str(e), "仅平台管理员可管理系统号", 403)
    body = request.get_json(silent=True) or {}
    system_users = body.get("system_users") or []
    tenants = body.get("tenants") or []
    op = str(body.get("op", "add")).strip().lower()
    if not isinstance(system_users, list) or not system_users:
        return _error("BAD_REQUEST", "system_users must be non-empty list", 400)
    if not isinstance(tenants, list) or not tenants:
        return _error("BAD_REQUEST", "tenants must be non-empty list", 400)
    if op not in {"add", "remove", "replace"}:
        return _error("BAD_REQUEST", "op must be add/remove/replace", 400)
    tenant_ids = {x["tenant_id"] for x in list_tenants() if bool(x.get("is_active", True))}
    valid_tenants = {str(x).strip() for x in tenants if str(x).strip() in tenant_ids}
    if not valid_tenants:
        return _error("BAD_REQUEST", "无有效城市", 400)
    acl = load_acl()
    affected = 0
    skipped: list[str] = []
    for u in system_users:
        system_user = str(u).strip()
        if not system_user:
            continue
        profile = acl.get(system_user)
        if not profile:
            skipped.append(system_user)
            continue
        role = str(profile.get("role", "city_viewer"))
        if role == "platform_admin":
            skipped.append(system_user)
            continue
        current = set(str(x).strip() for x in profile.get("tenants", []) if str(x).strip())
        if op == "add":
            current = current | valid_tenants
        elif op == "remove":
            current = current - valid_tenants
        else:
            current = set(valid_tenants)
        if not current:
            skipped.append(system_user)
            continue
        acl[system_user] = {"role": role, "tenants": sorted(current)}
        affected += 1
    save_acl(acl)
    return _json_response({"ok": True, "affected": affected, "skipped": skipped})


@app.get('/api/tenants')
def api_tenants():
    try:
        principal = _get_principal()
        allowed = set(principal.tenants if principal.tenants else get_accessible_tenants(principal.system_user))
    except PermissionError as e:
        return _error(str(e), "缺少或无效系统号", 401)
    include_inactive = _parse_bool_arg(request.args.get("include_inactive"), False)
    items = [x for x in list_tenants() if x["tenant_id"] in allowed]
    if not include_inactive and principal.role != "platform_admin":
        items = [x for x in items if bool(x.get("is_active", True))]
    return _json_response({'items': items})


@app.get('/api/<tenant_id>/slices')
def api_slices(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "slice.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问该城市切片", 403)

    status = request.args.get('status', 'all')
    keyword = request.args.get('keyword', '').strip()
    path_prefix = request.args.get('path_prefix', '').strip()
    requested_material_version_id = str(request.args.get('material_version_id', '')).strip()
    if status != "all" and status not in SLICE_STATUSES:
        return _error("INVALID_STATUS", "非法切片状态", 400)
    page, page_size = _parse_pagination()
    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    with start_span("api.slices", {"tenant_id": tenant_id, "status": status, "material_version_id": material_version_id, "path_prefix": path_prefix}):
        kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
        kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
        if not kb_items:
            return _json_response({"items": [], "total": 0, "page": page, "page_size": page_size, "material_version_id": material_version_id})

        display_paths = _build_display_paths(kb_items)
        reviews = _load_slice_review_for_material(tenant_id, material_version_id)
        items = []
        for i, s in enumerate(kb_items):
            if _is_slice_deleted(s):
                continue
            review = reviews.get(str(i), {})
            r_status = review.get('review_status', 'pending')
            if r_status not in SLICE_STATUSES:
                r_status = 'pending'
            path = display_paths[i] if i < len(display_paths) else str(s.get('完整路径', '') or '')
            if status != 'all' and r_status != status:
                continue
            if keyword and keyword not in path:
                continue
            if path_prefix and not str(path).startswith(path_prefix):
                continue
            full_content = _extract_slice_text(s)
            image_items = _extract_slice_images(s)
            items.append(
                {
                    'slice_id': i,
                    'path': path,
                    'mastery': s.get('掌握程度', ''),
                    'review_status': r_status,
                    'review_comment': review.get('comment', ''),
                    'preview': full_content[:180],
                    'slice_content': full_content,
                    'images': image_items,
                    'material_version_id': material_version_id,
                }
            )
        order_bucket = _load_slice_order_for_material(tenant_id, material_version_id)
        if order_bucket and items:
            group_anchor: dict[str, int] = {}
            rank_map: dict[tuple[str, int], int] = {}
            for p3, ids in order_bucket.items():
                for idx, sid in enumerate(ids):
                    rank_map[(p3, int(sid))] = idx
            for item in items:
                p3 = _path_prefix(item.get("path", ""), 3)
                sid = int(item.get("slice_id", -1))
                anchor = group_anchor.get(p3)
                if anchor is None or sid < anchor:
                    group_anchor[p3] = sid
            items.sort(
                key=lambda x: (
                    group_anchor.get(_path_prefix(x.get("path", ""), 3), int(x.get("slice_id", 0))),
                    rank_map.get(
                        (_path_prefix(x.get("path", ""), 3), int(x.get("slice_id", -1))),
                        1_000_000 + int(x.get("slice_id", 0)),
                    ),
                )
            )
    payload = _paginate(items, page, page_size)
    payload["material_version_id"] = material_version_id
    return _json_response(payload)


@app.get('/api/<tenant_id>/slices/image')
def api_slice_image(tenant_id: str):
    image_path = str(request.args.get("path", "")).strip()
    requested_material_version_id = str(request.args.get("material_version_id", "")).strip()
    if not image_path:
        return _error("BAD_REQUEST", "path is required", 400)
    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    p = Path(image_path)
    candidates: list[Path] = []
    base = Path(__file__).resolve().parent
    if p.is_absolute():
        candidates.append(p)
    candidates.append(base / p)
    if material_version_id:
        candidates.append(_material_slice_image_dir(tenant_id, material_version_id, create=False) / p.name)
    # legacy fallback
    candidates.append(base / "extracted_images" / p.name)

    target = next((c for c in candidates if c.exists() and c.is_file()), None)
    if target is None:
        return _error("IMAGE_NOT_FOUND", "图片不存在", 404)
    target_resolved = target.resolve()
    allowed_roots = [
        (Path(__file__).resolve().parent / "extracted_images").resolve(),
        (tenant_root(tenant_id) / "slices" / "images").resolve(),
    ]
    if not any(str(target_resolved).startswith(str(root)) for root in allowed_roots):
        return _error("ACCESS_DENIED", "图片路径不在允许范围内", 403)
    suffix = target.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        return _error("BAD_REQUEST", "不支持的图片类型", 400)
    return send_file(str(target))


@app.post('/api/<tenant_id>/images/ocr-test')
def api_image_ocr_test(tenant_id: str):
    """
    Debug endpoint to verify backend image OCR config/route.
    Accepts JSON body: { "path": "...", "material_version_id": "..." }
    """
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "material.upload")
    except PermissionError as e:
        return _error(str(e), "无权限执行图片解析测试", 403)

    body = request.get_json(silent=True) or {}
    image_path = str(body.get("path") or request.args.get("path", "")).strip()
    requested_material_version_id = str(body.get("material_version_id") or request.args.get("material_version_id", "")).strip()
    if not image_path:
        return _error("BAD_REQUEST", "path is required", 400)

    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    p = Path(image_path)
    candidates: list[Path] = []
    base = Path(__file__).resolve().parent
    if p.is_absolute():
        candidates.append(p)
    candidates.append(base / p)
    if material_version_id:
        candidates.append(_material_slice_image_dir(tenant_id, material_version_id, create=False) / p.name)
    candidates.append(base / "extracted_images" / p.name)

    target = next((c for c in candidates if c.exists() and c.is_file()), None)
    if target is None:
        return _error("IMAGE_NOT_FOUND", "图片不存在", 404)
    target_resolved = target.resolve()
    allowed_roots = [
        (Path(__file__).resolve().parent / "extracted_images").resolve(),
        (tenant_root(tenant_id) / "slices" / "images").resolve(),
    ]
    if not any(str(target_resolved).startswith(str(root)) for root in allowed_roots):
        return _error("ACCESS_DENIED", "图片路径不在允许范围内", 403)
    suffix = target.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        return _error("BAD_REQUEST", "不支持的图片类型", 400)

    # Load config the same way slicing does
    from process_textbook_images import load_config, analyze_image_with_qwen_vl, extract_table_from_content

    config = load_config()
    image_model = config.get("IMAGE_MODEL") or "doubao-seed-1.8"
    image_provider = (config.get("IMAGE_PROVIDER") or "").lower()
    image_base_url = config.get("IMAGE_BASE_URL") or config.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"
    ark_api_key = config.get("ARK_API_KEY") or ""
    volc_ak = config.get("VOLC_ACCESS_KEY_ID") or ""
    volc_sk = config.get("VOLC_SECRET_ACCESS_KEY") or ""
    ark_project_name = config.get("ARK_PROJECT_NAME") or ""
    api_key = (
        config.get("IMAGE_API_KEY")
        or config.get("AIT_API_KEY")
        or config.get("CRITIC_API_KEY")
        or config.get("OPENAI_API_KEY")
        or ""
    )

    analysis = analyze_image_with_qwen_vl(
        str(target_resolved),
        api_key,
        model_name=image_model,
        base_url=image_base_url,
        provider=image_provider,
        ark_api_key=ark_api_key,
        volc_ak=volc_ak,
        volc_sk=volc_sk,
        ark_project_name=ark_project_name,
    )
    if not analysis:
        detail = str(getattr(analyze_image_with_qwen_vl, "last_error", "") or "")
        return _json_response(
            {
                "ok": False,
                "error": detail or "analysis_failed",
                "config": {
                    "image_model": image_model,
                    "image_provider": image_provider,
                    "image_base_url": image_base_url,
                    "ark_project_name": ark_project_name,
                    "has_ark_api_key": bool(ark_api_key),
                    "has_volc_ak_sk": bool(volc_ak and volc_sk),
                },
            },
            status=200,
        )

    _, table_content = extract_table_from_content(analysis)
    return _json_response(
        {
            "ok": True,
            "analysis": analysis,
            "contains_table": bool(table_content),
            "contains_chart": any(
                kw in analysis.lower() for kw in ["坐标", "曲线", "趋势", "图表", "图", "axis", "chart"]
            ),
            "config": {
                "image_model": image_model,
                "image_provider": image_provider,
                "image_base_url": image_base_url,
                "ark_project_name": ark_project_name,
                "has_ark_api_key": bool(ark_api_key),
                "has_volc_ak_sk": bool(volc_ak and volc_sk),
            },
        },
        status=200,
    )


@app.post('/api/<tenant_id>/slices/review/batch')
def api_slice_batch_review(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "slice.review")
    except PermissionError as e:
        return _error(str(e), "无权限批量审核切片", 403)

    body = request.get_json(silent=True) or {}
    slice_ids = body.get('slice_ids') or []
    review_status = str(body.get('review_status', 'pending'))
    comment = str(body.get('comment', ''))
    reviewer = str(body.get('reviewer') or system_user)
    requested_material_version_id = str(body.get('material_version_id', '')).strip()

    if not slice_ids:
        return _error("BAD_REQUEST", "slice_ids is required", 400)
    if review_status not in SLICE_STATUSES:
        return _error("INVALID_STATUS", "非法切片审核状态", 400)
    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    if not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "当前城市暂无教材版本", 400)

    updated = 0
    for sid in slice_ids:
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            return _error("BAD_REQUEST", f"invalid slice_id: {sid}", 400)
        _upsert_slice_review_for_material(
            tenant_id=tenant_id,
            material_version_id=material_version_id,
            slice_id=sid_int,
            review_status=review_status,
            reviewer=reviewer,
            comment=comment,
        )
        write_audit_log(
            tenant_id,
            reviewer,
            'slice.review.batch',
            'slice_item',
            str(sid),
            after={'review_status': review_status, 'review_comment': comment, 'material_version_id': material_version_id},
        )
        updated += 1
    return _json_response({'updated': updated, 'material_version_id': material_version_id})


@app.post('/api/<tenant_id>/slices/<int:slice_id>/update')
def api_slice_update(tenant_id: str, slice_id: int):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "slice.review")
    except PermissionError as e:
        return _error(str(e), "无权限修改该城市切片", 403)

    body = request.get_json(silent=True) or {}
    requested_material_version_id = str(body.get('material_version_id', '')).strip()
    new_content = str(body.get('slice_content', '')).strip()
    if not new_content:
        return _error("BAD_REQUEST", "slice_content is required", 400)
    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    if not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "当前城市暂无教材版本", 400)

    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    if not kb_file:
        return _error("SLICE_FILE_NOT_FOUND", "切片文件不存在", 404)
    kb_items = _load_kb_items_from_file(kb_file)
    if slice_id < 0 or slice_id >= len(kb_items):
        return _error("SLICE_NOT_FOUND", "切片不存在", 404)

    item = kb_items[slice_id] if isinstance(kb_items[slice_id], dict) else {}
    original = _extract_slice_text(item)
    target_key = None
    for k in ("核心内容", "content", "chunk_text", "text", "正文", "内容"):
        if k in item:
            target_key = k
            break
    if not target_key:
        target_key = "核心内容"
    item[target_key] = new_content
    kb_items[slice_id] = item
    _save_kb_items_to_file(kb_file, kb_items)
    _upsert_slice_review_for_material(
        tenant_id=tenant_id,
        material_version_id=material_version_id,
        slice_id=slice_id,
        review_status="pending",
        reviewer=system_user,
        comment="内容已修改，待复核",
    )

    write_audit_log(
        tenant_id,
        system_user,
        'slice.content.update',
        'slice_item',
        str(slice_id),
        before={'slice_content': original[:200], 'material_version_id': material_version_id},
        after={'slice_content': new_content[:200], 'material_version_id': material_version_id},
    )
    return _json_response({'ok': True, 'slice_id': slice_id, 'material_version_id': material_version_id})


@app.post('/api/<tenant_id>/slices/<int:slice_id>/images/update')
def api_slice_image_update(tenant_id: str, slice_id: int):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "slice.review")
    except PermissionError as e:
        return _error(str(e), "无权限修改切片图片解析", 403)

    body = request.get_json(silent=True) or {}
    requested_material_version_id = str(body.get('material_version_id', '')).strip()
    image_id = str(body.get('image_id', '')).strip()
    image_path = str(body.get('image_path', '')).strip()
    image_index = body.get('image_index')
    new_analysis = str(body.get('analysis', '')).strip()
    if not new_analysis:
        return _error("BAD_REQUEST", "analysis is required", 400)

    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    if not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "当前城市暂无教材版本", 400)

    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    if not kb_file:
        return _error("SLICE_FILE_NOT_FOUND", "切片文件不存在", 404)
    kb_items = _load_kb_items_from_file(kb_file)
    if slice_id < 0 or slice_id >= len(kb_items):
        return _error("SLICE_NOT_FOUND", "切片不存在", 404)

    item = kb_items[slice_id] if isinstance(kb_items[slice_id], dict) else {}
    struct = item.get("结构化内容") or {}
    if not isinstance(struct, dict):
        return _error("BAD_REQUEST", "切片结构化内容不存在", 400)
    images = struct.get("images") or []
    if not isinstance(images, list) or not images:
        return _error("BAD_REQUEST", "切片无图片", 400)

    target_idx = None
    if isinstance(image_index, int) and 0 <= image_index < len(images):
        target_idx = image_index
    if target_idx is None:
        for idx, img in enumerate(images):
            if not isinstance(img, dict):
                continue
            if image_id and str(img.get("image_id", "")).strip() == image_id:
                target_idx = idx
                break
            if image_path:
                img_path = str(img.get("image_path", "")).strip()
                if img_path == image_path or img_path.split("/")[-1] == image_path.split("/")[-1]:
                    target_idx = idx
                    break

    if target_idx is None:
        return _error("IMAGE_NOT_FOUND", "图片不存在", 404)

    before_analysis = str(images[target_idx].get("analysis", ""))
    images[target_idx]["analysis"] = new_analysis

    # update flags (optional or inferred)
    contains_table = body.get("contains_table")
    contains_chart = body.get("contains_chart")
    if isinstance(contains_table, bool):
        images[target_idx]["contains_table"] = contains_table
    else:
        images[target_idx]["contains_table"] = _detect_table_from_text(new_analysis)
    if isinstance(contains_chart, bool):
        images[target_idx]["contains_chart"] = contains_chart
    else:
        images[target_idx]["contains_chart"] = _detect_chart_from_text(new_analysis)

    struct["images"] = images
    item["结构化内容"] = struct
    kb_items[slice_id] = item
    _save_kb_items_to_file(kb_file, kb_items)
    _upsert_slice_review_for_material(
        tenant_id=tenant_id,
        material_version_id=material_version_id,
        slice_id=slice_id,
        review_status="pending",
        reviewer=system_user,
        comment="图片解析已修改，待复核",
    )

    write_audit_log(
        tenant_id,
        system_user,
        'slice.image.update',
        'slice_item',
        str(slice_id),
        before={'image_id': image_id, 'image_path': image_path, 'analysis': before_analysis[:200], 'material_version_id': material_version_id},
        after={'image_id': image_id, 'image_path': image_path, 'analysis': new_analysis[:200], 'material_version_id': material_version_id},
    )
    return _json_response({'ok': True, 'slice_id': slice_id, 'image_index': target_idx, 'material_version_id': material_version_id})


@app.post('/api/<tenant_id>/slices/merge')
def api_slice_merge(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "slice.review")
    except PermissionError as e:
        return _error(str(e), "无权限合并切片", 403)

    body = request.get_json(silent=True) or {}
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    slice_ids = body.get("slice_ids") or []
    reviewer = str(body.get("reviewer") or system_user)

    if not isinstance(slice_ids, list) or len(slice_ids) < 2:
        return _error("BAD_REQUEST", "slice_ids must contain at least 2 ids", 400)

    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    if not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "当前城市暂无教材版本", 400)

    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    if not kb_file:
        return _error("SLICE_FILE_NOT_FOUND", "切片文件不存在", 404)
    kb_items = _load_kb_items_from_file(kb_file)
    if not kb_items:
        return _error("NO_SLICES", "当前教材没有切片", 400)

    valid_ids: list[int] = []
    for sid in slice_ids:
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            return _error("BAD_REQUEST", f"invalid slice_id: {sid}", 400)
        if sid_int < 0 or sid_int >= len(kb_items):
            return _error("BAD_REQUEST", f"slice_id out of range: {sid_int}", 400)
        if _is_slice_deleted(kb_items[sid_int]):
            return _error("BAD_REQUEST", f"slice already deleted: {sid_int}", 400)
        valid_ids.append(sid_int)
    valid_ids = list(dict.fromkeys(valid_ids))
    if len(valid_ids) < 2:
        return _error("BAD_REQUEST", "slice_ids must contain at least 2 unique ids", 400)

    p3 = _path_prefix((kb_items[valid_ids[0]] or {}).get("完整路径", ""), 3)
    if not p3:
        return _error("BAD_REQUEST", "invalid slice path", 400)
    for sid in valid_ids[1:]:
        cur_p3 = _path_prefix((kb_items[sid] or {}).get("完整路径", ""), 3)
        if cur_p3 != p3:
            return _error("BAD_REQUEST", "slice_ids must be under the same level-3 path", 400)

    base_id = valid_ids[0]
    merged_parts: list[str] = []
    for sid in valid_ids:
        txt = _extract_slice_text(kb_items[sid]).strip()
        if txt:
            merged_parts.append(f"【切片 {sid}】\n{txt}")
    merged_text = "\n\n".join(merged_parts).strip()
    if not merged_text:
        return _error("BAD_REQUEST", "merged content is empty", 400)

    base_item = kb_items[base_id] if isinstance(kb_items[base_id], dict) else {}
    target_key = None
    for k in ("核心内容", "content", "chunk_text", "text", "正文", "内容"):
        if k in base_item:
            target_key = k
            break
    if not target_key:
        target_key = "核心内容"
    base_item[target_key] = merged_text
    base_item["__deleted__"] = False
    base_item.pop("__merged_into__", None)
    base_item.pop("__merged_at__", None)
    kb_items[base_id] = base_item

    deleted_ids: list[int] = []
    merged_at = datetime.now(timezone.utc).isoformat()
    for sid in valid_ids[1:]:
        item = kb_items[sid] if isinstance(kb_items[sid], dict) else {}
        item["__deleted__"] = True
        item["__merged_into__"] = base_id
        item["__merged_at__"] = merged_at
        item["核心内容"] = ""
        kb_items[sid] = item
        deleted_ids.append(sid)

    _save_kb_items_to_file(kb_file, kb_items)
    _upsert_slice_review_for_material(
        tenant_id=tenant_id,
        material_version_id=material_version_id,
        slice_id=base_id,
        review_status="pending",
        reviewer=reviewer,
        comment="切片已合并，待复核",
    )

    order_bucket = _load_slice_order_for_material(tenant_id, material_version_id)
    group_ids = [sid for sid in order_bucket.get(p3, []) if sid not in deleted_ids]
    if base_id not in group_ids:
        group_ids.insert(0, base_id)
    order_bucket[p3] = list(dict.fromkeys(group_ids))
    _save_slice_order_for_material(tenant_id, material_version_id, order_bucket)

    write_audit_log(
        tenant_id,
        reviewer,
        "slice.merge",
        "slice_item",
        str(base_id),
        after={
            "base_slice_id": base_id,
            "deleted_slice_ids": deleted_ids,
            "path_prefix": p3,
            "material_version_id": material_version_id,
        },
    )
    return _json_response(
        {
            "ok": True,
            "base_slice_id": base_id,
            "deleted_slice_ids": deleted_ids,
            "material_version_id": material_version_id,
        }
    )


@app.post('/api/<tenant_id>/slices/add')
def api_slice_add(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "slice.review")
    except PermissionError as e:
        return _error(str(e), "无权限新增切片", 403)

    body = request.get_json(silent=True) or {}
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    path = str(body.get("path", "")).strip()
    slice_content = str(body.get("slice_content", "")).strip()
    mastery = str(body.get("mastery", "")).strip()
    reviewer = str(body.get("reviewer") or _get_system_user() or "admin")

    if not path:
        return _error("BAD_REQUEST", "path is required", 400)
    if not slice_content:
        return _error("BAD_REQUEST", "slice_content is required", 400)

    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    if not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "未找到可用教材版本", 404)

    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    if not kb_file:
        return _error("NO_SLICES_FILE", "当前教材切片文件不存在", 400)
    kb_items = _load_kb_items_from_file(kb_file)
    new_slice = {
        "完整路径": path,
        "掌握程度": mastery,
        "核心内容": slice_content,
        "结构化内容": {
            "context_before": "",
            "tables": [],
            "context_after": "",
            "examples": [],
            "formulas": [],
            "images": [],
            "rules": [],
            "key_params": [],
        },
    }
    kb_items.append(new_slice)
    new_slice_id = len(kb_items) - 1
    _save_kb_items_to_file(kb_file, kb_items)
    _upsert_slice_review_for_material(
        tenant_id=tenant_id,
        material_version_id=material_version_id,
        slice_id=new_slice_id,
        review_status="pending",
        reviewer=reviewer,
        comment="",
    )

    path3 = _path_prefix(path, 3)
    order_bucket = _load_slice_order_for_material(tenant_id, material_version_id)
    ids = [int(x) for x in order_bucket.get(path3, []) if isinstance(x, int) or str(x).isdigit()]
    ids.append(new_slice_id)
    order_bucket[path3] = list(dict.fromkeys(ids))
    _save_slice_order_for_material(tenant_id, material_version_id, order_bucket)

    write_audit_log(
        tenant_id,
        reviewer,
        "slice.add",
        "slice_item",
        str(new_slice_id),
        after={"path": path, "material_version_id": material_version_id},
    )
    return _json_response(
        {
            "ok": True,
            "slice_id": new_slice_id,
            "path": path,
            "material_version_id": material_version_id,
        }
    )


@app.post('/api/<tenant_id>/slices/order')
def api_slice_order(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "slice.review")
    except PermissionError as e:
        return _error(str(e), "无权限调整切片顺序", 403)

    body = request.get_json(silent=True) or {}
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    path_prefix = str(body.get("path_prefix", "")).strip()
    slice_ids = body.get("slice_ids") or []
    reviewer = str(body.get("reviewer") or _get_system_user() or "admin")

    if not path_prefix:
        return _error("BAD_REQUEST", "path_prefix is required", 400)
    if not isinstance(slice_ids, list) or not slice_ids:
        return _error("BAD_REQUEST", "slice_ids is required", 400)

    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    if not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "未找到可用教材版本", 404)

    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
    if not kb_items:
        return _error("NO_SLICES", "当前教材没有切片", 400)

    valid_ids: list[int] = []
    for sid in slice_ids:
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            return _error("BAD_REQUEST", f"invalid slice_id: {sid}", 400)
        if sid_int < 0 or sid_int >= len(kb_items):
            return _error("BAD_REQUEST", f"slice_id out of range: {sid_int}", 400)
        if _is_slice_deleted(kb_items[sid_int]):
            return _error("BAD_REQUEST", f"slice already deleted: {sid_int}", 400)
        valid_ids.append(sid_int)

    expected = _path_prefix(path_prefix, 3)
    if not expected:
        return _error("BAD_REQUEST", "invalid path_prefix", 400)
    for sid in valid_ids:
        cur_path = str((kb_items[sid] or {}).get("完整路径", ""))
        if _path_prefix(cur_path, 3) != expected:
            return _error("BAD_REQUEST", "slice_ids must be under the same level-3 path", 400)

    order_bucket = _load_slice_order_for_material(tenant_id, material_version_id)
    order_bucket[expected] = list(dict.fromkeys(valid_ids))
    _save_slice_order_for_material(tenant_id, material_version_id, order_bucket)

    write_audit_log(
        tenant_id,
        reviewer,
        "slice.reorder",
        "slice_item",
        expected,
        after={"path_prefix": expected, "slice_ids": valid_ids, "material_version_id": material_version_id},
    )
    return _json_response({"ok": True, "path_prefix": expected, "slice_ids": valid_ids, "material_version_id": material_version_id})


@app.get('/api/<tenant_id>/slices/export')
def api_slices_export(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "slice.read")
    except PermissionError as e:
        return _error(str(e), "无权限导出该城市切片", 403)

    status = request.args.get('status', 'all')
    keyword = request.args.get('keyword', '').strip()
    path_prefix = request.args.get('path_prefix', '').strip()
    requested_material_version_id = str(request.args.get('material_version_id', '')).strip()
    if status != "all" and status not in SLICE_STATUSES:
        return _error("INVALID_STATUS", "非法切片状态", 400)
    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
    display_paths = _build_display_paths(kb_items)
    reviews = _load_slice_review_for_material(tenant_id, material_version_id) if material_version_id else {}
    rows: list[dict[str, Any]] = []
    for i, s in enumerate(kb_items):
        review = reviews.get(str(i), {})
        r_status = review.get('review_status', 'pending')
        path = display_paths[i] if i < len(display_paths) else str(s.get('完整路径', '') or '')
        if status != 'all' and r_status != status:
            continue
        if keyword and keyword not in path:
            continue
        if path_prefix and not str(path).startswith(path_prefix):
            continue
        rows.append(
            {
                "slice_id": i,
                "path": path,
                "mastery": s.get("掌握程度", ""),
                "review_status": r_status,
                "review_comment": review.get("comment", ""),
                "slice_content": _extract_slice_text(s),
                "material_version_id": material_version_id,
            }
        )

    df = pd.DataFrame(rows, columns=[
        "slice_id", "path", "mastery", "review_status", "review_comment", "slice_content", "material_version_id",
    ])
    out = BytesIO()
    df.to_excel(out, index=False)
    out.seek(0)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    mid = material_version_id or "default"
    filename = f"{tenant_id}_slices_{mid}_{ts}.xlsx"
    return send_file(
        out,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


def _build_path_tree_options(paths: list[str]) -> list[dict[str, Any]]:
    root: dict[str, Any] = {}
    for p in paths:
        parts = _split_clean_path(p)
        cursor = root
        for part in parts:
            cursor = cursor.setdefault(part, {})

    def _convert(node: dict[str, Any]) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        for k in sorted(node.keys()):
            children = _convert(node[k])
            item: dict[str, Any] = {"label": k, "value": k}
            if children:
                item["children"] = children
            options.append(item)
        return options

    return _convert(root)


@app.get('/api/<tenant_id>/slices/path-tree')
def api_slices_path_tree(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "slice.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问该城市切片路径", 403)

    status = request.args.get('status', 'all')
    requested_material_version_id = str(request.args.get('material_version_id', '')).strip()
    if status != "all" and status not in SLICE_STATUSES:
        return _error("INVALID_STATUS", "非法切片状态", 400)
    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
    display_paths = _build_display_paths(kb_items)
    reviews = _load_slice_review_for_material(tenant_id, material_version_id) if material_version_id else {}
    paths: list[str] = []
    for i, s in enumerate(kb_items):
        review = reviews.get(str(i), {})
        r_status = review.get('review_status', 'pending')
        if status != 'all' and r_status != status:
            continue
        p = (display_paths[i] if i < len(display_paths) else str(s.get('完整路径', '') or '')).strip()
        if p:
            paths.append(p)

    return _json_response({
        "material_version_id": material_version_id,
        "options": _build_path_tree_options(paths),
    })


@app.get('/api/<tenant_id>/slices/path-summary')
def api_slices_path_summary(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "slice.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问该城市切片路径汇总", 403)

    requested_material_version_id = str(request.args.get('material_version_id', '')).strip()
    try:
        level = int(request.args.get('level', '2'))
    except ValueError:
        level = 2
    level = min(max(level, 1), 8)

    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
    display_paths = _build_display_paths(kb_items)
    reviews = _load_slice_review_for_material(tenant_id, material_version_id) if material_version_id else {}
    if not kb_items:
        return _json_response({"items": [], "material_version_id": material_version_id, "level": level})

    summary: dict[str, dict[str, Any]] = {}
    for i, s in enumerate(kb_items):
        raw_path = (display_paths[i] if i < len(display_paths) else str(s.get('完整路径', '') or '')).strip()
        if not raw_path:
            raw_path = "（未分类）"
        parts = [x.strip() for x in raw_path.split(" > ") if x and x.strip()]
        key = " > ".join(parts[:level]) if parts else "（未分类）"
        item = summary.setdefault(
            key,
            {
                "path_prefix": key,
                "total": 0,
                "pending": 0,
                "approved": 0,
                "rejected": 0,
                "revised": 0,
            },
        )
        item["total"] += 1
        r_status = str(reviews.get(str(i), {}).get("review_status", "pending"))
        if r_status in {"pending", "approved", "rejected", "revised"}:
            item[r_status] += 1
        else:
            item["pending"] += 1

    items = sorted(summary.values(), key=lambda x: (-int(x["total"]), str(x["path_prefix"])))
    return _json_response({"items": items, "material_version_id": material_version_id, "level": level})


@app.get('/api/<tenant_id>/mappings')
def api_mappings(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "map.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问该城市映射", 403)

    status = request.args.get('status', 'all')
    meta_conflict = request.args.get('meta_conflict', 'all')
    keyword = request.args.get('keyword', '').strip()
    path_prefix = request.args.get('path_prefix', '').strip()
    requested_material_version_id = str(request.args.get('material_version_id', '')).strip()
    if status != "all" and status not in MAP_STATUSES:
        return _error("INVALID_STATUS", "非法映射状态", 400)
    if meta_conflict not in {"all", "yes", "no"}:
        return _error("INVALID_META_CONFLICT", "非法元数据冲突筛选值", 400)
    page, page_size = _parse_pagination()
    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    with start_span("api.mappings", {"tenant_id": tenant_id, "status": status, "material_version_id": material_version_id}):
        mapping_path_obj = _resolve_mapping_path_for_material(tenant_id, material_version_id)
        if not mapping_path_obj:
            return _json_response({"items": [], "total": 0, "page": page, "page_size": page_size, "material_version_id": material_version_id})

        mapping = json.loads(mapping_path_obj.read_text(encoding='utf-8'))
        reviews = _load_mapping_review_for_material(tenant_id, material_version_id)
        kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
        kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
        kb_index = {i: item for i, item in enumerate(kb_items)}
        history_rows = _load_history_rows(tenant_id)

        items = []
        for slice_id, payload in mapping.items():
            path = payload.get('完整路径', '')
            if path_prefix and not str(path).startswith(path_prefix):
                continue
            if keyword and keyword not in path:
                continue
            for m in payload.get('matched_questions', []):
                q_idx = m.get('question_index')
                if q_idx is None:
                    continue
                evidence = m.get("evidence", {}) if isinstance(m.get("evidence"), dict) else {}
                is_meta_conflict = bool(evidence.get("meta_conflict"))
                if meta_conflict == "yes" and not is_meta_conflict:
                    continue
                if meta_conflict == "no" and is_meta_conflict:
                    continue
                map_key = f'{slice_id}:{q_idx}'
                review = reviews.get(map_key, {})
                confirm_status = _normalize_mapping_status(review.get('confirm_status', 'pending'))
                if status != 'all' and confirm_status != status:
                    continue
                slice_item = None
                if str(slice_id).isdigit():
                    slice_item = kb_index.get(int(slice_id))
                if not slice_item:
                    # Fallback to path match when id mismatch
                    slice_item = next((x for x in kb_items if x.get("完整路径", "") == path), None)
                slice_text = _extract_slice_text(slice_item or {})
                q_row = history_rows.get(int(q_idx), {})
                image_items = _extract_slice_images(slice_item or {})
                items.append(
                    {
                        'map_key': map_key,
                        'slice_id': int(slice_id) if str(slice_id).isdigit() else slice_id,
                        'path': path,
                        'question_index': int(q_idx),
                        'confidence': m.get('confidence', 0),
                        'confirm_status': confirm_status,
                        'review_comment': review.get('comment', ''),
                        'method': m.get('method', ''),
                        'meta_conflict': is_meta_conflict,
                        'meta_conflict_detail': evidence.get("meta_conflict_detail", ""),
                        'slice_preview': slice_text[:160],
                        'slice_content': slice_text,
                        'images': image_items,
                        'question_stem': q_row.get("题干", ""),
                        'question_answer': q_row.get("正确答案", ""),
                        'question_explanation': q_row.get("解析", ""),
                        'material_version_id': material_version_id,
                    }
                )
    payload = _paginate(items, page, page_size)
    payload["material_version_id"] = material_version_id
    return _json_response(payload)


@app.get('/api/<tenant_id>/stats')
def api_tenant_stats(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "slice.read")
        _check_tenant_permission(tenant_id, "map.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问城市统计", 403)

    effective_mid = _resolve_material_version_id(tenant_id, "")
    kb_file = _resolve_slice_file_for_material(tenant_id, effective_mid)

    # slice stats (effective material)
    slice_total = 0
    slice_pending = 0
    review = _load_slice_review_for_material(tenant_id, effective_mid) if effective_mid else {}
    kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
    if kb_items:
        for i, _ in enumerate(kb_items):
            slice_total += 1
            if review.get(str(i), {}).get("review_status", "pending") == "pending":
                slice_pending += 1
    else:
        slice_total = 0
        slice_pending = 0

    # mapping stats
    map_total = 0
    map_pending = 0
    mapping_path_obj = _resolve_mapping_path_for_material(tenant_id, effective_mid)
    mapping_review = _load_mapping_review_for_material(tenant_id, effective_mid) if effective_mid else {}
    if mapping_path_obj:
        mapping = json.loads(mapping_path_obj.read_text(encoding="utf-8"))
        for sid, payload in mapping.items():
            for m in payload.get("matched_questions", []):
                q_idx = m.get("question_index")
                if q_idx is None:
                    continue
                map_total += 1
                mk = f"{sid}:{q_idx}"
                if _normalize_mapping_status(mapping_review.get(mk, {}).get("confirm_status", "pending")) == "pending":
                    map_pending += 1

    slice_approved = max(slice_total - slice_pending, 0)
    slice_approval_rate = round((slice_approved / slice_total) * 100, 2) if slice_total else 0.0

    mapping_approved = max(map_total - map_pending, 0)
    mapping_approval_rate = round((mapping_approved / map_total) * 100, 2) if map_total else 0.0

    materials = list_material_versions(tenant_id)
    effective_version = next((m for m in materials if m.get("status") == "effective"), None)
    material_total = len(materials)

    bank = _load_bank(tenant_bank_path(tenant_id))
    bank_total_all = len(bank)
    bank_total_effective = 0
    if effective_mid:
        for q in bank:
            if str(q.get("教材版本ID", "")).strip() == effective_mid:
                bank_total_effective += 1

    now = datetime.now(timezone.utc)
    seven_days_ago = now.timestamp() - 7 * 24 * 3600
    events = _load_audit_events(tenant_id)
    last_upload_at = ""
    last_generate_at = ""
    gen_7d_total = 0
    gen_7d_success = 0
    gen_7d_failed = 0
    for ev in events:
        action = str(ev.get("action", ""))
        ts = str(ev.get("timestamp", ""))
        ts_epoch = 0.0
        if ts:
            try:
                ts_epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts_epoch = 0.0
        if action == "material.upload.slice":
            if not last_upload_at or ts > last_upload_at:
                last_upload_at = ts
        if action == "gen.create.batch":
            if not last_generate_at or ts > last_generate_at:
                last_generate_at = ts
            if ts_epoch >= seven_days_ago:
                gen_7d_total += 1
                after = ev.get("after", {}) if isinstance(ev.get("after"), dict) else {}
                generated = int(after.get("generated", 0) or 0)
                errors = after.get("errors", [])
                if generated > 0 and (not isinstance(errors, list) or len(errors) == 0):
                    gen_7d_success += 1
                else:
                    gen_7d_failed += 1

    focus_events: list[dict[str, Any]] = []
    if not effective_mid:
        focus_events.append({"text": "还没有生效教材版本，请先在资源上传里设置生效版本。", "path": "/materials", "action": "去设置"})
    if slice_pending > 0:
        focus_events.append({"text": f"还有 {slice_pending} 条切片待审核，建议优先处理。", "path": "/slice-review", "action": "去审核"})
    if map_pending > 0:
        focus_events.append({"text": f"还有 {map_pending} 条映射待审核，建议尽快处理。", "path": "/mapping-review", "action": "去审核"})
    if slice_approved > 0 and bank_total_effective == 0:
        focus_events.append({"text": "当前教材已有可出题切片，但题库为空，可以开始出题。", "path": "/ai-generate", "action": "去出题"})
    if not focus_events:
        focus_events.append({"text": "当前链路正常，可继续出题或抽检题库。", "path": "/question-bank", "action": "去题库"})

    return _json_response(
        {
            "tenant_id": tenant_id,
            "slice_total": slice_total,
            "slice_pending": slice_pending,
            "slice_approved": slice_approved,
            "slice_approval_rate": slice_approval_rate,
            "mapping_total": map_total,
            "mapping_pending": map_pending,
            "mapping_approved": mapping_approved,
            "mapping_approval_rate": mapping_approval_rate,
            "material_total": material_total,
            "effective_material_version": (effective_version or {}).get("material_version_id", ""),
            "bank_total_all": bank_total_all,
            "bank_total_effective": bank_total_effective,
            "gen_7d_total": gen_7d_total,
            "gen_7d_success": gen_7d_success,
            "gen_7d_failed": gen_7d_failed,
            "last_upload_at": last_upload_at,
            "last_generate_at": last_generate_at,
            "focus_events": focus_events,
        }
    )


@app.get('/api/<tenant_id>/materials')
def api_materials(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "material.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问教材版本", 403)
    items = list_material_versions(tenant_id)
    mapping_dir = tenant_root(tenant_id) / "mapping"
    ref_dir = _material_reference_dir(tenant_id)
    enriched = []
    for item in items:
        mid = str(item.get("material_version_id", "")).strip()
        mapping_file = mapping_dir / f"knowledge_question_mapping_{mid}.json"
        slice_file = tenant_slices_dir(tenant_id) / f"knowledge_slices_{mid}.jsonl"
        ref_file = next((p for p in sorted(ref_dir.glob(f"{mid}_*"), reverse=True) if p.is_file()), None)
        rec = dict(item)
        rec["mapping_ready"] = bool(mapping_file.exists())
        rec["mapping_file"] = str(mapping_file) if mapping_file.exists() else ""
        rec["reference_file"] = str(ref_file) if ref_file else ""
        rec["slice_ready"] = bool(slice_file.exists())
        if not rec.get("slice_status"):
            rec["slice_status"] = "success" if rec["slice_ready"] else "pending"
        if not rec.get("mapping_status"):
            rec["mapping_status"] = "success" if rec["mapping_ready"] else "pending"
        # Self-heal stale status: marked success but mapping artifact is missing.
        if str(rec.get("mapping_status", "")).strip() == "success" and not rec["mapping_ready"]:
            rec["mapping_status"] = "pending"
            if not str(rec.get("mapping_error", "")).strip():
                rec["mapping_error"] = "映射文件缺失，请重新映射"
        rec["slice_error"] = str(rec.get("slice_error", "") or "")
        rec["mapping_error"] = str(rec.get("mapping_error", "") or "")
        enriched.append(rec)
    return _json_response({"items": enriched})


@app.post('/api/<tenant_id>/materials/<material_version_id>/reference/upload')
def api_upload_reference_and_map(tenant_id: str, material_version_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "material.upload")
        _check_tenant_permission(tenant_id, "map.read")
    except PermissionError as e:
        return _error(str(e), "无权限上传参考题并生成映射", 403)

    target = str(material_version_id).strip()
    if not target:
        return _error("BAD_REQUEST", "material_version_id is required", 400)
    materials = list_material_versions(tenant_id)
    current = next((x for x in materials if str(x.get("material_version_id", "")).strip() == target), None)
    if not current:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    source_file = request.files.get("file")
    if source_file is None:
        return _error("BAD_REQUEST", "请上传参考题表格（xlsx/xls）", 400)
    suffix = Path(source_file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xls"}:
        return _error("BAD_REQUEST", "参考题仅支持 xlsx/xls", 400)

    ref_dir = _material_reference_dir(tenant_id)
    orig_name = _safe_filename(source_file.filename or f"reference{suffix}")
    ref_path = ref_dir / f"{target}_{orig_name}"
    source_file.save(str(ref_path))

    history_copy = _material_history_copy_path(tenant_id, target, suffix)
    shutil.copyfile(ref_path, history_copy)
    # Keep compatibility for existing retriever path.
    shutil.copyfile(ref_path, tenant_root(tenant_id) / "materials" / f"history_questions{suffix}")

    kb_file = _resolve_slice_file_for_material(tenant_id, target)
    if not kb_file:
        return _error("NO_SLICES_FILE", "该教材版本没有切片文件，无法生成映射", 400)
    upsert_material_runtime(
        tenant_id,
        target,
        mapping_status="running",
        mapping_error="",
    )

    mapping_dir = tenant_root(tenant_id) / "mapping"
    mapping_dir.mkdir(parents=True, exist_ok=True)
    output_path = mapping_dir / f"knowledge_question_mapping_{target}.json"
    cmd = [
        sys.executable,
        "map_knowledge_to_questions.py",
        "--tenant-id",
        tenant_id,
        "--kb-path",
        str(kb_file),
        "--history-path",
        str(history_copy),
        "--output",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
    if proc.returncode != 0:
        err_text = f"映射脚本执行失败: {proc.stderr[-500:] if proc.stderr else proc.stdout[-500:]}"
        upsert_material_runtime(
            tenant_id,
            target,
            mapping_status="failed",
            mapping_error=err_text,
        )
        return _error(
            "MAPPING_FAILED",
            err_text,
            500,
        )
    if not output_path.exists():
        upsert_material_runtime(
            tenant_id,
            target,
            mapping_status="failed",
            mapping_error="映射结果未生成",
        )
        return _error("MAPPING_EMPTY", "映射结果未生成", 500)
    try:
        mapping = json.loads(output_path.read_text(encoding="utf-8"))
        mapping_total = len(mapping) if isinstance(mapping, dict) else 0
    except json.JSONDecodeError:
        mapping_total = 0
    upsert_material_runtime(
        tenant_id,
        target,
        mapping_status="success",
        mapping_error="",
    )

    write_audit_log(
        tenant_id,
        system_user,
        "material.upload.reference_map",
        "material",
        target,
        after={
            "material_version_id": target,
            "kb_file": str(kb_file),
            "reference_file": str(ref_path),
            "history_copy": str(history_copy),
            "mapping_file": str(output_path),
            "mapping_total": mapping_total,
        },
    )
    return _json_response(
        {
            "material_version_id": target,
            "reference_file": str(ref_path),
            "mapping_file": str(output_path),
            "mapping_total": mapping_total,
        }
    )


@app.post('/api/<tenant_id>/materials/<material_version_id>/reslice')
def api_material_reslice(tenant_id: str, material_version_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "material.upload")
    except PermissionError as e:
        return _error(str(e), "无权限重新切片", 403)

    target = str(material_version_id).strip()
    if not target:
        return _error("BAD_REQUEST", "material_version_id is required", 400)
    current = _find_material_record(tenant_id, target)
    if not current:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    prev_status = str(current.get("status", "") or "archived")
    prev_mapping_status = str(current.get("mapping_status", "") or "pending")
    prev_mapping_error = str(current.get("mapping_error", "") or "")

    docx_path = _resolve_docx_from_material_record(current)
    if not docx_path:
        return _error("SOURCE_NOT_FOUND", "教材源文件不存在或不支持，请重新上传教材", 400)
    upsert_material_runtime(
        tenant_id,
        target,
        status="slicing",
        slice_status="running",
        slice_error="",
        mapping_status="pending",
        mapping_error="",
    )

    slices_output = tenant_slices_dir(tenant_id) / f"knowledge_slices_{target}.jsonl"
    slice_image_dir = _material_slice_image_dir(tenant_id, target)
    if slice_image_dir.exists():
        shutil.rmtree(slice_image_dir, ignore_errors=True)
        slice_image_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "generate_knowledge_slices.py",
        "--tenant-id",
        tenant_id,
        "--docx",
        str(docx_path),
        "--output",
        str(slices_output),
        "--extract-dir",
        str(slice_image_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
    if proc.returncode != 0:
        err_text = f"切片脚本执行失败: {proc.stderr[-500:] if proc.stderr else proc.stdout[-500:]}"
        upsert_material_runtime(
            tenant_id,
            target,
            status=prev_status,
            slice_status="failed",
            slice_error=err_text,
            mapping_status=prev_mapping_status,
            mapping_error=prev_mapping_error,
        )
        return _error(
            "SLICE_FAILED",
            err_text,
            500,
        )

    line_count = 0
    if slices_output.exists():
        with slices_output.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    line_count += 1
    if line_count <= 0:
        upsert_material_runtime(
            tenant_id,
            target,
            status=prev_status,
            slice_status="failed",
            slice_error="重新切片结果为空，请检查教材内容",
            mapping_status=prev_mapping_status,
            mapping_error=prev_mapping_error,
        )
        return _error("SLICE_EMPTY", "重新切片结果为空，请检查教材内容", 400)

    # 重新切片后，旧审核记录与旧映射都可能不再可靠，重置以避免脏数据。
    _delete_material_bucket(_slice_review_file_by_material(tenant_id), target)
    mapping_dir = tenant_root(tenant_id) / "mapping"
    for p in (
        mapping_dir / f"knowledge_question_mapping_{target}.json",
        mapping_dir / f"knowledge_question_mapping_{target}.jsonl",
    ):
        if p.exists():
            p.unlink(missing_ok=True)
    # Also remove legacy fallback mapping file to prevent stale mapping reuse.
    legacy_mapping = Path(tenant_mapping_path(tenant_id))
    if legacy_mapping.exists():
        legacy_mapping.unlink(missing_ok=True)
    _delete_material_bucket(_mapping_review_file_by_material(tenant_id), target)
    upsert_material_runtime(
        tenant_id,
        target,
        status=prev_status,
        slice_status="success",
        slice_error="",
        mapping_status="pending",
        mapping_error="切片已更新，请重新映射",
    )

    write_audit_log(
        tenant_id,
        system_user,
        "material.reslice",
        "material",
        target,
        after={
            "material_version_id": target,
            "docx_file": str(docx_path),
            "slices_file": str(slices_output),
            "slice_count": line_count,
            "mapping_reset": True,
        },
    )
    return _json_response(
        {
            "material_version_id": target,
            "slices_file": str(slices_output),
            "slice_count": line_count,
            "mapping_reset": True,
        }
    )


@app.post('/api/<tenant_id>/materials/<material_version_id>/remap')
def api_material_remap(tenant_id: str, material_version_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "material.upload")
        _check_tenant_permission(tenant_id, "map.read")
    except PermissionError as e:
        return _error(str(e), "无权限重新映射", 403)

    target = str(material_version_id).strip()
    if not target:
        return _error("BAD_REQUEST", "material_version_id is required", 400)
    current = _find_material_record(tenant_id, target)
    if not current:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    kb_file = _resolve_slice_file_for_material(tenant_id, target)
    if not kb_file:
        return _error("NO_SLICES_FILE", "该教材版本没有切片文件，无法重新映射", 400)
    history_file = _resolve_reference_file_for_material(tenant_id, target)
    if not history_file:
        return _error("REFERENCE_NOT_FOUND", "未找到该教材的参考题文件，请先上传参考题", 400)
    upsert_material_runtime(
        tenant_id,
        target,
        mapping_status="running",
        mapping_error="",
    )

    mapping_dir = tenant_root(tenant_id) / "mapping"
    mapping_dir.mkdir(parents=True, exist_ok=True)
    output_path = mapping_dir / f"knowledge_question_mapping_{target}.json"
    cmd = [
        sys.executable,
        "map_knowledge_to_questions.py",
        "--tenant-id",
        tenant_id,
        "--kb-path",
        str(kb_file),
        "--history-path",
        str(history_file),
        "--output",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
    if proc.returncode != 0:
        err_text = f"映射脚本执行失败: {proc.stderr[-500:] if proc.stderr else proc.stdout[-500:]}"
        upsert_material_runtime(
            tenant_id,
            target,
            mapping_status="failed",
            mapping_error=err_text,
        )
        return _error(
            "MAPPING_FAILED",
            err_text,
            500,
        )
    if not output_path.exists():
        upsert_material_runtime(
            tenant_id,
            target,
            mapping_status="failed",
            mapping_error="映射结果未生成",
        )
        return _error("MAPPING_EMPTY", "映射结果未生成", 500)
    try:
        mapping = json.loads(output_path.read_text(encoding="utf-8"))
        mapping_total = len(mapping) if isinstance(mapping, dict) else 0
    except json.JSONDecodeError:
        mapping_total = 0

    _delete_material_bucket(_mapping_review_file_by_material(tenant_id), target)
    upsert_material_runtime(
        tenant_id,
        target,
        mapping_status="success",
        mapping_error="",
    )

    write_audit_log(
        tenant_id,
        system_user,
        "material.remap",
        "material",
        target,
        after={
            "material_version_id": target,
            "kb_file": str(kb_file),
            "history_file": str(history_file),
            "mapping_file": str(output_path),
            "mapping_total": mapping_total,
        },
    )
    return _json_response(
        {
            "material_version_id": target,
            "mapping_file": str(output_path),
            "mapping_total": mapping_total,
            "reference_file": str(history_file),
        }
    )


@app.post('/api/<tenant_id>/materials/effective')
def api_material_set_effective(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "material.effective")
    except PermissionError as e:
        return _error(str(e), "无权限设置生效教材", 403)
    body = request.get_json(silent=True) or {}
    material_version_id = str(body.get("material_version_id", "")).strip()
    if not material_version_id:
        return _error("BAD_REQUEST", "material_version_id is required", 400)
    updated = set_effective_material_version(tenant_id, material_version_id)
    if not updated:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    write_audit_log(
        tenant_id,
        system_user,
        "material.set_effective",
        "material",
        material_version_id,
        after={"status": "effective"},
    )
    return _json_response({"item": updated})


@app.post('/api/<tenant_id>/materials/<material_version_id>/archive')
def api_material_archive(tenant_id: str, material_version_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "material.effective")
    except PermissionError as e:
        return _error(str(e), "无权限下线教材", 403)
    target = str(material_version_id).strip()
    if not target:
        return _error("BAD_REQUEST", "material_version_id is required", 400)
    materials = list_material_versions(tenant_id)
    current = next((x for x in materials if str(x.get("material_version_id", "")).strip() == target), None)
    if not current:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    if str(current.get("status", "")) == "effective":
        other_effective = [
            x for x in materials
            if str(x.get("material_version_id", "")).strip() != target
            and str(x.get("status", "")).strip() == "effective"
        ]
        if not other_effective:
            return _error("LAST_EFFECTIVE", "这是当前最后一个生效教材，不能下线", 409)

    updated = archive_material_version(tenant_id, target)
    if not updated:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    materials = list_material_versions(tenant_id)
    has_effective = any(str(x.get("status", "")) == "effective" for x in materials)
    if not has_effective:
        fallback = next((x for x in materials if str(x.get("material_version_id", "")).strip() != target), None)
        if fallback:
            set_effective_material_version(tenant_id, str(fallback.get("material_version_id", "")).strip())
    write_audit_log(
        tenant_id,
        system_user,
        "material.archive",
        "material",
        target,
        after={"status": "archived"},
    )
    return _json_response({"item": updated})


@app.delete('/api/<tenant_id>/materials/<material_version_id>')
def api_material_delete(tenant_id: str, material_version_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "material.effective")
    except PermissionError as e:
        return _error(str(e), "无权限删除教材", 403)

    target = str(material_version_id).strip()
    if not target:
        return _error("BAD_REQUEST", "material_version_id is required", 400)
    force = _parse_bool_arg(request.args.get("force"), False)
    materials = list_material_versions(tenant_id)
    current = next((x for x in materials if str(x.get("material_version_id", "")).strip() == target), None)
    if not current:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    if str(current.get("status", "")) == "effective" and not force:
        return _error("MATERIAL_EFFECTIVE", "当前版本是生效教材，请先切换生效版本或使用强制删除", 409)

    cleanup_stats = _cleanup_material_artifacts(tenant_id, target)
    deleted = delete_material_version(tenant_id, target)
    if not deleted:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    remaining = list_material_versions(tenant_id)
    has_effective = any(str(x.get("status", "")) == "effective" for x in remaining)
    if not has_effective and remaining:
        set_effective_material_version(tenant_id, str(remaining[0].get("material_version_id", "")).strip())

    write_audit_log(
        tenant_id,
        system_user,
        "material.delete",
        "material",
        target,
        after={"force": force, **cleanup_stats},
    )
    return _json_response({"deleted": True, "material_version_id": target, **cleanup_stats})


@app.post('/api/<tenant_id>/materials/upload')
def api_upload_material(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "material.upload")
    except PermissionError as e:
        return _error(str(e), "无权限上传教材", 403)

    upload_dir = _material_upload_dir(tenant_id)
    now = datetime.now(timezone.utc)
    version_id = now.strftime("v%Y%m%d_%H%M%S")
    source_file = request.files.get("file")
    raw_text = (request.form.get("text") or "").strip()
    if source_file is None and not raw_text:
        return _error("BAD_REQUEST", "请上传文件或输入教材文字", 400)

    if source_file is not None:
        orig_name = _safe_filename(source_file.filename or "material.docx")
        source_path = upload_dir / f"{version_id}_{orig_name}"
        source_file.save(str(source_path))
    else:
        source_path = upload_dir / f"{version_id}_manual.txt"
        source_path.write_text(raw_text, encoding="utf-8")

    if source_path.suffix.lower() == ".docx":
        docx_path = source_path
    else:
        docx_path = upload_dir / f"{version_id}_manual.docx"
        text_data = source_path.read_text(encoding="utf-8", errors="ignore")
        _text_to_docx(text_data, docx_path)

    checksum = _sha256_file(source_path)
    register_material_version(
        tenant_id=tenant_id,
        material_version_id=version_id,
        file_path=str(source_path),
        checksum=checksum,
        status="slicing",
        slice_status="running",
        mapping_status="pending",
        slice_error="",
        mapping_error="",
    )

    slices_output = tenant_slices_dir(tenant_id) / f"knowledge_slices_{version_id}.jsonl"
    slice_image_dir = _material_slice_image_dir(tenant_id, version_id)
    cmd = [
        sys.executable,
        "generate_knowledge_slices.py",
        "--tenant-id",
        tenant_id,
        "--docx",
        str(docx_path),
        "--output",
        str(slices_output),
        "--extract-dir",
        str(slice_image_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
    if proc.returncode != 0:
        err_text = f"切片脚本执行失败: {proc.stderr[-500:] if proc.stderr else proc.stdout[-500:]}"
        upsert_material_runtime(
            tenant_id,
            version_id,
            status="failed",
            slice_status="failed",
            slice_error=err_text,
        )
        return _error(
            "SLICE_FAILED",
            err_text,
            500,
        )

    line_count = 0
    if slices_output.exists():
        with slices_output.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    line_count += 1
    if line_count <= 0:
        upsert_material_runtime(
            tenant_id,
            version_id,
            status="failed",
            slice_status="failed",
            slice_error="切片结果为空，请检查教材内容后重试",
        )
        return _error("SLICE_EMPTY", "切片结果为空，请检查教材内容后重试", 400)
    set_effective_material_version(tenant_id, version_id)
    upsert_material_runtime(
        tenant_id,
        version_id,
        status="effective",
        slice_status="success",
        slice_error="",
        mapping_status="pending",
        mapping_error="",
    )
    write_audit_log(
        tenant_id,
        system_user,
        "material.upload.slice",
        "material",
        version_id,
        after={
            "source_file": str(source_path),
            "docx_file": str(docx_path),
            "slices_file": str(slices_output),
            "slice_count": line_count,
        },
    )
    return _json_response(
        {
            "material_version_id": version_id,
            "source_file": str(source_path),
            "slices_file": str(slices_output),
            "slice_count": line_count,
        }
    )


@app.post('/api/<tenant_id>/generate')
def api_generate_questions(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限执行出题", 403)

    body = request.get_json(silent=True) or {}
    gen_scope_mode = str(body.get("gen_scope_mode", "custom"))  # custom | per_slice
    num_questions = int(body.get("num_questions", 1))
    num_questions = min(max(num_questions, 1), 200)
    question_type = str(body.get("question_type", "单选题"))
    generation_mode = str(body.get("generation_mode", "灵活"))
    difficulty = str(body.get("difficulty", "随机"))
    slice_ids_input = body.get("slice_ids") or []
    save_to_bank = bool(body.get("save_to_bank", True))
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    task_id = str(body.get("task_id", "")).strip()

    if gen_scope_mode not in {"custom", "per_slice"}:
        return _error("BAD_REQUEST", "非法出题范围模式", 400)
    if question_type not in QUESTION_TYPES:
        return _error("BAD_REQUEST", "非法题型", 400)
    if generation_mode not in GEN_MODES:
        return _error("BAD_REQUEST", "非法出题模式", 400)

    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
    if not kb_items:
        return _error("NO_SLICES", "当前城市没有切片，请先上传教材并生成切片", 400)

    review_store = _load_slice_review_for_material(tenant_id, material_version_id)
    approved_ids = {
        int(k) for k, v in review_store.items()
        if str(k).isdigit() and isinstance(v, dict) and v.get("review_status") == "approved"
    }
    if not approved_ids:
        return _error("NO_APPROVED_SLICES", "当前城市暂无 approved 切片，无法出题", 400)

    selected_ids = set()
    for sid in slice_ids_input:
        try:
            selected_ids.add(int(sid))
        except (TypeError, ValueError):
            continue
    candidate_ids = sorted((selected_ids & approved_ids) if selected_ids else approved_ids)
    if not candidate_ids:
        return _error("NO_CANDIDATE_SLICES", "选中范围内没有 approved 切片", 400)
    if gen_scope_mode == "per_slice":
        num_questions = len(candidate_ids)
    if num_questions <= 0:
        return _error("BAD_REQUEST", "题量必须大于0", 400)

    set_active_tenant(tenant_id)
    os.environ["TENANT_ID"] = tenant_id
    if not kb_file:
        return _error("NO_SLICES_FILE", "当前城市没有可用切片文件", 400)
    retriever = KnowledgeRetriever(
        kb_path=str(kb_file),
        history_path=str(_resolve_history_path_for_material(tenant_id, material_version_id)),
        mapping_path=str(_resolve_mapping_path_for_material(tenant_id, material_version_id) or tenant_mapping_path(tenant_id)),
    )
    candidate_ids = [sid for sid in candidate_ids if 0 <= sid < len(retriever.kb_data)]
    if not candidate_ids:
        return _error("NO_VALID_SLICES", "审核记录与当前切片版本不一致，请重新审核切片后再出题", 400)
    cfg_path = Path("填写您的Key.txt")
    api_key = ""
    base_url = "https://api.deepseek.com"
    model_name = "deepseek-reasoner"
    if cfg_path.exists():
        cfg: dict[str, str] = {}
        for line in cfg_path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()

        def _usable_key(v: str) -> bool:
            return bool(v) and "请将您的Key" not in v

        # Keep provider triplet consistent: API_KEY/BASE_URL/MODEL must come from the same prefix.
        # Priority is OPENAI -> DEEPSEEK -> CRITIC to match current admin usage.
        for prefix in ("OPENAI", "DEEPSEEK", "CRITIC"):
            key = str(cfg.get(f"{prefix}_API_KEY", "")).strip()
            if not _usable_key(key):
                continue
            api_key = key
            candidate_base = str(cfg.get(f"{prefix}_BASE_URL", "")).strip()
            candidate_model = str(cfg.get(f"{prefix}_MODEL", "")).strip()
            if candidate_base and "http" in candidate_base:
                base_url = candidate_base
            if candidate_model:
                model_name = candidate_model
            break
    if not api_key:
        return _error("NO_API_KEY", "未配置可用 API Key，请检查 填写您的Key.txt", 400)

    difficulty_range = _parse_difficulty_range(difficulty)
    run_started_at = datetime.now(timezone.utc).isoformat()
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    generated: list[dict[str, Any]] = []
    errors: list[str] = []
    process_trace: list[dict[str, Any]] = []
    for i in range(num_questions):
        sid = candidate_ids[i % len(candidate_ids)] if num_questions > len(candidate_ids) else random.choice(candidate_ids)
        kb_chunk = retriever.kb_data[sid]
        started_at = datetime.now(timezone.utc)
        step_seq = 0
        seen_logs: set[str] = set()
        seen_step_keys: set[str] = set()
        trace_id = uuid.uuid4().hex
        question_id = f"{tenant_id}:{material_version_id or 'default'}:{i+1}:{sid}:{trace_id[:8]}"
        question_llm_trace: list[dict[str, Any]] = []
        question_trace: dict[str, Any] = {
            "run_id": run_id,
            "index": i + 1,
            "slice_id": sid,
            "slice_path": str(kb_chunk.get("完整路径", "")),
            "slice_content": _extract_slice_text(kb_chunk),
            "trace_id": trace_id,
            "question_id": question_id,
            "steps": [],
            "critic_result": {},
            "saved": False,
        }

        def _append_step(message: str, *, node: str = "", level: str = "info", detail: str = "") -> None:
            nonlocal step_seq
            dedupe_key = f"{node}|{level}|{message}|{detail}"
            if dedupe_key in seen_step_keys:
                return
            seen_step_keys.add(dedupe_key)
            step_seq += 1
            question_trace["steps"].append({
                "seq": step_seq,
                "node": node,
                "level": level,
                "message": message,
                "detail": detail,
                "time": datetime.now(timezone.utc).isoformat(),
            })

        _append_step("开始出题", node="system", detail=f"切片ID={sid}")
        inputs = {
            "kb_chunk": kb_chunk,
            "examples": [],
            "term_locks": [],
            "retry_count": 0,
            "logs": [],
            "trace_id": trace_id,
            "question_id": question_id,
            "llm_trace": [],
        }
        config = {
            "configurable": {
                "model": model_name,
                "api_key": api_key,
                "base_url": base_url,
                "retriever": retriever,
                "question_type": question_type,
                "generation_mode": generation_mode,
                "difficulty_range": difficulty_range,
            }
        }
        q_json = None
        saved_current = False
        critic_seen = False
        critic_passed = False
        try:
            for event in graph_app.stream(inputs, config=config):
                for node_name, state_update in event.items():
                    if not isinstance(state_update, dict):
                        continue
                    if node_name == "router":
                        details = state_update.get("router_details") or {}
                        agent = details.get("agent")
                        path = details.get("path")
                        _append_step(
                            "路由完成",
                            node=node_name,
                            detail=f"agent={agent or '-'} path={path or '-'}",
                        )
                    if node_name == "critic":
                        critic_result = state_update.get("critic_result") or {}
                        if isinstance(critic_result, dict) and ("passed" in critic_result):
                            question_trace["critic_result"] = critic_result
                            critic_seen = True
                            passed = bool(critic_result.get("passed"))
                            critic_passed = passed
                            reason = str(critic_result.get("reason", "")).strip()
                            _append_step(
                                "审核通过" if passed else "审核驳回",
                                node=node_name,
                                level="success" if passed else "warning",
                                detail=reason,
                            )
                    if node_name == "fixer":
                        fix_summary = state_update.get("fix_summary") or {}
                        changed = fix_summary.get("changed_fields") if isinstance(fix_summary, dict) else []
                        _append_step(
                            "执行修复",
                            node=node_name,
                            level="warning",
                            detail=f"changed={','.join(changed) if changed else '-'}",
                        )
                        logs = state_update.get("logs") or []
                        if isinstance(logs, list):
                            for log in logs:
                                text = str(log).strip()
                                if not text or text in seen_logs:
                                    continue
                                if _is_noisy_log(node_name, text):
                                    continue
                                seen_logs.add(text)
                                _append_step(text, node=node_name)
                    if isinstance(state_update, dict) and state_update.get("final_json"):
                        q_json = state_update.get("final_json")
                    _emit_node_highlights(node_name, state_update, _append_step)
                    llm_records = state_update.get("llm_trace") or []
                    if isinstance(llm_records, list):
                        question_llm_trace.extend([x for x in llm_records if isinstance(x, dict)])
            if q_json and critic_passed:
                q_json["来源路径"] = str(kb_chunk.get("完整路径", ""))
                q_json["来源切片ID"] = sid
                q_json["教材版本ID"] = material_version_id
                generated.append(q_json)
                saved_current = True
                _append_step("题目生成成功", node="system", level="success")
            elif q_json and not critic_seen:
                errors.append(f"第{i+1}题失败: 未经过 critic 审核")
                _append_step("未经过 critic 审核", node="critic", level="error")
            elif q_json and critic_seen and not critic_passed:
                errors.append(f"第{i+1}题失败: critic 未通过")
                _append_step("critic 未通过，题目未保存", node="critic", level="error")
            else:
                errors.append(f"第{i+1}题未产出 final_json")
                _append_step("未产出 final_json", node="writer", level="error")
        except Exception as e:
            errors.append(f"第{i+1}题失败: {e}")
            _append_step("出题异常", node="system", level="error", detail=str(e))
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        question_trace["elapsed_ms"] = elapsed_ms
        question_trace["llm_trace"] = question_llm_trace
        question_trace["llm_summary"] = summarize_llm_trace(question_llm_trace)
        question_trace["unstable_flags"] = mark_unstable(question_trace["llm_summary"])
        question_trace["saved"] = bool(saved_current)
        if isinstance(q_json, dict):
            question_trace["final_json"] = q_json
        if question_trace["unstable_flags"]:
            _append_step(
                "稳定性预警",
                node="system",
                level="warning",
                detail=",".join(question_trace["unstable_flags"]),
            )
        process_trace.append(question_trace)

    saved = 0
    if save_to_bank and generated:
        bank_path = tenant_bank_path(tenant_id)
        bank = _load_bank(bank_path)
        bank.extend(generated)
        _save_bank(bank_path, bank)
        saved = len(generated)

    run_ended_at = datetime.now(timezone.utc).isoformat()
    qa_run = _build_qa_run_payload(
        tenant_id=tenant_id,
        run_id=run_id,
        material_version_id=material_version_id,
        config_payload={
            "question_type": question_type,
            "generation_mode": generation_mode,
            "difficulty": difficulty,
            "difficulty_range": difficulty_range,
            "num_questions": num_questions,
                "model": model_name,
                "gen_scope_mode": gen_scope_mode,
                "task_id": task_id,
            },
        process_trace=process_trace,
        generated_count=len(generated),
        saved_count=saved,
        errors=errors,
        started_at=run_started_at,
        ended_at=run_ended_at,
    )
    _persist_qa_run(tenant_id, qa_run)

    if not generated and errors:
        return _error("GENERATION_FAILED", f"出题失败：{errors[0]}", 502)

    write_audit_log(
        tenant_id,
        system_user,
        "gen.create.batch",
        "question_generation",
        f"{tenant_id}:{datetime.now(timezone.utc).isoformat()}",
        after={
            "num_questions": num_questions,
            "generated": len(generated),
            "saved": saved,
            "errors": errors,
            "trace_count": len(process_trace),
            "question_type": question_type,
            "generation_mode": generation_mode,
            "material_version_id": material_version_id,
            "run_id": run_id,
        },
    )
    return _json_response(
        {
            "run_id": run_id,
            "items": generated,
            "generated_count": len(generated),
            "saved_count": saved,
            "errors": errors,
            "process_trace": process_trace,
            "material_version_id": material_version_id,
        }
    )


@app.post('/api/<tenant_id>/generate/stream')
def api_generate_questions_stream(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限执行出题", 403)

    body = request.get_json(silent=True) or {}
    gen_scope_mode = str(body.get("gen_scope_mode", "custom"))  # custom | per_slice
    num_questions = int(body.get("num_questions", 1))
    num_questions = min(max(num_questions, 1), 200)
    question_type = str(body.get("question_type", "单选题"))
    generation_mode = str(body.get("generation_mode", "灵活"))
    difficulty = str(body.get("difficulty", "随机"))
    slice_ids_input = body.get("slice_ids") or []
    save_to_bank = bool(body.get("save_to_bank", True))
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    task_id = str(body.get("task_id", "")).strip()

    if gen_scope_mode not in {"custom", "per_slice"}:
        return _error("BAD_REQUEST", "非法出题范围模式", 400)
    if question_type not in QUESTION_TYPES:
        return _error("BAD_REQUEST", "非法题型", 400)
    if generation_mode not in GEN_MODES:
        return _error("BAD_REQUEST", "非法出题模式", 400)

    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
    if not kb_items:
        return _error("NO_SLICES", "当前城市没有切片，请先上传教材并生成切片", 400)

    review_store = _load_slice_review_for_material(tenant_id, material_version_id)
    approved_ids = {
        int(k) for k, v in review_store.items()
        if str(k).isdigit() and isinstance(v, dict) and v.get("review_status") == "approved"
    }
    if not approved_ids:
        return _error("NO_APPROVED_SLICES", "当前城市暂无 approved 切片，无法出题", 400)

    selected_ids = set()
    for sid in slice_ids_input:
        try:
            selected_ids.add(int(sid))
        except (TypeError, ValueError):
            continue
    candidate_ids = sorted((selected_ids & approved_ids) if selected_ids else approved_ids)
    if not candidate_ids:
        return _error("NO_CANDIDATE_SLICES", "选中范围内没有 approved 切片", 400)
    if gen_scope_mode == "per_slice":
        num_questions = len(candidate_ids)
    if num_questions <= 0:
        return _error("BAD_REQUEST", "题量必须大于0", 400)

    set_active_tenant(tenant_id)
    os.environ["TENANT_ID"] = tenant_id
    if not kb_file:
        return _error("NO_SLICES_FILE", "当前城市没有可用切片文件", 400)
    retriever = KnowledgeRetriever(
        kb_path=str(kb_file),
        history_path=str(_resolve_history_path_for_material(tenant_id, material_version_id)),
        mapping_path=str(_resolve_mapping_path_for_material(tenant_id, material_version_id) or tenant_mapping_path(tenant_id)),
    )
    candidate_ids = [sid for sid in candidate_ids if 0 <= sid < len(retriever.kb_data)]
    if not candidate_ids:
        return _error("NO_VALID_SLICES", "审核记录与当前切片版本不一致，请重新审核切片后再出题", 400)

    cfg_path = Path("填写您的Key.txt")
    api_key = ""
    base_url = "https://api.deepseek.com"
    model_name = "deepseek-reasoner"
    if cfg_path.exists():
        cfg: dict[str, str] = {}
        for line in cfg_path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()

        def _usable_key(v: str) -> bool:
            return bool(v) and "请将您的Key" not in v

        for prefix in ("OPENAI", "DEEPSEEK", "CRITIC"):
            key = str(cfg.get(f"{prefix}_API_KEY", "")).strip()
            if not _usable_key(key):
                continue
            api_key = key
            candidate_base = str(cfg.get(f"{prefix}_BASE_URL", "")).strip()
            candidate_model = str(cfg.get(f"{prefix}_MODEL", "")).strip()
            if candidate_base and "http" in candidate_base:
                base_url = candidate_base
            if candidate_model:
                model_name = candidate_model
            break
    if not api_key:
        return _error("NO_API_KEY", "未配置可用 API Key，请检查 填写您的Key.txt", 400)

    difficulty_range = _parse_difficulty_range(difficulty)
    run_started_at = datetime.now(timezone.utc).isoformat()
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    def _sse(event_name: str, payload: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    @stream_with_context
    def _event_stream():
        generated: list[dict[str, Any]] = []
        errors: list[str] = []
        process_trace: list[dict[str, Any]] = []
        yield _sse(
            "started",
            {
                "run_id": run_id,
                "num_questions": num_questions,
                "material_version_id": material_version_id,
                "question_type": question_type,
                "generation_mode": generation_mode,
            },
        )
        for i in range(num_questions):
            sid = candidate_ids[i % len(candidate_ids)] if num_questions > len(candidate_ids) else random.choice(candidate_ids)
            kb_chunk = retriever.kb_data[sid]
            started_at = datetime.now(timezone.utc)
            step_seq = 0
            seen_logs: set[str] = set()
            seen_step_keys: set[str] = set()
            trace_id = uuid.uuid4().hex
            question_id = f"{tenant_id}:{material_version_id or 'default'}:{i+1}:{sid}:{trace_id[:8]}"
            question_llm_trace: list[dict[str, Any]] = []
            question_trace: dict[str, Any] = {
                "run_id": run_id,
                "index": i + 1,
                "slice_id": sid,
                "slice_path": str(kb_chunk.get("完整路径", "")),
                "slice_content": _extract_slice_text(kb_chunk),
                "trace_id": trace_id,
                "question_id": question_id,
                "steps": [],
                "critic_result": {},
                "saved": False,
            }
            yield _sse(
                "question_start",
                {
                    "index": i + 1,
                    "slice_id": sid,
                    "slice_path": question_trace["slice_path"],
                    "slice_content": question_trace["slice_content"],
                },
            )

            def _append_step(message: str, *, node: str = "", level: str = "info", detail: str = "") -> None:
                nonlocal step_seq
                dedupe_key = f"{node}|{level}|{message}|{detail}"
                if dedupe_key in seen_step_keys:
                    return
                seen_step_keys.add(dedupe_key)
                step_seq += 1
                step_payload = {
                    "seq": step_seq,
                    "node": node,
                    "level": level,
                    "message": message,
                    "detail": detail,
                    "time": datetime.now(timezone.utc).isoformat(),
                }
                question_trace["steps"].append(step_payload)
                yield_item = _sse("step", {"index": i + 1, **step_payload})
                _event_stream_buffer.append(yield_item)

            _event_stream_buffer: list[str] = []
            _append_step("开始出题", node="system", detail=f"切片ID={sid}")
            while _event_stream_buffer:
                yield _event_stream_buffer.pop(0)

            inputs = {
                "kb_chunk": kb_chunk,
                "examples": [],
                "term_locks": [],
                "retry_count": 0,
                "logs": [],
                "trace_id": trace_id,
                "question_id": question_id,
                "llm_trace": [],
            }
            config = {
                "configurable": {
                    "model": model_name,
                    "api_key": api_key,
                    "base_url": base_url,
                    "retriever": retriever,
                    "question_type": question_type,
                    "generation_mode": generation_mode,
                    "difficulty_range": difficulty_range,
                }
            }
            q_json = None
            saved_current = False
            critic_seen = False
            critic_passed = False
            try:
                for event in graph_app.stream(inputs, config=config):
                    for node_name, state_update in event.items():
                        if not isinstance(state_update, dict):
                            continue
                        if node_name == "router":
                            details = state_update.get("router_details") or {}
                            agent = details.get("agent")
                            path = details.get("path")
                            _append_step(
                                "路由完成",
                                node=node_name,
                                detail=f"agent={agent or '-'} path={path or '-'}",
                            )
                        if node_name == "critic":
                            critic_result = state_update.get("critic_result") or {}
                            if isinstance(critic_result, dict) and ("passed" in critic_result):
                                question_trace["critic_result"] = critic_result
                                critic_seen = True
                                passed = bool(critic_result.get("passed"))
                                critic_passed = passed
                                reason = str(critic_result.get("reason", "")).strip()
                                _append_step(
                                    "审核通过" if passed else "审核驳回",
                                    node=node_name,
                                    level="success" if passed else "warning",
                                    detail=reason,
                                )
                        if node_name == "fixer":
                            fix_summary = state_update.get("fix_summary") or {}
                            changed = fix_summary.get("changed_fields") if isinstance(fix_summary, dict) else []
                            _append_step(
                                "执行修复",
                                node=node_name,
                                level="warning",
                                detail=f"changed={','.join(changed) if changed else '-'}",
                            )
                        logs = state_update.get("logs") or []
                        if isinstance(logs, list):
                            for log in logs:
                                text = str(log).strip()
                                if not text or text in seen_logs:
                                    continue
                                if _is_noisy_log(node_name, text):
                                    continue
                                seen_logs.add(text)
                                _append_step(text, node=node_name)
                        if isinstance(state_update, dict) and state_update.get("final_json"):
                            q_json = state_update.get("final_json")
                        _emit_node_highlights(node_name, state_update, _append_step)
                        llm_records = state_update.get("llm_trace") or []
                        if isinstance(llm_records, list):
                            question_llm_trace.extend([x for x in llm_records if isinstance(x, dict)])
                        while _event_stream_buffer:
                            yield _event_stream_buffer.pop(0)
                if q_json and critic_passed:
                    q_json["来源路径"] = str(kb_chunk.get("完整路径", ""))
                    q_json["来源切片ID"] = sid
                    q_json["教材版本ID"] = material_version_id
                    generated.append(q_json)
                    saved_current = True
                    _append_step("题目生成成功", node="system", level="success")
                elif q_json and not critic_seen:
                    errors.append(f"第{i+1}题失败: 未经过 critic 审核")
                    _append_step("未经过 critic 审核", node="critic", level="error")
                elif q_json and critic_seen and not critic_passed:
                    errors.append(f"第{i+1}题失败: critic 未通过")
                    _append_step("critic 未通过，题目未保存", node="critic", level="error")
                else:
                    errors.append(f"第{i+1}题未产出 final_json")
                    _append_step("未产出 final_json", node="writer", level="error")
            except Exception as e:
                errors.append(f"第{i+1}题失败: {e}")
                _append_step("出题异常", node="system", level="error", detail=str(e))
            while _event_stream_buffer:
                yield _event_stream_buffer.pop(0)
            elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            question_trace["elapsed_ms"] = elapsed_ms
            question_trace["llm_trace"] = question_llm_trace
            question_trace["llm_summary"] = summarize_llm_trace(question_llm_trace)
            question_trace["unstable_flags"] = mark_unstable(question_trace["llm_summary"])
            question_trace["saved"] = bool(saved_current)
            if isinstance(q_json, dict):
                question_trace["final_json"] = q_json
            if question_trace["unstable_flags"]:
                _append_step(
                    "稳定性预警",
                    node="system",
                    level="warning",
                    detail=",".join(question_trace["unstable_flags"]),
                )
                while _event_stream_buffer:
                    yield _event_stream_buffer.pop(0)
            process_trace.append(question_trace)
            yield _sse(
                "question_done",
                {
                    "index": i + 1,
                    "elapsed_ms": elapsed_ms,
                    "item": q_json if saved_current and isinstance(q_json, dict) else None,
                    "trace": question_trace,
                    "generated_count": len(generated),
                    "saved_count": 0,
                    "error_count": len(errors),
                },
            )

        saved = 0
        if save_to_bank and generated:
            bank_path = tenant_bank_path(tenant_id)
            bank = _load_bank(bank_path)
            bank.extend(generated)
            _save_bank(bank_path, bank)
            saved = len(generated)

        run_ended_at = datetime.now(timezone.utc).isoformat()
        qa_run = _build_qa_run_payload(
            tenant_id=tenant_id,
            run_id=run_id,
            material_version_id=material_version_id,
            config_payload={
                "question_type": question_type,
                "generation_mode": generation_mode,
                "difficulty": difficulty,
                "difficulty_range": difficulty_range,
                "num_questions": num_questions,
                "model": model_name,
                "gen_scope_mode": gen_scope_mode,
                "task_id": task_id,
            },
            process_trace=process_trace,
            generated_count=len(generated),
            saved_count=saved,
            errors=errors,
            started_at=run_started_at,
            ended_at=run_ended_at,
        )
        _persist_qa_run(tenant_id, qa_run)

        if generated or errors:
            write_audit_log(
                tenant_id,
                system_user,
                "gen.create.batch",
                "question_generation",
                f"{tenant_id}:{datetime.now(timezone.utc).isoformat()}",
                after={
                    "num_questions": num_questions,
                    "generated": len(generated),
                    "saved": saved,
                    "errors": errors,
                    "trace_count": len(process_trace),
                    "question_type": question_type,
                    "generation_mode": generation_mode,
                    "material_version_id": material_version_id,
                    "run_id": run_id,
                },
            )

        yield _sse(
            "done",
            {
                "run_id": run_id,
                "items": generated,
                "generated_count": len(generated),
                "saved_count": saved,
                "errors": errors,
                "process_trace": process_trace,
                "material_version_id": material_version_id,
                "success": len(generated) > 0,
            },
        )

    resp = Response(_event_stream(), mimetype="text/event-stream")
    req_origin = request.headers.get("Origin", "")
    if req_origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = req_origin
        resp.headers["Vary"] = "Origin"
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-System-User'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    release_channel = getattr(g, "release_channel", "")
    if release_channel:
        resp.headers["X-Release-Channel"] = release_channel
    request_id = getattr(g, "request_id", "")
    if request_id:
        resp.headers["X-Request-Id"] = request_id
    return resp


def _parse_sse_chunk(raw: str) -> tuple[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    event_name = "message"
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return None
    data_text = "\n".join(data_lines)
    payload: Any = data_text
    try:
        payload = json.loads(data_text)
    except Exception:
        payload = data_text
    return event_name, payload


def _build_gen_task_summary(task: dict[str, Any]) -> dict[str, Any]:
    req = task.get("request") if isinstance(task.get("request"), dict) else {}
    return {
        "task_id": str(task.get("task_id", "")),
        "tenant_id": str(task.get("tenant_id", "")),
        "task_name": str(req.get("task_name", "") or str(task.get("task_name", "") or "")),
        "status": str(task.get("status", "pending")),
        "created_at": str(task.get("created_at", "")),
        "updated_at": str(task.get("updated_at", "")),
        "started_at": str(task.get("started_at", "")),
        "ended_at": str(task.get("ended_at", "")),
        "run_id": str(task.get("run_id", "")),
        "material_version_id": str(task.get("material_version_id", "")),
        "generated_count": int(task.get("generated_count", 0) or 0),
        "saved_count": int(task.get("saved_count", 0) or 0),
        "error_count": int(task.get("error_count", 0) or 0),
        "progress": task.get("progress") if isinstance(task.get("progress"), dict) else {"current": 0, "total": 0},
        "request": {
            "num_questions": int(req.get("num_questions", 0) or 0),
            "question_type": str(req.get("question_type", "")),
            "generation_mode": str(req.get("generation_mode", "")),
            "difficulty": str(req.get("difficulty", "")),
        },
    }


def _read_persisted_task(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    for row in reversed(_read_jsonl(_qa_gen_tasks_path(tenant_id))):
        if not isinstance(row, dict):
            continue
        if str(row.get("task_id", "")) == task_id:
            return row
    return None


def _qa_run_exists(tenant_id: str, run_id: str) -> bool:
    rid = str(run_id or "").strip()
    if not rid:
        return False
    for row in reversed(_read_jsonl(_qa_runs_path(tenant_id))):
        if not isinstance(row, dict):
            continue
        if str(row.get("run_id", "")) == rid:
            return True
    return False


def _persist_failed_task_qa_run(
    tenant_id: str,
    task: dict[str, Any],
    *,
    reason: str,
    started_at: str,
    ended_at: str,
) -> None:
    if not isinstance(task, dict):
        return
    run_id = str(task.get("run_id", "")).strip() or f"run_fail_{str(task.get('task_id', '')).strip()}"
    if _qa_run_exists(tenant_id, run_id):
        return
    req = task.get("request") if isinstance(task.get("request"), dict) else {}
    config_payload = {
        "question_type": str(req.get("question_type", "")),
        "generation_mode": str(req.get("generation_mode", "")),
        "difficulty": str(req.get("difficulty", "")),
        "num_questions": int(req.get("num_questions", 0) or 0),
        "gen_scope_mode": str(req.get("gen_scope_mode", "")),
        "task_id": str(task.get("task_id", "")),
    }
    errors = [str(x) for x in (task.get("errors") or []) if str(x)] or [str(reason or "任务失败")]
    qa_run = _build_qa_run_payload(
        tenant_id=tenant_id,
        run_id=run_id,
        material_version_id=str(task.get("material_version_id", "")),
        config_payload=config_payload,
        process_trace=[x for x in (task.get("process_trace") or []) if isinstance(x, dict)],
        generated_count=int(task.get("generated_count", 0) or 0),
        saved_count=int(task.get("saved_count", 0) or 0),
        errors=errors,
        started_at=str(started_at or task.get("started_at", "") or datetime.now(timezone.utc).isoformat()),
        ended_at=str(ended_at or datetime.now(timezone.utc).isoformat()),
    )
    _persist_qa_run(tenant_id, qa_run)


def _run_generate_task_worker(tenant_id: str, task_id: str, body: dict[str, Any], system_user: str) -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    _update_task_live(tenant_id, task_id, {"status": "running", "started_at": started_at})
    try:
        body_with_task = dict(body or {})
        body_with_task["task_id"] = task_id
        with app.test_request_context(
            f"/api/{tenant_id}/generate/stream",
            method="POST",
            json=body_with_task,
            headers={"X-System-User": system_user},
        ):
            resp = api_generate_questions_stream(tenant_id)
            status_code = int(getattr(resp, "status_code", 200) or 200)
            if status_code >= 400:
                payload = {}
                try:
                    payload = resp.get_json(silent=True) or {}
                except Exception:
                    payload = {}
                msg = str(((payload.get("error") if isinstance(payload, dict) else {}) or {}).get("message", "")).strip()
                if not msg:
                    msg = f"任务启动失败({status_code})"
                ended_at = datetime.now(timezone.utc).isoformat()
                failed_task = {
                    "status": "failed",
                    "ended_at": ended_at,
                    "errors": [msg],
                    "error_count": 1,
                }
                _update_task_live(tenant_id, task_id, failed_task)
                with GEN_TASK_LOCK:
                    task = GEN_TASKS.get(task_id)
                    if task:
                        _persist_failed_task_qa_run(tenant_id, task, reason=msg, started_at=started_at, ended_at=ended_at)
                        _persist_gen_task(tenant_id, task)
                return
            buffer = ""
            done_payload: dict[str, Any] | None = None
            for chunk in (resp.response or []):
                if isinstance(chunk, bytes):
                    buffer += chunk.decode("utf-8", errors="ignore")
                else:
                    buffer += str(chunk)
                while "\n\n" in buffer:
                    raw, buffer = buffer.split("\n\n", 1)
                    parsed = _parse_sse_chunk(raw)
                    if not parsed:
                        continue
                    event_name, data = parsed
                    if event_name == "started" and isinstance(data, dict):
                        _update_task_live(
                            tenant_id,
                            task_id,
                            {
                                "run_id": str(data.get("run_id", "")),
                                "material_version_id": str(data.get("material_version_id", "")),
                                "progress": {"current": 0, "total": int(data.get("num_questions", 0) or 0)},
                            },
                        )
                        continue
                    if event_name == "question_start" and isinstance(data, dict):
                        idx = int(data.get("index", 0) or 0)
                        trace_item = {
                            "index": idx,
                            "slice_id": int(data.get("slice_id", 0) or 0),
                            "slice_path": str(data.get("slice_path", "")),
                            "slice_content": str(data.get("slice_content", "")),
                            "steps": [],
                        }
                        _update_task_live(
                            tenant_id,
                            task_id,
                            {"progress": {"current": max(0, idx - 1), "total": int((GEN_TASKS.get(task_id, {}).get("progress") or {}).get("total", 0) or 0)}},
                            [trace_item],
                        )
                        continue
                    if event_name == "step" and isinstance(data, dict):
                        idx = int(data.get("index", 0) or 0)
                        _update_task_live(
                            tenant_id,
                            task_id,
                            {},
                            [{"index": idx, "steps": [data]}],
                        )
                        continue
                    if event_name == "question_done" and isinstance(data, dict):
                        idx = int(data.get("index", 0) or 0)
                        trace = data.get("trace") if isinstance(data.get("trace"), dict) else {"index": idx}
                        _update_task_live(
                            tenant_id,
                            task_id,
                            {
                                "generated_count": int(data.get("generated_count", 0) or 0),
                                "error_count": int(data.get("error_count", 0) or 0),
                                "progress": {
                                    "current": idx,
                                    "total": int((GEN_TASKS.get(task_id, {}).get("progress") or {}).get("total", 0) or 0),
                                },
                            },
                            [trace],
                        )
                        continue
                    if event_name == "done" and isinstance(data, dict):
                        done_payload = data
            if buffer.strip():
                parsed = _parse_sse_chunk(buffer)
                if parsed and parsed[0] == "done" and isinstance(parsed[1], dict):
                    done_payload = parsed[1]
            ended_at = datetime.now(timezone.utc).isoformat()
            if not isinstance(done_payload, dict):
                _update_task_live(
                    tenant_id,
                    task_id,
                    {"status": "failed", "ended_at": ended_at, "errors": ["任务异常结束，未收到完成事件"]},
                )
            else:
                _update_task_live(
                    tenant_id,
                    task_id,
                    {
                        "status": (
                            "completed"
                            if bool(done_payload.get("success", False)) or int(done_payload.get("generated_count", 0) or 0) > 0
                            else "failed"
                        ),
                        "ended_at": ended_at,
                        "run_id": str(done_payload.get("run_id", "")),
                        "material_version_id": str(done_payload.get("material_version_id", "")),
                        "items": list(done_payload.get("items") or []),
                        "errors": [str(x) for x in (done_payload.get("errors") or [])],
                        "generated_count": int(done_payload.get("generated_count", 0) or 0),
                        "saved_count": int(done_payload.get("saved_count", 0) or 0),
                        "progress": {
                            "current": int((GEN_TASKS.get(task_id, {}).get("progress") or {}).get("total", 0) or 0),
                            "total": int((GEN_TASKS.get(task_id, {}).get("progress") or {}).get("total", 0) or 0),
                        },
                    },
                    list(done_payload.get("process_trace") or []),
                )
            with GEN_TASK_LOCK:
                task = GEN_TASKS.get(task_id)
                if task:
                    if str(task.get("status", "")) == "failed":
                        _persist_failed_task_qa_run(
                            tenant_id,
                            task,
                            reason="任务异常结束，未收到完成事件",
                            started_at=started_at,
                            ended_at=ended_at,
                        )
                    _persist_gen_task(tenant_id, task)
    except Exception as e:
        ended_at = datetime.now(timezone.utc).isoformat()
        _update_task_live(
            tenant_id,
            task_id,
            {
                "status": "failed",
                "ended_at": ended_at,
                "errors": [str(e)],
            },
        )
        with GEN_TASK_LOCK:
            task = GEN_TASKS.get(task_id)
            if task:
                _persist_failed_task_qa_run(tenant_id, task, reason=str(e), started_at=started_at, ended_at=ended_at)
                _persist_gen_task(tenant_id, task)


@app.post('/api/<tenant_id>/generate/tasks')
def api_generate_task_create(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限执行出题", 403)
    body = request.get_json(silent=True) or {}
    task_name = str(body.get("task_name", "") or "").strip()
    if not task_name:
        return _error("BAD_REQUEST", "task_name is required", 400)
    normalized = task_name.casefold()
    with GEN_TASK_LOCK:
        for task in GEN_TASKS.values():
            if str(task.get("tenant_id", "")) != tenant_id:
                continue
            exist_name = str(task.get("task_name", "") or "").strip()
            if exist_name and exist_name.casefold() == normalized:
                return _error("BAD_REQUEST", "task_name already exists", 400)
    for task in _read_jsonl(_qa_gen_tasks_path(tenant_id)):
        if not isinstance(task, dict):
            continue
        exist_name = str(task.get("task_name", "") or "").strip()
        if exist_name and exist_name.casefold() == normalized:
            return _error("BAD_REQUEST", "task_name already exists", 400)
    body["task_name"] = task_name
    task = _make_gen_task(tenant_id, system_user, body)
    t = threading.Thread(
        target=_run_generate_task_worker,
        args=(tenant_id, str(task.get("task_id", "")), body, system_user),
        daemon=True,
    )
    t.start()
    return _json_response({"task": _build_gen_task_summary(task)})


@app.get('/api/<tenant_id>/generate/tasks')
def api_generate_task_list(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限查看出题任务", 403)
    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    rows: dict[str, dict[str, Any]] = {}
    with GEN_TASK_LOCK:
        for task in GEN_TASKS.values():
            if str(task.get("tenant_id", "")) != tenant_id:
                continue
            tid = str(task.get("task_id", ""))
            if tid:
                rows[tid] = _task_snapshot(task)
    for task in _read_jsonl(_qa_gen_tasks_path(tenant_id)):
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id", ""))
        if tid and tid not in rows:
            rows[tid] = task
    items = list(rows.values())
    items.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    items = items[:limit]
    return _json_response({"items": [_build_gen_task_summary(x) for x in items], "total": len(items)})


@app.get('/api/<tenant_id>/generate/tasks/<task_id>')
def api_generate_task_detail(tenant_id: str, task_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限查看出题任务详情", 403)
    with GEN_TASK_LOCK:
        task = GEN_TASKS.get(task_id)
        if task and str(task.get("tenant_id", "")) == tenant_id:
            return _json_response({"task": _task_snapshot(task)})
    persisted = _read_persisted_task(tenant_id, task_id)
    if isinstance(persisted, dict):
        return _json_response({"task": persisted})
    return _error("TASK_NOT_FOUND", "任务不存在", 404)


def _filter_qa_runs(
    tenant_id: str,
    *,
    material_version_id: str = "",
    days: int = 0,
) -> list[dict[str, Any]]:
    runs = _read_jsonl(_qa_runs_path(tenant_id))
    now_ts = datetime.now(timezone.utc).timestamp()
    out: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if material_version_id and str(run.get("material_version_id", "")) != material_version_id:
            continue
        if days > 0:
            ended_at = str(run.get("ended_at", "") or "")
            try:
                ts = datetime.fromisoformat(ended_at.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0.0
            if now_ts - ts > days * 24 * 3600:
                continue
        out.append(run)
    out.sort(key=lambda x: str(x.get("ended_at", "")), reverse=True)
    return out


def _decorate_alert_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        item = dict(r)
        due = _parse_iso_ts(str(item.get("sla_due_at", "") or ""))
        resolved = _parse_iso_ts(str(item.get("resolved_at", "") or ""))
        created = _parse_iso_ts(str(item.get("created_at", "") or ""))
        overdue = bool(due and (resolved is None) and now > due and str(item.get("status", "")) not in {"resolved", "ignored"})
        item["overdue"] = overdue
        if created and resolved:
            item["resolution_hours"] = round((resolved - created).total_seconds() / 3600.0, 3)
        else:
            item["resolution_hours"] = None
        out.append(item)
    return out


def _build_release_report(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    bm_base = base.get("batch_metrics") if isinstance(base.get("batch_metrics"), dict) else {}
    bm_target = target.get("batch_metrics") if isinstance(target.get("batch_metrics"), dict) else {}
    metrics = [
        ("hard_pass_rate", "higher_better"),
        ("quality_score_avg", "higher_better"),
        ("risk_high_rate", "lower_better"),
        ("logic_pass_rate", "higher_better"),
        ("duplicate_rate", "lower_better"),
        ("knowledge_match_rate", "higher_better"),
        ("avg_tokens_per_question", "lower_better"),
        ("avg_latency_ms_per_question", "lower_better"),
        ("avg_cost_per_question", "lower_better"),
        ("error_call_rate", "lower_better"),
    ]
    rows: list[dict[str, Any]] = []
    win = 0
    lose = 0
    for key, direct in metrics:
        b = float(bm_base.get(key, 0) or 0)
        t = float(bm_target.get(key, 0) or 0)
        delta = t - b
        better = (delta >= 0 and direct == "higher_better") or (delta <= 0 and direct == "lower_better")
        if abs(delta) < 1e-9:
            decision = "equal"
        elif better:
            decision = "better"
            win += 1
        else:
            decision = "worse"
            lose += 1
        rows.append({"metric": key, "base": b, "target": t, "delta": round(delta, 6), "direction": direct, "decision": decision})
    verdict = "hold"
    if win >= 7 and lose <= 2:
        verdict = "promote"
    elif lose >= 4:
        verdict = "rollback"
    return {
        "base_run_id": base.get("run_id", ""),
        "target_run_id": target.get("run_id", ""),
        "win_count": win,
        "lose_count": lose,
        "verdict": verdict,
        "conclusion": (
            "建议发布：核心指标整体改善" if verdict == "promote" else
            "建议回滚：关键指标明显退化" if verdict == "rollback" else
            "建议观察：指标有好有坏，需继续验证"
        ),
        "rows": rows,
    }


def _build_ops_weekly(rows: list[dict[str, Any]], *, days: int = 7) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    decorated = _decorate_alert_rows(rows)
    cutoff = now - timedelta(days=max(1, days))
    scoped = []
    for r in decorated:
        created = _parse_iso_ts(str(r.get("created_at", "") or ""))
        if created and created >= cutoff:
            scoped.append(r)
    total = len(scoped)
    open_cnt = sum(1 for r in scoped if str(r.get("status", "")) == "open")
    resolved = [r for r in scoped if str(r.get("status", "")) == "resolved"]
    overdue_cnt = sum(1 for r in scoped if bool(r.get("overdue")))
    high_cnt = sum(1 for r in scoped if str(r.get("level", "")) == "high")
    owner_map: dict[str, int] = {}
    for r in scoped:
        owner = str(r.get("owner", "") or "unassigned")
        owner_map[owner] = owner_map.get(owner, 0) + 1
    mttr_hours = _safe_div(sum(float(r.get("resolution_hours") or 0.0) for r in resolved), len(resolved))
    return {
        "days": days,
        "window_start": cutoff.isoformat(),
        "window_end": now.isoformat(),
        "total_alerts": total,
        "open_alerts": open_cnt,
        "resolved_alerts": len(resolved),
        "overdue_alerts": overdue_cnt,
        "high_alerts": high_cnt,
        "mttr_hours": round(mttr_hours, 3),
        "resolution_rate": round(_safe_div(len(resolved), total), 4),
        "owner_breakdown": [{"owner": k, "count": v} for k, v in sorted(owner_map.items(), key=lambda x: x[1], reverse=True)],
    }

@app.get('/api/<tenant_id>/qa/runs')
def api_qa_runs(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问评估数据", 403)
    page, page_size = _parse_pagination()
    material_version_id = str(request.args.get("material_version_id", "")).strip()
    days = max(0, int(request.args.get("days", 0) or 0))
    runs = _filter_qa_runs(tenant_id, material_version_id=material_version_id, days=days)
    items: list[dict[str, Any]] = []
    for r in runs:
        bm = r.get("batch_metrics") if isinstance(r.get("batch_metrics"), dict) else {}
        items.append(
            {
                "run_id": r.get("run_id", ""),
                "material_version_id": r.get("material_version_id", ""),
                "started_at": r.get("started_at", ""),
                "ended_at": r.get("ended_at", ""),
                "generated_count": bm.get("generated_count", 0),
                "saved_count": bm.get("saved_count", 0),
                "hard_pass_rate": bm.get("hard_pass_rate", 0),
                "quality_score_avg": bm.get("quality_score_avg", 0),
                "risk_high_rate": bm.get("risk_high_rate", 0),
                "avg_tokens_per_question": bm.get("avg_tokens_per_question", 0),
                "avg_latency_ms_per_question": bm.get("avg_latency_ms_per_question", 0),
                "avg_cost_per_question": bm.get("avg_cost_per_question", 0),
                "total_cost": bm.get("total_cost", 0),
                "currency": bm.get("currency", "CNY"),
                "error_call_rate": bm.get("error_call_rate", 0),
            }
        )
    payload = _paginate(items, page, page_size)
    payload["material_version_id"] = material_version_id
    payload["days"] = days
    return _json_response(payload)


@app.get('/api/<tenant_id>/qa/runs/<run_id>')
def api_qa_run_detail(tenant_id: str, run_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问评估详情", 403)
    runs = _read_jsonl(_qa_runs_path(tenant_id))
    target = next((x for x in runs if str(x.get("run_id", "")) == str(run_id)), None)
    if not isinstance(target, dict):
        return _error("RUN_NOT_FOUND", "评估运行不存在", 404)
    return _json_response(target)


@app.get('/api/<tenant_id>/qa/overview')
def api_qa_overview(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问评估总览", 403)
    material_version_id = str(request.args.get("material_version_id", "")).strip()
    days = max(0, int(request.args.get("days", 30) or 30))
    run_id = str(request.args.get("run_id", "")).strip()
    runs = _filter_qa_runs(tenant_id, material_version_id=material_version_id, days=days)
    if run_id:
        runs = [x for x in runs if str(x.get("run_id", "")) == run_id]
    if not runs:
        return _json_response(
            {
                "run_id": run_id,
                "material_version_id": material_version_id,
                "days": days,
                "run_count": 0,
                "hard_pass_rate": 0,
                "quality_score_avg": 0,
                "risk_high_rate": 0,
                "logic_pass_rate": 0,
                "duplicate_rate": 0,
                "knowledge_match_rate": 0,
                "avg_tokens_per_question": 0,
                "avg_latency_ms_per_question": 0,
                "avg_cost_per_question": 0,
                "total_cost": 0,
                "currency": "CNY",
                "avg_critic_loops": 0,
                "error_call_rate": 0,
            }
        )
    bm_list = [x.get("batch_metrics", {}) for x in runs if isinstance(x.get("batch_metrics"), dict)]
    n = len(bm_list)
    overview = {
        "run_id": run_id or str(runs[0].get("run_id", "")),
        "material_version_id": material_version_id,
        "days": days,
        "run_count": n,
        "hard_pass_rate": round(_safe_div(sum(float(x.get("hard_pass_rate", 0) or 0) for x in bm_list), n), 4),
        "quality_score_avg": round(_safe_div(sum(float(x.get("quality_score_avg", 0) or 0) for x in bm_list), n), 2),
        "risk_high_rate": round(_safe_div(sum(float(x.get("risk_high_rate", 0) or 0) for x in bm_list), n), 4),
        "logic_pass_rate": round(_safe_div(sum(float(x.get("logic_pass_rate", 0) or 0) for x in bm_list), n), 4),
        "duplicate_rate": round(_safe_div(sum(float(x.get("duplicate_rate", 0) or 0) for x in bm_list), n), 4),
        "knowledge_match_rate": round(_safe_div(sum(float(x.get("knowledge_match_rate", 0) or 0) for x in bm_list), n), 4),
        "avg_tokens_per_question": round(_safe_div(sum(float(x.get("avg_tokens_per_question", 0) or 0) for x in bm_list), n), 2),
        "avg_latency_ms_per_question": round(_safe_div(sum(float(x.get("avg_latency_ms_per_question", 0) or 0) for x in bm_list), n), 2),
        "avg_cost_per_question": round(_safe_div(sum(float(x.get("avg_cost_per_question", 0) or 0) for x in bm_list), n), 6),
        "total_cost": round(sum(float(x.get("total_cost", 0) or 0) for x in bm_list), 6),
        "currency": str((bm_list[0] or {}).get("currency", "CNY")),
        "avg_critic_loops": round(_safe_div(sum(float(x.get("avg_critic_loops", 0) or 0) for x in bm_list), n), 3),
        "error_call_rate": round(_safe_div(sum(float(x.get("error_call_rate", 0) or 0) for x in bm_list), n), 4),
    }
    return _json_response(overview)


@app.get('/api/<tenant_id>/qa/llm-calls')
def api_qa_llm_calls(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问模型调用明细", 403)
    run_id = str(request.args.get("run_id", "")).strip()
    question_id = str(request.args.get("question_id", "")).strip()
    page, page_size = _parse_pagination()
    runs = _read_jsonl(_qa_runs_path(tenant_id))
    if run_id:
        runs = [x for x in runs if str(x.get("run_id", "")) == run_id]
    else:
        runs.sort(key=lambda x: str(x.get("ended_at", "")), reverse=True)
        runs = runs[:1]
    calls: list[dict[str, Any]] = []
    for r in runs:
        rid = str(r.get("run_id", ""))
        for c in r.get("llm_calls") or []:
            if not isinstance(c, dict):
                continue
            if question_id and str(c.get("question_id", "")) != question_id:
                continue
            row = dict(c)
            row["run_id"] = rid
            calls.append(row)
    calls.sort(key=lambda x: str(x.get("ts", "")))
    payload = _paginate(calls, page, page_size)
    payload["run_id"] = run_id or (runs[0].get("run_id", "") if runs else "")
    payload["question_id"] = question_id
    return _json_response(payload)


@app.get('/api/<tenant_id>/qa/trends')
def api_qa_trends(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问趋势数据", 403)
    days = max(1, int(request.args.get("days", 30) or 30))
    material_version_id = str(request.args.get("material_version_id", "")).strip()
    runs = _filter_qa_runs(tenant_id, material_version_id=material_version_id, days=days)
    points: list[dict[str, Any]] = []
    for r in sorted(runs, key=lambda x: str(x.get("ended_at", ""))):
        bm = r.get("batch_metrics") if isinstance(r.get("batch_metrics"), dict) else {}
        points.append(
            {
                "date": str(r.get("ended_at", ""))[:10],
                "run_id": r.get("run_id", ""),
                "hard_pass_rate": bm.get("hard_pass_rate", 0),
                "quality_score_avg": bm.get("quality_score_avg", 0),
                "risk_high_rate": bm.get("risk_high_rate", 0),
                "logic_pass_rate": bm.get("logic_pass_rate", 0),
                "avg_tokens_per_question": bm.get("avg_tokens_per_question", 0),
                "avg_latency_ms_per_question": bm.get("avg_latency_ms_per_question", 0),
                "avg_cost_per_question": bm.get("avg_cost_per_question", 0),
                "total_cost": bm.get("total_cost", 0),
                "currency": bm.get("currency", "CNY"),
                "error_call_rate": bm.get("error_call_rate", 0),
            }
        )
    return _json_response({"days": days, "material_version_id": material_version_id, "points": points})


@app.get('/api/<tenant_id>/qa/drift')
def api_qa_drift(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问漂移对比", 403)
    base_run_id = str(request.args.get("base_run_id", "")).strip()
    target_run_id = str(request.args.get("target_run_id", "")).strip()
    if not base_run_id or not target_run_id:
        return _error("BAD_REQUEST", "base_run_id 和 target_run_id 必填", 400)
    runs = _read_jsonl(_qa_runs_path(tenant_id))
    base = next((x for x in runs if str(x.get("run_id", "")) == base_run_id), None)
    target = next((x for x in runs if str(x.get("run_id", "")) == target_run_id), None)
    if not isinstance(base, dict) or not isinstance(target, dict):
        return _error("RUN_NOT_FOUND", "对比运行不存在", 404)
    bm_base = base.get("batch_metrics") if isinstance(base.get("batch_metrics"), dict) else {}
    bm_target = target.get("batch_metrics") if isinstance(target.get("batch_metrics"), dict) else {}
    keys = [
        "hard_pass_rate",
        "quality_score_avg",
        "risk_high_rate",
        "logic_pass_rate",
        "duplicate_rate",
        "knowledge_match_rate",
        "avg_tokens_per_question",
        "avg_latency_ms_per_question",
        "avg_cost_per_question",
        "total_cost",
        "avg_critic_loops",
        "error_call_rate",
    ]
    compare = {}
    for k in keys:
        bv = float(bm_base.get(k, 0) or 0)
        tv = float(bm_target.get(k, 0) or 0)
        compare[k] = {"base": bv, "target": tv, "delta": round(tv - bv, 4)}
    return _json_response({"base_run_id": base_run_id, "target_run_id": target_run_id, "compare": compare})


@app.get('/api/<tenant_id>/qa/thresholds')
def api_qa_thresholds_get(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问阈值配置", 403)
    return _json_response(_load_qa_thresholds(tenant_id))


@app.put('/api/<tenant_id>/qa/thresholds')
def api_qa_thresholds_put(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限更新阈值配置", 403)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return _error("BAD_REQUEST", "请求体必须是 JSON 对象", 400)
    return _json_response(_save_qa_thresholds(tenant_id, body))


@app.get('/api/<tenant_id>/qa/pricing')
def api_qa_pricing_get(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问成本配置", 403)
    return _json_response(_load_qa_pricing(tenant_id))


@app.put('/api/<tenant_id>/qa/pricing')
def api_qa_pricing_put(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限更新成本配置", 403)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return _error("BAD_REQUEST", "请求体必须是 JSON 对象", 400)
    return _json_response(_save_qa_pricing(tenant_id, body))


@app.get('/api/<tenant_id>/qa/alerts')
def api_qa_alerts_get(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问告警列表", 403)
    page, page_size = _parse_pagination()
    run_id = str(request.args.get("run_id", "")).strip()
    status = str(request.args.get("status", "")).strip()
    level = str(request.args.get("level", "")).strip()
    rows = _read_jsonl(_qa_alerts_path(tenant_id))
    items = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if run_id and str(r.get("run_id", "")) != run_id:
            continue
        if status and str(r.get("status", "")) != status:
            continue
        if level and str(r.get("level", "")) != level:
            continue
        items.append(r)
    items = _decorate_alert_rows(items)
    items.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    payload = _paginate(items, page, page_size)
    return _json_response(payload)


@app.put('/api/<tenant_id>/qa/alerts/<alert_id>/status')
def api_qa_alert_status_put(tenant_id: str, alert_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限更新告警状态", 403)
    body = request.get_json(silent=True) or {}
    next_status = str(body.get("status", "")).strip()
    if next_status not in {"open", "ack", "ignored", "resolved"}:
        return _error("BAD_REQUEST", "status 非法", 400)
    owner = str(body.get("owner", "")).strip()
    path = _qa_alerts_path(tenant_id)
    rows = _read_jsonl(path)
    updated = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("alert_id", "")) != alert_id:
            continue
        r["status"] = next_status
        r["owner"] = owner or system_user
        r["updated_at"] = now_iso
        if next_status == "ack" and not str(r.get("acked_at", "")):
            r["acked_at"] = now_iso
        if next_status == "resolved":
            r["resolved_at"] = now_iso
        updated += 1
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows), encoding="utf-8")
    if updated <= 0:
        return _error("ALERT_NOT_FOUND", "告警不存在", 404)
    return _json_response({"updated": updated, "alert_id": alert_id, "status": next_status})


@app.get('/api/<tenant_id>/qa/release-report')
def api_qa_release_report(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问发布评估报告", 403)
    base_run_id = str(request.args.get("base_run_id", "")).strip()
    target_run_id = str(request.args.get("target_run_id", "")).strip()
    if not base_run_id or not target_run_id:
        return _error("BAD_REQUEST", "base_run_id 和 target_run_id 必填", 400)
    runs = _read_jsonl(_qa_runs_path(tenant_id))
    base = next((x for x in runs if str(x.get("run_id", "")) == base_run_id), None)
    target = next((x for x in runs if str(x.get("run_id", "")) == target_run_id), None)
    if not isinstance(base, dict) or not isinstance(target, dict):
        return _error("RUN_NOT_FOUND", "对比运行不存在", 404)
    return _json_response(_build_release_report(base, target))


@app.get('/api/<tenant_id>/qa/ops-weekly')
def api_qa_ops_weekly(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问运营周报", 403)
    days = max(1, int(request.args.get("days", 7) or 7))
    run_id = str(request.args.get("run_id", "")).strip()
    rows = _read_jsonl(_qa_alerts_path(tenant_id))
    if run_id:
        rows = [x for x in rows if isinstance(x, dict) and str(x.get("run_id", "")) == run_id]
    return _json_response(_build_ops_weekly(rows, days=days))


@app.get('/api/<tenant_id>/bank')
def api_bank_list(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问题库", 403)

    keyword = request.args.get("keyword", "").strip()
    requested_material_version_id = str(request.args.get("material_version_id", "")).strip()
    all_materials = requested_material_version_id == "__all__"
    page, page_size = _parse_pagination()
    material_version_id = "" if all_materials else _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not all_materials and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    bank_path = tenant_bank_path(tenant_id)
    bank = _load_bank(bank_path)
    items: list[dict[str, Any]] = []
    for idx, q in enumerate(bank):
        stem = str(q.get("题干", "")).strip()
        q_material = str(q.get("教材版本ID", "")).strip()
        if material_version_id:
            if q_material and q_material != material_version_id:
                continue
            if not q_material:
                # legacy question without material marker: only show in "全部教材"
                continue
        if keyword and keyword not in stem:
            continue
        item = dict(q)
        item["question_id"] = idx
        items.append(item)
    payload = _paginate(items, page, page_size)
    payload["material_version_id"] = material_version_id
    return _json_response(payload)


@app.post('/api/<tenant_id>/bank/delete')
def api_bank_delete(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限删除题库题目", 403)

    body = request.get_json(silent=True) or {}
    ids = body.get("question_ids") or []
    if not ids:
        return _error("BAD_REQUEST", "question_ids is required", 400)
    delete_ids = set()
    for x in ids:
        try:
            delete_ids.add(int(x))
        except (TypeError, ValueError):
            continue
    bank_path = tenant_bank_path(tenant_id)
    bank = _load_bank(bank_path)
    kept = [q for i, q in enumerate(bank) if i not in delete_ids]
    _save_bank(bank_path, kept)
    deleted = len(bank) - len(kept)
    write_audit_log(
        tenant_id,
        system_user,
        "bank.delete.batch",
        "question_bank",
        ",".join(str(x) for x in sorted(delete_ids)),
        after={"deleted": deleted},
    )
    return _json_response({"deleted": deleted, "remaining": len(kept)})


@app.post('/api/<tenant_id>/bank/export')
def api_bank_export(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限导出题库", 403)

    body = request.get_json(silent=True) or {}
    ids = body.get("question_ids") or []
    if not isinstance(ids, list) or not ids:
        return _error("BAD_REQUEST", "question_ids is required", 400)

    selected_ids = set()
    for x in ids:
        try:
            selected_ids.add(int(x))
        except (TypeError, ValueError):
            continue
    if not selected_ids:
        return _error("BAD_REQUEST", "无有效 question_ids", 400)

    bank = _load_bank(tenant_bank_path(tenant_id))
    selected_rows = [(idx, q) for idx, q in enumerate(bank) if idx in selected_ids and isinstance(q, dict)]
    if not selected_rows:
        return _error("BAD_REQUEST", "未命中可导出题目", 400)

    export_rows: list[dict[str, Any]] = []
    for _, q in selected_rows:
        path = str(q.get("来源路径", "") or "").strip()
        parts = [p.strip() for p in path.split(" > ") if p.strip()]
        raw_answer = q.get("正确答案", "")
        answer = str(raw_answer).strip().upper() if raw_answer else ""
        raw_diff = q.get("难度值", 0.5)
        try:
            difficulty = float(raw_diff) if raw_diff not in [None, "", "未知"] else 0.5
        except (ValueError, TypeError):
            difficulty = 0.5

        def safe_str(val: Any, default: str = "") -> str:
            if val is None:
                return default
            return str(val).strip() if val else default

        export_rows.append({
            "题干(必填)": safe_str(q.get("题干", "")),
            "选项A(必填)": safe_str(q.get("选项1", "")),
            "选项B(必填)": safe_str(q.get("选项2", "")),
            "选项C": safe_str(q.get("选项3", "")),
            "选项D": safe_str(q.get("选项4", "")),
            "选项E": safe_str(q.get("选项5", "")),
            "选项F": safe_str(q.get("选项6", "")),
            "选项G": safe_str(q.get("选项7", "")),
            "选项H": safe_str(q.get("选项8", "")),
            "答案选项(必填)": answer,
            "难度": difficulty,
            "一级知识点": safe_str(q.get("一级知识点", "")) or (parts[0] if len(parts) > 0 else ""),
            "二级知识点": safe_str(q.get("二级知识点", "")) or (parts[1] if len(parts) > 1 else ""),
            "三级知识点": safe_str(q.get("三级知识点", "")) or (parts[2] if len(parts) > 2 else ""),
            "四级知识点": safe_str(q.get("四级知识点", "")) or (parts[3] if len(parts) > 3 else ""),
            "题目解析": safe_str(q.get("解析", "")),
            "切片原文": safe_str(q.get("切片原文", "")) or _extract_slice_text(q),
            "结构化内容": _stringify_structured_value(q.get("结构化内容", "")),
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
        "一级知识点",
        "二级知识点",
        "三级知识点",
        "四级知识点",
        "题目解析",
        "切片原文",
        "结构化内容",
    ])

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False)
    data = buffer.getvalue()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{tenant_id}_question_bank_{ts}.xlsx"
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.post('/api/<tenant_id>/bank/add')
def api_bank_add(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限写入题库", 403)

    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    if not isinstance(items, list) or not items:
        return _error("BAD_REQUEST", "items is required", 400)
    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    valid_items: list[dict[str, Any]] = []
    for x in items:
        if not isinstance(x, dict):
            continue
        q = dict(x)
        q.pop("question_id", None)
        q.pop("_gen_key", None)
        if material_version_id and not str(q.get("教材版本ID", "")).strip():
            q["教材版本ID"] = material_version_id
        if not str(q.get("题干", "")).strip():
            continue
        valid_items.append(q)
    if not valid_items:
        return _error("BAD_REQUEST", "无可入库题目", 400)

    bank_path = tenant_bank_path(tenant_id)
    bank = _load_bank(bank_path)
    bank.extend(valid_items)
    _save_bank(bank_path, bank)

    write_audit_log(
        tenant_id,
        system_user,
        "bank.add.batch",
        "question_bank",
        f"{tenant_id}:{datetime.now(timezone.utc).isoformat()}",
        after={"added": len(valid_items), "material_version_id": material_version_id},
    )
    return _json_response({"added": len(valid_items), "total": len(bank), "material_version_id": material_version_id})


@app.post('/api/<tenant_id>/mappings/review/batch')
def api_mappings_batch_review(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "map.confirm")
    except PermissionError as e:
        return _error(str(e), "无权限批量确认映射", 403)

    body = request.get_json(silent=True) or {}
    map_keys = body.get('map_keys') or []
    confirm_status = _normalize_mapping_status(body.get('confirm_status', 'approved'))
    comment = str(body.get('comment', ''))
    reviewer = str(body.get('reviewer') or system_user)
    target = str(body.get('target_mother_question_id', ''))
    requested_material_version_id = str(body.get('material_version_id', '')).strip()

    if not map_keys:
        return _error("BAD_REQUEST", "map_keys is required", 400)
    if confirm_status not in MAP_STATUSES:
        return _error("INVALID_STATUS", "非法映射确认状态", 400)
    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    if not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "当前城市暂无教材版本", 400)

    updated = 0
    for mk in map_keys:
        _upsert_mapping_review_for_material(
            tenant_id=tenant_id,
            material_version_id=material_version_id,
            map_key=str(mk),
            confirm_status=confirm_status,
            reviewer=reviewer,
            comment=comment,
            target_mother_question_id=target,
        )
        write_audit_log(tenant_id, reviewer, 'map.confirm.batch', 'slice_question_map', str(mk))
        updated += 1
    return _json_response({'updated': updated, 'material_version_id': material_version_id})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8600, debug=False, use_reloader=False)
