"""Read-only god view of cities, armies, and in-progress movements.

Coordinates already live on the rows. Cities store integer ``x`` and ``y``.
Each movement leg stores ``origin_x/y``, ``destination_x/y``, ``depart_at``,
and ``arrive_at``. This module does not invent a map seed and does not write.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from simcore.api.deps import get_clock, get_session, require_admin
from simcore.clock import OffsetClock
from simcore.config import Settings
from simcore.constants import ArmyStatus, MovementStatus
from simcore.errors import GameError
from simcore.game.travel import interpolate, travel_progress
from simcore.models import Army, City, Event, Movement, Player

router = APIRouter()

DEFAULT_CITY_LIMIT = 2000
MAX_CITY_LIMIT = 5000
DEFAULT_ARMY_LIMIT = 2000
MAX_ARMY_LIMIT = 5000
DEFAULT_MOVEMENT_LIMIT = 1000
MAX_MOVEMENT_LIMIT = 2000
PLAYER_DIRECTORY_LIMIT = 2000

_NOTES = (
    "Coordinates are the stored city and movement columns. "
    "Current positions of in-progress movements are interpolated on the server "
    "with travel_progress and interpolate. "
    "No map seed and no synthetic placement are used. "
    "There is no neutral or NPC entity table."
)


def _xy(x: float, y: float) -> dict[str, float]:
    return {"x": round(float(x), 4), "y": round(float(y), 4)}


def _eta_seconds(arrive_at: datetime, now: datetime) -> int:
    remaining = (arrive_at - now).total_seconds()
    if remaining <= 0:
        return 0
    return int(math.ceil(remaining - 1e-9))


def _trace_state(trace_id: str | None, *, movement_present: bool) -> str:
    if not movement_present:
        return "NONE"
    if trace_id:
        return "AVAILABLE"
    return "LEGACY"


def _leg(movement: Movement, now: datetime) -> dict[str, object]:
    """Server interpolation for one in-progress leg. Positions are rounded to 4 decimals."""

    progress = travel_progress(movement.depart_at, movement.arrive_at, now)
    x, y = interpolate(
        movement.origin_x,
        movement.origin_y,
        movement.destination_x,
        movement.destination_y,
        progress,
    )
    reported = round(progress, 6)
    dx = round(float(movement.destination_x) - float(movement.origin_x), 4)
    dy = round(float(movement.destination_y) - float(movement.origin_y), 4)
    direction = None if dx == 0 and dy == 0 else {"dx": dx, "dy": dy}
    return {
        "origin": {
            "city_id": movement.origin_city_id,
            **_xy(movement.origin_x, movement.origin_y),
        },
        "destination": {
            "city_id": movement.destination_city_id,
            **_xy(movement.destination_x, movement.destination_y),
        },
        "position": _xy(x, y),
        "progress": reported,
        "progress_percent": round(reported * 100, 2),
        "eta_seconds": _eta_seconds(movement.arrive_at, now),
        "direction": direction,
    }


def _limit_meta(*, limit: int, returned: int, total: int) -> dict[str, object]:
    return {
        "limit": limit,
        "returned": returned,
        "total": total,
        "truncated": total > returned,
    }


def _bounds(points: list[tuple[float, float]]) -> dict[str, float] | None:
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {
        "min_x": round(min(xs), 4),
        "min_y": round(min(ys), 4),
        "max_x": round(max(xs), 4),
        "max_y": round(max(ys), 4),
    }


def _latest_event_ids(session: Session, movement_ids: list[int]) -> dict[int, int]:
    if not movement_ids:
        return {}
    rows = session.execute(
        select(Event.movement_id, Event.id)
        .where(Event.movement_id.in_(movement_ids))
        .order_by(Event.id.desc())
    ).all()
    found: dict[int, int] = {}
    for movement_id, event_id in rows:
        if movement_id is None or movement_id in found:
            continue
        found[int(movement_id)] = int(event_id)
    return found


def world_map(
    session: Session,
    *,
    now: datetime,
    player_id: int | None,
    city_limit: int,
    army_limit: int,
    movement_limit: int,
) -> dict[str, object]:
    """Assemble the god-view payload. Selects and counts only."""

    if player_id is not None and session.get(Player, player_id) is None:
        raise GameError("player not found", status_code=404, code="not_found")

    city_filter = City.player_id == player_id if player_id is not None else None
    army_filter = Army.player_id == player_id if player_id is not None else None

    city_count_stmt = select(func.count()).select_from(City)
    army_count_stmt = select(func.count()).select_from(Army)
    if city_filter is not None:
        city_count_stmt = city_count_stmt.where(city_filter)
    if army_filter is not None:
        army_count_stmt = army_count_stmt.where(army_filter)
    city_total = int(session.scalar(city_count_stmt) or 0)
    army_total = int(session.scalar(army_count_stmt) or 0)

    city_stmt = select(City).order_by(City.id).limit(city_limit)
    army_stmt = select(Army).order_by(Army.id).limit(army_limit)
    if city_filter is not None:
        city_stmt = city_stmt.where(city_filter)
    if army_filter is not None:
        army_stmt = army_stmt.where(army_filter)
    cities = list(session.scalars(city_stmt).all())
    armies = list(session.scalars(army_stmt).all())

    movement_count_stmt = (
        select(func.count())
        .select_from(Movement)
        .join(Army, Army.id == Movement.army_id)
        .where(Movement.status == MovementStatus.IN_PROGRESS)
    )
    movement_stmt = (
        select(Movement)
        .join(Army, Army.id == Movement.army_id)
        .where(Movement.status == MovementStatus.IN_PROGRESS)
        .order_by(Movement.id)
        .limit(movement_limit)
    )
    if player_id is not None:
        movement_count_stmt = movement_count_stmt.where(Army.player_id == player_id)
        movement_stmt = movement_stmt.where(Army.player_id == player_id)
    movement_total = int(session.scalar(movement_count_stmt) or 0)
    movements = list(session.scalars(movement_stmt).all())

    army_ids = [army.id for army in armies]
    position_movements: list[Movement] = []
    if army_ids:
        position_movements = list(
            session.scalars(
                select(Movement).where(
                    Movement.status == MovementStatus.IN_PROGRESS,
                    Movement.army_id.in_(army_ids),
                )
            ).all()
        )
    movement_by_army = {movement.army_id: movement for movement in position_movements}

    location_ids = [army.location_city_id for army in armies if army.location_city_id is not None]
    city_coords: dict[int, tuple[float, float]] = {}
    if location_ids:
        for city_id, x, y in session.execute(
            select(City.id, City.x, City.y).where(City.id.in_(location_ids))
        ).all():
            city_coords[int(city_id)] = (float(x), float(y))

    player_total = int(session.scalar(select(func.count()).select_from(Player)) or 0)
    players = list(session.scalars(select(Player).order_by(Player.id).limit(PLAYER_DIRECTORY_LIMIT)).all())
    names = {player.id: player.name for player in players}

    event_ids = _latest_event_ids(session, [movement.id for movement in movements])
    army_by_id = {army.id: army for army in armies}
    missing_army_ids = [movement.army_id for movement in movements if movement.army_id not in army_by_id]
    if missing_army_ids:
        for army in session.scalars(select(Army).where(Army.id.in_(missing_army_ids))).all():
            army_by_id[army.id] = army

    garrison: dict[int, list[int]] = {}
    for army in armies:
        if army.status == ArmyStatus.GARRISONED and army.location_city_id is not None:
            garrison.setdefault(army.location_city_id, []).append(army.id)

    city_rows: list[dict[str, object]] = []
    points: list[tuple[float, float]] = []
    for city in cities:
        points.append((float(city.x), float(city.y)))
        city_rows.append(
            {
                "id": city.id,
                "name": city.name,
                "player_id": city.player_id,
                "player_name": names.get(city.player_id),
                "x": city.x,
                "y": city.y,
                "garrison_army_ids": garrison.get(city.id, []),
                "trace_id": None,
                "trace_state": "NONE",
            }
        )

    army_rows: list[dict[str, object]] = []
    for army in armies:
        active = movement_by_army.get(army.id)
        if active is not None:
            leg = _leg(active, now)
            raw_position = leg["position"]
            assert isinstance(raw_position, dict)
            position = {"city_id": None, "x": raw_position["x"], "y": raw_position["y"]}
            points.append((float(position["x"]), float(position["y"])))
            placement = {
                "position": position,
                "position_state": "interpolated",
                "movement_id": active.id,
                "trace_id": active.trace_id,
                "trace_state": _trace_state(active.trace_id, movement_present=True),
            }
        elif army.status == ArmyStatus.GARRISONED and army.location_city_id is not None:
            coords = city_coords.get(army.location_city_id)
            if coords is None:
                placement = {
                    "position": None,
                    "position_state": "UNKNOWN",
                    "movement_id": None,
                    "trace_id": None,
                    "trace_state": "NONE",
                }
            else:
                position = {"city_id": army.location_city_id, **_xy(coords[0], coords[1])}
                points.append((float(position["x"]), float(position["y"])))
                placement = {
                    "position": position,
                    "position_state": "garrisoned",
                    "movement_id": None,
                    "trace_id": None,
                    "trace_state": "NONE",
                }
        else:
            placement = {
                "position": None,
                "position_state": "UNKNOWN",
                "movement_id": None,
                "trace_id": None,
                "trace_state": "NONE",
            }
        army_rows.append(
            {
                "id": army.id,
                "name": army.name,
                "player_id": army.player_id,
                "player_name": names.get(army.player_id),
                "status": army.status,
                "home_city_id": army.home_city_id,
                "location_city_id": army.location_city_id,
                "units": army.units,
                **placement,
            }
        )

    movement_rows: list[dict[str, object]] = []
    for movement in movements:
        army = army_by_id.get(movement.army_id)
        leg = _leg(movement, now)
        origin = leg["origin"]
        destination = leg["destination"]
        position = leg["position"]
        assert isinstance(origin, dict) and isinstance(destination, dict) and isinstance(position, dict)
        points.append((float(origin["x"]), float(origin["y"])))
        points.append((float(destination["x"]), float(destination["y"])))
        points.append((float(position["x"]), float(position["y"])))
        owner_id = None if army is None else army.player_id
        movement_rows.append(
            {
                "id": movement.id,
                "army_id": movement.army_id,
                "army_name": None if army is None else army.name,
                "player_id": owner_id,
                "player_name": None if owner_id is None else names.get(owner_id),
                "mission": movement.mission,
                "status": movement.status,
                "army_status": None if army is None else army.status,
                "depart_at": movement.depart_at,
                "arrive_at": movement.arrive_at,
                "trace_id": movement.trace_id,
                "trace_state": _trace_state(movement.trace_id, movement_present=True),
                "event_id": event_ids.get(movement.id),
                **leg,
            }
        )

    return {
        "server_time": now,
        "read_only": True,
        "fog_of_war": False,
        "coordinate_source": "stored",
        "neutral_entities": "NONE",
        "notes": _NOTES,
        "filter": {"player_id": player_id},
        "bounds": _bounds(points),
        "players": [{"id": player.id, "name": player.name} for player in players],
        "cities": city_rows,
        "armies": army_rows,
        "movements": movement_rows,
        "limits": {
            "cities": _limit_meta(limit=city_limit, returned=len(cities), total=city_total),
            "armies": _limit_meta(limit=army_limit, returned=len(armies), total=army_total),
            "movements": _limit_meta(limit=movement_limit, returned=len(movements), total=movement_total),
            "players": _limit_meta(limit=PLAYER_DIRECTORY_LIMIT, returned=len(players), total=player_total),
        },
    }


@router.get("/world-map")
def admin_world_map(
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
    player_id: int | None = None,
    city_limit: int = Query(default=DEFAULT_CITY_LIMIT, ge=1, le=MAX_CITY_LIMIT),
    army_limit: int = Query(default=DEFAULT_ARMY_LIMIT, ge=1, le=MAX_ARMY_LIMIT),
    movement_limit: int = Query(default=DEFAULT_MOVEMENT_LIMIT, ge=1, le=MAX_MOVEMENT_LIMIT),
) -> dict[str, object]:
    """God-view world map at the current game time. Read-only.

    Cities use stored ``x`` and ``y``. An in-progress movement is placed with
    ``travel_progress`` and ``interpolate`` from its stored origin, destination,
    ``depart_at``, and ``arrive_at``. A garrisoned army sits on its
    ``location_city_id``. Any other army has ``position: null`` and
    ``position_state: "UNKNOWN"``. Nothing is synthesized.

    ``player_id`` limits cities, armies, and movements to that owner. Omit it
    to see every player. There is no fog of war and no neutral/NPC table
    (``neutral_entities`` is ``NONE``).

    Bounds cover the rows returned after limits, not rows omitted as truncated.
    Army dots still use each returned army's own in-progress leg even when the
    movement list itself is truncated. See docs/WORLD_MAP.md.
    """

    return world_map(
        session,
        now=clock.now(),
        player_id=player_id,
        city_limit=city_limit,
        army_limit=army_limit,
        movement_limit=movement_limit,
    )
