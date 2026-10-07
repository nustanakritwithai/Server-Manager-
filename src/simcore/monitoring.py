"""Measured operator health. Values come from the database, this process, or the OS.

Nothing here is part of the world snapshot, and nothing here changes combat,
the ledger, or world_version. A missing measurement is UNKNOWN or
NOT INSTRUMENTED. It is never reported as OK.

API request counters live in this process and reset when the API process
restarts. The worker writes heartbeats and process marks. Periodic samples
are written by the worker loop and by the API sampler; a metric is inserted
only when its previous sample is older than the sample interval.
"""

from __future__ import annotations

import logging
import math
import os
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from simcore import __version__
from simcore.audit import append_audit
from simcore.clock import Clock, SystemClock
from simcore.config import Settings, get_settings
from simcore.constants import EventStatus
from simcore.db import get_engine, get_sessionmaker
from simcore.models import (
    Event,
    MonitoringCheckState,
    MonitoringSample,
    WorkerHeartbeat,
    WorkerProcessMark,
    WorldSnapshot,
    WorldState,
    utcnow,
)

logger = logging.getLogger("simcore.monitoring")

OK = "OK"
WARN = "WARN"
CRITICAL = "CRITICAL"
UNKNOWN = "UNKNOWN"
NOT_INSTRUMENTED = "NOT INSTRUMENTED"

UP = "UP"
STALE = "STALE"
DOWN = "DOWN"

# Separate from the audit-chain lock and the worker drain lock.
_SAMPLE_LOCK = 87410004
_API_WINDOW_SECONDS = 300
_RATE_WINDOWS = (60, 300, 900)
_HISTORY_LIMIT = 10080

HISTORY_METRICS: tuple[str, ...] = (
    "event_lag_seconds",
    "oldest_due_age_seconds",
    "queue_pending",
    "queue_due",
    "queue_failed",
    "processing_rate_per_min",
    "api_error_rate",
    "api_5xx_count",
    "api_latency_p95_ms",
    "api_latency_p50_ms",
    "disk_free_bytes",
    "db_rtt_ms",
    "worker_heartbeat_age_seconds",
)

HISTORY_WINDOWS: dict[str, int] = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
}

_SEVERITY = {CRITICAL: 3, WARN: 2, UNKNOWN: 1, OK: 0}
_UNPROCESSED = (EventStatus.PENDING, EventStatus.PROCESSING)
_COMMIT_MISSING = object()
_commit_cache: str | None | object = _COMMIT_MISSING


class ApiMetrics:
    """In-process request measurements. A new process starts these at zero."""

    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        self._started_mono = time.monotonic()
        self._lock = threading.Lock()
        self.total_requests = 0
        self.total_5xx = 0
        self._samples: deque[tuple[float, float, int]] = deque()

    def reset(self) -> None:
        """Test helper. Production drops this state by restarting the process."""

        with self._lock:
            self.started_at = datetime.now(timezone.utc)
            self._started_mono = time.monotonic()
            self.total_requests = 0
            self.total_5xx = 0
            self._samples.clear()

    def record(self, latency_ms: float, status_code: int) -> None:
        if not math.isfinite(latency_ms) or latency_ms < 0:
            return
        now = time.monotonic()
        code = int(status_code)
        with self._lock:
            self.total_requests += 1
            if code >= 500:
                self.total_5xx += 1
            self._samples.append((now, latency_ms, code))
            cutoff = now - 900
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            rows = list(self._samples)
            total_requests = self.total_requests
            total_5xx = self.total_5xx
            started_at = self.started_at
            uptime = now - self._started_mono
        windows: dict[str, Any] = {}
        for seconds in (60, _API_WINDOW_SECONDS, 900):
            chosen = [row for row in rows if row[0] >= now - seconds]
            latencies = [row[1] for row in chosen]
            errors = sum(1 for row in chosen if row[2] >= 500)
            count = len(chosen)
            windows[str(seconds)] = {
                "requests": count,
                "5xx": errors,
                "error_rate": None if count == 0 else errors / count,
                "latency_p50_ms": percentile(latencies, 50),
                "latency_p95_ms": percentile(latencies, 95),
            }
        return {
            "started_at": started_at,
            "uptime_seconds": uptime,
            "total_requests": total_requests,
            "total_5xx": total_5xx,
            "windows": windows,
            "resets_on_restart": True,
        }


api_metrics = ApiMetrics()


def percentile(values: list[float], percent: float) -> float | None:
    """Nearest-rank percentile. Empty input is None, not zero."""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = math.ceil((percent / 100.0) * len(ordered))
    index = min(len(ordered) - 1, max(0, rank - 1))
    return ordered[index]


def git_commit() -> str | None:
    """Commit SHA from SIMCORE_GIT_COMMIT or `git rev-parse HEAD`.

    Cached for the process. None means this process could not read a commit.
    """

    global _commit_cache
    if _commit_cache is not _COMMIT_MISSING:
        return _commit_cache if isinstance(_commit_cache, str) else None
    override = os.environ.get("SIMCORE_GIT_COMMIT", "").strip()
    if override:
        _commit_cache = override[:64]
        return _commit_cache
    root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _commit_cache = None
        return None
    sha = (completed.stdout or "").strip().lower()
    if completed.returncode == 0 and len(sha) >= 7 and all(c in "0123456789abcdef" for c in sha):
        _commit_cache = sha
        return sha
    _commit_cache = None
    return None


def clear_commit_cache() -> None:
    global _commit_cache
    _commit_cache = _COMMIT_MISSING


def disk_target(settings: Settings) -> str | None:
    configured = settings.monitor_disk_path.strip()
    if configured:
        return configured
    anchor = Path.cwd().anchor
    if anchor:
        return anchor
    return None


def pool_snapshot() -> dict[str, Any] | None:
    """SQLAlchemy queue pool of this process. None when the pool has no counters."""

    try:
        pool = get_engine().pool
    except Exception:
        logger.exception("pool snapshot failed")
        return None
    checkedout = getattr(pool, "checkedout", None)
    size = getattr(pool, "size", None)
    if not callable(checkedout) or not callable(size):
        return None
    max_overflow = getattr(pool, "_max_overflow", None)
    if not isinstance(max_overflow, int):
        return None
    try:
        checked = int(checkedout())
        pool_size = int(size())
        overflow_fn = getattr(pool, "overflow", None)
        overflow = int(overflow_fn()) if callable(overflow_fn) else 0
    except Exception:
        logger.exception("pool counters failed")
        return None
    capacity = pool_size + max(0, max_overflow)
    if capacity <= 0:
        return None
    return {
        "checked_out": checked,
        "pool_size": pool_size,
        "overflow": overflow,
        "max_overflow": max_overflow,
        "capacity": capacity,
        "utilization": checked / capacity,
    }


