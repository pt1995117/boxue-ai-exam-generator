import json
import time
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

import pytest

from sso_auth import SSOError, SSOManager, _safe_return_to


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _make_manager(tmp_path, monkeypatch, *, ttl: int = 3600) -> SSOManager:
    """创建一个指向临时目录的 SSOManager，使用 SQLite。"""
    runtime_dir = tmp_path / "runtime"
    config_dir = runtime_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    db_dir = runtime_dir / "db"
    db_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("BOXUE_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("SSO_ENABLED", "true")
    monkeypatch.setenv("SSO_SESSION_TTL_SEC", str(ttl))

    # 让 DBStore 写到临时目录
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_dir}/test.db")

    # 清除模块级缓存，让 get_store() 重建
    import db_store as _ds
    _ds._store = None

    return SSOManager()


def _write_bindings(config_dir, users: list) -> None:
    (config_dir / "sso_user_bindings.json").write_text(
        json.dumps({"users": users}, ensure_ascii=False),
        encoding="utf-8",
    )


# ── _safe_return_to ───────────────────────────────────────────────────────────

class TestSafeReturnTo:
    def test_empty_returns_slash(self):
        assert _safe_return_to("") == "/"

    def test_simple_path(self):
        assert _safe_return_to("/dashboard") == "/dashboard"

    def test_blocks_absolute_url(self):
        assert _safe_return_to("https://evil.com") == "/"

    def test_blocks_double_slash(self):
        assert _safe_return_to("//evil.com/path") == "/"

    def test_blocks_url_encoded_double_slash(self):
        # %2f%2f 解码后是 //，必须被拦截
        assert _safe_return_to("%2f%2fevil.com") == "/"

    def test_blocks_crlf(self):
        result = _safe_return_to("/path\r\nX-Injected: header")
        assert "\r" not in result
        assert "\n" not in result

    def test_path_with_query(self):
        assert _safe_return_to("/page?tab=1") == "/page?tab=1"

    def test_none_like_empty(self):
        assert _safe_return_to(None) == "/"  # type: ignore


# ── 绑定与会话基础流程 ─────────────────────────────────────────────────────────

class TestBindingAndSession:
    def test_binding_and_switch(self, tmp_path, monkeypatch):
        manager = _make_manager(tmp_path, monkeypatch)
        config_dir = tmp_path / "runtime" / "config"
        _write_bindings(config_dir, [
            {
                "ucid": "u-1",
                "tenant_id": "hz",
                "accounts": [
                    {"system_user": "user_a", "is_default": False},
                    {"system_user": "user_b", "is_default": True},
                ],
            }
        ])

        binding = manager.resolve_binding("u-1")
        assert binding is not None
        session = manager.create_session(
            ucid="u-1",
            tenant_id="hz",
            accounts=binding["accounts"],
            st="ST-1",
            business_token="2.01abc",
        )
        assert session.system_user == "user_b"

        switched = manager.switch_system_user(session.sid, "user_a")
        assert switched["system_user"] == "user_a"

    def test_switch_to_unbound_user_raises(self, tmp_path, monkeypatch):
        manager = _make_manager(tmp_path, monkeypatch)
        config_dir = tmp_path / "runtime" / "config"
        _write_bindings(config_dir, [
            {"ucid": "u-2", "tenant_id": "bj", "accounts": [{"system_user": "alice", "is_default": True}]}
        ])
        binding = manager.resolve_binding("u-2")
        session = manager.create_session(
            ucid="u-2", tenant_id="bj",
            accounts=binding["accounts"], st="ST-2", business_token="",
        )
        with pytest.raises(SSOError, match="SYSTEM_USER_FORBIDDEN"):
            manager.switch_system_user(session.sid, "hacker")

    def test_resolve_unknown_ucid_returns_none(self, tmp_path, monkeypatch):
        manager = _make_manager(tmp_path, monkeypatch)
        config_dir = tmp_path / "runtime" / "config"
        _write_bindings(config_dir, [])
        assert manager.resolve_binding("nobody") is None

    def test_no_accounts_raises(self, tmp_path, monkeypatch):
        manager = _make_manager(tmp_path, monkeypatch)
        with pytest.raises(SSOError, match="NO_SYSTEM_USER"):
            manager.create_session(
                ucid="u-x", tenant_id="sh",
                accounts=[], st="ST-x", business_token="",
            )


# ── 会话持久化 ────────────────────────────────────────────────────────────────

class TestSessionPersistence:
    def test_session_survives_new_manager_instance(self, tmp_path, monkeypatch):
        """会话写入 DB，新实例也能读到。"""
        manager1 = _make_manager(tmp_path, monkeypatch)
        session = manager1.create_session(
            ucid="u-p", tenant_id="wh",
            accounts=[{"system_user": "bob", "is_default": True}],
            st="ST-p", business_token="tok",
        )
        sid = session.sid

        # 清除内存缓存，模拟重启
        import db_store as _ds
        _ds._store = None

        manager2 = SSOManager()
        loaded = manager2.get_session(sid)
        assert loaded is not None
        assert loaded["system_user"] == "bob"

    def test_get_nonexistent_session_returns_none(self, tmp_path, monkeypatch):
        manager = _make_manager(tmp_path, monkeypatch)
        assert manager.get_session("nonexistent-sid") is None

    def test_clear_session_removes_from_db(self, tmp_path, monkeypatch):
        manager = _make_manager(tmp_path, monkeypatch)
        session = manager.create_session(
            ucid="u-c", tenant_id="cd",
            accounts=[{"system_user": "carol", "is_default": True}],
            st="ST-c", business_token="",
        )
        manager.clear_session(session.sid)
        assert manager.get_session(session.sid) is None


