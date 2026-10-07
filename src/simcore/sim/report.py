"""JSON and Markdown reports. Scoring stays in the verifier; this only writes it down."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_report(directory: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    md_path = directory / "report.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def render_markdown(payload: dict[str, Any]) -> str:
    counts = payload.get("counts") or {}
    latency = payload.get("latency_ms") or {}
    verdicts = payload.get("trace_verdicts") or {}
    monitoring = payload.get("monitoring") or {}
    lines = [
        "# Simulator report",
        "",
        f"**Result: {payload.get('result', 'FAIL')}**",
        "",
        "| | |",
        "| --- | --- |",
        f"| Mode | {payload.get('mode')} |",
        f"| Seed | {payload.get('seed')} |",
        f"| Players | {payload.get('players')} |",
        f"| Ticks | {payload.get('ticks')} |",
        f"| Command rate | {payload.get('command_rate')} |",
        f"| Duration (seconds) | {payload.get('duration_seconds')} |",
        f"| Commands accepted | {counts.get('commands_accepted')} |",
        f"| Commands rejected | {counts.get('commands_rejected')} |",
        f"| Commands skipped | {counts.get('commands_skipped')} |",
        f"| Events | {counts.get('events')} |",
        f"| Battles | {counts.get('battles')} |",
        f"| API latency avg | {_ms(latency.get('avg'))} |",
        f"| API latency p95 | {_ms(latency.get('p95'))} |",
        f"| Max event lag | {_seconds(payload.get('max_event_lag_seconds'))} |",
        f"| End event lag | {_seconds(payload.get('end_event_lag_seconds'))} |",
        f"| World checksum | {payload.get('world_checksum') or '—'} |",
        "",
        "## Trace verdicts",
        "",
        "| Verdict | Count |",
        "| --- | --- |",
        f"| PASS | {verdicts.get('PASS', 0)} |",
        f"| FAIL | {verdicts.get('FAIL', 0)} |",
        f"| INCOMPLETE | {verdicts.get('INCOMPLETE', 0)} |",
        f"| LEGACY | {verdicts.get('LEGACY', 0)} |",
        "",
        "INCOMPLETE is allowed only while a movement or event on that trace is still open. "
        "LEGACY / NOT TRACED and NOT CHECKED are not counted as PASS.",
        "",
        "## Not checked",
        "",
    ]
    not_checked = payload.get("not_checked_summary") or "None reported."
    lines.append(str(not_checked))
    lines.extend(["", "## Monitoring", ""])
    lines.append(f"Overall: {monitoring.get('overall')}.")
    lines.append("")
    lines.append(_bullet("CRITICAL", monitoring.get("critical")))
    lines.append(_bullet("UNKNOWN", monitoring.get("unknown")))
    lines.append(_bullet("NOT INSTRUMENTED", monitoring.get("not_instrumented")))
    lines.append("")
    lines.append("UNKNOWN and NOT INSTRUMENTED are listed as those states. They are not PASS.")
    lines.extend(["", "## Invariants", ""])
    lines.append("| Invariant | Status | Detail |")
    lines.append("| --- | --- | --- |")
    for item in payload.get("invariants") or []:
        detail = str(item.get("detail") or "").replace("|", "/")
        lines.append(f"| {item.get('invariant')} | {item.get('status')} | {detail} |")
    lines.extend(["", "## Failed invariants", ""])
    failed = payload.get("failed_invariants") or []
    if not failed:
        lines.append("None.")
    else:
        for item in failed:
            trace = item.get("trace_id") or "—"
            lines.append(f"- `{item.get('invariant')}` trace_id `{trace}`: {item.get('detail')}")
    lines.extend(["", "## Actions with no endpoint", ""])
    for item in payload.get("skipped_actions") or []:
        lines.append(f"- `{item.get('action')}`: {item.get('reason')}")
    lines.extend(["", "## Audit chain", ""])
    chain = payload.get("audit_chain") or {}
    lines.append(
        f"Status {chain.get('status')}, rows checked {chain.get('checked_rows')}."
    )
    if chain.get("reasons"):
        for reason in chain["reasons"]:
            lines.append(f"- {reason}")
    lines.append("")
    return "\n".join(lines)


def _ms(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:.1f} ms"


def _seconds(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:.3f} s"


def _bullet(label: str, rows: object) -> str:
    if not isinstance(rows, list) or not rows:
        return f"- {label}: none"
    names = ", ".join(str(row.get("name") or row) for row in rows if isinstance(row, dict)) or "none"
    return f"- {label}: {names}"
