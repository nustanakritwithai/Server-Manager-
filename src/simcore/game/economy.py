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


def garrison_snapshot(session: Session, city_id: int) -> tuple[int, str]:
    """Hourly food upkeep and the garrison composition that produced it.

    Composition is ``type*count`` joined with ``+``, unit names sorted. It is
    stored on the upkeep ledger key because army rows change after the accrue
    and there is no garrison history table.
    """

    armies = session.scalars(
        select(Army)
        .where(Army.location_city_id == city_id, Army.status == ArmyStatus.GARRISONED)
        .order_by(Army.id)
    ).all()
    totals: dict[str, int] = {}
    for army in armies:
        for stack in army.units or []:
            unit_type = str(stack["type"])
            count = int(stack["count"])
            if count > 0 and unit_type in UNIT_CATALOG:
                totals[unit_type] = totals.get(unit_type, 0) + count
    hourly = sum(UNIT_CATALOG[name].upkeep * count for name, count in totals.items())
    composition = "+".join(f"{name}*{totals[name]}" for name in sorted(totals))
    return hourly, composition


def city_food_upkeep_per_hour(session: Session, city_id: int) -> int:
    hourly, _composition = garrison_snapshot(session, city_id)
    return hourly


def stock_after_accrual(session: Session, city: City, now: datetime) -> dict[str, int]:
    """Balances accrue_city would leave, without writing.

    Callers use this to reject a spend before any ledger row is inserted.
    """

    if city.last_updated > now:
        elapsed = 0
    else:
        elapsed = int((now - city.last_updated).total_seconds())
    upkeep = city_food_upkeep_per_hour(session, city.id) if elapsed > 0 else 0
    stocks: dict[str, int] = {}
    for resource in RESOURCES:
        produced = produced_amount(int(getattr(city, f"{resource}_rate")), elapsed)
        amount = int(getattr(city, resource)) + produced
        if resource == "food" and upkeep:
            amount -= produced_amount(upkeep, elapsed)
        stocks[resource] = max(0, amount)
    return stocks


def accrue_city(
    session: Session,
    city: City,
    now: datetime,
    *,
    source_event_id: int | None = None,
    trace_id: str | None = None,
) -> None:
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
    upkeep, composition = garrison_snapshot(session, city.id)
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
                idempotency_key=f"accrue:{city.id}:{resource}:{stamp}:production:{rates[resource]}",
                source_event_id=source_event_id,
                now=now,
                trace_id=trace_id,
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
                    idempotency_key=f"accrue:{city.id}:food:{stamp}:upkeep:{upkeep}:{composition}",
                    source_event_id=source_event_id,
                    now=now,
                    trace_id=trace_id,
                )
    city.last_updated = now
    session.flush()
    bump_world_version(session)
