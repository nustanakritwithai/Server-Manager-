"""Post-run checks.

Trace verdicts, the audit hash chain, monitoring, and the snapshot checksum
come from the admin HTTP API. The server already computed them. Ledger,
army, and event checks are read-only queries. The session is READ ONLY and
is rolled back.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from simcore.constants import RESOURCES, ArmyStatus, EventStatus, EventType, MovementStatus, Reason
from simcore.db import get_sessionmaker
from simcore.models import Army, BattleReport, City, Event, Movement, PlayerCommand, Transaction
from simcore.sim.http import ApiClient
from simcore.sim.seed_world import expected_army_count, opening_resources, opening_units
from simcore.snapshot import world_checksum

_ALLOWED_REASONS = frozenset(
    {
        Reason.PRODUCTION,
        Reason.UPKEEP,
        Reason.LOOT_LOST,
        Reason.LOOT_GAINED,
        Reason.TRAIN,
        Reason.FOUND_CITY,
        Reason.TRANSFER_OUT,
        Reason.TRANSFER_IN,
    }
)
_SEEDED_MODES = frozenset({"ci", "full"})
_PASS = "PASS"
_FAIL = "FAIL"
_NOT_CHECKED = "NOT CHECKED"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _open_readonly() -> Session:
    session = get_sessionmaker()()
    session.execute(text("SET TRANSACTION READ ONLY"))
    return session


def _fail(name: str, detail: str, *, trace_id: str | None = None) -> dict[str, Any]:
    return {"invariant": name, "status": _FAIL, "trace_id": trace_id, "detail": detail}


def _pass(name: str, detail: str) -> dict[str, Any]:
    return {"invariant": name, "status": _PASS, "trace_id": None, "detail": detail, "required": True}


def verify_run(
    api: ApiClient,
    *,
    mode: str,
    player_count: int | None,
    thresholds: dict[str, Any],
    max_event_lag_seconds: float,
    roster: str = "default",
) -> dict[str, Any]:
    """Score the world. ``player_count`` is set when CI or full mode seeded the map."""

    monitoring = _read_monitoring(api)
    end_lag = _lag_value(monitoring)
    traces = _read_traces(api)
    audit = _read_audit(api)
    snapshot = (
        _read_snapshot(api)
        if mode in _SEEDED_MODES or thresholds.get("snapshot_checksum_required")
        else _snapshot_skipped()
    )

    session = _open_readonly()
    try:
        db = _db_checks(session, player_count=player_count, roster=roster)
        live_checksum = world_checksum(session)
    finally:
        session.rollback()
        session.close()

    failed: list[dict[str, Any]] = []
    invariants: list[dict[str, Any]] = []

    invariants.append(_score_traces(traces, failed))
    invariants.append(_score_legacy(db, thresholds, failed))
    invariants.append(_score_production(traces, failed))
    invariants.extend(_score_db(db, failed))
    invariants.append(_score_audit(audit, failed))
    invariants.extend(_score_monitoring(monitoring, failed))
    invariants.append(_score_lag(end_lag, max_event_lag_seconds, thresholds, failed))
    invariants.append(_score_snapshot(snapshot, live_checksum, mode, failed))

    return {
        "invariants": invariants,
        "failed": failed,
        "traces": traces["summary"],
        "trace_failures": traces["failures"],
        "legacy": traces["legacy"],
        "not_checked": traces["not_checked"],
        "audit_chain": audit,
        "monitoring": monitoring["summary"],
        "monitoring_checks": monitoring["checks"],
        "end_event_lag_seconds": end_lag,
        "max_event_lag_seconds": max_event_lag_seconds,
        "snapshot": snapshot,
        "live_checksum": live_checksum,
        "counts": db["counts"],
    }


def _read_monitoring(api: ApiClient) -> dict[str, Any]:
    status, body = api.json("GET", "/v1/admin/monitoring", headers=api.admin_headers)
    if status != 200 or not isinstance(body, dict):
        return {
            "summary": {
                "overall": "UNKNOWN",
                "critical": [{"name": "monitoring", "status": "UNKNOWN", "reason": f"HTTP {status}"}],
                "unknown": [{"name": "monitoring", "status": "UNKNOWN"}],
                "not_instrumented": [],
                "warn": [],
                "ok": [],
            },
            "checks": [],
            "raw_overall": None,
        }
    critical = []
    unknown = []
    not_instrumented = []
    warn = []
    ok = []
    checks = []
    for check in body.get("checks") or []:
        if not isinstance(check, dict):
            continue
        name = str(check.get("name") or "")
        state = str(check.get("status") or "UNKNOWN")
        short = {"name": name, "status": state, "value": check.get("value"), "reason": check.get("reason")}
        checks.append(short)
        if state == "CRITICAL":
            critical.append(short)
        elif state == "NOT INSTRUMENTED":
            not_instrumented.append(short)
        elif state == "UNKNOWN":
            unknown.append(short)
        elif state == "WARN":
            warn.append(short)
        elif state == "OK":
            ok.append(short)
        else:
            unknown.append(short)
    overall = body.get("overall") if isinstance(body.get("overall"), dict) else {}
    return {
        "summary": {
            "overall": overall.get("status", "UNKNOWN"),
            "overall_reason": overall.get("reason"),
            "critical": critical,
            "unknown": unknown,
            "not_instrumented": not_instrumented,
            "warn": warn,
            "ok_count": len(ok),
        },
        "checks": checks,
        "raw_overall": overall,
    }


def _lag_value(monitoring: dict[str, Any]) -> float | None:
    for check in monitoring["checks"]:
        if check["name"] == "event_queue.lag" and isinstance(check.get("value"), (int, float)):
            return float(check["value"])
    return None


def _read_traces(api: ApiClient) -> dict[str, Any]:
    traces: list[dict[str, Any]] = []
    offset = 0
    total = None
    legacy_search: list[dict[str, Any]] = []
    while True:
        status, body = api.json(
            "GET",
            "/v1/admin/trace",
            headers=api.admin_headers,
            params={"limit": 200, "offset": offset},
        )
        if status != 200 or not isinstance(body, dict):
            return {
                "summary": {"PASS": 0, "FAIL": 0, "INCOMPLETE": 0, "LEGACY": 0, "error": f"HTTP {status}"},
                "failures": [{"trace_id": None, "detail": f"trace search HTTP {status}"}],
                "legacy": [],
                "not_checked": [],
                "rows": [],
            }
        page = body.get("traces") or []
        traces.extend(page)
        if offset == 0:
            legacy_search = list(body.get("legacy") or [])
        total = int(body.get("total") or 0)
        offset += len(page)
        if not page or offset >= total:
            break

    verdicts = {"PASS": 0, "FAIL": 0, "INCOMPLETE": 0}
    failures: list[dict[str, Any]] = []
    not_checked: list[dict[str, Any]] = []
    production: list[dict[str, Any]] = []
    incomplete_without_pending = 0
    rows: list[dict[str, Any]] = []
    for entry in traces:
        trace_id = str(entry.get("trace_id"))
        status, detail = api.json("GET", f"/v1/admin/trace/{trace_id}", headers=api.admin_headers)
        if status != 200 or not isinstance(detail, dict):
            verdicts["FAIL"] = verdicts.get("FAIL", 0) + 1
            failures.append({"trace_id": trace_id, "detail": f"trace fetch HTTP {status}"})
            continue
        verdict = str(detail.get("verdict") or "FAIL")
        if verdict not in verdicts:
            verdicts["FAIL"] += 1
            failures.append({"trace_id": trace_id, "detail": f"unexpected verdict {verdict}"})
            continue
        verdicts[verdict] += 1
        integrity = detail.get("integrity") if isinstance(detail.get("integrity"), dict) else {}
        for item in integrity.get("not_checked") or []:
            if isinstance(item, dict):
                not_checked.append({"trace_id": trace_id, "name": item.get("name"), "status": item.get("status")})
        for check in integrity.get("checks") or []:
            if isinstance(check, dict) and check.get("name") == "production_upkeep":
                production.append(
                    {
                        "trace_id": trace_id,
                        "status": str(check.get("status") or _NOT_CHECKED),
                        "detail": check.get("detail"),
                    }
                )
        pending = _trace_still_open(detail)
        if verdict == "INCOMPLETE" and not pending:
            incomplete_without_pending += 1
            failures.append(
                {
                    "trace_id": trace_id,
                    "detail": "INCOMPLETE but no event or movement on this trace is still open",
                }
            )
        elif verdict == "FAIL":
            reasons = detail.get("reasons") or []
            failures.append({"trace_id": trace_id, "detail": "; ".join(str(reason) for reason in reasons)})
        rows.append({"trace_id": trace_id, "verdict": verdict, "pending": pending})

    return {
        "summary": {
            **verdicts,
            "LEGACY": len(legacy_search),
            "incomplete_without_pending": incomplete_without_pending,
            "total": len(rows),
        },
        "failures": failures,
        "legacy": legacy_search,
        "not_checked": not_checked,
        "production": production,
        "rows": rows,
    }


def _trace_still_open(detail: dict[str, Any]) -> bool:
    for step in detail.get("steps") or []:
        if not isinstance(step, dict):
            continue
        fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
        status = fields.get("status")
        if status in {EventStatus.PENDING, EventStatus.PROCESSING, MovementStatus.IN_PROGRESS}:
            return True
    return False


def _read_audit(api: ApiClient) -> dict[str, Any]:
    status, body = api.json("GET", "/v1/admin/audit", headers=api.admin_headers, params={"limit": 1})
    if status != 200 or not isinstance(body, dict):
        return {"status": "UNKNOWN", "checked_rows": None, "reasons": [f"HTTP {status}"]}
    chain = body.get("chain") if isinstance(body.get("chain"), dict) else {}
    return {
        "status": chain.get("status", "UNKNOWN"),
        "checked_rows": chain.get("checked_rows"),
        "reasons": list(chain.get("reasons") or []),
    }


def _read_snapshot(api: ApiClient) -> dict[str, Any]:
    status, body = api.json(
        "POST",
        "/v1/admin/snapshots",
        headers={**api.admin_headers, "content-type": "application/json"},
        json={"reason": "MANUAL"},
    )
    if status != 200 or not isinstance(body, dict):
        return {"status": "FAIL", "checksum": None, "snapshot_id": None, "detail": f"create HTTP {status}"}
    snapshot_id = body.get("snapshot_id")
    checksum = body.get("checksum")
    inspect_status, inspected = api.json(
        "GET",
        f"/v1/admin/snapshots/{snapshot_id}/inspect",
        headers=api.admin_headers,
    )
    checksum_ok = isinstance(inspected, dict) and inspected.get("checksum_ok") is True
    return {
        "status": "PASS" if inspect_status == 200 and checksum_ok and checksum else "FAIL",
        "checksum": checksum,
        "snapshot_id": snapshot_id,
        "checksum_ok": checksum_ok,
        "detail": None if checksum_ok else "snapshot inspect did not confirm the stored checksum",
    }


def _snapshot_skipped() -> dict[str, Any]:
    return {
        "status": _NOT_CHECKED,
        "checksum": None,
        "snapshot_id": None,
        "detail": "Snapshot checksum is required for CI. Staging does not take one unless CI rules apply.",
    }


def _db_checks(session: Session, *, player_count: int | None, roster: str) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    problems.extend(_negative_resources(session))
    problems.extend(_ledger(session, player_count=player_count))
    problems.extend(_events(session))
    problems.extend(_armies(session, player_count=player_count, roster=roster))
    legacy_commands = int(
        session.scalar(select(func.count()).select_from(PlayerCommand).where(PlayerCommand.trace_id.is_(None)))
        or 0
    )
    counts = {
        "commands": int(session.scalar(select(func.count()).select_from(PlayerCommand)) or 0),
        "events": int(session.scalar(select(func.count()).select_from(Event)) or 0),
        "events_by_status": _status_counts(session),
        "battles": int(session.scalar(select(func.count()).select_from(BattleReport)) or 0),
        "armies": int(session.scalar(select(func.count()).select_from(Army)) or 0),
    }
    return {"problems": problems, "legacy_commands": legacy_commands, "counts": counts}


def _status_counts(session: Session) -> dict[str, int]:
    found = {status: 0 for status in (
        EventStatus.PENDING,
        EventStatus.PROCESSING,
        EventStatus.COMPLETED,
        EventStatus.FAILED,
        EventStatus.CANCELLED,
    )}
    for status, count in session.execute(select(Event.status, func.count()).group_by(Event.status)):
        found[str(status)] = int(count)
    return found


def _negative_resources(session: Session) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    cities = session.scalars(select(City).order_by(City.id)).all()
    for city in cities:
        for name in RESOURCES:
            amount = int(getattr(city, name))
            if amount < 0:
                problems.append(_fail("negative_resources", f"city {city.id} {name} is {amount}"))
    rows = session.scalars(select(Transaction).where(Transaction.balance_after < 0).order_by(Transaction.id)).all()
    for row in rows:
        problems.append(
            _fail(
                "negative_resources",
                f"transaction {row.id} balance_after is {row.balance_after}",
                trace_id=row.trace_id,
            )
        )
    return problems


def _ledger(session: Session, *, player_count: int | None) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    rows = session.scalars(select(Transaction).order_by(Transaction.id)).all()
    by_city: dict[tuple[int, str], list[Transaction]] = defaultdict(list)
    sums = {name: 0 for name in RESOURCES}
    for row in rows:
        if row.resource not in RESOURCES:
            continue
        if row.reason not in _ALLOWED_REASONS:
            problems.append(
                _fail(
                    "ledger_conservation",
                    f"transaction {row.id} reason {row.reason!r} is outside the allowed ledger reasons",
                    trace_id=row.trace_id,
                )
            )
        if row.reason == Reason.UPKEEP and row.resource != "food":
            problems.append(
                _fail("ledger_conservation", f"upkeep on {row.resource}", trace_id=row.trace_id)
            )
        if row.reason == Reason.PRODUCTION and row.delta < 0:
            problems.append(_fail("ledger_conservation", f"production delta {row.delta}", trace_id=row.trace_id))
        if row.reason == Reason.UPKEEP and row.delta > 0:
            problems.append(_fail("ledger_conservation", f"upkeep delta {row.delta}", trace_id=row.trace_id))
        if row.reason in {Reason.TRAIN, Reason.FOUND_CITY, Reason.TRANSFER_OUT} and row.delta > 0:
            problems.append(
                _fail("ledger_conservation", f"{row.reason} delta {row.delta} is positive", trace_id=row.trace_id)
            )
        if row.reason == Reason.TRANSFER_IN and row.delta < 0:
            problems.append(
                _fail("ledger_conservation", f"transfer_in delta {row.delta} is negative", trace_id=row.trace_id)
            )
        sums[row.resource] += int(row.delta)
        if row.city_id is not None:
            by_city[(int(row.city_id), row.resource)].append(row)

    cities = session.scalars(select(City).order_by(City.id)).all()
    stocks = {name: 0 for name in RESOURCES}
    for city in cities:
        for name in RESOURCES:
            stocks[name] += int(getattr(city, name))
            chain = by_city.get((city.id, name), [])
            if not chain:
                continue
            running = int(chain[0].balance_after) - int(chain[0].delta)
            for row in chain:
                if running + int(row.delta) != int(row.balance_after):
                    problems.append(
                        _fail(
                            "ledger_conservation",
                            f"city {city.id} {name} transaction {row.id} does not continue the balance",
                            trace_id=row.trace_id,
                        )
                    )
                    break
                running = int(row.balance_after)
            else:
                if running != int(getattr(city, name)):
                    problems.append(
                        _fail(
                            "ledger_conservation",
                            f"city {city.id} {name} stock {getattr(city, name)} != ledger balance {running}",
                        )
                    )

    if player_count is not None:
        opening = opening_resources(player_count)
        for name in RESOURCES:
            expected = opening[name] + sums[name]
            if stocks[name] != expected:
                problems.append(
                    _fail(
                        "ledger_conservation",
                        f"{name}: stock {stocks[name]} != opening {opening[name]} + ledger {sums[name]}",
                    )
                )

    problems.extend(_loot_conservation(session))
    return problems


def _loot_conservation(session: Session) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    reports = session.scalars(select(BattleReport).order_by(BattleReport.id)).all()
    lost = _signed_loot(session, Reason.LOOT_LOST)
    gained = _signed_loot(session, Reason.LOOT_GAINED)
    cargo = {name: 0 for name in RESOURCES}
    movements = session.scalars(
        select(Movement).where(
            Movement.mission == "return",
            Movement.status == MovementStatus.IN_PROGRESS,
        )
    ).all()
    for movement in movements:
        cargo["wood"] += int(movement.loot_wood)
        cargo["food"] += int(movement.loot_food)
        cargo["iron"] += int(movement.loot_iron)
        cargo["gold"] += int(movement.loot_gold)

    recorded = {name: 0 for name in RESOURCES}
    for report in reports:
        loot = {name: int((report.loot or {}).get(name, 0) or 0) for name in RESOURCES}
        taken = lost["by_event"].get(report.event_id, {name: 0 for name in RESOURCES})
        for name in RESOURCES:
            if taken[name] != loot[name]:
                problems.append(
                    _fail(
                        "ledger_conservation",
                        f"battle {report.id} {name} removed {taken[name]} != report loot {loot[name]}",
                        trace_id=report.trace_id,
                    )
                )
        return_event = session.scalar(select(Event).where(Event.idempotency_key == f"return:{report.event_id}"))
        if return_event is None:
            for name in RESOURCES:
                recorded[name] += loot[name]
            continue
        if return_event.status in {EventStatus.PENDING, EventStatus.PROCESSING}:
            continue
        deposited = gained["by_event"].get(return_event.id, {name: 0 for name in RESOURCES})
        movement = session.get(Movement, return_event.movement_id) if return_event.movement_id else None
        destroyed = _army_destroyed(session, report.attacker_army_id)
        for name in RESOURCES:
            if destroyed and deposited[name] == 0:
                recorded[name] += loot[name]
            elif deposited[name] != loot[name]:
                problems.append(
                    _fail(
                        "ledger_conservation",
                        f"battle {report.id} {name} deposited {deposited[name]} != report loot {loot[name]}",
                        trace_id=report.trace_id,
                    )
                )
            if movement is not None and movement.status == MovementStatus.COMPLETED:
                carried = int(getattr(movement, f"loot_{name}"))
                if carried != deposited[name] and not (destroyed and deposited[name] == 0):
                    problems.append(
                        _fail(
                            "ledger_conservation",
                            f"return movement {movement.id} {name} cargo {carried} != deposited {deposited[name]}",
                            trace_id=movement.trace_id,
                        )
                    )

    for name in RESOURCES:
        taken = lost["totals"][name]
        accounted = gained["totals"][name] + cargo[name] + recorded[name]
        if taken != accounted:
            problems.append(
                _fail(
                    "ledger_conservation",
                    f"{name}: loot taken {taken} != deposited {gained['totals'][name]} "
                    f"+ in transit {cargo[name]} + recorded losses {recorded[name]}",
                )
            )
    return problems


def _signed_loot(session: Session, reason: str) -> dict[str, Any]:
    totals = {name: 0 for name in RESOURCES}
    by_event: dict[int, dict[str, int]] = defaultdict(lambda: {name: 0 for name in RESOURCES})
    rows = session.scalars(select(Transaction).where(Transaction.reason == reason)).all()
    for row in rows:
        if row.resource not in totals:
            continue
        amount = abs(int(row.delta))
        totals[row.resource] += amount
        if row.source_event_id is not None:
            by_event[int(row.source_event_id)][row.resource] += amount
    return {"totals": totals, "by_event": by_event}


def _army_destroyed(session: Session, army_id: int) -> bool:
    army = session.get(Army, army_id)
    if army is None:
        return True
    if army.status == ArmyStatus.DESTROYED:
        return True
    return not army.units


def _events(session: Session) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    duplicate = session.execute(
        select(
            Transaction.source_event_id,
            Transaction.city_id,
            Transaction.resource,
            Transaction.reason,
            func.count(),
        )
        .where(Transaction.source_event_id.is_not(None))
        .group_by(Transaction.source_event_id, Transaction.city_id, Transaction.resource, Transaction.reason)
        .having(func.count() > 1)
    ).all()
    for event_id, city_id, resource, reason, count in duplicate:
        trace_id = session.scalar(select(Event.trace_id).where(Event.id == event_id))
        problems.append(
            _fail(
                "duplicate_processing",
                f"event {event_id} city {city_id} has {count} {reason} rows for {resource}",
                trace_id=trace_id,
            )
        )
    live = session.execute(
        select(Event.movement_id, Event.type, func.count())
        .where(Event.status.in_((EventStatus.PENDING, EventStatus.PROCESSING)), Event.movement_id.is_not(None))
        .group_by(Event.movement_id, Event.type)
        .having(func.count() > 1)
    ).all()
    for movement_id, event_type, count in live:
        problems.append(
            _fail(
                "duplicate_processing",
                f"movement {movement_id} has {count} live {event_type} events",
            )
        )
    report_dupes = session.execute(
        select(BattleReport.event_id, func.count())
        .group_by(BattleReport.event_id)
        .having(func.count() > 1)
    ).all()
    for event_id, count in report_dupes:
        problems.append(_fail("duplicate_processing", f"event {event_id} has {count} battle reports"))
    failed = int(
        session.scalar(select(func.count()).select_from(Event).where(Event.status == EventStatus.FAILED)) or 0
    )
    processing = int(
        session.scalar(select(func.count()).select_from(Event).where(Event.status == EventStatus.PROCESSING)) or 0
    )
    if failed:
        problems.append(_fail("duplicate_processing", f"{failed} events are failed"))
    if processing:
        problems.append(_fail("duplicate_processing", f"{processing} events are still processing"))
    return problems


def _completed_training(session: Session) -> tuple[dict[str, int], int]:
    """Units added by completed training, and how many of those spawned a new army."""

    trained: dict[str, int] = defaultdict(int)
    spawned = 0
    events = session.scalars(
        select(Event).where(Event.type == EventType.TRAIN_COMPLETE, Event.status == EventStatus.COMPLETED)
    ).all()
    for event in events:
        payload = event.payload or {}
        unit_type = str(payload.get("unit_type") or "")
        count = int(payload.get("count") or 0)
        if unit_type and count > 0:
            trained[unit_type] += count
        if payload.get("spawned"):
            spawned += 1
    return trained, spawned


def _armies(session: Session, *, player_count: int | None, roster: str) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    armies = session.scalars(select(Army).order_by(Army.id)).all()
    trained, spawned = _completed_training(session)
    if player_count is not None and len(armies) != expected_army_count(player_count) + spawned:
        problems.append(
            _fail(
                "armies",
                f"army count {len(armies)} != seeded {expected_army_count(player_count)} + spawned trains {spawned}",
            )
        )
    active = session.execute(
        select(Movement.army_id, func.count())
        .where(Movement.status == MovementStatus.IN_PROGRESS)
        .group_by(Movement.army_id)
        .having(func.count() > 1)
    ).all()
    for army_id, count in active:
        problems.append(_fail("armies", f"army {army_id} has {count} in-progress movements"))
    totals: dict[str, int] = defaultdict(int)
    for army in armies:
        if army.status == ArmyStatus.GARRISONED and army.location_city_id is None:
            problems.append(_fail("armies", f"army {army.id} is garrisoned without a city"))
        if army.status in {ArmyStatus.MARCHING, ArmyStatus.RETURNING, ArmyStatus.DESTROYED}:
            if army.location_city_id is not None and army.status != ArmyStatus.GARRISONED:
                problems.append(_fail("armies", f"army {army.id} status {army.status} still has a location"))
        for stack in army.units or []:
            count = int(stack.get("count", 0))
            if count < 0:
                problems.append(_fail("armies", f"army {army.id} has a negative {stack.get('type')} count"))
            elif count:
                totals[str(stack.get("type"))] += count
    if player_count is not None:
        opening = opening_units(player_count, roster=roster)
        expected = dict(opening)
        for unit_type, count in trained.items():
            expected[unit_type] = expected.get(unit_type, 0) + count
        casualties = _casualty_totals(session)
        for unit_type, seeded in expected.items():
            left = totals.get(unit_type, 0) + casualties.get(unit_type, 0)
            if left != seeded:
                problems.append(
                    _fail(
                        "armies",
                        f"{unit_type}: alive {totals.get(unit_type, 0)} + casualties {casualties.get(unit_type, 0)} "
                        f"!= seeded {opening.get(unit_type, 0)} + trained {trained.get(unit_type, 0)}",
                    )
                )
        for unit_type in totals:
            if unit_type not in expected:
                problems.append(_fail("armies", f"unexpected unit type {unit_type}"))
    return problems


def _casualty_totals(session: Session) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    reports = session.scalars(select(BattleReport)).all()
    for report in reports:
        for side in (report.attacker_casualties or [], report.defender_casualties or []):
            for stack in side:
                totals[str(stack.get("type"))] += int(stack.get("count") or 0)
    return totals


def _score_traces(traces: dict[str, Any], failed: list[dict[str, Any]]) -> dict[str, Any]:
    summary = traces["summary"]
    if summary.get("error"):
        item = _fail("trace_verdicts", str(summary["error"]))
        failed.append(item)
        return {**item, "required": True}
    fail_count = int(summary.get("FAIL") or 0)
    incomplete_bad = int(summary.get("incomplete_without_pending") or 0)
    if fail_count or incomplete_bad:
        for row in traces["failures"]:
            item = _fail("trace_verdicts", str(row.get("detail")), trace_id=row.get("trace_id"))
            failed.append(item)
        return _fail(
            "trace_verdicts",
            f"{fail_count} FAIL, {incomplete_bad} INCOMPLETE without pending work",
        ) | {"required": True}
    return _pass(
        "trace_verdicts",
        f"PASS {summary.get('PASS', 0)}, INCOMPLETE {summary.get('INCOMPLETE', 0)} "
        f"(open work only), FAIL {fail_count}",
    )


def _score_legacy(db: dict[str, Any], thresholds: dict[str, Any], failed: list[dict[str, Any]]) -> dict[str, Any]:
    count = int(db["legacy_commands"])
    limit = int(thresholds["legacy_command_rows_max"])
    if count > limit:
        item = _fail("legacy_commands", f"{count} player commands are LEGACY / NOT TRACED")
        failed.append(item)
        return {**item, "required": True}
    return _pass(
        "legacy_commands",
        f"{count} LEGACY command rows, within the limit of {limit}. A LEGACY row is not a trace PASS.",
    )


def _score_production(traces: dict[str, Any], failed: list[dict[str, Any]]) -> dict[str, Any]:
    """PASS only when at least one trace's production/upkeep check passed and none failed.

    Traces with no accrual rows stay NOT CHECKED. They are listed in the detail
    and are not called PASS.
    """

    rows = list(traces.get("production") or [])
    failed_rows = [row for row in rows if row.get("status") == _FAIL]
    passed = [row for row in rows if row.get("status") == _PASS]
    unchecked = [row for row in rows if row.get("status") == _NOT_CHECKED]
    if failed_rows:
        item = _fail(
            "production_upkeep",
            f"{len(failed_rows)} trace(s) failed production/upkeep; first: {failed_rows[0].get('detail')}",
            trace_id=failed_rows[0].get("trace_id"),
        )
        failed.append(item)
        return {**item, "required": True}
    if passed:
        return _pass(
            "production_upkeep",
            f"{len(passed)} trace(s) PASS. {len(unchecked)} trace(s) had no verifiable accrual "
            "and stay NOT CHECKED. NOT CHECKED is not PASS.",
        )
    return {
        "invariant": "production_upkeep",
        "status": _NOT_CHECKED,
        "required": False,
        "trace_id": None,
        "detail": (
            "No command trace had production or upkeep rows that could be recomputed. "
            f"{len(unchecked)} trace(s) are NOT CHECKED. This is not PASS."
        ),
    }


def _score_db(db: dict[str, Any], failed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for problem in db["problems"]:
        grouped[str(problem["invariant"])].append(problem)
    scored = []
    for name in ("negative_resources", "ledger_conservation", "duplicate_processing", "armies"):
        problems = grouped.get(name, [])
        if problems:
            failed.extend(problems)
            scored.append(
                {
                    "invariant": name,
                    "status": _FAIL,
                    "required": True,
                    "trace_id": problems[0].get("trace_id"),
                    "detail": f"{len(problems)} problem(s); first: {problems[0]['detail']}",
                }
            )
        else:
            scored.append(_pass(name, "read-only queries found no break"))
    return scored


def _score_audit(audit: dict[str, Any], failed: list[dict[str, Any]]) -> dict[str, Any]:
    if audit.get("status") != "PASS":
        item = _fail("audit_chain", f"chain status {audit.get('status')}: {audit.get('reasons')}")
        failed.append(item)
        return {**item, "required": True}
    return _pass("audit_chain", f"server chain PASS, {audit.get('checked_rows')} rows")


def _score_monitoring(monitoring: dict[str, Any], failed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CRITICAL fails the run. UNKNOWN and NOT INSTRUMENTED stay labeled as themselves."""

    scored: list[dict[str, Any]] = []
    critical = monitoring["summary"].get("critical") or []
    if critical:
        names = ", ".join(str(item.get("name")) for item in critical)
        item = _fail("monitoring_critical", f"CRITICAL: {names}")
        failed.append(item)
        scored.append({**item, "required": True})
    else:
        scored.append(_pass("monitoring_critical", "no monitoring check is CRITICAL"))
    for item in monitoring["summary"].get("unknown") or []:
        scored.append(
            {
                "invariant": f"monitoring.{item.get('name')}",
                "status": "UNKNOWN",
                "required": False,
                "trace_id": None,
                "detail": item.get("reason") or "The check could not be measured. This is not a pass.",
            }
        )
    for item in monitoring["summary"].get("not_instrumented") or []:
        scored.append(
            {
                "invariant": f"monitoring.{item.get('name')}",
                "status": "NOT INSTRUMENTED",
                "required": False,
                "trace_id": None,
                "detail": item.get("reason") or "This server does not measure that check. This is not a pass.",
            }
        )
    return scored


