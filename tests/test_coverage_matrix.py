"""The coverage verdict stays INCOMPLETE until every row is actually tested."""

from __future__ import annotations

from simcore.sim.coverage import ADMIN_ENDPOINTS, COMMANDS, PLAYER_ENDPOINTS, coverage_report


def _state(*, valid: bool, invalid: bool, combat: bool) -> dict:
    flag = {"valid": valid, "invalid": invalid}
    return {
        "endpoints": {name: dict(flag) for name in (*PLAYER_ENDPOINTS, *ADMIN_ENDPOINTS)},
        "commands": {name: dict(flag) for name in COMMANDS},
        "combat": {"win": combat, "lose": combat, "draw": combat},
    }


def test_missing_invalid_and_unchecked_invariant_are_incomplete() -> None:
    state = _state(valid=True, invalid=False, combat=True)
    report = coverage_report(
        state,
        [{"invariant": "production_upkeep", "status": "NOT CHECKED", "detail": "no accrual rows"}],
        {"checks": []},
    )
    assert report["verdict"] == "INCOMPLETE"
    assert any("invalid is NOT TESTED" in gap for gap in report["gaps"])
    assert any("production_upkeep is NOT CHECKED" in gap for gap in report["gaps"])
    assert all(row["invalid"] == "NOT TESTED" for row in report["matrix"])


def test_complete_requires_pass_on_every_invariant() -> None:
    state = _state(valid=True, invalid=True, combat=True)
    report = coverage_report(
        state,
        [{"invariant": "ledger_conservation", "status": "PASS", "detail": "balanced"}],
        {"checks": [{"name": "command_traces", "status": "PASS", "detail": "all resolve"}]},
    )
    assert report["verdict"] == "COMPLETE"
    assert report["gaps"] == []


def test_unmeasured_backup_stays_unknown_and_does_not_block_complete() -> None:
    state = _state(valid=True, invalid=True, combat=True)
    backup = {
        "invariant": "monitoring.backup.last_success",
        "status": "UNKNOWN",
        "detail": "no off-site backup has been measured",
    }
    report = coverage_report(
        state,
        [
            {"invariant": "ledger_conservation", "status": "PASS", "detail": "balanced"},
            backup,
        ],
        {"checks": []},
    )
    assert report["verdict"] == "COMPLETE"
    assert report["gaps"] == []
    listed = next(row for row in report["invariants"] if row["name"] == "monitoring.backup.last_success")
    assert listed["status"] == "UNKNOWN"
    assert listed["status"] != "PASS"


def test_any_other_unknown_monitoring_check_is_incomplete() -> None:
    state = _state(valid=True, invalid=True, combat=True)
    report = coverage_report(
        state,
        [
            {"invariant": "ledger_conservation", "status": "PASS", "detail": "balanced"},
            {"invariant": "monitoring.host.cpu", "status": "UNKNOWN", "detail": "not measured"},
        ],
        {"checks": []},
    )
    assert report["verdict"] == "INCOMPLETE"
    assert any("monitoring.host.cpu is UNKNOWN" in gap for gap in report["gaps"])
