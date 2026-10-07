from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from simcore.api.command_guard import run_command
from simcore.api.deps import get_authenticated_player, get_clock, get_current_player, get_session
from simcore.api.player_auth import router as player_auth_router
from simcore.clock import OffsetClock
from simcore.errors import GameError
from simcore.constants import RESOURCES
from simcore.game.commands import (
    attack_city,
    found_city,
    garrison_army,
    move_army,
    queue_build,
    queue_research,
    recall_army,
    train_units,
    transfer_resources,
)
from simcore.game.economy import accrue_city
from simcore.models import Army, BattleReport, City, Event, Player
from simcore.present import army_body, city_body, movement_body, report_body

router = APIRouter(prefix="/v1")
router.include_router(player_auth_router)


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


class TrainIn(BaseModel):
    city_id: int
    unit_type: str = Field(min_length=1, max_length=40)
    count: int = Field(ge=1, le=100)
    army_id: int | None = None


class FoundCityIn(BaseModel):
    source_city_id: int
    x: int
    y: int
    name: str = Field(min_length=1, max_length=40)


class GarrisonIn(BaseModel):
    army_id: int
    city_id: int


class TransferIn(BaseModel):
    source_city_id: int
    destination_city_id: int
    wood: int = Field(default=0, ge=0)
    food: int = Field(default=0, ge=0)
    iron: int = Field(default=0, ge=0)
    gold: int = Field(default=0, ge=0)


def _require_city(session: Session, player: Player, city_id: int) -> City:
    city = session.get(City, city_id)
    if city is None:
        raise GameError("city not found", status_code=404, code="not_found")
    if city.player_id != player.id:
        raise GameError("that city belongs to another player", status_code=403, code="forbidden")
    return city


@router.get("/time", tags=["time"])
def server_time(
    clock: Annotated[OffsetClock, Depends(get_clock)],
    _: Annotated[Player, Depends(get_authenticated_player)],
) -> dict[str, object]:
    """Server clock the client uses to count down arrive_at locally.

    Requires a player access token. It does not accept a player id in the query.
    """

    now = clock.now()
    return {
        "server_time": now,
        "offset_seconds": clock.offset_seconds,
        "unix_ms": int(now.timestamp() * 1000),
    }


@router.get("/me", tags=["player"])
def me(player: Annotated[Player, Depends(get_current_player)]) -> dict[str, object]:
    return {"id": player.id, "name": player.name, "research": player.research}


@router.get("/me/cities", tags=["player"])
def my_cities(
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
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
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    city = _require_city(session, player, city_id)
    accrue_city(session, city, clock.now())
    return city_body(session, city, include_resources=True)


@router.get("/map/cities", tags=["map"])
def map_cities(
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
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
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    now = clock.now()
    armies = session.scalars(select(Army).where(Army.player_id == player.id).order_by(Army.id)).all()
    return {"server_time": now, "armies": [army_body(session, army, now) for army in armies]}


@router.get("/me/reports", tags=["reports"])
def my_reports(
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
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
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> dict[str, object]:
    report = session.get(BattleReport, report_id)
    if report is None:
        raise GameError("report not found", status_code=404, code="not_found")
    if player.id not in (report.attacker_player_id, report.defender_player_id):
        raise GameError("that report belongs to another battle", status_code=403, code="forbidden")
    return report_body(report)


@router.post("/commands/move", tags=["commands"], response_model=None)
def command_move(
    body: MoveIn,
    request: Request,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    # Any new POST /v1/commands/* route must call run_command so the rate limit
    # and the Idempotency-Key apply. The player argument is the token's player.
    return run_command(
        request,
        session,
        player,
        body,
        lambda: movement_body(
            *move_army(
                session,
                player,
                body.army_id,
                body.destination_city_id,
                clock.now(),
                relocate=body.relocate,
            )
        ),
    )


@router.post("/commands/attack", tags=["commands"], response_model=None)
def command_attack(
    body: AttackIn,
    request: Request,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    return run_command(
        request,
        session,
        player,
        body,
        lambda: movement_body(*attack_city(session, player, body.army_id, body.target_city_id, clock.now())),
    )


@router.post("/commands/recall", tags=["commands"], response_model=None)
def command_recall(
    body: RecallIn,
    request: Request,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    return run_command(
        request,
        session,
        player,
        body,
        lambda: movement_body(*recall_army(session, player, body.army_id, clock.now())),
    )


@router.post("/commands/build", tags=["commands"], response_model=None)
def command_build(
    body: BuildIn,
    request: Request,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    return run_command(
        request,
        session,
        player,
        body,
        lambda: _timed_event_body(queue_build(session, player, body.city_id, body.building, clock.now())),
    )


@router.post("/commands/research", tags=["commands"], response_model=None)
def command_research(
    body: ResearchIn,
    request: Request,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    return run_command(
        request,
        session,
        player,
        body,
        lambda: _timed_event_body(queue_research(session, player, body.tech, clock.now())),
    )


@router.post("/commands/train", tags=["commands"], response_model=None)
def command_train(
    body: TrainIn,
    request: Request,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    return run_command(
        request,
        session,
        player,
        body,
        lambda: _timed_event_body(
            train_units(
                session,
                player,
                body.city_id,
                body.unit_type,
                body.count,
                clock.now(),
                army_id=body.army_id,
            )
        ),
    )


@router.post("/commands/found-city", tags=["commands"], response_model=None)
def command_found_city(
    body: FoundCityIn,
    request: Request,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    def _found() -> dict[str, object]:
        city, event = found_city(session, player, body.source_city_id, body.x, body.y, body.name, clock.now())
        payload = city_body(session, city, include_resources=True)
        payload["event_id"] = event.id
        payload["trace_id"] = event.trace_id
        return payload

    return run_command(request, session, player, body, _found)


@router.post("/commands/garrison", tags=["commands"], response_model=None)
def command_garrison(
    body: GarrisonIn,
    request: Request,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    return run_command(
        request,
        session,
        player,
        body,
        lambda: movement_body(*garrison_army(session, player, body.army_id, body.city_id, clock.now())),
    )


@router.post("/commands/transfer", tags=["commands"], response_model=None)
def command_transfer(
    body: TransferIn,
    request: Request,
    player: Annotated[Player, Depends(get_current_player)],
    session: Annotated[Session, Depends(get_session, scope="function")],
    clock: Annotated[OffsetClock, Depends(get_clock)],
) -> dict[str, object]:
    def _transfer() -> dict[str, object]:
        amounts = {name: int(getattr(body, name)) for name in RESOURCES}
        event = transfer_resources(
            session,
            player,
            body.source_city_id,
            body.destination_city_id,
            amounts,
            clock.now(),
        )
        payload = _timed_event_body(event)
        payload["amounts"] = amounts
        return payload

    return run_command(request, session, player, body, _transfer)


def _timed_event_body(event: Event) -> dict[str, object]:
    return {
        "event_id": event.id,
        "type": event.type,
        "status": event.status,
        "due_at": event.due_at,
        "payload": event.payload,
        "trace_id": event.trace_id,
    }