def _check(
    name: str,
    status: str,
    *,
    value: object,
    unit: str | None,
    reason: str,
    affects_overall: bool,
    threshold: dict[str, object] | None = None,
    history_metric: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "status": status,
        "value": value,
        "unit": unit,
        "reason": reason,
        "affects_overall": affects_overall,
        "threshold": threshold,
        "history_metric": history_metric,
    }
    if detail:
        body["detail"] = detail
    return body


def _high(warn: float, critical: float, unit: str) -> dict[str, object]:
    return {"warn": warn, "critical": critical, "unit": unit, "comparison": "higher_is_worse"}


def _low(warn: float, critical: float, unit: str) -> dict[str, object]:
    return {"warn": warn, "critical": critical, "unit": unit, "comparison": "lower_is_worse"}


def _judge_high(value: float, warn: float, critical: float) -> str:
    if value >= critical:
        return CRITICAL
    if value >= warn:
        return WARN
    return OK


def _judge_low(value: float, warn: float, critical: float) -> str:
    if value <= critical:
        return CRITICAL
    if value <= warn:
        return WARN
    return OK


def _num(value: float) -> str:
    text_value = f"{value:.3f}".rstrip("0").rstrip(".")
    return text_value or "0"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _db_unknown(name: str, unit: str | None, reason: str, *, history_metric: str | None = None) -> dict[str, Any]:
    return _check(
        name,
        UNKNOWN,
        value=None,
        unit=unit,
        reason=reason,
        affects_overall=True,
        history_metric=history_metric,
    )


def _measure_rtt(session: Session) -> float:
    started = time.perf_counter()
    session.execute(text("SELECT 1"))
    return (time.perf_counter() - started) * 1000.0


def _database_size(session: Session) -> tuple[int, list[dict[str, object]]]:
    size = session.scalar(text("SELECT pg_database_size(current_database())"))
    rows = session.execute(
        text(
            """
            SELECT c.relname AS name, pg_total_relation_size(c.oid) AS bytes
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY pg_total_relation_size(c.oid) DESC, c.relname
            LIMIT 5
            """
        )
    ).all()
    tables = [{"name": str(row.name), "bytes": int(row.bytes)} for row in rows]
    return int(size or 0), tables


def _queue_counts(session: Session) -> dict[str, int]:
    counts = {status: 0 for status in (
        EventStatus.PENDING,
        EventStatus.PROCESSING,
        EventStatus.COMPLETED,
        EventStatus.FAILED,
        EventStatus.CANCELLED,
    )}
    for status, count in session.execute(select(Event.status, func.count()).group_by(Event.status)):
        counts[str(status)] = int(count)
    return counts


def _processing_rates(session: Session, wall_now: datetime) -> dict[int, float]:
    rates: dict[int, float] = {}
    for window in _RATE_WINDOWS:
        cutoff = wall_now - timedelta(seconds=window)
        count = session.scalar(
            select(func.count())
            .select_from(WorkerProcessMark)
            .where(WorkerProcessMark.outcome == "processed", WorkerProcessMark.wall_at >= cutoff)
        )
        rates[window] = float(count or 0) / (window / 60.0)
    return rates


