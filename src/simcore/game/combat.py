"""Deterministic combat.

resolve_battle is a pure function: no clock, no database, no global RNG.
The same stacks, resources, and seed always produce the same BattleResult.
"""

from __future__ import annotations

from dataclasses import dataclass

from simcore.game.catalog import (
    LOOT_PERCENT,
    MAX_BATTLE_ROUNDS,
    UNIT_CATALOG,
    VARIANCE_MIN_BP,
    VARIANCE_SPAN,
)
from simcore.constants import LOOT_ORDER


@dataclass(frozen=True)
class UnitStack:
    unit_type: str
    count: int


@dataclass(frozen=True)
class CombatSide:
    stacks: tuple[UnitStack, ...]
    resources: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class RoundLog:
    round_index: int
    attacker_variance_bp: int
    defender_variance_bp: int
    damage_to_attacker: int
    damage_to_defender: int


@dataclass(frozen=True)
class BattleResult:
    winner: str
    seed: int
    rounds: tuple[RoundLog, ...]
    attacker_before: tuple[UnitStack, ...]
    defender_before: tuple[UnitStack, ...]
    attacker_remaining: tuple[UnitStack, ...]
    defender_remaining: tuple[UnitStack, ...]
    attacker_casualties: tuple[UnitStack, ...]
    defender_casualties: tuple[UnitStack, ...]
    loot: tuple[tuple[str, int], ...]


class LCG:
    """Numerical Recipes 32-bit LCG. Isolated so replays do not depend on CPython's random."""

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFF
        if self.state == 0:
            self.state = 1

    def next_u32(self) -> int:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state

    def randbelow(self, n: int) -> int:
        if n <= 0:
            raise ValueError("n must be positive")
        return self.next_u32() % n


def _normalize(stacks: tuple[UnitStack, ...] | list[UnitStack]) -> tuple[UnitStack, ...]:
    merged: dict[str, int] = {}
    for stack in stacks:
        if stack.unit_type not in UNIT_CATALOG:
            raise ValueError(f"unknown unit type {stack.unit_type}")
        if stack.count < 0:
            raise ValueError("unit count cannot be negative")
        merged[stack.unit_type] = merged.get(stack.unit_type, 0) + stack.count
    return tuple(UnitStack(unit_type, count) for unit_type, count in sorted(merged.items()) if count > 0)


def _stats(stacks: tuple[UnitStack, ...]) -> tuple[int, int]:
    atk = 0
    defense = 0
    for stack in stacks:
        unit = UNIT_CATALOG[stack.unit_type]
        atk += stack.count * unit.atk
        defense += stack.count * unit.defense
    return atk, defense


def _hp(stacks: tuple[UnitStack, ...]) -> int:
    return sum(stack.count * UNIT_CATALOG[stack.unit_type].hp for stack in stacks)


def _damage(atk: int, variance_bp: int, defense: int) -> int:
    if atk <= 0:
        return 0
    return atk * variance_bp // (100 * (100 + defense))


