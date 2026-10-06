"""Travel time and position along a movement leg."""

from __future__ import annotations

import math
from datetime import datetime

from simcore.game.catalog import UNIT_CATALOG
from simcore.game.combat import UnitStack


def distance(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.hypot(x2 - x1, y2 - y1)


def army_speed(stacks: tuple[UnitStack, ...] | list[UnitStack]) -> int:
    """Tiles per hour. A mixed army moves at the speed of its slowest unit."""

    speeds = [UNIT_CATALOG[stack.unit_type].speed for stack in stacks if stack.count > 0]
    if not speeds:
        raise ValueError("army has no units")
    return min(speeds)


def travel_seconds(x1: float, y1: float, x2: float, y2: float, stacks: tuple[UnitStack, ...] | list[UnitStack]) -> int:
    """Seconds to march from (x1, y1) to (x2, y2).

    time = distance / speed, with speed in tiles per hour.
    Near-integer results caused by binary floating point are snapped, then
    anything else is rounded up so the army never arrives early.
    """

    dist = distance(x1, y1, x2, y2)
    if dist == 0:
        raise ValueError("zero distance")
    speed = army_speed(stacks)
    raw = dist * 3600 / speed
    nearest = round(raw)
    if abs(raw - nearest) < 1e-6:
        seconds = int(nearest)
    else:
        seconds = math.ceil(raw - 1e-9)
    return max(1, int(seconds))


def travel_progress(depart_at: datetime, arrive_at: datetime, now: datetime) -> float:
    total = (arrive_at - depart_at).total_seconds()
    if total <= 0:
        return 1.0
    return min(1.0, max(0.0, (now - depart_at).total_seconds() / total))


def interpolate(x1: float, y1: float, x2: float, y2: float, progress: float) -> tuple[float, float]:
    clamped = min(1.0, max(0.0, progress))
    return (x1 + (x2 - x1) * clamped, y1 + (y2 - y1) * clamped)
