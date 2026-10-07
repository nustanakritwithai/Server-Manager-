"""Phase 4: command traces, integrity verdicts, and the audit hash chain."""

from __future__ import annotations

import json

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from simcore.admin_auth import hash_admin_password
from simcore.config import Settings, get_settings
from simcore.constants import SNAPSHOT_SCHEMA_VERSION
from simcore.db import get_sessionmaker, reset_engine
from simcore.main import create_app
from simcore.models import Event, Movement
from tests.conftest import ADMIN
from tests.world import create_scenario

PASSWORD = "Plaintext-Admin-Password-9f3a"
WRONG_PASSWORD = "Wrong-Password-ZZZ-should-not-appear"
PASSWORD_HASH = hash_admin_password(PASSWORD)
SECRET = "session-secret-" + ("k" * 32)
OUTBOUND = 30_000
HOMEWARD = 30_000


def _login(client, name: str) -> dict[str, str]:
    response = client.post("/v1/auth/dev-login", json={"name": name})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _settings() -> Settings:
    get_settings.cache_clear()
    return Settings(
        _env_file=None,
        admin_password_hash=PASSWORD_HASH,
        admin_session_secret=SECRET,
        admin_session_ttl_seconds=3600,
    )


def _attack(client) -> str:
    alice = _login(client, "Alice")
    response = client.post(
        "/v1/commands/attack",
        json={"army_id": 1, "target_city_id": 2},
        headers=alice,
    )
    assert response.status_code == 200, response.text
    trace_id = response.json()["trace_id"]
    assert trace_id
    return trace_id


def _finish(client) -> None:
    advanced = client.post(
        "/v1/admin/clock/advance",
        json={"seconds": OUTBOUND + HOMEWARD},
        headers=ADMIN,
    )
    assert advanced.status_code == 200, advanced.text
    tick = client.post("/v1/admin/worker/tick", headers=ADMIN)
    assert tick.status_code == 200, tick.text
    assert tick.json()["processed"] == 2


def _check(body: dict, name: str) -> dict:
    checks = body["integrity"]["checks"]
    return next(item for item in checks if item["name"] == name)


def test_attack_trace_passes_after_the_army_returns(client, frozen) -> None:
    create_scenario(frozen.now(), rate=360, stock=1000)
    trace_id = _attack(client)
    _finish(client)

    traced = client.get(f"/v1/admin/trace/{trace_id}", headers=ADMIN)
    assert traced.status_code == 200, traced.text
    body = traced.json()
    assert body["verdict"] == "PASS"
    assert body["reasons"] == []
    assert body["integrity"]["verdict"] == "PASS"
    for check in body["integrity"]["checks"]:
        if check["status"] != "NOT CHECKED":
            assert check["status"] == "PASS", check
    not_checked = body["integrity"]["not_checked"]
    assert not_checked
    assert all(item["status"] == "NOT CHECKED" for item in not_checked)
    assert all(item["status"] != "PASS" for item in not_checked)
    assert any(item["name"] == "production_upkeep" for item in not_checked)

    types = [step["type"] for step in body["steps"]]
    command_at = types.index("command")
    movement_at = types.index("movement")
    event_at = types.index("event")
    battle_at = types.index("battle")
    ledger_at = types.index("ledger")
    report_at = types.index("report")
    assert command_at < movement_at < event_at < battle_at < ledger_at < report_at
    return_at = types.index("movement", report_at)
    return_event_at = types.index("event", return_at)
    gained_at = types.index("ledger", return_event_at)
    assert return_at < return_event_at < gained_at

    ledger = _check(body, "ledger_conservation")
    assert ledger["resources"]["gold"]["out"] == 1200
    assert ledger["resources"]["gold"]["in"] == 1200
    assert ledger["resources"]["gold"]["recorded_losses"] == 0
    assert ledger["resources"]["iron"]["out"] == ledger["resources"]["iron"]["in"] == 1200

    session = get_sessionmaker()()
    try:
        movements = session.query(Movement).filter(Movement.trace_id == trace_id).all()
        events = session.query(Event).filter(Event.trace_id == trace_id).all()
        assert len(movements) == 2
        assert {event.type for event in events} == {"ARMY_ARRIVE", "ARMY_RETURN"}
        rows = session.execute(
            text("SELECT reason, trace_id FROM transactions WHERE reason IN ('loot_lost', 'loot_gained', 'production')")
        ).all()
        loot = [row for row in rows if row.reason in {"loot_lost", "loot_gained"}]
        production = [row for row in rows if row.reason == "production"]
        assert loot and all(row.trace_id == trace_id for row in loot)
        assert production and all(row.trace_id is None for row in production)
    finally:
        session.close()

    found = client.get("/v1/admin/trace", headers=ADMIN, params={"army": 1, "player": 1})
    assert found.status_code == 200, found.text
    assert found.json()["traces"][0]["trace_id"] == trace_id
    assert found.json()["traces"][0]["verdict"] == "PASS"
    assert found.json()["legacy"] == []


