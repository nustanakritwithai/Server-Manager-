"""String constants stored in PostgreSQL. Kept as plain strings so the schema stays obvious."""


class ArmyStatus:
    GARRISONED = "garrisoned"
    MARCHING = "marching"
    RETURNING = "returning"
    DESTROYED = "destroyed"


class Mission:
    MOVE = "move"
    ATTACK = "attack"
    RETURN = "return"


class MovementStatus:
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class EventType:
    ARMY_ARRIVE = "ARMY_ARRIVE"
    ARMY_RETURN = "ARMY_RETURN"
    BUILD_COMPLETE = "BUILD_COMPLETE"
    RESEARCH_COMPLETE = "RESEARCH_COMPLETE"


class EventStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Reason:
    PRODUCTION = "production"
    UPKEEP = "upkeep"
    LOOT_LOST = "loot_lost"
    LOOT_GAINED = "loot_gained"


class SnapshotReason:
    """Why a world snapshot was taken. SAFETY is reserved for restore."""

    AUTO = "AUTO"
    MANUAL = "MANUAL"
    SAFETY = "SAFETY"

    ALL = frozenset({AUTO, MANUAL, SAFETY})
    API = frozenset({AUTO, MANUAL})


class SnapshotStatus:
    CREATING = "CREATING"
    READY = "READY"
    FAILED = "FAILED"
    RESTORING = "RESTORING"

    ALL = frozenset({CREATING, READY, FAILED, RESTORING})


# Bump when the captured world document changes shape. Older snapshots stay
# listed, but this server will refuse to restore a different schema_version.
# Version 2 adds nullable trace_id columns and the player_commands table.
SNAPSHOT_SCHEMA_VERSION = 2


RESOURCES: tuple[str, ...] = ("wood", "food", "iron", "gold")
LOOT_ORDER: tuple[str, ...] = ("gold", "iron", "wood", "food")
