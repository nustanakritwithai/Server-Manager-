"""Pass/fail bars for a simulator run.

These numbers are fixed before a run is scored. A report that exceeds one
of them is FAIL. UNKNOWN and NOT INSTRUMENTED monitoring checks are not
given these bars and are never treated as PASS.
"""

from __future__ import annotations

from typing import Any

# CI advances the clock by this many game seconds when --duration is omitted.
CI_DEFAULT_TICKS = 4
CI_DEFAULT_STEP_SECONDS = 3600

# Staging sleeps this many wall seconds per tick when --duration is omitted.
STAGING_DEFAULT_TICKS = 4
STAGING_DEFAULT_PAUSE_SECONDS = 15

# Player commands issued by each bot on each tick.
DEFAULT_COMMAND_RATE = 1
DEFAULT_PLAYERS = 4
DEFAULT_SEED = 8741

# Peak game-time lag is compared with the clock step. One missed drain
# pushes the oldest due event about two steps behind, which fails.
LAG_STEP_SLACK_SECONDS = 1

# Client-observed latency. Generous so a slow CI runner still passes, tight
# enough that a hung request fails.
API_P95_MS_MAX = 5000
API_AVG_MS_MAX = 2000

# Staging does not fast-forward. The worker's own critical lag is the bar.
STAGING_END_LAG_SECONDS_MAX = 120


def threshold_document(*, mode: str, step_seconds: int | None) -> dict[str, Any]:
    """The bars this run will be scored against."""

    if mode == "ci":
        step = CI_DEFAULT_STEP_SECONDS if step_seconds is None else step_seconds
        end_lag = 0
        max_lag = step + LAG_STEP_SLACK_SECONDS
    else:
        end_lag = STAGING_END_LAG_SECONDS_MAX
        max_lag = STAGING_END_LAG_SECONDS_MAX
    return {
        "trace_fail_max": 0,
        "incomplete_without_pending_max": 0,
        "legacy_command_rows_max": 0,
        "negative_resources_max": 0,
        "duplicate_processing_max": 0,
        "failed_events_max": 0,
        "processing_events_max": 0,
        "army_loss_or_duplicate_max": 0,
        "ledger_imbalance_max": 0,
        "audit_chain_status": "PASS",
        "monitoring_critical_max": 0,
        "end_event_lag_seconds_max": end_lag,
        "max_event_lag_seconds_max": max_lag,
        "api_p95_ms_max": API_P95_MS_MAX,
        "api_avg_ms_max": API_AVG_MS_MAX,
        "snapshot_checksum_required": mode == "ci",
    }
