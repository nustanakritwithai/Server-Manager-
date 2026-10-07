"""Auth cases the CI scenario must actually hit.

These run after the bots have played. Cases that only touch accounts, audit
rows, or rejected commands do not move armies. Registering the coverage user
adds one player row with no city; both seeds do that at the same game clock.
A case that is not exercised is INCOMPLETE, and the run does not pass.
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from simcore.config import Settings, get_settings
from simcore.db import get_sessionmaker
from simcore.main import create_app
from simcore.models import PlayerCommand
from simcore.player_auth import issue_access_token
from simcore.sim.auth_flow import auth_headers, refresh_bot, register_bot
from simcore.sim.http import ApiClient

_CASES = (
    "register",
    "login_me",
    "refresh",
    "wrong_password",
    "unknown_user_same_error",
    "expired_token",
    "forged_token",
    "cross_player_army",
    "refresh_reuse",
    "account_lockout",
    "ip_rate_limit",
    "command_rate_limit",
    "idempotent_replay",
    "dev_login_disabled_in_production",
    "dev_token_rejected_in_production",
    "logout_all",
)


def run_auth_coverage(api: ApiClient, bots: list[dict[str, Any]], *, seed: int) -> dict[str, Any]:
    """Exercise the auth matrix against the live API. Returns a COMPLETE or INCOMPLETE report."""

    rows: dict[str, dict[str, Any]] = {name: _gap(name) for name in _CASES}
    password = f"cover-{seed}-Aa9"
    try:
        registered = register_bot(api, name="covuser", password=password)
        rows["register"] = _pass("register", f"player {registered.get('player_id')}")
        me_status, me = api.json("GET", "/v1/auth/me", headers=auth_headers(str(registered["access_token"])))
        if me_status == 200 and isinstance(me, dict) and me.get("username") == "covuser":
            rows["login_me"] = _pass("login_me", "GET /v1/auth/me")
        else:
            rows["login_me"] = _fail("login_me", f"HTTP {me_status}")
        refreshed = refresh_bot(api, str(registered["refresh_token"]))
        rows["refresh"] = _pass("refresh", "rotated refresh token")
        access = str(refreshed["access_token"])
        _wrong_password(api, rows, password)
        _expired_and_forged(api, rows, access)
        _cross_player(api, rows, bots)
        _idempotent(api, rows, bots, seed)
        _reuse(api, seed, rows)
        _lockout(api, rows, password)
        _ip_limit(api, rows)
        _command_limit(api, rows, access)
        _production_dev_login(rows)
        logout_status, logout_body = api.json("POST", "/v1/auth/logout-all", headers=auth_headers(access))
        if logout_status == 200 and isinstance(logout_body, dict):
            rows["logout_all"] = _pass("logout_all", f"revoked {logout_body.get('revoked_sessions')}")
        else:
            rows["logout_all"] = _fail("logout_all", f"HTTP {logout_status}")
        blocked, _ = api.json("GET", "/v1/auth/me", headers=auth_headers(access))
        if blocked != 401:
            rows["logout_all"] = _fail("logout_all", f"access token still worked (HTTP {blocked})")
    except Exception as exc:
        rows["register"] = _fail("register", f"{exc.__class__.__name__}: {exc}")
    matrix = [rows[name] for name in _CASES]
    gaps = [str(row["detail"]) for row in matrix if row["status"] != "PASS"]
    return {
        "verdict": "COMPLETE" if not gaps else "INCOMPLETE",
        "matrix": matrix,
        "gaps": gaps,
    }


def _wrong_password(api: ApiClient, rows: dict[str, dict[str, Any]], password: str) -> None:
    known_status, known = api.json(
        "POST",
        "/v1/auth/login",
        json={"username": "covuser", "password": "not-the-password-zz"},
    )
    unknown_status, unknown = api.json(
        "POST",
        "/v1/auth/login",
        json={"username": "nobody-coverage", "password": password},
    )
    if known_status == 401 and unknown_status == 401 and known == unknown:
        rows["wrong_password"] = _pass("wrong_password", "401 invalid credentials")
        rows["unknown_user_same_error"] = _pass("unknown_user_same_error", "same body as a wrong password")
    else:
        rows["wrong_password"] = _fail("wrong_password", f"known HTTP {known_status}")
        rows["unknown_user_same_error"] = _fail(
            "unknown_user_same_error",
            f"known {known_status} {known} unknown {unknown_status} {unknown}",
        )


def _expired_and_forged(api: ApiClient, rows: dict[str, dict[str, Any]], access: str) -> None:
    settings = get_settings()
    expired, _ = issue_access_token(
        settings,
        player_id=1,
        account_id=1,
        session_id=1,
        now=int(time.time()),
        ttl=-120,
    )
    expired_status, _ = api.json("GET", "/v1/me", headers=auth_headers(expired))
    rows["expired_token"] = (
        _pass("expired_token", "401")
        if expired_status == 401
        else _fail("expired_token", f"HTTP {expired_status}")
    )
    forged = access[:-1] + ("A" if access[-1] != "A" else "B")
    forged_status, _ = api.json("GET", "/v1/me", headers=auth_headers(forged))
    rows["forged_token"] = (
        _pass("forged_token", "401") if forged_status == 401 else _fail("forged_token", f"HTTP {forged_status}")
    )


def _cross_player(api: ApiClient, rows: dict[str, dict[str, Any]], bots: list[dict[str, Any]]) -> None:
    if len(bots) < 2:
        rows["cross_player_army"] = _fail("cross_player_army", "need two bots")
        return
    first, second = bots[0], bots[1]
    status, armies = api.json("GET", "/v1/me/armies", headers=auth_headers(str(second["token"])))
    if status != 200 or not isinstance(armies, dict) or not armies.get("armies"):
        rows["cross_player_army"] = _fail("cross_player_army", f"armies HTTP {status}")
        return
    army_id = int(armies["armies"][0]["id"])
    attacked, body = api.json(
        "POST",
        "/v1/commands/attack",
        headers=auth_headers(str(first["token"])),
        json={"army_id": army_id, "target_city_id": 1, "player_id": int(second["id"])},
    )
    code = body.get("error", {}).get("code") if isinstance(body, dict) else None
    if attacked == 403 and code == "forbidden":
        rows["cross_player_army"] = _pass(
            "cross_player_army",
            "token for A was rejected for B's army; player_id in the body was ignored",
        )
    else:
        rows["cross_player_army"] = _fail("cross_player_army", f"HTTP {attacked} code {code}")


def _idempotent(api: ApiClient, rows: dict[str, dict[str, Any]], bots: list[dict[str, Any]], seed: int) -> None:
    if len(bots) < 2:
        rows["idempotent_replay"] = _fail("idempotent_replay", "need two bots")
        return
    before = _command_count()
    second = bots[1]
    status, armies = api.json("GET", "/v1/me/armies", headers=auth_headers(str(second["token"])))
    if status != 200 or not isinstance(armies, dict) or not armies.get("armies"):
        rows["idempotent_replay"] = _fail("idempotent_replay", f"armies HTTP {status}")
        return
    army_id = int(armies["armies"][0]["id"])
    key = f"sim-auth-{seed}-replay"
    headers = auth_headers(str(bots[0]["token"]), idempotency_key=key)
    payload = {"army_id": army_id, "target_city_id": 1}
    first_status, first_body = api.json("POST", "/v1/commands/attack", headers=headers, json=payload)
    mid = _command_count()
    second_status, second_body = api.json("POST", "/v1/commands/attack", headers=headers, json=payload)
    after = _command_count()
    if first_status == second_status and first_body == second_body and mid == before and after == before:
        rows["idempotent_replay"] = _pass("idempotent_replay", f"HTTP {first_status} stored and replayed once")
    else:
        rows["idempotent_replay"] = _fail(
            "idempotent_replay",
            f"status {first_status}/{second_status} commands {before}->{mid}->{after}",
        )


def _reuse(api: ApiClient, seed: int, rows: dict[str, dict[str, Any]]) -> None:
    registered = register_bot(api, name="covreuse", password=f"reuse-{seed}-Aa9")
    first_refresh = str(registered["refresh_token"])
    rotated = refresh_bot(api, first_refresh)
    reused, _ = api.json("POST", "/v1/auth/refresh", json={"refresh_token": first_refresh})
    family_dead, _ = api.json("POST", "/v1/auth/refresh", json={"refresh_token": str(rotated["refresh_token"])})
    access_dead, _ = api.json("GET", "/v1/auth/me", headers=auth_headers(str(rotated["access_token"])))
    if reused == 401 and family_dead == 401 and access_dead == 401:
        rows["refresh_reuse"] = _pass("refresh_reuse", "old refresh token revoked the family")
    else:
        rows["refresh_reuse"] = _fail(
            "refresh_reuse",
            f"reuse {reused} rotated {family_dead} access {access_dead}",
        )


def _lockout(api: ApiClient, rows: dict[str, dict[str, Any]], password: str) -> None:
    limit = int(os.environ.get("SIMCORE_PLAYER_LOGIN_MAX_FAILURES", "5"))
    # The wrong-password case already recorded one failure for covuser.
    for _ in range(max(0, limit - 1)):
        api.json("POST", "/v1/auth/login", json={"username": "covuser", "password": "not-the-password-zz"})
    status, body = api.json(
        "GET",
        "/v1/admin/audit",
        headers=api.admin_headers,
        params={"action": "auth.lockout", "limit": 20},
    )
    entries = body.get("entries") if isinstance(body, dict) else None
    locked = status == 200 and isinstance(entries, list) and any(
        isinstance(row, dict) and row.get("action") == "auth.lockout" for row in entries
    )
    still, still_body = api.json(
        "POST",
        "/v1/auth/login",
        json={"username": "covuser", "password": password},
    )
    hidden = still == 401 and still_body == {
        "error": {"code": "invalid_credentials", "message": "invalid username or password"}
    }
    if locked and hidden:
        rows["account_lockout"] = _pass("account_lockout", "lockout audited; login response stays generic")
    else:
        rows["account_lockout"] = _fail("account_lockout", f"audit {status} correct-password HTTP {still}")


def _ip_limit(api: ApiClient, rows: dict[str, dict[str, Any]]) -> None:
    limit = int(os.environ.get("SIMCORE_PLAYER_LOGIN_IP_MAX_FAILURES", "20"))
    last = 0
    for index in range(limit + 2):
        last, _ = api.json(
            "POST",
            "/v1/auth/login",
            json={"username": f"ip-miss-{index}", "password": "not-the-password-zz"},
        )
        if last == 429:
            rows["ip_rate_limit"] = _pass("ip_rate_limit", f"429 after {index + 1} attempts")
            return
    rows["ip_rate_limit"] = _fail("ip_rate_limit", f"last HTTP {last}")


def _command_limit(api: ApiClient, rows: dict[str, dict[str, Any]], access: str) -> None:
    limit = int(os.environ.get("SIMCORE_COMMAND_RATE_LIMIT", "30"))
    headers = auth_headers(access)
    last = 0
    for _ in range(limit + 2):
        last, _ = api.json(
            "POST",
            "/v1/commands/attack",
            headers=headers,
            json={"army_id": 999999, "target_city_id": 1},
        )
        if last == 429:
            rows["command_rate_limit"] = _pass("command_rate_limit", "429")
            return
    rows["command_rate_limit"] = _fail("command_rate_limit", f"last HTTP {last}")


def _production_dev_login(rows: dict[str, dict[str, Any]]) -> None:
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
    app = create_app(settings=settings)
    with TestClient(app) as client:
        denied = client.post("/v1/auth/dev-login", json={"name": "Alice"})
        dev_token = client.get("/v1/me", headers={"Authorization": "Bearer dev:1"})
    if denied.status_code == 404:
        rows["dev_login_disabled_in_production"] = _pass("dev_login_disabled_in_production", "404")
    else:
        rows["dev_login_disabled_in_production"] = _fail(
            "dev_login_disabled_in_production",
            f"HTTP {denied.status_code}",
        )
    if dev_token.status_code == 401:
        rows["dev_token_rejected_in_production"] = _pass("dev_token_rejected_in_production", "401")
    else:
        rows["dev_token_rejected_in_production"] = _fail(
            "dev_token_rejected_in_production",
            f"HTTP {dev_token.status_code}",
        )


def _command_count() -> int:
    session = get_sessionmaker()()
    try:
        return int(session.scalar(select(func.count()).select_from(PlayerCommand)) or 0)
    finally:
        session.rollback()
        session.close()


def _pass(name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "status": "PASS", "detail": detail}


def _fail(name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "status": "FAIL", "detail": detail}


def _gap(name: str) -> dict[str, Any]:
    return {"name": name, "status": "INCOMPLETE", "detail": "not exercised"}
