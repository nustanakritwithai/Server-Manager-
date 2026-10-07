"""Admin account controls. Read and a few writes. No deletion and no ledger edits.

Existing dev-login players have no password until an admin sets a temporary one.
That password must be changed at the next player login. Sessions can be revoked
or the account locked. The password and the hash are not written to the audit log.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from simcore.api.deps import get_session, require_admin
from simcore.audit import record_admin_action
from simcore.config import Settings
from simcore.errors import GameError
from simcore.models import Player, PlayerAccount, PlayerRefreshSession
from simcore.player_auth import (
    account_for_player,
    hash_player_password,
    player_name_taken,
    revoke_account_sessions,
    username_key,
    validate_new_password,
    validate_username,
)

router = APIRouter(prefix="/accounts", tags=["admin"])


class TemporaryPasswordIn(BaseModel):
    player_id: int
    password: str = Field(min_length=1, max_length=1024)


def _wall() -> datetime:
    return datetime.now(timezone.utc)


def _account_body(player: Player, account: PlayerAccount | None, session_count: int) -> dict[str, object]:
    if account is None:
        return {
            "account_id": None,
            "player_id": player.id,
            "player_name": player.name,
            "username": None,
            "email": None,
            "has_password": False,
            "locked": False,
            "must_change_password": False,
            "failed_login_count": 0,
            "login_locked_until": None,
            "created_at": None,
            "session_count": session_count,
        }
    return {
        "account_id": account.id,
        "player_id": player.id,
        "player_name": player.name,
        "username": account.username,
        "email": account.email,
        "has_password": True,
        "locked": bool(account.locked),
        "must_change_password": bool(account.must_change_password),
        "failed_login_count": int(account.failed_login_count),
        "login_locked_until": account.login_locked_until,
        "created_at": account.created_at,
        "session_count": session_count,
    }


def _session_count(session: Session, account_id: int | None) -> int:
    if account_id is None:
        return 0
    rows = session.scalars(
        select(PlayerRefreshSession.id).where(
            PlayerRefreshSession.account_id == account_id,
            PlayerRefreshSession.revoked_at.is_(None),
        )
    ).all()
    return len(rows)


def _require_account(session: Session, account_id: int) -> PlayerAccount:
    account = session.get(PlayerAccount, account_id)
    if account is None:
        raise GameError("account not found", status_code=404, code="not_found")
    return account


@router.get("")
def list_accounts(
    session: Annotated[Session, Depends(get_session, scope="function")],
    _: Annotated[Settings, Depends(require_admin)],
    q: str = "",
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, object]:
    needle = q.strip().casefold()
    players = session.scalars(select(Player).order_by(Player.id)).all()
    accounts = session.scalars(select(PlayerAccount).order_by(PlayerAccount.id)).all()
    by_player = {account.player_id: account for account in accounts if account.player_id is not None}
    rows: list[dict[str, object]] = []
    seen: set[int] = set()
    for player in players:
        account = by_player.get(player.id)
        body = _account_body(player, account, _session_count(session, None if account is None else account.id))
        if needle and not _matches(body, needle):
            continue
        rows.append(body)
        if account is not None:
            seen.add(account.id)
        if len(rows) >= limit:
            return {"accounts": rows}
    for account in accounts:
        if account.id in seen:
            continue
        player = session.get(Player, account.player_id) if account.player_id is not None else None
        if player is None:
            body = {
                "account_id": account.id,
                "player_id": account.player_id,
                "player_name": None,
                "username": account.username,
                "email": account.email,
                "has_password": True,
                "locked": bool(account.locked),
                "must_change_password": bool(account.must_change_password),
                "failed_login_count": int(account.failed_login_count),
                "login_locked_until": account.login_locked_until,
                "created_at": account.created_at,
                "session_count": _session_count(session, account.id),
            }
        else:
            body = _account_body(player, account, _session_count(session, account.id))
        if needle and not _matches(body, needle):
            continue
        rows.append(body)
        if len(rows) >= limit:
            break
    return {"accounts": rows}


def _matches(body: dict[str, object], needle: str) -> bool:
    for key in ("player_name", "username", "email"):
        value = body.get(key)
        if value is not None and needle in str(value).casefold():
            return True
    player_id = body.get("player_id")
    if player_id is not None and needle == str(player_id):
        return True
    return False


@router.get("/{account_id}/sessions")
def list_sessions(
    account_id: int,
    session: Annotated[Session, Depends(get_session, scope="function")],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    account = _require_account(session, account_id)
    rows = session.scalars(
        select(PlayerRefreshSession)
        .where(PlayerRefreshSession.account_id == account.id)
        .order_by(PlayerRefreshSession.id.desc())
    ).all()
    return {
        "account_id": account.id,
        "player_id": account.player_id,
        "sessions": [
            {
                "id": row.id,
                "family_id": row.family_id,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "revoked_at": row.revoked_at,
                "last_used_at": row.last_used_at,
                "created_ip": row.created_ip,
                "user_agent": row.user_agent,
                "rotated": row.replaced_by_id is not None,
            }
            for row in rows
        ],
    }


@router.post("/temporary-password")
def set_temporary_password(
    body: TemporaryPasswordIn,
    request: Request,
    session: Annotated[Session, Depends(get_session, scope="function")],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    player = session.get(Player, body.player_id)
    if player is None:
        raise GameError("player not found", status_code=404, code="not_found")
    try:
        username = validate_username(player.name)
    except GameError as exc:
        raise GameError(
            "this player's name cannot be used as a username; it was left unchanged",
            status_code=400,
            code="invalid_username",
        ) from exc
    password = validate_new_password(body.password, username=username, email=None)
    wall = _wall()
    account = account_for_player(session, player.id)
    created = account is None
    if account is None:
        if player_name_taken(session, username) and username_key(player.name) != username_key(username):
            raise GameError("that username is not available", status_code=409, code="username_taken")
        other = session.scalar(select(PlayerAccount).where(PlayerAccount.username_key == username.casefold()))
        if other is not None and other.player_id != player.id:
            raise GameError("that username is not available", status_code=409, code="username_taken")
        account = PlayerAccount(
            player_id=player.id,
            username=username,
            username_key=username.casefold(),
            email=None,
            email_key=None,
            password_hash=hash_player_password(password),
            locked=False,
            must_change_password=True,
            failed_login_count=0,
            login_locked_until=None,
            created_at=wall,
            updated_at=wall,
        )
        session.add(account)
    else:
        account.password_hash = hash_player_password(password)
        account.must_change_password = True
        account.failed_login_count = 0
        account.login_locked_until = None
        account.updated_at = wall
    session.flush()
    revoked = revoke_account_sessions(session, account.id, when=wall)
    record_admin_action(
        request,
        action="account.temporary_password",
        target=f"player:{player.id}",
        result="success",
        reason="created" if created else f"reset revoked={revoked}",
    )
    return {
        "account_id": account.id,
        "player_id": player.id,
        "username": account.username,
        "must_change_password": True,
        "created": created,
    }


@router.post("/{account_id}/lock")
def lock_account(
    account_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_session, scope="function")],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    account = _require_account(session, account_id)
    account.locked = True
    account.updated_at = _wall()
    revoked = revoke_account_sessions(session, account.id)
    record_admin_action(
        request,
        action="account.lock",
        target=f"account:{account.id}",
        result="success",
        reason=f"revoked={revoked}",
    )
    return {"account_id": account.id, "player_id": account.player_id, "locked": True}


@router.post("/{account_id}/unlock")
def unlock_account(
    account_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_session, scope="function")],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    account = _require_account(session, account_id)
    account.locked = False
    account.failed_login_count = 0
    account.login_locked_until = None
    account.updated_at = _wall()
    record_admin_action(
        request,
        action="account.unlock",
        target=f"account:{account.id}",
        result="success",
    )
    return {"account_id": account.id, "player_id": account.player_id, "locked": False}


@router.post("/{account_id}/revoke-sessions")
def revoke_sessions(
    account_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_session, scope="function")],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    account = _require_account(session, account_id)
    revoked = revoke_account_sessions(session, account.id)
    record_admin_action(
        request,
        action="account.revoke_sessions",
        target=f"account:{account.id}",
        result="success",
        reason=f"revoked={revoked}",
    )
    return {"account_id": account.id, "player_id": account.player_id, "revoked_sessions": revoked}
