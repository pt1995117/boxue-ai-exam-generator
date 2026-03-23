from __future__ import annotations

import json
import math
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
from urllib.parse import quote, urlsplit, urlunsplit

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
    tenant_generation_template_path,
    tenant_mapping_path,
    tenant_root,
    tenant_slices_dir,
    tenant_bank_path,
    upsert_tenant,
)
from tenant_context import get_accessible_tenants, assert_tenant_access, enforce_permission, load_acl, save_acl
from exam_factory import KnowledgeRetriever, set_active_tenant
from exam_graph import app as graph_app, mark_unstable, summarize_llm_trace, detect_router_high_risk_slice
from reference_loader import load_reference_questions

app = Flask(__name__)
init_observability("exam-admin-api")

SLICE_STATUSES = {"pending", "approved"}
MAP_STATUSES = {"pending", "approved"}
QUESTION_TYPES = {"单选题", "多选题", "判断题", "随机"}
GEN_MODES = {"基础概念/理解记忆", "实战应用/推演", "随机"}
ALLOWED_ORIGINS = set(
    x.strip()
    for x in os.getenv(
        "ADMIN_WEB_ORIGINS",
        "http://127.0.0.1:8520,http://localhost:8520,http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:3000,http://localhost:3000",
    ).split(",")
    if x.strip()
)
PRIMARY_KEY_FILE = Path(__file__).resolve().parent / "填写您的Key.txt"
_KEY_PLACEHOLDER_MARKERS = ("请将您的Key", "在这里填写", "your_key", "YOUR_KEY")


