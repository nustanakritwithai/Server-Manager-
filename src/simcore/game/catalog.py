from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnitType:
    id: str
    atk: int
    defense: int
    hp: int
    speed: int  # tiles per hour
    carry: int  # resource units one troop can haul
    upkeep: int  # food per hour while garrisoned


UNIT_CATALOG: dict[str, UnitType] = {
    "militia": UnitType("militia", atk=4, defense=3, hp=20, speed=7, carry=25, upkeep=1),
    "infantry": UnitType("infantry", atk=10, defense=8, hp=40, speed=6, carry=30, upkeep=2),
    "archer": UnitType("archer", atk=12, defense=4, hp=25, speed=7, carry=20, upkeep=2),
    "cavalry": UnitType("cavalry", atk=16, defense=6, hp=50, speed=12, carry=45, upkeep=4),
}

BUILDINGS: frozenset[str] = frozenset({"lumber_camp", "farm", "iron_mine", "warehouse", "barracks"})
RESEARCH: frozenset[str] = frozenset({"forestry", "husbandry", "metallurgy", "logistics"})

# Stub durations. Build and research do not spend resources in this MVP.
BUILD_SECONDS = 30 * 60
RESEARCH_SECONDS = 60 * 60

MAX_BATTLE_ROUNDS = 8
LOOT_PERCENT = 30
VARIANCE_MIN_BP = 9000
VARIANCE_SPAN = 2000  # rolls in [9000, 10999] basis points, i.e. 0.9000x .. 1.0999x
