"""Read-only world checks used after every failure scenario.

Each check is PASS, FAIL, or INCOMPLETE. A query that cannot be run is
INCOMPLETE with the exception class, not a guessed pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from simcore.audit import verify_chain
from simcore.backup import ledger_failures
from simcore.clock import SystemClock
from simcore.constants import RESOURCES
from simcore.db import get_sessionmaker
from simcore.models import WorldState
from simcore.snapshot import world_checksum


def _check(name: str, status: str, detail: str, **data: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "status": status, "detail": detail, "required": True}
    if data:
        body["data"] = data
    return body


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def open_readonly() -> Session:
    session = get_sessionmaker()()
    session.execute(text("SET TRANSACTION READ ONLY"))
    return session


def game_now(session: Session) -> datetime:
    state = session.get(WorldState, 1)
    if state is None:
        raise RuntimeError("world_state row is missing")
    return SystemClock().now() + timedelta(seconds=int(state.offset_seconds))


def event_counts(session: Session) -> dict[str, int]:
    now = game_now(session)
    rows = session.execute(
        text(
            """
            SELECT status, count(*)::int
            FROM events
            GROUP BY status
            """
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    due = session.execute(
        text(
            """
            SELECT count(*)::int
            FROM events
            WHERE status IN ('pending', 'processing')
              AND due_at <= :now
            """
        ),
        {"now": now},
    ).scalar_one()
    counts["due"] = int(due or 0)
    return counts


def check_invariants(session: Session) -> list[dict[str, Any]]:
    """Score the quiescent world. Caller rolls the session back."""

    checks: list[dict[str, Any]] = []
    checks.append(_ledger(session))
    checks.append(_events_once(session))
    checks.append(_battle_reports(session))
    checks.append(_armies(session))
    checks.append(_checksum(session))
    checks.append(_audit(session))
    checks.append(_gates(session))
    checks.append(_negative_stock(session))
    return checks


def _ledger(session: Session) -> dict[str, Any]:
    try:
        failures = ledger_failures(session)
    except Exception as exc:
        return _check("ledger_conservation", "INCOMPLETE", f"ledger query failed: {exc.__class__.__name__}")
    if failures:
        return _check("ledger_conservation", "FAIL", "; ".join(failures[:8]), failure_count=len(failures))
    return _check("ledger_conservation", "PASS", "city columns match the ledger chain")


def _events_once(session: Session) -> dict[str, Any]:
    try:
        counts = event_counts(session)
        duplicate_marks = session.execute(
            text(
                """
                SELECT event_id, count(*)::int AS n
                FROM worker_process_marks
                WHERE outcome = 'processed' AND event_id IS NOT NULL
                GROUP BY event_id
                HAVING count(*) > 1
                """
            )
        ).all()
        duplicate_keys = session.execute(
            text(
                """
                SELECT idempotency_key, count(*)::int AS n
                FROM events
                GROUP BY idempotency_key
                HAVING count(*) > 1
                """
            )
        ).all()
    except Exception as exc:
        return _check("events_exactly_once", "INCOMPLETE", f"event query failed: {exc.__class__.__name__}")
    problems: list[str] = []
    if counts.get("failed", 0):
        problems.append(f"{counts['failed']} events are failed")
    if counts.get("processing", 0):
        problems.append(f"{counts['processing']} events are still processing")
    if counts.get("due", 0):
        problems.append(f"{counts['due']} events are due and not completed")
    if duplicate_marks:
        problems.append(f"{len(duplicate_marks)} events have more than one processed mark")
    if duplicate_keys:
        problems.append(f"{len(duplicate_keys)} event idempotency keys are duplicated")
    detail = (
        "no due event left pending, processing, or failed, and no processed mark or event key is duplicated"
        if not problems
        else "; ".join(problems)
    )
    return _check(
        "events_exactly_once",
        "FAIL" if problems else "PASS",
        detail,
        counts=counts,
        duplicate_processed_marks=len(duplicate_marks),
    )


def _battle_reports(session: Session) -> dict[str, Any]:
    try:
        rows = session.execute(
            text(
                """
                SELECT event_id, count(*)::int AS n
                FROM battle_reports
                GROUP BY event_id
                HAVING count(*) > 1
                """
            )
        ).all()
        total = int(session.execute(text("SELECT count(*)::int FROM battle_reports")).scalar_one() or 0)
    except Exception as exc:
        return _check("battle_reports_unique", "INCOMPLETE", f"battle report query failed: {exc.__class__.__name__}")
    if rows:
        return _check(
            "battle_reports_unique",
            "FAIL",
            f"{len(rows)} events have more than one battle report",
            reports=total,
        )
    return _check("battle_reports_unique", "PASS", f"{total} battle reports, one per event at most", reports=total)


def _armies(session: Session) -> dict[str, Any]:
    try:
        two_moves = session.execute(
            text(
                """
                SELECT army_id, count(*)::int AS n
                FROM movements
                WHERE status = 'in_progress'
                GROUP BY army_id
                HAVING count(*) > 1
                """
            )
        ).all()
        garrison_and_march = session.execute(
            text(
                """
                SELECT a.id
                FROM armies a
                JOIN movements m ON m.army_id = a.id AND m.status = 'in_progress'
                WHERE a.status = 'garrisoned'
                """
            )
        ).all()
        located_while_moving = session.execute(
            text(
                """
                SELECT id
                FROM armies
                WHERE status IN ('marching', 'returning')
                  AND location_city_id IS NOT NULL
                """
            )
        ).all()
        garrison_without_city = session.execute(
            text(
                """
                SELECT id
                FROM armies
                WHERE status = 'garrisoned'
                  AND location_city_id IS NULL
                """
            )
        ).all()
    except Exception as exc:
        return _check("army_one_place", "INCOMPLETE", f"army query failed: {exc.__class__.__name__}")
    problems: list[str] = []
    if two_moves:
        problems.append(f"{len(two_moves)} armies have two in-progress movements")
    if garrison_and_march:
        problems.append(f"{len(garrison_and_march)} garrisoned armies also have an in-progress movement")
    if located_while_moving:
        problems.append(f"{len(located_while_moving)} moving armies still have a location city")
    if garrison_without_city:
        problems.append(f"{len(garrison_without_city)} garrisoned armies have no location city")
    if problems:
        return _check("army_one_place", "FAIL", "; ".join(problems))
    return _check("army_one_place", "PASS", "no army is in two places")


def _checksum(session: Session) -> dict[str, Any]:
    try:
        first = world_checksum(session)
        second = world_checksum(session)
    except Exception as exc:
        return _check("world_checksum", "INCOMPLETE", f"checksum failed: {exc.__class__.__name__}")
    if first != second:
        return _check("world_checksum", "FAIL", "two reads of world_checksum() differed", first=first, second=second)
    return _check("world_checksum", "PASS", first, checksum=first)


def _audit(session: Session) -> dict[str, Any]:
    try:
        chain = verify_chain(session)
    except Exception as exc:
        return _check("audit_hash_chain", "INCOMPLETE", f"audit query failed: {exc.__class__.__name__}")
    status = str(chain.get("status") or "INCOMPLETE")
    if status not in {"PASS", "FAIL", "INCOMPLETE"}:
        status = "FAIL"
    reasons = chain.get("reasons") or []
    detail = "hash chain matches" if status == "PASS" else "; ".join(str(item) for item in reasons[:6]) or status
    return _check(
        "audit_hash_chain",
        status,
        detail,
        checked_rows=chain.get("checked_rows"),
    )


def _gates(session: Session) -> dict[str, Any]:
    state = session.get(WorldState, 1)
    if state is None:
        return _check("world_gates", "FAIL", "world_state row is missing")
    if state.commands_open and not state.worker_paused and not state.restore_active:
        return _check("world_gates", "PASS", "commands are open and the worker is not paused")
    return _check(
        "world_gates",
        "FAIL",
        "maintenance gates were left closed",
        commands_open=bool(state.commands_open),
        worker_paused=bool(state.worker_paused),
        restore_active=bool(state.restore_active),
    )


def _negative_stock(session: Session) -> dict[str, Any]:
    try:
        negatives = []
        rows = session.execute(text("SELECT id, wood, food, iron, gold FROM cities")).all()
        for row in rows:
            for name in RESOURCES:
                if int(getattr(row, name)) < 0:
                    negatives.append(f"city {row.id} {name}")
    except Exception as exc:
        return _check("negative_resources", "INCOMPLETE", f"city query failed: {exc.__class__.__name__}")
    if negatives:
        return _check("negative_resources", "FAIL", ", ".join(negatives[:8]))
    return _check("negative_resources", "PASS", "no city stock is negative")


def lag_samples(session: Session) -> dict[str, Any]:
    """Game-time processed_at minus due_at for completed events.

    The worker stores processed_at as the game time it resolved the event.
    This query reports that difference. It does not convert wall clock time.
    """

    try:
        rows = session.execute(
            text(
                """
                SELECT EXTRACT(EPOCH FROM (processed_at - due_at)) AS lag
                FROM events
                WHERE status = 'completed'
                  AND processed_at IS NOT NULL
                  AND due_at IS NOT NULL
                """
            )
        ).all()
        missing = int(
            session.execute(
                text(
                    """
                    SELECT count(*)::int FROM events
                    WHERE status = 'completed' AND processed_at IS NULL
                    """
                )
            ).scalar_one()
            or 0
        )
    except Exception as exc:
        return {"status": "UNKNOWN", "reason": f"lag query failed: {exc.__class__.__name__}", "samples": []}
    values = [float(row.lag) for row in rows if row.lag is not None]
    return {"status": "MEASURED" if values else "UNKNOWN", "samples": values, "completed_without_processed_at": missing}


def wall_completion_lag(session: Session) -> dict[str, Any]:
    """Wall-clock mark time minus game-time due_at.

    Those clocks are the same only while offset_seconds is 0. After a clock
    advance the difference is not a lag, so this returns NOT INSTRUMENTED
    instead of a number.
    """

    try:
        offset = session.execute(text("SELECT offset_seconds FROM world_state WHERE id = 1")).scalar_one()
    except Exception as exc:
        return {"status": "UNKNOWN", "reason": f"offset query failed: {exc.__class__.__name__}"}
    if int(offset or 0) != 0:
        return {
            "status": "NOT INSTRUMENTED",
            "reason": (
                "world_state.offset_seconds is not 0. worker_process_marks.wall_at is wall time and "
                "events.due_at is game time, so their difference is not a processing lag."
            ),
            "offset_seconds": int(offset),
        }
    try:
        rows = session.execute(
            text(
                """
                SELECT EXTRACT(EPOCH FROM (m.wall_at - e.due_at)) AS lag
                FROM worker_process_marks m
                JOIN events e ON e.id = m.event_id
                WHERE m.outcome = 'processed'
                  AND e.due_at IS NOT NULL
                """
            )
        ).all()
    except Exception as exc:
        return {"status": "UNKNOWN", "reason": f"wall lag query failed: {exc.__class__.__name__}"}
    values = [float(row.lag) for row in rows if row.lag is not None]
    return {"status": "MEASURED" if values else "UNKNOWN", "samples": values, "offset_seconds": 0}


def due_spread(session: Session) -> dict[str, Any]:
    """Spread of due_at across pending events, in seconds. Measured, not rounded into zero."""

    try:
        row = session.execute(
            text(
                """
                SELECT min(due_at) AS earliest, max(due_at) AS latest, count(*)::int AS n
                FROM events
                WHERE status = 'pending'
                """
            )
        ).one()
    except Exception as exc:
        return {"status": "UNKNOWN", "reason": exc.__class__.__name__}
    if not row.n or row.earliest is None or row.latest is None:
        return {"status": "MEASURED", "pending": 0, "spread_seconds": None}
    spread = (_aware(row.latest) - _aware(row.earliest)).total_seconds()
    return {"status": "MEASURED", "pending": int(row.n), "spread_seconds": spread}


def with_invariants(prefix: str) -> list[dict[str, Any]]:
    """Run the invariant set and prefix each name so scenarios stay distinct in the report."""

    session = open_readonly()
    try:
        checks = check_invariants(session)
    except Exception as exc:
        return [_check(f"{prefix}.invariants", "INCOMPLETE", f"invariant session failed: {exc.__class__.__name__}")]
    finally:
        session.rollback()
        session.close()
    renamed = []
    for item in checks:
        copied = dict(item)
        copied["name"] = f"{prefix}.{item['name']}"
        renamed.append(copied)
    return renamed
