"""World snapshots: hasher, verify-before-restore, and the hash-equality proof."""

from __future__ import annotations

import copy
from datetime import datetime

from sqlalchemy import func, select

from simcore.constants import EventStatus
from simcore.db import get_sessionmaker
from simcore.models import (
    Army,
    BattleReport,
    City,
    Event,
    Movement,
    Player,
    Transaction,
    WorldSnapshot,
    WorldSnapshotPayload,
    WorldState,
)
from simcore.snapshot import canonical_json, capture_document, checksum_text, world_checksum
from simcore.worker import run_once
from tests.conftest import ADMIN
from tests.world import create_scenario

RATE = 360
STOCK = 1000
OUTBOUND = 30_000
HOMEWARD = 30_000


def _login(client, name: str) -> dict[str, str]:
    response = client.post("/v1/auth/dev-login", json={"name": name})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _open():
    session = get_sessionmaker()()
    return session


def _facts() -> dict[str, object]:
    session = _open()
    try:
        state = session.get(WorldState, 1)
        assert state is not None
        armies = {
            army.name: {"id": army.id, "status": army.status, "units": army.units}
            for army in session.scalars(select(Army).order_by(Army.id))
        }
        cities = {
            city.name: {
                "id": city.id,
                "wood": city.wood,
                "food": city.food,
                "iron": city.iron,
                "gold": city.gold,
            }
            for city in session.scalars(select(City).order_by(City.id))
        }
        counts = {
            "players": session.scalar(select(func.count()).select_from(Player)),
            "cities": session.scalar(select(func.count()).select_from(City)),
            "armies": session.scalar(select(func.count()).select_from(Army)),
            "movements": session.scalar(select(func.count()).select_from(Movement)),
            "events": session.scalar(select(func.count()).select_from(Event)),
            "battle_reports": session.scalar(select(func.count()).select_from(BattleReport)),
            "transactions": session.scalar(select(func.count()).select_from(Transaction)),
        }
        return {
            "checksum": world_checksum(session),
            "offset": state.offset_seconds,
            "world_version": int(state.world_version),
            "commands_open": state.commands_open,
            "worker_paused": state.worker_paused,
            "players": sorted(player.name for player in session.scalars(select(Player))),
            "armies": armies,
            "cities": cities,
            "counts": counts,
        }
    finally:
        session.close()


def _counts() -> dict[str, int]:
    facts = _facts()
    counts = facts["counts"]
    assert isinstance(counts, dict)
    return counts


def test_canonical_checksum_ignores_key_order_and_covers_each_section() -> None:
    left = {"b": 1, "a": [{"z": 1, "y": [2, 1]}]}
    right = {"a": [{"y": [2, 1], "z": 1}], "b": 1}
    assert canonical_json(left) == canonical_json(right)
    assert checksum_text(canonical_json(left)) == checksum_text(canonical_json(right))
    assert checksum_text(canonical_json(left)).startswith("sha256:")

    base = {
        "schema_version": 1,
        "world_state": {"id": 1, "offset_seconds": 0, "world_version": 0},
        "players": [],
        "cities": [],
        "armies": [],
        "movements": [],
        "events": [],
        "battle_reports": [],
        "transactions": [],
    }
    original = checksum_text(canonical_json(base))
    samples = {
        "world_state": {"id": 1, "offset_seconds": 1, "world_version": 0},
        "players": [{"id": 1, "name": "Ada"}],
        "cities": [{"id": 1, "wood": 1}],
        "armies": [{"id": 1, "units": [{"type": "infantry", "count": 1}]}],
        "movements": [{"id": 1, "status": "in_progress"}],
        "events": [{"id": 1, "status": "pending"}],
        "battle_reports": [{"id": 1, "winner": "attacker"}],
        "transactions": [{"id": 1, "delta": -5}],
    }
    for key, value in samples.items():
        mutated = copy.deepcopy(base)
        mutated[key] = value
        assert checksum_text(canonical_json(mutated)) != original