# ── 会话自动续期 ──────────────────────────────────────────────────────────────

class TestSessionRenewal:
    def test_session_renewed_when_near_expiry(self, tmp_path, monkeypatch):
        """会话剩余时间不足 1 小时时，get_session 应自动续期。"""
        manager = _make_manager(tmp_path, monkeypatch, ttl=7200)
        session = manager.create_session(
            ucid="u-r", tenant_id="gz",
            accounts=[{"system_user": "dave", "is_default": True}],
            st="ST-r", business_token="",
        )
        # 手动把 expires_at 设到 30 分钟后（低于 1 小时续期阈值）
        near_expiry = time.time() + 1800
        manager._get_store().refresh_sso_session(session.sid, near_expiry)

        loaded = manager.get_session(session.sid)
        assert loaded is not None
        # 续期后 expires_at 应接近 now + ttl (7200)
        assert loaded["expires_at"] > time.time() + 3600

    def test_session_not_renewed_when_plenty_of_time(self, tmp_path, monkeypatch):
        """会话剩余时间充足时，expires_at 不变。"""
        manager = _make_manager(tmp_path, monkeypatch, ttl=7200)
        session = manager.create_session(
            ucid="u-s", tenant_id="sh",
            accounts=[{"system_user": "eve", "is_default": True}],
            st="ST-s", business_token="",
        )
        original_expires = session.expires_at
        loaded = manager.get_session(session.sid)
        assert loaded is not None
        # 未续期，expires_at 变化不超过 1 秒
        assert abs(loaded["expires_at"] - original_expires) < 1


# ── 会话过期 ──────────────────────────────────────────────────────────────────

class TestSessionExpiry:
    def test_expired_session_not_returned(self, tmp_path, monkeypatch):
        """过期 session 不应被 get_session 返回。"""
        manager = _make_manager(tmp_path, monkeypatch, ttl=1)
        session = manager.create_session(
            ucid="u-e", tenant_id="cq",
            accounts=[{"system_user": "frank", "is_default": True}],
            st="ST-e", business_token="",
        )
        # 把 expires_at 强制设到过去
        manager._get_store().refresh_sso_session(session.sid, time.time() - 1)
        assert manager.get_session(session.sid) is None


# ── validate_ticket ───────────────────────────────────────────────────────────

CAS_SUCCESS_XML = """<?xml version="1.0"?>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationSuccess>
    <cas:user>u-cas</cas:user>
    <cas:attributes>
      <cas:ucid>u-cas</cas:ucid>
      <cas:businessToken>2.01xyz</cas:businessToken>
    </cas:attributes>
  </cas:authenticationSuccess>
</cas:serviceResponse>"""

CAS_FAILURE_XML = """<?xml version="1.0"?>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationFailure code="INVALID_TICKET">Ticket expired</cas:authenticationFailure>
</cas:serviceResponse>"""

CAS_SUCCESS_NO_ATTRS_XML = """<?xml version="1.0"?>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationSuccess>
    <cas:user>fallback-user</cas:user>
  </cas:authenticationSuccess>
</cas:serviceResponse>"""


class TestValidateTicket:
    def _manager(self) -> SSOManager:
        return SSOManager()

    def test_success_parses_ucid_and_token(self):
        manager = self._manager()
        with patch("sso_auth._urlopen_with_retry", return_value=CAS_SUCCESS_XML.encode()):
            result = manager.validate_ticket(ticket="ST-ok", service="http://svc/cb")
        assert result["ucid"] == "u-cas"
        assert result["business_token"] == "2.01xyz"

    def test_success_falls_back_to_user_element(self):
        manager = self._manager()
        with patch("sso_auth._urlopen_with_retry", return_value=CAS_SUCCESS_NO_ATTRS_XML.encode()):
            result = manager.validate_ticket(ticket="ST-fb", service="http://svc/cb")
        assert result["ucid"] == "fallback-user"

    def test_failure_raises_sso_error(self):
        manager = self._manager()
        with patch("sso_auth._urlopen_with_retry", return_value=CAS_FAILURE_XML.encode()):
            with pytest.raises(SSOError, match="INVALID_TICKET"):
                manager.validate_ticket(ticket="ST-bad", service="http://svc/cb")

    def test_network_error_raises_sso_error(self):
        from urllib.error import URLError
        manager = self._manager()
        with patch("sso_auth._urlopen_with_retry", side_effect=URLError("timeout")):
            with pytest.raises(SSOError, match="CAS_VALIDATE_FAILED"):
                manager.validate_ticket(ticket="ST-net", service="http://svc/cb")

    def test_invalid_xml_raises_sso_error(self):
        manager = self._manager()
        with patch("sso_auth._urlopen_with_retry", return_value=b"not xml at all"):
            with pytest.raises(SSOError, match="CAS_XML_PARSE_FAILED"):
                manager.validate_ticket(ticket="ST-xml", service="http://svc/cb")

    def test_missing_ucid_raises_sso_error(self):
        xml = """<?xml version="1.0"?>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationSuccess/>
</cas:serviceResponse>"""
        manager = self._manager()
        with patch("sso_auth._urlopen_with_retry", return_value=xml.encode()):
            with pytest.raises(SSOError, match="UCID_MISSING"):
                manager.validate_ticket(ticket="ST-noucid", service="http://svc/cb")

    def test_empty_ticket_raises_sso_error(self):
        manager = self._manager()
        with pytest.raises(SSOError, match="INVALID_REQUEST"):
            manager.validate_ticket(ticket="", service="http://svc/cb")
