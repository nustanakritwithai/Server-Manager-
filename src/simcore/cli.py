"""Dev admin CLI. Same operations as /v1/admin, for a shell on the VPS."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from simcore.clock import OffsetClock
from simcore.db import get_sessionmaker
from simcore.game.queue import run_event_now
from simcore.models import Army, Event, Transaction
from simcore.present import army_body


def _print(payload: object) -> None:
    print(json.dumps(payload, default=str, indent=2))


def _events(status: str | None, limit: int) -> None:
    session = get_sessionmaker()()
    try:
        stmt = select(Event).order_by(Event.due_at, Event.id).limit(limit)
        if status:
            stmt = select(Event).where(Event.status == status).order_by(Event.due_at, Event.id).limit(limit)
        rows = session.scalars(stmt).all()
        _print(
            [
                {
                    "id": row.id,
                    "type": row.type,
                    "status": row.status,
                    "due_at": row.due_at,
                    "attempts": row.attempts,
                    "idempotency_key": row.idempotency_key,
                    "movement_id": row.movement_id,
                    "last_error": row.last_error,
                }
                for row in rows
            ]
        )
    finally:
        session.close()


def _armies() -> None:
    session = get_sessionmaker()()
    try:
        clock = OffsetClock(session)
        now = clock.now()
        armies = session.scalars(select(Army).order_by(Army.id)).all()
        _print({"server_time": now, "armies": [army_body(session, army, now) for army in armies]})
    finally:
        session.close()


def _advance(seconds: int, minutes: int, hours: int) -> None:
    session = get_sessionmaker()()
    try:
        clock = OffsetClock(session)
        now = clock.advance(seconds=seconds, minutes=minutes, hours=hours)
        session.commit()
        _print({"server_time": now, "offset_seconds": clock.offset_seconds})
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _clock_now() -> None:
    session = get_sessionmaker()()
    try:
        clock = OffsetClock(session)
        _print({"server_time": clock.now(), "offset_seconds": clock.offset_seconds})
    finally:
        session.close()


def _run(event_id: int) -> None:
    session = get_sessionmaker()()
    try:
        clock = OffsetClock(session)
        event = run_event_now(session, event_id, clock.now(), worker_id="cli")
        session.commit()
        _print(
            {
                "id": event.id,
                "type": event.type,
                "status": event.status,
                "due_at": event.due_at,
                "processed_at": event.processed_at,
            }
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ledger(limit: int) -> None:
    session = get_sessionmaker()()
    try:
        rows = session.scalars(select(Transaction).order_by(Transaction.id.desc()).limit(limit)).all()
        _print(
            [
                {
                    "id": row.id,
                    "city_id": row.city_id,
                    "player_id": row.player_id,
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
        )
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and fast-forward the simulation")
    sub = parser.add_subparsers(dest="command", required=True)

    events = sub.add_parser("events", help="List world events")
    events.add_argument("--status", default=None)
    events.add_argument("--limit", type=int, default=100)

    sub.add_parser("armies", help="Where each army is, and its ETA when marching")

    now = sub.add_parser("clock", help="Show or advance simulated time")
    now_sub = now.add_subparsers(dest="clock_command", required=True)
    now_sub.add_parser("now")
    advance = now_sub.add_parser("advance")
    advance.add_argument("--seconds", type=int, default=0)
    advance.add_argument("--minutes", type=int, default=0)
    advance.add_argument("--hours", type=int, default=0)

    run = sub.add_parser("run-event", help="Resolve one event immediately")
    run.add_argument("--id", type=int, required=True)

    ledger = sub.add_parser("ledger", help="Show recent resource transactions")
    ledger.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()
    if args.command == "events":
        _events(args.status, args.limit)
    elif args.command == "armies":
        _armies()
    elif args.command == "clock" and args.clock_command == "now":
        _clock_now()
    elif args.command == "clock" and args.clock_command == "advance":
        _advance(args.seconds, args.minutes, args.hours)
    elif args.command == "run-event":
        _run(args.id)
    elif args.command == "ledger":
        _ledger(args.limit)
    else:
        parser.error("unknown command")


if __name__ == "__main__":
    main()
