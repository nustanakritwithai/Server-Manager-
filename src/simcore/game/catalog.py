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

# Training spends these resources immediately and finishes after seconds * count.
# Costs and times are config, not stored per city. See docs/GAME_RULES.md.
MAX_TRAIN_COUNT = 100

# A new city is paid for from one city the player already owns.
FOUND_CITY_COST: dict[str, int] = {"wood": 150, "food": 150, "iron": 60, "gold": 30}
FOUND_CITY_RATES: dict[str, int] = {"wood": 40, "food": 40, "iron": 20, "gold": 10}

# Founding rejects coordinates outside this inclusive square. Cities already on
# the map are not moved. Seeded and test cities sit inside it.
MAP_MIN = -500
MAP_MAX = 500
MAX_CITIES_PER_PLAYER = 8

# Resource transfers march at this many tiles per hour. Same rounding as armies.
CONVOY_TILES_PER_HOUR = 10


@dataclass(frozen=True)
class TrainCost:
    wood: int
    food: int
    iron: int
    gold: int
    seconds: int


UNIT_TRAINING = {
    "militia": TrainCost(wood=10, food=20, iron=0, gold=0, seconds=30),
    "infantry": TrainCost(wood=20, food=40, iron=15, gold=0, seconds=45),
    "archer": TrainCost(wood=25, food=25, iron=10, gold=5, seconds=45),
    "cavalry": TrainCost(wood=40, food=50, iron=30, gold=15, seconds=60),
}

MAX_BATTLE_ROUNDS = 8
LOOT_PERCENT = 30
VARIANCE_MIN_BP = 9000
VARIANCE_SPAN = 2000  # rolls in [9000, 10999] basis points, i.e. 0.9000x .. 1.0999x
