from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from simcore.admin_auth import AdminSessionBook, read_admin_session, secrets_equal
from simcore.auth import parse_dev_token
from simcore.clock import OffsetClock
from simcore.config import Settings
from simcore.db import get_sessionmaker
from simcore.errors import GameError
from simcore.models import Player

_bearer = HTTPBearer(auto_error=False)


def get_session() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_clock(request: Request, session: Session = Depends(get_session)) -> OffsetClock:
    return OffsetClock(session, request.app.state.base_clock)


def get_current_player(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Session = Depends(get_session),
) -> Player:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise GameError("missing bearer token", status_code=401, code="unauthorized")
    player_id = parse_dev_token(credentials.credentials)
    player = session.get(Player, player_id)
    if player is None:
        raise GameError("unknown player", status_code=401, code="unauthorized")
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
