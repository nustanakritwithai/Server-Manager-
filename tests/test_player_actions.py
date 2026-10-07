"""Train, found, garrison, and transfer. Rejected orders do not change the world."""

from __future__ import annotations

from simcore.db import get_sessionmaker
from simcore.models import Army, City
from tests.conftest import ADMIN
from tests.world import create_scenario


def _login(client, name: str) -> dict[str, str]:
    response = client.post("/v1/auth/dev-login", json={"name": name})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _stocks(client, headers: dict[str, str]) -> list[dict]:
    response = client.get("/v1/me/cities", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["cities"]


def test_new_commands_spend_through_the_ledger_and_reject_cleanly(client, frozen) -> None:
    create_scenario(frozen.now(), rate=0, stock=500)
    session = get_sessionmaker()()
    try:
        camp = City(
            player_id=1,
            name="Camp",
            x=0,
            y=2,
            wood=100,
            food=100,
            iron=100,
            gold=100,
            wood_rate=0,
            food_rate=0,
            iron_rate=0,
            gold_rate=0,
            buildings={},
            last_updated=frozen.now(),
            created_at=frozen.now(),
        )
        session.add(camp)
        session.commit()
        camp_id = camp.id
    finally:
        session.close()

    alice = _login(client, "Alice")
    before = _stocks(client, alice)
    own = client.post(
        "/v1/commands/attack",
        json={"army_id": 1, "target_city_id": 1},
        headers=alice,
    )
    assert own.status_code == 400, own.text
    stolen = client.post(
        "/v1/commands/transfer",
        json={
            "source_city_id": 1,
            "destination_city_id": 2,
            "wood": 10,
            "food": 0,
            "iron": 0,
            "gold": 0,
        },
        headers=alice,
    )
    assert stolen.status_code == 403, stolen.text
    assert _stocks(client, alice) == before

    trained = client.post(
        "/v1/commands/train",
        json={"city_id": 1, "unit_type": "militia", "count": 2, "army_id": 1},
        headers=alice,
    )
    assert trained.status_code == 200, trained.text
    assert trained.json()["trace_id"]
    advanced = client.post("/v1/admin/clock/advance", json={"seconds": 60}, headers=ADMIN)
    assert advanced.status_code == 200, advanced.text
    tick = client.post("/v1/admin/worker/tick", headers=ADMIN)
    assert tick.status_code == 200, tick.text
    session = get_sessionmaker()()
    try:
        army = session.get(Army, 1)
        assert army is not None
        militia = next(stack for stack in army.units if stack["type"] == "militia")
        assert militia["count"] == 2
    finally:
        session.close()

    founded = client.post(
        "/v1/commands/found-city",
        json={"source_city_id": 1, "x": 4, "y": 4, "name": "Newhold"},
        headers=alice,
    )
    assert founded.status_code == 200, founded.text
    assert founded.json()["x"] == 4
    assert founded.json()["player_id"] == 1
    again = client.post(
        "/v1/commands/found-city",
        json={"source_city_id": 1, "x": 4, "y": 4, "name": "Newhold"},
        headers=alice,
    )
    assert again.status_code == 409, again.text

    garrisoned = client.post(
        "/v1/commands/garrison",
        json={"army_id": 1, "city_id": camp_id},
        headers=alice,
    )
    assert garrisoned.status_code == 200, garrisoned.text
    assert garrisoned.json()["mission"] == "garrison"
    client.post("/v1/admin/clock/advance", json={"seconds": 1200}, headers=ADMIN)
    client.post("/v1/admin/worker/tick", headers=ADMIN)
    armies = client.get("/v1/me/armies", headers=alice).json()["armies"]
    assert armies[0]["location_city_id"] == camp_id
    assert armies[0]["status"] == "garrisoned"

    moved = client.post(
        "/v1/commands/transfer",
        json={
            "source_city_id": 1,
            "destination_city_id": camp_id,
            "wood": 25,
            "food": 0,
            "iron": 0,
            "gold": 0,
        },
        headers=alice,
    )
    assert moved.status_code == 200, moved.text
    replay = client.post(
        "/v1/commands/transfer",
        json={
            "source_city_id": 1,
            "destination_city_id": camp_id,
            "wood": 25,
            "food": 0,
            "iron": 0,
            "gold": 0,
        },
        headers=alice,
    )
    assert replay.status_code == 409, replay.text
    client.post("/v1/admin/clock/advance", json={"seconds": 800}, headers=ADMIN)
    client.post("/v1/admin/worker/tick", headers=ADMIN)
    stocks = {city["id"]: city["wood"] for city in _stocks(client, alice)}
    assert stocks[camp_id] == 125
