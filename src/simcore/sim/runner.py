"""Run bots against the HTTP API, then score the world.

CI starts an API on loopback with the frozen test clock, seeds bots, and
advances time through ``POST /v1/admin/clock/advance`` and
``POST /v1/admin/worker/tick``. Staging only sends player commands to
``--base-url`` and waits in real time. Phase 8 can replace ``submit_commands``
with an overlapping sender; the CI scenario keeps overlap at 1 so request
order does not change the snapshot checksum.
"""

from __future__ import annotations

import math
import os
import random
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from sqlalchemy import func, select, text

from simcore.sim.bots import UNAVAILABLE_ACTIONS, PlannedCommand, plan_tick
from simcore.sim.http import ApiClient, latency_summary
from simcore.sim.ids import install as install_trace_ids
from simcore.sim.report import write_report
from simcore.sim.safety import SafetyError, ensure_safe, is_live_database
from simcore.sim.seed_world import profile_for, seed_bots
from simcore.sim.thresholds import (
    CI_DEFAULT_STEP_SECONDS,
    CI_DEFAULT_TICKS,
    STAGING_DEFAULT_PAUSE_SECONDS,
    STAGING_DEFAULT_TICKS,
    threshold_document,
)
from simcore.sim.verify import verify_run

# Fixed epoch so two CI processes with the same seed share one timeline.
# The seed selects commands. It does not move this instant.
CI_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_CI_WORKER_ID = "simulator"

_COMMAND_PATHS = {
    "attack": "/v1/commands/attack",
    "move": "/v1/commands/move",
    "recall": "/v1/commands/recall",
    "build": "/v1/commands/build",
    "research": "/v1/commands/research",
}


@dataclass
class SimConfig:
    mode: str
    players: int
    seed: int
    ticks: int | None
    duration: int | None
    command_rate: int
    base_url: str | None
    database_url: str | None
    report_dir: Path
    allow_production: bool
    admin_token: str


