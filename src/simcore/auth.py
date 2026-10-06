"""Placeholder login for local development.

DEV ONLY. The token is the player id with a prefix. Anyone who can reach the
port can act as any seeded player. Replace this before the game is exposed.
"""

from __future__ import annotations

from simcore.errors import GameError

DEV_TOKEN_PREFIX = "dev:"
DEV_AUTH_WARNING = (
    "DEV ONLY placeholder auth. The token is not signed and must not be used in production."
)


def issue_dev_token(player_id: int) -> str:
    return f"{DEV_TOKEN_PREFIX}{player_id}"


def parse_dev_token(token: str) -> int:
    if not token.startswith(DEV_TOKEN_PREFIX):
        raise GameError("invalid dev token", status_code=401, code="unauthorized")
    raw = token[len(DEV_TOKEN_PREFIX) :]
    if not raw.isdigit():
        raise GameError("invalid dev token", status_code=401, code="unauthorized")
    return int(raw)
