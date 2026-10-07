"""Deterministic trace ids for an in-process CI server.

``new_trace_id`` is a random UUID in production. The world snapshot checksum
includes those ids, so two CI runs only match when the server draws them from
the scenario seed. Staging talks to another process and does not patch this.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable

from simcore.game import tracing


def uuid_for(rng: random.Random) -> str:
    bits = rng.getrandbits(128)
    bits &= ~(0xF << 76)
    bits |= 0x4 << 76
    bits &= ~(0x3 << 62)
    bits |= 0x2 << 62
    return str(uuid.UUID(int=bits))


def install(seed: int) -> Callable[[], None]:
    """Replace ``new_trace_id`` until the returned function runs."""

    rng = random.Random(f"simcore-trace-{seed}")
    previous = tracing.new_trace_id

    def _next() -> str:
        return uuid_for(rng)

    tracing.new_trace_id = _next

    def restore() -> None:
        tracing.new_trace_id = previous

    return restore
