"""Read-only monitoring routes. Same admin auth as the rest of /v1/admin."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from simcore.api.deps import require_admin
from simcore.config import Settings
from simcore.db import get_sessionmaker
from simcore.errors import GameError
from simcore.models import WorldState
from simcore.monitoring import HISTORY_WINDOWS, collect_report, history

router = APIRouter(tags=["admin"])


def _open_report(request: Request, settings: Settings) -> dict[str, object]:
    session = get_sessionmaker()()
    try:
        state = session.get(WorldState, 1)
        if state is None:
            game_now = None
        else:
            game_now = request.app.state.base_clock.now() + timedelta(seconds=int(state.offset_seconds))
        report = collect_report(session, game_now=game_now, settings=settings, include_api=True)
        session.rollback()
        return report
    finally:
        session.close()


@router.get("/monitoring")
def admin_monitoring(
    request: Request,
    settings: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    """Current measured checks. Does not write samples or change the world."""

    return _open_report(request, settings)


@router.get("/monitoring/history")
def admin_monitoring_history(
    settings: Annotated[Settings, Depends(require_admin)],
    metric: str = "",
    window: str = "24h",
) -> dict[str, object]:
    """Samples already stored. An empty series is empty; it is not filled with zeros."""

    chosen = window.strip() or "24h"
    if chosen not in HISTORY_WINDOWS:
        allowed = ", ".join(HISTORY_WINDOWS)
        raise GameError(f"window must be one of {allowed}", status_code=400, code="invalid_command")
    name = metric.strip()
    if not name:
        raise GameError("metric is required", status_code=400, code="invalid_command")
    if len(name) > 64 or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in name):
        raise GameError("metric must be a short lowercase name", status_code=400, code="invalid_command")
    session: Session = get_sessionmaker()()
    try:
        body = history(session, metric=name, window=chosen)
        session.rollback()
        return body
    finally:
        session.close()
