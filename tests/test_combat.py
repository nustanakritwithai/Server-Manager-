from simcore.game.combat import CombatSide, UnitStack, resolve_battle


def _side(*stacks: tuple[str, int], resources: dict[str, int] | None = None) -> CombatSide:
    payload = tuple(UnitStack(unit_type, count) for unit_type, count in stacks)
    if resources is None:
        return CombatSide(payload)
    return CombatSide(payload, resources=tuple(resources.items()))


def test_same_seed_is_identical_and_recorded() -> None:
    attacker = _side(("infantry", 25), ("cavalry", 10))
    defender = _side(("militia", 40), ("archer", 15), resources={"wood": 500, "food": 400, "iron": 200, "gold": 80})
    first = resolve_battle(attacker, defender, seed=8675309)
    second = resolve_battle(attacker, defender, seed=8675309)
    assert first == second
    assert first.seed == 8675309
    assert first.rounds


def test_input_order_does_not_change_the_result() -> None:
    left = resolve_battle(
        _side(("cavalry", 10), ("infantry", 25)),
        _side(("archer", 15), ("militia", 40)),
        seed=4,
    )
    right = resolve_battle(
        _side(("infantry", 25), ("cavalry", 10)),
        _side(("militia", 40), ("archer", 15)),
        seed=4,
    )
    assert left == right


def test_empty_defender_loots_by_carry_and_percent() -> None:
    # 10 infantry carry 300. 30% of 100/100/50/40 is 30/30/15/12, all within carry.
    result = resolve_battle(
        _side(("infantry", 10)),
        _side(resources={"wood": 100, "food": 100, "iron": 50, "gold": 40}),
        seed=1,
    )
    assert result.winner == "attacker"
    assert result.rounds == ()
    assert result.attacker_casualties == ()
    assert dict(result.loot) == {"gold": 12, "iron": 15, "wood": 30, "food": 30}


def test_large_attack_wipes_militia_and_fills_carry_in_loot_order() -> None:
    # 80 infantry cannot lose a unit to 10 militia at any variance this LCG allows.
    # Carry is 2400. 30% of 4000 is 1200, so gold and iron fill the wagons and wood/food stay.
    result = resolve_battle(
        _side(("infantry", 80)),
        _side(("militia", 10), resources={"wood": 4000, "food": 4000, "iron": 4000, "gold": 4000}),
        seed=99,
    )
    assert result.winner == "attacker"
    assert result.attacker_casualties == ()
    assert result.attacker_remaining == (UnitStack("infantry", 80),)
    assert result.defender_remaining == ()
    assert result.defender_casualties == (UnitStack("militia", 10),)
    assert dict(result.loot) == {"gold": 1200, "iron": 1200, "wood": 0, "food": 0}


def test_defender_win_takes_no_loot() -> None:
    result = resolve_battle(_side(("militia", 1)), _side(("cavalry", 50), resources={"gold": 9999}), seed=3)
    assert result.winner == "defender"
    assert all(amount == 0 for _, amount in result.loot)
