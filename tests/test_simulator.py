"""Simulator safety, deterministic plans, and one real CI run against Postgres."""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

import pytest

from simcore.sim.bots import plan_tick
from simcore.sim.cli import main
from simcore.sim.ids import uuid_for
from simcore.sim.safety import SafetyError, ensure_safe, production_reasons
from simcore.sim.runner import SimConfig, run
from tests.conftest import truncate


def test_production_host_is_refused() -> None:
    reasons = production_reasons(
        base_url="https://157-85-96-139.sslip.io",
        database_url=None,
        env_name="development",
    )
    assert reasons
    with pytest.raises(SafetyError):
        ensure_safe(
            mode="staging",
            base_url="https://157-85-96-139.sslip.io",
            database_url=None,
            env_name="development",
            allow_production=False,
        )


def test_production_flag_allows_the_check_without_connecting() -> None:
    ensure_safe(
        mode="staging",
        base_url="https://157-85-96-139.sslip.io",
        database_url=None,
        env_name="production",
        allow_production=True,
    )


def test_ci_refuses_the_live_database_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMCORE_ADMIN_TOKEN", "dev-admin")
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "--mode",
                "ci",
                "--players",
                "2",
                "--ticks",
                "1",
                "--database-url",
                "postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore",
            ]
        )
    assert caught.value.code == 2


def test_staging_requires_a_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMCORE_ADMIN_TOKEN", "dev-admin")
    with pytest.raises(SystemExit) as caught:
        main(["--mode", "staging", "--players", "1", "--ticks", "1"])
    assert caught.value.code == 2


def test_trace_ids_follow_the_seed() -> None:
    def draw(seed: int) -> list[str]:
        rng = random.Random(f"simcore-trace-{seed}")
        return [uuid_for(rng) for _ in range(3)]

    assert draw(7) == draw(7)
    assert draw(7) != draw(8)


def test_same_seed_plans_the_same_commands() -> None:
    players = [
        {
            "id": 1,
            "name": "Bot01",
            "profile": "aggressive",
            "armies": [{"id": 1, "status": "garrisoned", "location_city_id": 1, "home_city_id": 1, "units": []}],
            "cities": [{"id": 1}, {"id": 2}],
            "world_cities": [
                {"id": 1, "player_id": 1},
                {"id": 2, "player_id": 1},
                {"id": 3, "player_id": 2},
            ],
        },
        {
            "id": 2,
            "name": "Bot02",
            "profile": "defensive",
            "armies": [{"id": 2, "status": "garrisoned", "location_city_id": 3, "home_city_id": 3, "units": []}],
            "cities": [{"id": 3}, {"id": 4}],
            "world_cities": [
                {"id": 1, "player_id": 1},
                {"id": 3, "player_id": 2},
                {"id": 4, "player_id": 2},
            ],
        },
        {
            "id": 3,
            "name": "Bot03",
            "profile": "random",
            "armies": [{"id": 3, "status": "garrisoned", "location_city_id": 5, "home_city_id": 5, "units": []}],
            "cities": [{"id": 5}, {"id": 6}],
            "world_cities": [
                {"id": 1, "player_id": 1},
                {"id": 5, "player_id": 3},
                {"id": 6, "player_id": 3},
            ],
        },
    ]

    def once(seed: int) -> list[dict[str, object]]:
        rng = random.Random(seed)
        return [command.as_dict() for command in plan_tick(rng, tick=0, players=players, command_rate=1)]

    assert once(11) == once(11)
    assert once(11) != once(12)
    actions = [row["action"] for row in once(11)]
    assert actions[0] == "attack"
    assert actions[1] == "garrison"


def test_ci_same_seed_matches(db: None, tmp_path: Path) -> None:
    first = _run(tmp_path / "a")
    truncate()
    second = _run(tmp_path / "b")
    if first["result"] != "PASS":
        pytest.fail(json.dumps(first["failed_invariants"], indent=2, default=str))
    assert second["result"] == "PASS"
    assert first["world_checksum"] == second["world_checksum"]
    assert first["world_checksum"].startswith("sha256:")
    assert first["command_sequence"] == second["command_sequence"]
    assert first["counts"]["commands_accepted"] > 0
    assert first["counts"]["events"] > 0
    assert first["counts"]["battles"] > 0
    assert first["trace_verdicts"]["FAIL"] == 0
    assert first["audit_chain"]["status"] == "PASS"
    assert first["auth_coverage"]["verdict"] == "COMPLETE"
    assert second["auth_coverage"]["verdict"] == "COMPLETE"
    assert first["skipped_actions"] == []
    report = json.loads((tmp_path / "a" / "report.json").read_text(encoding="utf-8"))
    assert report["result"] == "PASS"
    assert (tmp_path / "a" / "report.md").read_text(encoding="utf-8").startswith("# Simulator report")


def _run(directory: Path) -> dict[str, object]:
    return run(
        SimConfig(
            mode="ci",
            players=3,
            seed=11,
            ticks=2,
            duration=None,
            command_rate=1,
            base_url=None,
            database_url=None,
            report_dir=directory,
            allow_production=False,
            admin_token=os.environ["SIMCORE_ADMIN_TOKEN"],
        )
    )
