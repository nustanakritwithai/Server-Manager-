"""A crashed worker must not grant loot twice, and a replay must not either."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from simcore.constants import EventStatus
from simcore.db import get_sessionmaker
from simcore.game.commands import attack_city
from simcore.game.ledger import apply_resource_delta
from simcore.game.processor import process_event
from simcore.models import Army, City, Event, Player, Transaction
from simcore.worker import run_once
from tests.world import create_scenario


def test_ledger_key_applies_once(db, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=50)
    session = get_sessionmaker()()
    try:
        city = session.get(City, ids["alice_city"])
        assert city is not None
        first = apply_resource_delta(
            session,
            city=city,
            resource="gold",
            delta=25,
            reason="loot_gained",
            idempotency_key="test:gold",
            source_event_id=None,
            now=frozen.now(),
        )
        second = apply_resource_delta(
            session,
            city=city,
            resource="gold",
            delta=25,
            reason="loot_gained",
            idempotency_key="test:gold",
            source_event_id=None,
            now=frozen.now(),
        )
        session.commit()
        assert first is not None and second is not None
        assert first.id == second.id
        assert city.gold == 75
        assert session.scalar(select(func.count()).select_from(Transaction)) == 1
    finally:
        session.close()


def test_crash_then_retry_and_forced_replay_grant_loot_once(db, frozen, monkeypatch) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=1000)
    session = get_sessionmaker()()
    try:
        alice = session.get(Player, ids["alice_id"])
        assert alice is not None
        _movement, event = attack_city(session, alice, ids["alice_army"], ids["bob_city"], frozen.now())
        event_id = event.id
        session.commit()
    finally:
        session.close()

    frozen.advance(seconds=30_000)

    def _crash(*_args, **_kwargs):
        raise RuntimeError("worker crashed before commit")

    monkeypatch.setattr("simcore.worker.process_event", _crash)
    status, failed_id = run_once(frozen)
    assert status == "failed"
    assert failed_id == event_id

    session = get_sessionmaker()()
    try:
        bob = session.get(City, ids["bob_city"])
        army = session.get(Army, ids["bob_army"])
        event = session.get(Event, event_id)
        assert bob is not None and army is not None and event is not None
        assert bob.gold == 1000
        assert army.status == "garrisoned"
        assert army.units == [{"type": "militia", "count": 10}]
        assert event.status == EventStatus.PENDING
        assert event.attempts == 1
        assert event.last_error is not None and "crashed" in event.last_error
        wait = (event.due_at - frozen.now()).total_seconds()
    finally:
        session.close()

    if wait > 0:
        frozen.advance(seconds=int(wait) + 1)

    monkeypatch.setattr("simcore.worker.process_event", process_event)
    status, processed_id = run_once(frozen)
    assert status == "processed"
    assert processed_id == event_id

    def _gold() -> int:
        check = get_sessionmaker()()
        try:
            city = check.get(City, ids["bob_city"])
            assert city is not None
            return city.gold
        finally:
            check.close()

    def _loot_rows() -> int:
        check = get_sessionmaker()()
        try:
            return int(
                check.scalar(
                    select(func.count())
                    .select_from(Transaction)
                    .where(Transaction.reason == "loot_lost", Transaction.source_event_id == event_id)
                )
                or 0
            )
        finally:
            check.close()

    gold_once = _gold()
    rows_once = _loot_rows()
    # 30% of the untouched 1000 gold stock. Production is zero in this scenario,
    # so a second grant would move the stock again.
    assert gold_once == 700
    assert rows_once == 4

    # The return leg is not due yet. A second tick must not touch the battle.
    status, _ = run_once(frozen)
    assert status == "empty"
    assert _gold() == gold_once
    assert _loot_rows() == rows_once

    # Pretend the completion flag was lost and the worker sees the event again.
    session = get_sessionmaker()()
    try:
        event = session.get(Event, event_id)
        assert event is not None
        event.status = EventStatus.PENDING
        event.due_at = frozen.now() - timedelta(seconds=5)
        session.commit()
    finally:
        session.close()

    status, replayed = run_once(frozen)
    assert status == "processed"
    assert replayed == event_id
    assert _gold() == gold_once
    assert _loot_rows() == rows_once

    session = get_sessionmaker()()
    try:
        army = session.get(Army, ids["bob_army"])
        assert army is not None
        assert army.status == "destroyed"
        assert army.units == []
    finally:
        session.close()


def test_poison_event_stops_after_max_attempts(db, frozen, monkeypatch) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=10)
    session = get_sessionmaker()()
    try:
        event = Event(
            due_at=frozen.now(),
            type="BUILD_COMPLETE",
            payload={"city_id": ids["alice_city"], "player_id": ids["alice_id"], "building": "farm"},
            status=EventStatus.PENDING,
            attempts=0,
            idempotency_key="poison-build",
            created_at=frozen.now(),
        )
        session.add(event)
        session.commit()
        event_id = event.id
    finally:
        session.close()

    monkeypatch.setattr("simcore.worker.process_event", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("bad payload")))

    for _ in range(5):
        status, seen = run_once(frozen)
        assert status == "failed"
        assert seen == event_id
        session = get_sessionmaker()()
        try:
            event = session.get(Event, event_id)
            assert event is not None
            due = event.due_at
            state = event.status
        finally:
            session.close()
        if state == EventStatus.PENDING:
            wait = (due - frozen.now()).total_seconds()
            if wait > 0:
                frozen.advance(seconds=int(wait) + 1)

    session = get_sessionmaker()()
    try:
        event = session.get(Event, event_id)
        city = session.get(City, ids["alice_city"])
        assert event is not None and city is not None
        assert event.status == EventStatus.FAILED
        assert event.attempts == 5
        assert city.buildings == {}
    finally:
        session.close()

    status, _ = run_once(frozen)
    assert status == "empty"
