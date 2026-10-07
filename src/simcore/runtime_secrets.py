"""Fill SIMCORE_PLAYER_TOKEN_SECRET in .env.prod when a production boot would refuse it.

deploy/windows/bootstrap.ps1 and update.ps1 write the same value. Alembic also
calls this before it loads settings, because the copy of update.ps1 already on
the VPS pulls new code and then runs ``alembic upgrade`` before it restarts the
API. The secret is random and is never logged.

The API process does not call this. If the value is still missing or weak when
the server starts, settings validation refuses to start.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

DEV_PLAYER_TOKEN_SECRET = "dev-player-token-secret-not-for-production"
_KEY = "SIMCORE_PLAYER_TOKEN_SECRET"
_MIN_LENGTH = 32
_FORBIDDEN = frozenset(
    {
        "",
        "change_me",
        "changeme",
        "dev",
        "dev-player",
        DEV_PLAYER_TOKEN_SECRET.casefold(),
    }
)


def player_token_secret_is_usable(secret: str, *, production: bool) -> bool:
    text = (secret or "").strip()
    if len(text) < _MIN_LENGTH:
        return False
    if production and text.casefold() in _FORBIDDEN:
        return False
    return True


def ensure_player_token_secret(env_path: Path | None = None) -> None:
    """Write a strong secret into .env.prod when production would reject the current one.

    Development and test processes are left alone, including the checked-in dev
    default. A strong value already in the environment or the file is kept.
    """

    path = env_path or Path.cwd() / ".env.prod"
    env_name = _environment_name(path)
    if env_name != "production":
        return
    current = os.environ.get(_KEY, "")
    if not current and path.is_file():
        current = _read_values(path).get(_KEY, "")
    if player_token_secret_is_usable(current, production=True):
        os.environ[_KEY] = current.strip()
        return
    if not path.is_file():
        return
    generated = secrets.token_urlsafe(48)
    _write_secret(path, generated)
    os.environ[_KEY] = generated


def _environment_name(env_path: Path) -> str:
    current = os.environ.get("SIMCORE_ENV", "").strip().lower()
    if current:
        return current
    if env_path.is_file():
        return _read_values(env_path).get("SIMCORE_ENV", "").strip().lower()
    return ""


def _read_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value
    return values


def _write_secret(path: Path, secret: str) -> None:
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    replaced = False
    written: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{_KEY}=") or stripped.startswith(f"{_KEY} ="):
            written.append(f"{_KEY}={secret}")
            replaced = True
        else:
            written.append(line)
    if not replaced:
        if written and written[-1] != "":
            written.append("")
        written.append(f"{_KEY}={secret}")
    payload = "\n".join(written)
    if original.endswith("\n") or not original:
        payload += "\n"
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
