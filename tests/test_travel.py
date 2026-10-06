import pytest

from simcore.game.combat import UnitStack
from simcore.game.travel import army_speed, distance, travel_seconds


def test_three_four_five_infantry() -> None:
    assert distance(0, 0, 3, 4) == 5
    # 5 tiles at 6 tiles/hour = 3000 seconds exactly.
    assert travel_seconds(0, 0, 3, 4, [UnitStack("infantry", 12)]) == 3000


def test_thirty_forty_fifty_uses_the_slowest_unit() -> None:
    stacks = [UnitStack("cavalry", 5), UnitStack("infantry", 40)]
    assert army_speed(stacks) == 6
    assert travel_seconds(10, 10, 40, 50, stacks) == 30000
    # Cavalry alone cover the same road in half the time.
    assert travel_seconds(0, 0, 30, 40, [UnitStack("cavalry", 1)]) == 15000


def test_zero_distance_is_rejected() -> None:
    with pytest.raises(ValueError, match="zero distance"):
        travel_seconds(4, 4, 4, 4, [UnitStack("militia", 1)])


def test_diagonal_is_deterministic_and_at_least_one_second() -> None:
    first = travel_seconds(0, 0, 1, 1, [UnitStack("archer", 3)])
    second = travel_seconds(0, 0, 1, 1, [UnitStack("archer", 3)])
    assert first == second
    assert first >= 1
