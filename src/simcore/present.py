"""Shape rows into the JSON the future Godot client will poll."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from simcore.constants import ArmyStatus, MovementStatus
from simcore.game.travel import interpolate, travel_progress
from simcore.models import Army, BattleReport, City, Event, Movement, Player


def _position(city_id: int | None, x: float, y: float) -> dict[str, float | int | None]:
    return {"city_id": city_id, "x": round(float(x), 4), "y": round(float(y), 4)}


def movement_body(movement: Movement, event: Event | None) -> dict[str, object]:
    return {
        "movement_id": movement.id,
        "event_id": None if event is None else event.id,
        "army_id": movement.army_id,
        "mission": movement.mission,
        "status": movement.status,
        "depart_at": movement.depart_at,
        "arrive_at": movement.arrive_at,
        "trace_id": movement.trace_id,
        "origin": _position(movement.origin_city_id, movement.origin_x, movement.origin_y),
        "destination": _position(movement.destination_city_id, movement.destination_x, movement.destination_y),
    }


def event_for_movement(session: Session, movement: Movement) -> Event | None:
    return session.scalar(
        select(Event).where(Event.movement_id == movement.id).order_by(Event.id.desc()).limit(1)
    )


def active_movement(session: Session, army_id: int) -> Movement | None:
    return session.scalar(
        select(Movement).where(Movement.army_id == army_id, Movement.status == MovementStatus.IN_PROGRESS)
    )


def army_body(session: Session, army: Army, now: datetime) -> dict[str, object]:
    movement = active_movement(session, army.id)
    if army.status == ArmyStatus.GARRISONED and army.location_city_id is not None:
        city = session.get(City, army.location_city_id)
        position = _position(city.id, city.x, city.y) if city is not None else _position(None, 0, 0)
    elif movement is not None:
        progress = travel_progress(movement.depart_at, movement.arrive_at, now)
        x, y = interpolate(movement.origin_x, movement.origin_y, movement.destination_x, movement.destination_y, progress)
        position = _position(None, x, y)
    else:
        position = _position(army.location_city_id, 0, 0)
    body: dict[str, object] = {
        "id": army.id,
        "name": army.name,
        "player_id": army.player_id,
        "status": army.status,
        "home_city_id": army.home_city_id,
        "location_city_id": army.location_city_id,
        "units": army.units,
        "position": position,
        "movement": None if movement is None else movement_body(movement, event_for_movement(session, movement)),
    }
    return body


def report_body(report: BattleReport) -> dict[str, object]:
    """Battle report JSON. Shared by the player client and the admin inspector."""

    return {
        "id": report.id,
        "event_id": report.event_id,
        "movement_id": report.movement_id,
        "attacker_player_id": report.attacker_player_id,
        "defender_player_id": report.defender_player_id,
        "attacker_army_id": report.attacker_army_id,
        "defender_city_id": report.defender_city_id,
        "seed": report.seed,
        "winner": report.winner,
        "attacker_before": report.attacker_before,
        "defender_before": report.defender_before,
        "attacker_remaining": report.attacker_remaining,
        "defender_remaining": report.defender_remaining,
        "attacker_casualties": report.attacker_casualties,
        "defender_casualties": report.defender_casualties,
        "defender_resources": report.defender_resources,
        "loot": report.loot,
        "rounds": report.rounds,
        "trace_id": report.trace_id,
        "created_at": report.created_at,
    }


def city_body(session: Session, city: City, *, include_resources: bool) -> dict[str, object]:
    owner = session.get(Player, city.player_id)
    garrison = session.scalars(
        select(Army.id).where(Army.location_city_id == city.id, Army.status == ArmyStatus.GARRISONED).order_by(Army.id)
    ).all()
    body: dict[str, object] = {
        "id": city.id,
        "name": city.name,
        "player_id": city.player_id,
        "player_name": None if owner is None else owner.name,
        "x": city.x,
        "y": city.y,
        "garrison_army_ids": list(garrison),
    }
    if include_resources:
        body.update(
            {
                "wood": city.wood,
                "food": city.food,
                "iron": city.iron,
                "gold": city.gold,
                "wood_rate": city.wood_rate,
                "food_rate": city.food_rate,
                "iron_rate": city.iron_rate,
                "gold_rate": city.gold_rate,
                "buildings": city.buildings,
                "last_updated": city.last_updated,
            }
        )
    return body
