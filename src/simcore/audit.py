"""Append-only admin audit log with a hash chain.

row_hash = sha256(prev_hash + canonical JSON). The canonical JSON covers id,
actor, action, target, occurred_at, source_ip, result, and reason. It does not
include row_hash. prev_hash is the previous row's row_hash, or 64 zero digits
for the first row.

Passwords, bearer tokens, session secrets, and password hashes are refused.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from simcore.admin_auth import client_address, read_admin_session, secrets_equal
from simcore.db import get_sessionmaker
from simcore.models import AuditLog, utcnow
from simcore.snapshot import canonical_json

_GENESIS = "0" * 64
_LOCK_KEY = 87410003
_REASON_LIMIT = 500


def _canon_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _reject_secrets(value: str) -> None:
    if "simadm1." in value or "simplyr1." in value or "scrypt$" in value:
        raise RuntimeError("audit log refused a secret value")


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text_value = value.replace("\x00", "")
    _reject_secrets(text_value)
    if len(text_value) > limit:
        return text_value[:limit]
    return text_value


def audit_content(
    *,
    row_id: int,
    actor: str,
    action: str,
    target: str,
    occurred_at: datetime,
    source_ip: str,
    result: str,
    reason: str | None,
) -> str:
    """Canonical row content. prev_hash is prepended outside this document."""

    return canonical_json(
        {
            "id": row_id,
            "actor": actor,
            "action": action,
            "target": target,
            "occurred_at": _canon_dt(occurred_at),
            "source_ip": source_ip,
            "result": result,
            "reason": reason,
        }
    )


def chain_hash(prev_hash: str, content: str) -> str:
    return hashlib.sha256((prev_hash + content).encode("utf-8")).hexdigest()


def append_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    target: str,
    source_ip: str,
    result: str,
    reason: str | None = None,
    occurred_at: datetime | None = None,
) -> AuditLog:
    """Insert one row. Caller commits. Locks the chain for this transaction."""

    actor_text = _clip(actor, 80) or "system"
    action_text = _clip(action, 64) or "unknown"
    target_text = _clip(target, 200) or "-"
    ip_text = _clip(source_ip, 64) or "unknown"
    result_text = _clip(result, 20) or "failure"
    reason_text = _clip(reason, _REASON_LIMIT)
    when = occurred_at or utcnow()

    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
    previous = session.scalar(select(AuditLog.row_hash).order_by(AuditLog.id.desc()).limit(1))
    prev_hash = previous or _GENESIS
    row = AuditLog(
        actor=actor_text,
        action=action_text,
        target=target_text,
        occurred_at=when,
        source_ip=ip_text,
        result=result_text,
        reason=reason_text,
        prev_hash=prev_hash,
        row_hash=_GENESIS,
    )
    session.add(row)
    session.flush()
    content = audit_content(
        row_id=row.id,
        actor=row.actor,
        action=row.action,
        target=row.target,
        occurred_at=row.occurred_at,
        source_ip=row.source_ip,
        result=row.result,
        reason=row.reason,
    )
    row.row_hash = chain_hash(prev_hash, content)
    session.flush()
    return row


def write_audit(
    *,
    actor: str,
    action: str,
    target: str,
    source_ip: str,
    result: str,
    reason: str | None = None,
) -> None:
    """Commit one audit row in its own transaction."""

    session = get_sessionmaker()()
    try:
        append_audit(
            session,
            actor=actor,
            action=action,
            target=target,
            source_ip=source_ip,
            result=result,
            reason=reason,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def actor_from_request(request: Request) -> str:
    """Admin actor label. A session id, or the static-token actor `admin`.

    The bearer token and the admin token value are not returned.
    """

    settings = request.app.state.settings
    book = request.app.state.admin_sessions
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if token:
        admin_session = read_admin_session(token, settings, book)
        if admin_session is not None:
            return f"session:{admin_session.jti}"
    admin_header = request.headers.get("x-admin-token", "")
    if admin_header and secrets_equal(admin_header, settings.admin_token):
        return "admin"
    return "admin"


def record_admin_action(
    request: Request,
    *,
    action: str,
    target: str,
    result: str,
    reason: str | None = None,
    actor: str | None = None,
) -> None:
    write_audit(
        actor=actor or actor_from_request(request),
        action=action,
        target=target,
        source_ip=client_address(request),
        result=result,
        reason=reason,
    )


def verify_chain(session: Session) -> dict[str, object]:
    """Recompute every row. An empty log is a completed check of zero rows."""

    rows = session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    reasons: list[str] = []
    previous = _GENESIS
    for row in rows:
        if row.prev_hash != previous:
            reasons.append(f"row {row.id} prev_hash does not match the previous row_hash")
        content = audit_content(
            row_id=row.id,
            actor=row.actor,
            action=row.action,
            target=row.target,
            occurred_at=row.occurred_at,
            source_ip=row.source_ip,
            result=row.result,
            reason=row.reason,
        )
        expected = chain_hash(row.prev_hash, content)
        if row.row_hash != expected:
            reasons.append(f"row {row.id} row_hash does not match its content")
        previous = row.row_hash
    return {
        "status": "FAIL" if reasons else "PASS",
        "checked_rows": len(rows),
        "reasons": reasons,
    }


def audit_entry(row: AuditLog) -> dict[str, object]:
    return {
        "id": row.id,
        "actor": row.actor,
        "action": row.action,
        "target": row.target,
        "timestamp": row.occurred_at,
        "source_ip": row.source_ip,
        "result": row.result,
        "reason": row.reason,
        "prev_hash": row.prev_hash,
        "row_hash": row.row_hash,
    }


def list_audit(
    session: Session,
    *,
    limit: int,
    offset: int,
    actor: str | None = None,
    action: str | None = None,
    result: str | None = None,
    target: str | None = None,
) -> dict[str, object]:
    """One page of the log, plus verification of the full chain (not just the page)."""

    filters = []
    if actor:
        filters.append(AuditLog.actor == actor)
    if action:
        filters.append(AuditLog.action == action)
    if result:
        filters.append(AuditLog.result == result)
    if target:
        filters.append(AuditLog.target == target)
    total = session.scalar(select(func.count()).select_from(AuditLog).where(*filters))
    rows = session.scalars(
        select(AuditLog).where(*filters).order_by(AuditLog.id.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "entries": [audit_entry(row) for row in rows],
        "limit": limit,
        "offset": offset,
        "total": int(total or 0),
        "chain": verify_chain(session),
    }