def test_trace_is_incomplete_while_the_army_is_en_route(client, frozen) -> None:
    create_scenario(frozen.now(), rate=0, stock=1000)
    trace_id = _attack(client)
    traced = client.get(f"/v1/admin/trace/{trace_id}", headers=ADMIN)
    assert traced.status_code == 200, traced.text
    body = traced.json()
    assert body["verdict"] == "INCOMPLETE"
    assert body["verdict"] != "PASS"
    assert any("en route" in reason for reason in body["reasons"])
    assert _check(body, "army_resolution")["status"] == "INCOMPLETE"
    assert _check(body, "ledger_conservation")["status"] == "INCOMPLETE"
    assert _check(body, "ledger_conservation")["status"] != "PASS"


def test_corrupted_ledger_fails_the_trace(client, frozen) -> None:
    create_scenario(frozen.now(), rate=360, stock=1000)
    trace_id = _attack(client)
    _finish(client)
    session = get_sessionmaker()()
    try:
        updated = session.execute(
            text(
                """
                UPDATE transactions
                SET delta = delta + 1
                WHERE trace_id = :trace_id AND reason = 'loot_gained' AND resource = 'gold'
                """
            ),
            {"trace_id": trace_id},
        )
        assert updated.rowcount == 1
        session.commit()
    finally:
        session.close()

    traced = client.get(f"/v1/admin/trace/{trace_id}", headers=ADMIN)
    assert traced.status_code == 200, traced.text
    body = traced.json()
    assert body["verdict"] == "FAIL"
    assert body["verdict"] != "PASS"
    blob = " ".join(body["reasons"])
    assert "gold: resources out 1200 != resources in 1201 + recorded losses 0" in blob
    assert _check(body, "ledger_conservation")["status"] == "FAIL"


def test_legacy_row_is_not_traced_and_is_not_guessed(client, frozen) -> None:
    create_scenario(frozen.now(), rate=0, stock=1000)
    trace_id = _attack(client)
    session = get_sessionmaker()()
    try:
        legacy_movement = Movement(
            army_id=1,
            origin_city_id=1,
            destination_city_id=1,
            origin_x=0,
            origin_y=0,
            destination_x=0,
            destination_y=0,
            depart_at=frozen.now(),
            arrive_at=frozen.now(),
            mission="move",
            status="completed",
            relocate=False,
            loot_wood=0,
            loot_food=0,
            loot_iron=0,
            loot_gold=0,
            trace_id=None,
            created_at=frozen.now(),
            resolved_at=frozen.now(),
        )
        session.add(legacy_movement)
        session.flush()
        legacy_event = Event(
            due_at=frozen.now(),
            type="ARMY_ARRIVE",
            payload={"movement_id": legacy_movement.id, "note": "pre-trace"},
            status="completed",
            attempts=1,
            idempotency_key="legacy-event",
            movement_id=legacy_movement.id,
            trace_id=None,
            processed_at=frozen.now(),
            created_at=frozen.now(),
        )
        session.add(legacy_event)
        session.commit()
        legacy_event_id = legacy_event.id
        legacy_movement_id = legacy_movement.id
    finally:
        session.close()

    by_event = client.get("/v1/admin/trace", headers=ADMIN, params={"event": legacy_event_id})
    assert by_event.status_code == 200, by_event.text
    payload = by_event.json()
    assert payload["traces"] == []
    assert payload["legacy"] == [
        {"kind": "event", "id": legacy_event_id, "trace": "LEGACY", "detail": "NOT TRACED"}
    ]
    assert trace_id not in by_event.text

    by_army = client.get("/v1/admin/trace", headers=ADMIN, params={"army": 1})
    assert by_army.status_code == 200, by_army.text
    army_body = by_army.json()
    assert [row["trace_id"] for row in army_body["traces"]] == [trace_id]
    assert {"kind": "movement", "id": legacy_movement_id, "trace": "LEGACY", "detail": "NOT TRACED"} in army_body["legacy"]
    missing = client.get("/v1/admin/trace/00000000-0000-0000-0000-000000000000", headers=ADMIN)
    assert missing.status_code == 404


