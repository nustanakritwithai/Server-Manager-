"""Apply a due event to the world. Callers wrap this in one database transaction."""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

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
from simcore.game.combat import (
    CombatSide,
    UnitStack,
    loot_to_dict,
    payload_to_stacks,
    resolve_battle,
    stacks_to_payload,
)
from simcore.game.economy import accrue_city
from simcore.game.ledger import apply_resource_delta
from simcore.game.locks import lock_armies, lock_cities
from simcore.game.scheduling import schedule_movement
from simcore.models import Army, BattleReport, City, Event, Movement, Player, Transaction
from simcore.world import bump_world_version


def battle_seed(event_id: int, movement_id: int) -> int:
    digest = hashlib.sha256(f"simcore-battle:{event_id}:{movement_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def process_event(session: Session, event: Event, now: datetime) -> None:
    """Resolve one event at game time `now`.

    Completed and cancelled events return without writes. Resource effects use
    idempotency keys so a second call, even if status was forced back to
    pending, does not grant loot or production twice.
    """

    if event.status in (EventStatus.COMPLETED, EventStatus.CANCELLED):
        return
    if event.type == EventType.ARMY_ARRIVE:
        _process_army_arrive(session, event, now)
    elif event.type == EventType.ARMY_RETURN:
        _process_army_return(session, event, now)
    elif event.type == EventType.BUILD_COMPLETE:
        _process_build(session, event, now)
    elif event.type == EventType.RESEARCH_COMPLETE:
        _process_research(session, event, now)
    elif event.type == EventType.TRAIN_COMPLETE:
        _process_train(session, event, now)
    elif event.type == EventType.TRANSFER_ARRIVE:
        _process_transfer(session, event, now)
    elif event.type == EventType.CITY_FOUNDED:
        pass
    else:
        raise GameError(f"unknown event type {event.type}", code="invalid_event")
    event.status = EventStatus.COMPLETED
    event.processed_at = now
    event.last_error = None
    session.flush()
    bump_world_version(session)


def _process_army_arrive(session: Session, event: Event, now: datetime) -> None:
    movement = _lock_movement(session, event)
    if movement.status == MovementStatus.CANCELLED:
        return
    existing = session.scalar(select(BattleReport).where(BattleReport.event_id == event.id))
    if existing is not None:
        if movement.status != MovementStatus.COMPLETED:
            movement.status = MovementStatus.COMPLETED
            movement.resolved_at = movement.resolved_at or now
        _ensure_return_home(session, event, movement, existing, now)
        return
    if movement.status == MovementStatus.COMPLETED:
        return
    if movement.mission == Mission.ATTACK:
        _resolve_attack(session, event, movement, now)
    elif movement.mission in {Mission.MOVE, Mission.GARRISON}:
        _resolve_move(session, event, movement, now)
    elif movement.mission == Mission.RETURN:
        _process_army_return(session, event, now)
    else:
        raise GameError(f"unknown mission {movement.mission}", code="invalid_event")


def _resolve_attack(session: Session, event: Event, movement: Movement, now: datetime) -> None:
    if movement.destination_city_id is None:
        raise GameError("attack movement has no destination city", code="invalid_event")
    attacker_peek = session.get(Army, movement.army_id)
    if attacker_peek is None:
        raise GameError("army missing", code="invalid_event")
    cities = lock_cities(session, movement.destination_city_id)
    destination = cities[movement.destination_city_id]
    accrue_city(session, destination, now, source_event_id=event.id, trace_id=event.trace_id)

    defender_ids = list(
        session.scalars(
            select(Army.id).where(
                Army.location_city_id == destination.id,
                Army.status == ArmyStatus.GARRISONED,
            )
        ).all()
    )
    armies = lock_armies(session, movement.army_id, *defender_ids)
    attacker = armies[movement.army_id]
    defenders = [
        armies[army_id]
        for army_id in sorted(defender_ids)
        if armies[army_id].status == ArmyStatus.GARRISONED and armies[army_id].location_city_id == destination.id
    ]

    attacker_stacks = payload_to_stacks(attacker.units)
    defender_stacks = _merge_garrison(defenders)
    resources = tuple((name, int(getattr(destination, name))) for name in RESOURCES)
    seed = battle_seed(event.id, movement.id)
    result = resolve_battle(
        CombatSide(attacker_stacks),
        CombatSide(defender_stacks, resources),
        seed,
    )

    attacker.units = stacks_to_payload(result.attacker_remaining)
    flag_modified(attacker, "units")
    _apply_garrison_losses(defenders, result.defender_casualties)
    if not attacker.units:
        attacker.status = ArmyStatus.DESTROYED
        attacker.location_city_id = None

    loot = loot_to_dict(result.loot)
    for resource, amount in loot.items():
        if amount <= 0:
            continue
        txn = apply_resource_delta(
            session,
            city=destination,
            resource=resource,
            delta=-amount,
            reason=Reason.LOOT_LOST,
            idempotency_key=f"event:{event.id}:loot_lost:{resource}",
            source_event_id=event.id,
            now=now,
            trace_id=event.trace_id,
        )
        if txn is None or txn.delta != -amount:
            raise GameError("loot could not be taken in full", code="invalid_event")

    report = BattleReport(
        event_id=event.id,
        movement_id=movement.id,
        attacker_player_id=attacker.player_id,
        defender_player_id=destination.player_id,
        attacker_army_id=attacker.id,
        defender_city_id=destination.id,
        seed=seed,
        winner=result.winner,
        attacker_before=stacks_to_payload(result.attacker_before),
        defender_before=stacks_to_payload(result.defender_before),
        attacker_remaining=stacks_to_payload(result.attacker_remaining),
        defender_remaining=stacks_to_payload(result.defender_remaining),
        attacker_casualties=stacks_to_payload(result.attacker_casualties),
        defender_casualties=stacks_to_payload(result.defender_casualties),
        defender_resources={name: amount for name, amount in resources},
        loot=loot,
        trace_id=event.trace_id,
        rounds=[
            {
                "round": entry.round_index,
                "attacker_variance_bp": entry.attacker_variance_bp,
                "defender_variance_bp": entry.defender_variance_bp,
                "damage_to_attacker": entry.damage_to_attacker,
                "damage_to_defender": entry.damage_to_defender,
            }
            for entry in result.rounds
        ],
        created_at=now,
    )
    session.add(report)
    movement.status = MovementStatus.COMPLETED
    movement.resolved_at = now
    session.flush()

    if not attacker.units:
        return
    home = session.get(City, attacker.home_city_id)
    if home is None:
        raise GameError("home city missing", code="invalid_event")
    schedule_movement(
        session,
        army=attacker,
        mission=Mission.RETURN,
        origin_city_id=destination.id,
        origin_x=float(destination.x),
        origin_y=float(destination.y),
        destination_city_id=home.id,
        destination_x=float(home.x),
        destination_y=float(home.y),
        depart_at=now,
        stacks=payload_to_stacks(attacker.units),
        event_type=EventType.ARMY_RETURN,
        idempotency_key=f"return:{event.id}",
        loot=loot,
        cause_event_id=event.id,
        trace_id=event.trace_id,
    )


def _resolve_move(session: Session, event: Event, movement: Movement, now: datetime) -> None:
    if movement.destination_city_id is None:
        raise GameError("move has no destination", code="invalid_event")
    cities = lock_cities(session, movement.destination_city_id)
    destination = cities[movement.destination_city_id]
    armies = lock_armies(session, movement.army_id)
    army = armies[movement.army_id]
    if destination.player_id != army.player_id:
        raise GameError("destination is no longer owned by the army's player", code="invalid_event")
    accrue_city(session, destination, now, source_event_id=event.id, trace_id=event.trace_id)
    army.location_city_id = destination.id
    army.status = ArmyStatus.GARRISONED
    if movement.relocate:
        army.home_city_id = destination.id
    movement.status = MovementStatus.COMPLETED
    movement.resolved_at = now
    session.flush()


def _process_army_return(session: Session, event: Event, now: datetime) -> None:
    movement = _lock_movement(session, event)
    if movement.status == MovementStatus.CANCELLED:
        return
    if movement.status == MovementStatus.COMPLETED:
        return
    if movement.destination_city_id is None:
        raise GameError("return has no home city", code="invalid_event")
    cities = lock_cities(session, movement.destination_city_id)
    home = cities[movement.destination_city_id]
    army = lock_armies(session, movement.army_id)[movement.army_id]
    accrue_city(session, home, now, source_event_id=event.id, trace_id=event.trace_id)
    for resource in RESOURCES:
        amount = int(getattr(movement, f"loot_{resource}"))
        if amount <= 0:
            continue
        txn = apply_resource_delta(
            session,
            city=home,
            resource=resource,
            delta=amount,
            reason=Reason.LOOT_GAINED,
            idempotency_key=f"event:{event.id}:loot_gained:{resource}",
            source_event_id=event.id,
            now=now,
            trace_id=event.trace_id,
        )
        if txn is not None and txn.delta != amount and txn.idempotency_key == f"event:{event.id}:loot_gained:{resource}":
            # A previous attempt already stored a different delta. Keep it.
            continue
    movement.status = MovementStatus.COMPLETED
    movement.resolved_at = now
    if army.units:
        army.status = ArmyStatus.GARRISONED
        army.location_city_id = home.id
    else:
        army.status = ArmyStatus.DESTROYED
        army.location_city_id = None
    session.flush()


def _ensure_return_home(
    session: Session,
    event: Event,
    movement: Movement,
    report: BattleReport,
    now: datetime,
) -> None:
    """If a replayed arrival already has a report, do not fight again.

    Create the walk-home leg only when the first attempt never scheduled it.
    """

    if movement.mission != Mission.ATTACK:
        return
    existing = session.scalar(select(Event).where(Event.idempotency_key == f"return:{event.id}"))
    if existing is not None:
        return
    army = session.get(Army, movement.army_id)
    if army is None or not army.units or army.status == ArmyStatus.DESTROYED:
        return
    if army.status == ArmyStatus.GARRISONED and army.location_city_id == army.home_city_id:
        return
    active = session.scalar(
        select(Movement).where(Movement.army_id == army.id, Movement.status == MovementStatus.IN_PROGRESS)
    )
    if active is not None:
        return
    home = session.get(City, army.home_city_id)
    if home is None or movement.destination_city_id is None:
        return
    loot = {name: int(report.loot.get(name, 0)) for name in RESOURCES}
    schedule_movement(
        session,
        army=army,
        mission=Mission.RETURN,
        origin_city_id=movement.destination_city_id,
        origin_x=movement.destination_x,
        origin_y=movement.destination_y,
        destination_city_id=home.id,
        destination_x=float(home.x),
        destination_y=float(home.y),
        depart_at=now,
        stacks=payload_to_stacks(army.units),
        event_type=EventType.ARMY_RETURN,
        idempotency_key=f"return:{event.id}",
        loot=loot,
        cause_event_id=event.id,
        trace_id=event.trace_id,
    )


def _process_build(session: Session, event: Event, now: datetime) -> None:
    city_id = int(event.payload["city_id"])
    building = str(event.payload["building"])
    key = f"event:{event.id}:build:{building}"
    city = lock_cities(session, city_id)[city_id]
    current = int((city.buildings or {}).get(building, 0))
    if not _record_effect(
        session,
        key=key,
        player_id=city.player_id,
        city_id=city.id,
        resource=f"building:{building}",
        delta=1,
        balance_after=current + 1,
        reason="build",
        source_event_id=event.id,
        now=now,
        trace_id=event.trace_id,
    ):
        return
    updated = dict(city.buildings or {})
    updated[building] = current + 1
    city.buildings = updated
    flag_modified(city, "buildings")
    session.flush()


def _process_research(session: Session, event: Event, now: datetime) -> None:
    player_id = int(event.payload["player_id"])
    tech = str(event.payload["tech"])
    key = f"event:{event.id}:research:{tech}"
    player = session.get(Player, player_id, with_for_update=True)
    if player is None:
        raise GameError("player missing", code="invalid_event")
    current = int((player.research or {}).get(tech, 0))
    if not _record_effect(
        session,
        key=key,
        player_id=player.id,
        city_id=None,
        resource=f"research:{tech}",
        delta=1,
        balance_after=current + 1,
        reason="research",
        source_event_id=event.id,
        now=now,
        trace_id=event.trace_id,
    ):
        return
    updated = dict(player.research or {})
    updated[tech] = current + 1
    player.research = updated
    flag_modified(player, "research")
    session.flush()


def _process_train(session: Session, event: Event, now: datetime) -> None:
    city_id = int(event.payload["city_id"])
    unit_type = str(event.payload["unit_type"])
    count = int(event.payload["count"])
    army_id = event.payload.get("army_id")
    key = f"event:{event.id}:train_units:{unit_type}"
    if session.scalar(select(Transaction).where(Transaction.idempotency_key == key)) is not None:
        return
    city = lock_cities(session, city_id)[city_id]
    accrue_city(session, city, now, source_event_id=event.id, trace_id=event.trace_id)
    army: Army | None = None
    if army_id is not None:
        candidate = session.get(Army, int(army_id), with_for_update=True)
        if (
            candidate is not None
            and candidate.player_id == city.player_id
            and candidate.status == ArmyStatus.GARRISONED
            and candidate.location_city_id == city.id
        ):
            army = candidate
    spawned = False
    if army is None:
        army = Army(
            player_id=city.player_id,
            name=f"Trained {city.id} {unit_type}"[:40],
            home_city_id=city.id,
            location_city_id=city.id,
            status=ArmyStatus.GARRISONED,
            units=[],
            created_at=now,
        )
        session.add(army)
        session.flush()
        spawned = True
    _add_units(army, unit_type, count)
    balance = _unit_count(army, unit_type)
    if not _record_effect(
        session,
        key=key,
        player_id=city.player_id,
        city_id=city.id,
        resource=f"unit:{unit_type}",
        delta=count,
        balance_after=balance,
        reason="train_units",
        source_event_id=event.id,
        now=now,
        trace_id=event.trace_id,
    ):
        return
    payload = dict(event.payload)
    payload["spawned"] = spawned
    payload["army_id"] = army.id
    event.payload = payload
    flag_modified(event, "payload")
    session.flush()


def _process_transfer(session: Session, event: Event, now: datetime) -> None:
    payload = event.payload
    destination_id = int(payload["destination_city_id"])
    player_id = int(payload["player_id"])
    amounts = payload.get("amounts") or {}
    city = lock_cities(session, destination_id)[destination_id]
    if city.player_id != player_id:
        raise GameError("destination is no longer owned by that player", code="invalid_event")
    accrue_city(session, city, now, source_event_id=event.id, trace_id=event.trace_id)
    for resource in RESOURCES:
        amount = int(amounts.get(resource, 0) or 0)
        if amount <= 0:
            continue
        apply_resource_delta(
            session,
            city=city,
            resource=resource,
            delta=amount,
            reason=Reason.TRANSFER_IN,
            idempotency_key=f"event:{event.id}:transfer_in:{resource}",
            source_event_id=event.id,
            now=now,
            trace_id=event.trace_id,
        )


def _add_units(army: Army, unit_type: str, count: int) -> None:
    stacks = [dict(stack) for stack in (army.units or [])]
    for stack in stacks:
        if str(stack.get("type")) == unit_type:
            stack["count"] = int(stack["count"]) + count
            break
    else:
        stacks.append({"type": unit_type, "count": count})
    army.units = stacks
    flag_modified(army, "units")


def _unit_count(army: Army, unit_type: str) -> int:
    total = 0
    for stack in army.units or []:
        if str(stack.get("type")) == unit_type:
            total += int(stack.get("count") or 0)
    return total


def _record_effect(
    session: Session,
    *,
    key: str,
    player_id: int | None,
    city_id: int | None,
    resource: str,
    delta: int,
    balance_after: int,
    reason: str,
    source_event_id: int,
    now: datetime,
    trace_id: str | None = None,
) -> bool:
    existing = session.scalar(select(Transaction).where(Transaction.idempotency_key == key))
    if existing is not None:
        return False
    nested = session.begin_nested()
    try:
        session.add(
            Transaction(
                player_id=player_id,
                city_id=city_id,
                resource=resource,
                delta=delta,
                balance_after=balance_after,
                reason=reason,
                source_event_id=source_event_id,
                idempotency_key=key,
                trace_id=trace_id,
                created_at=now,
            )
        )
        session.flush()
        nested.commit()
        return True
    except IntegrityError:
        nested.rollback()
        return False


def _lock_movement(session: Session, event: Event) -> Movement:
    movement_id = event.movement_id or int(event.payload["movement_id"])
    movement = session.get(Movement, movement_id, with_for_update=True)
    if movement is None:
        raise GameError("movement missing", code="invalid_event")
    return movement


def _merge_garrison(armies: list[Army]) -> tuple[UnitStack, ...]:
    stacks: list[UnitStack] = []
    for army in armies:
        stacks.extend(payload_to_stacks(army.units))
    return payload_to_stacks([{"type": stack.unit_type, "count": stack.count} for stack in stacks])


def _apply_garrison_losses(armies: list[Army], casualties: tuple[UnitStack, ...]) -> None:
    remaining = {stack.unit_type: stack.count for stack in casualties}
    for army in sorted(armies, key=lambda item: item.id):
        kept: list[dict[str, int | str]] = []
        for stack in army.units:
            unit_type = str(stack["type"])
            count = int(stack["count"])
            lose = min(count, remaining.get(unit_type, 0))
            remaining[unit_type] = remaining.get(unit_type, 0) - lose
            left = count - lose
            if left > 0:
                kept.append({"type": unit_type, "count": left})
        army.units = kept
        flag_modified(army, "units")
        if not kept:
            army.status = ArmyStatus.DESTROYED
            army.location_city_id = None
