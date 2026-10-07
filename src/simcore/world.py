"""Maintenance gates and the monotonic world_version counter.

world_version increases when the simulation changes (commands, accrual, events,
clock advances). Snapshot restore writes the captured value back and does not
go through these helpers.

Workers and restore share WORKER_DRAIN_LOCK. A worker holds it in shared mode
for one event transaction. Restore takes it in exclusive mode, so it waits
until that transaction ends and blocks the next claim.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from simcore.errors import GameError

# Fits in PostgreSQL bigint. Not a secret; it only separates this lock from others.
WORKER_DRAIN_LOCK = 7424242


def require_commands_open(session: Session) -> None:
    from simcore.models import WorldState

    state = session.get(WorldState, 1)
    if state is None:
        raise RuntimeError("world_state row is missing; run migrations")
    if not state.commands_open:
        raise GameError(
            "world is in maintenance; player commands are not accepted",
            status_code=503,
            code="maintenance",
        )


def bump_world_version(session: Session) -> None:
    """Increment world_version atomically. Safe for two workers at once."""

    from simcore.models import WorldState

    session.execute(text("UPDATE world_state SET world_version = world_version + 1 WHERE id = 1"))
    state = session.get(WorldState, 1)
    if state is not None:
        session.expire(state, ["world_version"])
