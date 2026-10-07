"""Injectable game clock.

Wall time plus a persisted offset lets the API and the worker share one
simulated timeline. Tests pass a FrozenClock as the base so fast-forward is
exact and independent of the host clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy.orm import Session


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """A clock that moves only when tests or debug code call advance."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("clock instant must be timezone-aware")
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def advance(self, *, seconds: int = 0, minutes: int = 0, hours: int = 0) -> datetime:
        self._instant = self._instant + timedelta(seconds=seconds, minutes=minutes, hours=hours)
        return self._instant


class OffsetClock:
    """Game time = base.now() + world_state.offset_seconds.

    `advance` persists the offset so every process observes it.
    """

    def __init__(self, session: Session, base: Clock | None = None) -> None:
        self.session = session
        self.base = base or SystemClock()

    def now(self) -> datetime:
        from simcore.models import WorldState

        state = self.session.get(WorldState, 1)
        if state is None:
            raise RuntimeError("world_state row is missing; run migrations")
        return self.base.now() + timedelta(seconds=state.offset_seconds)

    def advance(self, *, seconds: int = 0, minutes: int = 0, hours: int = 0) -> datetime:
        from simcore.models import WorldState

        delta = seconds + minutes * 60 + hours * 3600
        if delta < 0:
            raise ValueError("cannot rewind the game clock")
        state = self.session.get(WorldState, 1, with_for_update=True)
        if state is None:
            raise RuntimeError("world_state row is missing; run migrations")
        if not state.commands_open or state.worker_paused:
            from simcore.errors import GameError

            raise GameError(
                "world is in maintenance; the clock cannot advance",
                status_code=409,
                code="maintenance",
            )
        state.offset_seconds += delta
        state.world_version = int(state.world_version) + 1
        self.session.flush()
        return self.now()

    @property
    def offset_seconds(self) -> int:
        from simcore.models import WorldState

        state = self.session.get(WorldState, 1)
        if state is None:
            raise RuntimeError("world_state row is missing; run migrations")
        return state.offset_seconds