def _disk_check(settings: Settings) -> dict[str, Any]:
    path = disk_target(settings)
    threshold = _low(
        settings.monitor_disk_free_warn_bytes,
        settings.monitor_disk_free_critical_bytes,
        "bytes",
    )
    if not path:
        return _check(
            "disk.free",
            NOT_INSTRUMENTED,
            value=None,
            unit="bytes",
            reason="No disk path is configured and the process has no working-directory drive.",
            affects_overall=False,
            threshold=threshold,
            history_metric="disk_free_bytes",
        )
    try:
        usage = os_disk_usage(path)
    except OSError as exc:
        return _check(
            "disk.free",
            NOT_INSTRUMENTED,
            value=None,
            unit="bytes",
            reason=f"shutil.disk_usage failed for {path}: {exc.__class__.__name__}.",
            affects_overall=False,
            threshold=threshold,
            history_metric="disk_free_bytes",
            detail={"path": path},
        )
    status = _judge_low(usage.free, settings.monitor_disk_free_warn_bytes, settings.monitor_disk_free_critical_bytes)
    return _check(
        "disk.free",
        status,
        value=usage.free,
        unit="bytes",
        reason=(
            f"{usage.free} bytes free on {path}; "
            f"warn at or below {settings.monitor_disk_free_warn_bytes}, "
            f"critical at or below {settings.monitor_disk_free_critical_bytes}."
        ),
        affects_overall=True,
        threshold=threshold,
        history_metric="disk_free_bytes",
        detail={"path": path, "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free},
    )


def os_disk_usage(path: str):
    import shutil

    return shutil.disk_usage(path)


def _api_checks(settings: Settings) -> list[dict[str, Any]]:
    snap = api_metrics.snapshot()
    window = snap["windows"][str(_API_WINDOW_SECONDS)]
    checks: list[dict[str, Any]] = [
        _check(
            "api.requests",
            OK,
            value=snap["total_requests"],
            unit="requests",
            reason=(
                f"{snap['total_requests']} requests since this API process started at {snap['started_at'].isoformat()}. "
                "This counter resets when the API process restarts."
            ),
            affects_overall=False,
            detail={
                "window_5m_requests": window["requests"],
                "total_5xx": snap["total_5xx"],
                "resets_on_restart": True,
            },
        ),
        _check(
            "api.uptime",
            OK,
            value=snap["uptime_seconds"],
            unit="seconds",
            reason="Seconds since this API process started. It resets when the API process restarts.",
            affects_overall=False,
            detail={"started_at": snap["started_at"], "resets_on_restart": True},
        ),
    ]
    five_xx = int(window["5xx"])
    five_status = _judge_high(five_xx, settings.monitor_api_5xx_warn, settings.monitor_api_5xx_critical)
    checks.append(
        _check(
            "api.5xx",
            five_status,
            value=five_xx,
            unit="responses",
            reason=(
                f"{five_xx} responses with status >= 500 in the last {_API_WINDOW_SECONDS} seconds "
                f"of this API process; warn at {settings.monitor_api_5xx_warn}, "
                f"critical at {settings.monitor_api_5xx_critical}. "
                "The count resets when the API process restarts."
            ),
            affects_overall=True,
            threshold=_high(settings.monitor_api_5xx_warn, settings.monitor_api_5xx_critical, "responses"),
            history_metric="api_5xx_count",
            detail={"window_seconds": _API_WINDOW_SECONDS, "resets_on_restart": True},
        )
    )
    rate = window["error_rate"]
    if rate is None:
        checks.append(
            _check(
                "api.error_rate",
                UNKNOWN,
                value=None,
                unit="fraction",
                reason=(
                    f"No requests in the last {_API_WINDOW_SECONDS} seconds of this API process, "
                    "so the 5xx rate is undefined. This is not a pass. "
                    "The window resets when the API process restarts."
                ),
                affects_overall=False,
                threshold=_high(
                    settings.monitor_api_error_rate_warn,
                    settings.monitor_api_error_rate_critical,
                    "fraction",
                ),
                history_metric="api_error_rate",
                detail={"window_seconds": _API_WINDOW_SECONDS, "requests": 0, "resets_on_restart": True},
            )
        )
    else:
        rate_status = _judge_high(
            rate, settings.monitor_api_error_rate_warn, settings.monitor_api_error_rate_critical
        )
        checks.append(
            _check(
                "api.error_rate",
                rate_status,
                value=rate,
                unit="fraction",
                reason=(
                    f"5xx rate { _num(rate) } over {window['requests']} requests in the last "
                    f"{_API_WINDOW_SECONDS} seconds; warn at {settings.monitor_api_error_rate_warn}, "
                    f"critical at {settings.monitor_api_error_rate_critical}. "
                    "Resets when the API process restarts."
                ),
                affects_overall=True,
                threshold=_high(
                    settings.monitor_api_error_rate_warn,
                    settings.monitor_api_error_rate_critical,
                    "fraction",
                ),
                history_metric="api_error_rate",
                detail={"window_seconds": _API_WINDOW_SECONDS, "requests": window["requests"], "resets_on_restart": True},
            )
        )
    for name, key, metric in (
        ("api.latency_p50", "latency_p50_ms", "api_latency_p50_ms"),
        ("api.latency_p95", "latency_p95_ms", "api_latency_p95_ms"),
    ):
        latency = window[key]
        threshold = _high(settings.monitor_api_p95_warn_ms, settings.monitor_api_p95_critical_ms, "milliseconds")
        if latency is None:
            checks.append(
                _check(
                    name,
                    UNKNOWN,
                    value=None,
                    unit="milliseconds",
                    reason=(
                        f"No requests in the last {_API_WINDOW_SECONDS} seconds, so {name} was not measured. "
                        "This is not a pass. Resets when the API process restarts."
                    ),
                    affects_overall=False,
                    threshold=threshold,
                    history_metric=metric,
                    detail={"window_seconds": _API_WINDOW_SECONDS, "resets_on_restart": True},
                )
            )
            continue
        # p50 is reported with the same thresholds as p95 so both have a line.
        # Only p95 changes overall status. p50 stays visible either way.
        status = _judge_high(latency, settings.monitor_api_p95_warn_ms, settings.monitor_api_p95_critical_ms)
        affects = name == "api.latency_p95"
        checks.append(
            _check(
                name,
                status,
                value=latency,
                unit="milliseconds",
                reason=(
                    f"{name} is {_num(latency)} ms over {window['requests']} requests in the last "
                    f"{_API_WINDOW_SECONDS} seconds; warn at {settings.monitor_api_p95_warn_ms} ms, "
                    f"critical at {settings.monitor_api_p95_critical_ms} ms. "
                    "Resets when the API process restarts."
                ),
                affects_overall=affects,
                threshold=threshold,
                history_metric=metric,
                detail={"window_seconds": _API_WINDOW_SECONDS, "resets_on_restart": True, "affects_overall": affects},
            )
        )
    return checks


def _host_gaps() -> list[dict[str, Any]]:
    return [
        _check(
            "host.cpu",
            NOT_INSTRUMENTED,
            value=None,
            unit=None,
            reason="CPU use is not measured by this server.",
            affects_overall=False,
        ),
        _check(
            "host.memory",
            NOT_INSTRUMENTED,
            value=None,
            unit=None,
            reason="Memory use is not measured by this server.",
            affects_overall=False,
        ),
    ]


def _commit_check() -> dict[str, Any]:
    commit = git_commit()
    if commit is None:
        return _check(
            "build.commit",
            NOT_INSTRUMENTED,
            value=None,
            unit=None,
            reason="No SIMCORE_GIT_COMMIT value and git rev-parse HEAD did not return a commit.",
            affects_overall=False,
            detail={"version": __version__},
        )
    return _check(
        "build.commit",
        OK,
        value=commit,
        unit=None,
        reason=f"Commit {commit} for version {__version__}.",
        affects_overall=False,
        detail={"version": __version__},
    )


def _queue_checks(session: Session, game_now: datetime | None, wall_now: datetime, settings: Settings) -> list[dict[str, Any]]:
    if game_now is None:
        reason = "Game clock is unavailable, so queue lag was not measured. This is not a pass."
        names = (
            ("event_queue.pending", "events", "queue_pending"),
            ("event_queue.due", "events", "queue_due"),
            ("event_queue.lag", "seconds", "event_lag_seconds"),
            ("event_queue.oldest_due_age", "seconds", "oldest_due_age_seconds"),
            ("event_queue.failed", "events", "queue_failed"),
            ("event_queue.retried", "events", None),
            ("event_queue.processing", "events", None),
        )
        return [
            _db_unknown(name, unit, reason, history_metric=metric) for name, unit, metric in names
        ]
    counts = _queue_counts(session)
    pending = counts[EventStatus.PENDING]
    processing = counts[EventStatus.PROCESSING]
    failed = counts[EventStatus.FAILED]
    due = int(
        session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.status.in_(_UNPROCESSED), Event.due_at <= game_now)
        )
        or 0
    )
    retried = int(
        session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.status == EventStatus.PENDING, Event.attempts > 0)
        )
        or 0
    )
    oldest = session.scalar(
        select(func.min(Event.due_at)).where(Event.status.in_(_UNPROCESSED), Event.due_at <= game_now)
    )
    histogram = {status: counts[status] for status in counts}
    pending_status = _judge_high(pending, settings.monitor_queue_pending_warn, settings.monitor_queue_pending_critical)
    due_status = _judge_high(due, settings.monitor_queue_due_warn, settings.monitor_queue_due_critical)
    failed_status = _judge_high(failed, settings.monitor_failed_warn, settings.monitor_failed_critical)
    retried_status = _judge_high(retried, settings.monitor_retried_warn, settings.monitor_retried_critical)
    checks = [
        _check(
            "event_queue.pending",
            pending_status,
            value=pending,
            unit="events",
            reason=(
                f"{pending} events are pending; warn at {settings.monitor_queue_pending_warn}, "
                f"critical at {settings.monitor_queue_pending_critical}."
            ),
            affects_overall=True,
            threshold=_high(settings.monitor_queue_pending_warn, settings.monitor_queue_pending_critical, "events"),
            history_metric="queue_pending",
            detail={"by_status": histogram},
        ),
        _check(
            "event_queue.due",
            due_status,
            value=due,
            unit="events",
            reason=(
                f"{due} pending or processing events are due at or before game time; "
                f"warn at {settings.monitor_queue_due_warn}, critical at {settings.monitor_queue_due_critical}."
            ),
            affects_overall=True,
            threshold=_high(settings.monitor_queue_due_warn, settings.monitor_queue_due_critical, "events"),
            history_metric="queue_due",
        ),
    ]
    if oldest is None:
        checks.append(
            _check(
                "event_queue.lag",
                OK,
                value=0.0,
                unit="seconds",
                reason="No due unprocessed event. Game-time lag is 0 seconds.",
                affects_overall=True,
                threshold=_high(
                    settings.monitor_event_lag_warn_seconds,
                    settings.monitor_event_lag_critical_seconds,
                    "seconds",
                ),
                history_metric="event_lag_seconds",
                detail={"game_time": game_now},
            )
        )
        checks.append(
            _check(
                "event_queue.oldest_due_age",
                OK,
                value=0.0,
                unit="seconds",
                reason="No due unprocessed event. Wall-clock age is 0 seconds.",
                affects_overall=True,
                threshold=_high(
                    settings.monitor_oldest_due_warn_seconds,
                    settings.monitor_oldest_due_critical_seconds,
                    "seconds",
                ),
                history_metric="oldest_due_age_seconds",
            )
        )
    else:
        due_at = _aware(oldest)
        lag = (game_now - due_at).total_seconds()
        age = (wall_now - due_at).total_seconds()
        lag_status = _judge_high(
            lag, settings.monitor_event_lag_warn_seconds, settings.monitor_event_lag_critical_seconds
        )
        checks.append(
            _check(
                "event_queue.lag",
                lag_status,
                value=lag,
                unit="seconds",
                reason=(
                    f"Game time is {_num(lag)} seconds past the oldest due unprocessed event at {due_at.isoformat()}; "
                    f"warn at {settings.monitor_event_lag_warn_seconds}s, "
                    f"critical at {settings.monitor_event_lag_critical_seconds}s."
                ),
                affects_overall=True,
                threshold=_high(
                    settings.monitor_event_lag_warn_seconds,
                    settings.monitor_event_lag_critical_seconds,
                    "seconds",
                ),
                history_metric="event_lag_seconds",
                detail={"oldest_due_at": due_at, "game_time": game_now},
            )
        )
        if age < 0:
            age_status = OK
            age_reason = (
                f"Oldest due event at {due_at.isoformat()} is {_num(abs(age))} seconds ahead of the wall clock. "
                "Game-time lag is a separate check."
            )
        else:
            age_status = _judge_high(
                age, settings.monitor_oldest_due_warn_seconds, settings.monitor_oldest_due_critical_seconds
            )
            age_reason = (
                f"Oldest due event is {_num(age)} wall-clock seconds past due; "
                f"warn at {settings.monitor_oldest_due_warn_seconds}s, "
                f"critical at {settings.monitor_oldest_due_critical_seconds}s."
            )
        checks.append(
            _check(
                "event_queue.oldest_due_age",
                age_status,
                value=age,
                unit="seconds",
                reason=age_reason,
                affects_overall=True,
                threshold=_high(
                    settings.monitor_oldest_due_warn_seconds,
                    settings.monitor_oldest_due_critical_seconds,
                    "seconds",
                ),
                history_metric="oldest_due_age_seconds",
                detail={"oldest_due_at": due_at, "wall_time": wall_now},
            )
        )
    checks.append(
        _check(
            "event_queue.failed",
            failed_status,
            value=failed,
            unit="events",
            reason=(
                f"{failed} events are failed (dead after retries); "
                f"warn at {settings.monitor_failed_warn}, critical at {settings.monitor_failed_critical}."
            ),
            affects_overall=True,
            threshold=_high(settings.monitor_failed_warn, settings.monitor_failed_critical, "events"),
            history_metric="queue_failed",
        )
    )
    checks.append(
        _check(
            "event_queue.retried",
            retried_status,
            value=retried,
            unit="events",
            reason=(
                f"{retried} pending events have attempts > 0 (returned to the queue after a claim); "
                f"warn at {settings.monitor_retried_warn}, critical at {settings.monitor_retried_critical}."
            ),
            affects_overall=True,
            threshold=_high(settings.monitor_retried_warn, settings.monitor_retried_critical, "events"),
        )
    )
    if processing == 0:
        proc_status = OK
        proc_reason = "No committed event is in processing."
        proc_value: float | None = 0
    else:
        oldest_lock = session.scalar(
            select(func.min(Event.locked_at)).where(Event.status == EventStatus.PROCESSING)
        )
        if oldest_lock is None:
            proc_status = WARN
            proc_value = float(processing)
            proc_reason = f"{processing} processing events have no locked_at. Warn at 1."
        else:
            held = (game_now - _aware(oldest_lock)).total_seconds()
            proc_value = held
            proc_status = _judge_high(held, settings.monitor_processing_warn_seconds, settings.monitor_processing_critical_seconds)
            proc_reason = (
                f"{processing} processing events; the oldest lock is {_num(held)} game seconds old; "
                f"warn at {settings.monitor_processing_warn_seconds}s, "
                f"critical at {settings.monitor_processing_critical_seconds}s."
            )
    checks.append(
        _check(
            "event_queue.processing",
            proc_status,
            value=proc_value,
            unit="seconds" if processing else "events",
            reason=proc_reason,
            affects_overall=True,
            threshold=_high(
                settings.monitor_processing_warn_seconds,
                settings.monitor_processing_critical_seconds,
                "seconds",
            ),
            detail={"processing_count": processing},
        )
    )
    return checks


