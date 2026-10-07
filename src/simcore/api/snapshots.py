"""Admin endpoints for world snapshots. Same token as the rest of /v1/admin."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from simcore.api.deps import get_clock, get_session, require_admin
from simcore.audit import record_admin_action
from simcore.clock import OffsetClock
from simcore.config import Settings
from simcore.constants import SnapshotReason
from simcore.errors import GameError
from simcore.snapshot import (
    create_snapshot,
    get_snapshot,
    inspect_snapshot,
    list_snapshots,
    restore_snapshot,
    snapshot_metadata,
)

router = APIRouter(prefix="/snapshots", tags=["admin"])


class SnapshotCreateIn(BaseModel):
    reason: str = Field(default="MANUAL", max_length=16)


class SnapshotRestoreIn(BaseModel):
    confirm: bool = False


@router.post("")
def create_snapshot_route(
    body: SnapshotCreateIn,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    """Capture the live world. reason defaults to MANUAL. SAFETY is reserved for restore."""

    try:
        reason = body.reason.strip().upper()
        if reason == SnapshotReason.SAFETY:
            raise GameError(
                "SAFETY snapshots are created by restore, not by this endpoint",
                code="invalid_command",
            )
        if reason not in SnapshotReason.API:
            raise GameError("reason must be AUTO or MANUAL", code="invalid_command")
        row = create_snapshot(session, reason=reason, now=clock.now())
    except GameError as exc:
        record_admin_action(
            request,
            action="snapshot.create",
            target="snapshot",
            result="failure",
            reason=exc.message,
        )
        raise
    record_admin_action(
        request,
        action="snapshot.create",
        target=f"snapshot:{row.id}",
        result="success",
        reason=row.reason,
    )
    return snapshot_metadata(row)


@router.get("")
def list_snapshots_route(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    rows = list_snapshots(session, limit=limit)
    return {"snapshots": [snapshot_metadata(row) for row in rows]}


@router.get("/{snapshot_id}")
def get_snapshot_route(
    snapshot_id: int,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    """Metadata plus the summary counts stored at capture."""

    return snapshot_metadata(get_snapshot(session, snapshot_id))


@router.get("/{snapshot_id}/inspect")
def inspect_snapshot_route(
    snapshot_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    """Recomputed counts, payload checksum, and whether they match the stored row."""

    try:
        body = inspect_snapshot(session, snapshot_id)
    except GameError as exc:
        record_admin_action(
            request,
            action="snapshot.inspect",
            target=f"snapshot:{snapshot_id}",
            result="failure",
            reason=exc.message,
        )
        raise
    record_admin_action(
        request,
        action="snapshot.inspect",
        target=f"snapshot:{snapshot_id}",
        result="success",
        reason=None,
    )
    return body


@router.post("/{snapshot_id}/restore")
def restore_snapshot_route(
    snapshot_id: int,
    body: SnapshotRestoreIn,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    """Roll the simulation back. Requires confirm=true. See docs/SNAPSHOTS.md."""

    try:
        if not body.confirm:
            raise GameError(
                "set confirm to true to restore this snapshot",
                code="invalid_command",
            )
        result = restore_snapshot(session, snapshot_id, now=clock.now())
    except GameError as exc:
        record_admin_action(
            request,
            action="snapshot.restore",
            target=f"snapshot:{snapshot_id}",
            result="failure",
            reason=exc.message,
        )
        raise
    safety_id = result.get("safety_snapshot_id")
    record_admin_action(
        request,
        action="snapshot.restore",
        target=f"snapshot:{snapshot_id}",
        result="success",
        reason=None if safety_id is None else f"safety_snapshot_id={safety_id}",
    )
    return result
