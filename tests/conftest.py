"""Postgres fixtures. Tests refuse to truncate anything except simcore_test."""

from __future__ import annotations

import os

os.environ["SIMCORE_DATABASE_URL"] = os.environ.get(
    "SIMCORE_TEST_DATABASE_URL",
    "postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore_test",
)
os.environ.setdefault("SIMCORE_ENV", "development")
os.environ.setdefault("SIMCORE_ADMIN_TOKEN", "dev-admin")

from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from simcore.config import get_settings
from simcore.db import get_sessionmaker, reset_engine

get_settings.cache_clear()
reset_engine()

ADMIN = {"X-Admin-Token": "dev-admin"}


def _assert_test_database() -> None:
    url = get_settings().database_url.split("?", 1)[0].rstrip("/")
    if not url.endswith("/simcore_test"):
        raise RuntimeError(f"refusing to truncate non-test database: {url}")


def truncate() -> None:
    _assert_test_database()
    session = get_sessionmaker()()
    try:
        session.execute(
            text(
                """
                TRUNCATE TABLE
                  audit_log,
                  world_snapshot_payloads,
                  world_snapshots,
                  transactions,
                  battle_reports,
                  events,
                  movements,
                  player_commands,
                  armies,
                  cities,
                  players
                RESTART IDENTITY CASCADE
                """
            )
        )
        session.execute(
            text(
                """
                UPDATE world_state
                SET offset_seconds = 0,
                    world_version = 0,
                    commands_open = true,
                    worker_paused = false,
                    restore_active = false
                """
            )
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture(scope="session")
def _migrated() -> None:
    get_settings.cache_clear()
    reset_engine()
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def db(_migrated: None):
    truncate()
    yield
    truncate()


@pytest.fixture
def frozen():
    from simcore.clock import FrozenClock

    return FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.fixture
def client(db: None, frozen):
    from fastapi.testclient import TestClient

    from simcore.main import create_app

    app = create_app(base_clock=frozen)
    with TestClient(app) as test_client:
        yield test_client
