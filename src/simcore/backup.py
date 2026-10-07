"""Database backup helpers. A world snapshot is not a backup.

deploy/windows/backup.ps1 is what runs pg_dump and uploads the dump.
This module is the part that can be tested without Windows: disk budget,
manifest shape, retention, the status file the monitoring check reads,
and the restore-drill checks against a database pg_restore already loaded.

Nothing here changes combat, the ledger rules, or world_version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.engine.url import URL
from sqlalchemy.orm import Session

from simcore.constants import RESOURCES, SnapshotStatus
from simcore.models import City, WorldSnapshot
from simcore.snapshot import inspect_snapshot, world_checksum

STATUS_SCHEMA = 1
MANIFEST_SCHEMA = 1
DEFAULT_WARN_HOURS = 26.0
DEFAULT_CRITICAL_HOURS = 50.0
DEFAULT_DUMP_MARGIN = 1.5
DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024
WINDOWS_STATUS_PATH = Path(r"C:\simcore\backups\backup-status.json")
MAX_STATUS_BYTES = 1_000_000
FUTURE_SKEW_SECONDS = 120.0

# Tables a restore drill counts. Names are fixed; they are never taken from a file.
KEY_TABLES: tuple[str, ...] = (
    "world_state",
    "players",
    "cities",
    "armies",
    "player_commands",
    "movements",
    "events",
    "battle_reports",
    "transactions",
    "world_snapshots",
    "world_snapshot_payloads",
    "audit_log",
)

_DB_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_STEM = re.compile(r"^simcore-(\d{8}T\d{6}Z)-([0-9a-f]{7,40}|nogit)-([A-Za-z0-9_]+)$")
_URL_SECRET = re.compile(
    r"(postgres(?:ql)?(?:\+[A-Za-z0-9]+)?://[^:/\s]+:)[^@\s]+@",
    re.IGNORECASE,
)
_SECRET_KV = re.compile(
    r"(?i)\b(password|passwd|token|secret|pgpassword|api_key|authorization)\b(\s*[=:]\s*)(\S+)"
)
_GOOGLE_TOKEN = re.compile(r"ya29\.[A-Za-z0-9_\-]+")
_SECRET_KEY = re.compile(r"password|passwd|secret|token|pgpassword|database_url|api_key", re.IGNORECASE)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_LIVE_BLOCKED = frozenset({"postgres", "template0", "template1"})
_DRILL_PREFIX = "simcore_drill_"
_DROPPABLE_PREFIXES = ("simcore_drill_", "simcore_incoming_")


class BackupError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(redact(message))
        self.exit_code = exit_code


def redact(value: str) -> str:
    """Strip credentials that sometimes appear in driver errors."""

    cleaned = _URL_SECRET.sub(r"\1***@", value)
    cleaned = _SECRET_KV.sub(r"\1\2***", cleaned)
    cleaned = _GOOGLE_TOKEN.sub("ya29.***", cleaned)
    return cleaned


def assert_database_name(name: str) -> str:
    if not isinstance(name, str) or not _DB_NAME.match(name):
        raise BackupError(f"refusing database name {name!r}", exit_code=5)
    if name in _LIVE_BLOCKED:
        raise BackupError(f"refusing database name {name}", exit_code=5)
    return name


def assert_drill_database(name: str) -> str:
    assert_database_name(name)
    if not name.startswith(_DRILL_PREFIX):
        raise BackupError(
            f"refusing to drop {name}; only a {_DRILL_PREFIX} database can be dropped by the drill",
            exit_code=5,
        )
    return name


def assert_droppable_database(name: str) -> str:
    """Names the restore script may drop. Never the live database, postgres, or template databases."""

    assert_database_name(name)
    if not name.startswith(_DROPPABLE_PREFIXES):
        raise BackupError(
            "refusing to drop this database. Only simcore_drill_* and simcore_incoming_* "
            "can be dropped automatically.",
            exit_code=5,
        )
    return name


def parse_backup_stem(stem: str) -> datetime | None:
    match = _STEM.match(stem)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def backup_stem(*, taken_at: datetime, git_commit: str, alembic_revision: str) -> str:
    stamp = _aware(taken_at).strftime("%Y%m%dT%H%M%SZ")
    commit = git_commit.strip().lower()
    if re.fullmatch(r"[0-9a-f]{7,40}", commit):
        short = commit[:12]
    else:
        short = "nogit"
    revision = re.sub(r"[^A-Za-z0-9_]", "_", alembic_revision.strip()) or "norev"
    stem = f"simcore-{stamp}-{short}-{revision}"
    if parse_backup_stem(stem) is None:
        raise BackupError(f"backup name {stem!r} is not safe to write")
    return stem


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text_value = value.strip()
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    return _aware(parsed)


def libpq_parts(database_url: str) -> dict[str, Any]:
    """Host, port, user, database, and password. Caller must not log the password."""

    try:
        url = make_url(database_url)
    except Exception as exc:
        raise BackupError("database URL could not be parsed", exit_code=1) from exc
    host = (url.host or "").strip()
    if host not in _LOCAL_HOSTS:
        raise BackupError(
            "refusing to use a database host that is not localhost. PostgreSQL stays on 127.0.0.1.",
            exit_code=1,
        )
    database = assert_database_name(url.database or "")
    user = (url.username or "").strip()
    if not user:
        raise BackupError("database URL has no user", exit_code=1)
    password = url.password or ""
    if password == "":
        raise BackupError("database URL has no password", exit_code=1)
    port = int(url.port or 5432)
    return {
        "host": host,
        "port": port,
        "user": user,
        "database": database,
        "password": password,
    }


def connection_info(database_url: str) -> dict[str, Any]:
    parts = libpq_parts(database_url)
    return {
        "host": parts["host"],
        "port": parts["port"],
        "user": parts["user"],
        "database": parts["database"],
    }


def _pgpass_field(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def write_pgpass_file(path: Path, entries: list[tuple[str, str, str, str, str]]) -> None:
    lines = [":".join(_pgpass_field(field) for field in entry) for entry in entries]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _app_url() -> str:
    url = os.environ.get("SIMCORE_DATABASE_URL", "").strip()
    if not url:
        raise BackupError("SIMCORE_DATABASE_URL is not set", exit_code=1)
    return url


def admin_url(app_url: str) -> str:
    """Maintenance connection on localhost.

    The VPS sets POSTGRES_SUPER_PASSWORD and the role is postgres.
    CI does not, and the application user is the cluster superuser.
    """

    parts = libpq_parts(app_url)
    super_password = os.environ.get("POSTGRES_SUPER_PASSWORD", "")
    if super_password.strip():
        built = URL.create(
            drivername="postgresql+psycopg",
            username="postgres",
            password=super_password,
            host=parts["host"],
            port=parts["port"],
            database="postgres",
        )
        return built.render_as_string(hide_password=False)
    built = make_url(app_url).set(database="postgres")
    return built.render_as_string(hide_password=False)


def replace_database(database_url: str, database: str) -> str:
    assert_database_name(database)
    return make_url(database_url).set(database=database).render_as_string(hide_password=False)


def _engine(database_url: str, *, autocommit: bool = False) -> Engine:
    kwargs: dict[str, Any] = {
        "pool_pre_ping": False,
        "future": True,
        "connect_args": {"application_name": "simcore-backup"},
    }
    if autocommit:
        kwargs["isolation_level"] = "AUTOCOMMIT"
    return create_engine(database_url, **kwargs)


def estimate_dump_bytes(database_bytes: int, margin: float) -> int:
    if database_bytes < 0:
        raise BackupError("database size is negative", exit_code=2)
    if margin <= 0:
        raise BackupError("dump size margin must be greater than zero", exit_code=2)
    return int(math.ceil(database_bytes * margin))


def dump_fits(
    *,
    free_bytes: int,
    database_bytes: int,
    margin: float,
    minimum_free_bytes: int,
) -> dict[str, Any]:
    if free_bytes < 0 or minimum_free_bytes < 0:
        raise BackupError("free space values must be zero or greater", exit_code=2)
    estimated = estimate_dump_bytes(database_bytes, margin)
    remaining = free_bytes - estimated
    return {
        "ok": remaining >= minimum_free_bytes,
        "free_bytes": free_bytes,
        "database_bytes": database_bytes,
        "margin": margin,
        "estimated_bytes": estimated,
        "remaining_bytes": remaining,
        "minimum_free_bytes": minimum_free_bytes,
    }


def _reject_secret_keys(document: dict[str, Any]) -> None:
    for key in document:
        if _SECRET_KEY.search(str(key)):
            raise BackupError("manifest or status refused a secret-looking field", exit_code=1)


def parse_manifest(document: object) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise BackupError("manifest must be a JSON object", exit_code=1)
    _reject_secret_keys(document)
    required = ("time", "size_bytes", "database", "alembic_revision", "git_commit", "pg_dump_version")
    missing = [key for key in required if key not in document]
    if missing:
        raise BackupError("manifest is missing " + ", ".join(missing), exit_code=1)
    taken = _parse_time(document["time"])
    if taken is None:
        raise BackupError("manifest time is not ISO-8601", exit_code=1)
    size = document["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise BackupError("manifest size_bytes must be a non-negative integer", exit_code=1)
    database = assert_database_name(str(document["database"]))
    revision = str(document["alembic_revision"]).strip()
    commit = str(document["git_commit"]).strip()
    version = str(document["pg_dump_version"]).strip()
    if not revision or not commit or not version:
        raise BackupError("manifest revision, commit, and pg_dump version must be non-empty", exit_code=1)
    parsed: dict[str, Any] = {
        "schema": int(document.get("schema") or MANIFEST_SCHEMA),
        "time": taken.isoformat(),
        "size_bytes": size,
        "database": database,
        "alembic_revision": revision,
        "git_commit": commit,
        "pg_dump_version": version,
    }
    sha = document.get("sha256")
    if sha is not None:
        digest = str(sha).strip().lower()
        if not _HEX64.match(digest):
            raise BackupError("manifest sha256 must be 64 hex characters", exit_code=1)
        parsed["sha256"] = digest
    counts = document.get("row_counts")
    if counts is not None:
        if not isinstance(counts, dict) or not counts:
            raise BackupError("manifest row_counts must be a non-empty object", exit_code=1)
        clean: dict[str, int] = {}
        for key, value in counts.items():
            if key not in KEY_TABLES:
                raise BackupError("manifest row_counts has an unexpected table", exit_code=1)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BackupError("manifest row_counts values must be non-negative integers", exit_code=1)
            clean[str(key)] = value
        missing_tables = [table for table in KEY_TABLES if table not in clean]
        if missing_tables:
            raise BackupError("manifest row_counts is missing " + ", ".join(missing_tables), exit_code=1)
        parsed["row_counts"] = clean
    checksum = document.get("world_checksum")
    if checksum is not None:
        text_value = str(checksum).strip()
        if not text_value.startswith("sha256:") or not _HEX64.match(text_value.removeprefix("sha256:")):
            raise BackupError("manifest world_checksum must look like sha256:<64 hex>", exit_code=1)
        parsed["world_checksum"] = text_value
    return parsed


def build_manifest(
    *,
    taken_at: datetime,
    size_bytes: int,
    database: str,
    alembic_revision: str,
    git_commit: str,
    pg_dump_version: str,
    sha256: str,
    row_counts: dict[str, int],
    world_checksum_value: str,
) -> dict[str, Any]:
    document = {
        "schema": MANIFEST_SCHEMA,
        "time": _aware(taken_at).isoformat(),
        "size_bytes": size_bytes,
        "database": database,
        "alembic_revision": alembic_revision,
        "git_commit": git_commit,
        "pg_dump_version": pg_dump_version,
        "sha256": sha256.lower(),
        "row_counts": row_counts,
        "world_checksum": world_checksum_value,
        "dump_format": "custom",
    }
    return parse_manifest(document)


def retention_plan(names: list[str], *, keep_daily: int, keep_weekly: int) -> dict[str, list[str]]:
    """Keep the newest backup on each of the latest days and ISO weeks.

    Names that are not simcore backup stems are omitted from both lists so a
    prune pass does not delete them.
    """

    if keep_daily < 1:
        raise BackupError("keep_daily must be at least 1", exit_code=1)
    if keep_weekly < 0:
        raise BackupError("keep_weekly must be zero or greater", exit_code=1)
    parsed: list[tuple[datetime, str]] = []
    seen: set[str] = set()
    for name in names:
        stem = name.strip()
        taken = parse_backup_stem(stem)
        if taken is None or stem in seen:
            continue
        seen.add(stem)
        parsed.append((taken, stem))
    anchor = max(taken for taken, _stem in parsed)
    daily_cutoff = (anchor - timedelta(days=keep_daily - 1)).date()
    week_cutoff = anchor - timedelta(weeks=keep_weekly - 1)
    by_date: dict[Any, tuple[datetime, str]] = {}
    by_week: dict[tuple[int, int], tuple[datetime, str]] = {}
    for taken, stem in parsed:
        if taken.date() >= daily_cutoff:
            current = by_date.get(taken.date())
            if current is None or taken > current[0]:
                by_date[taken.date()] = (taken, stem)
        if taken >= week_cutoff:
            iso = taken.isocalendar()
            week = (int(iso.year), int(iso.week))
            current_week = by_week.get(week)
            if current_week is None or taken > current_week[0]:
                by_week[week] = (taken, stem)
    keep = {item[1] for item in by_date.values()}
    keep.update(item[1] for item in by_week.values())
    delete = sorted(stem for _, stem in parsed if stem not in keep)
    return {"keep": sorted(keep), "delete": delete}


def local_retention(names: list[str], *, keep: int) -> dict[str, list[str]]:
    if keep < 1:
        raise BackupError("local keep count must be at least 1", exit_code=1)
    parsed: list[tuple[datetime, str]] = []
    seen: set[str] = set()
    for name in names:
        stem = name.strip()
        taken = parse_backup_stem(stem)
        if taken is None or stem in seen:
            continue
        seen.add(stem)
        parsed.append((taken, stem))
    parsed.sort(key=lambda item: item[0], reverse=True)
    kept = [stem for _, stem in parsed[:keep]]
    delete = [stem for _, stem in parsed[keep:]]
    if parsed and parsed[0][1] not in kept:
        raise BackupError("local retention tried to delete the newest backup", exit_code=1)
    return {"keep": kept, "delete": delete}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def read_checksum_file(path: Path) -> str:
    text_value = path.read_text(encoding="utf-8").strip()
    if not text_value:
        raise BackupError("checksum file is empty", exit_code=5)
    token = text_value.split()[0].strip().lower()
    if not _HEX64.match(token):
        raise BackupError("checksum file does not start with a SHA-256 hex digest", exit_code=5)
    return token


def verify_checksum(dump_path: Path, checksum_path: Path) -> str:
    expected = read_checksum_file(checksum_path)
    actual = sha256_file(dump_path)
    if actual != expected:
        raise BackupError("dump checksum does not match the checksum file", exit_code=5)
    return actual


def load_status_document(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return (document, problem). problem is a short reason with no file contents."""

    try:
        if not path.is_file():
            return None, "missing"
        size = path.stat().st_size
    except OSError:
        return None, "unreadable"
    if size > MAX_STATUS_BYTES:
        return None, "too_large"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "unreadable"
    if not isinstance(parsed, dict):
        return None, "not_object"
    return parsed, None


