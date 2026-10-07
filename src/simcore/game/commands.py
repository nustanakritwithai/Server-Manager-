"""Player intents. The client names a target; the server builds the movement."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
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
from simcore.game.catalog import (
    BUILDINGS,
    BUILD_SECONDS,
    CONVOY_TILES_PER_HOUR,
    FOUND_CITY_COST,
    FOUND_CITY_RATES,
    MAP_MAX,
    MAP_MIN,
    MAX_CITIES_PER_PLAYER,
    MAX_TRAIN_COUNT,
    RESEARCH,
    RESEARCH_SECONDS,
    UNIT_TRAINING,
)
from simcore.game.combat import payload_to_stacks
from simcore.game.economy import accrue_city, stock_after_accrual
from simcore.game.ledger import apply_resource_delta
from simcore.game.locks import lock_armies, lock_cities
from simcore.game.scheduling import schedule_movement
from simcore.game.tracing import record_command
from simcore.game.travel import distance, fixed_speed_seconds, interpolate, travel_progress
from simcore.models import Army, City, Event, Movement, Player
from simcore.world import bump_world_version, require_commands_open


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

    require_commands_open(session)
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

    stacks = payload_to_stacks(army.units)
    trace_id = record_command(
        session,
        player_id=player.id,
        command_type="move",
        army_id=army.id,
        target={"destination_city_id": destination.id, "relocate": relocate},
        now=now,
    )
    accrue_city(session, origin, now, trace_id=trace_id)
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
        trace_id=trace_id,
    )


def attack_city(
    session: Session,
    player: Player,
    army_id: int,
    target_city_id: int,
    now: datetime,
) -> tuple[Movement, Event]:
    require_commands_open(session)
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

    stacks = payload_to_stacks(army.units)
    trace_id = record_command(
        session,
        player_id=player.id,
        command_type="attack",
        army_id=army.id,
        target={"target_city_id": target.id},
        now=now,
    )
    accrue_city(session, origin, now, trace_id=trace_id)
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
        trace_id=trace_id,
    )
    # The arrival event key used by replay/cancel is stable per movement.
    event.idempotency_key = f"arrive:{movement.id}"
    session.flush()
    return movement, event


def recall_army(session: Session, player: Player, army_id: int, now: datetime) -> tuple[Movement, Event]:
    """Turn a marching army around, or send a reinforced army back to its home city."""

    require_commands_open(session)
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
        stacks = payload_to_stacks(army.units)
        trace_id = record_command(
            session,
            player_id=player.id,
            command_type="recall",
            army_id=army.id,
            target={"home_city_id": home.id, "from_city_id": origin.id},
            now=now,
        )
        accrue_city(session, origin, now, trace_id=trace_id)
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
            trace_id=trace_id,
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

    trace_id = record_command(
        session,
        player_id=player.id,
        command_type="recall",
        army_id=army.id,
        target={"home_city_id": home.id, "cancelled_movement_id": active.id},
        now=now,
    )
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
            trace_id=trace_id,
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
            trace_id=trace_id,
            processed_at=now,
            created_at=now,
        )
        session.add(event)
        session.flush()
        bump_world_version(session)
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
        trace_id=trace_id,
    )


def queue_build(session: Session, player: Player, city_id: int, building: str, now: datetime) -> Event:
    require_commands_open(session)
    if building not in BUILDINGS:
        raise GameError(f"unknown building {building}", code="invalid_command")
    cities = lock_cities(session, city_id)
    city = cities[city_id]
    if city.player_id != player.id:
        raise GameError("that city belongs to another player", status_code=403, code="forbidden")
    trace_id = record_command(
        session,
        player_id=player.id,
        command_type="build",
        army_id=None,
        target={"city_id": city.id, "building": building},
        now=now,
    )
    event = Event(
        due_at=now + timedelta(seconds=BUILD_SECONDS),
        type=EventType.BUILD_COMPLETE,
        payload={"city_id": city.id, "player_id": player.id, "building": building},
        status=EventStatus.PENDING,
        attempts=0,
        idempotency_key=f"build:{city.id}:{building}:{int(now.timestamp())}",
        trace_id=trace_id,
        created_at=now,
    )
    session.add(event)
    session.flush()
    bump_world_version(session)
    return event


def queue_research(session: Session, player: Player, tech: str, now: datetime) -> Event:
    require_commands_open(session)
    if tech not in RESEARCH:
        raise GameError(f"unknown research {tech}", code="invalid_command")
    trace_id = record_command(
        session,
        player_id=player.id,
        command_type="research",
        army_id=None,
        target={"tech": tech},
        now=now,
    )
    event = Event(
        due_at=now + timedelta(seconds=RESEARCH_SECONDS),
        type=EventType.RESEARCH_COMPLETE,
        payload={"player_id": player.id, "tech": tech},
        status=EventStatus.PENDING,
        attempts=0,
        idempotency_key=f"research:{player.id}:{tech}:{int(now.timestamp())}",
        trace_id=trace_id,
        created_at=now,
    )
    session.add(event)
    session.flush()
    bump_world_version(session)
    return event


def _afford(session: Session, city: City, costs: dict[str, int], now: datetime) -> None:
    stock = stock_after_accrual(session, city, now)
    short = [name for name in RESOURCES if costs.get(name, 0) > stock[name]]
    if short:
        raise GameError(
            "not enough " + ", ".join(short),
            status_code=409,
            code="insufficient_resources",
        )


def _spend(
    session: Session,
    city: City,
    costs: dict[str, int],
    *,
    reason: str,
    prefix: str,
    event: Event,
    now: datetime,
    trace_id: str,
) -> None:
    for resource in RESOURCES:
        amount = int(costs.get(resource, 0))
        if amount <= 0:
            continue
        txn = apply_resource_delta(
            session,
            city=city,
            resource=resource,
            delta=-amount,
            reason=reason,
            idempotency_key=f"event:{event.id}:{prefix}:{resource}",
            source_event_id=event.id,
            now=now,
            trace_id=trace_id,
        )
        if txn is None or txn.delta != -amount:
            raise GameError(
                f"not enough {resource}",
                status_code=409,
                code="insufficient_resources",
            )


def _flush_event(session: Session, event: Event) -> None:
    session.add(event)
    try:
        session.flush()
    except IntegrityError as exc:
        raise GameError("duplicate order", status_code=409, code="conflict") from exc


def train_units(
    session: Session,
    player: Player,
    city_id: int,
    unit_type: str,
    count: int,
    now: datetime,
    army_id: int | None = None,
) -> Event:
    """Spend city resources and schedule the units to appear in that city."""

    require_commands_open(session)
    if unit_type not in UNIT_TRAINING:
        raise GameError(f"unknown unit {unit_type}", code="invalid_command")
    if count < 1 or count > MAX_TRAIN_COUNT:
        raise GameError(f"count must be from 1 to {MAX_TRAIN_COUNT}", code="invalid_command")
    spec = UNIT_TRAINING[unit_type]
    costs = {name: int(getattr(spec, name)) * count for name in RESOURCES}
    cities = lock_cities(session, city_id)
    city = cities[city_id]
    if city.player_id != player.id:
        raise GameError("that city belongs to another player", status_code=403, code="forbidden")
    if army_id is not None:
        peeked = _owned_army(session, player, army_id)
        army = lock_armies(session, peeked.id)[peeked.id]
        _require_garrisoned(army)
        if army.location_city_id != city.id:
            raise GameError("that army is not garrisoned in this city", status_code=409, code="conflict")
    _afford(session, city, costs, now)
    trace_id = record_command(
        session,
        player_id=player.id,
        command_type="train_units",
        army_id=army_id,
        target={"city_id": city.id, "unit_type": unit_type, "count": count, "army_id": army_id},
        now=now,
    )
    event = Event(
        due_at=now + timedelta(seconds=spec.seconds * count),
        type=EventType.TRAIN_COMPLETE,
        payload={
            "city_id": city.id,
            "player_id": player.id,
            "unit_type": unit_type,
            "count": count,
            "army_id": army_id,
            "spawned": False,
        },
        status=EventStatus.PENDING,
        attempts=0,
        idempotency_key=f"train:{city.id}:{unit_type}:{count}:{army_id or 0}:{int(now.timestamp())}",
        trace_id=trace_id,
        created_at=now,
    )
    _flush_event(session, event)
    accrue_city(session, city, now, source_event_id=event.id, trace_id=trace_id)
    _spend(session, city, costs, reason=Reason.TRAIN, prefix="train", event=event, now=now, trace_id=trace_id)
    bump_world_version(session)
    return event


def found_city(
    session: Session,
    player: Player,
    source_city_id: int,
    x: int,
    y: int,
    name: str,
    now: datetime,
) -> tuple[City, Event]:
    """Pay from one of your cities and place a new city you own on a free tile."""

    require_commands_open(session)
    if not MAP_MIN <= x <= MAP_MAX or not MAP_MIN <= y <= MAP_MAX:
        raise GameError(
            f"position is outside the map ({MAP_MIN}..{MAP_MAX})",
            code="invalid_command",
        )
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 40:
        raise GameError("city name must be 1 to 40 characters", code="invalid_command")
    cities = lock_cities(session, source_city_id)
    source = cities[source_city_id]
    if source.player_id != player.id:
        raise GameError("that city belongs to another player", status_code=403, code="forbidden")
    owned = int(
        session.scalar(select(func.count()).select_from(City).where(City.player_id == player.id)) or 0
    )
    if owned >= MAX_CITIES_PER_PLAYER:
        raise GameError(
            f"you already have {MAX_CITIES_PER_PLAYER} cities",
            status_code=409,
            code="conflict",
        )
    occupied = session.scalar(select(City.id).where(City.x == x, City.y == y))
    if occupied is not None:
        raise GameError("a city already stands on that tile", status_code=409, code="conflict")
    _afford(session, source, FOUND_CITY_COST, now)
    trace_id = record_command(
        session,
        player_id=player.id,
        command_type="found_city",
        army_id=None,
        target={"source_city_id": source.id, "x": x, "y": y, "name": cleaned},
        now=now,
    )
    event = Event(
        due_at=now,
        type=EventType.CITY_FOUNDED,
        payload={"player_id": player.id, "source_city_id": source.id, "x": x, "y": y, "name": cleaned},
        status=EventStatus.COMPLETED,
        attempts=0,
        idempotency_key=f"found:{player.id}:{x}:{y}",
        trace_id=trace_id,
        processed_at=now,
        created_at=now,
    )
    _flush_event(session, event)
    accrue_city(session, source, now, source_event_id=event.id, trace_id=trace_id)
    _spend(
        session,
        source,
        FOUND_CITY_COST,
        reason=Reason.FOUND_CITY,
        prefix="found",
        event=event,
        now=now,
        trace_id=trace_id,
    )
    city = City(
        player_id=player.id,
        name=cleaned,
        x=x,
        y=y,
        wood=0,
        food=0,
        iron=0,
        gold=0,
        wood_rate=FOUND_CITY_RATES["wood"],
        food_rate=FOUND_CITY_RATES["food"],
        iron_rate=FOUND_CITY_RATES["iron"],
        gold_rate=FOUND_CITY_RATES["gold"],
        buildings={},
        last_updated=now,
        created_at=now,
    )
    session.add(city)
    try:
        session.flush()
    except IntegrityError as exc:
        raise GameError("a city already stands on that tile", status_code=409, code="conflict") from exc
    bump_world_version(session)
    return city, event


def garrison_army(
    session: Session,
    player: Player,
    army_id: int,
    city_id: int,
    now: datetime,
) -> tuple[Movement, Event]:
    """Order a garrisoned army to garrison in another city you own."""

    require_commands_open(session)
    peeked = _owned_army(session, player, army_id)
    _require_garrisoned(peeked)
    cities = lock_cities(session, peeked.location_city_id, city_id)
    army = lock_armies(session, peeked.id)[peeked.id]
    _require_garrisoned(army)
    origin = cities[army.location_city_id]
    if origin.player_id != player.id:
        raise GameError("army is not in a city you control", status_code=403, code="forbidden")
    destination = cities.get(city_id)
    if destination is None:
        raise GameError("city not found", status_code=404, code="not_found")
    if destination.player_id != player.id:
        raise GameError("you can only garrison an army in a city you own", status_code=403, code="forbidden")
    if destination.id == origin.id:
        raise GameError("army is already garrisoned in that city", status_code=409, code="conflict")
    stacks = payload_to_stacks(army.units)
    trace_id = record_command(
        session,
        player_id=player.id,
        command_type="garrison",
        army_id=army.id,
        target={"city_id": destination.id},
        now=now,
    )
    accrue_city(session, origin, now, trace_id=trace_id)
    return schedule_movement(
        session,
        army=army,
        mission=Mission.GARRISON,
        origin_city_id=origin.id,
        origin_x=float(origin.x),
        origin_y=float(origin.y),
        destination_city_id=destination.id,
        destination_x=float(destination.x),
        destination_y=float(destination.y),
        depart_at=now,
        stacks=stacks,
        event_type=EventType.ARMY_ARRIVE,
        idempotency_key=f"garrison:{army.id}:{destination.id}:{int(now.timestamp())}",
        trace_id=trace_id,
    )


def transfer_resources(
    session: Session,
    player: Player,
    source_city_id: int,
    destination_city_id: int,
    amounts: dict[str, int],
    now: datetime,
) -> Event:
    """Move resources between two cities of the same player. The convoy is timed."""

    require_commands_open(session)
    cleaned = {name: int(amounts.get(name, 0)) for name in RESOURCES}
    if any(amount < 0 for amount in cleaned.values()):
        raise GameError("resource amounts cannot be negative", code="invalid_command")
    if not any(amount > 0 for amount in cleaned.values()):
        raise GameError("transfer at least one resource", code="invalid_command")
    if source_city_id == destination_city_id:
        raise GameError("source and destination are the same city", code="invalid_command")
    cities = lock_cities(session, source_city_id, destination_city_id)
    source = cities[source_city_id]
    destination = cities.get(destination_city_id)
    if destination is None:
        raise GameError("destination city not found", status_code=404, code="not_found")
    if source.player_id != player.id or destination.player_id != player.id:
        raise GameError(
            "you can only transfer resources between cities you own",
            status_code=403,
            code="forbidden",
        )
    _afford(session, source, cleaned, now)
    try:
        seconds = fixed_speed_seconds(
            float(source.x),
            float(source.y),
            float(destination.x),
            float(destination.y),
            CONVOY_TILES_PER_HOUR,
        )
    except ValueError as exc:
        raise GameError(str(exc), code="invalid_command") from exc
    trace_id = record_command(
        session,
        player_id=player.id,
        command_type="transfer_resources",
        army_id=None,
        target={
            "source_city_id": source.id,
            "destination_city_id": destination.id,
            **cleaned,
        },
        now=now,
    )
    amount_key = ",".join(f"{name}={cleaned[name]}" for name in RESOURCES)
    event = Event(
        due_at=now + timedelta(seconds=seconds),
        type=EventType.TRANSFER_ARRIVE,
        payload={
            "source_city_id": source.id,
            "destination_city_id": destination.id,
            "player_id": player.id,
            "amounts": cleaned,
        },
        status=EventStatus.PENDING,
        attempts=0,
        idempotency_key=f"transfer:{source.id}:{destination.id}:{amount_key}:{int(now.timestamp())}",
        trace_id=trace_id,
        created_at=now,
    )
    _flush_event(session, event)
    accrue_city(session, source, now, source_event_id=event.id, trace_id=trace_id)
    _spend(
        session,
        source,
        cleaned,
        reason=Reason.TRANSFER_OUT,
        prefix="transfer_out",
        event=event,
        now=now,
        trace_id=trace_id,
    )
    bump_world_version(session)
    return event
