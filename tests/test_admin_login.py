"""Admin password login, session tokens, and the static X-Admin-Token path."""

from __future__ import annotations

import io
import logging
import time

from fastapi.testclient import TestClient

from simcore.admin_auth import hash_admin_password, issue_admin_session, secrets_equal, verify_admin_password
from simcore.admin_password import main as hash_stdin
from simcore.config import Settings, get_settings
from simcore.main import create_app
from tests.conftest import ADMIN

PASSWORD = "Plaintext-Admin-Password-9f3a"
PASSWORD_HASH = hash_admin_password(PASSWORD)
SECRET = "session-secret-" + ("k" * 32)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "admin_password_hash": PASSWORD_HASH,
        "admin_session_secret": SECRET,
        "admin_session_ttl_seconds": 3600,
        "admin_login_max_failures": 3,
        "admin_login_window_seconds": 600,
        "admin_session_version": 1,
    }
    values.update(overrides)
    get_settings.cache_clear()
    return Settings(_env_file=None, **values)


def _open(frozen, settings: Settings) -> tuple[TestClient, object]:
    app = create_app(settings=settings, base_clock=frozen)
    return TestClient(app), app


def _login(client: TestClient, password: str = PASSWORD, **headers: str):
    return client.post("/v1/admin/login", json={"password": password}, headers=headers or None)


def test_password_hash_does_not_contain_the_password_and_accepts_paste_artifacts() -> None:
    assert PASSWORD not in PASSWORD_HASH
    assert verify_admin_password(PASSWORD, PASSWORD_HASH)
    assert verify_admin_password(" \n" + PASSWORD + "\u200b\u00a0\r\n", PASSWORD_HASH)
    assert not verify_admin_password(PASSWORD + "-no", PASSWORD_HASH)
    other = hash_admin_password(PASSWORD)
    assert other != PASSWORD_HASH
    assert verify_admin_password(PASSWORD, other)
    assert secrets_equal("same", "same")
    assert not secrets_equal("same", "different")
    names = set(Settings.model_fields)
    assert "admin_password" not in names
    assert "password" not in names
    assert "admin_password_hash" in names