def test_admin_actions_are_audited_without_secrets(db, frozen) -> None:
    create_scenario(frozen.now(), rate=0, stock=50)
    app = create_app(settings=_settings(), base_clock=frozen)
    with TestClient(app) as client:
        failed = client.post("/v1/admin/login", json={"password": WRONG_PASSWORD})
        assert failed.status_code == 401
        signed = client.post("/v1/admin/login", json={"password": PASSWORD})
        assert signed.status_code == 200, signed.text
        token = signed.json()["token"]
        bearer = {"Authorization": f"Bearer {token}"}
        created = client.post("/v1/admin/snapshots", json={"reason": "MANUAL"}, headers=bearer)
        assert created.status_code == 200, created.text
        snap_id = created.json()["snapshot_id"]
        inspected = client.get(f"/v1/admin/snapshots/{snap_id}/inspect", headers=bearer)
        assert inspected.status_code == 200, inspected.text
        advanced = client.post("/v1/admin/clock/advance", json={"seconds": 1}, headers=bearer)
        assert advanced.status_code == 200, advanced.text
        alice = _login(client, "Alice")
        attack = client.post("/v1/commands/attack", json={"army_id": 1, "target_city_id": 2}, headers=alice)
        assert attack.status_code == 200, attack.text
        event_id = attack.json()["event_id"]
        ran = client.post(f"/v1/admin/events/{event_id}/run", headers=bearer)
        assert ran.status_code == 200, ran.text
        tick = client.post("/v1/admin/worker/tick", headers=bearer)
        assert tick.status_code == 200, tick.text
        logged_out = client.post("/v1/admin/logout", headers=bearer)
        assert logged_out.status_code == 200, logged_out.text
        revoked = client.post("/v1/admin/sessions/revoke", headers=ADMIN)
        assert revoked.status_code == 200, revoked.text
        restored = client.post(
            f"/v1/admin/snapshots/{snap_id}/restore",
            json={"confirm": True},
            headers=ADMIN,
        )
        assert restored.status_code == 200, restored.text

        audit = client.get("/v1/admin/audit", headers=ADMIN, params={"limit": 200})
        assert audit.status_code == 200, audit.text
        body = audit.json()
        actions = {row["action"] for row in body["entries"]}
        assert {
            "admin.login",
            "admin.logout",
            "admin.sessions.revoke",
            "snapshot.create",
            "snapshot.inspect",
            "snapshot.restore",
            "clock.advance",
            "event.run",
            "worker.tick",
        } <= actions
        assert any(row["action"] == "admin.login" and row["result"] == "failure" for row in body["entries"])
        assert any(row["action"] == "admin.login" and row["result"] == "success" for row in body["entries"])
        assert body["chain"]["status"] == "PASS"
        assert body["chain"]["checked_rows"] == body["total"]
        dumped = json.dumps(body)
        assert PASSWORD not in dumped
        assert WRONG_PASSWORD not in dumped
        assert PASSWORD_HASH not in dumped
        assert SECRET not in dumped
        assert token not in dumped
        denied = client.post("/v1/admin/audit", headers=ADMIN)
        assert denied.status_code == 405
        removed = client.delete("/v1/admin/audit", headers=ADMIN)
        assert removed.status_code == 405
        missing = client.delete("/v1/admin/audit/1", headers=ADMIN)
        assert missing.status_code == 404


def test_tampering_an_audit_row_breaks_the_chain(client, frozen) -> None:
    stepped = client.post("/v1/admin/clock/advance", json={"seconds": 1}, headers=ADMIN)
    assert stepped.status_code == 200, stepped.text
    again = client.post("/v1/admin/clock/advance", json={"seconds": 1}, headers=ADMIN)
    assert again.status_code == 200, again.text
    intact = client.get("/v1/admin/audit", headers=ADMIN)
    assert intact.status_code == 200, intact.text
    assert intact.json()["chain"]["status"] == "PASS"
    assert intact.json()["chain"]["checked_rows"] >= 2

    session = get_sessionmaker()()
    try:
        session.execute(text("UPDATE audit_log SET reason = 'tampered' WHERE id = (SELECT MIN(id) FROM audit_log)"))
        session.commit()
    finally:
        session.close()

    broken = client.get("/v1/admin/audit", headers=ADMIN)
    assert broken.status_code == 200, broken.text
    chain = broken.json()["chain"]
    assert chain["status"] == "FAIL"
    assert chain["reasons"]
    assert any("row_hash" in reason for reason in chain["reasons"])