def _rate_check(session: Session, wall_now: datetime) -> dict[str, Any]:
    rates = _processing_rates(session, wall_now)
    return _check(
        "event_queue.processing_rate",
        OK,
        value=rates[60],
        unit="events_per_minute",
        reason=(
            f"Worker process marks: {_num(rates[60])} per minute over 60s, "
            f"{_num(rates[300])} over 5 min, {_num(rates[900])} over 15 min. "
            "Idle (0) is a measured rate, not a failure. "
            "Marks are wall-clock rows the worker writes after a tick; event.processed_at is game time and is not used."
        ),
        affects_overall=False,
        history_metric="processing_rate_per_min",
        detail={
            "per_minute_1m": rates[60],
            "per_minute_5m": rates[300],
            "per_minute_15m": rates[900],
            "source": "worker_process_marks",
        },
    )


def _heartbeat_checks(session: Session, wall_now: datetime, settings: Settings) -> list[dict[str, Any]]:
    rows = session.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_tick_at.desc())).all()
    threshold = _high(
        settings.monitor_heartbeat_warn_seconds,
        settings.monitor_heartbeat_critical_seconds,
        "seconds",
    )
    if not rows:
        return [
            _check(
                "worker.heartbeat",
                UNKNOWN,
                value=None,
                unit="seconds",
                reason="No worker heartbeat row. The worker liveness is unknown. This is not a pass.",
                affects_overall=True,
                threshold=threshold,
                history_metric="worker_heartbeat_age_seconds",
                detail={"liveness": UNKNOWN, "heartbeats": []},
            ),
            _check(
                "worker.pool",
                UNKNOWN,
                value=None,
                unit="fraction",
                reason="No worker heartbeat, so the worker connection pool was not measured. This is not a pass.",
                affects_overall=False,
            ),
        ]
    newest = rows[0]
    age = (wall_now - _aware(newest.last_tick_at)).total_seconds()
    heartbeats = [_heartbeat_body(row, wall_now) for row in rows]
    if age < 0:
        status = UNKNOWN
        liveness = UNKNOWN
        reason = (
            f"Newest heartbeat from {newest.worker_id} is {_num(abs(age))} seconds ahead of this clock. "
            "Liveness was not treated as up."
        )
        affects = True
    elif age <= settings.monitor_heartbeat_warn_seconds:
        status = OK
        liveness = UP
        reason = (
            f"Newest heartbeat from {newest.worker_id} is {_num(age)} seconds old; "
            f"warn after {settings.monitor_heartbeat_warn_seconds}s, "
            f"down after {settings.monitor_heartbeat_critical_seconds}s."
        )
        affects = True
    elif age <= settings.monitor_heartbeat_critical_seconds:
        status = WARN
        liveness = STALE
        reason = (
            f"STALE: newest heartbeat from {newest.worker_id} is {_num(age)} seconds old; "
            f"warn after {settings.monitor_heartbeat_warn_seconds}s, "
            f"down after {settings.monitor_heartbeat_critical_seconds}s."
        )
        affects = True
    else:
        status = CRITICAL
        liveness = DOWN
        reason = (
            f"DOWN: newest heartbeat from {newest.worker_id} is {_num(age)} seconds old; "
            f"critical after {settings.monitor_heartbeat_critical_seconds}s."
        )
        affects = True
    checks = [
        _check(
            "worker.heartbeat",
            status,
            value=age,
            unit="seconds",
            reason=reason,
            affects_overall=affects,
            threshold=threshold,
            history_metric="worker_heartbeat_age_seconds",
            detail={"liveness": liveness, "heartbeats": heartbeats, "worker_id": newest.worker_id},
        )
    ]
    if newest.pool_capacity is None or newest.pool_checked_out is None:
        checks.append(
            _check(
                "worker.pool",
                NOT_INSTRUMENTED,
                value=None,
                unit="fraction",
                reason="The newest heartbeat did not include worker pool counters.",
                affects_overall=False,
                detail={"worker_id": newest.worker_id, "last_tick_at": newest.last_tick_at},
            )
        )
    else:
        utilization = newest.pool_checked_out / newest.pool_capacity if newest.pool_capacity else None
        if utilization is None:
            checks.append(
                _check(
                    "worker.pool",
                    NOT_INSTRUMENTED,
                    value=None,
                    unit="fraction",
                    reason="The newest heartbeat reported a worker pool capacity of 0.",
                    affects_overall=False,
                )
            )
        else:
            pool_status = _judge_high(
                utilization, settings.monitor_db_pool_warn, settings.monitor_db_pool_critical
            )
            checks.append(
                _check(
                    "worker.pool",
                    pool_status,
                    value=utilization,
                    unit="fraction",
                    reason=(
                        f"Worker {newest.worker_id} pool utilization {_num(utilization)} "
                        f"({newest.pool_checked_out}/{newest.pool_capacity}) at {newest.last_tick_at.isoformat()}; "
                        f"warn at {settings.monitor_db_pool_warn}, critical at {settings.monitor_db_pool_critical}."
                    ),
                    affects_overall=True,
                    threshold=_high(settings.monitor_db_pool_warn, settings.monitor_db_pool_critical, "fraction"),
                    detail={
                        "worker_id": newest.worker_id,
                        "checked_out": newest.pool_checked_out,
                        "pool_size": newest.pool_size,
                        "overflow": newest.pool_overflow,
                        "capacity": newest.pool_capacity,
                        "last_tick_at": newest.last_tick_at,
                    },
                )
            )
    return checks


