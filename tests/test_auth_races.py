"""Concurrent auth and idempotency races.

These tests force two requests to overlap. A passing run is one effect for one
idempotency key, and one refresh rotation that revokes the family when the old
token is presented twice.
"""

from __future__ import annotations

import socket
import threading
from datetime import datetime, timedelta, timezone

import httpx
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from simcore.clock import FrozenClock, OffsetClock
from simcore.constants import EventType
from simcore.db import get_sessionmaker
from simcore.models import AuditLog, Event, PlayerRefreshSession
from tests.world import create_scenario

PASSWORD = "correct-horse-battery"
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _serve(frozen: FrozenClock) -> tuple[uvicorn.Server, threading.Thread, int]:
    from simcore.config import get_settings
    from simcore.main import create_app

    app = create_app(settings=get_settings(), base_clock=frozen)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    thread = threading.Thread(target=server.run, name="race-api", daemon=True)
    thread.start()
    deadline = threading.Event()
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0)
    try:
        for _ in range(50):
            try:
                response = client.get("/health/ready")
            except httpx.HTTPError:
                if deadline.wait(0.05):
                    break
                continue
            if response.status_code == 200:
                return server, thread, port
            if deadline.wait(0.05):
                break
    finally:
        client.close()
    raise RuntimeError("race API did not become ready")


