import base64
import json

import pytest

from authn import AccessDenied, resolve_principal


def _b64url(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _unsigned_token(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}
    return f"{_b64url(header)}.{_b64url(payload)}."


def test_legacy_header_auth(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "legacy")
    principal = resolve_principal(authorization_header="", system_user_header="admin")
    assert principal.system_user == "admin"
    assert principal.auth_mode == "legacy"
    assert "hz" in principal.tenants


def test_oidc_insecure_auth(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_INSECURE_ALLOW_UNSIGNED", "true")
    token = _unsigned_token(
        {
            "preferred_username": "oidc_user",
            "exp": 4070908800,
            "iss": "https://example-issuer",
            "aud": "admin-web",
            "role": "city_admin",
            "tenants": ["hz"],
            "permissions": ["slice.read", "map.read"],
        }
    )
    principal = resolve_principal(authorization_header=f"Bearer {token}", system_user_header="")
    assert principal.system_user == "oidc_user"
    assert principal.auth_mode == "oidc"
    assert principal.role == "city_admin"
    assert principal.tenants == ["hz"]
    assert "slice.read" in principal.permissions


def test_oidc_requires_token(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_INSECURE_ALLOW_UNSIGNED", "true")
    with pytest.raises(AccessDenied):
        resolve_principal(authorization_header="", system_user_header="admin")
