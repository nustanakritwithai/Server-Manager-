"""Bot decisions. The same seed and the same world produce the same plans.

Plans are a pure function of the seed, the tick, and the JSON the public
player routes returned. Submission order is player id, then command index.
Phase 8 can overlap the HTTP sends; this module does not.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from simcore.game.catalog import (
    BUILDINGS,
    FOUND_CITY_COST,
    MAX_CITIES_PER_PLAYER,
    RESEARCH,
    UNIT_TRAINING,
)
from simcore.sim.seed_world import profile_for

# Kept so older reports have a field. The public API now accepts every action.
UNAVAILABLE_ACTIONS: tuple[dict[str, str], ...] = ()

_BUILDINGS = tuple(sorted(BUILDINGS))
_RESEARCH = tuple(sorted(RESEARCH))


@dataclass(frozen=True)
class PlannedCommand:
    tick: int
    player_id: int
    player_name: str
    profile: str
    action: str
    body: dict[str, Any] | None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "player_id": self.player_id,
            "player_name": self.player_name,
            "profile": self.profile,
            "action": self.action,
            "body": self.body,
            "note": self.note,
        }


def plan_tick(
    rng: random.Random,
    *,
    tick: int,
    players: list[dict[str, Any]],
    command_rate: int,
) -> list[PlannedCommand]:
    """Plan every bot for this tick. ``players`` must already be in id order.

    Each entry has ``id``, ``name``, ``armies``, ``cities``, and ``world_cities``
    taken from the player API at the start of the tick. The RNG is consumed
    only for choices the profile actually makes.
    """

    planned: list[PlannedCommand] = []
    for player in players:
        profile = str(player.get("profile") or profile_for(int(player["id"]) - 1))
        armies = [dict(row) for row in player["armies"]]
        cities = list(player["cities"])
        world = list(player["world_cities"])
        for _ in range(command_rate):
            command = _plan_one(
                rng,
                tick=tick,
                player_id=int(player["id"]),
                player_name=str(player["name"]),
                profile=profile,
                armies=armies,
                cities=cities,
                world_cities=world,
            )
            planned.append(command)
            _optimistic(armies, command)
    return planned


def _plan_one(
    rng: random.Random,
    *,
    tick: int,
    player_id: int,
    player_name: str,
    profile: str,
    armies: list[dict[str, Any]],
    cities: list[dict[str, Any]],
    world_cities: list[dict[str, Any]],
) -> PlannedCommand:
    if profile == "aggressive":
        return _aggressive(rng, tick, player_id, player_name, armies, cities, world_cities)
    if profile == "defensive":
        return _defensive(tick, player_id, player_name, armies, cities)
    if profile == "random":
        return _random(rng, tick, player_id, player_name, armies, cities, world_cities)
    return PlannedCommand(
        tick, player_id, player_name, profile, "skip", None, note=f"unknown profile {profile}"
    )


def _aggressive(
    rng: random.Random,
    tick: int,
    player_id: int,
    player_name: str,
    armies: list[dict[str, Any]],
    cities: list[dict[str, Any]],
    world_cities: list[dict[str, Any]],
) -> PlannedCommand:
    ready = _garrisoned(armies)
    if not ready:
        trained = _train_command(tick, player_id, player_name, "aggressive", cities, armies)
        if trained is not None:
            return trained
        note = "army is not garrisoned"
        if any(row.get("status") == "destroyed" for row in armies):
            note = "army is destroyed and no city can afford a militia"
        return PlannedCommand(tick, player_id, player_name, "aggressive", "skip", None, note=note)
    enemies = [
        city
        for city in _by_id(world_cities)
        if int(city["player_id"]) != player_id and int(city["id"]) != int(ready[0]["location_city_id"])
    ]
    if not enemies:
        return PlannedCommand(
            tick, player_id, player_name, "aggressive", "skip", None, note="no enemy city on the map"
        )
    target = enemies[rng.randrange(len(enemies))]
    return PlannedCommand(
        tick,
        player_id,
        player_name,
        "aggressive",
        "attack",
        {"army_id": int(ready[0]["id"]), "target_city_id": int(target["id"])},
    )


def _defensive(
    tick: int,
    player_id: int,
    player_name: str,
    armies: list[dict[str, Any]],
    cities: list[dict[str, Any]],
) -> PlannedCommand:
    ready = _garrisoned(armies)
    own = _by_id(cities)
    if ready and own:
        army = ready[0]
        location = int(army["location_city_id"])
        home = int(army["home_city_id"])
        if location != home:
            return PlannedCommand(
                tick,
                player_id,
                player_name,
                "defensive",
                "recall",
                {"army_id": int(army["id"])},
            )
        others = [city for city in own if int(city["id"]) != location]
        if others:
            return PlannedCommand(
                tick,
                player_id,
                player_name,
                "defensive",
                "garrison",
                {"army_id": int(army["id"]), "city_id": int(others[0]["id"])},
            )
    for army in _by_id(armies):
        if army.get("status") == "marching":
            return PlannedCommand(
                tick,
                player_id,
                player_name,
                "defensive",
                "recall",
                {"army_id": int(army["id"])},
            )
    if own:
        building = _BUILDINGS[tick % len(_BUILDINGS)]
        return PlannedCommand(
            tick,
            player_id,
            player_name,
            "defensive",
            "build",
            {"city_id": int(own[0]["id"]), "building": building},
        )
    return PlannedCommand(tick, player_id, player_name, "defensive", "skip", None, note="no city to garrison")


def _random(
    rng: random.Random,
    tick: int,
    player_id: int,
    player_name: str,
    armies: list[dict[str, Any]],
    cities: list[dict[str, Any]],
    world_cities: list[dict[str, Any]],
) -> PlannedCommand:
    options: list[tuple[Any, ...]] = []
    own = _by_id(cities)
    enemies = [city for city in _by_id(world_cities) if int(city["player_id"]) != player_id]
    for army in _garrisoned(armies):
        location = int(army["location_city_id"])
        for city in enemies:
            if int(city["id"]) != location:
                options.append(("attack", int(army["id"]), int(city["id"])))
        for city in own:
            if int(city["id"]) != location:
                options.append(("reinforce", int(army["id"]), int(city["id"])))
                options.append(("move", int(army["id"]), int(city["id"])))
                options.append(("garrison", int(army["id"]), int(city["id"])))
        if location != int(army["home_city_id"]):
            options.append(("recall", int(army["id"])))
        for city in own:
            if int(city["id"]) == location and _can_train(city, "militia", 1):
                options.append(("train_units", int(city["id"]), "militia", 1, int(army["id"])))
    for army in _by_id(armies):
        if army.get("status") == "marching":
            options.append(("recall", int(army["id"])))
    for city in own:
        for building in _BUILDINGS:
            options.append(("build", int(city["id"]), building))
        if _can_train(city, "militia", 1):
            options.append(("train_units", int(city["id"]), "militia", 1, 0))
    if len(own) >= 2:
        source, destination = own[0], own[1]
        if int(source.get("wood") or 0) >= 10:
            options.append(("transfer_resources", int(source["id"]), int(destination["id"]), 10))
    if len(own) < MAX_CITIES_PER_PLAYER:
        payer = next((city for city in own if _can_afford(city, FOUND_CITY_COST)), None)
        if payer is not None:
            options.append(("found_city", int(payer["id"]), 300 + player_id, 10 + tick, f"Bot{player_id} Outpost"))
    for tech in _RESEARCH:
        options.append(("research", tech))
    options = sorted(set(options))
    if not options:
        return PlannedCommand(tick, player_id, player_name, "random", "skip", None, note="no legal action")
    chosen = options[rng.randrange(len(options))]
    return PlannedCommand(
        tick,
        player_id,
        player_name,
        "random",
        str(chosen[0]),
        _body_for(chosen),
    )


def _body_for(chosen: tuple[Any, ...]) -> dict[str, Any]:
    kind = chosen[0]
    if kind == "attack":
        return {"army_id": chosen[1], "target_city_id": chosen[2]}
    if kind == "reinforce":
        return {"army_id": chosen[1], "destination_city_id": chosen[2], "relocate": False}
    if kind == "move":
        return {"army_id": chosen[1], "destination_city_id": chosen[2], "relocate": True}
    if kind == "garrison":
        return {"army_id": chosen[1], "city_id": chosen[2]}
    if kind == "recall":
        return {"army_id": chosen[1]}
    if kind == "build":
        return {"city_id": chosen[1], "building": chosen[2]}
    if kind == "research":
        return {"tech": chosen[1]}
    if kind == "train_units":
        body: dict[str, Any] = {"city_id": chosen[1], "unit_type": chosen[2], "count": chosen[3]}
        if int(chosen[4]) > 0:
            body["army_id"] = int(chosen[4])
        return body
    if kind == "transfer_resources":
        return {
            "source_city_id": chosen[1],
            "destination_city_id": chosen[2],
            "wood": chosen[3],
            "food": 0,
            "iron": 0,
            "gold": 0,
        }
    if kind == "found_city":
        return {"source_city_id": chosen[1], "x": chosen[2], "y": chosen[3], "name": chosen[4]}
    raise ValueError(f"unknown action {kind}")


def _optimistic(armies: list[dict[str, Any]], command: PlannedCommand) -> None:
    """Keep later commands in the same tick from reusing an army that just marched."""

    if command.action in {"attack", "move", "reinforce", "garrison"} and command.body is not None:
        army_id = int(command.body["army_id"])
        for army in armies:
            if int(army["id"]) == army_id:
                army["status"] = "marching"
                army["location_city_id"] = None
    elif command.action == "recall" and command.body is not None:
        army_id = int(command.body["army_id"])
        for army in armies:
            if int(army["id"]) == army_id:
                army["status"] = "returning"
                army["location_city_id"] = None


def _can_afford(city: dict[str, Any], costs: dict[str, int]) -> bool:
    return all(int(city.get(name) or 0) >= int(amount) for name, amount in costs.items())


def _can_train(city: dict[str, Any], unit_type: str, count: int) -> bool:
    spec = UNIT_TRAINING[unit_type]
    costs = {name: int(getattr(spec, name)) * count for name in ("wood", "food", "iron", "gold")}
    return _can_afford(city, costs)


def _train_command(
    tick: int,
    player_id: int,
    player_name: str,
    profile: str,
    cities: list[dict[str, Any]],
    armies: list[dict[str, Any]],
) -> PlannedCommand | None:
    for city in _by_id(cities):
        if not _can_train(city, "militia", 1):
            continue
        garrisoned = [
            army
            for army in _garrisoned(armies)
            if int(army["location_city_id"]) == int(city["id"])
        ]
        body: dict[str, Any] = {"city_id": int(city["id"]), "unit_type": "militia", "count": 1}
        if garrisoned:
            body["army_id"] = int(garrisoned[0]["id"])
        return PlannedCommand(tick, player_id, player_name, profile, "train_units", body)
    return None


def _garrisoned(armies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ready = [
        army
        for army in armies
        if army.get("status") == "garrisoned" and army.get("location_city_id") is not None
    ]
    return _by_id(ready)


def _by_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: int(row["id"]))