def merge_status(
    previous: dict[str, Any] | None,
    *,
    now: datetime,
    attempt_ok: bool,
    error: str = "",
    database: str = "",
    dump_name: str = "",
    size_bytes: int | None = None,
    sha256: str = "",
    alembic_revision: str = "",
    git_commit: str = "",
    remote: str = "",
    uploaded: bool = False,
) -> dict[str, Any]:
    prev = previous if isinstance(previous, dict) else {}
    verified = prev.get("last_verified_at") if isinstance(prev.get("last_verified_at"), str) else None
    if attempt_ok:
        if not uploaded:
            raise BackupError("a successful status requires uploaded=true", exit_code=1)
        verified = _aware(now).isoformat()
        stored_dump = dump_name
        stored_size = size_bytes
        stored_sha = sha256.lower()
        stored_remote = remote
        stored_uploaded = True
        stored_error = None
    else:
        stored_dump = str(prev.get("dump_name") or "")
        stored_size = prev.get("size_bytes") if isinstance(prev.get("size_bytes"), int) else None
        stored_sha = str(prev.get("sha256") or "")
        stored_remote = str(prev.get("remote") or "")
        stored_uploaded = bool(prev.get("uploaded")) and bool(verified)
        stored_error = redact(error or "backup failed")[:500]
    document = {
        "schema": STATUS_SCHEMA,
        "last_verified_at": verified,
        "last_attempt_at": _aware(now).isoformat(),
        "last_attempt_ok": bool(attempt_ok),
        "last_error": stored_error,
        "database": database or str(prev.get("database") or ""),
        "dump_name": stored_dump,
        "size_bytes": stored_size,
        "sha256": stored_sha,
        "alembic_revision": alembic_revision or str(prev.get("alembic_revision") or ""),
        "git_commit": git_commit or str(prev.get("git_commit") or ""),
        "remote": stored_remote,
        "uploaded": stored_uploaded,
    }
    _reject_secret_keys(document)
    return document


