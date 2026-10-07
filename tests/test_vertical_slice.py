"""Full path: attack command, fast-forward, worker, report, return, ledger, replay."""

from __future__ import annotations

from datetime import datetime

from simcore.game.combat import CombatSide, UnitStack, resolve_battle
from simcore.game.processor import battle_seed
from tests.conftest import ADMIN
from tests.world import create_scenario

RATE = 360
STOCK = 1000
OUTBOUND = 30_000
HOMEWARD = 30_000


def produced(rate: int, seconds: int) -> int:
    """The contract from GAME_RULES: rate_per_hour * elapsed_seconds // 3600."""

    return rate * seconds // 3600


def _login(client, name: str) -> dict[str, str]:
    response = client.post("/v1/auth/dev-login", json={"name": name})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dev_only"] is True
    assert "DEV ONLY" in body["warning"]
    return {"Authorization": f"Bearer {body['token']}"}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_attack_resolves_while_the_player_is_offline_and_replay_is_a_noop(client, frozen) -> None:
    create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    alice = _login(client, "Alice")
    bob = _login(client, "Bob")

    clock = client.get("/v1/time", headers=alice)
    assert clock.status_code == 200
    assert clock.json()["offset_seconds"] == 0

    world = client.get("/v1/map/cities", headers=alice).json()["cities"]
    assert {city["name"] for city in world} == {"Oakhold", "Ironford"}
    assert sum(1 for city in world if city["is_mine"]) == 1

    armies = client.get("/v1/me/armies", headers=alice).json()["armies"]
    assert armies[0]["status"] == "garrisoned"
    assert armies[0]["movement"] is None

    target = next(city for city in world if not city["is_mine"])
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": armies[0]["id"], "target_city_id": target["id"]},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    march = attack.json()
    assert march["mission"] == "attack"
    assert march["status"] == "in_progress"
    depart = _parse(march["depart_at"])
    arrive = _parse(march["arrive_at"])
    assert (arrive - depart).total_seconds() == OUTBOUND

    # The player disconnects. Only the debug clock and the worker run from here.
    advanced = client.post("/v1/admin/clock/advance", json={"seconds": OUTBOUND + HOMEWARD}, headers=ADMIN)
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["offset_seconds"] == OUTBOUND + HOMEWARD

    tick = client.post("/v1/admin/worker/tick", headers=ADMIN)
    assert tick.status_code == 200, tick.text
    assert tick.json()["processed"] == 2
    assert tick.json()["failed"] == 0
    event_ids = tick.json()["event_ids"]

    reports = client.get("/v1/me/reports", headers=alice).json()["reports"]
    assert len(reports) == 1
    report = reports[0]
    assert report["winner"] == "attacker"
    assert report["loot"] == {"wood": 0, "food": 0, "iron": 1200, "gold": 1200}
    assert report["attacker_casualties"] == []
    assert report["defender_casualties"] == [{"type": "militia", "count": 10}]
    assert report["seed"] == battle_seed(report["event_id"], report["movement_id"])

    replay = resolve_battle(
        CombatSide(tuple(UnitStack(stack["type"], stack["count"]) for stack in report["attacker_before"])),
        CombatSide(
            tuple(UnitStack(stack["type"], stack["count"]) for stack in report["defender_before"]),
            resources=tuple(report["defender_resources"].items()),
        ),
        report["seed"],
    )
    assert replay.winner == report["winner"]
    assert {name: amount for name, amount in replay.loot} == report["loot"]

    events = client.get("/v1/admin/events", headers=ADMIN).json()["events"]
    by_id = {event["id"]: event for event in events}
    arrive_event = by_id[report["event_id"]]
    return_event = next(event for event in events if event["type"] == "ARMY_RETURN")
    assert arrive_event["status"] == "completed"
    assert return_event["status"] == "completed"
    assert (_parse(return_event["due_at"]) - _parse(arrive_event["due_at"])).total_seconds() == HOMEWARD

    alice_city = client.get("/v1/me/cities", headers=alice).json()["cities"][0]
    bob_city = client.get("/v1/me/cities", headers=bob).json()["cities"][0]
    upkeep = 10 * OUTBOUND // 3600
    assert alice_city["gold"] == STOCK + produced(RATE, OUTBOUND + HOMEWARD) + 1200
    assert alice_city["iron"] == STOCK + produced(RATE, OUTBOUND + HOMEWARD) + 1200
    assert alice_city["wood"] == STOCK + produced(RATE, OUTBOUND + HOMEWARD)
    assert alice_city["food"] == STOCK + produced(RATE, OUTBOUND + HOMEWARD)
    assert bob_city["gold"] == STOCK + produced(RATE, OUTBOUND) - 1200 + produced(RATE, HOMEWARD)
    assert bob_city["iron"] == STOCK + produced(RATE, OUTBOUND) - 1200 + produced(RATE, HOMEWARD)
    assert bob_city["wood"] == STOCK + produced(RATE, OUTBOUND) + produced(RATE, HOMEWARD)
    assert bob_city["food"] == STOCK + produced(RATE, OUTBOUND) - upkeep + produced(RATE, HOMEWARD)

    home = client.get("/v1/me/armies", headers=alice).json()["armies"][0]
    assert home["status"] == "garrisoned"
    assert home["location_city_id"] == home["home_city_id"]
    assert home["units"] == [{"type": "infantry", "count": 80}]

    board = client.get("/v1/admin/armies", headers=ADMIN).json()["armies"]
    defender = next(army for army in board if army["player_id"] != home["player_id"])
    assert defender["status"] == "destroyed"
    assert defender["units"] == []

    ledger = client.get("/v1/admin/transactions", headers=ADMIN, params={"limit": 500}).json()["transactions"]
    lost = [row for row in ledger if row["reason"] == "loot_lost"]
    gained = [row for row in ledger if row["reason"] == "loot_gained"]
    assert sum(row["delta"] for row in lost) == -2400
    assert sum(row["delta"] for row in gained) == 2400
    assert {row["source_event_id"] for row in lost} == {arrive_event["id"]}
    assert {row["source_event_id"] for row in gained} == {return_event["id"]}

    # The player comes back, the worker ticks again, and the same events are forced.
    # Nothing in the world moves.
    snapshot = (alice_city["gold"], bob_city["gold"], len(ledger), len(reports))
    again = client.post("/v1/admin/worker/tick", headers=ADMIN)
    assert again.json()["processed"] == 0
    for event_id in event_ids:
        rerun = client.post(f"/v1/admin/events/{event_id}/run", headers=ADMIN)
        assert rerun.status_code == 200, rerun.text
        assert rerun.json()["status"] == "completed"

    alice_again = client.get("/v1/me/cities", headers=alice).json()["cities"][0]["gold"]
    bob_again = client.get("/v1/me/cities", headers=bob).json()["cities"][0]["gold"]
    ledger_again = client.get("/v1/admin/transactions", headers=ADMIN, params={"limit": 500}).json()["transactions"]
    reports_again = client.get("/v1/me/reports", headers=alice).json()["reports"]
    assert (alice_again, bob_again, len(ledger_again), len(reports_again)) == snapshot
