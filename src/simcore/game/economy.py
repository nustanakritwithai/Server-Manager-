"""Lazy resource production and garrison upkeep."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from simcore.constants import RESOURCES, ArmyStatus, Reason
from simcore.game.catalog import UNIT_CATALOG
from simcore.game.ledger import apply_resource_delta
from simcore.models import Army, City
from simcore.world import bump_world_version


def produced_amount(rate_per_hour: int, elapsed_seconds: int) -> int:
    """Whole resources produced over an interval. The fractional remainder is dropped."""

    if rate_per_hour <= 0 or elapsed_seconds <= 0:
        return 0
    return rate_per_hour * elapsed_seconds // 3600


def army_upkeep_per_hour(units: list[dict[str, int | str]]) -> int:
    total = 0
    for stack in units:
        unit = UNIT_CATALOG[str(stack["type"])]
        total += unit.upkeep * int(stack["count"])
    return total


def city_food_upkeep_per_hour(session: Session, city_id: int) -> int:
    armies = session.scalars(
        select(Army).where(Army.location_city_id == city_id, Army.status == ArmyStatus.GARRISONED)
    ).all()
    return sum(army_upkeep_per_hour(army.units) for army in armies)


def accrue_city(session: Session, city: City, now: datetime, *, source_event_id: int | None = None) -> None:
    """Apply production and food upkeep since city.last_updated, then move the marker forward.

    Call this before changing the garrison so the elapsed window uses the garrison
    that was actually standing there.
    """

    if city.last_updated > now:
        return
    elapsed = int((now - city.last_updated).total_seconds())
    if elapsed <= 0:
        return

    stamp = city.last_updated.isoformat()
    upkeep = city_food_upkeep_per_hour(session, city.id)
    rates = {
        "wood": city.wood_rate,
        "food": city.food_rate,
        "iron": city.iron_rate,
        "gold": city.gold_rate,
    }
    for resource in RESOURCES:
        produced = produced_amount(rates[resource], elapsed)
        if produced:
            apply_resource_delta(
                session,
                city=city,
                resource=resource,
                delta=produced,
                reason=Reason.PRODUCTION,
                idempotency_key=f"accrue:{city.id}:{resource}:{stamp}:production",
                source_event_id=source_event_id,
                now=now,
            )
        if resource == "food" and upkeep:
            consumed = produced_amount(upkeep, elapsed)
            if consumed:
                apply_resource_delta(
                    session,
                    city=city,
                    resource="food",
                    delta=-consumed,
                    reason=Reason.UPKEEP,
                    idempotency_key=f"accrue:{city.id}:food:{stamp}:upkeep",
                    source_event_id=source_event_id,
                    now=now,
                )
    city.last_updated = now
    session.flush()
    bump_world_version(session)
