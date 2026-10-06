"""Two workers can claim different due events without blocking each other."""

from __future__ import annotations

from sqlalchemy import text

from simcore.constants import EventStatus
from simcore.db import get_sessionmaker
from simcore.game.queue import claim_one
from simcore.models import Event


def test_skip_locked_claims_distinct_events(db, frozen) -> None:
    now = frozen.now()
    session = get_sessionmaker()()
    try:
        session.add_all(
            [
                Event(
                    due_at=now,
                    type="BUILD_COMPLETE",
                    payload={"n": index},
                    status=EventStatus.PENDING,
                    attempts=0,
                    idempotency_key=f"lock-{index}",
                    created_at=now,
                )
                for index in (1, 2)
            ]
        )
        session.commit()
    finally:
        session.close()

    first = get_sessionmaker()()
    second = get_sessionmaker()()
    try:
        first.begin()
        second.begin()
        second.execute(text("SET LOCAL lock_timeout = '3s'"))
        claimed_first = claim_one(first, now, worker_id="worker-a")
        claimed_second = claim_one(second, now, worker_id="worker-b")
        assert claimed_first is not None and claimed_second is not None
        assert claimed_first.id != claimed_second.id
        assert {claimed_first.locked_by, claimed_second.locked_by} == {"worker-a", "worker-b"}
    finally:
        first.rollback()
        second.rollback()
        first.close()
        second.close()
