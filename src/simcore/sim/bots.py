"""Bot decisions. The same seed and the same world produce the same plans.

Plans are a pure function of the seed, the tick, and the JSON the public
player routes returned. Submission order is player id, then command index.
Phase 8 can overlap the HTTP sends; this module does not.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from simcore.game.catalog import BUILDINGS, RESEARCH
from simcore.sim.seed_world import profile_for

# Actions the public API does not offer. Reported on every run.
UNAVAILABLE_ACTIONS: tuple[dict[str, str], ...] = (
    {
        "action": "train_units",
        "reason": "No public endpoint recruits units or replaces a destroyed army.",
    },
    {
        "action": "found_city",
        "reason": "No public endpoint creates a city. Bots only use cities that already exist.",
    },
    {
        "action": "garrison",
        "reason": (
            "No garrison endpoint. A garrison is an army with status garrisoned. "
            "Defensive bots reinforce another own city with POST /v1/commands/move "
            "and walk home with POST /v1/commands/recall."
        ),
    },
    {
        "action": "transfer_resources",
        "reason": "No market or transfer endpoint. Resources move only by server production, upkeep, and loot.",
    },
)

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
        return _aggressive(rng, tick, player_id, player_name, armies, world_cities)
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
    world_cities: list[dict[str, Any]],
) -> PlannedCommand:
    ready = _garrisoned(armies)
    if not ready:
        note = "army is not garrisoned"
        if any(row.get("status") == "destroyed" for row in armies):
            note = "army is destroyed; train_units has no endpoint"
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
                "move",
                {"army_id": int(army["id"]), "destination_city_id": int(others[0]["id"]), "relocate": False},
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
                options.append(("move", int(army["id"]), int(city["id"])))
        if location != int(army["home_city_id"]):
            options.append(("recall", int(army["id"])))
    for army in _by_id(armies):
        if army.get("status") == "marching":
            options.append(("recall", int(army["id"])))
    for city in own:
        for building in _BUILDINGS:
            options.append(("build", int(city["id"]), building))
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
    if kind == "move":
        return {"army_id": chosen[1], "destination_city_id": chosen[2], "relocate": False}
    if kind == "recall":
        return {"army_id": chosen[1]}
    if kind == "build":
        return {"city_id": chosen[1], "building": chosen[2]}
    if kind == "research":
        return {"tech": chosen[1]}
    raise ValueError(f"unknown action {kind}")


def _optimistic(armies: list[dict[str, Any]], command: PlannedCommand) -> None:
    """Keep later commands in the same tick from reusing an army that just marched."""

    if command.action in {"attack", "move"} and command.body is not None:
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


def _garrisoned(armies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ready = [
        army
        for army in armies
        if army.get("status") == "garrisoned" and army.get("location_city_id") is not None
    ]
    return _by_id(ready)


def _by_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: int(row["id"]))