def _heartbeat_body(row: WorkerHeartbeat, wall_now: datetime) -> dict[str, Any]:
    return {
        "worker_id": row.worker_id,
        "pid": row.pid,
        "hostname": row.hostname,
        "version": row.version,
        "commit": row.commit_sha,
        "started_at": row.started_at,
        "last_tick_at": row.last_tick_at,
        "age_seconds": (wall_now - _aware(row.last_tick_at)).total_seconds(),
        "tick_duration_ms": row.tick_duration_ms,
        "events_processed_last_tick": row.events_processed,
        "tick_status": row.tick_status,
        "pool_checked_out": row.pool_checked_out,
        "pool_size": row.pool_size,
        "pool_overflow": row.pool_overflow,
        "pool_capacity": row.pool_capacity,
    }


def _game_checks(session: Session, game_now: datetime | None) -> list[dict[str, Any]]:
    state = session.get(WorldState, 1)
    if state is None or game_now is None:
        return [
            _check(
                "game.clock",
                UNKNOWN,
                value=None,
                unit=None,
                reason="world_state row is missing, so the game clock was not measured. This is not a pass.",
                affects_overall=True,
            ),
            _check(
                "game.world_version",
                UNKNOWN,
                value=None,
                unit=None,
                reason="world_state row is missing, so world_version was not measured. This is not a pass.",
                affects_overall=False,
            ),
            _check(
                "game.last_snapshot",
                UNKNOWN,
                value=None,
                unit=None,
                reason="Game clock is missing, so the latest snapshot was not read. This is not a pass.",
                affects_overall=False,
            ),
        ]
    latest = session.scalars(select(WorldSnapshot).order_by(WorldSnapshot.id.desc()).limit(1)).first()
    checks = [
        _check(
            "game.clock",
            OK,
            value=game_now.isoformat(),
            unit=None,
            reason=f"Game time is {game_now.isoformat()} with offset_seconds {int(state.offset_seconds)}.",
            affects_overall=False,
            detail={
                "server_time": game_now,
                "offset_seconds": int(state.offset_seconds),
                "commands_open": state.commands_open,
                "worker_paused": state.worker_paused,
            },
        ),
        _check(
            "game.world_version",
            OK,
            value=int(state.world_version),
            unit=None,
            reason=f"world_version is {int(state.world_version)}.",
            affects_overall=False,
        ),
    ]
    if latest is None:
        checks.append(
            _check(
                "game.last_snapshot",
                UNKNOWN,
                value=None,
                unit=None,
                reason="No world snapshot row exists. This is not a pass.",
                affects_overall=False,
            )
        )
    else:
        snap_status = WARN if latest.status in ("FAILED", "RESTORING", "CREATING") else OK
        checks.append(
            _check(
                "game.last_snapshot",
                snap_status,
                value=latest.created_at.isoformat(),
                unit=None,
                reason=f"Latest snapshot {latest.id} is {latest.status}, created at {latest.created_at.isoformat()}.",
                affects_overall=snap_status != OK,
                detail={
                    "snapshot_id": latest.id,
                    "status": latest.status,
                    "reason": latest.reason,
                    "world_time": latest.world_time,
                    "world_version": int(latest.world_version),
                },
            )
        )
    return checks


