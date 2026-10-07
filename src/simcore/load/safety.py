"""Refuse a production or non-local target unless the operator opts in at a low rate.

Failure injection is never allowed against a target this process did not start.
The probe is a separate, fixed, read-only budget.
"""

from __future__ import annotations

from simcore.sim.safety import base_host, database_parts, is_live_database, production_reasons

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})

# External traffic against a non-local URL stays inside this budget.
# The flag does not raise it.
PRODUCTION_MAX_RATE = 2.0
PRODUCTION_MAX_CONCURRENCY = 2
PRODUCTION_MAX_DURATION_SECONDS = 30.0
PRODUCTION_MAX_PLAYERS = 2

# Local runs can be larger. These only stop a typo from starting a huge job.
LOCAL_MAX_PLAYERS = 30
LOCAL_MAX_RATE = 500.0
LOCAL_MAX_CONCURRENCY = 64
LOCAL_MAX_DURATION_SECONDS = 600.0

PROBE_REQUESTS = 2


class LoadSafetyError(RuntimeError):
    """The target is not a local test, or the production opt-in is incomplete."""


def _is_test_database(name: str) -> bool:
    lowered = name.lower()
    return "test" in lowered or "_sim" in lowered


def ensure_local_target(database_url: str | None) -> tuple[str, str]:
    """Local mode migrates and writes a loopback test database. The opt-in flag does not lift this."""

    if not database_url:
        raise LoadSafetyError("local mode needs SIMCORE_DATABASE_URL or --database-url")
    host, name = database_parts(database_url)
    if host not in _LOOPBACK:
        raise LoadSafetyError(f"local mode only uses a loopback database, not {host!r}")
    if is_live_database(database_url) or not _is_test_database(name):
        raise LoadSafetyError(
            f"local mode only migrates a database whose name contains 'test' or '_sim' (got {name!r}). "
            "It will not use a live database name, even with --i-understand-this-is-production."
        )
    return host, name


def ensure_external_target(
    *,
    base_url: str | None,
    database_url: str | None,
    env_name: str | None,
    allow_production: bool,
    rate: float,
    concurrency: int,
    duration: float,
    players: int,
    failures: bool,
) -> None:
    """External mode sends HTTP to a server that is already running. It does not kill processes."""

    if not base_url:
        raise LoadSafetyError("external mode needs --base-url")
    host = base_host(base_url)
    reasons = production_reasons(base_url=base_url, database_url=database_url, env_name=env_name)
    if host not in _LOOPBACK:
        reasons.append(f"base URL host {host!r} is not loopback")
    # production_reasons already includes known public hosts. Deduplicate.
    unique: list[str] = []
    for reason in reasons:
        if reason not in unique:
            unique.append(reason)
    if failures:
        raise LoadSafetyError(
            "failure injection only runs in local mode, against processes this tool started. "
            "External mode is load traffic only."
        )
    if not unique:
        return
    if not allow_production:
        raise LoadSafetyError(
            "refusing to target a production or non-localhost URL: "
            + "; ".join(unique)
            + ". Re-run with --i-understand-this-is-production and a rate at or below "
            f"{PRODUCTION_MAX_RATE} requests/second, concurrency at or below {PRODUCTION_MAX_CONCURRENCY}, "
            f"duration at or below {PRODUCTION_MAX_DURATION_SECONDS:g}s, and at most {PRODUCTION_MAX_PLAYERS} players. "
            "Failure injection stays off."
        )
    over: list[str] = []
    if rate > PRODUCTION_MAX_RATE:
        over.append(f"rate {rate} > {PRODUCTION_MAX_RATE}")
    if concurrency > PRODUCTION_MAX_CONCURRENCY:
        over.append(f"concurrency {concurrency} > {PRODUCTION_MAX_CONCURRENCY}")
    if duration > PRODUCTION_MAX_DURATION_SECONDS:
        over.append(f"duration {duration} > {PRODUCTION_MAX_DURATION_SECONDS:g}")
    if players > PRODUCTION_MAX_PLAYERS:
        over.append(f"players {players} > {PRODUCTION_MAX_PLAYERS}")
    if over:
        raise LoadSafetyError(
            "the production opt-in does not raise the rate cap: " + "; ".join(over)
        )


def ensure_probe_target(base_url: str, *, allow_production: bool) -> None:
    """The probe sends two GETs and nothing else. A non-local URL still needs the flag."""

    host = base_host(base_url)
    if host in _LOOPBACK:
        return
    if allow_production:
        return
    raise LoadSafetyError(
        f"refusing to probe non-localhost host {host!r}. "
        "The probe is two read-only GETs (/health and /health/ready) and cannot be turned up. "
        "Re-run with --i-understand-this-is-production if you mean to send those two requests."
    )


def ensure_bounds(
    *,
    mode: str,
    players: int,
    rate: float,
    concurrency: int,
    duration: float,
) -> None:
    if players < 1 or players > LOCAL_MAX_PLAYERS:
        raise LoadSafetyError(f"--players must be from 1 to {LOCAL_MAX_PLAYERS}")
    if rate <= 0 or rate > LOCAL_MAX_RATE:
        raise LoadSafetyError(f"--rate must be greater than 0 and at most {LOCAL_MAX_RATE:g}")
    if concurrency < 1 or concurrency > LOCAL_MAX_CONCURRENCY:
        raise LoadSafetyError(f"--concurrency must be from 1 to {LOCAL_MAX_CONCURRENCY}")
    if duration <= 0 or duration > LOCAL_MAX_DURATION_SECONDS:
        raise LoadSafetyError(
            f"--duration must be greater than 0 and at most {LOCAL_MAX_DURATION_SECONDS:g} seconds"
        )
    if mode not in {"local", "external"}:
        raise LoadSafetyError("mode must be local or external")
