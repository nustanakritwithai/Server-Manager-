"""Phase 5: measured monitoring. Missing data is UNKNOWN, never OK."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from simcore.config import Settings, get_settings
from simcore.constants import SNAPSHOT_SCHEMA_VERSION, EventStatus
from simcore.db import get_sessionmaker, reset_engine
from simcore.models import Event, MonitoringSample, WorkerHeartbeat, WorkerProcessMark, utcnow
from simcore.monitoring import (
    DOWN,
    NOT_INSTRUMENTED,
    OK,
    UNKNOWN,
    UP,
    WARN,
    api_metrics,
    collect_report,
    percentile,
    sample_once,
)
from simcore.snapshot import capture_document, world_checksum
from simcore.worker import run_once
from tests.conftest import ADMIN
from tests.world import create_scenario

ROOT = Path(__file__).resolve().parents[1]


def _check(body: dict, name: str) -> dict:
    matches = [item for item in body["checks"] if item["name"] == name]
    assert matches, name
    return matches[0]


def _session():
    return get_sessionmaker()()


def test_percentile_does_not_invent_a_value_for_an_empty_window() -> None:
    assert percentile([], 95) is None
    assert percentile([10.0], 95) == 10.0
    # Nearest rank: ceil(0.95 * 4) = 4, index 3.
    assert percentile([10.0, 20.0, 30.0, 40.0], 95) == 40.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 20.0


def test_public_health_stays_minimal_and_monitoring_requires_admin(client) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    missing = client.get("/v1/admin/monitoring")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    wrong = client.get("/v1/admin/monitoring/history", params={"metric": "queue_pending"})
    assert wrong.status_code == 401
    posted = client.post("/v1/admin/monitoring", headers=ADMIN)
    assert posted.status_code == 405


def test_due_events_raise_lag_and_processing_clears_it(client, frozen) -> None:
    ids = create_scenario(frozen.now())
    now = frozen.now()
    session = _session()
    try:
        session.add_all(
            [
                Event(
                    due_at=now - timedelta(seconds=200),
                    type="BUILD_COMPLETE",
                    payload={"city_id": ids["alice_city"], "player_id": ids["alice_id"], "building": "barracks"},
                    status=EventStatus.PENDING,
                    attempts=0,
                    idempotency_key="monitor-due",
                    created_at=now,
                ),
                Event(
                    due_at=now + timedelta(seconds=1000),
                    type="BUILD_COMPLETE",
                    payload={"city_id": ids["alice_city"], "player_id": ids["alice_id"], "building": "wall"},
                    status=EventStatus.PENDING,
                    attempts=0,
                    idempotency_key="monitor-future",
                    created_at=now,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    before = client.get("/v1/admin/monitoring", headers=ADMIN)
    assert before.status_code == 200, before.text
    pending = _check(before.json(), "event_queue.pending")
    due = _check(before.json(), "event_queue.due")
    lag = _check(before.json(), "event_queue.lag")
    assert pending["value"] == 2
    assert due["value"] == 1
    assert lag["value"] >= 200
    assert lag["status"] == "CRITICAL"
    assert before.json()["overall"]["status"] != OK

    status, event_id = run_once(frozen)
    assert status == "processed"
    assert event_id is not None

    after = client.get("/v1/admin/monitoring", headers=ADMIN)
    assert after.status_code == 200, after.text
    assert _check(after.json(), "event_queue.pending")["value"] == 1
    assert _check(after.json(), "event_queue.due")["value"] == 0
    assert _check(after.json(), "event_queue.lag")["value"] == 0
    assert _check(after.json(), "event_queue.lag")["status"] == OK
    rate = _check(after.json(), "event_queue.processing_rate")
    assert rate["value"] >= 1
    assert rate["detail"]["source"] == "worker_process_marks"
    heart = _check(after.json(), "worker.heartbeat")
    assert heart["status"] == OK
    assert heart["detail"]["liveness"] == UP
    assert heart["detail"]["heartbeats"][0]["events_processed_last_tick"] == 1
    assert heart["detail"]["heartbeats"][0]["pid"] > 0
    assert heart["detail"]["heartbeats"][0]["version"]
    assert heart["value"] < 15


def test_missing_heartbeat_is_unknown_and_stale_heartbeat_is_down(client) -> None:
    quiet = client.get("/v1/admin/monitoring", headers=ADMIN)
    assert quiet.status_code == 200, quiet.text
    missing = _check(quiet.json(), "worker.heartbeat")
    assert missing["status"] == UNKNOWN
    assert missing["value"] is None
    assert missing["detail"]["liveness"] == UNKNOWN
    assert quiet.json()["overall"]["status"] != OK
    assert "worker.heartbeat" in quiet.json()["overall"]["unknown_checks"]

    session = _session()
    try:
        old = utcnow() - timedelta(minutes=5)
        session.add(
            WorkerHeartbeat(
                worker_id="stale-worker",
                pid=4242,
                hostname="test",
                version="0.1.0",
                commit_sha=None,
                started_at=old,
                last_tick_at=old,
                tick_duration_ms=3.5,
                events_processed=0,
                tick_status="empty",
                updated_at=old,
            )
        )
        session.commit()
    finally:
        session.close()

    stale = client.get("/v1/admin/monitoring", headers=ADMIN)
    heart = _check(stale.json(), "worker.heartbeat")
    assert heart["status"] == "CRITICAL"
    assert heart["detail"]["liveness"] == DOWN
    assert heart["value"] >= 300
    assert "DOWN" in heart["reason"]


def test_failed_events_warn_and_missing_disk_is_not_instrumented(client, frozen) -> None:
    now = frozen.now()
    session = _session()
    try:
        session.add(
            Event(
                due_at=now,
                type="BUILD_COMPLETE",
                payload={},
                status=EventStatus.FAILED,
                attempts=5,
                idempotency_key="monitor-failed",
                created_at=now,
            )
        )
        session.commit()
        game_now = now
        settings = Settings(_env_file=None, monitor_disk_path="/no/such/simcore-disk-path")
        report = collect_report(session, game_now=game_now, settings=settings, include_api=True)
    finally:
        session.close()

    failed = _check(report, "event_queue.failed")
    assert failed["value"] == 1
    assert failed["status"] == WARN
    disk = _check(report, "disk.free")
    assert disk["status"] == NOT_INSTRUMENTED
    assert disk["value"] is None
    cpu = _check(report, "host.cpu")
    assert cpu["status"] in {OK, WARN, "CRITICAL"}
    assert isinstance(cpu["value"], (int, float))
    memory = _check(report, "host.memory")
    assert memory["status"] in {OK, WARN, "CRITICAL"}
    assert isinstance(memory["value"], (int, float))


def test_host_failure_is_unknown_not_a_zero(client, monkeypatch) -> None:
    monkeypatch.setattr("simcore.monitoring.measure_host", lambda: (None, None, "OSError"))
    body = client.get("/v1/admin/monitoring", headers=ADMIN).json()
    cpu = _check(body, "host.cpu")
    memory = _check(body, "host.memory")
    assert cpu["status"] == UNKNOWN
    assert cpu["value"] is None
    assert memory["status"] == UNKNOWN
    assert memory["value"] is None
    assert "0" not in cpu["reason"]


def test_disk_and_database_use_real_measurements(client) -> None:
    body = client.get("/v1/admin/monitoring", headers=ADMIN).json()
    disk = _check(body, "disk.free")
    assert disk["status"] != NOT_INSTRUMENTED
    usage = shutil.disk_usage(disk["detail"]["path"])
    assert disk["detail"]["total_bytes"] == usage.total
    assert abs(disk["value"] - usage.free) < 1024**3
    if usage.free <= 2048 * 1024 * 1024:
        assert disk["status"] == "CRITICAL"
    elif usage.free <= 5120 * 1024 * 1024:
        assert disk["status"] == WARN
    else:
        assert disk["status"] == OK
    db = _check(body, "database.connectivity")
    assert db["status"] == OK
    assert db["value"] is True
    rtt = _check(body, "database.rtt")
    assert rtt["value"] >= 0
    size = _check(body, "database.size")
    assert size["value"] > 0
    assert size["detail"]["largest_tables"]
    pool = _check(body, "database.pool")
    assert pool["detail"]["process"] == "api"
    assert pool["detail"]["checked_out"] >= 1
    assert pool["value"] == pool["detail"]["checked_out"] / pool["detail"]["capacity"]


def test_api_counters_come_from_this_process_and_reset(client) -> None:
    api_metrics.reset()
    health = client.get("/health")
    assert health.status_code == 200
    api_metrics.record(2500.0, 500)
    api_metrics.record(10.0, 200)
    body = client.get("/v1/admin/monitoring", headers=ADMIN).json()
    assert body["api_process"]["resets_on_restart"] is True
    assert "reset" in body["api_process"]["note"].lower()
    assert body["api_process"]["total_requests"] >= 3
    errors = _check(body, "api.5xx")
    assert errors["value"] >= 1
    assert errors["status"] in (WARN, "CRITICAL")
    latency = _check(body, "api.latency_p95")
    assert latency["value"] >= 2500
    assert latency["status"] == "CRITICAL"
    api_metrics.reset()
    cleared = client.get("/v1/admin/monitoring", headers=ADMIN).json()
    # The reset dropped the injected samples. This request is recorded after the handler.
    assert _check(cleared, "api.5xx")["value"] == 0


def test_samples_history_and_retention(client, frozen) -> None:
    wall = datetime.now(timezone.utc)
    session = _session()
    try:
        session.add_all(
            [
                MonitoringSample(sampled_at=wall - timedelta(days=8), metric="queue_pending", value=9),
                MonitoringSample(sampled_at=wall - timedelta(hours=1), metric="queue_pending", value=4),
                WorkerProcessMark(
                    worker_id="old",
                    event_id=None,
                    outcome="processed",
                    wall_at=wall - timedelta(days=8),
                ),
                WorkerHeartbeat(
                    worker_id="ancient",
                    pid=1,
                    hostname="old",
                    version="0.1.0",
                    commit_sha=None,
                    started_at=wall - timedelta(days=9),
                    last_tick_at=wall - timedelta(days=8),
                    tick_duration_ms=1,
                    events_processed=0,
                    tick_status="empty",
                    updated_at=wall - timedelta(days=8),
                ),
                WorkerHeartbeat(
                    worker_id="current",
                    pid=2,
                    hostname="new",
                    version="0.1.0",
                    commit_sha=None,
                    started_at=wall,
                    last_tick_at=wall,
                    tick_duration_ms=1,
                    events_processed=0,
                    tick_status="empty",
                    updated_at=wall,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    sample_once(base_clock=frozen)
    listed = client.get(
        "/v1/admin/monitoring/history",
        headers=ADMIN,
        params={"metric": "queue_pending", "window": "24h"},
    )
    assert listed.status_code == 200, listed.text
    values = [point["value"] for point in listed.json()["points"]]
    assert 4 in values
    assert 9 not in values
    assert "event_lag_seconds" in listed.json()["metrics"]

    session = _session()
    try:
        old = session.scalar(
            text("SELECT COUNT(*) FROM monitoring_samples WHERE value = 9 AND metric = 'queue_pending'")
        )
        marks = session.scalar(text("SELECT COUNT(*) FROM worker_process_marks WHERE worker_id = 'old'"))
        ancient = session.get(WorkerHeartbeat, "ancient")
        current = session.get(WorkerHeartbeat, "current")
        assert old == 0
        assert marks == 0
        assert ancient is None
        assert current is not None
    finally:
        session.close()

    missing_metric = client.get("/v1/admin/monitoring/history", headers=ADMIN, params={"window": "24h"})
    assert missing_metric.status_code == 400
    bad_window = client.get(
        "/v1/admin/monitoring/history",
        headers=ADMIN,
        params={"metric": "queue_pending", "window": "2h"},
    )
    assert bad_window.status_code == 400
    unknown = client.get(
        "/v1/admin/monitoring/history",
        headers=ADMIN,
        params={"metric": "not_a_metric", "window": "1h"},
    )
    assert unknown.status_code == 200
    assert unknown.json()["points"] == []


def test_worker_liveness_transitions_extend_the_audit_chain(client, frozen) -> None:
    sample_once(base_clock=frozen)
    quiet = client.get("/v1/admin/audit", headers=ADMIN)
    assert quiet.status_code == 200, quiet.text
    assert quiet.json()["chain"]["status"] == "PASS"
    assert all(row["action"] != "monitor.worker.up" for row in quiet.json()["entries"])

    run_once(frozen)
    sample_once(base_clock=frozen)
    up = client.get("/v1/admin/audit", headers=ADMIN).json()
    assert up["chain"]["status"] == "PASS"
    assert any(row["action"] == "monitor.worker.recovered" and row["actor"] == "system" for row in up["entries"])

    session = _session()
    try:
        session.execute(
            text("UPDATE worker_heartbeats SET last_tick_at = :old"),
            {"old": datetime.now(timezone.utc) - timedelta(minutes=5)},
        )
        session.commit()
    finally:
        session.close()
    sample_once(base_clock=frozen)
    down = client.get("/v1/admin/audit", headers=ADMIN).json()
    assert down["chain"]["status"] == "PASS"
    assert any(row["action"] == "monitor.worker.down" and row["result"] == "failure" for row in down["entries"])
    dumped = str(down)
    assert "dev-admin" not in dumped
    assert "scrypt$" not in dumped

    session = _session()
    try:
        session.execute(text("UPDATE worker_heartbeats SET last_tick_at = :now"), {"now": datetime.now(timezone.utc)})
        session.commit()
    finally:
        session.close()
    sample_once(base_clock=frozen)
    back = client.get("/v1/admin/audit", headers=ADMIN).json()
    assert back["chain"]["status"] == "PASS"
    assert any(row["action"] == "monitor.worker.recovered" for row in back["entries"])


def test_monitoring_rows_do_not_change_the_world_checksum(client, frozen) -> None:
    create_scenario(frozen.now())
    session = _session()
    try:
        before = world_checksum(session)
        document = capture_document(session)
    finally:
        session.close()
    assert document["schema_version"] == SNAPSHOT_SCHEMA_VERSION == 2
    assert {
        "worker_heartbeats",
        "worker_process_marks",
        "monitoring_samples",
        "monitoring_check_state",
    }.isdisjoint(document)

    snap = client.post("/v1/admin/snapshots", json={"reason": "MANUAL"}, headers=ADMIN)
    assert snap.status_code == 200, snap.text
    assert snap.json()["checksum"] == before
    run_once(frozen)
    sample_once(base_clock=frozen)
    session = _session()
    try:
        assert world_checksum(session) == before
        heartbeats = session.scalar(text("SELECT COUNT(*) FROM worker_heartbeats"))
        assert heartbeats >= 1
    finally:
        session.close()

    moved = client.post("/v1/admin/clock/advance", json={"seconds": 30}, headers=ADMIN)
    assert moved.status_code == 200, moved.text
    session = _session()
    try:
        changed = world_checksum(session)
    finally:
        session.close()
    assert changed != before
    sample_once(base_clock=frozen)
    restored = client.post(
        f"/v1/admin/snapshots/{snap.json()['snapshot_id']}/restore",
        json={"confirm": True},
        headers=ADMIN,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["match"] is True
    session = _session()
    try:
        assert world_checksum(session) == before
        assert session.scalar(text("SELECT COUNT(*) FROM worker_heartbeats")) >= 1
        assert session.scalar(text("SELECT COUNT(*) FROM monitoring_samples")) >= 1
    finally:
        session.close()


def test_migration_0004_is_additive_on_a_0003_database(db) -> None:
    cfg = Config("alembic.ini")
    session = _session()
    try:
        command.downgrade(cfg, "0003_audit_trace")
        session.rollback()
        before_columns = session.execute(
            text(
                """
                SELECT table_name, column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            )
        ).all()
        before_indexes = set(
            session.execute(
                text("SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'public'")
            ).all()
        )
        session.execute(
            text(
                """
                INSERT INTO players (name, research, created_at)
                VALUES ('KeepPlayer', '{}'::jsonb, TIMESTAMPTZ '2026-01-01T00:00:00Z')
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
                  1, 'Keep', 1, 2, 40, 1, 2, 3,
                  0, 0, 0, 0, '{}'::jsonb, TIMESTAMPTZ '2026-01-01T00:00:00Z', TIMESTAMPTZ '2026-01-01T00:00:00Z'
                )
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO events (due_at, type, payload, status, idempotency_key, created_at)
                VALUES (
                  TIMESTAMPTZ '2026-01-01T01:00:00Z', 'BUILD_COMPLETE', '{}'::jsonb, 'pending',
                  'keep-event', TIMESTAMPTZ '2026-01-01T00:00:00Z'
                )
                """
            )
        )
        session.commit()
        command.upgrade(cfg, "0004_monitoring")
        session.rollback()
        after_columns = session.execute(
            text(
                """
                SELECT table_name, column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            )
        ).all()
        old_tables = {row.table_name for row in before_columns}
        assert [row for row in after_columns if row.table_name in old_tables] == list(before_columns)
        added_tables = {row.table_name for row in after_columns} - old_tables
        assert added_tables == {
            "worker_heartbeats",
            "worker_process_marks",
            "monitoring_samples",
            "monitoring_check_state",
        }
        after_indexes = set(
            session.execute(
                text("SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'public'")
            ).all()
        )
        removed = before_indexes - after_indexes
        assert removed == set()
        added_index_tables = {table for table, _name in after_indexes - before_indexes}
        assert added_index_tables <= added_tables
        kept = session.execute(
            text(
                """
                SELECT p.name, c.wood, e.status, e.trace_id
                FROM players p
                JOIN cities c ON c.player_id = p.id
                JOIN events e ON e.idempotency_key = 'keep-event'
                """
            )
        ).one()
        assert kept.name == "KeepPlayer"
        assert kept.wood == 40
        assert kept.status == "pending"
        assert kept.trace_id is None
        empty = session.scalar(text("SELECT COUNT(*) FROM monitoring_samples"))
        assert empty == 0
        session.rollback()

        command.downgrade(cfg, "0003_audit_trace")
        session.rollback()
        gone = session.execute(
            text(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN (
                    'worker_heartbeats', 'worker_process_marks',
                    'monitoring_samples', 'monitoring_check_state'
                  )
                """
            )
        ).all()
        assert gone == []
        still = session.scalar(text("SELECT wood FROM cities WHERE name = 'Keep'"))
        assert still == 40
    finally:
        session.close()
        command.upgrade(cfg, "head")
        reset_engine()
        get_settings.cache_clear()


def test_admin_page_displays_server_monitoring() -> None:
    html = (ROOT / "web" / "admin" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "admin" / "admin.js").read_text(encoding="utf-8")
    assert 'data-nav="monitoring"' in html
    assert 'data-view="monitoring"' in html
    assert 'id="monitor-auto"' in html
    assert "/v1/admin/monitoring" in js
    assert "monitoring/history" in js
    assert "createElementNS" in js
    assert "cdn" not in js.lower()
    assert js.count("confirm: true") == 1