def test_stdin_hasher_output(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    hash_stdin()
    written = capsys.readouterr().out
    assert PASSWORD not in written
    assert written.startswith("scrypt$")
    assert verify_admin_password(PASSWORD, written)


def test_login_success_and_bearer_access(db, frozen, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client, _app = _open(frozen, _settings())
    with client:
        denied = client.get("/v1/admin/dashboard")
        assert denied.status_code == 401
        failed = _login(client, "wrong-password")
        assert failed.status_code == 401
        assert failed.json()["error"]["code"] == "unauthorized"
        assert PASSWORD not in failed.text
        signed = _login(client)
        assert signed.status_code == 200, signed.text
        body = signed.json()
        assert body["token_type"] == "Bearer"
        assert body["token"].startswith("simadm1.")
        assert PASSWORD not in body["token"]
        assert PASSWORD not in signed.text
        assert 3500 <= body["expires_in"] <= 3600
        assert body["expires_at"].endswith("Z")
        board = client.get("/v1/admin/dashboard", headers={"Authorization": f"Bearer {body['token']}"})
        assert board.status_code == 200, board.text
        static = client.get("/v1/admin/dashboard", headers=ADMIN)
        assert static.status_code == 200, static.text
        both = client.get(
            "/v1/admin/dashboard",
            headers={"Authorization": "Bearer not-a-session", **ADMIN},
        )
        assert both.status_code == 200, both.text
        player = client.get("/v1/admin/dashboard", headers={"Authorization": "Bearer dev:1"})
        assert player.status_code == 401
    assert PASSWORD not in caplog.text
    dumped = _settings().model_dump()
    assert PASSWORD not in str(dumped)


def test_expired_and_tampered_sessions_are_rejected(db, frozen) -> None:
    client, app = _open(frozen, _settings())
    with client:
        expired_token, _stamp = issue_admin_session(app.state.settings, app.state.admin_sessions, now=1, ttl=30)
        stale = client.get("/v1/admin/dashboard", headers={"Authorization": f"Bearer {expired_token}"})
        assert stale.status_code == 401
        live = _login(client)
        token = live.json()["token"]
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        flipped = client.get("/v1/admin/dashboard", headers={"Authorization": f"Bearer {tampered}"})
        assert flipped.status_code == 401
        app.state.settings.admin_session_version = 2
        bumped = client.get("/v1/admin/dashboard", headers={"Authorization": f"Bearer {token}"})
        assert bumped.status_code == 401
        assert client.get("/v1/admin/dashboard", headers=ADMIN).status_code == 200


def test_logout_revokes_one_session_and_revoke_all_drops_the_rest(db, frozen) -> None:
    client, _app = _open(frozen, _settings())
    with client:
        first = _login(client).json()["token"]
        second = _login(client).json()["token"]
        logged_out = client.post("/v1/admin/logout", headers={"Authorization": f"Bearer {first}"})
        assert logged_out.status_code == 200, logged_out.text
        assert logged_out.json()["revoked"] is True
        assert client.get("/v1/admin/dashboard", headers={"Authorization": f"Bearer {first}"}).status_code == 401
        assert client.get("/v1/admin/dashboard", headers={"Authorization": f"Bearer {second}"}).status_code == 200
        static_logout = client.post("/v1/admin/logout", headers=ADMIN)
        assert static_logout.status_code == 200
        assert static_logout.json()["revoked"] is False
        assert "SIMCORE_ADMIN_SESSION_VERSION" in static_logout.json()["detail"]
        assert client.get("/v1/admin/dashboard", headers=ADMIN).status_code == 200
        revoked = client.post("/v1/admin/sessions/revoke", headers={"Authorization": f"Bearer {second}"})
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revoked"] == "all"
        assert client.get("/v1/admin/dashboard", headers={"Authorization": f"Bearer {second}"}).status_code == 401
        assert client.get("/v1/admin/dashboard", headers=ADMIN).status_code == 200
        fresh = _login(client)
        assert fresh.status_code == 200, fresh.text
        assert client.get(
            "/v1/admin/dashboard",
            headers={"Authorization": f"Bearer {fresh.json()['token']}"},
        ).status_code == 200


def test_login_rate_limit_is_per_address_and_clears_after_success(db, frozen) -> None:
    client, _app = _open(frozen, _settings())
    with client:
        for _ in range(3):
            failed = _login(client, "nope")
            assert failed.status_code == 401
        blocked = _login(client, PASSWORD)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"
        assert PASSWORD not in blocked.text
        assert client.get("/v1/admin/dashboard", headers=ADMIN).status_code == 200
    client, _app = _open(frozen, _settings())
    with client:
        assert _login(client, "nope").status_code == 401
        assert _login(client, "nope").status_code == 401
        assert _login(client).status_code == 200
        assert _login(client, "nope").status_code == 401
        assert _login(client, "nope").status_code == 401
        assert _login(client, "nope").status_code == 401
        assert _login(client).status_code == 429


def test_login_is_disabled_without_a_hash_or_a_usable_secret(db, frozen) -> None:
    client, _app = _open(frozen, _settings(admin_password_hash=""))
    with client:
        missing = _login(client)
        assert missing.status_code == 403
        assert missing.json()["error"]["code"] == "admin_login_disabled"
        assert "not configured" in missing.json()["error"]["message"]
        assert client.get("/v1/admin/dashboard", headers=ADMIN).status_code == 200
    client, _app = _open(frozen, _settings(admin_session_secret=""))
    with client:
        secret = _login(client)
        assert secret.status_code == 403
        assert "session secret" in secret.json()["error"]["message"]
    client, _app = _open(frozen, _settings(admin_password_hash="scrypt$nope"))
    with client:
        broken = _login(client)
        assert broken.status_code == 403
        assert "scrypt" in broken.json()["error"]["message"]


def test_production_login_stays_off_until_admin_is_enabled(db, frozen) -> None:
    hidden = _settings(
        env="production",
        admin_token="generated-token-not-a-default",
        enable_admin=False,
    )
    client, _app = _open(frozen, hidden)
    with client:
        login = _login(client)
        assert login.status_code == 404
        assert login.json()["error"]["code"] == "not_found"
        board = client.get("/v1/admin/dashboard", headers={"X-Admin-Token": "generated-token-not-a-default"})
        assert board.status_code == 404
    shown = _settings(
        env="production",
        admin_token="generated-token-not-a-default",
        enable_admin=True,
    )
    client, _app = _open(frozen, shown)
    with client:
        login = _login(client)
        assert login.status_code == 200, login.text
        board = client.get(
            "/v1/admin/dashboard",
            headers={"Authorization": f"Bearer {login.json()['token']}"},
        )
        assert board.status_code == 200, board.text


def test_session_expiry_follows_wall_clock_not_the_game_clock(db, frozen) -> None:
    client, _app = _open(frozen, _settings(admin_session_ttl_seconds=86_400))
    with client:
        body = _login(client).json()
        expires = time.time() + 86_400
        assert abs(body["expires_in"] - 86_400) < 5
        assert body["expires_at"].startswith(time.strftime("%Y-%m-%d", time.gmtime(expires))[:4])
