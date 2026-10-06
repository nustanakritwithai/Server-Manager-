"""The optional in-process worker claims a due event without the standalone process."""

from __future__ import annotations

import time
from datetime import datetime

from fastapi.testclient import TestClient

from simcore.config import Settings
from simcore.main import create_app
from tests.conftest import ADMIN
from tests.world import create_scenario


def _login(client: TestClient, name: str) -> dict[str, str]:
    response = client.post("/v1/auth/dev-login", json={"name": name})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_embedded_worker_processes_a_due_event(db, frozen) -> None:
    create_scenario(frozen.now())
    settings = Settings(_env_file=None, embedded_worker=True, worker_poll_seconds=0.05)
    assert settings.embedded_worker is True
    app = create_app(settings=settings, base_clock=frozen)

    with TestClient(app) as client:
        thread = app.state.embedded_worker_thread
        assert thread.is_alive()

        alice = _login(client, "Alice")
        world = client.get("/v1/map/cities", headers=alice).json()["cities"]
        army = client.get("/v1/me/armies", headers=alice).json()["armies"][0]
        target = next(city for city in world if not city["is_mine"])
        attack = client.post(
            "/v1/commands/attack",
            json={"army_id": army["id"], "target_city_id": target["id"]},
            headers=alice,
        )
        assert attack.status_code == 200, attack.text
        march = attack.json()
        seconds = int((_parse(march["arrive_at"]) - _parse(march["depart_at"])).total_seconds())
        assert seconds > 0

        advanced = client.post("/v1/admin/clock/advance", json={"seconds": seconds}, headers=ADMIN)
        assert advanced.status_code == 200, advanced.text

        report = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            body = client.get("/v1/me/reports", headers=alice)
            assert body.status_code == 200, body.text
            reports = body.json()["reports"]
            if reports:
                report = reports[0]
                break
            time.sleep(0.05)

        assert report is not None, "embedded worker did not process the due arrival"
        assert report["winner"] == "attacker"
        events = client.get("/v1/admin/events", headers=ADMIN).json()["events"]
        arrival = next(event for event in events if event["id"] == report["event_id"])
        assert arrival["status"] == "completed"

    assert not thread.is_alive()
