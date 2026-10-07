"""Client-side timings. Empty input stays unknown. Nothing here is estimated."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from simcore.monitoring import percentile


class Recorder:
    """One row per HTTP call this process made."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []

    def record(self, phase: str, route: str, status: int, code: str, latency_ms: float) -> None:
        with self._lock:
            self.rows.append(
                {
                    "phase": phase,
                    "route": route,
                    "status": int(status),
                    "code": code,
                    "latency_ms": float(latency_ms),
                }
            )

    def rows_for(self, phase: str | None) -> list[dict[str, Any]]:
        with self._lock:
            if phase is None:
                return list(self.rows)
            return [row for row in self.rows if row["phase"] == phase]


def error_code(status: int, body: object) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("code"):
            return str(err["code"])
    if status == 0:
        return "connection_error"
    return f"http_{status}"


def _latency_block(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "status": "UNKNOWN",
            "n": 0,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
            "reason": "no samples were recorded",
        }
    return {
        "status": "MEASURED",
        "n": len(values),
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
        "max_ms": max(values),
    }


def summarize(rows: list[dict[str, Any]], *, elapsed_seconds: float | None, target_rps: float | None) -> dict[str, Any]:
    by_route: dict[str, list[float]] = defaultdict(list)
    codes: dict[str, int] = defaultdict(int)
    statuses: dict[str, int] = defaultdict(int)
    for row in rows:
        by_route[str(row["route"])].append(float(row["latency_ms"]))
        codes[str(row["code"])] += 1
        statuses[str(row["status"])] += 1
    latency = {route: _latency_block(values) for route, values in sorted(by_route.items())}
    all_latency = _latency_block([float(row["latency_ms"]) for row in rows])
    count = len(rows)
    if elapsed_seconds is None or elapsed_seconds <= 0 or count == 0:
        throughput: dict[str, Any] = {
            "status": "UNKNOWN",
            "requests": count,
            "elapsed_seconds": elapsed_seconds,
            "achieved_rps": None,
            "target_rps": target_rps,
            "reason": "elapsed time or request count was not measured",
        }
    else:
        throughput = {
            "status": "MEASURED",
            "requests": count,
            "elapsed_seconds": elapsed_seconds,
            "achieved_rps": count / elapsed_seconds,
            "target_rps": target_rps,
        }
    http_5xx = sum(1 for row in rows if int(row["status"]) >= 500)
    connection_errors = sum(1 for row in rows if int(row["status"]) == 0)
    return {
        "requests": count,
        "latency_all": all_latency,
        "latency_by_route": latency,
        "errors_by_code": dict(sorted(codes.items())),
        "http_status": dict(sorted(statuses.items())),
        "http_5xx": http_5xx,
        "connection_errors": connection_errors,
        "throughput": throughput,
    }


def number_summary(values: list[float], *, unit: str) -> dict[str, Any]:
    """Percentiles of a measured series. An empty series is UNKNOWN, not zero."""

    block = _latency_block(values)
    if block["status"] != "MEASURED":
        return {"status": "UNKNOWN", "unit": unit, "n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "status": "MEASURED",
        "unit": unit,
        "n": block["n"],
        "p50": block["p50_ms"],
        "p95": block["p95_ms"],
        "p99": block["p99_ms"],
        "max": block["max_ms"],
    }