def test_snapshot_api_validation(client, frozen) -> None:
    create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    refused = client.post("/v1/admin/snapshots", json={"reason": "SAFETY"}, headers=ADMIN)
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "invalid_command"
    unknown = client.post("/v1/admin/snapshots", json={"reason": "nope"}, headers=ADMIN)
    assert unknown.status_code == 400, unknown.text

    created = client.post("/v1/admin/snapshots", json={}, headers=ADMIN)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["reason"] == "MANUAL"
    assert body["status"] == "READY"
    assert body["schema_version"] == 1
    assert body["summary"]["players"] == 2
    assert body["summary"]["battle_reports"] == 0
    snap_id = body["snapshot_id"]

    listed = client.get("/v1/admin/snapshots", headers=ADMIN)
    assert listed.status_code == 200, listed.text
    assert [row["snapshot_id"] for row in listed.json()["snapshots"]] == [snap_id]

    one = client.get(f"/v1/admin/snapshots/{snap_id}", headers=ADMIN)
    assert one.status_code == 200, one.text
    assert one.json()["summary"]["cities"] == 2

    inspected = client.get(f"/v1/admin/snapshots/{snap_id}/inspect", headers=ADMIN)
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["checksum_ok"] is True
    assert inspected.json()["canonical_ok"] is True
    assert inspected.json()["summary_ok"] is True
    assert inspected.json()["counts"] == body["summary"]

    missing = client.get("/v1/admin/snapshots/9999", headers=ADMIN)
    assert missing.status_code == 404

    unconfirmed = client.post(
        f"/v1/admin/snapshots/{snap_id}/restore",
        json={"confirm": False},
        headers=ADMIN,
    )
    assert unconfirmed.status_code == 400, unconfirmed.text
    assert _counts()["players"] == 2
    assert client.get("/v1/admin/snapshots", headers=ADMIN).json()["snapshots"][0]["reason"] == "MANUAL"


def test_snapshot_mutate_restore_hash_equality(client, frozen) -> None:
    """World A → snapshot → march, battle, losses, ledger → restore → same hash."""

    create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    before = _facts()
    assert before["counts"]["movements"] == 0
    assert before["counts"]["battle_reports"] == 0
    assert before["counts"]["transactions"] == 0
    assert before["offset"] == 0

    created = client.post("/v1/admin/snapshots", json={"reason": "MANUAL"}, headers=ADMIN)
    assert created.status_code == 200, created.text
    snap = created.json()
    snap_id = snap["snapshot_id"]
    assert snap["checksum"] == before["checksum"]
    assert snap["world_version"] == before["world_version"]
    assert _facts()["checksum"] == before["checksum"]
    world_time = datetime.fromisoformat(snap["world_time"].replace("Z", "+00:00"))
    assert world_time == frozen.now()

    alice = _login(client, "Alice")
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": before["armies"]["Oak Company"]["id"], "target_city_id": before["cities"]["Ironford"]["id"]},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    advanced = client.post(
        "/v1/admin/clock/advance",
        json={"seconds": OUTBOUND + HOMEWARD},
        headers=ADMIN,
    )
    assert advanced.status_code == 200, advanced.text
    tick = client.post("/v1/admin/worker/tick", headers=ADMIN)
    assert tick.status_code == 200, tick.text
    assert tick.json()["processed"] == 2
    assert tick.json()["paused"] is False

    mutated = _facts()
    assert mutated["checksum"] != before["checksum"]
    assert mutated["offset"] == OUTBOUND + HOMEWARD
    assert mutated["world_version"] > before["world_version"]
    assert mutated["counts"]["movements"] >= 1
    assert mutated["counts"]["events"] >= 2
    assert mutated["counts"]["battle_reports"] == 1
    assert mutated["counts"]["transactions"] > 0
    assert mutated["armies"]["Iron Watch"]["status"] == "destroyed"
    assert mutated["armies"]["Iron Watch"]["units"] == []
    assert mutated["cities"]["Oakhold"]["gold"] != before["cities"]["Oakhold"]["gold"]
    assert mutated["cities"]["Ironford"]["gold"] != before["cities"]["Ironford"]["gold"]

    restored = client.post(
        f"/v1/admin/snapshots/{snap_id}/restore",
        json={"confirm": True},
        headers=ADMIN,
    )
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["match"] is True
    assert body["restored_checksum"] == before["checksum"]
    assert body["checksum"] == before["checksum"]
    assert body["commands_open"] is True
    assert body["worker_paused"] is False
    assert body["safety_snapshot_id"] != snap_id

    after = _facts()
    assert after["checksum"] == before["checksum"]
    assert after["offset"] == before["offset"]
    assert after["world_version"] == before["world_version"]
    assert after["commands_open"] is True
    assert after["worker_paused"] is False
    assert after["players"] == before["players"]
    assert after["counts"] == before["counts"]
    assert after["armies"] == before["armies"]
    assert after["cities"] == before["cities"]

    listed = client.get("/v1/admin/snapshots", headers=ADMIN).json()["snapshots"]
    by_id = {row["snapshot_id"]: row for row in listed}
    assert by_id[snap_id]["status"] == "READY"
    safety = by_id[body["safety_snapshot_id"]]
    assert safety["reason"] == "SAFETY"
    assert safety["status"] == "READY"
    assert safety["checksum"] == mutated["checksum"]

    again = client.post(
        "/v1/commands/attack",
        json={"army_id": before["armies"]["Oak Company"]["id"], "target_city_id": before["cities"]["Ironford"]["id"]},
        headers=alice,
    )
    assert again.status_code == 200, again.text


