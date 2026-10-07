"""Per-player command rate limit and optional Idempotency-Key replay.

New ``POST /v1/commands/*`` routes must call ``run_command``. The acting player
comes from the access token. The key is scoped to that player. A replay returns
the stored status and body and does not run the command again. A missing key
still runs the command once; clients that retry should send a key.

The idempotency row is not part of the world snapshot. Restore deletes it.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import TypeVar

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from simcore.errors import GameError
from simcore.models import CommandIdempotency, Player, utcnow
from simcore.player_auth import SlidingWindowLimiter
from simcore.snapshot import canonical_json

T = TypeVar("T")
_KEY_HEADER = "idempotency-key"


def run_command(
    request: Request,
    session: Session,
    player: Player,
    payload: object,
    fn: Callable[[], T],
) -> T | JSONResponse:
    key = _idempotency_key(request)
    fingerprint = _fingerprint(request.url.path, payload)
    if key:
        existing = _load(session, player.id, key)
        if existing is not None:
            if existing.request_hash != fingerprint:
                raise GameError(
                    "idempotency key was already used for a different request",
                    status_code=409,
                    code="idempotency_conflict",
                )
            return JSONResponse(status_code=existing.status_code, content=existing.response_body)
    if not _allow_command(request, player.id):
        return JSONResponse(
            status_code=429,
            content={"error": {"code": "rate_limited", "message": "too many commands"}},
        )
    try:
        with session.begin_nested():
            body = fn()
    except GameError as exc:
        content = {"error": {"code": exc.code, "message": exc.message}}
        if key:
            _store(session, player.id, key, fingerprint, exc.status_code, content)
            return JSONResponse(status_code=exc.status_code, content=content)
        raise
    encoded = jsonable_encoder(body)
    if key:
        raced = _store(session, player.id, key, fingerprint, 200, encoded)
        if raced is not None:
            return raced
    return encoded


def _allow_command(request: Request, player_id: int) -> bool:
    limiter: SlidingWindowLimiter | None = getattr(request.app.state, "command_limiter", None)
    if limiter is None:
        return True
    return limiter.allow(str(player_id), time.monotonic())


def _idempotency_key(request: Request) -> str | None:
    raw = request.headers.get(_KEY_HEADER)
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        raise GameError("idempotency key is empty", status_code=400, code="invalid_idempotency_key")
    if len(key) > 200 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise GameError(
            "idempotency key must be 1 to 200 visible ASCII characters",
            status_code=400,
            code="invalid_idempotency_key",
        )
    return key


def _fingerprint(path: str, payload: object) -> str:
    if hasattr(payload, "model_dump"):
        document = payload.model_dump(mode="json")
    else:
        document = payload
    raw = f"POST {path}\n{canonical_json(document)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load(session: Session, player_id: int, key: str) -> CommandIdempotency | None:
    return session.scalar(
        select(CommandIdempotency).where(
            CommandIdempotency.player_id == player_id,
            CommandIdempotency.idempotency_key == key,
        )
    )


def _store(
    session: Session,
    player_id: int,
    key: str,
    fingerprint: str,
    status_code: int,
    body: dict[str, object],
) -> JSONResponse | None:
    row = CommandIdempotency(
        player_id=player_id,
        idempotency_key=key,
        request_hash=fingerprint,
        status_code=status_code,
        response_body=body,
        created_at=utcnow(),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = _load(session, player_id, key)
        if existing is None:
            raise
        if existing.request_hash != fingerprint:
            raise GameError(
                "idempotency key was already used for a different request",
                status_code=409,
                code="idempotency_conflict",
            ) from None
        return JSONResponse(status_code=existing.status_code, content=existing.response_body)
    return None
