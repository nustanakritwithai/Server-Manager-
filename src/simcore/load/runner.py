"""Local load and failure run, or external load-only traffic.

Local mode migrates a loopback test database, starts the API and worker, and
kills processes it started. External mode only sends HTTP. It does not kill
processes and it does not seed a world that already has cities.
"""

from __future__ import annotations

import os
import platform
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
from sqlalchemy import text
from sqlalchemy.engine.url import make_url

from simcore.load.httpapi import HttpApi
from simcore.load.invariants import lag_samples, open_readonly, wall_completion_lag
from simcore.load.metrics import Recorder, number_summary, summarize
from simcore.load.processes import ProcessSet, child_env
from simcore.load.report import overall, write_report
from simcore.load.safety import LoadSafetyError, ensure_bounds, ensure_external_target, ensure_local_target
from simcore.load.scenarios import (
    PlayerSlot,
    RunContext,
    check_failure_server_errors,
    check_load_errors,
    _REQUIRED_ROUTES,
    check_route_mix,
    refresh_views,
    run_mixed_load,
    scenario_api_restart,
    scenario_clock_during_processing,
    scenario_idempotency,
    scenario_kill_worker,
    scenario_postgres_drop,
    scenario_postgres_restart,
    scenario_refresh_race,
    scenario_snapshot_during_load,
    scenario_two_workers,
    scripted_mix,
)
from simcore.monitoring import git_commit
from simcore.sim.auth_flow import bot_password


@dataclass
class LoadConfig:
    mode: str
    players: int
    seed: int
    rate: float
    duration: float
    concurrency: int
    failures: bool
    base_url: str | None
    database_url: str | None
    report_dir: Path
    allow_production: bool
    admin_token: str
    profile: str


