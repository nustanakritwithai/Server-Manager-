"""Player intents. The client names a target; the server builds the movement."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from simcore.constants import (
    ArmyStatus,
    EventStatus,
    EventType,
    Mission,
    MovementStatus,
)
from simcore.errors import GameError
from simcore.game.catalog import BUILDINGS, BUILD_SECONDS, RESEARCH, RESEARCH_SECONDS
from simcore.game.combat import payload_to_stacks
from simcore.game.economy import accrue_city
from simcore.game.locks import lock_armies, lock_cities
from simcore.game.scheduling import schedule_movement
from simcore.game.travel import distance, interpolate, travel_progress
from simcore.models import Army, City, Event, Movement, Player


def _owned_army(session: Session, player: Player, army_id: int) -> Army:
    army = session.get(Army, army_id)
    if army is None:
        raise GameError("army not found", status_code=404, code="not_found")
    if army.player_id != player.id:
        raise GameError("that army belongs to another player", status_code=403, code="forbidden")
    return army


def _require_garrisoned(army: Army) -> None:
    if army.status == ArmyStatus.DESTROYED:
        raise GameError("that army has been destroyed", status_code=409, code="conflict")
    if army.status != ArmyStatus.GARRISONED or army.location_city_id is None:
        raise GameError("army is already on the march", status_code=409, code="conflict")


def move_army(
    session: Session,
    player: Player,
    army_id: int,
    destination_city_id: int,
    now: datetime,
    *,
    relocate: bool = False,
) -> tuple[Movement, Event]:
    """Send a garrisoned army to another city you own. It stays there (reinforce).

    relocate=True also changes the army's home city on arrival.
    """

    peeked = _owned_army(session, player, army_id)
    _require_garrisoned(peeked)
    cities = lock_cities(session, peeked.location_city_id, destination_city_id)
    army = lock_armies(session, peeked.id)[peeked.id]
    _require_garrisoned(army)
    origin = cities[army.location_city_id]
    if origin.player_id != player.id:
        raise GameError("army is not in a city you control", status_code=403, code="forbidden")
    destination = cities.get(destination_city_id)
    if destination is None:
        raise GameError("destination city not found", status_code=404, code="not_found")
    if destination.player_id != player.id:
        raise GameError("you can only move an army to a city you own", status_code=403, code="forbidden")
    if destination.id == origin.id:
        raise GameError("army is already in that city", status_code=400, code="invalid_command")

    accrue_city(session, origin, now)
    stacks = payload_to_stacks(army.units)
    return schedule_movement(
        session,
        army=army,
        mission=Mission.MOVE,
        origin_city_id=origin.id,
        origin_x=float(origin.x),
        origin_y=float(origin.y),
        destination_city_id=destination.id,
        destination_x=float(destination.x),
        destination_y=float(destination.y),
        depart_at=now,
        stacks=stacks,
        event_type=EventType.ARMY_ARRIVE,
        idempotency_key=f"move:{army.id}:{destination.id}:{int(now.timestamp())}",
        relocate=relocate,
    )


def attack_city(
    session: Session,
    player: Player,
    army_id: int,
    target_city_id: int,
    now: datetime,
) -> tuple[Movement, Event]:
    peeked = _owned_army(session, player, army_id)
    _require_garrisoned(peeked)
    if peeked.location_city_id == target_city_id:
        raise GameError("army is already in that city", status_code=400, code="invalid_command")
    cities = lock_cities(session, peeked.location_city_id, target_city_id)
    army = lock_armies(session, peeked.id)[peeked.id]
    _require_garrisoned(army)
    origin = cities[army.location_city_id]
    target = cities.get(target_city_id)
    if target is None:
        raise GameError("target city not found", status_code=404, code="not_found")
    if origin.player_id != player.id:
        raise GameError("army is not in a city you control", status_code=403, code="forbidden")
    if target.player_id == player.id:
        raise GameError("cannot attack your own city; use move", status_code=400, code="invalid_command")
    if target.x == origin.x and target.y == origin.y:
        raise GameError("army is already at that position", status_code=400, code="invalid_command")

    accrue_city(session, origin, now)
    stacks = payload_to_stacks(army.units)
    movement, event = schedule_movement(
        session,
        army=army,
        mission=Mission.ATTACK,
        origin_city_id=origin.id,
        origin_x=float(origin.x),
        origin_y=float(origin.y),
        destination_city_id=target.id,
        destination_x=float(target.x),
        destination_y=float(target.y),
        depart_at=now,
        stacks=stacks,
        event_type=EventType.ARMY_ARRIVE,
        idempotency_key=f"attack:{army.id}:{target.id}:{int(now.timestamp())}",
    )
    # The arrival event key used by replay/cancel is stable per movement.
    event.idempotency_key = f"arrive:{movement.id}"
    session.flush()
    return movement, event


def recall_army(session: Session, player: Player, army_id: int, now: datetime) -> tuple[Movement, Event]:
    """Turn a marching army around, or send a reinforced army back to its home city."""

    peeked = _owned_army(session, player, army_id)
    if peeked.status == ArmyStatus.DESTROYED:
        raise GameError("that army has been destroyed", status_code=409, code="conflict")

    active = session.scalar(
        select(Movement).where(Movement.army_id == peeked.id, Movement.status == MovementStatus.IN_PROGRESS)
    )
    pending_event: Event | None = None
    if active is not None:
        pending_event = session.scalar(
            select(Event)
            .where(Event.movement_id == active.id, Event.status == EventStatus.PENDING)
            .with_for_update()
        )
        active = session.get(Movement, active.id, with_for_update=True)

    home_id = peeked.home_city_id
    origin_city_id = peeked.location_city_id
    cities = lock_cities(session, home_id, origin_city_id)
    army = lock_armies(session, peeked.id)[peeked.id]
    home = cities[army.home_city_id]

    if army.status == ArmyStatus.GARRISONED:
        if army.location_city_id is None or army.location_city_id == army.home_city_id:
            raise GameError("army is already home", status_code=409, code="conflict")
        origin = cities[army.location_city_id]
        if origin.player_id != player.id:
            raise GameError("army is not in a city you control", status_code=403, code="forbidden")
        accrue_city(session, origin, now)
        stacks = payload_to_stacks(army.units)
        return schedule_movement(
            session,
            army=army,
            mission=Mission.RETURN,
            origin_city_id=origin.id,
            origin_x=float(origin.x),
            origin_y=float(origin.y),
            destination_city_id=home.id,
            destination_x=float(home.x),
            destination_y=float(home.y),
            depart_at=now,
            stacks=stacks,
            event_type=EventType.ARMY_RETURN,
            idempotency_key=f"recall-garrison:{army.id}:{origin.id}:{int(now.timestamp())}",
        )

    if active is None or active.status != MovementStatus.IN_PROGRESS:
        raise GameError("army has no march to recall", status_code=409, code="conflict")
    if active.mission == Mission.RETURN and active.destination_city_id == army.home_city_id:
        raise GameError("army is already returning home", status_code=409, code="conflict")
    if pending_event is None:
        raise GameError("that march is being resolved; try again", status_code=409, code="conflict")

    progress = travel_progress(active.depart_at, active.arrive_at, now)
    x, y = interpolate(active.origin_x, active.origin_y, active.destination_x, active.destination_y, progress)
    pending_event.status = EventStatus.CANCELLED
    active.status = MovementStatus.CANCELLED
    active.resolved_at = now
    session.flush()

    # Recalling before the army has left its tile puts it straight back in the garrison.
    if distance(x, y, float(home.x), float(home.y)) < 1e-6:
        army.status = ArmyStatus.GARRISONED
        army.location_city_id = home.id
        movement = Movement(
            army_id=army.id,
            origin_city_id=home.id,
            destination_city_id=home.id,
            origin_x=float(home.x),
            origin_y=float(home.y),
            destination_x=float(home.x),
            destination_y=float(home.y),
            depart_at=now,
            arrive_at=now,
            mission=Mission.RETURN,
            status=MovementStatus.COMPLETED,
            relocate=False,
            loot_wood=0,
            loot_food=0,
            loot_iron=0,
            loot_gold=0,
            cause_event_id=pending_event.id,
            created_at=now,
            resolved_at=now,
        )
        session.add(movement)
        session.flush()
        event = Event(
            due_at=now,
            type=EventType.ARMY_RETURN,
            payload={"movement_id": movement.id, "army_id": army.id, "mission": Mission.RETURN, "immediate": True},
            status=EventStatus.COMPLETED,
            attempts=0,
            idempotency_key=f"recall:{active.id}",
            movement_id=movement.id,
            processed_at=now,
            created_at=now,
        )
        session.add(event)
        session.flush()
        return movement, event

    stacks = payload_to_stacks(army.units)
    return schedule_movement(
        session,
        army=army,
        mission=Mission.RETURN,
        origin_city_id=None,
        origin_x=x,
        origin_y=y,
        destination_city_id=home.id,
        destination_x=float(home.x),
        destination_y=float(home.y),
        depart_at=now,
        stacks=stacks,
        event_type=EventType.ARMY_RETURN,
        idempotency_key=f"recall:{active.id}",
        cause_event_id=pending_event.id,
    )


def queue_build(session: Session, player: Player, city_id: int, building: str, now: datetime) -> Event:
    if building not in BUILDINGS:
        raise GameError(f"unknown building {building}", code="invalid_command")
    cities = lock_cities(session, city_id)
    city = cities[city_id]
    if city.player_id != player.id:
        raise GameError("that city belongs to another player", status_code=403, code="forbidden")
    event = Event(
        due_at=now + timedelta(seconds=BUILD_SECONDS),
        type=EventType.BUILD_COMPLETE,
        payload={"city_id": city.id, "player_id": player.id, "building": building},
        status=EventStatus.PENDING,
        attempts=0,
        idempotency_key=f"build:{city.id}:{building}:{int(now.timestamp())}",
        created_at=now,
    )
    session.add(event)
    session.flush()
    return event


def queue_research(session: Session, player: Player, tech: str, now: datetime) -> Event:
    if tech not in RESEARCH:
        raise GameError(f"unknown research {tech}", code="invalid_command")
    event = Event(
        due_at=now + timedelta(seconds=RESEARCH_SECONDS),
        type=EventType.RESEARCH_COMPLETE,
        payload={"player_id": player.id, "tech": tech},
        status=EventStatus.PENDING,
        attempts=0,
        idempotency_key=f"research:{player.id}:{tech}:{int(now.timestamp())}",
        created_at=now,
    )
    session.add(event)
    session.flush()
    return event
