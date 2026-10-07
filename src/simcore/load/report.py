"""JSON and Markdown reports. Statuses are PASS, FAIL, or INCOMPLETE.

A missing measurement stays UNKNOWN or NOT INSTRUMENTED. This module does not
fill those in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def overall(checks: list[dict[str, Any]]) -> str:
    """FAIL if any required check failed. INCOMPLETE if any required check is not PASS."""

    required = [item for item in checks if item.get("required", True)]
    if any(item.get("status") == "FAIL" for item in required):
        return "FAIL"
    if any(item.get("status") != "PASS" for item in required):
        return "INCOMPLETE"
    return "PASS"


def write_report(directory: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    md_path = directory / "report.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Load and failure report",
        "",
        f"Result: **{payload.get('result', 'INCOMPLETE')}**",
        "",
        f"- Profile: {payload.get('profile')}",
        f"- Mode: {payload.get('mode')}",
        f"- Seed: {payload.get('seed')}",
        f"- Git commit: {payload.get('git_commit') or 'UNKNOWN'}",
        f"- Started: {payload.get('started_at')}",
        f"- Finished: {payload.get('finished_at')}",
        f"- Elapsed seconds: {_num(payload.get('elapsed_seconds'))}",
        "",
        "## Host",
        "",
        _host_block(payload.get("host") or {}),
        "",
        "## Load",
        "",
        _load_block(payload.get("load") or {}),
        "",
        "## Worker lag",
        "",
        _lag_block(payload.get("worker_lag") or {}),
        "",
        "## Database pool and backends",
        "",
        _pool_block(payload.get("database") or {}),
        "",
        "## CPU and memory",
        "",
        _resource_block(payload.get("resources") or {}),
        "",
        "## Checks",
        "",
        "| Status | Check | Detail |",
        "| --- | --- | --- |",
    ]
    for item in payload.get("checks") or []:
        detail = str(item.get("detail") or "").replace("|", "/")
        lines.append(f"| {item.get('status')} | {item.get('name')} | {detail} |")
    lines.append("")
    failed = [item for item in payload.get("checks") or [] if item.get("status") != "PASS"]
    if failed:
        lines.append("Required checks that are not PASS make the result FAIL or INCOMPLETE.")
        lines.append("")
    return "\n".join(lines)


def _host_block(host: dict[str, Any]) -> str:
    if host.get("status") != "MEASURED":
        return f"Host specs: {host.get('status', 'UNKNOWN')}. {host.get('reason', '')}".rstrip()
    memory = host.get("memory_total_bytes")
    gib = None if memory is None else memory / (1024 ** 3)
    return "\n".join(
        [
            f"- Platform: {host.get('platform')}",
            f"- Python: {host.get('python')}",
            f"- CPU count: {host.get('cpu_count')}",
            f"- Memory total bytes: {memory} ({_num(gib)} GiB)",
            f"- Runner OS: {host.get('runner_os') or 'not a GitHub Actions runner'}",
            f"- Runner name: {host.get('runner_name') or 'not a GitHub Actions runner'}",
        ]
    )


def _load_block(load: dict[str, Any]) -> str:
    latency = load.get("latency_all") or {}
    throughput = load.get("throughput") or {}
    lines = [
        f"- Requests: {load.get('requests')}",
        f"- Latency status: {latency.get('status')}",
        f"- p50 ms: {_num(latency.get('p50_ms'))}",
        f"- p95 ms: {_num(latency.get('p95_ms'))}",
        f"- p99 ms: {_num(latency.get('p99_ms'))}",
        f"- Throughput: {throughput.get('status')} {_num(throughput.get('achieved_rps'))} requests/second "
        f"(target {_num(throughput.get('target_rps'))})",
        f"- HTTP 5xx: {load.get('http_5xx')}",
        f"- Connection errors: {load.get('connection_errors')}",
        "",
        "Latency by route (load phase only):",
        "",
        "| Route | n | p50 ms | p95 ms | p99 ms |",
        "| --- | --- | --- | --- | --- |",
    ]
    for route, block in (load.get("latency_by_route") or {}).items():
        lines.append(
            f"| {route} | {block.get('n')} | {_num(block.get('p50_ms'))} | "
            f"{_num(block.get('p95_ms'))} | {_num(block.get('p99_ms'))} |"
        )
    lines.extend(["", "Errors by code (load phase):", ""])
    codes = load.get("errors_by_code") or {}
    if not codes:
        lines.append("No load-phase responses were recorded.")
    else:
        for code, count in codes.items():
            lines.append(f"- `{code}`: {count}")
    return "\n".join(lines)


def _lag_block(lag: dict[str, Any]) -> str:
    lines = []
    for key in (
        "processed_at_minus_due_at_seconds",
        "monitoring_event_queue_lag_seconds",
        "monitoring_event_queue_lag_across_clock_advance_seconds",
        "wall_clock_completion_minus_due_seconds",
    ):
        block = lag.get(key) or {"status": "UNKNOWN"}
        lines.append(f"- `{key}`: {_series(block)}")
    note = lag.get("note")
    if note:
        lines.append("")
        lines.append(str(note))
    return "\n".join(lines)


def _pool_block(database: dict[str, Any]) -> str:
    lines = []
    for key in ("api_pool_utilization", "worker_pool_utilization", "pg_stat_activity_backends"):
        block = database.get(key) or {"status": "UNKNOWN"}
        lines.append(f"- `{key}`: {_series(block)}")
    note = database.get("note")
    if note:
        lines.append("")
        lines.append(str(note))
    return "\n".join(lines)


def _resource_block(resources: dict[str, Any]) -> str:
    lines = []
    for key in (
        "host_cpu_percent",
        "host_memory_percent",
        "child_process_cpu_percent",
        "child_process_rss_bytes",
    ):
        block = resources.get(key) or {"status": "UNKNOWN"}
        lines.append(f"- `{key}`: {_series(block)}")
    note = resources.get("note")
    if note:
        lines.append("")
        lines.append(str(note))
    return "\n".join(lines)


def _series(block: dict[str, Any]) -> str:
    status = block.get("status", "UNKNOWN")
    if status != "MEASURED":
        reason = block.get("reason") or ""
        return f"{status} {reason}".rstrip()
    return (
        f"MEASURED n={block.get('n')} p50={_num(block.get('p50'))} "
        f"p95={_num(block.get('p95'))} p99={_num(block.get('p99'))} max={_num(block.get('max'))} {block.get('unit') or ''}"
    ).rstrip()


def _num(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return "n/a" if value is None else str(value)
    if isinstance(value, (int, float)):
        return f"{value:.3f}" if isinstance(value, float) else str(value)
    return str(value)
