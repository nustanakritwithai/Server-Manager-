"""Player passwords, access tokens, and refresh-session families.

Passwords use the same scrypt KDF as the admin password (hashlib, no native
package). The plaintext is never stored. Access tokens are HMAC-SHA256 signed
with SIMCORE_PLAYER_TOKEN_SECRET and last a few minutes. Refresh tokens are
random, stored only as a SHA-256 hash, and rotate on every refresh. Presenting
a refresh token that was already rotated revokes that session family.

Admin sessions stay on SIMCORE_ADMIN_SESSION_SECRET. This module does not
read or write them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from simcore.admin_auth import hash_admin_password, verify_admin_password
from simcore.config import Settings
from simcore.errors import GameError
from simcore.models import Player, PlayerAccount, PlayerRefreshSession, utcnow

_TOKEN_PREFIX = "simplyr1"
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
_MIN_PASSWORD = 10
_MAX_PASSWORD = 1024
_DUMMY_LOCK = threading.Lock()
_DUMMY_HASH: str | None = None

# Short, common passwords. Compared after casefold. Registration reveals a
# policy failure, not whether the account exists.
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password12",
        "password123",
        "password1234",
        "passw0rd",
        "123456789",
        "1234567890",
        "12345678901",
        "123456789012",
        "qwertyuiop",
        "qwerty123",
        "qwerty1234",
        "iloveyou",
        "iloveyou1",
        "letmein",
        "letmein1",
        "welcome",
        "welcome1",
        "admin123",
        "admin1234",
        "changeme",
        "changeme1",
        "abc123456",
        "abcdef123",
        "trustno1",
        "monkey123",
        "dragon123",
        "master123",
        "login123",
        "passw0rd1",
        "sunshine1",
        "princess1",
        "football1",
        "baseball1",
        "shadow123",
        "michael1",
        "superman1",
        "batman123",
        "whatever1",
        "starwars1",
        "hello123",
        "hello1234",
        "freedom1",
        "secret123",
        "access123",
        "maggie123",
        "jennifer1",
        "jordan23",
        "charlie1",
        "donald123",
        "password!",
        "pass1234",
        "pa55word",
        "pa55w0rd",
        "test1234",
        "guest123",
        "root1234",
        "default1",
        "simcore123",
        "simcore1",
    }
)


class SlidingWindowLimiter:
    """Count events per key inside a window. In-process, one API service."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            recent = self._prune(key, now)
            if len(recent) >= self.limit:
                return False
            recent.append(now)
            self._hits[key] = recent
            return True

    def _prune(self, key: str, now: float) -> list[float]:
        recent = [stamp for stamp in self._hits.get(key, []) if now - stamp < self.window_seconds]
        self._hits[key] = recent
        return recent


def username_key(value: str) -> str:
    return value.strip().casefold()


def validate_username(value: str) -> str:
    text = (value or "").strip()
    if not _USERNAME.fullmatch(text):
        raise GameError(
            "username must be 3 to 32 characters and use letters, digits, dot, underscore, or hyphen",
            status_code=400,
            code="invalid_username",
        )
    return text


