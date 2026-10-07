"""Read-only shapes for the admin control center.

These functions select and count. They do not accrue resources, advance the
clock, claim events, or write snapshots. City balances are the stored columns.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from simcore.constants import EventStatus, MovementStatus
from simcore.errors import GameError
from simcore.models import (
    Army,
    BattleReport,
    City,
    Event,
    Movement,
    Player,
    Transaction,
    WorldSnapshot,
    WorldState,
)
from simcore.present import army_body, city_body, report_body
from simcore.snapshot import snapshot_metadata

# CPU and memory are still not measured. Heartbeat and disk are on
# GET /v1/admin/monitoring. The dashboard keeps this token for the gaps.
NOT_INSTRUMENTED = "NOT INSTRUMENTED"

_EVENT_STATUSES = (
    EventStatus.PENDING,
    EventStatus.PROCESSING,
    EventStatus.COMPLETED,
    EventStatus.FAILED,
    EventStatus.CANCELLED,
)


def _missing(kind: str) -> GameError:
    return GameError(f"{kind} not found", status_code=404, code="not_found")


def event_record(event: Event) -> dict[str, object]:
    return {
        "id": event.id,
        "type": event.type,
        "status": event.status,
        "due_at": event.due_at,
        "attempts": event.attempts,
        "processed_at": event.processed_at,
        "locked_by": event.locked_by,
        "locked_at": event.locked_at,
        "last_error": event.last_error,
        "idempotency_key": event.idempotency_key,
        "payload": event.payload,
        "movement_id": event.movement_id,
        "trace_id": event.trace_id,
        "created_at": event.created_at,
    }


def movement_record(movement: Movement) -> dict[str, object]:
    return {
        "id": movement.id,
        "army_id": movement.army_id,
        "origin_city_id": movement.origin_city_id,
        "destination_city_id": movement.destination_city_id,
        "origin_x": movement.origin_x,
        "origin_y": movement.origin_y,
        "destination_x": movement.destination_x,
        "destination_y": movement.destination_y,
        "depart_at": movement.depart_at,
        "arrive_at": movement.arrive_at,
        "mission": movement.mission,
        "status": movement.status,
        "relocate": movement.relocate,
        "loot_wood": movement.loot_wood,
        "loot_food": movement.loot_food,
        "loot_iron": movement.loot_iron,
        "loot_gold": movement.loot_gold,
        "cause_event_id": movement.cause_event_id,
        "trace_id": movement.trace_id,
        "created_at": movement.created_at,
        "resolved_at": movement.resolved_at,
    }


def transaction_record(row: Transaction) -> dict[str, object]:
    return {
        "id": row.id,
        "player_id": row.player_id,
        "city_id": row.city_id,
        "resource": row.resource,
        "delta": row.delta,
        "balance_after": row.balance_after,
        "reason": row.reason,
        "source_event_id": row.source_event_id,
        "trace_id": row.trace_id,
        "idempotency_key": row.idempotency_key,
        "created_at": row.created_at,
    }


def player_record(player: Player) -> dict[str, object]:
    return {
        "id": player.id,
        "name": player.name,
        "research": player.research,
        "created_at": player.created_at,
    }


def _count(session: Session, model: type) -> int:
    value = session.scalar(select(func.count()).select_from(model))
    return int(value or 0)


def dashboard(session: Session, *, now: datetime, offset_seconds: int) -> dict[str, object]:
    """Aggregate counts the admin dashboard can show without inventing probes."""

    state = session.get(WorldState, 1)
    event_counts = {status: 0 for status in _EVENT_STATUSES}
    for status, count in session.execute(select(Event.status, func.count()).group_by(Event.status)):
        event_counts[str(status)] = int(count)
    latest = session.scalars(select(WorldSnapshot).order_by(WorldSnapshot.id.desc()).limit(1)).first()
    active = session.scalar(
        select(func.count()).select_from(Movement).where(Movement.status == MovementStatus.IN_PROGRESS)
    )
    return {
        "server_time": now,
        "offset_seconds": offset_seconds,
        "world": {
            "world_version": None if state is None else int(state.world_version),
            "commands_open": None if state is None else state.commands_open,
            "worker_paused": None if state is None else state.worker_paused,
            "restore_active": None if state is None else state.restore_active,
        },
        "counts": {
            "players": _count(session, Player),
            "cities": _count(session, City),
            "armies": _count(session, Army),
            "active_movements": int(active or 0),
        },
        "events": event_counts,
        "latest_snapshot": None if latest is None else snapshot_metadata(latest),
        "uninstrumented": {
            "host_cpu": NOT_INSTRUMENTED,
            "host_memory": NOT_INSTRUMENTED,
        },
    }


def list_events(session: Session, *, status: str | None, limit: int) -> dict[str, object]:
    stmt = select(Event).order_by(Event.due_at, Event.id)
    if status is not None:
        stmt = stmt.where(Event.status == status)
    events = session.scalars(stmt.limit(limit)).all()
    return {"events": [event_record(event) for event in events]}


def event_detail(session: Session, event_id: int, now: datetime) -> dict[str, object]:
    event = session.get(Event, event_id)
    if event is None:
        raise _missing("event")
    movement = session.get(Movement, event.movement_id) if event.movement_id is not None else None
    army = session.get(Army, movement.army_id) if movement is not None else None
    battle = session.scalar(select(BattleReport).where(BattleReport.event_id == event.id))
    if battle is None and movement is not None:
        battle = session.scalar(select(BattleReport).where(BattleReport.movement_id == movement.id))
    transactions = session.scalars(
        select(Transaction).where(Transaction.source_event_id == event.id).order_by(Transaction.id)
    ).all()
    return {
        "event": event_record(event),
        "movement": None if movement is None else movement_record(movement),
        "army": None if army is None else army_body(session, army, now),
        "battle": None if battle is None else report_body(battle),
        "transactions": [transaction_record(row) for row in transactions],
    }


def list_players(session: Session) -> dict[str, object]:
    players = session.scalars(select(Player).order_by(Player.id)).all()
    cities_by: dict[int, list[int]] = {}
    for city_id, player_id in session.execute(select(City.id, City.player_id).order_by(City.id)):
        cities_by.setdefault(int(player_id), []).append(int(city_id))
    armies_by: dict[int, list[int]] = {}
    for army_id, player_id in session.execute(select(Army.id, Army.player_id).order_by(Army.id)):
        armies_by.setdefault(int(player_id), []).append(int(army_id))
    return {
        "players": [
            {
                **player_record(player),
                "city_ids": cities_by.get(player.id, []),
                "army_ids": armies_by.get(player.id, []),
            }
            for player in players
        ]
    }


def player_detail(session: Session, player_id: int, now: datetime) -> dict[str, object]:
    player = session.get(Player, player_id)
    if player is None:
        raise _missing("player")
    cities = session.scalars(select(City).where(City.player_id == player.id).order_by(City.id)).all()
    armies = session.scalars(select(Army).where(Army.player_id == player.id).order_by(Army.id)).all()
    return {
        "player": player_record(player),
        "cities": [city_body(session, city, include_resources=True) for city in cities],
        "armies": [army_body(session, army, now) for army in armies],
        "resource_balances": "stored_not_accrued",
    }


def list_cities(session: Session, *, player_id: int | None) -> dict[str, object]:
    stmt = select(City).order_by(City.id)
    if player_id is not None:
        stmt = stmt.where(City.player_id == player_id)
    cities = session.scalars(stmt).all()
    return {
        "cities": [city_body(session, city, include_resources=True) for city in cities],
        "resource_balances": "stored_not_accrued",
    }


def city_detail(session: Session, city_id: int) -> dict[str, object]:
    city = session.get(City, city_id)
    if city is None:
        raise _missing("city")
    owner = session.get(Player, city.player_id)
    home_ids = session.scalars(select(Army.id).where(Army.home_city_id == city.id).order_by(Army.id)).all()
    movement_ids = session.scalars(
        select(Movement.id)
        .where(or_(Movement.origin_city_id == city.id, Movement.destination_city_id == city.id))
        .order_by(Movement.id)
    ).all()
    return {
        "city": city_body(session, city, include_resources=True),
        "player": None if owner is None else {"id": owner.id, "name": owner.name},
        "home_army_ids": list(home_ids),
        "movement_ids": list(movement_ids),
        "resource_balances": "stored_not_accrued",
    }


def army_detail(session: Session, army_id: int, now: datetime) -> dict[str, object]:
    army = session.get(Army, army_id)
    if army is None:
        raise _missing("army")
    player = session.get(Player, army.player_id)
    movements = session.scalars(select(Movement).where(Movement.army_id == army.id).order_by(Movement.id)).all()
    return {
        "army": army_body(session, army, now),
        "player": None if player is None else {"id": player.id, "name": player.name},
        "movements": [movement_record(movement) for movement in movements],
    }


def list_movements(
    session: Session,
    *,
    status: str | None,
    army_id: int | None,
    limit: int,
) -> dict[str, object]:
    stmt = select(Movement).order_by(Movement.id.desc())
    if status is not None:
        stmt = stmt.where(Movement.status == status)
    if army_id is not None:
        stmt = stmt.where(Movement.army_id == army_id)
    rows = session.scalars(stmt.limit(limit)).all()
    return {"movements": [movement_record(row) for row in rows]}


def movement_detail(session: Session, movement_id: int, now: datetime) -> dict[str, object]:
    movement = session.get(Movement, movement_id)
    if movement is None:
        raise _missing("movement")
    army = session.get(Army, movement.army_id)
    events = session.scalars(select(Event).where(Event.movement_id == movement.id).order_by(Event.id)).all()
    battle = session.scalar(select(BattleReport).where(BattleReport.movement_id == movement.id))
    return {
        "movement": movement_record(movement),
        "army": None if army is None else army_body(session, army, now),
        "events": [event_record(event) for event in events],
        "battle": None if battle is None else report_body(battle),
    }


def list_reports(
    session: Session,
    *,
    limit: int,
    event_id: int | None,
    movement_id: int | None,
) -> dict[str, object]:
    stmt = select(BattleReport).order_by(BattleReport.id.desc())
    if event_id is not None:
        stmt = stmt.where(BattleReport.event_id == event_id)
    if movement_id is not None:
        stmt = stmt.where(BattleReport.movement_id == movement_id)
    rows = session.scalars(stmt.limit(limit)).all()
    return {"reports": [report_body(row) for row in rows]}


def report_detail(session: Session, report_id: int) -> dict[str, object]:
    report = session.get(BattleReport, report_id)
    if report is None:
        raise _missing("report")
    event = session.get(Event, report.event_id)
    movement = session.get(Movement, report.movement_id)
    transactions = session.scalars(
        select(Transaction).where(Transaction.source_event_id == report.event_id).order_by(Transaction.id)
    ).all()
    return {
        "report": report_body(report),
        "event": None if event is None else event_record(event),
        "movement": None if movement is None else movement_record(movement),
        "transactions": [transaction_record(row) for row in transactions],
    }


def list_transactions(
    session: Session,
    *,
    limit: int,
    player_id: int | None,
    city_id: int | None,
    source_event_id: int | None,
    resource: str | None,
) -> dict[str, object]:
    stmt = select(Transaction).order_by(Transaction.id.desc())
    if player_id is not None:
        stmt = stmt.where(Transaction.player_id == player_id)
    if city_id is not None:
        stmt = stmt.where(Transaction.city_id == city_id)
    if source_event_id is not None:
        stmt = stmt.where(Transaction.source_event_id == source_event_id)
    if resource is not None:
        stmt = stmt.where(Transaction.resource == resource)
    rows = session.scalars(stmt.limit(limit)).all()
    return {"transactions": [transaction_record(row) for row in rows]}
