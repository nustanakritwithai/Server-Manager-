from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from simcore.api.admin_login import router as admin_login_router
from simcore.api.deps import get_clock, get_session, require_admin
from simcore.api.trace_admin import audit_router, trace_router
from simcore.api.world_map import router as world_map_router
from simcore.audit import record_admin_action
from simcore.api.inspect import (
    army_detail,
    city_detail,
    dashboard,
    event_detail,
    list_cities,
    list_events,
    list_movements,
    list_players,
    list_reports,
    list_transactions,
    movement_detail,
    player_detail,
    report_detail,
)
from simcore.clock import OffsetClock
from simcore.config import Settings
from simcore.game.queue import run_event_now
from simcore.models import Army
from simcore.present import army_body
from simcore.api.monitoring import router as monitoring_router
from simcore.api.snapshots import router as snapshot_router
from simcore.worker import run_once

router = APIRouter(prefix="/v1/admin", tags=["admin"])
router.include_router(admin_login_router)
router.include_router(snapshot_router)
router.include_router(trace_router)
router.include_router(audit_router)
router.include_router(monitoring_router)
router.include_router(world_map_router)


class AdvanceIn(BaseModel):
    seconds: int = 0
    minutes: int = 0
    hours: int = 0


@router.get("/dashboard")
def admin_dashboard(
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    """Counts and the latest snapshot. Health probes stay on /health and /health/ready."""

    now = clock.now()
    return dashboard(session, now=now, offset_seconds=clock.offset_seconds)


@router.get("/events")
def admin_list_events(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    return list_events(session, status=status, limit=limit)


@router.get("/events/{event_id}")
def admin_event_detail(
    event_id: int,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    return event_detail(session, event_id, clock.now())


@router.get("/armies")
def list_armies(
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    now = clock.now()
    armies = session.scalars(select(Army).order_by(Army.id)).all()
    return {"server_time": now, "armies": [army_body(session, army, now) for army in armies]}


@router.get("/transactions")
def admin_list_transactions(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
    limit: int = Query(default=100, ge=1, le=500),
    player_id: int | None = None,
    city_id: int | None = None,
    source_event_id: int | None = None,
    resource: str | None = None,
) -> dict[str, object]:
    return list_transactions(
        session,
        limit=limit,
        player_id=player_id,
        city_id=city_id,
        source_event_id=source_event_id,
        resource=resource,
    )


@router.get("/players")
def admin_list_players(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    return list_players(session)


@router.get("/players/{player_id}")
def admin_player_detail(
    player_id: int,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    return player_detail(session, player_id, clock.now())


@router.get("/cities")
def admin_list_cities(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
    player_id: int | None = None,
) -> dict[str, object]:
    return list_cities(session, player_id=player_id)


@router.get("/cities/{city_id}")
def admin_city_detail(
    city_id: int,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    return city_detail(session, city_id)


@router.get("/armies/{army_id}")
def admin_army_detail(
    army_id: int,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    return army_detail(session, army_id, clock.now())


@router.get("/movements")
def admin_list_movements(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
    status: str | None = None,
    army_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, object]:
    return list_movements(session, status=status, army_id=army_id, limit=limit)


@router.get("/movements/{movement_id}")
def admin_movement_detail(
    movement_id: int,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    return movement_detail(session, movement_id, clock.now())


@router.get("/reports")
def admin_list_reports(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
    limit: int = Query(default=100, ge=1, le=500),
    event_id: int | None = None,
    movement_id: int | None = None,
) -> dict[str, object]:
    return list_reports(session, limit=limit, event_id=event_id, movement_id=movement_id)


@router.get("/reports/{report_id}")
def admin_report_detail(
    report_id: int,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    return report_detail(session, report_id)


@router.post("/clock/advance")
def advance_clock(
    body: AdvanceIn,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    from simcore.errors import GameError

    try:
        if body.seconds < 0 or body.minutes < 0 or body.hours < 0:
            raise GameError("cannot rewind time", code="invalid_command")
        now = clock.advance(seconds=body.seconds, minutes=body.minutes, hours=body.hours)
        session.flush()
    except GameError as exc:
        record_admin_action(
            request,
            action="clock.advance",
            target="clock",
            result="failure",
            reason=exc.message,
        )
        raise
    record_admin_action(
        request,
        action="clock.advance",
        target="clock",
        result="success",
        reason=f"seconds={body.seconds} minutes={body.minutes} hours={body.hours}",
    )
    return {"server_time": now, "offset_seconds": clock.offset_seconds}


@router.post("/events/{event_id}/run")
def run_event(
    event_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Settings, Depends(require_admin)],
) -> dict[str, object]:
    from simcore.errors import GameError

    try:
        event = run_event_now(session, event_id, clock.now(), worker_id="admin")
    except GameError as exc:
        record_admin_action(
            request,
            action="event.run",
            target=f"event:{event_id}",
            result="failure",
            reason=exc.message,
        )
        raise
    record_admin_action(
        request,
        action="event.run",
        target=f"event:{event_id}",
        result="success",
        reason=event.status,
    )
    return {
        "id": event.id,
        "type": event.type,
        "status": event.status,
        "due_at": event.due_at,
        "processed_at": event.processed_at,
        "attempts": event.attempts,
    }


@router.post("/worker/tick")
def worker_tick(
    request: Request,
    _: Annotated[Settings, Depends(require_admin)],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    """Run the real worker path. Each event commits in its own transaction."""

    processed: list[int] = []
    failed: list[int] = []
    paused = False
    base = request.app.state.base_clock
    try:
        for _ in range(limit):
            status, event_id = run_once(base)
            if status == "paused":
                paused = True
                break
            if status == "empty":
                break
            if status == "processed" and event_id is not None:
                processed.append(event_id)
            elif event_id is not None:
                failed.append(event_id)
    except Exception as exc:
        record_admin_action(
            request,
            action="worker.tick",
            target="worker",
            result="failure",
            reason=exc.__class__.__name__,
        )
        raise
    record_admin_action(
        request,
        action="worker.tick",
        target="worker",
        result="success",
        reason=f"processed={len(processed)} failed={len(failed)} paused={paused}",
    )
    return {
        "processed": len(processed),
        "failed": len(failed),
        "paused": paused,
        "event_ids": processed,
        "failed_ids": failed,
    }
