"""God-view world map: auth, server interpolation, and the static Map tab."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from simcore.constants import ArmyStatus
from simcore.db import get_sessionmaker
from simcore.game.travel import interpolate, travel_progress
from simcore.models import Army, City, Player
from simcore.snapshot import world_checksum
from tests.conftest import ADMIN
from tests.world import create_scenario

ROOT = Path(__file__).resolve().parents[1]
OUTBOUND = 30_000


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


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


def _map(client, **params):
    response = client.get("/v1/admin/world-map", headers=ADMIN, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(rows: list[dict], row_id: int) -> dict:
    return next(row for row in rows if row["id"] == row_id)


def _movement_for(body: dict, army_id: int) -> dict:
    rows = [row for row in body["movements"] if row["army_id"] == army_id]
    assert len(rows) == 1
    return rows[0]


def _checksum() -> str:
    session = get_sessionmaker()()
    try:
        return world_checksum(session)
    finally:
        session.close()


def test_world_map_requires_admin_auth(client) -> None:
    missing = client.get("/v1/admin/world-map")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    wrong = client.get("/v1/admin/world-map", headers={"X-Admin-Token": "not-the-token"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthorized"
    allowed = client.get("/v1/admin/world-map", headers=ADMIN)
    assert allowed.status_code == 200, allowed.text


def test_empty_world_map_has_no_invented_geometry(client, frozen) -> None:
    before = _checksum()
    body = _map(client)
    assert _checksum() == before
    assert _parse(body["server_time"]) == frozen.now()
    assert body["read_only"] is True
    assert body["fog_of_war"] is False
    assert body["coordinate_source"] == "stored"
    assert body["neutral_entities"] == "NONE"
    assert body["filter"] == {"player_id": None}
    assert body["bounds"] is None
    assert body["players"] == []
    assert body["cities"] == []
    assert body["armies"] == []
    assert body["movements"] == []
    assert body["limits"]["cities"]["total"] == 0
    assert body["limits"]["cities"]["truncated"] is False
    assert "No map seed" in body["notes"]
    missing = client.get("/v1/admin/world-map", headers=ADMIN, params={"player_id": 404})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_garrisoned_positions_are_city_coordinates_for_every_player(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=100)
    session = get_sessionmaker()()
    try:
        cara = Player(name="Cara", research={}, created_at=frozen.now())
        session.add(cara)
        session.flush()
        camp = City(
            player_id=cara.id,
            name="High Camp",
            x=5,
            y=80,
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
        session.flush()
        watch = Army(
            player_id=cara.id,
            name="Ridge Watch",
            home_city_id=camp.id,
            location_city_id=camp.id,
            status=ArmyStatus.GARRISONED,
            units=[{"type": "archer", "count": 4}],
            created_at=frozen.now(),
        )
        wreck = Army(
            player_id=cara.id,
            name="Lost Column",
            home_city_id=camp.id,
            location_city_id=None,
            status=ArmyStatus.DESTROYED,
            units=[],
            created_at=frozen.now(),
        )
        session.add_all([watch, wreck])
        session.commit()
        cara_id = cara.id
        camp_id = camp.id
        watch_id = watch.id
        wreck_id = wreck.id
    finally:
        session.close()

    body = _map(client)
    assert {player["name"] for player in body["players"]} == {"Alice", "Bob", "Cara"}
    assert {city["name"] for city in body["cities"]} == {"Oakhold", "Ironford", "High Camp"}
    assert {army["id"] for army in body["armies"]} >= {ids["alice_army"], ids["bob_army"], watch_id, wreck_id}
    assert body["movements"] == []
    assert body["bounds"] == {"min_x": 0, "min_y": 0, "max_x": 30, "max_y": 80}

    oak = _by_id(body["cities"], ids["alice_city"])
    alice = _by_id(body["armies"], ids["alice_army"])
    assert alice["status"] == "garrisoned"
    assert alice["position"] == {"city_id": ids["alice_city"], "x": 0, "y": 0}
    assert alice["position_state"] == "garrisoned"
    assert alice["player_id"] == ids["alice_id"]
    assert alice["trace_state"] == "NONE"
    assert ids["alice_army"] in oak["garrison_army_ids"]

    bob = _by_id(body["armies"], ids["bob_army"])
    assert bob["position"] == {"city_id": ids["bob_city"], "x": 30, "y": 40}
    assert bob["player_name"] == "Bob"

    cara_army = _by_id(body["armies"], watch_id)
    assert cara_army["player_id"] == cara_id
    assert cara_army["position"] == {"city_id": camp_id, "x": 5, "y": 80}

    lost = _by_id(body["armies"], wreck_id)
    assert lost["position"] is None
    assert lost["position_state"] == "UNKNOWN"
    assert lost["status"] == "destroyed"
    assert all(army["position"] != {"x": 0, "y": 0, "city_id": None} for army in body["armies"])

    only_bob = _map(client, player_id=ids["bob_id"])
    assert only_bob["filter"] == {"player_id": ids["bob_id"]}
    assert {city["id"] for city in only_bob["cities"]} == {ids["bob_city"]}
    assert {army["id"] for army in only_bob["armies"]} == {ids["bob_army"]}
    assert only_bob["movements"] == []
    assert {player["name"] for player in only_bob["players"]} == {"Alice", "Bob", "Cara"}


def test_outbound_positions_match_interpolation_at_start_midpoint_and_arrival(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=100)
    alice = _login(client, "Alice")
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": ids["alice_army"], "target_city_id": ids["bob_city"]},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    march = attack.json()
    assert march["trace_id"]
    depart = _parse(march["depart_at"])
    arrive = _parse(march["arrive_at"])
    assert (arrive - depart).total_seconds() == OUTBOUND

    def expect(now: datetime) -> tuple[float, float, float]:
        progress = travel_progress(depart, arrive, now)
        x, y = interpolate(0, 0, 30, 40, progress)
        return round(x, 4), round(y, 4), round(progress, 6)

    start = _map(client)
    assert _parse(start["server_time"]) == frozen.now()
    army = _by_id(start["armies"], ids["alice_army"])
    movement = _movement_for(start, ids["alice_army"])
    sx, sy, sp = expect(frozen.now())
    assert (sx, sy, sp) == (0, 0, 0)
    assert army["status"] == "marching"
    assert army["position"] == {"city_id": None, "x": sx, "y": sy}
    assert army["position_state"] == "interpolated"
    assert army["trace_id"] == march["trace_id"]
    assert army["trace_state"] == "AVAILABLE"
    assert movement["mission"] == "attack"
    assert movement["status"] == "in_progress"
    assert movement["army_status"] == "marching"
    assert movement["position"] == {"x": sx, "y": sy}
    assert movement["progress"] == sp
    assert movement["progress_percent"] == 0
    assert movement["eta_seconds"] == OUTBOUND
    assert movement["direction"] == {"dx": 30, "dy": 40}
    assert movement["origin"] == {"city_id": ids["alice_city"], "x": 0, "y": 0}
    assert movement["destination"] == {"city_id": ids["bob_city"], "x": 30, "y": 40}
    assert movement["trace_id"] == march["trace_id"]
    assert movement["event_id"] == march["event_id"]
    bob = _by_id(start["armies"], ids["bob_army"])
    assert bob["position"] == {"city_id": ids["bob_city"], "x": 30, "y": 40}
    assert {army["player_id"] for army in start["armies"]} == {ids["alice_id"], ids["bob_id"]}

    _advance(client, OUTBOUND // 2)
    mid_now = frozen.now() + timedelta(seconds=OUTBOUND // 2)
    mid = _map(client)
    assert _parse(mid["server_time"]) == mid_now
    mx, my, mp = expect(mid_now)
    assert (mx, my, mp) == (15, 20, 0.5)
    mid_move = _movement_for(mid, ids["alice_army"])
    mid_army = _by_id(mid["armies"], ids["alice_army"])
    assert mid_move["position"] == {"x": mx, "y": my}
    assert mid_army["position"] == {"city_id": None, "x": mx, "y": my}
    assert mid_move["progress"] == mp
    assert mid_move["progress_percent"] == 50
    assert mid_move["eta_seconds"] == OUTBOUND // 2
    assert _by_id(mid["armies"], ids["bob_army"])["status"] == "garrisoned"

    _advance(client, OUTBOUND // 2)
    end_now = frozen.now() + timedelta(seconds=OUTBOUND)
    end = _map(client)
    assert _parse(end["server_time"]) == end_now
    ex, ey, ep = expect(end_now)
    assert (ex, ey, ep) == (30, 40, 1)
    end_move = _movement_for(end, ids["alice_army"])
    assert end_move["status"] == "in_progress"
    assert end_move["position"] == {"x": ex, "y": ey}
    assert end_move["progress"] == ep
    assert end_move["progress_percent"] == 100
    assert end_move["eta_seconds"] == 0
    assert _by_id(end["armies"], ids["alice_army"])["status"] == "marching"

    assert _tick(client)["processed"] == 1
    returning = _map(client)
    assert _parse(returning["server_time"]) == end_now
    home = _movement_for(returning, ids["alice_army"])
    alice_army = _by_id(returning["armies"], ids["alice_army"])
    assert home["mission"] == "return"
    assert home["status"] == "in_progress"
    assert alice_army["status"] == "returning"
    assert home["origin"] == {"city_id": ids["bob_city"], "x": 30, "y": 40}
    assert home["destination"] == {"city_id": ids["alice_city"], "x": 0, "y": 0}
    assert home["progress"] == 0
    assert home["position"] == {"x": 30, "y": 40}
    assert alice_army["position"] == {"city_id": None, "x": 30, "y": 40}
    assert home["direction"] == {"dx": -30, "dy": -40}
    assert home["trace_id"] == march["trace_id"]
    bob_after = _by_id(returning["armies"], ids["bob_army"])
    assert bob_after["status"] == "destroyed"
    assert bob_after["position"] is None
    assert bob_after["position_state"] == "UNKNOWN"
    assert bob_after["player_id"] == ids["bob_id"]

    _advance(client, OUTBOUND // 2)
    half_home = frozen.now() + timedelta(seconds=OUTBOUND + OUTBOUND // 2)
    walked = _map(client)
    assert _parse(walked["server_time"]) == half_home
    back = _movement_for(walked, ids["alice_army"])
    progress = travel_progress(_parse(back["depart_at"]), _parse(back["arrive_at"]), half_home)
    x, y = interpolate(30, 40, 0, 0, progress)
    assert round(progress, 6) == 0.5
    assert back["position"] == {"x": round(x, 4), "y": round(y, 4)}
    assert back["position"] == {"x": 15, "y": 20}
    assert _by_id(walked["armies"], ids["alice_army"])["position"]["x"] == 15
    assert _by_id(walked["armies"], ids["alice_army"])["position"]["y"] == 20
    assert back["mission"] == "return"
    assert _by_id(walked["armies"], ids["bob_army"])["position"] is None


def test_recall_return_starts_at_the_interpolated_point(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=100)
    alice = _login(client, "Alice")
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": ids["alice_army"], "target_city_id": ids["bob_city"]},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    _advance(client, OUTBOUND // 2)
    recalled = client.post("/v1/commands/recall", json={"army_id": ids["alice_army"]}, headers=alice)
    assert recalled.status_code == 200, recalled.text
    turned = recalled.json()
    assert turned["mission"] == "return"
    assert turned["origin"]["x"] == 15
    assert turned["origin"]["y"] == 20

    now = frozen.now() + timedelta(seconds=OUTBOUND // 2)
    body = _map(client)
    movement = _movement_for(body, ids["alice_army"])
    army = _by_id(body["armies"], ids["alice_army"])
    assert movement["status"] == "in_progress"
    assert army["status"] == "returning"
    assert movement["progress"] == 0
    assert movement["origin"] == {"city_id": None, "x": 15, "y": 20}
    assert movement["destination"] == {"city_id": ids["alice_city"], "x": 0, "y": 0}
    assert movement["position"] == {"x": 15, "y": 20}
    assert army["position"] == {"city_id": None, "x": 15, "y": 20}
    assert _by_id(body["armies"], ids["bob_army"])["position"] == {"city_id": ids["bob_city"], "x": 30, "y": 40}

    depart = _parse(movement["depart_at"])
    arrive = _parse(movement["arrive_at"])
    assert (arrive - depart).total_seconds() == 15_000
    _advance(client, 7_500)
    later = now + timedelta(seconds=7_500)
    walked = _map(client)
    back = _movement_for(walked, ids["alice_army"])
    progress = travel_progress(depart, arrive, later)
    x, y = interpolate(15, 20, 0, 0, progress)
    assert _parse(walked["server_time"]) == later
    assert round(progress, 6) == 0.5
    assert back["position"] == {"x": round(x, 4), "y": round(y, 4)}
    assert back["position"] == {"x": 7.5, "y": 10}
    assert _by_id(walked["armies"], ids["alice_army"])["status"] == "returning"


def test_world_map_limits_report_truncation(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=0, stock=10)
    alice = _login(client, "Alice")
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": ids["alice_army"], "target_city_id": ids["bob_city"]},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    body = _map(client, city_limit=1, army_limit=1, movement_limit=1)
    assert body["limits"]["cities"] == {"limit": 1, "returned": 1, "total": 2, "truncated": True}
    assert body["limits"]["armies"]["truncated"] is True
    assert body["limits"]["armies"]["total"] == 2
    assert len(body["cities"]) == 1
    assert len(body["armies"]) == 1
    assert body["limits"]["movements"]["total"] == 1
    assert body["limits"]["movements"]["truncated"] is False
    rejected = client.get("/v1/admin/world-map", headers=ADMIN, params={"city_limit": 0})
    assert rejected.status_code == 422


def test_map_tab_is_read_only_and_draws_server_data() -> None:
    html = (ROOT / "web" / "admin" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "admin" / "admin.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "admin" / "map.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "admin" / "map.css").read_text(encoding="utf-8")

    assert 'data-nav="map"' in html
    assert 'data-view="map"' in html
    assert 'id="map-root"' in html
    assert 'href="map.css"' in html
    assert 'src="map.js"' in html
    assert html.index('src="map.js"') < html.index('src="admin.js"')
    section = re.search(r'<section\b[^>]*data-view="map"[^>]*>[\s\S]*?</section>', html)
    assert section is not None
    assert "POST" not in section.group(0)
    assert "command" in section.group(0).lower()

    assert "map: true" in js
    assert "SimcoreWorldMap" in js
    assert 'parsed.view === "map"' in js

    assert "/v1/admin/world-map" in page
    assert "EMPTY" in page
    assert "UNKNOWN" in page
    assert "applyPinch" in page
    assert "REFRESH_MS = 4000" in page
    assert "Pause auto-refresh" in page
    assert "source.x" in page and "source.y" in page
    assert "Math.random" not in page
    assert "localStorage" not in page
    assert "sessionStorage" not in page
    assert "document.cookie" not in page
    assert "innerHTML" not in page
    assert "fetch(" not in page
    assert "XMLHttpRequest" not in page
    for banned in ("unpkg", "jsdelivr", "cdnjs", "googleapis", "/v1/commands", "/v1/admin/clock", "/v1/admin/worker", "/v1/admin/snapshots"):
        assert banned not in page
        assert banned not in css
    assert "touch-action: none" in css
    assert page.count('method: "POST"') == 0
    assert page.count("method: 'POST'") == 0
