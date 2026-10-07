"""Admin endpoints for world snapshots. Same token as the rest of /v1/admin."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from simcore.api.deps import get_clock, get_session, require_admin
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
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    """Capture the live world. reason defaults to MANUAL. SAFETY is reserved for restore."""

    reason = body.reason.strip().upper()
    if reason == SnapshotReason.SAFETY:
        raise GameError(
            "SAFETY snapshots are created by restore, not by this endpoint",
            code="invalid_command",
        )
    if reason not in SnapshotReason.API:
        raise GameError("reason must be AUTO or MANUAL", code="invalid_command")
    row = create_snapshot(session, reason=reason, now=clock.now())
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
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    """Recomputed counts, payload checksum, and whether they match the stored row."""

    return inspect_snapshot(session, snapshot_id)


@router.post("/{snapshot_id}/restore")
def restore_snapshot_route(
    snapshot_id: int,
    body: SnapshotRestoreIn,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    """Roll the simulation back. Requires confirm=true. See docs/SNAPSHOTS.md."""

    if not body.confirm:
        raise GameError(
            "set confirm to true to restore this snapshot",
            code="invalid_command",
        )
    return restore_snapshot(session, snapshot_id, now=clock.now())