def write_status_file(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def status_path_for(configured: str) -> Path | None:
    raw = configured.strip()
    if raw:
        return Path(raw)
    if os.name == "nt":
        return WINDOWS_STATUS_PATH
    return None


def evaluate_backup_status(
    document: dict[str, Any] | None,
    *,
    problem: str | None,
    path_display: str,
    now: datetime,
    warn_hours: float,
    critical_hours: float,
) -> dict[str, Any]:
    """Age of the last verified off-site backup. Missing data is UNKNOWN, never OK."""

    warn_seconds = warn_hours * 3600.0
    critical_seconds = critical_hours * 3600.0
    threshold = {
        "warn": warn_seconds,
        "critical": critical_seconds,
        "unit": "seconds",
        "comparison": "higher_is_worse",
    }
    detail: dict[str, Any] = {"path": path_display, "warn_hours": warn_hours, "critical_hours": critical_hours}
    if problem == "unset" or (document is None and problem is None and not path_display):
        return _status_body(
            "UNKNOWN",
            None,
            "SIMCORE_BACKUP_STATUS_PATH is not set, so no off-site backup has been measured. This is not a pass.",
            False,
            threshold,
            detail,
        )
    if document is None:
        reason = {
            "missing": f"No backup status file at {path_display}. backup.ps1 has not recorded a verified off-site copy. This is not a pass.",
            "unreadable": f"Backup status file at {path_display} could not be read. This is not a pass.",
            "too_large": f"Backup status file at {path_display} is larger than expected. This is not a pass.",
            "not_object": f"Backup status file at {path_display} is not a JSON object. This is not a pass.",
        }.get(problem or "missing", f"Backup status at {path_display} is unavailable. This is not a pass.")
        return _status_body("UNKNOWN", None, reason, False, threshold, detail)
    verified = _parse_time(document.get("last_verified_at"))
    detail["last_attempt_at"] = document.get("last_attempt_at")
    detail["last_attempt_ok"] = document.get("last_attempt_ok")
    detail["dump_name"] = document.get("dump_name") or None
    detail["remote"] = document.get("remote") or None
    detail["uploaded"] = document.get("uploaded")
    last_error = document.get("last_error")
    if isinstance(last_error, str) and last_error.strip():
        detail["last_error"] = redact(last_error)[:500]
    if verified is None:
        extra = f" Last attempt said: {detail['last_error']}" if detail.get("last_error") else ""
        return _status_body(
            "UNKNOWN",
            None,
            "Backup status has no last_verified_at. A verified off-site backup has not been recorded. This is not a pass."
            + extra,
            False,
            threshold,
            detail,
        )
    age = (_aware(now) - verified).total_seconds()
    detail["last_verified_at"] = verified.isoformat()
    if age < -FUTURE_SKEW_SECONDS:
        return _status_body(
            "UNKNOWN",
            age,
            "last_verified_at is ahead of this clock by more than two minutes. The age was not treated as fresh.",
            False,
            threshold,
            detail,
        )
    if age < 0:
        age = 0.0
    if age > critical_seconds:
        status = "CRITICAL"
        how = "CRITICAL"
    elif age > warn_seconds:
        status = "WARN"
        how = "WARN"
    else:
        status = "OK"
        how = "OK"
    hours = age / 3600.0
    return _status_body(
        status,
        age,
        (
            f"{how}: last verified off-site backup is {hours:.2f} hours old "
            f"({verified.isoformat()}); warn after {warn_hours:g}h, critical after {critical_hours:g}h."
        ),
        True,
        threshold,
        detail,
    )


def _status_body(
    status: str,
    value: float | None,
    reason: str,
    affects: bool,
    threshold: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": status,
        "value": value,
        "unit": "seconds",
        "reason": reason,
        "affects_overall": affects,
        "threshold": threshold,
        "detail": detail,
    }


def evaluate_backup_check(configured_path: str, *, now: datetime, warn_hours: float, critical_hours: float) -> dict[str, Any]:
    path = status_path_for(configured_path)
    if path is None:
        return evaluate_backup_status(
            None,
            problem="unset",
            path_display="",
            now=now,
            warn_hours=warn_hours,
            critical_hours=critical_hours,
        )
    document, problem = load_status_document(path)
    return evaluate_backup_status(
        document,
        problem=problem,
        path_display=str(path),
        now=now,
        warn_hours=warn_hours,
        critical_hours=critical_hours,
    )


def _alembic_revision(session: Session) -> str | None:
    try:
        rows = session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    except Exception:
        session.rollback()
        return None
    values = sorted({str(row).strip() for row in rows if str(row).strip()})
    if not values:
        return None
    return ",".join(values)


def _table_counts(session: Session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in KEY_TABLES:
        counts[table] = int(session.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one())
    return counts


def collect_facts(session: Session) -> dict[str, Any]:
    revision = _alembic_revision(session)
    if revision is None:
        raise BackupError("alembic_version could not be read", exit_code=3)
    return {
        "alembic_revision": revision,
        "row_counts": _table_counts(session),
        "world_checksum": world_checksum(session),
    }


def ledger_failures(session: Session) -> list[str]:
    """The ledger chain for each city and resource, checked against the city column.

    An empty ledger is consistent. A broken chain or a last balance that does
    not match the city is not.
    """

    rows = session.execute(
        text(
            "SELECT id, city_id, resource, delta, balance_after "
            "FROM transactions ORDER BY id"
        )
    ).all()
    grouped: dict[tuple[int | None, str], list[Any]] = defaultdict(list)
    failures: list[str] = []
    for row in rows:
        resource = str(row.resource)
        if resource not in RESOURCES:
            failures.append(f"transaction {row.id} uses unknown resource {resource}")
            continue
        if int(row.balance_after) < 0:
            failures.append(f"transaction {row.id} has a negative balance_after")
        grouped[(None if row.city_id is None else int(row.city_id), resource)].append(row)
    for (city_id, resource), chain in grouped.items():
        previous: int | None = None
        for row in chain:
            balance = int(row.balance_after)
            delta = int(row.delta)
            if previous is not None and previous + delta != balance:
                failures.append(
                    f"city {city_id} {resource}: transaction {row.id} balance_after {balance} "
                    f"!= previous {previous} + delta {delta}"
                )
            previous = balance
        if city_id is None or previous is None:
            continue
        city = session.get(City, city_id)
        if city is None:
            failures.append(f"transaction refers to missing city {city_id}")
            continue
        current = int(getattr(city, resource))
        if current != previous:
            failures.append(
                f"city {city_id} {resource}: column {current} != last ledger balance_after {previous}"
            )
    return failures


def _check(name: str, ok: bool, detail: str, *, required: bool = True) -> dict[str, Any]:
    return {"name": name, "ok": ok, "required": required, "detail": detail}


def drill_session(session: Session, manifest_document: dict[str, Any]) -> dict[str, Any]:
    """Compare a restored database to the manifest. PASS only when every required check matches."""

    manifest = parse_manifest(manifest_document)
    checks: list[dict[str, Any]] = []
    revision = _alembic_revision(session)
    expected_revision = str(manifest["alembic_revision"])
    if revision is None:
        checks.append(_check("alembic_revision", False, "alembic_version could not be read"))
    else:
        checks.append(
            _check(
                "alembic_revision",
                revision == expected_revision,
                f"database {revision}; manifest {expected_revision}",
            )
        )
    expected_counts = manifest.get("row_counts")
    if not isinstance(expected_counts, dict):
        checks.append(_check("row_counts", False, "manifest has no row_counts"))
    else:
        try:
            actual_counts = _table_counts(session)
        except Exception as exc:
            checks.append(_check("row_counts", False, f"count query failed: {exc.__class__.__name__}"))
        else:
            mismatched = [
                f"{table} database {actual_counts[table]} != manifest {expected_counts[table]}"
                for table in KEY_TABLES
                if table not in expected_counts or actual_counts[table] != expected_counts[table]
            ]
            checks.append(
                _check(
                    "row_counts",
                    not mismatched,
                    "row counts match" if not mismatched else "; ".join(mismatched),
                )
            )
    expected_checksum = manifest.get("world_checksum")
    if not isinstance(expected_checksum, str):
        checks.append(_check("world_checksum", False, "manifest has no world_checksum"))
    else:
        try:
            live = world_checksum(session)
        except Exception as exc:
            checks.append(_check("world_checksum", False, f"world checksum failed: {exc.__class__.__name__}"))
        else:
            checks.append(
                _check(
                    "world_checksum",
                    live == expected_checksum,
                    "world checksum matches the manifest" if live == expected_checksum else f"{live} != {expected_checksum}",
                )
            )
    try:
        failures = ledger_failures(session)
    except Exception as exc:
        checks.append(_check("ledger_conservation", False, f"ledger check failed: {exc.__class__.__name__}"))
    else:
        checks.append(
            _check(
                "ledger_conservation",
                not failures,
                "ledger chain matches city balances" if not failures else "; ".join(failures),
            )
        )
    try:
        latest = session.scalars(
            select(WorldSnapshot)
            .where(WorldSnapshot.status == SnapshotStatus.READY)
            .order_by(WorldSnapshot.id.desc())
            .limit(1)
        ).first()
    except Exception as exc:
        checks.append(_check("snapshot.checksum", False, f"snapshot query failed: {exc.__class__.__name__}"))
    else:
        if latest is None:
            checks.append(
                _check(
                    "snapshot.checksum",
                    True,
                    "No READY world snapshot in this database. The manifest world checksum was still compared.",
                    required=False,
                )
            )
        else:
            try:
                inspected = inspect_snapshot(session, int(latest.id))
            except Exception as exc:
                checks.append(
                    _check("snapshot.checksum", False, f"snapshot {latest.id} could not be inspected: {exc.__class__.__name__}")
                )
            else:
                ok = bool(inspected.get("checksum_ok")) and bool(inspected.get("summary_ok"))
                checks.append(
                    _check(
                        "snapshot.checksum",
                        ok,
                        f"snapshot {latest.id} checksum_ok={inspected.get('checksum_ok')} summary_ok={inspected.get('summary_ok')}",
                    )
                )
    required = [item for item in checks if item["required"]]
    passed = bool(required) and all(bool(item["ok"]) for item in required)
    return {"passed": passed, "result": "PASS" if passed else "FAIL", "checks": checks}


def drill_database(database_url: str, manifest_document: dict[str, Any]) -> dict[str, Any]:
    libpq_parts(database_url)
    engine = _engine(database_url)
    try:
        with Session(engine) as session:
            return drill_session(session, manifest_document)
    finally:
        engine.dispose()


def database_size_bytes(database_url: str) -> int:
    engine = _engine(database_url)
    try:
        with engine.connect() as conn:
            size = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
        return int(size)
    finally:
        engine.dispose()


def _pgpass_for(parts: dict[str, Any], path: Path) -> None:
    write_pgpass_file(
        path,
        [(parts["host"], str(parts["port"]), parts["database"], parts["user"], parts["password"])],
    )


def pg_dump_version(pg_dump: str) -> str:
    completed = subprocess.run([pg_dump, "--version"], capture_output=True, text=True, check=False)
    text_value = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0 or not text_value:
        raise BackupError("pg_dump --version failed", exit_code=3)
    return text_value[:200]


def consistent_dump(database_url: str, dump_path: Path, pg_dump: str) -> dict[str, Any]:
    """pg_dump -Fc of one repeatable-read snapshot, plus facts from that snapshot.

    The exporting transaction stays open until pg_dump returns, then it commits.
    Game services are not stopped.
    """

    parts = libpq_parts(database_url)
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    version = pg_dump_version(pg_dump)
    engine = _engine(database_url)
    pgpass = dump_path.with_suffix(dump_path.suffix + ".pgpass")
    succeeded = False
    try:
        _pgpass_for(parts, pgpass)
        with engine.connect() as conn:
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
            with conn.begin():
                snapshot = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
                if not isinstance(snapshot, str) or not snapshot.strip():
                    raise BackupError("pg_export_snapshot returned nothing", exit_code=3)
                session = Session(bind=conn)
                try:
                    facts = collect_facts(session)
                finally:
                    session.close()
                env = os.environ.copy()
                env["PGPASSFILE"] = str(pgpass)
                env.pop("PGPASSWORD", None)
                command = [
                    pg_dump,
                    "-Fc",
                    "-Z",
                    "6",
                    "--snapshot",
                    snapshot,
                    "-w",
                    "-h",
                    parts["host"],
                    "-p",
                    str(parts["port"]),
                    "-U",
                    parts["user"],
                    "-d",
                    parts["database"],
                    "-f",
                    str(dump_path),
                ]
                completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
                if completed.returncode != 0:
                    detail = redact((completed.stderr or completed.stdout or "").strip())[:500]
                    raise BackupError(f"pg_dump failed (exit {completed.returncode}). {detail}", exit_code=3)
        facts["pg_dump_version"] = version
        facts["database"] = parts["database"]
        facts["snapshot"] = snapshot
        succeeded = True
        return facts
    finally:
        engine.dispose()
        if pgpass.exists():
            pgpass.unlink()
        if not succeeded and dump_path.exists():
            dump_path.unlink()


def _admin_execute(app_url: str, statement: str, params: dict[str, Any] | None = None) -> None:
    engine = _engine(admin_url(app_url), autocommit=True)
    try:
        with engine.connect() as conn:
            conn.execute(text(statement), params or {})
    finally:
        engine.dispose()


def _terminate(app_url: str, database: str) -> None:
    assert_database_name(database)
    _admin_execute(
        app_url,
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = :name AND pid <> pg_backend_pid()",
        {"name": database},
    )


def create_database(app_url: str, name: str, *, owner: str = "simcore") -> None:
    assert_database_name(name)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", owner):
        raise BackupError("refusing database owner", exit_code=5)
    live = libpq_parts(app_url)["database"]
    if name == live:
        raise BackupError(f"refusing to create {name} because it is the live database name", exit_code=5)
    _admin_execute(app_url, f'CREATE DATABASE "{name}" OWNER "{owner}" TEMPLATE template0')


def drop_database(app_url: str, name: str) -> None:
    assert_droppable_database(name)
    live = libpq_parts(app_url)["database"]
    if name == live:
        raise BackupError("refusing to drop the live database", exit_code=5)
    _terminate(app_url, name)
    _admin_execute(app_url, f'DROP DATABASE IF EXISTS "{name}"')


def rename_database(app_url: str, source: str, dest: str, *, allow_live: bool) -> None:
    assert_database_name(source)
    assert_database_name(dest)
    live = libpq_parts(app_url)["database"]
    if (source == live or dest == live) and not allow_live:
        raise BackupError("refusing to rename the live database without allow_live", exit_code=5)
    _terminate(app_url, source)
    _admin_execute(app_url, f'ALTER DATABASE "{source}" RENAME TO "{dest}"')


def database_exists(app_url: str, name: str) -> bool:
    assert_database_name(name)
    engine = _engine(admin_url(app_url), autocommit=True)
    try:
        with engine.connect() as conn:
            found = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}).scalar()
        return found == 1
    finally:
        engine.dispose()


