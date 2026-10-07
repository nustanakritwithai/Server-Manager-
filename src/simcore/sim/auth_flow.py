"""Shared player-auth steps for the simulator.

CI bot passwords are derived from the seed and the player name. They are
fixtures for this process, not production credentials, and they are not written
into the world snapshot.
"""

from __future__ import annotations

from typing import Any

from simcore.sim.http import ApiClient


def bot_password(seed: int, name: str, *, kind: str) -> str:
    """kind is ``play`` after a change, or ``temp`` for the admin temporary password."""

    return f"{kind}-{seed}-{name}-Aa9"


def auth_headers(token: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def register_bot(api: ApiClient, *, name: str, password: str) -> dict[str, Any]:
    status, body = api.json(
        "POST",
        "/v1/auth/register",
        json={"username": name, "password": password},
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise RuntimeError(f"register {name} failed (HTTP {status}): {_brief(body)}")
    return body


def login_bot(api: ApiClient, *, name: str, password: str) -> dict[str, Any]:
    status, body = api.json(
        "POST",
        "/v1/auth/login",
        json={"username": name, "password": password},
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise RuntimeError(f"login {name} failed (HTTP {status}): {_brief(body)}")
    return body


def refresh_bot(api: ApiClient, refresh_token: str) -> dict[str, Any]:
    status, body = api.json("POST", "/v1/auth/refresh", json={"refresh_token": refresh_token})
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise RuntimeError(f"refresh failed (HTTP {status}): {_brief(body)}")
    return body


def change_password(api: ApiClient, *, access_token: str, current: str, new: str) -> dict[str, Any]:
    status, body = api.json(
        "POST",
        "/v1/auth/change-password",
        headers=auth_headers(access_token),
        json={"current_password": current, "new_password": new},
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise RuntimeError(f"change-password failed (HTTP {status}): {_brief(body)}")
    return body


def bot_record(body: dict[str, Any], *, name: str, index: int) -> dict[str, Any]:
    from simcore.sim.seed_world import profile_for

    return {
        "id": int(body["player_id"]),
        "name": str(body.get("player_name") or name),
        "token": str(body["access_token"]),
        "refresh_token": str(body["refresh_token"]),
        "profile": profile_for(index),
    }


def _brief(body: object) -> str:
    text = str(body)
    return text[:300]
