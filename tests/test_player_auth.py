"""Player accounts, tokens, admin account controls, and snapshot exclusion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from simcore.config import Settings, get_settings
from simcore.db import get_engine, get_sessionmaker, reset_engine
from simcore.main import create_app
from simcore.models import CommandIdempotency, PlayerAccount, PlayerCommand, PlayerRefreshSession
from simcore.player_auth import issue_access_token
from simcore.runtime_secrets import ensure_player_token_secret
from simcore.snapshot import capture_document, world_checksum
from tests.conftest import ADMIN
from tests.world import create_scenario

PASSWORD = "correct-horse-battery"
OTHER = "another-horse-battery"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient, username: str, password: str = PASSWORD, email: str | None = None) -> dict:
    body: dict[str, object] = {"username": username, "password": password}
    if email is not None:
        body["email"] = email
    response = client.post("/v1/auth/register", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _production_app(frozen) -> TestClient:
    settings = Settings(
        _env_file=None,
        env="production",
        admin_token="generated-token-not-a-default-value",
        player_token_secret="p" * 48,
        enable_dev_login=False,
        enable_admin=False,
        monitor_api_sampler=False,
        monitor_sample_seconds=0,
    )
    return TestClient(create_app(settings=settings, base_clock=frozen))


def test_register_login_refresh_logout_and_me(client) -> None:
    registered = _register(client, "Ada", email="Ada@Example.com")
    assert registered["token_type"] == "bearer"
    assert registered["must_change_password"] is False
    me = client.get("/v1/auth/me", headers=_auth(registered["access_token"]))
    assert me.status_code == 200
    assert me.json()["username"] == "Ada"
    assert me.json()["email"] == "Ada@Example.com"

    by_email = client.post("/v1/auth/login", json={"username": "ada@example.com", "password": PASSWORD})
    assert by_email.status_code == 200, by_email.text
    first_refresh = registered["refresh_token"]
    rotated = client.post("/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refresh_token"] != first_refresh

    reused = client.post("/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert reused.status_code == 401
    assert reused.json()["error"]["code"] == "invalid_refresh"
    family = client.post("/v1/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]})
    assert family.status_code == 401
    dead = client.get("/v1/auth/me", headers=_auth(rotated.json()["access_token"]))
    assert dead.status_code == 401

    fresh = client.post("/v1/auth/login", json={"username": "ada", "password": PASSWORD})
    assert fresh.status_code == 200, fresh.text
    logged_out = client.post("/v1/auth/logout", headers=_auth(fresh.json()["access_token"]))
    assert logged_out.status_code == 200
    assert client.get("/v1/me", headers=_auth(fresh.json()["access_token"])).status_code == 401

    again = client.post("/v1/auth/login", json={"username": "Ada", "password": PASSWORD}).json()
    other = client.post("/v1/auth/login", json={"username": "Ada", "password": PASSWORD}).json()
    logout_all = client.post("/v1/auth/logout-all", headers=_auth(again["access_token"]))
    assert logout_all.status_code == 200
    assert logout_all.json()["revoked_sessions"] >= 1
    assert client.get("/v1/auth/me", headers=_auth(again["access_token"])).status_code == 401
    assert client.get("/v1/auth/me", headers=_auth(other["access_token"])).status_code == 401


def test_password_policy_and_login_does_not_reveal_the_account(client) -> None:
    _register(client, "Ada")
    taken = client.post("/v1/auth/register", json={"username": "ada", "password": OTHER})
    assert taken.status_code == 409
    assert taken.json()["error"]["code"] == "username_taken"
    short = client.post("/v1/auth/register", json={"username": "Bea", "password": "short"})
    assert short.status_code == 400
    assert short.json()["error"]["code"] == "weak_password"
    common = client.post("/v1/auth/register", json={"username": "Bea", "password": "password123"})
    assert common.status_code == 400
    known = client.post("/v1/auth/login", json={"username": "Ada", "password": "not-the-password"})
    unknown = client.post("/v1/auth/login", json={"username": "nobody-here", "password": PASSWORD})
    assert known.status_code == 401
    assert unknown.status_code == 401
    assert known.json() == unknown.json()
    assert known.json()["error"]["code"] == "invalid_credentials"
    missing = client.get("/v1/me")
    assert missing.status_code == 401


def test_expired_forged_and_cross_player_tokens(client, frozen) -> None:
    world = create_scenario(frozen.now())
    ada = _register(client, "Ada")
    expired, _claims = issue_access_token(
        client.app.state.settings,
        player_id=ada["player_id"],
        account_id=1,
        session_id=1,
        now=1_700_000_000,
        ttl=-120,
    )
    assert client.get("/v1/me", headers=_auth(expired)).status_code == 401
    forged = ada["access_token"][:-1] + ("A" if ada["access_token"][-1] != "A" else "B")
    assert client.get("/v1/me", headers=_auth(forged)).status_code == 401

    bob = client.post("/v1/auth/dev-login", json={"name": "Bob"})
    assert bob.status_code == 200, bob.text
    attacked = client.post(
        "/v1/commands/attack",
        headers=_auth(ada["access_token"]),
        json={"army_id": world["bob_army"], "target_city_id": world["alice_city"], "player_id": world["bob_id"]},
    )
    assert attacked.status_code == 403
    assert attacked.json()["error"]["code"] == "forbidden"


def test_idempotent_replay_returns_the_original_result(client, frozen) -> None:
    world = create_scenario(frozen.now())
    alice = client.post("/v1/auth/dev-login", json={"name": "Alice"})
    headers = _auth(alice.json()["token"])
    headers["Idempotency-Key"] = "march-once"
    payload = {"army_id": world["alice_army"], "target_city_id": world["bob_city"], "player_id": 999}
    first = client.post("/v1/commands/attack", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    second = client.post("/v1/commands/attack", headers=headers, json=payload)
    assert second.status_code == 200
    assert second.json() == first.json()
    session = get_sessionmaker()()
    try:
        commands = session.scalar(select(func.count()).select_from(PlayerCommand))
        keys = session.scalar(select(func.count()).select_from(CommandIdempotency))
    finally:
        session.close()
    assert commands == 1
    assert keys == 1
    conflict = client.post(
        "/v1/commands/attack",
        headers=headers,
        json={"army_id": world["alice_army"], "target_city_id": world["alice_city"]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_login_and_command_rate_limits(client, frozen) -> None:
    settings = Settings(
        _env_file=None,
        env="development",
        player_login_max_failures=2,
        player_login_ip_max_failures=3,
        player_login_window_seconds=900,
        player_login_lockout_seconds=900,
        command_rate_limit=2,
        command_rate_window_seconds=60,
        monitor_api_sampler=False,
        monitor_sample_seconds=0,
    )
    with TestClient(create_app(settings=settings, base_clock=frozen)) as limited:
        _register(limited, "Ada")
        for _ in range(2):
            failed = limited.post("/v1/auth/login", json={"username": "Ada", "password": "not-the-password"})
            assert failed.status_code == 401
        locked = limited.post("/v1/auth/login", json={"username": "Ada", "password": PASSWORD})
        assert locked.status_code == 401
        assert locked.json()["error"]["code"] == "invalid_credentials"
        audit = client.get("/v1/admin/audit", params={"action": "auth.lockout"}, headers=ADMIN)
        assert audit.status_code == 200
        assert any(row["action"] == "auth.lockout" for row in audit.json()["entries"])

        limited_ip = Settings(
            _env_file=None,
            env="development",
            player_login_max_failures=20,
            player_login_ip_max_failures=2,
            command_rate_limit=2,
            monitor_api_sampler=False,
            monitor_sample_seconds=0,
        )
        with TestClient(create_app(settings=limited_ip, base_clock=frozen)) as ip_app:
            for index in range(2):
                missed = ip_app.post(
                    "/v1/auth/login",
                    json={"username": f"missing-{index}", "password": "not-the-password"},
                )
                assert missed.status_code == 401
            blocked = ip_app.post(
                "/v1/auth/login",
                json={"username": "missing-last", "password": "not-the-password"},
            )
            assert blocked.status_code == 429
            assert blocked.json()["error"]["code"] == "rate_limited"

        token = _register(limited, "Bea")["access_token"]
        headers = _auth(token)
        for _ in range(2):
            attempt = limited.post(
                "/v1/commands/attack",
                headers=headers,
                json={"army_id": 999999, "target_city_id": 1},
            )
            assert attempt.status_code != 429
        limited_command = limited.post(
            "/v1/commands/attack",
            headers=headers,
            json={"army_id": 999999, "target_city_id": 1},
        )
        assert limited_command.status_code == 429
        assert limited_command.json()["error"]["code"] == "rate_limited"


def test_admin_claims_a_dev_player_and_forces_a_password_change(client, frozen) -> None:
    world = create_scenario(frozen.now())
    listed = client.get("/v1/admin/accounts", params={"q": "alice"}, headers=ADMIN)
    assert listed.status_code == 200, listed.text
    row = listed.json()["accounts"][0]
    assert row["player_id"] == world["alice_id"]
    assert row["has_password"] is False
    claimed = client.post(
        "/v1/admin/accounts/temporary-password",
        headers=ADMIN,
        json={"player_id": world["alice_id"], "password": "temporary-pass-1"},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["must_change_password"] is True
    assert "password" not in claimed.json()
    logged = client.post("/v1/auth/login", json={"username": "Alice", "password": "temporary-pass-1"})
    assert logged.status_code == 200, logged.text
    assert logged.json()["must_change_password"] is True
    token = logged.json()["access_token"]
    blocked = client.get("/v1/me", headers=_auth(token))
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "password_change_required"
    profile = client.get("/v1/auth/me", headers=_auth(token))
    assert profile.status_code == 200
    changed = client.post(
        "/v1/auth/change-password",
        headers=_auth(token),
        json={"current_password": "temporary-pass-1", "new_password": PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["must_change_password"] is False
    cities = client.get("/v1/me/cities", headers=_auth(changed.json()["access_token"]))
    assert cities.status_code == 200
    assert cities.json()["cities"]

    account_id = claimed.json()["account_id"]
    sessions = client.get(f"/v1/admin/accounts/{account_id}/sessions", headers=ADMIN)
    assert sessions.status_code == 200
    assert sessions.json()["sessions"]
    assert "token_hash" not in json.dumps(sessions.json())
    locked = client.post(f"/v1/admin/accounts/{account_id}/lock", headers=ADMIN)
    assert locked.status_code == 200
    # Lock revokes refresh sessions, so the access token is no longer live.
    assert client.get("/v1/me", headers=_auth(changed.json()["access_token"])).status_code == 401
    denied = client.post("/v1/auth/login", json={"username": "Alice", "password": PASSWORD})
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_credentials"
    unlocked = client.post(f"/v1/admin/accounts/{account_id}/unlock", headers=ADMIN)
    assert unlocked.status_code == 200
    revoked = client.post(f"/v1/admin/accounts/{account_id}/revoke-sessions", headers=ADMIN)
    assert revoked.status_code == 200
    assert revoked.json()["revoked_sessions"] >= 0


def test_dev_login_is_off_in_production_and_on_in_development(client, frozen) -> None:
    import logging

    create_scenario(frozen.now())
    allowed = client.post("/v1/auth/dev-login", json={"name": "Alice"})
    assert allowed.status_code == 200
    assert str(allowed.json()["token"]).startswith("dev:")
    captured: list[str] = []
    auth_logger = logging.getLogger("simcore.auth")
    original = auth_logger.warning

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        captured.append(message % args if args else message)
        original(message, *args, **kwargs)

    auth_logger.warning = _warning  # type: ignore[method-assign]
    try:
        with _production_app(frozen) as prod:
            denied = prod.post("/v1/auth/dev-login", json={"name": "Alice"})
            assert denied.status_code == 404, denied.text
            assert denied.json()["error"]["code"] == "not_found"
            rejected = prod.get("/v1/me", headers=_auth(allowed.json()["token"]))
            assert rejected.status_code == 401
    finally:
        auth_logger.warning = original  # type: ignore[method-assign]
    assert captured, "dev-login did not log a rejection"
    audit = client.get("/v1/admin/audit", params={"action": "auth.dev_login"}, headers=ADMIN)
    assert any(row["result"] == "denied" and row["reason"] == "disabled" for row in audit.json()["entries"])


def test_audit_records_auth_without_secrets(client) -> None:
    registered = _register(client, "Ada")
    client.post("/v1/auth/login", json={"username": "Ada", "password": "not-the-password"})
    client.post("/v1/auth/logout-all", headers=_auth(registered["access_token"]))
    audit = client.get("/v1/admin/audit", headers=ADMIN)
    assert audit.status_code == 200
    body = audit.json()
    assert body["chain"]["status"] == "PASS"
    actions = {row["action"] for row in body["entries"]}
    assert "auth.register" in actions
    assert "auth.login" in actions
    assert "auth.logout_all" in actions
    dumped = json.dumps(body)
    assert PASSWORD not in dumped
    assert registered["access_token"] not in dumped
    assert registered["refresh_token"] not in dumped
    assert "scrypt$" not in dumped


def test_snapshots_omit_auth_and_restore_clears_idempotency(client, frozen) -> None:
    world = create_scenario(frozen.now())
    session = get_sessionmaker()()
    try:
        before = world_checksum(session)
        document = capture_document(session)
    finally:
        session.close()
    assert "player_accounts" not in document
    assert "player_refresh_sessions" not in document
    assert "command_idempotency_keys" not in document
    claimed = client.post(
        "/v1/admin/accounts/temporary-password",
        headers=ADMIN,
        json={"player_id": world["alice_id"], "password": "temporary-pass-1"},
    )
    assert claimed.status_code == 200, claimed.text
    logged = client.post("/v1/auth/login", json={"username": "Alice", "password": "temporary-pass-1"})
    assert logged.status_code == 200, logged.text
    changed = client.post(
        "/v1/auth/change-password",
        headers=_auth(logged.json()["access_token"]),
        json={"current_password": "temporary-pass-1", "new_password": PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    session = get_sessionmaker()()
    try:
        assert world_checksum(session) == before
        assert session.scalar(select(func.count()).select_from(PlayerAccount)) == 1
        assert session.scalar(select(func.count()).select_from(PlayerRefreshSession)) >= 1
    finally:
        session.close()

    created = client.post("/v1/admin/snapshots", json={"reason": "MANUAL"}, headers=ADMIN)
    assert created.status_code == 200, created.text
    headers = _auth(changed.json()["access_token"])
    headers["Idempotency-Key"] = "before-restore"
    attack = client.post(
        "/v1/commands/attack",
        headers=headers,
        json={"army_id": world["alice_army"], "target_city_id": world["bob_city"]},
    )
    assert attack.status_code == 200, attack.text
    restored = client.post(
        f"/v1/admin/snapshots/{created.json()['snapshot_id']}/restore",
        json={"confirm": True},
        headers=ADMIN,
    )
    assert restored.status_code == 200, restored.text
    session = get_sessionmaker()()
    try:
        assert world_checksum(session) == before
        assert session.scalar(select(func.count()).select_from(PlayerAccount)) == 1
        assert session.scalar(select(func.count()).select_from(PlayerRefreshSession)) >= 1
        assert session.scalar(select(func.count()).select_from(CommandIdempotency)) == 0
    finally:
        session.close()


def test_downgrade_drops_only_the_auth_tables(db) -> None:
    cfg = Config("alembic.ini")
    reset_engine()
    try:
        command.downgrade(cfg, "0004_monitoring")
        reset_engine()
        names = set(inspect(get_engine()).get_table_names())
        assert "player_accounts" not in names
        assert "player_refresh_sessions" not in names
        assert "command_idempotency_keys" not in names
        assert "players" in names
        assert "audit_log" in names
        assert "monitoring_samples" in names
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
        get_settings.cache_clear()


def test_ensure_player_token_secret_writes_production_file_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env.prod"
    env_file.write_text("SIMCORE_ENV=production\n", encoding="utf-8")
    monkeypatch.setenv("SIMCORE_ENV", "production")
    monkeypatch.delenv("SIMCORE_PLAYER_TOKEN_SECRET", raising=False)
    ensure_player_token_secret(env_file)
    text_value = env_file.read_text(encoding="utf-8")
    secret = ""
    for line in text_value.splitlines():
        if line.startswith("SIMCORE_PLAYER_TOKEN_SECRET="):
            secret = line.split("=", 1)[1]
    assert len(secret) >= 32
    assert secret != "dev-player-token-secret-not-for-production"
    ensure_player_token_secret(env_file)
    again = ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("SIMCORE_PLAYER_TOKEN_SECRET="):
            again = line.split("=", 1)[1]
    assert again == secret

    monkeypatch.setenv("SIMCORE_ENV", "development")
    missing = tmp_path / "missing.env"
    ensure_player_token_secret(missing)
    assert not missing.exists()