def _db_checks(session: Session, settings: Settings) -> tuple[list[dict[str, Any]], str | None]:
    """Return database checks and an error class name when the probe query failed.

    Failures use a savepoint so a sampler's outer transaction (and its advisory
    lock) stays open.
    """

    try:
        with session.begin_nested():
            rtt = _measure_rtt(session)
    except Exception as exc:
        name = exc.__class__.__name__
        failed = [
            _check(
                "database.connectivity",
                CRITICAL,
                value=False,
                unit=None,
                reason=f"Database probe failed: {name}.",
                affects_overall=True,
            ),
            _db_unknown("database.rtt", "milliseconds", f"Round trip was not measured because the probe failed: {name}.", history_metric="db_rtt_ms"),
            _db_unknown("database.size", "bytes", f"Database size was not measured because the probe failed: {name}."),
        ]
        return failed, name
    try:
        with session.begin_nested():
            size, tables = _database_size(session)
    except Exception as exc:
        name = exc.__class__.__name__
        return [
            _check(
                "database.connectivity",
                OK,
                value=True,
                unit=None,
                reason="SELECT 1 succeeded.",
                affects_overall=True,
            ),
            _check(
                "database.rtt",
                _judge_high(rtt, settings.monitor_db_rtt_warn_ms, settings.monitor_db_rtt_critical_ms),
                value=rtt,
                unit="milliseconds",
                reason=(
                    f"SELECT 1 round trip {_num(rtt)} ms; warn at {settings.monitor_db_rtt_warn_ms} ms, "
                    f"critical at {settings.monitor_db_rtt_critical_ms} ms."
                ),
                affects_overall=True,
                threshold=_high(settings.monitor_db_rtt_warn_ms, settings.monitor_db_rtt_critical_ms, "milliseconds"),
                history_metric="db_rtt_ms",
            ),
            _db_unknown("database.size", "bytes", f"Database size query failed: {name}. This is not a pass."),
        ], None
    size_status = _judge_high(
        size, settings.monitor_db_size_warn_bytes, settings.monitor_db_size_critical_bytes
    )
    rtt_status = _judge_high(rtt, settings.monitor_db_rtt_warn_ms, settings.monitor_db_rtt_critical_ms)
    checks = [
        _check(
            "database.connectivity",
            OK,
            value=True,
            unit=None,
            reason="SELECT 1 succeeded.",
            affects_overall=True,
        ),
        _check(
            "database.rtt",
            rtt_status,
            value=rtt,
            unit="milliseconds",
            reason=(
                f"SELECT 1 round trip {_num(rtt)} ms; warn at {settings.monitor_db_rtt_warn_ms} ms, "
                f"critical at {settings.monitor_db_rtt_critical_ms} ms."
            ),
            affects_overall=True,
            threshold=_high(settings.monitor_db_rtt_warn_ms, settings.monitor_db_rtt_critical_ms, "milliseconds"),
            history_metric="db_rtt_ms",
        ),
        _check(
            "database.size",
            size_status,
            value=size,
            unit="bytes",
            reason=(
                f"Current database is {size} bytes; warn at {settings.monitor_db_size_warn_bytes}, "
                f"critical at {settings.monitor_db_size_critical_bytes}."
            ),
            affects_overall=True,
            threshold=_high(
                settings.monitor_db_size_warn_bytes,
                settings.monitor_db_size_critical_bytes,
                "bytes",
            ),
            detail={"largest_tables": tables},
        ),
    ]
    return checks, None


def _api_pool_check(settings: Settings) -> dict[str, Any]:
    snap = pool_snapshot()
    threshold = _high(settings.monitor_db_pool_warn, settings.monitor_db_pool_critical, "fraction")
    if snap is None:
        return _check(
            "database.pool",
            NOT_INSTRUMENTED,
            value=None,
            unit="fraction",
            reason="This process's SQLAlchemy pool does not expose checked-out, size, and max overflow counters.",
            affects_overall=False,
            threshold=threshold,
            detail={"process": "api"},
        )
    status = _judge_high(snap["utilization"], settings.monitor_db_pool_warn, settings.monitor_db_pool_critical)
    return _check(
        "database.pool",
        status,
        value=snap["utilization"],
        unit="fraction",
        reason=(
            f"API process pool utilization {_num(snap['utilization'])} "
            f"({snap['checked_out']}/{snap['capacity']}); "
            f"warn at {settings.monitor_db_pool_warn}, critical at {settings.monitor_db_pool_critical}. "
            "This is the API process pool, not the worker pool."
        ),
        affects_overall=True,
        threshold=threshold,
        detail={"process": "api", **snap},
    )


