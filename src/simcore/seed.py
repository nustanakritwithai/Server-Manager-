"""Demo world: two players, one city and one army each.

Idempotent. A second run leaves the existing world alone.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from simcore.clock import OffsetClock
from simcore.constants import ArmyStatus
from simcore.db import get_sessionmaker
from simcore.models import Army, City, Player

logger = logging.getLogger("simcore.seed")


def seed_world(session: Session, now: datetime) -> bool:
    if session.scalar(select(Player).limit(1)) is not None:
        return False

    alice = Player(name="Alice", research={}, created_at=now)
    bob = Player(name="Bob", research={}, created_at=now)
    session.add_all([alice, bob])
    session.flush()

    oakhold = City(
        player_id=alice.id,
        name="Oakhold",
        x=10,
        y=10,
        wood=1200,
        food=1200,
        iron=600,
        gold=200,
        wood_rate=120,
        food_rate=120,
        iron_rate=60,
        gold_rate=30,
        buildings={},
        last_updated=now,
        created_at=now,
    )
    ironford = City(
        player_id=bob.id,
        name="Ironford",
        x=40,
        y=50,
        wood=1500,
        food=1000,
        iron=800,
        gold=400,
        wood_rate=100,
        food_rate=100,
        iron_rate=80,
        gold_rate=40,
        buildings={},
        last_updated=now,
        created_at=now,
    )
    session.add_all([oakhold, ironford])
    session.flush()

    session.add_all(
        [
            Army(
                player_id=alice.id,
                name="Oak Company",
                home_city_id=oakhold.id,
                location_city_id=oakhold.id,
                status=ArmyStatus.GARRISONED,
                units=[{"type": "infantry", "count": 40}, {"type": "cavalry", "count": 10}],
                created_at=now,
            ),
            Army(
                player_id=bob.id,
                name="Iron Watch",
                home_city_id=ironford.id,
                location_city_id=ironford.id,
                status=ArmyStatus.GARRISONED,
                units=[{"type": "militia", "count": 30}, {"type": "archer", "count": 15}],
                created_at=now,
            ),
        ]
    )
    session.flush()
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    session = get_sessionmaker()()
    try:
        now = OffsetClock(session).now()
        created = seed_world(session, now)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    if created:
        print("Seeded Alice (Oakhold) and Bob (Ironford).")
        print("Dev login: POST /v1/auth/dev-login {\"name\": \"Alice\"}  — placeholder token, not real auth.")
    else:
        print("World already has players; seed skipped.")


if __name__ == "__main__":
    main()
