"""Open a trace for one accepted player command.

The id is a random UUID. Combat seeds do not read it. Follow-on rows copy the
same value; they do not open another command.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from simcore.models import PlayerCommand


def new_trace_id() -> str:
    return str(uuid.uuid4())


def record_command(
    session: Session,
    *,
    player_id: int,
    command_type: str,
    army_id: int | None,
    target: dict[str, Any],
    now: datetime,
) -> str:
    trace_id = new_trace_id()
    session.add(
        PlayerCommand(
            trace_id=trace_id,
            player_id=player_id,
            command_type=command_type,
            army_id=army_id,
            target=target,
            accepted_at=now,
            created_at=now,
        )
    )
    session.flush()
    return trace_id
