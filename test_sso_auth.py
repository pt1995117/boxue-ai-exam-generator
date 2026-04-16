import json

from sso_auth import SSOManager


def test_sso_binding_and_switch(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    config_dir = runtime_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    binding_file = config_dir / "sso_user_bindings.json"
    binding_file.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "ucid": "u-1",
                        "tenant_id": "hz",
                        "accounts": [
                            {"system_user": "user_a", "is_default": False},
                            {"system_user": "user_b", "is_default": True},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXUE_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("SSO_ENABLED", "true")
    manager = SSOManager()
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