def run(config: SimConfig) -> dict[str, Any]:
    if config.mode not in {"ci", "staging"}:
        raise SafetyError("mode must be ci or staging")
    if config.players < 1 or config.players > 30:
        raise SafetyError("--players must be from 1 to 30")
    if config.command_rate < 1 or config.command_rate > 20:
        raise SafetyError("--command-rate must be from 1 to 20")
    if not config.admin_token:
        raise SafetyError("SIMCORE_ADMIN_TOKEN is not set. The simulator does not ship a token.")

    database_url = config.database_url or os.environ.get("SIMCORE_DATABASE_URL", "").strip() or None
    env_name = os.environ.get("SIMCORE_ENV", "")
    if config.mode == "ci" and database_url and is_live_database(database_url):
        raise SafetyError(
            "CI mode seeds a fresh test database and will not do that to a live database name, "
            "even with --i-understand-this-is-production. Use --mode staging to send commands "
            "to an existing world, and take a snapshot first."
        )
    ensure_safe(
        mode=config.mode,
        base_url=config.base_url if config.mode == "staging" else None,
        database_url=database_url,
        env_name=env_name,
        allow_production=config.allow_production,
    )

    saved_env = {
        key: os.environ.get(key)
        for key in (
            "SIMCORE_DATABASE_URL",
            "SIMCORE_ADMIN_TOKEN",
            "SIMCORE_WORKER_ID",
            "SIMCORE_MONITOR_API_SAMPLER",
            "SIMCORE_MONITOR_SAMPLE_SECONDS",
            "SIMCORE_EMBEDDED_WORKER",
            "SIMCORE_ENV",
        )
    }
    restore_ids = None
    server = None
    thread = None
    api: ApiClient | None = None
    try:
        if database_url:
            os.environ["SIMCORE_DATABASE_URL"] = database_url
        os.environ["SIMCORE_ADMIN_TOKEN"] = config.admin_token
        if config.mode == "ci":
            os.environ["SIMCORE_WORKER_ID"] = _CI_WORKER_ID
            os.environ["SIMCORE_MONITOR_API_SAMPLER"] = "false"
            os.environ["SIMCORE_MONITOR_SAMPLE_SECONDS"] = "0"
            os.environ["SIMCORE_EMBEDDED_WORKER"] = "false"
            if os.environ.get("SIMCORE_ENV", "").strip().lower() == "production":
                os.environ["SIMCORE_ENV"] = "development"
        _reload_settings()
        if config.mode == "ci":
            _migrate()
            restore_ids = install_trace_ids(config.seed)
            from simcore.clock import FrozenClock, OffsetClock
            from simcore.db import get_sessionmaker

            frozen = FrozenClock(CI_EPOCH)
            session = get_sessionmaker()()
            try:
                now = OffsetClock(session, frozen).now()
                seed_bots(session, now, config.players)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
            server, thread, port = _start_server(frozen)
            base_url = f"http://127.0.0.1:{port}"
            ensure_safe(
                mode="ci",
                base_url=base_url,
                database_url=database_url,
                env_name=os.environ.get("SIMCORE_ENV", ""),
                allow_production=config.allow_production,
            )
        else:
            base_url = str(config.base_url)
            frozen = None

        ticks, duration, step_or_pause = _schedule(config)
        thresholds = threshold_document(
            mode=config.mode,
            step_seconds=step_or_pause if config.mode == "ci" else None,
        )
        api = ApiClient(base_url, admin_token=config.admin_token)
        _wait_ready(api)
        bots = _login_bots(api, config)
        rng = random.Random(config.seed)
        sequence: list[dict[str, Any]] = []
        max_lag = 0.0
        drain_failures: list[int] = []
        for tick in range(ticks):
            views = [_view(api, bot) for bot in bots]
            planned = plan_tick(rng, tick=tick, players=views, command_rate=config.command_rate)
            sequence.extend(submit_commands(api, bots, planned, overlap=1))
            if config.mode == "ci":
                _advance(api, int(step_or_pause))
                observed = _sample_lag(api)
                max_lag = max(max_lag, observed)
                drained = _drain(api)
                drain_failures.extend(drained["failed"])
                if drained["paused"]:
                    raise RuntimeError("worker is paused; the simulator does not resume a restore")
            elif tick + 1 < ticks and step_or_pause > 0:
                time.sleep(step_or_pause)
        if config.mode == "ci":
            caught = _catch_up(api)
            max_lag = max(max_lag, caught["max_lag"])
            drain_failures.extend(caught["failed"])

        if config.mode == "staging":
            max_lag = max(max_lag, _sample_lag(api))

        verified = verify_run(
            api,
            mode=config.mode,
            player_count=config.players if config.mode == "ci" else None,
            thresholds=thresholds,
            max_event_lag_seconds=max_lag,
        )
        latency = latency_summary(api.latencies_ms)
        payload = _payload(
            config,
            base_url=base_url,
            ticks=ticks,
            duration=duration,
            step_or_pause=step_or_pause,
            thresholds=thresholds,
            sequence=sequence,
            verified=verified,
            latency=latency,
            drain_failures=drain_failures,
        )
        write_report(config.report_dir, payload)
        return payload
    finally:
        if api is not None:
            api.close()
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        if restore_ids is not None:
            restore_ids()
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _reload_settings()


def submit_commands(
    api: ApiClient,
    bots: list[dict[str, Any]],
    planned: list[PlannedCommand],
    *,
    overlap: int,
) -> list[dict[str, Any]]:
    """Send planned commands.

    ``overlap`` is 1 for CI and staging. A higher value is the Phase 8 seam:
    it is intentionally rejected here so a load run cannot be mistaken for a
    checksum run. The planned list is already in a stable order.
    """

    if overlap != 1:
        raise RuntimeError(
            "overlapping command submission is reserved for Phase 8 load tests. "
            "The CI scenario keeps overlap at 1 so the snapshot checksum stays a function of the seed."
        )
    tokens = {int(bot["id"]): bot["token"] for bot in bots}
    sent: list[dict[str, Any]] = []
    for command in planned:
        row = command.as_dict()
        if command.action == "skip" or command.body is None:
            row.update({"http_status": None, "result": "skipped", "trace_id": None})
            sent.append(row)
            continue
        path = _COMMAND_PATHS[command.action]
        status, body = api.json(
            "POST",
            path,
            headers={"Authorization": f"Bearer {tokens[command.player_id]}"},
            json=command.body,
        )
        trace_id = body.get("trace_id") if isinstance(body, dict) else None
        if status == 200:
            result = "accepted"
        elif status >= 500:
            result = "error"
        else:
            result = "rejected"
        error = None
        if isinstance(body, dict) and status != 200:
            err = body.get("error")
            error = err if isinstance(err, dict) else {"message": str(body)[:300]}
        row.update({"http_status": status, "result": result, "trace_id": trace_id, "error": error})
        sent.append(row)
    return sent


