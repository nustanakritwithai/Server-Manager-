from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from simcore.admin_auth import AdminSessionBook, read_admin_session, secrets_equal
from simcore.auth import DEV_TOKEN_PREFIX, parse_dev_token
from simcore.clock import OffsetClock
from simcore.config import Settings
from simcore.db import get_sessionmaker
from simcore.errors import GameError
from simcore.models import Player, PlayerAccount, PlayerRefreshSession
from simcore.player_auth import AccessClaims, account_for_player, read_access_token

_bearer = HTTPBearer(auto_error=False)


def get_session() -> Iterator[Session]:
    """Request session.

    Callers must use ``Depends(get_session, scope="function")``. FastAPI's
    default scope commits after the response body is written, so the next
    request can start before this commit. Function scope commits first.
    """

    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_clock(request: Request, session: Session = Depends(get_session, scope="function")) -> OffsetClock:
    return OffsetClock(session, request.app.state.base_clock)


def get_authenticated_player(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Session = Depends(get_session, scope="function"),
) -> Player:
    """Player from the bearer token. Allows a password-change session.

    The id in the token is the only identity. Request bodies are not consulted.
    """

    request.state.player_access = None
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise GameError("missing bearer token", status_code=401, code="unauthorized")
    settings: Settings = request.app.state.settings
    token = credentials.credentials or ""
    if token.startswith(DEV_TOKEN_PREFIX):
        return _player_from_dev_token(token, settings, session)
    claims = read_access_token(token, settings)
    if claims is None:
        raise GameError("invalid or expired token", status_code=401, code="unauthorized")
    player = _player_from_access(claims, session)
    request.state.player_access = claims
    return player


def get_current_player(
    request: Request,
    player: Annotated[Player, Depends(get_authenticated_player)],
    session: Session = Depends(get_session, scope="function"),
) -> Player:
    """Authenticated player who is allowed to play.

    A temporary password must be changed before cities, the map, or commands.
    """

    account = account_for_player(session, player.id)
    if account is not None and account.must_change_password:
        raise GameError(
            "password change required",
            status_code=403,
            code="password_change_required",
        )
    return player


def _player_from_dev_token(token: str, settings: Settings, session: Session) -> Player:
    if not settings.dev_login_enabled:
        raise GameError("invalid or expired token", status_code=401, code="unauthorized")
    player_id = parse_dev_token(token)
    player = session.get(Player, player_id)
    if player is None:
        raise GameError("invalid or expired token", status_code=401, code="unauthorized")
    account = account_for_player(session, player.id)
    if account is not None and account.locked:
        raise GameError("account is locked", status_code=403, code="account_locked")
    return player


def _player_from_access(claims: AccessClaims, session: Session) -> Player:
    row = session.get(PlayerRefreshSession, claims.session_id)
    if row is None or row.revoked_at is not None or row.account_id != claims.account_id:
        raise GameError("invalid or expired token", status_code=401, code="unauthorized")
    account = session.get(PlayerAccount, claims.account_id)
    if account is None or account.player_id != claims.player_id:
        raise GameError("invalid or expired token", status_code=401, code="unauthorized")
    if account.locked:
        raise GameError("account is locked", status_code=403, code="account_locked")
    player = session.get(Player, claims.player_id)
    if player is None:
        raise GameError("invalid or expired token", status_code=401, code="unauthorized")
    return player


def require_admin(
    request: Request,
    x_admin_token: Annotated[str | None, Header()] = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Settings:
    settings: Settings = request.app.state.settings
    if not settings.admin_enabled:
        raise GameError("admin API is disabled", status_code=404, code="not_found")
    book: AdminSessionBook = request.app.state.admin_sessions
    bearer = ""
    if credentials is not None and credentials.scheme.lower() == "bearer":
        bearer = credentials.credentials or ""
    if bearer and read_admin_session(bearer, settings, book) is not None:
        return settings
    header = x_admin_token or ""
    if header and secrets_equal(header, settings.admin_token):
        return settings
    raise GameError("invalid admin token", status_code=401, code="unauthorized")
