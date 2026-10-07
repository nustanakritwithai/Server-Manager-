"""Refuse to drive a database or URL that looks like the live world.

The escape hatch is ``--i-understand-this-is-production``. The docs tell the
operator to take a snapshot before using it. This module does not open a
connection and does not read a secret.
"""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy.engine.url import make_url

# The public game API from the deploy docs. Loopback docker-compose is not
# in this set; its database name is what marks that world as live.
KNOWN_PRODUCTION_HOSTS = frozenset(
    {
        "157-85-96-139.sslip.io",
        "157.85.96.139",
    }
)

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})

# Database names that hold a real or local play world, not a simulator run.
_LIVE_DATABASES = frozenset({"simcore", "postgres", "production", "prod"})


class SafetyError(RuntimeError):
    """The target looks like production, or CI is not pointed at a test database."""


def database_parts(database_url: str) -> tuple[str, str]:
    """Return ``(host, database name)``. Raises SafetyError on a bad URL."""

    try:
        parsed = make_url(database_url)
    except Exception as exc:
        raise SafetyError(f"database URL could not be parsed: {exc.__class__.__name__}") from exc
    host = (parsed.host or "").strip().lower()
    name = (parsed.database or "").strip()
    return host, name


def base_host(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SafetyError("base URL must be an http or https URL with a host")
    return parsed.hostname.strip().lower()


def production_reasons(*, base_url: str | None, database_url: str | None, env_name: str | None) -> list[str]:
    """Why this target looks like a live world. Empty means it does not."""

    reasons: list[str] = []
    if (env_name or "").strip().lower() == "production":
        reasons.append("SIMCORE_ENV is production")
    if database_url:
        host, name = database_parts(database_url)
        lowered = name.lower()
        if lowered in _LIVE_DATABASES or "prod" in lowered:
            reasons.append(f"database name {name!r} looks like a live database")
        if host and host not in _LOOPBACK and not _is_simulator_database(lowered):
            reasons.append(f"database host {host!r} is not loopback and the database name is not a test/sim name")
    if base_url:
        host = base_host(base_url)
        if host in KNOWN_PRODUCTION_HOSTS or host.endswith(".sslip.io"):
            reasons.append(f"base URL host {host!r} is a known public game host")
        elif "prod" in host or "production" in host:
            reasons.append(f"base URL host {host!r} looks like production")
    return reasons


def _is_simulator_database(name: str) -> bool:
    lowered = name.lower()
    return "test" in lowered or "_sim" in lowered


def is_live_database(database_url: str) -> bool:
    _host, name = database_parts(database_url)
    return name.lower() in _LIVE_DATABASES or "prod" in name.lower()


def ensure_safe(
    *,
    mode: str,
    base_url: str | None,
    database_url: str | None,
    env_name: str | None,
    allow_production: bool,
) -> None:
    """Raise SafetyError unless this target is a local test or an acknowledged live world."""

    reasons = production_reasons(base_url=base_url, database_url=database_url, env_name=env_name)
    if mode in {"ci", "full"}:
        if not database_url:
            raise SafetyError("CI mode needs SIMCORE_DATABASE_URL or --database-url")
        host, name = database_parts(database_url)
        lowered = name.lower()
        if host not in _LOOPBACK:
            reasons.append(f"{mode} mode only runs against a loopback database, not {host!r}")
        if not _is_simulator_database(lowered):
            reasons.append(
                f"{mode} mode only seeds a database whose name contains 'test' or '_sim' (got {name!r})"
            )
        if base_url is not None:
            api_host = base_host(base_url)
            if api_host not in _LOOPBACK:
                reasons.append(f"{mode} mode serves the API on loopback, not {api_host!r}")
    else:
        if not base_url:
            raise SafetyError("staging mode needs --base-url")
    if reasons and not allow_production:
        joined = "; ".join(reasons)
        raise SafetyError(
            f"refusing to run: {joined}. Take a snapshot of that world first "
            "(POST /v1/admin/snapshots), then re-run with --i-understand-this-is-production "
            "only if you mean to send bot commands there."
        )