def _payload(
    config: SimConfig,
    *,
    base_url: str,
    ticks: int,
    duration: int,
    step_or_pause: int,
    thresholds: dict[str, Any],
    sequence: list[dict[str, Any]],
    verified: dict[str, Any],
    latency: dict[str, Any],
    drain_failures: list[int],
) -> dict[str, Any]:
    accepted = sum(1 for row in sequence if row["result"] == "accepted")
    rejected = sum(1 for row in sequence if row["result"] == "rejected")
    skipped = sum(1 for row in sequence if row["result"] == "skipped")
    errors = [row for row in sequence if row["result"] == "error"]
    counts = dict(verified["counts"])
    counts.update(
        {
            "commands_attempted": accepted + rejected + len(errors),
            "commands_accepted": accepted,
            "commands_rejected": rejected,
            "commands_skipped": skipped,
            "commands_http_5xx": len(errors),
        }
    )
    invariants = list(verified["invariants"])
    failed = list(verified["failed"])
    if errors:
        item = {
            "invariant": "player_api",
            "status": "FAIL",
            "required": True,
            "trace_id": errors[0].get("trace_id"),
            "detail": f"{len(errors)} player commands returned HTTP 5xx",
        }
        invariants.append(item)
        failed.append(item)
    if drain_failures:
        item = {
            "invariant": "worker",
            "status": "FAIL",
            "required": True,
            "trace_id": None,
            "detail": f"worker tick failed event ids {drain_failures}",
        }
        invariants.append(item)
        failed.append(item)
    latency_status = "PASS"
    latency_detail = f"avg {latency.get('avg')} ms, p95 {latency.get('p95')} ms over {latency.get('samples')} calls"
    if not isinstance(latency.get("p95"), (int, float)) or not isinstance(latency.get("avg"), (int, float)):
        latency_status = "FAIL"
        latency_detail = "no API calls were timed"
    elif float(latency["p95"]) > float(thresholds["api_p95_ms_max"]) or float(latency["avg"]) > float(
        thresholds["api_avg_ms_max"]
    ):
        latency_status = "FAIL"
        latency_detail = (
            f"avg {latency['avg']:.1f} ms (max {thresholds['api_avg_ms_max']}), "
            f"p95 {latency['p95']:.1f} ms (max {thresholds['api_p95_ms_max']})"
        )
    latency_row = {
        "invariant": "api_latency",
        "status": latency_status,
        "required": True,
        "trace_id": None,
        "detail": latency_detail,
    }
    invariants.append(latency_row)
    if latency_status != "PASS":
        failed.append(latency_row)

    result = "PASS"
    for item in invariants:
        if item.get("required", True) and item.get("status") != "PASS":
            result = "FAIL"
            break

    not_checked = next(
        (item.get("detail") for item in invariants if item.get("status") == "NOT CHECKED" and item.get("invariant") == "production_upkeep"),
        None,
    )
    snapshot = verified["snapshot"]
    return {
        "result": result,
        "mode": config.mode,
        "seed": config.seed,
        "players": config.players,
        "ticks": ticks,
        "command_rate": config.command_rate,
        "duration_seconds": duration,
        "step_seconds": step_or_pause if config.mode == "ci" else None,
        "staging_pause_seconds": step_or_pause if config.mode == "staging" else None,
        "base_url": base_url,
        "thresholds": thresholds,
        "counts": counts,
        "latency_ms": latency,
        "max_event_lag_seconds": verified["max_event_lag_seconds"],
        "end_event_lag_seconds": verified["end_event_lag_seconds"],
        "trace_verdicts": verified["traces"],
        "not_checked_summary": not_checked,
        "legacy": verified["legacy"],
        "audit_chain": verified["audit_chain"],
        "monitoring": verified["monitoring"],
        "world_checksum": snapshot.get("checksum"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "invariants": invariants,
        "failed_invariants": failed,
        "skipped_actions": [dict(item) for item in UNAVAILABLE_ACTIONS],
        "command_sequence": [
            {
                "tick": row["tick"],
                "player_id": row["player_id"],
                "player_name": row["player_name"],
                "profile": row["profile"],
                "action": row["action"],
                "body": row["body"],
                "result": row["result"],
                "http_status": row["http_status"],
                "trace_id": row.get("trace_id"),
            }
            for row in sequence
        ],
    }


def _schedule(config: SimConfig) -> tuple[int, int, int]:
    if config.ticks is not None and config.ticks < 1:
        raise SafetyError("--ticks must be at least 1")
    if config.duration is not None and config.duration < 1:
        raise SafetyError("--duration must be at least 1")
    if config.mode == "ci":
        if config.ticks is None and config.duration is None:
            ticks = CI_DEFAULT_TICKS
            step = CI_DEFAULT_STEP_SECONDS
        elif config.ticks is None:
            step = CI_DEFAULT_STEP_SECONDS
            ticks = max(1, int(config.duration) // step)
            step = max(1, int(config.duration) // ticks)
        elif config.duration is None:
            ticks = config.ticks
            step = CI_DEFAULT_STEP_SECONDS
        else:
            ticks = config.ticks
            step = max(1, int(config.duration) // ticks)
        if ticks > 1000:
            raise SafetyError("--ticks is capped at 1000 for a CI run")
        return ticks, ticks * step, step
    if config.ticks is None and config.duration is None:
        ticks = STAGING_DEFAULT_TICKS
        pause = STAGING_DEFAULT_PAUSE_SECONDS
    elif config.ticks is None:
        ticks = STAGING_DEFAULT_TICKS
        pause = max(0, int(config.duration) // ticks)
    elif config.duration is None:
        ticks = config.ticks
        pause = STAGING_DEFAULT_PAUSE_SECONDS
    else:
        ticks = config.ticks
        pause = max(0, int(config.duration) // ticks)
    return ticks, ticks * pause, pause


def _reload_settings() -> None:
    from simcore.config import get_settings
    from simcore.db import reset_engine

    get_settings.cache_clear()
    reset_engine()


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    if not Path("alembic.ini").is_file():
        raise SafetyError("alembic.ini was not found. Run the simulator from the repository root.")
    command.upgrade(Config("alembic.ini"), "head")


def _start_server(frozen: Any) -> tuple[uvicorn.Server, threading.Thread, int]:
    from simcore.config import get_settings
    from simcore.main import create_app

    app = create_app(settings=get_settings(), base_clock=frozen)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    thread = threading.Thread(target=server.run, name="simcore-sim-api", daemon=True)
    thread.start()
    return server, thread, port


def _wait_ready(api: ApiClient) -> None:
    last = ""
    for _ in range(50):
        try:
            status, body = api.json("GET", "/health/ready")
        except Exception as exc:
            last = exc.__class__.__name__
            time.sleep(0.1)
            continue
        if status == 200:
            return
        last = str(body)
        time.sleep(0.1)
    raise RuntimeError(f"API did not become ready: {last}")


def _login_bots(api: ApiClient, config: SimConfig) -> list[dict[str, Any]]:
    if config.mode == "ci":
        names = [f"Bot{index:02d}" for index in range(1, config.players + 1)]
    else:
        status, body = api.json("GET", "/v1/admin/players", headers=api.admin_headers)
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"could not list players (HTTP {status})")
        rows = body.get("players") or []
        if len(rows) < config.players:
            raise RuntimeError(
                f"the server has {len(rows)} players and --players is {config.players}. "
                "Staging does not create players."
            )
        names = [str(row["name"]) for row in rows[: config.players]]
    bots = []
    for index, name in enumerate(names):
        status, body = api.json("POST", "/v1/auth/dev-login", json={"name": name})
        if status != 200 or not isinstance(body, dict) or "token" not in body:
            raise RuntimeError(f"dev login for {name} failed (HTTP {status})")
        bots.append(
            {
                "id": int(body["player_id"]),
                "name": str(body.get("player_name") or name),
                "token": str(body["token"]),
                "profile": profile_for(index),
            }
        )
    bots.sort(key=lambda bot: int(bot["id"]))
    return bots


def _view(api: ApiClient, bot: dict[str, Any]) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {bot['token']}"}
    me_status, me = api.json("GET", "/v1/me", headers=headers)
    city_status, cities = api.json("GET", "/v1/me/cities", headers=headers)
    army_status, armies = api.json("GET", "/v1/me/armies", headers=headers)
    map_status, world = api.json("GET", "/v1/map/cities", headers=headers)
    for status, label in (
        (me_status, "me"),
        (city_status, "cities"),
        (army_status, "armies"),
        (map_status, "map"),
    ):
        if status != 200:
            raise RuntimeError(f"GET /v1 {label} for {bot['name']} returned HTTP {status}")
    return {
        "id": bot["id"],
        "name": me.get("name") or bot["name"],
        "profile": bot["profile"],
        "armies": armies.get("armies") or [],
        "cities": cities.get("cities") or [],
        "world_cities": world.get("cities") or [],
    }


def _advance(api: ApiClient, seconds: int) -> None:
    status, body = api.json(
        "POST",
        "/v1/admin/clock/advance",
        headers=api.admin_headers,
        json={"seconds": seconds, "minutes": 0, "hours": 0},
    )
    if status != 200:
        raise RuntimeError(f"clock advance failed (HTTP {status}): {body}")


def _sample_lag(api: ApiClient) -> float:
    status, body = api.json("GET", "/v1/admin/monitoring", headers=api.admin_headers)
    if status != 200 or not isinstance(body, dict):
        return 0.0
    for check in body.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == "event_queue.lag":
            value = check.get("value")
            if isinstance(value, (int, float)):
                return float(value)
    return 0.0


def _drain(api: ApiClient) -> dict[str, Any]:
    processed: list[int] = []
    failed: list[int] = []
    paused = False
    for _ in range(400):
        status, body = api.json(
            "POST",
            "/v1/admin/worker/tick",
            headers=api.admin_headers,
            params={"limit": 50},
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"worker tick failed (HTTP {status})")
        processed.extend(int(event_id) for event_id in body.get("event_ids") or [])
        failed.extend(int(event_id) for event_id in body.get("failed_ids") or [])
        paused = bool(body.get("paused"))
        if paused or (int(body.get("processed") or 0) == 0 and int(body.get("failed") or 0) == 0):
            break
    return {"processed": processed, "failed": failed, "paused": paused}


def _catch_up(api: ApiClient) -> dict[str, Any]:
    """Advance to each future due_at and drain. Stops when nothing is pending."""

    from simcore.constants import EventStatus
    from simcore.db import get_sessionmaker
    from simcore.models import Event

    failed: list[int] = []
    max_lag = 0.0
    for _ in range(100):
        session = get_sessionmaker()()
        try:
            session.execute(text("SET TRANSACTION READ ONLY"))
            pending = int(
                session.scalar(
                    select(func.count()).select_from(Event).where(Event.status == EventStatus.PENDING)
                )
                or 0
            )
            due = session.scalar(
                select(func.min(Event.due_at)).where(Event.status == EventStatus.PENDING)
            )
        finally:
            session.rollback()
            session.close()
        if pending == 0:
            break
        now = _server_now(api)
        if due is not None:
            due_at = due if due.tzinfo else due.replace(tzinfo=timezone.utc)
            if due_at > now:
                seconds = max(1, math.ceil((due_at - now).total_seconds()))
                _advance(api, seconds)
        max_lag = max(max_lag, _sample_lag(api))
        drained = _drain(api)
        failed.extend(drained["failed"])
        if drained["paused"]:
            raise RuntimeError("worker is paused during catch-up")
        if not drained["processed"] and not drained["failed"]:
            break
    return {"failed": failed, "max_lag": max_lag}


def _server_now(api: ApiClient) -> datetime:
    status, body = api.json("GET", "/v1/time")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError("GET /v1/time failed")
    raw = body.get("server_time")
    if not isinstance(raw, str):
        raise RuntimeError("server_time was not a string")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
