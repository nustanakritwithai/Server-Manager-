"""Opening totals the simulator checks against, and a legacy direct insert.

CI and the load tool no longer call ``seed_holdings``. Registration grants the
home, the army, and the resources through the API. ``opening_resources`` is
zero because those stocks are ledger rows. ``opening_units`` is the configured
starting army times the player count. ``seed_holdings`` remains for a test that
still wants a hand-built world; it writes balances without the ledger, so a
ledger check on that world does not describe a registered player.
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

# Full-mode roster. Two 20-cavalry armies, and two single militia so a 1v1
# fight draws, a militia versus cavalry loses, and an empty city is a win.
_COVERAGE_UNITS = {
    "aggressive": [{"type": "cavalry", "count": 20}],
    "defensive": [{"type": "militia", "count": 1}],
    "random": [{"type": "militia", "count": 1}],
}

_ROSTERS = {"default": _UNITS, "coverage": _COVERAGE_UNITS}

# Homes are 6 tiles apart so a cavalry march is exactly 1800 game seconds.
_PLAYER_SPACING = 6
_SECOND_CITY_DY = 2


def profile_for(index: int) -> str:
    """Profile of player ``index`` (0-based). Independent of the RNG seed."""

    return PROFILES[index % len(PROFILES)]


def opening_resources(player_count: int) -> dict[str, int]:
    """Non-ledger baseline. Starting stocks are ledger rows, so this is zero.

    ``player_count`` is accepted so callers stay stable. It does not add a
    hidden stock. A negative count is rejected.
    """

    if player_count < 0:
        raise ValueError("player_count must be zero or greater")
    return {"wood": 0, "food": 0, "iron": 0, "gold": 0}


def opening_units(player_count: int, roster: str = "default") -> dict[str, int]:
    """Units the start grant places on each player. ``roster`` no longer changes them.

    The grant is one configured army per player. Training and casualties are
    applied by the verifier on top of this total.
    """

    if player_count < 0:
        raise ValueError("player_count must be zero or greater")
    if roster not in _ROSTERS:
        raise ValueError(f"unknown roster {roster}")
    from simcore.config import get_settings
    from simcore.game.start import parse_start_units

    totals: dict[str, int] = {}
    for stack in parse_start_units(get_settings().start_units):
        totals[str(stack["type"])] = totals.get(str(stack["type"]), 0) + int(stack["count"]) * player_count
    return totals


def expected_army_count(player_count: int) -> int:
    return player_count


def _roster_unit_totals(player_count: int, roster: str) -> dict[str, int]:
    units = _ROSTERS[roster]
    totals: dict[str, int] = {}
    for index in range(player_count):
        for stack in units[profile_for(index)]:
            totals[str(stack["type"])] = totals.get(str(stack["type"]), 0) + int(stack["count"])
    return totals


def seed_bots(session: Session, now: datetime, player_count: int, roster: str = "default") -> dict[str, object]:
    """Insert the bot world. Refuses when any player already exists.

    Does not delete or truncate. A second call on a used database raises.
    """

    if player_count < 1:
        raise ValueError("player_count must be at least 1")
    if roster not in _ROSTERS:
        raise ValueError(f"unknown roster {roster}")
    units = _ROSTERS[roster]
    existing = session.scalar(select(func.count()).select_from(Player))
    if existing:
        raise RuntimeError(
            "database already has players. CI mode needs a fresh database after migrations. "
            "It will not delete them."
        )

    for index in range(player_count):
        name = f"Bot{index + 1:02d}"
        player = Player(name=name, research={}, created_at=now)
        session.add(player)
        session.flush()
        _add_holdings(session, player, index, now, units)

    return {
        "players": player_count,
        "profiles": [profile_for(index) for index in range(player_count)],
        "resources": {name: amount * player_count * 2 for name, amount in _HOME_RESOURCES.items()},
        "units": _roster_unit_totals(player_count, roster),
        "roster": roster,
        "armies": expected_army_count(player_count),
    }


def seed_holdings(
    session: Session,
    now: datetime,
    names: list[str],
    roster: str = "default",
) -> dict[str, object]:
    """Attach a home, a camp, and one army to players that already exist.

    Registration creates the player rows. This does not create players and does
    not delete anything. It refuses when the database already has a city.
    ``roster`` selects the opening stacks. Full mode uses ``coverage``.
    """

    if not names:
        raise ValueError("names must not be empty")
    if roster not in _ROSTERS:
        raise ValueError(f"unknown roster {roster}")
    units = _ROSTERS[roster]
    existing = session.scalar(select(func.count()).select_from(City))
    if existing:
        raise RuntimeError(
            "database already has cities. CI mode needs a fresh database after migrations. "
            "It will not delete them."
        )
    for index, name in enumerate(names):
        player = session.scalar(select(Player).where(Player.name == name))
        if player is None:
            raise RuntimeError(f"player {name} is not registered")
        _add_holdings(session, player, index, now, units)
    count = len(names)
    return {
        "players": count,
        "profiles": [profile_for(index) for index in range(count)],
        "resources": {name: amount * count * 2 for name, amount in _HOME_RESOURCES.items()},
        "units": _roster_unit_totals(count, roster),
        "roster": roster,
        "armies": expected_army_count(count),
    }


def _add_holdings(
    session: Session,
    player: Player,
    index: int,
    now: datetime,
    units: dict[str, list[dict[str, object]]],
) -> None:
    profile = profile_for(index)
    name = player.name
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
            units=list(units[profile]),
            created_at=now,
        )
    )
    session.flush()


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
