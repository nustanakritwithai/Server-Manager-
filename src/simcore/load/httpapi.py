"""Timed HTTP calls. Connection failures are status 0, not a made-up latency."""

from __future__ import annotations

import time
from typing import Any

import httpx

from simcore.load.metrics import Recorder, error_code


class HttpApi:
    def __init__(self, base_url: str, admin_token: str, recorder: Recorder) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_token = admin_token
        self.recorder = recorder
        self._client = httpx.Client(base_url=self.base_url, timeout=20.0)

    def close(self) -> None:
        self._client.close()

    @property
    def admin_headers(self) -> dict[str, str]:
        return {"X-Admin-Token": self.admin_token}

    def call(
        self,
        method: str,
        path: str,
        *,
        phase: str,
        headers: dict[str, str] | None = None,
        json: object | None = None,
        timeout: float | None = None,
        record: bool = True,
    ) -> tuple[int, Any]:
        started = time.perf_counter()
        try:
            response = self._client.request(method, path, headers=headers, json=json, timeout=timeout)
        except httpx.TimeoutException:
            elapsed = (time.perf_counter() - started) * 1000.0
            body: Any = {"error": {"code": "timeout", "message": "timed out"}}
            if record:
                self.recorder.record(phase, f"{method} {path}", 0, "timeout", elapsed)
            return 0, body
        except httpx.HTTPError as exc:
            elapsed = (time.perf_counter() - started) * 1000.0
            body = {"error": {"code": "connection_error", "message": exc.__class__.__name__}}
            if record:
                self.recorder.record(phase, f"{method} {path}", 0, "connection_error", elapsed)
            return 0, body
        elapsed = (time.perf_counter() - started) * 1000.0
        try:
            payload = response.json()
        except Exception:
            payload = {"error": {"code": "bad_response", "message": response.text[:300]}}
        if record:
            self.recorder.record(
                phase,
                f"{method} {path}",
                response.status_code,
                error_code(response.status_code, payload),
                elapsed,
            )
        return response.status_code, payload
