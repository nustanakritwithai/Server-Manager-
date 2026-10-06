"""Claim due events with FOR UPDATE SKIP LOCKED.

Each claim locks only rows this worker will handle. A second worker skips
locked rows instead of waiting, so running more than one worker is safe.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from simcore.constants import EventStatus
from simcore.errors import GameError
from simcore.game.processor import process_event
from simcore.models import Event


def claim_one(session: Session, now: datetime, *, worker_id: str = "worker") -> Event | None:
    event = session.scalars(
        select(Event)
        .where(Event.status == EventStatus.PENDING, Event.due_at <= now)
        .order_by(Event.due_at, Event.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if event is None:
        return None
    event.status = EventStatus.PROCESSING
    event.attempts += 1
    event.locked_at = now
    event.locked_by = worker_id
    session.flush()
    return event


def process_due_events(session: Session, now: datetime, *, limit: int = 50, worker_id: str = "inline") -> list[int]:
    """Process up to `limit` due events inside the caller's transaction.

    Effects are applied at each event's due_at, not at the later wall clock,
    so a worker that wakes up late still resolves the world as of the ETA.
    Newly scheduled events (the walk home) are claimed in the same loop when
    they are already due.
    """

    processed: list[int] = []
    for _ in range(limit):
        event = claim_one(session, now, worker_id=worker_id)
        if event is None:
            break
        process_event(session, event, event.due_at)
        processed.append(event.id)
    return processed


def run_event_now(session: Session, event_id: int, now: datetime, *, worker_id: str = "admin") -> Event:
    """Resolve one event immediately, even if its due_at is still in the future."""

    event = session.get(Event, event_id, with_for_update=True)
    if event is None:
        raise GameError("event not found", status_code=404, code="not_found")
    if event.status in (EventStatus.COMPLETED, EventStatus.CANCELLED):
        return event
    event.status = EventStatus.PROCESSING
    event.attempts += 1
    event.locked_at = now
    event.locked_by = worker_id
    process_event(session, event, now)
    return event