def _stop(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


def test_concurrent_idempotency_key_applies_once(db, monkeypatch) -> None:
    """Two overlapping builds with one key must leave one event.

    The first handler waits until a second handler is also inside the command,
    or until that wait times out. The second call shifts its clock so the
    event's own idempotency key would not collide. Without a lock around the
    player key, both builds commit.
    """

    from simcore.api import routes as route_module
    from simcore.sim.seed_world import seed_holdings

    frozen = FrozenClock(_EPOCH)
    original = route_module.queue_build
    state = {"n": 0}
    state_lock = threading.Lock()
    second_inside = threading.Event()

    def wrapped(session, player, city_id, building, now):
        with state_lock:
            state["n"] += 1
            seen = state["n"]
        if seen == 1:
            second_inside.wait(timeout=1.0)
        else:
            second_inside.set()
            now = now + timedelta(seconds=5)
        return original(session, player, city_id, building, now)

    monkeypatch.setattr(route_module, "queue_build", wrapped)
    server, thread, port = _serve(frozen)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as client:
            registered = client.post(
                "/v1/auth/register",
                json={"username": "Ada", "password": PASSWORD},
            )
            assert registered.status_code == 200, registered.text
            token = registered.json()["access_token"]
            holder = get_sessionmaker()()
            try:
                now = OffsetClock(holder, frozen).now()
                seed_holdings(holder, now, ["Ada"])
                holder.commit()
            finally:
                holder.close()
            headers = {"Authorization": f"Bearer {token}"}
            cities = client.get("/v1/me/cities", headers=headers)
            assert cities.status_code == 200, cities.text
            city_id = int(cities.json()["cities"][0]["id"])
            body = {"city_id": city_id, "building": "warehouse"}
            key_headers = {**headers, "Idempotency-Key": "race-build-once"}
            barrier = threading.Barrier(8)
            results: list[tuple[int, object]] = []
            results_lock = threading.Lock()

            def once() -> None:
                barrier.wait(timeout=5)
                response = client.post("/v1/commands/build", headers=key_headers, json=body)
                try:
                    payload = response.json()
                except Exception:
                    payload = {"error": {"code": "bad_response", "message": response.text[:300]}}
                with results_lock:
                    results.append((response.status_code, payload))

            workers = [threading.Thread(target=once) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
            assert all(not worker.is_alive() for worker in workers)
        assert len(results) == 8
        statuses = [status for status, _payload in results]
        assert 500 not in statuses
        assert all(status == statuses[0] for status in statuses)
        payloads = [payload for _status, payload in results]
        assert all(payload == payloads[0] for payload in payloads)
        check = get_sessionmaker()()
        try:
            events = int(
                check.scalar(
                    select(func.count())
                    .select_from(Event)
                    .where(Event.type == EventType.BUILD_COMPLETE)
                )
                or 0
            )
        finally:
            check.close()
        assert events == 1
        assert state["n"] == 1
    finally:
        _stop(server, thread)


def test_refresh_reuse_race_revokes_the_family(db, monkeypatch) -> None:
    """Overlapping refreshes of one token must not leave two live sessions."""

    real_get = Session.get
    state = {"n": 0}
    state_lock = threading.Lock()
    release = threading.Barrier(2)

    def patched_get(self, entity, ident, **kwargs):
        row = real_get(self, entity, ident, **kwargs)
        if entity is PlayerRefreshSession and row is not None and row.revoked_at is None:
            with state_lock:
                state["n"] += 1
                seen = state["n"]
            if seen <= 2:
                try:
                    release.wait(timeout=1.0)
                except threading.BrokenBarrierError:
                    pass
        return row

    monkeypatch.setattr(Session, "get", patched_get)
    frozen = FrozenClock(_EPOCH)
    server, thread, port = _serve(frozen)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as client:
            registered = client.post(
                "/v1/auth/register",
                json={"username": "Bea", "password": PASSWORD},
            )
            assert registered.status_code == 200, registered.text
            refresh_token = registered.json()["refresh_token"]
            barrier = threading.Barrier(8)
            results: list[httpx.Response] = []
            results_lock = threading.Lock()

            def once() -> None:
                barrier.wait(timeout=5)
                response = client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
                with results_lock:
                    results.append(response)

            workers = [threading.Thread(target=once) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
            assert all(not worker.is_alive() for worker in workers)
            assert len(results) == 8
            winners = [response for response in results if response.status_code == 200]
            losers = [response for response in results if response.status_code == 401]
            assert len(winners) == 1
            assert len(losers) == 7
            assert all(response.json()["error"]["code"] == "invalid_refresh" for response in losers)
            winner_token = winners[0].json()["refresh_token"]
            reused = client.post("/v1/auth/refresh", json={"refresh_token": winner_token})
            assert reused.status_code == 401
            old = client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
            assert old.status_code == 401
        check = get_sessionmaker()()
        try:
            live = int(
                check.scalar(
                    select(func.count())
                    .select_from(PlayerRefreshSession)
                    .where(PlayerRefreshSession.revoked_at.is_(None))
                )
                or 0
            )
            reuse = int(
                check.scalar(
                    select(func.count()).select_from(AuditLog).where(AuditLog.action == "auth.refresh_reuse")
                )
                or 0
            )
        finally:
            check.close()
        assert live == 0
        assert reuse >= 1
    finally:
        _stop(server, thread)


def test_same_second_duplicate_build_and_research_are_conflicts(client, frozen) -> None:
    """A repeated build or research in the same game second is a conflict, not a 500.

    The event key includes the game-time second. Dev-login keeps the frozen clock
    still, so the second call collides with the first.
    """

    world = create_scenario(frozen.now())
    alice = client.post("/v1/auth/dev-login", json={"name": "Alice"})
    assert alice.status_code == 200, alice.text
    headers = {"Authorization": f"Bearer {alice.json()['token']}"}
    build = {"city_id": world["alice_city"], "building": "warehouse"}
    first = client.post("/v1/commands/build", headers=headers, json=build)
    assert first.status_code == 200, first.text
    second = client.post("/v1/commands/build", headers=headers, json=build)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "conflict"
    research = {"tech": "forestry"}
    first_research = client.post("/v1/commands/research", headers=headers, json=research)
    assert first_research.status_code == 200, first_research.text
    second_research = client.post("/v1/commands/research", headers=headers, json=research)
    assert second_research.status_code == 409, second_research.text
    assert second_research.json()["error"]["code"] == "conflict"