def _section(session: Session, fn):
    try:
        with session.begin_nested():
            return fn()
    except Exception as exc:
        logger.exception("monitoring section failed")
        return exc


def collect_report(
    session: Session,
    *,
    game_now: datetime | None,
    settings: Settings | None = None,
    include_api: bool = True,
) -> dict[str, Any]:
    """Read-only report. Does not insert samples or audit rows."""

    settings = settings or get_settings()
    wall_now = datetime.now(timezone.utc)
    checks: list[dict[str, Any]] = []
    db_checks, db_error = _db_checks(session, settings)
    checks.extend(db_checks)
    checks.append(_api_pool_check(settings))
    if db_error is None:
        queue = _section(session, lambda: _queue_checks(session, game_now, wall_now, settings))
        if isinstance(queue, Exception):
            checks.append(
                _db_unknown(
                    "event_queue.pending",
                    "events",
                    f"Queue counts failed: {queue.__class__.__name__}. This is not a pass.",
                    history_metric="queue_pending",
                )
            )
        else:
            checks.extend(queue)
        rate = _section(session, lambda: [_rate_check(session, wall_now)])
        if isinstance(rate, Exception):
            checks.append(
                _check(
                    "event_queue.processing_rate",
                    UNKNOWN,
                    value=None,
                    unit="events_per_minute",
                    reason=f"Process marks could not be counted: {rate.__class__.__name__}. This is not a pass.",
                    affects_overall=False,
                    history_metric="processing_rate_per_min",
                )
            )
        else:
            checks.extend(rate)
        hearts = _section(session, lambda: _heartbeat_checks(session, wall_now, settings))
        if isinstance(hearts, Exception):
            checks.append(
                _db_unknown(
                    "worker.heartbeat",
                    "seconds",
                    f"Heartbeat query failed: {hearts.__class__.__name__}. This is not a pass.",
                    history_metric="worker_heartbeat_age_seconds",
                )
            )
        else:
            checks.extend(hearts)
        game = _section(session, lambda: _game_checks(session, game_now))
        if isinstance(game, Exception):
            checks.append(
                _check(
                    "game.clock",
                    UNKNOWN,
                    value=None,
                    unit=None,
                    reason=f"Game clock query failed: {game.__class__.__name__}. This is not a pass.",
                    affects_overall=True,
                )
            )
        else:
            checks.extend(game)
    else:
        reason = f"Skipped because the database probe failed: {db_error}. This is not a pass."
        checks.append(_db_unknown("event_queue.pending", "events", reason, history_metric="queue_pending"))
        checks.append(_db_unknown("event_queue.lag", "seconds", reason, history_metric="event_lag_seconds"))
        checks.append(
            _db_unknown(
                "worker.heartbeat",
                "seconds",
                reason,
                history_metric="worker_heartbeat_age_seconds",
            )
        )
        checks.append(
            _check(
                "game.clock",
                UNKNOWN,
                value=None,
                unit=None,
                reason=reason,
                affects_overall=True,
            )
        )
    if include_api:
        checks.extend(_api_checks(settings))
    checks.append(_disk_check(settings))
    checks.extend(_host_gaps())
    checks.append(_commit_check())
    commit = git_commit()
    api_snap = api_metrics.snapshot() if include_api else None
    return {
        "generated_at": wall_now,
        "overall": _overall(checks),
        "checks": checks,
        "api_process": None
        if api_snap is None
        else {
            "version": __version__,
            "commit": commit,
            "started_at": api_snap["started_at"],
            "uptime_seconds": api_snap["uptime_seconds"],
            "total_requests": api_snap["total_requests"],
            "total_5xx": api_snap["total_5xx"],
            "resets_on_restart": True,
            "note": (
                "Request counts, 5xx counts, and latency percentiles are measured in this API process "
                "and reset when the API process restarts."
            ),
        },
        "sampling": {
            "interval_seconds": settings.monitor_sample_seconds,
            "retention_days": settings.monitor_retention_days,
        },
    }


def _overall(checks: list[dict[str, Any]]) -> dict[str, Any]:
    affecting = [item for item in checks if item["affects_overall"]]
    unknown = [item["name"] for item in checks if item["status"] == UNKNOWN]
    not_instrumented = [item["name"] for item in checks if item["status"] == NOT_INSTRUMENTED]
    if not affecting:
        return {
            "status": UNKNOWN,
            "reason": "No check affects overall status. This is not a pass.",
            "unknown_checks": unknown,
            "not_instrumented": not_instrumented,
        }
    def rank(item: dict[str, Any]) -> int:
        return _SEVERITY.get(str(item["status"]), _SEVERITY[UNKNOWN])

    worst = max(affecting, key=rank)
    status = str(worst["status"])
    if status not in _SEVERITY:
        status = UNKNOWN
    if status == OK:
        reason = "All checks that affect overall status are OK."
    else:
        names = [item["name"] for item in affecting if item["status"] == status]
        reason = status + ": " + ", ".join(names)
    return {
        "status": status,
        "reason": reason,
        "unknown_checks": unknown,
        "not_instrumented": not_instrumented,
    }


def observe_tick(
    *,
    worker_id: str,
    started_at: datetime,
    tick_status: str,
    tick_duration_ms: float,
    events_processed: int,
    event_id: int | None,
) -> None:
    """Upsert the heartbeat and, when an event was handled, append a process mark.

    Called after the event transaction has finished. A failure here is logged
    and does not change the tick result.
    """

    session = get_sessionmaker()()
    try:
        now = utcnow()
        pool = pool_snapshot()
        row = session.get(WorkerHeartbeat, worker_id)
        if row is None:
            row = WorkerHeartbeat(
                worker_id=worker_id[:80],
                pid=os.getpid(),
                hostname=socket.gethostname()[:80],
                version=__version__[:40],
                commit_sha=git_commit(),
                started_at=started_at,
                last_tick_at=now,
                tick_duration_ms=tick_duration_ms,
                events_processed=events_processed,
                tick_status=tick_status[:20],
                updated_at=now,
            )
            session.add(row)
        else:
            row.pid = os.getpid()
            row.hostname = socket.gethostname()[:80]
            row.version = __version__[:40]
            row.commit_sha = git_commit()
            row.last_tick_at = now
            row.tick_duration_ms = tick_duration_ms
            row.events_processed = events_processed
            row.tick_status = tick_status[:20]
            row.updated_at = now
        if pool is None:
            row.pool_checked_out = None
            row.pool_size = None
            row.pool_overflow = None
            row.pool_capacity = None
        else:
            row.pool_checked_out = int(pool["checked_out"])
            row.pool_size = int(pool["pool_size"])
            row.pool_overflow = int(pool["overflow"])
            row.pool_capacity = int(pool["capacity"])
        if event_id is not None and tick_status in ("processed", "failed"):
            session.add(
                WorkerProcessMark(
                    worker_id=worker_id[:80],
                    event_id=event_id,
                    outcome=tick_status,
                    wall_at=now,
                )
            )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("monitoring observation failed")
    finally:
        session.close()


