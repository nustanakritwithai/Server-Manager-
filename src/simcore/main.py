from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from simcore import __version__
from simcore.api.admin import router as admin_router
from simcore.api.routes import router
from simcore.clock import Clock, SystemClock
from simcore.config import Settings, get_settings
from simcore.db import get_sessionmaker
from simcore.errors import GameError


def create_app(settings: Settings | None = None, base_clock: Clock | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Server Simulation Core",
        version=__version__,
        summary="Real-time strategy server: commands in, timed events out.",
        description=(
            "The Godot client sends intent (attack, move, recall). "
            "This server validates it, schedules a Movement, and a worker applies the result at arrive_at "
            "even if the player is offline. Dev login is a placeholder and is not authentication."
        ),
    )
    app.state.settings = settings
    app.state.base_clock = base_clock or SystemClock()

    @app.exception_handler(GameError)
    def _game_error(_request: object, exc: GameError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["meta"])
    def ready() -> JSONResponse:
        try:
            with get_sessionmaker()() as session:
                session.execute(text("SELECT 1"))
            return JSONResponse({"status": "ok"})
        except Exception:
            return JSONResponse({"status": "degraded"}, status_code=503)

    @app.get("/", tags=["meta"])
    def root() -> dict[str, str]:
        return {"name": "simcore", "version": __version__, "docs": "/docs", "time": "/v1/time", "health": "/health"}

    app.include_router(router)
    app.include_router(admin_router)
    return app


app = create_app()
