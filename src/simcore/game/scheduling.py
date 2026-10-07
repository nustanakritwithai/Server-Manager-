"""Create a movement leg and the event that fires when it arrives."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from simcore.constants import ArmyStatus, EventStatus, MovementStatus
from simcore.errors import GameError
from simcore.game.combat import UnitStack
from simcore.game.travel import travel_seconds
from simcore.models import Army, Event, Movement
from simcore.world import bump_world_version


def schedule_movement(
    session: Session,
    *,
    army: Army,
    mission: str,
    origin_city_id: int | None,
    origin_x: float,
    origin_y: float,
    destination_city_id: int | None,
    destination_x: float,
    destination_y: float,
    depart_at: datetime,
    stacks: tuple[UnitStack, ...] | list[UnitStack],
    event_type: str,
    idempotency_key: str,
    relocate: bool = False,
    loot: dict[str, int] | None = None,
    cause_event_id: int | None = None,
    payload_extra: dict[str, object] | None = None,
    trace_id: str | None = None,
) -> tuple[Movement, Event]:
    try:
        seconds = travel_seconds(origin_x, origin_y, destination_x, destination_y, stacks)
    except ValueError as exc:
        raise GameError(str(exc), code="invalid_command") from exc

    haul = loot or {}
    movement = Movement(
        army_id=army.id,
        origin_city_id=origin_city_id,
        destination_city_id=destination_city_id,
        origin_x=origin_x,
        origin_y=origin_y,
        destination_x=destination_x,
        destination_y=destination_y,
        depart_at=depart_at,
        arrive_at=depart_at + timedelta(seconds=seconds),
        mission=mission,
        status=MovementStatus.IN_PROGRESS,
        relocate=relocate,
        loot_wood=int(haul.get("wood", 0)),
        loot_food=int(haul.get("food", 0)),
        loot_iron=int(haul.get("iron", 0)),
        loot_gold=int(haul.get("gold", 0)),
        cause_event_id=cause_event_id,
        trace_id=trace_id,
        created_at=depart_at,
    )
    session.add(movement)
    try:
        session.flush()
    except IntegrityError as exc:
        raise GameError("army already has an active movement", status_code=409, code="conflict") from exc

    army.status = ArmyStatus.RETURNING if mission == "return" else ArmyStatus.MARCHING
    army.location_city_id = None

    payload: dict[str, object] = {
        "movement_id": movement.id,
        "army_id": army.id,
        "mission": mission,
    }
    if payload_extra:
        payload.update(payload_extra)
    event = Event(
        due_at=movement.arrive_at,
        type=event_type,
        payload=payload,
        status=EventStatus.PENDING,
        attempts=0,
        idempotency_key=idempotency_key,
        movement_id=movement.id,
        trace_id=trace_id,
        created_at=depart_at,
    )
    session.add(event)
    try:
        session.flush()
    except IntegrityError as exc:
        raise GameError("duplicate event", status_code=409, code="conflict") from exc
    bump_world_version(session)
    return movement, event