def _load_primary_key_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not PRIMARY_KEY_FILE.exists():
        return cfg
    try:
        for line in PRIMARY_KEY_FILE.read_text(encoding="utf-8").splitlines():
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
    return cfg


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
    PRIMARY_KEY_FILE.write_text(normalized, encoding="utf-8")
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
            assignable = min(base, row_remaining[path_prefix], col_remaining[mastery])
            if assignable <= 0:
                continue
            allocation[(path_prefix, mastery)] += assignable
            row_remaining[path_prefix] -= assignable
            col_remaining[mastery] -= assignable

    def _alloc_priority(path_prefix: str, mastery: str) -> tuple[float, float, float, int]:
        score = float(cell_scores.get((path_prefix, mastery), 0.0))
        fractional = score - math.floor(score)
        available = plan_unit_available_by_mastery.get(path_prefix, {}).get(mastery, 0)
        scarcity = 1.0 / max(available, 1)
        route_idx = unit_index_by_prefix.get(path_prefix, 0)
        return (fractional, score, scarcity, -route_idx)

    while any(v > 0 for v in row_remaining.values()):
        progressed = False
        for mastery in GEN_TEMPLATE_MASTERIES:
            while col_remaining.get(mastery, 0) > 0:
                candidates: list[tuple[tuple[float, float, float, int], str]] = []
                for unit in plan_units:
                    path_prefix = str(unit.get("unit_prefix", ""))
                    if row_remaining.get(path_prefix, 0) <= 0:
                        continue
                    if plan_unit_available_by_mastery.get(path_prefix, {}).get(mastery, 0) <= 0:
                        continue
                    candidates.append((_alloc_priority(path_prefix, mastery), path_prefix))
                if not candidates:
                    raise ValueError(f"全局需要 {col_remaining.get(mastery, 0)} 道“{mastery}”题，但可用路由切片不足，无法满足模板占比")
                candidates.sort(reverse=True)
                chosen_path = candidates[0][1]
                allocation[(chosen_path, mastery)] += 1
                row_remaining[chosen_path] -= 1
                col_remaining[mastery] -= 1
                progressed = True
        if not progressed:
            break

    unresolved_rows = [path for path, remain in row_remaining.items() if remain > 0]
    unresolved_cols = [mastery for mastery, remain in col_remaining.items() if remain > 0]
    if unresolved_rows or unresolved_cols:
        details: list[str] = []
        if unresolved_rows:
            details.append("路由剩余未分配: " + ", ".join(f"{path}={row_remaining[path]}" for path in unresolved_rows))
        if unresolved_cols:
            details.append("掌握程度剩余未分配: " + ", ".join(f"{mastery}={col_remaining[mastery]}" for mastery in unresolved_cols))
        raise ValueError("；".join(details) or "模板切片分配失败")

    for route in route_summaries:
        path_prefix = str(route.get("path_prefix", ""))
        for mastery_item in route.get("mastery_breakdown", []):
            mastery = str(mastery_item.get("mastery", ""))
            mastery_item["count"] = int(allocation.get((path_prefix, mastery), 0) or 0)

    planned_slice_ids: list[int] = []
    for unit in plan_units:
        path_prefix = str(unit.get("unit_prefix", ""))
        for mastery in GEN_TEMPLATE_MASTERIES:
            count = int(allocation.get((path_prefix, mastery), 0) or 0)
            if count <= 0:
                continue
            bucket = list(slice_buckets.get((path_prefix, mastery), []))
            random.shuffle(bucket)
            for idx in range(count):
                planned_slice_ids.append(int(bucket[idx % len(bucket)]["slice_id"]))
    random.shuffle(planned_slice_ids)
    return {
        "planned_slice_ids": planned_slice_ids,
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
    for row in _read_jsonl(_qa_gen_tasks_path(tenant_id)):
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
_ORPHAN_GEN_GRACE_SECONDS = max(300, int(os.getenv("ORPHAN_GEN_GRACE_SECONDS", "7200") or 7200))
_ORPHAN_GEN_ZERO_PROGRESS_SECONDS = max(60, int(os.getenv("ORPHAN_GEN_ZERO_PROGRESS_SECONDS", "900") or 900))
_ORPHAN_JUDGE_GRACE_SECONDS = max(120, int(os.getenv("ORPHAN_JUDGE_GRACE_SECONDS", "1800") or 1800))
_TASK_MAINTENANCE_INTERVAL_SECONDS = max(30, int(os.getenv("TASK_MAINTENANCE_INTERVAL_SECONDS", "120") or 120))
_MAINTENANCE_STARTED = False
_MAINTENANCE_LOCK = threading.Lock()


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


def _latest_rows_by_task_id(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(path):
        if not isinstance(row, dict):
            continue
        tid = str(row.get("task_id", "")).strip()
        if tid:
            rows[tid] = row
    return rows


def _build_bank_task_recovery_stats(tenant_id: str) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for row in _load_bank(tenant_bank_path(tenant_id)):
        if not isinstance(row, dict):
            continue
        tid = str(row.get("出题任务ID") or row.get("source_task_id") or row.get("task_id") or "").strip()
        if not tid:
            continue
        run_id = str(row.get("出题RunID") or row.get("source_run_id") or row.get("run_id") or "").strip()
        bucket = stats.setdefault(tid, {"saved_count": 0, "run_ids": set()})
        bucket["saved_count"] = int(bucket.get("saved_count", 0) or 0) + 1
        run_ids = bucket.get("run_ids")
        if run_id and isinstance(run_ids, set):
            run_ids.add(run_id)
    for tid, bucket in stats.items():
        run_ids = bucket.get("run_ids")
        if isinstance(run_ids, set):
            bucket["run_ids"] = sorted([str(x) for x in run_ids if str(x).strip()])
    return stats


def _apply_gen_task_bank_recovery(task: dict[str, Any], bank_stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(task, dict):
        return task
    tid = str(task.get("task_id", "")).strip()
    if not tid:
        return task
    stat = bank_stats.get(tid)
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


def _build_run_questions_from_bank(tenant_id: str, run_id: str) -> list[dict[str, Any]]:
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
                "slice_id": row.get("来源切片ID"),
                "slice_path": str(row.get("来源路径", "") or ""),
                "slice_content": str(row.get("切片原文", "") or ""),
                "final_json": final_json,
            }
        )
    return questions


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
        if not _is_orphan_reconcile_due(task, now_dt, _ORPHAN_GEN_GRACE_SECONDS):
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
    """Mark persisted pending/running judge tasks as failed when no in-memory worker exists."""
    with JUDGE_TASK_LOCK:
        live_ids = {
            str(t.get("task_id", ""))
            for t in JUDGE_TASKS.values()
            if str(t.get("tenant_id", "")) == tenant_id and str(t.get("task_id", ""))
        }
    updates: list[dict[str, Any]] = []
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    for tid, task in list(rows.items()):
        if not isinstance(task, dict):
            continue
        status = str(task.get("status", "") or "")
        # pending may be a valid queued state in serial mode; only running is orphan-sensitive.
        if status != "running":
            continue
        if tid in live_ids:
            continue
        if _is_judge_run_still_active(tenant_id, str(task.get("run_id", "") or ""), tid, _ORPHAN_JUDGE_GRACE_SECONDS):
            continue
        if not _is_orphan_reconcile_due(task, now_dt, _ORPHAN_JUDGE_GRACE_SECONDS):
            continue
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
        gen_rows = _latest_rows_by_task_id(_qa_gen_tasks_path(tenant_id))
        if gen_rows:
            _reconcile_orphan_generate_tasks(tenant_id, gen_rows)
        judge_rows = _latest_rows_by_task_id(_qa_judge_tasks_path(tenant_id))
        if judge_rows:
            _reconcile_orphan_judge_tasks(tenant_id, judge_rows)


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
    """True if this task has been requested to cancel (used by generate loop to exit early)."""
    with GEN_TASK_LOCK:
        t = GEN_TASKS.get(str(task_id or ""))
        return bool(t and t.get("cancel_requested"))


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
    for row in _read_jsonl(_qa_gen_tasks_path(tenant_id)):
        if not isinstance(row, dict):
            continue
        rid = str(row.get("run_id", "") or "").strip()
        if not rid:
            continue
        name = str(row.get("task_name", "") or "").strip()
        if name:
            lookup[rid] = name
    for row in _read_jsonl(_qa_runs_path(tenant_id)):
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
    for row in _read_jsonl(_qa_judge_tasks_path(tenant_id)):
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
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            cwd=str(Path(__file__).resolve().parent),
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

def _trace_to_question_input(
    question_trace: dict[str, Any],
    config_payload: dict[str, Any],
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
        qin_dict = _trace_to_question_input(question_trace, config_payload)
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
        for row in reversed(_read_jsonl(_qa_gen_tasks_path(tenant_id))):
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
        ji = _trace_to_question_input(trace, cfg)
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
    llm_summary = question_trace.get("llm_summary") if isinstance(question_trace.get("llm_summary"), dict) else {}
    unstable_flags = [str(x) for x in (question_trace.get("unstable_flags") or []) if str(x)]
    all_issues = [str(x) for x in (critic_result.get("all_issues") or []) if str(x)]
    quality_issues = [str(x) for x in (critic_result.get("quality_issues") or []) if str(x)]
    missing_conditions = [str(x) for x in (critic_result.get("missing_conditions") or []) if str(x)]
    can_deduce_unique = bool(critic_result.get("can_deduce_unique_answer", False))
    passed = bool(critic_result.get("passed", False))
    saved = bool(question_trace.get("saved", False))
    hard_pass = bool(passed and saved)

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
        "critic_fail_types": [str(x) for x in (critic_result.get("fail_types") or []) if str(x)] if not hard_pass else [],
        "llm_summary": llm_summary,
        "question_text": str(final_json.get("题干", "")),
        "answer": str(final_json.get("正确答案", "")),
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
    # Allow unauthenticated GET for slice images so前端 Markdown 图片渲染不会因缺少头失败
    if request.method == "GET" and re.match(r"^/api/[^/]+/slices/image$", path):
        return None
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
                    'is_calculation_slice': _is_calculation_slice(s),
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
    save_to_bank = bool(body.get("save_to_bank", True))
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    task_id = str(body.get("task_id", "")).strip()
    task_name = str(body.get("task_name", "")).strip()
    if gen_scope_mode not in {"custom", "per_slice"}:
        return _error("BAD_REQUEST", "非法出题范围模式", 400)
    if question_type not in QUESTION_TYPES:
        return _error("BAD_REQUEST", "非法题型", 400)
    if generation_mode not in GEN_MODES:
        return _error("BAD_REQUEST", "非法出题模式", 400)

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
    planned_slice_ids: list[int] = []
    template_plan: dict[str, Any] | None = None
    if template:
        num_questions = int(template.get("question_count", num_questions) or num_questions)
        try:
            template_plan = _build_generation_template_plan(
                question_count=num_questions,
                template=template,
                candidate_slices=candidate_slices,
            )
        except ValueError as e:
            return _error("TEMPLATE_PLAN_INVALID", str(e), 400)
        planned_slice_ids = [int(x) for x in (template_plan.get("planned_slice_ids") or []) if int(x) in candidate_ids]
    else:
        for sid in planned_slice_ids_input:
            try:
                sid_int = int(sid)
            except (TypeError, ValueError):
                continue
            if sid_int in candidate_ids:
                planned_slice_ids.append(sid_int)
        if gen_scope_mode == "per_slice":
            num_questions = len(candidate_ids)
    if planned_slice_ids:
        num_questions = len(planned_slice_ids)
    if num_questions <= 0:
        return _error("BAD_REQUEST", "题量必须大于0", 400)
    target_question_count = num_questions
    # 任务级重试预算：不论模板/非模板，都给足“失败后补位”空间，
    # 否则当 max_attempts==题量 时，只要出现一次 critic 驳回就无法凑齐目标题数。
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
    random_difficulty_buckets = _random_difficulty_buckets() if difficulty == "随机" else []
    attempt_count = 0
    slice_fail_counts: dict[int, int] = {}
    skipped_slice_ids: set[int] = set()
    while len(generated) < target_question_count and attempt_count < max_attempts and not fuse_triggered:
        if task_id and _is_task_cancelled(task_id):
            cancelled_by_user = True
            break
        success_index = len(generated)
        attempt_count += 1
        if attempt_count - 1 < len(planned_slice_ids):
            sid = planned_slice_ids[attempt_count - 1]
        else:
            active_ids = [x for x in candidate_ids if x not in skipped_slice_ids] or list(candidate_ids)
            sid = (
                active_ids[(attempt_count - 1) % len(active_ids)]
                if target_question_count > len(active_ids)
                else random.choice(active_ids)
            )
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
        question_trace: dict[str, Any] = {
            "run_id": run_id,
            "index": attempt_count,
            "target_index": success_index + 1,
            "slice_id": sid,
            "slice_path": str(kb_chunk.get("完整路径", "")),
            "slice_content": _extract_slice_text(kb_chunk),
            "trace_id": trace_id,
            "question_id": question_id,
            "question_type": "",
            "difficulty_range": list(effective_difficulty_range) if effective_difficulty_range else None,
            "steps": [],
            "critic_result": {},
            "saved": False,
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
        critic_seen = False
        critic_passed = False
        attempt_error_info: dict[str, Any] | None = None
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
                            current_run_id += 1  # reroute starts next round; first route remains round 0
                        else:
                            router_seen = True
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
                            question_trace["critic_result"] = critic_result
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
                        question_trace["critic_result"] = {}
                        question_trace.pop("critic_details", None)
                    _emit_node_highlights(node_name, state_update, _append_step)
                    # Stream yields full state after each step; sync llm_trace to avoid duplicates
                    llm_records = state_update.get("llm_trace") or []
                    if isinstance(llm_records, list):
                        question_llm_trace[:] = _merge_llm_trace_records(
                            question_llm_trace,
                            [x for x in llm_records if isinstance(x, dict)],
                        )
                if cancelled_by_user:
                    break
            if q_json and critic_passed:
                final_qt_cn = _resolve_storage_question_type_cn(
                    final_json=q_json,
                    trace_question_type=question_trace.get("question_type"),
                    config_question_type=question_type,
                )
                question_trace["question_type"] = final_qt_cn
                q_json["题目类型"] = final_qt_cn
                q_json["来源路径"] = str(kb_chunk.get("完整路径", ""))
                q_json["来源切片ID"] = sid
                q_json["教材版本ID"] = material_version_id
                if task_id:
                    q_json["出题任务ID"] = task_id
                if task_name:
                    q_json["出题任务名称"] = task_name
                q_json["出题RunID"] = run_id
                _attach_mother_questions_to_question_payload(q_json, mother_questions)
                _attach_mother_question_full_to_question_payload(q_json, mother_full_questions)
                _attach_related_slices_to_question_payload(q_json, question_trace.get("related_slice_paths") or [])
                generated.append(q_json)
                if save_to_bank:
                    try:
                        _append_bank_item(bank_path, q_json)
                        saved += 1
                        saved_current = True
                        _append_step("题目已落库", node="system", level="success")
                    except Exception as e:
                        saved_current = False
                        errors.append(f"第{attempt_count}次尝试落库失败: {e}")
                        _append_step("落库失败", node="system", level="error", detail=str(e))
                _append_step("题目生成成功", node="system", level="success")
            elif q_json and not critic_seen:
                errors.append(f"第{attempt_count}次尝试失败: 未经过 critic 审核")
                _append_step("未经过 critic 审核", node="critic", level="error")
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

        if attempt_error_info and not saved_current:
            slice_fail_counts[sid] = int(slice_fail_counts.get(sid, 0) or 0) + 1
            if slice_fail_counts[sid] >= 2:
                skipped_slice_ids.add(int(sid))
                _append_step(
                    "切片降权跳过",
                    node="system",
                    level="warning",
                    detail=f"slice_id={sid} fail_count={slice_fail_counts[sid]}",
                )
            err_key = str(attempt_error_info.get("error_key", "attempt_failed")).strip() or "attempt_failed"
            category = str(attempt_error_info.get("category", "") or "").strip()
            is_critic_family = err_key.startswith("critic:") or category in {"critic_rejected", "critic_missing"}
            if is_critic_family:
                failure_key_counts[err_key] = int(failure_key_counts.get(err_key, 0) or 0) + 1
                failure_examples.setdefault(err_key, attempt_error_info)
            if is_critic_family and failure_key_counts[err_key] >= fuse_threshold:
                fuse_triggered = True
                example = failure_examples.get(err_key, attempt_error_info)
                fuse_info = {
                    "triggered": True,
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
                errors.append(
                    f"任务熔断：critic 同类错误[{err_key}]在单次任务中已超过{fuse_threshold}次（本次第{failure_key_counts[err_key]}次触发）。"
                    f" 证据：{fuse_info['evidence']}。建议：{fuse_info['solution']}"
                )
                _append_step(
                    "触发熔断",
                    node="system",
                    level="error",
                    detail=f"error_key={err_key} count={failure_key_counts[err_key]} solution={fuse_info['solution']}",
                )
                if task_id:
                    _update_task_live(
                        tenant_id,
                        task_id,
                        {
                            "current_node": "fuse",
                            "current_node_updated_at": datetime.now(timezone.utc).isoformat(),
                            "fuse_info": fuse_info,
                        },
                    )
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
        # Ensure critic result is visible in step list (e.g. rule-based reject before LLM)
        _ensure_critic_step_in_trace(question_trace)
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
                },
                [question_trace],
            )
        if cancelled_by_user:
            errors.append("用户取消")
            break
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

    if fuse_triggered and errors:
        return _json_response(
            {
                "error": {
                    "code": "GENERATION_FUSED",
                    "message": errors[-1],
                },
                "run_id": run_id,
                "generated_count": len(generated),
                "saved_count": saved,
                "errors": errors,
                "fuse_info": fuse_info,
                "process_trace": process_trace,
                "material_version_id": material_version_id,
            },
            status=502,
        )
    if len(generated) < target_question_count and errors:
        return _error("GENERATION_FAILED", f"出题失败：{errors[0]}", 502)

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
    save_to_bank = bool(body.get("save_to_bank", True))
    requested_material_version_id = str(body.get("material_version_id", "")).strip()
    task_id = str(body.get("task_id", "")).strip()
    task_name = str(body.get("task_name", "")).strip()
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
    planned_slice_ids: list[int] = []
    template_plan: dict[str, Any] | None = None
    if template:
        num_questions = int(template.get("question_count", num_questions) or num_questions)
        try:
            template_plan = _build_generation_template_plan(
                question_count=num_questions,
                template=template,
                candidate_slices=candidate_slices,
            )
        except ValueError as e:
            return _error("TEMPLATE_PLAN_INVALID", str(e), 400)
        planned_slice_ids = [int(x) for x in (template_plan.get("planned_slice_ids") or []) if int(x) in candidate_ids]
    else:
        for sid in planned_slice_ids_input:
            try:
                sid_int = int(sid)
            except (TypeError, ValueError):
                continue
            if sid_int in candidate_ids:
                planned_slice_ids.append(sid_int)
        if gen_scope_mode == "per_slice":
            num_questions = len(candidate_ids)
    if planned_slice_ids:
        num_questions = len(planned_slice_ids)
    if num_questions <= 0:
        return _error("BAD_REQUEST", "题量必须大于0", 400)
    target_question_count = num_questions
    # 流式模式与普通模式一致：给足“失败后补位”重试预算，避免一轮失败直接打满尝试上限。
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
            },
        )
        random_difficulty_buckets = _random_difficulty_buckets() if difficulty == "随机" else []
        fuse_threshold = 5
        fuse_triggered = False
        fuse_info: dict[str, Any] | None = None
        failure_key_counts: dict[str, int] = {}
        failure_examples: dict[str, dict[str, Any]] = {}
        attempt_count = 0
        slice_fail_counts: dict[int, int] = {}
        skipped_slice_ids: set[int] = set()
        while len(generated) < target_question_count and attempt_count < max_attempts and not fuse_triggered:
            success_index = len(generated)
            attempt_count += 1
            if attempt_count - 1 < len(planned_slice_ids):
                sid = planned_slice_ids[attempt_count - 1]
            else:
                active_ids = [x for x in candidate_ids if x not in skipped_slice_ids] or list(candidate_ids)
                sid = (
                    active_ids[(attempt_count - 1) % len(active_ids)]
                    if target_question_count > len(active_ids)
                    else random.choice(active_ids)
                )
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
            question_trace: dict[str, Any] = {
                "run_id": run_id,
                "index": attempt_count,
                "target_index": success_index + 1,
                "slice_id": sid,
                "slice_path": str(kb_chunk.get("完整路径", "")),
                "slice_content": _extract_slice_text(kb_chunk),
                "trace_id": trace_id,
                "question_id": question_id,
                "question_type": "",
                "difficulty_range": list(effective_difficulty_range) if effective_difficulty_range else None,
                "steps": [],
                "critic_result": {},
                "saved": False,
            }
            yield _sse(
                "question_start",
                {
                    "index": attempt_count,
                    "target_index": success_index + 1,
                    "slice_id": sid,
                    "slice_path": question_trace["slice_path"],
                    "slice_content": question_trace["slice_content"],
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
            critic_seen = False
            critic_passed = False
            attempt_error_info: dict[str, Any] | None = None
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
                                current_run_id += 1  # reroute starts next round; first route remains round 0
                            else:
                                router_seen = True
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
                                question_trace["critic_result"] = critic_result
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
                            question_trace["critic_result"] = {}
                            question_trace.pop("critic_details", None)
                        _emit_node_highlights(node_name, state_update, _append_step)
                        # Stream yields full state after each step; sync llm_trace to avoid duplicates
                        llm_records = state_update.get("llm_trace") or []
                        if isinstance(llm_records, list):
                            question_llm_trace[:] = _merge_llm_trace_records(
                                question_llm_trace,
                                [x for x in llm_records if isinstance(x, dict)],
                            )
                        while _event_stream_buffer:
                            yield _event_stream_buffer.pop(0)
                if q_json and critic_passed:
                    final_qt_cn = _resolve_storage_question_type_cn(
                        final_json=q_json,
                        trace_question_type=question_trace.get("question_type"),
                        config_question_type=question_type,
                    )
                    question_trace["question_type"] = final_qt_cn
                    q_json["题目类型"] = final_qt_cn
                    q_json["来源路径"] = str(kb_chunk.get("完整路径", ""))
                    q_json["来源切片ID"] = sid
                    q_json["教材版本ID"] = material_version_id
                    if task_id:
                        q_json["出题任务ID"] = task_id
                    if task_name:
                        q_json["出题任务名称"] = task_name
                    q_json["出题RunID"] = run_id
                    _attach_mother_questions_to_question_payload(q_json, mother_questions)
                    _attach_mother_question_full_to_question_payload(q_json, mother_full_questions)
                    _attach_related_slices_to_question_payload(q_json, question_trace.get("related_slice_paths") or [])
                    generated.append(q_json)
                    if save_to_bank:
                        try:
                            _append_bank_item(bank_path, q_json)
                            saved += 1
                            saved_current = True
                            _append_step("题目已落库", node="system", level="success")
                        except Exception as e:
                            saved_current = False
                            errors.append(f"第{attempt_count}次尝试落库失败: {e}")
                            _append_step("落库失败", node="system", level="error", detail=str(e))
                    _append_step("题目生成成功", node="system", level="success")
                elif q_json and not critic_seen:
                    errors.append(f"第{attempt_count}次尝试失败: 未经过 critic 审核")
                    _append_step("未经过 critic 审核", node="critic", level="error")
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

            if attempt_error_info and not saved_current:
                slice_fail_counts[sid] = int(slice_fail_counts.get(sid, 0) or 0) + 1
                if slice_fail_counts[sid] >= 2:
                    skipped_slice_ids.add(int(sid))
                    _append_step(
                        "切片降权跳过",
                        node="system",
                        level="warning",
                        detail=f"slice_id={sid} fail_count={slice_fail_counts[sid]}",
                    )
                err_key = str(attempt_error_info.get("error_key", "attempt_failed")).strip() or "attempt_failed"
                category = str(attempt_error_info.get("category", "") or "").strip()
                is_critic_family = err_key.startswith("critic:") or category in {"critic_rejected", "critic_missing"}
                if is_critic_family:
                    failure_key_counts[err_key] = int(failure_key_counts.get(err_key, 0) or 0) + 1
                    failure_examples.setdefault(err_key, attempt_error_info)
                if is_critic_family and failure_key_counts[err_key] >= fuse_threshold:
                    fuse_triggered = True
                    example = failure_examples.get(err_key, attempt_error_info)
                    fuse_info = {
                        "triggered": True,
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
                    errors.append(
                        f"任务熔断：critic 同类错误[{err_key}]在单次任务中已超过{fuse_threshold}次（本次第{failure_key_counts[err_key]}次触发）。"
                        f" 证据：{fuse_info['evidence']}。建议：{fuse_info['solution']}"
                    )
                    _append_step(
                        "触发熔断",
                        node="system",
                        level="error",
                        detail=f"error_key={err_key} count={failure_key_counts[err_key]} solution={fuse_info['solution']}",
                    )
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
            _ensure_critic_step_in_trace(question_trace)
            process_trace.append(question_trace)
            yield _sse(
                "question_done",
                {
                    "index": attempt_count,
                    "target_index": success_index + 1,
                    "elapsed_ms": elapsed_ms,
                    "item": q_json if isinstance(q_json, dict) and critic_passed else None,
                    "trace": question_trace,
                    "generated_count": len(generated),
                    "saved_count": saved,
                    "error_count": len(errors),
                    "fuse_triggered": fuse_triggered,
                    "fuse_info": fuse_info,
                },
            )
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
        "current_node": str(task.get("current_node", "")),
        "current_node_updated_at": str(task.get("current_node_updated_at", "")),
        "request": {
            "num_questions": int(req.get("num_questions", 0) or 0),
            "question_type": str(req.get("question_type", "")),
            "generation_mode": _normalize_generation_mode(req.get("generation_mode", "")),
            "difficulty": str(req.get("difficulty", "")),
            "template_id": str(req.get("template_id", "")),
            "template_name": str(req.get("template_name", "")),
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
    _update_task_live(tenant_id, task_id, {"status": "running", "started_at": started_at})
    _persist_live_task_snapshot(tenant_id, task_id)
    try:
        body_with_task = dict(body or {})
        body_with_task["task_id"] = task_id
        total = int(body_with_task.get("num_questions", 0) or 0)
        _update_task_live(
            tenant_id,
            task_id,
            {
                "progress": {"current": 0, "total": total},
                "material_version_id": str(body_with_task.get("material_version_id", "")),
            },
        )
        _persist_live_task_snapshot(tenant_id, task_id)

        result_holder: dict[str, Any] = {}

        def _invoke_generate_once() -> None:
            with app.test_request_context(
                f"/api/{tenant_id}/generate",
                method="POST",
                json=body_with_task,
                headers={"X-System-User": system_user},
            ):
                result_holder["resp"] = api_generate_questions(tenant_id)

        t = threading.Thread(target=_invoke_generate_once, daemon=True)
        t.start()
        # No task-level timeout: wait until the batch completes (or fails)
        t.join()

        resp = result_holder.get("resp")
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
        done_payload: dict[str, Any] | None = None
        try:
            done_payload = resp.get_json(silent=True) or {}
        except Exception:
            done_payload = {}
        ended_at = datetime.now(timezone.utc).isoformat()
        if not isinstance(done_payload, dict):
            _update_task_live(
                tenant_id,
                task_id,
                {"status": "failed", "ended_at": ended_at, "errors": ["任务异常结束，未收到完成事件"]},
            )
        else:
            status = "cancelled" if done_payload.get("cancelled") else (
                "completed"
                if bool(done_payload.get("success", False)) or int(done_payload.get("generated_count", 0) or 0) > 0
                else "failed"
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
                    "progress": {
                        "current": len(list(done_payload.get("process_trace") or [])),
                        "total": int(body_with_task.get("num_questions", 0) or 0),
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
    # Persist immediately so pending/running tasks survive process restarts.
    _persist_gen_task(tenant_id, task)
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
        # Persisted task file is append-only; keep latest snapshot for same task_id.
        if tid:
            rows[tid] = task
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
        status = "failed" if error_count > 0 else "completed"
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
    bank_task_stats = _build_bank_task_recovery_stats(tenant_id)
    if bank_task_stats:
        rows = {tid: _apply_gen_task_bank_recovery(task, bank_task_stats) for tid, task in rows.items()}
    items = list(rows.values())
    items.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    items = items[:limit]
    return _json_response({"items": [_build_gen_task_summary(x) for x in items], "total": len(items)})


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
    with GEN_TASK_LOCK:
        task = GEN_TASKS.get(task_id)
        if task and str(task.get("tenant_id", "")) == tenant_id:
            snap = _task_snapshot(task)
            snap["errors"] = _sanitize_task_errors(snap.get("errors"))
            _enrich_task_with_qa_run(tenant_id, snap)
            return _json_response({"task": snap})
    persisted = _read_persisted_task(tenant_id, task_id)
    if isinstance(persisted, dict):
        out = dict(persisted)
        out["errors"] = _sanitize_task_errors(out.get("errors"))
        _enrich_task_with_qa_run(tenant_id, out)
        return _json_response({"task": out})
    return _error("TASK_NOT_FOUND", "任务不存在", 404)


def _get_task_id_by_run_id(tenant_id: str, run_id: str) -> str:
    """Return task_id from gen_tasks for the given run_id (completed task only). Empty if not found."""
    tid, _ = _get_task_id_and_name_by_run_id(tenant_id, run_id)
    return tid


def _get_task_id_and_name_by_run_id(tenant_id: str, run_id: str) -> tuple[str, str]:
    """Return (task_id, task_name) from gen_tasks for the given run_id (completed task only). Empty strings if not found."""
    rid = str(run_id or "").strip()
    if not rid or rid.startswith("run_fail_"):
        return "", ""
    for row in reversed(_read_jsonl(_qa_gen_tasks_path(tenant_id))):
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
) -> list[dict[str, Any]]:
    """Filter QA runs. When success_only=True, exclude run_fail_* (failed-task placeholder runs)."""
    runs = _read_jsonl(_qa_runs_path(tenant_id))
    now_ts = datetime.now(timezone.utc).timestamp()
    out: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_id = str(run.get("run_id", "") or "")
        if success_only and run_id.startswith("run_fail_"):
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
    runs = _filter_qa_runs(
        tenant_id,
        material_version_id=material_version_id,
        days=days,
        success_only=success_only,
    )
    latest_judge_by_run = _load_latest_judge_task_by_run(tenant_id)
    items: list[dict[str, Any]] = []
    for r in runs:
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
        task_id, task_name = _get_task_id_and_name_by_run_id(tenant_id, run_id) if run_id else ("", "")
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
    payload = _paginate(items, page, page_size)
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
        report = _run_offline_judge_for_question(q, config_payload, judge_llm)
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
    for task in _read_jsonl(_qa_judge_tasks_path(tenant_id)):
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id", ""))
        # Persisted task file is append-only; keep latest snapshot for same task_id.
        if tid:
            rows[tid] = task
    items = list(rows.values())
    if run_id_filter:
        items = [x for x in items if str(x.get("run_id", "")) == run_id_filter]
    items.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    items = items[:limit]
    return _json_response({"items": [_build_judge_task_summary(x) for x in items], "total": len(items)})


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
    origin_lookup = _build_bank_origin_lookup(tenant_id)
    selected_rows = [(idx, q) for idx, q in enumerate(bank) if idx in selected_ids and isinstance(q, dict)]
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
    port = int(os.getenv("PORT", "8600").strip() or 8600)
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
