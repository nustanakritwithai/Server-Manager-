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


RESOURCES: tuple[str, ...] = ("wood", "food", "iron", "gold")
LOOT_ORDER: tuple[str, ...] = ("gold", "iron", "wood", "food")
