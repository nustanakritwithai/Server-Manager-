from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from simcore.api.deps import get_clock, get_session, require_admin
from simcore.clock import OffsetClock
from simcore.config import Settings
from simcore.game.queue import run_event_now
from simcore.models import Army, Event, Transaction
from simcore.present import army_body
from simcore.api.snapshots import router as snapshot_router
from simcore.worker import run_once

router = APIRouter(prefix="/v1/admin", tags=["admin"])
router.include_router(snapshot_router)


class AdvanceIn(BaseModel):
    seconds: int = 0
    minutes: int = 0
    hours: int = 0


@router.get("/events")
def list_events(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    stmt = select(Event).order_by(Event.due_at, Event.id).limit(limit)
    if status is not None:
        stmt = select(Event).where(Event.status == status).order_by(Event.due_at, Event.id).limit(limit)
    events = session.scalars(stmt).all()
    return {
        "events": [
            {
                "id": event.id,
                "type": event.type,
                "status": event.status,
                "due_at": event.due_at,
                "attempts": event.attempts,
                "idempotency_key": event.idempotency_key,
                "movement_id": event.movement_id,
                "payload": event.payload,
                "locked_by": event.locked_by,
                "last_error": event.last_error,
                "processed_at": event.processed_at,
            }
            for event in events
        ]
    }


@router.get("/armies")
def list_armies(
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    now = clock.now()
    armies = session.scalars(select(Army).order_by(Army.id)).all()
    return {"server_time": now, "armies": [army_body(session, army, now) for army in armies]}


@router.get("/transactions")
def list_transactions(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    rows = session.scalars(select(Transaction).order_by(Transaction.id.desc()).limit(limit)).all()
    return {
        "transactions": [
            {
                "id": row.id,
                "player_id": row.player_id,
                "city_id": row.city_id,
                "resource": row.resource,
                "delta": row.delta,
                "balance_after": row.balance_after,
                "reason": row.reason,
                "source_event_id": row.source_event_id,
                "idempotency_key": row.idempotency_key,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


@router.post("/clock/advance")
def advance_clock(
    body: AdvanceIn,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    if body.seconds < 0 or body.minutes < 0 or body.hours < 0:
        from simcore.errors import GameError

        raise GameError("cannot rewind time", code="invalid_command")
    now = clock.advance(seconds=body.seconds, minutes=body.minutes, hours=body.hours)
    session.flush()
    return {"server_time": now, "offset_seconds": clock.offset_seconds}


@router.post("/events/{event_id}/run")
def run_event(
    event_id: int,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    event = run_event_now(session, event_id, clock.now(), worker_id="admin")
    return {
        "id": event.id,
        "type": event.type,
        "status": event.status,
        "due_at": event.due_at,
        "processed_at": event.processed_at,
        "attempts": event.attempts,
    }


@router.post("/worker/tick")
def worker_tick(
    request: Request,
    _: Annotated[Settings, Depends(require_admin)],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    """Run the real worker path. Each event commits in its own transaction."""

    processed: list[int] = []
    failed: list[int] = []
    paused = False
    base = request.app.state.base_clock
    for _ in range(limit):
        status, event_id = run_once(base)
        if status == "paused":
            paused = True
            break
        if status == "empty":
            break
        if status == "processed" and event_id is not None:
            processed.append(event_id)
        elif event_id is not None:
            failed.append(event_id)
    return {
        "processed": len(processed),
        "failed": len(failed),
        "paused": paused,
        "event_ids": processed,
        "failed_ids": failed,
    }
