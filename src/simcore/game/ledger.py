"""Resource ledger. Every non-zero change is one transactions row.

The unique idempotency key is the second line of defense behind the single
database transaction that processes an event. A retried event sees the row and
does not apply the delta again.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from simcore.constants import RESOURCES
from simcore.models import City, Transaction


def apply_resource_delta(
    session: Session,
    *,
    city: City,
    resource: str,
    delta: int,
    reason: str,
    idempotency_key: str,
    source_event_id: int | None,
    now: datetime,
) -> Transaction | None:
    if resource not in RESOURCES:
        raise ValueError(f"unknown resource {resource}")
    if delta == 0:
        return None

    existing = session.scalar(select(Transaction).where(Transaction.idempotency_key == idempotency_key))
    if existing is not None:
        return existing

    nested = session.begin_nested()
    try:
        current = getattr(city, resource)
        balance = max(0, current + delta)
        actual = balance - current
        setattr(city, resource, balance)
        txn = Transaction(
            player_id=city.player_id,
            city_id=city.id,
            resource=resource,
            delta=actual,
            balance_after=balance,
            reason=reason,
            source_event_id=source_event_id,
            idempotency_key=idempotency_key,
            created_at=now,
        )
        session.add(txn)
        session.flush()
        nested.commit()
        return txn
    except IntegrityError:
        nested.rollback()
        session.refresh(city)
        return session.scalar(select(Transaction).where(Transaction.idempotency_key == idempotency_key))
