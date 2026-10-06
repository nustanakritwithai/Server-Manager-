"""Row locks in a fixed order: cities by id, then armies by id.

Event and movement rows are locked by the caller before these helpers.
Keeping that order avoids deadlocks between a recall and an arrival.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from simcore.errors import GameError
from simcore.models import Army, City


def lock_cities(session: Session, *city_ids: int | None) -> dict[int, City]:
    locked: dict[int, City] = {}
    for city_id in sorted({city_id for city_id in city_ids if city_id is not None}):
        city = session.get(City, city_id, with_for_update=True)
        if city is None:
            raise GameError("city not found", status_code=404, code="not_found")
        locked[city_id] = city
    return locked


def lock_armies(session: Session, *army_ids: int) -> dict[int, Army]:
    locked: dict[int, Army] = {}
    for army_id in sorted(set(army_ids)):
        army = session.get(Army, army_id, with_for_update=True)
        if army is None:
            raise GameError("army not found", status_code=404, code="not_found")
        locked[army_id] = army
    return locked
