"""Move, recall, build, and research through the HTTP API."""

from __future__ import annotations

import math
import time
from datetime import datetime

from simcore.constants import ArmyStatus
from simcore.db import get_sessionmaker
from simcore.models import City
from tests.conftest import ADMIN
from tests.world import create_scenario


def _login(client, name: str) -> dict[str, str]:
    response = client.post("/v1/auth/dev-login", json={"name": name})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _advance(client, seconds: int) -> None:
    response = client.post("/v1/admin/clock/advance", json={"seconds": seconds}, headers=ADMIN)
    assert response.status_code == 200, response.text


def _tick(client) -> dict:
    response = client.post("/v1/admin/worker/tick", headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def test_move_then_recall_home(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=100)
    session = get_sessionmaker()()
    try:
        outpost = City(
            player_id=ids["alice_id"],
            name="North Camp",
            x=18,
            y=0,
            wood=0,
            food=0,
            iron=0,
            gold=0,
            wood_rate=0,
            food_rate=0,
            iron_rate=0,
            gold_rate=0,
            buildings={},
            last_updated=frozen.now(),
            created_at=frozen.now(),
        )
        session.add(outpost)
        session.commit()
        outpost_id = outpost.id
    finally:
        session.close()

    alice = _login(client, "Alice")
    moved = client.post(
        "/v1/commands/move",
        json={"army_id": ids["alice_army"], "destination_city_id": outpost_id},
        headers=alice,
    )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["mission"] == "move"
    assert body["arrive_at"] > body["depart_at"]

    _advance(client, 10_800)  # 18 tiles at infantry speed 6
    assert _tick(client)["processed"] == 1
    army = client.get("/v1/me/armies", headers=alice).json()["armies"][0]
    assert army["status"] == "garrisoned"
    assert army["location_city_id"] == outpost_id
    assert army["home_city_id"] == ids["alice_city"]

    recalled = client.post("/v1/commands/recall", json={"army_id": ids["alice_army"]}, headers=alice)
    assert recalled.status_code == 200, recalled.text
    assert recalled.json()["mission"] == "return"
    _advance(client, 10_800)
    assert _tick(client)["processed"] == 1
    army = client.get("/v1/me/armies", headers=alice).json()["armies"][0]
    assert army["status"] == "garrisoned"
    assert army["location_city_id"] == ids["alice_city"]


def test_recall_halfway_cancels_the_attack(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=100)
    alice = _login(client, "Alice")
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": ids["alice_army"], "target_city_id": ids["bob_city"]},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    outbound_id = attack.json()["event_id"]

    _advance(client, 15_000)
    recalled = client.post("/v1/commands/recall", json={"army_id": ids["alice_army"]}, headers=alice)
    assert recalled.status_code == 200, recalled.text
    assert recalled.json()["mission"] == "return"
    # Halfway back along a 50-tile road is 25 tiles, another 15000 seconds at speed 6.
    _advance(client, 15_000)
    assert _tick(client)["processed"] == 1

    events = client.get("/v1/admin/events", headers=ADMIN).json()["events"]
    outbound = next(event for event in events if event["id"] == outbound_id)
    assert outbound["status"] == "cancelled"
    assert client.get("/v1/me/reports", headers=alice).json()["reports"] == []
    army = client.get("/v1/me/armies", headers=alice).json()["armies"][0]
    assert army["status"] == ArmyStatus.GARRISONED
    assert army["location_city_id"] == ids["alice_city"]


def test_immediate_recall_stays_home(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=10)
    alice = _login(client, "Alice")
    client.post(
        "/v1/commands/attack",
        json={"army_id": ids["alice_army"], "target_city_id": ids["bob_city"]},
        headers=alice,
    )
    recalled = client.post("/v1/commands/recall", json={"army_id": ids["alice_army"]}, headers=alice)
    assert recalled.status_code == 200, recalled.text
    body = recalled.json()
    assert body["depart_at"] == body["arrive_at"]
    assert body["status"] == "completed"
    army = client.get("/v1/me/armies", headers=alice).json()["armies"][0]
    assert army["status"] == "garrisoned"
    assert army["location_city_id"] == ids["alice_city"]
    assert _tick(client)["processed"] == 0
    assert client.get("/v1/me/reports", headers=alice).json()["reports"] == []


def test_build_and_research_complete_once(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=10)
    alice = _login(client, "Alice")
    build = client.post(
        "/v1/commands/build",
        json={"city_id": ids["alice_city"], "building": "barracks"},
        headers=alice,
    )
    assert build.status_code == 200, build.text
    research = client.post("/v1/commands/research", json={"tech": "logistics"}, headers=alice)
    assert research.status_code == 200, research.text

    _advance(client, 3600)
    assert _tick(client)["processed"] == 2
    city = client.get(f"/v1/me/cities/{ids['alice_city']}", headers=alice).json()
    me = client.get("/v1/me", headers=alice).json()
    assert city["buildings"] == {"barracks": 1}
    assert me["research"] == {"logistics": 1}

    assert _tick(client)["processed"] == 0
    for event_id in (build.json()["event_id"], research.json()["event_id"]):
        rerun = client.post(f"/v1/admin/events/{event_id}/run", headers=ADMIN)
        assert rerun.json()["status"] == "completed"
    city = client.get(f"/v1/me/cities/{ids['alice_city']}", headers=alice).json()
    me = client.get("/v1/me", headers=alice).json()
    assert city["buildings"]["barracks"] == 1
    assert me["research"]["logistics"] == 1


def test_live_server_shows_the_march_as_soon_as_move_returns(db, frozen) -> None:
    """A follow-up request on the same connection must see the committed march.

    Uvicorn can start the next request as soon as the response bytes are
    written. Committing after that write made reinforce flaky: catch-up ticked
    the old clock, processed nothing, and the army was still at home.
    """

    import socket
    import threading

    import httpx
    import uvicorn

    from simcore.db import get_sessionmaker
    from simcore.main import create_app
    from simcore.models import City

    ids = create_scenario(frozen.now(), rate=0, stock=100)
    session = get_sessionmaker()()
    try:
        camp = City(
            player_id=ids["alice_id"],
            name="Near Camp",
            x=6,
            y=0,
            wood=0,
            food=0,
            iron=0,
            gold=0,
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

    app = create_app(base_clock=frozen)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, name="move-visibility", daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as live:
            ready = 0
            for _ in range(50):
                try:
                    ready_response = live.get("/health/ready")
                except httpx.HTTPError:
                    time.sleep(0.05)
                    continue
                ready = ready_response.status_code
                if ready == 200:
                    break
                time.sleep(0.05)
            assert ready == 200
            logged = live.post("/v1/auth/dev-login", json={"name": "Alice"})
            assert logged.status_code == 200, logged.text
            alice = {"Authorization": f"Bearer {logged.json()['token']}"}

            for _ in range(40):
                advanced = live.post("/v1/admin/clock/advance", json={"seconds": 1}, headers=ADMIN)
                assert advanced.status_code == 200, advanced.text
                seen = live.get("/v1/time", headers=alice)
                assert seen.status_code == 200, seen.text
                assert seen.json()["offset_seconds"] == advanced.json()["offset_seconds"]

            for _ in range(4):
                moved = live.post(
                    "/v1/commands/move",
                    json={"army_id": ids["alice_army"], "destination_city_id": camp_id, "relocate": False},
                    headers=alice,
                )
                assert moved.status_code == 200, moved.text
                event_id = int(moved.json()["event_id"])
                marching = live.get("/v1/me/armies", headers=alice)
                assert marching.status_code == 200, marching.text
                row = marching.json()["armies"][0]
                assert row["status"] == "marching", row
                assert row["location_city_id"] is None
                detail = live.get(f"/v1/admin/events/{event_id}", headers=ADMIN)
                assert detail.status_code == 200, detail.text
                assert detail.json()["event"]["status"] == "pending"

                clock = live.get("/v1/time", headers=alice).json()
                arrive = datetime.fromisoformat(str(moved.json()["arrive_at"]).replace("Z", "+00:00"))
                now = datetime.fromisoformat(str(clock["server_time"]).replace("Z", "+00:00"))
                seconds = max(1, math.ceil((arrive - now).total_seconds()))
                advanced = live.post("/v1/admin/clock/advance", json={"seconds": seconds}, headers=ADMIN)
                assert advanced.status_code == 200, advanced.text
                ticked = live.post("/v1/admin/worker/tick", headers=ADMIN)
                assert ticked.status_code == 200, ticked.text
                assert int(ticked.json()["processed"]) >= 1, ticked.text
                garrisoned = live.get("/v1/me/armies", headers=alice).json()["armies"][0]
                assert garrisoned["status"] == "garrisoned", garrisoned
                assert garrisoned["location_city_id"] == camp_id
                assert garrisoned["home_city_id"] == ids["alice_city"]
                completed = live.get(f"/v1/admin/events/{event_id}", headers=ADMIN)
                assert completed.json()["event"]["status"] == "completed"

                recalled = live.post(
                    "/v1/commands/recall",
                    json={"army_id": ids["alice_army"]},
                    headers=alice,
                )
                assert recalled.status_code == 200, recalled.text
                home_event = int(recalled.json()["event_id"])
                leaving = live.get("/v1/me/armies", headers=alice).json()["armies"][0]
                assert leaving["status"] == "returning", leaving
                clock = live.get("/v1/time", headers=alice).json()
                arrive = datetime.fromisoformat(str(recalled.json()["arrive_at"]).replace("Z", "+00:00"))
                now = datetime.fromisoformat(str(clock["server_time"]).replace("Z", "+00:00"))
                seconds = max(1, math.ceil((arrive - now).total_seconds()))
                advanced = live.post("/v1/admin/clock/advance", json={"seconds": seconds}, headers=ADMIN)
                assert advanced.status_code == 200, advanced.text
                ticked = live.post("/v1/admin/worker/tick", headers=ADMIN)
                assert int(ticked.json()["processed"]) >= 1, ticked.text
                home = live.get("/v1/me/armies", headers=alice).json()["armies"][0]
                assert home["status"] == "garrisoned", home
                assert home["location_city_id"] == ids["alice_city"]
                assert live.get(f"/v1/admin/events/{home_event}", headers=ADMIN).json()["event"]["status"] == "completed"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_command_permissions(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=10)
    alice = _login(client, "Alice")
    own = client.post(
        "/v1/commands/attack",
        json={"army_id": ids["alice_army"], "target_city_id": ids["alice_city"]},
        headers=alice,
    )
    assert own.status_code == 400
    stolen = client.post(
        "/v1/commands/attack",
        json={"army_id": ids["bob_army"], "target_city_id": ids["alice_city"]},
        headers=alice,
    )
    assert stolen.status_code == 403
    missing = client.post("/v1/commands/move", json={"army_id": 999, "destination_city_id": ids["bob_city"]}, headers=alice)
    assert missing.status_code == 404
    home = client.post("/v1/commands/recall", json={"army_id": ids["alice_army"]}, headers=alice)
    assert home.status_code == 409
