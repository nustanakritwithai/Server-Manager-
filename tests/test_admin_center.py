"""Admin control center: read models, auth, and the snapshot confirm contract."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import func, select

from simcore.db import get_sessionmaker
from simcore.models import Transaction, WorldState
from simcore.snapshot import world_checksum
from tests.conftest import ADMIN
from tests.world import create_scenario

ROOT = Path(__file__).resolve().parents[1]
RATE = 360
STOCK = 1000
OUTBOUND = 30_000

READ_PATHS = (
    "/v1/admin/dashboard",
    "/v1/admin/events",
    "/v1/admin/events/1",
    "/v1/admin/players",
    "/v1/admin/players/1",
    "/v1/admin/cities",
    "/v1/admin/cities/1",
    "/v1/admin/armies",
    "/v1/admin/armies/1",
    "/v1/admin/movements",
    "/v1/admin/movements/1",
    "/v1/admin/reports",
    "/v1/admin/reports/1",
    "/v1/admin/transactions",
    "/v1/admin/snapshots",
)


def _login(client, name: str) -> dict[str, str]:
    response = client.post("/v1/auth/dev-login", json={"name": name})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _fingerprint() -> tuple[int, int, int, str]:
    session = get_sessionmaker()()
    try:
        state = session.get(WorldState, 1)
        assert state is not None
        transactions = session.scalar(select(func.count()).select_from(Transaction))
        return (int(state.world_version), int(state.offset_seconds), int(transactions or 0), world_checksum(session))
    finally:
        session.close()


def _input_tag(html: str, element_id: str) -> str:
    match = re.search(rf'<input\b[^>]*\bid="{re.escape(element_id)}"[^>]*>', html)
    assert match is not None, element_id
    return match.group(0)


def test_admin_pages_store_the_token_only_when_remembered() -> None:
    js = (ROOT / "web" / "admin" / "admin.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "admin" / "index.html").read_text(encoding="utf-8")
    game = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    remember = _input_tag(html, "remember-token")
    assert 'type="checkbox"' in remember
    assert re.search(r"\bchecked\b", remember) is None
    assert "Remember token on this device" in html
    stay = _input_tag(html, "stay-signed-in")
    assert 'type="checkbox"' in stay
    assert re.search(r"\bchecked\b", stay) is not None
    password = _input_tag(html, "admin-password")
    assert 'type="password"' in password
    assert 'autocomplete="current-password"' in password
    assert "value=" not in password
    username = _input_tag(html, "admin-username")
    assert 'autocomplete="username"' in username
    assert 'value="admin"' in username
    advanced = re.search(r'<details\b[^>]*\bid="token-advanced"[^>]*>', html)
    assert advanced is not None
    assert re.search(r"\bopen\b", advanced.group(0)) is None
    assert 'const STORAGE_SESSION = "simcore.adminSession"' in js
    assert "/v1/admin/login" in js
    assert "/v1/admin/logout" in js

    assert 'const STORAGE_TOKEN = "simcore.adminToken"' in js
    assert "simcore.apiBaseUrl" in js
    assert "simcore.token" not in js
    assert re.search(r'state\.token\s*=\s*"[^"]+"', js) is None
    assert re.search(r"state\.token\s*=\s*'[^']+'", js) is None
    writes = re.findall(r"localStorage\.setItem\(([^)]*)\)", js)
    assert set(writes) == {
        "STORAGE_URL, state.apiBaseUrl",
        "STORAGE_TOKEN, clean",
        "STORAGE_SESSION, sessionValue",
    }
    assert "if (clean) localStorage.setItem(STORAGE_TOKEN, clean);" in js
    assert "if (sessionValue && stay) localStorage.setItem(STORAGE_SESSION, sessionValue);" in js
    assert all("password" not in write.lower() for write in writes)
    assert 'const remember = $("remember-token").checked;' in js
    assert re.findall(r"(?<!function )persistToken\(([^)]*)\)", js) == [
        'remember ? state.token : ""',
        '""',
    ]
    lock = re.search(r'\$\("lock-btn"\)\.addEventListener\("click", \(\) => \{([\s\S]*?)\n  \}\);', js)
    assert lock is not None
    assert 'state.token = ""' in lock.group(1)
    assert '$("remember-token").checked = false' in lock.group(1)
    assert 'persistToken("")' in lock.group(1)
    assert "localStorage.removeItem(STORAGE_TOKEN)" in js

    assert "sessionStorage" not in js
    assert "document.cookie" not in js
    assert "console." not in js
    for line in js.splitlines():
        if re.search(r"state\.token|STORAGE_TOKEN|normalizeAdminToken|X-Admin-Token", line):
            assert "location." not in line
            assert "history." not in line
            assert "URLSearchParams" not in line
            assert "document.cookie" not in line
            assert "console." not in line
            assert "sessionStorage" not in line

    for path in (ROOT / "web").rglob("*"):
        if path.is_file():
            assert "dev-admin" not in path.read_text(encoding="utf-8"), path

    assert js.count("confirm: true") == 1
    assert "X-Admin-Token" in js
    assert 'id="restore-go"' in html
    assert "disabled" in html
    assert 'id="login-form"' in game
    assert 'id="attack-btn"' in game
    assert 'href="admin/"' in game


def test_typed_admin_token_is_normalized_before_use() -> None:
    js = (ROOT / "web" / "admin" / "admin.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "admin" / "index.html").read_text(encoding="utf-8")
    match = re.search(r"function normalizeAdminToken\(value\) \{\n([\s\S]*?)\n  \}", js)
    assert match is not None
    body = match.group(1)
    zero_width = body.find(r"[\u200B-\u200D\uFEFF]")
    nbsp = body.find(r"\u00A0")
    breaks = body.find(r"[\r\n]")
    trim = body.find(".trim()")
    assert zero_width != -1 and "/g" in body[zero_width:zero_width + 40]
    assert nbsp != -1 and "/g" in body[nbsp:nbsp + 24]
    assert breaks != -1 and "/g" in body[breaks:breaks + 20]
    assert trim != -1
    assert max(zero_width, nbsp, breaks) < trim
    assert 'const typed = normalizeAdminToken($("admin-token").value);' in js
    assert "if (typed) state.token = typed;" in js
    assert 'return normalizeAdminToken(localStorage.getItem(STORAGE_TOKEN) || "");' in js
    assert "const clean = normalizeAdminToken(token);" in js
    assert "const token = normalizeAdminToken(state.token);" in js
    assert 'headers.set("X-Admin-Token", token);' in js
    assert 'state.token = $("admin-token").value' not in js
    token_input = _input_tag(html, "admin-token")
    assert 'type="password"' in token_input
    assert "value=" not in token_input
    for attr in (
        'autocomplete="off"',
        'autocapitalize="off"',
        'autocorrect="off"',
        'spellcheck="false"',
        'inputmode="text"',
    ):
        assert attr in token_input


def test_admin_routes_reject_a_missing_or_wrong_token(client) -> None:
    for path in READ_PATHS:
        missing = client.get(path)
        assert missing.status_code == 401, path
        assert missing.json()["error"]["code"] == "unauthorized"
        wrong = client.get(path, headers={"X-Admin-Token": "not-the-token"})
        assert wrong.status_code == 401, path
    for path, payload in (
        ("/v1/admin/clock/advance", {"seconds": 1}),
        ("/v1/admin/worker/tick", None),
        ("/v1/admin/snapshots", {"reason": "MANUAL"}),
        ("/v1/admin/snapshots/1/restore", {"confirm": True}),
    ):
        denied = client.post(path, json=payload)
        assert denied.status_code == 401, path


def test_dashboard_counts_and_event_filter(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    board = client.get("/v1/admin/dashboard", headers=ADMIN)
    assert board.status_code == 200, board.text
    body = board.json()
    assert body["counts"] == {"players": 2, "cities": 2, "armies": 2, "active_movements": 0}
    assert body["events"]["pending"] == 0
    assert body["events"]["processing"] == 0
    assert body["events"]["completed"] == 0
    assert body["events"]["failed"] == 0
    assert body["events"]["cancelled"] == 0
    assert body["latest_snapshot"] is None
    assert body["uninstrumented"]["worker_heartbeat"] == "NOT INSTRUMENTED"
    assert body["uninstrumented"]["host_cpu"] == "NOT INSTRUMENTED"
    assert body["world"]["commands_open"] is True
    assert body["offset_seconds"] == 0

    alice = _login(client, "Alice")
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": ids["alice_army"], "target_city_id": ids["bob_city"]},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    movement_id = attack.json()["movement_id"]

    after = client.get("/v1/admin/dashboard", headers=ADMIN).json()
    assert after["counts"]["active_movements"] == 1
    assert after["events"]["pending"] == 1

    pending = client.get("/v1/admin/events", headers=ADMIN, params={"status": "pending"})
    assert pending.status_code == 200, pending.text
    events = pending.json()["events"]
    assert len(events) == 1
    event = events[0]
    assert event["status"] == "pending"
    assert event["movement_id"] == movement_id
    assert event["idempotency_key"]
    assert "payload" in event
    assert "locked_by" in event
    assert "last_error" in event
    failed = client.get("/v1/admin/events", headers=ADMIN, params={"status": "failed"})
    assert failed.json()["events"] == []

    detail = client.get(f"/v1/admin/events/{event['id']}", headers=ADMIN)
    assert detail.status_code == 200, detail.text
    linked = detail.json()
    assert linked["movement"]["id"] == movement_id
    assert linked["movement"]["army_id"] == ids["alice_army"]
    assert linked["army"]["id"] == ids["alice_army"]
    assert linked["army"]["home_city_id"] == ids["alice_city"]
    assert linked["battle"] is None
    assert linked["transactions"] == []

    missing = client.get("/v1/admin/events/99999", headers=ADMIN)
    assert missing.status_code == 404

    players = client.get("/v1/admin/players", headers=ADMIN).json()["players"]
    alice_row = next(row for row in players if row["name"] == "Alice")
    assert ids["alice_city"] in alice_row["city_ids"]
    assert ids["alice_army"] in alice_row["army_ids"]
    player = client.get(f"/v1/admin/players/{ids['alice_id']}", headers=ADMIN)
    assert player.status_code == 200, player.text
    assert player.json()["cities"][0]["id"] == ids["alice_city"]
    assert player.json()["resource_balances"] == "stored_not_accrued"

    movements = client.get("/v1/admin/movements", headers=ADMIN, params={"status": "in_progress"})
    assert [row["id"] for row in movements.json()["movements"]] == [movement_id]


def test_event_detail_links_battle_and_ledger_after_resolution(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    alice = _login(client, "Alice")
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": ids["alice_army"], "target_city_id": ids["bob_city"]},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    advanced = client.post("/v1/admin/clock/advance", json={"seconds": OUTBOUND}, headers=ADMIN)
    assert advanced.status_code == 200, advanced.text
    tick = client.post("/v1/admin/worker/tick", headers=ADMIN)
    assert tick.status_code == 200, tick.text
    assert tick.json()["processed"] == 1
    event_id = tick.json()["event_ids"][0]

    before = _fingerprint()
    detail = client.get(f"/v1/admin/events/{event_id}", headers=ADMIN)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["event"]["status"] == "completed"
    assert body["battle"]["winner"] == "attacker"
    assert body["battle"]["event_id"] == event_id
    assert body["battle"]["movement_id"] == body["movement"]["id"]
    assert body["battle"]["seed"]
    assert any(row["reason"] == "loot_lost" for row in body["transactions"])

    reports = client.get("/v1/admin/reports", headers=ADMIN)
    assert reports.status_code == 200, reports.text
    assert len(reports.json()["reports"]) == 1
    report_id = reports.json()["reports"][0]["id"]
    report = client.get(f"/v1/admin/reports/{report_id}", headers=ADMIN)
    assert report.status_code == 200, report.text
    assert report.json()["event"]["id"] == event_id
    assert report.json()["movement"]["id"] == body["movement"]["id"]
    assert report.json()["report"]["rounds"]

    ledger = client.get("/v1/admin/transactions", headers=ADMIN, params={"source_event_id": event_id, "limit": 50})
    assert ledger.status_code == 200, ledger.text
    rows = ledger.json()["transactions"]
    assert rows
    assert {row["source_event_id"] for row in rows} == {event_id}
    assert _fingerprint() == before


def test_snapshot_contract_and_restore_requires_confirm(client, frozen) -> None:
    create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    created = client.post("/v1/admin/snapshots", json={"reason": "MANUAL"}, headers=ADMIN)
    assert created.status_code == 200, created.text
    snap = created.json()
    assert snap["status"] == "READY"
    assert snap["reason"] == "MANUAL"
    assert snap["checksum"].startswith("sha256:")
    assert snap["summary"]["players"] == 2
    snap_id = snap["snapshot_id"]

    listed = client.get("/v1/admin/snapshots", headers=ADMIN)
    assert [row["snapshot_id"] for row in listed.json()["snapshots"]] == [snap_id]
    one = client.get(f"/v1/admin/snapshots/{snap_id}", headers=ADMIN)
    assert one.json()["checksum"] == snap["checksum"]
    inspected = client.get(f"/v1/admin/snapshots/{snap_id}/inspect", headers=ADMIN)
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["checksum_ok"] is True
    assert inspected.json()["counts"]["cities"] == 2

    board = client.get("/v1/admin/dashboard", headers=ADMIN).json()
    assert board["latest_snapshot"]["snapshot_id"] == snap_id
    assert board["latest_snapshot"]["checksum"] == snap["checksum"]
    assert board["latest_snapshot"]["status"] == "READY"

    before = _fingerprint()
    unconfirmed = client.post(f"/v1/admin/snapshots/{snap_id}/restore", json={"confirm": False}, headers=ADMIN)
    assert unconfirmed.status_code == 400, unconfirmed.text
    omitted = client.post(f"/v1/admin/snapshots/{snap_id}/restore", json={}, headers=ADMIN)
    assert omitted.status_code == 400, omitted.text
    assert _fingerprint() == before

    restored = client.post(f"/v1/admin/snapshots/{snap_id}/restore", json={"confirm": True}, headers=ADMIN)
    assert restored.status_code == 200, restored.text
    assert restored.json()["match"] is True
    assert restored.json()["restored_checksum"] == snap["checksum"]
    assert restored.json()["safety_snapshot_id"] != snap_id
    assert restored.json()["commands_open"] is True


def test_inspector_reads_do_not_mutate_stored_resources(client, frozen) -> None:
    create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    frozen.advance(hours=2)
    before = _fingerprint()
    cities = client.get("/v1/admin/cities", headers=ADMIN)
    assert cities.status_code == 200, cities.text
    assert cities.json()["resource_balances"] == "stored_not_accrued"
    oak = next(city for city in cities.json()["cities"] if city["name"] == "Oakhold")
    assert oak["wood"] == STOCK
    for path in (
        "/v1/admin/dashboard",
        "/v1/admin/events",
        "/v1/admin/players",
        "/v1/admin/armies",
        "/v1/admin/movements",
        "/v1/admin/reports",
        "/v1/admin/transactions",
        f"/v1/admin/cities/{oak['id']}",
        f"/v1/admin/players/{oak['player_id']}",
    ):
        response = client.get(path, headers=ADMIN)
        assert response.status_code == 200, response.text
    assert _fingerprint() == before

    alice = _login(client, "Alice")
    accrued = client.get("/v1/me/cities", headers=alice)
    assert accrued.status_code == 200, accrued.text
    assert accrued.json()["cities"][0]["wood"] == STOCK + RATE * 2


def test_game_client_endpoints_still_work(client, frozen) -> None:
    ids = create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    client.get("/v1/admin/dashboard", headers=ADMIN)
    alice = _login(client, "Alice")
    me = client.get("/v1/me", headers=alice)
    assert me.status_code == 200, me.text
    assert me.json()["name"] == "Alice"
    cities = client.get("/v1/me/cities", headers=alice)
    assert cities.status_code == 200, cities.text
    armies = client.get("/v1/me/armies", headers=alice)
    assert armies.status_code == 200, armies.text
    world = client.get("/v1/map/cities", headers=alice)
    assert world.status_code == 200, world.text
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": ids["alice_army"], "target_city_id": ids["bob_city"]},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    assert attack.json()["mission"] == "attack"
    assert attack.json()["status"] == "in_progress"
