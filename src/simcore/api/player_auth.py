"""Player register, login, refresh, logout, and profile.

JSON only. No cookies. The client sends ``Authorization: Bearer`` with the
access token. Refresh tokens travel in the JSON body.

Login failures use one message for an unknown identifier, a wrong password,
and a locked account. Registration is the call that reveals a taken username.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from simcore.admin_auth import LoginRateLimiter, client_address
from simcore.api.deps import get_authenticated_player, get_clock, get_current_player, get_session
from simcore.clock import OffsetClock
from simcore.audit import append_audit, write_audit
from simcore.auth import DEV_AUTH_WARNING, issue_dev_token
from simcore.config import Settings
from simcore.errors import GameError
from simcore.game.start import grant_start, start_public
from simcore.models import Player, PlayerAccount, PlayerRefreshSession
from simcore.player_auth import (
    IssuedTokens,
    account_by_login,
    account_for_player,
    dummy_password_hash,
    email_key,
    hash_player_password,
    issue_token_pair,
    login_locked,
    parse_refresh_token,
    player_name_taken,
    refresh_matches,
    revoke_account_sessions,
    revoke_family,
    revoke_session,
    token_response,
    validate_email,
    validate_new_password,
    validate_username,
    verify_player_password,
)

logger = logging.getLogger("simcore.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

_INVALID = {"error": {"code": "invalid_credentials", "message": "invalid username or password"}}
_REFRESH_INVALID = {"error": {"code": "invalid_refresh", "message": "invalid refresh token"}}


class RegisterIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=1024)
    email: str | None = Field(default=None, max_length=254)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=500)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class DevLoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class HomeCityOut(BaseModel):
    id: int
    name: str
    x: int
    y: int


class StartOut(BaseModel):
    """Whether this account already has a server-placed home.

    ``home_city`` is null until the start is granted. ``army_id`` is the army
    that was created with that city, when one still exists.
    """

    start_granted: bool
    home_city: HomeCityOut | None = None
    army_id: int | None = None


class AuthMeOut(StartOut):
    account_id: int | None = None
    username: str | None = None
    email: str | None = None
    player_id: int
    player_name: str
    must_change_password: bool
    locked: bool
    has_password: bool


class RegisterOut(StartOut):
    token_type: str
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    must_change_password: bool
    player_id: int
    player_name: str
    username: str


class ClaimStartOut(StartOut):
    created: bool
    trace_id: str | None = None


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _ip_limiter(request: Request) -> LoginRateLimiter:
    return request.app.state.player_login_ip


def _audit(
    session: Session,
    request: Request,
    *,
    actor: str,
    action: str,
    target: str,
    result: str,
    reason: str | None = None,
) -> None:
    append_audit(
        session,
        actor=actor,
        action=action,
        target=target,
        source_ip=client_address(request),
        result=result,
        reason=reason,
    )


def _invalid() -> JSONResponse:
    return JSONResponse(status_code=401, content=_INVALID)


def _rate_limited() -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": {"code": "rate_limited", "message": "too many attempts"}},
    )


def _grant_on_register(
    session: Session,
    request: Request,
    player: Player,
    now: datetime,
    settings: Settings,
) -> dict[str, object]:
    """Place the start inside the register transaction. World-full rolls the account back."""

    try:
        grant_start(
            session,
            player,
            now,
            settings,
            actor=f"player:{player.id}",
            source_ip=client_address(request),
        )
    except GameError as exc:
        if exc.code == "world_full":
            write_audit(
                actor="anonymous",
                action="player.start",
                target=f"username:{player.name.casefold()}",
                source_ip=client_address(request),
                result="failure",
                reason="world_full",
            )
        raise
    return start_public(session, player)


@router.post(
    "/register",
    response_model=None,
    responses={200: {"model": RegisterOut}},
    summary="Create an account and its starting city, army, and resources",
)
def register(
    body: RegisterIn,
    request: Request,
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object] | JSONResponse:
    settings = _settings(request)
    username = validate_username(body.username)
    email = validate_email(body.email)
    password = validate_new_password(body.password, username=username, email=email)
    if player_name_taken(session, username) or account_by_login(session, username) is not None:
        _audit(
            session,
            request,
            actor="anonymous",
            action="auth.register",
            target=f"username:{username.casefold()}",
            result="failure",
            reason="username_taken",
        )
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "username_taken", "message": "that username is not available"}},
        )
    if email and account_by_login(session, email) is not None:
        _audit(
            session,
            request,
            actor="anonymous",
            action="auth.register",
            target="email",
            result="failure",
            reason="email_taken",
        )
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "email_taken", "message": "that email is not available"}},
        )
    now = clock.now()
    player = Player(name=username, research={}, created_at=now)
    try:
        with session.begin_nested():
            session.add(player)
            session.flush()
            account = PlayerAccount(
                player_id=player.id,
                username=username,
                username_key=username.casefold(),
                email=email,
                email_key=email_key(email),
                password_hash=hash_player_password(password),
                locked=False,
                must_change_password=False,
                failed_login_count=0,
                created_at=now,
                updated_at=now,
            )
            session.add(account)
            session.flush()
    except IntegrityError:
        write_audit(
            actor="anonymous",
            action="auth.register",
            target=f"username:{username.casefold()}",
            source_ip=client_address(request),
            result="failure",
            reason="username_taken",
        )
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "username_taken", "message": "that username is not available"}},
        )
    issued = issue_token_pair(
        session,
        settings,
        account,
        created_ip=client_address(request),
        user_agent=request.headers.get("user-agent"),
    )
    start = _grant_on_register(session, request, player, now, settings)
    _audit(
        session,
        request,
        actor=f"player:{player.id}",
        action="auth.register",
        target=f"player:{player.id}",
        result="success",
    )
    return {**token_response(settings, account, player, issued), **start}


@router.post("/login", response_model=None)
def login(
    body: LoginIn,
    request: Request,
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> dict[str, object] | JSONResponse:
    settings = _settings(request)
    limiter = _ip_limiter(request)
    address = client_address(request)
    now = time.monotonic()
    if not limiter.allowed(address, now):
        _audit(
            session,
            request,
            actor="anonymous",
            action="auth.login",
            target="ip",
            result="failure",
            reason="rate_limited",
        )
        return _rate_limited()
    identifier = body.username.strip()
    account = account_by_login(session, identifier)
    if account is None:
        verify_player_password(body.password, dummy_password_hash())
        limiter.record_failure(address, now)
        _audit(
            session,
            request,
            actor="anonymous",
            action="auth.login",
            target="login",
            result="failure",
            reason="invalid_credentials",
        )
        return _invalid()
    password_ok = verify_player_password(body.password, account.password_hash)
    wall = datetime.now(timezone.utc)
    if login_locked(account, now=wall) or not password_ok:
        limiter.record_failure(address, now)
        if not account.locked and not login_locked(account, now=wall):
            account.failed_login_count = int(account.failed_login_count) + 1
            account.updated_at = wall
            if account.failed_login_count >= settings.player_login_max_failures:
                account.login_locked_until = wall + timedelta(seconds=settings.player_login_lockout_seconds)
                _audit(
                    session,
                    request,
                    actor=f"player:{account.player_id or 0}",
                    action="auth.lockout",
                    target=f"player:{account.player_id or 0}",
                    result="failure",
                    reason="too_many_failures",
                )
        _audit(
            session,
            request,
            actor=f"player:{account.player_id or 0}",
            action="auth.login",
            target=f"player:{account.player_id or 0}",
            result="failure",
            reason="invalid_credentials",
        )
        return _invalid()
    player = session.get(Player, account.player_id) if account.player_id is not None else None
    if player is None:
        limiter.record_failure(address, now)
        _audit(
            session,
            request,
            actor=f"account:{account.id}",
            action="auth.login",
            target=f"account:{account.id}",
            result="failure",
            reason="unlinked",
        )
        return _invalid()
    limiter.record_success(address)
    account.failed_login_count = 0
    account.login_locked_until = None
    account.updated_at = wall
    issued = issue_token_pair(
        session,
        settings,
        account,
        created_ip=address,
        user_agent=request.headers.get("user-agent"),
    )
    _audit(
        session,
        request,
        actor=f"player:{player.id}",
        action="auth.login",
        target=f"player:{player.id}",
        result="success",
    )
    return token_response(settings, account, player, issued)


@router.post("/refresh", response_model=None)
def refresh(
    body: RefreshIn,
    request: Request,
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> dict[str, object] | JSONResponse:
    settings = _settings(request)
    parsed = parse_refresh_token(body.refresh_token)
    if parsed is None:
        return JSONResponse(status_code=401, content=_REFRESH_INVALID)
    session_id, secret = parsed
    # Lock the presented row so two overlapping refreshes cannot both observe
    # it as live and both mint a successor. The waiter sees the rotation and
    # revokes the family.
    row = session.get(PlayerRefreshSession, session_id, with_for_update=True)
    if row is None or not refresh_matches(row, secret):
        return JSONResponse(status_code=401, content=_REFRESH_INVALID)
    wall = datetime.now(timezone.utc)
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if row.revoked_at is not None and row.replaced_by_id is not None:
        revoke_family(session, row.family_id, when=wall)
        _audit(
            session,
            request,
            actor=f"account:{row.account_id}",
            action="auth.refresh_reuse",
            target=f"family:{row.family_id}",
            result="failure",
            reason="reuse_detected",
        )
        logger.warning("refresh token reuse detected account_id=%s family=%s", row.account_id, row.family_id)
        return JSONResponse(status_code=401, content=_REFRESH_INVALID)
    if row.revoked_at is not None or expires <= wall:
        return JSONResponse(status_code=401, content=_REFRESH_INVALID)
    account = session.get(PlayerAccount, row.account_id)
    if account is None or account.locked or account.player_id is None:
        return JSONResponse(status_code=401, content=_REFRESH_INVALID)
    player = session.get(Player, account.player_id)
    if player is None:
        return JSONResponse(status_code=401, content=_REFRESH_INVALID)
    issued: IssuedTokens = issue_token_pair(
        session,
        settings,
        account,
        family_id=row.family_id,
        created_ip=client_address(request),
        user_agent=request.headers.get("user-agent"),
    )
    row.revoked_at = wall
    row.replaced_by_id = issued.session_id
    row.last_used_at = wall
    return token_response(settings, account, player, issued)


@router.post("/logout")
def logout(
    request: Request,
    player: Annotated[Player, Depends(get_authenticated_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> dict[str, object]:
    claims = request.state.player_access
    row = session.get(PlayerRefreshSession, claims.session_id) if claims is not None else None
    if row is not None:
        revoke_session(row)
    return {"status": "ok", "player_id": player.id}


@router.post("/logout-all")
def logout_all(
    request: Request,
    player: Annotated[Player, Depends(get_authenticated_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> dict[str, object]:
    account = account_for_player(session, player.id)
    revoked = 0
    if account is not None:
        revoked = revoke_account_sessions(session, account.id)
        _audit(
            session,
            request,
            actor=f"player:{player.id}",
            action="auth.logout_all",
            target=f"account:{account.id}",
            result="success",
            reason=f"revoked={revoked}",
        )
    return {"status": "ok", "player_id": player.id, "revoked_sessions": revoked}


@router.post("/change-password")
def change_password(
    body: ChangePasswordIn,
    request: Request,
    player: Annotated[Player, Depends(get_authenticated_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> dict[str, object]:
    settings = _settings(request)
    account = account_for_player(session, player.id)
    if account is None or not verify_player_password(body.current_password, account.password_hash):
        raise GameError("current password is wrong", status_code=400, code="invalid_password")
    new_password = validate_new_password(body.new_password, username=account.username, email=account.email)
    wall = datetime.now(timezone.utc)
    account.password_hash = hash_player_password(new_password)
    account.must_change_password = False
    account.failed_login_count = 0
    account.login_locked_until = None
    account.updated_at = wall
    revoke_account_sessions(session, account.id, when=wall)
    issued = issue_token_pair(
        session,
        settings,
        account,
        created_ip=client_address(request),
        user_agent=request.headers.get("user-agent"),
    )
    _audit(
        session,
        request,
        actor=f"player:{player.id}",
        action="auth.password_change",
        target=f"account:{account.id}",
        result="success",
    )
    return token_response(settings, account, player, issued)


@router.get("/me", response_model=AuthMeOut)
def auth_me(
    player: Annotated[Player, Depends(get_authenticated_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> dict[str, object]:
    """Profile plus whether the server has already placed this player's home.

    ``start_granted`` is false only when the player has no city. Seeded players
    and anyone who already has a city report true. A new registration is true
    immediately. An older account with no city stays false until
    ``POST /v1/auth/claim-start``.
    """

    account = account_for_player(session, player.id)
    start = start_public(session, player)
    if account is None:
        return {
            "account_id": None,
            "username": None,
            "email": None,
            "player_id": player.id,
            "player_name": player.name,
            "must_change_password": False,
            "locked": False,
            "has_password": False,
            **start,
        }
    return {
        "account_id": account.id,
        "username": account.username,
        "email": account.email,
        "player_id": player.id,
        "player_name": player.name,
        "must_change_password": bool(account.must_change_password),
        "locked": bool(account.locked),
        "has_password": True,
        **start,
    }


@router.post(
    "/claim-start",
    response_model=ClaimStartOut,
    summary="Grant the starting city once, or return the home that already exists",
)
def claim_start(
    request: Request,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    """Idempotent start for an account that was created before starts existed.

    New registrations already have a home, so this returns that home and does
    not create a second city. A player with no city gets one. Two overlapping
    calls lock the player row and the spawn lock; only one city is inserted.
    A temporary password must be changed first (``get_current_player``).
    Login does not grant a start: a world-full failure would have to break the
    single ``invalid_credentials`` response, and login is already a race surface.
    """

    settings = _settings(request)
    try:
        granted = grant_start(
            session,
            player,
            clock.now(),
            settings,
            actor=f"player:{player.id}",
            source_ip=client_address(request),
        )
    except GameError as exc:
        if exc.code == "world_full":
            write_audit(
                actor=f"player:{player.id}",
                action="player.start",
                target=f"player:{player.id}",
                source_ip=client_address(request),
                result="failure",
                reason="world_full",
            )
        raise
    body = start_public(session, player)
    return {
        "created": granted.created,
        "trace_id": granted.trace_id,
        **body,
    }


@router.post("/dev-login")
def dev_login(
    body: DevLoginIn,
    request: Request,
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> dict[str, object]:
    settings = _settings(request)
    if not settings.dev_login_enabled:
        logger.warning(
            "dev-login rejected env=%s ip=%s name=%s",
            settings.env,
            client_address(request),
            body.name,
        )
        write_audit(
            actor="anonymous",
            action="auth.dev_login",
            target=f"name:{body.name}",
            source_ip=client_address(request),
            result="denied",
            reason="disabled",
        )
        raise GameError("not found", status_code=404, code="not_found")
    player = session.scalar(select(Player).where(Player.name == body.name))
    if player is None:
        raise GameError("no such player", status_code=404, code="not_found")
    return {
        "token": issue_dev_token(player.id),
        "token_type": "bearer",
        "player_id": player.id,
        "player_name": player.name,
        "dev_only": True,
        "warning": DEV_AUTH_WARNING,
    }
