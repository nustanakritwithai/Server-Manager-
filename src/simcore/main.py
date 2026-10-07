from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from simcore import __version__
from simcore.admin_auth import AdminSessionBook, LoginRateLimiter
from simcore.api.admin import router as admin_router
from simcore.api.routes import router
from simcore.clock import Clock, SystemClock
from simcore.config import Settings, get_settings
from simcore.db import get_sessionmaker
from simcore.errors import GameError
from simcore.worker import serve

logger = logging.getLogger("simcore.api")


def create_app(settings: Settings | None = None, base_clock: Clock | None = None) -> FastAPI:
    settings = settings or get_settings()
    resolved_clock = base_clock or SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop_event = threading.Event()
        thread: threading.Thread | None = None
        if app.state.settings.embedded_worker:
            thread = threading.Thread(
                target=serve,
                kwargs={
                    "base_clock": app.state.base_clock,
                    "stop_event": stop_event,
                    "poll_seconds": app.state.settings.worker_poll_seconds,
                },
                name="simcore-embedded-worker",
                daemon=True,
            )
            thread.start()
            app.state.embedded_worker_stop = stop_event
            app.state.embedded_worker_thread = thread
            logger.info("embedded worker thread started")
        try:
            yield
        finally:
            if thread is not None:
                stop_event.set()
                thread.join(timeout=10)
                logger.info("embedded worker thread stopped")

    app = FastAPI(
        title="Server Simulation Core",
        version=__version__,
        summary="Real-time strategy server: commands in, timed events out.",
        description=(
            "The client sends intent (attack, move, recall). "
            "This server validates it, schedules a Movement, and a worker applies the result at arrive_at "
            "even if the player is offline. Dev login is a placeholder and is not authentication. "
            "Production uses the real system clock. Advancing time is an admin-only endpoint."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.base_clock = resolved_clock
    app.state.admin_sessions = AdminSessionBook()
    app.state.login_limiter = LoginRateLimiter(
        max_failures=settings.admin_login_max_failures,
        window_seconds=settings.admin_login_window_seconds,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Admin-Token"],
    )

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
