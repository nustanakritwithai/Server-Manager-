"""Phase 6 backup/DR. A world snapshot is not a database backup.

The Windows scripts shell out to this module. These tests cover the decisions
and the restore drill. The pg_dump round trip runs when the PostgreSQL 16
client tools are installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from simcore.backup import (
    BackupError,
    assert_droppable_database,
    backup_stem,
    build_manifest,
    collect_facts,
    consistent_dump,
    create_database,
    drill_database,
    drill_session,
    drop_database,
    dump_fits,
    evaluate_backup_status,
    libpq_parts,
    local_retention,
    merge_status,
    parse_backup_stem,
    parse_manifest,
    read_checksum_file,
    redact,
    rename_database,
    retention_plan,
    sha256_file,
    verify_checksum,
    write_pgpass_file,
)
from simcore.config import Settings, get_settings
from simcore.constants import SnapshotReason
from simcore.db import get_sessionmaker
from simcore.models import City, Transaction, WorldSnapshotPayload
from simcore.monitoring import UNKNOWN, collect_report
from simcore.snapshot import create_snapshot
from tests.conftest import ADMIN
from tests.world import create_scenario

ROOT = Path(__file__).resolve().parents[1]
GB = 1024 * 1024 * 1024


def _check(body: dict, name: str) -> dict:
    matches = [item for item in body["checks"] if item["name"] == name]
    assert matches, name
    return matches[0]


def _now() -> datetime:
    return datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)


def _stem(when: datetime) -> str:
    return backup_stem(taken_at=when, git_commit="abcdef1234567890", alembic_revision="0004_monitoring")


def _manifest_from_facts(facts: dict, *, size: int = 10, digest: str | None = None) -> dict:
    return build_manifest(
        taken_at=_now(),
        size_bytes=size,
        database=facts["database"],
        alembic_revision=facts["alembic_revision"],
        git_commit="abcdef1234567890abcdef1234567890abcdef12",
        pg_dump_version=facts.get("pg_dump_version") or "pg_dump (PostgreSQL) 16",
        sha256=digest or ("ab" * 32),
        row_counts=facts["row_counts"],
        world_checksum_value=facts["world_checksum"],
    )


def test_disk_budget_refuses_when_the_dump_would_leave_under_one_gigabyte() -> None:
    free = 2 * GB
    small = dump_fits(free_bytes=free, database_bytes=100 * 1024 * 1024, margin=1.5, minimum_free_bytes=GB)
    assert small["ok"] is True
    assert small["remaining_bytes"] == free - small["estimated_bytes"]
    tight = dump_fits(free_bytes=free, database_bytes=800 * 1024 * 1024, margin=1.5, minimum_free_bytes=GB)
    assert tight["ok"] is False
    assert tight["remaining_bytes"] < GB


def test_dump_host_must_stay_on_localhost_and_secrets_are_redacted() -> None:
    with pytest.raises(BackupError, match="localhost"):
        libpq_parts("postgresql+psycopg://simcore:secret@203.0.113.5:5432/simcore")
    shown = redact("postgresql+psycopg://simcore:s3cret@127.0.0.1:5432/simcore password=s3cret ya29.abcdef")
    assert "s3cret" not in shown
    assert "ya29.abcdef" not in shown
    assert "***" in shown


def test_manifest_requires_the_backup_fields_and_rejects_secrets() -> None:
    facts = {
        "database": "simcore",
        "alembic_revision": "0004_monitoring",
        "row_counts": {"players": 2},
        "world_checksum": "sha256:" + "cd" * 32,
        "pg_dump_version": "pg_dump (PostgreSQL) 16.11",
    }
    with pytest.raises(BackupError, match="row_counts"):
        build_manifest(
            taken_at=_now(),
            size_bytes=1,
            database="simcore",
            alembic_revision="0004_monitoring",
            git_commit="abc",
            pg_dump_version="pg_dump (PostgreSQL) 16.11",
            sha256="ab" * 32,
            row_counts={"players": 2},
            world_checksum_value=facts["world_checksum"],
        )
    document = {
        "time": "2026-04-01T12:00:00+00:00",
        "size_bytes": 4,
        "database": "simcore",
        "alembic_revision": "0004_monitoring",
        "git_commit": "abc",
        "pg_dump_version": "pg_dump (PostgreSQL) 16.11",
        "password": "nope",
    }
    with pytest.raises(BackupError, match="secret"):
        parse_manifest(document)
    with pytest.raises(BackupError, match="missing"):
        parse_manifest({"time": "2026-04-01T12:00:00Z", "database": "simcore"})


def test_retention_keeps_recent_daily_and_weekly_copies_only() -> None:
    base = _now()
    later = base.replace(hour=18)
    early = base.replace(hour=1)
    monthish = base - timedelta(days=30)
    ancient = base - timedelta(days=120)
    names = [_stem(early), _stem(later), _stem(monthish), _stem(ancient), "notes.txt"]
    plan = retention_plan(names, keep_daily=14, keep_weekly=8)
    assert _stem(later) in plan["keep"]
    assert _stem(early) in plan["delete"]
    assert _stem(monthish) in plan["keep"]
    assert _stem(ancient) in plan["delete"]
    assert "notes.txt" not in plan["keep"]
    assert "notes.txt" not in plan["delete"]
    local = local_retention(names, keep=2)
    assert local["keep"][0] == _stem(later)
    assert _stem(ancient) in local["delete"]
    assert _stem(later) not in local["delete"]
    assert parse_backup_stem(_stem(later)) == later.replace(minute=0, second=0, microsecond=0)


def test_failed_attempt_keeps_the_last_verified_backup() -> None:
    verified = _now()
    good = merge_status(
        None,
        now=verified,
        attempt_ok=True,
        uploaded=True,
        database="simcore",
        dump_name="simcore-good.dump",
        size_bytes=20,
        sha256="ab" * 32,
        alembic_revision="0004_monitoring",
        git_commit="abc",
        remote="gdrive:simcore-backups/simcore-good.dump",
    )
    failed = merge_status(
        good,
        now=verified + timedelta(hours=1),
        attempt_ok=False,
        error="postgresql+psycopg://simcore:s3cret@127.0.0.1:5432/simcore refused",
        uploaded=False,
    )
    assert failed["last_verified_at"] == good["last_verified_at"]
    assert failed["last_attempt_ok"] is False
    assert failed["dump_name"] == "simcore-good.dump"
    assert "s3cret" not in json.dumps(failed)
    with pytest.raises(BackupError):
        merge_status(None, now=verified, attempt_ok=True, uploaded=False)


def test_backup_age_thresholds_are_strict() -> None:
    verified = _now()
    document = {"last_verified_at": verified.isoformat(), "last_attempt_ok": True, "uploaded": True}

    def judge(hours: float) -> dict:
        return evaluate_backup_status(
            document,
            problem=None,
            path_display=r"C:\simcore\backups\backup-status.json",
            now=verified + timedelta(hours=hours),
            warn_hours=26,
            critical_hours=50,
        )

    assert judge(26)["status"] == "OK"
    assert judge(26 + 1 / 3600)["status"] == "WARN"
    assert judge(50)["status"] == "WARN"
    assert judge(50 + 1 / 3600)["status"] == "CRITICAL"
    assert judge(1)["affects_overall"] is True
    missing = evaluate_backup_status(
        None,
        problem="missing",
        path_display=r"C:\simcore\backups\backup-status.json",
        now=verified,
        warn_hours=26,
        critical_hours=50,
    )
    assert missing["status"] == "UNKNOWN"
    assert missing["value"] is None
    assert missing["affects_overall"] is False
    never = evaluate_backup_status(
        {"last_verified_at": None, "last_attempt_ok": False, "last_error": "upload failed"},
        problem=None,
        path_display="status.json",
        now=verified,
        warn_hours=26,
        critical_hours=50,
    )
    assert never["status"] == "UNKNOWN"
    assert "upload failed" in never["reason"]
    ahead = evaluate_backup_status(
        document,
        problem=None,
        path_display="status.json",
        now=verified - timedelta(minutes=10),
        warn_hours=26,
        critical_hours=50,
    )
    assert ahead["status"] == "UNKNOWN"


def test_backup_thresholds_reject_a_warn_that_is_not_below_critical() -> None:
    with pytest.raises(ValidationError, match="SIMCORE_BACKUP_WARN_HOURS"):
        Settings(_env_file=None, backup_warn_hours=50, backup_critical_hours=26)


def test_drop_guards_refuse_the_live_database_name() -> None:
    with pytest.raises(BackupError):
        assert_droppable_database("simcore")
    with pytest.raises(BackupError):
        assert_droppable_database("simcore_restore_test")
    assert assert_droppable_database("simcore_drill_20260401t120000z") == "simcore_drill_20260401t120000z"
    url = get_settings().database_url
    with pytest.raises(BackupError, match="live"):
        rename_database(url, "simcore_test", "simcore_drill_nope", allow_live=False)
    with pytest.raises(BackupError, match="live"):
        create_database(url, "simcore_test")


def test_monitoring_backup_check_reads_the_status_file(client, db, tmp_path: Path) -> None:
    missing = client.get("/v1/admin/monitoring", headers=ADMIN)
    assert missing.status_code == 200, missing.text
    assert _check(missing.json(), "backup.last_success")["status"] == UNKNOWN

    path = tmp_path / "backup-status.json"
    verified = datetime.now(timezone.utc) - timedelta(hours=27)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "last_verified_at": verified.isoformat(),
                "last_attempt_ok": True,
                "uploaded": True,
                "dump_name": "simcore-example.dump",
                "remote": "gdrive:simcore-backups/simcore-example.dump",
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, backup_status_path=str(path), backup_warn_hours=26, backup_critical_hours=50)
    session = get_sessionmaker()()
    try:
        report = collect_report(session, game_now=None, settings=settings, include_api=False)
    finally:
        session.close()
    check = _check(report, "backup.last_success")
    assert check["status"] == "WARN"
    assert check["affects_overall"] is True
    assert check["value"] > 26 * 3600
    assert check["detail"]["dump_name"] == "simcore-example.dump"


def _session():
    return get_sessionmaker()()


def _consistent_ledger(session) -> None:
    city = session.scalars(select(City).order_by(City.id)).first()
    assert city is not None
    city.wood = 1010
    session.add(
        Transaction(
            player_id=city.player_id,
            city_id=city.id,
            resource="wood",
            delta=10,
            balance_after=1010,
            reason="production",
            idempotency_key="backup-consistent-wood",
            created_at=_now(),
        )
    )
    session.commit()


def test_drill_fails_unless_the_restored_world_matches(db) -> None:
    create_scenario(_now())
    session = _session()
    try:
        _consistent_ledger(session)
        create_snapshot(session, reason=SnapshotReason.MANUAL, now=_now())
        session.commit()
        facts = collect_facts(session)
        facts["database"] = "simcore_test"
        manifest = _manifest_from_facts(facts)
        passed = drill_session(session, manifest)
        assert passed["passed"] is True
        assert passed["result"] == "PASS"
        assert all(item["ok"] for item in passed["checks"] if item["required"])

        wrong_revision = dict(manifest)
        wrong_revision["alembic_revision"] = "not_a_revision"
        assert drill_session(session, wrong_revision)["passed"] is False

        payload = session.scalars(select(WorldSnapshotPayload)).first()
        assert payload is not None
        payload.body = payload.body + " "
        session.commit()
        tampered = drill_session(session, manifest)
        assert tampered["passed"] is False
        assert tampered["result"] == "FAIL"
        snapshot_check = next(item for item in tampered["checks"] if item["name"] == "snapshot.checksum")
        assert snapshot_check["ok"] is False
    finally:
        session.close()


def test_drill_reports_a_broken_ledger_chain(db) -> None:
    create_scenario(_now())
    session = _session()
    try:
        city = session.scalars(select(City).order_by(City.id)).first()
        assert city is not None
        session.add(
            Transaction(
                player_id=city.player_id,
                city_id=city.id,
                resource="wood",
                delta=10,
                balance_after=500,
                reason="production",
                idempotency_key="backup-broken-wood",
                created_at=_now(),
            )
        )
        session.commit()
        facts = collect_facts(session)
        facts["database"] = "simcore_test"
        manifest = _manifest_from_facts(facts)
        report = drill_session(session, manifest)
        assert report["passed"] is False
        ledger = next(item for item in report["checks"] if item["name"] == "ledger_conservation")
        assert ledger["ok"] is False
        assert "wood" in ledger["detail"]
    finally:
        session.close()


def _pg_tools() -> tuple[str, str]:
    pg_dump = shutil.which("pg_dump")
    pg_restore = shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        pytest.skip("pg_dump and pg_restore are not installed")
    return pg_dump, pg_restore


def test_pg_dump_restore_round_trip_and_a_changed_row_fails(db, tmp_path: Path) -> None:
    pg_dump, pg_restore = _pg_tools()
    create_scenario(_now())
    session = _session()
    try:
        _consistent_ledger(session)
        create_snapshot(session, reason=SnapshotReason.MANUAL, now=_now())
        session.commit()
    finally:
        session.close()

    url = get_settings().database_url
    dump_path = tmp_path / "simcore_test.dump"
    facts = consistent_dump(url, dump_path, pg_dump)
    assert dump_path.stat().st_size > 0
    assert "-Fc" in Path(ROOT / "src" / "simcore" / "backup.py").read_text(encoding="utf-8")
    listed = subprocess.run([pg_restore, "--list", str(dump_path)], capture_output=True, text=True, check=False)
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.strip()
    digest = sha256_file(dump_path)
    checksum = tmp_path / "simcore_test.sha256"
    checksum.write_text(f"{digest}  simcore_test.dump\n", encoding="utf-8")
    assert verify_checksum(dump_path, checksum) == digest
    assert read_checksum_file(checksum) == digest
    manifest = _manifest_from_facts(facts, size=dump_path.stat().st_size, digest=digest)
    manifest_path = tmp_path / "simcore_test.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    drill_name = f"simcore_drill_pytest{os.getpid()}"
    previous_super = os.environ.pop("POSTGRES_SUPER_PASSWORD", None)
    parts = libpq_parts(url)
    pgpass = tmp_path / "pgpass"
    write_pgpass_file(pgpass, [(parts["host"], str(parts["port"]), drill_name, parts["user"], parts["password"])])
    env = os.environ.copy()
    env["PGPASSFILE"] = str(pgpass)
    env.pop("PGPASSWORD", None)
    try:
        create_database(url, drill_name)
        restored = subprocess.run(
            [
                pg_restore,
                "-w",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--clean",
                "--if-exists",
                "-h",
                parts["host"],
                "-p",
                str(parts["port"]),
                "-U",
                parts["user"],
                "-d",
                drill_name,
                str(dump_path),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert restored.returncode == 0, redact(restored.stderr or restored.stdout)
        report = drill_database(replace_database_url(url, drill_name), manifest)
        assert report["passed"] is True, json.dumps(report, indent=2)
        assert report["result"] == "PASS"
        engine = create_engine(replace_database_url(url, drill_name))
        try:
            with Session(engine) as broken:
                broken.execute(text("UPDATE cities SET wood = wood + 1"))
                broken.commit()
        finally:
            engine.dispose()
        failed = drill_database(replace_database_url(url, drill_name), manifest)
        assert failed["passed"] is False
        assert failed["result"] == "FAIL"
        assert "PASS" != failed["result"]
    finally:
        drop_database(url, drill_name)
        if previous_super is not None:
            os.environ["POSTGRES_SUPER_PASSWORD"] = previous_super
        if pgpass.exists():
            pgpass.unlink()


def replace_database_url(url: str, database: str) -> str:
    from simcore.backup import replace_database

    return replace_database(url, database)


def test_windows_scripts_call_pg_dump_and_do_not_embed_secrets() -> None:
    deploy = ROOT / "deploy" / "windows"
    backup = (deploy / "backup.ps1").read_text(encoding="utf-8")
    restore = (deploy / "restore-backup.ps1").read_text(encoding="utf-8")
    install = (deploy / "install-backup-tools.ps1").read_text(encoding="utf-8")
    task = (deploy / "register-backup-task.ps1").read_text(encoding="utf-8")
    assert "pg_dump" in backup
    assert "-Fc" in backup or "consistent_dump" in backup or "simcore.backup" in backup
    assert "pg_restore" in backup
    assert "ReplaceLive" in restore
    assert "REPLACE LIVE" in restore
    assert "-Drill" in restore
    assert "Unregister" in task
    assert "200eb602c126d82aa38b51e0f6b9ae837473ff99b51278d3f6f837574c494d6e" in install
    for script in (backup, restore, install, task):
        assert "CHANGE_ME" not in script
        assert "ya29." not in script
        assert "POSTGRES_SUPER_PASSWORD=" not in script
