"""Password login for the admin control center.

The session token is returned in the JSON body. Callers keep it themselves.
This route does not set a cookie.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from simcore.admin_auth import (
    AdminSessionBook,
    LoginRateLimiter,
    client_address,
    issue_admin_session,
    password_hash_is_usable,
    read_admin_session,
    session_secret_is_usable,
    verify_admin_password,
)
from simcore.api.deps import _bearer, require_admin
from simcore.audit import actor_from_request, write_audit
from simcore.config import Settings
from simcore.errors import GameError

router = APIRouter()


class LoginIn(BaseModel):
    password: str = ""


def _expires_at(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_login_configured(settings: Settings) -> None:
    if not settings.admin_enabled:
        raise GameError("admin API is disabled", status_code=404, code="not_found")
    if not settings.admin_password_hash:
        raise GameError(
            "Admin password login is not configured.",
            status_code=403,
            code="admin_login_disabled",
        )
    if not password_hash_is_usable(settings.admin_password_hash):
        raise GameError(
            "Admin password hash is not a valid scrypt hash.",
            status_code=403,
            code="admin_login_disabled",
        )
    if not session_secret_is_usable(settings):
        raise GameError(
            "Admin session secret is not configured.",
            status_code=403,
            code="admin_login_disabled",
        )


def _audit_login(request: Request, *, result: str, reason: str | None, actor: str = "admin") -> None:
    write_audit(
        actor=actor,
        action="admin.login",
        target="admin",
        source_ip=client_address(request),
        result=result,
        reason=reason,
    )


@router.post("/login")
def admin_login(body: LoginIn, request: Request) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    try:
        _require_login_configured(settings)
    except GameError as exc:
        _audit_login(request, result="failure", reason=exc.message)
        raise
    limiter: LoginRateLimiter = request.app.state.login_limiter
    address = client_address(request)
    now = time.time()
    if not limiter.allowed(address, now):
        _audit_login(request, result="failure", reason="rate_limited")
        raise GameError(
            "Too many sign-in attempts from this address. Wait and try again.",
            status_code=429,
            code="rate_limited",
        )
    if not verify_admin_password(body.password, settings.admin_password_hash):
        limiter.record_failure(address, now)
        _audit_login(request, result="failure", reason="invalid password")
        raise GameError("invalid password", status_code=401, code="unauthorized")
    limiter.record_success(address)
    book: AdminSessionBook = request.app.state.admin_sessions
    token, expires_at = issue_admin_session(settings, book)
    admin_session = read_admin_session(token, settings, book)
    actor = f"session:{admin_session.jti}" if admin_session is not None else "admin"
    _audit_login(request, result="success", reason=None, actor=actor)
    return {
        "token_type": "Bearer",
        "token": token,
        "expires_at": _expires_at(expires_at),
        "expires_in": max(0, expires_at - int(time.time())),
    }


@router.post("/logout")
def admin_logout(
    request: Request,
    _: Annotated[Settings, Depends(require_admin)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    book: AdminSessionBook = request.app.state.admin_sessions
    actor = actor_from_request(request)
    bearer = ""
    if credentials is not None and credentials.scheme.lower() == "bearer":
        bearer = credentials.credentials or ""
    session = read_admin_session(bearer, settings, book) if bearer else None
    if session is None:
        write_audit(
            actor=actor,
            action="admin.logout",
            target="admin",
            source_ip=client_address(request),
            result="success",
            reason="static admin token is not a session",
        )
        return {
            "revoked": False,
            "detail": (
                "The static admin token is not a session. "
                "Rotate SIMCORE_ADMIN_TOKEN, or increase SIMCORE_ADMIN_SESSION_VERSION and restart the API, to revoke it."
            ),
        }
    book.revoke(session.jti)
    write_audit(
        actor=actor,
        action="admin.logout",
        target=f"session:{session.jti}",
        source_ip=client_address(request),
        result="success",
        reason=None,
    )
    return {"revoked": True}


@router.post("/sessions/revoke")
def admin_revoke_sessions(
    request: Request,
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    book: AdminSessionBook = request.app.state.admin_sessions
    actor = actor_from_request(request)
    epoch = book.revoke_all()
    write_audit(
        actor=actor,
        action="admin.sessions.revoke",
        target="sessions",
        source_ip=client_address(request),
        result="success",
        reason=f"session_epoch={epoch}",
    )
    return {"revoked": "all", "session_epoch": epoch}
