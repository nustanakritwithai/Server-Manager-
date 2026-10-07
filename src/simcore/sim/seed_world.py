"""Initial world for CI bots.

This writes players, cities, and armies once, before any command. There is
no public endpoint that creates a player. After this returns, bots act only
through HTTP. Staging mode does not call this.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from simcore.constants import ArmyStatus
from simcore.models import Army, City, Player

PROFILES = ("aggressive", "defensive", "random")

# One city of each of these, per player. Two cities lets a defensive bot
# reinforce with MOVE, which is the public garrison/reinforce action.
_HOME_RESOURCES = {"wood": 2000, "food": 2000, "iron": 800, "gold": 400}
_HOME_RATES = {"wood": 80, "food": 80, "iron": 40, "gold": 20}

_UNITS = {
    "aggressive": [{"type": "cavalry", "count": 24}],
    "defensive": [{"type": "militia", "count": 40}, {"type": "archer", "count": 12}],
    "random": [{"type": "infantry", "count": 30}],
}

# Homes are 6 tiles apart so a cavalry march is exactly 1800 game seconds.
_PLAYER_SPACING = 6
_SECOND_CITY_DY = 2


def profile_for(index: int) -> str:
    """Profile of player ``index`` (0-based). Independent of the RNG seed."""

    return PROFILES[index % len(PROFILES)]


def opening_resources(player_count: int) -> dict[str, int]:
    """Sum of each resource placed on the map. Both cities of a player start equal."""

    cities = player_count * 2
    return {name: amount * cities for name, amount in _HOME_RESOURCES.items()}


def opening_units(player_count: int) -> dict[str, int]:
    totals: dict[str, int] = {}
    for index in range(player_count):
        for stack in _UNITS[profile_for(index)]:
            totals[stack["type"]] = totals.get(stack["type"], 0) + int(stack["count"])
    return totals


def expected_army_count(player_count: int) -> int:
    return player_count


def seed_bots(session: Session, now: datetime, player_count: int) -> dict[str, object]:
    """Insert the bot world. Refuses when any player already exists.

    Does not delete or truncate. A second call on a used database raises.
    """

    if player_count < 1:
        raise ValueError("player_count must be at least 1")
    existing = session.scalar(select(func.count()).select_from(Player))
    if existing:
        raise RuntimeError(
            "database already has players. CI mode needs a fresh database after migrations. "
            "It will not delete them."
        )

    for index in range(player_count):
        profile = profile_for(index)
        number = index + 1
        name = f"Bot{number:02d}"
        player = Player(name=name, research={}, created_at=now)
        session.add(player)
        session.flush()

        home_x = index * _PLAYER_SPACING
        home = _city(player.id, f"{name} Home", home_x, 0, now)
        camp = _city(player.id, f"{name} Camp", home_x, _SECOND_CITY_DY, now)
        session.add_all([home, camp])
        session.flush()
        session.add(
            Army(
                player_id=player.id,
                name=f"{name} Army",
                home_city_id=home.id,
                location_city_id=home.id,
                status=ArmyStatus.GARRISONED,
                units=list(_UNITS[profile]),
                created_at=now,
            )
        )
        session.flush()

    return {
        "players": player_count,
        "profiles": [profile_for(index) for index in range(player_count)],
        "resources": opening_resources(player_count),
        "units": opening_units(player_count),
        "armies": expected_army_count(player_count),
    }


def _city(player_id: int, name: str, x: int, y: int, now: datetime) -> City:
    return City(
        player_id=player_id,
        name=name,
        x=x,
        y=y,
        wood=_HOME_RESOURCES["wood"],
        food=_HOME_RESOURCES["food"],
        iron=_HOME_RESOURCES["iron"],
        gold=_HOME_RESOURCES["gold"],
        wood_rate=_HOME_RATES["wood"],
        food_rate=_HOME_RATES["food"],
        iron_rate=_HOME_RATES["iron"],
        gold_rate=_HOME_RATES["gold"],
        buildings={},
        last_updated=now,
        created_at=now,
    )