def _apply_damage(stacks: tuple[UnitStack, ...], damage: int) -> tuple[UnitStack, ...]:
    if damage <= 0 or not stacks:
        return stacks
    order = sorted(
        stacks,
        key=lambda stack: (
            UNIT_CATALOG[stack.unit_type].defense,
            UNIT_CATALOG[stack.unit_type].atk,
            stack.unit_type,
        ),
    )
    counts = {stack.unit_type: stack.count for stack in stacks}
    remaining = damage
    for stack in order:
        unit = UNIT_CATALOG[stack.unit_type]
        killed = min(counts[stack.unit_type], remaining // unit.hp)
        counts[stack.unit_type] -= killed
        remaining -= killed * unit.hp
        if remaining <= 0:
            break
    return tuple(UnitStack(unit_type, count) for unit_type, count in sorted(counts.items()) if count > 0)


def _casualties(before: tuple[UnitStack, ...], after: tuple[UnitStack, ...]) -> tuple[UnitStack, ...]:
    remaining = {stack.unit_type: stack.count for stack in after}
    losses: list[UnitStack] = []
    for stack in before:
        lost = stack.count - remaining.get(stack.unit_type, 0)
        if lost > 0:
            losses.append(UnitStack(stack.unit_type, lost))
    return tuple(losses)


def _loot(attacker: tuple[UnitStack, ...], resources: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    carry = sum(stack.count * UNIT_CATALOG[stack.unit_type].carry for stack in attacker)
    available = {name: max(0, amount) for name, amount in resources}
    taken: dict[str, int] = {}
    for resource in LOOT_ORDER:
        if carry <= 0:
            taken[resource] = 0
            continue
        desired = available.get(resource, 0) * LOOT_PERCENT // 100
        amount = min(desired, carry)
        taken[resource] = amount
        carry -= amount
    return tuple((resource, taken[resource]) for resource in LOOT_ORDER)


def _empty_loot() -> tuple[tuple[str, int], ...]:
    return tuple((resource, 0) for resource in LOOT_ORDER)


def resolve_battle(attacker: CombatSide, defender: CombatSide, seed: int) -> BattleResult:
    """Resolve one battle.

    Each round both sides deal damage at the same time:
        damage = atk * variance_bp // (100 * (100 + opponent_defense))
    variance_bp is an integer in [9000, 10999] drawn from the seeded LCG.
    Damage removes whole units, lowest defense first. Leftover damage that does
    not fill a unit's HP is discarded. After at most 8 rounds the side still
    standing wins; if both stand, the higher remaining HP wins.
    """

    atk_before = _normalize(attacker.stacks)
    def_before = _normalize(defender.stacks)
    rng = LCG(seed)
    rounds: list[RoundLog] = []
    atk_now = atk_before
    def_now = def_before

    if atk_now and def_now:
        for index in range(1, MAX_BATTLE_ROUNDS + 1):
            atk_power, atk_defense = _stats(atk_now)
            def_power, def_defense = _stats(def_now)
            atk_var = VARIANCE_MIN_BP + rng.randbelow(VARIANCE_SPAN)
            def_var = VARIANCE_MIN_BP + rng.randbelow(VARIANCE_SPAN)
            dmg_to_def = _damage(atk_power, atk_var, def_defense)
            dmg_to_atk = _damage(def_power, def_var, atk_defense)
            next_atk = _apply_damage(atk_now, dmg_to_atk)
            next_def = _apply_damage(def_now, dmg_to_def)
            rounds.append(
                RoundLog(
                    round_index=index,
                    attacker_variance_bp=atk_var,
                    defender_variance_bp=def_var,
                    damage_to_attacker=dmg_to_atk,
                    damage_to_defender=dmg_to_def,
                )
            )
            atk_now = next_atk
            def_now = next_def
            if not atk_now or not def_now:
                break

    if atk_now and not def_now:
        winner = "attacker"
    elif def_now and not atk_now:
        winner = "defender"
    elif not atk_now and not def_now:
        winner = "draw"
    else:
        atk_hp = _hp(atk_now)
        def_hp = _hp(def_now)
        if atk_hp > def_hp:
            winner = "attacker"
        elif def_hp > atk_hp:
            winner = "defender"
        else:
            winner = "draw"

    loot = _loot(atk_now, defender.resources) if winner == "attacker" and atk_now else _empty_loot()
    return BattleResult(
        winner=winner,
        seed=seed,
        rounds=tuple(rounds),
        attacker_before=atk_before,
        defender_before=def_before,
        attacker_remaining=atk_now,
        defender_remaining=def_now,
        attacker_casualties=_casualties(atk_before, atk_now),
        defender_casualties=_casualties(def_before, def_now),
        loot=loot,
    )


def stacks_to_payload(stacks: tuple[UnitStack, ...]) -> list[dict[str, int | str]]:
    return [{"type": stack.unit_type, "count": stack.count} for stack in stacks]


def payload_to_stacks(payload: list[dict[str, int | str]] | tuple[UnitStack, ...]) -> tuple[UnitStack, ...]:
    stacks: list[UnitStack] = []
    for item in payload:
        if isinstance(item, UnitStack):
            stacks.append(item)
        else:
            stacks.append(UnitStack(str(item["type"]), int(item["count"])))
    return _normalize(tuple(stacks))


def loot_to_dict(loot: tuple[tuple[str, int], ...]) -> dict[str, int]:
    return {name: amount for name, amount in loot}
