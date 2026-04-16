from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import random
import time
import uuid
from io import BytesIO
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from copy import deepcopy
from urllib.parse import quote, urlsplit, urlunsplit

import pandas as pd
from flask import Flask, Response, g, jsonify, redirect, request, send_file, stream_with_context
from werkzeug.exceptions import HTTPException

from authn import AccessDenied, Principal, resolve_legacy_principal, resolve_principal
from audit_log import write_audit_log
from governance import circuit_breaker, rate_limiter, select_release_channel
from mapping_review_store import load_mapping_review
from observability import init_observability, start_span
from runtime_paths import ensure_parent, repo_tenant_data_dir, resolve_primary_key_file, runtime_key_file
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
    TenantDataMissingError,
    delete_tenant,
    list_tenants,
    resolve_tenant_history_path,
    set_tenant_status,
    tenant_audit_log_path,
    tenant_generation_template_path,
    tenant_mapping_path,
    tenant_mapping_review_path,
    tenant_root,
    tenant_slices_dir,
    tenant_bank_path,
    upsert_tenant,
)
from tenant_context import get_accessible_tenants, assert_tenant_access, enforce_permission, load_acl, save_acl
from exam_factory import KnowledgeRetriever, build_knowledge_retriever
from exam_graph import (
    app as graph_app,
    attach_question_wall_clock_budget,
    call_llm,
    detach_question_wall_clock_budget,
    detect_router_high_risk_slice,
    mark_unstable,
    parse_json_from_response,
    summarize_llm_trace,
)
from reference_loader import load_reference_questions
from sso_auth import SSOError, SSOManager

app = Flask(__name__)
init_observability("exam-admin-api")
SSO_MANAGER = SSOManager()

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8600
FRONTEND_PORT = 8522

SLICE_STATUSES = {"pending", "approved"}
MAP_STATUSES = {"pending", "approved"}
QUESTION_TYPES = {"单选题", "多选题", "判断题", "随机"}
GEN_MODES = {"基础概念/理解记忆", "实战应用/推演", "随机"}
ALLOWED_ORIGINS = set(
    x.strip()
    for x in os.getenv(
        "ADMIN_WEB_ORIGINS",
        f"http://127.0.0.1:{FRONTEND_PORT},http://localhost:{FRONTEND_PORT}",
    ).split(",")
    if x.strip()
)
PRIMARY_KEY_FILE = runtime_key_file()
_KEY_PLACEHOLDER_MARKERS = ("请将您的Key", "在这里填写", "your_key", "YOUR_KEY")


def _new_material_version_id(tenant_id: str, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    base = current.strftime("v%Y%m%d_%H%M%S")
    existing = {
        str(item.get("material_version_id", "")).strip()
        for item in list_material_versions(tenant_id)
        if isinstance(item, dict)
    }
    if base not in existing:
        return base
    for _ in range(8):
        candidate = f"{base}_{uuid.uuid4().hex[:4]}"
        if candidate not in existing:
            return candidate
    return f"{base}_{uuid.uuid4().hex[:8]}"


def _load_primary_key_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    key_file = resolve_primary_key_file()
    if not key_file.exists():
        return cfg
    try:
        for line in key_file.read_text(encoding="utf-8").splitlines():
            raw = str(line).strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            key = str(k).strip()
            val = str(v).strip()
            if key:
                cfg[key] = val
    except Exception:
        return {}
    return cfg


def _is_usable_secret(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    for marker in _KEY_PLACEHOLDER_MARKERS:
        if marker in text:
            return False
    return True


def _autoload_primary_key_env(*, override: bool = False) -> dict[str, str]:
    cfg = _load_primary_key_config()
    if not cfg:
        return {}
    for k, v in cfg.items():
        if _is_usable_secret(v) and (override or not os.environ.get(k)):
            os.environ[k] = str(v).strip()
    _sync_key_modules(cfg)
    return cfg


def _sync_key_modules(cfg: dict) -> None:
    """将 Key 配置同步回 exam_factory / exam_graph 的模块级变量。

    exam_factory 和 exam_graph 里的 API_KEY / BASE_URL / *_MODEL 是模块级常量，
    在进程启动时求值。当管理后台在线写入 Key 后，需要主动把这些变量更新，
    否则那些直接引用模块级常量的 LLM 调用仍然用空 key，导致 no_question。
    """
    import exam_factory
    import exam_graph as _exam_graph_mod

    api_key = (
        str(cfg.get("AIT_API_KEY") or cfg.get("OPENAI_API_KEY") or cfg.get("DEEPSEEK_API_KEY") or "").strip()
    )
    base_url = (
        str(cfg.get("AIT_BASE_URL") or cfg.get("OPENAI_BASE_URL") or cfg.get("DEEPSEEK_BASE_URL") or "").strip()
        or "https://openapi-ait.ke.com/v1"
    )
    model_name = (
        str(cfg.get("AIT_MODEL") or cfg.get("OPENAI_MODEL") or cfg.get("DEEPSEEK_MODEL") or "").strip()
        or "deepseek-v3.2"
    )
    _EMPTY = ("", None)

    def _pick(key: str, fallback: str) -> str:
        v = str(cfg.get(key) or "").strip()
        return v if v else fallback

    for mod in (exam_factory, _exam_graph_mod):
        if api_key:
            if getattr(mod, "API_KEY", None) in _EMPTY:
                mod.API_KEY = api_key
            mod.API_KEY = api_key  # always refresh on override
        if base_url:
            mod.BASE_URL = base_url
        if model_name:
            if getattr(mod, "MODEL_NAME", None) in _EMPTY:
                mod.MODEL_NAME = model_name
            mod.MODEL_NAME = model_name

    # Sync role-specific models
    for attr, key in (
        ("WRITER_MODEL", "WRITER_MODEL"),
        ("ROUTER_MODEL", "ROUTER_MODEL"),
        ("SPECIALIST_MODEL", "SPECIALIST_MODEL"),
        ("CALC_MODEL", "CALC_MODEL"),
        ("CRITIC_API_KEY", "CRITIC_API_KEY"),
        ("CRITIC_BASE_URL", "CRITIC_BASE_URL"),
        ("CRITIC_MODEL", "CRITIC_MODEL"),
        ("CODE_GEN_API_KEY", "CODE_GEN_API_KEY"),
        ("CODE_GEN_BASE_URL", "CODE_GEN_BASE_URL"),
        ("CODE_GEN_MODEL", "CODE_GEN_MODEL"),
        ("ARK_API_KEY", "ARK_API_KEY"),
    ):
        v = _pick(key, "")
        if not v:
            continue
        for mod in (exam_factory, _exam_graph_mod):
            if hasattr(mod, attr):
                setattr(mod, attr, v)


def _resolve_generation_llm_from_primary_key() -> tuple[str, str, str]:
    cfg = _autoload_primary_key_env(override=True)
    api_key = ""
    base_url = "https://openapi-ait.ke.com/v1"
    model_name = "deepseek-v3.2"
    for prefix in ("AIT", "OPENAI", "DEEPSEEK", "CRITIC"):
        key = str(cfg.get(f"{prefix}_API_KEY", "")).strip()
        if not _is_usable_secret(key):
            continue
        api_key = key
        candidate_base = str(cfg.get(f"{prefix}_BASE_URL", "")).strip()
        candidate_model = str(cfg.get(f"{prefix}_MODEL", "")).strip()
        if candidate_base and "http" in candidate_base:
            base_url = candidate_base
        if candidate_model:
            model_name = candidate_model
        break
    return api_key, base_url, model_name


def _save_primary_key_config_text(content: str) -> dict[str, Any]:
    normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    ensure_parent(PRIMARY_KEY_FILE).write_text(normalized, encoding="utf-8")
    try:
        os.chmod(PRIMARY_KEY_FILE, 0o600)
    except Exception:
        pass
    cfg = _autoload_primary_key_env(override=True)
    return {
        "path": str(PRIMARY_KEY_FILE),
        "exists": PRIMARY_KEY_FILE.exists(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "line_count": len(normalized.splitlines()) if normalized else 0,
        "has_ait_api_key": _is_usable_secret(cfg.get("AIT_API_KEY", "")),
        "has_openai_api_key": _is_usable_secret(cfg.get("OPENAI_API_KEY", "")),
        "has_deepseek_api_key": _is_usable_secret(cfg.get("DEEPSEEK_API_KEY", "")),
        "has_critic_api_key": _is_usable_secret(cfg.get("CRITIC_API_KEY", "")),
        "has_git_username": _is_usable_secret(cfg.get("GIT_USERNAME", "")),
        "has_git_token": _is_usable_secret(cfg.get("GIT_TOKEN", "")) or _is_usable_secret(cfg.get("GIT_PASSWORD", "")),
    }


# Auto-load once at process startup so all requests can directly use env/config.
_autoload_primary_key_env(override=False)


@app.get("/")
def root_status():
    """简单健康检查/本地调试入口，不做认证。"""
    return _json_response(
        {
            "ok": True,
            "message": "exam-admin-api is running",
            "hint": "业务接口请通过 /api/... 访问，前端会自动带上 X-System-User / Authorization 头。",
        }
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


def _normalize_generation_mode(raw_mode: Any) -> str:
    """
    统一出题筛选条件，并兼容历史“灵活/严谨”取值。
    """
    mode = str(raw_mode or "").strip()
    if mode in GEN_MODES:
        return mode
    if mode == "灵活":
        return "实战应用/推演"
    if mode == "严谨":
        return "基础概念/理解记忆"
    return "随机"


def _slice_forbidden_question_types(kb_item: dict[str, Any]) -> tuple[set[str], str]:
    """
    Return forbidden question types for one slice.
    Current hard rule: slices with parallel rules/material checklists prohibit 单选题.
    """
    path = str((kb_item or {}).get("完整路径", "") or "").strip()
    content = _extract_slice_text(kb_item)
    profile = detect_router_high_risk_slice(content, path)
    forbidden: set[str] = set()
    reasons: list[str] = []
    if bool(profile.get("prohibit_single_choice")):
        forbidden.add("单选题")
        if bool(profile.get("has_material_checklist")):
            reasons.append("材料清单切片")
        if bool(profile.get("has_parallel_rules")):
            reasons.append("并列规则切片")
        if not reasons:
            reasons.append("高风险切片")
    return forbidden, "、".join(reasons) if reasons else ""


def _filter_candidate_ids_by_question_type(
    retriever: KnowledgeRetriever,
    candidate_ids: list[int],
    question_type: str,
) -> tuple[list[int], list[dict[str, Any]]]:
    """
    If question_type is explicitly selected (not 随机), remove slices that forbid this type.
    """
    qtype = str(question_type or "").strip()
    if qtype not in {"单选题", "多选题", "判断题"}:
        return list(candidate_ids), []
    filtered: list[int] = []
    skipped: list[dict[str, Any]] = []
    for sid in candidate_ids:
        kb_item = retriever.kb_data[sid]
        forbidden, reason = _slice_forbidden_question_types(kb_item)
        if qtype in forbidden:
            skipped.append(
                {
                    "slice_id": int(sid),
                    "path": str(kb_item.get("完整路径", "") or ""),
                    "forbidden_type": qtype,
                    "reason": reason or "切片禁止该题型",
                }
            )
            continue
        filtered.append(int(sid))
    return filtered, skipped


GEN_TEMPLATE_MASTERIES = ("掌握", "熟悉", "了解")


def _normalize_template_ratio_map(raw: Any, *, keys: tuple[str, ...], allow_zero: bool = False) -> dict[str, float]:
    values = raw if isinstance(raw, dict) else {}
    out: dict[str, float] = {}
    for key in keys:
        try:
            value = float(values.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value < 0:
            value = 0.0
        out[key] = value
    total = sum(out.values())
    if total <= 0:
        if allow_zero:
            return {key: 0.0 for key in keys}
        raise ValueError("占比必须大于0")
    return out


def _largest_remainder_counts(total: int, weights: list[float]) -> list[int]:
    if total <= 0 or not weights:
        return [0 for _ in weights]
    safe_weights = [max(float(w or 0), 0.0) for w in weights]
    weight_sum = sum(safe_weights)
    if weight_sum <= 0:
        raise ValueError("权重必须大于0")
    exacts = [total * w / weight_sum for w in safe_weights]
    floors = [int(x) for x in exacts]
    remainder = total - sum(floors)
    order = sorted(
        range(len(weights)),
        key=lambda idx: (exacts[idx] - floors[idx], safe_weights[idx], -idx),
        reverse=True,
    )
    for idx in order[:remainder]:
        floors[idx] += 1
    return floors


def _gen_template_path(tenant_id: str) -> Path:
    return tenant_generation_template_path(tenant_id)


def _load_gen_templates(tenant_id: str) -> list[dict[str, Any]]:
    payload = _read_json(_gen_template_path(tenant_id), {"items": []})
    items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _save_gen_templates(tenant_id: str, items: list[dict[str, Any]]) -> None:
    _write_json(_gen_template_path(tenant_id), {"items": items})


def _normalize_route_rules(raw_rules: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_rules, list):
        return []
    normalized: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            continue
        path_prefix = str(raw.get("path_prefix", "")).strip()
        if not path_prefix:
            continue
        try:
            ratio = float(raw.get("ratio", 0) or 0)
        except (TypeError, ValueError):
            ratio = 0.0
        if ratio <= 0:
            continue
        normalized.append(
            {
                "rule_id": str(raw.get("rule_id", "")).strip() or f"route_{idx + 1}",
                "path_prefix": path_prefix,
                "ratio": ratio,
            }
        )
    return normalized


def _sanitize_gen_template(item: dict[str, Any]) -> dict[str, Any]:
    template_id = str(item.get("template_id", "")).strip()
    name = str(item.get("name", "")).strip()
    description = str(item.get("description", "")).strip()
    material_version_id = str(item.get("material_version_id", "")).strip()
    question_count = int(item.get("question_count", 1) or 1)
    question_count = min(max(question_count, 1), 200)
    route_rules = _normalize_route_rules(item.get("route_rules"))
    mastery_ratio = _normalize_template_ratio_map(
        item.get("mastery_ratio") or {},
        keys=GEN_TEMPLATE_MASTERIES,
    )
    mastery_total = sum(mastery_ratio.values())
    route_total = sum(float(rule.get("ratio", 0) or 0) for rule in route_rules)
    return {
        "template_id": template_id,
        "name": name,
        "description": description,
        "material_version_id": material_version_id,
        "question_count": question_count,
        "mastery_ratio": mastery_ratio,
        "mastery_percentages": {
            key: round(float(value) * 100.0 / mastery_total, 2)
            for key, value in mastery_ratio.items()
        },
        "route_rules": route_rules,
        "route_total_ratio": round(route_total, 4),
        "created_at": str(item.get("created_at", "")).strip(),
        "updated_at": str(item.get("updated_at", "")).strip(),
    }


def _validate_gen_template_payload(tenant_id: str, payload: dict[str, Any], *, template_id: str = "") -> dict[str, Any]:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("模板名称不能为空")
    material_version_id = str(payload.get("material_version_id", "")).strip()
    if not material_version_id:
        raise ValueError("请选择教材版本")
    resolved_material_version_id = _resolve_material_version_id(tenant_id, material_version_id)
    if not resolved_material_version_id:
        raise ValueError("教材版本不存在")
    try:
        question_count = int(payload.get("question_count", 1) or 1)
    except (TypeError, ValueError):
        raise ValueError("题量必须是整数")
    question_count = min(max(question_count, 1), 200)
    route_rules = _normalize_route_rules(payload.get("route_rules"))
    if not route_rules:
        raise ValueError("至少需要配置一个切片路由占比")
    mastery_ratio = _normalize_template_ratio_map(
        payload.get("mastery_ratio") or {},
        keys=GEN_TEMPLATE_MASTERIES,
    )
    existing = _load_gen_templates(tenant_id)
    normalized_name = name.casefold()
    for item in existing:
        current_id = str(item.get("template_id", "")).strip()
        if template_id and current_id == template_id:
            continue
        if str(item.get("name", "")).strip().casefold() == normalized_name:
            raise ValueError("模板名称已存在")
    return {
        "template_id": template_id,
        "name": name,
        "description": str(payload.get("description", "")).strip(),
        "material_version_id": resolved_material_version_id,
        "question_count": question_count,
        "mastery_ratio": mastery_ratio,
        "route_rules": route_rules,
    }


def _get_gen_template(tenant_id: str, template_id: str) -> dict[str, Any] | None:
    tid = str(template_id or "").strip()
    if not tid:
        return None
    for item in _load_gen_templates(tenant_id):
        if str(item.get("template_id", "")).strip() == tid:
            return _sanitize_gen_template(item)
    return None


def _build_template_reachability_report(
    *,
    question_count: int,
    template: dict[str, Any],
    candidate_slices: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build preflight reachability report for template route/mastery buckets."""
    route_rules = _normalize_route_rules(template.get("route_rules"))
    mastery_ratio = _normalize_template_ratio_map(
        template.get("mastery_ratio") or {},
        keys=GEN_TEMPLATE_MASTERIES,
    )
    route_counts = _largest_remainder_counts(
        question_count,
        [float(rule.get("ratio", 0) or 0) for rule in route_rules],
    )
    mastery_counts = _largest_remainder_counts(
        question_count,
        [float(mastery_ratio.get(key, 0) or 0) for key in GEN_TEMPLATE_MASTERIES],
    )
    mastery_weights = [float(mastery_ratio.get(key, 0) or 0) for key in GEN_TEMPLATE_MASTERIES]
    route_mastery_stats: list[dict[str, Any]] = []
    route_stats: list[dict[str, Any]] = []
    mastery_available_slice_count = {m: 0 for m in GEN_TEMPLATE_MASTERIES}
    mastery_available_route_count = {m: 0 for m in GEN_TEMPLATE_MASTERIES}
    for rule, route_required in zip(route_rules, route_counts):
        path_prefix = str(rule.get("path_prefix", "")).strip()
        route_candidates = [
            item for item in (candidate_slices or [])
            if str(item.get("path", "")).startswith(path_prefix)
        ]
        route_available = len(route_candidates)
        route_stats.append(
            {
                "path_prefix": path_prefix,
                "required_count": int(route_required or 0),
                "available_slice_count": route_available,
                "estimated_gap": int(route_required or 0) if int(route_required or 0) > 0 and route_available <= 0 else 0,
            }
        )
        route_mastery_required = _largest_remainder_counts(int(route_required or 0), mastery_weights)
        for mastery, required_count in zip(GEN_TEMPLATE_MASTERIES, route_mastery_required):
            available_count = sum(
                1 for item in route_candidates
                if str(item.get("mastery", "")).strip() == mastery
            )
            if available_count > 0:
                mastery_available_route_count[mastery] += 1
            mastery_available_slice_count[mastery] += available_count
            route_mastery_stats.append(
                {
                    "path_prefix": path_prefix,
                    "mastery": mastery,
                    "required_count": int(required_count or 0),
                    "available_slice_count": int(available_count),
                    "estimated_gap": int(required_count or 0) if int(required_count or 0) > 0 and int(available_count) <= 0 else 0,
                }
            )
    mastery_stats = []
    for mastery, required_count in zip(GEN_TEMPLATE_MASTERIES, mastery_counts):
        available_slice_count = int(mastery_available_slice_count.get(mastery, 0) or 0)
        available_route_count = int(mastery_available_route_count.get(mastery, 0) or 0)
        mastery_stats.append(
            {
                "mastery": mastery,
                "required_count": int(required_count or 0),
                "available_slice_count": available_slice_count,
                "available_route_count": available_route_count,
                "estimated_gap": int(required_count or 0) if int(required_count or 0) > 0 and available_slice_count <= 0 else 0,
            }
        )
    route_gaps = [item for item in route_stats if int(item.get("estimated_gap", 0) or 0) > 0]
    route_mastery_gaps = [item for item in route_mastery_stats if int(item.get("estimated_gap", 0) or 0) > 0]
    mastery_gaps = [item for item in mastery_stats if int(item.get("estimated_gap", 0) or 0) > 0]
    return {
        "ok": not (route_gaps or route_mastery_gaps or mastery_gaps),
        "route_stats": route_stats,
        "route_mastery_stats": route_mastery_stats,
        "mastery_stats": mastery_stats,
    }


def _format_template_reachability_error(base_error: str, report: dict[str, Any] | None) -> str:
    """Format human-readable reachability errors with bucket-level gaps."""
    if not isinstance(report, dict):
        return str(base_error or "模板可达成性检查失败")
    lines: list[str] = []
    route_gaps = [
        item for item in (report.get("route_stats") or [])
        if isinstance(item, dict) and int(item.get("estimated_gap", 0) or 0) > 0
    ]
    mastery_gaps = [
        item for item in (report.get("mastery_stats") or [])
        if isinstance(item, dict) and int(item.get("estimated_gap", 0) or 0) > 0
    ]
    bucket_gaps = [
        item for item in (report.get("route_mastery_stats") or [])
        if isinstance(item, dict) and int(item.get("estimated_gap", 0) or 0) > 0
    ]
    for item in route_gaps[:6]:
        lines.append(
            "路由桶[{path}] 需求{required} / 可用{available} / 预计缺口{gap}".format(
                path=str(item.get("path_prefix", "")),
                required=int(item.get("required_count", 0) or 0),
                available=int(item.get("available_slice_count", 0) or 0),
                gap=int(item.get("estimated_gap", 0) or 0),
            )
        )
    for item in mastery_gaps[:6]:
        lines.append(
            "掌握桶[{mastery}] 需求{required} / 可用{available} / 预计缺口{gap}".format(
                mastery=str(item.get("mastery", "")),
                required=int(item.get("required_count", 0) or 0),
                available=int(item.get("available_slice_count", 0) or 0),
                gap=int(item.get("estimated_gap", 0) or 0),
            )
        )
    for item in bucket_gaps[:12]:
        lines.append(
            "组合桶[{path}|{mastery}] 需求{required} / 可用{available} / 预计缺口{gap}".format(
                path=str(item.get("path_prefix", "")),
                mastery=str(item.get("mastery", "")),
                required=int(item.get("required_count", 0) or 0),
                available=int(item.get("available_slice_count", 0) or 0),
                gap=int(item.get("estimated_gap", 0) or 0),
            )
        )
    if not lines:
        return str(base_error or "模板可达成性检查失败")
    prefix = str(base_error or "").strip()
    if prefix:
        return f"{prefix}；" + "；".join(lines)
    return "；".join(lines)


def _build_generation_template_plan(
    *,
    question_count: int,
    template: dict[str, Any],
    candidate_slices: list[dict[str, Any]],
) -> dict[str, Any]:
    route_rules = _normalize_route_rules(template.get("route_rules"))
    mastery_ratio = _normalize_template_ratio_map(
        template.get("mastery_ratio") or {},
        keys=GEN_TEMPLATE_MASTERIES,
    )
    route_counts = _largest_remainder_counts(
        question_count,
        [float(rule.get("ratio", 0) or 0) for rule in route_rules],
    )
    mastery_counts_global = _largest_remainder_counts(
        question_count,
        [float(mastery_ratio.get(key, 0) or 0) for key in GEN_TEMPLATE_MASTERIES],
    )
    slice_buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    route_summaries: list[dict[str, Any]] = []
    missing_reasons: list[str] = []
    plan_units: list[dict[str, Any]] = []
    plan_unit_available_by_mastery: dict[str, dict[str, int]] = {}
    template_slice_usage_counts: dict[int, int] = {}
    for rule, route_count in zip(route_rules, route_counts):
        path_prefix = str(rule.get("path_prefix", "")).strip()
        route_candidates = [
            item for item in candidate_slices
            if str(item.get("path", "")).startswith(path_prefix)
        ]
        if route_count > 0 and not route_candidates:
            missing_reasons.append(f"{path_prefix} 没有可用 approved 切片")
            continue
        mastery_summary: list[dict[str, Any]] = [
            {
                "mastery": mastery,
                "count": 0,
                "available_slice_count": sum(
                    1 for item in route_candidates if str(item.get("mastery", "")).strip() == mastery
                ),
            }
            for mastery in GEN_TEMPLATE_MASTERIES
        ]
        mastery_available: dict[str, int] = {}
        for mastery in GEN_TEMPLATE_MASTERIES:
            bucket = [item for item in route_candidates if str(item.get("mastery", "")).strip() == mastery]
            slice_buckets[(path_prefix, mastery)] = bucket
            mastery_available[mastery] = len(bucket)
        plan_unit_available_by_mastery[path_prefix] = mastery_available
        plan_units.append(
            {
                "unit_prefix": path_prefix,
                "count": int(route_count or 0),
            }
        )
        route_summaries.append(
            {
                "path_prefix": path_prefix,
                "ratio": float(rule.get("ratio", 0) or 0),
                "count": route_count,
                "available_slice_count": len(route_candidates),
                "mastery_breakdown": mastery_summary,
            }
        )

    total_available_by_mastery = {
        mastery: sum(plan_unit_available_by_mastery.get(str(unit.get("unit_prefix", "")), {}).get(mastery, 0) for unit in plan_units)
        for mastery in GEN_TEMPLATE_MASTERIES
    }
    for mastery, mastery_count in zip(GEN_TEMPLATE_MASTERIES, mastery_counts_global):
        if mastery_count > 0 and total_available_by_mastery.get(mastery, 0) <= 0:
            missing_reasons.append(f"全局缺少“{mastery}”切片，无法满足模板占比")
    if missing_reasons:
        raise ValueError("；".join(missing_reasons))

    # Global-first allocation:
    # 1) route_counts strictly follow route ratio
    # 2) mastery_counts_global strictly follow template mastery_ratio
    # 3) assign matrix cells so both row sums and column sums are satisfied
    unit_index_by_prefix = {
        str(unit.get("unit_prefix", "")): idx
        for idx, unit in enumerate(plan_units)
    }
    allocation: dict[tuple[str, str], int] = {
        (str(unit.get("unit_prefix", "")), mastery): 0
        for unit in plan_units
        for mastery in GEN_TEMPLATE_MASTERIES
    }
    row_remaining = {
        str(unit.get("unit_prefix", "")): int(unit.get("count", 0) or 0)
        for unit in plan_units
    }
    col_remaining = {
        mastery: count for mastery, count in zip(GEN_TEMPLATE_MASTERIES, mastery_counts_global)
    }
    mastery_total = float(sum(float(mastery_ratio.get(key, 0) or 0) for key in GEN_TEMPLATE_MASTERIES) or 1.0)

    cell_scores: dict[tuple[str, str], float] = {}
    for unit in plan_units:
        path_prefix = str(unit.get("unit_prefix", ""))
        route_count = int(unit.get("count", 0) or 0)
        for mastery in GEN_TEMPLATE_MASTERIES:
            ratio_value = float(mastery_ratio.get(mastery, 0) or 0)
            ideal = route_count * ratio_value / mastery_total if mastery_total > 0 else 0.0
            cell_scores[(path_prefix, mastery)] = ideal
            base = int(math.floor(ideal))
            available = plan_unit_available_by_mastery.get(path_prefix, {}).get(mastery, 0)
            if available <= 0 or base <= 0:
                continue
            assignable = min(base, row_remaining[path_prefix], col_remaining[mastery], available)
            if assignable <= 0:
                continue
            allocation[(path_prefix, mastery)] += assignable
            row_remaining[path_prefix] -= assignable
            col_remaining[mastery] -= assignable

    def _alloc_priority(path_prefix: str, mastery: str) -> tuple[float, float, float, int]:
        score = float(cell_scores.get((path_prefix, mastery), 0.0))
        fractional = score - math.floor(score)
        available = plan_unit_available_by_mastery.get(path_prefix, {}).get(mastery, 0)
        scarcity = -1.0 / max(available, 1)
        route_idx = unit_index_by_prefix.get(path_prefix, 0)
        return (fractional, score, scarcity, -route_idx)

    greedy_dead_end_error = ""
    while any(v > 0 for v in row_remaining.values()):
        progressed = False
        for mastery in GEN_TEMPLATE_MASTERIES:
            while col_remaining.get(mastery, 0) > 0:
                candidates: list[tuple[tuple[float, float, float, int], str]] = []
                for unit in plan_units:
                    path_prefix = str(unit.get("unit_prefix", ""))
                    if row_remaining.get(path_prefix, 0) <= 0:
                        continue
                    cell_available = plan_unit_available_by_mastery.get(path_prefix, {}).get(mastery, 0)
                    if cell_available <= 0:
                        continue
                    if allocation.get((path_prefix, mastery), 0) >= cell_available:
                        continue
                    candidates.append((_alloc_priority(path_prefix, mastery), path_prefix))
                if not candidates:
                    greedy_dead_end_error = (
                        f"全局需要 {col_remaining.get(mastery, 0)} 道“{mastery}”题，但可用路由切片不足，无法满足模板占比"
                    )
                    break
                candidates.sort(reverse=True)
                chosen_path = candidates[0][1]
                allocation[(chosen_path, mastery)] += 1
                row_remaining[chosen_path] -= 1
                col_remaining[mastery] -= 1
                progressed = True
            if greedy_dead_end_error:
                break
        if greedy_dead_end_error:
            break
        if not progressed:
            break

    unresolved_rows = [path for path, remain in row_remaining.items() if remain > 0]
    unresolved_cols = [mastery for mastery, remain in col_remaining.items() if remain > 0]
    if unresolved_rows and unresolved_cols:
        remaining_units = [
            {
                "unit_prefix": str(unit.get("unit_prefix", "")),
                "count": int(unit.get("count", 0) or 0),
            }
            for unit in plan_units
            if int(unit.get("count", 0) or 0) > 0
        ]
        remaining_units.sort(
            key=lambda unit: (
                sum(
                    1
                    for mastery in GEN_TEMPLATE_MASTERIES
                    if plan_unit_available_by_mastery.get(str(unit.get("unit_prefix", "")), {}).get(mastery, 0) > 0
                ),
                int(unit.get("count", 0) or 0),
                unit_index_by_prefix.get(str(unit.get("unit_prefix", "")), 0),
            )
        )

        def _remaining_capacity(start_idx: int) -> dict[str, int]:
            capacity = {mastery: 0 for mastery in GEN_TEMPLATE_MASTERIES}
            for unit in remaining_units[start_idx:]:
                path_prefix = str(unit.get("unit_prefix", ""))
                route_count = int(unit.get("count", 0) or 0)
                for mastery in GEN_TEMPLATE_MASTERIES:
                    if plan_unit_available_by_mastery.get(path_prefix, {}).get(mastery, 0) > 0:
                        capacity[mastery] += route_count
            return capacity

        def _iter_unit_assignments(path_prefix: str, route_count: int, remaining_cols: dict[str, int]):
            max_first = min(
                route_count,
                int(remaining_cols.get(GEN_TEMPLATE_MASTERIES[0], 0) or 0),
            )
            for first_count in range(max_first, -1, -1):
                if (
                    first_count > 0
                    and plan_unit_available_by_mastery.get(path_prefix, {}).get(GEN_TEMPLATE_MASTERIES[0], 0) <= 0
                ):
                    continue
                max_second = min(
                    route_count - first_count,
                    int(remaining_cols.get(GEN_TEMPLATE_MASTERIES[1], 0) or 0),
                )
                for second_count in range(max_second, -1, -1):
                    if (
                        second_count > 0
                        and plan_unit_available_by_mastery.get(path_prefix, {}).get(GEN_TEMPLATE_MASTERIES[1], 0) <= 0
                    ):
                        continue
                    third_count = route_count - first_count - second_count
                    if third_count < 0:
                        continue
                    if third_count > int(remaining_cols.get(GEN_TEMPLATE_MASTERIES[2], 0) or 0):
                        continue
                    if (
                        third_count > 0
                        and plan_unit_available_by_mastery.get(path_prefix, {}).get(GEN_TEMPLATE_MASTERIES[2], 0) <= 0
                    ):
                        continue
                    yield {
                        GEN_TEMPLATE_MASTERIES[0]: first_count,
                        GEN_TEMPLATE_MASTERIES[1]: second_count,
                        GEN_TEMPLATE_MASTERIES[2]: third_count,
                    }

        repair_assignment: dict[str, dict[str, int]] = {}

        def _search_remaining(unit_idx: int, remaining_cols: dict[str, int]) -> bool:
            if unit_idx >= len(remaining_units):
                return all(int(remaining_cols.get(mastery, 0) or 0) == 0 for mastery in GEN_TEMPLATE_MASTERIES)
            capacity = _remaining_capacity(unit_idx)
            if any(int(remaining_cols.get(mastery, 0) or 0) > capacity.get(mastery, 0) for mastery in GEN_TEMPLATE_MASTERIES):
                return False
            unit = remaining_units[unit_idx]
            path_prefix = str(unit.get("unit_prefix", ""))
            route_count = int(unit.get("count", 0) or 0)
            target_scores = {
                mastery: float(cell_scores.get((path_prefix, mastery), 0.0))
                for mastery in GEN_TEMPLATE_MASTERIES
            }
            assignments = list(_iter_unit_assignments(path_prefix, route_count, remaining_cols))
            assignments.sort(
                key=lambda item: (
                    sum(1 for mastery in GEN_TEMPLATE_MASTERIES if int(item.get(mastery, 0) or 0) > 0),
                    -sum(abs(int(item.get(mastery, 0) or 0) - target_scores.get(mastery, 0.0)) for mastery in GEN_TEMPLATE_MASTERIES),
                )
            )
            for unit_assignment in assignments:
                next_cols = {
                    mastery: int(remaining_cols.get(mastery, 0) or 0) - int(unit_assignment.get(mastery, 0) or 0)
                    for mastery in GEN_TEMPLATE_MASTERIES
                }
                if any(v < 0 for v in next_cols.values()):
                    continue
                repair_assignment[path_prefix] = unit_assignment
                if _search_remaining(unit_idx + 1, next_cols):
                    return True
                repair_assignment.pop(path_prefix, None)
            return False

        if _search_remaining(
            0,
            {mastery: int(count or 0) for mastery, count in zip(GEN_TEMPLATE_MASTERIES, mastery_counts_global)},
        ):
            allocation = {
                (str(unit.get("unit_prefix", "")), mastery): 0
                for unit in plan_units
                for mastery in GEN_TEMPLATE_MASTERIES
            }
            row_remaining = {
                str(unit.get("unit_prefix", "")): int(unit.get("count", 0) or 0)
                for unit in plan_units
            }
            col_remaining = {
                mastery: int(count or 0) for mastery, count in zip(GEN_TEMPLATE_MASTERIES, mastery_counts_global)
            }
            for path_prefix, unit_assignment in repair_assignment.items():
                for mastery in GEN_TEMPLATE_MASTERIES:
                    assigned = int(unit_assignment.get(mastery, 0) or 0)
                    if assigned <= 0:
                        continue
                    allocation[(path_prefix, mastery)] += assigned
                    row_remaining[path_prefix] -= assigned
                    col_remaining[mastery] -= assigned

    unresolved_rows = [path for path, remain in row_remaining.items() if remain > 0]
    unresolved_cols = [mastery for mastery, remain in col_remaining.items() if remain > 0]
    if unresolved_rows or unresolved_cols:
        details: list[str] = []
        if unresolved_rows:
            details.append("路由剩余未分配: " + ", ".join(f"{path}={row_remaining[path]}" for path in unresolved_rows))
        if unresolved_cols:
            details.append("掌握程度剩余未分配: " + ", ".join(f"{mastery}={col_remaining[mastery]}" for mastery in unresolved_cols))
        raise ValueError("；".join(details) or greedy_dead_end_error or "模板切片分配失败")

    for route in route_summaries:
        path_prefix = str(route.get("path_prefix", ""))
        for mastery_item in route.get("mastery_breakdown", []):
            mastery = str(mastery_item.get("mastery", ""))
            mastery_item["count"] = int(allocation.get((path_prefix, mastery), 0) or 0)

    planned_slots: list[dict[str, Any]] = []
    for unit in plan_units:
        path_prefix = str(unit.get("unit_prefix", ""))
        for mastery in GEN_TEMPLATE_MASTERIES:
            count = int(allocation.get((path_prefix, mastery), 0) or 0)
            if count <= 0:
                continue
            bucket = list(slice_buckets.get((path_prefix, mastery), []))
            for idx in range(count):
                bucket_ids = [int(item.get("slice_id")) for item in bucket if str(item.get("slice_id", "")).isdigit()]
                chosen_sid = _pick_preferred_slice_id(
                    bucket_ids,
                    usage_counts=template_slice_usage_counts,
                    excluded_slice_ids=set(),
                    max_questions_per_slice=0,
                    prefer_unused=True,
                )
                if chosen_sid is None:
                    chosen = bucket[idx % len(bucket)]
                else:
                    chosen = next(
                        (
                            item for item in bucket
                            if int(item.get("slice_id", 0) or 0) == int(chosen_sid)
                        ),
                        bucket[idx % len(bucket)],
                    )
                chosen_sid_int = int(chosen.get("slice_id", 0) or 0)
                if chosen_sid_int > 0:
                    template_slice_usage_counts[chosen_sid_int] = int(template_slice_usage_counts.get(chosen_sid_int, 0) or 0) + 1
                planned_slots.append(
                    {
                        "slice_id": int(chosen["slice_id"]),
                        "route_prefix": path_prefix,
                        "mastery": mastery,
                    }
                )
    def _slot_candidate_count(slot: dict[str, Any]) -> int:
        if not isinstance(slot, dict):
            return 0
        route_prefix = str(slot.get("route_prefix", "") or "").strip()
        mastery = str(slot.get("mastery", "") or "").strip()
        return len(slice_buckets.get((route_prefix, mastery), []) or [])
    # 先跑“好补位”的位次（候选桶更大），把最难位次留到后面，
    # 以提高在固定尝试预算内的整体完成率。
    planned_slots.sort(
        key=lambda slot: (
            -_slot_candidate_count(slot),
            str((slot or {}).get("route_prefix", "") or ""),
            str((slot or {}).get("mastery", "") or ""),
            int((slot or {}).get("slice_id", 0) or 0),
        )
    )
    planned_slice_ids = [int(slot.get("slice_id")) for slot in planned_slots if str(slot.get("slice_id", "")).isdigit()]
    return {
        "planned_slice_ids": planned_slice_ids,
        "planned_slots": planned_slots,
        "route_breakdown": route_summaries,
        "mastery_ratio": mastery_ratio,
        "mastery_counts": {
            mastery: count for mastery, count in zip(GEN_TEMPLATE_MASTERIES, mastery_counts_global)
        },
    }


def _normalize_calc_label(raw_value: Any) -> str:
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


def _normalize_judge_question_type(raw_value: Any) -> str:
    text = str(raw_value or "").strip().lower()
    if not text:
        return ""
    if text in {"single_choice", "single", "单选题", "单选"}:
        return "single_choice"
    if text in {"multiple_choice", "multi_choice", "multiple", "多选题", "多选"}:
        return "multiple_choice"
    if text in {"true_false", "judge", "判断题", "判断"}:
        return "true_false"
    return ""


def _infer_judge_question_type(stem: str, options: list[str], correct_answer: str) -> str:
    ans = str(correct_answer or "").strip().upper()
    normalized_options = [str(x or "").strip() for x in (options or [])]

    if ans in {"正确", "错误"}:
        return "true_false"

    if ans in {"A", "B"} and len(normalized_options) >= 2:
        o1 = normalized_options[0]
        o2 = normalized_options[1]
        if ("正确" in o1 and "错误" in o2) or ("错误" in o1 and "正确" in o2):
            return "true_false"

    compact = ans.replace("，", ",").replace("、", ",").replace(" ", "")
    if "," in compact:
        toks = [x for x in compact.split(",") if x]
        if len(toks) >= 2 and all(t in {"A", "B", "C", "D"} for t in toks):
            return "multiple_choice"
    if re.fullmatch(r"[A-D]{2,}", compact):
        return "multiple_choice"

    stem_text = str(stem or "")
    if re.search(r"(以下|下列).{0,8}(正确|错误).{0,6}(有|包括)", stem_text):
        return "multiple_choice"

    return "single_choice"


def _resolve_judge_question_type(
    *,
    preferred: Any = "",
    stem: str = "",
    options: list[str] | None = None,
    correct_answer: str = "",
    config_question_type: Any = "",
) -> str:
    qt = _normalize_judge_question_type(preferred)
    if qt:
        return qt

    config_qt = _normalize_judge_question_type(config_question_type)
    if config_qt:
        return config_qt

    return _infer_judge_question_type(stem, options or [], correct_answer)


def _judge_question_type_to_cn(qt: str) -> str:
    if qt == "multiple_choice":
        return "多选题"
    if qt == "true_false":
        return "判断题"
    return "单选题"


def _resolve_storage_question_type_cn(
    *,
    final_json: dict[str, Any] | None,
    trace_question_type: Any = "",
    config_question_type: Any = "",
) -> str:
    payload = final_json if isinstance(final_json, dict) else {}
    options = [str(payload.get(f"选项{i}", "") or "").strip() for i in range(1, 5)]
    options = [x for x in options if x]
    qt = _resolve_judge_question_type(
        preferred=trace_question_type or payload.get("题目类型"),
        stem=str(payload.get("题干", "") or ""),
        options=options,
        correct_answer=str(payload.get("正确答案", "") or ""),
        config_question_type=config_question_type,
    )
    return _judge_question_type_to_cn(qt)


def _resolve_calc_question_type(question: dict[str, Any]) -> str:
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
        label = _normalize_calc_label(question.get(key))
        if label:
            return label

    text_parts = [
        str(question.get("题干", "") or ""),
        str(question.get("解析", "") or ""),
    ]
    for idx in range(1, 9):
        text_parts.append(str(question.get(f"选项{idx}", "") or ""))
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


def _is_calculation_slice(slice_item: dict[str, Any]) -> bool:
    """True if slice may lead to calculation questions (formulas, or content/path hints)."""
    if not isinstance(slice_item, dict):
        return False
    meta = slice_item.get("metadata") or {}
    if meta.get("包含计算公式"):
        return True
    struct = slice_item.get("结构化内容") or {}
    if (struct.get("formulas") or []) and len(struct.get("formulas") or []) > 0:
        return True
    # Tables with numeric content often support calculation questions
    tables = struct.get("tables") or []
    if tables and any(re.search(r"\d", str(t)) for t in tables):
        return True
    path = str(slice_item.get("完整路径", "") or "")
    content = _extract_slice_text(slice_item)
    combined = f"{path}\n{content}"
    # Broader hints: anything that could lead to calculation-style questions
    calc_hints = (
        "计算", "公式", "税率", "税费", "贷款", "月供", "首付", "利息", "金额", "比例", "百分比",
        "契税", "增值税", "个税", "数额", "万元", "总价", "单价", "面积", "折扣", "合计", "得房率",
        "税", "率", "%", "元", "数字",
    )
    return sum(1 for h in calc_hints if h in combined) >= 1


def _error(code: str, message: str, status: int = 400):
    return _json_response({"error": {"code": code, "message": message}}, status=status)


def _get_sso_sid_from_cookie() -> str:
    if not SSO_MANAGER.enabled:
        return ""
    return str(request.cookies.get(SSO_MANAGER.cookie_name, "")).strip()


def _get_sso_session() -> dict[str, Any] | None:
    sid = _get_sso_sid_from_cookie()
    if not sid:
        return None
    return SSO_MANAGER.get_session(sid)


def _resolve_principal_from_sso_session() -> Principal:
    session = _get_sso_session()
    if not session:
        raise AccessDenied("SSO_SESSION_REQUIRED")
    system_user = str(session.get("system_user", "")).strip()
    if not system_user:
        raise AccessDenied("SSO_SYSTEM_USER_MISSING")
    principal = resolve_legacy_principal(system_user)
    if str(session.get("tenant_id", "")).strip() and str(session.get("tenant_id", "")).strip() not in principal.tenants:
        raise AccessDenied("TENANT_FORBIDDEN")
    g.sso_session = session
    return principal


def _sso_public_session(session: dict[str, Any] | None) -> dict[str, Any]:
    if not session:
        return {"logged_in": False}
    return {
        "logged_in": True,
        "ucid": str(session.get("ucid", "")).strip(),
        "tenant_id": str(session.get("tenant_id", "")).strip(),
        "system_user": str(session.get("system_user", "")).strip(),
        "accounts": [
            {
                "system_user": str(item.get("system_user", "")).strip(),
                "is_default": bool(item.get("is_default", False)),
            }
            for item in (session.get("accounts") or [])
            if isinstance(item, dict) and str(item.get("system_user", "")).strip()
        ],
        "expires_at": float(session.get("expires_at", 0) or 0),
    }


def _get_principal() -> Principal:
    principal = getattr(g, "principal", None)
    if principal is None:
        auth_header = (request.headers.get("Authorization") or "")
        system_user_header = (request.headers.get("X-System-User") or "")
        # SSO 开启时禁止 legacy X-System-User 绕过
        if SSO_MANAGER.enabled and system_user_header.strip() and not auth_header.strip():
            raise AccessDenied("SSO_LEGACY_BYPASS_DENIED")
        try:
            principal = resolve_principal(
                authorization_header=auth_header,
                system_user_header=system_user_header,
            )
        except AccessDenied:
            if SSO_MANAGER.enabled and not auth_header.strip() and not system_user_header.strip():
                principal = _resolve_principal_from_sso_session()
            else:
                raise
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
    if s == "approved":
        return "approved"
    return "pending"


def _has_dual_review_completed_slice(tenant_id: str, material_version_id: str) -> tuple[bool, int]:
    """
    Return whether material has at least one slice that completed both reviews:
    - slice review is approved
    - all mapping review entries for that slice are approved
    """
    if not tenant_id or not material_version_id:
        return False, 0
    slice_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    mapping_file = _resolve_mapping_path_for_material(tenant_id, material_version_id)
    if not slice_file or not mapping_file:
        return False, 0

    kb_items = _load_kb_items_from_file(slice_file)
    if not kb_items:
        return False, 0
    try:
        mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, 0
    if not isinstance(mapping, dict) or not mapping:
        return False, 0

    slice_reviews = _load_slice_review_for_material(tenant_id, material_version_id)
    mapping_reviews = _load_mapping_review_for_material(tenant_id, material_version_id)
    approved_slice_ids = {
        i for i in range(len(kb_items))
        if str((slice_reviews.get(str(i), {}) or {}).get("review_status", "pending")) == "approved"
    }
    if not approved_slice_ids:
        return False, 0

    completed_slice_ids: set[int] = set()
    for slice_id, payload in mapping.items():
        if not str(slice_id).isdigit():
            continue
        sid = int(slice_id)
        if sid not in approved_slice_ids:
            continue
        matched_questions = payload.get("matched_questions", []) if isinstance(payload, dict) else []
        if not isinstance(matched_questions, list) or not matched_questions:
            continue

        total_entries = 0
        approved_entries = 0
        for entry in matched_questions:
            if not isinstance(entry, dict):
                continue
            question_index = entry.get("question_index")
            if question_index is None:
                continue
            total_entries += 1
            map_key = f"{slice_id}:{question_index}"
            review_status = _normalize_mapping_status((mapping_reviews.get(map_key, {}) or {}).get("confirm_status", "pending"))
            if review_status == "approved":
                approved_entries += 1
        if total_entries > 0 and approved_entries == total_entries:
            completed_slice_ids.add(sid)

    return bool(completed_slice_ids), len(completed_slice_ids)


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
        # context_before
        txt = _as_text(struct.get("context_before"))
        if txt:
            parts.append(txt)
        # 图片解析（按切片预览方式完整呈现）
        seen_image_analysis: set[str] = set()
        for img in (struct.get("images") or []):
            if isinstance(img, dict):
                analysis = _as_text(img.get("analysis"))
                norm = analysis.strip()
                if norm and norm not in seen_image_analysis:
                    seen_image_analysis.add(norm)
                    parts.append(analysis)
        for key in ("tables", "context_after", "examples", "formulas", "rules", "key_params"):
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


def _build_complete_slice_content_for_mapping(
    slice_item: dict[str, Any] | None,
    slice_id: int | str,
    kb_items: list[dict[str, Any]],
    path: str,
) -> str:
    """
    构建映射审核用的完整切片内容：与切片核对页一致，仅展示当前切片内容。
    """
    return _extract_slice_text(slice_item or {})


def _extract_slice_images(item: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        return []
    raw_images = (item.get("结构化内容", {}) or {}).get("images", []) or []
    image_items: list[dict[str, Any]] = []
    if not isinstance(raw_images, list):
        return image_items
    seen_keys: set[tuple[str, str, str]] = set()
    for img in raw_images:
        if not isinstance(img, dict):
            continue
        image_id = str(img.get("image_id", ""))
        image_path = str(img.get("image_path", ""))
        analysis = str(img.get("analysis", ""))
        dedup_key = (image_id, image_path, analysis.strip())
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        image_items.append(
            {
                "image_id": image_id,
                "image_path": image_path,
                "analysis": analysis,
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
    """Return full text without truncation for debugging/trace visibility."""
    return str(value or "").replace("\n", " ").strip()


def _extract_question_parts(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize question payload from draft/final formats."""
    if not isinstance(payload, dict):
        return {"stem": "", "options": [], "answer": "", "explanation": ""}
    stem = str(
        payload.get("题干", "")
        or payload.get("question", "")
        or payload.get("stem", "")
        or payload.get("题目", "")
        or payload.get("question_stem", "")
    ).strip()
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


def _extract_mother_questions_from_examples(examples: Any, limit: int = 3) -> list[str]:
    """Extract readable mother-question stems from state examples for UI display/storage."""
    if not isinstance(examples, list):
        return []
    rows: list[str] = []
    seen: set[str] = set()
    for ex in examples:
        if len(rows) >= max(1, int(limit or 1)):
            break
        stem = ""
        if isinstance(ex, dict):
            for key in ("题干", "question", "母题", "母题题干", "关联母题"):
                v = str(ex.get(key, "")).strip()
                if v:
                    stem = v
                    break
        else:
            stem = str(ex or "").strip()
        if not stem or stem in seen:
            continue
        seen.add(stem)
        rows.append(stem)
    return rows


def _extract_mother_question_full_from_examples(examples: Any, limit: int = 3) -> list[dict[str, Any]]:
    """Extract full mother-question payloads from state examples for export/audit."""
    if not isinstance(examples, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ex in examples:
        if len(rows) >= max(1, int(limit or 1)):
            break
        if not isinstance(ex, dict):
            stem = str(ex or "").strip()
            if not stem or stem in seen:
                continue
            seen.add(stem)
            rows.append({"题干": stem, "选项": {}, "正确答案": "", "解析": ""})
            continue
        stem = str(ex.get("题干", "") or ex.get("question", "")).strip()
        if not stem:
            continue
        if stem in seen:
            continue
        seen.add(stem)
        options_payload: dict[str, str] = {}
        raw_options = ex.get("选项")
        if isinstance(raw_options, dict):
            for key in ("A", "B", "C", "D", "E", "F", "G", "H"):
                value = str(raw_options.get(key, "") or "").strip()
                if value:
                    options_payload[key] = value
        for i, key in enumerate(("A", "B", "C", "D", "E", "F", "G", "H"), start=1):
            value = str(ex.get(f"选项{i}", "") or "").strip()
            if value and key not in options_payload:
                options_payload[key] = value
        raw_list_options = ex.get("options")
        if isinstance(raw_list_options, list):
            for idx, value in enumerate(raw_list_options):
                if idx >= 8:
                    break
                text = str(value or "").strip()
                if not text:
                    continue
                key = chr(ord("A") + idx)
                if key not in options_payload:
                    options_payload[key] = text
        rows.append({
            "题干": stem,
            "选项": options_payload,
            "正确答案": str(ex.get("正确答案", "") or ex.get("answer", "")).strip(),
            "解析": str(ex.get("解析", "") or ex.get("explanation", "")).strip(),
        })
    return rows


def _attach_mother_questions_to_question_payload(q_json: dict[str, Any], mother_questions: list[str]) -> None:
    """Attach normalized mother-question fields so frontend can render directly."""
    if not isinstance(q_json, dict):
        return
    stems = [str(x).strip() for x in (mother_questions or []) if str(x).strip()]
    if not stems:
        return
    lines = [f"{i + 1}. {stem}" for i, stem in enumerate(stems)]
    text = "\n".join(lines)
    q_json["关联母题"] = text
    q_json["母题题干"] = text
    q_json["mother_questions"] = stems


def _attach_mother_question_full_to_question_payload(q_json: dict[str, Any], mother_full_rows: list[dict[str, Any]]) -> None:
    """Attach full mother-question content for export."""
    if not isinstance(q_json, dict):
        return
    rows = [x for x in (mother_full_rows or []) if isinstance(x, dict)]
    if not rows:
        return
    q_json["mother_questions_full"] = rows
    blocks: list[str] = []
    for i, row in enumerate(rows, start=1):
        stem = str(row.get("题干", "")).strip()
        options = row.get("选项") if isinstance(row.get("选项"), dict) else {}
        answer = str(row.get("正确答案", "")).strip()
        explanation = str(row.get("解析", "")).strip()
        option_lines = []
        for key in ("A", "B", "C", "D", "E", "F", "G", "H"):
            value = str(options.get(key, "") or "").strip()
            if value:
                option_lines.append(f"{key}. {value}")
        option_text = "\n".join(option_lines) if option_lines else "（无）"
        blocks.append(
            f"母题{i}\n题干：{stem or '（无）'}\n选项：\n{option_text}\n正确答案：{answer or '（无）'}\n解析：{explanation or '（无）'}"
        )
    q_json["参考母题全文"] = "\n\n".join(blocks)


_SLICE_TEXT_INDEX_CACHE: dict[str, dict[str, str]] = {}


def _get_slice_text_index(tenant_id: str, material_version_id: str) -> dict[str, str]:
    mid = str(material_version_id or "").strip()
    if not mid:
        return {}
    cache_key = f"{tenant_id}:{mid}"
    cached = _SLICE_TEXT_INDEX_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return cached
    kb_file = _resolve_slice_file_for_material(tenant_id, mid)
    kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
    out: dict[str, str] = {}
    for item in kb_items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("完整路径", "") or "").strip()
        if not path or path in out:
            continue
        out[path] = _extract_slice_text(item)
    _SLICE_TEXT_INDEX_CACHE[cache_key] = out
    return out


def _normalize_related_slice_paths(raw_value: Any, *, limit: int = 20) -> list[str]:
    """Normalize related slice paths from list/json/newline text."""
    rows: list[str] = []
    if isinstance(raw_value, list):
        rows = [str(x).strip() for x in raw_value if str(x).strip()]
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if text:
            parsed: Any = None
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                rows = [str(x).strip() for x in parsed if str(x).strip()]
            else:
                parts = re.split(r"[\n;,]+", text)
                rows = [str(x).strip() for x in parts if str(x).strip()]
    elif raw_value is not None:
        text = str(raw_value).strip()
        if text:
            rows = [text]
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row in seen:
            continue
        seen.add(row)
        out.append(row)
        if len(out) >= max(1, int(limit or 1)):
            break
    return out


def _normalize_slice_text_list(raw_value: Any, *, limit: int = 20) -> list[str]:
    """Normalize related/reference slices (path or text) into deduped list."""
    rows: list[str] = []
    if isinstance(raw_value, list):
        rows = [str(x).strip() for x in raw_value if str(x).strip()]
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if text:
            parsed: Any = None
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                rows = [str(x).strip() for x in parsed if str(x).strip()]
            else:
                # Keep line-based split first; avoid splitting by comma to reduce accidental content truncation.
                parts = [p.strip() for p in text.split("\n") if p.strip()]
                rows = parts if len(parts) > 1 else [text]
    elif raw_value is not None:
        text = str(raw_value).strip()
        if text:
            rows = [text]
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row in seen:
            continue
        seen.add(row)
        out.append(row)
        if len(out) >= max(1, int(limit or 1)):
            break
    return out


def _extract_related_reference_slices(
    question_trace: dict[str, Any],
    final_json: dict[str, Any],
    fallback_input: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    ji = fallback_input if isinstance(fallback_input, dict) else {}

    related: list[str] = []
    for raw in (
        question_trace.get("related_slices"),
        question_trace.get("related_slice_texts"),
        question_trace.get("related_slice_contents"),
        question_trace.get("related_slice_paths"),
        question_trace.get("critic_basis_paths"),
        final_json.get("关联切片"),
        final_json.get("关联切片原文"),
        final_json.get("关联切片路径"),
        final_json.get("关联切片路径文本"),
        ji.get("related_slices"),
    ):
        related.extend(_normalize_slice_text_list(raw, limit=50))

    reference: list[str] = []
    for raw in (
        question_trace.get("reference_slices"),
        question_trace.get("reference_slice_texts"),
        question_trace.get("reference_slice_contents"),
        question_trace.get("reference_slice_paths"),
        final_json.get("参考切片"),
        final_json.get("参考切片原文"),
        final_json.get("reference_slices"),
        ji.get("reference_slices"),
    ):
        reference.extend(_normalize_slice_text_list(raw, limit=50))

    # Dedupe & cap
    related = _normalize_slice_text_list(related, limit=20)
    reference = _normalize_slice_text_list(reference, limit=20)
    return related, reference


def _attach_related_slices_to_question_payload(q_json: dict[str, Any], related_paths: list[str]) -> None:
    """Attach related slice paths to final question payload for UI/export."""
    if not isinstance(q_json, dict):
        return
    paths = _normalize_related_slice_paths(related_paths, limit=30)
    q_json["关联切片路径"] = paths
    q_json["关联切片数量"] = len(paths)
    q_json["关联切片路径文本"] = "\n".join(paths)


def _attach_preview_context_to_question_payload(
    q_json: dict[str, Any],
    *,
    tenant_id: str,
    material_version_id: str,
    question_trace: dict[str, Any],
    source_path: str = "",
    source_slice_id: Any = None,
    mother_questions: list[str] | None = None,
    mother_full_questions: list[dict[str, Any]] | None = None,
) -> None:
    """Attach full preview context so task detail can render failed/unsaved questions with slice and mother data."""
    if not isinstance(q_json, dict):
        return
    trace = question_trace if isinstance(question_trace, dict) else {}
    resolved_source_path = str(source_path or trace.get("slice_path") or q_json.get("来源路径") or "").strip()
    resolved_source_slice_id = (
        source_slice_id
        if source_slice_id not in (None, "")
        else trace.get("slice_id", q_json.get("来源切片ID"))
    )
    resolved_material_version_id = str(material_version_id or q_json.get("教材版本ID") or "").strip()
    source_slice_text = str(
        trace.get("slice_content")
        or q_json.get("切片原文")
        or q_json.get("来源切片原文")
        or ""
    ).strip()

    if resolved_source_path:
        q_json["来源路径"] = resolved_source_path
    if resolved_source_slice_id not in (None, ""):
        q_json["来源切片ID"] = resolved_source_slice_id
    if resolved_material_version_id:
        q_json["教材版本ID"] = resolved_material_version_id

    related_paths, _reference_paths = _extract_related_reference_slices(trace, q_json, None)
    _attach_related_slices_to_question_payload(q_json, related_paths)
    _attach_mother_questions_to_question_payload(q_json, mother_questions or [])
    _attach_mother_question_full_to_question_payload(q_json, mother_full_questions or [])

    if not source_slice_text and resolved_source_path and resolved_material_version_id:
        source_slice_text = str(_get_slice_text_index(tenant_id, resolved_material_version_id).get(resolved_source_path, "") or "").strip()
    if source_slice_text:
        q_json["切片原文"] = source_slice_text
        q_json["来源切片原文"] = source_slice_text

    all_slice_paths: list[str] = []
    for raw_path in [resolved_source_path] + related_paths:
        path = str(raw_path or "").strip()
        if path and path not in all_slice_paths:
            all_slice_paths.append(path)
    q_json["全部切片路径"] = "\n".join(all_slice_paths)

    slice_text_index = _get_slice_text_index(tenant_id, resolved_material_version_id) if resolved_material_version_id else {}
    all_slice_blocks: list[str] = []
    for path in all_slice_paths:
        block_text = source_slice_text if (path == resolved_source_path and source_slice_text) else str(slice_text_index.get(path, "") or "").strip()
        all_slice_blocks.append(f"【{path}】\n{block_text if block_text else '（未找到该切片原文）'}")
    if all_slice_blocks:
        q_json["全部切片原文"] = "\n\n".join(all_slice_blocks)


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
        # Explicit writer node step so UI shows "writer" in the process (e.g. 作家润色完成)
        if isinstance(state_update.get("final_json"), dict):
            append_step(
                "作家润色完成",
                node=node_name,
                level="success",
                detail="已生成定稿，进入 critic 审核",
            )
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


def _ensure_critic_step_in_trace(question_trace: dict[str, Any]) -> None:
    """If trace has critic_result but no critic step (e.g. rule-based reject), add one so UI shows the link."""
    critic_result = question_trace.get("critic_result") if isinstance(question_trace.get("critic_result"), dict) else None
    if not critic_result or "passed" not in critic_result:
        return
    steps = question_trace.get("steps") or []
    if any(s.get("node") == "critic" and (s.get("message") in ("审核通过", "审核驳回")) for s in steps):
        return
    passed = bool(critic_result.get("passed"))
    reason = str(critic_result.get("reason", "")).strip()
    if not reason and not passed:
        reason = str(question_trace.get("critic_details", "")).strip()
    reason = reason or "审核未通过（原因未返回）"
    seq = max((s.get("seq") or 0 for s in steps), default=0) + 1
    elapsed_ms = int(question_trace.get("elapsed_ms") or 0)
    now = datetime.now(timezone.utc)
    steps.append({
        "seq": seq,
        "node": "critic",
        "level": "success" if passed else "warning",
        "message": "审核通过" if passed else "审核驳回",
        "detail": reason,
        "time": now.isoformat(),
        "elapsed_ms": elapsed_ms,
        "delta_ms": None,
    })


def _infer_solution_by_error_key(
    *,
    error_key: str,
    fail_types: list[str],
    reason: str,
    missing_conditions: list[str],
) -> str:
    ft = set(str(x) for x in (fail_types or []) if str(x))
    if error_key == "process:reroute_round_limit":
        return "减少单题 reroute 轮次，优先让 specialist 直接拿到上一轮 critic 的必改项，避免重复兜圈。"
    if error_key == "process:question_elapsed_timeout":
        return "缩短单题链路，优先排查卡住节点和过长 prompt，避免单题执行时间持续超限。"
    if error_key == "critic:per_question_loop_fused":
        return "单题在 critic/fixer 间反复循环，需收紧修复目标或直接改写题目，而不是继续原地修补。"
    if missing_conditions or "reverse_solve_fail" in ft:
        return "题干需补齐判题必要条件（如主贷人/限购/多子女/房龄口径），并确保只有一条可推导路径。"
    if "grounding_fail" in ft:
        return "题干与解析需锁定当前切片，不要混用相似切片规则；必要时在题干中显式标注规则来源。"
    if "code_check_fail" in ft or "answer_mismatch" in ft or "calc" in reason.lower():
        return "计算题改为“先算后写”：正确答案、选项和解析统一引用同一计算结果，避免数值闭环断裂。"
    if "writer_issue" in ft:
        return "收紧 writer 结构化约束，避免解析自相矛盾、选项语义冲突和格式不一致。"
    if "question_type_mismatch" in ft:
        return "检查 question_type 在 router/specialist/writer/critic 之间的传递与覆盖逻辑，确保全链路一致。"
    if error_key == "critic_missing":
        return "确保所有题目必须经过 critic 节点并返回判定结果，再进入保存分支。"
    if error_key == "no_final_json":
        return "检查 writer/fixer 是否稳定产出 final_json，并在失败时直接重生而非空结果返回。"
    if error_key == "storage:append_bank_item_failed":
        return "题目已生成成功但落库失败，需检查题库文件路径、写入权限和 payload 可序列化性。"
    return "根据失败日志补齐约束并做前置校验，减少同类错误反复重试。"


def _classify_generation_attempt_error(
    *,
    question_trace: dict[str, Any],
    q_json: Any,
    critic_seen: bool,
    critic_passed: bool,
    error_text: str,
) -> dict[str, Any]:
    critic_result = question_trace.get("critic_result") if isinstance(question_trace.get("critic_result"), dict) else {}
    fail_types = [str(x) for x in (critic_result.get("fail_types") or []) if str(x).strip()]
    reason = str(critic_result.get("reason", "") or "").strip()
    missing_conditions = [str(x) for x in (critic_result.get("missing_conditions") or []) if str(x).strip()]
    basis_paths = [str(x) for x in (critic_result.get("basis_paths") or []) if str(x).strip()]

    def _canonical_fail_type(ft: str) -> str:
        x = str(ft or "").strip()
        if not x:
            return x
        if x.startswith("calc") or "calculation_" in x:
            return "calculation_fail"
        if x in {"reverse_solve_fail", "answer_mismatch", "grounding_fail"}:
            return x
        if x in {"quality_fail", "explanation_fail", "format_fail", "writer_issue", "readability_fail"}:
            return "writer_quality_family"
        if x in {"code_check_fail"}:
            return "code_check_fail"
        if x in {"question_type_mismatch", "question_type_config_conflict", "prohibit_single_choice_conflict"}:
            return "question_type_contract_fail"
        if x in {"generation_mode"}:
            return "generation_mode_fail"
        return x

    canonical_fail_types = sorted({_canonical_fail_type(x) for x in fail_types if str(x).strip()})

    if isinstance(q_json, dict) and critic_seen and not critic_passed:
        if canonical_fail_types:
            error_key = "critic:" + "|".join(canonical_fail_types)
        else:
            error_key = "critic:rejected"
        category = "critic_rejected"
    elif isinstance(q_json, dict) and not critic_seen:
        error_key = "critic_missing"
        category = "critic_missing"
    elif not isinstance(q_json, dict):
        error_key = "no_final_json"
        category = "no_final_json"
    else:
        # Includes storage/runtime branches that still fall into failure path.
        error_key = "attempt_failed"
        category = "attempt_failed"

    solution = _infer_solution_by_error_key(
        error_key=error_key,
        fail_types=fail_types,
        reason=reason,
        missing_conditions=missing_conditions,
    )
    evidence = reason or str(error_text or "").strip() or "未返回明确失败原因"
    return {
        "error_key": error_key,
        "category": category,
        "reason": reason,
        "evidence": evidence,
        "fail_types": canonical_fail_types or fail_types,
        "missing_conditions": missing_conditions,
        "basis_paths": basis_paths,
        "solution": solution,
    }


def _build_process_control_attempt_error(
    *,
    error_key: str,
    error_text: str,
    question_trace: dict[str, Any],
) -> dict[str, Any]:
    critic_result = question_trace.get("critic_result") if isinstance(question_trace.get("critic_result"), dict) else {}
    fail_types = [str(x) for x in (critic_result.get("fail_types") or []) if str(x).strip()]
    missing_conditions = [str(x) for x in (critic_result.get("missing_conditions") or []) if str(x).strip()]
    basis_paths = [str(x) for x in (critic_result.get("basis_paths") or []) if str(x).strip()]
    reason = str(error_text or "").strip()
    return {
        "error_key": error_key,
        "category": "process_control",
        "reason": reason,
        "evidence": reason or "流程控制中止",
        "fail_types": fail_types,
        "missing_conditions": missing_conditions,
        "basis_paths": basis_paths,
        "solution": _infer_solution_by_error_key(
            error_key=error_key,
            fail_types=fail_types,
            reason=reason,
            missing_conditions=missing_conditions,
        ),
    }


def _build_abort_attempt_error(
    *,
    abort_reason: str,
    question_trace: dict[str, Any],
) -> dict[str, Any]:
    reason = str(abort_reason or "").strip()
    if reason.startswith("超出单题重路由轮次上限"):
        return _build_process_control_attempt_error(
            error_key="process:reroute_round_limit",
            error_text=reason,
            question_trace=question_trace,
        )
    if reason.startswith("超出单题耗时上限"):
        return _build_process_control_attempt_error(
            error_key="process:question_elapsed_timeout",
            error_text=reason,
            question_trace=question_trace,
        )
    if "critic->fixer循环超过3次" in reason:
        return _build_process_control_attempt_error(
            error_key="critic:per_question_loop_fused",
            error_text=reason,
            question_trace=question_trace,
        )
    return _build_process_control_attempt_error(
        error_key="process:aborted",
        error_text=reason or "单题流程被中止",
        question_trace=question_trace,
    )


def _should_skip_fuse_for_error(*, error_key: str, target_question_count: int) -> bool:
    key = str(error_key or "").strip()
    count = int(target_question_count or 0)
    # 单题循环熔断用于“跳过坏题继续跑”，不应再触发整任务级熔断。
    if key == "critic:per_question_loop_fused":
        return True
    # 大批量任务中，writer质量类问题可通过后续题目/重试自愈，不应过早熔断整任务。
    return key == "critic:writer_quality_family" and count >= 100


def _summarize_trace_fail_levels(process_trace: list[dict[str, Any]]) -> tuple[int, int]:
    """Return (hard_failed_count, soft_warning_count) at question level."""
    hard_failed = 0
    soft_warn = 0
    for row in (process_trace or []):
        if not isinstance(row, dict):
            continue
        cr = row.get("critic_result") if isinstance(row.get("critic_result"), dict) else {}
        if not cr:
            continue
        passed = bool(cr.get("passed", False))
        if passed:
            soft_quality_only = bool(cr.get("soft_quality_only"))
            soft_issues = [str(x) for x in (cr.get("soft_quality_issues") or []) if str(x).strip()]
            if soft_quality_only or soft_issues:
                soft_warn += 1
        else:
            hard_failed += 1
    return hard_failed, soft_warn


def _should_soft_pass_on_format_only_fuse(critic_result: dict[str, Any]) -> bool:
    return _is_abort_whitelist_pass(critic_result)


_CRITIC_ABORT_WHITELIST_FAIL_TYPES = {
    "format_fail",
    "format_bracket",
    "quality_fail",
    "readability_fail",
    "explanation_fail",
    "condition_overload",
    "difficulty_out_of_range",
    "focus_overload",
    "term_lock_fail",
    "name_semantic_issue",
    "writer_issue",
    "critic_schema_incomplete",
    "generation_mode",
}


def _is_distractor_weak_issue_text(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    # 仅识别“干扰项偏弱/迷惑性不足”这一类可修复质量问题。
    if not re.search(r"(干扰项|选项)", t):
        return False
    weak_markers = [
        r"幼稚",
        r"迷惑性",
        r"质量不佳",
        r"无需专业知识即可排除",
        r"错误原因过于表面",
    ]
    if not any(re.search(p, t) for p in weak_markers):
        return False
    # 明确排除其他质量失败类型（如直给答案、前提缺失、多解等）。
    block_markers = [
        r"直给答案",
        r"缺少.*前提",
        r"无法唯一",
        r"多解",
        r"答案不一致",
        r"解析.*不一致",
        r"计算题无正确答案",
    ]
    return not any(re.search(p, t) for p in block_markers)


def _is_single_distractor_weak_only_pass(critic_result: dict[str, Any]) -> bool:
    fail_types = [str(x).strip() for x in (critic_result.get("fail_types") or []) if str(x).strip()]
    if set(fail_types) != {"quality_fail"}:
        return False

    quality_issues = [str(x).strip() for x in (critic_result.get("quality_issues") or []) if str(x).strip()]
    if not quality_issues:
        return False
    # 仅允许“一个质量问题且该问题就是干扰项偏弱”。
    if len(quality_issues) != 1:
        return False
    return _is_distractor_weak_issue_text(quality_issues[0])


def _extract_critic_issue_record(critic_result: dict[str, Any] | None) -> tuple[list[str], str]:
    if not isinstance(critic_result, dict):
        return [], ""
    fail_types = [str(x).strip() for x in (critic_result.get("fail_types") or []) if str(x).strip()]
    parts: list[str] = []
    reason = str(critic_result.get("reason", "") or "").strip()
    details = str(critic_result.get("details", "") or "").strip()
    if reason:
        parts.append(reason)
    if details and details != reason:
        parts.append(details)
    for field in ("all_issues", "quality_issues", "missing_conditions"):
        values = [str(x).strip() for x in (critic_result.get(field) or []) if str(x).strip()]
        if values:
            parts.append(f"{field}: " + "；".join(values))
    return fail_types, "\n".join(parts).strip()


def _is_abort_whitelist_pass(critic_result: dict[str, Any] | None) -> bool:
    if not isinstance(critic_result, dict):
        return False
    if bool(critic_result.get("passed")):
        return False
    fail_types = [str(x).strip() for x in (critic_result.get("fail_types") or []) if str(x).strip()]
    if not fail_types:
        return False
    if _is_single_distractor_weak_only_pass(critic_result):
        return True
    if set(fail_types).issubset(_CRITIC_ABORT_WHITELIST_FAIL_TYPES):
        return True
    return bool(
        critic_result.get("answer_field_mismatch_whitelist_candidate")
        or critic_result.get("question_type_alignment_whitelist_candidate")
    )


def _build_whitelist_pass_bank_item(
    *,
    final_json: dict[str, Any],
    critic_result: dict[str, Any],
    task_id: str,
    task_name: str,
    run_id: str,
) -> dict[str, Any]:
    item = deepcopy(final_json)
    fail_types, error_content = _extract_critic_issue_record(critic_result)
    if task_id:
        item["出题任务ID"] = task_id
    if task_name:
        item["出题任务名称"] = task_name
    item["出题RunID"] = run_id
    item["审计状态"] = "whitelist_pass"
    item["是否正式通过"] = True
    item["白名单通过"] = True
    item["白名单错误类型"] = fail_types
    item["白名单错误内容"] = error_content
    item["白名单critic结果"] = deepcopy(critic_result)
    return item


def _record_slice_generation_failure(
    *,
    tenant_id: str,
    material_version_id: str,
    slice_id: int,
    critic_result: dict[str, Any] | None,
    task_id: str,
    run_id: str,
) -> dict[str, Any]:
    path = _slice_generation_health_file_by_material(tenant_id)
    bucket = _load_material_bucket(path, material_version_id)
    key = str(int(slice_id))
    current = bucket.get(key) if isinstance(bucket.get(key), dict) else {}
    fail_types, error_content = _extract_critic_issue_record(critic_result if isinstance(critic_result, dict) else {})
    failure_count = int(current.get("failure_count", 0) or 0) + 1
    manual_blocked = bool(current.get("manual_blocked"))
    manual_reason = str(current.get("blocked_reason", "") or "").strip()
    auto_blocked = failure_count > 10
    blocked = manual_blocked or auto_blocked
    blocked_reason = (
        manual_reason
        if manual_blocked
        else ("该切片累计非白名单失败超过10次，修改前禁止继续出题" if auto_blocked else "")
    )
    bucket[key] = {
        "slice_id": int(slice_id),
        "failure_count": failure_count,
        "blocked": blocked,
        "blocked_reason": blocked_reason,
        "manual_blocked": manual_blocked,
        "block_source": "manual" if manual_blocked else ("auto" if auto_blocked else ""),
        "last_fail_types": fail_types,
        "last_error_content": error_content,
        "last_task_id": str(task_id or ""),
        "last_run_id": str(run_id or ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_material_bucket(path, material_version_id, bucket)
    return dict(bucket[key])


def _build_slice_candidate_lookup(
    candidate_slices: list[dict[str, Any]],
    *,
    template_route_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    by_id: dict[int, dict[str, Any]] = {}
    bucket_to_ids: dict[tuple[str, str], list[int]] = {}
    template_bucket_to_ids: dict[tuple[str, str], list[int]] = {}
    route_rules = _normalize_route_rules(template_route_rules or [])
    for row in candidate_slices or []:
        if not isinstance(row, dict):
            continue
        try:
            sid = int(row.get("slice_id"))
        except (TypeError, ValueError):
            continue
        path = str(row.get("path", "") or "").strip()
        mastery = str(row.get("mastery", "") or "").strip()
        bucket_key = (_path_prefix(path, 3), mastery)
        info = {"slice_id": sid, "path": path, "mastery": mastery, "bucket_key": bucket_key}
        matched_route_prefix = ""
        if route_rules:
            matched = [
                str(rule.get("path_prefix", "")).strip()
                for rule in route_rules
                if path.startswith(str(rule.get("path_prefix", "")).strip())
            ]
            if matched:
                matched_route_prefix = max(matched, key=len)
                template_bucket_to_ids.setdefault((matched_route_prefix, mastery), []).append(sid)
                info["template_bucket_key"] = (matched_route_prefix, mastery)
        by_id[sid] = info
        bucket_to_ids.setdefault(bucket_key, []).append(sid)
    return {
        "by_id": by_id,
        "bucket_to_ids": bucket_to_ids,
        "template_bucket_to_ids": template_bucket_to_ids,
    }


def _normalize_slice_usage_counts(raw_counts: Any) -> dict[int, int]:
    out: dict[int, int] = {}
    if isinstance(raw_counts, dict):
        iterable = raw_counts.items()
    else:
        iterable = []
    for key, value in iterable:
        try:
            sid = int(key)
            count = int(value or 0)
        except (TypeError, ValueError):
            continue
        if sid > 0 and count > 0:
            out[sid] = count
    return out


def _build_task_saved_slice_counts_from_bank(tenant_id: str, task_name: str) -> dict[int, int]:
    name = str(task_name or "").strip()
    if not name:
        return {}
    counts: dict[int, int] = {}
    for row in _load_bank(tenant_bank_path(tenant_id)):
        if not isinstance(row, dict):
            continue
        row_task_name = str(row.get("出题任务名称") or row.get("task_name") or "").strip()
        if row_task_name != name and not row_task_name.startswith(f"{name}#"):
            continue
        try:
            sid = int(row.get("来源切片ID") or 0)
        except (TypeError, ValueError):
            continue
        if sid <= 0:
            continue
        counts[sid] = int(counts.get(sid, 0) or 0) + 1
    return counts


def _pick_preferred_slice_id(
    candidate_ids: list[int],
    *,
    usage_counts: dict[int, int] | None = None,
    excluded_slice_ids: set[int] | None = None,
    max_questions_per_slice: int = 0,
    prefer_unused: bool = True,
) -> int | None:
    usage = usage_counts or {}
    excluded = {int(x) for x in (excluded_slice_ids or set())}
    filtered: list[int] = []
    overflow: list[int] = []
    for sid in candidate_ids or []:
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            continue
        if sid_int in excluded:
            continue
        if max_questions_per_slice > 0 and int(usage.get(sid_int, 0) or 0) >= max_questions_per_slice:
            overflow.append(sid_int)
            continue
        filtered.append(sid_int)
    pool = filtered or overflow
    if not pool:
        return None
    ordered = list(pool)
    random.shuffle(ordered)
    ordered.sort(
        key=lambda sid: (
            0 if prefer_unused and int(usage.get(sid, 0) or 0) <= 0 else 1,
            int(usage.get(sid, 0) or 0),
            sid,
        )
    )
    return ordered[0]


def _choose_generation_slice_id(
    *,
    planned_slice_ids: list[int],
    planned_slots: list[dict[str, Any]] | None,
    success_index: int,
    candidate_ids: list[int],
    attempt_count: int,
    target_question_count: int,
    excluded_slice_ids: set[int],
    candidate_lookup: dict[str, Any] | None,
    slice_usage_counts: dict[int, int] | None = None,
    max_questions_per_slice: int = 0,
) -> tuple[int | None, str]:
    excluded = {int(x) for x in (excluded_slice_ids or set())}
    usage_counts = _normalize_slice_usage_counts(slice_usage_counts)
    if planned_slice_ids and success_index < len(planned_slice_ids):
        target_sid = int(planned_slice_ids[success_index])
        target_usage = int(usage_counts.get(target_sid, 0) or 0)
        if target_sid not in excluded and (max_questions_per_slice <= 0 or target_usage < max_questions_per_slice):
            return target_sid, ""
        lookup = candidate_lookup or {}
        by_id = lookup.get("by_id") if isinstance(lookup.get("by_id"), dict) else {}
        bucket_to_ids = lookup.get("bucket_to_ids") if isinstance(lookup.get("bucket_to_ids"), dict) else {}
        template_bucket_to_ids = (
            lookup.get("template_bucket_to_ids")
            if isinstance(lookup.get("template_bucket_to_ids"), dict)
            else {}
        )
        bucket_key = ((by_id.get(target_sid) or {}).get("bucket_key")) if isinstance(by_id, dict) else None
        if bucket_key in bucket_to_ids:
            preferred = _pick_preferred_slice_id(
                bucket_to_ids.get(bucket_key, []),
                usage_counts=usage_counts,
                excluded_slice_ids=excluded,
                max_questions_per_slice=max_questions_per_slice,
                prefer_unused=True,
            )
            if preferred is not None:
                return preferred, ""
        planned_slot = planned_slots[success_index] if isinstance(planned_slots, list) and success_index < len(planned_slots) and isinstance(planned_slots[success_index], dict) else {}
        route_prefix = str(planned_slot.get("route_prefix", "") or "").strip()
        mastery = str(planned_slot.get("mastery", "") or "").strip()
        template_bucket_key = (route_prefix, mastery) if route_prefix and mastery else None
        same_mastery_all_ids: list[int] = []
        if template_bucket_key in template_bucket_to_ids:
            preferred = _pick_preferred_slice_id(
                template_bucket_to_ids.get(template_bucket_key, []),
                usage_counts=usage_counts,
                excluded_slice_ids=excluded,
                max_questions_per_slice=max_questions_per_slice,
                prefer_unused=True,
            )
            if preferred is not None:
                return preferred, ""
        if mastery:
            for (rp, m), sids in template_bucket_to_ids.items():
                if m == mastery:
                    same_mastery_all_ids.extend(sids)
            if same_mastery_all_ids:
                preferred = _pick_preferred_slice_id(
                    same_mastery_all_ids,
                    usage_counts=usage_counts,
                    excluded_slice_ids=excluded,
                    max_questions_per_slice=max_questions_per_slice,
                    prefer_unused=True,
                )
                if preferred is not None:
                    return preferred, ""
        same_mastery_pool = len(set(same_mastery_all_ids)) if mastery else 0
        strict_pool = len(
            [
                sid for sid in (template_bucket_to_ids.get(template_bucket_key, []) if template_bucket_key else [])
                if str(sid).isdigit()
            ]
        )
        return None, (
            f"模板位次不可替代: target={success_index + 1} route={route_prefix or '-'} mastery={mastery or '-'} "
            f"strict_candidates={strict_pool} same_mastery_candidates={same_mastery_pool}"
        )
    available = [int(sid) for sid in candidate_ids if int(sid) not in excluded]
    if not available:
        return None, "当前范围内已无可继续出题的切片"
    preferred = _pick_preferred_slice_id(
        available,
        usage_counts=usage_counts,
        excluded_slice_ids=excluded,
        max_questions_per_slice=max_questions_per_slice,
        prefer_unused=True,
    )
    if preferred is not None:
        return preferred, ""
    if target_question_count > len(available):
        return available[(attempt_count - 1) % len(available)], ""
    return random.choice(available), ""


def _planned_slot_trace_fields(
    planned_slots: list[dict[str, Any]] | None,
    success_index: int,
) -> dict[str, Any]:
    """
    从出题计划位次取出路由前缀与掌握程度，写入 process_trace，便于详情页核对模板比例。

    :param planned_slots: 与 api_generate 内 success_index 对齐的计划位次列表
    :param success_index: 当前题在 planned_slots 中的 0-based 下标
    :return: 供合并进 trace 的字典（无计划时为空）
    """
    if not isinstance(planned_slots, list):
        return {}
    idx = int(success_index)
    if idx < 0 or idx >= len(planned_slots):
        return {}
    slot = planned_slots[idx]
    if not isinstance(slot, dict):
        return {}
    rp = str(slot.get("route_prefix", "") or "").strip()
    mastery = str(slot.get("mastery", "") or "").strip()
    out: dict[str, Any] = {}
    if rp:
        out["planned_route_prefix"] = rp
    if mastery:
        out["planned_mastery"] = mastery
    return out


def _planned_slot_target_index(
    planned_slots: list[dict[str, Any]] | None,
    success_index: int,
) -> int:
    """Resolve 1-based global template target index for the current local success_index."""
    default_idx = int(success_index) + 1
    if not isinstance(planned_slots, list):
        return default_idx
    idx = int(success_index)
    if idx < 0 or idx >= len(planned_slots):
        return default_idx
    slot = planned_slots[idx]
    if not isinstance(slot, dict):
        return default_idx
    try:
        gti = int(slot.get("_global_target_index", 0) or 0)
    except (TypeError, ValueError):
        gti = 0
    return int(gti if gti > 0 else default_idx)


def _is_template_same_mastery_hard_gap(
    *,
    planned_slots: list[dict[str, Any]] | None,
    success_index: int,
    sid: int,
    candidate_lookup: dict[str, Any] | None,
) -> bool:
    if not isinstance(planned_slots, list) or success_index >= len(planned_slots):
        return False
    planned_slot = planned_slots[success_index] if isinstance(planned_slots[success_index], dict) else {}
    route_prefix = str(planned_slot.get("route_prefix", "") or "").strip()
    mastery = str(planned_slot.get("mastery", "") or "").strip()
    if not route_prefix or not mastery:
        return False
    lookup = candidate_lookup or {}
    template_bucket_to_ids = (
        lookup.get("template_bucket_to_ids")
        if isinstance(lookup.get("template_bucket_to_ids"), dict)
        else {}
    )
    peers = []
    for alt_sid in template_bucket_to_ids.get((route_prefix, mastery), []):
        try:
            alt_sid_int = int(alt_sid)
        except (TypeError, ValueError):
            continue
        if alt_sid_int != int(sid):
            peers.append(alt_sid_int)
    return len(peers) == 0


def _template_slot_candidate_ids(
    *,
    planned_slots: list[dict[str, Any]] | None,
    target_index: int,
    candidate_lookup: dict[str, Any] | None,
    include_cross_route_same_mastery: bool = True,
) -> list[int]:
    slots = [slot for slot in (planned_slots or []) if isinstance(slot, dict)]
    idx = int(target_index or 0)
    if idx <= 0 or idx > len(slots):
        return []
    slot = slots[idx - 1] if isinstance(slots[idx - 1], dict) else {}
    route_prefix = str(slot.get("route_prefix", "") or "").strip()
    mastery = str(slot.get("mastery", "") or "").strip()
    if not route_prefix or not mastery:
        return []
    lookup = candidate_lookup or {}
    template_bucket_to_ids = (
        lookup.get("template_bucket_to_ids")
        if isinstance(lookup.get("template_bucket_to_ids"), dict)
        else {}
    )
    candidate_ids: list[int] = []
    for sid in (template_bucket_to_ids.get((route_prefix, mastery), []) or []):
        if str(sid).isdigit():
            candidate_ids.append(int(sid))
    if include_cross_route_same_mastery:
        for (rp, m), sids in template_bucket_to_ids.items():
            if m != mastery or rp == route_prefix:
                continue
            for sid in (sids or []):
                if str(sid).isdigit():
                    candidate_ids.append(int(sid))
    # keep order while removing duplicates
    deduped: list[int] = []
    seen: set[int] = set()
    for sid in candidate_ids:
        if sid in seen:
            continue
        seen.add(sid)
        deduped.append(sid)
    return deduped


def _describe_template_target_gap(
    *,
    target_index: int,
    planned_slots: list[dict[str, Any]] | None,
    candidate_lookup: dict[str, Any] | None,
    process_trace: list[dict[str, Any]] | None = None,
) -> str:
    slots = [slot for slot in (planned_slots or []) if isinstance(slot, dict)]
    idx = int(target_index or 0)
    if idx <= 0 or idx > len(slots):
        return f"target={idx} 超出模板位次范围"
    slot = slots[idx - 1] if isinstance(slots[idx - 1], dict) else {}
    route_prefix = str(slot.get("route_prefix", "") or "").strip() or "-"
    mastery = str(slot.get("mastery", "") or "").strip() or "-"
    strict_count = len(
        _template_slot_candidate_ids(
            planned_slots=slots,
            target_index=idx,
            candidate_lookup=candidate_lookup,
            include_cross_route_same_mastery=False,
        )
    )
    fallback_count = len(
        _template_slot_candidate_ids(
            planned_slots=slots,
            target_index=idx,
            candidate_lookup=candidate_lookup,
            include_cross_route_same_mastery=True,
        )
    )
    attempted: list[int] = []
    for row in (process_trace or []):
        if not isinstance(row, dict):
            continue
        try:
            row_target_index = int(row.get("target_index", 0) or 0)
        except (TypeError, ValueError):
            row_target_index = 0
        if row_target_index != idx:
            continue
        try:
            sid = int(row.get("slice_id", 0) or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid > 0 and sid not in attempted:
            attempted.append(sid)
    attempted_text = ",".join(str(x) for x in attempted[:12]) if attempted else "-"
    return (
        f"target={idx} route={route_prefix} mastery={mastery} "
        f"strict_candidates={strict_count} same_mastery_candidates={fallback_count} "
        f"attempted_slice_ids={attempted_text}"
    )


def _sort_template_target_indexes_by_ease(
    *,
    indexes: list[int],
    planned_slots: list[dict[str, Any]] | None,
    candidate_lookup: dict[str, Any] | None,
) -> list[int]:
    """Sort target indexes by replacement ease: easy first, hard last."""
    unique_indexes = sorted({int(x) for x in (indexes or []) if int(x) > 0})
    if not unique_indexes:
        return []
    slots = [slot for slot in (planned_slots or []) if isinstance(slot, dict)]
    def _candidate_count(idx: int) -> int:
        return len(
            _template_slot_candidate_ids(
                planned_slots=slots,
                target_index=idx,
                candidate_lookup=candidate_lookup,
                include_cross_route_same_mastery=True,
            )
        )
    return sorted(unique_indexes, key=lambda idx: (-_candidate_count(idx), idx))


def _should_exclude_failed_slice_from_task(
    *,
    allow_single_retry: bool,
    sid: int,
    failure_counts: dict[int, int] | None,
    retry_limit: int = 2,
) -> bool:
    if not allow_single_retry:
        return True
    counts = failure_counts if isinstance(failure_counts, dict) else {}
    current = int(counts.get(int(sid), 0) or 0) + 1
    counts[int(sid)] = current
    return current >= max(1, int(retry_limit or 1))


def _should_abort_question_attempt(
    *,
    started_at: datetime,
    current_run_id: int,
    max_graph_rounds_per_question: int,
    max_question_elapsed_ms: int,
) -> tuple[bool, str]:
    elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    if current_run_id >= max_graph_rounds_per_question:
        return True, f"超出单题重路由轮次上限({max_graph_rounds_per_question})"
    if elapsed_ms >= max_question_elapsed_ms:
        return True, f"超出单题耗时上限({max_question_elapsed_ms}ms)"
    return False, ""


def _merge_llm_trace_records(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in list(existing or []) + list(incoming or []):
        if not isinstance(row, dict):
            continue
        call_id = str(row.get("call_id", "") or "").strip()
        if call_id:
            key = f"call_id:{call_id}"
        else:
            # Backward-compatible fallback for old rows without call_id.
            # Include more fields to avoid dropping distinct calls within the same second.
            key = "|".join(
                [
                    str(row.get("trace_id", "") or ""),
                    str(row.get("node", "") or ""),
                    str(row.get("ts_ms", "") or row.get("ts", "") or ""),
                    str(row.get("model", "") or ""),
                    str(row.get("prompt_tokens", "") or ""),
                    str(row.get("completion_tokens", "") or ""),
                    str(row.get("total_tokens", "") or ""),
                    str(row.get("latency_ms", "") or ""),
                    str(row.get("retries", "") or ""),
                    str(row.get("success", "") or ""),
                    str(row.get("error", "") or ""),
                ]
            )
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


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


def _load_slice_progress_for_material(tenant_id: str, material_version_id: str) -> dict[str, Any]:
    """
    Read live slice progress produced by generate_knowledge_slices.py.
    """
    out = {"progress": 0, "message": ""}
    p = tenant_slices_dir(tenant_id) / f"knowledge_slices_{material_version_id}.jsonl.progress.jsonl"
    if not p.exists() or not p.is_file():
        return out
    try:
        last_image_idx = 0
        last_image_total = 0
        last_final_idx = 0
        last_final_total = 0
        done = False
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    row = json.loads(s)
                except Exception:
                    continue
                event = str(row.get("event", "") or "")
                payload = row.get("payload", {}) or {}
                if event == "image":
                    idx = int(payload.get("index", 0) or 0)
                    total = int(payload.get("total", 0) or 0)
                    if idx > last_image_idx:
                        last_image_idx = idx
                    if total > 0:
                        last_image_total = total
                elif event == "slice_final_written":
                    idx = int(payload.get("index", 0) or 0)
                    total = int(payload.get("total", 0) or 0)
                    if idx > last_final_idx:
                        last_final_idx = idx
                    if total > 0:
                        last_final_total = total
                elif event == "done":
                    done = True
        if done:
            out["progress"] = 100
            out["message"] = "切片处理完成"
            return out
        if last_final_total > 0:
            pct = int(max(0, min(100, round(last_final_idx * 100 / max(1, last_final_total)))))
            out["progress"] = pct
            out["message"] = f"正在写入切片 {last_final_idx}/{last_final_total}"
            return out
        if last_image_total > 0:
            pct = int(max(0, min(100, round(last_image_idx * 100 / max(1, last_image_total)))))
            out["progress"] = pct
            out["message"] = f"正在处理图片 {last_image_idx}/{last_image_total}"
            return out
    except Exception:
        return out
    return out


def _mapping_progress_file_for_material(tenant_id: str, material_version_id: str) -> Path:
    root = tenant_root(tenant_id) / "mapping"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"knowledge_question_mapping_{material_version_id}.progress.jsonl"


def _append_mapping_progress_event(
    tenant_id: str,
    material_version_id: str,
    *,
    status: str,
    progress: int,
    message: str,
) -> None:
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": str(status or "").strip() or "running",
        "progress": max(0, min(100, int(progress))),
        "message": str(message or "").strip(),
    }
    _append_jsonl(_mapping_progress_file_for_material(tenant_id, material_version_id), row)


def _load_mapping_progress_for_material(tenant_id: str, material_version_id: str) -> dict[str, Any]:
    out = {"status": "", "progress": 0, "message": ""}
    p = _mapping_progress_file_for_material(tenant_id, material_version_id)
    if not p.exists() or not p.is_file():
        return out
    try:
        last = None
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    row = json.loads(s)
                except Exception:
                    continue
                if isinstance(row, dict):
                    last = row
        if not isinstance(last, dict):
            return out
        out["status"] = str(last.get("status", "") or "")
        out["progress"] = max(0, min(100, int(last.get("progress", 0) or 0)))
        out["message"] = str(last.get("message", "") or "")
        return out
    except Exception:
        return out


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


def _slice_generation_health_file_by_material(tenant_id: str) -> Path:
    path = tenant_root(tenant_id) / "slices" / "slice_generation_health_by_material.json"
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


def _load_slice_generation_health_for_material(tenant_id: str, material_version_id: str) -> dict[str, dict[str, Any]]:
    return _load_material_bucket(_slice_generation_health_file_by_material(tenant_id), material_version_id)


def _save_slice_generation_health_for_material(
    tenant_id: str,
    material_version_id: str,
    bucket: dict[str, dict[str, Any]],
) -> None:
    _save_material_bucket(_slice_generation_health_file_by_material(tenant_id), material_version_id, bucket)


def _reset_slice_generation_health_for_material(
    tenant_id: str,
    material_version_id: str,
    *,
    slice_ids: list[int],
    reason: str,
) -> None:
    path = _slice_generation_health_file_by_material(tenant_id)
    bucket = _load_material_bucket(path, material_version_id)
    changed = False
    for sid in slice_ids:
        key = str(int(sid))
        if key not in bucket:
            continue
        current = bucket.get(key) if isinstance(bucket.get(key), dict) else {}
        # 手工禁用是显式业务决策，不应在切片内容/图片改动时自动解除。
        if bool(current.get("manual_blocked")):
            continue
        changed = True
        bucket[key] = {
            "slice_id": int(sid),
            "failure_count": 0,
            "blocked": False,
            "blocked_reason": "",
            "manual_blocked": False,
            "block_source": "",
            "last_fail_types": [],
            "last_error_content": "",
            "last_task_id": "",
            "last_run_id": "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reset_reason": str(reason or "").strip(),
        }
    if changed:
        _save_material_bucket(path, material_version_id, bucket)


def _set_slice_generation_manual_block_for_material(
    tenant_id: str,
    material_version_id: str,
    *,
    slice_id: int,
    blocker: str,
    reason: str,
) -> dict[str, Any]:
    path = _slice_generation_health_file_by_material(tenant_id)
    bucket = _load_material_bucket(path, material_version_id)
    key = str(int(slice_id))
    current = bucket.get(key) if isinstance(bucket.get(key), dict) else {}
    now = datetime.now(timezone.utc).isoformat()
    next_reason = str(reason or "").strip() or "该切片已被手工标记为禁止出题"
    bucket[key] = {
        "slice_id": int(slice_id),
        "failure_count": int(current.get("failure_count", 0) or 0),
        "blocked": True,
        "blocked_reason": next_reason,
        "manual_blocked": True,
        "block_source": "manual",
        "last_fail_types": [str(x) for x in (current.get("last_fail_types") or []) if str(x).strip()],
        "last_error_content": str(current.get("last_error_content", "") or ""),
        "last_task_id": str(current.get("last_task_id", "") or ""),
        "last_run_id": str(current.get("last_run_id", "") or ""),
        "updated_at": now,
        "blocked_at": now,
        "blocked_by": str(blocker or ""),
    }
    _save_material_bucket(path, material_version_id, bucket)
    return dict(bucket[key])


def _blocked_slice_ids_for_material(tenant_id: str, material_version_id: str) -> set[int]:
    bucket = _load_slice_generation_health_for_material(tenant_id, material_version_id)
    out: set[int] = set()
    for k, v in bucket.items():
        if not (
            str(k).isdigit()
            and isinstance(v, dict)
            and (bool(v.get("blocked")) or bool(v.get("manual_blocked")))
        ):
            continue
        try:
            out.add(int(k))
        except (TypeError, ValueError):
            continue
    return out


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
    manual_question_stem: str = "",
    manual_question_options: list[str] | None = None,
    manual_question_explanation: str = "",
) -> None:
    path = _mapping_review_file_by_material(tenant_id)
    bucket = _load_material_bucket(path, material_version_id)
    normalized_status = _normalize_mapping_status(confirm_status)
    options = manual_question_options if isinstance(manual_question_options, list) else []
    options = [str(x or "").strip() for x in options if str(x or "").strip()][:8]
    bucket[str(map_key)] = {
        "map_key": str(map_key),
        "confirm_status": normalized_status,
        "reviewer": reviewer,
        "comment": comment,
        "target_mother_question_id": target_mother_question_id,
        "manual_question_stem": str(manual_question_stem or "").strip(),
        "manual_question_options": options,
        "manual_question_explanation": str(manual_question_explanation or "").strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "material_version_id": material_version_id,
    }
    _save_material_bucket(path, material_version_id, bucket)


def _load_history_rows(tenant_id: str) -> dict[int, dict[str, Any]]:
    def _extract_options_from_record(rec: Any) -> list[str]:
        if not isinstance(rec, dict):
            return []
        options: list[str] = []
        raw_options = rec.get("选项")
        if isinstance(raw_options, dict):
            for key in ("A", "B", "C", "D", "E", "F", "G", "H"):
                txt = str(raw_options.get(key, "") or "").strip()
                if txt:
                    options.append(txt)
        elif isinstance(raw_options, list):
            for value in raw_options[:8]:
                txt = str(value or "").strip()
                if txt:
                    options.append(txt)
        for i in range(1, 9):
            txt = str(rec.get(f"选项{i}", "") or "").strip()
            if txt:
                options.append(txt)
        for key in ("A", "B", "C", "D", "E", "F", "G", "H"):
            txt = str(rec.get(f"选项{key}", "") or rec.get(f"选项{key}(必填)", "") or rec.get(key, "") or "").strip()
            if txt:
                options.append(txt)
        dedup: list[str] = []
        seen: set[str] = set()
        for opt in options:
            key = opt.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            dedup.append(key)
        return dedup

    history_path = resolve_tenant_history_path(tenant_id)
    rows: dict[int, dict[str, Any]] = {}
    if Path(history_path).exists():
        try:
            df = load_reference_questions(history_path)
            for idx, row in df.iterrows():
                stem = str(row.get("题干", "")).strip()
                ans = str(row.get("正确答案", "")).strip()
                exp = str(row.get("解析", "")).strip()
                options = _extract_options_from_record(row.to_dict() if hasattr(row, "to_dict") else row)
                rows[int(idx)] = {
                    "题干": stem,
                    "选项": options,
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
            rows[idx] = {
                "题干": stem,
                "选项": _extract_options_from_record(rec),
                "正确答案": ans,
                "解析": exp,
            }
            idx += 1
    return rows


def _is_mapping_review_ready(q_row: dict[str, Any]) -> tuple[bool, list[str]]:
    if not isinstance(q_row, dict):
        return False, ["题干", "选项", "解析"]
    stem = str(q_row.get("题干", "") or "").strip()
    explanation = str(q_row.get("解析", "") or "").strip()
    options = q_row.get("选项", [])
    if not isinstance(options, list):
        options = []
    options = [str(x or "").strip() for x in options if str(x or "").strip()]
    missing: list[str] = []
    if not stem:
        missing.append("题干")
    if not options:
        missing.append("选项")
    if not explanation:
        missing.append("解析")
    return len(missing) == 0, missing


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
    for ext in (".xlsx", ".xls", ".docx", ".txt", ".md"):
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
    for ext in (".xlsx", ".xls", ".docx", ".txt", ".md"):
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
    _delete_material_bucket(_slice_generation_health_file_by_material(tenant_id), material_version_id)
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


def _random_difficulty_buckets() -> list[tuple[float, float]]:
    buckets = [(0.3, 0.5), (0.5, 0.7), (0.7, 0.9)]
    random.shuffle(buckets)
    return buckets


def _normalize_answer_key(raw_value: Any) -> str:
    text = str(raw_value or "").strip().upper()
    if not text:
        return ""
    text = text.replace("，", ",").replace(" ", "")
    if "," in text:
        parts = [p for p in text.split(",") if p]
        letters = [p for p in parts if len(p) == 1 and p in "ABCDEFGH"]
        if letters and len(letters) == len(parts):
            return "".join(sorted(set(letters)))
        return ",".join(parts)
    if text and all(ch in "ABCDEFGH" for ch in text):
        return "".join(sorted(set(text)))
    return text


def _normalize_text_key(raw_value: Any) -> str:
    text = str(raw_value or "").strip()
    if not text:
        return ""
    return re.sub(r"\s+", "", text)


def _build_bank_origin_lookup(tenant_id: str) -> dict[str, dict[str, Any]]:
    def _fmt_offline_quality_conclusion(oj: dict[str, Any]) -> str:
        basis = str(oj.get("quality_scoring_basis", "") or "").strip()
        reasons = [str(x).strip() for x in (oj.get("quality_reasons") or []) if str(x).strip()]
        dim = oj.get("quality_dimension_feedback") if isinstance(oj.get("quality_dimension_feedback"), dict) else {}
        dim_lines = [f"{str(k).strip()}:{str(v).strip()}" for k, v in dim.items() if str(k).strip() and str(v).strip()]
        parts: list[str] = []
        if basis:
            parts.append(f"核心依据：{basis}")
        if reasons:
            parts.append(f"质量原因：{'；'.join(reasons)}")
        if dim_lines:
            parts.append(f"分维反馈：{'；'.join(dim_lines)}")
        return "\n".join(parts).strip()

    def _fmt_offline_baseline_conclusion(oj: dict[str, Any]) -> str:
        dim = oj.get("dimension_results") if isinstance(oj.get("dimension_results"), dict) else {}
        dim_lines: list[str] = []
        for name, raw in dim.items():
            dr = raw if isinstance(raw, dict) else {}
            status = str(dr.get("status", "") or "").strip().upper() or "-"
            score_raw = dr.get("score_10")
            score_txt = "-"
            if score_raw is not None:
                try:
                    score_txt = f"{float(score_raw):.1f}"
                except Exception:
                    score_txt = str(score_raw)
            issues = [str(x).strip() for x in (dr.get("issues") or []) if str(x).strip()]
            reasons = [str(x).strip() for x in (dr.get("reasons") or []) if str(x).strip()]
            detail = "；".join((issues + reasons)[:2]).strip()
            dim_lines.append(f"{name}(status={status},score={score_txt}){f'：{detail}' if detail else ''}")
        all_reasons = [str(x).strip() for x in (oj.get("reasons") or []) if str(x).strip()]
        baseline_reasons = [x for x in all_reasons if not x.startswith("【质量评分】") and not x.startswith("【质量评分依据】")]
        hard_gate = oj.get("hard_gate") if isinstance(oj.get("hard_gate"), dict) else {}
        hard_fail = [str(k).strip() for k, v in hard_gate.items() if v is False and str(k).strip()]
        parts: list[str] = []
        if dim_lines:
            parts.append(f"维度依据：{'；'.join(dim_lines[:8])}")
        if baseline_reasons:
            parts.append(f"基线原因：{'；'.join(baseline_reasons)}")
        if hard_fail:
            parts.append(f"硬闸门未通过：{'、'.join(hard_fail)}")
        return "\n".join(parts).strip()

    task_name_lookup: dict[str, str] = {}
    for row in _latest_gen_task_rows(tenant_id, allow_full_fallback=True).values():
        if not isinstance(row, dict):
            continue
        tid = str(row.get("task_id", "")).strip()
        tname = str(row.get("task_name", "")).strip()
        if tid and tname:
            task_name_lookup[tid] = tname
    runs = _read_jsonl(_qa_runs_path(tenant_id))
    lookup: dict[str, dict[str, Any]] = {}
    lookup_no_material: dict[str, dict[str, Any]] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_id = str(run.get("run_id", "")).strip()
        config = run.get("config") if isinstance(run.get("config"), dict) else {}
        task_id = str(config.get("task_id", "")).strip()
        task_name = str(config.get("task_name", "")).strip() or task_name_lookup.get(task_id, "")
        material_version_id = str(run.get("material_version_id", "")).strip()
        questions = run.get("questions") if isinstance(run.get("questions"), list) else []
        for q in questions:
            if not isinstance(q, dict):
                continue
            judge_input = q.get("judge_input") if isinstance(q.get("judge_input"), dict) else {}
            stem_key = _normalize_text_key(q.get("question_text") or judge_input.get("stem"))
            path_key = _normalize_text_key(q.get("slice_path"))
            answer_key = _normalize_answer_key(q.get("answer") or judge_input.get("correct_answer"))
            if not stem_key:
                continue
            offline_judge = q.get("offline_judge") if isinstance(q.get("offline_judge"), dict) else {}
            score = offline_judge.get("overall_score")
            if score is None:
                score = offline_judge.get("quality_score")
            baseline_score = offline_judge.get("baseline_score", offline_judge.get("penalty_score"))
            quality_score = offline_judge.get("quality_score")
            decision = str(offline_judge.get("decision", "") or "").strip().lower()
            meta = {
                "source_run_id": run_id,
                "source_task_id": task_id,
                "source_task_name": task_name,
                "offline_judge_score": score,
                "offline_judge_decision": decision,
                "offline_judge_quality_score": quality_score,
                "offline_judge_baseline_score": baseline_score,
                "offline_judge_quality_conclusion": _fmt_offline_quality_conclusion(offline_judge),
                "offline_judge_baseline_conclusion": _fmt_offline_baseline_conclusion(offline_judge),
            }
            key_with_material = f"{material_version_id}|{path_key}|{answer_key}|{stem_key}"
            key_without_material = f"{path_key}|{answer_key}|{stem_key}"
            lookup[key_with_material] = meta
            lookup_no_material[key_without_material] = meta
    lookup["__without_material__"] = lookup_no_material  # internal key for fallback
    return lookup


def _fill_bank_item_origin_fields(item: dict[str, Any], origin_lookup: dict[str, dict[str, Any]]) -> None:
    if not isinstance(item, dict):
        return
    source_task = str(item.get("source_task_id") or item.get("出题任务ID") or "").strip()
    source_task_name = str(item.get("source_task_name") or item.get("出题任务名称") or "").strip()
    source_run = str(item.get("source_run_id") or item.get("出题RunID") or "").strip()
    score = item.get("offline_judge_score")
    if score is None:
        score = item.get("离线Judge评分")
    decision = str(item.get("offline_judge_decision") or item.get("离线Judge结论") or "").strip().lower()
    quality_score = item.get("offline_judge_quality_score")
    if quality_score is None:
        quality_score = item.get("离线Judge质量分")
    baseline_score = item.get("offline_judge_baseline_score")
    if baseline_score is None:
        baseline_score = item.get("离线Judge基准分")
    quality_conclusion = str(item.get("offline_judge_quality_conclusion") or item.get("离线Judge质量分结论") or "").strip()
    baseline_conclusion = str(item.get("offline_judge_baseline_conclusion") or item.get("离线Judge基准分结论") or "").strip()
    if source_task and source_task_name and source_run and score is not None and decision:
        item["source_task_id"] = source_task
        item["source_task_name"] = source_task_name
        item["source_run_id"] = source_run
        item["offline_judge_score"] = score
        item["offline_judge_decision"] = decision
        item["offline_judge_quality_score"] = quality_score
        item["offline_judge_baseline_score"] = baseline_score
        item["offline_judge_quality_conclusion"] = quality_conclusion
        item["offline_judge_baseline_conclusion"] = baseline_conclusion
        return

    material_version_id = str(item.get("教材版本ID", "")).strip()
    path_key = _normalize_text_key(item.get("来源路径"))
    answer_key = _normalize_answer_key(item.get("正确答案"))
    stem_key = _normalize_text_key(item.get("题干"))
    key_with_material = f"{material_version_id}|{path_key}|{answer_key}|{stem_key}"
    key_without_material = f"{path_key}|{answer_key}|{stem_key}"
    fallback = origin_lookup.get("__without_material__")
    meta = origin_lookup.get(key_with_material)
    if not isinstance(meta, dict) and isinstance(fallback, dict):
        meta = fallback.get(key_without_material)
    if not isinstance(meta, dict):
        meta = {}

    item["source_task_id"] = source_task or str(meta.get("source_task_id", "")).strip()
    item["source_task_name"] = source_task_name or str(meta.get("source_task_name", "")).strip()
    item["source_run_id"] = source_run or str(meta.get("source_run_id", "")).strip()
    item["offline_judge_score"] = score if score is not None else meta.get("offline_judge_score")
    item["offline_judge_decision"] = decision or str(meta.get("offline_judge_decision", "")).strip().lower()
    item["offline_judge_quality_score"] = quality_score if quality_score is not None else meta.get("offline_judge_quality_score")
    item["offline_judge_baseline_score"] = baseline_score if baseline_score is not None else meta.get("offline_judge_baseline_score")
    item["offline_judge_quality_conclusion"] = quality_conclusion or str(meta.get("offline_judge_quality_conclusion", "")).strip()
    item["offline_judge_baseline_conclusion"] = baseline_conclusion or str(meta.get("offline_judge_baseline_conclusion", "")).strip()


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


def _append_bank_item(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(item, ensure_ascii=False)
    with BANK_WRITE_LOCK:
        with path.open("a", encoding="utf-8") as f:
            if path.exists() and path.stat().st_size > 0:
                f.write("\n")
            f.write(line)


def _build_needs_fix_bank_item(
    *,
    final_json: dict[str, Any],
    question_trace: dict[str, Any],
    attempt_error_info: dict[str, Any] | None,
    task_id: str,
    task_name: str,
    run_id: str,
) -> dict[str, Any]:
    item = deepcopy(final_json)
    critic_result = question_trace.get("critic_result") if isinstance(question_trace.get("critic_result"), dict) else {}
    issues = [str(x) for x in (critic_result.get("all_issues") or []) if str(x).strip()]
    if not issues:
        issues = [str(x) for x in (critic_result.get("quality_issues") or []) if str(x).strip()]
    if not issues:
        issues = [str(x) for x in (critic_result.get("missing_conditions") or []) if str(x).strip()]

    reason = str((attempt_error_info or {}).get("reason", "") or critic_result.get("reason", "") or "").strip()
    evidence = str((attempt_error_info or {}).get("evidence", "") or reason or "").strip()
    solution = str((attempt_error_info or {}).get("solution", "") or "").strip()
    error_key = str((attempt_error_info or {}).get("error_key", "") or "").strip()
    fail_types = [str(x) for x in ((attempt_error_info or {}).get("fail_types") or critic_result.get("fail_types") or []) if str(x).strip()]
    missing_conditions = [
        str(x)
        for x in ((attempt_error_info or {}).get("missing_conditions") or critic_result.get("missing_conditions") or [])
        if str(x).strip()
    ]
    basis_paths = [
        str(x)
        for x in ((attempt_error_info or {}).get("basis_paths") or critic_result.get("basis_paths") or [])
        if str(x).strip()
    ]

    item["出题RunID"] = run_id
    if task_id:
        item["出题任务ID"] = task_id
    if task_name:
        item["出题任务名称"] = task_name
    item["审计状态"] = "needs_fix"
    item["是否正式通过"] = False
    item["待修复"] = True
    item["待修复错误键"] = error_key
    item["待修复原因"] = reason
    item["待修复证据"] = evidence
    item["待修复建议"] = solution
    item["待修复问题"] = issues
    item["待修复失败类型"] = fail_types
    item["待修复缺失条件"] = missing_conditions
    item["待修复依据切片"] = basis_paths
    item["critic_result"] = critic_result
    item["_needs_fix_saved"] = True
    return item


def _attach_template_candidate_bank_metadata(
    *,
    final_json: dict[str, Any],
    question_trace: dict[str, Any],
    task_name: str,
    planned_slots: list[dict[str, Any]] | None,
    success_index: int,
) -> dict[str, Any]:
    item = dict(final_json)
    parent_task_name = str(task_name or "").split("#", 1)[0].strip() if str(task_name or "").strip() else ""
    target_index = int(question_trace.get("target_index", 0) or 0)
    route_prefix = ""
    mastery = ""
    if isinstance(planned_slots, list) and 0 <= int(success_index) < len(planned_slots):
        slot = planned_slots[int(success_index)] if isinstance(planned_slots[int(success_index)], dict) else {}
        route_prefix = str(slot.get("route_prefix", "") or "").strip()
        mastery = str(slot.get("mastery", "") or "").strip()
        if target_index <= 0:
            try:
                target_index = int(slot.get("_global_target_index", 0) or 0)
            except (TypeError, ValueError):
                target_index = 0
    item["模板任务"] = True
    item["模板父任务名称"] = parent_task_name
    item["模板目标位次"] = int(target_index or 0)
    item["模板路由"] = route_prefix
    item["模板掌握度"] = mastery
    item["模板正式题"] = False
    item["模板备选题"] = True
    item["模板备选原因"] = "待父任务全局模板收口"
    return item


def _reconcile_template_bank_formal_selection(
    *,
    tenant_id: str,
    parent_task_name: str,
    planned_slots: list[dict[str, Any]],
    process_trace: list[dict[str, Any]],
) -> dict[str, int]:
    name = str(parent_task_name or "").strip()
    if not name:
        return {"official_count": 0, "backup_count": 0, "updated_count": 0}
    official_traces = _collect_unique_saved_template_traces(
        planned_slots=planned_slots,
        process_trace=process_trace,
    )
    official_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in official_traces:
        if not isinstance(row, dict):
            continue
        final_json = row.get("final_json") if isinstance(row.get("final_json"), dict) else {}
        stem = str(final_json.get("题干", "") or "").strip()
        run_id = str(row.get("run_id", "") or final_json.get("出题RunID", "") or "").strip()
        slice_id = str(row.get("slice_id", "") or final_json.get("来源切片ID", "") or "").strip()
        if not stem:
            continue
        official_by_key[(run_id, stem, slice_id)] = row

    bank_path = tenant_bank_path(tenant_id)
    rows = _load_bank(bank_path)
    changed = False
    official_count = 0
    backup_count = 0
    updated_count = 0
    def _normalize_route_bucket(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    required_pair_counts: dict[tuple[str, str], int] = {}
    pair_target_indexes: dict[tuple[str, str], list[int]] = {}
    required_route_counts: dict[str, int] = {}
    required_mastery_counts: dict[str, int] = {}
    for idx, slot in enumerate(planned_slots or [], start=1):
        if not isinstance(slot, dict):
            continue
        rp = _normalize_route_bucket(slot.get("route_prefix", ""))
        ms = str(slot.get("mastery", "") or "").strip()
        if not rp or not ms:
            continue
        key = (rp, ms)
        required_pair_counts[key] = int(required_pair_counts.get(key, 0) or 0) + 1
        pair_target_indexes.setdefault(key, []).append(int(idx))
        required_route_counts[rp] = int(required_route_counts.get(rp, 0) or 0) + 1
        required_mastery_counts[ms] = int(required_mastery_counts.get(ms, 0) or 0) + 1

    def _resolve_row_template_pair(row: dict[str, Any]) -> tuple[str, str]:
        # 优先按题目真实归属重新分桶，避免复用历史收口写入的旧模板桶导致再次偏配。
        route_prefix = str(row.get("一级知识点", "") or "").strip()
        if not route_prefix:
            route_prefix = _path_prefix(row.get("来源路径", ""), 1)
        if not route_prefix:
            route_prefix = str(row.get("模板路由", "") or "").strip()
        route_prefix = _normalize_route_bucket(route_prefix)
        mastery = str(row.get("掌握程度", "") or "").strip()
        if not mastery:
            mastery = str(row.get("模板掌握度", "") or "").strip()
        return route_prefix, mastery

    eligible_row_indexes: list[int] = []
    for row_idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        row_task_name = str(row.get("出题任务名称") or row.get("task_name") or "").strip()
        if row_task_name != name and not row_task_name.startswith(f"{name}#"):
            continue
        if bool(row.get("待修复")) or str(row.get("审计状态", "") or "").strip() == "needs_fix":
            continue
        eligible_row_indexes.append(int(row_idx))
        stem = str(row.get("题干", "") or "").strip()
        run_id = str(row.get("出题RunID") or row.get("source_run_id") or row.get("run_id") or "").strip()
        slice_id = str(row.get("来源切片ID", "") or "").strip()
        official_trace = official_by_key.get((run_id, stem, slice_id))
        row["模板任务"] = True
        row["模板父任务名称"] = name
        # 服务重启/子任务降级时，部分题目可能缺失模板桶元数据。
        # 在正式题收口阶段回填模板路由/掌握度，保证配额重选可继续执行。
        route_fallback, mastery_fallback = _resolve_row_template_pair(row)
        if route_fallback and not str(row.get("模板路由", "") or "").strip():
            row["模板路由"] = route_fallback
            changed = True
        if mastery_fallback and not str(row.get("模板掌握度", "") or "").strip():
            row["模板掌握度"] = mastery_fallback
            changed = True
        if isinstance(official_trace, dict):
            target_index = int(official_trace.get("target_index", 0) or 0)
            route_prefix = ""
            mastery = ""
            if 1 <= target_index <= len(planned_slots):
                slot = planned_slots[target_index - 1] if isinstance(planned_slots[target_index - 1], dict) else {}
                route_prefix = str(slot.get("route_prefix", "") or "").strip()
                mastery = str(slot.get("mastery", "") or "").strip()
            before = (
                row.get("模板正式题"),
                row.get("模板备选题"),
                row.get("是否正式通过"),
                row.get("审计状态"),
                row.get("模板目标位次"),
                row.get("模板路由"),
                row.get("模板掌握度"),
            )
            row["模板正式题"] = True
            row["模板备选题"] = False
            row["是否正式通过"] = True
            if str(row.get("审计状态", "") or "").strip() not in {"whitelist_pass"}:
                row["审计状态"] = "passed"
            row["模板目标位次"] = target_index
            row["模板路由"] = route_prefix
            row["模板掌握度"] = mastery
            row.pop("模板备选原因", None)
            after = (
                row.get("模板正式题"),
                row.get("模板备选题"),
                row.get("是否正式通过"),
                row.get("审计状态"),
                row.get("模板目标位次"),
                row.get("模板路由"),
                row.get("模板掌握度"),
            )
            official_count += 1
            if before != after:
                changed = True
                updated_count += 1
        else:
            before = (
                row.get("模板正式题"),
                row.get("模板备选题"),
                row.get("是否正式通过"),
                row.get("审计状态"),
                row.get("模板备选原因"),
            )
            row["模板正式题"] = False
            row["模板备选题"] = True
            row["是否正式通过"] = False
            row["审计状态"] = "template_backup_pass"
            row["模板备选原因"] = "未进入模板正式题集合"
            after = (
                row.get("模板正式题"),
                row.get("模板备选题"),
                row.get("是否正式通过"),
                row.get("审计状态"),
                row.get("模板备选原因"),
            )
            backup_count += 1
            if before != after:
                changed = True
                updated_count += 1

    # Fallback reselection:
    # When target_index mapping is noisy, prefer "generate more then select best 100 by template quotas".
    if required_pair_counts and eligible_row_indexes:
        def _row_sort_key(i: int) -> tuple[str, str, str]:
            return (
                str(rows[i].get("出题RunID") or rows[i].get("source_run_id") or rows[i].get("run_id") or ""),
                str(rows[i].get("题干", "") or ""),
                str(rows[i].get("来源切片ID", "") or ""),
            )

        def _apply_selected_indexes(selected_indexes: set[int]) -> None:
            route_target_indexes: dict[str, list[int]] = {}
            for pos, slot in enumerate(planned_slots or [], start=1):
                if not isinstance(slot, dict):
                    continue
                rp = _normalize_route_bucket(slot.get("route_prefix", ""))
                if rp:
                    route_target_indexes.setdefault(rp, []).append(int(pos))
            selected_by_route: dict[str, list[int]] = {}
            for idx in selected_indexes:
                route_prefix, _ = _resolve_row_template_pair(rows[idx])
                selected_by_route.setdefault(route_prefix, []).append(idx)
            for route_prefix, idxs in selected_by_route.items():
                selected_by_route[route_prefix] = sorted(idxs, key=_row_sort_key, reverse=True)

            for idx in eligible_row_indexes:
                row = rows[idx]
                route_prefix, mastery = _resolve_row_template_pair(row)
                row["模板任务"] = True
                row["模板父任务名称"] = name
                if route_prefix:
                    row["模板路由"] = route_prefix
                if mastery:
                    row["模板掌握度"] = mastery
                if idx in selected_indexes:
                    targets = route_target_indexes.get(route_prefix, [])
                    selected_list = selected_by_route.get(route_prefix, [])
                    if idx in selected_list:
                        pos = selected_list.index(idx)
                        if 0 <= pos < len(targets):
                            row["模板目标位次"] = int(targets[pos])
                    row["模板正式题"] = True
                    row["模板备选题"] = False
                    row["是否正式通过"] = True
                    if str(row.get("审计状态", "") or "").strip() not in {"whitelist_pass"}:
                        row["审计状态"] = "passed"
                    row.pop("模板备选原因", None)
                else:
                    row["模板正式题"] = False
                    row["模板备选题"] = True
                    row["是否正式通过"] = False
                    row["审计状态"] = "template_backup_pass"
                    row["模板备选原因"] = "未进入模板正式题集合"

        current_pair_counts: dict[tuple[str, str], int] = {}
        for idx in eligible_row_indexes:
            row = rows[idx]
            if not bool(row.get("模板正式题")):
                continue
            pair = _resolve_row_template_pair(row)
            if not pair[0] or not pair[1]:
                continue
            current_pair_counts[pair] = int(current_pair_counts.get(pair, 0) or 0) + 1

        if current_pair_counts != required_pair_counts:
            pair_to_candidates: dict[tuple[str, str], list[int]] = {}
            for idx in eligible_row_indexes:
                row = rows[idx]
                pair = _resolve_row_template_pair(row)
                if not pair[0] or not pair[1]:
                    continue
                pair_to_candidates.setdefault(pair, []).append(idx)

            feasible = all(len(pair_to_candidates.get(pair, [])) >= need for pair, need in required_pair_counts.items())
            if feasible:
                selected_indexes: set[int] = set()
                for pair, need in required_pair_counts.items():
                    cands = pair_to_candidates.get(pair, [])
                    cands_sorted = sorted(cands, key=_row_sort_key, reverse=True)
                    selected_indexes.update(cands_sorted[: int(need)])
                _apply_selected_indexes(selected_indexes)
                changed = True
            else:
                # Relaxed fallback:
                # Keep route counts hard, keep global mastery counts hard; do not require per-route mastery ratio.
                route_keys = [k for k in required_route_counts.keys() if str(k).strip()]
                mastery_keys = [k for k in required_mastery_counts.keys() if str(k).strip()]
                avail: dict[tuple[str, str], int] = {
                    (r, m): 0 for r in route_keys for m in mastery_keys
                }
                candidates_by_cell: dict[tuple[str, str], list[int]] = {
                    (r, m): [] for r in route_keys for m in mastery_keys
                }
                for idx in eligible_row_indexes:
                    route_prefix, mastery = _resolve_row_template_pair(rows[idx])
                    if route_prefix not in required_route_counts or mastery not in required_mastery_counts:
                        continue
                    key = (route_prefix, mastery)
                    avail[key] = int(avail.get(key, 0) or 0) + 1
                    candidates_by_cell.setdefault(key, []).append(idx)

                for key, cands in candidates_by_cell.items():
                    candidates_by_cell[key] = sorted(cands, key=_row_sort_key, reverse=True)

                route_feasible = all(
                    sum(avail.get((r, m), 0) for m in mastery_keys) >= int(required_route_counts.get(r, 0) or 0)
                    for r in route_keys
                )
                mastery_feasible = all(
                    sum(avail.get((r, m), 0) for r in route_keys) >= int(required_mastery_counts.get(m, 0) or 0)
                    for m in mastery_keys
                )
                total_required = sum(int(v or 0) for v in required_route_counts.values())
                total_available = sum(int(v or 0) for v in avail.values())
                relaxed_allocation: dict[tuple[str, str], int] | None = None

                if route_feasible and mastery_feasible and total_available >= total_required:
                    route_needs = {r: int(required_route_counts.get(r, 0) or 0) for r in route_keys}
                    mastery_needs = {m: int(required_mastery_counts.get(m, 0) or 0) for m in mastery_keys}
                    remaining_route_order = list(route_keys)

                    def _dfs_route_allocate(
                        route_pos: int,
                        current_mastery_needs: dict[str, int],
                        alloc: dict[tuple[str, str], int],
                    ) -> dict[tuple[str, str], int] | None:
                        if route_pos >= len(remaining_route_order):
                            if all(int(v or 0) == 0 for v in current_mastery_needs.values()):
                                return dict(alloc)
                            return None
                        route = remaining_route_order[route_pos]
                        need = int(route_needs.get(route, 0) or 0)
                        if need <= 0:
                            return _dfs_route_allocate(route_pos + 1, current_mastery_needs, alloc)

                        m0 = mastery_keys[0] if mastery_keys else ""
                        m1 = mastery_keys[1] if len(mastery_keys) > 1 else ""
                        m2 = mastery_keys[2] if len(mastery_keys) > 2 else ""
                        max0 = min(
                            need,
                            int(avail.get((route, m0), 0) or 0),
                            int(current_mastery_needs.get(m0, 0) or 0),
                        ) if m0 else 0
                        for x0 in range(max0, -1, -1):
                            need_after_0 = need - x0
                            max1 = min(
                                need_after_0,
                                int(avail.get((route, m1), 0) or 0),
                                int(current_mastery_needs.get(m1, 0) or 0),
                            ) if m1 else 0
                            for x1 in range(max1, -1, -1):
                                x2 = need_after_0 - x1
                                if m2:
                                    if x2 < 0:
                                        continue
                                    if x2 > int(avail.get((route, m2), 0) or 0):
                                        continue
                                    if x2 > int(current_mastery_needs.get(m2, 0) or 0):
                                        continue
                                elif x2 != 0:
                                    continue

                                next_needs = dict(current_mastery_needs)
                                if m0:
                                    next_needs[m0] = int(next_needs.get(m0, 0) or 0) - int(x0)
                                if m1:
                                    next_needs[m1] = int(next_needs.get(m1, 0) or 0) - int(x1)
                                if m2:
                                    next_needs[m2] = int(next_needs.get(m2, 0) or 0) - int(x2)
                                if any(int(v or 0) < 0 for v in next_needs.values()):
                                    continue

                                # Prune by future capacity.
                                future_routes = remaining_route_order[route_pos + 1 :]
                                cap_ok = True
                                for m in mastery_keys:
                                    future_cap = sum(int(avail.get((fr, m), 0) or 0) for fr in future_routes)
                                    if int(next_needs.get(m, 0) or 0) > future_cap:
                                        cap_ok = False
                                        break
                                if not cap_ok:
                                    continue

                                if m0:
                                    alloc[(route, m0)] = int(x0)
                                if m1:
                                    alloc[(route, m1)] = int(x1)
                                if m2:
                                    alloc[(route, m2)] = int(x2)
                                found = _dfs_route_allocate(route_pos + 1, next_needs, alloc)
                                if found is not None:
                                    return found
                        return None

                    relaxed_allocation = _dfs_route_allocate(0, mastery_needs, {})

                if isinstance(relaxed_allocation, dict):
                    selected_indexes: set[int] = set()
                    for route_prefix in route_keys:
                        for mastery in mastery_keys:
                            need = int(relaxed_allocation.get((route_prefix, mastery), 0) or 0)
                            if need <= 0:
                                continue
                            cands = candidates_by_cell.get((route_prefix, mastery), [])
                            selected_indexes.update(cands[:need])
                    if len(selected_indexes) == total_required:
                        _apply_selected_indexes(selected_indexes)
                        changed = True

    # Re-count after potential fallback reselection.
    official_count = 0
    backup_count = 0
    for idx in eligible_row_indexes:
        row = rows[idx]
        if bool(row.get("模板正式题")):
            official_count += 1
        elif bool(row.get("模板备选题")):
            backup_count += 1
    if changed:
        _save_bank(bank_path, rows)
    return {
        "official_count": official_count,
        "backup_count": backup_count,
        "updated_count": updated_count,
    }


def _maybe_reconcile_template_task_selection(
    tenant_id: str,
    task: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(task, dict):
        return task
    req = task.get("request") if isinstance(task.get("request"), dict) else {}
    template_id = str(req.get("template_id", "") or "").strip()
    if not template_id:
        return task
    task_name = str(task.get("task_name", "") or req.get("task_name", "") or "").strip()
    if not task_name:
        return task
    process_trace = [x for x in (task.get("process_trace") or []) if isinstance(x, dict)]
    if not process_trace:
        return task
    ctx, err = _resolve_template_parallel_context(
        tenant_id,
        {
            "template_id": template_id,
            "material_version_id": str(req.get("material_version_id", "") or task.get("material_version_id", "") or "").strip(),
            "question_type": str(req.get("question_type", "随机") or "随机"),
            "num_questions": int(req.get("num_questions", 0) or 0),
            "slice_ids": [int(x) for x in (req.get("slice_ids") or []) if str(x).isdigit()],
        },
    )
    if err or not isinstance(ctx, dict):
        return task
    planned_slots = [slot for slot in (ctx.get("planned_slots") or []) if isinstance(slot, dict)]
    if not planned_slots:
        return task
    selection_stats = _reconcile_template_bank_formal_selection(
        tenant_id=tenant_id,
        parent_task_name=task_name.split("#", 1)[0].strip(),
        planned_slots=planned_slots,
        process_trace=process_trace,
    )
    if not selection_stats:
        return task
    patched = dict(task)
    patched["template_selection"] = selection_stats
    patched["backup_count"] = int(selection_stats.get("backup_count", 0) or 0)
    return patched


def _persist_needs_fix_bank_item(
    *,
    path: Path,
    final_json: dict[str, Any] | None,
    question_trace: dict[str, Any],
    attempt_error_info: dict[str, Any] | None,
    task_id: str,
    task_name: str,
    run_id: str,
) -> tuple[bool, dict[str, Any] | None, str]:
    if not isinstance(final_json, dict):
        return False, None, "未产出 final_json，无法保存待修复题"
    item = _build_needs_fix_bank_item(
        final_json=final_json,
        question_trace=question_trace,
        attempt_error_info=attempt_error_info,
        task_id=task_id,
        task_name=task_name,
        run_id=run_id,
    )
    _append_bank_item(path, item)
    return True, item, ""


def _persist_template_gap_failed_item(
    *,
    enabled: bool,
    path: Path,
    final_json: dict[str, Any] | None,
    question_trace: dict[str, Any],
    attempt_error_info: dict[str, Any] | None,
    task_id: str,
    task_name: str,
    run_id: str,
) -> tuple[bool, dict[str, Any] | None, str]:
    if not enabled:
        return False, None, ""
    return _persist_needs_fix_bank_item(
        path=path,
        final_json=final_json,
        question_trace=question_trace,
        attempt_error_info=attempt_error_info,
        task_id=task_id,
        task_name=task_name,
        run_id=run_id,
    )


def _build_template_missing_slot_placeholder(
    *,
    target_index: int,
    planned_slot: dict[str, Any],
    question_type: str,
    failure_reason: str,
) -> dict[str, Any]:
    """Build a minimal bank item for template slot fallback."""
    route_prefix = str((planned_slot or {}).get("route_prefix", "") or "").strip()
    mastery = str((planned_slot or {}).get("mastery", "") or "").strip()
    slice_id = int((planned_slot or {}).get("slice_id", 0) or 0)
    title_parts = [f"模板位次{int(target_index)}"]
    if route_prefix:
        title_parts.append(route_prefix)
    if mastery:
        title_parts.append(mastery)
    title = " | ".join(title_parts)
    reason_text = str(failure_reason or "达到任务熔断或无可用切片，自动补位为待修复题").strip()
    return {
        "题目类型": str(question_type or "单选题"),
        "题干": f"【待修复】{title} 生成失败",
        "选项": [],
        "答案": "",
        "解析": f"该位次自动出题失败，请老师在题库中单题修复后替换。失败原因：{reason_text}",
        "来源切片ID": slice_id,
        "模板失败补位": True,
        "模板失败部位": {
            "target_index": int(target_index),
            "route_prefix": route_prefix,
            "mastery": mastery,
        },
    }


def _persist_template_remaining_failed_slots(
    *,
    enabled: bool,
    bank_path: Path,
    planned_slots: list[dict[str, Any]] | None,
    process_trace: list[dict[str, Any]],
    generated: list[dict[str, Any]],
    saved_count: int,
    task_id: str,
    task_name: str,
    run_id: str,
    question_type: str,
    failure_reason: str,
) -> tuple[int, list[str]]:
    """Persist unresolved template slots as needs_fix items for later manual repair."""
    if not enabled:
        return int(saved_count or 0), []
    slots = [slot for slot in (planned_slots or []) if isinstance(slot, dict)]
    if not slots:
        return int(saved_count or 0), []
    target_to_trace: dict[int, dict[str, Any]] = {}
    saved_targets: set[int] = set()
    for row in (process_trace or []):
        if not isinstance(row, dict):
            continue
        try:
            target_idx = int(row.get("target_index", 0) or 0)
        except (TypeError, ValueError):
            continue
        if target_idx <= 0 or target_idx > len(slots):
            continue
        prev = target_to_trace.get(target_idx)
        if not isinstance(prev, dict) or int(row.get("index", 0) or 0) >= int(prev.get("index", 0) or 0):
            target_to_trace[target_idx] = row
        if bool(row.get("saved")) and isinstance(row.get("final_json"), dict):
            saved_targets.add(target_idx)
    new_saved_count = int(saved_count or 0)
    helper_errors: list[str] = []
    for target_idx in range(1, len(slots) + 1):
        if target_idx in saved_targets:
            continue
        slot = slots[target_idx - 1] if isinstance(slots[target_idx - 1], dict) else {}
        trace = target_to_trace.get(target_idx)
        trace_row = dict(trace) if isinstance(trace, dict) else {
            "run_id": run_id,
            "index": len(process_trace) + 1,
            "target_index": target_idx,
            "slice_id": int(slot.get("slice_id", 0) or 0),
            "slice_path": "",
            "slice_content": "",
            "question_type": str(question_type or ""),
            "steps": [],
            "critic_result": {},
            "snapshot_stage": "final",
            "saved": False,
        }
        fallback_reason = str(failure_reason or "").strip()
        if not fallback_reason:
            fallback_reason = str(
                ((trace_row.get("critic_result") if isinstance(trace_row.get("critic_result"), dict) else {}).get("reason", "")) or ""
            ).strip()
        if not fallback_reason:
            fallback_reason = "达到任务熔断或无可用切片"
        final_json_raw = trace_row.get("final_json") if isinstance(trace_row.get("final_json"), dict) else {}
        if isinstance(final_json_raw, dict) and final_json_raw:
            base_final_json = deepcopy(final_json_raw)
        else:
            base_final_json = _build_template_missing_slot_placeholder(
                target_index=target_idx,
                planned_slot=slot,
                question_type=str(trace_row.get("question_type", "") or question_type or "单选题"),
                failure_reason=fallback_reason,
            )
        try:
            base_final_json = _attach_template_candidate_bank_metadata(
                final_json=base_final_json,
                question_trace={"target_index": target_idx},
                task_name=task_name,
                planned_slots=slots,
                success_index=target_idx - 1,
            )
        except Exception:
            pass
        attempt_error_info = {
            "error_key": "template:slot_unfilled",
            "category": "template_slot_unfilled",
            "reason": fallback_reason,
            "evidence": fallback_reason,
            "fail_types": [
                str(x)
                for x in (
                    (trace_row.get("critic_result") if isinstance(trace_row.get("critic_result"), dict) else {}).get("fail_types")
                    or []
                )
                if str(x).strip()
            ],
            "missing_conditions": [
                str(x)
                for x in (
                    (trace_row.get("critic_result") if isinstance(trace_row.get("critic_result"), dict) else {}).get("missing_conditions")
                    or []
                )
                if str(x).strip()
            ],
            "basis_paths": [
                str(x)
                for x in (
                    (trace_row.get("critic_result") if isinstance(trace_row.get("critic_result"), dict) else {}).get("basis_paths")
                    or []
                )
                if str(x).strip()
            ],
            "solution": "请在题库中按模板位次进行单题修复并替换该题。",
        }
        persisted, saved_item, save_err = _persist_template_gap_failed_item(
            enabled=True,
            path=bank_path,
            final_json=base_final_json,
            question_trace=trace_row,
            attempt_error_info=attempt_error_info,
            task_id=task_id,
            task_name=task_name,
            run_id=run_id,
        )
        if not persisted or not isinstance(saved_item, dict):
            if save_err:
                helper_errors.append(f"模板位次{target_idx}待修复题落库失败: {save_err}")
            continue
        trace_row["final_json"] = deepcopy(saved_item)
        trace_row["saved"] = True
        trace_row["saved_with_issues"] = True
        trace_row["template_gap_final_failure"] = True
        trace_row["snapshot_stage"] = "final"
        trace_row["target_index"] = target_idx
        if not isinstance(trace_row.get("critic_result"), dict):
            trace_row["critic_result"] = {"passed": False, "reason": fallback_reason}
        generated.append(saved_item)
        new_saved_count += 1
        saved_targets.add(target_idx)
        helper_errors.append(
            "模板失败部位已入库: 第{idx}题 ({route}|{mastery})".format(
                idx=target_idx,
                route=str(slot.get("route_prefix", "") or "").strip() or "-",
                mastery=str(slot.get("mastery", "") or "").strip() or "-",
            )
        )
        if isinstance(trace, dict):
            trace.update(trace_row)
        else:
            process_trace.append(trace_row)
    return new_saved_count, helper_errors


def _qa_dir(tenant_id: str) -> Path:
    path = tenant_root(tenant_id) / "audit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _qa_runs_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "qa_runs.jsonl"


def _qa_alerts_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "qa_alerts.jsonl"


def _qa_traces_path(tenant_id: str) -> Path:
    """Per-tenant per-question offline Judge traces (JSON Lines)."""
    return _qa_dir(tenant_id) / "qa_traces.jsonl"


def _qa_thresholds_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "qa_thresholds.json"


def _judge_log_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "judge.log"


def _append_judge_log(tenant_id: str, event: str, detail: str | dict[str, Any] | None = None) -> None:
    """Append one line to tenant audit judge.log for offline Judge debugging."""
    try:
        path = _judge_log_path(tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(detail, dict):
            detail_str = json.dumps(detail, ensure_ascii=False)
        else:
            detail_str = str(detail or "")
        line = f"{ts}\t{event}\t{detail_str}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _append_qa_trace(tenant_id: str, row: dict[str, Any]) -> None:
    """
    Append one JSON line to qa_traces.jsonl for offline Judge debugging.

    This is intentionally best-effort and must never break the main flow.
    """
    try:
        payload = {k: v for k, v in (row or {}).items() if v is not None}
        if not payload:
            return
        _append_jsonl(_qa_traces_path(tenant_id), payload)
    except Exception:
        # Trace logging failures must not affect Judge execution.
        pass


def _qa_pricing_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "qa_pricing.json"


def _qa_config_path(tenant_id: str) -> Path:
    """Per-tenant QA config (e.g. baseline run for release comparison)."""
    return _qa_dir(tenant_id) / "qa_config.json"


def _qa_releases_path(tenant_id: str) -> Path:
    """Per-tenant manual release records (version number, release notes, run_id)."""
    return _qa_dir(tenant_id) / "qa_releases.jsonl"


def _qa_gen_tasks_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "gen_tasks.jsonl"


def _qa_gen_tasks_summary_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "gen_tasks_summary.jsonl"


def _qa_read_path(tenant_id: str, filename: str) -> Path:
    preferred = _qa_dir(tenant_id) / filename
    if preferred.exists():
        return preferred
    legacy = repo_tenant_data_dir(tenant_id) / "audit" / filename
    if legacy.exists():
        return legacy
    return preferred


def _qa_read_paths(tenant_id: str, filename: str) -> list[Path]:
    preferred = _qa_dir(tenant_id) / filename
    legacy = repo_tenant_data_dir(tenant_id) / "audit" / filename
    paths: list[Path] = []
    for candidate in (preferred, legacy):
        if candidate in paths:
            continue
        if candidate.exists():
            paths.append(candidate)
    if not paths:
        paths.append(preferred)
    return paths


def _qa_gen_task_snapshot_dir(tenant_id: str) -> Path:
    path = _qa_dir(tenant_id) / "gen_task_snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _qa_gen_task_snapshot_path(tenant_id: str, task_id: str) -> Path:
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task_id or "").strip()) or "unknown"
    return _qa_gen_task_snapshot_dir(tenant_id) / f"{safe_task_id}.json"


def _qa_judge_tasks_path(tenant_id: str) -> Path:
    return _qa_dir(tenant_id) / "judge_tasks.jsonl"


GEN_TASKS: dict[str, dict[str, Any]] = {}
GEN_TASK_LOCK = threading.Lock()
GEN_TASK_KEEP = 200
JUDGE_TASKS: dict[str, dict[str, Any]] = {}
JUDGE_TASK_LOCK = threading.Lock()
JUDGE_TASK_KEEP = 200
QA_PERSIST_LOCK = threading.Lock()
BANK_WRITE_LOCK = threading.Lock()
MAPPING_JOBS: dict[str, dict[str, Any]] = {}
MAPPING_JOB_LOCK = threading.Lock()
RETRIEVER_CACHE: dict[tuple[str, str, str, str, str], KnowledgeRetriever] = {}
RETRIEVER_CACHE_LOCK = threading.Lock()
RETRIEVER_CACHE_INFLIGHT: dict[tuple[str, str, str, str, str], threading.Event] = {}
RETRIEVER_CACHE_ERRORS: dict[tuple[str, str, str, str, str], Exception] = {}
GEN_TASK_NAME_INFLIGHT: set[tuple[str, str]] = set()


_REPLACEMENT_FOR_TIMEOUT_MSG = "任务执行失败（当前版本已取消任务执行时间限制；该错误可能来自历史任务记录）"


def _sanitize_task_errors(errors: list[str] | None) -> list[str]:
    """Replace legacy task-timeout error messages; task-level timeout has been removed."""
    if not isinstance(errors, list):
        return []
    out: list[str] = []
    seen_timeout_replacement = False
    for e in errors:
        s = str(e).strip()
        if "任务执行超时" in s or "task execution timeout" in s.lower():
            if not seen_timeout_replacement:
                out.append(_REPLACEMENT_FOR_TIMEOUT_MSG)
                seen_timeout_replacement = True
        else:
            out.append(s)
    return out


_ORPHAN_GEN_TASK_MSG = "任务在服务重启后未恢复，已自动标记失败，请重新发起出题任务。"
_ORPHAN_JUDGE_TASK_MSG = "Judge 任务在服务重启后未恢复，已自动标记失败，请重新发起 Judge 任务。"
_ORPHAN_JUDGE_TASK_RECOVERED_MSG = "Judge 任务在服务重启后已自动恢复，将从断点继续执行。"
_ORPHAN_GEN_GRACE_SECONDS = max(300, int(os.getenv("ORPHAN_GEN_GRACE_SECONDS", "7200") or 7200))
_ORPHAN_GEN_ZERO_PROGRESS_SECONDS = max(60, int(os.getenv("ORPHAN_GEN_ZERO_PROGRESS_SECONDS", "900") or 900))
_ORPHAN_GEN_UNOWNED_RUNNING_SECONDS = max(
    120, int(os.getenv("ORPHAN_GEN_UNOWNED_RUNNING_SECONDS", "300") or 300)
)
_ORPHAN_JUDGE_GRACE_SECONDS = max(120, int(os.getenv("ORPHAN_JUDGE_GRACE_SECONDS", "1800") or 1800))
_TASK_MAINTENANCE_INTERVAL_SECONDS = max(30, int(os.getenv("TASK_MAINTENANCE_INTERVAL_SECONDS", "120") or 120))
_RETRIEVER_CACHE_WAIT_SECONDS = max(5, int(os.getenv("RETRIEVER_CACHE_WAIT_SECONDS", "180") or 180))
_PARALLEL_CHILD_WAIT_SECONDS = max(10, int(os.getenv("PARALLEL_CHILD_WAIT_SECONDS", "1800") or 1800))
_PARALLEL_CHILD_TIMEOUT_MIN_SECONDS = max(10, int(os.getenv("PARALLEL_CHILD_TIMEOUT_MIN_SECONDS", "180") or 180))
_PARALLEL_CHILD_TIMEOUT_MAX_SECONDS = max(
    _PARALLEL_CHILD_TIMEOUT_MIN_SECONDS,
    int(os.getenv("PARALLEL_CHILD_TIMEOUT_MAX_SECONDS", "10800") or 10800),
)
_PARALLEL_CHILD_TIMEOUT_BASE_SECONDS = max(0, int(os.getenv("PARALLEL_CHILD_TIMEOUT_BASE_SECONDS", "120") or 120))
_PARALLEL_CHILD_TIMEOUT_PER_Q_BASE_SECONDS = max(
    1, int(os.getenv("PARALLEL_CHILD_TIMEOUT_PER_Q_BASE_SECONDS", "40") or 40)
)
_PARALLEL_CHILD_TIMEOUT_PER_Q_RETRY_SECONDS = max(
    0, int(os.getenv("PARALLEL_CHILD_TIMEOUT_PER_Q_RETRY_SECONDS", "20") or 20)
)
_PARALLEL_CHILD_TIMEOUT_RETRY_CAP = max(
    1, int(os.getenv("PARALLEL_CHILD_TIMEOUT_RETRY_CAP", "12") or 12)
)
_PARALLEL_CHILD_TIMEOUT_SAFETY_MULTIPLIER = max(
    1.0, float(os.getenv("PARALLEL_CHILD_TIMEOUT_SAFETY_MULTIPLIER", "1.35") or 1.35)
)
_PARALLEL_BATCH_TIMEOUT_MAX_SECONDS = max(60, int(os.getenv("PARALLEL_BATCH_TIMEOUT_MAX_SECONDS", "43200") or 43200))
_PARALLEL_BATCH_TIMEOUT_BUFFER_BASE_SECONDS = max(
    0, int(os.getenv("PARALLEL_BATCH_TIMEOUT_BUFFER_BASE_SECONDS", "90") or 90)
)
_PARALLEL_BATCH_TIMEOUT_BUFFER_PER_SUBTASK_SECONDS = max(
    0, int(os.getenv("PARALLEL_BATCH_TIMEOUT_BUFFER_PER_SUBTASK_SECONDS", "20") or 20)
)
_MAINTENANCE_STARTED = False
_MAINTENANCE_LOCK = threading.Lock()


def _clamp_int(value: int, low: int, high: int) -> int:
    if low > high:
        low, high = high, low
    return max(low, min(high, int(value)))


def _estimate_internal_subtask_max_attempts(question_count: int) -> int:
    q = max(1, int(question_count or 1))
    return min(400, max(1, q * 6 + 16))


def _estimate_parallel_child_timeout_seconds(question_count: int, max_attempts: int | None = None) -> int:
    q = max(1, int(question_count or 1))
    attempts = int(max_attempts or 0)
    if attempts <= 0:
        attempts = _estimate_internal_subtask_max_attempts(q)
    attempts_for_timeout = min(max(1, attempts), _PARALLEL_CHILD_TIMEOUT_RETRY_CAP)
    per_question = _PARALLEL_CHILD_TIMEOUT_PER_Q_BASE_SECONDS + _PARALLEL_CHILD_TIMEOUT_PER_Q_RETRY_SECONDS * max(
        0, attempts_for_timeout - 1
    )
    timeout_seconds = int(
        (_PARALLEL_CHILD_TIMEOUT_BASE_SECONDS + q * per_question) * _PARALLEL_CHILD_TIMEOUT_SAFETY_MULTIPLIER
    )
    return _clamp_int(timeout_seconds, _PARALLEL_CHILD_TIMEOUT_MIN_SECONDS, _PARALLEL_CHILD_TIMEOUT_MAX_SECONDS)


def _estimate_parallel_batch_timeout_seconds(child_timeouts: list[int], subtask_count: int) -> int:
    max_child = max([int(x) for x in child_timeouts if int(x) > 0], default=_PARALLEL_CHILD_TIMEOUT_MIN_SECONDS)
    # Parent parallel timeout must be 4x the max child timeout.
    timeout_seconds = int(max_child) * 4
    timeout_seconds = min(timeout_seconds, _PARALLEL_BATCH_TIMEOUT_MAX_SECONDS)
    # Keep backward compatibility: honor legacy fixed timeout as a floor if configured larger.
    return max(int(_PARALLEL_CHILD_WAIT_SECONDS), int(timeout_seconds))


def _is_orphan_reconcile_due(task: dict[str, Any], now: datetime, grace_seconds: int) -> bool:
    """Only mark as orphan-failed when task has been stale long enough."""
    if not isinstance(task, dict):
        return True
    last_seen: datetime | None = None
    for key in ("updated_at", "started_at", "created_at"):
        dt = _parse_iso_ts(str(task.get(key, "") or ""))
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if last_seen is None or dt > last_seen:
            last_seen = dt
    if last_seen is None:
        # Missing timestamps: keep previous strict behavior.
        return True
    # Fast-fail for generate tasks stuck at running+zero-progress for too long.
    # This avoids long-lived ghost tasks after worker crash/restart.
    try:
        progress = task.get("progress") if isinstance(task.get("progress"), dict) else {}
        current = int(progress.get("current", 0) or 0)
        total = int(progress.get("total", 0) or 0)
        status = str(task.get("status", "") or "").strip().lower()
        if status == "running" and total > 0 and current <= 0:
            if (now - last_seen).total_seconds() >= float(_ORPHAN_GEN_ZERO_PROGRESS_SECONDS):
                return True
    except Exception:
        pass
    return (now - last_seen).total_seconds() >= float(max(1, int(grace_seconds or 1)))


def _is_unowned_running_task_stale(task: dict[str, Any], now: datetime, stale_seconds: int) -> bool:
    """Fast stale detection for running tasks without any in-process worker ownership."""
    if not isinstance(task, dict):
        return True
    status = str(task.get("status", "") or "").strip().lower()
    if status != "running":
        return False
    last_seen: datetime | None = None
    for key in ("updated_at", "started_at", "created_at"):
        dt = _parse_iso_ts(str(task.get(key, "") or ""))
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if last_seen is None or dt > last_seen:
            last_seen = dt
    if last_seen is None:
        return True
    return (now - last_seen).total_seconds() >= float(max(1, int(stale_seconds or 1)))


def _latest_rows_by_task_id(
    path: Path,
    *,
    max_bytes: int = 0,
    stop_after: int = 0,
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    source_rows = _read_jsonl_tail(path, max_bytes) if max_bytes > 0 else _read_jsonl(path)
    for row in reversed(source_rows):
        if not isinstance(row, dict):
            continue
        tid = str(row.get("task_id", "")).strip()
        if tid and tid not in rows:
            rows[tid] = row
            if stop_after > 0 and len(rows) >= stop_after:
                break
    return rows


def _latest_rows_by_task_id_from_paths(
    paths: list[Path],
    *,
    max_bytes: int = 0,
    stop_after: int = 0,
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in paths:
        remaining = max(0, int(stop_after) - len(rows)) if stop_after > 0 else 0
        next_rows = _latest_rows_by_task_id(
            path,
            max_bytes=max_bytes,
            stop_after=remaining,
        )
        for tid, row in next_rows.items():
            if tid not in rows:
                rows[tid] = row
                if stop_after > 0 and len(rows) >= stop_after:
                    return rows
    return rows


_GEN_TASK_FULL_FALLBACK_MAX_BYTES = max(
    0, int(os.getenv("GEN_TASK_FULL_FALLBACK_MAX_BYTES", str(8 * 1024 * 1024)) or 0)
)
_GEN_TASK_SUMMARY_TAIL_SYNC_BYTES = max(
    0, int(os.getenv("GEN_TASK_SUMMARY_TAIL_SYNC_BYTES", str(16 * 1024 * 1024)) or 0)
)
_GEN_TASK_SUMMARY_READ_MAX_BYTES = max(
    0, int(os.getenv("GEN_TASK_SUMMARY_READ_MAX_BYTES", str(4 * 1024 * 1024)) or 0)
)
_RECENT_JSONL_FULL_READ_MAX_BYTES = max(
    0, int(os.getenv("RECENT_JSONL_FULL_READ_MAX_BYTES", str(4 * 1024 * 1024)) or 0)
)
_RECENT_JSONL_INITIAL_BYTES = max(
    0, int(os.getenv("RECENT_JSONL_INITIAL_BYTES", str(2 * 1024 * 1024)) or 0)
)
_RECENT_JSONL_MAX_BYTES = max(
    _RECENT_JSONL_INITIAL_BYTES,
    int(os.getenv("RECENT_JSONL_MAX_BYTES", str(64 * 1024 * 1024)) or 0),
)


def _allow_gen_tasks_full_fallback(tenant_id: str) -> bool:
    for path in _qa_read_paths(tenant_id, "gen_tasks.jsonl"):
        try:
            if path.exists() and path.stat().st_size <= _GEN_TASK_FULL_FALLBACK_MAX_BYTES:
                return True
        except Exception:
            continue
    return False


def _read_jsonl_tail(path: Path, max_bytes: int) -> list[dict[str, Any]]:
    if max_bytes <= 0 or not path.exists():
        return []
    try:
        file_size = path.stat().st_size
        start = max(0, file_size - max_bytes)
        with path.open("rb") as f:
            if start > 0:
                f.seek(start)
                f.readline()
            else:
                f.seek(0)
            raw = f.read()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _paths_total_size(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        try:
            if path.exists():
                total += int(path.stat().st_size)
        except Exception:
            continue
    return total


def _collect_recent_jsonl_rows_from_paths(
    paths: list[Path],
    *,
    target_count: int,
    sort_key: Callable[[dict[str, Any]], str],
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    unique_key: Callable[[dict[str, Any]], str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    existing_paths = [path for path in paths if path.exists()]
    if not existing_paths:
        return [], True

    def _finalize(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = [row for row in source_rows if isinstance(row, dict)]
        if predicate is not None:
            rows = [row for row in rows if predicate(row)]
        rows.sort(key=sort_key, reverse=True)
        if unique_key is None:
            return rows
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            key = str(unique_key(row) or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    total_size = _paths_total_size(existing_paths)
    if total_size <= _RECENT_JSONL_FULL_READ_MAX_BYTES:
        source_rows: list[dict[str, Any]] = []
        for path in existing_paths:
            source_rows.extend(_read_jsonl(path))
        return _finalize(source_rows), True

    target = max(1, int(target_count or 1))
    read_bytes = max(1, _RECENT_JSONL_INITIAL_BYTES)
    while True:
        source_rows = []
        exhausted = True
        for path in existing_paths:
            try:
                file_size = int(path.stat().st_size)
            except Exception:
                file_size = 0
            if file_size > read_bytes:
                exhausted = False
            source_rows.extend(_read_jsonl_tail(path, min(read_bytes, file_size)))
        rows = _finalize(source_rows)
        if len(rows) >= target or exhausted or read_bytes >= _RECENT_JSONL_MAX_BYTES:
            return rows, exhausted or read_bytes >= _RECENT_JSONL_MAX_BYTES
        read_bytes = min(_RECENT_JSONL_MAX_BYTES, read_bytes * 2)


def _refresh_gen_task_summary_from_tail(tenant_id: str) -> None:
    full_path = _qa_read_path(tenant_id, "gen_tasks.jsonl")
    summary_path = _qa_read_path(tenant_id, "gen_tasks_summary.jsonl")
    if not full_path.exists() or _GEN_TASK_SUMMARY_TAIL_SYNC_BYTES <= 0:
        return
    try:
        full_mtime = full_path.stat().st_mtime
        summary_mtime = summary_path.stat().st_mtime if summary_path.exists() else 0.0
        if summary_mtime >= full_mtime:
            return
    except Exception:
        return

    existing = _latest_rows_by_task_id(summary_path)
    tail_rows = _read_jsonl_tail(full_path, _GEN_TASK_SUMMARY_TAIL_SYNC_BYTES)
    pending: list[dict[str, Any]] = []
    for row in tail_rows:
        tid = str(row.get("task_id", "")).strip()
        if not tid:
            continue
        summary = _build_gen_task_summary(row)
        prev = existing.get(tid)
        prev_ts = ""
        if isinstance(prev, dict):
            prev_ts = str(prev.get("updated_at", "") or prev.get("created_at", "") or "")
        curr_ts = str(summary.get("updated_at", "") or summary.get("created_at", "") or "")
        if not prev or curr_ts >= prev_ts:
            pending.append(summary)
            existing[tid] = summary
    for row in pending:
        _append_jsonl(summary_path, row)


def _latest_gen_task_rows(
    tenant_id: str,
    *,
    allow_full_fallback: bool = False,
    refresh_summary: bool = True,
    prefer_compact_summary: bool = False,
    stop_after: int = 0,
) -> dict[str, dict[str, Any]]:
    if refresh_summary:
        _refresh_gen_task_summary_from_tail(tenant_id)
    summary_rows = _latest_rows_by_task_id_from_paths(
        _qa_read_paths(tenant_id, "gen_tasks_summary.jsonl"),
        max_bytes=_GEN_TASK_SUMMARY_READ_MAX_BYTES if prefer_compact_summary else 0,
        stop_after=stop_after,
    )
    if summary_rows:
        return summary_rows
    if allow_full_fallback and _allow_gen_tasks_full_fallback(tenant_id):
        return _latest_rows_by_task_id_from_paths(
            _qa_read_paths(tenant_id, "gen_tasks.jsonl"),
            stop_after=stop_after,
        )
    return {}


def _build_bank_task_recovery_stats(tenant_id: str) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    def _merge(bucket_key: str, run_id: str) -> None:
        if not bucket_key:
            return
        bucket = stats.setdefault(bucket_key, {"saved_count": 0, "run_ids": set()})
        bucket["saved_count"] = int(bucket.get("saved_count", 0) or 0) + 1
        run_ids = bucket.get("run_ids")
        if run_id and isinstance(run_ids, set):
            run_ids.add(run_id)

    for row in _load_bank(tenant_bank_path(tenant_id)):
        if not isinstance(row, dict):
            continue
        tid = str(row.get("出题任务ID") or row.get("source_task_id") or row.get("task_id") or "").strip()
        task_name = str(row.get("出题任务名称") or row.get("task_name") or "").strip()
        if not tid:
            tid = ""
        run_id = str(row.get("出题RunID") or row.get("source_run_id") or row.get("run_id") or "").strip()
        if tid:
            _merge(f"task_id:{tid}", run_id)
        if task_name:
            _merge(f"task_name:{task_name}", run_id)
            parent_name = re.sub(r"#(?:p|repair)\d+$", "", task_name).strip()
            if parent_name and parent_name != task_name:
                _merge(f"task_name:{parent_name}", run_id)
    for tid, bucket in stats.items():
        run_ids = bucket.get("run_ids")
        if isinstance(run_ids, set):
            bucket["run_ids"] = sorted([str(x) for x in run_ids if str(x).strip()])
    return stats


def _apply_gen_task_bank_recovery(task: dict[str, Any], bank_stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(task, dict):
        return task
    tid = str(task.get("task_id", "")).strip()
    task_name = str(task.get("task_name", "")).strip()
    stat = None
    if tid:
        stat = bank_stats.get(f"task_id:{tid}")
    if not isinstance(stat, dict) and task_name:
        stat = bank_stats.get(f"task_name:{task_name}")
    if not isinstance(stat, dict):
        return task
    saved_count = int(stat.get("saved_count", 0) or 0)
    if saved_count <= 0:
        return task
    patched = dict(task)
    patched["saved_count"] = int(max(int(patched.get("saved_count", 0) or 0), saved_count))
    patched["generated_count"] = int(max(int(patched.get("generated_count", 0) or 0), int(patched["saved_count"])))
    progress = patched.get("progress") if isinstance(patched.get("progress"), dict) else {}
    req = patched.get("request") if isinstance(patched.get("request"), dict) else {}
    total = int(progress.get("total", 0) or 0) or int(req.get("num_questions", 0) or 0)
    current = int(progress.get("current", 0) or 0)
    patched["progress"] = {"current": int(max(current, int(patched["saved_count"]))), "total": int(max(total, 0))}
    if not str(patched.get("run_id", "")).strip():
        run_ids = stat.get("run_ids")
        if isinstance(run_ids, list) and len(run_ids) == 1:
            patched["run_id"] = str(run_ids[0]).strip()
    return patched


def _is_bank_row_template_backup_pass(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    return bool(row.get("模板备选题")) and str(row.get("审计状态", "") or "").strip() in {"template_backup_pass"}


def _build_run_questions_from_bank(
    tenant_id: str,
    run_id: str,
    *,
    include_template_backups: bool = False,
) -> list[dict[str, Any]]:
    rid = str(run_id or "").strip()
    if not rid:
        return []
    bank = _load_bank(tenant_bank_path(tenant_id))
    questions: list[dict[str, Any]] = []
    idx = 0
    for row in bank:
        if not isinstance(row, dict):
            continue
        row_run_id = str(row.get("出题RunID") or row.get("source_run_id") or row.get("run_id") or "").strip()
        if row_run_id != rid:
            continue
        if row.get("是否正式通过") is False and not (include_template_backups and _is_bank_row_template_backup_pass(row)):
            continue
        idx += 1
        stem = str(row.get("题干", "") or "").strip()
        answer = str(row.get("正确答案", "") or "").strip()
        explanation = str(row.get("解析", "") or "").strip()
        options: list[str] = []
        final_json: dict[str, Any] = {
            "题干": stem,
            "正确答案": answer,
            "解析": explanation,
            "来源路径": str(row.get("来源路径", "") or ""),
            "来源切片ID": row.get("来源切片ID"),
            "教材版本ID": str(row.get("教材版本ID", "") or ""),
        }
        for i in range(1, 9):
            opt = str(row.get(f"选项{i}", "") or "").strip()
            final_json[f"选项{i}"] = str(row.get(f"选项{i}", "") or "")
            if opt:
                options.append(opt)
        questions.append(
            {
                "question_id": f"{tenant_id}:{rid}:{idx}",
                "index": idx,
                "question_text": stem,
                "answer": answer,
                "explanation": explanation,
                "options": options,
                "saved": True,
                "template_backup": bool(row.get("模板备选题")),
                "slice_id": row.get("来源切片ID"),
                "slice_path": str(row.get("来源路径", "") or ""),
                "slice_content": str(row.get("切片原文", "") or ""),
                "final_json": final_json,
            }
        )
    return questions


def _build_task_questions_from_bank(
    tenant_id: str,
    task_name: str,
    *,
    include_template_backups: bool = False,
) -> list[dict[str, Any]]:
    name = str(task_name or "").strip()
    if not name:
        return []
    matched_rows: list[dict[str, Any]] = []
    has_template_selection = False
    for row in _load_bank(tenant_bank_path(tenant_id)):
        if not isinstance(row, dict):
            continue
        row_task_name = str(row.get("出题任务名称") or row.get("task_name") or "").strip()
        if not row_task_name:
            continue
        if row_task_name != name and not row_task_name.startswith(f"{name}#"):
            continue
        matched_rows.append(row)
        if "模板正式题" in row or "模板备选题" in row or str(row.get("模板父任务名称", "") or "").strip() == name:
            has_template_selection = True
    if has_template_selection:
        formal_or_backup_rows = [
            row for row in matched_rows
            if bool(row.get("模板正式题")) or (include_template_backups and _is_bank_row_template_backup_pass(row))
        ]
        if formal_or_backup_rows:
            matched_rows = formal_or_backup_rows
        else:
            # 模板任务在父任务中断/失败时，可能尚未完成“正式题收口”，此时仅有模板备选题。
            # 为保证任务详情可回看已产出结果，回退展示“未显式判定失败”的暂存题。
            matched_rows = [row for row in matched_rows if row.get("是否正式通过") is not False]
    else:
        matched_rows = [row for row in matched_rows if row.get("是否正式通过") is not False]
    questions: list[dict[str, Any]] = []
    idx = 0
    for row in matched_rows:
        idx += 1
        stem = str(row.get("题干", "") or "").strip()
        answer = str(row.get("正确答案", "") or "").strip()
        explanation = str(row.get("解析", "") or "").strip()
        options: list[str] = []
        for opt_idx in range(1, 9):
            opt_val = str(row.get(f"选项{opt_idx}", "") or "").strip()
            if opt_val:
                options.append(opt_val)
        questions.append(
            {
                "index": idx,
                "question_id": f"bank_task:{idx}",
                "question_text": stem,
                "answer": answer,
                "explanation": explanation,
                "options": options,
                "slice_path": str(row.get("来源路径", "") or ""),
                "slice_id": row.get("来源切片ID"),
                "saved": True,
                "template_backup": bool(row.get("模板备选题")),
                "final_json": dict(row),
            }
        )
    return questions


def _build_task_questions_from_related_runs(tenant_id: str, task_name: str) -> list[dict[str, Any]]:
    name = str(task_name or "").strip()
    if not name:
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    running_index = 0
    for run in _read_jsonl(_qa_runs_path(tenant_id)):
        if not isinstance(run, dict):
            continue
        run_task_name = str(run.get("task_name", "") or ((run.get("config") or {}).get("task_name", "") if isinstance(run.get("config"), dict) else "") or "").strip()
        if not run_task_name:
            continue
        if run_task_name != name and not _parse_template_child_task_name(name, run_task_name):
            continue
        questions = run.get("questions") if isinstance(run.get("questions"), list) else []
        for row in questions:
            if not isinstance(row, dict):
                continue
            qid = str(row.get("question_id", "") or "").strip()
            if qid and qid in seen_ids:
                continue
            if qid:
                seen_ids.add(qid)
            running_index += 1
            item = dict(row)
            item["index"] = running_index
            if not str(item.get("question_id", "") or "").strip():
                item["question_id"] = f"run_task:{running_index}"
            out.append(item)
    return out


def _resolve_judge_aggregate_task_name(tenant_id: str, run: dict[str, Any]) -> str:
    if not isinstance(run, dict):
        return ""
    cfg = run.get("config") if isinstance(run.get("config"), dict) else {}
    task_id = str(cfg.get("task_id", "") or run.get("task_id", "") or "").strip()
    direct_parent_name = str(
        cfg.get("parent_task_name", "")
        or run.get("parent_task_name", "")
        or ""
    ).strip()
    if direct_parent_name:
        return direct_parent_name

    task_row = _latest_gen_task_rows(tenant_id, allow_full_fallback=True).get(task_id) if task_id else None
    if isinstance(task_row, dict):
        task_row_parent_name = str(
            task_row.get("parent_task_name", "")
            or ((task_row.get("request") or {}).get("parent_task_name", "") if isinstance(task_row.get("request"), dict) else "")
            or ""
        ).strip()
        if task_row_parent_name:
            return task_row_parent_name

    parent_task_id = str(cfg.get("parent_task_id", "") or run.get("parent_task_id", "") or "").strip()
    if parent_task_id:
        parent_row = _latest_gen_task_rows(tenant_id, allow_full_fallback=True).get(parent_task_id) or {}
        parent_name = str(parent_row.get("task_name", "") or "").strip()
        if parent_name:
            return parent_name

    task_name = str(
        run.get("task_name", "")
        or cfg.get("task_name", "")
        or (task_row.get("task_name", "") if isinstance(task_row, dict) else "")
        or ""
    ).strip()
    if task_name:
        if "#" in task_name:
            return task_name.split("#", 1)[0].strip()
        # Parent template task runs may not carry template context after recovery/restart.
        # If bank rows exist under "<task_name>#...", judge should still aggregate at parent-task scope.
        bank_stats = _build_bank_task_recovery_stats(tenant_id)
        if isinstance(bank_stats.get(f"task_name:{task_name}"), dict):
            for bucket_key in bank_stats.keys():
                if isinstance(bucket_key, str) and bucket_key.startswith(f"task_name:{task_name}#"):
                    return task_name
    has_template_context = bool(
        str(cfg.get("template_id", "") or "").strip()
        or str(cfg.get("resume_from_task_id", "") or "").strip()
        or str(cfg.get("child_kind", "") or "").strip()
        or str(((task_row.get("request") or {}).get("template_id", "") if isinstance(task_row, dict) and isinstance(task_row.get("request"), dict) else "")).strip()
    )
    if task_name and "#" in task_name and has_template_context:
        return task_name.split("#", 1)[0].strip()
    return ""


def _hydrate_judge_run_questions_from_parent_task_if_needed(
    tenant_id: str,
    run: dict[str, Any],
    requested_ids_raw: Any = None,
) -> tuple[dict[str, Any], bool]:
    if not isinstance(run, dict):
        return run, False
    requested_ids = requested_ids_raw
    if requested_ids is not None and not isinstance(requested_ids, list):
        requested_ids = [requested_ids] if requested_ids else []
    if requested_ids:
        return run, False
    parent_task_name = _resolve_judge_aggregate_task_name(tenant_id, run)
    if not parent_task_name:
        return run, False
    aggregate_questions = _build_task_questions_from_bank(
        tenant_id,
        parent_task_name,
        include_template_backups=True,
    )
    if not aggregate_questions:
        return run, False
    current_questions = run.get("questions") if isinstance(run.get("questions"), list) else []
    if len(aggregate_questions) <= len(current_questions):
        return run, False
    hydrated = dict(run)
    hydrated["questions"] = aggregate_questions
    judge_scope = hydrated.get("judge_scope") if isinstance(hydrated.get("judge_scope"), dict) else {}
    judge_scope.update(
        {
            "mode": "task_aggregate",
            "parent_task_name": parent_task_name,
            "question_count": len(aggregate_questions),
        }
    )
    hydrated["judge_scope"] = judge_scope
    return hydrated, True


def _build_task_items_from_qa_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in questions or []:
        if not isinstance(row, dict):
            continue
        final_json = row.get("final_json") if isinstance(row.get("final_json"), dict) else {}
        item = dict(final_json) if final_json else {}
        if not item:
            item = {
                "题干": str(row.get("question_text", "") or ""),
                "正确答案": str(row.get("answer", "") or ""),
                "解析": str(row.get("explanation", "") or ""),
                "来源路径": str(row.get("slice_path", "") or ""),
                "来源切片ID": row.get("slice_id"),
            }
            options = row.get("options") if isinstance(row.get("options"), list) else []
            for idx, opt in enumerate(options[:8], start=1):
                item[f"选项{idx}"] = str(opt or "")
        if "题干" not in item:
            item["题干"] = str(row.get("question_text", "") or "")
        if "正确答案" not in item:
            item["正确答案"] = str(row.get("answer", "") or "")
        if "解析" not in item:
            item["解析"] = str(row.get("explanation", "") or "")
        if "来源路径" not in item:
            item["来源路径"] = str(row.get("slice_path", "") or "")
        if "来源切片ID" not in item:
            item["来源切片ID"] = row.get("slice_id")
        items.append(item)
    return items


def _parse_template_child_task_name(parent_task_name: str, child_task_name: str) -> dict[str, Any] | None:
    parent_name = str(parent_task_name or "").strip()
    child_name = str(child_task_name or "").strip()
    if not parent_name or not child_name or child_name == parent_name:
        return None
    if not child_name.startswith(f"{parent_name}#"):
        return None
    suffix = child_name[len(parent_name) + 1 :]
    m_repair = re.match(r"repair(\d+)-(\d+)$", suffix)
    if m_repair:
        return {
            "kind": "repair",
            "label": suffix,
            "round": int(m_repair.group(1)),
            "shard_index": int(m_repair.group(2)),
        }
    m_shard = re.match(r"p(\d+)$", suffix)
    if m_shard:
        return {
            "kind": "shard",
            "label": suffix,
            "round": 0,
            "shard_index": int(m_shard.group(1)),
        }
    m_resume = re.match(r"resume(\d+)$", suffix)
    if m_resume:
        return {
            "kind": "resume",
            "label": suffix,
            "round": 0,
            "shard_index": int(m_resume.group(1)),
        }
    return {
        "kind": "child",
        "label": suffix,
        "round": 0,
        "shard_index": 0,
    }


def _summarize_slice_failure_stats(process_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, dict[str, Any]] = {}
    for row in process_trace or []:
        if not isinstance(row, dict):
            continue
        sid = int(row.get("slice_id", 0) or 0)
        if sid <= 0:
            continue
        bucket = buckets.setdefault(
            sid,
            {
                "slice_id": sid,
                "attempt_count": 0,
                "fail_count": 0,
                "pass_count": 0,
                "saved_with_issues_count": 0,
                "latest_target_index": 0,
                "last_fail_types": [],
                "last_reason": "",
                "latest_path": str(row.get("slice_path", "") or "").strip(),
            },
        )
        bucket["attempt_count"] = int(bucket.get("attempt_count", 0) or 0) + 1
        bucket["latest_target_index"] = max(
            int(bucket.get("latest_target_index", 0) or 0),
            int(row.get("target_index", 0) or row.get("index", 0) or 0),
        )
        if bool(row.get("saved")):
            bucket["pass_count"] = int(bucket.get("pass_count", 0) or 0) + 1
        else:
            bucket["fail_count"] = int(bucket.get("fail_count", 0) or 0) + 1
            critic_result = row.get("critic_result") if isinstance(row.get("critic_result"), dict) else {}
            fail_types = critic_result.get("fail_types") if isinstance(critic_result.get("fail_types"), list) else []
            if not fail_types:
                fail_types = row.get("critic_last_fail_types") if isinstance(row.get("critic_last_fail_types"), list) else []
            bucket["last_fail_types"] = [str(x) for x in fail_types if str(x).strip()][:8]
            reason = str(
                critic_result.get("reason", "")
                or critic_result.get("fix_reason", "")
                or row.get("critic_details", "")
                or ""
            ).strip()
            if reason:
                bucket["last_reason"] = reason
        if bool(row.get("saved_with_issues")):
            bucket["saved_with_issues_count"] = int(bucket.get("saved_with_issues_count", 0) or 0) + 1
    out = list(buckets.values())
    out.sort(
        key=lambda x: (
            -int(x.get("fail_count", 0) or 0),
            -int(x.get("attempt_count", 0) or 0),
            int(x.get("slice_id", 0) or 0),
        )
    )
    return out


def _build_task_related_run_diagnostics(tenant_id: str, task: dict[str, Any]) -> dict[str, Any]:
    task_name = str(task.get("task_name", "") or "").strip()
    task_id = str(task.get("task_id", "") or "").strip()
    task_run_id = str(task.get("run_id", "") or "").strip()
    if not task_name:
        return {"subtasks": [], "repair_rounds": [], "related_run_count": 0}

    subtasks: list[dict[str, Any]] = []
    repair_buckets: dict[int, dict[str, Any]] = {}
    seen_keys: set[tuple[str, str]] = set()
    subtask_index: dict[tuple[str, str], dict[str, Any]] = {}
    latest_rows = _latest_gen_task_rows(tenant_id, allow_full_fallback=True)
    latest_task_name_to_id: dict[str, str] = {}
    for tid, row in latest_rows.items():
        if not isinstance(row, dict):
            continue
        tn = str(row.get("task_name", "") or "").strip()
        if not tn:
            continue
        prev_tid = latest_task_name_to_id.get(tn, "")
        if not prev_tid:
            latest_task_name_to_id[tn] = str(tid or "")
            continue
        prev_row = latest_rows.get(prev_tid) or {}
        prev_ts = _parse_iso_ts(str(prev_row.get("updated_at", "") or "")) or _parse_iso_ts(str(prev_row.get("started_at", "") or ""))
        cur_ts = _parse_iso_ts(str(row.get("updated_at", "") or "")) or _parse_iso_ts(str(row.get("started_at", "") or ""))
        if cur_ts and prev_ts and cur_ts >= prev_ts:
            latest_task_name_to_id[tn] = str(tid or "")

    for run in reversed(_read_jsonl(_qa_runs_path(tenant_id))):
        if not isinstance(run, dict):
            continue
        run_id = str(run.get("run_id", "") or "").strip()
        cfg = run.get("config") if isinstance(run.get("config"), dict) else {}
        run_task_name = str(run.get("task_name", "") or cfg.get("task_name", "") or "").strip()
        if not run_task_name or run_task_name == task_name:
            continue
        parsed = _parse_template_child_task_name(task_name, run_task_name)
        if not isinstance(parsed, dict):
            continue
        dedupe_key = (run_task_name, run_id)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        metrics = run.get("batch_metrics") if isinstance(run.get("batch_metrics"), dict) else {}
        errors = run.get("errors") if isinstance(run.get("errors"), list) else []
        generated_count = int(metrics.get("generated_count", 0) or len(run.get("questions") or []) or 0)
        saved_count = int(metrics.get("saved_count", 0) or 0)
        error_count = int(metrics.get("error_count", 0) or len(errors))
        ended_at = str(run.get("ended_at", "") or "").strip()
        status = "running" if not ended_at else ("failed" if error_count > 0 and saved_count <= 0 else "completed")
        inferred_task_id = str(cfg.get("task_id", "") or "").strip()
        if not inferred_task_id:
            inferred_task_id = str(latest_task_name_to_id.get(run_task_name, "") or "").strip()
        row = {
            "task_id": inferred_task_id,
            "task_name": run_task_name,
            "run_id": run_id,
            "kind": str(parsed.get("kind", "") or "child"),
            "round": int(parsed.get("round", 0) or 0),
            "shard_index": int(parsed.get("shard_index", 0) or 0),
            "status": status,
            "started_at": str(run.get("started_at", "") or ""),
            "ended_at": ended_at,
            "generated_count": generated_count,
            "saved_count": saved_count,
            "error_count": error_count,
            "generated_total_count": int(max(generated_count, 0) + max(error_count, 0)),
            "source_task_id": str(cfg.get("task_id", "") or ""),
            "source_parent_task_id": task_id,
            "latest_error": str(errors[-1]).strip() if errors else "",
        }
        # Prefer latest persisted task row (O(1) map lookup) to avoid per-subtask heavy snapshot scans.
        if inferred_task_id:
            child_task = latest_rows.get(inferred_task_id)
            if isinstance(child_task, dict):
                child_errors = [str(x).strip() for x in (child_task.get("errors") or []) if str(x).strip()]
                row["status"] = str(child_task.get("status", "") or row["status"])
                row["started_at"] = str(child_task.get("started_at", "") or row["started_at"])
                row["ended_at"] = str(child_task.get("ended_at", "") or row["ended_at"])
                row["generated_count"] = int(child_task.get("generated_count", row["generated_count"]) or row["generated_count"])
                row["saved_count"] = int(child_task.get("saved_count", row["saved_count"]) or row["saved_count"])
                row["error_count"] = int(child_task.get("error_count", row["error_count"]) or row["error_count"])
                row["generated_total_count"] = int(max(int(row.get("generated_count", 0) or 0), 0) + max(int(row.get("error_count", 0) or 0), 0))
                if child_errors:
                    row["latest_error"] = child_errors[-1]
        if "generated_total_count" not in row:
            row["generated_total_count"] = int(max(int(row.get("generated_count", 0) or 0), 0) + max(int(row.get("error_count", 0) or 0), 0))
        subtasks.append(row)
        subtask_index[dedupe_key] = row
        if row["kind"] == "repair" and row["round"] > 0:
            bucket = repair_buckets.setdefault(
                row["round"],
                {
                    "round": row["round"],
                    "strategy": "",
                    "strategy_reason": "",
                    "targets": [],
                    "subtask_count": 0,
                    "generated_count": 0,
                    "saved_count": 0,
                    "error_count": 0,
                    "run_ids": [],
                    "statuses": [],
                },
            )
            bucket["subtask_count"] = int(bucket.get("subtask_count", 0) or 0) + 1
            bucket["generated_count"] = int(bucket.get("generated_count", 0) or 0) + int(row.get("generated_count", 0) or 0)
            bucket["saved_count"] = int(bucket.get("saved_count", 0) or 0) + int(row.get("saved_count", 0) or 0)
            bucket["error_count"] = int(bucket.get("error_count", 0) or 0) + int(row.get("error_count", 0) or 0)
            bucket["generated_total_count"] = int(bucket.get("generated_total_count", 0) or 0) + int(row.get("generated_total_count", 0) or 0)
            bucket["run_ids"] = list(bucket.get("run_ids") or []) + ([run_id] if run_id else [])
            bucket["statuses"] = list(bucket.get("statuses") or []) + [status]

    # Bank recovery: merge bank-derived shard counters to补齐缺失子任务，并修正每个子任务可复核产出量。
    bank_subtask_counts: dict[str, int] = {}
    bank_subtask_meta: dict[str, dict[str, Any]] = {}
    for row in _load_bank(tenant_bank_path(tenant_id)):
        if not isinstance(row, dict):
            continue
        run_task_name = str(row.get("出题任务名称") or row.get("task_name") or "").strip()
        if not run_task_name or run_task_name == task_name:
            continue
        parsed = _parse_template_child_task_name(task_name, run_task_name)
        if not isinstance(parsed, dict):
            continue
        bank_subtask_counts[run_task_name] = int(bank_subtask_counts.get(run_task_name, 0) or 0) + 1
        if run_task_name not in bank_subtask_meta:
            bank_subtask_meta[run_task_name] = {
                "kind": str(parsed.get("kind", "") or "child"),
                "round": int(parsed.get("round", 0) or 0),
                "shard_index": int(parsed.get("shard_index", 0) or 0),
            }

    by_task_name: dict[str, dict[str, Any]] = {}
    for row in subtasks:
        name_key = str(row.get("task_name", "") or "").strip()
        if name_key and name_key not in by_task_name:
            by_task_name[name_key] = row
    for run_task_name, count in bank_subtask_counts.items():
        if count <= 0:
            continue
        existing = by_task_name.get(run_task_name)
        if isinstance(existing, dict):
            existing["saved_count"] = int(max(int(existing.get("saved_count", 0) or 0), count))
            existing["generated_count"] = int(max(int(existing.get("generated_count", 0) or 0), count))
            existing["generated_total_count"] = int(max(int(existing.get("generated_total_count", 0) or 0), int(existing.get("generated_count", 0) or 0) + int(existing.get("error_count", 0) or 0)))
            continue
        meta = bank_subtask_meta.get(run_task_name) or {}
        recovered = {
            "task_id": "",
            "task_name": run_task_name,
            "run_id": "",
            "kind": str(meta.get("kind", "") or "child"),
            "round": int(meta.get("round", 0) or 0),
            "shard_index": int(meta.get("shard_index", 0) or 0),
            "status": "completed",
            "started_at": "",
            "ended_at": "",
            "generated_count": int(count),
            "saved_count": int(count),
            "error_count": 0,
            "generated_total_count": int(count),
            "source_task_id": "",
            "source_parent_task_id": task_id,
            "latest_error": "",
        }
        subtasks.append(recovered)
        by_task_name[run_task_name] = recovered
    strategy_map: dict[int, str] = {}
    reason_map: dict[int, str] = {}
    for err in task.get("errors") or []:
        msg = str(err or "").strip()
        m = re.search(r"模板修复策略\(第(\d+)轮\):\s*([a-zA-Z_]+)(?:（(.*)）)?", msg)
        if not m:
            continue
        round_no = int(m.group(1))
        strategy_map[round_no] = str(m.group(2) or "").strip()
        reason_map[round_no] = str(m.group(3) or "").strip()
    repair_rounds = []
    for round_no in sorted(repair_buckets.keys()):
        bucket = dict(repair_buckets[round_no])
        bucket["strategy"] = strategy_map.get(round_no, "")
        bucket["strategy_reason"] = reason_map.get(round_no, "")
        statuses = [str(x).strip() for x in (bucket.get("statuses") or []) if str(x).strip()]
        if any(x == "running" for x in statuses):
            bucket["status"] = "running"
        elif any(x == "failed" for x in statuses):
            bucket["status"] = "partial"
        else:
            bucket["status"] = "completed"
        bucket.pop("statuses", None)
        repair_rounds.append(bucket)
    subtasks.sort(
        key=lambda x: (
            0 if str(x.get("kind", "")) == "shard" else 1,
            int(x.get("round", 0) or 0),
            int(x.get("shard_index", 0) or 0),
            str(x.get("started_at", "") or ""),
            str(x.get("run_id", "") or ""),
        )
    )
    return {
        "subtasks": subtasks,
        "repair_rounds": repair_rounds,
        "related_run_count": len(subtasks) + (1 if task_run_id else 0),
    }


def _build_live_subtask_traces(tenant_id: str, task: dict[str, Any]) -> list[dict[str, Any]]:
    subtasks = [x for x in (task.get("subtasks") or []) if isinstance(x, dict)]
    if not subtasks:
        return []
    concrete_named_subtasks: set[str] = set()
    for sub in subtasks:
        if not isinstance(sub, dict):
            continue
        task_name = str(sub.get("task_name", "") or "").strip()
        if not task_name:
            continue
        if str(sub.get("task_id", "") or "").strip() or str(sub.get("run_id", "") or "").strip():
            concrete_named_subtasks.add(task_name)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sub in subtasks:
        child_task_id = str(sub.get("task_id", "") or "").strip()
        sub_run_id = str(sub.get("run_id", "") or "").strip()
        sub_task_name = str(sub.get("task_name", "") or "").strip()
        # Prefer concrete task rows. Anonymous summary rows with the same task_name only add duplicate empty traces.
        if not child_task_id and not sub_run_id and sub_task_name and sub_task_name in concrete_named_subtasks:
            continue
        dedupe_id = child_task_id or sub_run_id or sub_task_name
        if not dedupe_id or dedupe_id in seen:
            continue
        seen.add(dedupe_id)
        child = None
        if child_task_id:
            child = _get_latest_gen_task_snapshot(tenant_id, child_task_id)
            if not isinstance(child, dict):
                child = _read_persisted_task(tenant_id, child_task_id)
        trace_rows = [x for x in ((child or {}).get("process_trace") or []) if isinstance(x, dict)]
        run = _get_qa_run_by_id(tenant_id, sub_run_id) if sub_run_id else None
        if not trace_rows and isinstance(run, dict):
            run_trace = [x for x in (run.get("process_trace") or []) if isinstance(x, dict)]
            if run_trace:
                trace_rows = run_trace
            else:
                run_questions = run.get("questions") if isinstance(run.get("questions"), list) else []
                if run_questions:
                    trace_rows = _build_minimal_trace_from_qa_questions(run_questions)
            run_errors = [str(x).strip() for x in (run.get("errors") or []) if str(x).strip()]
            if run_errors:
                base_idx = len(trace_rows)
                for err_idx, err_msg in enumerate(run_errors, start=1):
                    trace_rows.append(
                        {
                            "index": base_idx + err_idx,
                            "target_index": base_idx + err_idx,
                            "question_id": f"fail:{sub_run_id or dedupe_id}:{err_idx}",
                            "slice_id": 0,
                            "slice_path": "",
                            "elapsed_ms": 0,
                            "timing_unknown": True,
                            "saved": False,
                            "steps": [
                                {
                                    "seq": 1,
                                    "node": "system",
                                    "level": "error",
                                    "message": "出题失败",
                                    "detail": err_msg,
                                    "time": "",
                                    "elapsed_ms": 0,
                                    "delta_ms": 0,
                                    "run_id": 0,
                                }
                            ],
                            "final_json": {},
                            "critic_result": {"passed": False, "reason": err_msg},
                        }
                    )
        target_start = int(sub.get("target_start", 0) or 0)
        mapped_trace: list[dict[str, Any]] = []
        for idx, row in enumerate(trace_rows, start=1):
            mapped = dict(row)
            local_target = int(mapped.get("target_index", 0) or mapped.get("index", 0) or idx)
            global_target = target_start + local_target - 1 if target_start > 0 and local_target > 0 else local_target
            if global_target > 0:
                mapped["target_index"] = global_target
                if not int(mapped.get("index", 0) or 0):
                    mapped["index"] = global_target
            mapped_trace.append(mapped)
        child_status = str((child or {}).get("status", "") or sub.get("status", "") or "").strip().lower()
        if not mapped_trace and child_status in {"pending", "running"} and isinstance(child, dict):
            current_node = str((child or {}).get("current_node", "") or "").strip() or "system"
            current_label = (
                f"第 {int(target_start)} 题"
                if int(sub.get("target_start", 0) or 0) == int(sub.get("target_end", 0) or 0) and int(target_start) > 0
                else (
                    f"第 {int(sub.get('target_start', 0) or 0)}-{int(sub.get('target_end', 0) or 0)} 题"
                    if int(sub.get("target_start", 0) or 0) > 0 and int(sub.get("target_end", 0) or 0) > 0
                    else "当前子任务"
                )
            )
            mapped_trace = [
                {
                    "index": int(target_start or 1),
                    "target_index": int(target_start or 1),
                    "question_id": f"live:{child_task_id}",
                    "slice_id": int((task.get("current_subcall") or {}).get("slice_id", 0) or 0),
                    "slice_path": "",
                    "elapsed_ms": 0,
                    "timing_unknown": True,
                    "saved": False,
                    "steps": [
                        {
                            "seq": 1,
                            "node": current_node,
                            "level": "info",
                            "message": "子任务执行中",
                            "detail": f"{current_label} 正在执行，当前节点={current_node}",
                            "time": "",
                            "elapsed_ms": 0,
                            "delta_ms": 0,
                            "run_id": 0,
                        }
                    ],
                    "final_json": {},
                    "critic_result": {},
                }
            ]
        # Ended subtasks without replayable traces should not produce noisy placeholder rows in UI.
        if not mapped_trace and child_status in {"completed", "failed", "cancelled", "canceled"}:
            continue
        out.append(
            {
                "task_id": child_task_id or sub_run_id or sub_task_name,
                "task_name": str((child or {}).get("task_name", "") or sub_task_name or ""),
                "status": str((child or {}).get("status", "") or sub.get("status", "") or ""),
                "current_node": str((child or {}).get("current_node", "") or ""),
                "run_id": sub_run_id,
                "target_start": int(sub.get("target_start", 0) or 0),
                "target_end": int(sub.get("target_end", 0) or 0),
                "round": int(sub.get("round", 0) or 0),
                "kind": str(sub.get("kind", "") or ""),
                "process_trace": mapped_trace,
            }
        )
    out.sort(
        key=lambda x: (
            int(x.get("target_start", 0) or 0),
            int(x.get("round", 0) or 0),
            str(x.get("task_id", "") or ""),
        )
    )
    return out


def _flatten_live_subtask_traces(live_subtask_traces: list[dict[str, Any]], *, limit: int = 4000) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_fingerprints: set[tuple[Any, ...]] = set()
    if not isinstance(live_subtask_traces, list):
        return rows
    for sub in live_subtask_traces:
        if not isinstance(sub, dict):
            continue
        sub_trace = [x for x in (sub.get("process_trace") or []) if isinstance(x, dict)]
        if not sub_trace:
            continue
        sub_task_id = str(sub.get("task_id", "") or "").strip()
        sub_task_name = str(sub.get("task_name", "") or "").strip()
        for row in sub_trace:
            mapped = dict(row)
            if sub_task_id:
                mapped["subtask_id"] = sub_task_id
            if sub_task_name:
                mapped["subtask_name"] = sub_task_name
            first_step = (mapped.get("steps") or [{}])[0] if isinstance(mapped.get("steps"), list) and mapped.get("steps") else {}
            fp = (
                str(mapped.get("question_id", "") or "").strip(),
                int(mapped.get("target_index", 0) or 0),
                int(mapped.get("slice_id", 0) or 0),
                str(sub_task_name or "").strip(),
                bool(mapped.get("saved")),
                str(first_step.get("node", "") or "").strip(),
                str(first_step.get("message", "") or "").strip(),
                str(first_step.get("detail", "") or "").strip(),
            )
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)
            rows.append(mapped)
            if len(rows) >= max(1, int(limit or 1)):
                break
        if len(rows) >= max(1, int(limit or 1)):
            break
    rows.sort(key=lambda x: (int(x.get("target_index", 0) or 0), int(x.get("index", 0) or 0)))
    return rows


def _derive_current_subcall(task: dict[str, Any]) -> dict[str, Any]:
    current_subcall = task.get("current_subcall") if isinstance(task.get("current_subcall"), dict) else {}
    if current_subcall:
        return dict(current_subcall)
    current_node = str(task.get("current_node", "") or "").strip()
    current_question_id = str(task.get("current_question_id", "") or "").strip()
    progress = task.get("progress") if isinstance(task.get("progress"), dict) else {}
    if not current_node and not current_question_id:
        return {}
    return {
        "mode": current_node or "unknown",
        "question_label": current_question_id,
        "progress_current": int(progress.get("current", 0) or 0),
        "progress_total": int(progress.get("total", 0) or 0),
        "updated_at": str(task.get("current_node_updated_at", "") or ""),
    }


def _build_minimal_trace_from_qa_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for idx, row in enumerate(questions or [], start=1):
        if not isinstance(row, dict):
            continue
        answer = str(row.get("answer", "") or "").strip()
        slice_path = str(row.get("slice_path", "") or "").strip()
        steps = [
            {
                "seq": 1,
                "node": "system",
                "level": "success",
                "message": "题目已恢复",
                "detail": "服务重启后从 qa_runs 恢复已生成题目",
                "time": "",
                "elapsed_ms": 0,
                "delta_ms": 0,
                "run_id": 0,
            },
            {
                "seq": 2,
                "node": "writer",
                "level": "info",
                "message": "定稿题干",
                "detail": str(row.get("question_text", "") or ""),
                "time": "",
                "elapsed_ms": 0,
                "delta_ms": 0,
                "run_id": 0,
            },
        ]
        options = row.get("options") if isinstance(row.get("options"), list) else []
        if options:
            steps.append(
                {
                    "seq": 3,
                    "node": "writer",
                    "level": "info",
                    "message": "定稿选项",
                    "detail": " | ".join(
                        f"{chr(64 + opt_idx)}. {str(opt or '').strip()}"
                        for opt_idx, opt in enumerate(options[:8], start=1)
                    ),
                    "time": "",
                    "elapsed_ms": 0,
                    "delta_ms": 0,
                    "run_id": 0,
                }
            )
        if answer:
            steps.append(
                {
                    "seq": len(steps) + 1,
                    "node": "critic",
                    "level": "success",
                    "message": "审核通过",
                    "detail": f"答案={answer}",
                    "time": "",
                    "elapsed_ms": 0,
                    "delta_ms": 0,
                    "run_id": 0,
                }
            )
        trace.append(
            {
                "index": idx,
                "target_index": idx,
                "question_id": str(row.get("question_id", "") or ""),
                "slice_id": row.get("slice_id"),
                "slice_path": slice_path,
                "elapsed_ms": 0,
                "timing_unknown": True,
                "saved": bool(row.get("saved", True)),
                "steps": steps,
                "final_json": dict(row.get("final_json")) if isinstance(row.get("final_json"), dict) else {},
                "critic_result": {"passed": True} if answer else {},
            }
        )
    return trace


def _hydrate_task_detail_from_run(tenant_id: str, task: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(task, dict):
        return task
    out = dict(task)
    live_subtasks = [x for x in (out.get("subtasks") or []) if isinstance(x, dict)]
    task_status = str(out.get("status", "")).strip().lower()
    is_live_task = task_status in {"pending", "running"}
    run_id = str(out.get("run_id", "") or "").strip()
    run = _get_qa_run_by_id(tenant_id, run_id) if run_id else None
    questions = run.get("questions") if isinstance(run, dict) and isinstance(run.get("questions"), list) else []
    has_task_trace = bool([x for x in (out.get("process_trace") or []) if isinstance(x, dict)])
    has_task_items = bool([x for x in (out.get("items") or []) if isinstance(x, dict)])
    # 常规回填：终态任务从 run/bank 回填。
    if not questions and not is_live_task and not has_task_trace and not has_task_items:
        task_name = str(out.get("task_name", "") or "")
        questions = _build_task_questions_from_related_runs(tenant_id, task_name)
        bank_questions = _build_task_questions_from_bank(tenant_id, task_name)
        # Prefer the larger recoverable set so detail "可复核"与任务字段口径一致，避免出现 19/33 混淆。
        if len(bank_questions) > len(questions):
            questions = bank_questions
    # 续跑中的父任务也要看到历史记录：当已保存题数>0但当前快照 items/trace 不完整时，补历史回填。
    if not questions and is_live_task:
        req = out.get("request") if isinstance(out.get("request"), dict) else {}
        is_parent = not str(out.get("parent_task_id", "") or "").strip()
        is_template_task = bool(str(req.get("template_id", "") or out.get("template_id", "") or "").strip())
        if is_parent and is_template_task and int(out.get("saved_count", 0) or 0) > 0:
            task_name = str(out.get("task_name", "") or "")
            bank_questions = _build_task_questions_from_bank(tenant_id, task_name)
            if bank_questions:
                questions = bank_questions
            else:
                questions = _build_task_questions_from_related_runs(tenant_id, task_name)
    if questions and not (isinstance(out.get("items"), list) and out.get("items")):
        out["items"] = _build_task_items_from_qa_questions(questions)
    if questions and not (isinstance(out.get("process_trace"), list) and out.get("process_trace")):
        out["process_trace"] = _build_minimal_trace_from_qa_questions(questions)
    if questions:
        q_count = len(questions)
        out["generated_count"] = max(int(out.get("generated_count", 0) or 0), q_count)
        out["saved_count"] = max(int(out.get("saved_count", 0) or 0), sum(1 for q in questions if isinstance(q, dict) and q.get("saved", True)))
        progress = out.get("progress") if isinstance(out.get("progress"), dict) else {}
        total = int(progress.get("total", 0) or 0)
        current = int(progress.get("current", 0) or 0)
        out["progress"] = {
            "current": max(current, int(out["generated_count"])),
            "total": max(total, int((out.get("request") or {}).get("num_questions", 0) or 0)),
        }
    out["generated_total_count"] = int(
        max(int(out.get("generated_total_count", 0) or 0), int(out.get("generated_count", 0) or 0) + int(out.get("error_count", 0) or 0))
    )
    bm = run.get("batch_metrics") if isinstance(run, dict) and isinstance(run.get("batch_metrics"), dict) else {}
    if bm:
        out["batch_metrics"] = bm
    cs = run.get("cost_summary") if isinstance(run, dict) and isinstance(run.get("cost_summary"), dict) else {}
    if cs:
        out["cost_summary"] = cs
    diagnostics = {"subtasks": [], "repair_rounds": [], "related_run_count": 0}
    diag_subtasks: list[dict[str, Any]] = []
    if not is_live_task and not live_subtasks:
        diagnostics = _build_task_related_run_diagnostics(tenant_id, out)
        diag_subtasks = diagnostics.get("subtasks") if isinstance(diagnostics.get("subtasks"), list) else []
    elif is_live_task:
        # 运行中也补一份历史子任务，保证续跑详情与首次任务同结构可回放。
        diagnostics = _build_task_related_run_diagnostics(tenant_id, out)
        diag_subtasks = diagnostics.get("subtasks") if isinstance(diagnostics.get("subtasks"), list) else []
    if is_live_task and diag_subtasks:
        live_ids = {
            str(x.get("task_id", "") or "").strip()
            for x in live_subtasks
            if isinstance(x, dict) and str(x.get("task_id", "") or "").strip()
        }
        filtered_diag_subtasks: list[dict[str, Any]] = []
        for row in diag_subtasks:
            if not isinstance(row, dict):
                continue
            st = str(row.get("status", "") or "").strip().lower()
            tid_row = str(row.get("task_id", "") or "").strip()
            if st in {"pending", "running"}:
                if not tid_row:
                    continue
                if tid_row not in live_ids:
                    continue
            filtered_diag_subtasks.append(row)
        diag_subtasks = filtered_diag_subtasks

    merged_subtasks_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in diag_subtasks:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("task_id", "") or ""), str(row.get("task_name", "") or ""))
        merged_subtasks_by_key[key] = dict(row)
    for row in live_subtasks:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("task_id", "") or ""), str(row.get("task_name", "") or ""))
        base = merged_subtasks_by_key.get(key, {})
        merged = {**base, **row}
        if not str(merged.get("task_id", "") or "").strip():
            merged["task_id"] = str(base.get("task_id", "") or "")
        if not str(merged.get("run_id", "") or "").strip():
            merged["run_id"] = str(base.get("run_id", "") or "")
        merged_subtasks_by_key[key] = merged
    merged_subtasks = list(merged_subtasks_by_key.values())
    deduped_subtasks: dict[tuple[str, str, int, int, int], dict[str, Any]] = {}
    for row in merged_subtasks:
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("task_name", "") or "").strip(),
            str(row.get("kind", "") or "").strip(),
            int(row.get("round", 0) or 0),
            int(row.get("target_start", 0) or 0),
            int(row.get("target_end", 0) or 0),
        )
        prev = deduped_subtasks.get(key)
        if not isinstance(prev, dict):
            deduped_subtasks[key] = row
            continue
        prev_has_task_id = bool(str(prev.get("task_id", "") or "").strip())
        cur_has_task_id = bool(str(row.get("task_id", "") or "").strip())
        if cur_has_task_id and not prev_has_task_id:
            deduped_subtasks[key] = row
            continue
        prev_updated = str(prev.get("updated_at", "") or prev.get("ended_at", "") or prev.get("started_at", "") or "")
        cur_updated = str(row.get("updated_at", "") or row.get("ended_at", "") or row.get("started_at", "") or "")
        if cur_updated >= prev_updated:
            deduped_subtasks[key] = row
    merged_subtasks = list(deduped_subtasks.values())
    for row in merged_subtasks:
        if not isinstance(row, dict):
            continue
        row["generated_total_count"] = int(
            max(
                int(row.get("generated_total_count", 0) or 0),
                int(row.get("generated_count", 0) or 0) + int(row.get("error_count", 0) or 0),
            )
        )
    if (
        not (isinstance(out.get("process_trace"), list) and out.get("process_trace"))
        and str(out.get("status", "")).strip().lower() in {"pending", "running"}
        and not merged_subtasks
    ):
        req = out.get("request") if isinstance(out.get("request"), dict) else {}
        is_template_task = bool(str(req.get("template_id", "") or out.get("template_id", "") or "").strip())
        progress = out.get("progress") if isinstance(out.get("progress"), dict) else {}
        current = int(progress.get("current", 0) or 0)
        total = int(progress.get("total", 0) or req.get("num_questions", 0) or 0)
        detail = (
            f"模板并发子任务正在后台执行，已完成 {current} / {total} 题。首批结果回传后会展示逐题过程。"
            if is_template_task else
            f"任务正在后台执行，已完成 {current} / {total} 题。首批结果回传后会展示逐题过程。"
        )
        out["process_trace"] = [
            {
                "index": 1,
                "target_index": 1,
                "question_id": "live:task",
                "slice_id": 0,
                "slice_path": "",
                "elapsed_ms": 0,
                "timing_unknown": True,
                "saved": False,
                "steps": [
                    {
                        "seq": 1,
                        "node": "system",
                        "level": "info",
                        "message": "任务已启动",
                        "detail": detail,
                        "time": "",
                        "elapsed_ms": 0,
                        "delta_ms": 0,
                        "run_id": 0,
                    }
                ],
                "final_json": {},
                "critic_result": {},
            }
        ]
    out["subtasks"] = merged_subtasks
    out["subtask_count"] = len(merged_subtasks)
    out["repair_rounds"] = (
        diagnostics.get("repair_rounds") if isinstance(diagnostics.get("repair_rounds"), list) else []
    )
    out["related_run_count"] = int(max(int(diagnostics.get("related_run_count", 0) or 0), len(merged_subtasks)) or 0)
    trace_rows = [x for x in (out.get("process_trace") or []) if isinstance(x, dict)]
    out["slice_failure_stats"] = _summarize_slice_failure_stats(trace_rows)
    out["current_subcall"] = _derive_current_subcall(out)
    out["live_subtask_traces"] = _build_live_subtask_traces(tenant_id, out)
    if not trace_rows and isinstance(out.get("live_subtask_traces"), list) and out.get("live_subtask_traces"):
        stitched_trace = _flatten_live_subtask_traces(out.get("live_subtask_traces") or [])
        if stitched_trace:
            out["process_trace"] = stitched_trace
            trace_rows = [x for x in stitched_trace if isinstance(x, dict)]
            if not (isinstance(out.get("items"), list) and out.get("items")):
                out["items"] = [
                    dict(x.get("final_json"))
                    for x in trace_rows
                    if isinstance(x, dict) and bool(x.get("saved")) and isinstance(x.get("final_json"), dict)
                ]
            out["slice_failure_stats"] = _summarize_slice_failure_stats(trace_rows)
    return out


def _is_generate_run_still_active(tenant_id: str, run_id: str) -> bool:
    rid = str(run_id or "").strip()
    if not rid:
        return False
    run = _get_qa_run_by_id(tenant_id, rid)
    if not isinstance(run, dict):
        return False
    ended_at = str(run.get("ended_at", "") or "").strip()
    return not bool(ended_at)


def _is_judge_run_still_active(tenant_id: str, run_id: str, task_id: str, grace_seconds: int) -> bool:
    rid = str(run_id or "").strip()
    if not rid:
        return False
    run = _get_qa_run_by_id(tenant_id, rid)
    if not isinstance(run, dict):
        return False
    judge_job = run.get("judge_job") if isinstance(run.get("judge_job"), dict) else {}
    if str(judge_job.get("task_id", "")).strip() != str(task_id or "").strip():
        return False
    status = str(judge_job.get("status", "")).lower().strip()
    if status != "running":
        return False
    updated_at = _parse_iso_ts(str(judge_job.get("updated_at", "") or ""))
    if updated_at is None:
        return True
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated_at).total_seconds() < float(max(1, int(grace_seconds or 1)))


def _reconcile_orphan_generate_tasks(tenant_id: str, rows: dict[str, dict[str, Any]]) -> None:
    """Mark persisted pending/running generate tasks as failed when no in-memory worker exists."""
    with GEN_TASK_LOCK:
        live_ids = {
            str(t.get("task_id", ""))
            for t in GEN_TASKS.values()
            if str(t.get("tenant_id", "")) == tenant_id and str(t.get("task_id", ""))
        }
    updates: list[dict[str, Any]] = []
    bank_stats = _build_bank_task_recovery_stats(tenant_id)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    for tid, task in list(rows.items()):
        if not isinstance(task, dict):
            continue
        status = str(task.get("status", "") or "")
        # Keep queued tasks intact; only stale running tasks are orphan-sensitive.
        if status != "running":
            continue
        if tid in live_ids:
            continue
        if _is_generate_run_still_active(tenant_id, str(task.get("run_id", "") or "")):
            continue
        if not (
            _is_orphan_reconcile_due(task, now_dt, _ORPHAN_GEN_GRACE_SECONDS)
            or _is_unowned_running_task_stale(task, now_dt, _ORPHAN_GEN_UNOWNED_RUNNING_SECONDS)
        ):
            continue
        patched = _apply_gen_task_bank_recovery(dict(task), bank_stats)
        errs = [str(x).strip() for x in (patched.get("errors") or []) if str(x).strip()]
        if _ORPHAN_GEN_TASK_MSG not in errs:
            errs.append(_ORPHAN_GEN_TASK_MSG)
        patched["status"] = "failed"
        patched["ended_at"] = str(patched.get("ended_at", "") or now)
        patched["updated_at"] = now
        patched["errors"] = errs
        patched["error_count"] = len(errs)
        rows[tid] = patched
        updates.append(patched)
    for patched in updates:
        _persist_gen_task(tenant_id, patched)
        # Ensure orphan-reconciled failed tasks are visible in QA runs as selectable run_id rows.
        _persist_failed_task_qa_run(
            tenant_id,
            patched,
            reason=_ORPHAN_GEN_TASK_MSG,
            started_at=str(patched.get("started_at", "") or ""),
            ended_at=str(patched.get("ended_at", "") or now),
        )


def _reconcile_orphan_judge_tasks(tenant_id: str, rows: dict[str, dict[str, Any]]) -> None:
    """Recover persisted pending/running judge tasks when no in-memory worker exists."""
    def _recover_orphan_judge_task(task: dict[str, Any], now: str) -> dict[str, Any] | None:
        run_id = str(task.get("run_id", "") or "").strip()
        if not run_id:
            return None
        req = task.get("request") if isinstance(task.get("request"), dict) else {}
        requested_ids = req.get("question_ids")
        run, questions, ids_to_run, err = _prepare_judge_run_targets(tenant_id, run_id, requested_ids)
        if err:
            return None
        if not isinstance(run, dict):
            return None
        ordered_ids = [
            str(q.get("question_id", "")).strip()
            for q in questions
            if isinstance(q, dict) and str(q.get("question_id", "")).strip() in ids_to_run
        ]
        done_ids: set[str] = set()
        done_success = 0
        for q in questions:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("question_id", "")).strip()
            if not qid or qid not in ids_to_run:
                continue
            oj = q.get("offline_judge") if isinstance(q.get("offline_judge"), dict) else {}
            has_done = bool(oj) and (
                bool(str(oj.get("decision", "")).strip())
                or bool(str(oj.get("error", "")).strip())
                or bool(oj.get("observability"))
            )
            if not has_done:
                continue
            done_ids.add(qid)
            if not bool(str(oj.get("error", "")).strip()):
                done_success += 1
        remaining_ids = [qid for qid in ordered_ids if qid not in done_ids]
        patched = dict(task)
        patched_req = dict(req)
        patched_req["question_ids"] = remaining_ids
        patched["request"] = patched_req
        errs = [str(x).strip() for x in (patched.get("errors") or []) if str(x).strip()]
        errs = [x for x in errs if x != _ORPHAN_JUDGE_TASK_MSG]
        if _ORPHAN_JUDGE_TASK_RECOVERED_MSG not in errs:
            errs.append(_ORPHAN_JUDGE_TASK_RECOVERED_MSG)
        if remaining_ids:
            patched["status"] = "pending"
            patched["ended_at"] = ""
            patched["errors"] = errs
            patched["current_question_id"] = ""
            patched["judge_count"] = len(done_ids)
            patched["success_count"] = int(done_success)
            patched["progress"] = {"current": int(len(done_ids)), "total": int(len(ordered_ids))}
        else:
            patched["status"] = "completed"
            patched["ended_at"] = str(patched.get("ended_at", "") or now)
            patched["errors"] = errs
            patched["current_question_id"] = ""
            patched["judge_count"] = len(done_ids)
            patched["success_count"] = int(done_success)
            patched["progress"] = {"current": int(len(ordered_ids)), "total": int(len(ordered_ids))}
        patched["updated_at"] = now
        patched["error_count"] = len(patched.get("errors") or [])
        return patched

    with JUDGE_TASK_LOCK:
        live_ids = {
            str(t.get("task_id", ""))
            for t in JUDGE_TASKS.values()
            if str(t.get("tenant_id", "")) == tenant_id and str(t.get("task_id", ""))
        }
    updates: list[dict[str, Any]] = []
    rehydrate: list[dict[str, Any]] = []
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    for tid, task in list(rows.items()):
        if not isinstance(task, dict):
            continue
        status = str(task.get("status", "") or "")
        if tid in live_ids:
            continue
        if status == "pending":
            rehydrate.append(dict(task))
            continue
        # Only running needs orphan reconciliation.
        if status != "running":
            continue
        if _is_judge_run_still_active(tenant_id, str(task.get("run_id", "") or ""), tid, _ORPHAN_JUDGE_GRACE_SECONDS):
            continue
        if not _is_orphan_reconcile_due(task, now_dt, _ORPHAN_JUDGE_GRACE_SECONDS):
            continue
        recovered = _recover_orphan_judge_task(task, now)
        if isinstance(recovered, dict):
            patched = recovered
            if str(patched.get("status", "")).lower() == "pending":
                rehydrate.append(dict(patched))
        else:
            patched = dict(task)
            errs = [str(x).strip() for x in (patched.get("errors") or []) if str(x).strip()]
            if _ORPHAN_JUDGE_TASK_MSG not in errs:
                errs.append(_ORPHAN_JUDGE_TASK_MSG)
            patched["status"] = "failed"
            patched["ended_at"] = str(patched.get("ended_at", "") or now)
            patched["updated_at"] = now
            patched["errors"] = errs
            patched["error_count"] = len(errs)
        rows[tid] = patched
        updates.append(patched)
    if rehydrate:
        with JUDGE_TASK_LOCK:
            for task in rehydrate:
                tid = str(task.get("task_id", "")).strip()
                if not tid:
                    continue
                if str(task.get("tenant_id", "") or "").strip() != tenant_id:
                    continue
                JUDGE_TASKS[tid] = dict(task)
            _prune_judge_task_cache()
    for patched in updates:
        _persist_judge_task(tenant_id, patched)


def _maintenance_tenant_ids() -> list[str]:
    ids: list[str] = []
    for item in list_tenants():
        if not isinstance(item, dict):
            continue
        if not bool(item.get("is_active", True)):
            continue
        tid = str(item.get("tenant_id", "")).strip()
        if tid:
            ids.append(tid)
    return ids


def _reconcile_orphans_once() -> None:
    for tenant_id in _maintenance_tenant_ids():
        gen_rows = _latest_gen_task_rows(tenant_id)
        if gen_rows:
            _reconcile_orphan_generate_tasks(tenant_id, gen_rows)
            for task_id, row in gen_rows.items():
                if not isinstance(row, dict):
                    continue
                patched = _maybe_reconcile_template_task_selection(tenant_id, row)
                if patched != row:
                    _persist_gen_task(tenant_id, patched)
        judge_rows = _latest_rows_by_task_id(_qa_judge_tasks_path(tenant_id))
        if judge_rows:
            _reconcile_orphan_judge_tasks(tenant_id, judge_rows)
            _start_next_judge_task_if_idle(tenant_id)


def _task_maintenance_loop() -> None:
    while True:
        try:
            _reconcile_orphans_once()
        except Exception:
            # Maintenance should never break main API.
            pass
        try:
            threading.Event().wait(float(_TASK_MAINTENANCE_INTERVAL_SECONDS))
        except Exception:
            threading.Event().wait(120.0)


def _ensure_task_maintenance_started() -> None:
    global _MAINTENANCE_STARTED
    if _MAINTENANCE_STARTED:
        return
    with _MAINTENANCE_LOCK:
        if _MAINTENANCE_STARTED:
            return
        t = threading.Thread(target=_task_maintenance_loop, daemon=True)
        t.start()
        _MAINTENANCE_STARTED = True


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
    active_ids = {
        str(x.get("task_id", ""))
        for x in GEN_TASKS.values()
        if str(x.get("status", "")).lower() in {"pending", "running"}
    }
    keep_ids.update(active_ids)
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
            "generation_mode": _normalize_generation_mode(body.get("generation_mode", "随机")),
            "difficulty": str(body.get("difficulty", "随机")),
            "template_id": str(body.get("template_id", "")).strip(),
            "template_name": str(body.get("template_name", "")).strip(),
            "persist_to_bank": bool(body.get("persist_to_bank", body.get("save_to_bank", True))),
            "save_to_bank": bool(body.get("persist_to_bank", body.get("save_to_bank", True))),
            "slice_ids": [int(x) for x in (body.get("slice_ids") or []) if str(x).isdigit()],
            "planned_slice_ids": [int(x) for x in (body.get("planned_slice_ids") or []) if str(x).isdigit()],
            "planned_slots": [
                {
                    "slice_id": int(slot.get("slice_id", 0) or 0),
                    "route_prefix": str(slot.get("route_prefix", "") or "").strip(),
                    "mastery": str(slot.get("mastery", "") or "").strip(),
                    "_global_target_index": int(slot.get("_global_target_index", 0) or 0),
                }
                for slot in (body.get("planned_slots") or [])
                if isinstance(slot, dict) and str(slot.get("slice_id", "")).isdigit()
            ],
            "material_version_id": str(body.get("material_version_id", "")).strip(),
            "resume_from_task_id": str(body.get("resume_from_task_id", "")).strip(),
            "resume_done_count": int(body.get("resume_done_count", 0) or 0),
            "resume_total_count": int(body.get("resume_total_count", 0) or 0),
            "resume_remaining_count": int(body.get("resume_remaining_count", 0) or 0),
            "resume_note": str(body.get("resume_note", "") or "").strip(),
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
        "current_subcall": {},
        "subtasks": [],
        "repair_rounds": [],
        "slice_failure_stats": [],
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
        # When writer/fixer emits a fresh final_json before the next critic verdict,
        # clear stale critic fields from older live snapshots so the UI does not show
        # a previous-round reject reason on the new draft.
        if item.get("final_json") is not None:
            incoming_critic = item.get("critic_result")
            has_incoming_critic_details = "critic_details" in item
            incoming_critic_is_empty = (
                incoming_critic is None
                or (isinstance(incoming_critic, dict) and not incoming_critic)
            )
            if incoming_critic_is_empty and not has_incoming_critic_details:
                merged.pop("critic_details", None)
                merged["critic_result"] = {}
        steps = []
        seen: set[str] = set()
        for s in list(prev.get("steps") or []) + list(item.get("steps") or []):
            if not isinstance(s, dict):
                continue
            # Include run_id so post-reroute steps (run_id>=1) are never deduped with first run
            run_id = s.get("run_id", 0)
            key = f"{run_id}|{s.get('seq','')}|{s.get('node','')}|{s.get('message','')}|{s.get('detail','')}"
            if key in seen:
                continue
            seen.add(key)
            steps.append(s)
        merged["steps"] = steps
        by_index[idx] = merged
    return [by_index[k] for k in sorted(by_index.keys())]


def _is_task_cancelled(task_id: str) -> bool:
    """True if this task (or its parent task) has been requested to cancel."""
    with GEN_TASK_LOCK:
        t = GEN_TASKS.get(str(task_id or ""))
        if not isinstance(t, dict):
            return False
        if bool(t.get("cancel_requested")):
            return True
        parent_task_id = str(t.get("parent_task_id", "") or "").strip()
        if parent_task_id and parent_task_id != str(task_id or "").strip():
            parent = GEN_TASKS.get(parent_task_id)
            if isinstance(parent, dict):
                return bool(parent.get("cancel_requested"))
        return False


def _resolve_task_snapshot_for_policy(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    """
    读取任务快照（内存优先，其次快照文件、持久化 JSONL），用于策略查询/动态开关。
    """
    tid = str(task_id or "").strip()
    if not tid:
        return None
    with GEN_TASK_LOCK:
        t = GEN_TASKS.get(tid)
        if isinstance(t, dict) and str(t.get("tenant_id", "")) == tenant_id:
            return _task_snapshot(t)
    snap = _read_gen_task_snapshot_file(tenant_id, tid)
    if isinstance(snap, dict):
        return snap
    return _read_persisted_task(tenant_id, tid)


def _is_task_auto_bank_enabled(tenant_id: str, task_id: str, default_enabled: bool) -> bool:
    """
    运行时判断任务是否应自动入库。
    - 任务本身可切换；
    - 子任务若存在 parent_task_id，优先跟随父任务开关，确保模板并发子任务即时生效。
    """
    tid = str(task_id or "").strip()
    if not tid:
        return bool(default_enabled)
    task = _resolve_task_snapshot_for_policy(tenant_id, tid)
    if not isinstance(task, dict):
        return bool(default_enabled)
    req = task.get("request") if isinstance(task.get("request"), dict) else {}
    parent_task_id = str(req.get("parent_task_id", "") or task.get("parent_task_id", "") or "").strip()
    if parent_task_id and parent_task_id != tid:
        parent = _resolve_task_snapshot_for_policy(tenant_id, parent_task_id)
        if isinstance(parent, dict):
            preq = parent.get("request") if isinstance(parent.get("request"), dict) else {}
            return bool(preq.get("persist_to_bank", preq.get("save_to_bank", default_enabled)))
    return bool(req.get("persist_to_bank", req.get("save_to_bank", default_enabled)))


def _sync_parent_subtask_stats_from_child(
    tenant_id: str,
    child_task_id: str,
    child_task: dict[str, Any],
    *,
    now_iso: str,
) -> None:
    """
    将模板/并发子任务在出题过程中的 generated_count、saved_count 写回父任务 subtasks 对应行。
    父任务表格原仅在分片 HTTP 返回后更新，导致进行中子任务长期显示 0/0；须在每次子任务 live 更新后同步。

    调用方须已持有 GEN_TASK_LOCK，且 child_task 为内存中已 apply patch 后的任务快照。
    """
    parent_id = str((child_task or {}).get("parent_task_id", "") or "").strip()
    if not parent_id:
        return
    parent = GEN_TASKS.get(parent_id)
    if not isinstance(parent, dict) or str(parent.get("tenant_id", "")) != tenant_id:
        return
    subtasks = parent.get("subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        return
    cid = str(child_task_id or "").strip()
    if not cid:
        return
    gen = int((child_task or {}).get("generated_count", 0) or 0)
    sav = int((child_task or {}).get("saved_count", 0) or 0)
    err = int((child_task or {}).get("error_count", 0) or 0)
    child_status = str((child_task or {}).get("status", "") or "").strip().lower()
    run_id = str((child_task or {}).get("run_id", "") or "").strip()
    matched = False
    for sub in subtasks:
        if not isinstance(sub, dict):
            continue
        if str(sub.get("task_id", "") or "").strip() != cid:
            continue
        matched = True
        sub["generated_count"] = gen
        sub["saved_count"] = sav
        sub["error_count"] = err
        if run_id:
            sub["run_id"] = run_id
        if child_status in {"pending", "running"}:
            prev = str(sub.get("status", "") or "").strip().lower()
            if prev not in {"failed", "cancelled"}:
                sub["status"] = "running"
                sub["ended_at"] = ""
        break
    if matched:
        parent["updated_at"] = now_iso


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
        _sync_parent_subtask_stats_from_child(tenant_id, str(task_id or ""), task, now_iso=now)


def _mark_live_final_json_stale(question_trace: dict[str, Any], append_step: Callable[..., None] | None = None) -> None:
    """When reroute starts a new round, mark previous live final_json as stale for UI/debugging."""
    if not isinstance(question_trace, dict):
        return
    final_json = question_trace.get("final_json")
    if not isinstance(final_json, dict) or not final_json:
        return
    if bool(question_trace.get("final_json_expired")):
        return
    question_trace["final_json_expired"] = True
    question_trace["final_json_expired_at"] = datetime.now(timezone.utc).isoformat()
    question_trace["final_json_expired_run_id"] = int(question_trace.get("active_run_id", 0) or 0)
    if callable(append_step):
        append_step(
            "上一轮定稿已过期",
            node="system",
            level="warning",
            detail="已进入新一轮重试，上一轮 writer/fixer 预览仅供回溯，不再代表当前轮次内容。",
        )


def _persist_gen_task(tenant_id: str, task: dict[str, Any]) -> None:
    _append_jsonl(_qa_gen_tasks_path(tenant_id), task)
    _append_jsonl(_qa_gen_tasks_summary_path(tenant_id), _build_gen_task_summary(task))


def _persist_gen_task_snapshot_file(tenant_id: str, task: dict[str, Any]) -> None:
    tid = str((task or {}).get("task_id", "") or "").strip()
    if not tid:
        return
    path = _qa_gen_task_snapshot_path(tenant_id, tid)
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False)
    tmp_path.replace(path)


def _read_gen_task_snapshot_file(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    tid = str(task_id or "").strip()
    if not tid:
        return None
    path = _qa_gen_task_snapshot_path(tenant_id, tid)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _persist_live_task_snapshot(tenant_id: str, task_id: str) -> None:
    """Best-effort persist current in-memory task snapshot for crash/restart recovery."""
    with GEN_TASK_LOCK:
        task = GEN_TASKS.get(str(task_id or ""))
        if not isinstance(task, dict):
            return
        if str(task.get("tenant_id", "")) != tenant_id:
            return
        snap = _task_snapshot(task)
    _persist_gen_task(tenant_id, snap)
    _persist_gen_task_snapshot_file(tenant_id, snap)


def _get_latest_gen_task_snapshot(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    tid = str(task_id or "").strip()
    if not tid:
        return None
    live_snap: dict[str, Any] | None = None
    with GEN_TASK_LOCK:
        live = GEN_TASKS.get(tid)
        if isinstance(live, dict) and str(live.get("tenant_id", "")) == tenant_id:
            live_snap = _task_snapshot(live)
    live_status = str((live_snap or {}).get("status", "") or "").strip().lower()
    if isinstance(live_snap, dict) and live_status in {"pending", "running"}:
        return live_snap
    file_snap = _read_gen_task_snapshot_file(tenant_id, tid)
    persisted = _read_persisted_task(tenant_id, tid)
    persisted_snap = dict(persisted) if isinstance(persisted, dict) else None
    if isinstance(file_snap, dict) and isinstance(persisted_snap, dict):
        file_snap = _pick_newer_terminal_task_snapshot(file_snap, persisted_snap) or file_snap
    if isinstance(file_snap, dict):
        if isinstance(live_snap, dict):
            live_ts = _parse_iso_ts(str(live_snap.get("updated_at", "") or "")) or _parse_iso_ts(
                str(live_snap.get("started_at", "") or "")
            )
            file_ts = _parse_iso_ts(str(file_snap.get("updated_at", "") or "")) or _parse_iso_ts(
                str(file_snap.get("started_at", "") or "")
            )
            if live_ts and file_ts:
                return live_snap if live_ts >= file_ts else file_snap
            return live_snap
        return file_snap
    if isinstance(live_snap, dict) and isinstance(persisted_snap, dict):
        live_ts = _parse_iso_ts(str(live_snap.get("updated_at", "") or "")) or _parse_iso_ts(
            str(live_snap.get("started_at", "") or "")
        )
        persisted_ts = _parse_iso_ts(str(persisted_snap.get("updated_at", "") or "")) or _parse_iso_ts(
            str(persisted_snap.get("started_at", "") or "")
        )
        if live_ts and persisted_ts:
            return persisted_snap if persisted_ts >= live_ts else live_snap
        # If timestamps are absent/unparseable, prefer persisted snapshot to avoid stale in-memory state blocking resume.
        return persisted_snap
    if isinstance(live_snap, dict):
        return live_snap
    if isinstance(persisted_snap, dict):
        return persisted_snap
    return None


def _pick_newer_terminal_task_snapshot(
    primary_task: dict[str, Any] | None,
    candidate_task: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """
    Prefer candidate when primary looks active (pending/running) but candidate is a newer terminal snapshot.
    This prevents stale live snapshot files from masking persisted failed/cancelled/completed status.
    """
    if not isinstance(primary_task, dict):
        return dict(candidate_task) if isinstance(candidate_task, dict) else primary_task
    if not isinstance(candidate_task, dict):
        return dict(primary_task)
    primary_status = str(primary_task.get("status", "") or "").strip().lower()
    candidate_status = str(candidate_task.get("status", "") or "").strip().lower()
    primary_active = primary_status in {"pending", "running"}
    candidate_terminal = candidate_status in {"completed", "failed", "cancelled", "canceled"}
    if not (primary_active and candidate_terminal):
        return dict(primary_task)

    def _task_ts(task: dict[str, Any]) -> datetime | None:
        for key in ("updated_at", "ended_at", "started_at", "created_at"):
            ts = _parse_iso_ts(str(task.get(key, "") or ""))
            if isinstance(ts, datetime):
                return ts
        return None

    primary_ts = _task_ts(primary_task)
    candidate_ts = _task_ts(candidate_task)
    if candidate_ts and primary_ts:
        return dict(candidate_task) if candidate_ts >= primary_ts else dict(primary_task)
    if candidate_ts and not primary_ts:
        return dict(candidate_task)
    return dict(primary_task)


def _build_resume_task_body_from_source(
    tenant_id: str,
    source_task: dict[str, Any],
    *,
    force_remaining: int | None = None,
    inplace: bool = False,
) -> dict[str, Any] | None:
    req = source_task.get("request") if isinstance(source_task.get("request"), dict) else {}
    total = int(req.get("num_questions", 0) or 0)
    if total <= 0:
        return None
    is_template_task = bool(str(req.get("template_id", "") or "").strip())
    generated_count = int(source_task.get("generated_count", 0) or 0)
    saved_count = int(source_task.get("saved_count", 0) or 0)
    done = max(generated_count, saved_count)
    remain = int(force_remaining if force_remaining is not None else (total - done))
    if remain <= 0 and not is_template_task:
        return None
    used_slice_counts = _build_task_saved_slice_counts_from_bank(
        tenant_id,
        str(source_task.get("task_name", "") or req.get("task_name", "") or ""),
    )
    if not used_slice_counts:
        for tr in (source_task.get("process_trace") or []):
            if not isinstance(tr, dict) or not bool(tr.get("saved")):
                continue
            try:
                sid = int(tr.get("slice_id"))
            except (TypeError, ValueError):
                continue
            if sid > 0:
                used_slice_counts[sid] = int(used_slice_counts.get(sid, 0) or 0) + 1
    task_name_base = str(source_task.get("task_name", "") or req.get("task_name", "") or "").strip() or "续跑任务"
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    task_name = task_name_base if inplace else f"{task_name_base}-续跑-{suffix}"
    return {
        "task_name": task_name,
        "gen_scope_mode": str(req.get("gen_scope_mode", "custom") or "custom"),
        "num_questions": int(max(remain, 0)),
        "question_type": str(req.get("question_type", "随机") or "随机"),
        "generation_mode": _normalize_generation_mode(req.get("generation_mode", "随机")),
        "difficulty": str(req.get("difficulty", "随机") or "随机"),
        "template_id": str(req.get("template_id", "") or "").strip(),
        "template_name": str(req.get("template_name", "") or "").strip(),
        "persist_to_bank": bool(req.get("persist_to_bank", req.get("save_to_bank", True))),
        "save_to_bank": bool(req.get("persist_to_bank", req.get("save_to_bank", True))),
        "slice_ids": [int(x) for x in (req.get("slice_ids") or []) if str(x).isdigit()],
        "material_version_id": str(req.get("material_version_id", "") or source_task.get("material_version_id", "") or "").strip(),
        "resume_from_task_id": str(source_task.get("task_id", "") or "").strip(),
        "resume_done_count": int(done),
        "resume_total_count": int(total),
        "resume_remaining_count": int(max(remain, 0)),
        "resume_note": f"断点续跑：复用原任务已成功 {done} 题，补齐剩余 {max(remain, 0)} 题",
        "resume_inplace": bool(inplace),
        "resume_original_total": int(total),
        "used_slice_counts": used_slice_counts,
    }


def _prepare_inplace_resume_task(tenant_id: str, source_task: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    req = source_task.get("request") if isinstance(source_task.get("request"), dict) else {}
    task = dict(source_task)
    task["tenant_id"] = tenant_id
    task["task_name"] = str(source_task.get("task_name", "") or req.get("task_name", "") or "").strip()
    task["status"] = "pending"
    task["ended_at"] = ""
    task["updated_at"] = now
    task["cancel_requested"] = False
    return task


def _create_internal_child_gen_task(
    tenant_id: str,
    system_user: str,
    parent_task: dict[str, Any],
    body: dict[str, Any],
    *,
    child_suffix: str,
    child_kind: str,
) -> dict[str, Any]:
    parent_task_name = str(parent_task.get("task_name", "") or "").strip()
    child_name = f"{parent_task_name}#{child_suffix}" if parent_task_name else child_suffix
    child_body = {
        **dict(body or {}),
        "task_name": child_name,
    }
    child_task = _make_gen_task(tenant_id, system_user, child_body)
    now = datetime.now(timezone.utc).isoformat()
    child_task["task_name"] = child_name
    child_task["status"] = "running"
    child_task["started_at"] = now
    child_task["updated_at"] = now
    child_task["progress"] = {
        "current": 0,
        "total": max(1, int(child_body.get("num_questions", 1) or 1)),
    }
    child_task["parent_task_id"] = str(parent_task.get("task_id", "") or "")
    child_task["parent_task_name"] = parent_task_name
    child_task["child_kind"] = str(child_kind or "child")
    req = child_task.get("request") if isinstance(child_task.get("request"), dict) else {}
    req["parent_task_id"] = str(parent_task.get("task_id", "") or "")
    req["parent_task_name"] = parent_task_name
    req["child_kind"] = str(child_kind or "child")
    child_task["request"] = req
    with GEN_TASK_LOCK:
        GEN_TASKS[str(child_task.get("task_id", ""))] = _task_snapshot(child_task)
        _prune_task_cache()
    _persist_gen_task(tenant_id, child_task)
    return child_task


def _finalize_internal_child_gen_task(
    tenant_id: str,
    child_task_id: str,
    *,
    resp: Any = None,
    explicit_error: str = "",
) -> None:
    task = _get_latest_gen_task_snapshot(tenant_id, child_task_id)
    if not isinstance(task, dict):
        return
    payload: dict[str, Any] = {}
    status_code = int(getattr(resp, "status_code", 200) or 200) if resp is not None else 0
    if resp is not None:
        try:
            payload = resp.get_json(silent=True) or {}
        except Exception:
            payload = {}
    existing_errors = [str(x) for x in (task.get("errors") or []) if str(x).strip()]
    payload_errors = [str(x) for x in ((payload.get("errors") if isinstance(payload, dict) else []) or []) if str(x).strip()]
    merged_errors = existing_errors + payload_errors
    if explicit_error:
        merged_errors.append(str(explicit_error).strip())
    ended_at = datetime.now(timezone.utc).isoformat()
    generated_count = int(task.get("generated_count", 0) or 0)
    saved_count = int(task.get("saved_count", 0) or 0)
    process_trace = [x for x in (task.get("process_trace") or []) if isinstance(x, dict)]
    items = [x for x in (task.get("items") or []) if isinstance(x, dict)]
    if isinstance(payload, dict):
        generated_count = max(generated_count, int(payload.get("generated_count", 0) or 0))
        saved_count = max(saved_count, int(payload.get("saved_count", 0) or 0))
        payload_trace = [x for x in (payload.get("process_trace") or []) if isinstance(x, dict)]
        if payload_trace:
            process_trace = _merge_task_trace_by_index(process_trace, payload_trace)
        payload_items = [x for x in (payload.get("items") or []) if isinstance(x, dict)]
        if payload_items:
            items = payload_items
    success = bool(generated_count > 0 or saved_count > 0)
    payload_success = bool((payload or {}).get("success", False)) if isinstance(payload, dict) else False
    payload_partial = bool((payload or {}).get("partial_completed", False)) if isinstance(payload, dict) else False
    cancelled = bool((payload or {}).get("cancelled")) if isinstance(payload, dict) else False
    req = task.get("request") if isinstance(task.get("request"), dict) else {}
    progress = task.get("progress") if isinstance(task.get("progress"), dict) else {}
    expected_total = max(
        int(progress.get("total", 0) or 0),
        int(req.get("num_questions", 0) or 0),
        0,
    )
    target_not_met = expected_total > 0 and int(generated_count) < int(expected_total)
    has_errors = len(merged_errors) > 0
    # 出题循环会把每次失败尝试都 append 到 errors，重试成功后也不会回滚；若已达成计划题量，不应仅因历史错误标 partial。
    target_fully_met = expected_total > 0 and int(generated_count) >= int(expected_total)
    if cancelled:
        status = "cancelled"
    elif explicit_error or status_code >= 400 or (not payload_success and not success):
        status = "failed"
    elif target_fully_met:
        status = "completed"
    elif payload_partial or target_not_met or has_errors:
        status = "partial"
    else:
        status = "completed"
    patch = {
        "status": status,
        "ended_at": ended_at,
        "updated_at": ended_at,
        "run_id": str((payload or {}).get("run_id", "") or task.get("run_id", "") or ""),
        "items": items,
        "process_trace": process_trace,
        "generated_count": generated_count,
        "saved_count": saved_count,
        "errors": merged_errors,
        "error_count": len(merged_errors),
        "progress": {
            "current": generated_count,
            "total": max(expected_total, generated_count, 1),
        },
        "current_subcall": {},
    }
    _update_task_live(tenant_id, child_task_id, patch)
    _persist_live_task_snapshot(tenant_id, child_task_id)
    latest = _get_latest_gen_task_snapshot(tenant_id, child_task_id)
    if isinstance(latest, dict) and status == "failed":
        _persist_failed_task_qa_run(
            tenant_id,
            latest,
            reason=explicit_error or (merged_errors[-1] if merged_errors else "子任务失败"),
            started_at=str(latest.get("started_at", "") or ended_at),
            ended_at=ended_at,
        )


def _rebuild_template_resume_gap_plan(
    tenant_id: str,
    source_task: dict[str, Any],
    resume_body: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """
    For template tasks, rebuild full template plan by original total count, then subtract
    already saved questions to obtain remaining planned_slice_ids for resume.
    """
    try:
        req = source_task.get("request") if isinstance(source_task.get("request"), dict) else {}
        template_id = str(req.get("template_id", "") or "").strip()
        if not template_id:
            return resume_body, None
        template = _get_gen_template(tenant_id, template_id)
        if not isinstance(template, dict):
            return resume_body, "模板不存在，无法按占比续跑"

        effective_material_version_request = str(
            req.get("material_version_id", "") or source_task.get("material_version_id", "") or ""
        ).strip()
        material_version_id = _resolve_material_version_id(tenant_id, effective_material_version_request)
        if not material_version_id:
            return resume_body, "教材版本不存在，无法按占比续跑"

        kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
        kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
        if not kb_items:
            return resume_body, "切片为空，无法按占比续跑"

        review_store = _load_slice_review_for_material(tenant_id, material_version_id)
        approved_ids = {
            int(k) for k, v in review_store.items()
            if str(k).isdigit() and isinstance(v, dict) and v.get("review_status") == "approved"
        }
        if not approved_ids:
            return resume_body, "无 approved 切片，无法按占比续跑"

        selected_ids = set()
        for sid in (req.get("slice_ids") or []):
            try:
                selected_ids.add(int(sid))
            except (TypeError, ValueError):
                continue
        candidate_ids = sorted((selected_ids & approved_ids) if selected_ids else approved_ids)
        if not candidate_ids:
            return resume_body, "模板范围内无可用切片，无法按占比续跑"

        history_path = str(_resolve_history_path_for_material(tenant_id, material_version_id))
        mapping_path = str(_resolve_mapping_path_for_material(tenant_id, material_version_id) or tenant_mapping_path(tenant_id))
        mapping_review_path = str(tenant_mapping_review_path(tenant_id))
        retriever = _get_cached_retriever(
            tenant_id=tenant_id,
            kb_path=str(kb_file),
            history_path=history_path,
            mapping_path=mapping_path,
            mapping_review_path=mapping_review_path,
        )
        candidate_ids = [sid for sid in candidate_ids if 0 <= sid < len(retriever.kb_data)]
        question_type = str(req.get("question_type", "随机") or "随机")
        candidate_ids, _ = _filter_candidate_ids_by_question_type(retriever, candidate_ids, question_type)
        blocked_slice_ids = _blocked_slice_ids_for_material(tenant_id, material_version_id)
        candidate_ids = [sid for sid in candidate_ids if sid not in blocked_slice_ids]
        if not candidate_ids:
            return resume_body, "模板题型与切片冲突，无法按占比续跑"

        candidate_slices = []
        for sid in candidate_ids:
            kb_item = retriever.kb_data[sid]
            candidate_slices.append(
                {
                    "slice_id": sid,
                    "path": str(kb_item.get("完整路径", "")).strip(),
                    "mastery": str(kb_item.get("掌握程度", "")).strip(),
                }
            )

        total_original = int(req.get("num_questions", 0) or 0)
        if total_original <= 0:
            return resume_body, None
        full_plan = _build_generation_template_plan(
            question_count=total_original,
            template=template,
            candidate_slices=candidate_slices,
        )
        full_slots = [
            slot for slot in (full_plan.get("planned_slots") or [])
            if isinstance(slot, dict) and int(slot.get("slice_id", -1)) in candidate_ids
        ]
        if not full_slots:
            return resume_body, None

        candidate_lookup = _build_slice_candidate_lookup(
            candidate_slices,
            template_route_rules=(template or {}).get("route_rules") if template else None,
        )
        report = _analyze_template_parallel_result(
            planned_slots=full_slots,
            process_trace=[x for x in (source_task.get("process_trace") or []) if isinstance(x, dict)],
            candidate_lookup=candidate_lookup,
        )
        invalid_targets = [
            int(item.get("target_index", 0) or 0)
            for item in (report.get("invalid_targets") or [])
            if isinstance(item, dict) and int(item.get("target_index", 0) or 0) > 0
        ]
        remaining_target_indexes = sorted(
            {
                *[int(x) for x in (report.get("missing_target_indexes") or []) if int(x) > 0],
                *invalid_targets,
            }
        )
        if not remaining_target_indexes and bool(report.get("ok")):
            return resume_body, None

        remaining_slots: list[dict[str, Any]] = [
            {**dict(full_slots[idx - 1]), "_global_target_index": idx}
            for idx in remaining_target_indexes
            if 1 <= idx <= len(full_slots) and isinstance(full_slots[idx - 1], dict)
        ]
        remaining_plan = [int(slot.get("slice_id")) for slot in remaining_slots if str(slot.get("slice_id", "")).isdigit()]
        if remaining_plan:
            patched = dict(resume_body)
            patched["planned_slice_ids"] = remaining_plan
            patched["planned_slots"] = remaining_slots
            patched["num_questions"] = int(len(remaining_plan))
            patched["resume_remaining_count"] = int(len(remaining_plan))
            patched["resume_note"] = f"模板断点续跑：重建模板缺口 {len(remaining_plan)} 题"
            return patched, None
        return resume_body, None
    except Exception as e:
        return resume_body, f"模板占比缺口重建失败: {e}"


def _resolve_template_parallel_context(
    tenant_id: str,
    request_body: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        template_id = str(request_body.get("template_id", "") or "").strip()
        if not template_id:
            return None, None
        template = _get_gen_template(tenant_id, template_id)
        if not isinstance(template, dict):
            return None, "模板不存在，无法进行模板并发分片"

        material_version_id = _resolve_material_version_id(
            tenant_id,
            str(request_body.get("material_version_id", "") or "").strip(),
        )
        if not material_version_id:
            return None, "教材版本不存在，无法进行模板并发分片"

        kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
        kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
        if not kb_items:
            return None, "切片为空，无法进行模板并发分片"

        review_store = _load_slice_review_for_material(tenant_id, material_version_id)
        approved_ids = {
            int(k) for k, v in review_store.items()
            if str(k).isdigit() and isinstance(v, dict) and v.get("review_status") == "approved"
        }
        if not approved_ids:
            return None, "无 approved 切片，无法进行模板并发分片"

        selected_ids = set()
        for sid in (request_body.get("slice_ids") or []):
            try:
                selected_ids.add(int(sid))
            except (TypeError, ValueError):
                continue
        candidate_ids = sorted((selected_ids & approved_ids) if selected_ids else approved_ids)
        if not candidate_ids:
            return None, "模板范围内没有 approved 切片"

        if not kb_file:
            return None, "当前城市没有可用切片文件"
        history_path = str(_resolve_history_path_for_material(tenant_id, material_version_id))
        mapping_path = str(_resolve_mapping_path_for_material(tenant_id, material_version_id) or tenant_mapping_path(tenant_id))
        mapping_review_path = str(tenant_mapping_review_path(tenant_id))
        retriever = _get_cached_retriever(
            tenant_id=tenant_id,
            kb_path=str(kb_file),
            history_path=history_path,
            mapping_path=mapping_path,
            mapping_review_path=mapping_review_path,
        )
        candidate_ids = [sid for sid in candidate_ids if 0 <= sid < len(retriever.kb_data)]
        if not candidate_ids:
            return None, "审核记录与当前切片版本不一致，请重新审核切片后再出题"

        question_type = str(request_body.get("question_type", "随机") or "随机")
        candidate_ids, _ = _filter_candidate_ids_by_question_type(retriever, candidate_ids, question_type)
        if not candidate_ids:
            return None, "模板题型在当前切片范围内均被禁止，无法并发分片"

        blocked_slice_ids = _blocked_slice_ids_for_material(tenant_id, material_version_id)
        candidate_ids = [sid for sid in candidate_ids if sid not in blocked_slice_ids]
        if not candidate_ids:
            return None, "当前范围内切片均已因累计失败被禁用，请先修复切片"

        candidate_slices = []
        for sid in candidate_ids:
            kb_item = retriever.kb_data[sid]
            candidate_slices.append(
                {
                    "slice_id": sid,
                    "path": str(kb_item.get("完整路径", "")).strip(),
                    "mastery": str(kb_item.get("掌握程度", "")).strip(),
                }
            )
        candidate_lookup = _build_slice_candidate_lookup(
            candidate_slices,
            template_route_rules=(template or {}).get("route_rules") if template else None,
        )
        question_count = int(template.get("question_count", request_body.get("num_questions", 0)) or request_body.get("num_questions", 0) or 0)
        if question_count <= 0:
            return None, "模板题量必须大于0"
        precheck_report = _build_template_reachability_report(
            question_count=question_count,
            template=template,
            candidate_slices=candidate_slices,
        )
        try:
            template_plan = _build_generation_template_plan(
                question_count=question_count,
                template=template,
                candidate_slices=candidate_slices,
            )
        except ValueError as e:
            return None, _format_template_reachability_error(str(e), precheck_report)
        planned_slots = [
            slot for slot in (template_plan.get("planned_slots") or [])
            if isinstance(slot, dict) and int(slot.get("slice_id", -1)) in candidate_ids
        ]
        if not planned_slots:
            return None, "模板计划为空，无法进行并发分片"
        return {
            "template": template,
            "template_plan": template_plan,
            "planned_slots": planned_slots,
            "planned_slice_ids": [int(slot.get("slice_id")) for slot in planned_slots],
            # Global candidate pool under current template constraints.
            # Child shard should use this pool to do in-shard补位，而不是只在分片初始slice里打转。
            "candidate_slice_ids": [int(sid) for sid in candidate_ids],
            "candidate_lookup": candidate_lookup,
            "material_version_id": material_version_id,
        }, None
    except Exception as e:
        return None, f"模板并发计划构建失败: {e}"


def _compute_template_parallel_min_shard_size(template_plan: dict[str, Any], total: int) -> int:
    route_breakdown = template_plan.get("route_breakdown") if isinstance(template_plan, dict) else []
    mastery_counts = template_plan.get("mastery_counts") if isinstance(template_plan, dict) else {}
    active_route_count = sum(
        1 for row in (route_breakdown or [])
        if isinstance(row, dict) and int(row.get("count", 0) or 0) > 0
    )
    active_mastery_count = sum(
        1 for key in GEN_TEMPLATE_MASTERIES
        if int((mastery_counts or {}).get(key, 0) or 0) > 0
    )
    active_combo_count = 0
    for row in (route_breakdown or []):
        if not isinstance(row, dict):
            continue
        for item in (row.get("mastery_breakdown") or []):
            if isinstance(item, dict) and int(item.get("count", 0) or 0) > 0:
                active_combo_count += 1
    min_size = max(4, active_mastery_count)
    return min(max(1, min_size), max(1, int(total or 1)))


def _split_template_slots_for_parallel(
    planned_slots: list[dict[str, Any]],
    shard_count: int,
) -> list[list[dict[str, Any]]]:
    if shard_count <= 1:
        normalized: list[dict[str, Any]] = []
        for idx, slot in enumerate(planned_slots or [], start=1):
            global_idx = int(slot.get("_global_target_index", 0) or 0) if isinstance(slot, dict) else 0
            if global_idx <= 0:
                global_idx = idx
            normalized.append({**slot, "_global_target_index": global_idx})
        return [normalized]
    shards: list[list[dict[str, Any]]] = [[] for _ in range(shard_count)]
    for local_idx, slot in enumerate(planned_slots or [], start=1):
        global_idx = int(slot.get("_global_target_index", 0) or 0) if isinstance(slot, dict) else 0
        if global_idx <= 0:
            global_idx = local_idx
        shard_idx = (local_idx - 1) % shard_count
        shards[shard_idx].append({**slot, "_global_target_index": global_idx})
    return [shard for shard in shards if shard]


def _validate_template_parallel_result(
    *,
    planned_slots: list[dict[str, Any]],
    process_trace: list[dict[str, Any]],
    candidate_lookup: dict[str, Any] | None,
) -> tuple[bool, str]:
    report = _analyze_template_parallel_result(
        planned_slots=planned_slots,
        process_trace=process_trace,
        candidate_lookup=candidate_lookup,
    )
    if report.get("ok"):
        return True, ""
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    return False, "；".join([str(x).strip() for x in issues if str(x).strip()][:6]) or "模板整体校验失败"


def _analyze_template_parallel_result(
    *,
    planned_slots: list[dict[str, Any]],
    process_trace: list[dict[str, Any]],
    candidate_lookup: dict[str, Any] | None,
) -> dict[str, Any]:
    slot_count = len([slot for slot in (planned_slots or []) if isinstance(slot, dict)])
    if slot_count <= 0:
        return {"ok": False, "issues": ["模板计划为空，无法校验"], "missing_target_indexes": [], "invalid_targets": []}
    def _is_business_passed_trace(row: dict[str, Any]) -> bool:
        """
        模板达标统计口径：业务通过（critic通过/白名单通过），与是否入库解耦。
        """
        if not isinstance(row, dict):
            return False
        final_json = row.get("final_json")
        if not isinstance(final_json, dict) or not final_json:
            return False
        if bool(row.get("saved")) or bool(row.get("saved_with_issues")):
            return True
        critic_result = row.get("critic_result") if isinstance(row.get("critic_result"), dict) else {}
        if critic_result.get("passed") is True:
            return True
        for step in (row.get("steps") or []):
            if not isinstance(step, dict):
                continue
            if str(step.get("node", "")).strip() == "critic" and str(step.get("message", "")).strip() == "审核通过":
                return True
        return False

    passed_traces = [
        row for row in (process_trace or [])
        if isinstance(row, dict) and _is_business_passed_trace(row)
    ]
    by_target: dict[int, dict[str, Any]] = {}
    issues: list[str] = []
    invalid_targets: list[dict[str, Any]] = []
    for row in passed_traces:
        try:
            target_idx = int(row.get("target_index", 0) or 0)
        except (TypeError, ValueError):
            target_idx = 0
        if target_idx <= 0 or target_idx > slot_count:
            issues.append(f"存在越界 target_index：{target_idx}")
            invalid_targets.append({"target_index": target_idx, "reason": "out_of_range", "trace": row})
            continue
        if target_idx in by_target:
            issues.append(f"模板位次重复写入：target_index={target_idx}")
            invalid_targets.append({"target_index": target_idx, "reason": "duplicate_target", "trace": row})
            by_target[target_idx] = row
            continue
        by_target[target_idx] = row
    template_bucket_to_ids = (
        candidate_lookup.get("template_bucket_to_ids")
        if isinstance(candidate_lookup, dict) and isinstance(candidate_lookup.get("template_bucket_to_ids"), dict)
        else {}
    )
    by_id = (
        candidate_lookup.get("by_id")
        if isinstance(candidate_lookup, dict) and isinstance(candidate_lookup.get("by_id"), dict)
        else {}
    )

    def _same_mastery_template_slice_ids(m: str) -> set[int]:
        """Collect slice ids that fall under any template route bucket with mastery ``m``."""
        out: set[int] = set()
        for (_rp, mm), sids in template_bucket_to_ids.items():
            if mm != m:
                continue
            for x in sids or []:
                if str(x).isdigit():
                    out.add(int(x))
        return out

    def _cross_route_same_mastery_fallback_ok(*, sid: int, slot_mastery: str) -> bool:
        """
        True when ``sid`` is not in the strict (route_prefix, mastery) bucket but still valid:
        same mastery as the slot and assigned to some template ``route_rules`` prefix (Level-4 pick).
        """
        if sid <= 0 or not slot_mastery:
            return False
        meta = by_id.get(sid) if isinstance(by_id.get(sid), dict) else {}
        kb_mastery = str(meta.get("mastery", "") or "").strip()
        if kb_mastery and kb_mastery != slot_mastery:
            return False
        return sid in _same_mastery_template_slice_ids(slot_mastery)

    for idx, slot in enumerate(planned_slots, start=1):
        row = by_target.get(idx)
        if not isinstance(row, dict):
            continue
        try:
            actual_sid = int(row.get("slice_id", 0) or 0)
        except (TypeError, ValueError):
            actual_sid = 0
        route_prefix = str(slot.get("route_prefix", "") or "").strip()
        mastery = str(slot.get("mastery", "") or "").strip()
        if route_prefix and mastery:
            allowed_ids = {
                int(x) for x in (template_bucket_to_ids.get((route_prefix, mastery), []) or [])
                if str(x).isdigit()
            }
            if actual_sid not in allowed_ids and not _cross_route_same_mastery_fallback_ok(
                sid=actual_sid, slot_mastery=mastery
            ):
                issues.append(
                    f"模板位次 {idx} 未命中指定桶：期望 route={route_prefix}, mastery={mastery}，实际 slice_id={actual_sid}"
                )
                invalid_targets.append({"target_index": idx, "reason": "wrong_bucket", "trace": row})
        else:
            try:
                expected_sid = int(slot.get("slice_id", 0) or 0)
            except (TypeError, ValueError):
                expected_sid = 0
            if expected_sid and actual_sid != expected_sid:
                issues.append(f"模板位次 {idx} 切片偏移：期望 {expected_sid}，实际 {actual_sid}")
                invalid_targets.append({"target_index": idx, "reason": "wrong_slice", "trace": row})
    missing_target_indexes = [idx for idx in range(1, slot_count + 1) if idx not in by_target]
    if len(passed_traces) != slot_count:
        issues.insert(0, f"模板并发聚合后通过题数不足：期望 {slot_count} 题，实际 {len(passed_traces)} 题")
    for idx in missing_target_indexes:
        issues.append(f"模板位次缺失：target_index={idx}")
    valid_by_target = {
        idx: row for idx, row in by_target.items()
        if idx not in {int(x.get("target_index", 0) or 0) for x in invalid_targets if isinstance(x, dict)}
    }
    return {
        "ok": not issues,
        "issues": issues,
        "slot_count": slot_count,
        "valid_by_target": valid_by_target,
        "missing_target_indexes": missing_target_indexes,
        "invalid_targets": invalid_targets,
    }


def _collect_unique_saved_template_traces(
    *,
    planned_slots: list[dict[str, Any]],
    process_trace: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    slot_count = len([slot for slot in (planned_slots or []) if isinstance(slot, dict)])
    if slot_count <= 0:
        return []
    by_target: dict[int, dict[str, Any]] = {}
    def _is_business_passed_trace(row: dict[str, Any]) -> bool:
        if not isinstance(row, dict):
            return False
        final_json = row.get("final_json")
        if not isinstance(final_json, dict) or not final_json:
            return False
        if bool(row.get("saved")) or bool(row.get("saved_with_issues")):
            return True
        critic_result = row.get("critic_result") if isinstance(row.get("critic_result"), dict) else {}
        if critic_result.get("passed") is True:
            return True
        for step in (row.get("steps") or []):
            if not isinstance(step, dict):
                continue
            if str(step.get("node", "")).strip() == "critic" and str(step.get("message", "")).strip() == "审核通过":
                return True
        return False
    for row in (process_trace or []):
        if not isinstance(row, dict):
            continue
        if not _is_business_passed_trace(row):
            continue
        try:
            target_idx = int(row.get("target_index", 0) or 0)
        except (TypeError, ValueError):
            continue
        if target_idx <= 0 or target_idx > slot_count:
            continue
        prev = by_target.get(target_idx)
        if not isinstance(prev, dict) or int(row.get("index", 0) or 0) >= int(prev.get("index", 0) or 0):
            by_target[target_idx] = row
    return [by_target[idx] for idx in sorted(by_target.keys())]


def _template_repair_trace_row_has_business_pass(row: dict[str, Any]) -> bool:
    """判断 trace 是否已达业务可用（与模板聚合口径一致），用于补位轮换时识别「失败过的切片」。"""
    if not isinstance(row, dict):
        return False
    final_json = row.get("final_json")
    if not isinstance(final_json, dict) or not final_json:
        return False
    if bool(row.get("saved")) or bool(row.get("saved_with_issues")):
        return True
    critic_result = row.get("critic_result") if isinstance(row.get("critic_result"), dict) else {}
    if critic_result.get("passed") is True:
        return True
    for step in (row.get("steps") or []):
        if not isinstance(step, dict):
            continue
        if str(step.get("node", "")).strip() == "critic" and str(step.get("message", "")).strip() == "审核通过":
            return True
    return False


def _rotate_template_repair_slots_for_retry(
    repair_slots: list[dict[str, Any]],
    *,
    merged_traces: list[dict[str, Any]],
    candidate_lookup: dict[str, Any],
    rotation_step: int,
) -> list[dict[str, Any]]:
    """
    模板补位重试时在同 (route_prefix, mastery) 桶内轮换 slice_id，
    避免同一策略+目标组合第二轮立刻被签名去重拦下，且始终粘在首次失败的切片上。
    """
    template_bucket_to_ids = (
        candidate_lookup.get("template_bucket_to_ids")
        if isinstance(candidate_lookup, dict) and isinstance(candidate_lookup.get("template_bucket_to_ids"), dict)
        else {}
    )
    out: list[dict[str, Any]] = []
    rs = max(0, int(rotation_step or 0))
    for slot in repair_slots:
        if not isinstance(slot, dict):
            continue
        gti = int(slot.get("_global_target_index", 0) or 0)
        route_prefix = str(slot.get("route_prefix", "") or "").strip()
        mastery = str(slot.get("mastery", "") or "").strip()
        key = (route_prefix, mastery)
        allowed_raw = template_bucket_to_ids.get(key)
        if not isinstance(allowed_raw, list) or not allowed_raw:
            out.append(dict(slot))
            continue
        allowed: list[int] = []
        for x in allowed_raw:
            if str(x).isdigit():
                allowed.append(int(x))
        if not allowed:
            out.append(dict(slot))
            continue
        failed_sids: set[int] = set()
        if gti > 0:
            for tr in merged_traces:
                if not isinstance(tr, dict):
                    continue
                if int(tr.get("target_index", 0) or 0) != gti:
                    continue
                if _template_repair_trace_row_has_business_pass(tr):
                    continue
                sid = int(tr.get("slice_id", 0) or 0)
                if sid > 0:
                    failed_sids.add(sid)
        prefer = [sid for sid in allowed if sid not in failed_sids]
        pool = prefer if prefer else allowed
        pick = pool[rs % len(pool)]
        new_slot = dict(slot)
        new_slot["slice_id"] = int(pick)
        out.append(new_slot)
    return out


def _plan_template_repair_strategy(report: dict[str, Any]) -> tuple[str, str]:
    issue_lines = [str(x).strip() for x in (report.get("issues") or []) if str(x).strip()]
    prompt = f"""
你是模板出题修复调度器。你只能在以下策略中选择一种：
1. `repair_missing_slots`：仅补缺失位次
2. `retry_invalid_and_missing_slots`：作废错位/重复位次，并补跑错位+缺失位次
3. `abort`：不要继续修复，直接失败

当前模板聚合校验报告：
{json.dumps({
    "slot_count": report.get("slot_count"),
    "missing_target_indexes": report.get("missing_target_indexes"),
    "invalid_targets": [
        {
            "target_index": x.get("target_index"),
            "reason": x.get("reason"),
            "slice_id": ((x.get("trace") or {}).get("slice_id") if isinstance(x.get("trace"), dict) else None),
        }
        for x in (report.get("invalid_targets") or [])
        if isinstance(x, dict)
    ],
    "issues": issue_lines,
}, ensure_ascii=False)}

决策要求：
- 如果只有缺失位次，没有错位/重复，优先选 `repair_missing_slots`
- 如果存在错位、重复或错桶，优先选 `retry_invalid_and_missing_slots`
- 只有在问题无法通过补位/重试局部位次解决时，才选 `abort`

输出格式（纯文本，不要 JSON）：
第一行：STRATEGY=repair_missing_slots 或 retry_invalid_and_missing_slots 或 abort
第二行：REASON=一句话说明
"""
    # Rule-first guardrail: when invalid targets exist (duplicate/wrong-bucket/etc),
    # always use combined repair to avoid LLM picking "missing only" and getting stuck.
    invalid_targets = report.get("invalid_targets") if isinstance(report.get("invalid_targets"), list) else []
    if invalid_targets:
        return "retry_invalid_and_missing_slots", "存在错位/重复位次，优先局部作废并补跑"

    try:
        api_key, base_url, model_name = _resolve_generation_llm_from_primary_key()
        if not api_key:
            raise RuntimeError("missing_api_key")
        content, _, _ = call_llm(
            node_name="template.repair_plan",
            prompt=prompt,
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            provider="ait",
            temperature=0.0,
            max_tokens=200,
            timeout=60,
        )
        raw = str(content or "").strip()
        strategy = ""
        reason = ""
        line_candidates = [line.strip() for line in raw.splitlines() if line.strip()]
        for line in line_candidates:
            lower = line.lower()
            if lower.startswith("strategy=") or lower.startswith("strategy:"):
                strategy = (
                    line.split("=", 1)[1].strip() if "=" in line else line.split(":", 1)[1].strip()
                )
                break
        if not strategy:
            # Fallback: find any allowed token in free-form text.
            for token in ("retry_invalid_and_missing_slots", "repair_missing_slots", "abort"):
                if token in raw:
                    strategy = token
                    break
        for line in line_candidates:
            lower = line.lower()
            if lower.startswith("reason=") or lower.startswith("reason:"):
                reason = line.split("=", 1)[1].strip() if "=" in line else line.split(":", 1)[1].strip()
                break
        if not reason and raw:
            reason = raw[:120]
    except Exception:
        strategy = ""
        reason = ""
    allowed = {"repair_missing_slots", "retry_invalid_and_missing_slots", "abort"}
    if strategy not in allowed:
        missing_targets = report.get("missing_target_indexes") if isinstance(report.get("missing_target_indexes"), list) else []
        if invalid_targets:
            strategy = "retry_invalid_and_missing_slots"
            reason = reason or "存在错位/重复位次，优先局部作废并补跑"
        elif missing_targets:
            strategy = "repair_missing_slots"
            reason = reason or "仅存在缺失位次，直接补位"
        else:
            strategy = "abort"
            reason = reason or "未识别出可修复缺口"
    return strategy, reason


def _prune_judge_task_cache() -> None:
    if len(JUDGE_TASKS) <= JUDGE_TASK_KEEP:
        return
    rows = sorted(
        JUDGE_TASKS.values(),
        key=lambda x: str(x.get("updated_at", "") or x.get("created_at", "")),
        reverse=True,
    )
    keep_ids = {str(x.get("task_id", "")) for x in rows[:JUDGE_TASK_KEEP]}
    active_ids = {
        str(x.get("task_id", ""))
        for x in JUDGE_TASKS.values()
        if str(x.get("status", "")).lower() in {"pending", "running"}
    }
    keep_ids.update(active_ids)
    for tid in list(JUDGE_TASKS.keys()):
        if tid not in keep_ids:
            JUDGE_TASKS.pop(tid, None)


def _make_judge_task(tenant_id: str, run_id: str, system_user: str, body: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    task_id = f"judge_task_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    requested_ids = body.get("question_ids")
    if requested_ids is not None and not isinstance(requested_ids, list):
        requested_ids = [str(requested_ids)] if requested_ids else []
    req_ids = [str(x).strip() for x in (requested_ids or []) if str(x).strip()]
    task_name = str(body.get("task_name", "") or "").strip() or f"Judge-{run_id[:18]}"
    task = {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "run_id": run_id,
        "task_name": task_name,
        "creator": system_user,
        "created_at": now,
        "updated_at": now,
        "started_at": "",
        "ended_at": "",
        "status": "pending",  # pending|running|completed|failed|cancelled
        "request": {
            "task_name": task_name,
            "run_id": run_id,
            "question_ids": req_ids,
        },
        "errors": [],
        "error_count": 0,
        "progress": {"current": 0, "total": 0},
        "success_count": 0,
        "judge_count": 0,
        "current_question_id": "",
    }
    with JUDGE_TASK_LOCK:
        JUDGE_TASKS[task_id] = task
        _prune_judge_task_cache()
        return _task_snapshot(task)


def _judge_task_name_exists(tenant_id: str, task_name: str) -> bool:
    normalized = str(task_name or "").strip().casefold()
    if not normalized:
        return False
    with JUDGE_TASK_LOCK:
        for task in JUDGE_TASKS.values():
            if str(task.get("tenant_id", "")) != tenant_id:
                continue
            exist = str(task.get("task_name", "") or "").strip().casefold()
            if exist and exist == normalized:
                return True
    for row in _read_jsonl(_qa_judge_tasks_path(tenant_id)):
        if not isinstance(row, dict):
            continue
        exist = str(row.get("task_name", "") or "").strip().casefold()
        if exist and exist == normalized:
            return True
    return False


def _path_version_token(path_like: Any) -> str:
    path_str = str(path_like or "").strip()
    if not path_str:
        return ""
    try:
        stat = Path(path_str).stat()
        return f"{path_str}|{int(stat.st_mtime_ns)}|{int(stat.st_size)}"
    except Exception:
        return path_str


def _get_cached_retriever(
    *,
    tenant_id: str,
    kb_path: str,
    history_path: str,
    mapping_path: str,
    mapping_review_path: str,
) -> KnowledgeRetriever:
    cache_key = (
        str(tenant_id or "").strip(),
        _path_version_token(kb_path),
        _path_version_token(history_path),
        _path_version_token(mapping_path),
        _path_version_token(mapping_review_path),
    )
    wait_event: threading.Event | None = None
    should_build = False
    with RETRIEVER_CACHE_LOCK:
        cached = RETRIEVER_CACHE.get(cache_key)
        if cached is not None:
            return cached
        wait_event = RETRIEVER_CACHE_INFLIGHT.get(cache_key)
        if wait_event is None:
            wait_event = threading.Event()
            RETRIEVER_CACHE_INFLIGHT[cache_key] = wait_event
            RETRIEVER_CACHE_ERRORS.pop(cache_key, None)
            should_build = True
    if not should_build:
        assert wait_event is not None
        if not wait_event.wait(timeout=float(_RETRIEVER_CACHE_WAIT_SECONDS)):
            raise RuntimeError(
                f"retriever_cache_wait_timeout[{cache_key[0]}]: waited {_RETRIEVER_CACHE_WAIT_SECONDS}s"
            )
        with RETRIEVER_CACHE_LOCK:
            cached = RETRIEVER_CACHE.get(cache_key)
            if cached is not None:
                return cached
            err = RETRIEVER_CACHE_ERRORS.pop(cache_key, None)
        if err is not None:
            raise RuntimeError(f"retriever_build_failed[{cache_key[0]}]") from err
        raise RuntimeError(f"retriever_cache_wait_failed[{cache_key[0]}]")

    try:
        retriever = build_knowledge_retriever(
            tenant_id=tenant_id,
            kb_path=str(kb_path),
            history_path=str(history_path),
            mapping_path=str(mapping_path),
            mapping_review_path=str(mapping_review_path),
        )
    except Exception as e:
        with RETRIEVER_CACHE_LOCK:
            RETRIEVER_CACHE_ERRORS[cache_key] = e
            inflight = RETRIEVER_CACHE_INFLIGHT.pop(cache_key, None)
            if inflight is not None:
                inflight.set()
        raise

    with RETRIEVER_CACHE_LOCK:
        RETRIEVER_CACHE[cache_key] = retriever
        RETRIEVER_CACHE_ERRORS.pop(cache_key, None)
        if len(RETRIEVER_CACHE) > 12:
            stale_keys = [k for k in RETRIEVER_CACHE.keys() if k != cache_key]
            for stale_key in stale_keys[: max(0, len(RETRIEVER_CACHE) - 12)]:
                RETRIEVER_CACHE.pop(stale_key, None)
                RETRIEVER_CACHE_ERRORS.pop(stale_key, None)
        inflight = RETRIEVER_CACHE_INFLIGHT.pop(cache_key, None)
        if inflight is not None:
            inflight.set()
        return RETRIEVER_CACHE[cache_key]


def _judge_request_body_from_task(task: dict[str, Any]) -> dict[str, Any]:
    req = task.get("request") if isinstance(task.get("request"), dict) else {}
    qids = req.get("question_ids")
    if qids is not None and not isinstance(qids, list):
        qids = [str(qids)] if qids else []
    return {"question_ids": [str(x).strip() for x in (qids or []) if str(x).strip()]}


def _start_next_judge_task_if_idle(tenant_id: str) -> None:
    """
    Serial queue per tenant: if no running judge task, start the earliest pending one.
    Pending tasks can accumulate across multiple runs.
    """
    now = datetime.now(timezone.utc).isoformat()
    to_persist: list[dict[str, Any]] = []
    next_task: dict[str, Any] | None = None
    with JUDGE_TASK_LOCK:
        has_running = any(
            str(t.get("tenant_id", "")) == tenant_id and str(t.get("status", "")).lower() == "running"
            for t in JUDGE_TASKS.values()
            if isinstance(t, dict)
        )
        if has_running:
            return
        pending = [
            t for t in JUDGE_TASKS.values()
            if isinstance(t, dict)
            and str(t.get("tenant_id", "")) == tenant_id
            and str(t.get("status", "")).lower() == "pending"
        ]
        pending.sort(key=lambda x: str(x.get("created_at", "") or ""))
        for task in pending:
            if bool(task.get("cancel_requested")):
                task["status"] = "cancelled"
                task["ended_at"] = str(task.get("ended_at", "") or now)
                task["updated_at"] = now
                to_persist.append(_task_snapshot(task))
                continue
            task["status"] = "running"
            task["started_at"] = str(task.get("started_at", "") or now)
            task["updated_at"] = now
            next_task = _task_snapshot(task)
            to_persist.append(next_task)
            break
    for snap in to_persist:
        _persist_judge_task(tenant_id, snap)
    if not isinstance(next_task, dict):
        return
    task_id = str(next_task.get("task_id", "")).strip()
    run_id = str(next_task.get("run_id", "")).strip()
    if not task_id or not run_id:
        return
    body = _judge_request_body_from_task(next_task)
    t = threading.Thread(
        target=_run_judge_task_worker,
        args=(tenant_id, task_id, run_id, body),
        daemon=True,
    )
    t.start()


def _update_judge_task_live(tenant_id: str, task_id: str, patch: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with JUDGE_TASK_LOCK:
        task = JUDGE_TASKS.get(task_id)
        if not task or str(task.get("tenant_id", "")) != tenant_id:
            return
        task.update(patch or {})
        task["error_count"] = len(task.get("errors") or [])
        task["updated_at"] = now


def _persist_judge_task(tenant_id: str, task: dict[str, Any]) -> None:
    _append_jsonl(_qa_judge_tasks_path(tenant_id), task)


def _persist_live_judge_task_snapshot(tenant_id: str, task_id: str) -> None:
    with JUDGE_TASK_LOCK:
        task = JUDGE_TASKS.get(str(task_id or ""))
        if not isinstance(task, dict):
            return
        if str(task.get("tenant_id", "")) != tenant_id:
            return
        snap = _task_snapshot(task)
    _persist_judge_task(tenant_id, snap)


def _read_persisted_judge_task(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    for row in reversed(_read_jsonl(_qa_judge_tasks_path(tenant_id))):
        if not isinstance(row, dict):
            continue
        if str(row.get("task_id", "")) == task_id:
            return row
    return None


def _is_judge_task_cancelled(task_id: str) -> bool:
    with JUDGE_TASK_LOCK:
        t = JUDGE_TASKS.get(str(task_id or ""))
        return bool(t and t.get("cancel_requested"))


def _load_run_task_name_lookup(tenant_id: str) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for row in _latest_gen_task_rows(tenant_id, allow_full_fallback=True).values():
        if not isinstance(row, dict):
            continue
        rid = str(row.get("run_id", "") or "").strip()
        if not rid:
            continue
        name = str(row.get("task_name", "") or "").strip()
        if name:
            lookup[rid] = name
    recent_runs, _ = _collect_recent_jsonl_rows_from_paths(
        _qa_read_paths(tenant_id, "qa_runs.jsonl"),
        target_count=400,
        sort_key=lambda row: str(row.get("ended_at", "") or ""),
        unique_key=lambda row: str(row.get("run_id", "") or ""),
    )
    for row in recent_runs:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("run_id", "") or "").strip()
        if not rid or rid in lookup:
            continue
        name = str(row.get("task_name", "") or "").strip()
        if name:
            lookup[rid] = name
    return lookup


def _load_latest_judge_task_by_run(tenant_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    rows, _ = _collect_recent_jsonl_rows_from_paths(
        _qa_read_paths(tenant_id, "judge_tasks.jsonl"),
        target_count=400,
        sort_key=lambda row: str(row.get("created_at", "") or ""),
        unique_key=lambda row: str(row.get("run_id", "") or ((row.get("request") or {}).get("run_id", "") if isinstance(row.get("request"), dict) else "")).strip(),
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        run_id = str(row.get("run_id", "") or "").strip()
        if not run_id:
            req = row.get("request") if isinstance(row.get("request"), dict) else {}
            run_id = str(req.get("run_id", "") or "").strip()
        if not run_id:
            continue
        created_at = str(row.get("created_at", "") or "")
        prev = out.get(run_id)
        if isinstance(prev, dict) and str(prev.get("created_at", "") or "") >= created_at:
            continue
        req = row.get("request") if isinstance(row.get("request"), dict) else {}
        task_name = str(row.get("task_name", "") or req.get("task_name", "") or "").strip()
        out[run_id] = {
            "task_id": str(row.get("task_id", "") or "").strip(),
            "task_name": task_name,
            "status": str(row.get("status", "") or "").strip(),
            "created_at": created_at,
            "started_at": str(row.get("started_at", "") or "").strip(),
            "ended_at": str(row.get("ended_at", "") or "").strip(),
        }
    return out


def _build_judge_task_summary(task: dict[str, Any], run_task_name_lookup: dict[str, str] | None = None) -> dict[str, Any]:
    req = task.get("request") if isinstance(task.get("request"), dict) else {}
    run_id = str(task.get("run_id", "") or req.get("run_id", "") or "").strip()
    tenant_id = str(task.get("tenant_id", "") or "").strip()
    task_name = str(task.get("task_name", "") or str(req.get("task_name", ""))).strip()
    source_task_name = ""
    if run_id and tenant_id:
        if run_task_name_lookup is None:
            run_task_name_lookup = _load_run_task_name_lookup(tenant_id)
        source_task_name = str(run_task_name_lookup.get(run_id, "")).strip()
    if not task_name and source_task_name:
        task_name = source_task_name
    return {
        "task_id": str(task.get("task_id", "")),
        "tenant_id": tenant_id,
        "run_id": run_id,
        "task_name": task_name,
        "source_task_name": source_task_name,
        "status": str(task.get("status", "pending")),
        "created_at": str(task.get("created_at", "")),
        "updated_at": str(task.get("updated_at", "")),
        "started_at": str(task.get("started_at", "")),
        "ended_at": str(task.get("ended_at", "")),
        "current_question_id": str(task.get("current_question_id", "")),
        "judge_count": int(task.get("judge_count", 0) or 0),
        "success_count": int(task.get("success_count", 0) or 0),
        "error_count": int(task.get("error_count", 0) or 0),
        "progress": task.get("progress") if isinstance(task.get("progress"), dict) else {"current": 0, "total": 0},
        "errors": [str(x) for x in (task.get("errors") or [])],
        "request": {
            "run_id": str(req.get("run_id", "")),
            "question_ids": [str(x) for x in (req.get("question_ids") or [])],
        },
    }


def _mapping_job_key(tenant_id: str, material_version_id: str) -> str:
    return f"{tenant_id}:{material_version_id}"


def _get_mapping_job_snapshot(tenant_id: str, material_version_id: str) -> dict[str, Any] | None:
    key = _mapping_job_key(tenant_id, material_version_id)
    with MAPPING_JOB_LOCK:
        job = MAPPING_JOBS.get(key)
        if not isinstance(job, dict):
            return None
        return deepcopy(job)


def _update_mapping_job(tenant_id: str, material_version_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    key = _mapping_job_key(tenant_id, material_version_id)
    now = datetime.now(timezone.utc).isoformat()
    with MAPPING_JOB_LOCK:
        prev = MAPPING_JOBS.get(key) or {
            "job_id": f"map_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "tenant_id": tenant_id,
            "material_version_id": material_version_id,
            "status": "pending",  # pending|running|completed|failed
            "progress": 0,
            "message": "",
            "mapping_total": 0,
            "created_at": now,
            "started_at": "",
            "ended_at": "",
            "updated_at": now,
        }
        prev.update(patch or {})
        prev["updated_at"] = now
        MAPPING_JOBS[key] = prev
        return deepcopy(prev)


def _run_material_mapping_job_worker(
    *,
    tenant_id: str,
    material_version_id: str,
    system_user: str,
    kb_file: Path,
    history_file: Path,
    output_path: Path,
    audit_action: str,
    reference_file: str = "",
) -> None:
    try:
        processed_re = re.compile(r"Processed\s+(\d+)\s*/\s*(\d+)\s+questions", re.IGNORECASE)
        progress_pct_re = re.compile(r"\((\d+(?:\.\d+)?)%\)")

        _update_mapping_job(
            tenant_id,
            material_version_id,
            {
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "progress": 0,
                "message": "映射任务已启动，准备执行",
                "ended_at": "",
                "mapping_total": 0,
            },
        )
        _append_mapping_progress_event(
            tenant_id,
            material_version_id,
            status="running",
            progress=0,
            message="映射任务已启动，准备执行",
        )
        upsert_material_runtime(
            tenant_id,
            material_version_id,
            mapping_status="running",
            mapping_error="",
        )
        _update_mapping_job(
            tenant_id,
            material_version_id,
            {"progress": 5, "message": "映射脚本已启动，正在加载模型"},
        )
        _append_mapping_progress_event(
            tenant_id,
            material_version_id,
            status="running",
            progress=5,
            message="映射脚本已启动，正在加载模型",
        )
        upsert_material_runtime(
            tenant_id,
            material_version_id,
            mapping_status="running",
            mapping_error="",
        )

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
        dep_check_cmd = [
            sys.executable,
            "-c",
            "import flask, pandas, jieba, sentence_transformers, openpyxl, xlrd",
        ]
        dep_check = subprocess.run(
            dep_check_cmd,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent),
        )
        if dep_check.returncode != 0:
            err_text = (
                "映射依赖缺失，请检查 Python 环境（flask/pandas/jieba/sentence-transformers/openpyxl/xlrd）："
                f" {(dep_check.stderr or dep_check.stdout or '').strip()[:400]}"
            )
            upsert_material_runtime(
                tenant_id,
                material_version_id,
                mapping_status="failed",
                mapping_error=err_text,
            )
            _update_mapping_job(
                tenant_id,
                material_version_id,
                {
                    "status": "failed",
                    "progress": 100,
                    "message": err_text,
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            _append_mapping_progress_event(
                tenant_id,
                material_version_id,
                status="failed",
                progress=100,
                message=err_text,
            )
            return

        env = os.environ.copy()
        # Mapping must use the bundled local BGE model only on remote hosts.
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            cwd=str(Path(__file__).resolve().parent),
            env=env,
        )
        last_progress = 5
        output_tail: list[str] = []

        def _push_progress(progress: int, message: str) -> None:
            nonlocal last_progress
            p = max(last_progress, min(99, int(progress)))
            msg = str(message or "").strip()
            if p == last_progress and not msg:
                return
            last_progress = p
            _update_mapping_job(
                tenant_id,
                material_version_id,
                {"progress": p, "message": msg or "正在执行映射脚本"},
            )
            _append_mapping_progress_event(
                tenant_id,
                material_version_id,
                status="running",
                progress=p,
                message=msg or "正在执行映射脚本",
            )

        if proc.stdout is not None:
            for raw in proc.stdout:
                line = str(raw or "").strip()
                if not line:
                    continue
                output_tail.append(line)
                if len(output_tail) > 80:
                    output_tail = output_tail[-80:]

                if "Loading BGE model" in line:
                    _push_progress(8, "正在加载BGE模型")
                    continue
                if "Building slice metadata" in line:
                    _push_progress(12, "正在准备切片元数据")
                    continue
                if "Precomputing BGE embeddings for all slices" in line:
                    _push_progress(18, "正在预计算切片向量")
                    continue
                if "Saving mapping to" in line:
                    _push_progress(92, "脚本执行完成，正在写入映射结果")
                    continue

                m = processed_re.search(line)
                if m:
                    done = int(m.group(1))
                    total = max(int(m.group(2)), 1)
                    pct = (done / total) * 100.0
                    mapped = 20 + int(round((pct / 100.0) * 68))  # 20~88 mapped from real question progress
                    _push_progress(mapped, f"正在映射题目：{done}/{total} ({pct:.1f}%)")
                    continue
                p = progress_pct_re.search(line)
                if p:
                    try:
                        pct = float(p.group(1))
                        mapped = 20 + int(round((pct / 100.0) * 68))
                        _push_progress(mapped, f"正在执行映射脚本（{pct:.1f}%）")
                    except Exception:
                        pass

        proc.wait()
        if proc.returncode != 0:
            tail = "\n".join(output_tail[-8:]).strip()
            err_text = f"映射脚本执行失败: {tail or '请查看服务日志'}"
            upsert_material_runtime(
                tenant_id,
                material_version_id,
                mapping_status="failed",
                mapping_error=err_text,
            )
            _update_mapping_job(
                tenant_id,
                material_version_id,
                {
                    "status": "failed",
                    "progress": 100,
                    "message": err_text,
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            _append_mapping_progress_event(
                tenant_id,
                material_version_id,
                status="failed",
                progress=100,
                message=err_text,
            )
            return
        if not output_path.exists():
            err_text = "映射结果未生成"
            upsert_material_runtime(
                tenant_id,
                material_version_id,
                mapping_status="failed",
                mapping_error=err_text,
            )
            _update_mapping_job(
                tenant_id,
                material_version_id,
                {
                    "status": "failed",
                    "progress": 100,
                    "message": err_text,
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            _append_mapping_progress_event(
                tenant_id,
                material_version_id,
                status="failed",
                progress=100,
                message=err_text,
            )
            return

        _update_mapping_job(
            tenant_id,
            material_version_id,
            {"progress": 90, "message": "脚本执行完成，正在写入结果"},
        )
        mapping_total = 0
        try:
            mapping = json.loads(output_path.read_text(encoding="utf-8"))
            mapping_total = len(mapping) if isinstance(mapping, dict) else 0
        except json.JSONDecodeError:
            mapping_total = 0
        upsert_material_runtime(
            tenant_id,
            material_version_id,
            mapping_status="success",
            mapping_error="",
        )
        _update_mapping_job(
            tenant_id,
            material_version_id,
            {
                "status": "completed",
                "progress": 100,
                "mapping_total": mapping_total,
                "message": f"映射完成，共 {mapping_total} 条",
                "ended_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        _append_mapping_progress_event(
            tenant_id,
            material_version_id,
            status="completed",
            progress=100,
            message=f"映射完成，共 {mapping_total} 条",
        )
        write_audit_log(
            tenant_id,
            system_user,
            audit_action,
            "material",
            material_version_id,
            after={
                "material_version_id": material_version_id,
                "kb_file": str(kb_file),
                "reference_file": reference_file,
                "history_copy": str(history_file),
                "mapping_file": str(output_path),
                "mapping_total": mapping_total,
                "mode": "async",
            },
        )
    except Exception as e:
        err_text = f"映射任务异常: {str(e)}"
        upsert_material_runtime(
            tenant_id,
            material_version_id,
            mapping_status="failed",
            mapping_error=err_text,
        )
        _update_mapping_job(
            tenant_id,
            material_version_id,
            {
                "status": "failed",
                "progress": 100,
                "message": err_text,
                "ended_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        _append_mapping_progress_event(
            tenant_id,
            material_version_id,
            status="failed",
            progress=100,
            message=err_text,
        )

def _default_qa_thresholds() -> dict[str, Any]:
    return {
        "hard_pass_rate_min": 1.0,
        "logic_pass_rate_min": 0.95,
        "out_of_scope_rate_max": 0.02,
        "duplicate_rate_max": 0.03,
        "avg_distractor_score_min": 3.5,
        "avg_critic_loops_max": 2.0,
        "risk_high_rate_max": 0.03,  # now: critic_fail_rate (fail with reason / task output count)
        "avg_tokens_per_question_max": 3000,
        "avg_latency_ms_per_question_max": 10000,
        "avg_cost_per_question_max": 1.5,
        "cpvq_max": 2.0,
        "sla_hours_high": 24,
        "sla_hours_medium": 72,
        "sla_hours_low": 168,
    }


def _configured_models_from_key_file() -> list[str]:
    models: list[str] = []
    cfg = _load_primary_key_config()
    if not cfg:
        return models
    try:
        for name, value in cfg.items():
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


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Overwrite path with one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _default_qa_config() -> dict[str, Any]:
    return {"baseline_run_id": "", "baseline_run_ids": []}


def _load_qa_config(tenant_id: str) -> dict[str, Any]:
    path = _qa_config_path(tenant_id)
    defaults = _default_qa_config()
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


def _save_qa_config(tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    merged = _load_qa_config(tenant_id)
    if isinstance(payload, dict) and "baseline_run_id" in payload:
        merged["baseline_run_id"] = str(payload.get("baseline_run_id", "")).strip()
    if isinstance(payload, dict) and "baseline_run_ids" in payload:
        raw_ids = payload.get("baseline_run_ids")
        if isinstance(raw_ids, list):
            merged["baseline_run_ids"] = [str(x).strip() for x in raw_ids if str(x).strip()]
        elif raw_ids is None:
            merged["baseline_run_ids"] = []
        else:
            txt = str(raw_ids).strip()
            merged["baseline_run_ids"] = [x.strip() for x in txt.split(",") if x.strip()]
    _qa_config_path(tenant_id).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return merged


def _load_qa_releases(tenant_id: str) -> list[dict[str, Any]]:
    """Return releases newest first."""
    path = _qa_releases_path(tenant_id)
    if not path.exists():
        return []
    rows = _read_jsonl(path)
    out = [r for r in rows if isinstance(r, dict)]
    out.sort(key=lambda x: str(x.get("published_at", "")), reverse=True)
    return out


def _append_qa_release(tenant_id: str, release: dict[str, Any]) -> None:
    _append_jsonl(_qa_releases_path(tenant_id), release)


_GIT_CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx",
    ".css", ".scss", ".less", ".html",
    ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".sql", ".sh", ".bash", ".zsh", ".command",
    ".txt",
}
_GIT_CODE_FILENAMES = {"Dockerfile", "Makefile", "docker-compose.yml", "docker-compose.yaml"}
_GIT_EXCLUDED_PREFIXES = (
    "data/",
    "admin-web/node_modules/",
    "admin-web/dist/",
    ".venv/",
    "__pycache__/",
    "tmp/",
)
_DEFAULT_RELEASE_GIT_REMOTE_URL = "git@git.lianjia.com:confucius/huaqiao_vibe/boxue-ai-exam-generator.git"
_DEFAULT_RELEASE_GIT_USER_EMAIL = "panting047@ke.com"
_DEFAULT_RELEASE_GIT_COMMIT_MESSAGE = "[紧急]fix"
_DEFAULT_RELEASE_GIT_BRANCH = "main"
_RELEASE_REMOTE_NAME = "release_sync_remote"


def _sanitize_git_remote_url(remote_url: str) -> str:
    txt = str(remote_url or "").strip()
    if not txt:
        return ""
    try:
        u = urlsplit(txt)
    except Exception:
        return txt
    if u.scheme not in {"http", "https"}:
        return txt
    netloc = u.netloc
    if "@" not in netloc:
        return txt
    host = netloc.split("@", 1)[1]
    return urlunsplit((u.scheme, host, u.path, u.query, u.fragment))


def _inject_http_auth_to_remote_url(remote_url: str, username: str, token: str) -> str:
    txt = str(remote_url or "").strip()
    user = str(username or "").strip()
    secret = str(token or "").strip()
    if not txt or not user or not secret:
        return txt
    try:
        u = urlsplit(txt)
    except Exception:
        return txt
    if u.scheme not in {"http", "https"}:
        return txt
    if "@" in u.netloc:
        return txt
    auth = f"{quote(user, safe='')}:{quote(secret, safe='')}@{u.netloc}"
    return urlunsplit((u.scheme, auth, u.path, u.query, u.fragment))


def _maybe_convert_lianjia_https_to_ssh(remote_url: str) -> str:
    txt = str(remote_url or "").strip()
    if not txt:
        return txt
    try:
        u = urlsplit(txt)
    except Exception:
        return txt
    if u.scheme not in {"http", "https"}:
        return txt
    host = str(u.hostname or "").strip().lower()
    if host != "git.lianjia.com":
        return txt
    path = str(u.path or "").strip().lstrip("/")
    if not path:
        return txt
    return f"git@git.lianjia.com:{path}"


def _is_code_path_for_release_commit(rel_path: str) -> bool:
    p = str(rel_path or "").strip().replace("\\", "/")
    if not p:
        return False
    if p.startswith("./"):
        p = p[2:]
    for prefix in _GIT_EXCLUDED_PREFIXES:
        if p.startswith(prefix):
            return False
    name = Path(p).name
    if name in _GIT_CODE_FILENAMES:
        return True
    return Path(p).suffix.lower() in _GIT_CODE_EXTS


def _collect_changed_paths(repo_root: Path) -> list[str]:
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRTD", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=8,
    )
    if tracked.returncode != 0:
        raise RuntimeError((tracked.stderr or tracked.stdout or "git diff failed").strip())
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=8,
    )
    if untracked.returncode != 0:
        raise RuntimeError((untracked.stderr or untracked.stdout or "git ls-files failed").strip())
    out: set[str] = set()
    for line in (tracked.stdout or "").splitlines():
        p = line.strip()
        if p:
            out.add(p)
    for line in (untracked.stdout or "").splitlines():
        p = line.strip()
        if p:
            out.add(p)
    return sorted(out)


def _run_git_commit_for_release(
    tenant_id: str,
    version: str,
    release_notes: str,
    git_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Optionally commit code-only changes (exclude runtime data). Returns {ok, message, error}."""
    try:
        opts = git_options if isinstance(git_options, dict) else {}
        cfg = _autoload_primary_key_env(override=True)
        remote_url = str(
            opts.get("remote_url")
            or cfg.get("GIT_REPO_URL")
            or os.getenv("GIT_REPO_URL")
            or _DEFAULT_RELEASE_GIT_REMOTE_URL
        ).strip()
        push_branch = str(opts.get("push_branch", _DEFAULT_RELEASE_GIT_BRANCH) or "main").strip() or "main"
        commit_message = str(opts.get("commit_message", "") or "").strip()
        user_email = str(
            opts.get("user_email")
            or cfg.get("GIT_USER_EMAIL")
            or os.getenv("GIT_USER_EMAIL")
            or _DEFAULT_RELEASE_GIT_USER_EMAIL
        ).strip()
        user_name = str(
            opts.get("user_name")
            or cfg.get("GIT_USER_NAME")
            or os.getenv("GIT_USER_NAME")
            or ""
        ).strip()
        git_username = str(
            opts.get("git_username")
            or cfg.get("GIT_USERNAME")
            or os.getenv("GIT_USERNAME")
            or ""
        ).strip()
        git_token = str(
            opts.get("git_token")
            or cfg.get("GIT_TOKEN")
            or os.getenv("GIT_TOKEN")
            or cfg.get("GIT_PASSWORD")
            or os.getenv("GIT_PASSWORD")
            or ""
        ).strip()
        if remote_url and remote_url.startswith("https://") and "git.lianjia.com/" in remote_url and not git_username:
            # 在未提供 https 凭证时，优先切到 SSH，避免非交互环境下卡在用户名输入。
            remote_url = _maybe_convert_lianjia_https_to_ssh(remote_url)
        remote_url_for_git = _inject_http_auth_to_remote_url(remote_url, git_username, git_token)
        display_remote_url = _sanitize_git_remote_url(remote_url)
        service_root = Path(__file__).resolve().parent
        repo_probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=service_root,
            capture_output=True,
            text=True,
            timeout=8,
        )
        if repo_probe.returncode != 0:
            return {
                "ok": False,
                "message": "not a git repo",
                "error": (repo_probe.stderr or repo_probe.stdout or "").strip() or "git rev-parse failed",
            }
        repo_root_txt = str(repo_probe.stdout or "").strip()
        if not repo_root_txt:
            return {"ok": False, "message": "not a git repo", "error": "empty repo root"}
        repo_root = Path(repo_root_txt)
        if not (repo_root / ".git").exists():
            return {"ok": False, "message": "not a git repo", "error": f"no .git under {repo_root}"}

        changed_paths = _collect_changed_paths(repo_root)
        code_paths = [p for p in changed_paths if _is_code_path_for_release_commit(p)]
        if not code_paths:
            return {
                "ok": True,
                "message": "no code changes to commit",
                "checked_changed_files": len(changed_paths),
                "remote_url": display_remote_url,
                "push_branch": push_branch,
            }

        msg = commit_message or (f"Release {version}: {release_notes[:200]}" + ("..." if len(release_notes) > 200 else ""))
        commit_env = os.environ.copy()
        if user_email:
            commit_env["GIT_AUTHOR_EMAIL"] = user_email
            commit_env["GIT_COMMITTER_EMAIL"] = user_email
        if user_name:
            commit_env["GIT_AUTHOR_NAME"] = user_name
            commit_env["GIT_COMMITTER_NAME"] = user_name
        for i in range(0, len(code_paths), 200):
            chunk = code_paths[i:i + 200]
            r1 = subprocess.run(
                ["git", "add", "-A", "--", *chunk],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if r1.returncode != 0:
                return {"ok": False, "message": "git add failed", "error": (r1.stderr or r1.stdout or "").strip()}
        r2 = subprocess.run(
            ["git", "commit", "-m", msg, "--", *code_paths],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            env=commit_env,
        )
        if r2.returncode != 0:
            if "nothing to commit" in (r2.stderr or r2.stdout or "").lower():
                return {"ok": True, "message": "no changes to commit (already committed)"}
            return {"ok": False, "message": "git commit failed", "error": (r2.stderr or r2.stdout or "").strip()}
        push_result: dict[str, Any] = {"ok": True, "message": "skip push"}
        if remote_url:
            list_remote = subprocess.run(
                ["git", "remote"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=8,
            )
            if list_remote.returncode != 0:
                return {"ok": False, "message": "git remote list failed", "error": (list_remote.stderr or list_remote.stdout or "").strip()}
            remotes = {x.strip() for x in (list_remote.stdout or "").splitlines() if x.strip()}
            if _RELEASE_REMOTE_NAME in remotes:
                set_url = subprocess.run(
                    ["git", "remote", "set-url", _RELEASE_REMOTE_NAME, remote_url_for_git],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if set_url.returncode != 0:
                    return {"ok": False, "message": "git remote set-url failed", "error": (set_url.stderr or set_url.stdout or "").strip()}
            else:
                add_remote = subprocess.run(
                    ["git", "remote", "add", _RELEASE_REMOTE_NAME, remote_url_for_git],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if add_remote.returncode != 0:
                    return {"ok": False, "message": "git remote add failed", "error": (add_remote.stderr or add_remote.stdout or "").strip()}
            push_cmd = ["git", "push", _RELEASE_REMOTE_NAME, f"HEAD:{push_branch}"]
            r3 = subprocess.run(
                push_cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            if r3.returncode != 0:
                err_text = (r3.stderr or r3.stdout or "").strip()
                lowered = err_text.lower()
                if "non-fast-forward" in lowered or "[rejected]" in lowered:
                    fallback_branch = f"auto-release/{tenant_id}/{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
                    r3b = subprocess.run(
                        ["git", "push", _RELEASE_REMOTE_NAME, f"HEAD:{fallback_branch}"],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    )
                    if r3b.returncode == 0:
                        push_result = {
                            "ok": True,
                            "message": "pushed_to_fallback_branch",
                            "remote_url": display_remote_url,
                            "push_branch": fallback_branch,
                            "requested_push_branch": push_branch,
                            "warning": f"目标分支 {push_branch} 非 fast-forward，已回退推送到 {fallback_branch}",
                        }
                        return {
                            "ok": True,
                            "message": "committed_and_pushed_fallback_branch",
                            "commit_message": msg,
                            "committed_code_files": len(code_paths),
                            "checked_changed_files": len(changed_paths),
                            "remote_url": display_remote_url,
                            "push_branch": fallback_branch,
                            "requested_push_branch": push_branch,
                            "push": push_result,
                            "warning": push_result["warning"],
                        }
                return {
                    "ok": False,
                    "message": "git push failed",
                    "error": err_text,
                    "commit_message": msg,
                    "remote_url": display_remote_url,
                    "push_branch": push_branch,
                }
            push_result = {"ok": True, "message": "pushed", "remote_url": display_remote_url, "push_branch": push_branch}
        return {
            "ok": True,
            "message": "committed_and_pushed" if push_result.get("ok") else "committed",
            "commit_message": msg,
            "committed_code_files": len(code_paths),
            "checked_changed_files": len(changed_paths),
            "remote_url": display_remote_url,
            "push_branch": push_branch,
            "push": push_result,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "timeout", "error": "git command timeout"}
    except Exception as e:
        return {"ok": False, "message": str(type(e).__name__), "error": str(e)}


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


def _resolve_judge_city_name(
    tenant_id: str | None,
    config_payload: dict[str, Any] | None,
) -> str:
    """
    解析离线 Judge 使用的命题城市展示名：优先配置显式字段，否则按 tenant_id 查 tenants 注册表。
    """
    cfg = config_payload if isinstance(config_payload, dict) else {}
    for key in ("city_name", "tenant_display_name", "命题城市"):
        v = str(cfg.get(key, "") or "").strip()
        if v:
            return v
    tid = str(cfg.get("tenant_id", "") or (tenant_id or "")).strip()
    if not tid:
        return ""
    try:
        for row in list_tenants():
            if str(row.get("tenant_id", "")).strip() == tid:
                name = str(row.get("name", "") or "").strip()
                return name or tid
    except Exception:
        pass
    return tid


def _trace_to_question_input(
    question_trace: dict[str, Any],
    config_payload: dict[str, Any],
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """
    Build a dict suitable for Judge QuestionInput from process_trace item and config.
    Used by manual run-judge APIs to build/补全 offline Judge 入参。
    """
    final_json = question_trace.get("final_json") if isinstance(question_trace.get("final_json"), dict) else {}
    stem = str(final_json.get("题干", "") or "").strip()
    correct_answer = str(final_json.get("正确答案", "") or "").strip()
    explanation = str(final_json.get("解析", "") or "").strip()
    options: list[str] = []
    for i in range(1, 5):
        opt = str(final_json.get(f"选项{i}", "") or "").strip()
        if opt:
            options.append(opt)
    if not options:
        options = [""]
    textbook_slice = str(
        question_trace.get("slice_content")
        or question_trace.get("textbook_slice")
        or final_json.get("教材原文")
        or final_json.get("切片原文")
        or final_json.get("知识切片")
        or ""
    ).strip()
    related_slices, reference_slices = _extract_related_reference_slices(question_trace, final_json, None)
    question_type = _resolve_judge_question_type(
        preferred=question_trace.get("question_type"),
        stem=stem,
        options=options,
        correct_answer=correct_answer,
        config_question_type=config_payload.get("question_type", "单选题"),
    )
    generation_mode = str(config_payload.get("generation_mode", "随机") or "随机")
    assessment_type = "实战应用/推演" if generation_mode == "实战应用/推演" else "基础概念/理解记忆"
    city_name = _resolve_judge_city_name(tenant_id, config_payload)
    return {
        "question_id": str(question_trace.get("question_id", "")),
        "stem": stem,
        "options": options,
        "correct_answer": correct_answer,
        "explanation": explanation,
        "textbook_slice": textbook_slice or "(无切片原文)",
        "related_slices": related_slices,
        "reference_slices": reference_slices,
        "question_type": question_type,
        "assessment_type": assessment_type,
        "is_calculation": False,
        "city_name": city_name,
    }


def _get_offline_judge_llm() -> tuple[Any | None, str | None]:
    """
    Build LLM for offline Judge from env or key file. Uses AIT provider and gpt-5.2 by default.
    Returns (llm, None) on success, (None, error_message) on failure.
    """
    try:
        judge_dir = Path(__file__).resolve().parent / "离线Judge"
        if str(judge_dir) not in sys.path:
            sys.path.insert(0, str(judge_dir))
        from src.llm import build_llm
    except Exception as e:
        return None, f"无法加载 Judge 模块: {e!s}"
    ait_keys = ("AIT_API_KEY", "AIT_BASE_URL", "AIT_MODEL", "AIT_MAX_TOKENS", "AIT_JUDGE_MODEL")
    cfg = _autoload_primary_key_env(override=True)
    if cfg:
        for k in ait_keys:
            v = str(cfg.get(k, "")).strip()
            if _is_usable_secret(v) and not os.environ.get(k):
                os.environ[k] = v
    try:
        judge_model = os.getenv("AIT_JUDGE_MODEL") or os.getenv("JUDGE_MODEL", "gpt-5.2")
        llm = build_llm(
            provider="ait",
            model=judge_model,
            temperature=0,
        )
        return llm, None
    except Exception as e:
        return None, f"构建 Judge LLM 失败: {e!s}"


def _run_offline_judge_for_trace(
    question_trace: dict[str, Any],
    config_payload: dict[str, Any],
    llm: Any,
) -> dict[str, Any] | None:
    """
    Run offline Judge on one question trace; returns report dict or None on error.
    Caller must ensure Judge module is importable (sys.path includes 离线Judge).
    """
    try:
        judge_dir = Path(__file__).resolve().parent / "离线Judge"
        if str(judge_dir) not in sys.path:
            sys.path.insert(0, str(judge_dir))
        from src.pipeline.runner import run_judge
        from src.schemas.evaluation import QuestionInput
    except Exception:
        return None
    try:
        tid = str((config_payload or {}).get("tenant_id", "") or "").strip() or None
        qin_dict = _trace_to_question_input(question_trace, config_payload, tid)
        qin = QuestionInput(**qin_dict)
        report = run_judge(qin, llm)
        out = report.model_dump()
        return {
            "decision": out.get("decision"),
            "overall_score": out.get("overall_score"),
            "baseline_score": out.get("baseline_score", out.get("penalty_score")),
            "quality_score": out.get("quality_score"),
            "quality_reasons": out.get("quality_reasons") or [],
            "quality_scoring_basis": out.get("quality_scoring_basis") or "",
            "quality_dimension_feedback": out.get("quality_dimension_feedback") or {},
            "scores": out.get("scores"),
            "dimension_results": out.get("dimension_results"),
            "reasons": out.get("reasons") or [],
            "actionable_feedback": out.get("actionable_feedback") or "",
            "hard_gate": out.get("hard_gate"),
            "semantic_drift": out.get("semantic_drift"),
            "solver_validation": out.get("solver_validation"),
            "distractor_quality": out.get("distractor_quality"),
            "knowledge_match": out.get("knowledge_match"),
            "teaching_value": out.get("teaching_value"),
            "risk_assessment": out.get("risk_assessment"),
            "observability": out.get("observability"),
            "costs": out.get("costs"),
        }
    except Exception:
        return None


def _build_judge_input_from_question(question: dict[str, Any]) -> dict[str, Any] | None:
    """
    Get or build judge_input for a run question. Used when running Judge in single-question
    evaluation phase so we can call Judge even if the run was saved without judge_input.
    """
    ji = question.get("judge_input") if isinstance(question.get("judge_input"), dict) else None
    if ji and (str(ji.get("stem", "")).strip() or str(question.get("question_text", "")).strip()):
        return ji
    final_json = question.get("final_json") if isinstance(question.get("final_json"), dict) else {}
    stem = str((question.get("question_text") or final_json.get("题干") or (ji.get("stem") if ji else "") or "") or "").strip()
    options = list(question.get("options") or []) if isinstance(question.get("options"), list) else []
    if not options and isinstance(final_json, dict):
        for i in range(1, 5):
            opt = str(final_json.get(f"选项{i}", "") or "").strip()
            if opt:
                options.append(opt)
    if not options:
        options = [""]
    correct_answer = str(question.get("answer") or (final_json.get("正确答案") if isinstance(final_json, dict) else "") or "").strip()
    explanation = str(question.get("explanation") or (final_json.get("解析") if isinstance(final_json, dict) else "") or "").strip()
    textbook_slice = str(
        question.get("slice_content")
        or question.get("textbook_slice")
        or (ji.get("textbook_slice") if ji else "")
        or final_json.get("教材原文")
        or final_json.get("切片原文")
        or final_json.get("知识切片")
        or ""
    ).strip() or "(无切片原文)"
    related_slices, reference_slices = _extract_related_reference_slices(question, final_json, ji or {})
    question_type = _resolve_judge_question_type(
        preferred=(ji or {}).get("question_type") or question.get("question_type") or final_json.get("题目类型"),
        stem=stem,
        options=options,
        correct_answer=correct_answer,
        config_question_type="",
    )
    return {
        "stem": stem or "(题干缺失)",
        "options": options,
        "correct_answer": correct_answer,
        "explanation": explanation,
        "textbook_slice": textbook_slice,
        "related_slices": related_slices,
        "reference_slices": reference_slices,
        "question_type": question_type,
    }


def _is_effective_judge_input(ji: dict[str, Any] | None) -> bool:
    if not isinstance(ji, dict):
        return False
    stem = str(ji.get("stem", "") or "").strip()
    answer = str(ji.get("correct_answer", "") or "").strip()
    options = [str(x or "").strip() for x in (ji.get("options") or []) if str(x or "").strip()]
    return bool(stem and answer and options)


def _is_effective_run_question_payload(question: dict[str, Any] | None) -> bool:
    if not isinstance(question, dict):
        return False
    if _is_effective_judge_input(question.get("judge_input") if isinstance(question.get("judge_input"), dict) else None):
        return True
    stem = str(question.get("question_text", "") or "").strip()
    answer = str(question.get("answer", "") or "").strip()
    options = [str(x or "").strip() for x in (question.get("options") or []) if str(x or "").strip()]
    if stem and answer and options:
        return True
    final_json = question.get("final_json") if isinstance(question.get("final_json"), dict) else {}
    fj_stem = str(final_json.get("题干", "") or "").strip()
    fj_answer = str(final_json.get("正确答案", "") or final_json.get("答案", "") or "").strip()
    fj_options = [
        str(final_json.get(f"选项{i}", "") or "").strip()
        for i in range(1, 9)
        if str(final_json.get(f"选项{i}", "") or "").strip()
    ]
    return bool(fj_stem and fj_answer and fj_options)


def _find_task_trace_for_run_question(
    tenant_id: str,
    run_id: str,
    task_id: str,
    question: dict[str, Any],
) -> dict[str, Any] | None:
    rid = str(run_id or "").strip()
    tid = str(task_id or "").strip()
    qid = str(question.get("question_id", "") or "").strip()
    q_index = int(question.get("index", 0) or 0)
    q_slice_id = str(question.get("slice_id", "") or "").strip()

    task_row: dict[str, Any] | None = None
    if tid:
        task_row = _read_persisted_task(tenant_id, tid)
    if not isinstance(task_row, dict):
        for row in reversed(_read_jsonl(_qa_read_path(tenant_id, "gen_tasks.jsonl"))):
            if not isinstance(row, dict):
                continue
            if str(row.get("run_id", "") or "").strip() == rid:
                task_row = row
                break
    if not isinstance(task_row, dict):
        return None

    traces = [x for x in (task_row.get("process_trace") or []) if isinstance(x, dict)]
    if not traces:
        return None
    if qid:
        hit = next((x for x in traces if str(x.get("question_id", "") or "").strip() == qid), None)
        if isinstance(hit, dict):
            return hit
    hit = next(
        (
            x for x in traces
            if int(x.get("index", 0) or 0) == q_index
            and str(x.get("slice_id", "") or "").strip() == q_slice_id
        ),
        None,
    )
    return hit if isinstance(hit, dict) else None


def _hydrate_run_questions_from_task_if_needed(
    tenant_id: str,
    run: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    run_id = str(run.get("run_id", "") or "").strip()
    cfg = run.get("config") if isinstance(run.get("config"), dict) else {}
    task_id = str(cfg.get("task_id", "") or "").strip()
    questions = list(run.get("questions") or [])
    if questions and all(_is_effective_run_question_payload(q) for q in questions if isinstance(q, dict)):
        return run, False
    changed = False

    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            continue
        if _is_effective_judge_input(q.get("judge_input") if isinstance(q.get("judge_input"), dict) else None):
            continue
        trace = _find_task_trace_for_run_question(tenant_id, run_id, task_id, q)
        if not isinstance(trace, dict):
            continue

        q2 = dict(q)
        final_json = trace.get("final_json") if isinstance(trace.get("final_json"), dict) else {}
        if isinstance(final_json, dict) and final_json:
            q2["final_json"] = final_json
            q2["question_text"] = str(q2.get("question_text") or final_json.get("题干") or "")
            q2["answer"] = str(q2.get("answer") or final_json.get("正确答案") or "")
            q2["explanation"] = str(q2.get("explanation") or final_json.get("解析") or "")
            opts: list[str] = []
            for oi in range(1, 5):
                opt = str(final_json.get(f"选项{oi}", "") or "").strip()
                if opt:
                    opts.append(opt)
            if opts:
                q2["options"] = opts
        if not str(q2.get("slice_content", "") or "").strip():
            q2["slice_content"] = str(trace.get("slice_content", "") or "").strip()
        ji = _trace_to_question_input(trace, cfg, tenant_id)
        if _is_effective_judge_input(ji):
            q2["judge_input"] = {
                "stem": str(ji.get("stem", "") or ""),
                "options": list(ji.get("options") or []),
                "correct_answer": str(ji.get("correct_answer", "") or ""),
                "explanation": str(ji.get("explanation", "") or ""),
                "textbook_slice": str(ji.get("textbook_slice", "") or "(无切片原文)"),
                "related_slices": list(ji.get("related_slices") or []),
                "reference_slices": list(ji.get("reference_slices") or []),
                "question_type": str(ji.get("question_type", "") or ""),
                "city_name": str(ji.get("city_name", "") or ""),
            }
        if q2 != q:
            questions[i] = q2
            changed = True

    if changed:
        run = dict(run)
        run["questions"] = questions
    return run, changed


def _run_offline_judge_for_question(
    question: dict[str, Any],
    config_payload: dict[str, Any],
    llm: Any,
    *,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Run offline Judge on one question. Uses judge_input if present; otherwise builds input
    from question_text/answer/final_json so that post-hoc Judge (single-question evaluation)
    still invokes the Judge LLM.
    Returns report dict or None on error. On import/input failure returns dict with "error" key.
    """
    try:
        judge_dir = Path(__file__).resolve().parent / "离线Judge"
        if str(judge_dir) not in sys.path:
            sys.path.insert(0, str(judge_dir))
        from src.pipeline.runner import run_judge
        from src.schemas.evaluation import QuestionInput
    except Exception as e:
        return {"error": f"Judge 模块加载失败: {e!s}"}
    ji = _build_judge_input_from_question(question)
    if not ji:
        return {"error": "题目缺少 judge_input 且无法从题干/选项拼出"}
    cfg = dict(config_payload) if isinstance(config_payload, dict) else {}
    eff_tid = str(tenant_id or cfg.get("tenant_id", "") or "").strip() or None
    city_name = _resolve_judge_city_name(eff_tid, cfg)
    ji_city = str(ji.get("city_name", "") or "").strip()
    if ji_city:
        city_name = ji_city
    question_final_json = question.get("final_json") if isinstance(question.get("final_json"), dict) else {}
    question_type = _resolve_judge_question_type(
        preferred=(ji or {}).get("question_type") or question.get("question_type") or question_final_json.get("题目类型"),
        stem=str(ji.get("stem", "") or ""),
        options=list(ji.get("options") or []),
        correct_answer=str(ji.get("correct_answer", "") or ""),
        config_question_type=config_payload.get("question_type", "单选题"),
    )
    generation_mode = str(config_payload.get("generation_mode", "随机") or "随机")
    assessment_type = "实战应用/推演" if generation_mode == "实战应用/推演" else "基础概念/理解记忆"
    question_id = str(question.get("question_id", ""))
    trace_meta: dict[str, Any] = {
        "question_id": question_id,
        "judge_input": {
            "stem": str(ji.get("stem", "") or ""),
            "options": list(ji.get("options") or []),
            "correct_answer": str(ji.get("correct_answer", "") or ""),
            "explanation": str(ji.get("explanation", "") or ""),
            "textbook_slice": str(ji.get("textbook_slice", "") or "(无切片原文)"),
            "related_slices": list(ji.get("related_slices") or []),
            "reference_slices": list(ji.get("reference_slices") or []),
            "question_type": question_type,
            "assessment_type": assessment_type,
            "city_name": city_name,
        },
    }
    try:
        if not llm:
            return {"error": "Judge LLM 未配置或未传入，无法调用大模型"}
        qin = QuestionInput(
            question_id=question_id,
            stem=str(ji.get("stem", "") or ""),
            options=list(ji.get("options") or []),
            correct_answer=str(ji.get("correct_answer", "") or ""),
            explanation=str(ji.get("explanation", "") or ""),
            textbook_slice=str(ji.get("textbook_slice", "") or "(无切片原文)"),
            related_slices=list(ji.get("related_slices") or []),
            reference_slices=list(ji.get("reference_slices") or []),
            question_type=question_type,
            assessment_type=assessment_type,
            is_calculation=False,
            city_name=city_name,
        )
        report = run_judge(qin, llm)
        out = report.model_dump()
        # When solver_validation falls back to NONE (模型未返回可解析结果), record last raw LLM
        # response from Judge observability for debugging.
        debug_solver_raw: str | None = None
        try:
            solver = out.get("solver_validation") or {}
            predicted = str(solver.get("predicted_answer", "") or "")
            reasoning = str(solver.get("reasoning_path", "") or "")
            # Capture raw solver output whenever we failed to recover a unique answer.
            # Do not rely on a specific reasoning phrase because fallback wording may change.
            if predicted.upper() == "NONE":
                judge_dir = Path(__file__).resolve().parent / "离线Judge"
                if str(judge_dir) not in sys.path:
                    sys.path.insert(0, str(judge_dir))
                from src.llm.client import get_observability

                obs = get_observability()
                raw = str(obs.get("last_raw_response") or "")
                if raw:
                    debug_solver_raw = raw[:2000]
        except Exception:
            debug_solver_raw = None
        result: dict[str, Any] = {
            "decision": out.get("decision"),
            "overall_score": out.get("overall_score"),
            "baseline_score": out.get("baseline_score", out.get("penalty_score")),
            "quality_score": out.get("quality_score"),
            "quality_reasons": out.get("quality_reasons") or [],
            "quality_scoring_basis": out.get("quality_scoring_basis") or "",
            "quality_dimension_feedback": out.get("quality_dimension_feedback") or {},
            "scores": out.get("scores"),
            "dimension_results": out.get("dimension_results"),
            "reasons": out.get("reasons") or [],
            "actionable_feedback": out.get("actionable_feedback") or "",
            "hard_gate": out.get("hard_gate"),
            "semantic_drift": out.get("semantic_drift"),
            "solver_validation": out.get("solver_validation"),
            "distractor_quality": out.get("distractor_quality"),
            "knowledge_match": out.get("knowledge_match"),
            "teaching_value": out.get("teaching_value"),
            "risk_assessment": out.get("risk_assessment"),
            "observability": out.get("observability"),
            "costs": out.get("costs"),
        }
        if debug_solver_raw:
            result["debug_solver_raw_response_preview"] = debug_solver_raw
        # Attach core Judge state for per-question trace logging.
        trace_meta.update(
            {
                "solver_validation": out.get("solver_validation"),
                "calculation": out.get("calculation_data"),
                "decision": result.get("decision"),
                "overall_score": result.get("overall_score"),
                "baseline_score": result.get("baseline_score"),
                "quality_score": result.get("quality_score"),
                "hard_gate": result.get("hard_gate"),
            }
        )
        result["_qa_trace"] = trace_meta
        return result
    except Exception as e:
        return {"error": str(e)}


def _score_question_from_trace(question_trace: dict[str, Any]) -> dict[str, Any]:
    critic_result = question_trace.get("critic_result") if isinstance(question_trace.get("critic_result"), dict) else {}
    draft_revision = int(question_trace.get("draft_revision", 0) or 0)
    critic_revision = int(
        question_trace.get("critic_revision", critic_result.get("critic_revision", draft_revision)) or 0
    )
    if critic_result and bool(critic_result.get("passed")) is False and draft_revision > 0 and critic_revision < draft_revision:
        # Ignore stale critic result from older draft revisions.
        critic_result = {}
    llm_summary = question_trace.get("llm_summary") if isinstance(question_trace.get("llm_summary"), dict) else {}
    unstable_flags = [str(x) for x in (question_trace.get("unstable_flags") or []) if str(x)]
    all_issues = [str(x) for x in (critic_result.get("all_issues") or []) if str(x)]
    quality_issues = [str(x) for x in (critic_result.get("quality_issues") or []) if str(x)]
    missing_conditions = [str(x) for x in (critic_result.get("missing_conditions") or []) if str(x)]
    can_deduce_unique = bool(critic_result.get("can_deduce_unique_answer", False))
    passed = bool(critic_result.get("passed", False))
    saved = bool(question_trace.get("saved", False))
    final_status = "failed"
    if passed and saved:
        final_status = "passed_saved"
    elif passed and not saved:
        final_status = "passed_unsaved"

    # No quality scoring or risk reporting during 出题; keep structure for compatibility (zeros / no risk).
    logic_score = 0
    distractor_score = 0.0
    knowledge_match_score = 0.0
    teaching_value_score = 0.0
    risk_tags: list[str] = []
    risk_level = "low"

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
    options: list[str] = []
    for i in range(1, 5):
        opt = str(final_json.get(f"选项{i}", "") or "").strip()
        if opt:
            options.append(opt)
    if not options:
        options = [""]
    question_type = _resolve_judge_question_type(
        preferred=question_trace.get("question_type") or final_json.get("题目类型"),
        stem=str(final_json.get("题干", "") or "").strip(),
        options=options,
        correct_answer=str(final_json.get("正确答案", "") or "").strip(),
        config_question_type="",
    )
    judge_input = {
        "stem": str(final_json.get("题干", "") or "").strip(),
        "options": options,
        "correct_answer": str(final_json.get("正确答案", "") or "").strip(),
        "explanation": str(final_json.get("解析", "") or "").strip(),
        "textbook_slice": str(question_trace.get("slice_content", "") or "").strip() or "(无切片原文)",
        "related_slices": _normalize_slice_text_list(question_trace.get("related_slice_paths"), limit=20),
        "reference_slices": _normalize_slice_text_list(question_trace.get("reference_slices"), limit=20),
        "question_type": question_type,
    }
    return {
        "question_id": str(question_trace.get("question_id", "")),
        "index": int(question_trace.get("index", 0) or 0),
        "question_type": question_type,
        "slice_id": question_trace.get("slice_id"),
        "slice_path": str(question_trace.get("slice_path", "")),
        "judge_input": judge_input,
        "hard_gate": {
            "pass": passed,
            "failed_rules": [] if passed else [str(critic_result.get("reason", "hard_gate_failed"))],
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
        "critic_fail_types": [str(x) for x in (critic_result.get("fail_types") or []) if str(x)] if not passed else [],
        "llm_summary": llm_summary,
        "question_text": str(final_json.get("题干", "")),
        "answer": str(final_json.get("正确答案", "")),
        "draft_revision": draft_revision,
        "critic_revision": critic_revision,
        "final_status": final_status,
        "saved": saved,
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
    # Critic fail rate: questions where a critic failure (with reason) existed during the process, not only final state.
    # Count if: (1) final fail with reason, or (2) process had at least one reject (critic_loops >= 2).
    def _critic_fail_in_process(q: dict) -> bool:
        reason = str((q.get("issues") or {}).get("reason", "") or (q.get("critic_result") or {}).get("reason", "") or "").strip()
        if not bool(q.get("hard_gate", {}).get("pass")):
            return bool(reason)  # final fail: count only when there is a reason
        loops = int(q.get("stability", {}).get("critic_loops", 0) or 0)
        return loops >= 2  # passed but had at least one reject during process
    critic_fail_with_reason_cnt = sum(1 for q in questions if _critic_fail_in_process(q))
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

    # Critic rejection reason type counts (for quality evaluation)
    critic_fail_type_counts: dict[str, int] = {}
    for q in questions:
        if not bool(q.get("hard_gate", {}).get("pass")):
            for t in q.get("critic_fail_types") or []:
                if t:
                    critic_fail_type_counts[t] = critic_fail_type_counts.get(t, 0) + 1

    # CPVQ = Cost Per Valid Question: total_cost / saved_count when saved_count > 0
    saved = int(saved_count or 0)
    cpvq = round(_safe_div(total_cost, saved), 6) if saved > 0 else None
    judge_pass_cnt = sum(1 for q in questions if str((q.get("offline_judge") or {}).get("decision", "")).lower() == "pass")
    judge_review_cnt = sum(1 for q in questions if str((q.get("offline_judge") or {}).get("decision", "")).lower() == "review")
    judge_reject_cnt = sum(1 for q in questions if str((q.get("offline_judge") or {}).get("decision", "")).lower() == "reject")
    judge_with_result = judge_pass_cnt + judge_review_cnt + judge_reject_cnt
    judge_calls_sum = 0
    judge_failed_calls_sum = 0
    judge_prompt_tokens_sum = 0
    judge_completion_tokens_sum = 0
    judge_total_tokens_sum = 0
    judge_latency_ms_sum = 0
    judge_cost_usd_sum = 0.0
    for q in questions:
        oj = q.get("offline_judge") if isinstance(q.get("offline_judge"), dict) else {}
        obs = oj.get("observability") if isinstance(oj.get("observability"), dict) else {}
        tok = obs.get("tokens") if isinstance(obs.get("tokens"), dict) else {}
        costs = oj.get("costs") if isinstance(oj.get("costs"), dict) else {}
        judge_calls_sum += int(obs.get("llm_calls", 0) or 0)
        judge_failed_calls_sum += int(obs.get("failed_calls", 0) or 0)
        judge_prompt_tokens_sum += int(tok.get("prompt_tokens", 0) or 0)
        judge_completion_tokens_sum += int(tok.get("completion_tokens", 0) or 0)
        judge_total_tokens_sum += int(tok.get("total_tokens", 0) or 0)
        judge_latency_ms_sum += int(obs.get("latency_ms", 0) or 0)
        judge_cost_usd_sum += float(costs.get("per_question_usd", 0.0) or 0.0)
    judge_overall_scores = [float((q.get("offline_judge") or {}).get("overall_score", 0) or 0) for q in questions if (q.get("offline_judge") or {}).get("overall_score") is not None]
    judge_baseline_scores = [
        float(
            (q.get("offline_judge") or {}).get(
                "baseline_score",
                (q.get("offline_judge") or {}).get("penalty_score"),
            )
            or 0
        )
        for q in questions
        if (
            (q.get("offline_judge") or {}).get("baseline_score") is not None
            or (q.get("offline_judge") or {}).get("penalty_score") is not None
        )
    ]
    # When offline Judge has run and produced quality_score, use it for batch quality_score_avg (same formula as fusion node)
    judge_quality_scores = [float((q.get("offline_judge") or {}).get("quality_score")) for q in questions if (q.get("offline_judge") or {}).get("quality_score") is not None]
    if judge_quality_scores:
        quality_score_avg_val = round(_safe_div(sum(judge_quality_scores), len(judge_quality_scores)), 2)
    else:
        quality_score_avg_val = round((avg_logic * 0.5) + (avg_distractor * 10 * 0.15) + (avg_knowledge * 100 * 0.2) + (avg_teaching * 10 * 0.15), 2)
    batch_metrics = {
        "question_count": n,
        "generated_count": int(generated_count or 0),
        "saved_count": saved,
        "error_count": len(errors or []),
        "hard_pass_rate": round(_safe_div(hard_pass_cnt, n), 4),
        "quality_score_avg": quality_score_avg_val,
        "logic_pass_rate": round(_safe_div(logic_pass_cnt, n), 4),
        "out_of_scope_rate": round(_safe_div(out_of_scope_cnt, n), 4),
        "duplicate_rate": round(_safe_div(duplicate_cnt, n), 4),
        "risk_high_rate": round(_safe_div(critic_fail_with_reason_cnt, n), 4),  # now: critic fail rate (fail with reason / n)
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
        "cpvq": cpvq,
        "cpvq_currency": currency if cpvq is not None else None,
        "avg_cost_per_call": round(_safe_div(total_cost, len(llm_calls)), 6),
        "currency": currency,
        "error_calls": int(error_calls),
        "total_llm_calls": int(total_calls),
        "error_call_rate": round(_safe_div(error_calls, total_calls), 4),
        "critic_fail_type_counts": critic_fail_type_counts,
        "judge_total_llm_calls": int(judge_calls_sum),
        "judge_failed_llm_calls": int(judge_failed_calls_sum),
        "judge_total_prompt_tokens": int(judge_prompt_tokens_sum),
        "judge_total_completion_tokens": int(judge_completion_tokens_sum),
        "judge_total_tokens": int(judge_total_tokens_sum),
        "judge_total_latency_ms": int(judge_latency_ms_sum),
        "judge_total_cost_usd": round(judge_cost_usd_sum, 6),
        "judge_avg_tokens_per_question": round(_safe_div(judge_total_tokens_sum, judge_with_result), 2) if judge_with_result > 0 else 0.0,
        "judge_avg_latency_ms_per_question": round(_safe_div(judge_latency_ms_sum, judge_with_result), 2) if judge_with_result > 0 else 0.0,
        "judge_avg_cost_usd_per_question": round(_safe_div(judge_cost_usd_sum, judge_with_result), 6) if judge_with_result > 0 else 0.0,
    }
    if judge_with_result > 0:
        batch_metrics["judge_pass_count"] = judge_pass_cnt
        batch_metrics["judge_review_count"] = judge_review_cnt
        batch_metrics["judge_reject_count"] = judge_reject_cnt
        batch_metrics["judge_pass_rate"] = round(_safe_div(judge_pass_cnt, judge_with_result), 4)
        batch_metrics["judge_reject_rate"] = round(_safe_div(judge_reject_cnt, judge_with_result), 4)
        batch_metrics["judge_overall_score_avg"] = round(_safe_div(sum(judge_overall_scores), len(judge_overall_scores)), 2)
        if judge_baseline_scores:
            batch_metrics["judge_baseline_score_avg"] = round(_safe_div(sum(judge_baseline_scores), len(judge_baseline_scores)), 2)
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
        ("cpvq", "cpvq_max", "above_max"),
    ]
    for metric_key, threshold_key, mode in check_pairs:
        mv = metrics.get(metric_key)
        tv = thresholds.get(threshold_key)
        if tv is None:
            continue
        # CPVQ: only trigger when saved_count > 0 and cpvq is a number
        if metric_key == "cpvq":
            if mv is None or not isinstance(mv, (int, float)):
                continue
            trigger = mode == "above_max" and float(mv) > float(tv)
        elif mv is None:
            continue
        else:
            trigger = (mode == "below_min" and float(mv) < float(tv)) or (mode == "above_max" and float(mv) > float(tv))
        if not trigger:
            continue
        level = "high" if metric_key in {"hard_pass_rate", "logic_pass_rate", "risk_high_rate"} else "medium"  # risk_high_rate = critic_fail_rate
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
    # Alert when no valid questions but some were generated
    gen_count = int(metrics.get("generated_count", 0) or 0)
    saved_count_val = int(metrics.get("saved_count", 0) or 0)
    if gen_count > 0 and saved_count_val == 0:
        level = "high"
        sla_hours = int(thresholds.get(f"sla_hours_{level}", thresholds.get("sla_hours_medium", 72)) or 72)
        due_at = (created_dt + timedelta(hours=sla_hours)).isoformat()
        alerts.append(
            {
                "alert_id": f"alert_{run_id}_no_valid_questions",
                "tenant_id": str(qa_run.get("tenant_id", "")),
                "run_id": run_id,
                "question_id": "",
                "level": level,
                "type": "batch_metric",
                "metric": "saved_count",
                "threshold_key": "no_valid_questions",
                "metric_value": 0,
                "threshold_value": gen_count,
                "message": f"本批生成 {gen_count} 题但无有效入库题目 (saved_count=0)",
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
    _ensure_task_maintenance_started()
    if request.method == 'OPTIONS':
        return _json_response({'ok': True}, 200)
    # 允许非 /api/ 路径直接访问（例如在浏览器打开 127.0.0.1:8600 查看服务是否启动）
    path = request.path or ""
    if not path.startswith("/api/"):
        return None
    if path.startswith("/api/auth/"):
        return None
    # Allow unauthenticated GET for slice images so前端 Markdown 图片渲染不会因缺少头失败
    if request.method == "GET" and re.match(r"^/api/[^/]+/slices/image$", path):
        return None
    auth_header = (request.headers.get("Authorization") or "")
    system_user_header = (request.headers.get("X-System-User") or "")

    # 当 SSO 开启时，禁止通过 legacy X-System-User 头绕过 SSO 直接进入。
    # OIDC Bearer token 仍然允许（供机器账号 / 内部服务调用）。
    if SSO_MANAGER.enabled and system_user_header.strip() and not auth_header.strip():
        return _error("SSO_LEGACY_BYPASS_DENIED", "SSO 模式下不允许使用 X-System-User 头直接认证", 401)

    try:
        principal = resolve_principal(
            authorization_header=auth_header,
            system_user_header=system_user_header,
        )
    except AccessDenied as e:
        if SSO_MANAGER.enabled and not auth_header.strip() and not system_user_header.strip():
            try:
                principal = _resolve_principal_from_sso_session()
            except AccessDenied as sso_err:
                return _error(str(sso_err), "认证失败，请先完成单点登录", 401)
        else:
            return _error(str(e), "认证失败，请检查系统号或 OIDC Token", 401)
    g.principal = principal
    try:
        request_tick = int(os.times().elapsed * 1000)
    except Exception:
        request_tick = int(time.time() * 1000)
    g.request_id = f"{principal.system_user}-{os.getpid()}-{request_tick}"
    try:
        g.release_channel = select_release_channel(
            principal.system_user,
            forced_channel=(request.headers.get("X-Release-Channel") or ""),
        )
    except Exception:
        g.release_channel = "stable"
    api_key = f"{principal.system_user}:{request.path}"
    try:
        allowed, retry_after = rate_limiter.allow(api_key)
    except Exception:
        allowed, retry_after = True, 0
    if not allowed:
        resp = _error("RATE_LIMITED", f"请求过于频繁，请 {retry_after}s 后重试", 429)
        resp.headers["Retry-After"] = str(retry_after)
        return resp
    try:
        if not circuit_breaker.allow(request.path):
            return _error("CIRCUIT_OPEN", "服务正在自动恢复中，请稍后重试", 503)
    except Exception:
        pass
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
    if isinstance(err, TenantDataMissingError):
        return _error("TENANT_DATA_MISSING", str(err), 400)
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
                "sso": SSO_MANAGER.enabled,
                "rate_limit_rpm": int(os.getenv("ADMIN_API_RATE_LIMIT_RPM", "240")),
                "circuit_breaker": True,
                "otel_enabled": bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()),
            },
        }
    )


@app.get('/api/auth/meta')
def api_auth_meta():
    return_to = str(request.args.get("return_to", "/") or "/")
    if not SSO_MANAGER.enabled:
        return _json_response({"enabled": False, "logged_in": False})
    session = _get_sso_session()
    payload = _sso_public_session(session)
    payload.update(
        {
            "enabled": True,
            "login_url": SSO_MANAGER.login_redirect_url(return_to=return_to),
            "logout_url": SSO_MANAGER.logout_redirect_url(return_to=return_to),
        }
    )
    return _json_response(payload)


@app.get('/api/auth/login')
def api_auth_login():
    if not SSO_MANAGER.enabled:
        return _error("SSO_DISABLED", "单点登录未开启", 404)
    return_to = str(request.args.get("return_to", "/") or "/")
    level = str(request.args.get("level", "") or "")
    return redirect(SSO_MANAGER.login_redirect_url(return_to=return_to, level=level), code=302)


@app.get('/api/auth/callback')
def api_auth_callback():
    if not SSO_MANAGER.enabled:
        return _error("SSO_DISABLED", "单点登录未开启", 404)
    ticket = str(request.args.get("ticket", "")).strip()
    return_to = str(request.args.get("rt", "/") or "/")
    if not ticket:
        return _error("TICKET_REQUIRED", "缺少 ticket 参数", 400)
    service = SSO_MANAGER.service_url(return_to=return_to)
    try:
        cas_result = SSO_MANAGER.validate_ticket(ticket=ticket, service=service)
    except SSOError as e:
        return _error("CAS_VALIDATE_FAILED", str(e), 401)
    ucid = str(cas_result.get("ucid", "")).strip()
    binding = SSO_MANAGER.resolve_binding(ucid)
    if not binding:
        return _error("UCID_NOT_BOUND", "当前账号未绑定系统号，请联系管理员", 403)
    try:
        session = SSO_MANAGER.create_session(
            ucid=ucid,
            tenant_id=str(binding.get("tenant_id", "")).strip().lower(),
            accounts=list(binding.get("accounts") or []),
            st=ticket,
            business_token=str(cas_result.get("business_token", "")).strip(),
        )
    except SSOError as e:
        return _error("SSO_SESSION_CREATE_FAILED", str(e), 500)
    resp = redirect(SSO_MANAGER.frontend_redirect_url(return_to=return_to), code=302)
    resp.set_cookie(
        SSO_MANAGER.cookie_name,
        session.sid,
        max_age=SSO_MANAGER.session_ttl_sec,
        httponly=True,
        secure=SSO_MANAGER.cookie_secure,
        samesite="Lax",
        path="/",
    )
    return resp


@app.post('/api/auth/switch-system-user')
def api_auth_switch_system_user():
    if not SSO_MANAGER.enabled:
        return _error("SSO_DISABLED", "单点登录未开启", 404)
    sid = _get_sso_sid_from_cookie()
    if not sid:
        return _error("SSO_SESSION_REQUIRED", "请先登录", 401)
    body = request.get_json(silent=True) or {}
    target_system_user = str(body.get("system_user", "")).strip()
    if not target_system_user:
        return _error("SYSTEM_USER_REQUIRED", "system_user 不能为空", 400)
    try:
        session = SSO_MANAGER.switch_system_user(sid=sid, system_user=target_system_user)
    except SSOError as e:
        return _error(str(e), "切换系统号失败", 400)
    return _json_response({"ok": True, "session": _sso_public_session(session)})


@app.route('/api/auth/logout', methods=['GET', 'POST'])
def api_auth_logout():
    if not SSO_MANAGER.enabled:
        return _json_response({"ok": True})
    return_to = "/"
    if request.method == "GET":
        return_to = str(request.args.get("return_to", "/") or "/")
    else:
        body = request.get_json(silent=True) or {}
        return_to = str(body.get("return_to", "/") or "/")
    sid = _get_sso_sid_from_cookie()
    if sid:
        SSO_MANAGER.clear_session(sid)
    logout_url = SSO_MANAGER.logout_redirect_url(return_to=return_to)
    if request.method == "GET":
        resp = redirect(logout_url, code=302)
    else:
        resp = _json_response({"ok": True, "logout_url": logout_url})
    # 同时触发服务端过期 session 清理（非阻塞，异常不影响响应）
    try:
        SSO_MANAGER._get_store().purge_expired_sso_sessions()
    except Exception:
        pass
    resp.delete_cookie(
        SSO_MANAGER.cookie_name,
        path="/",
        samesite="Lax",
        secure=SSO_MANAGER.cookie_secure,
    )
    return resp


@app.get('/api/admin/key-config')
@app.get('/api/platform/key-config')
def api_admin_key_config_get():
    try:
        _require_platform_admin()
    except PermissionError as e:
        return _error(str(e), "仅平台管理员可管理全局 Key 配置", 403)
    exists = PRIMARY_KEY_FILE.exists()
    content = PRIMARY_KEY_FILE.read_text(encoding="utf-8") if exists else ""
    cfg = _load_primary_key_config()
    return _json_response(
        {
            "path": str(PRIMARY_KEY_FILE),
            "exists": exists,
            "content": content,
            "line_count": len(content.splitlines()) if content else 0,
            "updated_at": datetime.fromtimestamp(PRIMARY_KEY_FILE.stat().st_mtime, timezone.utc).isoformat()
            if exists else "",
            "has_ait_api_key": _is_usable_secret(cfg.get("AIT_API_KEY", "")),
            "has_openai_api_key": _is_usable_secret(cfg.get("OPENAI_API_KEY", "")),
            "has_deepseek_api_key": _is_usable_secret(cfg.get("DEEPSEEK_API_KEY", "")),
            "has_critic_api_key": _is_usable_secret(cfg.get("CRITIC_API_KEY", "")),
            "has_git_username": _is_usable_secret(cfg.get("GIT_USERNAME", "")),
            "has_git_token": _is_usable_secret(cfg.get("GIT_TOKEN", "")) or _is_usable_secret(cfg.get("GIT_PASSWORD", "")),
            "note": "该配置为平台全局配置，所有城市共用。",
        }
    )


@app.put('/api/admin/key-config')
@app.put('/api/platform/key-config')
def api_admin_key_config_put():
    try:
        _require_platform_admin()
    except PermissionError as e:
        return _error(str(e), "仅平台管理员可管理全局 Key 配置", 403)
    body = request.get_json(silent=True) or {}
    content = body.get("content")
    if content is None:
        items = body.get("items")
        if isinstance(items, dict):
            lines: list[str] = []
            for k, v in items.items():
                key = str(k or "").strip()
                if not key:
                    continue
                lines.append(f"{key}={str(v or '').strip()}")
            content = "\n".join(lines)
        else:
            content = ""
    result = _save_primary_key_config_text(str(content))
    return _json_response({"ok": True, "item": result})


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
    template_id = str(request.args.get('template_id', '')).strip()
    requested_material_version_id = str(request.args.get('material_version_id', '')).strip()
    if status != "all" and status not in SLICE_STATUSES:
        return _error("INVALID_STATUS", "非法切片状态", 400)
    page, page_size = _parse_pagination()
    template = _get_gen_template(tenant_id, template_id) if template_id else None
    if template_id and not template:
        return _error("TEMPLATE_NOT_FOUND", "出题模板不存在", 404)
    effective_material_version_request = (
        str(template.get("material_version_id", "")).strip()
        if template else requested_material_version_id
    )
    material_version_id = _resolve_material_version_id(tenant_id, effective_material_version_request)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    with start_span("api.slices", {"tenant_id": tenant_id, "status": status, "material_version_id": material_version_id, "path_prefix": path_prefix}):
        kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
        kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
        if not kb_items:
            return _json_response({"items": [], "total": 0, "page": page, "page_size": page_size, "material_version_id": material_version_id})

        display_paths = _build_display_paths(kb_items)
        reviews = _load_slice_review_for_material(tenant_id, material_version_id)
        generation_health = _load_slice_generation_health_for_material(tenant_id, material_version_id)
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
            health = generation_health.get(str(i), {}) if isinstance(generation_health.get(str(i)), dict) else {}
            generation_blocked = bool(health.get("blocked") or health.get("manual_blocked"))
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
                    'is_calculation_slice': _is_calculation_slice(s),
                    'generation_failure_count': int(health.get("failure_count", 0) or 0),
                    'generation_blocked': generation_blocked,
                    'generation_block_reason': str(health.get("blocked_reason", "") or ""),
                    'generation_last_fail_types': [str(x) for x in (health.get("last_fail_types") or []) if str(x).strip()],
                    'generation_last_error_content': str(health.get("last_error_content", "") or ""),
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
    template_id = str(request.args.get('template_id', '')).strip()
    requested_material_version_id = str(request.args.get("material_version_id", "")).strip()
    if not image_path:
        return _error("BAD_REQUEST", "path is required", 400)
    template = _get_gen_template(tenant_id, template_id) if template_id else None
    if template_id and not template:
        return _error("TEMPLATE_NOT_FOUND", "出题模板不存在", 404)
    effective_material_version_request = (
        str(template.get("material_version_id", "")).strip()
        if template else requested_material_version_id
    )
    material_version_id = _resolve_material_version_id(tenant_id, effective_material_version_request)
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
    if image_provider == "ark":
        image_base_url = config.get("IMAGE_BASE_URL") or config.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"
    else:
        image_base_url = (
            config.get("IMAGE_BASE_URL")
            or config.get("AIT_BASE_URL")
            or config.get("OPENAI_BASE_URL")
            or "https://openapi-ait.ke.com/v1"
        )
    ark_api_key = config.get("ARK_API_KEY") or ""
    volc_ak = config.get("VOLC_ACCESS_KEY_ID") or ""
    volc_sk = config.get("VOLC_SECRET_ACCESS_KEY") or ""
    ark_project_name = config.get("ARK_PROJECT_NAME") or ""
    if image_provider == "ark":
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
    _reset_slice_generation_health_for_material(
        tenant_id,
        material_version_id,
        slice_ids=[slice_id],
        reason="切片内容已修改，清空历史失败禁用状态",
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


@app.post('/api/<tenant_id>/slices/<int:slice_id>/generation/block')
def api_slice_generation_block(tenant_id: str, slice_id: int):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "slice.review")
    except PermissionError as e:
        return _error(str(e), "无权限禁用切片出题", 403)

    body = request.get_json(silent=True) or {}
    requested_material_version_id = str(body.get('material_version_id', '')).strip()
    reason = str(body.get('reason', '')).strip()
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
    if _is_slice_deleted(kb_items[slice_id]):
        return _error("SLICE_NOT_FOUND", "切片不存在", 404)

    health = _set_slice_generation_manual_block_for_material(
        tenant_id,
        material_version_id,
        slice_id=slice_id,
        blocker=system_user,
        reason=reason,
    )
    write_audit_log(
        tenant_id,
        system_user,
        'slice.generation.block',
        'slice_item',
        str(slice_id),
        after={
            'blocked': True,
            'blocked_reason': str(health.get('blocked_reason', '') or ''),
            'material_version_id': material_version_id,
        },
    )
    return _json_response({
        'ok': True,
        'slice_id': slice_id,
        'material_version_id': material_version_id,
        'generation_blocked': True,
        'generation_block_reason': str(health.get('blocked_reason', '') or ''),
    })


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
    _reset_slice_generation_health_for_material(
        tenant_id,
        material_version_id,
        slice_ids=[slice_id],
        reason="切片图片解析已修改，清空历史失败禁用状态",
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
    _reset_slice_generation_health_for_material(
        tenant_id,
        material_version_id,
        slice_ids=[base_id, *deleted_ids],
        reason="切片已合并，清空相关失败禁用状态",
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
    _reset_slice_generation_health_for_material(
        tenant_id,
        material_version_id,
        slice_ids=[new_slice_id],
        reason="新增切片，初始化失败禁用状态",
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


@app.post('/api/<tenant_id>/slices/<int:slice_id>/delete')
def api_slice_delete(tenant_id: str, slice_id: int):
    try:
        _check_tenant_permission(tenant_id, "slice.review")
    except PermissionError as e:
        return _error(str(e), "无权限删除切片", 403)

    body = request.get_json(silent=True) or {}
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    reviewer = str(body.get("reviewer") or _get_system_user() or "admin")

    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    if not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "未找到可用教材版本", 404)

    kb_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    if not kb_file:
        return _error("NO_SLICES_FILE", "当前教材切片文件不存在", 400)
    kb_items = _load_kb_items_from_file(kb_file)
    if slice_id < 0 or slice_id >= len(kb_items):
        return _error("BAD_REQUEST", f"slice_id out of range: {slice_id}", 400)
    if _is_slice_deleted(kb_items[slice_id]):
        return _error("BAD_REQUEST", f"slice already deleted: {slice_id}", 400)

    item = kb_items[slice_id] if isinstance(kb_items[slice_id], dict) else {}
    path = str(item.get("完整路径", "")).strip()
    path3 = _path_prefix(path, 3)
    deleted_at = datetime.now(timezone.utc).isoformat()

    item["__deleted__"] = True
    item["__deleted_at__"] = deleted_at
    item["核心内容"] = ""
    kb_items[slice_id] = item
    _save_kb_items_to_file(kb_file, kb_items)

    review_bucket = _load_slice_review_for_material(tenant_id, material_version_id)
    if str(slice_id) in review_bucket:
        review_bucket.pop(str(slice_id), None)
        _save_material_bucket(_slice_review_file_by_material(tenant_id), material_version_id, review_bucket)

    health_bucket = _load_slice_generation_health_for_material(tenant_id, material_version_id)
    if str(slice_id) in health_bucket:
        health_bucket.pop(str(slice_id), None)
        _save_slice_generation_health_for_material(tenant_id, material_version_id, health_bucket)

    if path3:
        order_bucket = _load_slice_order_for_material(tenant_id, material_version_id)
        group_ids = [sid for sid in order_bucket.get(path3, []) if int(sid) != int(slice_id)]
        if group_ids:
            order_bucket[path3] = group_ids
        else:
            order_bucket.pop(path3, None)
        _save_slice_order_for_material(tenant_id, material_version_id, order_bucket)

    write_audit_log(
        tenant_id,
        reviewer,
        "slice.delete",
        "slice_item",
        str(slice_id),
        after={"path": path, "path_prefix": path3, "material_version_id": material_version_id},
    )
    return _json_response(
        {
            "ok": True,
            "deleted": True,
            "slice_id": slice_id,
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
                slice_text = _build_complete_slice_content_for_mapping(
                    slice_item, slice_id, kb_items, path
                )
                raw_q_idx = int(q_idx)
                target_q_idx_text = str(review.get("target_mother_question_id", "") or "").strip()
                target_q_idx: int | None = None
                if target_q_idx_text and target_q_idx_text.isdigit():
                    target_q_idx = int(target_q_idx_text)
                effective_q_idx = target_q_idx if target_q_idx is not None else raw_q_idx
                q_row = dict(history_rows.get(int(effective_q_idx), {}) or {})
                manual_stem = str(review.get("manual_question_stem", "") or "").strip()
                manual_explanation = str(review.get("manual_question_explanation", "") or "").strip()
                manual_options = review.get("manual_question_options", [])
                if not isinstance(manual_options, list):
                    manual_options = []
                manual_options = [str(x or "").strip() for x in manual_options if str(x or "").strip()]
                manual_payload = {
                    "题干": manual_stem,
                    "选项": manual_options,
                    "解析": manual_explanation,
                    "正确答案": str(q_row.get("正确答案", "") or "").strip(),
                }
                manual_ready, _ = _is_mapping_review_ready(manual_payload)
                question_source = "manual" if manual_ready else ("history" if q_row else "none")
                if manual_ready:
                    q_row = manual_payload
                review_ready, review_missing_fields = _is_mapping_review_ready(q_row)
                image_items = _extract_slice_images(slice_item or {})
                items.append(
                    {
                        'map_key': map_key,
                        'slice_id': int(slice_id) if str(slice_id).isdigit() else slice_id,
                        'path': path,
                        'question_index': int(effective_q_idx),
                        'raw_question_index': raw_q_idx,
                        'target_mother_question_id': target_q_idx_text,
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
                        'question_options': q_row.get("选项", []) if isinstance(q_row.get("选项", []), list) else [],
                        'question_answer': q_row.get("正确答案", ""),
                        'question_explanation': q_row.get("解析", ""),
                        'manual_question_stem': manual_stem,
                        'manual_question_options': manual_options,
                        'manual_question_explanation': manual_explanation,
                        'question_source': question_source,
                        'review_ready': review_ready,
                        'review_missing_fields': review_missing_fields,
                        'material_version_id': material_version_id,
                    }
                )
        order_bucket = _load_slice_order_for_material(tenant_id, material_version_id)
        if items:
            group_anchor: dict[str, int] = {}
            rank_map: dict[tuple[str, int], int] = {}
            if order_bucket:
                for p3, ids in order_bucket.items():
                    for idx, sid in enumerate(ids):
                        rank_map[(p3, int(sid))] = idx
            for item in items:
                p3 = _path_prefix(item.get("path", ""), 3)
                sid_raw = item.get("slice_id", -1)
                sid = int(sid_raw) if str(sid_raw).isdigit() else 10**9
                anchor = group_anchor.get(p3)
                if anchor is None or sid < anchor:
                    group_anchor[p3] = sid

            def _mapping_sort_key(x: dict[str, Any]):
                p3 = _path_prefix(x.get("path", ""), 3)
                sid_raw = x.get("slice_id", -1)
                sid = int(sid_raw) if str(sid_raw).isdigit() else 10**9
                qid_raw = x.get("question_index", -1)
                qid = int(qid_raw) if str(qid_raw).isdigit() else 10**9
                return (
                    group_anchor.get(p3, sid),
                    rank_map.get((p3, sid), 1_000_000 + sid),
                    sid,
                    qid,
                )

            items.sort(key=_mapping_sort_key)
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
        # Self-heal stale material status: slicing should not remain after successful slicing.
        if (
            str(rec.get("status", "")).strip() == "slicing"
            and str(rec.get("slice_status", "")).strip() == "success"
        ):
            rec["status"] = "ready_for_review"
        slice_prog = _load_slice_progress_for_material(tenant_id, mid)
        rec["slice_progress"] = int(slice_prog.get("progress", 0) or 0)
        rec["slice_message"] = str(slice_prog.get("message", "") or "")
        if str(rec.get("slice_status", "")) == "success":
            rec["slice_progress"] = 100
            if not rec["slice_message"]:
                rec["slice_message"] = "切片处理完成"
        elif str(rec.get("slice_status", "")) == "running" and not rec["slice_message"]:
            rec["slice_message"] = "任务运行中"
        if not rec.get("mapping_status"):
            rec["mapping_status"] = "success" if rec["mapping_ready"] else "pending"
        # Self-heal stale status: marked success but mapping artifact is missing.
        if str(rec.get("mapping_status", "")).strip() == "success" and not rec["mapping_ready"]:
            rec["mapping_status"] = "pending"
            if not str(rec.get("mapping_error", "")).strip():
                rec["mapping_error"] = "映射文件缺失，请重新映射"
        job = _get_mapping_job_snapshot(tenant_id, mid)
        rec["mapping_job"] = job or {}
        if job:
            rec["mapping_progress"] = int(job.get("progress", 0) or 0)
            rec["mapping_message"] = str(job.get("message", "") or "")
            # Let frontend poll by material status as before.
            if str(job.get("status", "")) == "running":
                rec["mapping_status"] = "running"
                if not str(rec.get("mapping_error", "") or ""):
                    rec["mapping_error"] = rec["mapping_message"]
        mp = _load_mapping_progress_for_material(tenant_id, mid)
        mp_progress = int(mp.get("progress", 0) or 0)
        mp_message = str(mp.get("message", "") or "")
        mp_status = str(mp.get("status", "") or "")
        if mp_progress > 0 or mp_message:
            rec["mapping_progress"] = max(int(rec.get("mapping_progress", 0) or 0), mp_progress)
            if mp_message:
                rec["mapping_message"] = mp_message
        if mp_status == "running":
            rec["mapping_status"] = "running"
        elif mp_status == "failed":
            rec["mapping_status"] = "failed"
            if mp_message:
                rec["mapping_error"] = mp_message
        elif mp_status == "completed" and rec.get("mapping_ready"):
            rec["mapping_status"] = "success"
        can_set_effective, dual_review_slice_count = _has_dual_review_completed_slice(tenant_id, mid)
        rec["can_set_effective"] = bool(can_set_effective)
        rec["dual_review_slice_count"] = int(dual_review_slice_count)
        if can_set_effective:
            rec["effective_block_reason"] = ""
        else:
            rec["effective_block_reason"] = "需至少存在1条映射核对与切片核对都完成的知识切片"
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
        return _error("BAD_REQUEST", "请上传参考题文件（xlsx/xls/docx/txt/md）", 400)
    suffix = Path(source_file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xls", ".docx", ".txt", ".md"}:
        return _error("BAD_REQUEST", "参考题仅支持 xlsx/xls/docx/txt/md", 400)

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
    running_job = _get_mapping_job_snapshot(tenant_id, target)
    if running_job and str(running_job.get("status", "")) == "running":
        return _json_response(
            {
                "accepted": True,
                "material_version_id": target,
                "job": running_job,
                "message": "该教材映射任务正在执行中",
            },
            status=202,
        )

    upsert_material_runtime(
        tenant_id,
        target,
        mapping_status="running",
        mapping_error="",
    )
    _mapping_progress_file_for_material(tenant_id, target).unlink(missing_ok=True)
    _delete_material_bucket(_mapping_review_file_by_material(tenant_id), target)
    mapping_dir = tenant_root(tenant_id) / "mapping"
    mapping_dir.mkdir(parents=True, exist_ok=True)
    output_path = mapping_dir / f"knowledge_question_mapping_{target}.json"
    job = _update_mapping_job(
        tenant_id,
        target,
        {
            "status": "pending",
            "progress": 0,
            "message": "任务已排队，等待执行",
            "mapping_total": 0,
            "started_at": "",
            "ended_at": "",
            "kb_file": str(kb_file),
            "history_file": str(history_copy),
            "output_file": str(output_path),
            "reference_file": str(ref_path),
        },
    )
    t = threading.Thread(
        target=_run_material_mapping_job_worker,
        kwargs={
            "tenant_id": tenant_id,
            "material_version_id": target,
            "system_user": system_user,
            "kb_file": kb_file,
            "history_file": history_copy,
            "output_path": output_path,
            "audit_action": "material.upload.reference_map",
            "reference_file": str(ref_path),
        },
        daemon=True,
    )
    t.start()
    return _json_response(
        {
            "accepted": True,
            "material_version_id": target,
            "reference_file": str(ref_path),
            "mapping_file": str(output_path),
            "job": job,
            "message": "映射任务已启动，请在教材列表查看进度",
        },
        status=202,
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
    _delete_material_bucket(_slice_generation_health_file_by_material(tenant_id), target)
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
    next_status = prev_status if prev_status in {"effective", "archived"} else "ready_for_review"
    upsert_material_runtime(
        tenant_id,
        target,
        status=next_status,
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
    running_job = _get_mapping_job_snapshot(tenant_id, target)
    if running_job and str(running_job.get("status", "")) == "running":
        return _json_response(
            {
                "accepted": True,
                "material_version_id": target,
                "job": running_job,
                "message": "该教材映射任务正在执行中",
            },
            status=202,
        )
    upsert_material_runtime(
        tenant_id,
        target,
        mapping_status="running",
        mapping_error="",
    )
    _mapping_progress_file_for_material(tenant_id, target).unlink(missing_ok=True)

    mapping_dir = tenant_root(tenant_id) / "mapping"
    mapping_dir.mkdir(parents=True, exist_ok=True)
    output_path = mapping_dir / f"knowledge_question_mapping_{target}.json"
    job = _update_mapping_job(
        tenant_id,
        target,
        {
            "status": "pending",
            "progress": 0,
            "message": "任务已排队，等待执行",
            "mapping_total": 0,
            "started_at": "",
            "ended_at": "",
            "kb_file": str(kb_file),
            "history_file": str(history_file),
            "output_file": str(output_path),
            "reference_file": str(history_file),
        },
    )
    t = threading.Thread(
        target=_run_material_mapping_job_worker,
        kwargs={
            "tenant_id": tenant_id,
            "material_version_id": target,
            "system_user": system_user,
            "kb_file": kb_file,
            "history_file": history_file,
            "output_path": output_path,
            "audit_action": "material.remap",
            "reference_file": str(history_file),
        },
        daemon=True,
    )
    t.start()
    return _json_response(
        {
            "accepted": True,
            "material_version_id": target,
            "mapping_file": str(output_path),
            "job": job,
            "reference_file": str(history_file),
            "message": "重新映射任务已启动，请在教材列表查看进度",
        },
        status=202,
    )


@app.get('/api/<tenant_id>/materials/<material_version_id>/mapping-job')
def api_material_mapping_job(tenant_id: str, material_version_id: str):
    try:
        _check_tenant_permission(tenant_id, "material.read")
        _check_tenant_permission(tenant_id, "map.read")
    except PermissionError as e:
        return _error(str(e), "无权限查看映射任务", 403)

    target = str(material_version_id).strip()
    if not target:
        return _error("BAD_REQUEST", "material_version_id is required", 400)
    job = _get_mapping_job_snapshot(tenant_id, target)
    mp = _load_mapping_progress_for_material(tenant_id, target)
    if job:
        j = dict(job)
        j["progress"] = max(int(j.get("progress", 0) or 0), int(mp.get("progress", 0) or 0))
        if str(mp.get("message", "") or "").strip():
            j["message"] = str(mp.get("message", "") or "")
        mp_status = str(mp.get("status", "") or "")
        if mp_status in {"running", "failed", "completed"}:
            j["status"] = mp_status
        job = j
        return _json_response({"job": job})
    current = _find_material_record(tenant_id, target)
    if not current:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    status = str(mp.get("status", "") or current.get("mapping_status", "") or "pending")
    msg = str(mp.get("message", "") or current.get("mapping_error", "") or "")
    progress = int(mp.get("progress", 0) or 0)
    if status in {"success", "completed", "failed"}:
        progress = 100
    return _json_response(
        {
            "job": {
                "job_id": "",
                "tenant_id": tenant_id,
                "material_version_id": target,
                "status": "completed" if status == "success" else ("failed" if status == "failed" else status),
                "progress": progress,
                "message": msg,
                "mapping_total": 0,
                "created_at": "",
                "started_at": "",
                "ended_at": "",
                "updated_at": str(current.get("updated_at", "") or ""),
            }
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
    current = _find_material_record(tenant_id, material_version_id)
    if not current:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
    slice_status = str(current.get("slice_status", "") or "").strip()
    mapping_status = str(current.get("mapping_status", "") or "").strip()
    slice_file = _resolve_slice_file_for_material(tenant_id, material_version_id)
    mapping_file = _resolve_mapping_path_for_material(tenant_id, material_version_id)
    if slice_status != "success" or not slice_file:
        return _error("MATERIAL_NOT_READY", "切片未成功，不能设为生效教材", 400)
    if mapping_status != "success" or not mapping_file:
        return _error("MATERIAL_NOT_READY", "映射未成功，不能设为生效教材", 400)
    has_dual_review_slice, _ = _has_dual_review_completed_slice(tenant_id, material_version_id)
    if not has_dual_review_slice:
        return _error("MATERIAL_REVIEW_NOT_READY", "需至少存在1条映射核对与切片核对都完成的知识切片，才能设为生效教材", 400)
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

    updated = archive_material_version(tenant_id, target)
    if not updated:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)
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
        return _error("MATERIAL_EFFECTIVE", "当前版本是生效教材，请先下线该教材或使用强制删除", 409)

    cleanup_stats = _cleanup_material_artifacts(tenant_id, target)
    deleted = delete_material_version(tenant_id, target)
    if not deleted:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

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
    version_id = _new_material_version_id(tenant_id, now)
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
    upsert_material_runtime(
        tenant_id,
        version_id,
        status="ready_for_review",
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


@app.get('/api/<tenant_id>/generate/templates')
def api_list_generate_templates(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限查看出题模板", 403)
    items = [_sanitize_gen_template(item) for item in _load_gen_templates(tenant_id)]
    items.sort(key=lambda item: str(item.get("updated_at", "") or item.get("created_at", "")), reverse=True)
    return _json_response({"items": items})


@app.post('/api/<tenant_id>/generate/templates')
def api_create_generate_template(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限创建出题模板", 403)
    body = request.get_json(silent=True) or {}
    try:
        item = _validate_gen_template_payload(tenant_id, body)
    except ValueError as e:
        return _error("BAD_REQUEST", str(e), 400)
    now = datetime.now(timezone.utc).isoformat()
    item["template_id"] = f"tpl_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    item["created_at"] = now
    item["updated_at"] = now
    items = _load_gen_templates(tenant_id)
    items.append(item)
    _save_gen_templates(tenant_id, items)
    write_audit_log(
        tenant_id,
        system_user,
        "gen.template.create",
        "question_generation_template",
        item["template_id"],
        after=_sanitize_gen_template(item),
    )
    return _json_response({"item": _sanitize_gen_template(item)})


@app.put('/api/<tenant_id>/generate/templates/<template_id>')
def api_update_generate_template(tenant_id: str, template_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限修改出题模板", 403)
    body = request.get_json(silent=True) or {}
    existing = _load_gen_templates(tenant_id)
    target = None
    for item in existing:
        if str(item.get("template_id", "")).strip() == str(template_id).strip():
            target = item
            break
    if not target:
        return _error("TEMPLATE_NOT_FOUND", "出题模板不存在", 404)
    try:
        normalized = _validate_gen_template_payload(tenant_id, body, template_id=str(template_id).strip())
    except ValueError as e:
        return _error("BAD_REQUEST", str(e), 400)
    normalized["template_id"] = str(template_id).strip()
    normalized["created_at"] = str(target.get("created_at", "")).strip() or datetime.now(timezone.utc).isoformat()
    normalized["updated_at"] = datetime.now(timezone.utc).isoformat()
    updated_items = []
    for item in existing:
        if str(item.get("template_id", "")).strip() == str(template_id).strip():
            updated_items.append(normalized)
        else:
            updated_items.append(item)
    _save_gen_templates(tenant_id, updated_items)
    write_audit_log(
        tenant_id,
        system_user,
        "gen.template.update",
        "question_generation_template",
        normalized["template_id"],
        after=_sanitize_gen_template(normalized),
    )
    return _json_response({"item": _sanitize_gen_template(normalized)})


@app.delete('/api/<tenant_id>/generate/templates/<template_id>')
def api_delete_generate_template(tenant_id: str, template_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限删除出题模板", 403)
    template_id = str(template_id or "").strip()
    if not template_id:
        return _error("BAD_REQUEST", "template_id is required", 400)
    items = _load_gen_templates(tenant_id)
    remaining = [item for item in items if str(item.get("template_id", "")).strip() != template_id]
    if len(remaining) == len(items):
        return _error("TEMPLATE_NOT_FOUND", "出题模板不存在", 404)
    _save_gen_templates(tenant_id, remaining)
    write_audit_log(
        tenant_id,
        system_user,
        "gen.template.delete",
        "question_generation_template",
        template_id,
        after={"template_id": template_id, "deleted": True},
    )
    return _json_response({"ok": True, "template_id": template_id})


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
    generation_mode = _normalize_generation_mode(body.get("generation_mode", "随机"))
    if generation_mode not in GEN_MODES:
        generation_mode = "随机"
    difficulty = str(body.get("difficulty", "随机"))
    slice_ids_input = body.get("slice_ids") or []
    template_id = str(body.get("template_id", "")).strip()
    planned_slice_ids_input = body.get("planned_slice_ids") or []
    planned_slots_input = body.get("planned_slots") or []
    planned_slots_input = body.get("planned_slots") or []
    persist_to_bank = bool(body.get("persist_to_bank", body.get("save_to_bank", True)))
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    task_id = str(body.get("task_id", "")).strip()
    task_name = str(body.get("task_name", "")).strip()
    parent_task_id = str(body.get("parent_task_id", "")).strip()
    child_kind = str(body.get("child_kind", "")).strip()
    is_internal_subtask_request = bool(parent_task_id or child_kind)
    if gen_scope_mode not in {"custom", "per_slice"}:
        return _error("BAD_REQUEST", "非法出题范围模式", 400)
    if question_type not in QUESTION_TYPES:
        return _error("BAD_REQUEST", "非法题型", 400)
    if generation_mode not in GEN_MODES:
        return _error("BAD_REQUEST", "非法出题模式", 400)
    max_graph_rounds_per_question = max(1, int(os.getenv("MAX_GRAPH_ROUNDS_PER_QUESTION", "3") or 3))
    max_question_elapsed_ms = max(1000, int(os.getenv("MAX_QUESTION_ELAPSED_MS", "900000") or 900000))

    template = _get_gen_template(tenant_id, template_id) if template_id else None
    if template_id and not template:
        return _error("TEMPLATE_NOT_FOUND", "出题模板不存在", 404)
    effective_material_version_request = (
        str(template.get("material_version_id", "")).strip()
        if template else requested_material_version_id
    )
    material_version_id = _resolve_material_version_id(tenant_id, effective_material_version_request)
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

    if not kb_file:
        return _error("NO_SLICES_FILE", "当前城市没有可用切片文件", 400)
    history_path = str(_resolve_history_path_for_material(tenant_id, material_version_id))
    mapping_path = str(_resolve_mapping_path_for_material(tenant_id, material_version_id) or tenant_mapping_path(tenant_id))
    mapping_review_path = str(tenant_mapping_review_path(tenant_id))
    retriever = _get_cached_retriever(
        tenant_id=tenant_id,
        kb_path=str(kb_file),
        history_path=history_path,
        mapping_path=mapping_path,
        mapping_review_path=mapping_review_path,
    )
    candidate_ids = [sid for sid in candidate_ids if 0 <= sid < len(retriever.kb_data)]
    if not candidate_ids:
        return _error("NO_VALID_SLICES", "审核记录与当前切片版本不一致，请重新审核切片后再出题", 400)
    candidate_ids, skipped_type_conflicts = _filter_candidate_ids_by_question_type(retriever, candidate_ids, question_type)
    if not candidate_ids:
        skipped_msg = "；".join(
            f"slice_id={x.get('slice_id')}({x.get('reason')})"
            for x in (skipped_type_conflicts[:8] if isinstance(skipped_type_conflicts, list) else [])
        )
        return _error(
            "NO_TYPE_COMPATIBLE_SLICES",
            f"所选题型在当前切片范围内均被禁止，未生成题目。{skipped_msg}".strip(),
            400,
        )
    blocked_slice_ids = _blocked_slice_ids_for_material(tenant_id, material_version_id)
    candidate_ids = [sid for sid in candidate_ids if sid not in blocked_slice_ids]
    if not candidate_ids:
        return _error("NO_UNBLOCKED_SLICES", "当前范围内切片均已因累计失败被禁用，请先修复切片", 400)
    candidate_slices = []
    for sid in candidate_ids:
        kb_item = retriever.kb_data[sid]
        candidate_slices.append(
            {
                "slice_id": sid,
                "path": str(kb_item.get("完整路径", "")).strip(),
                "mastery": str(kb_item.get("掌握程度", "")).strip(),
            }
        )
    candidate_lookup = _build_slice_candidate_lookup(
        candidate_slices,
        template_route_rules=(template or {}).get("route_rules") if template else None,
    )
    planned_slice_ids: list[int] = []
    planned_slots: list[dict[str, Any]] = []
    template_plan: dict[str, Any] | None = None
    if template:
        num_questions = int(template.get("question_count", num_questions) or num_questions)
        precheck_report = _build_template_reachability_report(
            question_count=num_questions,
            template=template,
            candidate_slices=candidate_slices,
        )
        try:
            template_plan = _build_generation_template_plan(
                question_count=num_questions,
                template=template,
                candidate_slices=candidate_slices,
            )
        except ValueError as e:
            return _error(
                "TEMPLATE_PLAN_INVALID",
                _format_template_reachability_error(str(e), precheck_report),
                400,
            )
        planned_slots = [
            slot for slot in (template_plan.get("planned_slots") or [])
            if isinstance(slot, dict) and int(slot.get("slice_id", -1)) in candidate_ids
        ]
        planned_slice_ids = [int(slot.get("slice_id")) for slot in planned_slots]
    else:
        for slot in planned_slots_input:
            if not isinstance(slot, dict):
                continue
            try:
                sid_int = int(slot.get("slice_id"))
            except (TypeError, ValueError):
                continue
            if sid_int not in candidate_ids:
                continue
            planned_slots.append(
                {
                    "slice_id": sid_int,
                    "route_prefix": str(slot.get("route_prefix", "") or "").strip(),
                    "mastery": str(slot.get("mastery", "") or "").strip(),
                    "_global_target_index": int(slot.get("_global_target_index", 0) or 0),
                }
            )
        for sid in planned_slice_ids_input:
            try:
                sid_int = int(sid)
            except (TypeError, ValueError):
                continue
            if sid_int in candidate_ids:
                planned_slice_ids.append(sid_int)
        if planned_slots and not planned_slice_ids:
            planned_slice_ids = [int(slot.get("slice_id")) for slot in planned_slots]
        if gen_scope_mode == "per_slice":
            num_questions = len(candidate_ids)
    if planned_slots:
        num_questions = len(planned_slots)
    elif planned_slice_ids:
        num_questions = len(planned_slice_ids)
    if num_questions <= 0:
        return _error("BAD_REQUEST", "题量必须大于0", 400)
    target_question_count = num_questions
    if is_internal_subtask_request:
        # 子任务内失败会重试同一位次；attempt 含 critic 熔断、换切片等，2×题量过易 partial，提高上限以便在分片内尽量补满。
        max_attempts = min(400, max(1, target_question_count * 6 + 16))
    else:
        max_attempts = min(max(target_question_count * 3, target_question_count + 3), 600)
    api_key, base_url, model_name = _resolve_generation_llm_from_primary_key()
    if not api_key:
        return _error("NO_API_KEY", "未配置可用 API Key，请检查 填写您的Key.txt", 400)

    difficulty_range = _parse_difficulty_range(difficulty)
    run_started_at = datetime.now(timezone.utc).isoformat()
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    generated: list[dict[str, Any]] = []
    errors: list[str] = []
    process_trace: list[dict[str, Any]] = []
    saved = 0
    bank_path = tenant_bank_path(tenant_id)
    if task_id and _is_task_cancelled(task_id):
        run_ended_at = datetime.now(timezone.utc).isoformat()
        qa_run = _build_qa_run_payload(
            tenant_id=tenant_id,
            run_id=run_id,
            material_version_id=material_version_id,
            config_payload={
                "tenant_id": tenant_id,
                "question_type": question_type,
                "generation_mode": generation_mode,
                "difficulty": difficulty,
                "difficulty_range": difficulty_range,
                "num_questions": target_question_count,
                "max_attempts": max_attempts,
                "model": model_name,
                "gen_scope_mode": gen_scope_mode,
                "persist_to_bank": persist_to_bank,
                "save_to_bank": persist_to_bank,
                "task_id": task_id,
                "template_id": template_id,
                "template_name": str(template.get("name", "")).strip() if template else "",
                "template_snapshot": template if template else None,
                "template_plan": template_plan,
                "persist_to_bank": persist_to_bank,
                "enable_offline_judge": False,
            },
            process_trace=[],
            generated_count=0,
            saved_count=0,
            errors=["用户取消"],
            started_at=run_started_at,
            ended_at=run_ended_at,
        )
        _persist_qa_run(tenant_id, qa_run)
        return _json_response({
            "run_id": run_id,
            "items": [],
            "generated_count": 0,
            "saved_count": 0,
            "errors": ["用户取消"],
            "process_trace": [],
            "material_version_id": material_version_id,
            "cancelled": True,
        })
    cancelled_by_user = False
    fuse_threshold = 5
    fuse_triggered = False
    fuse_info: dict[str, Any] | None = None
    failure_key_counts: dict[str, int] = {}
    failure_examples: dict[str, dict[str, Any]] = {}
    task_slice_failure_counts: dict[int, int] = {}
    task_slice_usage_counts = _normalize_slice_usage_counts(body.get("used_slice_counts") or {})
    max_questions_per_slice = max(0, int(body.get("max_questions_per_slice", 2 if template else 0) or 0))
    random_difficulty_buckets = _random_difficulty_buckets() if difficulty == "随机" else []
    attempt_count = 0
    task_excluded_slice_ids: set[int] = set(blocked_slice_ids)
    _template_slot_cursor = 0
    _template_skipped_slots: set[int] = set()
    while len(generated) < target_question_count and attempt_count < max_attempts:
        if task_id and _is_task_cancelled(task_id):
            cancelled_by_user = True
            break
        success_index = len(generated)
        if planned_slots:
            while _template_slot_cursor in _template_skipped_slots:
                _template_slot_cursor += 1
            success_index = _template_slot_cursor
            if success_index >= len(planned_slots):
                break
        attempt_count += 1
        allow_retry_on_current_slot = bool(template) and _is_template_same_mastery_hard_gap(
            planned_slots=planned_slots,
            success_index=success_index,
            sid=int(planned_slice_ids[success_index]) if planned_slice_ids and success_index < len(planned_slice_ids) else -1,
            candidate_lookup=candidate_lookup,
        )
        template_gap_final_failure = False
        sid, sid_pick_error = _choose_generation_slice_id(
            planned_slice_ids=planned_slice_ids,
            planned_slots=planned_slots,
            success_index=success_index,
            candidate_ids=candidate_ids,
            attempt_count=attempt_count,
            target_question_count=target_question_count,
            excluded_slice_ids=task_excluded_slice_ids,
            candidate_lookup=candidate_lookup,
            slice_usage_counts=task_slice_usage_counts,
            max_questions_per_slice=max_questions_per_slice,
        )
        if sid is None:
            errors.append(sid_pick_error or "无可用切片")
            if planned_slots and success_index < len(planned_slots):
                _template_skipped_slots.add(success_index)
                continue
            break
        kb_chunk = retriever.kb_data[sid]
        effective_difficulty_range = (
            random_difficulty_buckets[success_index % len(random_difficulty_buckets)]
            if random_difficulty_buckets
            else difficulty_range
        )
        started_at = datetime.now(timezone.utc)
        step_seq = 0
        last_step_time = started_at
        current_run_id = 0  # round index for question generation: first route stays 0, reroute becomes 1/2...
        router_seen = False
        seen_logs: set[str] = set()
        seen_step_keys: set[str] = set()
        trace_id = uuid.uuid4().hex
        question_id = f"{tenant_id}:{material_version_id or 'default'}:{attempt_count}:{sid}:{trace_id[:8]}"
        question_llm_trace: list[dict[str, Any]] = []
        current_target_index = _planned_slot_target_index(planned_slots, success_index)
        question_trace: dict[str, Any] = {
            "run_id": run_id,
            "index": attempt_count,
            "target_index": current_target_index,
            "slice_id": sid,
            "slice_path": str(kb_chunk.get("完整路径", "")),
            "slice_content": _extract_slice_text(kb_chunk),
            "trace_id": trace_id,
            "question_id": question_id,
            "question_type": "",
            "difficulty_range": list(effective_difficulty_range) if effective_difficulty_range else None,
            "steps": [],
            "critic_result": {},
            "snapshot_stage": "live",
            "saved": False,
            "active_run_id": 0,
            "final_json_expired": False,
            "draft_revision": 0,
            "critic_revision": 0,
            **_planned_slot_trace_fields(planned_slots, success_index),
        }

        def _append_step(message: str, *, node: str = "", level: str = "info", detail: str = "") -> None:
            nonlocal step_seq, last_step_time
            dedupe_key = f"{current_run_id}|{node}|{level}|{message}|{detail}"
            if dedupe_key in seen_step_keys:
                return
            seen_step_keys.add(dedupe_key)
            step_seq += 1
            now = datetime.now(timezone.utc)
            elapsed_ms = int((now - started_at).total_seconds() * 1000)
            delta_ms = int((now - last_step_time).total_seconds() * 1000) if last_step_time else None
            last_step_time = now
            question_trace["steps"].append({
                "seq": step_seq,
                "node": node,
                "level": level,
                "message": message,
                "detail": detail,
                "time": now.isoformat(),
                "elapsed_ms": elapsed_ms,
                "delta_ms": delta_ms,
                "run_id": current_run_id,
            })

        _append_step("开始出题", node="system", detail=f"切片ID={sid}")
        if effective_difficulty_range:
            _append_step(
                "本题难度目标",
                node="system",
                detail=f"{effective_difficulty_range[0]:.1f}-{effective_difficulty_range[1]:.1f}",
            )
        if task_id:
            # Progress numerator means completed count (not in-progress index).
            _update_task_live(
                tenant_id,
                task_id,
                {
                    "progress": {"current": len(generated), "total": target_question_count},
                    "current_node": "system",
                    "current_node_updated_at": datetime.now(timezone.utc).isoformat(),
                },
                [question_trace],
            )
            _persist_live_task_snapshot(tenant_id, task_id)
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
                "difficulty_range": effective_difficulty_range,
            }
        }
        q_json = None
        mother_questions: list[str] = []
        mother_full_questions: list[dict[str, Any]] = []
        saved_current = False
        saved_with_issues_current = False
        critic_seen = False
        critic_passed = False
        critic_reject_count = 0
        abort_question_attempt = False
        abort_question_reason = ""
        attempt_error_info: dict[str, Any] | None = None
        whitelist_saved_current = False
        _wall_token = attach_question_wall_clock_budget(
            started_at_utc=started_at,
            max_elapsed_ms=max_question_elapsed_ms,
        )
        try:
            for event in graph_app.stream(inputs, config=config):
                for node_name, state_update in event.items():
                    if not isinstance(state_update, dict):
                        continue
                    extracted_mothers = _extract_mother_questions_from_examples(state_update.get("examples"))
                    if extracted_mothers:
                        mother_questions = extracted_mothers
                        question_trace["mother_questions"] = mother_questions
                    extracted_mothers_full = _extract_mother_question_full_from_examples(state_update.get("examples"))
                    if extracted_mothers_full:
                        mother_full_questions = extracted_mothers_full
                        question_trace["mother_questions_full"] = mother_full_questions
                    current_qt = str(state_update.get("current_question_type", "") or "").strip()
                    if current_qt in {"单选题", "多选题", "判断题"}:
                        question_trace["question_type"] = current_qt
                    related_paths = _normalize_related_slice_paths(state_update.get("critic_basis_paths"))
                    if related_paths:
                        question_trace["related_slice_paths"] = related_paths
                        question_trace["related_slice_count"] = len(related_paths)
                    # Report current graph node so timeout can show where it got stuck
                    if task_id:
                        if _is_task_cancelled(task_id):
                            cancelled_by_user = True
                            break
                        trace_updates = None
                        if question_trace.get("steps"):
                            # Set elapsed_ms so client sees real elapsed time during run
                            question_trace["elapsed_ms"] = int(
                                (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
                            )
                            trace_updates = [question_trace]
                        _update_task_live(
                            tenant_id,
                            task_id,
                            {
                                # Progress reflects accepted questions so retries do not overflow total.
                                "progress": {"current": len(generated), "total": target_question_count},
                                "current_node": node_name,
                                "current_node_updated_at": datetime.now(timezone.utc).isoformat(),
                            },
                            trace_updates,
                        )
                        _persist_live_task_snapshot(tenant_id, task_id)
                    if node_name == "router":
                        details = state_update.get("router_details") or {}
                        agent = details.get("agent")
                        path = details.get("path")
                        _append_step(
                            "路由完成",
                            node=node_name,
                            detail=f"agent={agent or '-'} path={path or '-'}",
                        )
                        if router_seen:
                            _mark_live_final_json_stale(question_trace, _append_step)
                            current_run_id += 1  # reroute starts next round; first route remains round 0
                            question_trace["active_run_id"] = current_run_id
                        else:
                            router_seen = True
                            question_trace["active_run_id"] = current_run_id
                    # Show specialist/calculator logs so "随机题型：本题已选定【X】" is visible
                    if node_name in ("specialist", "calculator"):
                        logs = state_update.get("logs") or []
                        if isinstance(logs, list):
                            for log in logs:
                                text = str(log).strip()
                                if not text or text in seen_logs:
                                    continue
                                seen_logs.add(text)
                                _append_step(text, node=node_name)
                    if node_name == "critic":
                        critic_result = state_update.get("critic_result") or {}
                        if isinstance(critic_result, dict) and ("passed" in critic_result):
                            current_draft_revision = int(question_trace.get("draft_revision", 0) or 0)
                            critic_revision = int(critic_result.get("critic_revision", current_draft_revision) or 0)
                            critic_result = dict(critic_result)
                            critic_result["critic_revision"] = critic_revision
                            question_trace["critic_result"] = critic_result
                            question_trace["critic_revision"] = critic_revision
                            fail_types_preview, error_content_preview = _extract_critic_issue_record(critic_result)
                            question_trace["critic_last_fail_types"] = fail_types_preview
                            question_trace["critic_last_error_content"] = error_content_preview
                            critic_details = state_update.get("critic_details")
                            if critic_details is not None:
                                question_trace["critic_details"] = str(critic_details).strip()
                            critic_seen = True
                            passed = bool(critic_result.get("passed"))
                            critic_passed = passed
                            reason = str(critic_result.get("reason", "")).strip()
                            if not reason and not passed:
                                reason = str(question_trace.get("critic_details", "")).strip() or "审核未通过（原因未返回）"
                            _append_step(
                                "审核通过" if passed else "审核驳回",
                                node=node_name,
                                level="success" if passed else "warning",
                                detail=reason or ("" if passed else "审核未通过（原因未返回）"),
                            )
                            if not passed:
                                critic_reject_count += 1
                                if critic_reject_count > 3:
                                    abort_question_attempt = True
                                    abort_question_reason = "单题critic->fixer循环超过3次，熔断本题"
                                    _append_step(
                                        "单题熔断",
                                        node="system",
                                        level="error",
                                        detail=abort_question_reason,
                                    )
                                    attempt_error_info = _build_abort_attempt_error(
                                        abort_reason=abort_question_reason,
                                        question_trace=question_trace,
                                    )
                                    break
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
                    # Only take final_json from writer/fixer so stored content matches last fix
                    # (no accidental overwrite from other nodes).
                    # Also clear stale critic verdict here: after a new draft/fix is produced and before
                    # next critic pass returns, UI should not show previous-round critic_result.
                    if node_name in ("writer", "fixer") and isinstance(state_update, dict) and state_update.get("final_json"):
                        q_json = state_update.get("final_json")
                        current_draft_revision = int(question_trace.get("draft_revision", 0) or 0) + 1
                        question_trace["draft_revision"] = current_draft_revision
                        question_trace["critic_result"] = {}
                        question_trace["critic_revision"] = 0
                        question_trace.pop("critic_details", None)
                        critic_seen = False
                        critic_passed = False
                        if isinstance(q_json, dict):
                            # 实时透出当前定稿，便于前端在 critic/fix 循环中展示完整题目（非落库）
                            question_trace["final_json"] = deepcopy(q_json)
                            question_trace["final_json_expired"] = False
                            question_trace.pop("final_json_expired_at", None)
                            question_trace["final_json_run_id"] = current_run_id
                    _emit_node_highlights(node_name, state_update, _append_step)
                    # Stream yields full state after each step; sync llm_trace to avoid duplicates
                    llm_records = state_update.get("llm_trace") or []
                    if isinstance(llm_records, list):
                        question_llm_trace[:] = _merge_llm_trace_records(
                            question_llm_trace,
                            [x for x in llm_records if isinstance(x, dict)],
                        )
                    if not abort_question_attempt:
                        should_abort, should_abort_reason = _should_abort_question_attempt(
                            started_at=started_at,
                            current_run_id=current_run_id,
                            max_graph_rounds_per_question=max_graph_rounds_per_question,
                            max_question_elapsed_ms=max_question_elapsed_ms,
                        )
                        if should_abort:
                            abort_question_attempt = True
                            abort_question_reason = should_abort_reason
                            attempt_error_info = _build_abort_attempt_error(
                                abort_reason=abort_question_reason,
                                question_trace=question_trace,
                            )
                            _append_step(
                                "单题中止",
                                node="system",
                                level="error",
                                detail=abort_question_reason,
                            )
                            break
                if abort_question_attempt:
                    break
                if cancelled_by_user:
                    break
            if abort_question_attempt:
                last_critic_result = question_trace.get("critic_result") if isinstance(question_trace.get("critic_result"), dict) else {}
                if isinstance(q_json, dict) and _is_abort_whitelist_pass(last_critic_result):
                    soft_reason = "单题在熔断/中止前仅命中 critic 白名单问题，按正式通过处理并保留问题标记。"
                    soft_pass_result = dict(last_critic_result)
                    soft_pass_result["passed"] = True
                    soft_pass_result["whitelist_pass"] = True
                    soft_pass_result["whitelist_pass_reason"] = soft_reason
                    question_trace["critic_result"] = soft_pass_result
                    question_trace["critic_original_result"] = deepcopy(last_critic_result)
                    question_trace["critic_details"] = soft_reason
                    question_trace["whitelist_pass"] = True
                    critic_passed = True
                    whitelist_saved_current = True
                    attempt_error_info = None
                    _append_step("白名单问题通过", node="system", level="warning", detail=soft_reason)
                else:
                    errors.append(f"第{attempt_count}次尝试失败: {abort_question_reason}")
                    exclude_now = _should_exclude_failed_slice_from_task(
                        allow_single_retry=allow_retry_on_current_slot and int(sid) == int(planned_slice_ids[success_index]) if planned_slice_ids and success_index < len(planned_slice_ids) else False,
                        sid=int(sid),
                        failure_counts=task_slice_failure_counts,
                    )
                    if exclude_now:
                        task_excluded_slice_ids.add(int(sid))
                    template_gap_final_failure = bool(exclude_now and allow_retry_on_current_slot)
                    if critic_seen and isinstance(question_trace.get("critic_result"), dict):
                        health = _record_slice_generation_failure(
                            tenant_id=tenant_id,
                            material_version_id=material_version_id,
                            slice_id=int(sid),
                            critic_result=question_trace.get("critic_result"),
                            task_id=task_id,
                            run_id=run_id,
                        )
                        if bool(health.get("blocked")):
                            _append_step("切片已禁用", node="system", level="error", detail=str(health.get("blocked_reason", "")))
            elif q_json and critic_passed:
                final_qt_cn = _resolve_storage_question_type_cn(
                    final_json=q_json,
                    trace_question_type=question_trace.get("question_type"),
                    config_question_type=question_type,
                )
                question_trace["question_type"] = final_qt_cn
                q_json["题目类型"] = final_qt_cn
                if task_id:
                    q_json["出题任务ID"] = task_id
                if task_name:
                    q_json["出题任务名称"] = task_name
                q_json["出题RunID"] = run_id
                _attach_preview_context_to_question_payload(
                    q_json,
                    tenant_id=tenant_id,
                    material_version_id=material_version_id,
                    question_trace=question_trace,
                    source_path=str(kb_chunk.get("完整路径", "")),
                    source_slice_id=sid,
                    mother_questions=mother_questions,
                    mother_full_questions=mother_full_questions,
                )
                if whitelist_saved_current:
                    q_json = _build_whitelist_pass_bank_item(
                        final_json=q_json,
                        critic_result=question_trace.get("critic_original_result") if isinstance(question_trace.get("critic_original_result"), dict) else {},
                        task_id=task_id,
                        task_name=task_name,
                        run_id=run_id,
                    )
                    question_trace["final_json"] = deepcopy(q_json)
                question_trace["run_id"] = run_id
                if str(task_name or "").strip() and isinstance(planned_slots, list) and planned_slots:
                    q_json = _attach_template_candidate_bank_metadata(
                        final_json=q_json,
                        question_trace=question_trace,
                        task_name=task_name,
                        planned_slots=planned_slots,
                        success_index=success_index,
                    )
                    question_trace["final_json"] = deepcopy(q_json)
                generated.append(q_json)
                if planned_slots:
                    _template_slot_cursor = success_index + 1
                if persist_to_bank and _is_task_auto_bank_enabled(tenant_id, task_id, persist_to_bank):
                    try:
                        _append_bank_item(bank_path, q_json)
                        saved += 1
                        saved_current = True
                        _append_step("题目已落库", node="system", level="success", detail="白名单通过" if whitelist_saved_current else "")
                    except Exception as e:
                        saved_current = False
                        errors.append(f"第{attempt_count}次尝试落库失败: {e}")
                        _append_step("落库失败", node="system", level="error", detail=str(e))
                        attempt_error_info = {
                            "error_key": "storage:append_bank_item_failed",
                            "category": "storage_failure",
                            "reason": str(e),
                            "evidence": str(e),
                            "fail_types": [],
                            "missing_conditions": [],
                            "basis_paths": [],
                            "solution": _infer_solution_by_error_key(
                                error_key="storage:append_bank_item_failed",
                                fail_types=[],
                                reason=str(e),
                                missing_conditions=[],
                            ),
                        }
                _append_step("题目生成成功", node="system", level="success")
            elif q_json and not critic_seen:
                errors.append(f"第{attempt_count}次尝试失败: 未经过 critic 审核")
                _append_step("未经过 critic 审核", node="critic", level="error")
                exclude_now = _should_exclude_failed_slice_from_task(
                    allow_single_retry=allow_retry_on_current_slot and int(sid) == int(planned_slice_ids[success_index]) if planned_slice_ids and success_index < len(planned_slice_ids) else False,
                    sid=int(sid),
                    failure_counts=task_slice_failure_counts,
                )
                if exclude_now:
                    task_excluded_slice_ids.add(int(sid))
                template_gap_final_failure = bool(exclude_now and allow_retry_on_current_slot)
                attempt_error_info = _classify_generation_attempt_error(
                    question_trace=question_trace,
                    q_json=q_json,
                    critic_seen=critic_seen,
                    critic_passed=critic_passed,
                    error_text="未经过 critic 审核",
                )
            elif q_json and critic_seen and not critic_passed:
                errors.append(f"第{attempt_count}次尝试失败: critic 未通过")
                _append_step("critic 未通过，题目未保存", node="critic", level="error")
                exclude_now = _should_exclude_failed_slice_from_task(
                    allow_single_retry=allow_retry_on_current_slot and int(sid) == int(planned_slice_ids[success_index]) if planned_slice_ids and success_index < len(planned_slice_ids) else False,
                    sid=int(sid),
                    failure_counts=task_slice_failure_counts,
                )
                if exclude_now:
                    task_excluded_slice_ids.add(int(sid))
                template_gap_final_failure = bool(exclude_now and allow_retry_on_current_slot)
                health = _record_slice_generation_failure(
                    tenant_id=tenant_id,
                    material_version_id=material_version_id,
                    slice_id=int(sid),
                    critic_result=question_trace.get("critic_result") if isinstance(question_trace.get("critic_result"), dict) else {},
                    task_id=task_id,
                    run_id=run_id,
                )
                if bool(health.get("blocked")):
                    _append_step("切片已禁用", node="system", level="error", detail=str(health.get("blocked_reason", "")))
                attempt_error_info = _classify_generation_attempt_error(
                    question_trace=question_trace,
                    q_json=q_json,
                    critic_seen=critic_seen,
                    critic_passed=critic_passed,
                    error_text="critic 未通过",
                )
            else:
                errors.append(f"第{attempt_count}次尝试未产出 final_json")
                _append_step("未产出 final_json", node="writer", level="error")
                exclude_now = _should_exclude_failed_slice_from_task(
                    allow_single_retry=allow_retry_on_current_slot and int(sid) == int(planned_slice_ids[success_index]) if planned_slice_ids and success_index < len(planned_slice_ids) else False,
                    sid=int(sid),
                    failure_counts=task_slice_failure_counts,
                )
                if exclude_now:
                    task_excluded_slice_ids.add(int(sid))
                template_gap_final_failure = bool(exclude_now and allow_retry_on_current_slot)
                attempt_error_info = _classify_generation_attempt_error(
                    question_trace=question_trace,
                    q_json=q_json,
                    critic_seen=critic_seen,
                    critic_passed=critic_passed,
                    error_text="未产出 final_json",
                )
        except Exception as e:
            errors.append(f"第{attempt_count}次尝试失败: {e}")
            _append_step("出题异常", node="system", level="error", detail=str(e))
            attempt_error_info = {
                "error_key": f"runtime:{type(e).__name__}",
                "category": "runtime_exception",
                "reason": str(e),
                "evidence": str(e),
                "fail_types": [],
                "missing_conditions": [],
                "basis_paths": [],
                "solution": "检查对应节点异常堆栈与输入切片，修复后再重跑。",
            }
        finally:
            detach_question_wall_clock_budget(_wall_token)

        if attempt_error_info and not saved_current:
            err_key = str(attempt_error_info.get("error_key", "attempt_failed")).strip() or "attempt_failed"
            category = str(attempt_error_info.get("category", "") or "").strip()
            is_critic_family = err_key.startswith("critic:") or category in {"critic_rejected", "critic_missing"}
            if is_critic_family:
                failure_key_counts[err_key] = int(failure_key_counts.get(err_key, 0) or 0) + 1
                failure_examples.setdefault(err_key, attempt_error_info)
            if is_critic_family and failure_key_counts[err_key] >= fuse_threshold:
                if _should_skip_fuse_for_error(
                    error_key=err_key,
                    target_question_count=target_question_count,
                ):
                    _append_step(
                        "熔断豁免",
                        node="system",
                        level="warning",
                        detail=f"error_key={err_key} count={failure_key_counts[err_key]} reason=large_batch_writer_quality_family",
                    )
                else:
                    # 容错策略：同类错误超阈值仅告警，不中断整批；继续跑后续题补位。
                    example = failure_examples.get(err_key, attempt_error_info)
                    fuse_info = {
                        "triggered": False,
                        "threshold": fuse_threshold,
                        "error_key": err_key,
                        "count": failure_key_counts[err_key],
                        "category": example.get("category", ""),
                        "fail_types": example.get("fail_types") or [],
                        "missing_conditions": example.get("missing_conditions") or [],
                        "basis_paths": example.get("basis_paths") or [],
                        "evidence": str(example.get("evidence", "") or "").strip(),
                        "solution": str(example.get("solution", "") or "").strip(),
                    }
                    _append_step(
                        "同类错误超阈值，继续补位",
                        node="system",
                        level="warning",
                        detail=f"error_key={err_key} count={failure_key_counts[err_key]}",
                    )
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        if template_gap_final_failure and not saved_current and isinstance(q_json, dict):
            persisted, saved_item, save_err = _persist_template_gap_failed_item(
                enabled=bool(persist_to_bank),
                path=bank_path,
                final_json=q_json,
                question_trace=question_trace,
                attempt_error_info=attempt_error_info,
                task_id=task_id,
                task_name=task_name,
                run_id=run_id,
            )
            if persisted and isinstance(saved_item, dict):
                q_json = saved_item
                question_trace["final_json"] = deepcopy(saved_item)
                generated.append(saved_item)
                if planned_slots:
                    _template_slot_cursor = success_index + 1
                saved += 1
                saved_current = True
                saved_with_issues_current = True
                _append_step("题目已落库", node="system", level="warning", detail="模板唯一缺口失败入库，待修复")
            elif save_err:
                errors.append(f"第{attempt_count}次尝试待修复题落库失败: {save_err}")
                _append_step("待修复题落库失败", node="system", level="error", detail=save_err)
        question_trace["elapsed_ms"] = elapsed_ms
        question_trace["llm_trace"] = question_llm_trace
        question_trace["llm_summary"] = summarize_llm_trace(question_llm_trace)
        question_trace["unstable_flags"] = mark_unstable(question_trace["llm_summary"])
        question_trace["saved"] = bool(saved_current)
        question_trace["saved_with_issues"] = bool(saved_with_issues_current)
        if isinstance(q_json, dict):
            _attach_preview_context_to_question_payload(
                q_json,
                tenant_id=tenant_id,
                material_version_id=material_version_id,
                question_trace=question_trace,
                source_path=str(kb_chunk.get("完整路径", "")),
                source_slice_id=sid,
                mother_questions=mother_questions,
                mother_full_questions=mother_full_questions,
            )
            question_trace["final_json"] = q_json
        if question_trace["unstable_flags"]:
            _append_step(
                "稳定性预警",
                node="system",
                level="warning",
                detail=",".join(question_trace["unstable_flags"]),
            )
        # Ensure critic result is visible in step list (e.g. rule-based reject before LLM)
        _ensure_critic_step_in_trace(question_trace)
        question_trace["snapshot_stage"] = "final"
        process_trace.append(question_trace)
        # Live task mode: flush finalized question trace immediately so UI can mark
        # this question as passed/failed without waiting for the whole batch to end.
        if task_id:
            _update_task_live(
                tenant_id,
                task_id,
                {
                    # Progress reflects accepted questions so retries do not overflow total.
                    "progress": {"current": len(generated), "total": target_question_count},
                    "generated_count": len(generated),
                    "saved_count": saved,
                    "error_count": len(errors),
                    "saved_with_issues_count": sum(1 for row in process_trace if isinstance(row, dict) and row.get("saved_with_issues")),
                },
                [question_trace],
            )
            _persist_live_task_snapshot(tenant_id, task_id)
        if bool(saved_current) and int(sid) > 0:
            task_slice_usage_counts[int(sid)] = int(task_slice_usage_counts.get(int(sid), 0) or 0) + 1
        if cancelled_by_user:
            errors.append("用户取消")
            break
    if (
        is_internal_subtask_request
        and not cancelled_by_user
        and len(generated) < target_question_count
        and attempt_count >= max_attempts
    ):
        fuse_triggered = True
        fuse_info = {
            "triggered": True,
            "category": "subtask_attempt_budget",
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "target_question_count": target_question_count,
            "passed_count": len(generated),
        }
        errors.append(
            f"子任务熔断：尝试总数(通过+失败)达到 {attempt_count}，"
            f"已触达子任务尝试次数上限({max_attempts})，目标 {target_question_count} 题，当前通过 {len(generated)} 题"
        )
    if template and not cancelled_by_user and len(generated) < target_question_count:
        saved, template_gap_errors = _persist_template_remaining_failed_slots(
            enabled=bool(persist_to_bank and _is_task_auto_bank_enabled(tenant_id, task_id, persist_to_bank)),
            bank_path=bank_path,
            planned_slots=planned_slots,
            process_trace=process_trace,
            generated=generated,
            saved_count=saved,
            task_id=task_id,
            task_name=task_name,
            run_id=run_id,
            question_type=question_type,
            failure_reason=errors[-1] if errors else "达到任务熔断或无可用切片",
        )
        if template_gap_errors:
            errors.extend(template_gap_errors)
        if len(generated) >= target_question_count:
            errors.append("模板缺口已自动补位入库为待修复题，请在题库按位次修复。")
    if template and not cancelled_by_user and len(generated) < target_question_count:
        errors.append(
            f"模板要求 {target_question_count} 题，但在 {attempt_count} 次尝试后仅生成 {len(generated)} 题通过 critic，请调整模板或切片范围后重试"
        )

    run_ended_at = datetime.now(timezone.utc).isoformat()
    qa_run = _build_qa_run_payload(
        tenant_id=tenant_id,
        run_id=run_id,
        material_version_id=material_version_id,
        config_payload={
            "tenant_id": tenant_id,
            "question_type": question_type,
            "generation_mode": generation_mode,
            "difficulty": difficulty,
            "difficulty_range": difficulty_range,
            "num_questions": target_question_count,
            "max_attempts": max_attempts,
            "model": model_name,
            "gen_scope_mode": gen_scope_mode,
            "task_id": task_id,
            "template_id": template_id,
            "template_name": str(template.get("name", "")).strip() if template else "",
            "template_snapshot": template if template else None,
            "template_plan": template_plan,
            "fuse_info": fuse_info,
            "enable_offline_judge": False,
        },
        process_trace=process_trace,
        generated_count=len(generated),
        saved_count=saved,
        errors=errors,
        started_at=run_started_at,
        ended_at=run_ended_at,
    )
    _persist_qa_run(tenant_id, qa_run)
    hard_failed_count, soft_warning_count = _summarize_trace_fail_levels(process_trace)

    if fuse_triggered and errors:
        # 容错策略：只要有有效产出，不把整批任务标记为失败；返回 200 + partial 标记。
        if len(generated) > 0:
            return _json_response(
                {
                    "success": False,
                    "partial_completed": True,
                    "error": {
                        "code": "GENERATION_FUSED",
                        "message": errors[-1],
                    },
                    "run_id": run_id,
                    "items": generated,
                    "generated_count": len(generated),
                    "saved_count": saved,
                    "errors": errors,
                    "fuse_info": fuse_info,
                    "hard_failed_count": hard_failed_count,
                    "soft_warning_count": soft_warning_count,
                    "process_trace": process_trace,
                    "material_version_id": material_version_id,
                },
                status=200,
            )
        # 即便 0 题通过，也返回 200 + 结构化错误，交由任务层按“完成但有失败题”处理。
        return _json_response(
            {
                "success": False,
                "partial_completed": False,
                "error": {
                    "code": "GENERATION_FUSED",
                    "message": errors[-1],
                },
                "run_id": run_id,
                "generated_count": len(generated),
                "saved_count": saved,
                "errors": errors,
                "fuse_info": fuse_info,
                "hard_failed_count": hard_failed_count,
                "soft_warning_count": soft_warning_count,
                "process_trace": process_trace,
                "material_version_id": material_version_id,
            },
            status=200,
        )
    if len(generated) < target_question_count and errors:
        # 容错策略：统一返回 200，避免“单题失败导致整任务失败”。
        if len(generated) > 0:
            return _json_response(
                {
                    "success": False,
                    "partial_completed": True,
                    "run_id": run_id,
                    "items": generated,
                    "generated_count": len(generated),
                    "saved_count": saved,
                    "errors": errors,
                    "hard_failed_count": hard_failed_count,
                    "soft_warning_count": soft_warning_count,
                    "process_trace": process_trace,
                    "material_version_id": material_version_id,
                },
                status=200,
            )
        return _json_response(
            {
                "success": False,
                "partial_completed": False,
                "error": {
                    "code": "GENERATION_FAILED",
                    "message": f"出题失败：{errors[0]}",
                },
                "run_id": run_id,
                "items": [],
                "generated_count": 0,
                "saved_count": 0,
                "errors": errors,
                "hard_failed_count": hard_failed_count,
                "soft_warning_count": soft_warning_count,
                "process_trace": process_trace,
                "material_version_id": material_version_id,
            },
            status=200,
        )

    write_audit_log(
        tenant_id,
        system_user,
        "gen.create.batch",
        "question_generation",
        f"{tenant_id}:{datetime.now(timezone.utc).isoformat()}",
        after={
            "num_questions": target_question_count,
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "generated": len(generated),
            "saved": saved,
            "errors": errors,
            "trace_count": len(process_trace),
            "question_type": question_type,
            "generation_mode": generation_mode,
            "material_version_id": material_version_id,
            "template_id": template_id,
            "template_name": str(template.get("name", "")).strip() if template else "",
            "run_id": run_id,
            "fuse_info": fuse_info,
        },
    )
    return _json_response(
        {
            "run_id": run_id,
            "items": generated,
            "generated_count": len(generated),
            "saved_count": saved,
            "errors": errors,
            "hard_failed_count": hard_failed_count,
            "soft_warning_count": soft_warning_count,
            "process_trace": process_trace,
            "material_version_id": material_version_id,
            "cancelled": cancelled_by_user,
            "fuse_triggered": fuse_triggered,
            "fuse_info": fuse_info,
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
    generation_mode = _normalize_generation_mode(body.get("generation_mode", "随机"))
    if generation_mode not in GEN_MODES:
        generation_mode = "随机"
    difficulty = str(body.get("difficulty", "随机"))
    slice_ids_input = body.get("slice_ids") or []
    template_id = str(body.get("template_id", "")).strip()
    planned_slice_ids_input = body.get("planned_slice_ids") or []
    persist_to_bank = bool(body.get("persist_to_bank", body.get("save_to_bank", True)))
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    task_id = str(body.get("task_id", "")).strip()
    task_name = str(body.get("task_name", "")).strip()
    parent_task_id = str(body.get("parent_task_id", "")).strip()
    child_kind = str(body.get("child_kind", "")).strip()
    is_internal_subtask_request = bool(parent_task_id or child_kind)
    if gen_scope_mode not in {"custom", "per_slice"}:
        return _error("BAD_REQUEST", "非法出题范围模式", 400)
    if question_type not in QUESTION_TYPES:
        return _error("BAD_REQUEST", "非法题型", 400)
    if generation_mode not in GEN_MODES:
        return _error("BAD_REQUEST", "非法出题模式", 400)
    max_graph_rounds_per_question = max(1, int(os.getenv("MAX_GRAPH_ROUNDS_PER_QUESTION", "3") or 3))
    max_question_elapsed_ms = max(1000, int(os.getenv("MAX_QUESTION_ELAPSED_MS", "900000") or 900000))

    template = _get_gen_template(tenant_id, template_id) if template_id else None
    if template_id and not template:
        return _error("TEMPLATE_NOT_FOUND", "出题模板不存在", 404)
    effective_material_version_request = (
        str(template.get("material_version_id", "")).strip()
        if template else requested_material_version_id
    )
    material_version_id = _resolve_material_version_id(tenant_id, effective_material_version_request)
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

    if not kb_file:
        return _error("NO_SLICES_FILE", "当前城市没有可用切片文件", 400)
    history_path = str(_resolve_history_path_for_material(tenant_id, material_version_id))
    mapping_path = str(_resolve_mapping_path_for_material(tenant_id, material_version_id) or tenant_mapping_path(tenant_id))
    mapping_review_path = str(tenant_mapping_review_path(tenant_id))
    retriever = _get_cached_retriever(
        tenant_id=tenant_id,
        kb_path=str(kb_file),
        history_path=history_path,
        mapping_path=mapping_path,
        mapping_review_path=mapping_review_path,
    )
    candidate_ids = [sid for sid in candidate_ids if 0 <= sid < len(retriever.kb_data)]
    if not candidate_ids:
        return _error("NO_VALID_SLICES", "审核记录与当前切片版本不一致，请重新审核切片后再出题", 400)
    candidate_ids, skipped_type_conflicts = _filter_candidate_ids_by_question_type(retriever, candidate_ids, question_type)
    if not candidate_ids:
        skipped_msg = "；".join(
            f"slice_id={x.get('slice_id')}({x.get('reason')})"
            for x in (skipped_type_conflicts[:8] if isinstance(skipped_type_conflicts, list) else [])
        )
        return _error(
            "NO_TYPE_COMPATIBLE_SLICES",
            f"所选题型在当前切片范围内均被禁止，未生成题目。{skipped_msg}".strip(),
            400,
        )
    blocked_slice_ids = _blocked_slice_ids_for_material(tenant_id, material_version_id)
    candidate_ids = [sid for sid in candidate_ids if sid not in blocked_slice_ids]
    if not candidate_ids:
        return _error("NO_UNBLOCKED_SLICES", "当前范围内切片均已因累计失败被禁用，请先修复切片", 400)
    candidate_slices = []
    for sid in candidate_ids:
        kb_item = retriever.kb_data[sid]
        candidate_slices.append(
            {
                "slice_id": sid,
                "path": str(kb_item.get("完整路径", "")).strip(),
                "mastery": str(kb_item.get("掌握程度", "")).strip(),
            }
        )
    candidate_lookup = _build_slice_candidate_lookup(
        candidate_slices,
        template_route_rules=(template or {}).get("route_rules") if template else None,
    )
    planned_slice_ids: list[int] = []
    planned_slots: list[dict[str, Any]] = []
    template_plan: dict[str, Any] | None = None
    if template:
        num_questions = int(template.get("question_count", num_questions) or num_questions)
        precheck_report = _build_template_reachability_report(
            question_count=num_questions,
            template=template,
            candidate_slices=candidate_slices,
        )
        try:
            template_plan = _build_generation_template_plan(
                question_count=num_questions,
                template=template,
                candidate_slices=candidate_slices,
            )
        except ValueError as e:
            return _error(
                "TEMPLATE_PLAN_INVALID",
                _format_template_reachability_error(str(e), precheck_report),
                400,
            )
        planned_slots = [
            slot for slot in (template_plan.get("planned_slots") or [])
            if isinstance(slot, dict) and int(slot.get("slice_id", -1)) in candidate_ids
        ]
        planned_slice_ids = [int(slot.get("slice_id")) for slot in planned_slots]
    else:
        for slot in planned_slots_input:
            if not isinstance(slot, dict):
                continue
            try:
                sid_int = int(slot.get("slice_id"))
            except (TypeError, ValueError):
                continue
            if sid_int not in candidate_ids:
                continue
            planned_slots.append(
                {
                    "slice_id": sid_int,
                    "route_prefix": str(slot.get("route_prefix", "") or "").strip(),
                    "mastery": str(slot.get("mastery", "") or "").strip(),
                    "_global_target_index": int(slot.get("_global_target_index", 0) or 0),
                }
            )
        for sid in planned_slice_ids_input:
            try:
                sid_int = int(sid)
            except (TypeError, ValueError):
                continue
            if sid_int in candidate_ids:
                planned_slice_ids.append(sid_int)
        if planned_slots and not planned_slice_ids:
            planned_slice_ids = [int(slot.get("slice_id")) for slot in planned_slots]
        if gen_scope_mode == "per_slice":
            num_questions = len(candidate_ids)
    if planned_slots:
        num_questions = len(planned_slots)
    elif planned_slice_ids:
        num_questions = len(planned_slice_ids)
    if num_questions <= 0:
        return _error("BAD_REQUEST", "题量必须大于0", 400)
    target_question_count = num_questions
    if is_internal_subtask_request:
        # 与同步 POST /generate 子任务 attempt 上限一致，见上文说明。
        max_attempts = min(400, max(1, target_question_count * 6 + 16))
    else:
        max_attempts = min(max(target_question_count * 3, target_question_count + 3), 600)

    api_key, base_url, model_name = _resolve_generation_llm_from_primary_key()
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
        saved = 0
        bank_path = tenant_bank_path(tenant_id)
        yield _sse(
            "started",
            {
                "run_id": run_id,
                "num_questions": target_question_count,
                "max_attempts": max_attempts,
                "material_version_id": material_version_id,
                "question_type": question_type,
                "generation_mode": generation_mode,
                "template_id": template_id,
                "template_name": str(template.get("name", "")).strip() if template else "",
                "template_snapshot": template if template else None,
                "template_plan": template_plan,
                "persist_to_bank": persist_to_bank,
            },
        )
        random_difficulty_buckets = _random_difficulty_buckets() if difficulty == "随机" else []
        fuse_threshold = 5
        fuse_triggered = False
        fuse_info: dict[str, Any] | None = None
        failure_key_counts: dict[str, int] = {}
        failure_examples: dict[str, dict[str, Any]] = {}
        task_slice_failure_counts: dict[int, int] = {}
        task_slice_usage_counts = _normalize_slice_usage_counts(body.get("used_slice_counts") or {})
        max_questions_per_slice = max(0, int(body.get("max_questions_per_slice", 2 if template else 0) or 0))
        attempt_count = 0
        task_excluded_slice_ids: set[int] = set(blocked_slice_ids)
        _template_slot_cursor2 = 0
        _template_skipped_slots2: set[int] = set()
        while len(generated) < target_question_count and attempt_count < max_attempts:
            success_index = len(generated)
            if planned_slots:
                while _template_slot_cursor2 in _template_skipped_slots2:
                    _template_slot_cursor2 += 1
                success_index = _template_slot_cursor2
                if success_index >= len(planned_slots):
                    break
            attempt_count += 1
            allow_retry_on_current_slot = bool(template) and _is_template_same_mastery_hard_gap(
                planned_slots=planned_slots,
                success_index=success_index,
                sid=int(planned_slice_ids[success_index]) if planned_slice_ids and success_index < len(planned_slice_ids) else -1,
                candidate_lookup=candidate_lookup,
            )
            template_gap_final_failure = False
            sid, sid_pick_error = _choose_generation_slice_id(
                planned_slice_ids=planned_slice_ids,
                planned_slots=planned_slots,
                success_index=success_index,
                candidate_ids=candidate_ids,
                attempt_count=attempt_count,
                target_question_count=target_question_count,
                excluded_slice_ids=task_excluded_slice_ids,
                candidate_lookup=candidate_lookup,
                slice_usage_counts=task_slice_usage_counts,
                max_questions_per_slice=max_questions_per_slice,
            )
            if sid is None:
                errors.append(sid_pick_error or "无可用切片")
                if planned_slots and success_index < len(planned_slots):
                    _template_skipped_slots2.add(success_index)
                    continue
                break
            kb_chunk = retriever.kb_data[sid]
            effective_difficulty_range = (
                random_difficulty_buckets[success_index % len(random_difficulty_buckets)]
                if random_difficulty_buckets
                else difficulty_range
            )
            started_at = datetime.now(timezone.utc)
            step_seq = 0
            seen_logs: set[str] = set()
            seen_step_keys: set[str] = set()
            trace_id = uuid.uuid4().hex
            question_id = f"{tenant_id}:{material_version_id or 'default'}:{attempt_count}:{sid}:{trace_id[:8]}"
            question_llm_trace: list[dict[str, Any]] = []
            current_target_index = _planned_slot_target_index(planned_slots, success_index)
            question_trace: dict[str, Any] = {
                "run_id": run_id,
                "index": attempt_count,
                "target_index": current_target_index,
                "slice_id": sid,
                "slice_path": str(kb_chunk.get("完整路径", "")),
                "slice_content": _extract_slice_text(kb_chunk),
                "trace_id": trace_id,
                "question_id": question_id,
                "question_type": "",
                "difficulty_range": list(effective_difficulty_range) if effective_difficulty_range else None,
                "steps": [],
                "critic_result": {},
                "snapshot_stage": "live",
                "saved": False,
                "active_run_id": 0,
                "final_json_expired": False,
                "draft_revision": 0,
                "critic_revision": 0,
                **_planned_slot_trace_fields(planned_slots, success_index),
            }
            yield _sse(
                "question_start",
                {
                    "index": attempt_count,
                    "target_index": current_target_index,
                    "slice_id": sid,
                    "slice_path": question_trace["slice_path"],
                    "slice_content": question_trace["slice_content"],
                    **_planned_slot_trace_fields(planned_slots, success_index),
                },
            )

            last_step_time = started_at
            current_run_id = 0  # round index for question generation: first route stays 0, reroute becomes 1/2...
            router_seen = False

            def _append_step(message: str, *, node: str = "", level: str = "info", detail: str = "") -> None:
                nonlocal step_seq, last_step_time
                dedupe_key = f"{current_run_id}|{node}|{level}|{message}|{detail}"
                if dedupe_key in seen_step_keys:
                    return
                seen_step_keys.add(dedupe_key)
                step_seq += 1
                now = datetime.now(timezone.utc)
                elapsed_ms = int((now - started_at).total_seconds() * 1000)
                delta_ms = int((now - last_step_time).total_seconds() * 1000) if last_step_time else None
                last_step_time = now
                step_payload = {
                    "seq": step_seq,
                    "node": node,
                    "level": level,
                    "message": message,
                    "detail": detail,
                    "time": now.isoformat(),
                    "elapsed_ms": elapsed_ms,
                    "delta_ms": delta_ms,
                    "run_id": current_run_id,
                }
                question_trace["steps"].append(step_payload)
                yield_item = _sse("step", {"index": attempt_count, "target_index": success_index + 1, **step_payload})
                _event_stream_buffer.append(yield_item)

            _event_stream_buffer: list[str] = []
            _append_step("开始出题", node="system", detail=f"切片ID={sid}")
            if effective_difficulty_range:
                _append_step(
                    "本题难度目标",
                    node="system",
                    detail=f"{effective_difficulty_range[0]:.1f}-{effective_difficulty_range[1]:.1f}",
                )
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
                    "difficulty_range": effective_difficulty_range,
                }
            }
            q_json = None
            mother_questions: list[str] = []
            mother_full_questions: list[dict[str, Any]] = []
            saved_current = False
            saved_with_issues_current = False
            critic_seen = False
            critic_passed = False
            critic_reject_count = 0
            abort_question_attempt = False
            abort_question_reason = ""
            attempt_error_info: dict[str, Any] | None = None
            whitelist_saved_current = False
            _wall_token = attach_question_wall_clock_budget(
                started_at_utc=started_at,
                max_elapsed_ms=max_question_elapsed_ms,
            )
            try:
                for event in graph_app.stream(inputs, config=config):
                    for node_name, state_update in event.items():
                        if not isinstance(state_update, dict):
                            continue
                        extracted_mothers = _extract_mother_questions_from_examples(state_update.get("examples"))
                        if extracted_mothers:
                            mother_questions = extracted_mothers
                            question_trace["mother_questions"] = mother_questions
                        extracted_mothers_full = _extract_mother_question_full_from_examples(state_update.get("examples"))
                        if extracted_mothers_full:
                            mother_full_questions = extracted_mothers_full
                            question_trace["mother_questions_full"] = mother_full_questions
                        current_qt = str(state_update.get("current_question_type", "") or "").strip()
                        if current_qt in {"单选题", "多选题", "判断题"}:
                            question_trace["question_type"] = current_qt
                        related_paths = _normalize_related_slice_paths(state_update.get("critic_basis_paths"))
                        if related_paths:
                            question_trace["related_slice_paths"] = related_paths
                            question_trace["related_slice_count"] = len(related_paths)
                        if node_name == "router":
                            details = state_update.get("router_details") or {}
                            agent = details.get("agent")
                            path = details.get("path")
                            _append_step(
                                "路由完成",
                                node=node_name,
                                detail=f"agent={agent or '-'} path={path or '-'}",
                            )
                            if router_seen:
                                _mark_live_final_json_stale(question_trace, _append_step)
                                current_run_id += 1  # reroute starts next round; first route remains round 0
                                question_trace["active_run_id"] = current_run_id
                            else:
                                router_seen = True
                                question_trace["active_run_id"] = current_run_id
                        if node_name in ("specialist", "calculator"):
                            logs = state_update.get("logs") or []
                            if isinstance(logs, list):
                                for log in logs:
                                    text = str(log).strip()
                                    if not text or text in seen_logs:
                                        continue
                                    seen_logs.add(text)
                                    _append_step(text, node=node_name)
                        if node_name == "critic":
                            critic_result = state_update.get("critic_result") or {}
                            if isinstance(critic_result, dict) and ("passed" in critic_result):
                                current_draft_revision = int(question_trace.get("draft_revision", 0) or 0)
                                critic_revision = int(critic_result.get("critic_revision", current_draft_revision) or 0)
                                critic_result = dict(critic_result)
                                critic_result["critic_revision"] = critic_revision
                                question_trace["critic_result"] = critic_result
                                question_trace["critic_revision"] = critic_revision
                                fail_types_preview, error_content_preview = _extract_critic_issue_record(critic_result)
                                question_trace["critic_last_fail_types"] = fail_types_preview
                                question_trace["critic_last_error_content"] = error_content_preview
                                critic_details = state_update.get("critic_details")
                                if critic_details is not None:
                                    question_trace["critic_details"] = str(critic_details).strip()
                                critic_seen = True
                                passed = bool(critic_result.get("passed"))
                                critic_passed = passed
                                reason = str(critic_result.get("reason", "")).strip()
                                if not reason and not passed:
                                    reason = str(question_trace.get("critic_details", "")).strip() or "审核未通过（原因未返回）"
                                _append_step(
                                    "审核通过" if passed else "审核驳回",
                                    node=node_name,
                                    level="success" if passed else "warning",
                                    detail=reason or ("" if passed else "审核未通过（原因未返回）"),
                                )
                                if not passed:
                                    critic_reject_count += 1
                                    if critic_reject_count > 3:
                                        abort_question_attempt = True
                                        abort_question_reason = "单题critic->fixer循环超过3次，熔断本题"
                                        _append_step(
                                            "单题熔断",
                                            node="system",
                                            level="error",
                                            detail=abort_question_reason,
                                        )
                                        attempt_error_info = _build_abort_attempt_error(
                                            abort_reason=abort_question_reason,
                                            question_trace=question_trace,
                                        )
                                        break
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
                        # Only take final_json from writer/fixer so stored content matches last fix.
                        # Also clear stale critic verdict until new critic result arrives.
                        if node_name in ("writer", "fixer") and isinstance(state_update, dict) and state_update.get("final_json"):
                            q_json = state_update.get("final_json")
                            current_draft_revision = int(question_trace.get("draft_revision", 0) or 0) + 1
                            question_trace["draft_revision"] = current_draft_revision
                            question_trace["critic_result"] = {}
                            question_trace["critic_revision"] = 0
                            question_trace.pop("critic_details", None)
                            critic_seen = False
                            critic_passed = False
                            if isinstance(q_json, dict):
                                question_trace["final_json"] = deepcopy(q_json)
                                question_trace["final_json_expired"] = False
                                question_trace.pop("final_json_expired_at", None)
                                question_trace["final_json_run_id"] = current_run_id
                        _emit_node_highlights(node_name, state_update, _append_step)
                        # Stream yields full state after each step; sync llm_trace to avoid duplicates
                        llm_records = state_update.get("llm_trace") or []
                        if isinstance(llm_records, list):
                            question_llm_trace[:] = _merge_llm_trace_records(
                                question_llm_trace,
                                [x for x in llm_records if isinstance(x, dict)],
                            )
                        if not abort_question_attempt:
                            should_abort, should_abort_reason = _should_abort_question_attempt(
                                started_at=started_at,
                                current_run_id=current_run_id,
                                max_graph_rounds_per_question=max_graph_rounds_per_question,
                                max_question_elapsed_ms=max_question_elapsed_ms,
                            )
                            if should_abort:
                                abort_question_attempt = True
                                abort_question_reason = should_abort_reason
                                attempt_error_info = _build_abort_attempt_error(
                                    abort_reason=abort_question_reason,
                                    question_trace=question_trace,
                                )
                                _append_step(
                                    "单题中止",
                                    node="system",
                                    level="error",
                                    detail=abort_question_reason,
                                )
                                break
                        while _event_stream_buffer:
                            yield _event_stream_buffer.pop(0)
                    if abort_question_attempt:
                        break
                if abort_question_attempt:
                    last_critic_result = question_trace.get("critic_result") if isinstance(question_trace.get("critic_result"), dict) else {}
                    if isinstance(q_json, dict) and _is_abort_whitelist_pass(last_critic_result):
                        soft_reason = "单题在熔断/中止前仅命中 critic 白名单问题，按正式通过处理并保留问题标记。"
                        soft_pass_result = dict(last_critic_result)
                        soft_pass_result["passed"] = True
                        soft_pass_result["whitelist_pass"] = True
                        soft_pass_result["whitelist_pass_reason"] = soft_reason
                        question_trace["critic_result"] = soft_pass_result
                        question_trace["critic_original_result"] = deepcopy(last_critic_result)
                        question_trace["critic_details"] = soft_reason
                        question_trace["whitelist_pass"] = True
                        critic_passed = True
                        whitelist_saved_current = True
                        attempt_error_info = None
                        _append_step("白名单问题通过", node="system", level="warning", detail=soft_reason)
                    else:
                        errors.append(f"第{attempt_count}次尝试失败: {abort_question_reason}")
                        exclude_now = _should_exclude_failed_slice_from_task(
                            allow_single_retry=allow_retry_on_current_slot and int(sid) == int(planned_slice_ids[success_index]) if planned_slice_ids and success_index < len(planned_slice_ids) else False,
                            sid=int(sid),
                            failure_counts=task_slice_failure_counts,
                        )
                        if exclude_now:
                            task_excluded_slice_ids.add(int(sid))
                        template_gap_final_failure = bool(exclude_now and allow_retry_on_current_slot)
                        if critic_seen and isinstance(question_trace.get("critic_result"), dict):
                            health = _record_slice_generation_failure(
                                tenant_id=tenant_id,
                                material_version_id=material_version_id,
                                slice_id=int(sid),
                                critic_result=question_trace.get("critic_result"),
                                task_id=task_id,
                                run_id=run_id,
                            )
                            if bool(health.get("blocked")):
                                _append_step("切片已禁用", node="system", level="error", detail=str(health.get("blocked_reason", "")))
                elif q_json and critic_passed:
                    final_qt_cn = _resolve_storage_question_type_cn(
                        final_json=q_json,
                        trace_question_type=question_trace.get("question_type"),
                        config_question_type=question_type,
                    )
                    question_trace["question_type"] = final_qt_cn
                    q_json["题目类型"] = final_qt_cn
                    if task_id:
                        q_json["出题任务ID"] = task_id
                    if task_name:
                        q_json["出题任务名称"] = task_name
                    q_json["出题RunID"] = run_id
                    _attach_preview_context_to_question_payload(
                        q_json,
                        tenant_id=tenant_id,
                        material_version_id=material_version_id,
                        question_trace=question_trace,
                        source_path=str(kb_chunk.get("完整路径", "")),
                        source_slice_id=sid,
                        mother_questions=mother_questions,
                        mother_full_questions=mother_full_questions,
                    )
                    if whitelist_saved_current:
                        q_json = _build_whitelist_pass_bank_item(
                            final_json=q_json,
                            critic_result=question_trace.get("critic_original_result") if isinstance(question_trace.get("critic_original_result"), dict) else {},
                            task_id=task_id,
                            task_name=task_name,
                            run_id=run_id,
                        )
                        question_trace["final_json"] = deepcopy(q_json)
                    question_trace["run_id"] = run_id
                    if str(task_name or "").strip() and isinstance(planned_slots, list) and planned_slots:
                        q_json = _attach_template_candidate_bank_metadata(
                            final_json=q_json,
                            question_trace=question_trace,
                            task_name=task_name,
                            planned_slots=planned_slots,
                            success_index=success_index,
                        )
                        question_trace["final_json"] = deepcopy(q_json)
                    generated.append(q_json)
                    if planned_slots:
                        _template_slot_cursor2 = success_index + 1
                    if persist_to_bank and _is_task_auto_bank_enabled(tenant_id, task_id, persist_to_bank):
                        try:
                            _append_bank_item(bank_path, q_json)
                            saved += 1
                            saved_current = True
                            _append_step("题目已落库", node="system", level="success", detail="白名单通过" if whitelist_saved_current else "")
                        except Exception as e:
                            saved_current = False
                            errors.append(f"第{attempt_count}次尝试落库失败: {e}")
                            _append_step("落库失败", node="system", level="error", detail=str(e))
                            attempt_error_info = {
                                "error_key": "storage:append_bank_item_failed",
                                "category": "storage_failure",
                                "reason": str(e),
                                "evidence": str(e),
                                "fail_types": [],
                                "missing_conditions": [],
                                "basis_paths": [],
                                "solution": _infer_solution_by_error_key(
                                    error_key="storage:append_bank_item_failed",
                                    fail_types=[],
                                    reason=str(e),
                                    missing_conditions=[],
                                ),
                            }
                    _append_step("题目生成成功", node="system", level="success")
                elif q_json and not critic_seen:
                    errors.append(f"第{attempt_count}次尝试失败: 未经过 critic 审核")
                    _append_step("未经过 critic 审核", node="critic", level="error")
                    exclude_now = _should_exclude_failed_slice_from_task(
                        allow_single_retry=allow_retry_on_current_slot and int(sid) == int(planned_slice_ids[success_index]) if planned_slice_ids and success_index < len(planned_slice_ids) else False,
                        sid=int(sid),
                        failure_counts=task_slice_failure_counts,
                    )
                    if exclude_now:
                        task_excluded_slice_ids.add(int(sid))
                    template_gap_final_failure = bool(exclude_now and allow_retry_on_current_slot)
                    attempt_error_info = _classify_generation_attempt_error(
                        question_trace=question_trace,
                        q_json=q_json,
                        critic_seen=critic_seen,
                        critic_passed=critic_passed,
                        error_text="未经过 critic 审核",
                    )
                elif q_json and critic_seen and not critic_passed:
                    errors.append(f"第{attempt_count}次尝试失败: critic 未通过")
                    _append_step("critic 未通过，题目未保存", node="critic", level="error")
                    exclude_now = _should_exclude_failed_slice_from_task(
                        allow_single_retry=allow_retry_on_current_slot and int(sid) == int(planned_slice_ids[success_index]) if planned_slice_ids and success_index < len(planned_slice_ids) else False,
                        sid=int(sid),
                        failure_counts=task_slice_failure_counts,
                    )
                    if exclude_now:
                        task_excluded_slice_ids.add(int(sid))
                    template_gap_final_failure = bool(exclude_now and allow_retry_on_current_slot)
                    health = _record_slice_generation_failure(
                        tenant_id=tenant_id,
                        material_version_id=material_version_id,
                        slice_id=int(sid),
                        critic_result=question_trace.get("critic_result") if isinstance(question_trace.get("critic_result"), dict) else {},
                        task_id=task_id,
                        run_id=run_id,
                    )
                    if bool(health.get("blocked")):
                        _append_step("切片已禁用", node="system", level="error", detail=str(health.get("blocked_reason", "")))
                    attempt_error_info = _classify_generation_attempt_error(
                        question_trace=question_trace,
                        q_json=q_json,
                        critic_seen=critic_seen,
                        critic_passed=critic_passed,
                        error_text="critic 未通过",
                    )
                else:
                    errors.append(f"第{attempt_count}次尝试未产出 final_json")
                    _append_step("未产出 final_json", node="writer", level="error")
                    exclude_now = _should_exclude_failed_slice_from_task(
                        allow_single_retry=allow_retry_on_current_slot and int(sid) == int(planned_slice_ids[success_index]) if planned_slice_ids and success_index < len(planned_slice_ids) else False,
                        sid=int(sid),
                        failure_counts=task_slice_failure_counts,
                    )
                    if exclude_now:
                        task_excluded_slice_ids.add(int(sid))
                    template_gap_final_failure = bool(exclude_now and allow_retry_on_current_slot)
                    attempt_error_info = _classify_generation_attempt_error(
                        question_trace=question_trace,
                        q_json=q_json,
                        critic_seen=critic_seen,
                        critic_passed=critic_passed,
                        error_text="未产出 final_json",
                    )
            except Exception as e:
                errors.append(f"第{attempt_count}次尝试失败: {e}")
                _append_step("出题异常", node="system", level="error", detail=str(e))
                attempt_error_info = {
                    "error_key": f"runtime:{type(e).__name__}",
                    "category": "runtime_exception",
                    "reason": str(e),
                    "evidence": str(e),
                    "fail_types": [],
                    "missing_conditions": [],
                    "basis_paths": [],
                    "solution": "检查对应节点异常堆栈与输入切片，修复后再重跑。",
                }
            finally:
                detach_question_wall_clock_budget(_wall_token)

            if attempt_error_info and not saved_current:
                err_key = str(attempt_error_info.get("error_key", "attempt_failed")).strip() or "attempt_failed"
                category = str(attempt_error_info.get("category", "") or "").strip()
                is_critic_family = err_key.startswith("critic:") or category in {"critic_rejected", "critic_missing"}
                if is_critic_family:
                    failure_key_counts[err_key] = int(failure_key_counts.get(err_key, 0) or 0) + 1
                    failure_examples.setdefault(err_key, attempt_error_info)
                if is_critic_family and failure_key_counts[err_key] >= fuse_threshold:
                    if _should_skip_fuse_for_error(
                        error_key=err_key,
                        target_question_count=target_question_count,
                    ):
                        _append_step(
                            "熔断豁免",
                            node="system",
                            level="warning",
                            detail=f"error_key={err_key} count={failure_key_counts[err_key]} reason=large_batch_writer_quality_family",
                        )
                    else:
                        # 容错策略：同类错误超阈值仅告警，不中断整批；继续跑后续题补位。
                        example = failure_examples.get(err_key, attempt_error_info)
                        fuse_info = {
                            "triggered": False,
                            "threshold": fuse_threshold,
                            "error_key": err_key,
                            "count": failure_key_counts[err_key],
                            "category": example.get("category", ""),
                            "fail_types": example.get("fail_types") or [],
                            "missing_conditions": example.get("missing_conditions") or [],
                            "basis_paths": example.get("basis_paths") or [],
                            "evidence": str(example.get("evidence", "") or "").strip(),
                            "solution": str(example.get("solution", "") or "").strip(),
                        }
                        _append_step(
                            "同类错误超阈值，继续补位",
                            node="system",
                            level="warning",
                            detail=f"error_key={err_key} count={failure_key_counts[err_key]}",
                        )
            while _event_stream_buffer:
                yield _event_stream_buffer.pop(0)
            elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            if template_gap_final_failure and not saved_current and isinstance(q_json, dict):
                persisted, saved_item, save_err = _persist_template_gap_failed_item(
                    enabled=bool(persist_to_bank),
                    path=bank_path,
                    final_json=q_json,
                    question_trace=question_trace,
                    attempt_error_info=attempt_error_info,
                    task_id=task_id,
                    task_name=task_name,
                    run_id=run_id,
                )
                if persisted and isinstance(saved_item, dict):
                    q_json = saved_item
                    question_trace["final_json"] = deepcopy(saved_item)
                    generated.append(saved_item)
                    if planned_slots:
                        _template_slot_cursor2 = success_index + 1
                    saved += 1
                    saved_current = True
                    saved_with_issues_current = True
                    _append_step("题目已落库", node="system", level="warning", detail="模板唯一缺口失败入库，待修复")
                elif save_err:
                    errors.append(f"第{attempt_count}次尝试待修复题落库失败: {save_err}")
                    _append_step("待修复题落库失败", node="system", level="error", detail=save_err)
            question_trace["elapsed_ms"] = elapsed_ms
            question_trace["llm_trace"] = question_llm_trace
            question_trace["llm_summary"] = summarize_llm_trace(question_llm_trace)
            question_trace["unstable_flags"] = mark_unstable(question_trace["llm_summary"])
            question_trace["saved"] = bool(saved_current)
            question_trace["saved_with_issues"] = bool(saved_with_issues_current)
            if isinstance(q_json, dict):
                _attach_preview_context_to_question_payload(
                    q_json,
                    tenant_id=tenant_id,
                    material_version_id=material_version_id,
                    question_trace=question_trace,
                    source_path=str(kb_chunk.get("完整路径", "")),
                    source_slice_id=sid,
                    mother_questions=mother_questions,
                    mother_full_questions=mother_full_questions,
                )
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
            _ensure_critic_step_in_trace(question_trace)
            question_trace["snapshot_stage"] = "final"
            process_trace.append(question_trace)
            yield _sse(
                "question_done",
                {
                    "index": attempt_count,
                    "target_index": current_target_index,
                    "elapsed_ms": elapsed_ms,
                    "item": q_json if isinstance(q_json, dict) and critic_passed else None,
                    "trace": question_trace,
                    "generated_count": len(generated),
                    "saved_count": saved,
                    "saved_with_issues": bool(saved_with_issues_current),
                    "error_count": len(errors),
                    "fuse_triggered": fuse_triggered,
                    "fuse_info": fuse_info,
                },
            )
            if bool(saved_current) and int(sid) > 0:
                task_slice_usage_counts[int(sid)] = int(task_slice_usage_counts.get(int(sid), 0) or 0) + 1
        if (
            is_internal_subtask_request
            and len(generated) < target_question_count
            and attempt_count >= max_attempts
        ):
            fuse_triggered = True
            fuse_info = {
                "triggered": True,
                "category": "subtask_attempt_budget",
                "attempt_count": attempt_count,
                "max_attempts": max_attempts,
                "target_question_count": target_question_count,
                "passed_count": len(generated),
            }
            errors.append(
                f"子任务熔断：尝试总数(通过+失败)达到 {attempt_count}，"
                f"已触达子任务尝试次数上限({max_attempts})，目标 {target_question_count} 题，当前通过 {len(generated)} 题"
            )
        if template and len(generated) < target_question_count:
            saved, template_gap_errors = _persist_template_remaining_failed_slots(
                enabled=bool(persist_to_bank and _is_task_auto_bank_enabled(tenant_id, task_id, persist_to_bank)),
                bank_path=bank_path,
                planned_slots=planned_slots,
                process_trace=process_trace,
                generated=generated,
                saved_count=saved,
                task_id=task_id,
                task_name=task_name,
                run_id=run_id,
                question_type=question_type,
                failure_reason=errors[-1] if errors else "达到任务熔断或无可用切片",
            )
            if template_gap_errors:
                errors.extend(template_gap_errors)
            if len(generated) >= target_question_count:
                errors.append("模板缺口已自动补位入库为待修复题，请在题库按位次修复。")
        if template and len(generated) < target_question_count:
            errors.append(
                f"模板要求 {target_question_count} 题，但在 {attempt_count} 次尝试后仅生成 {len(generated)} 题通过 critic，请调整模板或切片范围后重试"
            )

        run_ended_at = datetime.now(timezone.utc).isoformat()
        qa_run = _build_qa_run_payload(
            tenant_id=tenant_id,
            run_id=run_id,
            material_version_id=material_version_id,
            config_payload={
                "tenant_id": tenant_id,
                "question_type": question_type,
                "generation_mode": generation_mode,
                "difficulty": difficulty,
                "difficulty_range": difficulty_range,
                "num_questions": target_question_count,
                "max_attempts": max_attempts,
                "model": model_name,
                "gen_scope_mode": gen_scope_mode,
                "persist_to_bank": persist_to_bank,
                "save_to_bank": persist_to_bank,
                "task_id": task_id,
                "template_id": template_id,
                "template_name": str(template.get("name", "")).strip() if template else "",
                "template_snapshot": template if template else None,
                "template_plan": template_plan,
                "fuse_info": fuse_info,
                "enable_offline_judge": False,
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
                    "num_questions": target_question_count,
                    "attempt_count": attempt_count,
                    "max_attempts": max_attempts,
                    "generated": len(generated),
                    "saved": saved,
                    "errors": errors,
                    "trace_count": len(process_trace),
                    "question_type": question_type,
                    "generation_mode": generation_mode,
                    "material_version_id": material_version_id,
                    "template_id": template_id,
                    "template_name": str(template.get("name", "")).strip() if template else "",
                    "run_id": run_id,
                    "fuse_info": fuse_info,
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
                "success": (len(generated) > 0) and not fuse_triggered,
                "fuse_triggered": fuse_triggered,
                "fuse_info": fuse_info,
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
    current_subcall = task.get("current_subcall") if isinstance(task.get("current_subcall"), dict) else {}
    repair_rounds = task.get("repair_rounds") if isinstance(task.get("repair_rounds"), list) else []
    slice_failure_stats = task.get("slice_failure_stats") if isinstance(task.get("slice_failure_stats"), list) else []
    subtasks = task.get("subtasks") if isinstance(task.get("subtasks"), list) else []
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
        "generated_total_count": int(
            task.get(
                "generated_total_count",
                int(task.get("generated_count", 0) or 0) + int(task.get("error_count", 0) or 0),
            )
            or 0
        ),
        "saved_count": int(task.get("saved_count", 0) or 0),
        "error_count": int(task.get("error_count", 0) or 0),
        "progress": task.get("progress") if isinstance(task.get("progress"), dict) else {"current": 0, "total": 0},
        "current_node": str(task.get("current_node", "")),
        "current_node_updated_at": str(task.get("current_node_updated_at", "")),
        "current_subcall": current_subcall,
        "subtask_count": len(subtasks),
        "repair_round_count": len(repair_rounds),
        "failure_slice_count": len(slice_failure_stats),
        "parent_task_id": str(task.get("parent_task_id", "") or ""),
        "parent_task_name": str(task.get("parent_task_name", "") or ""),
        "child_kind": str(task.get("child_kind", "") or ""),
        "request": {
            "num_questions": int(req.get("num_questions", 0) or 0),
            "question_type": str(req.get("question_type", "")),
            "generation_mode": _normalize_generation_mode(req.get("generation_mode", "")),
            "difficulty": str(req.get("difficulty", "")),
            "template_id": str(req.get("template_id", "")),
            "template_name": str(req.get("template_name", "")),
        },
    }


def _recover_task_counts_from_subtasks(task: dict[str, Any]) -> dict[str, Any]:
    """
    Recover parent-task counters from child subtasks.
    Used for template/parallel runs where parent may stop before final aggregation.
    """
    if not isinstance(task, dict):
        return task
    # Child task itself should not aggregate from its own subtasks.
    if str(task.get("parent_task_id", "") or "").strip():
        return task
    live_subtask_traces = [x for x in (task.get("live_subtask_traces") or []) if isinstance(x, dict)]
    if not live_subtask_traces:
        return task
    sub_generated_total = 0
    sub_saved_total = 0
    for sub in live_subtask_traces:
        trace_rows = [x for x in (sub.get("process_trace") or []) if isinstance(x, dict)]
        valid_saved_rows = [
            x for x in trace_rows
            if bool(x.get("saved")) and isinstance(x.get("final_json"), dict) and bool(x.get("final_json"))
        ]
        sub_generated_total += len(valid_saved_rows)
        sub_saved_total += len(valid_saved_rows)
    if sub_generated_total <= 0 and sub_saved_total <= 0:
        return task
    patched = dict(task)
    patched["generated_count"] = int(max(int(task.get("generated_count", 0) or 0), sub_generated_total))
    patched["saved_count"] = int(max(int(task.get("saved_count", 0) or 0), sub_saved_total))
    req = patched.get("request") if isinstance(patched.get("request"), dict) else {}
    progress = patched.get("progress") if isinstance(patched.get("progress"), dict) else {}
    total = int(progress.get("total", 0) or req.get("num_questions", 0) or 0)
    current = int(progress.get("current", 0) or 0)
    patched["progress"] = {
        "current": int(max(current, int(patched["generated_count"]))),
        "total": int(max(total, int(req.get("num_questions", 0) or 0), 0)),
    }
    return patched


def _is_internal_child_gen_task(task: dict[str, Any]) -> bool:
    if not isinstance(task, dict):
        return False
    if str(task.get("parent_task_id", "") or "").strip():
        return True
    if str(task.get("child_kind", "") or "").strip():
        return True
    req = task.get("request") if isinstance(task.get("request"), dict) else {}
    if str(req.get("parent_task_id", "") or "").strip():
        return True
    if str(req.get("child_kind", "") or "").strip():
        return True
    task_name = str(task.get("task_name", "") or req.get("task_name", "") or "").strip()
    if re.search(r"#(?:p\d+|repair\d+|resume[\w_]+)$", task_name, re.IGNORECASE):
        return True
    return False


def _read_persisted_task(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    latest_rows = _latest_gen_task_rows(tenant_id, allow_full_fallback=True)
    latest = latest_rows.get(str(task_id or "").strip())
    if isinstance(latest, dict):
        return latest
    for path in _qa_read_paths(tenant_id, "gen_tasks.jsonl"):
        for row in reversed(_read_jsonl(path)):
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


def _get_qa_run_by_id(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    """Load a single QA run by run_id for task detail enrichment."""
    rid = str(run_id or "").strip()
    if not rid:
        return None
    for row in reversed(_read_jsonl(_qa_runs_path(tenant_id))):
        if not isinstance(row, dict):
            continue
        if str(row.get("run_id", "")) == rid:
            return row
    return None


def _update_qa_run(tenant_id: str, run_id: str, updated_run: dict[str, Any]) -> bool:
    """Replace the run with run_id in qa_runs.jsonl with updated_run. Returns True if updated."""
    with QA_PERSIST_LOCK:
        path = _qa_runs_path(tenant_id)
        rid = str(run_id or "").strip()
        if not rid:
            return False
        rows = _read_jsonl(path)
        idx = -1
        for i in range(len(rows) - 1, -1, -1):
            if isinstance(rows[i], dict) and str(rows[i].get("run_id", "")) == rid:
                idx = i
                break
        if idx < 0:
            return False
        rows[idx] = updated_run
        _write_jsonl(path, rows)
        return True


def _enrich_task_with_qa_run(tenant_id: str, task: dict[str, Any]) -> None:
    """Attach batch_metrics and cost_summary from the task's run when run_id is present."""
    run_id = str(task.get("run_id", "") or "").strip()
    if not run_id:
        return
    run = _get_qa_run_by_id(tenant_id, run_id)
    if not isinstance(run, dict):
        return
    bm = run.get("batch_metrics")
    if isinstance(bm, dict):
        task["batch_metrics"] = bm
    cs = run.get("cost_summary")
    if isinstance(cs, dict):
        task["cost_summary"] = cs


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
        "tenant_id": tenant_id,
        "question_type": str(req.get("question_type", "")),
        "generation_mode": _normalize_generation_mode(req.get("generation_mode", "")),
        "difficulty": str(req.get("difficulty", "")),
        "num_questions": int(req.get("num_questions", 0) or 0),
        "gen_scope_mode": str(req.get("gen_scope_mode", "")),
        "task_id": str(task.get("task_id", "")),
        "enable_offline_judge": False,
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
    recovered_questions = _build_run_questions_from_bank(tenant_id, run_id)
    if recovered_questions:
        qa_run["questions"] = recovered_questions
        bm = qa_run.get("batch_metrics") if isinstance(qa_run.get("batch_metrics"), dict) else {}
        bm["question_count"] = len(recovered_questions)
        bm["generated_count"] = max(int(bm.get("generated_count", 0) or 0), len(recovered_questions))
        bm["saved_count"] = max(int(bm.get("saved_count", 0) or 0), len(recovered_questions))
        qa_run["batch_metrics"] = bm
        qa_run["trace_count"] = max(int(qa_run.get("trace_count", 0) or 0), len(recovered_questions))
    # Keep task identity at top level so UI selectors can display failed task names as run labels.
    qa_run["task_id"] = str(task.get("task_id", "") or config_payload.get("task_id", ""))
    qa_run["task_name"] = str(task.get("task_name", "") or str(req.get("task_name", "")))
    _persist_qa_run(tenant_id, qa_run)


def _run_generate_task_worker(tenant_id: str, task_id: str, body: dict[str, Any], system_user: str) -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    if bool((body or {}).get("resume_inplace")):
        latest = _get_latest_gen_task_snapshot(tenant_id, task_id) or {}
        prev_started = str(latest.get("started_at", "") or "").strip()
        if prev_started:
            # 续跑沿用原 started_at，任务总耗时按同一任务累计展示。
            started_at = prev_started
    _update_task_live(tenant_id, task_id, {"status": "running", "started_at": started_at})
    _persist_live_task_snapshot(tenant_id, task_id)
    try:
        body_with_task = dict(body or {})
        body_with_task["task_id"] = task_id
        resume_inplace = bool(body_with_task.get("resume_inplace"))
        seed_generated_count = 0
        seed_saved_count = 0
        seed_items: list[dict[str, Any]] = []
        seed_errors: list[str] = []
        seed_trace: list[dict[str, Any]] = []
        seed_subtasks: list[dict[str, Any]] = []
        if resume_inplace:
            with GEN_TASK_LOCK:
                seed_task = GEN_TASKS.get(task_id)
                if isinstance(seed_task, dict) and str(seed_task.get("tenant_id", "")) == tenant_id:
                    seed_generated_count = int(seed_task.get("generated_count", 0) or 0)
                    seed_saved_count = int(seed_task.get("saved_count", 0) or 0)
                    seed_items = [x for x in (seed_task.get("items") or []) if isinstance(x, dict)]
                    seed_errors = [str(x) for x in (seed_task.get("errors") or []) if str(x).strip()]
                    seed_trace = [x for x in (seed_task.get("process_trace") or []) if isinstance(x, dict)]
                    seed_subtasks = [x for x in (seed_task.get("subtasks") or []) if isinstance(x, dict)]
        total = int(body_with_task.get("num_questions", 0) or 0)
        total_for_progress = int(body_with_task.get("resume_original_total", 0) or 0) if resume_inplace else total
        if total_for_progress <= 0:
            total_for_progress = total
        current_for_progress = seed_generated_count if resume_inplace else 0
        _update_task_live(
            tenant_id,
            task_id,
            {
                "progress": {"current": current_for_progress, "total": total_for_progress},
                "material_version_id": str(body_with_task.get("material_version_id", "")),
            },
        )
        _persist_live_task_snapshot(tenant_id, task_id)

        done_payload: dict[str, Any] | None = None
        # 任务内并发策略：
        # - 非模板题：固定 3 并发
        # - 模板题：按全局 planned_slots 分片并发，保证最终聚合后仍满足整体模板要求
        is_template_task = bool(str(body_with_task.get("template_id", "") or "").strip())
        template_parallel_context: dict[str, Any] | None = None
        if is_template_task:
            template_parallel_context, template_parallel_error = _resolve_template_parallel_context(tenant_id, body_with_task)
            if template_parallel_error:
                _update_task_live(
                    tenant_id,
                    task_id,
                    {
                        "status": "failed",
                        "ended_at": datetime.now(timezone.utc).isoformat(),
                        "errors": [template_parallel_error],
                        "error_count": 1,
                    },
                )
                _persist_live_task_snapshot(tenant_id, task_id)
                return
            if resume_inplace and isinstance(template_parallel_context, dict):
                resume_slots_raw = [
                    slot for slot in (body_with_task.get("planned_slots") or [])
                    if isinstance(slot, dict) and str(slot.get("slice_id", "")).isdigit()
                ]
                if resume_slots_raw:
                    normalized_resume_slots: list[dict[str, Any]] = []
                    for idx, slot in enumerate(resume_slots_raw, start=1):
                        mapped_slot = dict(slot)
                        global_target_index = int(mapped_slot.get("_global_target_index", 0) or 0)
                        if global_target_index <= 0:
                            global_target_index = seed_generated_count + idx
                        mapped_slot["_global_target_index"] = int(global_target_index)
                        normalized_resume_slots.append(mapped_slot)
                    template_parallel_context = {
                        **template_parallel_context,
                        "planned_slots": normalized_resume_slots,
                        "planned_slice_ids": [
                            int(slot.get("slice_id"))
                            for slot in normalized_resume_slots
                            if str(slot.get("slice_id", "")).isdigit()
                        ],
                    }
        default_concurrency = max(1, int(os.getenv("GENERATE_TASK_CONCURRENCY", "5") or 5))
        concurrency = min(default_concurrency, max(1, total))

        def _normalize_resume_runtime_payload(payload: dict[str, Any]) -> dict[str, Any]:
            req_payload = dict(payload or {})
            if not resume_inplace:
                return req_payload
            slots = [slot for slot in (req_payload.get("planned_slots") or []) if isinstance(slot, dict)]
            cleaned_slots: list[dict[str, Any]] = []
            for slot in slots:
                try:
                    sid = int(slot.get("slice_id"))
                except (TypeError, ValueError):
                    continue
                cleaned_slots.append(
                    {
                        "slice_id": sid,
                        "route_prefix": str(slot.get("route_prefix", "") or "").strip(),
                        "mastery": str(slot.get("mastery", "") or "").strip(),
                        "_global_target_index": int(slot.get("_global_target_index", 0) or 0),
                    }
                )
            if not cleaned_slots:
                return req_payload
            req_payload["planned_slots"] = cleaned_slots
            req_payload["planned_slice_ids"] = [int(slot.get("slice_id")) for slot in cleaned_slots]
            req_payload["num_questions"] = int(len(cleaned_slots))
            # 续跑执行层仅按传入 planned_slots 生成，避免 template_id 触发全量模板重建。
            req_payload.pop("template_id", None)
            req_payload.pop("template_name", None)
            return req_payload

        def _invoke_generate_payload(payload: dict[str, Any], *, detach_from_parent_task: bool = False):
            req_payload = _normalize_resume_runtime_payload(payload)
            # 续跑场景下，不让子运行直接写父任务，避免中途失败时覆盖历史 process_trace/errors。
            if detach_from_parent_task:
                req_payload.pop("task_id", None)
            with app.test_request_context(
                f"/api/{tenant_id}/generate",
                method="POST",
                json=req_payload,
                headers={"X-System-User": system_user},
            ):
                return api_generate_questions(tenant_id)

        def _invoke_generate_child_task(payload: dict[str, Any], child_task_id: str):
            resp = None
            explicit_error = ""
            try:
                req_payload = _normalize_resume_runtime_payload(payload)
                req_payload["task_id"] = str(child_task_id or "")
                with app.test_request_context(
                    f"/api/{tenant_id}/generate",
                    method="POST",
                    json=req_payload,
                    headers={"X-System-User": system_user},
                ):
                    resp = api_generate_questions(tenant_id)
                return resp
            except Exception as e:
                explicit_error = f"续跑子任务异常: {e}"
                raise
            finally:
                _finalize_internal_child_gen_task(
                    tenant_id,
                    str(child_task_id or ""),
                    resp=resp,
                    explicit_error=explicit_error,
                )

        if concurrency <= 1 or total <= 1:
            # 续跑串行：按单题子批次执行，实时回写进度，避免长时间卡在同一进度。
            if resume_inplace and total > 1:
                planned_ids = [int(x) for x in (body_with_task.get("planned_slice_ids") or []) if str(x).isdigit()]
                planned_slots_raw = [slot for slot in (body_with_task.get("planned_slots") or []) if isinstance(slot, dict)]
                planned_slots = []
                for slot_idx, slot in enumerate(planned_slots_raw, start=1):
                    mapped_slot = dict(slot)
                    global_target_index = int(mapped_slot.get("_global_target_index", 0) or 0)
                    if global_target_index <= 0:
                        global_target_index = seed_generated_count + slot_idx
                    mapped_slot["_global_target_index"] = int(global_target_index)
                    planned_slots.append(mapped_slot)
                merged_traces: list[dict[str, Any]] = []
                merged_errors: list[str] = []
                merged_run_ids: list[str] = []
                resume_live_subtasks: list[dict[str, Any]] = [dict(x) for x in seed_subtasks]
                merged_saved_targets: set[int] = set()
                resume_batch_round_limit = max(1, int(os.getenv("RESUME_TEMPLATE_SUBTASK_ROUNDS", "4") or 4))
                resume_template_batch_size = max(1, int(os.getenv("RESUME_TEMPLATE_SUBTASK_SIZE", "3") or 3))
                is_template_resume = bool(str(body_with_task.get("template_id", "") or "").strip()) and bool(planned_slots)
                resume_batch_specs: list[dict[str, Any]] = []
                if is_template_resume:
                    for batch_idx, start in enumerate(range(0, len(planned_slots), resume_template_batch_size), start=1):
                        resume_batch_specs.append(
                            {
                                "batch_index": batch_idx,
                                "slots": [dict(x) for x in planned_slots[start : start + resume_template_batch_size]],
                            }
                        )
                else:
                    for idx in range(total):
                        slot_list: list[dict[str, Any]] = []
                        if planned_slots:
                            if idx < len(planned_slots):
                                slot_list = [dict(planned_slots[idx])]
                            else:
                                slot_list = [dict(planned_slots[-1])]
                        elif planned_ids:
                            sid = int(planned_ids[idx]) if idx < len(planned_ids) else int(planned_ids[-1])
                            slot_list = [{"slice_id": sid, "_global_target_index": seed_generated_count + idx + 1}]
                        resume_batch_specs.append({"batch_index": idx + 1, "slots": slot_list})

                for batch_spec in resume_batch_specs:
                    if _is_task_cancelled(task_id):
                        merged_errors.append("用户取消")
                        break
                    batch_slots_original = [dict(x) for x in (batch_spec.get("slots") or []) if isinstance(x, dict)]
                    remaining_batch_slots = [dict(x) for x in batch_slots_original]
                    batch_start_target = int(remaining_batch_slots[0].get("_global_target_index", 0) or 0) if remaining_batch_slots else 0
                    batch_end_target = int(remaining_batch_slots[-1].get("_global_target_index", 0) or 0) if remaining_batch_slots else 0
                    batch_round = 0
                    batch_saved_before = len(merged_saved_targets)
                    while remaining_batch_slots and batch_round < resume_batch_round_limit:
                        if _is_task_cancelled(task_id):
                            merged_errors.append("用户取消")
                            break
                        batch_round += 1
                        shard_payload = dict(body_with_task)
                        cleaned_slots = [
                            {
                                "slice_id": int(slot.get("slice_id")),
                                "route_prefix": str(slot.get("route_prefix", "") or "").strip(),
                                "mastery": str(slot.get("mastery", "") or "").strip(),
                            }
                            for slot in remaining_batch_slots
                        ]
                        shard_payload["num_questions"] = len(cleaned_slots)
                        if cleaned_slots:
                            shard_payload["planned_slots"] = cleaned_slots
                            shard_payload["planned_slice_ids"] = [int(slot.get("slice_id")) for slot in cleaned_slots]
                        current_slice_id = int(cleaned_slots[0].get("slice_id", 0) or 0) if cleaned_slots else 0
                        question_label = (
                            f"第 {batch_start_target} 题"
                            if batch_start_target == batch_end_target
                            else f"第 {batch_start_target}-{batch_end_target} 题"
                        )
                        child_suffix = (
                            f"resume{batch_start_target}"
                            if batch_start_target == batch_end_target
                            else f"resume{batch_start_target}_{batch_end_target}_r{batch_round}"
                        )
                        child_task = _create_internal_child_gen_task(
                            tenant_id,
                            system_user,
                            {
                                "task_id": task_id,
                                "task_name": str(body_with_task.get("task_name", "") or ""),
                            },
                            shard_payload,
                            child_suffix=child_suffix,
                            child_kind="resume",
                        )
                        child_task_id = str(child_task.get("task_id", "") or "")
                        child_task_name = str(child_task.get("task_name", "") or "")
                        resume_live_subtasks.append(
                            {
                                "task_id": child_task_id,
                                "task_name": child_task_name,
                                "run_id": "",
                                "kind": "resume",
                                "round": batch_round,
                                "shard_index": batch_start_target,
                                "status": "running",
                                "started_at": str(child_task.get("started_at", "") or ""),
                                "ended_at": "",
                                "generated_count": 0,
                                "saved_count": 0,
                                "error_count": 0,
                                "latest_error": "",
                                "source_task_id": task_id,
                                "target_start": batch_start_target,
                                "target_end": batch_end_target,
                                "target_total": len(batch_slots_original),
                            }
                        )
                        _update_task_live(
                            tenant_id,
                            task_id,
                            {
                                "subtasks": resume_live_subtasks,
                                "current_node": "resume_subrun",
                                "current_node_updated_at": datetime.now(timezone.utc).isoformat(),
                                "current_subcall": {
                                    "mode": "resume_subrun",
                                    "question_label": question_label,
                                    "target_index": batch_start_target,
                                    "slice_id": current_slice_id,
                                    "child_task_id": child_task_id,
                                    "child_task_name": child_task_name,
                                    "batch_round": batch_round,
                                    "batch_target_total": len(batch_slots_original),
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                },
                            },
                        )
                        _persist_live_task_snapshot(tenant_id, task_id)
                        sub_ex = ThreadPoolExecutor(max_workers=1)
                        future = sub_ex.submit(_invoke_generate_child_task, shard_payload, child_task_id)
                        child_q = int(shard_payload.get("num_questions", len(cleaned_slots)) or len(cleaned_slots) or 1)
                        child_max_attempts = int(
                            shard_payload.get("max_attempts", _estimate_internal_subtask_max_attempts(child_q))
                            or _estimate_internal_subtask_max_attempts(child_q)
                        )
                        child_timeout_seconds = _estimate_parallel_child_timeout_seconds(
                            question_count=child_q,
                            max_attempts=child_max_attempts,
                        )
                        resp = None
                        timed_out = False
                        deadline = time.monotonic() + float(child_timeout_seconds)
                        heartbeat_interval = max(1.0, min(5.0, float(os.getenv("RESUME_SUBTASK_HEARTBEAT_SECONDS", "2") or 2)))
                        while not future.done():
                            if time.monotonic() >= deadline:
                                timed_out = True
                                future.cancel()
                                merged_errors.append(
                                    f"续跑子批次超时({child_timeout_seconds}s)：第 {batch_start_target}-{batch_end_target} 题第 {batch_round} 轮"
                                )
                                break
                            child_latest = _get_latest_gen_task_snapshot(tenant_id, child_task_id) or {}
                            child_errors = [str(x) for x in (child_latest.get("errors") or []) if str(x).strip()]
                            child_status = str(child_latest.get("status", "") or "running")
                            for sub in resume_live_subtasks:
                                if str(sub.get("task_id", "") or "") != child_task_id:
                                    continue
                                sub["run_id"] = str(child_latest.get("run_id", "") or sub.get("run_id", "") or "")
                                sub["generated_count"] = int(child_latest.get("generated_count", sub.get("generated_count", 0)) or 0)
                                sub["saved_count"] = int(child_latest.get("saved_count", sub.get("saved_count", 0)) or 0)
                                sub["error_count"] = int(child_latest.get("error_count", sub.get("error_count", 0)) or 0)
                                sub["status"] = child_status
                                if child_errors:
                                    sub["latest_error"] = child_errors[-1]
                                if str(child_latest.get("ended_at", "") or "").strip():
                                    sub["ended_at"] = str(child_latest.get("ended_at", "") or "")
                                break
                            live_saved_by_subtasks = sum(int(x.get("saved_count", 0) or 0) for x in resume_live_subtasks)
                            live_generated = int(seed_generated_count + live_saved_by_subtasks)
                            parent_latest = _get_latest_gen_task_snapshot(tenant_id, task_id) or {}
                            _update_task_live(
                                tenant_id,
                                task_id,
                                {
                                    "subtasks": resume_live_subtasks,
                                    "generated_count": int(max(live_generated, int(parent_latest.get("generated_count", 0) or 0))),
                                    "saved_count": int(max(seed_saved_count + live_saved_by_subtasks, int(parent_latest.get("saved_count", 0) or 0))),
                                    "current_node": "resume_subrun",
                                    "current_node_updated_at": datetime.now(timezone.utc).isoformat(),
                                    "current_subcall": {
                                        "mode": "resume_subrun",
                                        "question_label": question_label,
                                        "target_index": batch_start_target,
                                        "slice_id": current_slice_id,
                                        "child_task_id": child_task_id,
                                        "child_task_name": child_task_name,
                                        "child_task_status": child_status,
                                        "batch_round": batch_round,
                                        "batch_target_total": len(batch_slots_original),
                                        "batch_remaining": len(remaining_batch_slots),
                                        "updated_at": datetime.now(timezone.utc).isoformat(),
                                    },
                                },
                            )
                            _persist_live_task_snapshot(tenant_id, task_id)
                            time.sleep(heartbeat_interval)
                        try:
                            if not timed_out:
                                resp = future.result(timeout=0.1)
                        except FuturesTimeoutError:
                            resp = None
                        finally:
                            sub_ex.shutdown(wait=False, cancel_futures=False)

                        if resp is None:
                            merged_errors.append(
                                f"续跑子批次未拿到生成响应，第 {batch_start_target}-{batch_end_target} 题第 {batch_round} 轮跳过"
                            )
                            child_latest = _get_latest_gen_task_snapshot(tenant_id, child_task_id) or {}
                            child_errors = [str(x) for x in (child_latest.get("errors") or []) if str(x).strip()]
                            for sub in resume_live_subtasks:
                                if str(sub.get("task_id", "") or "") != child_task_id:
                                    continue
                                sub["run_id"] = str(child_latest.get("run_id", "") or "")
                                sub["generated_count"] = int(child_latest.get("generated_count", 0) or 0)
                                sub["saved_count"] = int(child_latest.get("saved_count", 0) or 0)
                                sub["error_count"] = len(child_errors)
                                sub["status"] = str(child_latest.get("status", "") or "failed")
                                sub["ended_at"] = str(child_latest.get("ended_at", "") or "")
                                sub["latest_error"] = child_errors[-1] if child_errors else ""
                                break
                            _update_task_live(tenant_id, task_id, {"subtasks": resume_live_subtasks})
                            _persist_live_task_snapshot(tenant_id, task_id)
                            continue

                        status_code = int(getattr(resp, "status_code", 200) or 200)
                        payload: dict[str, Any] = {}
                        try:
                            payload = resp.get_json(silent=True) or {}
                        except Exception:
                            payload = {}
                        child_latest = _get_latest_gen_task_snapshot(tenant_id, child_task_id) or {}
                        child_errors = [str(x) for x in (child_latest.get("errors") or []) if str(x).strip()]
                        if status_code >= 400:
                            msg = str(((payload.get("error") if isinstance(payload, dict) else {}) or {}).get("message", "")).strip()
                            merged_errors.append(
                                msg or f"续跑子批次失败({status_code})，第 {batch_start_target}-{batch_end_target} 题第 {batch_round} 轮未补齐"
                            )
                            for sub in resume_live_subtasks:
                                if str(sub.get("task_id", "") or "") != child_task_id:
                                    continue
                                sub["run_id"] = str(payload.get("run_id", "") or child_latest.get("run_id", "") or "")
                                sub["generated_count"] = int(payload.get("generated_count", 0) or child_latest.get("generated_count", 0) or 0)
                                sub["saved_count"] = int(payload.get("saved_count", 0) or child_latest.get("saved_count", 0) or 0)
                                sub["error_count"] = len(child_errors) if child_errors else len([str(x) for x in (payload.get("errors") or []) if str(x).strip()])
                                sub["status"] = str(child_latest.get("status", "") or "failed")
                                sub["ended_at"] = str(child_latest.get("ended_at", "") or "")
                                sub["latest_error"] = msg or (child_errors[-1] if child_errors else "")
                                break
                            _update_task_live(tenant_id, task_id, {"subtasks": resume_live_subtasks})
                            _persist_live_task_snapshot(tenant_id, task_id)
                            continue

                        new_traces_raw = [x for x in (payload.get("process_trace") or []) if isinstance(x, dict)]
                        trace_base = len(seed_trace) + len(merged_traces)
                        batch_saved_this_round: set[int] = set()
                        new_traces: list[dict[str, Any]] = []
                        for t_idx, row in enumerate(new_traces_raw, start=1):
                            mapped = dict(row)
                            mapped_idx = int(mapped.get("index", 0) or t_idx)
                            local_target = int(mapped.get("target_index", 0) or t_idx)
                            if 1 <= local_target <= len(remaining_batch_slots):
                                global_target = int(remaining_batch_slots[local_target - 1].get("_global_target_index", batch_start_target))
                            else:
                                global_target = batch_start_target
                            mapped["index"] = trace_base + mapped_idx
                            mapped["target_index"] = global_target
                            new_traces.append(mapped)
                            if bool(mapped.get("saved")):
                                batch_saved_this_round.add(global_target)
                        merged_traces.extend(new_traces)
                        merged_errors.extend([str(x) for x in (payload.get("errors") or []) if str(x).strip()])
                        rid = str(payload.get("run_id", "") or "").strip()
                        if rid:
                            merged_run_ids.append(rid)
                        merged_saved_targets.update(batch_saved_this_round)
                        remaining_batch_slots = [
                            dict(slot)
                            for slot in remaining_batch_slots
                            if int(slot.get("_global_target_index", 0) or 0) not in batch_saved_this_round
                        ]
                        for sub in resume_live_subtasks:
                            if str(sub.get("task_id", "") or "") != child_task_id:
                                continue
                            sub["run_id"] = rid or str(child_latest.get("run_id", "") or "")
                            sub["generated_count"] = int(payload.get("generated_count", 0) or 0)
                            sub["saved_count"] = int(payload.get("saved_count", 0) or 0)
                            sub["error_count"] = len(child_errors) if child_errors else len([str(x) for x in (payload.get("errors") or []) if str(x).strip()])
                            sub["status"] = str(child_latest.get("status", "") or ("completed" if not remaining_batch_slots else "partial"))
                            sub["ended_at"] = str(child_latest.get("ended_at", "") or datetime.now(timezone.utc).isoformat())
                            sub["latest_error"] = child_errors[-1] if child_errors else ""
                            sub["completed_targets"] = int(payload.get("saved_count", 0) or 0)
                            sub["remaining_targets"] = len(remaining_batch_slots)
                            break

                        valid_saved_traces = (
                            _collect_unique_saved_template_traces(
                                planned_slots=planned_slots,
                                process_trace=merged_traces,
                            )
                            if is_template_resume
                            else [
                                x for x in merged_traces
                                if isinstance(x, dict) and bool(x.get("saved")) and isinstance(x.get("final_json"), dict)
                            ]
                        )
                        valid_saved_traces.sort(key=lambda x: int(x.get("target_index", 0) or 0))
                        live_items = [dict(x.get("final_json")) for x in valid_saved_traces]
                        live_trace = list(seed_trace) + list(merged_traces)
                        live_generated = seed_generated_count + len(valid_saved_traces)
                        next_target = min(live_generated + 1, int(total_for_progress))
                        _update_task_live(
                            tenant_id,
                            task_id,
                            {
                                "items": list(seed_items) + live_items,
                                "process_trace": live_trace,
                                "subtasks": resume_live_subtasks,
                                "generated_count": int(live_generated),
                                "saved_count": int(seed_saved_count + len(valid_saved_traces)),
                                "errors": list(seed_errors) + list(merged_errors),
                                "progress": {
                                    "current": min(live_generated, int(total_for_progress)),
                                    "total": int(total_for_progress),
                                },
                                "current_question_id": f"第 {next_target} 题" if live_generated < int(total_for_progress) else "",
                                "current_node": "resume_subrun",
                                "current_node_updated_at": datetime.now(timezone.utc).isoformat(),
                                "current_subcall": {
                                    "mode": "resume_subrun",
                                    "question_label": question_label,
                                    "target_index": batch_start_target,
                                    "slice_id": current_slice_id,
                                    "child_task_id": child_task_id,
                                    "child_task_name": child_task_name,
                                    "child_task_status": str((child_latest or {}).get("status", "") or ("completed" if not remaining_batch_slots else "partial")),
                                    "batch_round": batch_round,
                                    "batch_target_total": len(batch_slots_original),
                                    "batch_remaining": len(remaining_batch_slots),
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                },
                            },
                            live_trace,
                        )
                        _persist_live_task_snapshot(tenant_id, task_id)
                        if not remaining_batch_slots:
                            break

                    if remaining_batch_slots:
                        batch_saved_after = len(merged_saved_targets)
                        if batch_saved_after <= batch_saved_before:
                            merged_errors.append(
                                f"续跑子批次未补齐：第 {batch_start_target}-{batch_end_target} 题在 {resume_batch_round_limit} 轮内无新增成功题"
                            )
                        else:
                            merged_errors.append(
                                f"续跑子批次未补齐：第 {batch_start_target}-{batch_end_target} 题仍缺 {len(remaining_batch_slots)} 题"
                            )

                valid_saved_traces = (
                    _collect_unique_saved_template_traces(
                        planned_slots=planned_slots,
                        process_trace=merged_traces,
                    )
                    if is_template_resume
                    else [
                        x for x in merged_traces
                        if isinstance(x, dict) and bool(x.get("saved")) and isinstance(x.get("final_json"), dict)
                    ]
                )
                valid_saved_traces.sort(key=lambda x: int(x.get("target_index", 0) or 0))
                merged_items = [dict(x.get("final_json")) for x in valid_saved_traces]
                done_payload = {
                    "run_id": ",".join(merged_run_ids),
                    "items": merged_items,
                    "generated_count": len(merged_items),
                    "saved_count": len(merged_items),
                    "errors": merged_errors,
                    "process_trace": merged_traces,
                    "material_version_id": str(body_with_task.get("material_version_id", "")),
                    "success": len(merged_items) > 0,
                    "partial_completed": len(merged_items) > 0 and len(merged_items) < total,
                }
            else:
                # 原有串行行为
                resp = _invoke_generate_payload(body_with_task, detach_from_parent_task=resume_inplace)
                if resp is None:
                    raise RuntimeError("任务失败：未拿到生成响应")
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
                    if resume_inplace:
                        merged_errors = list(seed_errors)
                        merged_errors.append(msg)
                        failed_task = {
                            "status": "failed",
                            "ended_at": ended_at,
                            "errors": merged_errors,
                            "error_count": len(merged_errors),
                        }
                    else:
                        failed_task = {
                            "status": "failed",
                            "ended_at": ended_at,
                            "errors": [msg],
                            "error_count": 1,
                        }
                    _update_task_live(tenant_id, task_id, failed_task)
                    _persist_live_task_snapshot(tenant_id, task_id)
                    task_to_persist: dict[str, Any] | None = None
                    with GEN_TASK_LOCK:
                        task = GEN_TASKS.get(task_id)
                        if task:
                            task_to_persist = _task_snapshot(task)
                    if isinstance(task_to_persist, dict):
                        _persist_failed_task_qa_run(
                            tenant_id,
                            task_to_persist,
                            reason=msg,
                            started_at=started_at,
                            ended_at=ended_at,
                        )
                        _persist_gen_task(tenant_id, task_to_persist)
                    return
                try:
                    done_payload = resp.get_json(silent=True) or {}
                except Exception:
                    done_payload = {}
        else:
            shard_specs: list[dict[str, Any]] = []
            if is_template_task and isinstance(template_parallel_context, dict):
                planned_slots_all = [slot for slot in (template_parallel_context.get("planned_slots") or []) if isinstance(slot, dict)]
                template_plan = template_parallel_context.get("template_plan") if isinstance(template_parallel_context.get("template_plan"), dict) else {}
                min_shard_size = _compute_template_parallel_min_shard_size(template_plan, len(planned_slots_all))
                template_shard_cap = min(5, max(1, len(planned_slots_all) // max(1, min_shard_size)))
                shard_count = max(1, template_shard_cap)
                template_shards = _split_template_slots_for_parallel(planned_slots_all, shard_count)
                if len(template_shards) == 1:
                    single_shard_slots = [slot for slot in (template_shards[0] or []) if isinstance(slot, dict)]
                    single_shard_parallel = min(5, len(single_shard_slots))
                    if single_shard_parallel > 1:
                        template_shards = _split_template_slots_for_parallel(single_shard_slots, single_shard_parallel)
                        shard_count = len(template_shards)
                    else:
                        shard_count = 1
                for idx, shard_slots in enumerate(template_shards):
                    global_candidate_slice_ids = [
                        int(sid) for sid in ((template_parallel_context or {}).get("candidate_slice_ids") or [])
                        if str(sid).isdigit()
                    ]
                    cleaned_slots = [
                        {
                            "slice_id": int(slot.get("slice_id")),
                            "route_prefix": str(slot.get("route_prefix", "") or "").strip(),
                            "mastery": str(slot.get("mastery", "") or "").strip(),
                        }
                        for slot in shard_slots
                    ]
                    shard_specs.append(
                        {
                            "shard_idx": idx,
                            "mode": "template",
                            "slots": shard_slots,
                            "payload": {
                                **dict(body_with_task),
                                "num_questions": len(cleaned_slots),
                                "planned_slots": cleaned_slots,
                                "planned_slice_ids": [int(slot.get("slice_id")) for slot in cleaned_slots],
                                "slice_ids": global_candidate_slice_ids,
                                "task_name": f"{str(body_with_task.get('task_name', '')).strip()}#p{idx + 1}",
                            },
                        }
                    )
            else:
                shard_count = min(concurrency, total)
                base = total // shard_count
                remainder = total % shard_count
                shard_sizes = [base + (1 if i < remainder else 0) for i in range(shard_count)]
                shard_sizes = [x for x in shard_sizes if x > 0]
                for idx, shard_num in enumerate(shard_sizes):
                    shard_specs.append(
                        {
                            "shard_idx": idx,
                            "mode": "regular",
                            "payload": {
                                **dict(body_with_task),
                                "num_questions": int(shard_num),
                                "task_name": f"{str(body_with_task.get('task_name', '')).strip()}#p{idx + 1}",
                            },
                        }
                    )

            merged_errors: list[str] = []
            merged_run_ids: list[str] = []
            shard_results: dict[int, dict[str, Any]] = {}
            completed_shards = 0
            live_subtasks: list[dict[str, Any]] = []
            is_template_parallel = bool(is_template_task and isinstance(template_parallel_context, dict))

            def _refresh_template_subtasks_from_traces(
                traces: list[dict[str, Any]],
                *,
                finalize: bool = False,
            ) -> None:
                """按当前全量 trace 回写模板分片子任务进度/状态。"""
                if not is_template_parallel:
                    return
                trace_rows = [x for x in (traces or []) if isinstance(x, dict)]
                planned_slots_for_validate = [
                    slot for slot in ((template_parallel_context or {}).get("planned_slots") or [])
                    if isinstance(slot, dict)
                ]
                candidate_lookup_for_validate = (
                    (template_parallel_context or {}).get("candidate_lookup")
                    if isinstance((template_parallel_context or {}).get("candidate_lookup"), dict)
                    else {}
                )
                validation_report = _analyze_template_parallel_result(
                    planned_slots=planned_slots_for_validate,
                    process_trace=trace_rows,
                    candidate_lookup=candidate_lookup_for_validate,
                )
                valid_target_indexes = {
                    int(k)
                    for k in ((validation_report.get("valid_by_target") or {}).keys() if isinstance(validation_report, dict) else [])
                    if int(k) > 0
                }
                now_iso = datetime.now(timezone.utc).isoformat()
                for sub in live_subtasks:
                    if str(sub.get("kind", "") or "") != "shard":
                        continue
                    if str(sub.get("status", "") or "") in {"failed", "cancelled"}:
                        continue
                    target_total = int(sub.get("target_total", 0) or 0)
                    planned_targets = [
                        int(x) for x in (sub.get("planned_target_indexes") or [])
                        if str(x).isdigit() and int(x) > 0
                    ]
                    required_targets: set[int] = set(planned_targets)
                    if not required_targets:
                        start = int(sub.get("target_start", 0) or 0)
                        end = int(sub.get("target_end", 0) or 0)
                        if start > 0 and end >= start:
                            required_targets = {x for x in range(start, end + 1)}
                    if not target_total:
                        target_total = len(required_targets)
                    achieved_targets = required_targets & valid_target_indexes if required_targets else set(valid_target_indexes)
                    achieved = len(achieved_targets)
                    sub["generated_count"] = int(achieved)
                    sub["saved_count"] = int(achieved)
                    unmet = target_total > 0 and achieved < target_total
                    if unmet:
                        missing = target_total - achieved
                        if finalize:
                            sub["status"] = "partial"
                            if not str(sub.get("ended_at", "") or "").strip():
                                sub["ended_at"] = now_iso
                            sub["latest_error"] = f"未达目标位次：{achieved}/{target_total}，仍缺 {missing} 个合规位次"
                        else:
                            sub["status"] = "running"
                            sub["ended_at"] = ""
                            sub["latest_error"] = f"未达目标位次：{achieved}/{target_total}，继续补齐中"
                    else:
                        sub["status"] = "completed"
                        if not str(sub.get("ended_at", "") or "").strip():
                            sub["ended_at"] = now_iso
                        sub["latest_error"] = ""

            with ThreadPoolExecutor(max_workers=max(1, len(shard_specs))) as ex:
                future_map: dict[Any, dict[str, Any]] = {}
                future_meta: dict[Any, dict[str, Any]] = {}
                child_timeouts: list[int] = []
                for spec in shard_specs:
                    shard_payload = dict(spec.get("payload") or {})
                    shard_payload.pop("task_id", None)
                    if spec.get("mode") == "template":
                        shard_payload.pop("template_id", None)
                    shard_slots = [slot for slot in (spec.get("slots") or []) if isinstance(slot, dict)]
                    child_suffix = f"p{int(spec.get('shard_idx', 0) or 0) + 1}"
                    child_kind = "shard"
                    child_task = _create_internal_child_gen_task(
                        tenant_id,
                        system_user,
                        {
                            "task_id": task_id,
                            "task_name": str(body_with_task.get("task_name", "") or ""),
                        },
                        shard_payload,
                        child_suffix=child_suffix,
                        child_kind=child_kind,
                    )
                    child_task_id = str(child_task.get("task_id", "") or "")
                    future = ex.submit(_invoke_generate_child_task, shard_payload, child_task_id)
                    future_map[future] = spec
                    child_q = int(shard_payload.get("num_questions", 1) or 1)
                    child_max_attempts = int(
                        shard_payload.get("max_attempts", _estimate_internal_subtask_max_attempts(child_q))
                        or _estimate_internal_subtask_max_attempts(child_q)
                    )
                    child_timeout_seconds = _estimate_parallel_child_timeout_seconds(
                        question_count=child_q,
                        max_attempts=child_max_attempts,
                    )
                    child_timeouts.append(int(child_timeout_seconds))
                    future_meta[future] = {
                        "task_id": child_task_id,
                        "task_name": str(shard_payload.get("task_name", "") or ""),
                        "timeout_seconds": int(child_timeout_seconds),
                    }
                    target_start = 0
                    target_end = 0
                    if shard_slots:
                        target_start = int(shard_slots[0].get("_global_target_index", 0) or 0)
                        target_end = int(shard_slots[-1].get("_global_target_index", 0) or 0)
                    planned_target_indexes = [
                        int(slot.get("_global_target_index", 0) or 0)
                        for slot in shard_slots
                        if int(slot.get("_global_target_index", 0) or 0) > 0
                    ]
                    live_subtasks.append(
                        {
                            "task_id": child_task_id,
                            "task_name": str(shard_payload.get("task_name", "") or ""),
                            "run_id": "",
                            "kind": child_kind if str(spec.get("mode", "") or "") == "template" else str(spec.get("mode", "") or "child"),
                            "round": 0,
                            "shard_index": int(spec.get("shard_idx", 0) or 0) + 1,
                            "status": "running",
                            "started_at": str(child_task.get("started_at", "") or datetime.now(timezone.utc).isoformat()),
                            "ended_at": "",
                            "generated_count": 0,
                            "saved_count": 0,
                            "error_count": 0,
                            "latest_error": "",
                            "source_task_id": task_id,
                            "target_start": target_start,
                            "target_end": target_end,
                            "target_total": int(len(shard_slots) or int(shard_payload.get("num_questions", 0) or 0)),
                            "planned_target_indexes": planned_target_indexes,
                        }
                    )
                _update_task_live(
                    tenant_id,
                    task_id,
                    {
                        "subtasks": live_subtasks,
                        "current_subcall": {
                            "mode": "parallel_shards",
                            "question_label": "",
                            "target_index": 0,
                            "slice_id": 0,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    },
                )
                _persist_live_task_snapshot(tenant_id, task_id)
                batch_timeout_seconds = _estimate_parallel_batch_timeout_seconds(
                    child_timeouts=child_timeouts,
                    subtask_count=len(shard_specs),
                )
                def _apply_parallel_shard_result(spec: dict[str, Any], status_code: int, payload: dict[str, Any]) -> None:
                    shard_results[int(spec.get("shard_idx", 0) or 0)] = {
                        "spec": spec,
                        "status_code": int(status_code),
                        "payload": payload if isinstance(payload, dict) else {},
                    }
                    if int(status_code) >= 400:
                        msg = str(((payload.get("error") if isinstance(payload, dict) else {}) or {}).get("message", "")).strip()
                        merged_errors.append(msg or f"并发子任务失败({int(status_code)})")
                    else:
                        rid = str((payload.get("run_id") if isinstance(payload, dict) else "") or "").strip()
                        if rid:
                            merged_run_ids.append(rid)
                    shard_name = str((spec.get("payload") or {}).get("task_name", "") or "")
                    child_task_latest = None
                    for sub in live_subtasks:
                        if str(sub.get("task_name", "") or "") != shard_name:
                            continue
                        child_task_latest = _get_latest_gen_task_snapshot(tenant_id, str(sub.get("task_id", "") or ""))
                        sub["run_id"] = str(
                            (payload.get("run_id") if isinstance(payload, dict) else "")
                            or (child_task_latest or {}).get("run_id", "")
                            or ""
                        )
                        sub["generated_count"] = int(
                            (payload.get("generated_count", 0) if isinstance(payload, dict) else 0)
                            or (child_task_latest or {}).get("generated_count", 0)
                            or len([x for x in ((payload.get("items") if isinstance(payload, dict) else []) or []) if isinstance(x, dict)])
                        )
                        sub["saved_count"] = int(
                            (payload.get("saved_count", 0) if isinstance(payload, dict) else 0)
                            or (child_task_latest or {}).get("saved_count", 0)
                            or 0
                        )
                        payload_errors = [
                            str(x) for x in ((payload.get("errors") if isinstance(payload, dict) else []) or [])
                            if str(x).strip()
                        ]
                        sub["error_count"] = len(payload_errors) or int((child_task_latest or {}).get("error_count", 0) or 0)
                        sub["ended_at"] = str((child_task_latest or {}).get("ended_at", "") or datetime.now(timezone.utc).isoformat())
                        target_total = int(sub.get("target_total", 0) or 0)
                        is_unmet = target_total > 0 and sub["generated_count"] < target_total
                        fallback_status = "failed" if int(status_code) >= 400 else (
                            "running" if (is_template_parallel and is_unmet) else (
                                "partial" if (sub["error_count"] > 0 or is_unmet) else "completed"
                            )
                        )
                        sub["status"] = str((child_task_latest or {}).get("status", "") or fallback_status)
                        if is_template_parallel and is_unmet and sub["status"] == "running":
                            sub["ended_at"] = ""
                            sub["latest_error"] = f"未达目标：{int(sub['generated_count'])}/{target_total}，继续补齐中"
                        elif sub["error_count"] > 0:
                            errs = payload_errors
                            if not errs and isinstance(child_task_latest, dict):
                                errs = [str(x) for x in ((child_task_latest or {}).get("errors") or []) if str(x).strip()]
                            sub["latest_error"] = errs[-1] if errs else ""
                        else:
                            sub["latest_error"] = ""
                        break

                def _refresh_parallel_parent_progress() -> None:
                    est_generated = 0
                    for row in shard_results.values():
                        p = row.get("payload") if isinstance(row, dict) else {}
                        est_generated += len([x for x in ((p or {}).get("items") or []) if isinstance(x, dict)])
                    _update_task_live(
                        tenant_id,
                        task_id,
                        {
                            "progress": {"current": min(est_generated, total), "total": total},
                            "current_node": "system",
                            "current_node_updated_at": datetime.now(timezone.utc).isoformat(),
                            "subtasks": live_subtasks,
                            "current_subcall": {
                                "mode": "parallel_shards",
                                "completed_subtasks": completed_shards,
                                "total_subtasks": len(shard_specs),
                                "question_label": "",
                                "target_index": 0,
                                "slice_id": 0,
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            },
                        },
                    )
                    _persist_live_task_snapshot(tenant_id, task_id)

                try:
                    future_iter = as_completed(list(future_map.keys()), timeout=float(batch_timeout_seconds))
                    for fut in future_iter:
                        spec = future_map[fut]
                        try:
                            resp = fut.result()
                            status_code = int(getattr(resp, "status_code", 200) or 200)
                            payload: dict[str, Any] = {}
                            try:
                                payload = resp.get_json(silent=True) or {}
                            except Exception:
                                payload = {}
                        except Exception as fut_err:
                            status_code = 500
                            payload = {}
                            merged_errors.append(f"并发子任务异常: {fut_err}")
                        _apply_parallel_shard_result(spec, status_code, payload)
                        completed_shards += 1
                        _refresh_parallel_parent_progress()
                except FuturesTimeoutError:
                    timeout_now = datetime.now(timezone.utc).isoformat()
                    for fut, spec in future_map.items():
                        shard_idx = int(spec.get("shard_idx", 0) or 0)
                        if shard_idx in shard_results:
                            continue
                        if fut.done():
                            try:
                                resp = fut.result(timeout=0)
                                status_code = int(getattr(resp, "status_code", 200) or 200)
                                payload: dict[str, Any] = {}
                                try:
                                    payload = resp.get_json(silent=True) or {}
                                except Exception:
                                    payload = {}
                            except Exception as fut_err:
                                status_code = 500
                                payload = {}
                                merged_errors.append(f"并发子任务异常: {fut_err}")
                            _apply_parallel_shard_result(spec, status_code, payload)
                            completed_shards += 1
                            continue
                        fut.cancel()
                        meta = future_meta.get(fut) or {}
                        timeout_seconds = int(meta.get("timeout_seconds", batch_timeout_seconds) or batch_timeout_seconds)
                        shard_name = str((spec.get("payload") or {}).get("task_name", "") or str(meta.get("task_name", "") or ""))
                        merged_errors.append(f"并发子任务超时({timeout_seconds}s): {shard_name or 'unknown_shard'}")
                        shard_results[shard_idx] = {
                            "spec": spec,
                            "status_code": 504,
                            "payload": {"errors": [f"并发子任务超时({timeout_seconds}s)"]},
                        }
                        for sub in live_subtasks:
                            if str(sub.get("task_name", "") or "") != shard_name:
                                continue
                            sub["status"] = "failed"
                            sub["ended_at"] = timeout_now
                            sub["error_count"] = int(sub.get("error_count", 0) or 0) + 1
                            sub["latest_error"] = f"并发子任务超时({timeout_seconds}s)"
                            break
                        completed_shards += 1
                    _refresh_parallel_parent_progress()

            merged_items: list[dict[str, Any]] = []
            merged_traces: list[dict[str, Any]] = []
            merged_saved = 0
            ordered_entries: list[dict[str, Any]] = []
            trace_seq = 0
            for shard_idx in sorted(shard_results.keys()):
                row = shard_results.get(shard_idx) or {}
                spec = row.get("spec") if isinstance(row.get("spec"), dict) else {}
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                status_code = int(row.get("status_code", 200) or 200)
                if status_code >= 400:
                    continue
                shard_items = [x for x in (payload.get("items") or []) if isinstance(x, dict)]
                shard_traces_raw = [x for x in (payload.get("process_trace") or []) if isinstance(x, dict)]
                shard_mode = str(spec.get("mode", "") or "")
                if shard_mode == "template":
                    shard_slots = [slot for slot in (spec.get("slots") or []) if isinstance(slot, dict)]
                    saved_traces_local = [x for x in shard_traces_raw if bool(x.get("saved"))]
                    saved_traces_local.sort(key=lambda x: int(x.get("target_index", 0) or 0))
                    for t_idx, tr in enumerate(shard_traces_raw, start=1):
                        mapped = dict(tr)
                        local_target = int(mapped.get("target_index", 0) or t_idx)
                        if 1 <= local_target <= len(shard_slots):
                            mapped["target_index"] = int(shard_slots[local_target - 1].get("_global_target_index", local_target))
                        trace_seq += 1
                        mapped["index"] = trace_seq
                        merged_traces.append(mapped)
                    for item, tr in zip(shard_items, saved_traces_local):
                        local_target = int(tr.get("target_index", 0) or 0)
                        if 1 <= local_target <= len(shard_slots):
                            global_target = int(shard_slots[local_target - 1].get("_global_target_index", local_target))
                        else:
                            global_target = len(ordered_entries) + 1
                        ordered_entries.append({"target_index": global_target, "item": item})
                else:
                    shard_base = len(ordered_entries)
                    for t_idx, tr in enumerate(shard_traces_raw, start=1):
                        mapped = dict(tr)
                        current_target = int(mapped.get("target_index", 0) or t_idx)
                        mapped["target_index"] = shard_base + current_target
                        trace_seq += 1
                        mapped["index"] = trace_seq
                        merged_traces.append(mapped)
                    for item_idx, item in enumerate(shard_items, start=1):
                        ordered_entries.append({"target_index": shard_base + item_idx, "item": item})
                merged_saved += int(payload.get("saved_count", 0) or 0)
                merged_errors.extend([str(x) for x in (payload.get("errors") or []) if str(x).strip()])

            ordered_entries.sort(key=lambda x: int(x.get("target_index", 0) or 0))
            merged_items = [entry.get("item") for entry in ordered_entries if isinstance(entry.get("item"), dict)]
            merged_traces.sort(key=lambda x: (int(x.get("target_index", 0) or 0), int(x.get("index", 0) or 0)))
            for idx, trace in enumerate(merged_traces, start=1):
                trace["index"] = idx

            if len(merged_items) > total:
                merged_items = merged_items[:total]
            _refresh_template_subtasks_from_traces(merged_traces, finalize=False)
            done_payload = {
                "run_id": ",".join(merged_run_ids),
                "items": merged_items,
                "generated_count": len(merged_items),
                "saved_count": min(merged_saved, len(merged_items)),
                "errors": merged_errors,
                "process_trace": merged_traces,
                "material_version_id": str(
                    (template_parallel_context or {}).get("material_version_id", body_with_task.get("material_version_id", ""))
                ),
                "success": len(merged_items) > 0,
                "partial_completed": len(merged_items) > 0 and len(merged_items) < total,
            }
            if is_template_task and isinstance(template_parallel_context, dict):
                planned_slots_all = [slot for slot in (template_parallel_context.get("planned_slots") or []) if isinstance(slot, dict)]
                candidate_lookup = (
                    template_parallel_context.get("candidate_lookup")
                    if isinstance(template_parallel_context.get("candidate_lookup"), dict)
                    else {}
                )
                repair_report = _analyze_template_parallel_result(
                    planned_slots=planned_slots_all,
                    process_trace=merged_traces,
                    candidate_lookup=candidate_lookup,
                )
                ok = bool(repair_report.get("ok"))
                reason = "；".join([str(x).strip() for x in (repair_report.get("issues") or []) if str(x).strip()][:6])
                repair_round_limit = max(1, int(os.getenv("TEMPLATE_REPAIR_MAX_ROUNDS", "6") or 6))
                max_same_signature = max(1, int(os.getenv("TEMPLATE_REPAIR_MAX_SAME_SIGNATURE", "5") or 5))
                repair_round = 0
                repair_signature_attempts: dict[str, int] = {}
                repair_round_records: list[dict[str, Any]] = []
                while not ok and repair_round < repair_round_limit:
                    repair_strategy, repair_strategy_reason = _plan_template_repair_strategy(repair_report)
                    repair_targets: list[int] = []
                    invalid_targets_from_report = sorted(
                        {
                            int(item.get("target_index", 0) or 0)
                            for item in (repair_report.get("invalid_targets") or [])
                            if isinstance(item, dict) and int(item.get("target_index", 0) or 0) > 0
                        }
                    )
                    if repair_strategy == "repair_missing_slots":
                        repair_targets = _sort_template_target_indexes_by_ease(
                            indexes=[
                                *[int(x) for x in (repair_report.get("missing_target_indexes") or []) if int(x) > 0],
                                *invalid_targets_from_report,
                            ],
                            planned_slots=planned_slots_all,
                            candidate_lookup=candidate_lookup,
                        )
                    elif repair_strategy == "retry_invalid_and_missing_slots":
                        repair_targets = _sort_template_target_indexes_by_ease(
                            indexes=[
                                *[int(x) for x in (repair_report.get("missing_target_indexes") or []) if int(x) > 0],
                                *[
                                    int(item.get("target_index", 0) or 0)
                                    for item in (repair_report.get("invalid_targets") or [])
                                    if isinstance(item, dict) and int(item.get("target_index", 0) or 0) > 0
                                ],
                            ],
                            planned_slots=planned_slots_all,
                            candidate_lookup=candidate_lookup,
                        )
                    hard_gap_targets: list[int] = []
                    filtered_targets: list[int] = []
                    for target_idx in (repair_targets or []):
                        if len(
                            _template_slot_candidate_ids(
                                planned_slots=planned_slots_all,
                                target_index=int(target_idx),
                                candidate_lookup=candidate_lookup,
                                include_cross_route_same_mastery=True,
                            )
                        ) <= 0:
                            hard_gap_targets.append(int(target_idx))
                            continue
                        filtered_targets.append(int(target_idx))
                    repair_targets = filtered_targets
                    if hard_gap_targets:
                        gap_msgs = [
                            _describe_template_target_gap(
                                target_index=idx,
                                planned_slots=planned_slots_all,
                                candidate_lookup=candidate_lookup,
                                process_trace=merged_traces,
                            )
                            for idx in hard_gap_targets
                        ]
                        done_payload["errors"] = list(done_payload.get("errors") or []) + [
                            "模板位次不可替代，已跳过当前位次并继续补其他位次: " + " | ".join(gap_msgs[:8])
                        ]
                    if not repair_targets:
                        done_payload["errors"] = list(done_payload.get("errors") or []) + [
                            f"模板修复策略: {repair_strategy}（{repair_strategy_reason or '无'}）"
                        ]
                        break
                    repair_signature = f"{repair_strategy}|{','.join(str(x) for x in repair_targets)}"
                    prev_sig_attempts = int(repair_signature_attempts.get(repair_signature, 0) or 0)
                    if prev_sig_attempts >= max_same_signature:
                        done_payload["errors"] = list(done_payload.get("errors") or []) + [
                            f"模板修复停止: {repair_strategy} 目标 [{','.join(str(x) for x in repair_targets)}] "
                            f"已连续补位 {max_same_signature} 次仍不达标（可在桶内轮换切片，见环境变量 TEMPLATE_REPAIR_MAX_SAME_SIGNATURE）"
                        ]
                        break
                    rotation_step = prev_sig_attempts
                    repair_signature_attempts[repair_signature] = prev_sig_attempts + 1
                    repair_round += 1
                    repair_round_record = {
                        "round": repair_round,
                        "strategy": repair_strategy,
                        "strategy_reason": repair_strategy_reason,
                        "targets": repair_targets,
                        "subtask_count": 0,
                        "generated_count": 0,
                        "saved_count": 0,
                        "error_count": 0,
                        "run_ids": [],
                        "status": "running",
                    }
                    repair_round_records.append(repair_round_record)
                    _update_task_live(
                        tenant_id,
                        task_id,
                        {
                            "repair_rounds": repair_round_records,
                            "current_subcall": {
                                "mode": "template_repair",
                                "repair_round": repair_round,
                                "question_label": "",
                                "target_index": 0,
                                "slice_id": 0,
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            },
                        },
                    )
                    _persist_live_task_snapshot(tenant_id, task_id)
                    repair_slots = [
                        {**dict(planned_slots_all[idx - 1]), "_global_target_index": int(idx)}
                        for idx in repair_targets
                        if 1 <= idx <= len(planned_slots_all)
                    ]
                    supplement_msgs: list[str] = []
                    by_id_lookup = (
                        candidate_lookup.get("by_id")
                        if isinstance(candidate_lookup, dict) and isinstance(candidate_lookup.get("by_id"), dict)
                        else {}
                    )
                    supplemented_slots: list[dict[str, Any]] = []
                    for slot in repair_slots:
                        if not isinstance(slot, dict):
                            continue
                        global_target_index = int(slot.get("_global_target_index", 0) or 0)
                        strict_ids = _template_slot_candidate_ids(
                            planned_slots=planned_slots_all,
                            target_index=global_target_index,
                            candidate_lookup=candidate_lookup,
                            include_cross_route_same_mastery=False,
                        )
                        fallback_ids = _template_slot_candidate_ids(
                            planned_slots=planned_slots_all,
                            target_index=global_target_index,
                            candidate_lookup=candidate_lookup,
                            include_cross_route_same_mastery=True,
                        )
                        if strict_ids or not fallback_ids:
                            supplemented_slots.append(dict(slot))
                            continue
                        failed_sids: set[int] = set()
                        for tr in merged_traces:
                            if not isinstance(tr, dict):
                                continue
                            try:
                                tr_target_index = int(tr.get("target_index", 0) or 0)
                            except (TypeError, ValueError):
                                tr_target_index = 0
                            if tr_target_index != global_target_index:
                                continue
                            try:
                                sid = int(tr.get("slice_id", 0) or 0)
                            except (TypeError, ValueError):
                                sid = 0
                            if sid <= 0:
                                continue
                            if _template_repair_trace_row_has_business_pass(tr):
                                continue
                            failed_sids.add(sid)
                        pool = [sid for sid in fallback_ids if sid not in failed_sids] or list(fallback_ids)
                        pick = int(pool[rotation_step % len(pool)])
                        new_slot = dict(slot)
                        new_slot["slice_id"] = pick
                        meta = by_id_lookup.get(pick) if isinstance(by_id_lookup.get(pick), dict) else {}
                        template_bucket_key = meta.get("template_bucket_key") if isinstance(meta.get("template_bucket_key"), tuple) else ()
                        if len(template_bucket_key) == 2:
                            new_slot["route_prefix"] = str(template_bucket_key[0] or new_slot.get("route_prefix", "")).strip()
                            new_slot["mastery"] = str(template_bucket_key[1] or new_slot.get("mastery", "")).strip()
                        supplemented_slots.append(new_slot)
                        supplement_msgs.append(
                            _describe_template_target_gap(
                                target_index=global_target_index,
                                planned_slots=planned_slots_all,
                                candidate_lookup=candidate_lookup,
                                process_trace=merged_traces,
                            )
                            + f" -> fallback_slice={pick}"
                        )
                    repair_slots = supplemented_slots
                    if supplement_msgs:
                        done_payload["errors"] = list(done_payload.get("errors") or []) + [
                            "模板修复已启用同掌握程度跨路由补位: " + " | ".join(supplement_msgs[:8])
                        ]
                    repair_slots = _rotate_template_repair_slots_for_retry(
                        repair_slots,
                        merged_traces=merged_traces,
                        candidate_lookup=candidate_lookup,
                        rotation_step=rotation_step,
                    )
                    repair_specs = _split_template_slots_for_parallel(repair_slots, min(3, max(1, len(repair_slots))))
                    global_candidate_slice_ids = [
                        int(sid) for sid in ((template_parallel_context or {}).get("candidate_slice_ids") or [])
                        if str(sid).isdigit()
                    ]
                    repair_results: list[dict[str, Any]] = []
                    repair_errors: list[str] = []
                    with ThreadPoolExecutor(max_workers=max(1, len(repair_specs))) as repair_ex:
                        future_map: dict[Any, list[dict[str, Any]]] = {}
                        future_timeouts: dict[Any, int] = {}
                        child_timeouts: list[int] = []
                        for idx, shard_slots in enumerate(repair_specs):
                            shard_payload = dict(body_with_task)
                            shard_payload.pop("task_id", None)
                            shard_payload.pop("template_id", None)
                            cleaned_slots = [
                                {
                                    "slice_id": int(slot.get("slice_id")),
                                    "route_prefix": str(slot.get("route_prefix", "") or "").strip(),
                                    "mastery": str(slot.get("mastery", "") or "").strip(),
                                }
                                for slot in shard_slots
                            ]
                            shard_payload["num_questions"] = len(cleaned_slots)
                            shard_payload["planned_slots"] = cleaned_slots
                            shard_payload["planned_slice_ids"] = [int(slot.get("slice_id")) for slot in cleaned_slots]
                            shard_payload["slice_ids"] = global_candidate_slice_ids
                            shard_payload["task_name"] = f"{str(body_with_task.get('task_name', '')).strip()}#repair{repair_round}-{idx + 1}"
                            future = repair_ex.submit(_invoke_generate_payload, shard_payload)
                            future_map[future] = shard_slots
                            child_q = int(shard_payload.get("num_questions", len(cleaned_slots)) or len(cleaned_slots) or 1)
                            child_max_attempts = int(
                                shard_payload.get("max_attempts", _estimate_internal_subtask_max_attempts(child_q))
                                or _estimate_internal_subtask_max_attempts(child_q)
                            )
                            child_timeout_seconds = _estimate_parallel_child_timeout_seconds(
                                question_count=child_q,
                                max_attempts=child_max_attempts,
                            )
                            child_timeouts.append(int(child_timeout_seconds))
                            future_timeouts[future] = int(child_timeout_seconds)
                        repair_batch_timeout_seconds = _estimate_parallel_batch_timeout_seconds(
                            child_timeouts=child_timeouts,
                            subtask_count=len(repair_specs),
                        )
                        try:
                            for fut in as_completed(list(future_map.keys()), timeout=float(repair_batch_timeout_seconds)):
                                repair_payload: dict[str, Any] = {}
                                try:
                                    resp = fut.result()
                                except Exception as fut_err:
                                    repair_results.append(
                                        {
                                            "slots": future_map[fut],
                                            "status_code": 500,
                                            "payload": {},
                                        }
                                    )
                                    repair_errors.append(f"模板修复子任务异常: {fut_err}")
                                    continue
                                try:
                                    repair_payload = resp.get_json(silent=True) or {}
                                except Exception:
                                    repair_payload = {}
                                repair_results.append(
                                    {
                                        "slots": future_map[fut],
                                        "status_code": int(getattr(resp, "status_code", 200) or 200),
                                        "payload": repair_payload,
                                    }
                                )
                        except FuturesTimeoutError:
                            for fut, shard_slots in future_map.items():
                                if fut.done():
                                    continue
                                fut.cancel()
                                timeout_seconds = int(
                                    future_timeouts.get(fut, repair_batch_timeout_seconds) or repair_batch_timeout_seconds
                                )
                                repair_results.append(
                                    {
                                        "slots": shard_slots,
                                        "status_code": 504,
                                        "payload": {
                                            "errors": [f"模板修复子任务超时({timeout_seconds}s)"],
                                        },
                                    }
                                )
                    before_saved_count = sum(
                        1 for x in merged_traces
                        if isinstance(x, dict) and bool(x.get("saved")) and isinstance(x.get("final_json"), dict)
                    )
                    discard_targets = set(repair_targets)
                    normalized_existing_traces: list[dict[str, Any]] = []
                    for tr in merged_traces:
                        mapped = dict(tr)
                        target_idx = int(mapped.get("target_index", 0) or 0)
                        if bool(mapped.get("saved")) and target_idx in discard_targets:
                            mapped["saved"] = False
                            mapped["template_repair_discarded"] = True
                        normalized_existing_traces.append(mapped)
                    appended_repair_traces: list[dict[str, Any]] = []
                    next_trace_index = len(normalized_existing_traces)
                    repair_run_ids: list[str] = []
                    for row in repair_results:
                        status_code = int(row.get("status_code", 200) or 200)
                        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                        shard_slots = [slot for slot in (row.get("slots") or []) if isinstance(slot, dict)]
                        allowed_target_indexes = {
                            int(slot.get("_global_target_index", 0) or 0)
                            for slot in shard_slots
                            if int(slot.get("_global_target_index", 0) or 0) > 0
                        }
                        if status_code >= 400:
                            msg = str(((payload.get("error") if isinstance(payload, dict) else {}) or {}).get("message", "")).strip()
                            repair_errors.append(msg or f"模板修复子任务失败({status_code})")
                            continue
                        rid = str(payload.get("run_id", "") or "").strip()
                        if rid:
                            repair_run_ids.append(rid)
                            repair_round_record["run_ids"] = list(repair_round_record.get("run_ids") or []) + [rid]
                        repair_round_record["subtask_count"] = int(repair_round_record.get("subtask_count", 0) or 0) + 1
                        repair_round_record["error_count"] = int(repair_round_record.get("error_count", 0) or 0) + len(
                            [str(x) for x in (payload.get("errors") or []) if str(x).strip()]
                        )
                        if not [x for x in (payload.get("process_trace") or []) if isinstance(x, dict)]:
                            repair_errors.append(
                                f"模板修复子任务返回缺少 process_trace，忽略本次入账（run_id={rid or '-'}）"
                            )
                        row_generated_count = 0
                        row_saved_targets: set[int] = set()
                        for t_idx, tr in enumerate([x for x in (payload.get("process_trace") or []) if isinstance(x, dict)], start=1):
                            mapped = dict(tr)
                            local_target = int(mapped.get("target_index", 0) or t_idx)
                            if 1 <= local_target <= len(shard_slots):
                                mapped["target_index"] = int(shard_slots[local_target - 1].get("_global_target_index", local_target))
                            target_idx = int(mapped.get("target_index", 0) or 0)
                            if allowed_target_indexes and target_idx not in allowed_target_indexes:
                                repair_errors.append(
                                    f"模板修复结果位次越界：target_index={target_idx} 不在本轮目标位次 {sorted(allowed_target_indexes)}，已丢弃"
                                )
                                continue
                            next_trace_index += 1
                            mapped["index"] = next_trace_index
                            appended_repair_traces.append(mapped)
                            row_generated_count += 1
                            if bool(mapped.get("saved")) and isinstance(mapped.get("final_json"), dict):
                                row_saved_targets.add(int(target_idx))
                        repair_round_record["generated_count"] = int(repair_round_record.get("generated_count", 0) or 0) + int(row_generated_count)
                        repair_round_record["saved_count"] = int(repair_round_record.get("saved_count", 0) or 0) + len(
                            [x for x in row_saved_targets if x in allowed_target_indexes]
                        )
                        repair_errors.extend([str(x) for x in (payload.get("errors") or []) if str(x).strip()])
                    merged_traces = normalized_existing_traces + appended_repair_traces
                    merged_traces.sort(key=lambda x: (int(x.get("target_index", 0) or 0), int(x.get("index", 0) or 0)))
                    for idx, trace in enumerate(merged_traces, start=1):
                        trace["index"] = idx
                    repair_report = _analyze_template_parallel_result(
                        planned_slots=planned_slots_all,
                        process_trace=merged_traces,
                        candidate_lookup=candidate_lookup,
                    )
                    ok = bool(repair_report.get("ok"))
                    reason = "；".join([str(x).strip() for x in (repair_report.get("issues") or []) if str(x).strip()][:6])
                    done_payload["errors"] = list(done_payload.get("errors") or []) + repair_errors
                    if repair_strategy_reason:
                        done_payload["errors"].append(
                            f"模板修复策略(第{repair_round}轮): {repair_strategy}（{repair_strategy_reason}）"
                        )
                    if repair_run_ids:
                        merged_run_ids.extend(repair_run_ids)
                    valid_saved_traces = _collect_unique_saved_template_traces(
                        planned_slots=planned_slots_all,
                        process_trace=merged_traces,
                    )
                    valid_saved_traces.sort(key=lambda x: int(x.get("target_index", 0) or 0))
                    merged_items = [dict(x.get("final_json")) for x in valid_saved_traces]
                    done_payload["items"] = merged_items
                    done_payload["generated_count"] = len(merged_items)
                    done_payload["saved_count"] = len(merged_items)
                    done_payload["process_trace"] = merged_traces
                    done_payload["run_id"] = ",".join([x for x in merged_run_ids if str(x).strip()])
                    done_payload["partial_completed"] = len(merged_items) > 0 and len(merged_items) < total
                    _refresh_template_subtasks_from_traces(merged_traces, finalize=False)
                    after_saved_count = len(valid_saved_traces)
                    if not ok and after_saved_count <= before_saved_count:
                        repair_round_record["status"] = "partial"
                        done_payload["errors"] = list(done_payload.get("errors") or []) + [
                            f"模板修复停止: 第 {repair_round} 轮未新增成功题，当前通过 {after_saved_count}/{len(planned_slots_all)}"
                        ]
                        break
                    repair_round_record["status"] = "completed" if ok else "partial"
                    _update_task_live(
                        tenant_id,
                        task_id,
                        {
                            "repair_rounds": repair_round_records,
                            "subtasks": live_subtasks,
                            "current_subcall": {
                                "mode": "template_repair",
                                "repair_round": repair_round,
                                "question_label": "",
                                "target_index": 0,
                                "slice_id": 0,
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            },
                        },
                    )
                    _persist_live_task_snapshot(tenant_id, task_id)
                _refresh_template_subtasks_from_traces(merged_traces, finalize=True)
                _update_task_live(
                    tenant_id,
                    task_id,
                    {
                        "subtasks": live_subtasks,
                        "current_subcall": {
                            "mode": "parallel_shards",
                            "question_label": "",
                            "target_index": 0,
                            "slice_id": 0,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    },
                )
                _persist_live_task_snapshot(tenant_id, task_id)
                if not ok:
                    done_payload["errors"] = list(done_payload.get("errors") or []) + [f"模板整体校验失败: {reason}"]
                    done_payload["success"] = False
                selection_stats = _reconcile_template_bank_formal_selection(
                    tenant_id=tenant_id,
                    parent_task_name=str(body_with_task.get("task_name", "") or "").split("#", 1)[0].strip(),
                    planned_slots=planned_slots_all,
                    process_trace=merged_traces,
                )
                done_payload["template_selection"] = selection_stats
                done_payload["backup_count"] = int(selection_stats.get("backup_count", 0) or 0)

        if resume_inplace and isinstance(done_payload, dict):
            newly_items = [x for x in (done_payload.get("items") or []) if isinstance(x, dict)]
            newly_traces = [x for x in (done_payload.get("process_trace") or []) if isinstance(x, dict)]
            resume_target_index_map: dict[int, int] = {}
            for local_idx, slot in enumerate((body_with_task.get("planned_slots") or []), start=1):
                if not isinstance(slot, dict):
                    continue
                gti = int(slot.get("_global_target_index", 0) or 0)
                if gti > 0:
                    resume_target_index_map[int(local_idx)] = int(gti)
            resume_planned_global_targets = {
                int(slot.get("_global_target_index", 0) or 0)
                for slot in (body_with_task.get("planned_slots") or [])
                if isinstance(slot, dict) and int(slot.get("_global_target_index", 0) or 0) > 0
            }
            trace_offset = len(seed_trace)
            remapped_traces: list[dict[str, Any]] = []
            for idx, row in enumerate(newly_traces, start=1):
                mapped = dict(row)
                current_idx = int(mapped.get("index", 0) or idx)
                current_target = int(mapped.get("target_index", 0) or current_idx)
                mapped["index"] = trace_offset + current_idx
                mapped_target = int(resume_target_index_map.get(current_target, 0) or 0)
                if mapped_target > 0:
                    mapped["target_index"] = mapped_target
                elif current_target in resume_planned_global_targets:
                    mapped["target_index"] = current_target
                else:
                    mapped["target_index"] = seed_generated_count + current_target
                remapped_traces.append(mapped)
            merged_trace = list(seed_trace) + remapped_traces
            remapped_items: list[dict[str, Any]] = []
            for item in newly_items:
                mapped_item = dict(item)
                try:
                    raw_target = int(mapped_item.get("模板目标位次", 0) or 0)
                except (TypeError, ValueError):
                    raw_target = 0
                mapped_target = int(resume_target_index_map.get(raw_target, 0) or 0)
                if mapped_target > 0:
                    mapped_item["模板目标位次"] = mapped_target
                remapped_items.append(mapped_item)
            merged_items = list(seed_items) + remapped_items
            merged_errors = list(seed_errors) + [str(x) for x in (done_payload.get("errors") or []) if str(x).strip()]
            # 清理历史续跑遗留的“未补齐”提示，避免后续增量补位成功后仍挂旧错误。
            merged_errors = [
                err for err in merged_errors
                if not str(err).startswith("模板续跑未补齐：")
            ]
            merged_generated_count = seed_generated_count + int(done_payload.get("generated_count", 0) or 0)
            merged_saved_count = seed_saved_count + int(done_payload.get("saved_count", 0) or 0)
            hard_failed_count, soft_warning_count = _summarize_trace_fail_levels(merged_trace)
            done_payload = {
                **done_payload,
                "items": merged_items,
                "process_trace": merged_trace,
                "errors": merged_errors,
                "generated_count": merged_generated_count,
                "saved_count": merged_saved_count,
                "hard_failed_count": hard_failed_count,
                "soft_warning_count": soft_warning_count,
                "partial_completed": merged_generated_count > 0 and merged_generated_count < int(total_for_progress),
            }
            if (
                str(body_with_task.get("template_id", "") or "").strip()
                and merged_generated_count < int(total_for_progress)
            ):
                template_msg = (
                    f"模板续跑未补齐：目标 {int(total_for_progress)} 题，当前累计通过 {int(merged_generated_count)} 题"
                )
                if template_msg not in done_payload["errors"]:
                    done_payload["errors"].append(template_msg)
                done_payload["success"] = False
            if str(body_with_task.get("template_id", "") or "").strip() and isinstance(body_with_task.get("planned_slots"), list):
                selection_stats = _reconcile_template_bank_formal_selection(
                    tenant_id=tenant_id,
                    parent_task_name=str(body_with_task.get("task_name", "") or "").split("#", 1)[0].strip(),
                    planned_slots=[slot for slot in (body_with_task.get("planned_slots") or []) if isinstance(slot, dict)],
                    process_trace=merged_trace,
                )
                done_payload["template_selection"] = selection_stats
                done_payload["backup_count"] = int(selection_stats.get("backup_count", 0) or 0)

        if isinstance(done_payload, dict) and _is_task_cancelled(task_id):
            done_payload["cancelled"] = True
            errs = [str(x) for x in (done_payload.get("errors") or []) if str(x).strip()]
            if "用户取消" not in errs:
                errs.append("用户取消")
            done_payload["errors"] = errs

        ended_at = datetime.now(timezone.utc).isoformat()
        if not isinstance(done_payload, dict):
            if resume_inplace:
                merged_errors = list(seed_errors)
                merged_errors.append("任务异常结束，未收到完成事件")
                _update_task_live(
                    tenant_id,
                    task_id,
                    {"status": "failed", "ended_at": ended_at, "errors": merged_errors},
                )
                _persist_live_task_snapshot(tenant_id, task_id)
            else:
                _update_task_live(
                    tenant_id,
                    task_id,
                    {"status": "failed", "ended_at": ended_at, "errors": ["任务异常结束，未收到完成事件"]},
                )
                _persist_live_task_snapshot(tenant_id, task_id)
        else:
            template_incomplete = (
                bool(resume_inplace)
                and bool(str(body_with_task.get("template_id", "") or "").strip())
                and int(done_payload.get("generated_count", 0) or 0) < int(total_for_progress)
            )
            if "success" in done_payload:
                payload_success = bool(done_payload.get("success", False))
            else:
                expected_now = max(1, int(body_with_task.get("num_questions", 0) or 0))
                payload_success = int(done_payload.get("generated_count", 0) or 0) >= expected_now
            # 模板续跑要求“补齐总量才成功”；若仍未补齐则保持 failed 便于继续续跑。
            status = (
                "cancelled" if done_payload.get("cancelled")
                else ("failed" if (template_incomplete or not payload_success) else "completed")
            )
            _update_task_live(
                tenant_id,
                task_id,
                {
                    "status": status,
                    "ended_at": ended_at,
                    "run_id": str(done_payload.get("run_id", "")),
                    "material_version_id": str(done_payload.get("material_version_id", "")),
                    "items": list(done_payload.get("items") or []),
                    "errors": [str(x) for x in (done_payload.get("errors") or [])],
                    "generated_count": int(done_payload.get("generated_count", 0) or 0),
                    "saved_count": int(done_payload.get("saved_count", 0) or 0),
                    "backup_count": int(done_payload.get("backup_count", 0) or 0),
                    "hard_failed_count": int(done_payload.get("hard_failed_count", 0) or 0),
                    "soft_warning_count": int(done_payload.get("soft_warning_count", 0) or 0),
                    "template_selection": done_payload.get("template_selection") if isinstance(done_payload.get("template_selection"), dict) else {},
                    "progress": {
                        "current": (
                            min(int(done_payload.get("generated_count", 0) or 0), int(total_for_progress))
                            if resume_inplace
                            else len(list(done_payload.get("process_trace") or []))
                        ),
                        "total": int(total_for_progress),
                    },
                    "current_subcall": {},
                    "slice_failure_stats": _summarize_slice_failure_stats(
                        [x for x in (done_payload.get("process_trace") or []) if isinstance(x, dict)]
                    ),
                },
                list(done_payload.get("process_trace") or []),
            )
            _persist_live_task_snapshot(tenant_id, task_id)
        task_to_persist: dict[str, Any] | None = None
        should_persist_failed_qa = False
        with GEN_TASK_LOCK:
            task = GEN_TASKS.get(task_id)
            if task:
                task_to_persist = _task_snapshot(task)
                should_persist_failed_qa = str(task.get("status", "")) == "failed"
        if isinstance(task_to_persist, dict):
            if should_persist_failed_qa:
                _persist_failed_task_qa_run(
                    tenant_id,
                    task_to_persist,
                    reason="任务异常结束，未收到完成事件",
                    started_at=started_at,
                    ended_at=ended_at,
                )
            _persist_gen_task(tenant_id, task_to_persist)
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
        _persist_live_task_snapshot(tenant_id, task_id)
        task_to_persist: dict[str, Any] | None = None
        with GEN_TASK_LOCK:
            task = GEN_TASKS.get(task_id)
            if task:
                task_to_persist = _task_snapshot(task)
        if isinstance(task_to_persist, dict):
            _persist_failed_task_qa_run(tenant_id, task_to_persist, reason=str(e), started_at=started_at, ended_at=ended_at)
            _persist_gen_task(tenant_id, task_to_persist)


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
    name_key = (tenant_id, normalized)
    with GEN_TASK_LOCK:
        if name_key in GEN_TASK_NAME_INFLIGHT:
            return _error("BAD_REQUEST", "task_name already exists", 400)
        for task in GEN_TASKS.values():
            if str(task.get("tenant_id", "")) != tenant_id:
                continue
            exist_name = str(task.get("task_name", "") or "").strip()
            if exist_name and exist_name.casefold() == normalized:
                return _error("BAD_REQUEST", "task_name already exists", 400)
        GEN_TASK_NAME_INFLIGHT.add(name_key)
    try:
        for task in _latest_gen_task_rows(tenant_id, allow_full_fallback=True).values():
            if not isinstance(task, dict):
                continue
            exist_name = str(task.get("task_name", "") or "").strip()
            if exist_name and exist_name.casefold() == normalized:
                return _error("BAD_REQUEST", "task_name already exists", 400)
        template_id = str(body.get("template_id", "") or "").strip()
        if template_id:
            template = _get_gen_template(tenant_id, template_id)
            if not isinstance(template, dict):
                return _error("TEMPLATE_NOT_FOUND", "出题模板不存在", 404)
            template_count = int(template.get("question_count", 0) or 0)
            if template_count <= 0:
                return _error("BAD_REQUEST", "模板题量非法", 400)
            requested = int(body.get("num_questions", template_count) or template_count)
            # 始终写入模板题量：否则 body 缺省 num_questions 时 _make_gen_task 会回退为 1，分片/落盘错误。
            body["num_questions"] = template_count
            if requested != template_count:
                body["template_count_adjusted_from"] = requested
            if not str(body.get("template_name", "") or "").strip():
                body["template_name"] = str(template.get("name", "") or "").strip()
        body["task_name"] = task_name
        task = _make_gen_task(tenant_id, system_user, body)
        # Persist immediately so pending/running tasks survive process restarts.
        _persist_gen_task(tenant_id, task)
        t = threading.Thread(
            target=_run_generate_task_worker,
            args=(tenant_id, str(task.get("task_id", "")), body, system_user),
            daemon=True,
        )
        t.start()
        return _json_response({"task": _build_gen_task_summary(task)})
    finally:
        with GEN_TASK_LOCK:
            GEN_TASK_NAME_INFLIGHT.discard(name_key)


@app.post('/api/<tenant_id>/generate/tasks/<task_id>/resume')
def api_generate_task_resume(tenant_id: str, task_id: str):
    """Resume an incomplete generation task in-place (reuse the same task_id/task_name)."""
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限续跑出题任务", 403)

    source_task = _get_latest_gen_task_snapshot(tenant_id, task_id)
    if not isinstance(source_task, dict):
        return _error("TASK_NOT_FOUND", "任务不存在", 404)

    source_status = str(source_task.get("status", "") or "").strip().lower()
    if source_status in {"pending", "running"}:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        tid = str(source_task.get("task_id", "") or "").strip()
        with GEN_TASK_LOCK:
            in_live = tid in GEN_TASKS
        still_active = _is_generate_run_still_active(tenant_id, str(source_task.get("run_id", "") or ""))
        can_force_orphan_fail = (
            not in_live
            and not still_active
            and _is_unowned_running_task_stale(source_task, now_dt, _ORPHAN_GEN_UNOWNED_RUNNING_SECONDS)
        )
        if can_force_orphan_fail:
            patched = dict(source_task)
            errs = [str(x).strip() for x in (patched.get("errors") or []) if str(x).strip()]
            if _ORPHAN_GEN_TASK_MSG not in errs:
                errs.append(_ORPHAN_GEN_TASK_MSG)
            patched["status"] = "failed"
            patched["ended_at"] = str(patched.get("ended_at", "") or now)
            patched["updated_at"] = now
            patched["errors"] = errs
            patched["error_count"] = len(errs)
            _persist_gen_task(tenant_id, patched)
            _persist_failed_task_qa_run(
                tenant_id,
                patched,
                reason=_ORPHAN_GEN_TASK_MSG,
                started_at=str(patched.get("started_at", "") or ""),
                ended_at=str(patched.get("ended_at", "") or now),
            )
            source_task = patched
            source_status = "failed"
        else:
            return _error("TASK_STILL_RUNNING", "原任务仍在执行中，暂不支持续跑", 409)

    body = request.get_json(silent=True) or {}
    force_remaining = body.get("remaining_questions")
    remain_val: int | None = None
    if force_remaining is not None:
        try:
            remain_val = max(1, int(force_remaining))
        except Exception:
            return _error("BAD_REQUEST", "remaining_questions 必须是整数", 400)
    is_template_task = bool(str((source_task.get("request") or {}).get("template_id", "") or "").strip())
    resume_body = _build_resume_task_body_from_source(
        tenant_id,
        source_task,
        force_remaining=remain_val,
        inplace=True,
    )
    if not isinstance(resume_body, dict) and not is_template_task:
        return _error("NO_REMAINING_WORK", "原任务已完成，无剩余题目可续跑", 400)
    if is_template_task:
        if not isinstance(resume_body, dict):
            req = source_task.get("request") if isinstance(source_task.get("request"), dict) else {}
            resume_body = {
                "task_name": str(source_task.get("task_name", "") or req.get("task_name", "") or "").strip(),
                "gen_scope_mode": str(req.get("gen_scope_mode", "custom") or "custom"),
                "num_questions": 0,
                "question_type": str(req.get("question_type", "随机") or "随机"),
                "generation_mode": _normalize_generation_mode(req.get("generation_mode", "随机")),
                "difficulty": str(req.get("difficulty", "随机") or "随机"),
                "template_id": str(req.get("template_id", "") or "").strip(),
                "template_name": str(req.get("template_name", "") or "").strip(),
                "persist_to_bank": bool(req.get("persist_to_bank", req.get("save_to_bank", True))),
                "save_to_bank": bool(req.get("persist_to_bank", req.get("save_to_bank", True))),
                "slice_ids": [int(x) for x in (req.get("slice_ids") or []) if str(x).isdigit()],
                "material_version_id": str(req.get("material_version_id", "") or source_task.get("material_version_id", "") or "").strip(),
                "resume_from_task_id": str(source_task.get("task_id", "") or "").strip(),
                "resume_done_count": int(max(int(source_task.get("generated_count", 0) or 0), int(source_task.get("saved_count", 0) or 0))),
                "resume_total_count": int(req.get("num_questions", 0) or 0),
                "resume_remaining_count": 0,
                "resume_note": "模板续跑：按模板缺口重建剩余位次",
                "resume_inplace": True,
                "resume_original_total": int(req.get("num_questions", 0) or 0),
                "used_slice_counts": {},
            }
        resume_body, plan_err = _rebuild_template_resume_gap_plan(tenant_id, source_task, resume_body)
        if plan_err:
            return _error("TEMPLATE_RESUME_PLAN_INVALID", plan_err, 400)
        if int(resume_body.get("num_questions", 0) or 0) <= 0:
            return _error("NO_REMAINING_WORK", "模板缺口已补齐，无需续跑", 400)
    elif not isinstance(resume_body, dict):
        return _error("NO_REMAINING_WORK", "原任务已完成，无剩余题目可续跑", 400)

    resumed_task = _prepare_inplace_resume_task(tenant_id, source_task)
    with GEN_TASK_LOCK:
        GEN_TASKS[str(resumed_task.get("task_id", ""))] = _task_snapshot(resumed_task)
        _prune_task_cache()
    _persist_gen_task(tenant_id, resumed_task)
    t = threading.Thread(
        target=_run_generate_task_worker,
        args=(tenant_id, str(resumed_task.get("task_id", "")), resume_body, system_user),
        daemon=True,
    )
    t.start()
    return _json_response(
        {
            "task": _build_gen_task_summary(resumed_task),
            "resume_from": {
                "task_id": str(source_task.get("task_id", "") or ""),
                "task_name": str(source_task.get("task_name", "") or ""),
                "status": str(source_task.get("status", "") or ""),
                "generated_count": int(source_task.get("generated_count", 0) or 0),
                "saved_count": int(source_task.get("saved_count", 0) or 0),
                "target_count": int((source_task.get("request") or {}).get("num_questions", 0) or 0),
                "remaining_count": int((resume_body.get("num_questions", 0) or 0)),
            },
        }
    )


@app.post('/api/<tenant_id>/generate/tasks/resume-incomplete')
def api_generate_task_resume_incomplete(tenant_id: str):
    """Batch resume incomplete tasks in-place (especially template tasks)."""
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限批量续跑出题任务", 403)

    body = request.get_json(silent=True) or {}
    template_only = bool(body.get("template_only", True))
    limit = max(1, min(int(body.get("limit", 20) or 20), 100))

    rows: dict[str, dict[str, Any]] = {}
    with GEN_TASK_LOCK:
        for task in GEN_TASKS.values():
            if str(task.get("tenant_id", "")) != tenant_id:
                continue
            tid = str(task.get("task_id", ""))
            if tid:
                rows[tid] = _task_snapshot(task)
    for task in _latest_gen_task_rows(tenant_id, allow_full_fallback=True).values():
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id", ""))
        if tid:
            rows[tid] = task

    active_resume_sources: set[str] = set()
    for t in rows.values():
        if not isinstance(t, dict):
            continue
        status = str(t.get("status", "") or "").strip().lower()
        if status not in {"pending", "running"}:
            continue
        req = t.get("request") if isinstance(t.get("request"), dict) else {}
        src = str(req.get("resume_from_task_id", "") or "").strip()
        if src:
            active_resume_sources.add(src)

    candidates: list[dict[str, Any]] = []
    for task in rows.values():
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id", "") or "").strip()
        if not tid:
            continue
        if tid in active_resume_sources:
            continue
        status = str(task.get("status", "") or "").strip().lower()
        if status in {"pending", "running"}:
            continue
        req = task.get("request") if isinstance(task.get("request"), dict) else {}
        if template_only and not str(req.get("template_id", "") or "").strip():
            continue
        resume_body = _build_resume_task_body_from_source(tenant_id, task, inplace=True)
        if not isinstance(resume_body, dict):
            continue
        if str((task.get("request") or {}).get("template_id", "") or "").strip():
            resume_body, plan_err = _rebuild_template_resume_gap_plan(tenant_id, task, resume_body)
            if plan_err:
                row = dict(task)
                row["_resume_skip_reason"] = plan_err
                row["_resume_body"] = None
                candidates.append(row)
                continue
            if int(resume_body.get("num_questions", 0) or 0) <= 0:
                continue
        row = dict(task)
        row["_resume_body"] = resume_body
        candidates.append(row)

    candidates.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    selected = candidates[:limit]

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for src in selected:
        resume_body = src.get("_resume_body") if isinstance(src.get("_resume_body"), dict) else None
        if not isinstance(resume_body, dict):
            skipped.append({
                "source_task_id": str(src.get("task_id", "") or ""),
                "reason": str(src.get("_resume_skip_reason", "") or "无可续跑剩余题目"),
            })
            continue
        resumed_task = _prepare_inplace_resume_task(tenant_id, src)
        with GEN_TASK_LOCK:
            GEN_TASKS[str(resumed_task.get("task_id", ""))] = _task_snapshot(resumed_task)
            _prune_task_cache()
        _persist_gen_task(tenant_id, resumed_task)
        t = threading.Thread(
            target=_run_generate_task_worker,
            args=(tenant_id, str(resumed_task.get("task_id", "")), resume_body, system_user),
            daemon=True,
        )
        t.start()
        created.append(
            {
                "source_task_id": str(src.get("task_id", "") or ""),
                "source_task_name": str(src.get("task_name", "") or ""),
                "source_status": str(src.get("status", "") or ""),
                "task": _build_gen_task_summary(resumed_task),
                "remaining_count": int(resume_body.get("num_questions", 0) or 0),
            }
        )

    return _json_response(
        {
            "created_count": len(created),
            "skipped_count": len(skipped),
            "created": created,
            "skipped": skipped,
            "template_only": template_only,
            "limit": limit,
        }
    )


@app.get('/api/<tenant_id>/generate/tasks')
def api_generate_task_list(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限查看出题任务", 403)
    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    include_legacy = str(request.args.get("include_legacy", "0") or "").strip().lower() in {"1", "true", "yes"}
    enable_bank_recovery = str(request.args.get("with_recovery", "0") or "").strip().lower() in {"1", "true", "yes"}
    enable_subtask_diagnostics = str(request.args.get("with_subtask_diagnostics", "0") or "").strip().lower() in {"1", "true", "yes"}
    rows: dict[str, dict[str, Any]] = {}
    with GEN_TASK_LOCK:
        for task in GEN_TASKS.values():
            if str(task.get("tenant_id", "")) != tenant_id:
                continue
            tid = str(task.get("task_id", ""))
            if tid:
                rows[tid] = _task_snapshot(task)
    # List pages must stay on the summary path. Do not synchronously refresh or
    # fully scan historical gen_tasks.jsonl here; paging only helps if it is
    # applied before the expensive read, not after loading the whole audit file.
    persisted_stop_after = max(limit * 4, 200)
    for task in _latest_gen_task_rows(
        tenant_id,
        refresh_summary=False,
        prefer_compact_summary=True,
        stop_after=persisted_stop_after,
    ).values():
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id", ""))
        # Persisted task file is append-only; keep latest snapshot for same task_id.
        if tid:
            rows[tid] = task
    if include_legacy:
        # Backfill legacy/history runs that were persisted in qa_runs but never had
        # a corresponding gen_tasks row (e.g. old sync generation path).
        existing_run_ids = {
            str(x.get("run_id", "")).strip()
            for x in rows.values()
            if isinstance(x, dict) and str(x.get("run_id", "")).strip()
        }
        for run in _read_jsonl(_qa_runs_path(tenant_id)):
            if not isinstance(run, dict):
                continue
            rid = str(run.get("run_id", "")).strip()
            if not rid or rid in existing_run_ids:
                continue
            cfg = run.get("config") if isinstance(run.get("config"), dict) else {}
            bm = run.get("batch_metrics") if isinstance(run.get("batch_metrics"), dict) else {}
            started_at = str(run.get("started_at", "") or "")
            ended_at = str(run.get("ended_at", "") or "")
            generated_count = int(bm.get("generated_count", 0) or 0)
            saved_count = int(bm.get("saved_count", 0) or 0)
            error_count = int(bm.get("error_count", 0) or 0)
            status = "failed" if (error_count > 0 and generated_count <= 0) else "completed"
            total_q = int(bm.get("question_count", 0) or 0)
            progress_current = generated_count + error_count
            task_id = str(cfg.get("task_id", "") or "").strip() or f"legacy_{rid}"
            if task_id in rows:
                snap = dict(rows[task_id])
                if not str(snap.get("run_id", "")).strip():
                    snap["run_id"] = rid
                snap["material_version_id"] = str(snap.get("material_version_id", "") or run.get("material_version_id", ""))
                snap["generated_count"] = int(snap.get("generated_count", generated_count) or generated_count)
                snap["saved_count"] = int(snap.get("saved_count", saved_count) or saved_count)
                snap["error_count"] = int(snap.get("error_count", error_count) or error_count)
                rows[task_id] = snap
                existing_run_ids.add(rid)
                continue
            rows[task_id] = {
                "task_id": task_id,
                "tenant_id": tenant_id,
                "task_name": str(cfg.get("task_name", "") or run.get("task_name", "") or ""),
                "created_at": started_at,
                "updated_at": ended_at or started_at,
                "started_at": started_at,
                "ended_at": ended_at,
                "status": status,
                "request": {
                    "num_questions": int(cfg.get("num_questions", 0) or 0),
                    "question_type": str(cfg.get("question_type", "") or ""),
                    "generation_mode": str(cfg.get("generation_mode", "") or ""),
                    "difficulty": str(cfg.get("difficulty", "") or ""),
                },
                "run_id": rid,
                "material_version_id": str(run.get("material_version_id", "") or ""),
                "generated_count": generated_count,
                "saved_count": saved_count,
                "error_count": error_count,
                "progress": {"current": int(max(progress_current, 0)), "total": int(max(total_q, progress_current, 0))},
                "current_node": "",
                "current_node_updated_at": "",
            }
            existing_run_ids.add(rid)
    if enable_bank_recovery:
        bank_task_stats = _build_bank_task_recovery_stats(tenant_id)
        if bank_task_stats:
            rows = {tid: _apply_gen_task_bank_recovery(task, bank_task_stats) for tid, task in rows.items()}
    items = [task for task in rows.values() if not _is_internal_child_gen_task(task)]
    items.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    items = items[:limit]
    enriched_items: list[dict[str, Any]] = []
    for task in items:
        row = dict(task) if isinstance(task, dict) else {}
        if enable_subtask_diagnostics:
            status = str(row.get("status", "") or "").strip().lower()
            is_terminal = status in {"completed", "failed", "cancelled", "canceled"}
            is_parent = not str(row.get("parent_task_id", "") or "").strip()
            has_subtasks = bool([x for x in (row.get("subtasks") or []) if isinstance(x, dict)])
            if is_terminal and is_parent and not has_subtasks and str(row.get("task_name", "") or "").strip():
                diagnostics = _build_task_related_run_diagnostics(tenant_id, row)
                diag_subtasks = diagnostics.get("subtasks") if isinstance(diagnostics.get("subtasks"), list) else []
                if diag_subtasks:
                    row["subtasks"] = [x for x in diag_subtasks if isinstance(x, dict)]
        enriched_items.append(row)
    return _json_response({"items": [_build_gen_task_summary(x) for x in enriched_items], "total": len(items)})


@app.post('/api/<tenant_id>/generate/tasks/<task_id>/cancel')
def api_generate_task_cancel(tenant_id: str, task_id: str):
    """Request cancellation of a running or pending task. Running task will stop after current question."""
    try:
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限取消出题任务", 403)
    tid = str(task_id or "").strip()
    if not tid:
        return _error("BAD_REQUEST", "task_id is required", 400)
    with GEN_TASK_LOCK:
        task = GEN_TASKS.get(tid)
        if not task or str(task.get("tenant_id", "")) != tenant_id:
            task = _read_persisted_task(tenant_id, tid)
        if not isinstance(task, dict):
            return _error("TASK_NOT_FOUND", "任务不存在", 404)
        status = str(task.get("status", "") or "")
        if status not in ("pending", "running"):
            return _json_response({
                "ok": True,
                "task_id": tid,
                "status": status,
                "message": "任务已结束，无需取消",
            })
        if tid not in GEN_TASKS or str(GEN_TASKS[tid].get("tenant_id", "")) != tenant_id:
            return _json_response({
                "ok": False,
                "task_id": tid,
                "status": status,
                "message": "任务不在当前进程中，无法取消（可能服务已重启）",
            })
        GEN_TASKS[tid]["cancel_requested"] = True
    return _json_response({"ok": True, "task_id": tid, "status": "cancel_requested", "message": "已请求取消，任务将尽快停止"})


@app.get('/api/<tenant_id>/generate/tasks/<task_id>')
def api_generate_task_detail(tenant_id: str, task_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限查看出题任务详情", 403)
    tid = str(task_id or "").strip()
    legacy_prefix = "legacy_"
    allow_legacy_detail = str(request.args.get("include_legacy", "0") or "").strip().lower() in {"1", "true", "yes"}
    enable_bank_recovery = str(request.args.get("with_recovery", "0") or "").strip().lower() in {"1", "true", "yes"}
    enable_template_reconcile = str(request.args.get("with_reconcile", "0") or "").strip().lower() in {"1", "true", "yes"}
    if tid.startswith(legacy_prefix) and not allow_legacy_detail:
        return _error("TASK_NOT_FOUND", "任务不存在", 404)
    latest_row = _latest_gen_task_rows(tenant_id, allow_full_fallback=True).get(tid)
    if isinstance(latest_row, dict) and str(latest_row.get("tenant_id", "") or "").strip() != tenant_id:
        latest_row = None
    live_file_task = _read_gen_task_snapshot_file(tenant_id, task_id)
    if isinstance(live_file_task, dict):
        out = _pick_newer_terminal_task_snapshot(live_file_task, latest_row) or dict(live_file_task)
        file_status = str(out.get("status", "") or "").strip().lower()
        bank_task_stats = {} if (file_status in {"pending", "running"} or not enable_bank_recovery) else _build_bank_task_recovery_stats(tenant_id)
        if bank_task_stats:
            out = _apply_gen_task_bank_recovery(out, bank_task_stats)
        if enable_template_reconcile and file_status not in {"pending", "running"}:
            out = _maybe_reconcile_template_task_selection(tenant_id, out)
        out["errors"] = _sanitize_task_errors(out.get("errors"))
        out = _hydrate_task_detail_from_run(tenant_id, out)
        _enrich_task_with_qa_run(tenant_id, out)
        return _json_response({"task": out})
    with GEN_TASK_LOCK:
        task = GEN_TASKS.get(task_id)
        if task and str(task.get("tenant_id", "")) == tenant_id:
            mem_status = str(task.get("status", "") or "").strip().lower()
            if mem_status in {"pending", "running"}:
                snap = _pick_newer_terminal_task_snapshot(_task_snapshot(task), latest_row) or _task_snapshot(task)
                live_status = str(snap.get("status", "") or "").strip().lower()
                bank_task_stats = {} if (live_status in {"pending", "running"} or not enable_bank_recovery) else _build_bank_task_recovery_stats(tenant_id)
                if bank_task_stats:
                    snap = _apply_gen_task_bank_recovery(snap, bank_task_stats)
                if enable_template_reconcile and live_status not in {"pending", "running"}:
                    snap = _maybe_reconcile_template_task_selection(tenant_id, snap)
                snap["errors"] = _sanitize_task_errors(snap.get("errors"))
                snap = _hydrate_task_detail_from_run(tenant_id, snap)
                _enrich_task_with_qa_run(tenant_id, snap)
                return _json_response({"task": snap})
    persisted = _read_persisted_task(tenant_id, task_id)
    if isinstance(persisted, dict):
        out = dict(persisted)
        persisted_status = str(out.get("status", "") or "").strip().lower()
        bank_task_stats = {} if (persisted_status in {"pending", "running"} or not enable_bank_recovery) else _build_bank_task_recovery_stats(tenant_id)
        if bank_task_stats:
            out = _apply_gen_task_bank_recovery(out, bank_task_stats)
        if enable_template_reconcile and persisted_status not in {"pending", "running"}:
            out = _maybe_reconcile_template_task_selection(tenant_id, out)
        out["errors"] = _sanitize_task_errors(out.get("errors"))
        out = _hydrate_task_detail_from_run(tenant_id, out)
        _enrich_task_with_qa_run(tenant_id, out)
        return _json_response({"task": out})
    if tid.startswith(legacy_prefix):
        run_id = tid[len(legacy_prefix):].strip()
        run = _get_qa_run_by_id(tenant_id, run_id)
        if isinstance(run, dict):
            cfg = run.get("config") if isinstance(run.get("config"), dict) else {}
            bm = run.get("batch_metrics") if isinstance(run.get("batch_metrics"), dict) else {}
            started_at = str(run.get("started_at", "") or "")
            ended_at = str(run.get("ended_at", "") or "")
            generated_count = int(bm.get("generated_count", 0) or 0)
            saved_count = int(bm.get("saved_count", 0) or 0)
            error_count = int(bm.get("error_count", 0) or 0)
            if ended_at:
                status = "failed" if (error_count > 0 and generated_count <= 0) else "completed"
            else:
                status = "running"
            total_q = int(bm.get("question_count", 0) or cfg.get("num_questions", 0) or 0)
            progress_current = generated_count + max(error_count, 0)
            if total_q <= 0:
                total_q = max(progress_current, generated_count, saved_count, 1)
            legacy_task = {
                "task_id": tid,
                "tenant_id": tenant_id,
                "task_name": str(cfg.get("task_name", "") or run.get("task_name", "") or tid),
                "creator": str(cfg.get("system_user", "") or "admin"),
                "created_at": started_at,
                "updated_at": ended_at or started_at,
                "started_at": started_at,
                "ended_at": ended_at,
                "status": status,
                "request": {
                    "num_questions": int(cfg.get("num_questions", 0) or total_q),
                    "question_type": str(cfg.get("question_type", "") or ""),
                    "generation_mode": str(cfg.get("generation_mode", "") or ""),
                    "difficulty": str(cfg.get("difficulty", "") or ""),
                    "template_id": str(cfg.get("template_id", "") or ""),
                    "template_name": str(cfg.get("template_name", "") or ""),
                    "material_version_id": str(run.get("material_version_id", "") or ""),
                },
                "run_id": run_id,
                "material_version_id": str(run.get("material_version_id", "") or ""),
                "process_trace": [],
                "items": [],
                "errors": _sanitize_task_errors(run.get("errors")),
                "generated_count": generated_count,
                "saved_count": saved_count,
                "error_count": error_count,
                "progress": {"current": int(max(progress_current, 0)), "total": int(max(total_q, 1))},
                "current_node": "",
                "current_node_updated_at": "",
                "current_subcall": {},
                "subtasks": [],
                "repair_rounds": [],
                "slice_failure_stats": [],
            }
            legacy_task = _hydrate_task_detail_from_run(tenant_id, legacy_task)
            _enrich_task_with_qa_run(tenant_id, legacy_task)
            return _json_response({"task": legacy_task})
    return _error("TASK_NOT_FOUND", "任务不存在", 404)


@app.post('/api/<tenant_id>/generate/tasks/<task_id>/bank-policy')
def api_generate_task_bank_policy(tenant_id: str, task_id: str):
    """
    动态切换任务入库策略，并立即同步当前已通过题：
    - enabled=true: 将当前已通过题补入题库（去重）
    - enabled=false: 将该任务作用域（含子任务）已入库题从题库移除
    """
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限修改任务入库策略", 403)
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        return _error("BAD_REQUEST", "enabled is required", 400)
    enabled = bool(body.get("enabled"))
    tid = str(task_id or "").strip()
    if not tid:
        return _error("BAD_REQUEST", "task_id is required", 400)
    source = _resolve_task_snapshot_for_policy(tenant_id, tid)
    if not isinstance(source, dict):
        return _error("TASK_NOT_FOUND", "任务不存在", 404)
    hydrated = _hydrate_task_detail_from_run(tenant_id, dict(source))
    patched = dict(source)
    req = patched.get("request") if isinstance(patched.get("request"), dict) else {}
    req["persist_to_bank"] = bool(enabled)
    req["save_to_bank"] = bool(enabled)
    patched["request"] = req
    now = datetime.now(timezone.utc).isoformat()
    patched["updated_at"] = now
    added, removed, scope_saved_count = _sync_task_bank_policy(tenant_id, hydrated, enabled)
    patched["saved_count"] = int(scope_saved_count)
    progress = patched.get("progress") if isinstance(patched.get("progress"), dict) else {}
    current = int(progress.get("current", 0) or 0)
    total = int(progress.get("total", 0) or 0)
    patched["progress"] = {"current": current, "total": total}
    with GEN_TASK_LOCK:
        live = GEN_TASKS.get(tid)
        if isinstance(live, dict) and str(live.get("tenant_id", "")) == tenant_id:
            live_req = live.get("request") if isinstance(live.get("request"), dict) else {}
            live_req["persist_to_bank"] = bool(enabled)
            live_req["save_to_bank"] = bool(enabled)
            live["request"] = live_req
            live["saved_count"] = int(scope_saved_count)
            live["updated_at"] = now
            GEN_TASKS[tid] = live
    _persist_gen_task(tenant_id, patched)
    _persist_gen_task_snapshot_file(tenant_id, patched)
    write_audit_log(
        tenant_id,
        system_user,
        "gen.task.bank_policy.update",
        "question_generation_task",
        tid,
        after={
            "enabled": bool(enabled),
            "added": int(added),
            "removed": int(removed),
            "saved_count": int(scope_saved_count),
        },
    )
    return _json_response(
        {
            "ok": True,
            "task_id": tid,
            "enabled": bool(enabled),
            "added": int(added),
            "removed": int(removed),
            "saved_count": int(scope_saved_count),
        }
    )


def _get_task_id_by_run_id(tenant_id: str, run_id: str) -> str:
    """Return task_id from gen_tasks for the given run_id (completed task only). Empty if not found."""
    tid, _ = _get_task_id_and_name_by_run_id(tenant_id, run_id)
    return tid


def _get_task_id_and_name_by_run_id(tenant_id: str, run_id: str) -> tuple[str, str]:
    """Return (task_id, task_name) from gen_tasks for the given run_id (completed task only). Empty strings if not found."""
    rid = str(run_id or "").strip()
    if not rid or rid.startswith("run_fail_"):
        return "", ""
    rows = list(_latest_gen_task_rows(tenant_id, allow_full_fallback=True).values())
    rows.sort(key=lambda x: str(x.get("updated_at", "") or x.get("created_at", "")))
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("run_id", "") or "").strip() != rid:
            continue
        if str(row.get("status", "") or "").strip().lower() != "completed":
            continue
        tid = str(row.get("task_id", "") or "").strip()
        tname = str(row.get("task_name", "") or "").strip()
        return tid, tname
    return "", ""


def _filter_qa_runs(
    tenant_id: str,
    *,
    material_version_id: str = "",
    days: int = 0,
    success_only: bool = False,
    target_count: int = 0,
) -> list[dict[str, Any]]:
    """Filter QA runs. When success_only=True, exclude run_fail_* (failed-task placeholder runs)."""
    now_ts = datetime.now(timezone.utc).timestamp()
    def _predicate(run: dict[str, Any]) -> bool:
        run_id = str(run.get("run_id", "") or "")
        if success_only and run_id.startswith("run_fail_"):
            return False
        if material_version_id and str(run.get("material_version_id", "")) != material_version_id:
            return False
        if days > 0:
            ended_at = str(run.get("ended_at", "") or "")
            try:
                ts = datetime.fromisoformat(ended_at.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0.0
            if now_ts - ts > days * 24 * 3600:
                return False
        return True

    runs, _ = _collect_recent_jsonl_rows_from_paths(
        _qa_read_paths(tenant_id, "qa_runs.jsonl"),
        target_count=max(1, int(target_count or 200)),
        sort_key=lambda row: str(row.get("ended_at", "") or ""),
        predicate=_predicate,
        unique_key=lambda row: str(row.get("run_id", "") or ""),
    )
    return runs


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
        ("cpvq", "lower_better"),
        ("error_call_rate", "lower_better"),
    ]
    rows: list[dict[str, Any]] = []
    win = 0
    lose = 0
    for key, direct in metrics:
        raw_b = bm_base.get(key)
        raw_t = bm_target.get(key)
        # cpvq can be None when saved_count=0; treat as 0 for drift comparison
        b = float(raw_b if raw_b is not None else 0)
        t = float(raw_t if raw_t is not None else 0)
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
        "base_run_ids": [str(base.get("run_id", ""))] if str(base.get("run_id", "")) else [],
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


def _build_virtual_baseline_run(runs: list[dict[str, Any]], base_run_ids: list[str]) -> dict[str, Any]:
    """Aggregate multiple runs into one virtual baseline using arithmetic mean per metric."""
    metrics = [
        "hard_pass_rate",
        "quality_score_avg",
        "risk_high_rate",
        "logic_pass_rate",
        "duplicate_rate",
        "knowledge_match_rate",
        "avg_tokens_per_question",
        "avg_latency_ms_per_question",
        "avg_cost_per_question",
        "cpvq",
        "error_call_rate",
    ]
    values: dict[str, list[float]] = {k: [] for k in metrics}
    for run in runs:
        bm = run.get("batch_metrics") if isinstance(run.get("batch_metrics"), dict) else {}
        for k in metrics:
            raw = bm.get(k)
            if raw is None:
                continue
            try:
                values[k].append(float(raw))
            except (TypeError, ValueError):
                continue
    bm_virtual: dict[str, Any] = {}
    for k in metrics:
        if values[k]:
            bm_virtual[k] = round(sum(values[k]) / len(values[k]), 6)
        else:
            bm_virtual[k] = 0.0
    return {
        "run_id": "__multi_baseline__",
        "batch_metrics": bm_virtual,
        "base_run_ids": list(base_run_ids),
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


def _build_slice_success_stats_from_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Aggregate per-slice generation success across filtered runs.
    Success criterion: question saved into bank (saved == True), i.e. critic最终通过并落库。
    """
    agg: dict[tuple[int, str], dict[str, Any]] = {}
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        questions = run.get("questions") if isinstance(run.get("questions"), list) else []
        for q in questions:
            if not isinstance(q, dict):
                continue
            try:
                sid = int(q.get("slice_id", -1))
            except (TypeError, ValueError):
                continue
            if sid < 0:
                continue
            spath = str(q.get("slice_path", "") or "").strip()
            key = (sid, spath)
            row = agg.get(key)
            if row is None:
                row = {
                    "slice_id": sid,
                    "slice_path": spath,
                    "attempt_count": 0,
                    "success_count": 0,
                }
                agg[key] = row
            row["attempt_count"] += 1
            if bool(q.get("saved", False)):
                row["success_count"] += 1
    out: list[dict[str, Any]] = []
    for row in agg.values():
        attempts = int(row.get("attempt_count", 0) or 0)
        success = int(row.get("success_count", 0) or 0)
        row["success_rate"] = round(_safe_div(success, attempts), 4) if attempts > 0 else 0.0
        out.append(row)
    out.sort(
        key=lambda x: (
            -int(x.get("attempt_count", 0) or 0),
            -float(x.get("success_rate", 0.0) or 0.0),
            int(x.get("slice_id", 0) or 0),
        )
    )
    return out

@app.get('/api/<tenant_id>/qa/runs')
def api_qa_runs(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问评估数据", 403)
    page, page_size = _parse_pagination()
    material_version_id = str(request.args.get("material_version_id", "")).strip()
    days = max(0, int(request.args.get("days", 0) or 0))
    success_only = request.args.get("success_only", "1").strip() in ("1", "true", "yes")
    target_count = max(page * page_size + 1, 200)
    runs = _filter_qa_runs(
        tenant_id,
        material_version_id=material_version_id,
        days=days,
        success_only=success_only,
        target_count=target_count,
    )
    # Build run->(task_id, task_name) lookup once to avoid O(runs * tasks) rescans.
    run_task_lookup: dict[str, tuple[str, str]] = {}
    latest_task_rows = list(_latest_gen_task_rows(tenant_id, allow_full_fallback=True).values())
    latest_task_rows.sort(key=lambda x: str(x.get("updated_at", "") or x.get("created_at", "")))
    for row in reversed(latest_task_rows):
        if not isinstance(row, dict):
            continue
        rid = str(row.get("run_id", "") or "").strip()
        if not rid or rid.startswith("run_fail_") or rid in run_task_lookup:
            continue
        if str(row.get("status", "") or "").strip().lower() != "completed":
            continue
        run_task_lookup[rid] = (
            str(row.get("task_id", "") or "").strip(),
            str(row.get("task_name", "") or "").strip(),
        )
    latest_judge_by_run = _load_latest_judge_task_by_run(tenant_id)
    items: list[dict[str, Any]] = []
    for r in runs:
        r, _ = _normalize_run_batch_metrics(r)
        bm = r.get("batch_metrics") if isinstance(r.get("batch_metrics"), dict) else {}
        saved_count = int(bm.get("saved_count", 0) or 0)
        has_judge = (
            bm.get("judge_pass_rate") is not None
            or bm.get("judge_pass_count") is not None
            or any(bool(q.get("offline_judge")) for q in (r.get("questions") or []) if isinstance(q, dict))
        )
        release_eligible = saved_count > 0 and has_judge
        if saved_count <= 0:
            release_eligible_reason = "无落库题目，请先跑出题任务并确保有题目入库"
        elif not has_judge:
            release_eligible_reason = "落库题目未跑离线 Judge，请对该 run 执行「运行 Judge」"
        else:
            release_eligible_reason = ""
        run_id = r.get("run_id", "")
        task_id, task_name = run_task_lookup.get(str(run_id or "").strip(), ("", "")) if run_id else ("", "")
        # Fallback to fields carried in run row (e.g. recovered runs from bank).
        if not str(task_id or "").strip():
            task_id = str(r.get("task_id", "") or "").strip()
        if not str(task_name or "").strip():
            task_name = str(r.get("task_name", "") or "").strip()
        if not str(task_name or "").strip():
            cfg = r.get("config") if isinstance(r.get("config"), dict) else {}
            task_name = str(cfg.get("task_name", "") or "").strip()
        if not str(task_name or "").strip():
            if str(task_id or "").strip():
                task_name = f"未命名任务({task_id})"
            elif str(run_id or "").strip():
                task_name = f"未命名任务({run_id})"
        started_at = str(r.get("started_at", "") or "")
        ended_at = str(r.get("ended_at", "") or "")
        run_duration_sec: float | None = None
        st = _parse_iso_ts(started_at)
        ed = _parse_iso_ts(ended_at)
        if st is not None and ed is not None:
            run_duration_sec = round(max(0.0, (ed - st).total_seconds()), 3)
        judge_meta = latest_judge_by_run.get(run_id) if run_id else None
        judge_job = r.get("judge_job") if isinstance(r.get("judge_job"), dict) else {}
        judge_started_at = str(judge_job.get("started_at", "") or "")
        judge_ended_at = str(judge_job.get("finished_at", "") or judge_job.get("ended_at", "") or "")
        judge_duration_sec: float | None = None
        jst = _parse_iso_ts(judge_started_at)
        jed = _parse_iso_ts(judge_ended_at)
        if jst is not None and jed is not None:
            judge_duration_sec = round(max(0.0, (jed - jst).total_seconds()), 3)
        items.append(
            {
                "run_id": run_id,
                "task_id": task_id,
                "task_name": task_name,
                "material_version_id": r.get("material_version_id", ""),
                "started_at": started_at,
                "ended_at": ended_at,
                "run_duration_sec": run_duration_sec,
                "generated_count": bm.get("generated_count", 0),
                "saved_count": bm.get("saved_count", 0),
                "hard_pass_rate": bm.get("hard_pass_rate", 0),
                "quality_score_avg": bm.get("quality_score_avg", 0),
                "risk_high_rate": bm.get("risk_high_rate", 0),
                "avg_tokens_per_question": bm.get("avg_tokens_per_question", 0),
                "avg_latency_ms_per_question": bm.get("avg_latency_ms_per_question", 0),
                "avg_cost_per_question": bm.get("avg_cost_per_question", 0),
                "total_cost": bm.get("total_cost", 0),
                "cpvq": bm.get("cpvq"),
                "currency": bm.get("currency", "CNY"),
                "error_call_rate": bm.get("error_call_rate", 0),
                "release_eligible": release_eligible,
                "release_eligible_reason": release_eligible_reason,
                "latest_judge_task_id": str((judge_meta or {}).get("task_id", "") or ""),
                "latest_judge_task_name": str((judge_meta or {}).get("task_name", "") or ""),
                "latest_judge_status": str((judge_meta or {}).get("status", "") or str(judge_job.get("status", "") or "")),
                "latest_judge_started_at": str((judge_meta or {}).get("started_at", "") or judge_started_at),
                "latest_judge_ended_at": str((judge_meta or {}).get("ended_at", "") or judge_ended_at),
                "latest_judge_duration_sec": judge_duration_sec,
            }
        )
    total = len(items) if len(runs) < target_count else max(len(items), target_count)
    start = (page - 1) * page_size
    end = start + page_size
    payload = {"items": items[start:end], "total": total, "page": page, "page_size": page_size}
    payload["material_version_id"] = material_version_id
    payload["days"] = days
    payload["success_only"] = success_only
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
    target, hydrated = _hydrate_run_questions_from_task_if_needed(tenant_id, target)
    if hydrated:
        _update_qa_run(tenant_id, run_id, target)
    target, _aggregate_hydrated = _hydrate_judge_run_questions_from_parent_task_if_needed(
        tenant_id,
        target,
        requested_ids_raw=None,
    )
    target, normalized = _normalize_run_batch_metrics(target)
    if normalized:
        _update_qa_run(tenant_id, run_id, target)
    return _json_response(target)


def _prepare_judge_run_targets(
    tenant_id: str,
    run_id: str,
    requested_ids_raw: Any = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], set[str], tuple[str, str, int] | None]:
    run = _get_qa_run_by_id(tenant_id, run_id)
    if not isinstance(run, dict):
        return None, [], set(), ("RUN_NOT_FOUND", "评估运行不存在", 404)
    run, hydrated = _hydrate_run_questions_from_task_if_needed(tenant_id, run)
    if hydrated and not _update_qa_run(tenant_id, run_id, run):
        return None, [], set(), ("UPDATE_FAILED", "同步 run 题目失败", 500)
    run, _aggregate_hydrated = _hydrate_judge_run_questions_from_parent_task_if_needed(
        tenant_id,
        run,
        requested_ids_raw=requested_ids_raw,
    )
    questions = run.get("questions") if isinstance(run.get("questions"), list) else []
    if not questions:
        return None, [], set(), ("BAD_REQUEST", "该 run 无题目，无法运行 Judge", 400)
    all_ids = {str(q.get("question_id", "")).strip() for q in questions if isinstance(q, dict)}
    saved_ids = {
        str(q.get("question_id", "")).strip()
        for q in questions
        if isinstance(q, dict) and bool(q.get("saved", False)) is True and str(q.get("question_id", "")).strip()
    }
    if not saved_ids:
        return None, [], set(), ("BAD_REQUEST", "该 run 无落库题（saved=true），无法运行 Judge", 400)
    requested_ids = requested_ids_raw
    if requested_ids is not None and not isinstance(requested_ids, list):
        requested_ids = [str(requested_ids)] if requested_ids else []
    req_ids = [str(x).strip() for x in (requested_ids or []) if str(x).strip()]
    if req_ids:
        invalid = set(req_ids) - all_ids
        if invalid:
            return None, [], set(), (
                "BAD_REQUEST",
                f"以下 question_id 不在本 run 中: {', '.join(sorted(invalid)[:5])}{'...' if len(invalid) > 5 else ''}",
                400,
            )
        unsaved = set(req_ids) - saved_ids
        if unsaved:
            return None, [], set(), (
                "BAD_REQUEST",
                f"以下 question_id 非落库题（saved=false），不支持运行 Judge: {', '.join(sorted(unsaved)[:5])}{'...' if len(unsaved) > 5 else ''}",
                400,
            )
        ids_to_run = set(req_ids) & saved_ids
    else:
        ids_to_run = set(saved_ids)
    if not ids_to_run:
        return None, [], set(), ("BAD_REQUEST", "无可执行 Judge 的落库题目（saved=true）", 400)
    return run, questions, ids_to_run, None


def _recompute_run_judge_metrics(run: dict[str, Any], questions: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    bm = dict(run.get("batch_metrics") or {})
    question_rows = [q for q in questions if isinstance(q, dict)]
    bm["generated_count"] = int(len(question_rows))
    bm["saved_count"] = int(sum(1 for q in question_rows if bool(q.get("saved", False)) is True))
    judge_pass_cnt = sum(1 for q in questions if str((q.get("offline_judge") or {}).get("decision", "")).lower() == "pass")
    judge_review_cnt = sum(1 for q in questions if str((q.get("offline_judge") or {}).get("decision", "")).lower() == "review")
    judge_reject_cnt = sum(1 for q in questions if str((q.get("offline_judge") or {}).get("decision", "")).lower() == "reject")
    judge_with_result = judge_pass_cnt + judge_review_cnt + judge_reject_cnt
    judge_calls_sum = 0
    judge_failed_calls_sum = 0
    judge_prompt_tokens_sum = 0
    judge_completion_tokens_sum = 0
    judge_total_tokens_sum = 0
    judge_latency_ms_sum = 0
    judge_cost_usd_sum = 0.0
    for q in questions:
        oj = q.get("offline_judge") if isinstance(q.get("offline_judge"), dict) else {}
        obs = oj.get("observability") if isinstance(oj.get("observability"), dict) else {}
        tok = obs.get("tokens") if isinstance(obs.get("tokens"), dict) else {}
        costs = oj.get("costs") if isinstance(oj.get("costs"), dict) else {}
        judge_calls_sum += int(obs.get("llm_calls", 0) or 0)
        judge_failed_calls_sum += int(obs.get("failed_calls", 0) or 0)
        judge_prompt_tokens_sum += int(tok.get("prompt_tokens", 0) or 0)
        judge_completion_tokens_sum += int(tok.get("completion_tokens", 0) or 0)
        judge_total_tokens_sum += int(tok.get("total_tokens", 0) or 0)
        judge_latency_ms_sum += int(obs.get("latency_ms", 0) or 0)
        judge_cost_usd_sum += float(costs.get("per_question_usd", 0.0) or 0.0)
    judge_overall_scores = [
        float((q.get("offline_judge") or {}).get("overall_score", 0) or 0)
        for q in questions
        if (q.get("offline_judge") or {}).get("overall_score") is not None
    ]
    judge_baseline_scores_run = [
        float(
            (q.get("offline_judge") or {}).get(
                "baseline_score",
                (q.get("offline_judge") or {}).get("penalty_score"),
            )
            or 0
        )
        for q in questions
        if (
            (q.get("offline_judge") or {}).get("baseline_score") is not None
            or (q.get("offline_judge") or {}).get("penalty_score") is not None
        )
    ]
    judge_quality_scores_run = [
        float((q.get("offline_judge") or {}).get("quality_score"))
        for q in questions
        if (q.get("offline_judge") or {}).get("quality_score") is not None
    ]
    if judge_with_result > 0:
        bm["judge_pass_count"] = judge_pass_cnt
        bm["judge_review_count"] = judge_review_cnt
        bm["judge_reject_count"] = judge_reject_cnt
        bm["judge_pass_rate"] = round(_safe_div(judge_pass_cnt, judge_with_result), 4)
        bm["judge_reject_rate"] = round(_safe_div(judge_reject_cnt, judge_with_result), 4)
        bm["judge_overall_score_avg"] = round(_safe_div(sum(judge_overall_scores), len(judge_overall_scores)), 2)
        if judge_baseline_scores_run:
            bm["judge_baseline_score_avg"] = round(_safe_div(sum(judge_baseline_scores_run), len(judge_baseline_scores_run)), 2)
    bm["judge_total_llm_calls"] = int(judge_calls_sum)
    bm["judge_failed_llm_calls"] = int(judge_failed_calls_sum)
    bm["judge_total_prompt_tokens"] = int(judge_prompt_tokens_sum)
    bm["judge_total_completion_tokens"] = int(judge_completion_tokens_sum)
    bm["judge_total_tokens"] = int(judge_total_tokens_sum)
    bm["judge_total_latency_ms"] = int(judge_latency_ms_sum)
    bm["judge_total_cost_usd"] = round(judge_cost_usd_sum, 6)
    bm["judge_avg_tokens_per_question"] = round(_safe_div(judge_total_tokens_sum, judge_with_result), 2) if judge_with_result > 0 else 0.0
    bm["judge_avg_latency_ms_per_question"] = round(_safe_div(judge_latency_ms_sum, judge_with_result), 2) if judge_with_result > 0 else 0.0
    bm["judge_avg_cost_usd_per_question"] = round(_safe_div(judge_cost_usd_sum, judge_with_result), 6) if judge_with_result > 0 else 0.0
    if judge_quality_scores_run:
        bm["quality_score_avg"] = round(_safe_div(sum(judge_quality_scores_run), len(judge_quality_scores_run)), 2)
    return bm, judge_with_result


def _normalize_run_batch_metrics(run: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if not isinstance(run, dict):
        return run, False
    questions = run.get("questions") if isinstance(run.get("questions"), list) else []
    question_rows = [q for q in questions if isinstance(q, dict)]
    if not question_rows:
        return run, False
    out = dict(run)
    bm = dict(out.get("batch_metrics") or {})
    actual_generated = int(len(question_rows))
    actual_saved = int(sum(1 for q in question_rows if bool(q.get("saved", False)) is True))
    changed = False
    if int(bm.get("generated_count", -1) or 0) != actual_generated:
        bm["generated_count"] = actual_generated
        changed = True
    if int(bm.get("saved_count", -1) or 0) != actual_saved:
        bm["saved_count"] = actual_saved
        changed = True
    if changed:
        out["batch_metrics"] = bm
    return out, changed


def _execute_judge_run(
    tenant_id: str,
    run_id: str,
    requested_ids_raw: Any = None,
    *,
    task_id: str = "",
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    run, questions, ids_to_run, err = _prepare_judge_run_targets(tenant_id, run_id, requested_ids_raw)
    if err:
        code, message, status = err
        raise RuntimeError(f"{code}|{status}|{message}")
    assert isinstance(run, dict)
    config_payload = run.get("config") if isinstance(run.get("config"), dict) else {}
    judge_llm, judge_llm_error = _get_offline_judge_llm()
    if judge_llm is None:
        _append_judge_log(tenant_id, "LLM_BUILD_FAIL", {"run_id": run_id, "error": judge_llm_error})
        msg = judge_llm_error or "请检查 填写您的Key.txt 中 AIT_API_KEY 及 AIT_BASE_URL"
        raise RuntimeError(f"JUDGE_UNAVAILABLE|503|Judge 依赖的 LLM 不可用: {msg}")
    n_to_run = len(ids_to_run)
    _append_judge_log(tenant_id, "JUDGE_RUN_START", {"run_id": run_id, "question_count": len(questions), "ids_to_run_count": n_to_run})
    started = datetime.now(timezone.utc).isoformat()
    run = dict(run)
    run["questions"] = list(questions)
    run["judge_job"] = {
        "status": "running",
        "task_id": task_id,
        "started_at": started,
        "updated_at": started,
        "finished_at": "",
        "requested_count": int(n_to_run),
        "completed_count": 0,
        "success_count": 0,
        "error_count": 0,
        "current_question_id": "",
        "requested_question_ids": sorted(ids_to_run),
        "last_error": "",
    }
    if not _update_qa_run(tenant_id, run_id, run):
        raise RuntimeError("UPDATE_FAILED|500|初始化 Judge 运行状态失败")

    completed_count = 0
    success_count = 0
    error_count = 0
    cancelled = False
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            continue
        qid = str(q.get("question_id", "")).strip()
        if qid not in ids_to_run:
            continue
        if task_id and _is_judge_task_cancelled(task_id):
            cancelled = True
            break
        run["judge_job"]["current_question_id"] = qid
        run["judge_job"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        _update_qa_run(tenant_id, run_id, run)
        if on_progress:
            on_progress(
                {
                    "progress": {"current": int(completed_count), "total": int(n_to_run)},
                    "current_question_id": qid,
                    "success_count": int(success_count),
                    "error_count": int(error_count),
                    "judge_count": int(completed_count),
                }
            )
        _append_judge_log(tenant_id, "JUDGE_QUESTION_START", {"run_id": run_id, "question_id": qid})
        report = _run_offline_judge_for_question(q, config_payload, judge_llm, tenant_id=tenant_id)
        if report is None:
            _append_judge_log(tenant_id, "JUDGE_SKIP_NONE", {"run_id": run_id, "question_id": qid})
            continue
        questions[i] = dict(q)
        questions[i]["offline_judge"] = report
        run["questions"] = questions
        trace_row = {"run_id": run_id, "question_id": qid, "index": int(q.get("index", 0) or 0)}
        trace_row.update(report.get("_qa_trace") or {})
        _append_qa_trace(tenant_id, trace_row)
        completed_count += 1
        if report.get("error"):
            error_count += 1
        else:
            success_count += 1
        run["judge_job"].update(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "completed_count": int(completed_count),
                "success_count": int(success_count),
                "error_count": int(error_count),
                "current_question_id": qid,
            }
        )
        if not _update_qa_run(tenant_id, run_id, run):
            raise RuntimeError("UPDATE_FAILED|500|更新 Judge 题目进度失败")
        if report.get("error"):
            log_detail = {"run_id": run_id, "question_id": qid, "error": report["error"]}
            _append_judge_log(tenant_id, "JUDGE_QUESTION_FAIL", log_detail)
        else:
            _append_judge_log(
                tenant_id,
                "JUDGE_QUESTION_DONE",
                {"run_id": run_id, "question_id": qid, "decision": report.get("decision", "")},
            )
        if on_progress:
            on_progress(
                {
                    "progress": {"current": int(completed_count), "total": int(n_to_run)},
                    "current_question_id": qid,
                    "success_count": int(success_count),
                    "error_count": int(error_count),
                    "judge_count": int(completed_count),
                }
            )

    run = dict(run)
    run["questions"] = questions
    bm, judge_with_result = _recompute_run_judge_metrics(run, questions)
    run["batch_metrics"] = bm
    now = datetime.now(timezone.utc).isoformat()
    if cancelled:
        status_text = "cancelled"
    else:
        status_text = "completed"
    run["judge_job"] = {
        **(run.get("judge_job") or {}),
        "status": status_text,
        "updated_at": now,
        "finished_at": now,
        "completed_count": int(completed_count),
        "success_count": int(success_count),
        "error_count": int(error_count),
        "current_question_id": "",
        "last_error": "",
    }
    if not _update_qa_run(tenant_id, run_id, run):
        raise RuntimeError("UPDATE_FAILED|500|更新 run 失败")
    _append_judge_log(
        tenant_id,
        "JUDGE_RUN_DONE" if not cancelled else "JUDGE_RUN_CANCELLED",
        {
            "run_id": run_id,
            "requested_count": int(n_to_run),
            "completed_count": int(completed_count),
            "success_count": int(success_count),
            "error_count": int(error_count),
        },
    )
    return {
        "ok": True,
        "run_id": run_id,
        "judge_count": int(judge_with_result),
        "completed_count": int(completed_count),
        "success_count": int(success_count),
        "error_count": int(error_count),
        "requested_count": int(n_to_run),
        "cancelled": cancelled,
    }


def _run_judge_task_worker(tenant_id: str, task_id: str, run_id: str, body: dict[str, Any]) -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    _update_judge_task_live(
        tenant_id,
        task_id,
        {"status": "running", "started_at": started_at, "run_id": run_id},
    )
    _persist_live_judge_task_snapshot(tenant_id, task_id)
    try:
        requested_ids = (body or {}).get("question_ids")

        def _on_progress(patch: dict[str, Any]) -> None:
            _update_judge_task_live(tenant_id, task_id, patch)
            _persist_live_judge_task_snapshot(tenant_id, task_id)

        result = _execute_judge_run(
            tenant_id,
            run_id,
            requested_ids_raw=requested_ids,
            task_id=task_id,
            on_progress=_on_progress,
        )
        ended_at = datetime.now(timezone.utc).isoformat()
        status = "cancelled" if result.get("cancelled") else "completed"
        _update_judge_task_live(
            tenant_id,
            task_id,
            {
                "status": status,
                "ended_at": ended_at,
                "progress": {
                    "current": int(result.get("completed_count", 0) or 0),
                    "total": int(result.get("requested_count", 0) or 0),
                },
                "judge_count": int(result.get("judge_count", 0) or 0),
                "success_count": int(result.get("success_count", 0) or 0),
                "error_count": int(result.get("error_count", 0) or 0),
                "current_question_id": "",
                "errors": [],
            },
        )
        _persist_live_judge_task_snapshot(tenant_id, task_id)
    except Exception as e:
        ended_at = datetime.now(timezone.utc).isoformat()
        msg = str(e)
        parts = msg.split("|", 2)
        if len(parts) == 3 and parts[1].isdigit():
            msg = parts[2]
        _update_judge_task_live(
            tenant_id,
            task_id,
            {"status": "failed", "ended_at": ended_at, "errors": [msg], "current_question_id": ""},
        )
        _persist_live_judge_task_snapshot(tenant_id, task_id)
    finally:
        # Continue serial queue for this tenant.
        _start_next_judge_task_if_idle(tenant_id)


@app.post('/api/<tenant_id>/judge/tasks')
def api_judge_task_create(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限执行 Judge 测评", 403)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    run_id = str(body.get("run_id", "")).strip()
    if not run_id:
        return _error("BAD_REQUEST", "run_id is required", 400)
    task_name = str(body.get("task_name", "") or "").strip()
    if not task_name:
        task_name = f"Judge-{run_id[:18]}"
    if _judge_task_name_exists(tenant_id, task_name):
        return _error("BAD_REQUEST", "task_name already exists", 400)
    body["task_name"] = task_name
    _, _, _, err = _prepare_judge_run_targets(tenant_id, run_id, body.get("question_ids"))
    if err:
        code, message, status = err
        return _error(code, message, status)
    task = _make_judge_task(tenant_id, run_id, system_user, body)
    _persist_judge_task(tenant_id, task)
    _start_next_judge_task_if_idle(tenant_id)
    return _json_response({"task": _build_judge_task_summary(task)})


@app.get('/api/<tenant_id>/judge/tasks')
def api_judge_task_list(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限查看 Judge 任务", 403)
    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    run_id_filter = str(request.args.get("run_id", "")).strip()
    rows: dict[str, dict[str, Any]] = {}
    with JUDGE_TASK_LOCK:
        for task in JUDGE_TASKS.values():
            if str(task.get("tenant_id", "")) != tenant_id:
                continue
            tid = str(task.get("task_id", ""))
            if tid:
                rows[tid] = _task_snapshot(task)
    desired_count = max(limit * 4, 200)
    persisted_rows, _ = _collect_recent_jsonl_rows_from_paths(
        _qa_read_paths(tenant_id, "judge_tasks.jsonl"),
        target_count=desired_count,
        sort_key=lambda row: str(row.get("created_at", "") or ""),
        predicate=(
            (lambda row: str(row.get("run_id", "") or ((row.get("request") or {}).get("run_id", "") if isinstance(row.get("request"), dict) else "")).strip() == run_id_filter)
            if run_id_filter else None
        ),
        unique_key=lambda row: str(row.get("task_id", "") or ""),
    )
    for task in persisted_rows:
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id", ""))
        # Persisted task file is append-only; keep latest snapshot for same task_id.
        if tid:
            rows[tid] = task
    items = list(rows.values())
    if run_id_filter:
        items = [
            x for x in items
            if str(x.get("run_id", "") or ((x.get("request") or {}).get("run_id", "") if isinstance(x.get("request"), dict) else "")).strip() == run_id_filter
        ]
    items.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    items = items[:limit]
    run_task_name_lookup = _load_run_task_name_lookup(tenant_id)
    return _json_response(
        {
            "items": [_build_judge_task_summary(x, run_task_name_lookup=run_task_name_lookup) for x in items],
            "total": len(items),
        }
    )


@app.get('/api/<tenant_id>/judge/tasks/<task_id>')
def api_judge_task_detail(tenant_id: str, task_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限查看 Judge 任务详情", 403)
    with JUDGE_TASK_LOCK:
        task = JUDGE_TASKS.get(task_id)
        if task and str(task.get("tenant_id", "")) == tenant_id:
            return _json_response({"task": _build_judge_task_summary(task)})
    persisted = _read_persisted_judge_task(tenant_id, task_id)
    if isinstance(persisted, dict):
        return _json_response({"task": _build_judge_task_summary(persisted)})
    return _error("TASK_NOT_FOUND", "Judge 任务不存在", 404)


@app.post('/api/<tenant_id>/judge/tasks/<task_id>/cancel')
def api_judge_task_cancel(tenant_id: str, task_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限取消 Judge 任务", 403)
    tid = str(task_id or "").strip()
    if not tid:
        return _error("BAD_REQUEST", "task_id is required", 400)
    cancelled_pending = False
    with JUDGE_TASK_LOCK:
        task = JUDGE_TASKS.get(tid)
        if not task or str(task.get("tenant_id", "")) != tenant_id:
            task = _read_persisted_judge_task(tenant_id, tid)
        if not isinstance(task, dict):
            return _error("TASK_NOT_FOUND", "Judge 任务不存在", 404)
        status = str(task.get("status", "") or "")
        if status not in ("pending", "running"):
            return _json_response({"ok": True, "task_id": tid, "status": status, "message": "任务已结束，无需取消"})
        if tid not in JUDGE_TASKS or str(JUDGE_TASKS[tid].get("tenant_id", "")) != tenant_id:
            return _json_response(
                {"ok": False, "task_id": tid, "status": status, "message": "任务不在当前进程中，无法取消（可能服务已重启）"}
            )
        JUDGE_TASKS[tid]["cancel_requested"] = True
        if status == "pending":
            now = datetime.now(timezone.utc).isoformat()
            JUDGE_TASKS[tid]["status"] = "cancelled"
            JUDGE_TASKS[tid]["ended_at"] = now
            JUDGE_TASKS[tid]["updated_at"] = now
            _persist_judge_task(tenant_id, _task_snapshot(JUDGE_TASKS[tid]))
            cancelled_pending = True
    if cancelled_pending:
        _start_next_judge_task_if_idle(tenant_id)
        return _json_response({"ok": True, "task_id": tid, "status": "cancelled", "message": "排队中的任务已取消"})
    return _json_response({"ok": True, "task_id": tid, "status": "cancel_requested", "message": "已请求取消，任务将尽快停止"})


@app.post('/api/<tenant_id>/qa/runs/<run_id>/run-judge')
def api_qa_run_judge(tenant_id: str, run_id: str):
    """兼容旧接口：改为创建异步 Judge 任务并立即返回 task。"""
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限执行 Judge 测评", 403)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    body["run_id"] = str(run_id or "").strip()
    task_name = str(body.get("task_name", "") or "").strip() or f"Judge-{str(run_id)[:18]}"
    if _judge_task_name_exists(tenant_id, task_name):
        return _error("BAD_REQUEST", "task_name already exists", 400)
    body["task_name"] = task_name
    _, _, _, err = _prepare_judge_run_targets(tenant_id, run_id, body.get("question_ids"))
    if err:
        code, message, status = err
        return _error(code, message, status)
    task = _make_judge_task(tenant_id, run_id, system_user, body)
    _persist_judge_task(tenant_id, task)
    _start_next_judge_task_if_idle(tenant_id)
    return _json_response({"ok": True, "run_id": run_id, "task": _build_judge_task_summary(task)})


@app.get('/api/<tenant_id>/qa/overview')
def api_qa_overview(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问评估总览", 403)
    material_version_id = str(request.args.get("material_version_id", "")).strip()
    days = max(0, int(request.args.get("days", 30) or 30))
    run_id = str(request.args.get("run_id", "")).strip()
    run_ids_raw = str(request.args.get("run_ids", "")).strip()
    run_ids = [x.strip() for x in run_ids_raw.split(",") if x.strip()]
    run_ids_set = set(run_ids)
    success_only = request.args.get("success_only", "1").strip() in ("1", "true", "yes")
    runs = _filter_qa_runs(
        tenant_id,
        material_version_id=material_version_id,
        days=days,
        success_only=success_only,
    )
    if run_ids_set:
        runs = [x for x in runs if str(x.get("run_id", "")) in run_ids_set]
    if run_id:
        runs = [x for x in runs if str(x.get("run_id", "")) == run_id]
    if not runs:
        return _json_response(
            {
                "run_id": run_id,
                "run_ids": run_ids,
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
                "cpvq": None,
                "currency": "CNY",
                "avg_critic_loops": 0,
                "error_call_rate": 0,
                "judge_pass_rate": None,
                "judge_review_rate": None,
                "judge_reject_rate": None,
                "judge_overall_score_avg": None,
                "judge_baseline_score_avg": None,
                "judge_total_llm_calls": 0,
                "judge_failed_llm_calls": 0,
                "judge_total_tokens": 0,
                "judge_total_latency_ms": 0,
                "judge_total_cost_usd": 0,
                "judge_avg_tokens_per_question": 0,
                "judge_avg_latency_ms_per_question": 0,
                "judge_avg_cost_usd_per_question": 0,
                "judge_question_count": 0,
                "judge_scored_count": 0,
                "judge_pass_count": 0,
                "judge_review_count": 0,
                "judge_reject_count": 0,
                "slice_success_stats": [],
            }
        )
    bm_list = [x.get("batch_metrics", {}) for x in runs if isinstance(x.get("batch_metrics"), dict)]
    n = len(bm_list)
    total_questions = sum(int(x.get("question_count", 0) or 0) for x in bm_list)
    total_llm_calls_sum = sum(int(x.get("total_llm_calls", 0) or 0) for x in bm_list)
    error_calls_sum = sum(int(x.get("error_calls", 0) or 0) for x in bm_list)
    total_cost_sum = sum(float(x.get("total_cost", 0) or 0) for x in bm_list)
    weighted = lambda key: _safe_div(
        sum(float(x.get(key, 0) or 0) * int(x.get("question_count", 0) or 0) for x in bm_list),
        total_questions,
    )
    overview = {
        "run_id": run_id or str(runs[0].get("run_id", "")),
        "run_ids": [str(x.get("run_id", "")) for x in runs if str(x.get("run_id", "")).strip()],
        "material_version_id": material_version_id,
        "days": days,
        "run_count": n,
        "hard_pass_rate": round(weighted("hard_pass_rate"), 4),
        "quality_score_avg": round(weighted("quality_score_avg"), 2),
        "risk_high_rate": round(weighted("risk_high_rate"), 4),
        "logic_pass_rate": round(weighted("logic_pass_rate"), 4),
        "duplicate_rate": round(weighted("duplicate_rate"), 4),
        "knowledge_match_rate": round(weighted("knowledge_match_rate"), 4),
        "avg_tokens_per_question": round(weighted("avg_tokens_per_question"), 2),
        "avg_latency_ms_per_question": round(weighted("avg_latency_ms_per_question"), 2),
        "avg_cost_per_question": round(_safe_div(total_cost_sum, total_questions), 6),
        "total_cost": round(total_cost_sum, 6),
        "currency": str((bm_list[0] or {}).get("currency", "CNY")),
        "avg_critic_loops": round(weighted("avg_critic_loops"), 3),
        "error_call_rate": round(_safe_div(error_calls_sum, total_llm_calls_sum), 4),
        "slice_success_stats": _build_slice_success_stats_from_runs(runs),
    }
    # Judge metrics: aggregate directly from latest offline_judge payload on each question.
    judge_pass_sum = 0
    judge_review_sum = 0
    judge_reject_sum = 0
    judge_total_questions = 0
    judge_scored_count = 0
    judge_overall_scores: list[float] = []
    judge_baseline_scores: list[float] = []
    judge_quality_scores: list[float] = []
    judge_calls_sum = 0
    judge_failed_calls_sum = 0
    judge_total_tokens_sum = 0
    judge_total_latency_ms_sum = 0
    judge_total_cost_usd_sum = 0.0
    for run in runs:
        for q in (run.get("questions") or []):
            if not isinstance(q, dict):
                continue
            oj = q.get("offline_judge") if isinstance(q.get("offline_judge"), dict) else {}
            if not oj:
                continue
            judge_total_questions += 1
            decision = str(oj.get("decision", "") or "").strip().lower()
            if decision == "pass":
                judge_pass_sum += 1
            elif decision == "review":
                judge_review_sum += 1
            elif decision == "reject":
                judge_reject_sum += 1
            if decision in {"pass", "review", "reject"}:
                judge_scored_count += 1
            overall_score = oj.get("overall_score")
            if overall_score is not None:
                try:
                    judge_overall_scores.append(float(overall_score))
                except Exception:
                    pass
            baseline_score = oj.get("baseline_score")
            if baseline_score is None:
                baseline_score = oj.get("penalty_score")
            if baseline_score is not None:
                try:
                    judge_baseline_scores.append(float(baseline_score))
                except Exception:
                    pass
            quality_score = oj.get("quality_score")
            if quality_score is not None:
                try:
                    judge_quality_scores.append(float(quality_score))
                except Exception:
                    pass
            obs = oj.get("observability") if isinstance(oj.get("observability"), dict) else {}
            tok = obs.get("tokens") if isinstance(obs.get("tokens"), dict) else {}
            costs = oj.get("costs") if isinstance(oj.get("costs"), dict) else {}
            judge_calls_sum += int(obs.get("llm_calls", 0) or 0)
            judge_failed_calls_sum += int(obs.get("failed_calls", 0) or 0)
            judge_total_tokens_sum += int(tok.get("total_tokens", 0) or 0)
            judge_total_latency_ms_sum += int(obs.get("latency_ms", 0) or 0)
            judge_total_cost_usd_sum += float(costs.get("per_question_usd", 0.0) or 0.0)

    judge_total = judge_pass_sum + judge_review_sum + judge_reject_sum
    if judge_total > 0:
        overview["judge_pass_rate"] = round(_safe_div(judge_pass_sum, judge_total), 4)
        overview["judge_review_rate"] = round(_safe_div(judge_review_sum, judge_total), 4)
        overview["judge_reject_rate"] = round(_safe_div(judge_reject_sum, judge_total), 4)
    else:
        overview["judge_pass_rate"] = None
        overview["judge_review_rate"] = None
        overview["judge_reject_rate"] = None
    overview["judge_overall_score_avg"] = round(_safe_div(sum(judge_overall_scores), len(judge_overall_scores)), 2) if judge_overall_scores else None
    overview["judge_baseline_score_avg"] = round(_safe_div(sum(judge_baseline_scores), len(judge_baseline_scores)), 2) if judge_baseline_scores else None
    # For overview card, prefer quality_score from offline_judge when available.
    if judge_quality_scores:
        overview["quality_score_avg"] = round(_safe_div(sum(judge_quality_scores), len(judge_quality_scores)), 2)

    overview["judge_question_count"] = int(judge_total_questions)
    overview["judge_scored_count"] = int(judge_scored_count)
    overview["judge_pass_count"] = int(judge_pass_sum)
    overview["judge_review_count"] = int(judge_review_sum)
    overview["judge_reject_count"] = int(judge_reject_sum)
    overview["judge_total_llm_calls"] = int(judge_calls_sum)
    overview["judge_failed_llm_calls"] = int(judge_failed_calls_sum)
    overview["judge_total_tokens"] = int(judge_total_tokens_sum)
    overview["judge_total_latency_ms"] = int(judge_total_latency_ms_sum)
    overview["judge_total_cost_usd"] = round(judge_total_cost_usd_sum, 6)
    overview["judge_avg_tokens_per_question"] = round(_safe_div(judge_total_tokens_sum, judge_scored_count), 2) if judge_scored_count > 0 else 0.0
    overview["judge_avg_latency_ms_per_question"] = round(_safe_div(judge_total_latency_ms_sum, judge_scored_count), 2) if judge_scored_count > 0 else 0.0
    overview["judge_avg_cost_usd_per_question"] = round(_safe_div(judge_total_cost_usd_sum, judge_scored_count), 6) if judge_scored_count > 0 else 0.0
    saved_sum = sum(int(x.get("saved_count", 0) or 0) for x in bm_list)
    overview["saved_count"] = saved_sum
    overview["cpvq"] = round(_safe_div(total_cost_sum, saved_sum), 6) if saved_sum > 0 else None
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
    success_only = request.args.get("success_only", "1").strip() in ("1", "true", "yes")
    runs = _filter_qa_runs(
        tenant_id,
        material_version_id=material_version_id,
        days=days,
        success_only=success_only,
    )
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
                "cpvq": bm.get("cpvq"),
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
    base_run_ids_arg = str(request.args.get("base_run_ids", "")).strip()
    target_run_id = str(request.args.get("target_run_id", "")).strip()
    if not target_run_id:
        return _error("BAD_REQUEST", "target_run_id 必填", 400)
    runs = _read_jsonl(_qa_runs_path(tenant_id))
    by_id = {str(x.get("run_id", "")): x for x in runs if isinstance(x, dict)}
    base_run_ids: list[str] = []
    if base_run_ids_arg:
        base_run_ids = [x.strip() for x in base_run_ids_arg.split(",") if x.strip()]
    elif base_run_id:
        base_run_ids = [base_run_id]
    if not base_run_ids:
        return _error("BAD_REQUEST", "base_run_id 或 base_run_ids 必填", 400)
    base_runs = [by_id.get(rid) for rid in base_run_ids]
    if any(not isinstance(x, dict) for x in base_runs):
        return _error("RUN_NOT_FOUND", "基线运行不存在", 404)
    target = next((x for x in runs if str(x.get("run_id", "")) == target_run_id), None)
    if not isinstance(target, dict):
        return _error("RUN_NOT_FOUND", "对比运行不存在", 404)
    base_rows = [x for x in base_runs if isinstance(x, dict)]
    if len(base_rows) == 1:
        bm_base = base_rows[0].get("batch_metrics") if isinstance(base_rows[0].get("batch_metrics"), dict) else {}
    else:
        bm_base = (_build_virtual_baseline_run(base_rows, base_run_ids).get("batch_metrics") or {})
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
    return _json_response(
        {
            "base_run_id": base_run_ids[0],
            "base_run_ids": base_run_ids,
            "target_run_id": target_run_id,
            "compare": compare,
        }
    )


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


@app.get('/api/<tenant_id>/qa/config')
def api_qa_config_get(tenant_id: str):
    """Return QA config (e.g. baseline_run_id for release comparison)."""
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问 QA 配置", 403)
    return _json_response(_load_qa_config(tenant_id))


@app.put('/api/<tenant_id>/qa/config')
def api_qa_config_put(tenant_id: str):
    """Update QA config (e.g. set baseline_run_id)."""
    try:
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限更新 QA 配置", 403)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return _error("BAD_REQUEST", "请求体必须是 JSON 对象", 400)
    return _json_response(_save_qa_config(tenant_id, body))


@app.get('/api/<tenant_id>/qa/releases')
def api_qa_releases_get(tenant_id: str):
    """List manual releases (newest first). Used as baseline for quality comparison."""
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问发布记录", 403)
    items = _load_qa_releases(tenant_id)
    return _json_response({"items": items})


@app.post('/api/<tenant_id>/qa/releases')
def api_qa_releases_post(tenant_id: str):
    """Publish a version: version number, release notes, run_ids (or run_id). Optional git commit trigger."""
    try:
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限发布版本", 403)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return _error("BAD_REQUEST", "请求体必须是 JSON 对象", 400)
    version = str(body.get("version", "")).strip()
    release_notes = str(body.get("release_notes", "")).strip()
    raw_run_ids = body.get("run_ids")
    run_ids: list[str] = []
    if isinstance(raw_run_ids, list):
        run_ids = [str(x).strip() for x in raw_run_ids if str(x).strip()]
    elif raw_run_ids is not None:
        run_ids = [x.strip() for x in str(raw_run_ids).split(",") if x.strip()]
    else:
        rid = str(body.get("run_id", "")).strip()
        if rid:
            run_ids = [rid]
    trigger_git_commit = bool(body.get("trigger_git_commit", False))
    git_repo_url = str(body.get("git_repo_url", _DEFAULT_RELEASE_GIT_REMOTE_URL) or "").strip()
    git_user_email = str(body.get("git_user_email", _DEFAULT_RELEASE_GIT_USER_EMAIL) or "").strip()
    git_user_name = str(body.get("git_user_name", "") or "").strip()
    git_username = str(body.get("git_username", "") or "").strip()
    git_token = str(body.get("git_token", "") or "").strip()
    git_commit_message = str(body.get("git_commit_message", _DEFAULT_RELEASE_GIT_COMMIT_MESSAGE) or "").strip()
    git_push_branch = str(body.get("git_push_branch", _DEFAULT_RELEASE_GIT_BRANCH) or "").strip() or _DEFAULT_RELEASE_GIT_BRANCH
    if not version:
        return _error("BAD_REQUEST", "version 必填", 400)
    if not run_ids:
        return _error("BAD_REQUEST", "run_ids 必填（至少 1 个）", 400)
    if len(set(run_ids)) != len(run_ids):
        deduped: list[str] = []
        seen: set[str] = set()
        for rid in run_ids:
            if rid in seen:
                continue
            seen.add(rid)
            deduped.append(rid)
        run_ids = deduped
    runs_by_id = {str(x.get("run_id", "")): x for x in _read_jsonl(_qa_runs_path(tenant_id)) if isinstance(x, dict)}
    selected_runs: list[dict[str, Any]] = []
    for rid in run_ids:
        run = runs_by_id.get(rid)
        if not isinstance(run, dict):
            return _error("RUN_NOT_FOUND", f"该 run_id 不存在: {rid}", 404)
        bm = run.get("batch_metrics") if isinstance(run.get("batch_metrics"), dict) else {}
        saved_count = int(bm.get("saved_count", 0) or 0)
        if saved_count <= 0:
            return _error(
                "RELEASE_PREREQ",
                f"发布版本前所选 run 须有落库题目（saved_count>0）: {rid}",
                400,
            )
        has_judge = (
            bm.get("judge_pass_rate") is not None
            or bm.get("judge_pass_count") is not None
            or any(bool(q.get("offline_judge")) for q in (run.get("questions") or []) if isinstance(q, dict))
        )
        if not has_judge:
            return _error(
                "RELEASE_PREREQ",
                f"发布版本前所选 run 的落库题目须已跑过离线 Judge: {rid}",
                400,
            )
        selected_runs.append(run)
    system_user = _get_system_user()
    published_at = datetime.now(timezone.utc).isoformat()
    primary_run_id = run_ids[0]
    release = {
        "version": version,
        "release_notes": release_notes,
        "run_id": primary_run_id,
        "run_ids": run_ids,
        "published_at": published_at,
        "published_by": system_user,
    }
    suggested_commit_message = f"Release {version}: {release_notes[:200]}" + ("..." if len(release_notes) > 200 else "")
    out = {"release": release, "suggested_commit_message": suggested_commit_message}
    if trigger_git_commit:
        git_result = _run_git_commit_for_release(
            tenant_id,
            version,
            release_notes,
            git_options={
                "remote_url": git_repo_url,
                "user_email": git_user_email,
                "user_name": git_user_name,
                "git_username": git_username,
                "git_token": git_token,
                "commit_message": git_commit_message,
                "push_branch": git_push_branch,
            },
        )
        out["git"] = git_result
        release["git"] = {
            "ok": bool(git_result.get("ok", False)),
            "message": str(git_result.get("message", "") or ""),
            "error": str(git_result.get("error", "") or ""),
            "warning": str(git_result.get("warning", "") or ""),
            "commit_message": str(git_result.get("commit_message", "") or git_commit_message),
            "remote_url": str(git_result.get("remote_url", "") or git_repo_url),
            "push_branch": str(git_result.get("push_branch", "") or git_push_branch),
            "requested_push_branch": str(git_result.get("requested_push_branch", "") or git_push_branch),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    with QA_PERSIST_LOCK:
        _append_qa_release(tenant_id, release)
    return _json_response(out)


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
    target_count = max(page * page_size + 1, 200)
    items, exhausted = _collect_recent_jsonl_rows_from_paths(
        _qa_read_paths(tenant_id, "qa_alerts.jsonl"),
        target_count=target_count,
        sort_key=lambda row: str(row.get("created_at", "") or ""),
        predicate=lambda row: (
            (not run_id or str(row.get("run_id", "")) == run_id)
            and (not status or str(row.get("status", "")) == status)
            and (not level or str(row.get("level", "")) == level)
        ),
    )
    items = _decorate_alert_rows(items)
    total = len(items) if exhausted else max(len(items), target_count)
    start = (page - 1) * page_size
    end = start + page_size
    payload = {"items": items[start:end], "total": total, "page": page, "page_size": page_size}
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
    base_run_ids_arg = str(request.args.get("base_run_ids", "")).strip()
    target_run_id = str(request.args.get("target_run_id", "")).strip()
    if not target_run_id:
        return _error("BAD_REQUEST", "target_run_id 必填", 400)
    runs = _read_jsonl(_qa_runs_path(tenant_id))
    by_id = {str(x.get("run_id", "")): x for x in runs if isinstance(x, dict)}
    base_run_ids: list[str] = []
    if base_run_ids_arg:
        base_run_ids = [x.strip() for x in base_run_ids_arg.split(",") if x.strip()]
    elif base_run_id:
        base_run_ids = [base_run_id]
    else:
        # fallback to latest release baseline
        latest_release = (_load_qa_releases(tenant_id) or [{}])[0]
        if isinstance(latest_release, dict):
            if isinstance(latest_release.get("run_ids"), list):
                base_run_ids = [str(x).strip() for x in (latest_release.get("run_ids") or []) if str(x).strip()]
            if not base_run_ids:
                rid = str(latest_release.get("run_id", "")).strip()
                if rid:
                    base_run_ids = [rid]
    if not base_run_ids:
        return _error("BAD_REQUEST", "base_run_id 或 base_run_ids 必填", 400)
    base_runs = [by_id.get(rid) for rid in base_run_ids]
    if any(not isinstance(x, dict) for x in base_runs):
        return _error("RUN_NOT_FOUND", "基线运行不存在", 404)
    target = next((x for x in runs if str(x.get("run_id", "")) == target_run_id), None)
    if not isinstance(target, dict):
        return _error("RUN_NOT_FOUND", "对比运行不存在", 404)
    base_rows = [x for x in base_runs if isinstance(x, dict)]
    if len(base_rows) == 1:
        return _json_response(_build_release_report(base_rows[0], target))
    report = _build_release_report(_build_virtual_baseline_run(base_rows, base_run_ids), target)
    report["base_run_ids"] = base_run_ids
    report["base_run_id"] = base_run_ids[0]
    return _json_response(report)


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


def _collect_task_scope_identifiers(task: dict[str, Any]) -> tuple[set[str], set[str], str]:
    """
    收集任务作用域标识：
    - task_ids: 父任务 + 子任务ID
    - task_names: 父任务 + 子任务名称
    - parent_name: 父任务名称（用于匹配 #p/#repair/#resume 子任务前缀）
    """
    task_ids: set[str] = set()
    task_names: set[str] = set()
    parent_name = str(task.get("task_name", "") or "").strip()
    tid = str(task.get("task_id", "") or "").strip()
    if tid:
        task_ids.add(tid)
    if parent_name:
        task_names.add(parent_name)
    for sub in (task.get("subtasks") or []):
        if not isinstance(sub, dict):
            continue
        sid = str(sub.get("task_id", "") or "").strip()
        sname = str(sub.get("task_name", "") or "").strip()
        if sid:
            task_ids.add(sid)
        if sname:
            task_names.add(sname)
    return task_ids, task_names, parent_name


def _is_trace_row_passed_for_bank(row: dict[str, Any]) -> bool:
    """
    判断单题 trace 是否属于“已通过可入库”。
    """
    if not isinstance(row, dict):
        return False
    final_json = row.get("final_json")
    if not isinstance(final_json, dict) or not final_json:
        return False
    if bool(row.get("saved")) or bool(row.get("saved_with_issues")):
        return True
    critic_result = row.get("critic_result") if isinstance(row.get("critic_result"), dict) else {}
    if critic_result.get("passed") is True:
        return True
    for step in (row.get("steps") or []):
        if not isinstance(step, dict):
            continue
        if str(step.get("node", "")).strip() == "critic" and str(step.get("message", "")).strip() == "审核通过":
            return True
    return False


def _normalize_bank_identity_key(item: dict[str, Any]) -> str:
    """
    归一化题目主键（含来源任务维度）用于去重。
    """
    stem = _normalize_text_key(item.get("题干"))
    ans = _normalize_answer_key(item.get("正确答案"))
    path = _normalize_text_key(item.get("来源路径"))
    tid = str(item.get("source_task_id") or item.get("出题任务ID") or item.get("task_id") or "").strip()
    return f"{tid}|{path}|{ans}|{stem}"


def _collect_passed_bank_items_from_task(task: dict[str, Any]) -> list[dict[str, Any]]:
    """
    从任务详情中抽取“已通过题目”并转成可入库 payload。
    同时补齐来源字段，便于后续按任务开关回收。
    """
    out: list[dict[str, Any]] = []
    task_ids, _, _ = _collect_task_scope_identifiers(task)
    task_id = str(task.get("task_id", "") or "").strip()
    task_name = str(task.get("task_name", "") or "").strip()
    run_id = str(task.get("run_id", "") or "").strip()
    material_version_id = str(task.get("material_version_id", "") or (task.get("request") or {}).get("material_version_id", "") or "").strip()
    traces: list[dict[str, Any]] = []
    traces.extend([x for x in (task.get("process_trace") or []) if isinstance(x, dict)])
    for sub in (task.get("live_subtask_traces") or []):
        if not isinstance(sub, dict):
            continue
        traces.extend([x for x in (sub.get("process_trace") or []) if isinstance(x, dict)])
    for row in traces:
        if not _is_trace_row_passed_for_bank(row):
            continue
        fj = row.get("final_json")
        if not isinstance(fj, dict) or not fj:
            continue
        q = dict(fj)
        src_tid = str(q.get("source_task_id") or q.get("出题任务ID") or "").strip()
        src_tname = str(q.get("source_task_name") or q.get("出题任务名称") or "").strip()
        src_rid = str(q.get("source_run_id") or q.get("出题RunID") or "").strip()
        if not src_tid or src_tid not in task_ids:
            q["source_task_id"] = src_tid or task_id
            q["出题任务ID"] = src_tid or task_id
        if not src_tname:
            q["source_task_name"] = task_name
            q["出题任务名称"] = task_name
        if not src_rid:
            q["source_run_id"] = run_id
            q["出题RunID"] = run_id
        if material_version_id and not str(q.get("教材版本ID", "")).strip():
            q["教材版本ID"] = material_version_id
        out.append(q)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in out:
        key = _normalize_bank_identity_key(row)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _is_bank_row_in_task_scope(row: dict[str, Any], task: dict[str, Any]) -> bool:
    """
    判断题库题目是否属于该任务作用域（父任务+子任务）。
    """
    task_ids, task_names, parent_name = _collect_task_scope_identifiers(task)
    tid = str(row.get("source_task_id") or row.get("出题任务ID") or row.get("task_id") or "").strip()
    tname = str(row.get("source_task_name") or row.get("出题任务名称") or row.get("task_name") or "").strip()
    if tid and tid in task_ids:
        return True
    if tname and tname in task_names:
        return True
    if parent_name and tname.startswith(f"{parent_name}#"):
        return True
    return False


def _sync_task_bank_policy(tenant_id: str, task: dict[str, Any], enabled: bool) -> tuple[int, int, int]:
    """
    将任务入库策略同步到题库数据。
    返回值：(added, removed, scope_saved_count)。
    """
    bank_path = tenant_bank_path(tenant_id)
    bank = _load_bank(bank_path)
    added = 0
    removed = 0
    if enabled:
        existing_keys = {_normalize_bank_identity_key(x) for x in bank if isinstance(x, dict)}
        candidates = _collect_passed_bank_items_from_task(task)
        for q in candidates:
            key = _normalize_bank_identity_key(q)
            if not key or key in existing_keys:
                continue
            bank.append(q)
            existing_keys.add(key)
            added += 1
    else:
        kept: list[dict[str, Any]] = []
        for row in bank:
            if isinstance(row, dict) and _is_bank_row_in_task_scope(row, task):
                removed += 1
                continue
            kept.append(row)
        bank = kept
    scope_saved_count = sum(1 for row in bank if isinstance(row, dict) and _is_bank_row_in_task_scope(row, task))
    if added > 0 or removed > 0:
        _save_bank(bank_path, bank)
    return added, removed, scope_saved_count


def _normalize_bank_question_item(
    raw_item: Any,
    *,
    fallback_item: dict[str, Any] | None = None,
    merge_with_fallback: bool = True,
) -> dict[str, Any]:
    """Normalize/merge one bank question item into the storage schema."""
    base = deepcopy(fallback_item) if (merge_with_fallback and isinstance(fallback_item, dict)) else {}
    candidate = deepcopy(raw_item) if isinstance(raw_item, dict) else {}
    for key in ("item", "question", "final_json", "draft", "optimized_question"):
        nested = candidate.get(key)
        if isinstance(nested, dict):
            candidate = deepcopy(nested)
            break

    out = dict(base)
    out.update(candidate)
    out.pop("question_id", None)
    out.pop("_gen_key", None)

    stem = str(
        out.get("题干", "")
        or out.get("question", "")
        or out.get("stem", "")
        or out.get("题目", "")
    ).strip()
    if stem:
        out["题干"] = stem

    explanation = str(out.get("解析", "") or out.get("explanation", "")).strip()
    if explanation:
        out["解析"] = explanation

    raw_answer = str(out.get("正确答案", "") or out.get("answer", "")).strip()
    normalized_answer = raw_answer.replace(" ", "").upper()
    if normalized_answer:
        out["正确答案"] = normalized_answer

    options: list[str] = []
    option_dict = out.get("选项")
    if isinstance(option_dict, dict):
        for k in ("A", "B", "C", "D", "E", "F", "G", "H"):
            value = str(option_dict.get(k, "") or "").strip()
            if value:
                options.append(value)

    if not options and isinstance(out.get("options"), list):
        options = [str(x or "").strip() for x in (out.get("options") or []) if str(x or "").strip()]

    if not options:
        for i in range(1, 9):
            value = str(out.get(f"选项{i}", "") or "").strip()
            if value:
                options.append(value)

    if options:
        for i in range(1, 9):
            out[f"选项{i}"] = options[i - 1] if i <= len(options) else ""
    return out


def _extract_bank_question_from_text(raw_content: Any) -> dict[str, Any]:
    """
    Best-effort extractor for free-form LLM output when JSON parsing fails.
    Supports:
    - `题干/正确答案/解析/选项1...` key-value lines
    - A/B/C/D style option lines.
    """
    text = str(raw_content or "").strip()
    if not text:
        return {}
    # Strip optional markdown code fences.
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    lines = [str(line).rstrip() for line in text.splitlines()]
    out: dict[str, Any] = {}
    letter_options: dict[str, str] = {}
    current_multiline_key: str | None = None

    def _set_multiline_value(key: str, value: str) -> None:
        nonlocal current_multiline_key
        val = str(value or "").strip()
        if not val:
            return
        out[key] = val
        current_multiline_key = key

    for raw_line in lines:
        line = str(raw_line).strip()
        if not line:
            continue
        line = re.sub(r"^[\-*]\s+", "", line)

        m = re.match(r"^(题干|question|stem|题目)\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
        if m:
            _set_multiline_value("题干", m.group(2))
            continue

        m = re.match(r"^(解析|explanation)\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
        if m:
            _set_multiline_value("解析", m.group(2))
            continue

        m = re.match(r"^(正确答案|答案|answer)\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
        if m:
            out["正确答案"] = str(m.group(2) or "").strip()
            current_multiline_key = None
            continue

        m = re.match(r"^(题目类型|question_type)\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
        if m:
            out["题目类型"] = str(m.group(2) or "").strip()
            current_multiline_key = None
            continue

        m = re.match(r"^(难度值|difficulty|difficulty_score)\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
        if m:
            out["难度值"] = str(m.group(2) or "").strip()
            current_multiline_key = None
            continue

        m = re.match(r"^选项\s*([1-8])\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
        if m:
            out[f"选项{int(m.group(1))}"] = str(m.group(2) or "").strip()
            current_multiline_key = None
            continue

        m = re.match(r"^([A-H])(?:[\.\)、:：]|[\s\-—]+)\s*(.+)$", line, flags=re.IGNORECASE)
        if m:
            letter_options[m.group(1).upper()] = str(m.group(2) or "").strip()
            current_multiline_key = None
            continue

        if current_multiline_key in {"题干", "解析"}:
            out[current_multiline_key] = str(out.get(current_multiline_key, "")).strip() + "\n" + line

    if letter_options:
        for idx, letter in enumerate(("A", "B", "C", "D", "E", "F", "G", "H"), start=1):
            if letter in letter_options:
                out[f"选项{idx}"] = letter_options[letter]
    return out


def _build_bank_optimize_prompt(base_item: dict[str, Any], feedback: str) -> str:
    source = json.dumps(base_item, ensure_ascii=False, indent=2)
    return f"""
你是出题调优助手。请基于“用户反馈”和“当前题目JSON”，输出一个可直接入库的优化后题目 JSON。

要求：
1) 输出格式优先 JSON 对象（不要 markdown、解释或多余文本）。
2) 仅改动与反馈相关内容；若用户未要求，尽量保留题目元数据（例如知识点、来源路径、任务信息等）。
3) 字段使用中文键：题干、选项1~选项8、正确答案、解析、难度值、题目类型 等；如无必要不要新增无关字段。
4) 题目需自洽：题干、选项、正确答案、解析一致，且正确答案可由题干与选项唯一确定。
5) 如果反馈与原题冲突，以反馈为准，但要保证题目质量。
6) 如果确实无法输出 JSON，则仅输出纯文本固定键，不要与 JSON 混合：
   题干: ...
   选项1: ...
   选项2: ...
   正确答案: ...
   解析: ...

用户反馈：
{feedback}

当前题目 JSON：
{source}
"""


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
    origin_lookup = _build_bank_origin_lookup(tenant_id)
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
        _fill_bank_item_origin_fields(item, origin_lookup)
        items.append(item)
    # 题库优先展示最新入库题，避免用户误以为“未展示”。
    items.reverse()
    payload = _paginate(items, page, page_size)
    payload["material_version_id"] = material_version_id
    return _json_response(payload)


@app.get('/api/<tenant_id>/bank/<question_id>')
def api_bank_get_one(tenant_id: str, question_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.read")
    except PermissionError as e:
        return _error(str(e), "无权限访问题库", 403)

    try:
        qid = int(question_id)
    except (TypeError, ValueError):
        return _error("BAD_REQUEST", "question_id is invalid", 400)

    bank = _load_bank(tenant_bank_path(tenant_id))
    if qid < 0 or qid >= len(bank):
        return _error("NOT_FOUND", "题目不存在", 404)

    row = bank[qid]
    if not isinstance(row, dict):
        return _error("NOT_FOUND", "题目不存在", 404)

    item = dict(row)
    item["question_id"] = qid
    origin_lookup = _build_bank_origin_lookup(tenant_id)
    _fill_bank_item_origin_fields(item, origin_lookup)
    return _json_response({"item": item})


@app.post('/api/<tenant_id>/bank/optimize')
def api_bank_optimize(tenant_id: str):
    try:
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限调优题目", 403)

    body = request.get_json(silent=True) or {}
    raw_qid = body.get("question_id", None)
    feedback = str(body.get("feedback", "") or "").strip()
    draft_item = body.get("question")

    if raw_qid in (None, ""):
        return _error("BAD_REQUEST", "question_id is required", 400)
    if not feedback:
        return _error("BAD_REQUEST", "feedback is required", 400)

    try:
        question_id = int(raw_qid)
    except (TypeError, ValueError):
        return _error("BAD_REQUEST", "question_id is invalid", 400)

    bank = _load_bank(tenant_bank_path(tenant_id))
    if question_id < 0 or question_id >= len(bank):
        return _error("NOT_FOUND", "题目不存在", 404)

    base_item = bank[question_id] if isinstance(bank[question_id], dict) else {}
    if isinstance(draft_item, dict) and str(draft_item.get("题干", "") or draft_item.get("question", "")).strip():
        base_item = _normalize_bank_question_item(draft_item, fallback_item=base_item, merge_with_fallback=True)

    prompt = _build_bank_optimize_prompt(base_item, feedback)
    try:
        api_key, base_url, model_name = _resolve_generation_llm_from_primary_key()
        if not api_key:
            return _error("LLM_CONFIG_MISSING", "未配置可用模型 Key，无法进行AI调优", 400)
        content, _, _ = call_llm(
            node_name="bank.optimize",
            prompt=prompt,
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            provider="ait",
            temperature=0.2,
            max_tokens=2500,
            timeout=120,
        )
        parse_err = ""
        try:
            parsed = parse_json_from_response(content)
        except Exception as e:
            parsed = {}
            parse_err = str(e)
        if not isinstance(parsed, dict) or not parsed:
            parsed = _extract_bank_question_from_text(content)
        if not isinstance(parsed, dict) or not parsed:
            raise RuntimeError(f"模型输出不可解析（JSON与文本兜底均失败）: {parse_err or 'invalid output'}")
        optimized = _normalize_bank_question_item(parsed, fallback_item=base_item, merge_with_fallback=True)
    except Exception as e:
        return _error("LLM_OPTIMIZE_FAILED", f"AI调优失败：{str(e)}", 500)

    if not str(optimized.get("题干", "") or "").strip():
        return _error("LLM_INVALID_OUTPUT", "AI调优结果缺少题干", 500)

    return _json_response(
        {
            "question_id": question_id,
            "item": optimized,
        }
    )


@app.post('/api/<tenant_id>/bank/update')
def api_bank_update(tenant_id: str):
    try:
        system_user = _get_system_user()
        _check_tenant_permission(tenant_id, "gen.create")
    except PermissionError as e:
        return _error(str(e), "无权限修改题库题目", 403)

    body = request.get_json(silent=True) or {}
    raw_qid = body.get("question_id", None)
    raw_item = body.get("item")
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    if raw_qid in (None, ""):
        return _error("BAD_REQUEST", "question_id is required", 400)
    if not isinstance(raw_item, dict):
        return _error("BAD_REQUEST", "item is required", 400)

    try:
        question_id = int(raw_qid)
    except (TypeError, ValueError):
        return _error("BAD_REQUEST", "question_id is invalid", 400)

    material_version_id = _resolve_material_version_id(tenant_id, requested_material_version_id)
    if requested_material_version_id and not material_version_id:
        return _error("MATERIAL_NOT_FOUND", "教材版本不存在", 404)

    bank_path = tenant_bank_path(tenant_id)
    with BANK_WRITE_LOCK:
        bank = _load_bank(bank_path)
        if question_id < 0 or question_id >= len(bank):
            return _error("NOT_FOUND", "题目不存在", 404)
        old_item = bank[question_id] if isinstance(bank[question_id], dict) else {}
        new_item = _normalize_bank_question_item(raw_item, merge_with_fallback=False)
        if material_version_id and not str(new_item.get("教材版本ID", "")).strip():
            new_item["教材版本ID"] = material_version_id
        if not str(new_item.get("题干", "") or "").strip():
            return _error("BAD_REQUEST", "题干不能为空", 400)
        bank[question_id] = new_item
        _save_bank(bank_path, bank)

    write_audit_log(
        tenant_id,
        system_user,
        "bank.update.single",
        "question_bank",
        str(question_id),
        before={"stem": str(old_item.get("题干", "") or "").strip()},
        after={"stem": str(new_item.get("题干", "") or "").strip()},
    )
    return _json_response({"updated": 1, "question_id": question_id, "item": new_item})


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
    only_template_official = bool(body.get("only_template_official", False))
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
    origin_lookup = _build_bank_origin_lookup(tenant_id)
    selected_rows = []
    for idx, q in enumerate(bank):
        if idx not in selected_ids or not isinstance(q, dict):
            continue
        if only_template_official:
            has_template_marks = (
                "模板正式题" in q
                or "模板备选题" in q
                or bool(q.get("模板任务"))
                or bool(str(q.get("模板父任务名称", "") or "").strip())
            )
            if has_template_marks and not bool(q.get("模板正式题")):
                continue
        selected_rows.append((idx, q))
    if not selected_rows:
        return _error("BAD_REQUEST", "未命中可导出题目", 400)

    export_rows: list[dict[str, Any]] = []
    fallback_material_version_id = _resolve_material_version_id(tenant_id, "")
    slice_text_index_cache: dict[str, dict[str, str]] = {}

    def _get_slice_text_index(material_version_id: str) -> dict[str, str]:
        mid = str(material_version_id or "").strip()
        if not mid:
            return {}
        if mid in slice_text_index_cache:
            return slice_text_index_cache[mid]
        kb_file = _resolve_slice_file_for_material(tenant_id, mid)
        kb_items = _load_kb_items_from_file(kb_file) if kb_file else []
        out: dict[str, str] = {}
        for item in kb_items:
            if not isinstance(item, dict):
                continue
            path = str(item.get("完整路径", "") or "").strip()
            if not path or path in out:
                continue
            out[path] = _extract_slice_text(item)
        slice_text_index_cache[mid] = out
        return out

    for _, q in selected_rows:
        qx = dict(q)
        _fill_bank_item_origin_fields(qx, origin_lookup)
        q = qx
        path = str(q.get("来源路径", "") or "").strip()
        parts = [p.strip() for p in path.split(" > ") if p.strip()]
        related_paths = _normalize_related_slice_paths(
            q.get("关联切片路径")
            or q.get("related_slice_paths")
            or q.get("critic_basis_paths")
            or q.get("关联切片路径文本")
            or ""
        )
        q_material_version_id = str(q.get("教材版本ID", "") or "").strip() or fallback_material_version_id
        slice_text_index = _get_slice_text_index(q_material_version_id)
        source_slice_text = str(q.get("切片原文", "") or "").strip()
        if not source_slice_text and path:
            source_slice_text = str(slice_text_index.get(path, "") or "").strip()

        all_slice_paths: list[str] = []
        for p in [path] + related_paths:
            pp = str(p or "").strip()
            if pp and pp not in all_slice_paths:
                all_slice_paths.append(pp)
        all_slice_blocks: list[str] = []
        for p in all_slice_paths:
            p_text = source_slice_text if (p == path and source_slice_text) else str(slice_text_index.get(p, "") or "").strip()
            all_slice_blocks.append(f"【{p}】\n{p_text if p_text else '（未找到该切片原文）'}")
        all_slice_text = "\n\n".join(all_slice_blocks)
        mother_full_text = str(
            q.get("参考母题全文", "")
            or q.get("mother_questions_full_text", "")
            or ""
        ).strip()
        if not mother_full_text:
            mother_full_rows = q.get("mother_questions_full")
            if isinstance(mother_full_rows, list) and mother_full_rows:
                blocks: list[str] = []
                for i, row in enumerate(mother_full_rows, start=1):
                    if not isinstance(row, dict):
                        continue
                    stem = str(row.get("题干", "")).strip()
                    options = row.get("选项") if isinstance(row.get("选项"), dict) else {}
                    answer = str(row.get("正确答案", "")).strip()
                    explanation = str(row.get("解析", "")).strip()
                    option_lines = []
                    for key in ("A", "B", "C", "D", "E", "F", "G", "H"):
                        value = str(options.get(key, "") or "").strip()
                        if value:
                            option_lines.append(f"{key}. {value}")
                    blocks.append(
                        f"母题{i}\n题干：{stem or '（无）'}\n选项：\n{chr(10).join(option_lines) if option_lines else '（无）'}\n正确答案：{answer or '（无）'}\n解析：{explanation or '（无）'}"
                    )
                mother_full_text = "\n\n".join(blocks)
        if not mother_full_text:
            mother_full_text = str(q.get("关联母题", "") or q.get("母题题干", "") or "").strip()
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
            "掌握程度": safe_str(q.get("模板掌握度", "")) or safe_str(q.get("掌握程度", "")),
            "题型": _resolve_calc_question_type(q),
            "一级知识点": safe_str(q.get("一级知识点", "")) or (parts[0] if len(parts) > 0 else ""),
            "二级知识点": safe_str(q.get("二级知识点", "")) or (parts[1] if len(parts) > 1 else ""),
            "三级知识点": safe_str(q.get("三级知识点", "")) or (parts[2] if len(parts) > 2 else ""),
            "四级知识点": safe_str(q.get("四级知识点", "")) or (parts[3] if len(parts) > 3 else ""),
            "题目解析": safe_str(q.get("解析", "")),
            "切片原文": source_slice_text,
            "关联切片数量": len(related_paths),
            "关联切片路径": "\n".join(related_paths),
            "全部切片路径": "\n".join(all_slice_paths),
            "全部切片原文": all_slice_text,
            "参考母题全文": mother_full_text,
            "结构化内容": _stringify_structured_value(q.get("结构化内容", "")),
            "出题任务名称": safe_str(q.get("source_task_name", "")),
            "出题任务ID": safe_str(q.get("source_task_id", "")),
            "出题RunID": safe_str(q.get("source_run_id", "")),
            "离线Judge评分": q.get("offline_judge_score"),
            "离线Judge结论": safe_str(q.get("offline_judge_decision", "")),
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
        "掌握程度",
        "题型",
        "一级知识点",
        "二级知识点",
        "三级知识点",
        "四级知识点",
        "题目解析",
        "切片原文",
        "关联切片数量",
        "关联切片路径",
        "全部切片路径",
        "全部切片原文",
        "参考母题全文",
        "结构化内容",
        "出题任务名称",
        "出题任务ID",
        "出题RunID",
        "离线Judge评分",
        "离线Judge结论",
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
    target_provided = "target_mother_question_id" in body
    manual_stem = str(body.get('manual_question_stem', '') or '').strip()
    manual_stem_provided = "manual_question_stem" in body
    manual_explanation = str(body.get('manual_question_explanation', '') or '').strip()
    manual_explanation_provided = "manual_question_explanation" in body
    manual_options_raw = body.get('manual_question_options', [])
    manual_options_provided = "manual_question_options" in body
    if isinstance(manual_options_raw, str):
        manual_options = [x.strip() for x in manual_options_raw.splitlines() if str(x).strip()]
    elif isinstance(manual_options_raw, list):
        manual_options = [str(x or "").strip() for x in manual_options_raw if str(x or "").strip()]
    else:
        manual_options = []
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

    if confirm_status == "approved":
        mapping_path_obj = _resolve_mapping_path_for_material(tenant_id, material_version_id)
        mapping = {}
        if mapping_path_obj:
            try:
                mapping = json.loads(mapping_path_obj.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                mapping = {}
        reviews = _load_mapping_review_for_material(tenant_id, material_version_id)
        history_rows = _load_history_rows(tenant_id)
        not_ready: list[dict[str, Any]] = []
        for mk in map_keys:
            mk_str = str(mk)
            sid_part, sep, qid_part = mk_str.partition(":")
            if not sep or not qid_part.isdigit():
                not_ready.append({"map_key": mk_str, "missing": ["题干", "选项", "解析"]})
                continue
            review_payload = dict(reviews.get(mk_str, {}) or {})
            if target_provided:
                review_payload["target_mother_question_id"] = target
            if manual_stem_provided:
                review_payload["manual_question_stem"] = manual_stem
            if manual_explanation_provided:
                review_payload["manual_question_explanation"] = manual_explanation
            if manual_options_provided:
                review_payload["manual_question_options"] = manual_options

            review_manual_payload = {
                "题干": str(review_payload.get("manual_question_stem", "") or "").strip(),
                "选项": review_payload.get("manual_question_options", []) if isinstance(review_payload.get("manual_question_options", []), list) else [],
                "解析": str(review_payload.get("manual_question_explanation", "") or "").strip(),
                "正确答案": "",
            }
            manual_ready, _ = _is_mapping_review_ready(review_manual_payload)
            if manual_ready:
                candidate_q_row = review_manual_payload
            else:
                effective_qid = qid_part
                target_qid = str(review_payload.get("target_mother_question_id", "") or "").strip()
                if target_qid.isdigit():
                    effective_qid = target_qid
                candidate_q_row = history_rows.get(int(effective_qid), {}) if str(effective_qid).isdigit() else {}
            ready, missing = _is_mapping_review_ready(candidate_q_row if isinstance(candidate_q_row, dict) else {})
            if not ready:
                not_ready.append({"map_key": mk_str, "missing": missing})
        if not_ready:
            sample = not_ready[:5]
            details = "; ".join([f"{x['map_key']} 缺少 {'/'.join(x['missing'])}" for x in sample])
            extra = "" if len(not_ready) <= 5 else f" 等{len(not_ready)}条"
            return _error("MAPPING_NOT_READY", f"以下映射未补全母题题干/选项/解析，不能通过审核：{details}{extra}", 400)

    reviews = _load_mapping_review_for_material(tenant_id, material_version_id)
    updated = 0
    for mk in map_keys:
        existing = reviews.get(str(mk), {}) if isinstance(reviews, dict) else {}
        final_target = target if target_provided else str(existing.get("target_mother_question_id", "") or "")
        final_manual_stem = manual_stem if manual_stem_provided else str(existing.get("manual_question_stem", "") or "")
        final_manual_explanation = (
            manual_explanation if manual_explanation_provided else str(existing.get("manual_question_explanation", "") or "")
        )
        existing_options = existing.get("manual_question_options", []) if isinstance(existing, dict) else []
        if not isinstance(existing_options, list):
            existing_options = []
        final_manual_options = manual_options if manual_options_provided else existing_options
        _upsert_mapping_review_for_material(
            tenant_id=tenant_id,
            material_version_id=material_version_id,
            map_key=str(mk),
            confirm_status=confirm_status,
            reviewer=reviewer,
            comment=comment,
            target_mother_question_id=final_target,
            manual_question_stem=final_manual_stem,
            manual_question_options=final_manual_options,
            manual_question_explanation=final_manual_explanation,
        )
        write_audit_log(tenant_id, reviewer, 'map.confirm.batch', 'slice_question_map', str(mk))
        updated += 1
    return _json_response({'updated': updated, 'material_version_id': material_version_id})


if __name__ == '__main__':
    # Remote deployment is pinned to 127.0.0.1:8600; do not drift via env overrides.
    app.run(host=BACKEND_HOST, port=BACKEND_PORT, debug=False, use_reloader=False, threaded=True)
