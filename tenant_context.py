from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from tenants_config import DEFAULT_TENANTS, ROLE_PERMISSIONS, list_tenants

USER_TENANT_FILE = Path("tenant_users.json")


def _default_acl() -> Dict[str, dict]:
    tenant_ids = list(DEFAULT_TENANTS.keys())
    return {
        "admin": {"role": "platform_admin", "tenants": tenant_ids},
        "teacher_hz": {"role": "city_teacher", "tenants": ["hz"]},
        "viewer_hz": {"role": "city_viewer", "tenants": ["hz"]},
    }


def load_acl() -> Dict[str, dict]:
    if USER_TENANT_FILE.exists():
        try:
            data = json.loads(USER_TENANT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
        except json.JSONDecodeError:
            pass
    return _default_acl()


def save_acl(data: Dict[str, dict]) -> None:
    USER_TENANT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def get_user_profile(system_user: str) -> Optional[dict]:
    acl = load_acl()
    profile = acl.get(system_user)
    if not profile:
        return None
    role = profile.get("role", "city_viewer")
    tenants = profile.get("tenants", [])
    if role == "platform_admin":
        tenants = [x["tenant_id"] for x in list_tenants()]
        if not tenants:
            tenants = list(DEFAULT_TENANTS.keys())
    return {"system_user": system_user, "role": role, "tenants": tenants}


def get_accessible_tenants(system_user: str) -> List[str]:
    profile = get_user_profile(system_user)
    if not profile:
        return []
    return list(profile.get("tenants", []))


def enforce_permission(system_user: str, tenant_id: str, perm_code: str) -> None:
    profile = get_user_profile(system_user)
    if not profile:
        raise PermissionError("UNKNOWN_USER")
    if tenant_id not in profile.get("tenants", []):
        raise PermissionError("TENANT_FORBIDDEN")
    role = profile.get("role", "city_viewer")
    if perm_code not in ROLE_PERMISSIONS.get(role, set()):
        raise PermissionError("PERMISSION_DENIED")


def assert_tenant_access(system_user: str, tenant_id: str) -> None:
    profile = get_user_profile(system_user)
    if not profile:
        raise PermissionError("UNKNOWN_USER")
    if tenant_id not in profile.get("tenants", []):
        raise PermissionError("TENANT_FORBIDDEN")
