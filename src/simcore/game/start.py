"""Opening city, resources, and army for one player.

Registration and ``POST /v1/auth/claim-start`` both call ``grant_start``.
The tile is chosen here. A client coordinate is never accepted.

Placement is a row-major scan of the configured window, so the same occupied
set always yields the same tile. Chebyshev distance (king-move) from every
existing city must be at least ``start_min_distance``. A city tile is occupied.
A garrisoned army stands on its city's tile, so it is covered by that set.
There is no separate tile table. Marching armies are between tiles and do not
reserve a square.

Two grants take ``pg_advisory_xact_lock`` so they cannot pick the same tile.
A claim also locks the player row, so two claims for one account cannot both
pass the "no city yet" check. Resources are inserted at zero and then written
through the ledger. A failure before commit leaves no player, no city, and no
ledger row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from simcore.audit import append_audit
from simcore.constants import RESOURCES, ArmyStatus, Reason
from simcore.errors import GameError
from simcore.game.catalog import UNIT_CATALOG
from simcore.game.ledger import apply_resource_delta
from simcore.game.tracing import record_command
from simcore.models import Army, City, Player
from simcore.world import bump_world_version, require_commands_open

# Distinct from the worker drain lock and the audit chain lock.
SPAWN_LOCK_KEY = 7424244
_NAME_LIMIT = 40
_PLACE_ATTEMPTS = 5


@dataclass(frozen=True)
class StartGrant:
    created: bool
    city: City | None
    army: Army | None
    trace_id: str | None


def parse_start_units(value: str) -> list[dict[str, int | str]]:
    """Parse ``militia:1,archer:2``. Raises ValueError on a bad catalog entry."""

    text_value = (value or "").strip()
    if not text_value:
        raise ValueError("SIMCORE_START_UNITS must list at least one unit, such as militia:1")
    stacks: list[dict[str, int | str]] = []
    seen: set[str] = set()
    for part in text_value.split(","):
        item = part.strip()
        if ":" not in item:
            raise ValueError(
                "SIMCORE_START_UNITS entries must look like militia:1 "
                f"(got {item!r})"
            )
        unit, count_text = item.split(":", 1)
        unit = unit.strip()
        count_text = count_text.strip()
        if unit not in UNIT_CATALOG:
            raise ValueError(f"SIMCORE_START_UNITS unknown unit {unit}")
        if unit in seen:
            raise ValueError(f"SIMCORE_START_UNITS repeats {unit}")
        if not count_text.isdigit():
            raise ValueError(f"SIMCORE_START_UNITS count for {unit} must be a positive integer")
        count = int(count_text)
        if count < 1 or count > 100_000:
            raise ValueError(f"SIMCORE_START_UNITS count for {unit} must be from 1 to 100000")
        seen.add(unit)
        stacks.append({"type": unit, "count": count})
    return stacks


def render_label(template: str, player_name: str) -> str:
    rendered = template.replace("{name}", player_name).strip()
    if len(rendered) > _NAME_LIMIT:
        rendered = rendered[:_NAME_LIMIT].rstrip()
    if not rendered:
        rendered = (player_name or "Home")[:_NAME_LIMIT]
    return rendered


def choose_spawn(
    occupied: set[tuple[int, int]],
    *,
    map_min: int,
    map_max: int,
    min_distance: int,
) -> tuple[int, int] | None:
    """First free tile in row-major order, or None when the window is full.

    ``min_distance`` is Chebyshev. A tile closer than that to any occupied tile
    is skipped. The same inputs always return the same coordinate.
    """

    if min_distance < 1 or map_min > map_max:
        return None
    for y in range(map_min, map_max + 1):
        for x in range(map_min, map_max + 1):
            if _far_enough(x, y, occupied, min_distance):
                return (x, y)
    return None


def _far_enough(x: int, y: int, occupied: set[tuple[int, int]], min_distance: int) -> bool:
    for ox, oy in occupied:
        if max(abs(x - ox), abs(y - oy)) < min_distance:
            return False
    return True


def home_city(session: Session, player_id: int) -> City | None:
    return session.scalar(select(City).where(City.player_id == player_id).order_by(City.id).limit(1))


def home_army(session: Session, player_id: int, city_id: int | None) -> Army | None:
    if city_id is not None:
        matched = session.scalar(
            select(Army)
            .where(Army.player_id == player_id, Army.home_city_id == city_id)
            .order_by(Army.id)
            .limit(1)
        )
        if matched is not None:
            return matched
    return session.scalar(select(Army).where(Army.player_id == player_id).order_by(Army.id).limit(1))


def start_public(session: Session, player: Player) -> dict[str, object]:
    """What the client needs after register, login, or ``GET /v1/auth/me``."""

    city = home_city(session, player.id)
    if city is None:
        return {"start_granted": False, "home_city": None, "army_id": None}
    army = home_army(session, player.id, city.id)
    return {
        "start_granted": True,
        "home_city": {"id": city.id, "name": city.name, "x": city.x, "y": city.y},
        "army_id": None if army is None else army.id,
    }


def grant_start(
    session: Session,
    player: Player,
    now: datetime,
    settings: object,
    *,
    actor: str,
    source_ip: str,
) -> StartGrant:
    """Give ``player`` a home if they do not have one.

    Caller and this function share one transaction. The player row is locked
    first. A second caller waits, then sees the city and does not create another.
    """

    locked = session.get(Player, player.id, with_for_update=True)
    if locked is None:
        raise GameError("player not found", status_code=404, code="not_found")
    existing = home_city(session, locked.id)
    if existing is not None:
        army = home_army(session, locked.id, existing.id)
        return StartGrant(created=False, city=existing, army=army, trace_id=None)
    require_commands_open(session)
    return _insert_start(session, locked, now, settings, actor=actor, source_ip=source_ip)


def _insert_start(
    session: Session,
    player: Player,
    now: datetime,
    settings: object,
    *,
    actor: str,
    source_ip: str,
) -> StartGrant:
    """Place one city. The caller holds the player row lock. This takes the spawn lock."""

    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SPAWN_LOCK_KEY})
    stacks = parse_start_units(getattr(settings, "start_units"))
    amounts = {
        "wood": int(getattr(settings, "start_wood")),
        "food": int(getattr(settings, "start_food")),
        "iron": int(getattr(settings, "start_iron")),
        "gold": int(getattr(settings, "start_gold")),
    }
    rates = {
        "wood": int(getattr(settings, "start_wood_rate")),
        "food": int(getattr(settings, "start_food_rate")),
        "iron": int(getattr(settings, "start_iron_rate")),
        "gold": int(getattr(settings, "start_gold_rate")),
    }
    map_min = int(getattr(settings, "start_map_min"))
    map_max = int(getattr(settings, "start_map_max"))
    min_distance = int(getattr(settings, "start_min_distance"))
    city_name = render_label(str(getattr(settings, "start_city_name")), player.name)
    army_name = render_label(str(getattr(settings, "start_army_name")), player.name)

    city: City | None = None
    for _attempt in range(_PLACE_ATTEMPTS):
        occupied = _occupied_tiles(session)
        tile = choose_spawn(occupied, map_min=map_min, map_max=map_max, min_distance=min_distance)
        if tile is None:
            raise GameError(
                "the world has no free tile that respects the spawn distance",
                status_code=409,
                code="world_full",
            )
        x, y = tile
        try:
            with session.begin_nested():
                city = City(
                    player_id=player.id,
                    name=city_name,
                    x=x,
                    y=y,
                    wood=0,
                    food=0,
                    iron=0,
                    gold=0,
                    wood_rate=rates["wood"],
                    food_rate=rates["food"],
                    iron_rate=rates["iron"],
                    gold_rate=rates["gold"],
                    buildings={},
                    last_updated=now,
                    created_at=now,
                )
                session.add(city)
                session.flush()
            break
        except IntegrityError:
            city = None
    if city is None:
        raise GameError(
            "a starting tile could not be reserved",
            status_code=409,
            code="conflict",
        )

    units = [{"type": str(stack["type"]), "count": int(stack["count"])} for stack in stacks]
    army = Army(
        player_id=player.id,
        name=army_name,
        home_city_id=city.id,
        location_city_id=city.id,
        status=ArmyStatus.GARRISONED,
        units=units,
        created_at=now,
    )
    session.add(army)
    session.flush()
    trace_id = record_command(
        session,
        player_id=player.id,
        command_type="start",
        army_id=army.id,
        target={
            "city_id": city.id,
            "army_id": army.id,
            "x": city.x,
            "y": city.y,
            "name": city.name,
            "resources": amounts,
            "units": units,
        },
        now=now,
    )
    for resource in RESOURCES:
        amount = amounts[resource]
        if amount <= 0:
            continue
        apply_resource_delta(
            session,
            city=city,
            resource=resource,
            delta=amount,
            reason=Reason.START,
            idempotency_key=f"start:{player.id}:{resource}",
            source_event_id=None,
            now=now,
            trace_id=trace_id,
        )
    bump_world_version(session)
    append_audit(
        session,
        actor=actor,
        action="player.start",
        target=f"player:{player.id}",
        source_ip=source_ip,
        result="success",
        reason=f"city:{city.id} army:{army.id} tile:{city.x},{city.y}",
    )
    return StartGrant(created=True, city=city, army=army, trace_id=trace_id)


def _occupied_tiles(session: Session) -> set[tuple[int, int]]:
    rows = session.execute(select(City.x, City.y)).all()
    return {(int(row.x), int(row.y)) for row in rows}
