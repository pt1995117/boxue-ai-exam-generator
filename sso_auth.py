from __future__ import annotations

import json
import os
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from runtime_paths import REPO_ROOT, runtime_config_root

CAS_NS = "{http://www.yale.edu/tp/cas}"


class SSOError(RuntimeError):
    pass


@dataclass
class SSOSession:
    sid: str
    ucid: str
    tenant_id: str
    system_user: str
    accounts: list[dict[str, Any]]
    business_token: str
    st: str
    created_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "ucid": self.ucid,
            "tenant_id": self.tenant_id,
            "system_user": self.system_user,
            "accounts": list(self.accounts),
            "business_token": self.business_token,
            "st": self.st,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _safe_return_to(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return "/"
    if not value.startswith("/"):
        return "/"
    if value.startswith("//"):
        return "/"
    return value


def _find_text(root: ET.Element, path: str) -> str:
    node = root.find(path)
    if node is None or node.text is None:
        return ""
    return str(node.text).strip()


class SSOManager:
    def __init__(self) -> None:
        self.enabled = _as_bool(os.getenv("SSO_ENABLED"), False)
        self.login_url = os.getenv("SSO_LOGIN_URL", "https://login.ke.com/login").strip()
        self.logout_url = os.getenv("SSO_LOGOUT_URL", "https://login.ke.com/logout").strip()
        self.validate_url = os.getenv("SSO_VALIDATE_URL", "http://i.login.lianjia.com/serviceValidate").strip()
        self.service_base_url = os.getenv("SSO_SERVICE_BASE_URL", "http://127.0.0.1:8600").strip().rstrip("/")
        self.frontend_base_url = os.getenv("SSO_FRONTEND_BASE_URL", "http://127.0.0.1:8522").strip().rstrip("/")
        self.callback_path = os.getenv("SSO_CALLBACK_PATH", "/api/auth/callback").strip() or "/api/auth/callback"
        self.cookie_name = os.getenv("SSO_COOKIE_NAME", "boxue_sso_sid").strip() or "boxue_sso_sid"
        self.cookie_secure = _as_bool(os.getenv("SSO_COOKIE_SECURE"), False)
        self.session_ttl_sec = max(int(os.getenv("SSO_SESSION_TTL_SEC", "28800") or 28800), 300)
        self._lock = threading.Lock()
        self._sessions: Dict[str, dict[str, Any]] = {}

    def _binding_path(self) -> Path:
        runtime_file = runtime_config_root() / "sso_user_bindings.json"
        repo_file = REPO_ROOT / "sso_user_bindings.json"
        if runtime_file.exists():
            return runtime_file
        return repo_file

    def _load_bindings(self) -> dict[str, dict[str, Any]]:
        path = self._binding_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        items = payload.get("users") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for raw in items:
            if not isinstance(raw, dict):
                continue
            ucid = str(raw.get("ucid", "")).strip()
            tenant_id = str(raw.get("tenant_id", "")).strip().lower()
            if not ucid or not tenant_id:
                continue
            accounts_raw = raw.get("accounts")
            accounts: list[dict[str, Any]] = []
            if isinstance(accounts_raw, list):
                for account in accounts_raw:
                    if not isinstance(account, dict):
                        continue
                    system_user = str(account.get("system_user", "")).strip()
                    if not system_user:
                        continue
                    accounts.append(
                        {
                            "system_user": system_user,
                            "is_default": bool(account.get("is_default", False)),
                        }
                    )
            if not accounts:
                system_user = str(raw.get("system_user", "")).strip()
                if system_user:
                    accounts.append({"system_user": system_user, "is_default": True})
            if not accounts:
                continue
            out[ucid] = {
                "ucid": ucid,
                "tenant_id": tenant_id,
                "accounts": accounts,
            }
        return out

    def resolve_binding(self, ucid: str) -> Optional[dict[str, Any]]:
        return self._load_bindings().get(str(ucid).strip())

    def _pick_default_system_user(self, accounts: list[dict[str, Any]]) -> str:
        for account in accounts:
            if bool(account.get("is_default")):
                return str(account.get("system_user", "")).strip()
        return str(accounts[0].get("system_user", "")).strip()

    def _purge_expired_sessions(self) -> None:
        now = time.time()
        expired = [sid for sid, item in self._sessions.items() if float(item.get("expires_at", 0)) <= now]
        for sid in expired:
            self._sessions.pop(sid, None)

    def create_session(self, *, ucid: str, tenant_id: str, accounts: list[dict[str, Any]], st: str, business_token: str) -> SSOSession:
        system_user = self._pick_default_system_user(accounts)
        if not system_user:
            raise SSOError("NO_SYSTEM_USER")
        now = time.time()
        sid = uuid.uuid4().hex
        session = SSOSession(
            sid=sid,
            ucid=str(ucid).strip(),
            tenant_id=str(tenant_id).strip(),
            system_user=system_user,
            accounts=list(accounts),
            business_token=str(business_token or "").strip(),
            st=str(st or "").strip(),
            created_at=now,
            expires_at=now + float(self.session_ttl_sec),
        )
        with self._lock:
            self._purge_expired_sessions()
            self._sessions[sid] = session.to_dict()
        return session

    def get_session(self, sid: str) -> Optional[dict[str, Any]]:
        key = str(sid or "").strip()
        if not key:
            return None
        with self._lock:
            self._purge_expired_sessions()
            item = self._sessions.get(key)
            return dict(item) if item else None

    def clear_session(self, sid: str) -> None:
        key = str(sid or "").strip()
        if not key:
            return
        with self._lock:
            self._sessions.pop(key, None)

    def switch_system_user(self, sid: str, system_user: str) -> dict[str, Any]:
        key = str(sid or "").strip()
        target = str(system_user or "").strip()
        if not key or not target:
            raise SSOError("BAD_REQUEST")
        with self._lock:
            item = self._sessions.get(key)
            if not item:
                raise SSOError("SESSION_NOT_FOUND")
            users = {str(x.get("system_user", "")).strip() for x in item.get("accounts", []) if isinstance(x, dict)}
            if target not in users:
                raise SSOError("SYSTEM_USER_FORBIDDEN")
            item["system_user"] = target
            self._sessions[key] = item
            return dict(item)

    def frontend_redirect_url(self, return_to: str) -> str:
        return urljoin(self.frontend_base_url + "/", _safe_return_to(return_to).lstrip("/"))

    def service_url(self, return_to: str) -> str:
        rt = _safe_return_to(return_to)
        query = urlencode({"rt": rt})
        return f"{self.service_base_url}{self.callback_path}?{query}"

    def login_redirect_url(self, return_to: str, level: str = "") -> str:
        params = {"service": self.service_url(return_to)}
        if str(level or "").strip():
            params["level"] = str(level).strip()
        return f"{self.login_url}?{urlencode(params)}"

    def logout_redirect_url(self, return_to: str) -> str:
        service = self.frontend_redirect_url(return_to)
        return f"{self.logout_url}?{urlencode({'service': service})}"

    def validate_ticket(self, *, ticket: str, service: str) -> dict[str, Any]:
        t = str(ticket or "").strip()
        s = str(service or "").strip()
        if not t or not s:
            raise SSOError("INVALID_REQUEST")
        url = f"{self.validate_url}?{urlencode({'ticket': t, 'service': s})}"
        req = Request(url, headers={"Accept": "application/xml"})
        try:
            with urlopen(req, timeout=5) as resp:
                xml_text = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            raise SSOError(f"CAS_VALIDATE_FAILED:{exc}") from exc
        try:
            root = ET.fromstring(xml_text)
        except Exception as exc:
            raise SSOError("CAS_XML_PARSE_FAILED") from exc
        failure = root.find(f".//{CAS_NS}authenticationFailure")
        if failure is not None:
            code = str(failure.attrib.get("code", "")).strip() or "CAS_AUTH_FAILURE"
            message = str((failure.text or "")).strip()
            raise SSOError(f"{code}:{message}" if message else code)
        success = root.find(f".//{CAS_NS}authenticationSuccess")
        if success is None:
            raise SSOError("CAS_AUTH_SUCCESS_MISSING")
        attrs = success.find(f"{CAS_NS}attributes")
        ucid = ""
        business_token = ""
        if attrs is not None:
            ucid = _find_text(attrs, f"{CAS_NS}ucid")
            business_token = _find_text(attrs, f"{CAS_NS}businessToken")
        if not ucid:
            ucid = _find_text(success, f"{CAS_NS}user")
        if not ucid:
            raise SSOError("UCID_MISSING")
        return {
            "ucid": ucid,
            "business_token": business_token,
        }
