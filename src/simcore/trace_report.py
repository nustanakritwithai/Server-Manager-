"""Read-only trace timeline and server-side integrity verdict.

The browser displays this document. It does not decide PASS, FAIL, or
INCOMPLETE. Production and upkeep are scored when this trace has accrual
ledger rows whose idempotency keys record the rate, the elapsed window, and
(for upkeep) the garrison composition. A trace with no such rows stays
NOT CHECKED. NOT CHECKED is never reported as PASS.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from simcore.constants import (
    RESOURCES,
    ArmyStatus,
    EventStatus,
    EventType,
    Mission,
    MovementStatus,
    Reason,
)
from simcore.errors import GameError
from simcore.game.catalog import FOUND_CITY_COST, UNIT_CATALOG, UNIT_TRAINING
from simcore.game.economy import produced_amount
from simcore.models import Army, BattleReport, City, Event, Movement, Player, PlayerCommand, Transaction

PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"
NOT_CHECKED = "NOT CHECKED"
LEGACY = "LEGACY"
NOT_TRACED = "NOT TRACED"

_OPEN_EVENT = frozenset({EventStatus.PENDING, EventStatus.PROCESSING})
_MARCH_COMMANDS = frozenset({"attack", "move", "recall", "garrison"})
_LOOT_REASONS = frozenset({Reason.LOOT_LOST, Reason.LOOT_GAINED})
_ACCRUAL_REASONS = frozenset({Reason.PRODUCTION, Reason.UPKEEP})


def _not_found(kind: str) -> GameError:
    return GameError(f"{kind} not found", status_code=404, code="not_found")


def legacy_entry(kind: str, row_id: int) -> dict[str, object]:
    return {"kind": kind, "id": row_id, "trace": LEGACY, "detail": NOT_TRACED}


def _sort_stamp(value: datetime | None, fallback: datetime) -> datetime:
    current = value or fallback
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _army_destroyed(army: Army | None) -> bool:
    if army is None:
        return False
    return army.status == ArmyStatus.DESTROYED or not army.units


def _resource_sums(transactions: list[Transaction]) -> tuple[dict[str, int], dict[str, int]]:
    outgoing = {name: 0 for name in RESOURCES}
    incoming = {name: 0 for name in RESOURCES}
    for row in transactions:
        if row.resource not in outgoing:
            continue
        if row.delta < 0:
            outgoing[row.resource] += -int(row.delta)
        elif row.delta > 0:
            incoming[row.resource] += int(row.delta)
    return outgoing, incoming


def _report_loot(reports: list[BattleReport]) -> dict[str, int] | None:
    if not reports:
        return None
    totals = {name: 0 for name in RESOURCES}
    for report in reports:
        loot = report.loot or {}
        for name in RESOURCES:
            totals[name] += int(loot.get(name, 0) or 0)
    return totals


def _carry(movements: list[Movement]) -> dict[str, int]:
    totals = {name: 0 for name in RESOURCES}
    for movement in movements:
        totals["wood"] += int(movement.loot_wood)
        totals["food"] += int(movement.loot_food)
        totals["iron"] += int(movement.loot_iron)
        totals["gold"] += int(movement.loot_gold)
    return totals


def _check(name: str, fail: list[str], incomplete: list[str], *, detail: str | None = None) -> dict[str, object]:
    if fail:
        status = FAIL
        reasons = list(fail)
    elif incomplete:
        status = INCOMPLETE
        reasons = list(incomplete)
    else:
        status = PASS
        reasons = []
    body: dict[str, object] = {"name": name, "status": status, "reasons": reasons}
    if detail is not None:
        body["detail"] = detail
    return body


def _trace_army(session: Session, command: PlayerCommand | None, movements: list[Movement]) -> Army | None:
    army_id = command.army_id if command is not None else None
    if army_id is None and movements:
        army_id = movements[0].army_id
    if army_id is None:
        return None
    return session.get(Army, army_id)


def _ledger_check(
    session: Session,
    *,
    command: PlayerCommand | None,
    movements: list[Movement],
    events: list[Event],
    reports: list[BattleReport],
    transactions: list[Transaction],
) -> dict[str, object]:
    """Resources out == resources in + recorded losses, per resource.

    Recorded losses are the battle-report loot that stayed out of the world
    because the army was destroyed before it could deposit. They are read from
    the report. A gap in the ledger is not rewritten into a loss.
    """

    all_transactions = transactions
    transactions = [row for row in transactions if row.reason in _LOOT_REASONS]
    outgoing, incoming = _resource_sums(transactions)
    observed = _report_loot(reports)
    open_move = any(row.status == MovementStatus.IN_PROGRESS for row in movements)
    open_event = any(row.status in _OPEN_EVENT for row in events)
    closed = not open_move and not open_event
    fail: list[str] = []
    incomplete: list[str] = []
    recorded: dict[str, int] | None = None

    if not closed:
        if observed is not None:
            for name in RESOURCES:
                if outgoing[name] != observed[name]:
                    fail.append(f"{name}: ledger out {outgoing[name]} != battle report loot {observed[name]}")
                if incoming[name] != 0:
                    fail.append(f"{name}: ledger in {incoming[name]} before the return movement completed")
        if not fail:
            if open_move:
                incomplete.append("army is en route; ledger conservation is not closed")
            else:
                incomplete.append("an event is still open; ledger conservation is not closed")
    else:
        report_loot = observed if observed is not None else {name: 0 for name in RESOURCES}
        returns_done = [
            row for row in movements if row.mission == Mission.RETURN and row.status == MovementStatus.COMPLETED
        ]
        army = _trace_army(session, command, movements)
        destroyed = _army_destroyed(army) and not returns_done and bool(reports)
        if destroyed:
            recorded = dict(report_loot)
        else:
            recorded = {name: 0 for name in RESOURCES}
        carried = _carry(returns_done)
        for name in RESOURCES:
            if outgoing[name] != incoming[name] + recorded[name]:
                fail.append(
                    f"{name}: resources out {outgoing[name]} != resources in {incoming[name]} "
                    f"+ recorded losses {recorded[name]}"
                )
            if outgoing[name] != report_loot[name]:
                fail.append(f"{name}: ledger out {outgoing[name]} != battle report loot {report_loot[name]}")
            if recorded[name] == 0 and incoming[name] != report_loot[name]:
                fail.append(f"{name}: ledger in {incoming[name]} != battle report loot {report_loot[name]}")
            if recorded[name] and incoming[name] != 0:
                fail.append(f"{name}: recorded losses {recorded[name]} but ledger in is {incoming[name]}")
            if returns_done and carried[name] != incoming[name]:
                fail.append(f"{name}: return movement loot {carried[name]} != ledger in {incoming[name]}")

    spend_fail, spend_incomplete = _spend_transfer_check(command, events, all_transactions)
    fail.extend(spend_fail)
    incomplete.extend(spend_incomplete)

    resources: dict[str, dict[str, int | None]] = {}
    for name in RESOURCES:
        resources[name] = {
            "out": outgoing[name],
            "in": incoming[name],
            "recorded_losses": None if recorded is None else recorded[name],
            "report_loot": None if observed is None else observed[name],
        }
    body = _check("ledger_conservation", fail, incomplete)
    body["resources"] = resources
    return body


def _missing_links(
    session: Session,
    *,
    command: PlayerCommand | None,
    movements: list[Movement],
    events: list[Event],
    reports: list[BattleReport],
    transactions: list[Transaction],
) -> dict[str, object]:
    fail: list[str] = []
    incomplete: list[str] = []
    event_ids = {row.id for row in events}
    movement_ids = {row.id for row in movements}
    if command is None:
        fail.append("no command row for this trace")
    for event in events:
        if event.movement_id is not None and event.movement_id not in movement_ids:
            fail.append(f"event {event.id} movement_id {event.movement_id} is not in this trace")
        if event.status == EventStatus.COMPLETED and event.processed_at is None:
            fail.append(f"event {event.id} is completed without processed_at")
    for movement in movements:
        if movement.cause_event_id is not None and movement.cause_event_id not in event_ids:
            fail.append(
                f"movement {movement.id} cause_event_id {movement.cause_event_id} is not in this trace"
            )
    for report in reports:
        if report.event_id not in event_ids:
            fail.append(f"battle report {report.id} has no source event in this trace")
        source = session.get(Event, report.event_id)
        if source is None:
            fail.append(f"battle report {report.id} source event {report.event_id} does not exist")
        if report.movement_id not in movement_ids:
            fail.append(f"battle report {report.id} movement_id {report.movement_id} is not in this trace")
    for row in transactions:
        if row.source_event_id is None:
            if row.reason in _ACCRUAL_REASONS or row.reason == Reason.START:
                continue
            fail.append(f"transaction {row.id} has no source event (orphan)")
        elif row.source_event_id not in event_ids:
            fail.append(
                f"transaction {row.id} source event {row.source_event_id} is not in this trace (orphan)"
            )
        else:
            source = session.get(Event, row.source_event_id)
            if source is None:
                fail.append(f"transaction {row.id} source event {row.source_event_id} does not exist (orphan)")
    if command is not None and command.command_type in {"build", "research"}:
        for event in events:
            effects = [row for row in transactions if row.source_event_id == event.id]
            if event.status in _OPEN_EVENT:
                incomplete.append(f"event {event.id} is {event.status}; its effect row is not written yet")
            elif event.status == EventStatus.COMPLETED and len(effects) != 1:
                fail.append(f"event {event.id} completed with {len(effects)} effect transactions")
            elif event.status == EventStatus.FAILED:
                fail.append(f"event {event.id} failed")
    if command is not None and command.command_type == "train_units":
        for event in events:
            if event.type != EventType.TRAIN_COMPLETE:
                continue
            effects = [
                row
                for row in transactions
                if row.source_event_id == event.id and row.reason == "train_units"
            ]
            if event.status in _OPEN_EVENT:
                incomplete.append(f"event {event.id} is {event.status}; trained units are not in the world yet")
            elif event.status == EventStatus.COMPLETED and len(effects) != 1:
                fail.append(f"event {event.id} completed with {len(effects)} train_units effect rows")
    return _check("missing_links", fail, incomplete)


def _duplicate_processing(
    events: list[Event],
    reports: list[BattleReport],
    transactions: list[Transaction],
) -> dict[str, object]:
    fail: list[str] = []
    grouped: dict[tuple[int | None, str, str], list[int]] = {}
    for row in transactions:
        grouped.setdefault((row.source_event_id, row.city_id, row.resource, row.reason), []).append(row.id)
    for (event_id, city_id, resource, reason), ids in grouped.items():
        if len(ids) > 1:
            fail.append(
                f"event {event_id} city {city_id} resource {resource} reason {reason} has {len(ids)} "
                f"ledger rows {ids}; processed more than once"
            )
    by_movement: dict[tuple[int | None, str], list[int]] = {}
    for event in events:
        if event.status == EventStatus.CANCELLED or event.movement_id is None:
            continue
        by_movement.setdefault((event.movement_id, event.type), []).append(event.id)
    for (movement_id, event_type), ids in by_movement.items():
        if len(ids) > 1:
            fail.append(
                f"movement {movement_id} has {len(ids)} {event_type} events {ids}; processed more than once"
            )
    by_event: dict[int, list[int]] = {}
    for report in reports:
        by_event.setdefault(report.event_id, []).append(report.id)
    for event_id, ids in by_event.items():
        if len(ids) > 1:
            fail.append(f"event {event_id} has {len(ids)} battle reports {ids}; processed more than once")
    return _check("duplicate_processing", fail, [])


def _army_resolution(
    session: Session,
    *,
    command: PlayerCommand | None,
    movements: list[Movement],
    events: list[Event],
    reports: list[BattleReport],
) -> dict[str, object]:
    if command is None or command.command_type not in _MARCH_COMMANDS:
        return {
            "name": "army_resolution",
            "status": NOT_CHECKED,
            "reasons": [],
            "detail": "This command does not march an army, so departure and return were not scored.",
        }
    fail: list[str] = []
    incomplete: list[str] = []
    for movement in movements:
        if movement.status == MovementStatus.IN_PROGRESS:
            incomplete.append(
                f"army {movement.army_id} movement {movement.id} is in_progress (en route)"
            )
    for event in events:
        if event.status in _OPEN_EVENT and event.type in {EventType.ARMY_ARRIVE, EventType.ARMY_RETURN}:
            incomplete.append(f"event {event.id} is {event.status}")

    if command.command_type == "attack":
        outbound = [row for row in movements if row.mission == Mission.ATTACK]
        if not outbound:
            fail.append("attack command has no movement")
        for movement in outbound:
            if movement.status != MovementStatus.COMPLETED:
                continue
            matched = [row for row in reports if row.movement_id == movement.id]
            if not matched:
                fail.append(f"completed attack movement {movement.id} has no battle report")
            army = session.get(Army, movement.army_id)
            returns = [row for row in movements if row.mission == Mission.RETURN]
            if any(row.status == MovementStatus.IN_PROGRESS for row in returns):
                continue
            if any(row.status == MovementStatus.COMPLETED for row in returns):
                continue
            if _army_destroyed(army):
                continue
            fail.append(
                f"army {movement.army_id} departed on movement {movement.id} "
                "and did not return, die, or arrive"
            )
    elif command.command_type in {"move", "garrison"}:
        if not movements:
            fail.append(f"{command.command_type} command has no movement")
        for movement in movements:
            if movement.status != MovementStatus.COMPLETED:
                continue
            army = session.get(Army, movement.army_id)
            arrived = (
                army is not None
                and army.status == ArmyStatus.GARRISONED
                and army.location_city_id == movement.destination_city_id
            )
            # A later command may march the army away. Arrival still happened if
            # that later leg starts at this destination.
            left_after_arrival = session.scalar(
                select(Movement.id).where(
                    Movement.army_id == movement.army_id,
                    Movement.id > movement.id,
                    Movement.origin_city_id == movement.destination_city_id,
                )
            )
            if not arrived and not _army_destroyed(army) and left_after_arrival is None:
                fail.append(f"army {movement.army_id} movement {movement.id} completed without arriving")
    elif command.command_type == "recall":
        if not movements:
            fail.append("recall command has no movement")
        for movement in movements:
            if movement.status == MovementStatus.COMPLETED:
                continue
            if movement.status == MovementStatus.CANCELLED:
                continue
            if movement.status != MovementStatus.IN_PROGRESS:
                fail.append(f"recall movement {movement.id} is {movement.status}")
    return _check("army_resolution", fail, incomplete)


def _steps(
    command: PlayerCommand | None,
    movements: list[Movement],
    events: list[Event],
    reports: list[BattleReport],
    transactions: list[Transaction],
) -> list[dict[str, object]]:
    steps: list[dict[str, Any]] = []
    if command is not None:
        steps.append(
            {
                "type": "command",
                "game_time": command.accepted_at,
                "order": 10,
                "tie": command.id,
                "ids": {
                    "command_id": command.id,
                    "trace_id": command.trace_id,
                    "player_id": command.player_id,
                    "army_id": command.army_id,
                },
                "fields": {
                    "command_type": command.command_type,
                    "target": command.target,
                    "accepted_at": command.accepted_at,
                },
            }
        )
    for movement in movements:
        steps.append(
            {
                "type": "movement",
                "game_time": movement.depart_at,
                "order": 70 if movement.mission == Mission.RETURN else 20,
                "tie": movement.id,
                "ids": {
                    "movement_id": movement.id,
                    "army_id": movement.army_id,
                    "trace_id": movement.trace_id,
                },
                "fields": {
                    "mission": movement.mission,
                    "status": movement.status,
                    "depart_at": movement.depart_at,
                    "arrive_at": movement.arrive_at,
                    "origin_city_id": movement.origin_city_id,
                    "destination_city_id": movement.destination_city_id,
                    "cause_event_id": movement.cause_event_id,
                    "loot_wood": movement.loot_wood,
                    "loot_food": movement.loot_food,
                    "loot_iron": movement.loot_iron,
                    "loot_gold": movement.loot_gold,
                    "resolved_at": movement.resolved_at,
                },
            }
        )
    for event in events:
        steps.append(
            {
                "type": "event",
                "game_time": event.processed_at or event.due_at,
                "order": 80 if event.type == EventType.ARMY_RETURN else 30,
                "tie": event.id,
                "ids": {
                    "event_id": event.id,
                    "movement_id": event.movement_id,
                    "trace_id": event.trace_id,
                },
                "fields": {
                    "type": event.type,
                    "status": event.status,
                    "due_at": event.due_at,
                    "processed_at": event.processed_at,
                    "attempts": event.attempts,
                    "idempotency_key": event.idempotency_key,
                },
            }
        )
    for report in reports:
        shared_ids = {
            "report_id": report.id,
            "event_id": report.event_id,
            "movement_id": report.movement_id,
            "trace_id": report.trace_id,
        }
        steps.append(
            {
                "type": "battle",
                "game_time": report.created_at,
                "order": 40,
                "tie": report.id,
                "ids": shared_ids,
                "fields": {
                    "seed": report.seed,
                    "winner": report.winner,
                    "attacker_army_id": report.attacker_army_id,
                    "defender_city_id": report.defender_city_id,
                    "attacker_casualties": report.attacker_casualties,
                    "defender_casualties": report.defender_casualties,
                    "loot": report.loot,
                },
            }
        )
        steps.append(
            {
                "type": "report",
                "game_time": report.created_at,
                "order": 60,
                "tie": report.id,
                "ids": dict(shared_ids),
                "fields": {
                    "winner": report.winner,
                    "loot": report.loot,
                    "seed": report.seed,
                    "attacker_remaining": report.attacker_remaining,
                    "defender_remaining": report.defender_remaining,
                },
            }
        )
    for row in transactions:
        if row.reason == "loot_gained":
            ledger_order = 90
        elif row.reason == "loot_lost":
            ledger_order = 55
        else:
            ledger_order = 55
        steps.append(
            {
                "type": "ledger",
                "game_time": row.created_at,
                "order": ledger_order,
                "tie": row.id,
                "ids": {
                    "transaction_id": row.id,
                    "source_event_id": row.source_event_id,
                    "city_id": row.city_id,
                    "player_id": row.player_id,
                    "trace_id": row.trace_id,
                },
                "fields": {
                    "resource": row.resource,
                    "delta": row.delta,
                    "balance_after": row.balance_after,
                    "reason": row.reason,
                    "idempotency_key": row.idempotency_key,
                },
            }
        )
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    steps.sort(
        key=lambda step: (
            _sort_stamp(step["game_time"], epoch),
            int(step["order"]),
            int(step["tie"]),
            str(step["type"]),
        )
    )
    ordered: list[dict[str, object]] = []
    for index, step in enumerate(steps, start=1):
        ordered.append(
            {
                "sequence": index,
                "type": step["type"],
                "game_time": step["game_time"],
                "ids": step["ids"],
                "fields": step["fields"],
            }
        )
    return ordered


def _load(session: Session, trace_id: str) -> tuple[
    PlayerCommand | None,
    list[Movement],
    list[Event],
    list[BattleReport],
    list[Transaction],
]:
    command = session.scalar(select(PlayerCommand).where(PlayerCommand.trace_id == trace_id))
    movements = list(
        session.scalars(select(Movement).where(Movement.trace_id == trace_id).order_by(Movement.id)).all()
    )
    events = list(session.scalars(select(Event).where(Event.trace_id == trace_id).order_by(Event.id)).all())
    reports = list(
        session.scalars(select(BattleReport).where(BattleReport.trace_id == trace_id).order_by(BattleReport.id)).all()
    )
    transactions = list(
        session.scalars(select(Transaction).where(Transaction.trace_id == trace_id).order_by(Transaction.id)).all()
    )
    return command, movements, events, reports, transactions


def _command_body(command: PlayerCommand | None) -> dict[str, object] | None:
    if command is None:
        return None
    return {
        "id": command.id,
        "trace_id": command.trace_id,
        "player_id": command.player_id,
        "command_type": command.command_type,
        "army_id": command.army_id,
        "target": command.target,
        "accepted_at": command.accepted_at,
    }


def _parse_stamp(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _accrual_elapsed(stamp: str, created_at: datetime) -> int | None:
    start = _parse_stamp(stamp)
    if start is None:
        return None
    end = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
    return int((end - start).total_seconds())


def _split_accrue(key: str) -> dict[str, object] | None:
    """Parse an accrue idempotency key. The stamp itself contains colons."""

    if not key.startswith("accrue:"):
        return None
    if ":production" in key:
        head, _, tail = key.partition(":production")
        parts = head.split(":", 3)
        if len(parts) != 4:
            return None
        _prefix, city, resource, stamp = parts
        rate = None
        if tail.startswith(":") and tail[1:].isdigit():
            rate = int(tail[1:])
        return {
            "kind": "production",
            "city_id": int(city),
            "resource": resource,
            "stamp": stamp,
            "rate": rate,
        }
    if ":upkeep" in key:
        head, _, tail = key.partition(":upkeep")
        parts = head.split(":", 3)
        if len(parts) != 4:
            return None
        _prefix, city, resource, stamp = parts
        hourly = None
        composition = None
        if tail.startswith(":"):
            hourly_text, separator, composition = tail[1:].partition(":")
            if hourly_text.isdigit():
                hourly = int(hourly_text)
            if not separator:
                composition = None
        return {
            "kind": "upkeep",
            "city_id": int(city),
            "resource": resource,
            "stamp": stamp,
            "hourly": hourly,
            "composition": composition,
        }
    return None


def _hourly_from_composition(composition: str) -> int | None:
    if composition == "":
        return 0
    total = 0
    for part in composition.split("+"):
        name, separator, count_text = part.partition("*")
        if not separator or name not in UNIT_CATALOG or not count_text.isdigit():
            return None
        total += UNIT_CATALOG[name].upkeep * int(count_text)
    return total


def _production_upkeep_check(session: Session, transactions: list[Transaction]) -> dict[str, object]:
    """Score accrual rows on this trace. No rows means the check was not run."""

    rows = [row for row in transactions if row.reason in _ACCRUAL_REASONS]
    if not rows:
        return {
            "name": "production_upkeep",
            "status": NOT_CHECKED,
            "reasons": [],
            "detail": (
                "This trace has no production or upkeep ledger rows, so those formulas were not scored. "
                "This is not PASS."
            ),
        }
    fail: list[str] = []
    unverified: list[str] = []
    for row in rows:
        parsed = _split_accrue(row.idempotency_key)
        if parsed is None:
            unverified.append(
                f"transaction {row.id} is {row.reason} but its idempotency key does not record an accrue window"
            )
            continue
        elapsed = _accrual_elapsed(str(parsed["stamp"]), row.created_at)
        if elapsed is None or elapsed < 0:
            unverified.append(f"transaction {row.id} accrue window could not be read")
            continue
        if parsed["kind"] == "production":
            rate = parsed["rate"]
            city = session.get(City, int(parsed["city_id"]))
            column = f"{row.resource}_rate"
            if not isinstance(rate, int):
                if city is None or not hasattr(city, column):
                    unverified.append(
                        f"transaction {row.id} production row has no stored rate and the city rate is unavailable"
                    )
                    continue
                rate = int(getattr(city, column))
            elif city is not None and hasattr(city, column) and int(getattr(city, column)) != rate:
                fail.append(
                    f"transaction {row.id} stored {row.resource} rate {rate} != city {column} {getattr(city, column)}"
                )
            expected = produced_amount(rate, elapsed)
            if row.delta != expected:
                fail.append(
                    f"transaction {row.id} production {row.resource} delta {row.delta} "
                    f"!= {rate} per hour * {elapsed}s // 3600 = {expected}"
                )
            if row.resource != parsed["resource"]:
                fail.append(f"transaction {row.id} resource {row.resource} != key resource {parsed['resource']}")
            continue
        if row.resource != "food":
            fail.append(f"transaction {row.id} upkeep is on {row.resource}, not food")
            continue
        hourly = parsed["hourly"]
        composition = parsed["composition"]
        if not isinstance(hourly, int) or not isinstance(composition, str):
            unverified.append(
                f"transaction {row.id} upkeep row does not record the garrison composition, "
                "so the hourly rate cannot be recomputed from the unit catalog"
            )
            continue
        recomputed = _hourly_from_composition(composition)
        if recomputed is None:
            unverified.append(f"transaction {row.id} garrison composition {composition!r} is not in the unit catalog")
            continue
        if recomputed != hourly:
            fail.append(
                f"transaction {row.id} upkeep hourly {hourly} != catalog upkeep {recomputed} of {composition}"
            )
        expected = produced_amount(hourly, elapsed)
        if row.delta != -expected:
            fail.append(
                f"transaction {row.id} upkeep delta {row.delta} "
                f"!= -({hourly} per hour * {elapsed}s // 3600 = {expected})"
            )
    if fail:
        return _check("production_upkeep", fail, [])
    if unverified:
        return {
            "name": "production_upkeep",
            "status": NOT_CHECKED,
            "reasons": [],
            "detail": " ".join(unverified) + " This is not PASS.",
        }
    return _check(
        "production_upkeep",
        [],
        [],
        detail=f"{len(rows)} production and upkeep rows match the stored rate, window, and catalog",
    )


def _spend_transfer_check(
    command: PlayerCommand | None,
    events: list[Event],
    transactions: list[Transaction],
) -> tuple[list[str], list[str]]:
    """Training and founding consume resources. Transfers move them between own cities."""

    if command is None:
        return [], []
    fail: list[str] = []
    incomplete: list[str] = []
    kind = command.command_type
    if kind == "train_units":
        unit_type = str((command.target or {}).get("unit_type") or "")
        count = int((command.target or {}).get("count") or 0)
        spec = UNIT_TRAINING.get(unit_type)
        if spec is None or count < 1:
            fail.append("train command target is not a catalog unit and a positive count")
            return fail, incomplete
        expected = {name: int(getattr(spec, name)) * count for name in RESOURCES}
        got = {name: 0 for name in RESOURCES}
        for row in transactions:
            if row.reason != Reason.TRAIN or row.resource not in got:
                continue
            if row.delta > 0:
                fail.append(f"train transaction {row.id} has a positive delta")
            got[row.resource] += -int(row.delta)
        for name in RESOURCES:
            if got[name] != expected[name]:
                fail.append(f"{name}: train spend {got[name]} != catalog cost {expected[name]}")
    elif kind == "start":
        expected = (command.target or {}).get("resources") or {}
        if not isinstance(expected, dict):
            fail.append("start command target resources are not an object")
            return fail, incomplete
        got = {name: 0 for name in RESOURCES}
        for row in transactions:
            if row.reason != Reason.START or row.resource not in got:
                continue
            if row.delta < 0:
                fail.append(f"start transaction {row.id} has a negative delta")
            got[row.resource] += int(row.delta)
        for name in RESOURCES:
            wanted = int(expected.get(name) or 0)
            if got[name] != wanted:
                fail.append(f"{name}: start grant {got[name]} != recorded amount {wanted}")
    elif kind == "found_city":
        got = {name: 0 for name in RESOURCES}
        for row in transactions:
            if row.reason != Reason.FOUND_CITY or row.resource not in got:
                continue
            if row.delta > 0:
                fail.append(f"found_city transaction {row.id} has a positive delta")
            got[row.resource] += -int(row.delta)
        for name in RESOURCES:
            if got[name] != int(FOUND_CITY_COST[name]):
                fail.append(f"{name}: found_city spend {got[name]} != catalog cost {FOUND_CITY_COST[name]}")
    elif kind == "transfer_resources":
        target = command.target or {}
        requested = {name: int(target.get(name) or 0) for name in RESOURCES}
        outgoing = {name: 0 for name in RESOURCES}
        incoming = {name: 0 for name in RESOURCES}
        for row in transactions:
            if row.resource not in outgoing:
                continue
            if row.reason == Reason.TRANSFER_OUT:
                if row.delta > 0:
                    fail.append(f"transfer_out transaction {row.id} has a positive delta")
                outgoing[row.resource] += -int(row.delta)
            elif row.reason == Reason.TRANSFER_IN:
                if row.delta < 0:
                    fail.append(f"transfer_in transaction {row.id} has a negative delta")
                incoming[row.resource] += int(row.delta)
        open_transfer = any(
            row.type == EventType.TRANSFER_ARRIVE and row.status in _OPEN_EVENT for row in events
        )
        for name in RESOURCES:
            if outgoing[name] != requested[name]:
                fail.append(f"{name}: transfer out {outgoing[name]} != requested {requested[name]}")
            if open_transfer:
                if incoming[name] != 0:
                    fail.append(f"{name}: transfer credited {incoming[name]} before arrival")
            elif incoming[name] != outgoing[name]:
                fail.append(f"{name}: transfer in {incoming[name]} != transfer out {outgoing[name]}")
        if open_transfer and not fail:
            incomplete.append("transfer has not arrived; conservation is not closed")
    return fail, incomplete


def _verdict(checks: list[dict[str, object]]) -> tuple[str, list[str]]:
    scored = [check for check in checks if check["status"] != NOT_CHECKED]
    reasons: list[str] = []
    for check in scored:
        if check["status"] in {FAIL, INCOMPLETE}:
            for reason in check["reasons"]:
                if isinstance(reason, str):
                    reasons.append(reason)
    if any(check["status"] == FAIL for check in scored):
        return FAIL, reasons
    if any(check["status"] == INCOMPLETE for check in scored):
        return INCOMPLETE, reasons
    if scored and all(check["status"] == PASS for check in scored):
        return PASS, []
    return FAIL, ["no integrity check was run"]


def build_trace(session: Session, trace_id: str) -> dict[str, object]:
    command, movements, events, reports, transactions = _load(session, trace_id)
    if command is None and not movements and not events and not reports and not transactions:
        raise _not_found("trace")
    checks = [
        _ledger_check(
            session,
            command=command,
            movements=movements,
            events=events,
            reports=reports,
            transactions=transactions,
        ),
        _missing_links(
            session,
            command=command,
            movements=movements,
            events=events,
            reports=reports,
            transactions=transactions,
        ),
        _duplicate_processing(events, reports, transactions),
        _army_resolution(session, command=command, movements=movements, events=events, reports=reports),
        _production_upkeep_check(session, transactions),
    ]
    verdict, reasons = _verdict(checks)
    not_checked = [
        {
            "name": check["name"],
            "status": NOT_CHECKED,
            "detail": check.get("detail")
            or "This check was not run. This is not PASS.",
        }
        for check in checks
        if check["status"] == NOT_CHECKED
    ]
    return {
        "trace_id": trace_id,
        "verdict": verdict,
        "reasons": reasons,
        "integrity": {
            "verdict": verdict,
            "reasons": reasons,
            "checks": checks,
            "not_checked": not_checked,
        },
        "command": _command_body(command),
        "steps": _steps(command, movements, events, reports, transactions),
    }


def _restrict(current: set[str] | None, found: set[str]) -> set[str]:
    if current is None:
        return found
    return current & found


def _append_legacy_movements(session: Session, army_ids: list[int], legacy: list[dict[str, object]]) -> None:
    if not army_ids:
        return
    rows = session.scalars(
        select(Movement).where(Movement.army_id.in_(army_ids), Movement.trace_id.is_(None)).order_by(Movement.id)
    ).all()
    seen = {(entry["kind"], entry["id"]) for entry in legacy}
    for row in rows:
        key = ("movement", row.id)
        if key in seen:
            continue
        seen.add(key)
        legacy.append(legacy_entry("movement", row.id))


def search_traces(
    session: Session,
    *,
    player_id: int | None,
    army_id: int | None,
    event_id: int | None,
    command_id: int | None,
    limit: int,
    offset: int,
) -> dict[str, object]:
    """Find traces from entry points. A null trace_id is LEGACY and is not joined onward."""

    legacy: list[dict[str, object]] = []
    trace_ids: set[str] | None = None
    filtered = any(value is not None for value in (player_id, army_id, event_id, command_id))

    if event_id is not None:
        event = session.get(Event, event_id)
        if event is None:
            raise _not_found("event")
        if event.trace_id is None:
            legacy.append(legacy_entry("event", event.id))
            trace_ids = _restrict(trace_ids, set())
        else:
            trace_ids = _restrict(trace_ids, {event.trace_id})

    if command_id is not None:
        command = session.get(PlayerCommand, command_id)
        if command is None:
            raise _not_found("command")
        if command.trace_id is None:
            legacy.append(legacy_entry("command", command.id))
            trace_ids = _restrict(trace_ids, set())
        else:
            trace_ids = _restrict(trace_ids, {command.trace_id})

    if army_id is not None:
        army = session.get(Army, army_id)
        if army is None:
            raise _not_found("army")
        from_commands = set(
            session.scalars(
                select(PlayerCommand.trace_id).where(
                    PlayerCommand.army_id == army_id,
                    PlayerCommand.trace_id.is_not(None),
                )
            ).all()
        )
        from_movements = set(
            session.scalars(
                select(Movement.trace_id).where(
                    Movement.army_id == army_id,
                    Movement.trace_id.is_not(None),
                )
            ).all()
        )
        trace_ids = _restrict(trace_ids, {str(value) for value in from_commands | from_movements})
        _append_legacy_movements(session, [army_id], legacy)

    if player_id is not None:
        player = session.get(Player, player_id)
        if player is None:
            raise _not_found("player")
        from_commands = set(
            session.scalars(
                select(PlayerCommand.trace_id).where(
                    PlayerCommand.player_id == player_id,
                    PlayerCommand.trace_id.is_not(None),
                )
            ).all()
        )
        trace_ids = _restrict(trace_ids, {str(value) for value in from_commands})
        army_ids = list(session.scalars(select(Army.id).where(Army.player_id == player_id)).all())
        _append_legacy_movements(session, army_ids, legacy)

    if trace_ids is None and not filtered:
        from_commands = set(
            session.scalars(select(PlayerCommand.trace_id).where(PlayerCommand.trace_id.is_not(None))).all()
        )
        from_rows = set(
            session.scalars(select(Movement.trace_id).where(Movement.trace_id.is_not(None))).all()
        )
        trace_ids = {str(value) for value in from_commands | from_rows}

    ids = sorted(trace_ids or [])
    commands = (
        list(session.scalars(select(PlayerCommand).where(PlayerCommand.trace_id.in_(ids))).all()) if ids else []
    )
    by_trace = {row.trace_id: row for row in commands if row.trace_id}
    floor = datetime.min.replace(tzinfo=timezone.utc)

    def sort_key(trace_id: str) -> tuple[datetime, int, str]:
        row = by_trace.get(trace_id)
        if row is None:
            return (floor, 0, trace_id)
        return (_sort_stamp(row.accepted_at, floor), row.id, trace_id)

    ids.sort(key=sort_key, reverse=True)
    page = ids[offset : offset + limit]
    traces: list[dict[str, object]] = []
    for trace_id in page:
        body = build_trace(session, trace_id)
        command = body["command"]
        command_body = command if isinstance(command, dict) else {}
        traces.append(
            {
                "trace_id": trace_id,
                "command_id": command_body.get("id"),
                "command_type": command_body.get("command_type"),
                "player_id": command_body.get("player_id"),
                "army_id": command_body.get("army_id"),
                "accepted_at": command_body.get("accepted_at"),
                "verdict": body["verdict"],
            }
        )
    legacy_total = len(legacy)
    return {
        "traces": traces,
        "legacy": legacy[:limit],
        "legacy_total": legacy_total,
        "limit": limit,
        "offset": offset,
        "total": len(ids),
    }
