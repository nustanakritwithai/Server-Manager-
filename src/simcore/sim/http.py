"""HTTP calls the bots and the checker make. Latency is measured here."""

from __future__ import annotations

import math
import time
from typing import Any

import httpx


class ApiClient:
    """One httpx client plus the latencies of every call it made."""

    def __init__(self, base_url: str, *, admin_token: str) -> None:
        self.admin_token = admin_token
        self.latencies_ms: list[float] = []
        self._client = httpx.Client(base_url=base_url, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    @property
    def admin_headers(self) -> dict[str, str]:
        return {"X-Admin-Token": self.admin_token}

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        started = time.perf_counter()
        try:
            response = self._client.request(method, path, **kwargs)
        finally:
            self.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        return response

    def json(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        response = self.request(method, path, **kwargs)
        body: Any
        try:
            body = response.json()
        except Exception:
            body = {"error": {"code": "bad_response", "message": response.text[:500]}}
        return response.status_code, body


def latency_summary(samples_ms: list[float]) -> dict[str, float | int | None]:
    if not samples_ms:
        return {"avg": None, "p95": None, "samples": 0}
    ordered = sorted(samples_ms)
    average = sum(ordered) / len(ordered)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {"avg": average, "p95": ordered[index], "samples": len(ordered)}