def _print_json(document: object) -> None:
    sys.stdout.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simcore.backup")
    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("connection-info")
    info.add_argument("--database-url-env", default="SIMCORE_DATABASE_URL")

    size = sub.add_parser("database-size")
    size.add_argument("--database-url-env", default="SIMCORE_DATABASE_URL")

    disk = sub.add_parser("disk-budget")
    disk.add_argument("--free", type=int, required=True)
    disk.add_argument("--database-bytes", type=int, required=True)
    disk.add_argument("--margin", type=float, required=True)
    disk.add_argument("--minimum-free", type=int, required=True)

    dump = sub.add_parser("dump")
    dump.add_argument("--pg-dump", required=True)
    dump.add_argument("--output", required=True)
    dump.add_argument("--facts", required=True)

    manifest = sub.add_parser("build-manifest")
    manifest.add_argument("--facts", required=True)
    manifest.add_argument("--output", required=True)
    manifest.add_argument("--time", required=True)
    manifest.add_argument("--size-bytes", type=int, required=True)
    manifest.add_argument("--sha256", required=True)
    manifest.add_argument("--git-commit", required=True)

    checksum = sub.add_parser("verify-checksum")
    checksum.add_argument("--dump", required=True)
    checksum.add_argument("--checksum", required=True)

    status = sub.add_parser("write-status")
    status.add_argument("--path", required=True)
    status.add_argument("--ok", action="store_true")
    status.add_argument("--error", default="")
    status.add_argument("--database", default="")
    status.add_argument("--dump-name", default="")
    status.add_argument("--size-bytes", type=int, default=-1)
    status.add_argument("--sha256", default="")
    status.add_argument("--alembic-revision", default="")
    status.add_argument("--git-commit", default="")
    status.add_argument("--remote", default="")
    status.add_argument("--uploaded", action="store_true")

    drill = sub.add_parser("drill")
    drill.add_argument("--manifest", required=True)
    drill.add_argument("--database-name", default="")
    drill.add_argument("--database-url-env", default="SIMCORE_DRILL_DATABASE_URL")

    retention = sub.add_parser("retention")
    retention.add_argument("--keep-daily", type=int, required=True)
    retention.add_argument("--keep-weekly", type=int, required=True)
    retention.add_argument("--names-file", default="")

    local = sub.add_parser("local-retention")
    local.add_argument("--keep", type=int, required=True)
    local.add_argument("--names-file", default="")

    created = sub.add_parser("create-database")
    created.add_argument("--name", required=True)
    created.add_argument("--owner", default="simcore")

    dropped = sub.add_parser("drop-database")
    dropped.add_argument("--name", required=True)

    renamed = sub.add_parser("rename-database")
    renamed.add_argument("--source", required=True)
    renamed.add_argument("--dest", required=True)
    renamed.add_argument("--allow-live", action="store_true")

    exists = sub.add_parser("database-exists")
    exists.add_argument("--name", required=True)

    pgpass = sub.add_parser("write-pgpass")
    pgpass.add_argument("--output", required=True)
    pgpass.add_argument("--superuser", action="store_true")
    pgpass.add_argument("--database", default="")

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except BackupError as exc:
        sys.stderr.write(redact(str(exc)) + "\n")
        return exc.exit_code
    except Exception as exc:
        sys.stderr.write(redact(f"{exc.__class__.__name__}: {exc}") + "\n")
        return 1