def run(config: LoadConfig) -> dict[str, Any]:
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    if config.failures and config.mode != "local":
        raise LoadSafetyError("failure injection only runs in local mode")
    if config.mode == "local":
        if not config.database_url:
            raise LoadSafetyError("local mode needs SIMCORE_DATABASE_URL or --database-url")
        ensure_local_target(config.database_url)
        ensure_bounds(
            mode="local",
            players=config.players,
            rate=config.rate,
            concurrency=config.concurrency,
            duration=config.duration,
        )
        payload = _run_local(config, started_at=started_at, started=started)
    else:
        ensure_external_target(
            base_url=config.base_url,
            database_url=config.database_url,
            env_name=os.environ.get("SIMCORE_ENV"),
            allow_production=config.allow_production,
            rate=config.rate,
            concurrency=config.concurrency,
            duration=config.duration,
            players=config.players,
            failures=config.failures,
        )
        ensure_bounds(
            mode="external",
            players=config.players,
            rate=config.rate,
            concurrency=config.concurrency,
            duration=config.duration,
        )
        payload = _run_external(config, started_at=started_at, started=started)
    payload["result"] = overall(payload["checks"])
    payload["elapsed_seconds"] = time.perf_counter() - started
    payload["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_report(config.report_dir, payload)
    return payload


def _run_local(config: LoadConfig, *, started_at: str, started: float) -> dict[str, Any]:
    assert config.database_url is not None
    os.environ["SIMCORE_DATABASE_URL"] = config.database_url
    os.environ["SIMCORE_ADMIN_TOKEN"] = config.admin_token
    os.environ["SIMCORE_ENV"] = "development"
    _reload_settings()
    _migrate()
    _refuse_existing_players()
    processes = ProcessSet(
        env=child_env(config.database_url, config.admin_token),
        log_dir=config.report_dir / "logs",
    )
    recorder = Recorder()
    ctx: RunContext | None = None
    sampler: _Sampler | None = None
    try:
        base_url = processes.start_api()
        api = HttpApi(base_url, config.admin_token, recorder)
        players = _register(api, config.players, config.seed)
        _attach_holdings([player.name for player in players])
        ctx = RunContext(
            api=api,
            players=players,
            processes=processes,
            checks=[],
            database_url=config.database_url,
            seed=config.seed,
            manage_processes=True,
        )
        refresh_views(ctx)
        scripted_mix(ctx)
        sampler = _Sampler(ctx)
        sampler.start()
        if config.failures:
            _failure_suite(ctx)
        elapsed = run_mixed_load(
            ctx,
            duration=config.duration,
            rate=config.rate,
            concurrency=config.concurrency,
        )
        from simcore.load.scenarios import drain

        if not drain(ctx, timeout=90):
            ctx.add("after_load_drain", "FAIL", "due events remained after the mixed load")
        else:
            ctx.add("after_load_drain", "PASS", "no due events remained after the mixed load")
        ctx.processes.stop_workers(kill=False)
        ctx.invariants("after_load")
        check_route_mix(ctx)
        ctx.checks.append(check_load_errors(recorder))
        ctx.checks.append(check_failure_server_errors(recorder))
        scenario_refresh_race(ctx)
        ctx.invariants("final")
        sampler.stop()
        return _payload(config, ctx, recorder, sampler, load_elapsed=elapsed, started_at=started_at, started=started)
    except Exception as exc:
        if ctx is None:
            checks = [
                {
                    "name": "harness",
                    "status": "FAIL",
                    "detail": f"{exc.__class__.__name__}: {exc}",
                    "required": True,
                }
            ]
            players_n = 0
        else:
            ctx.add("harness", "FAIL", f"{exc.__class__.__name__}: {exc}")
            checks = ctx.checks
            players_n = len(ctx.players)
        if sampler is not None:
            sampler.stop()
        return _shell(
            config,
            checks=checks,
            recorder=recorder,
            sampler=sampler,
            load_elapsed=None,
            started_at=started_at,
            started=started,
            base_url=processes.base_url if processes.api_port else None,
            players=players_n,
        )
    finally:
        if ctx is not None:
            ctx.api.close()
        processes.close()
        _reload_settings()


def _run_external(config: LoadConfig, *, started_at: str, started: float) -> dict[str, Any]:
    if not config.base_url:
        raise LoadSafetyError("external mode needs --base-url")
    recorder = Recorder()
    api = HttpApi(config.base_url, config.admin_token, recorder)
    processes = ProcessSet(env={}, log_dir=config.report_dir / "logs")
    ctx: RunContext | None = None
    sampler: _Sampler | None = None
    try:
        players = _register(api, config.players, config.seed)
        if config.database_url:
            ensure_local_target(config.database_url)
            os.environ["SIMCORE_DATABASE_URL"] = config.database_url
            os.environ["SIMCORE_ENV"] = "development"
            _reload_settings()
            _attach_holdings([player.name for player in players])
        ctx = RunContext(
            api=api,
            players=players,
            processes=processes,
            checks=[],
            database_url=config.database_url or "",
            seed=config.seed,
            manage_processes=False,
        )
        if config.database_url:
            refresh_views(ctx)
        else:
            ctx.add(
                "holdings",
                "INCOMPLETE",
                "external mode had no local test database, so cities were not seeded and invariant SQL was not run",
            )
        scripted_mix(ctx)
        sampler = _Sampler(ctx)
        sampler.start()
        elapsed = run_mixed_load(
            ctx,
            duration=config.duration,
            rate=config.rate,
            concurrency=config.concurrency,
        )
        check_route_mix(ctx)
        ctx.checks.append(check_load_errors(recorder))
        if config.database_url:
            ctx.invariants("external")
        sampler.stop()
        return _payload(config, ctx, recorder, sampler, load_elapsed=elapsed, started_at=started_at, started=started)
    except Exception as exc:
        checks = [] if ctx is None else ctx.checks
        if ctx is None:
            checks = [
                {
                    "name": "harness",
                    "status": "FAIL",
                    "detail": f"{exc.__class__.__name__}: {exc}",
                    "required": True,
                }
            ]
        else:
            ctx.add("harness", "FAIL", f"{exc.__class__.__name__}: {exc}")
            checks = ctx.checks
        if sampler is not None:
            sampler.stop()
        return _shell(
            config,
            checks=checks,
            recorder=recorder,
            sampler=sampler,
            load_elapsed=None,
            started_at=started_at,
            started=started,
            base_url=config.base_url,
            players=0 if ctx is None else len(ctx.players),
        )
    finally:
        api.close()


def _failure_suite(ctx: RunContext) -> None:
    scenario_kill_worker(ctx)
    scenario_two_workers(ctx)
    scenario_api_restart(ctx)
    scenario_postgres_drop(ctx)
    scenario_postgres_restart(ctx)
    scenario_idempotency(ctx)
    scenario_clock_during_processing(ctx)
    scenario_snapshot_during_load(ctx)


def _register(api: HttpApi, count: int, seed: int) -> list[PlayerSlot]:
    slots: list[PlayerSlot] = []
    for index in range(count):
        name = f"Bot{index + 1:02d}"
        password = bot_password(seed, name, kind="play")
        status, body = api.call(
            "POST",
            "/v1/auth/register",
            phase="setup",
            json={"username": name, "password": password},
        )
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            raise RuntimeError(f"register {name} failed with HTTP {status}")
        slots.append(
            PlayerSlot(
                name=name,
                password="",
                player_id=int(body["player_id"]),
                access_token=str(body["access_token"]),
                refresh_token=str(body["refresh_token"]),
            )
        )
    return slots


def _attach_holdings(names: list[str]) -> None:
    from simcore.clock import OffsetClock, SystemClock
    from simcore.db import get_sessionmaker
    from simcore.sim.seed_world import seed_holdings

    session = get_sessionmaker()()
    try:
        now = OffsetClock(session, SystemClock()).now()
        seed_holdings(session, now, names, roster="default")
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _refuse_existing_players() -> None:
    from simcore.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        count = int(session.execute(text("SELECT count(*)::int FROM players")).scalar_one() or 0)
    finally:
        session.rollback()
        session.close()
    if count:
        raise LoadSafetyError(
            f"database already has {count} players. This tool does not delete them. Use a fresh database."
        )


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    if not Path("alembic.ini").is_file():
        raise LoadSafetyError("alembic.ini was not found. Run this from the repository root.")
    command.upgrade(Config("alembic.ini"), "head")


def _reload_settings() -> None:
    from simcore.config import get_settings
    from simcore.db import reset_engine

    get_settings.cache_clear()
    reset_engine()


def _payload(
    config: LoadConfig,
    ctx: RunContext,
    recorder: Recorder,
    sampler: _Sampler,
    *,
    load_elapsed: float,
    started_at: str,
    started: float,
) -> dict[str, Any]:
    body = _shell(
        config,
        checks=ctx.checks,
        recorder=recorder,
        sampler=sampler,
        load_elapsed=load_elapsed,
        started_at=started_at,
        started=started,
        base_url=ctx.api.base_url,
        players=len(ctx.players),
    )
    if config.database_url and config.mode == "local":
        body["worker_lag"] = _lag_report(sampler)
        body["database"] = _database_report(sampler)
    return body


def _shell(
    config: LoadConfig,
    *,
    checks: list[dict[str, Any]],
    recorder: Recorder,
    sampler: _Sampler | None,
    load_elapsed: float | None,
    started_at: str,
    started: float,
    base_url: str | None,
    players: int,
) -> dict[str, Any]:
    load_rows = recorder.rows_for("load")
    resources = sampler.resources() if sampler is not None else _unknown_resources("sampler did not start")
    database = sampler.database() if sampler is not None else _unknown_database("sampler did not start")
    lag = {
        "processed_at_minus_due_at_seconds": {"status": "UNKNOWN", "reason": "not queried"},
        "monitoring_event_queue_lag_seconds": database.pop("_lag_preview", {"status": "UNKNOWN"}),
        "wall_clock_completion_minus_due_seconds": {
            "status": "UNKNOWN",
            "reason": "not queried",
        },
        "note": _LAG_NOTE,
    }
    if sampler is not None and config.mode == "local":
        measured = _lag_report(sampler)
        lag = measured
    return {
        "profile": config.profile,
        "mode": config.mode,
        "seed": config.seed,
        "git_commit": git_commit(),
        "started_at": started_at,
        "finished_at": None,
        "elapsed_seconds": time.perf_counter() - started,
        "host": _host(),
        "config": {
            "players": config.players,
            "registered_players": players,
            "rate_per_second": config.rate,
            "duration_seconds": config.duration,
            "concurrency": config.concurrency,
            "failures": config.failures,
            "base_url": base_url,
            "database": _redact(config.database_url),
            "command_rate_limit_in_child": 1000 if config.mode == "local" else None,
        },
        "checks": checks,
        "load": _with_missing_routes(summarize(load_rows, elapsed_seconds=load_elapsed, target_rps=config.rate)),
        "scripted": summarize(recorder.rows_for("scripted"), elapsed_seconds=None, target_rps=None),
        "failure_http": summarize(recorder.rows_for("failure"), elapsed_seconds=None, target_rps=None),
        "worker_lag": lag,
        "database": {key: value for key, value in database.items() if key != "_lag_preview"},
        "resources": resources,
        "sample_interval_seconds": 0.4 if sampler is not None else None,
    }


_LAG_NOTE = (
    "processed_at on events is the game time the worker stored, which this server sets to due_at. "
    "A measured difference near zero confirms that stored timestamp. It is not wall-clock delay. "
    "monitoring event_queue.lag is game time minus the oldest due event, sampled about every 0.4s. "
    "Gaps between samples are not filled in. wall_at minus due_at is reported only while the game "
    "clock offset is still zero; after a clock advance it is NOT INSTRUMENTED."
)


def _lag_report(sampler: _Sampler) -> dict[str, Any]:
    stored: dict[str, Any]
    wall: dict[str, Any]
    try:
        session = open_readonly()
    except Exception as exc:
        stored = {"status": "UNKNOWN", "reason": exc.__class__.__name__, "samples": []}
        wall = {"status": "UNKNOWN", "reason": exc.__class__.__name__}
        session = None
    else:
        try:
            stored = lag_samples(session)
            wall = wall_completion_lag(session)
        except Exception as exc:
            stored = {"status": "UNKNOWN", "reason": exc.__class__.__name__, "samples": []}
            wall = {"status": "UNKNOWN", "reason": exc.__class__.__name__}
        finally:
            session.rollback()
            session.close()
    samples = stored.get("samples") or []
    processed = number_summary([float(value) for value in samples], unit="seconds")
    if stored.get("status") == "UNKNOWN" and not samples:
        processed = {
            "status": "UNKNOWN",
            "unit": "seconds",
            "n": 0,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
            "reason": stored.get("reason") or "no completed events had processed_at",
        }
    wall_block: dict[str, Any]
    if wall.get("status") == "MEASURED":
        wall_block = number_summary([float(value) for value in wall.get("samples") or []], unit="seconds")
    else:
        wall_block = {
            "status": wall.get("status", "UNKNOWN"),
            "unit": "seconds",
            "reason": wall.get("reason"),
            "offset_seconds": wall.get("offset_seconds"),
        }
    return {
        "processed_at_minus_due_at_seconds": processed,
        "monitoring_event_queue_lag_seconds": number_summary(sampler.queue_lag, unit="seconds")
        if sampler.queue_lag
        else {
            "status": "UNKNOWN",
            "unit": "seconds",
            "n": 0,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
            "reason": "GET /v1/admin/monitoring did not return a measured event_queue.lag sample",
        },
        "wall_clock_completion_minus_due_seconds": wall_block,
        "note": _LAG_NOTE,
        "monitoring_lag_samples": len(sampler.queue_lag),
        "monitoring_lag_missing_samples": sampler.lag_misses,
    }


def _database_report(sampler: _Sampler) -> dict[str, Any]:
    base = sampler.database()
    base.pop("_lag_preview", None)
    return base


def _with_missing_routes(summary: dict[str, Any]) -> dict[str, Any]:
    """A required route with no load-phase samples stays UNKNOWN. It is not omitted."""

    by_route = summary.setdefault("latency_by_route", {})
    for route in _REQUIRED_ROUTES:
        if route not in by_route:
            by_route[route] = {
                "status": "UNKNOWN",
                "n": 0,
                "p50_ms": None,
                "p95_ms": None,
                "p99_ms": None,
                "max_ms": None,
                "reason": "no load-phase samples",
            }
    summary["latency_by_route"] = dict(sorted(by_route.items()))
    return summary


def _host() -> dict[str, Any]:
    try:
        memory = psutil.virtual_memory().total
    except Exception as exc:
        return {"status": "UNKNOWN", "reason": exc.__class__.__name__}
    return {
        "status": "MEASURED",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": int(memory),
        "runner_os": os.environ.get("RUNNER_OS") or None,
        "runner_name": os.environ.get("RUNNER_NAME") or None,
    }


def _redact(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = make_url(url)
    except Exception:
        return "unparsed"
    if parsed.password:
        parsed = parsed.set(password="***")
    return str(parsed)


def _unknown_resources(reason: str) -> dict[str, Any]:
    block = {"status": "UNKNOWN", "reason": reason, "unit": "", "n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "host_cpu_percent": dict(block),
        "host_memory_percent": dict(block),
        "child_process_cpu_percent": {"status": "NOT INSTRUMENTED", "reason": reason},
        "child_process_rss_bytes": {"status": "NOT INSTRUMENTED", "reason": reason},
        "note": "Samples are taken about every 0.4 seconds. Nothing is interpolated.",
    }


def _unknown_database(reason: str) -> dict[str, Any]:
    block = {"status": "UNKNOWN", "reason": reason}
    return {
        "api_pool_utilization": dict(block),
        "worker_pool_utilization": dict(block),
        "pg_stat_activity_backends": dict(block),
        "_lag_preview": dict(block),
        "note": (
            "API pool figures come from database.pool on GET /v1/admin/monitoring. "
            "Worker pool figures come from worker.pool on the same response. "
            "pg_stat_activity_backends is a count of client backends, not pool utilization."
        ),
    }


class _Sampler:
    """Background samples. A failed read is skipped, not stored as zero."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.queue_lag: list[float] = []
        self.api_pool: list[float] = []
        self.worker_pool: list[float] = []
        self.backends: list[float] = []
        self.host_cpu: list[float] = []
        self.host_memory: list[float] = []
        self.child_cpu: list[float] = []
        self.child_rss: list[float] = []
        self.lag_misses = 0
        self.pool_misses = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._processes: dict[int, psutil.Process] = {}

    def start(self) -> None:
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
        self._thread = threading.Thread(target=self._loop, name="simcore-load-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(0.4)

    def _sample(self) -> None:
        self._sample_http()
        self._sample_backends()
        self._sample_cpu()

    def _sample_http(self) -> None:
        try:
            status, body = self.ctx.api.call(
                "GET",
                "/v1/admin/monitoring",
                phase="sample",
                headers=self.ctx.api.admin_headers,
                record=False,
                timeout=5,
            )
        except Exception:
            self.lag_misses += 1
            self.pool_misses += 1
            return
        if status != 200 or not isinstance(body, dict):
            self.lag_misses += 1
            self.pool_misses += 1
            return
        checks = body.get("checks")
        if not isinstance(checks, list):
            self.lag_misses += 1
            return
        found_lag = False
        found_api = False
        found_worker = False
        for item in checks:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            item_status = item.get("status")
            if item_status in {"UNKNOWN", "NOT INSTRUMENTED"} or not isinstance(value, (int, float)):
                continue
            if name == "event_queue.lag":
                self.queue_lag.append(float(value))
                found_lag = True
            elif name == "database.pool":
                self.api_pool.append(float(value))
                found_api = True
            elif name == "worker.pool":
                self.worker_pool.append(float(value))
                found_worker = True
        if not found_lag:
            self.lag_misses += 1
        if not found_api or not found_worker:
            self.pool_misses += 1

    def _sample_backends(self) -> None:
        if not self.ctx.database_url:
            return
        try:
            import psycopg

            parsed = make_url(self.ctx.database_url)
            conn = psycopg.connect(
                host=parsed.host or "127.0.0.1",
                port=parsed.port or 5432,
                user=parsed.username,
                password=parsed.password,
                dbname=parsed.database,
                connect_timeout=2,
            )
            try:
                row = conn.execute(
                    """
                    SELECT count(*)::int
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                    """
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            return
        if row is not None:
            self.backends.append(float(row[0]))

    def _sample_cpu(self) -> None:
        try:
            self.host_cpu.append(float(psutil.cpu_percent(interval=None)))
            self.host_memory.append(float(psutil.virtual_memory().percent))
        except Exception:
            pass
        if not self.ctx.manage_processes:
            return
        pids: list[int] = []
        api_pid = self.ctx.processes.api_pid()
        if api_pid is not None:
            pids.append(api_pid)
        pids.extend(self.ctx.processes.worker_pids())
        cpu = 0.0
        rss = 0.0
        seen = 0
        for pid in pids:
            proc = self._processes.get(pid)
            if proc is None:
                try:
                    proc = psutil.Process(pid)
                    proc.cpu_percent(interval=None)
                except psutil.Error:
                    continue
                self._processes[pid] = proc
                continue
            try:
                cpu += float(proc.cpu_percent(interval=None))
                rss += float(proc.memory_info().rss)
                seen += 1
            except psutil.Error:
                self._processes.pop(pid, None)
        if seen:
            self.child_cpu.append(cpu)
            self.child_rss.append(rss)

    def resources(self) -> dict[str, Any]:
        child_reason = "this run did not start child processes" if not self.ctx.manage_processes else "no child sample"
        return {
            "host_cpu_percent": _measured(self.host_cpu, "percent"),
            "host_memory_percent": _measured(self.host_memory, "percent"),
            "child_process_cpu_percent": _measured(self.child_cpu, "percent")
            if self.child_cpu
            else {"status": "NOT INSTRUMENTED" if not self.ctx.manage_processes else "UNKNOWN", "reason": child_reason},
            "child_process_rss_bytes": _measured(self.child_rss, "bytes")
            if self.child_rss
            else {"status": "NOT INSTRUMENTED" if not self.ctx.manage_processes else "UNKNOWN", "reason": child_reason},
            "note": "Samples are taken about every 0.4 seconds. The first CPU reading of a process is discarded. Nothing is interpolated.",
        }

    def database(self) -> dict[str, Any]:
        return {
            "api_pool_utilization": _measured(self.api_pool, "fraction")
            if self.api_pool
            else {
                "status": "UNKNOWN",
                "reason": "database.pool was not present on the monitoring samples",
            },
            "worker_pool_utilization": _measured(self.worker_pool, "fraction")
            if self.worker_pool
            else {
                "status": "UNKNOWN",
                "reason": "worker.pool was not present on the monitoring samples",
            },
            "pg_stat_activity_backends": _measured(self.backends, "backends")
            if self.backends
            else {
                "status": "UNKNOWN" if self.ctx.database_url else "NOT INSTRUMENTED",
                "reason": "no pg_stat_activity sample" if self.ctx.database_url else "no database URL",
            },
            "_lag_preview": _measured(self.queue_lag, "seconds") if self.queue_lag else {"status": "UNKNOWN"},
            "note": (
                "API pool figures come from database.pool on GET /v1/admin/monitoring. "
                "Worker pool figures come from worker.pool on the same response. "
                "pg_stat_activity_backends is a count of client backends, not pool utilization."
            ),
        }


def _measured(values: list[float], unit: str) -> dict[str, Any]:
    if not values:
        return {"status": "UNKNOWN", "unit": unit, "n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return number_summary(values, unit=unit)
