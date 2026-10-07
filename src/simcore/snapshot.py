"""World snapshots: capture, checksum, and safe restore.

A snapshot is a deterministic copy of the simulation (players, cities, armies,
movements, events, battle reports, the ledger, and world_state clock fields).
It is not a PostgreSQL backup and it does not replace pg_dump.

Checksum
--------
sha256:<64 lowercase hex> of the UTF-8 canonical JSON document.

canonical JSON is json.dumps(document, ensure_ascii=False, sort_keys=True,
separators=(",", ":"), allow_nan=False). Datetimes are timezone-aware UTC
ISO-8601 strings (offset +00:00) before encoding. Lists keep primary-key order.
Dict key order does not matter because sort_keys is on.

The document covers schema_version, world_state (id, offset_seconds,
world_version), and every column of players, cities, armies, player_commands,
movements, events, battle_reports, and transactions. Nullable trace_id columns
are part of that document. It does not cover snapshot rows, the audit log,
player accounts, refresh sessions, command idempotency keys, worker heartbeats,
process marks, monitoring samples, monitoring check state, commands_open, or
worker_paused. Restore deletes command idempotency keys so a replay cannot
return a result from the pre-restore world. It does not delete accounts or
refresh sessions. world_time on the snapshot row is metadata
(the simulated clock at capture) and is not hashed; offset_seconds is the
hashed clock state.

Create and restore both call world_checksum, which hashes capture_document.

Safe restore
------------
1. Close player commands and pause the worker (committed before any rewrite).
2. Wait until in-flight worker transactions release the shared drain lock and
   no event is left in processing.
3. Write a SAFETY snapshot of the current world.
4. Verify the target: status READY, schema_version matches this server, and
   sha256(stored payload) equals the stored checksum. A checksum mismatch marks
   the target FAILED and does not apply it.
5. In one transaction, replace the simulation rows and world_state clock fields.
6. Recompute the checksum. On mismatch, roll the transaction back.
7. Unpause the worker.
8. Open player commands.

If a step fails before the replacement commits, commands are opened again when
the live checksum still matches the safety snapshot (or when that snapshot was
never created, because only the gates changed). If the replacement committed
but reopening the gates fails, the gates stay closed. Snapshot rows are never
deleted by restore.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.sql.sqltypes import DateTime

from simcore.constants import (
    SNAPSHOT_SCHEMA_VERSION,
    EventStatus,
    SnapshotReason,
    SnapshotStatus,
)
from simcore.errors import GameError
from simcore.models import (
    Army,
    BattleReport,
    City,
    CommandIdempotency,
    Event,
    Movement,
    Player,
    PlayerCommand,
    Transaction,
    WorldSnapshot,
    WorldSnapshotPayload,
    WorldState,
    utcnow,
)
from simcore.world import WORKER_DRAIN_LOCK

logger = logging.getLogger("simcore.snapshot")

_LOCK_TIMEOUT = "5s"
_DRAIN_SECONDS = 5.0

_OPERATIONAL_COLUMNS = frozenset({"commands_open", "worker_paused", "restore_active"})

# Order is parents-before-children on insert and the reverse on delete.
_ENTITY_MODELS: tuple[tuple[str, type], ...] = (
    ("players", Player),
    ("cities", City),
    ("armies", Army),
    ("player_commands", PlayerCommand),
    ("movements", Movement),
    ("events", Event),
    ("battle_reports", BattleReport),
    ("transactions", Transaction),
)
_DELETE_ORDER: tuple[type, ...] = (
    Transaction,
    BattleReport,
    Event,
    Movement,
    PlayerCommand,
    Army,
    City,
    Player,
)
_SEQUENCE_TABLES: tuple[str, ...] = tuple(name for name, _model in _ENTITY_MODELS)


def canonical_json(document: object) -> str:
    """Stable JSON. Object keys are sorted at every level; list order is kept."""

    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def checksum_text(body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canon_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise GameError(f"{label} must be an ISO-8601 string", code="snapshot_schema")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GameError(f"{label} is not ISO-8601", code="snapshot_schema") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _columns(model: type, *, exclude: frozenset[str] = frozenset()) -> list[Any]:
    return [column for column in model.__table__.columns if column.key not in exclude]


def _jsonable(column: Any, value: object) -> object:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        if not isinstance(value, datetime):
            raise TypeError(f"{column.key} is not a datetime")
        return _canon_dt(value)
    return value


def _python_value(column: Any, value: object, *, label: str) -> object:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return _parse_dt(value, label=label)
    return value


def capture_document(session: Session) -> dict[str, Any]:
    """JSON-ready world document. Does not lock and does not write."""

    state = session.get(WorldState, 1)
    if state is None:
        raise RuntimeError("world_state row is missing; run migrations")
    world_columns = _columns(WorldState, exclude=_OPERATIONAL_COLUMNS)
    document: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "world_state": {column.key: _jsonable(column, getattr(state, column.key)) for column in world_columns},
    }
    for name, model in _ENTITY_MODELS:
        columns = _columns(model)
        rows = session.scalars(select(model).order_by(model.id)).all()
        document[name] = [
            {column.key: _jsonable(column, getattr(row, column.key)) for column in columns} for row in rows
        ]
    return document


def summarize(document: dict[str, Any]) -> dict[str, int]:
    return {name: len(document[name]) for name, _model in _ENTITY_MODELS}


def world_checksum(session: Session) -> str:
    """Checksum of the live world. Same function create and restore use."""

    return checksum_text(canonical_json(capture_document(session)))


def _lock_world_tables(session: Session) -> None:
    session.execute(
        text(
            "LOCK TABLE world_state, players, cities, armies, player_commands, movements, events, "
            "battle_reports, transactions IN SHARE ROW EXCLUSIVE MODE"
        )
    )


def snapshot_metadata(row: WorldSnapshot) -> dict[str, object]:
    return {
        "snapshot_id": row.id,
        "created_at": row.created_at,
        "world_time": row.world_time,
        "schema_version": row.schema_version,
        "world_version": row.world_version,
        "checksum": row.checksum,
        "reason": row.reason,
        "status": row.status,
        "summary": row.summary,
        "error": row.error,
    }


def create_snapshot(session: Session, *, reason: str, now: datetime) -> WorldSnapshot:
    """Capture the live world. Caller commits.

    reason SAFETY is for the restore flow. The admin create endpoint refuses it.
    """

    if reason not in SnapshotReason.ALL:
        raise GameError(
            "reason must be AUTO, MANUAL, or SAFETY",
            code="invalid_command",
        )
    _lock_world_tables(session)
    session.flush()
    document = capture_document(session)
    body = canonical_json(document)
    row = WorldSnapshot(
        created_at=utcnow(),
        world_time=now,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        world_version=int(document["world_state"]["world_version"]),
        checksum=checksum_text(body),
        reason=reason,
        status=SnapshotStatus.CREATING,
        summary=summarize(document),
        error=None,
    )
    session.add(row)
    session.flush()
    session.add(WorldSnapshotPayload(snapshot_id=row.id, body=body))
    row.status = SnapshotStatus.READY
    session.flush()
    logger.info(
        "snapshot %s ready reason=%s checksum=%s",
        row.id,
        row.reason,
        row.checksum,
    )
    return row


def list_snapshots(session: Session, *, limit: int) -> list[WorldSnapshot]:
    return list(
        session.scalars(select(WorldSnapshot).order_by(WorldSnapshot.id.desc()).limit(limit)).all()
    )


def get_snapshot(session: Session, snapshot_id: int) -> WorldSnapshot:
    row = session.get(WorldSnapshot, snapshot_id)
    if row is None:
        raise GameError("snapshot not found", status_code=404, code="not_found")
    return row


def inspect_snapshot(session: Session, snapshot_id: int) -> dict[str, object]:
    """Recompute counts and the payload checksum without restoring."""

    row = get_snapshot(session, snapshot_id)
    payload = session.get(WorldSnapshotPayload, snapshot_id)
    body = payload.body if payload is not None else ""
    payload_checksum = checksum_text(body) if payload is not None else None
    counts: dict[str, int] | None = None
    canonical_ok = False
    world_state: object = None
    if payload is not None:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            canonical_ok = canonical_json(parsed) == body
            if all(name in parsed and isinstance(parsed[name], list) for name, _model in _ENTITY_MODELS):
                counts = summarize(parsed)
            world_state = parsed.get("world_state")
    stored = dict(row.summary) if isinstance(row.summary, dict) else {}
    return {
        **snapshot_metadata(row),
        "payload_checksum": payload_checksum,
        "checksum_ok": payload_checksum == row.checksum and payload is not None,
        "canonical_ok": canonical_ok,
        "counts": counts,
        "summary_ok": counts == stored and counts is not None,
        "world_state": world_state,
    }


def _load_payload(session: Session, snapshot_id: int) -> tuple[WorldSnapshot, WorldSnapshotPayload]:
    row = get_snapshot(session, snapshot_id)
    payload = session.get(WorldSnapshotPayload, snapshot_id)
    if payload is None:
        raise GameError("snapshot payload is missing", status_code=409, code="snapshot_checksum")
    return row, payload


def verify_snapshot(session: Session, snapshot_id: int) -> dict[str, Any]:
    """Check READY, schema, and checksum. Mark FAILED when the bytes do not match.

    The caller commits a FAILED status. Schema mismatches are left READY so a
    newer server can still see them, but this server will not restore them.
    """

    row, payload = _load_payload(session, snapshot_id)
    if row.status != SnapshotStatus.READY:
        raise GameError(
            f"snapshot {snapshot_id} is {row.status}; only READY snapshots can be restored",
            status_code=409,
            code="snapshot_not_ready",
        )
    if row.schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise GameError(
            f"snapshot schema_version {row.schema_version} is not supported "
            f"(this server writes version {SNAPSHOT_SCHEMA_VERSION})",
            status_code=409,
            code="snapshot_schema",
        )
    actual = checksum_text(payload.body)
    if actual != row.checksum:
        row.status = SnapshotStatus.FAILED
        row.error = "stored payload checksum does not match the snapshot checksum"
        raise GameError(
            f"snapshot {snapshot_id} payload does not match its checksum; "
            "it was marked FAILED and was not applied",
            status_code=409,
            code="snapshot_checksum",
        )
    try:
        parsed = json.loads(payload.body)
    except json.JSONDecodeError as exc:
        row.status = SnapshotStatus.FAILED
        row.error = "snapshot payload is not JSON"
        raise GameError(
            f"snapshot {snapshot_id} payload is not JSON; it was marked FAILED and was not applied",
            status_code=409,
            code="snapshot_checksum",
        ) from exc
    if not isinstance(parsed, dict) or canonical_json(parsed) != payload.body:
        row.status = SnapshotStatus.FAILED
        row.error = "snapshot payload is not canonical JSON"
        raise GameError(
            f"snapshot {snapshot_id} payload is not canonical JSON; it was marked FAILED and was not applied",
            status_code=409,
            code="snapshot_checksum",
        )
    if parsed.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise GameError(
            "snapshot document schema_version does not match this server",
            status_code=409,
            code="snapshot_schema",
        )
    _require_document_shape(parsed)
    return parsed


def _require_document_shape(document: dict[str, Any]) -> None:
    world = document.get("world_state")
    expected_world = {column.key for column in _columns(WorldState, exclude=_OPERATIONAL_COLUMNS)}
    if not isinstance(world, dict) or set(world) != expected_world:
        raise GameError("snapshot world_state does not match this schema", code="snapshot_schema")
    for name, model in _ENTITY_MODELS:
        rows = document.get(name)
        if not isinstance(rows, list):
            raise GameError(f"snapshot {name} is missing", code="snapshot_schema")
        expected = {column.key for column in _columns(model)}
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != expected:
                raise GameError(
                    f"snapshot {name}[{index}] does not match this schema",
                    code="snapshot_schema",
                )


def _row_kwargs(model: type, row: dict[str, Any], *, label: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for column in _columns(model):
        values[column.key] = _python_value(column, row[column.key], label=f"{label}.{column.key}")
    return values


def _reset_sequences(session: Session) -> None:
    for table in _SEQUENCE_TABLES:
        sequence = session.scalar(text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": table})
        if not sequence:
            raise RuntimeError(f"no id sequence for {table}")
        max_id = session.scalar(text(f"SELECT MAX(id) FROM {table}"))
        if max_id is None:
            session.execute(text("SELECT setval(:seq, 1, false)"), {"seq": sequence})
        else:
            session.execute(text("SELECT setval(:seq, :value, true)"), {"seq": sequence, "value": int(max_id)})


def _replace_world(session: Session, document: dict[str, Any]) -> None:
    # Command replays describe results, not the world. Drop them with the
    # replacement so a key from before the restore cannot skip a new command.
    # Accounts and refresh sessions are intentionally left in place.
    session.execute(delete(CommandIdempotency))
    session.flush()
    for model in _DELETE_ORDER:
        session.execute(delete(model))
    session.flush()
    session.expunge_all()
    for name, model in _ENTITY_MODELS:
        for index, row in enumerate(document[name]):
            session.add(model(**_row_kwargs(model, row, label=f"{name}[{index}]")))
        session.flush()
    state = session.get(WorldState, 1, with_for_update=True)
    if state is None:
        raise RuntimeError("world_state row is missing; run migrations")
    world = document["world_state"]
    state.offset_seconds = int(world["offset_seconds"])
    state.world_version = int(world["world_version"])
    session.flush()


def _apply_snapshot(session: Session, snapshot_id: int, document: dict[str, Any]) -> str:
    """Replace the world and check the checksum before the caller commits."""

    _lock_world_tables(session)
    row = session.get(WorldSnapshot, snapshot_id, with_for_update=True)
    if row is None:
        raise GameError("snapshot not found", status_code=404, code="not_found")
    if row.status != SnapshotStatus.READY:
        raise GameError(
            f"snapshot {snapshot_id} is {row.status}; only READY snapshots can be restored",
            status_code=409,
            code="snapshot_not_ready",
        )
    expected = row.checksum
    row.status = SnapshotStatus.RESTORING
    session.flush()
    _replace_world(session, document)
    session.expire_all()
    live = world_checksum(session)
    if live != expected:
        raise GameError(
            "integrity check failed after restore; the replacement was rolled back "
            "and the pre-restore world is unchanged",
            status_code=500,
            code="restore_integrity",
        )
    _reset_sequences(session)
    restored = session.get(WorldSnapshot, snapshot_id)
    if restored is None:
        raise RuntimeError("snapshot row disappeared during restore")
    restored.status = SnapshotStatus.READY
    restored.error = None
    session.flush()
    return live


def _lock_world_state(session: Session) -> WorldState:
    state = session.get(WorldState, 1, with_for_update=True)
    if state is None:
        raise RuntimeError("world_state row is missing; run migrations")
    return state


def _enter_maintenance(session: Session) -> None:
    state = _lock_world_state(session)
    if state.restore_active:
        raise GameError(
            "another snapshot restore is already running",
            status_code=409,
            code="restore_in_progress",
        )
    state.restore_active = True
    state.commands_open = False
    state.worker_paused = True
    session.flush()


def _set_gates(session: Session, *, open_world: bool) -> None:
    state = _lock_world_state(session)
    state.restore_active = False
    if open_world:
        state.commands_open = True
        state.worker_paused = False
    session.flush()


def _leave_maintenance(session: Session) -> None:
    _set_gates(session, open_world=True)


def _processing_count(session: Session) -> int:
    value = session.scalar(
        select(func.count()).select_from(Event).where(Event.status == EventStatus.PROCESSING)
    )
    return int(value or 0)


def _drain_worker(session: Session) -> None:
    """Wait until in-flight worker transactions release the shared drain lock.

    The exclusive lock is transaction-scoped, so rollback releases it. New
    claims block only while this transaction holds the lock.
    """

    deadline = time.monotonic() + _DRAIN_SECONDS
    while True:
        try:
            session.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": WORKER_DRAIN_LOCK})
            processing = _processing_count(session)
        except OperationalError as exc:
            session.rollback()
            raise GameError(
                "timed out waiting for the worker to finish its current event; "
                "restore aborted before the world was replaced",
                status_code=409,
                code="worker_busy",
            ) from exc
        session.rollback()
        if processing == 0:
            return
        if time.monotonic() >= deadline:
            raise GameError(
                "events are still marked processing; restore aborted before the world was replaced",
                status_code=409,
                code="worker_busy",
            )
        time.sleep(0.05)


def _reopen_if_world_matches(session: Session, safety_id: int) -> bool:
    """Open the gates when the live world still matches the safety snapshot."""

    safety = session.get(WorldSnapshot, safety_id)
    payload = session.get(WorldSnapshotPayload, safety_id)
    if safety is None or payload is None or safety.checksum != checksum_text(payload.body):
        return False
    live = world_checksum(session)
    if live != safety.checksum:
        return False
    _leave_maintenance(session)
    return True


def _recover(session: Session, phase: str, safety_id: int | None) -> bool:
    """Return True when player commands must stay closed.

    restore_active is cleared either way so a later restore can run. The gates
    stay closed only when the live world does not match the safety snapshot
    after a committed replace, or when reopening itself fails.
    """

    if phase == "reopen":
        try:
            _leave_maintenance(session)
            session.commit()
            return False
        except Exception:
            logger.exception("could not re-open commands after a committed restore")
            session.rollback()
            return True
    if safety_id is not None:
        try:
            matched = _reopen_if_world_matches(session, safety_id)
            if not matched:
                _set_gates(session, open_world=False)
            session.commit()
            return not matched
        except Exception:
            logger.exception("could not compare the live world to safety snapshot %s", safety_id)
            session.rollback()
            return True
    try:
        _leave_maintenance(session)
        session.commit()
        return False
    except Exception:
        logger.exception("could not leave maintenance after a failed restore")
        session.rollback()
        return True


def _maintenance_suffix(*, held: bool) -> str:
    if held:
        return (
            "Maintenance remains in effect: player commands are closed and the worker is paused. "
            "The world was not left half-applied."
        )
    return "Commands are open and the worker is running. The world was not left half-applied."


def restore_snapshot(session: Session, snapshot_id: int, *, now: datetime) -> dict[str, object]:
    """Run the safe restore sequence. `now` is the simulated clock for the safety snapshot."""

    return _restore_locked(session, snapshot_id, now=now)


def _restore_locked(session: Session, snapshot_id: int, *, now: datetime) -> dict[str, object]:
    phase = "lookup"
    safety_id: int | None = None
    acquired = False
    try:
        target = session.get(WorldSnapshot, snapshot_id)
        if target is None:
            raise GameError("snapshot not found", status_code=404, code="not_found")
        if target.status != SnapshotStatus.READY:
            raise GameError(
                f"snapshot {snapshot_id} is {target.status}; only READY snapshots can be restored",
                status_code=409,
                code="snapshot_not_ready",
            )

        phase = "maintenance"
        logger.info("restore %s: closing commands and pausing the worker", snapshot_id)
        _enter_maintenance(session)
        session.commit()
        acquired = True

        phase = "drain"
        logger.info("restore %s: waiting for the worker to go idle", snapshot_id)
        _drain_worker(session)

        phase = "safety"
        logger.info("restore %s: writing a safety snapshot", snapshot_id)
        safety = create_snapshot(session, reason=SnapshotReason.SAFETY, now=now)
        safety_id = safety.id
        session.commit()
        logger.info("restore %s: safety snapshot %s checksum %s", snapshot_id, safety_id, safety.checksum)

        phase = "verify"
        logger.info("restore %s: verifying target payload", snapshot_id)
        try:
            document = verify_snapshot(session, snapshot_id)
        except GameError as exc:
            if exc.code == "snapshot_checksum":
                session.commit()
            raise

        phase = "apply"
        logger.info("restore %s: applying payload", snapshot_id)
        live = _apply_snapshot(session, snapshot_id, document)
        session.commit()

        phase = "reopen"
        logger.info("restore %s: starting the worker and opening commands", snapshot_id)
        _leave_maintenance(session)
        session.commit()

        restored = get_snapshot(session, snapshot_id)
        state = session.get(WorldState, 1)
        if state is None:
            raise RuntimeError("world_state row is missing; run migrations")
        return {
            **snapshot_metadata(restored),
            "restored_checksum": live,
            "match": live == restored.checksum,
            "safety_snapshot_id": safety_id,
            "commands_open": state.commands_open,
            "worker_paused": state.worker_paused,
        }
    except Exception as exc:
        session.rollback()
        if phase == "lookup" or not acquired:
            raise
        held = True
        try:
            held = _recover(session, phase, safety_id)
        except Exception:
            logger.exception("restore %s recovery failed during %s", snapshot_id, phase)
            session.rollback()
            held = True
        if isinstance(exc, GameError):
            status_code = exc.status_code
            code = exc.code
            message = exc.message
        else:
            status_code = 500
            code = "restore_failed"
            message = f"restore failed: {exc}"
        raise GameError(
            f"{message} {_maintenance_suffix(held=held)}",
            status_code=status_code,
            code=code,
        ) from exc
