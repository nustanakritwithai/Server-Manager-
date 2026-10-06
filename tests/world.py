"""Small worlds for integration tests. Not the demo seed."""

from __future__ import annotations

from datetime import datetime

from simcore.constants import ArmyStatus
from simcore.db import get_sessionmaker
from simcore.models import Army, City, Player


def create_scenario(
    now: datetime,
    *,
    alice_units: list[dict[str, int | str]] | None = None,
    bob_units: list[dict[str, int | str]] | None = None,
    rate: int = 360,
    stock: int = 1000,
) -> dict[str, int]:
    """Two players, one city and one army each, on a 30-40-50 triangle."""

    alice_units = alice_units or [{"type": "infantry", "count": 80}]
    bob_units = bob_units or [{"type": "militia", "count": 10}]
    session = get_sessionmaker()()
    try:
        alice = Player(name="Alice", research={}, created_at=now)
        bob = Player(name="Bob", research={}, created_at=now)
        session.add_all([alice, bob])
        session.flush()
        oak = City(
            player_id=alice.id,
            name="Oakhold",
            x=0,
            y=0,
            wood=stock,
            food=stock,
            iron=stock,
            gold=stock,
            wood_rate=rate,
            food_rate=rate,
            iron_rate=rate,
            gold_rate=rate,
            buildings={},
            last_updated=now,
            created_at=now,
        )
        iron = City(
            player_id=bob.id,
            name="Ironford",
            x=30,
            y=40,
            wood=stock,
            food=stock,
            iron=stock,
            gold=stock,
            wood_rate=rate,
            food_rate=rate,
            iron_rate=rate,
            gold_rate=rate,
            buildings={},
            last_updated=now,
            created_at=now,
        )
        session.add_all([oak, iron])
        session.flush()
        alice_army = Army(
            player_id=alice.id,
            name="Oak Company",
            home_city_id=oak.id,
            location_city_id=oak.id,
            status=ArmyStatus.GARRISONED,
            units=alice_units,
            created_at=now,
        )
        bob_army = Army(
            player_id=bob.id,
            name="Iron Watch",
            home_city_id=iron.id,
            location_city_id=iron.id,
            status=ArmyStatus.GARRISONED,
            units=bob_units,
            created_at=now,
        )
        session.add_all([alice_army, bob_army])
        session.commit()
        return {
            "alice_id": alice.id,
            "bob_id": bob.id,
            "alice_city": oak.id,
            "bob_city": iron.id,
            "alice_army": alice_army.id,
            "bob_army": bob_army.id,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
