"""Two read-only health GETs. There is no knob that sends more."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from simcore.load.report import overall, write_report
from simcore.load.safety import PROBE_REQUESTS, ensure_probe_target


def run_probe(*, base_url: str, allow_production: bool, report_dir: Path) -> dict[str, Any]:
    ensure_probe_target(base_url, allow_production=allow_production)
    origin = base_url.rstrip("/")
    paths = ("/health", "/health/ready")
    if len(paths) != PROBE_REQUESTS:
        raise RuntimeError("the probe budget is two requests")
    checks: list[dict[str, Any]] = []
    client = httpx.Client(timeout=10.0, follow_redirects=False)
    try:
        for path in paths:
            started = time.perf_counter()
            try:
                response = client.get(origin + path)
            except httpx.HTTPError as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                checks.append(
                    {
                        "name": f"probe{path}",
                        "status": "FAIL",
                        "detail": f"{exc.__class__.__name__} after {elapsed_ms:.1f} ms",
                        "required": True,
                    }
                )
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if response.status_code == 200:
                checks.append(
                    {
                        "name": f"probe{path}",
                        "status": "PASS",
                        "detail": f"HTTP 200 in {elapsed_ms:.1f} ms",
                        "required": True,
                        "data": {"latency_ms": elapsed_ms, "status": 200},
                    }
                )
            else:
                checks.append(
                    {
                        "name": f"probe{path}",
                        "status": "FAIL",
                        "detail": f"HTTP {response.status_code} in {elapsed_ms:.1f} ms",
                        "required": True,
                        "data": {"latency_ms": elapsed_ms, "status": response.status_code},
                    }
                )
    finally:
        client.close()
    payload = {
        "result": overall(checks),
        "kind": "probe",
        "base_url_host": httpx.URL(origin).host,
        "requests": PROBE_REQUESTS,
        "checks": checks,
        "note": "This probe sends GET /health and GET /health/ready and nothing else.",
    }
    write_report(report_dir, payload)
    return payload
