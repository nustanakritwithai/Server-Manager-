"""Background worker. One event, one database transaction, then commit.

A crash before commit rolls the world back and the event stays pending.
The failure path records the error in a separate transaction and never
replays a committed loot grant.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import threading
import time
from datetime import timedelta

from sqlalchemy import text

from simcore.clock import Clock, OffsetClock, SystemClock
from simcore.config import get_settings
from simcore.constants import EventStatus
from simcore.db import get_sessionmaker
from simcore.game.queue import claim_one
from simcore.game.processor import process_event
from simcore.models import Event, WorldState, utcnow
from simcore.monitoring import observe_tick, sample_once
from simcore.world import WORKER_DRAIN_LOCK

logger = logging.getLogger("simcore.worker")

WORKER_STARTED_AT = utcnow()


def resolve_worker_id() -> str:
    """Id stored on a claimed event.

    The default is hostname and pid. ``SIMCORE_WORKER_ID`` overrides it so two
    CI processes can leave the same value on the world snapshot. Unset, the
    worker behaves as before.
    """

    override = os.environ.get("SIMCORE_WORKER_ID", "").strip()
    if override:
        return override[:80]
    return f"{socket.gethostname()}:{os.getpid()}"


def run_once(base_clock: Clock | None = None) -> tuple[str, int | None]:
    """Claim and resolve a single due event.

    Returns ("processed", id), ("failed", id), ("paused", None), or ("empty", None).

    The shared drain lock is held for this transaction. Snapshot restore takes
    the exclusive lock, so it waits for an in-flight event and then sees
    worker_paused. Two workers can still hold the shared lock together.

    After the event transaction finishes, the worker writes a heartbeat. That
    write is a separate transaction and cannot roll the world back.
    """

    base = base_clock or SystemClock()
    started = time.perf_counter()
    session = get_sessionmaker()()
    event_id: int | None = None
    status = "failed"
    try:
        with session.begin():
            session.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": WORKER_DRAIN_LOCK})
            state = session.get(WorldState, 1)
            if state is None:
                raise RuntimeError("world_state row is missing; run migrations")
            if state.worker_paused:
                status = "paused"
            else:
                clock = OffsetClock(session, base)
                event = claim_one(session, clock.now(), worker_id=resolve_worker_id())
                if event is None:
                    status = "empty"
                else:
                    event_id = event.id
                    # Resolve as of the scheduled instant so a late worker does not
                    # stretch travel or production past the ETA the client counted down.
                    process_event(session, event, event.due_at)
                    status = "processed"
        if status == "processed":
            logger.info("processed event %s", event_id)
    except Exception as exc:
        logger.exception("event %s failed", event_id)
        status = "failed"
        if event_id is not None:
            _record_failure(event_id, exc, base)
    finally:
        session.close()
    _observe(status, event_id, started)
    return status, event_id


def _observe(status: str, event_id: int | None, started: float) -> None:
    try:
        observe_tick(
            worker_id=resolve_worker_id(),
            started_at=WORKER_STARTED_AT,
            tick_status=status,
            tick_duration_ms=(time.perf_counter() - started) * 1000.0,
            events_processed=1 if status == "processed" else 0,
            event_id=event_id,
        )
    except Exception:
        logger.exception("worker heartbeat failed")


def _record_failure(event_id: int, exc: BaseException, base: Clock) -> None:
    settings = get_settings()
    session = get_sessionmaker()()
    try:
        with session.begin():
            event = session.get(Event, event_id, with_for_update=True)
            if event is None or event.status == EventStatus.COMPLETED:
                return
            now = OffsetClock(session, base).now()
            event.attempts += 1
            event.last_error = str(exc)[:2000]
            event.locked_by = None
            event.locked_at = None
            if event.attempts >= settings.max_event_attempts:
                event.status = EventStatus.FAILED
            else:
                event.status = EventStatus.PENDING
                backoff = min(60, 2 ** event.attempts)
                event.due_at = now + timedelta(seconds=backoff)
    finally:
        session.close()


def serve(
    base_clock: Clock | None = None,
    stop_event: threading.Event | None = None,
    poll_seconds: float | None = None,
) -> None:
    """Poll until the process is killed, or until stop_event is set.

    The standalone worker leaves stop_event empty and runs until the service
    stops it. The optional embedded worker passes a stop event so API shutdown
    can join the thread.
    """

    settings = get_settings()
    interval = settings.worker_poll_seconds if poll_seconds is None else poll_seconds
    logger.info("worker %s polling every %.2fs", resolve_worker_id(), interval)
    last_sample = time.monotonic()
    while stop_event is None or not stop_event.is_set():
        status, _event_id = run_once(base_clock)
        if settings.monitor_sample_seconds > 0 and time.monotonic() - last_sample >= settings.monitor_sample_seconds:
            try:
                sample_once(base_clock=base_clock, include_api=False, settings=settings)
            except Exception:
                logger.exception("worker monitoring sample failed")
            last_sample = time.monotonic()
        if status in ("empty", "paused"):
            if stop_event is None:
                time.sleep(interval)
            elif stop_event.wait(interval):
                return


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Process due world events")
    parser.add_argument("--once", action="store_true", help="Process a single due event and exit")
    args = parser.parse_args()
    if args.once:
        status, event_id = run_once()
        print(f"{status} {event_id or ''}".strip())
        return
    serve()


if __name__ == "__main__":
    main()