def _history_value(check: dict[str, Any]) -> float | None:
    metric = check.get("history_metric")
    if not isinstance(metric, str) or metric not in HISTORY_METRICS:
        return None
    value = check.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _liveness(report: dict[str, Any]) -> tuple[str, str]:
    for check in report["checks"]:
        if check["name"] == "worker.heartbeat":
            detail = check.get("detail") or {}
            liveness = detail.get("liveness")
            if liveness not in (UP, STALE, DOWN, UNKNOWN):
                if check["status"] == UNKNOWN:
                    liveness = UNKNOWN
                elif check["status"] == OK:
                    liveness = UP
                elif check["status"] == WARN:
                    liveness = STALE
                elif check["status"] == CRITICAL:
                    liveness = DOWN
                else:
                    liveness = UNKNOWN
            return str(liveness), str(check.get("reason") or "")
    return UNKNOWN, "worker heartbeat check missing"


def _note_transition(session: Session, report: dict[str, Any], wall_now: datetime) -> None:
    liveness, reason = _liveness(report)
    row = session.get(MonitoringCheckState, "worker.liveness")
    previous = None if row is None else row.status
    if previous == liveness:
        return
    action: str | None = None
    result = "success"
    if previous is None and liveness == UNKNOWN:
        action = None
    elif previous is None and liveness == UP:
        action = "monitor.worker.up"
        result = "success"
    elif liveness == UP:
        action = "monitor.worker.recovered"
        result = "success"
    elif liveness == STALE:
        action = "monitor.worker.stale"
        result = "warning"
    elif liveness == DOWN:
        action = "monitor.worker.down"
        result = "failure"
    elif liveness == UNKNOWN:
        action = "monitor.worker.unknown"
        result = "failure"
    if action is not None:
        append_audit(
            session,
            actor="system",
            action=action,
            target="worker",
            source_ip="local",
            result=result,
            reason=reason[:500],
            occurred_at=wall_now,
        )
    if row is None:
        session.add(
            MonitoringCheckState(
                check_name="worker.liveness",
                status=liveness,
                detail=reason[:500],
                updated_at=wall_now,
            )
        )
    else:
        row.status = liveness
        row.detail = reason[:500]
        row.updated_at = wall_now


def sample_once(
    *,
    base_clock: Clock | None = None,
    include_api: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Write due samples, prune old rows, and audit worker liveness changes."""

    settings = settings or get_settings()
    session = get_sessionmaker()()
    try:
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SAMPLE_LOCK})
        state = session.get(WorldState, 1)
        if state is None:
            game_now = None
        else:
            game_now = (base_clock or SystemClock()).now() + timedelta(seconds=int(state.offset_seconds))
        report = collect_report(session, game_now=game_now, settings=settings, include_api=include_api)
        wall_now = report["generated_at"]
        if not isinstance(wall_now, datetime):
            wall_now = utcnow()
        gap = timedelta(seconds=settings.monitor_sample_seconds * 0.9) if settings.monitor_sample_seconds else None
        for check in report["checks"]:
            metric = check.get("history_metric")
            value = _history_value(check)
            if metric is None or value is None or gap is None:
                continue
            if not include_api and str(metric).startswith("api_"):
                continue
            latest = session.scalar(
                select(func.max(MonitoringSample.sampled_at)).where(MonitoringSample.metric == metric)
            )
            if latest is not None and _aware(latest) >= wall_now - gap:
                continue
            session.add(MonitoringSample(sampled_at=wall_now, metric=str(metric), value=value))
        _prune(session, settings, wall_now)
        _note_transition(session, report, wall_now)
        session.commit()
        return report
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _prune(session: Session, settings: Settings, wall_now: datetime) -> None:
    cutoff = wall_now - timedelta(days=settings.monitor_retention_days)
    session.execute(delete(MonitoringSample).where(MonitoringSample.sampled_at < cutoff))
    session.execute(delete(WorkerProcessMark).where(WorkerProcessMark.wall_at < cutoff))
    newest_id = session.scalar(
        select(WorkerHeartbeat.worker_id).order_by(WorkerHeartbeat.last_tick_at.desc()).limit(1)
    )
    if newest_id is not None:
        session.execute(
            delete(WorkerHeartbeat).where(
                WorkerHeartbeat.last_tick_at < cutoff,
                WorkerHeartbeat.worker_id != newest_id,
            )
        )


def history(
    session: Session,
    *,
    metric: str,
    window: str,
) -> dict[str, Any]:
    seconds = HISTORY_WINDOWS[window]
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    rows = session.scalars(
        select(MonitoringSample)
        .where(MonitoringSample.metric == metric, MonitoringSample.sampled_at >= cutoff)
        .order_by(MonitoringSample.sampled_at, MonitoringSample.id)
        .limit(_HISTORY_LIMIT)
    ).all()
    return {
        "metric": metric,
        "window": window,
        "window_seconds": seconds,
        "points": [{"sampled_at": row.sampled_at, "value": row.value} for row in rows],
        "metrics": list(HISTORY_METRICS),
    }


def run_sampler_loop(stop: threading.Event, settings: Settings, base_clock: Clock | None) -> None:
    """API-side sampler. The first sample waits one interval so startup is quiet."""

    if settings.monitor_sample_seconds <= 0:
        return
    if stop.wait(settings.monitor_sample_seconds):
        return
    while not stop.is_set():
        try:
            sample_once(base_clock=base_clock, include_api=True, settings=settings)
        except Exception:
            logger.exception("API monitoring sample failed")
        if stop.wait(settings.monitor_sample_seconds):
            return
