from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from simcore.api.deps import get_clock, get_current_player, get_session
from simcore.auth import DEV_AUTH_WARNING, issue_dev_token
from simcore.clock import OffsetClock
from simcore.errors import GameError
from simcore.game.commands import attack_city, move_army, queue_build, queue_research, recall_army
from simcore.game.economy import accrue_city
from simcore.models import Army, BattleReport, City, Event, Player
from simcore.present import army_body, city_body, movement_body, report_body

router = APIRouter(prefix="/v1")


class DevLoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class MoveIn(BaseModel):
    army_id: int
    destination_city_id: int
    relocate: bool = False


class AttackIn(BaseModel):
    army_id: int
    target_city_id: int


class RecallIn(BaseModel):
    army_id: int


class BuildIn(BaseModel):
    city_id: int
    building: str = Field(min_length=1, max_length=40)


class ResearchIn(BaseModel):
    tech: str = Field(min_length=1, max_length=40)


def _require_city(session: Session, player: Player, city_id: int) -> City:
    city = session.get(City, city_id)
    if city is None:
        raise GameError("city not found", status_code=404, code="not_found")
    if city.player_id != player.id:
        raise GameError("that city belongs to another player", status_code=403, code="forbidden")
    return city


@router.get("/time", tags=["time"])
def server_time(clock: Annotated[OffsetClock, Depends(get_clock)]) -> dict[str, object]:
    """Server clock the client uses to count down arrive_at locally."""

    now = clock.now()
    return {
        "server_time": now,
        "offset_seconds": clock.offset_seconds,
        "unix_ms": int(now.timestamp() * 1000),
    }


@router.post("/auth/dev-login", tags=["auth"])
def dev_login(body: DevLoginIn, session: Annotated[Session, Depends(get_session)]) -> dict[str, object]:
    player = session.scalar(select(Player).where(Player.name == body.name))
    if player is None:
        raise GameError("no such player", status_code=404, code="not_found")
    return {
        "token": issue_dev_token(player.id),
        "token_type": "bearer",
        "player_id": player.id,
        "player_name": player.name,
        "dev_only": True,
        "warning": DEV_AUTH_WARNING,
    }


@router.get("/me", tags=["player"])
def me(player: Annotated[Player, Depends(get_current_player)]) -> dict[str, object]:
    return {"id": player.id, "name": player.name, "research": player.research}


@router.get("/me/cities", tags=["player"])
def my_cities(
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    now = clock.now()
    cities = session.scalars(select(City).where(City.player_id == player.id).order_by(City.id)).all()
    for city in cities:
        accrue_city(session, city, now)
    return {"cities": [city_body(session, city, include_resources=True) for city in cities]}


@router.get("/me/cities/{city_id}", tags=["player"])
def my_city(
    city_id: int,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    city = _require_city(session, player, city_id)
    accrue_city(session, city, clock.now())
    return city_body(session, city, include_resources=True)


@router.get("/map/cities", tags=["map"])
def map_cities(
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    cities = session.scalars(select(City).order_by(City.id)).all()
    payload = []
    for city in cities:
        body = city_body(session, city, include_resources=False)
        body["is_mine"] = city.player_id == player.id
        payload.append(body)
    return {"cities": payload}


@router.get("/me/armies", tags=["armies"])
def my_armies(
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    now = clock.now()
    armies = session.scalars(select(Army).where(Army.player_id == player.id).order_by(Army.id)).all()
    return {"server_time": now, "armies": [army_body(session, army, now) for army in armies]}


@router.get("/me/reports", tags=["reports"])
def my_reports(
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    reports = session.scalars(
        select(BattleReport)
        .where(or_(BattleReport.attacker_player_id == player.id, BattleReport.defender_player_id == player.id))
        .order_by(BattleReport.id)
    ).all()
    return {"reports": [report_body(report) for report in reports]}


@router.get("/me/reports/{report_id}", tags=["reports"])
def my_report(
    report_id: int,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    report = session.get(BattleReport, report_id)
    if report is None:
        raise GameError("report not found", status_code=404, code="not_found")
    if player.id not in (report.attacker_player_id, report.defender_player_id):
        raise GameError("that report belongs to another battle", status_code=403, code="forbidden")
    return report_body(report)


@router.post("/commands/move", tags=["commands"])
def command_move(
    body: MoveIn,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    movement, event = move_army(
        session, player, body.army_id, body.destination_city_id, clock.now(), relocate=body.relocate
    )
    return movement_body(movement, event)


@router.post("/commands/attack", tags=["commands"])
def command_attack(
    body: AttackIn,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    movement, event = attack_city(session, player, body.army_id, body.target_city_id, clock.now())
    return movement_body(movement, event)


@router.post("/commands/recall", tags=["commands"])
def command_recall(
    body: RecallIn,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    movement, event = recall_army(session, player, body.army_id, clock.now())
    return movement_body(movement, event)


@router.post("/commands/build", tags=["commands"])
def command_build(
    body: BuildIn,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    event = queue_build(session, player, body.city_id, body.building, clock.now())
    return _timed_event_body(event)


@router.post("/commands/research", tags=["commands"])
def command_research(
    body: ResearchIn,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    event = queue_research(session, player, body.tech, clock.now())
    return _timed_event_body(event)


def _timed_event_body(event: Event) -> dict[str, object]:
    return {
        "event_id": event.id,
        "type": event.type,
        "status": event.status,
        "due_at": event.due_at,
        "payload": event.payload,
    }



