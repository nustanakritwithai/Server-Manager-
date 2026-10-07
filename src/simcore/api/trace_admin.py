"""Read-only trace and audit routes. Same admin auth as the rest of /v1/admin."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from simcore.api.deps import get_session, require_admin
from simcore.audit import list_audit
from simcore.config import Settings
from simcore.trace_report import build_trace, search_traces

trace_router = APIRouter(prefix="/trace", tags=["admin"])
audit_router = APIRouter(prefix="/audit", tags=["admin"])


@trace_router.get("")
def admin_search_traces(
    session: Annotated[Session, Depends(get_session, scope="function")],
    _: Annotated[Settings, Depends(require_admin)],
    player: int | None = None,
    army: int | None = None,
    event: int | None = None,
    command: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """Find traces from a player, army, event, or command. Null trace ids stay LEGACY."""

    return search_traces(
        session,
        player_id=player,
        army_id=army,
        event_id=event,
        command_id=command,
        limit=limit,
        offset=offset,
    )


@trace_router.get("/{trace_id}")
def admin_get_trace(
    trace_id: str,
    session: Annotated[Session, Depends(get_session, scope="function")],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    """Ordered timeline and the server-side integrity verdict for one command."""

    return build_trace(session, trace_id)


@audit_router.get("")
def admin_list_audit(
    session: Annotated[Session, Depends(get_session, scope="function")],
    _: Annotated[Settings, Depends(require_admin)],
    actor: str | None = None,
    action: str | None = None,
    result: str | None = None,
    target: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """One page of the audit log, plus verification of the whole hash chain."""

    return list_audit(
        session,
        limit=limit,
        offset=offset,
        actor=actor,
        action=action,
        result=result,
        target=target,
    )
