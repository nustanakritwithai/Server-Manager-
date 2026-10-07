"""Admin password checks and HMAC session tokens.

The plaintext password is never stored or logged. SIMCORE_ADMIN_PASSWORD_HASH
holds a scrypt hash from hashlib (stdlib). Argon2id would need a native
package on the Windows VPS; scrypt is memory-hard and already in Python.

Session tokens are HMAC-SHA256 signed with SIMCORE_ADMIN_SESSION_SECRET.
They are not cookies. The admin page on GitHub Pages and the API are
different origins, and mobile browsers often drop third-party cookies.

Revoking one session drops its id in this process. Revoking every session
bumps an in-process epoch until restart. A restart keeps old tokens valid
unless SIMCORE_ADMIN_SESSION_VERSION is increased or the session secret is
rotated in .env.prod.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import Request

from simcore.config import Settings

# Interactive scrypt: 32 MiB, one hash is about 100 ms on this host.
# 128 * N * r * p = 32 MiB. maxmem sits above that so OpenSSL accepts it.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SCRYPT_N_LIMIT = 2**16
_TOKEN_PREFIX = "simadm1"
_MIN_SECRET_LENGTH = 32
_MAX_PASSWORD_LENGTH = 1024
_INVISIBLE = ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00a0", "\r", "\n")


def normalize_admin_password(value: str) -> str:
    """Drop paste artifacts, then trim. Internal spaces stay."""

    text = str(value or "")
    for char in _INVISIBLE:
        text = text.replace(char, "")
    return text.strip()


def secrets_equal(left: str, right: str) -> bool:
    """Compare two strings without leaking the mismatch position."""

    return hmac.compare_digest(
        hashlib.sha256(left.encode("utf-8")).digest(),
        hashlib.sha256(right.encode("utf-8")).digest(),
    )


def hash_admin_password(password: str) -> str:
    cleaned = normalize_admin_password(password)
    if not cleaned:
        raise ValueError("password is empty")
    if len(cleaned) > _MAX_PASSWORD_LENGTH:
        raise ValueError("password is too long")
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        cleaned.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64encode(salt),
            _b64encode(derived),
        )
    )


def password_hash_is_usable(stored: str) -> bool:
    return _parse_hash(stored) is not None


def verify_admin_password(password: str, stored: str) -> bool:
    parsed = _parse_hash(stored)
    if parsed is None:
        return False
    cleaned = normalize_admin_password(password)
    if not cleaned or len(cleaned) > _MAX_PASSWORD_LENGTH:
        return False
    n, r, p, salt, expected = parsed
    try:
        derived = hashlib.scrypt(
            cleaned.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except ValueError:
        return False
    return hmac.compare_digest(derived, expected)


def client_address(request: Request) -> str:
    """Use the TCP peer. Trust X-Forwarded-For only from a loopback proxy."""

    peer = ""
    if request.client is not None and request.client.host:
        peer = request.client.host
    if peer in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return peer or "unknown"


def session_secret_is_usable(settings: Settings) -> bool:
    secret = settings.admin_session_secret.strip()
    return len(secret) >= _MIN_SECRET_LENGTH


@dataclass(frozen=True)
class AdminSession:
    expires_at: int
    version: int
    epoch: int
    jti: str


class AdminSessionBook:
    """In-process revocation. Durable revoke is the env session version."""

    def __init__(self) -> None:
        self.epoch = 0
        self._revoked: set[str] = set()
        self._lock = threading.Lock()

    def revoke(self, jti: str) -> None:
        if not jti:
            return
        with self._lock:
            self._revoked.add(jti)

    def revoke_all(self) -> int:
        with self._lock:
            self.epoch += 1
            self._revoked.clear()
            return self.epoch

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            return jti in self._revoked

    def current_epoch(self) -> int:
        with self._lock:
            return self.epoch


class LoginRateLimiter:
    """Per-address failure window. In-process is enough for one API service."""

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        self.max_failures = max(1, int(max_failures))
        self.window_seconds = max(1, int(window_seconds))
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, key: str, now: float) -> bool:
        with self._lock:
            recent = self._prune(key, now)
            return len(recent) < self.max_failures

    def record_failure(self, key: str, now: float) -> None:
        with self._lock:
            recent = self._prune(key, now)
            recent.append(now)
            self._failures[key] = recent

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _prune(self, key: str, now: float) -> list[float]:
        recent = [stamp for stamp in self._failures.get(key, []) if now - stamp < self.window_seconds]
        self._failures[key] = recent
        return recent


def issue_admin_session(
    settings: Settings,
    book: AdminSessionBook,
    *,
    now: int | None = None,
    ttl: int | None = None,
) -> tuple[str, int]:
    if not session_secret_is_usable(settings):
        raise ValueError("admin session secret is not configured")
    issued = int(time.time()) if now is None else int(now)
    lifetime = settings.admin_session_ttl_seconds if ttl is None else int(ttl)
    if lifetime < 1:
        raise ValueError("session lifetime must be positive")
    expires_at = issued + lifetime
    payload = {
        "e": book.current_epoch(),
        "exp": expires_at,
        "jti": secrets.token_urlsafe(16),
        "v": int(settings.admin_session_version),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _sign(settings.admin_session_secret, body)
    return f"{_TOKEN_PREFIX}.{body}.{signature}", expires_at


def read_admin_session(
    token: str,
    settings: Settings,
    book: AdminSessionBook,
    *,
    now: int | None = None,
) -> AdminSession | None:
    if not session_secret_is_usable(settings):
        return None
    parsed = _parse_token(token)
    if parsed is None:
        return None
    body, signature, payload = parsed
    expected = _sign(settings.admin_session_secret, body)
    if not secrets_equal(expected, signature):
        return None
    current = int(time.time()) if now is None else int(now)
    if int(payload["exp"]) <= current:
        return None
    if int(payload["v"]) != int(settings.admin_session_version):
        return None
    if int(payload["e"]) != book.current_epoch():
        return None
    jti = str(payload["jti"])
    if book.is_revoked(jti):
        return None
    return AdminSession(
        expires_at=int(payload["exp"]),
        version=int(payload["v"]),
        epoch=int(payload["e"]),
        jti=jti,
    )


def _parse_hash(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = (stored or "").strip().split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = _b64decode(parts[4])
        derived = _b64decode(parts[5])
    except (ValueError, TypeError):
        return None
    if n < 2 or n > _SCRYPT_N_LIMIT or n & (n - 1):
        return None
    if r < 1 or r > 32 or p < 1 or p > 8:
        return None
    if len(salt) < 8 or len(derived) < 16:
        return None
    if 128 * n * r * p > _SCRYPT_MAXMEM:
        return None
    return n, r, p, salt, derived


def _parse_token(token: str) -> tuple[str, str, dict[str, object]] | None:
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX or not parts[1] or not parts[2]:
        return None
    try:
        payload = json.loads(_b64decode(parts[1]))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("exp", "v", "e", "jti"):
        if key not in payload:
            return None
    try:
        int(payload["exp"])
        int(payload["v"])
        int(payload["e"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload["jti"], str) or not payload["jti"]:
        return None
    return parts[1], parts[2], payload


def _sign(secret: str, body: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return _b64encode(digest)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padded = text + ("=" * (-len(text) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))
