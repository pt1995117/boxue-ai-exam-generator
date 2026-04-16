from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.request import Request, urlopen

from tenant_context import get_accessible_tenants, get_user_profile
from tenants_config import DEFAULT_TENANTS, ROLE_PERMISSIONS


class AccessDenied(PermissionError):
    pass


@dataclass
class Principal:
    system_user: str
    role: str
    tenants: list[str]
    permissions: set[str]
    auth_mode: str
    claims: Dict[str, Any]


def _b64url_json(data: str) -> Dict[str, Any]:
    pad = "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(data + pad).decode("utf-8")
    obj = json.loads(raw)
    return obj if isinstance(obj, dict) else {}


class OIDCVerifier:
    def __init__(self) -> None:
        self.issuer = os.getenv("OIDC_ISSUER_URL", "").strip()
        self.audience = os.getenv("OIDC_AUDIENCE", "").strip()
        self.allow_insecure = os.getenv("OIDC_INSECURE_ALLOW_UNSIGNED", "false").lower() in {"1", "true", "yes"}
        self._jwks_cache: Dict[str, Any] = {}
        self._jwks_ts = 0.0
        self._jwks_ttl_sec = int(os.getenv("OIDC_JWKS_TTL_SEC", "600"))

    def _jwks_url(self) -> str:
        if not self.issuer:
            raise AccessDenied("OIDC_ISSUER_REQUIRED")
        return self.issuer.rstrip("/") + "/protocol/openid-connect/certs"

    def _fetch_jwks(self) -> Dict[str, Any]:
        now = time.time()
        if self._jwks_cache and now - self._jwks_ts < self._jwks_ttl_sec:
            return self._jwks_cache
        req = Request(self._jwks_url(), headers={"Accept": "application/json"})
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self._jwks_cache = data if isinstance(data, dict) else {}
        self._jwks_ts = now
        return self._jwks_cache

    def decode(self, token: str) -> Dict[str, Any]:
        if self.allow_insecure:
            parts = token.split(".")
            if len(parts) != 3:
                raise AccessDenied("INVALID_TOKEN")
            payload = _b64url_json(parts[1])
            self._validate_claims(payload)
            return payload

        try:
            import jwt  # type: ignore
            from jwt import PyJWKClient  # type: ignore
        except Exception as exc:
            raise AccessDenied(f"OIDC_LIB_MISSING:{exc}") from exc

        jwks_url = self._jwks_url()
        jwk_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=self._jwks_ttl_sec)
        signing_key = jwk_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
            audience=self.audience or None,
            issuer=self.issuer or None,
            options={"verify_aud": bool(self.audience), "verify_iss": bool(self.issuer)},
        )
        return payload if isinstance(payload, dict) else {}

    def _validate_claims(self, payload: Dict[str, Any]) -> None:
        now = int(time.time())
        exp = int(payload.get("exp", now + 1))
        if exp <= now:
            raise AccessDenied("TOKEN_EXPIRED")
        if self.issuer and payload.get("iss") and payload.get("iss") != self.issuer:
            raise AccessDenied("TOKEN_ISSUER_INVALID")
        if self.audience:
            aud = payload.get("aud")
            if isinstance(aud, str) and aud != self.audience:
                raise AccessDenied("TOKEN_AUDIENCE_INVALID")
            if isinstance(aud, list) and self.audience not in aud:
                raise AccessDenied("TOKEN_AUDIENCE_INVALID")


def _parse_permissions(claims: Dict[str, Any], role: str) -> set[str]:
    perms: set[str] = set()
    claim_perms = claims.get("permissions")
    if isinstance(claim_perms, list):
        perms.update(str(x) for x in claim_perms if x)
    scope = claims.get("scope", "")
    if isinstance(scope, str):
        perms.update(x.strip() for x in scope.split() if x.strip())
    if not perms and role in ROLE_PERMISSIONS:
        perms = set(ROLE_PERMISSIONS[role])
    return perms


def _parse_tenants(claims: Dict[str, Any], role: str) -> list[str]:
    tenants = claims.get("tenants") or claims.get("tenant_ids") or []
    out: list[str] = []
    if isinstance(tenants, list):
        out = [str(x).strip() for x in tenants if str(x).strip()]
    elif isinstance(tenants, str):
        out = [x.strip() for x in tenants.split(",") if x.strip()]
    if role == "platform_admin" and not out:
        return list(DEFAULT_TENANTS.keys())
    return out


def _role_from_claims(claims: Dict[str, Any]) -> str:
    role = str(claims.get("role") or "").strip()
    if role:
        return role
    realm_access = claims.get("realm_access") or {}
    roles = realm_access.get("roles") if isinstance(realm_access, dict) else []
    if isinstance(roles, list):
        for candidate in ("platform_admin", "city_admin", "city_teacher", "city_viewer"):
            if candidate in roles:
                return candidate
    return "city_viewer"


def _system_user_from_claims(claims: Dict[str, Any]) -> str:
    for key in ("preferred_username", "system_user", "sub"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    raise AccessDenied("TOKEN_SUBJECT_MISSING")


def _principal_from_oidc(authorization: str) -> Principal:
    if not authorization.startswith("Bearer "):
        raise AccessDenied("TOKEN_REQUIRED")
    token = authorization[7:].strip()
    if not token:
        raise AccessDenied("TOKEN_REQUIRED")
    claims = OIDCVerifier().decode(token)
    role = _role_from_claims(claims)
    system_user = _system_user_from_claims(claims)
    tenants = _parse_tenants(claims, role)
    if not tenants:
        # fallback to local ACL mapping for compatibility
        profile = get_user_profile(system_user)
        if profile:
            tenants = list(profile.get("tenants", []))
            role = str(profile.get("role", role))
    permissions = _parse_permissions(claims, role)
    return Principal(
        system_user=system_user,
        role=role,
        tenants=tenants,
        permissions=permissions,
        auth_mode="oidc",
        claims=claims,
    )


def _principal_from_legacy_header(system_user: str) -> Principal:
    user = system_user.strip()
    if not user:
        raise AccessDenied("UNAUTHORIZED")
    profile = get_user_profile(user)
    role = "city_viewer"
    if profile:
        role = str(profile.get("role", "city_viewer"))
    tenants = get_accessible_tenants(user)
    permissions = set(ROLE_PERMISSIONS.get(role, set()))
    return Principal(
        system_user=user,
        role=role,
        tenants=tenants,
        permissions=permissions,
        auth_mode="legacy",
        claims={},
    )


def resolve_principal(authorization_header: str, system_user_header: str) -> Principal:
    mode = os.getenv("AUTH_MODE", "legacy").strip().lower()
    if mode == "oidc":
        principal = _principal_from_oidc(authorization_header or "")
        if not principal.tenants:
            raise AccessDenied("TENANT_MISSING")
        return principal
    return _principal_from_legacy_header(system_user_header or "")


def resolve_legacy_principal(system_user_header: str) -> Principal:
    return _principal_from_legacy_header(system_user_header or "")


def compute_canary_bucket(system_user: str, salt: str = "") -> int:
    digest = hashlib.sha256(f"{salt}:{system_user}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100
