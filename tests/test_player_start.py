"""New-player start: one transaction, one tile, one claim.

Spawn rules come from settings. Resources go through the ledger. A second
claim, including one that overlaps the first, does not create a second city.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from simcore.backup import ledger_failures
from simcore.config import Settings, get_settings
from simcore.constants import SNAPSHOT_SCHEMA_VERSION, Reason
from simcore.db import get_sessionmaker
from simcore.game.start import choose_spawn
from simcore.main import create_app
from simcore.models import Army, City, Player, PlayerAccount, PlayerCommand, Transaction
from simcore.player_auth import hash_player_password
from simcore.snapshot import capture_document
from tests.conftest import ADMIN
from tests.test_auth_races import _serve, _stop

PASSWORD = "correct-horse-battery"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient, username: str, password: str = PASSWORD) -> dict:
    response = client.post("/v1/auth/register", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def _tiny_client(frozen) -> TestClient:
    settings = Settings(
        _env_file=None,
        start_map_min=0,
        start_map_max=0,
        start_min_distance=1,
        monitor_api_sampler=False,
        monitor_sample_seconds=0,
    )
    return TestClient(create_app(settings=settings, base_clock=frozen))


def _legacy(now, *, name: str = "Legacy", must_change: bool = False) -> int:
    session = get_sessionmaker()()
    try:
        player = Player(name=name, research={}, created_at=now)
        session.add(player)
        session.flush()
        session.add(
            PlayerAccount(
                player_id=player.id,
                username=name,
                username_key=name.casefold(),
                email=None,
                email_key=None,
                password_hash=hash_player_password(PASSWORD),
                locked=False,
                must_change_password=must_change,
                failed_login_count=0,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
        return int(player.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def test_choose_spawn_is_deterministic_and_keeps_distance() -> None:
    first = choose_spawn(set(), map_min=-500, map_max=500, min_distance=8)
    assert first == (-500, -500)
    second = choose_spawn({first}, map_min=-500, map_max=500, min_distance=8)
    assert second == (-492, -500)
    assert second == choose_spawn({first}, map_min=-500, map_max=500, min_distance=8)
    assert choose_spawn({(0, 0)}, map_min=0, map_max=0, min_distance=1) is None


def test_register_grants_a_city_army_and_ledger_start(client) -> None:
    registered = _register(client, "Ada")
    assert registered["start_granted"] is True
    home = registered["home_city"]
    assert home["x"] == -500 and home["y"] == -500
    assert home["name"] == "Ada Home"
    assert registered["army_id"] is not None

    me = client.get("/v1/auth/me", headers=_auth(registered["access_token"]))
    assert me.status_code == 200
    assert me.json()["start_granted"] is True
    assert me.json()["home_city"]["id"] == home["id"]
    assert me.json()["army_id"] == registered["army_id"]

    headers = _auth(registered["access_token"])
    trained = client.post(
        "/v1/commands/train",
        headers=headers,
        json={"city_id": home["id"], "unit_type": "militia", "count": 1, "army_id": registered["army_id"]},
    )
    assert trained.status_code == 200, trained.text
    built = client.post(
        "/v1/commands/build",
        headers=headers,
        json={"city_id": home["id"], "building": "farm"},
    )
    assert built.status_code == 200, built.text

    session = get_sessionmaker()()
    try:
        city = session.get(City, home["id"])
        assert city is not None
        assert (city.wood, city.food, city.iron, city.gold) == (1990, 1980, 800, 400)
        rows = session.scalars(
            select(Transaction).where(Transaction.city_id == city.id, Transaction.reason == Reason.START)
        ).all()
        assert {row.resource: row.delta for row in rows} == {"wood": 2000, "food": 2000, "iron": 800, "gold": 400}
        assert all(row.trace_id for row in rows)
        assert ledger_failures(session) == []
        command = session.scalar(select(PlayerCommand).where(PlayerCommand.command_type == "start"))
        assert command is not None and command.trace_id
        trace_id = command.trace_id
        army = session.get(Army, registered["army_id"])
        assert army is not None and army.status == "garrisoned"
        assert army.units == [{"type": "militia", "count": 1}]
    finally:
        session.close()

    traced = client.get(f"/v1/admin/trace/{trace_id}", headers=ADMIN)
    assert traced.status_code == 200, traced.text
    assert traced.json()["verdict"] == "PASS"
    spend = next(check for check in traced.json()["integrity"]["checks"] if check["name"] == "ledger_conservation")
    assert spend["status"] == "PASS"

    listed = client.get("/v1/admin/accounts", params={"q": "ada"}, headers=ADMIN)
    assert listed.status_code == 200
    row = listed.json()["accounts"][0]
    assert row["start_granted"] is True
    assert row["home_city_id"] == home["id"]

    schema = client.app.openapi()
    assert "/v1/auth/claim-start" in schema["paths"]
    models = schema["components"]["schemas"]
    assert "start_granted" in models["AuthMeOut"]["properties"]
    assert "home_city" in models["RegisterOut"]["properties"]
    assert "created" in models["ClaimStartOut"]["properties"]
    assert "start_granted" in models["AccountOut"]["properties"]
    assert "home_city_id" in models["AccountOut"]["properties"]


def test_two_registers_do_not_overlap(client) -> None:
    first = _register(client, "Ada")
    second = _register(client, "Bea")
    assert first["home_city"]["id"] != second["home_city"]["id"]
    ax, ay = first["home_city"]["x"], first["home_city"]["y"]
    bx, by = second["home_city"]["x"], second["home_city"]["y"]
    assert max(abs(ax - bx), abs(ay - by)) >= 8
    session = get_sessionmaker()()
    try:
        assert session.scalar(select(func.count()).select_from(City)) == 2
        assert session.scalar(select(func.count()).select_from(Army)) == 2
        assert ledger_failures(session) == []
    finally:
        session.close()


def test_world_full_rolls_the_account_back(db, frozen) -> None:
    with _tiny_client(frozen) as client:
        first = _register(client, "Ada")
        assert first["home_city"] == {"id": first["home_city"]["id"], "name": "Ada Home", "x": 0, "y": 0}
        blocked = client.post("/v1/auth/register", json={"username": "Bea", "password": PASSWORD})
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "world_full"
        assert "free tile" in blocked.json()["error"]["message"]
    session = get_sessionmaker()()
    try:
        names = set(session.scalars(select(Player.name)).all())
        assert names == {"Ada"}
        assert session.scalar(select(func.count()).select_from(PlayerAccount)) == 1
        assert session.scalar(select(func.count()).select_from(City)) == 1
        audit = client_audit_reasons(session)
        assert "world_full" in audit
        assert ledger_failures(session) == []
    finally:
        session.close()


def client_audit_reasons(session) -> set[str | None]:
    from simcore.models import AuditLog

    return set(session.scalars(select(AuditLog.reason)).all())


def test_a_crash_during_the_grant_leaves_no_account(client, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("ledger failed")

    monkeypatch.setattr("simcore.game.start.apply_resource_delta", boom)
    with pytest.raises(RuntimeError, match="ledger failed"):
        client.post("/v1/auth/register", json={"username": "Ada", "password": PASSWORD})
    session = get_sessionmaker()()
    try:
        assert session.scalar(select(func.count()).select_from(Player)) == 0
        assert session.scalar(select(func.count()).select_from(PlayerAccount)) == 0
        assert session.scalar(select(func.count()).select_from(City)) == 0
        assert session.scalar(select(func.count()).select_from(Army)) == 0
        assert session.scalar(select(func.count()).select_from(Transaction)) == 0
    finally:
        session.close()


def test_claim_is_idempotent_and_blocked_until_the_password_changes(client, frozen) -> None:
    legacy_id = _legacy(frozen.now(), must_change=True)
    logged = client.post("/v1/auth/login", json={"username": "Legacy", "password": PASSWORD})
    assert logged.status_code == 200, logged.text
    blocked = client.post("/v1/auth/claim-start", headers=_auth(logged.json()["access_token"]))
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "password_change_required"
    session = get_sessionmaker()()
    try:
        assert session.scalar(select(func.count()).select_from(City).where(City.player_id == legacy_id)) == 0
    finally:
        session.close()

    changed = client.post(
        "/v1/auth/change-password",
        headers=_auth(logged.json()["access_token"]),
        json={"current_password": PASSWORD, "new_password": "correct-horse-other"},
    )
    assert changed.status_code == 200, changed.text
    headers = _auth(changed.json()["access_token"])
    me = client.get("/v1/auth/me", headers=headers)
    assert me.json()["start_granted"] is False
    first = client.post("/v1/auth/claim-start", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True
    assert first.json()["start_granted"] is True
    assert first.json()["trace_id"]
    second = client.post("/v1/auth/claim-start", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["home_city"]["id"] == first.json()["home_city"]["id"]
    assert second.json()["trace_id"] is None
    session = get_sessionmaker()()
    try:
        assert session.scalar(select(func.count()).select_from(City).where(City.player_id == legacy_id)) == 1
        assert session.scalar(select(func.count()).select_from(Army).where(Army.player_id == legacy_id)) == 1
        assert ledger_failures(session) == []
    finally:
        session.close()

    listed = client.get("/v1/admin/accounts", params={"q": "legacy"}, headers=ADMIN)
    assert listed.json()["accounts"][0]["start_granted"] is True


def test_seeded_player_claim_does_not_add_a_city(client, frozen) -> None:
    from tests.world import create_scenario

    world = create_scenario(frozen.now())
    claimed = client.post(
        "/v1/admin/accounts/temporary-password",
        headers=ADMIN,
        json={"player_id": world["alice_id"], "password": "temporary-pass-1"},
    )
    assert claimed.status_code == 200, claimed.text
    logged = client.post("/v1/auth/login", json={"username": "Alice", "password": "temporary-pass-1"})
    changed = client.post(
        "/v1/auth/change-password",
        headers=_auth(logged.json()["access_token"]),
        json={"current_password": "temporary-pass-1", "new_password": PASSWORD},
    )
    headers = _auth(changed.json()["access_token"])
    before = client.get("/v1/me/cities", headers=headers)
    claim = client.post("/v1/auth/claim-start", headers=headers)
    assert claim.status_code == 200, claim.text
    assert claim.json()["created"] is False
    assert claim.json()["home_city"]["id"] == world["alice_city"]
    after = client.get("/v1/me/cities", headers=headers)
    assert len(after.json()["cities"]) == len(before.json()["cities"])


def test_snapshot_restore_keeps_the_start_rows(client) -> None:
    ada = _register(client, "Ada")
    session = get_sessionmaker()()
    try:
        before = capture_document(session)
        assert before["schema_version"] == SNAPSHOT_SCHEMA_VERSION == 2
        assert any(row["name"] == "Ada Home" for row in before["cities"])
        assert any(row["reason"] == Reason.START for row in before["transactions"])
    finally:
        session.close()
    created = client.post("/v1/admin/snapshots", json={"reason": "MANUAL"}, headers=ADMIN)
    assert created.status_code == 200, created.text
    assert created.json()["schema_version"] == 2
    _register(client, "Bea")
    restored = client.post(
        f"/v1/admin/snapshots/{created.json()['snapshot_id']}/restore",
        json={"confirm": True},
        headers=ADMIN,
    )
    assert restored.status_code == 200, restored.text
    session = get_sessionmaker()()
    try:
        names = set(session.scalars(select(Player.name)).all())
        assert names == {"Ada"}
        city = session.scalar(select(City).where(City.name == "Ada Home"))
        assert city is not None and city.id == ada["home_city"]["id"]
        assert session.scalar(select(func.count()).select_from(Army)) == 1
        assert session.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.reason == Reason.START)
        ) == 4
        assert ledger_failures(session) == []
        document = capture_document(session)
        assert document["schema_version"] == 2
    finally:
        session.close()


def test_concurrent_claims_create_one_city(db, monkeypatch) -> None:
    from simcore.game import start as start_module

    original = start_module._insert_start
    state = {"n": 0}
    entered = threading.Event()

    def wrapped(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            entered.wait(0.4)
        else:
            entered.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(start_module, "_insert_start", wrapped)
    frozen_holder = {}
    from simcore.clock import FrozenClock
    from datetime import datetime, timezone

    frozen_holder["clock"] = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    _legacy(frozen_holder["clock"].now(), name="Legacy")
    server, thread, port = _serve(frozen_holder["clock"])
    try:
        import httpx

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as client:
            logged = client.post("/v1/auth/login", json={"username": "Legacy", "password": PASSWORD})
            assert logged.status_code == 200, logged.text
            headers = {"Authorization": f"Bearer {logged.json()['access_token']}"}
            barrier = threading.Barrier(2)
            results: list[tuple[int, object]] = []
            lock = threading.Lock()

            def once() -> None:
                barrier.wait(timeout=5)
                response = client.post("/v1/auth/claim-start", headers=headers)
                try:
                    payload = response.json()
                except Exception:
                    payload = response.text
                with lock:
                    results.append((response.status_code, payload))

            workers = [threading.Thread(target=once) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
        assert state["n"] == 1
        assert len(results) == 2
        assert [status for status, _payload in results] == [200, 200] or sorted(
            status for status, _payload in results
        ) == [200, 200]
        created_flags = [payload.get("created") for _status, payload in results if isinstance(payload, dict)]
        assert sorted(created_flags) == [False, True]
        ids = {payload["home_city"]["id"] for _status, payload in results if isinstance(payload, dict)}
        assert len(ids) == 1
    finally:
        _stop(server, thread)
    session = get_sessionmaker()()
    try:
        assert session.scalar(select(func.count()).select_from(City)) == 1
        assert session.scalar(select(func.count()).select_from(Army)) == 1
        assert ledger_failures(session) == []
    finally:
        session.close()


def test_concurrent_registers_do_not_share_a_tile(db, monkeypatch) -> None:
    from simcore.game import start as start_module

    original = start_module._occupied_tiles
    overlap = {"inside": 0, "bad": False}
    gate = threading.Lock()

    def wrapped(session):
        # Called only after pg_advisory_xact_lock. Two grants must not be here together.
        with gate:
            overlap["inside"] += 1
            if overlap["inside"] > 1:
                overlap["bad"] = True
        try:
            time.sleep(0.3)
            return original(session)
        finally:
            with gate:
                overlap["inside"] -= 1

    monkeypatch.setattr(start_module, "_occupied_tiles", wrapped)
    from datetime import datetime, timezone

    from simcore.clock import FrozenClock

    frozen = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    server, thread, port = _serve(frozen)
    try:
        import httpx

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15.0) as client:
            barrier = threading.Barrier(2)
            results: list[tuple[int, object]] = []
            lock = threading.Lock()

            def once(name: str) -> None:
                barrier.wait(timeout=5)
                response = client.post(
                    "/v1/auth/register",
                    json={"username": name, "password": PASSWORD},
                )
                try:
                    payload = response.json()
                except Exception:
                    payload = response.text
                with lock:
                    results.append((response.status_code, payload))

            workers = [threading.Thread(target=once, args=(name,)) for name in ("Ada", "Bea")]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
        assert overlap["bad"] is False
        assert len(results) == 2
        assert all(status == 200 for status, _payload in results)
        homes = [payload["home_city"] for _status, payload in results]
        assert homes[0]["id"] != homes[1]["id"]
        assert (homes[0]["x"], homes[0]["y"]) != (homes[1]["x"], homes[1]["y"])
    finally:
        _stop(server, thread)
    session = get_sessionmaker()()
    try:
        assert session.scalar(select(func.count()).select_from(City)) == 2
        assert session.scalar(select(func.count()).select_from(Player)) == 2
        assert ledger_failures(session) == []
    finally:
        session.close()