def _score_lag(
    end_lag: float | None,
    max_lag: float,
    thresholds: dict[str, Any],
    failed: list[dict[str, Any]],
) -> dict[str, Any]:
    if end_lag is None:
        item = _fail("event_lag", "event_queue.lag was not measured. UNKNOWN is not PASS.")
        failed.append(item)
        return {**item, "required": True}
    if end_lag > float(thresholds["end_event_lag_seconds_max"]):
        item = _fail(
            "event_lag",
            f"end lag {end_lag}s exceeds {thresholds['end_event_lag_seconds_max']}s",
        )
        failed.append(item)
        return {**item, "required": True}
    if max_lag > float(thresholds["max_event_lag_seconds_max"]):
        item = _fail(
            "event_lag",
            f"max lag {max_lag}s exceeds {thresholds['max_event_lag_seconds_max']}s",
        )
        failed.append(item)
        return {**item, "required": True}
    return _pass("event_lag", f"end {end_lag}s, max observed {max_lag}s")


def _score_snapshot(
    snapshot: dict[str, Any],
    live_checksum: str,
    mode: str,
    failed: list[dict[str, Any]],
) -> dict[str, Any]:
    if snapshot.get("status") == _NOT_CHECKED:
        return {
            "invariant": "snapshot_checksum",
            "status": _NOT_CHECKED,
            "required": mode in _SEEDED_MODES,
            "trace_id": None,
            "detail": snapshot.get("detail"),
        }
    checksum = snapshot.get("checksum")
    if snapshot.get("status") != "PASS" or not checksum:
        item = _fail("snapshot_checksum", str(snapshot.get("detail") or "snapshot was not confirmed"))
        failed.append(item)
        return {**item, "required": True}
    if checksum != live_checksum:
        item = _fail(
            "snapshot_checksum",
            "stored snapshot checksum does not match a fresh world_checksum() read",
        )
        failed.append(item)
        return {**item, "required": True}
    return _pass("snapshot_checksum", str(checksum))