def test_migration_0003_upgrades_a_0002_database_without_rewriting_rows(db) -> None:
    cfg = Config("alembic.ini")
    session = get_sessionmaker()()
    try:
        command.downgrade(cfg, "0002_world_snapshots")
        session.execute(
            text(
                """
                INSERT INTO players (name, research, created_at)
                VALUES ('LegacyPlayer', '{}'::jsonb, TIMESTAMPTZ '2026-01-01T00:00:00Z')
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO cities (
                  player_id, name, x, y, wood, food, iron, gold,
                  wood_rate, food_rate, iron_rate, gold_rate, buildings, last_updated, created_at
                )
                VALUES (
                  1, 'Oldkeep', 7, 8, 40, 41, 42, 43,
                  0, 0, 0, 0, '{}'::jsonb, TIMESTAMPTZ '2026-01-01T00:00:00Z', TIMESTAMPTZ '2026-01-01T00:00:00Z'
                )
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO armies (player_id, name, home_city_id, location_city_id, status, units, created_at)
                VALUES (1, 'Old Company', 1, 1, 'garrisoned', '[]'::jsonb, TIMESTAMPTZ '2026-01-01T00:00:00Z')
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO movements (
                  army_id, origin_city_id, destination_city_id, origin_x, origin_y, destination_x, destination_y,
                  depart_at, arrive_at, mission, status, created_at
                )
                VALUES (
                  1, 1, 1, 7, 8, 7, 8,
                  TIMESTAMPTZ '2026-01-01T00:00:00Z', TIMESTAMPTZ '2026-01-01T01:00:00Z',
                  'move', 'completed', TIMESTAMPTZ '2026-01-01T00:00:00Z'
                )
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO events (due_at, type, payload, status, idempotency_key, movement_id, created_at)
                VALUES (
                  TIMESTAMPTZ '2026-01-01T01:00:00Z', 'ARMY_ARRIVE', '{}'::jsonb, 'completed',
                  'legacy-migration-event', 1, TIMESTAMPTZ '2026-01-01T00:00:00Z'
                )
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO transactions (
                  player_id, city_id, resource, delta, balance_after, reason, source_event_id,
                  idempotency_key, created_at
                )
                VALUES (
                  1, 1, 'wood', 3, 43, 'production', 1, 'legacy-migration-txn',
                  TIMESTAMPTZ '2026-01-01T01:00:00Z'
                )
                """
            )
        )
        session.commit()

        command.upgrade(cfg, "0003_audit_trace")
        session.rollback()
        row = session.execute(
            text(
                """
                SELECT p.name, c.wood, m.mission, m.trace_id AS movement_trace,
                       e.trace_id AS event_trace, t.trace_id AS txn_trace, t.delta
                FROM players p
                JOIN cities c ON c.player_id = p.id
                JOIN movements m ON m.army_id = 1
                JOIN events e ON e.movement_id = m.id
                JOIN transactions t ON t.source_event_id = e.id
                WHERE p.name = 'LegacyPlayer'
                """
            )
        ).one()
        assert row.name == "LegacyPlayer"
        assert row.wood == 40
        assert row.mission == "move"
        assert row.delta == 3
        assert row.movement_trace is None
        assert row.event_trace is None
        assert row.txn_trace is None
        nullable = session.execute(
            text(
                """
                SELECT table_name, is_nullable
                FROM information_schema.columns
                WHERE column_name = 'trace_id'
                  AND table_name IN ('movements', 'events', 'battle_reports', 'transactions', 'player_commands')
                """
            )
        ).all()
        assert {item.table_name for item in nullable} == {
            "movements",
            "events",
            "battle_reports",
            "transactions",
            "player_commands",
        }
        assert all(item.is_nullable == "YES" for item in nullable)

        session.rollback()
        command.downgrade(cfg, "0002_world_snapshots")
        session.rollback()
        gone = session.execute(
            text(
                """
                SELECT COUNT(*) FROM information_schema.columns
                WHERE column_name = 'trace_id' AND table_schema = 'public'
                """
            )
        ).scalar_one()
        assert gone == 0
        kept = session.execute(text("SELECT wood FROM cities WHERE name = 'Oldkeep'")).scalar_one()
        assert kept == 40
        tables = session.execute(
            text(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name IN ('audit_log', 'player_commands')
                """
            )
        ).all()
        assert tables == []
    finally:
        session.close()
        command.upgrade(cfg, "head")
        reset_engine()
        get_settings.cache_clear()

    assert SNAPSHOT_SCHEMA_VERSION == 2