def _env_url(name: str) -> str:
    url = os.environ.get(name, "").strip()
    if not url:
        raise BackupError(f"{name} is not set", exit_code=1)
    return url


def _dispatch(args: argparse.Namespace) -> int:
    command = args.command
    if command == "connection-info":
        _print_json(connection_info(_env_url(args.database_url_env)))
        return 0
    if command == "database-size":
        sys.stdout.write(str(database_size_bytes(_env_url(args.database_url_env))) + "\n")
        return 0
    if command == "disk-budget":
        budget = dump_fits(
            free_bytes=args.free,
            database_bytes=args.database_bytes,
            margin=args.margin,
            minimum_free_bytes=args.minimum_free,
        )
        _print_json(budget)
        if not budget["ok"]:
            sys.stderr.write(
                "Not enough free disk for this dump. "
                f"FreeBytes={budget['free_bytes']} EstimatedDumpBytes={budget['estimated_bytes']} "
                f"RemainingBytes={budget['remaining_bytes']} MinimumFreeBytes={budget['minimum_free_bytes']}. "
                "Refusing to dump. Free space or lower the local retention before trying again.\n"
            )
            return 2
        return 0
    if command == "dump":
        facts = consistent_dump(_app_url(), Path(args.output), args.pg_dump)
        Path(args.facts).write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sys.stdout.write(facts.get("snapshot", "") + "\n")
        return 0
    if command == "build-manifest":
        facts = _load_json(Path(args.facts))
        if not isinstance(facts, dict):
            raise BackupError("facts file is not a JSON object", exit_code=1)
        taken = _parse_time(args.time)
        if taken is None:
            raise BackupError("manifest time is not ISO-8601", exit_code=1)
        document = build_manifest(
            taken_at=taken,
            size_bytes=args.size_bytes,
            database=str(facts["database"]),
            alembic_revision=str(facts["alembic_revision"]),
            git_commit=args.git_commit,
            pg_dump_version=str(facts["pg_dump_version"]),
            sha256=args.sha256,
            row_counts=facts["row_counts"],
            world_checksum_value=str(facts["world_checksum"]),
        )
        Path(args.output).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    if command == "verify-checksum":
        digest = verify_checksum(Path(args.dump), Path(args.checksum))
        sys.stdout.write(digest + "\n")
        return 0
    if command == "write-status":
        path = Path(args.path)
        previous, _problem = load_status_document(path)
        size = None if args.size_bytes < 0 else args.size_bytes
        document = merge_status(
            previous,
            now=datetime.now(timezone.utc),
            attempt_ok=bool(args.ok),
            error=args.error,
            database=args.database,
            dump_name=args.dump_name,
            size_bytes=size,
            sha256=args.sha256,
            alembic_revision=args.alembic_revision,
            git_commit=args.git_commit,
            remote=args.remote,
            uploaded=bool(args.uploaded),
        )
        write_status_file(path, document)
        return 0
    if command == "drill":
        manifest = _load_json(Path(args.manifest))
        if not isinstance(manifest, dict):
            raise BackupError("manifest is not a JSON object", exit_code=1)
        if args.database_name.strip():
            drill_url = replace_database(_app_url(), args.database_name.strip())
        else:
            drill_url = _env_url(args.database_url_env)
        report = drill_database(drill_url, manifest)
        sys.stdout.write(("PASS" if report["passed"] else "FAIL") + "\n")
        _print_json(report)
        return 0 if report["passed"] else 1
    if command in ("retention", "local-retention"):
        if args.names_file:
            raw = Path(args.names_file).read_text(encoding="utf-8")
        else:
            raw = sys.stdin.read() or "[]"
        names = json.loads(raw)
        if not isinstance(names, list):
            raise BackupError("retention input must be a JSON list of names", exit_code=1)
        if command == "retention":
            _print_json(retention_plan([str(item) for item in names], keep_daily=args.keep_daily, keep_weekly=args.keep_weekly))
        else:
            _print_json(local_retention([str(item) for item in names], keep=args.keep))
        return 0
    if command == "create-database":
        create_database(_app_url(), args.name, owner=args.owner)
        return 0
    if command == "drop-database":
        drop_database(_app_url(), args.name)
        return 0
    if command == "rename-database":
        rename_database(_app_url(), args.source, args.dest, allow_live=bool(args.allow_live))
        return 0
    if command == "database-exists":
        sys.stdout.write("yes\n" if database_exists(_app_url(), args.name) else "no\n")
        return 0
    if command == "write-pgpass":
        app = _app_url()
        parts = libpq_parts(app)
        database = args.database.strip() or parts["database"]
        if args.superuser:
            password = os.environ.get("POSTGRES_SUPER_PASSWORD", "")
            if not password.strip():
                raise BackupError("POSTGRES_SUPER_PASSWORD is not set", exit_code=1)
            database = args.database.strip() or "*"
            write_pgpass_file(
                Path(args.output),
                [(parts["host"], str(parts["port"]), database, "postgres", password)],
            )
        else:
            write_pgpass_file(
                Path(args.output),
                [(parts["host"], str(parts["port"]), database, parts["user"], parts["password"])],
            )
        return 0
    raise BackupError(f"unknown command {command}", exit_code=1)


if __name__ == "__main__":
    sys.exit(main())