def test_restore_rejects_tampered_payload_and_leaves_the_world(client, frozen) -> None:
    create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    created = client.post("/v1/admin/snapshots", json={"reason": "MANUAL"}, headers=ADMIN)
    snap_id = created.json()["snapshot_id"]
    alice = _login(client, "Alice")
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": 1, "target_city_id": 2},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    mutated = _facts()

    session = _open()
    try:
        payload = session.get(WorldSnapshotPayload, snap_id)
        assert payload is not None
        payload.body = payload.body + " "
        session.commit()
    finally:
        session.close()

    refused = client.post(
        f"/v1/admin/snapshots/{snap_id}/restore",
        json={"confirm": True},
        headers=ADMIN,
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "snapshot_checksum"
    assert "not left half-applied" in refused.json()["error"]["message"]

    after = _facts()
    assert after["checksum"] == mutated["checksum"]
    assert after["counts"] == mutated["counts"]
    assert after["commands_open"] is True
    assert after["worker_paused"] is False

    listed = client.get("/v1/admin/snapshots", headers=ADMIN).json()["snapshots"]
    target = next(row for row in listed if row["snapshot_id"] == snap_id)
    assert target["status"] == "FAILED"
    assert any(row["reason"] == "SAFETY" for row in listed)

    second = client.post(
        f"/v1/admin/snapshots/{snap_id}/restore",
        json={"confirm": True},
        headers=ADMIN,
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "snapshot_not_ready"
    assert _facts()["checksum"] == mutated["checksum"]


def test_restore_rejects_unsupported_schema_without_applying(client, frozen) -> None:
    create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    created = client.post("/v1/admin/snapshots", json={}, headers=ADMIN)
    snap_id = created.json()["snapshot_id"]
    before = _facts()
    session = _open()
    try:
        row = session.get(WorldSnapshot, snap_id)
        assert row is not None
        row.schema_version = 99
        session.commit()
    finally:
        session.close()

    refused = client.post(
        f"/v1/admin/snapshots/{snap_id}/restore",
        json={"confirm": True},
        headers=ADMIN,
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "snapshot_schema"
    after = _facts()
    assert after["checksum"] == before["checksum"]
    assert after["commands_open"] is True
    session = _open()
    try:
        row = session.get(WorldSnapshot, snap_id)
        assert row is not None
        assert row.status == "READY"
    finally:
        session.close()


def test_commands_and_worker_stop_while_maintenance_gates_are_closed(client, frozen) -> None:
    create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    alice = _login(client, "Alice")
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": 1, "target_city_id": 2},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    session = _open()
    try:
        state = session.get(WorldState, 1)
        assert state is not None
        state.commands_open = False
        state.worker_paused = True
        session.commit()
    finally:
        session.close()

    blocked = client.post(
        "/v1/commands/attack",
        json={"army_id": 1, "target_city_id": 2},
        headers=alice,
    )
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()["error"]["code"] == "maintenance"
    advanced = client.post("/v1/admin/clock/advance", json={"seconds": OUTBOUND}, headers=ADMIN)
    assert advanced.status_code == 409, advanced.text
    assert advanced.json()["error"]["code"] == "maintenance"

    status, event_id = run_once(frozen)
    assert status == "paused"
    assert event_id is None
    tick = client.post("/v1/admin/worker/tick", headers=ADMIN)
    assert tick.status_code == 200, tick.text
    assert tick.json()["paused"] is True
    assert tick.json()["processed"] == 0

    session = _open()
    try:
        event = session.scalar(select(Event).where(Event.status == EventStatus.PENDING))
        assert event is not None
    finally:
        session.close()


def test_battle_report_and_march_roundtrip_through_restore(client, frozen) -> None:
    """A snapshot taken after the march, and one taken after the battle, both restore."""

    create_scenario(frozen.now(), rate=RATE, stock=STOCK)
    alice = _login(client, "Alice")
    attack = client.post(
        "/v1/commands/attack",
        json={"army_id": 1, "target_city_id": 2},
        headers=alice,
    )
    assert attack.status_code == 200, attack.text
    marching = _facts()
    assert marching["counts"]["movements"] == 1
    assert marching["counts"]["battle_reports"] == 0
    march_snap = client.post("/v1/admin/snapshots", json={"reason": "MANUAL"}, headers=ADMIN)
    assert march_snap.status_code == 200, march_snap.text
    march_id = march_snap.json()["snapshot_id"]
    assert march_snap.json()["checksum"] == marching["checksum"]

    advanced = client.post(
        "/v1/admin/clock/advance",
        json={"seconds": OUTBOUND + HOMEWARD},
        headers=ADMIN,
    )
    assert advanced.status_code == 200, advanced.text
    tick = client.post("/v1/admin/worker/tick", headers=ADMIN)
    assert tick.json()["processed"] == 2
    fought = _facts()
    assert fought["counts"]["battle_reports"] == 1
    assert fought["checksum"] != marching["checksum"]

    session = _open()
    try:
        document = capture_document(session)
        assert document["battle_reports"][0]["winner"] == "attacker"
        assert checksum_text(canonical_json(document)) == fought["checksum"]
    finally:
        session.close()

    battle_snap = client.post("/v1/admin/snapshots", json={"reason": "AUTO"}, headers=ADMIN)
    assert battle_snap.status_code == 200, battle_snap.text
    battle_id = battle_snap.json()["snapshot_id"]
    assert battle_snap.json()["checksum"] == fought["checksum"]
    assert battle_snap.json()["reason"] == "AUTO"

    restored_march = client.post(
        f"/v1/admin/snapshots/{march_id}/restore",
        json={"confirm": True},
        headers=ADMIN,
    )
    assert restored_march.status_code == 200, restored_march.text
    assert restored_march.json()["match"] is True
    after_march = _facts()
    assert after_march["checksum"] == marching["checksum"]
    assert after_march["counts"] == marching["counts"]
    assert after_march["armies"]["Oak Company"]["status"] == "marching"
    assert after_march["cities"] == marching["cities"]

    restored_battle = client.post(
        f"/v1/admin/snapshots/{battle_id}/restore",
        json={"confirm": True},
        headers=ADMIN,
    )
    assert restored_battle.status_code == 200, restored_battle.text
    assert restored_battle.json()["restored_checksum"] == fought["checksum"]
    after_battle = _facts()
    assert after_battle["checksum"] == fought["checksum"]
    assert after_battle["counts"] == fought["counts"]
    assert after_battle["armies"] == fought["armies"]
    assert after_battle["cities"] == fought["cities"]
    assert after_battle["offset"] == fought["offset"]
