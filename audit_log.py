from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from db_store import get_store
from tenants_config import tenant_audit_log_path


def write_audit_log(
    tenant_id: str,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    before: Dict[str, Any] | None = None,
    after: Dict[str, Any] | None = None,
) -> None:
    before_obj = before or {}
    after_obj = after or {}
    get_store().write_audit_log(
        tenant_id=tenant_id,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before_json=json.dumps(before_obj, ensure_ascii=False),
        after_json=json.dumps(after_obj, ensure_ascii=False),
    )

    # File backup for compatibility
    path = tenant_audit_log_path(tenant_id)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "actor": actor,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "before": before_obj,
        "after": after_obj,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