def validate_email(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > 254 or text.count("@") != 1:
        raise GameError("email is not valid", status_code=400, code="invalid_email")
    local, domain = text.split("@", 1)
    if not local or not domain or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise GameError("email is not valid", status_code=400, code="invalid_email")
    return text


def email_key(value: str | None) -> str | None:
    if value is None:
        return None
    return value.casefold()


def validate_new_password(password: str, *, username: str, email: str | None) -> str:
    """Policy for a password the player or an admin is choosing now."""

    cleaned = (password or "").strip()
    if len(cleaned) < _MIN_PASSWORD:
        raise GameError(
            f"password must be at least {_MIN_PASSWORD} characters",
            status_code=400,
            code="weak_password",
        )
    if len(cleaned) > _MAX_PASSWORD:
        raise GameError("password is too long", status_code=400, code="weak_password")
    folded = cleaned.casefold()
    if folded in _COMMON_PASSWORDS:
        raise GameError("password is too common", status_code=400, code="weak_password")
    if folded == username.casefold() or (email and folded == email.casefold()):
        raise GameError("password must not match the username or email", status_code=400, code="weak_password")
    return cleaned


def hash_player_password(password: str) -> str:
    return hash_admin_password(password)


def verify_player_password(password: str, stored: str) -> bool:
    return verify_admin_password(password, stored)


def dummy_password_hash() -> str:
    """A real scrypt hash so an unknown username takes a similar amount of time."""

    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        with _DUMMY_LOCK:
            if _DUMMY_HASH is None:
                _DUMMY_HASH = hash_admin_password("dummy-password-not-a-login")
    return _DUMMY_HASH


def account_for_player(session: Session, player_id: int) -> PlayerAccount | None:
    return session.scalar(select(PlayerAccount).where(PlayerAccount.player_id == player_id))


def account_by_login(session: Session, identifier: str) -> PlayerAccount | None:
    key = identifier.strip().casefold()
    if not key:
        return None
    return session.scalar(
        select(PlayerAccount).where((PlayerAccount.username_key == key) | (PlayerAccount.email_key == key))
    )


def player_name_taken(session: Session, name: str) -> bool:
    """Case-insensitive. Player names are a small set; this does not rewrite rows."""

    key = username_key(name)
    names = session.scalars(select(Player.name)).all()
    return any(username_key(existing) == key for existing in names)


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    access_expires_at: int
    refresh_expires_at: datetime
    session_id: int
    family_id: str


def issue_token_pair(
    session: Session,
    settings: Settings,
    account: PlayerAccount,
    *,
    family_id: str | None = None,
    created_ip: str | None = None,
    user_agent: str | None = None,
    now: int | None = None,
) -> IssuedTokens:
    issued = int(time.time()) if now is None else int(now)
    refresh_expires = datetime.fromtimestamp(issued, timezone.utc) + timedelta(
        seconds=settings.player_refresh_ttl_seconds
    )
    secret = secrets.token_urlsafe(32)
    row = PlayerRefreshSession(
        account_id=account.id,
        family_id=family_id or str(uuid.uuid4()),
        token_hash=_digest(secret),
        expires_at=refresh_expires,
        created_at=datetime.fromtimestamp(issued, timezone.utc),
        created_ip=(created_ip or "")[:64] or None,
        user_agent=(user_agent or "")[:200] or None,
    )
    session.add(row)
    session.flush()
    refresh = f"{_TOKEN_PREFIX}.{row.id}.{secret}"
    access, access_exp = issue_access_token(
        settings,
        player_id=_player_id(account),
        account_id=account.id,
        session_id=row.id,
        now=issued,
    )
    return IssuedTokens(
        access_token=access,
        refresh_token=refresh,
        access_expires_at=access_exp,
        refresh_expires_at=refresh_expires,
        session_id=row.id,
        family_id=row.family_id,
    )


def issue_access_token(
    settings: Settings,
    *,
    player_id: int,
    account_id: int,
    session_id: int,
    now: int | None = None,
    ttl: int | None = None,
) -> tuple[str, int]:
    if not player_token_secret_ok(settings):
        raise GameError("player token secret is not configured", status_code=503, code="unavailable")
    issued = int(time.time()) if now is None else int(now)
    lifetime = settings.player_access_ttl_seconds if ttl is None else int(ttl)
    expires_at = issued + lifetime
    payload = {
        "aid": int(account_id),
        "exp": expires_at,
        "pid": int(player_id),
        "sid": int(session_id),
        "typ": "access",
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _sign(settings.player_token_secret, body)
    return f"{_TOKEN_PREFIX}.{body}.{signature}", expires_at


@dataclass(frozen=True)
class AccessClaims:
    player_id: int
    account_id: int
    session_id: int
    expires_at: int


def read_access_token(token: str, settings: Settings, *, now: int | None = None) -> AccessClaims | None:
    if not player_token_secret_ok(settings):
        return None
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX or not parts[1] or not parts[2]:
        return None
    try:
        payload = json.loads(_b64decode(parts[1]))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("typ") != "access":
        return None
    try:
        player_id = int(payload["pid"])
        account_id = int(payload["aid"])
        session_id = int(payload["sid"])
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError):
        return None
    expected = _sign(settings.player_token_secret, parts[1])
    if len(expected) != len(parts[2]) or not hmac.compare_digest(expected, parts[2]):
        return None
    current = int(time.time()) if now is None else int(now)
    if expires_at <= current:
        return None
    return AccessClaims(
        player_id=player_id,
        account_id=account_id,
        session_id=session_id,
        expires_at=expires_at,
    )


def parse_refresh_token(token: str) -> tuple[int, str] | None:
    parts = (token or "").split(".", 2)
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX or not parts[1].isdigit() or not parts[2]:
        return None
    return int(parts[1]), parts[2]


def refresh_matches(row: PlayerRefreshSession, secret: str) -> bool:
    return hmac.compare_digest(row.token_hash, _digest(secret))


def revoke_session(row: PlayerRefreshSession, *, when: datetime | None = None) -> None:
    if row.revoked_at is None:
        row.revoked_at = when or utcnow()


def revoke_family(session: Session, family_id: str, *, when: datetime | None = None) -> int:
    moment = when or utcnow()
    rows = session.scalars(
        select(PlayerRefreshSession).where(PlayerRefreshSession.family_id == family_id)
    ).all()
    count = 0
    for row in rows:
        if row.revoked_at is None:
            row.revoked_at = moment
            count += 1
    return count


def revoke_account_sessions(session: Session, account_id: int, *, when: datetime | None = None) -> int:
    moment = when or utcnow()
    rows = session.scalars(
        select(PlayerRefreshSession).where(PlayerRefreshSession.account_id == account_id)
    ).all()
    count = 0
    for row in rows:
        if row.revoked_at is None:
            row.revoked_at = moment
            count += 1
    return count


def login_locked(account: PlayerAccount, *, now: datetime | None = None) -> bool:
    if account.locked:
        return True
    until = account.login_locked_until
    if until is None:
        return False
    current = now or datetime.now(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > current


def player_token_secret_ok(settings: Settings) -> bool:
    from simcore.runtime_secrets import player_token_secret_is_usable

    return player_token_secret_is_usable(
        settings.player_token_secret,
        production=settings.env == "production",
    )


def token_response(
    settings: Settings,
    account: PlayerAccount,
    player: Player,
    issued: IssuedTokens,
) -> dict[str, object]:
    return {
        "token_type": "bearer",
        "access_token": issued.access_token,
        "refresh_token": issued.refresh_token,
        "expires_in": max(0, issued.access_expires_at - int(time.time())),
        "refresh_expires_in": settings.player_refresh_ttl_seconds,
        "must_change_password": bool(account.must_change_password),
        "player_id": player.id,
        "player_name": player.name,
        "username": account.username,
    }


def _player_id(account: PlayerAccount) -> int:
    if account.player_id is None:
        raise GameError("this account is not linked to a player", status_code=403, code="account_unlinked")
    return int(account.player_id)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _sign(secret: str, body: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return _b64encode(digest)


def _b64encode(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    import base64

    padded = text + ("=" * (-len(text) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))
