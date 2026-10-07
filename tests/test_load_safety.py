"""Safety gate for the load tool. No network and no database."""

from __future__ import annotations

import pytest

from simcore.load.report import overall
from simcore.load.safety import (
    LoadSafetyError,
    ensure_bounds,
    ensure_external_target,
    ensure_local_target,
    ensure_probe_target,
)

_VPS = "https://157-85-96-139.sslip.io"


def test_production_url_is_refused_without_the_flag() -> None:
    with pytest.raises(LoadSafetyError, match="refusing"):
        ensure_external_target(
            base_url=_VPS,
            database_url=None,
            env_name=None,
            allow_production=False,
            rate=1,
            concurrency=1,
            duration=10,
            players=1,
            failures=False,
        )


def test_production_flag_does_not_raise_the_rate_cap() -> None:
    with pytest.raises(LoadSafetyError, match="rate cap"):
        ensure_external_target(
            base_url=_VPS,
            database_url=None,
            env_name=None,
            allow_production=True,
            rate=10,
            concurrency=1,
            duration=10,
            players=1,
            failures=False,
        )


def test_production_flag_allows_a_capped_load_without_failures() -> None:
    ensure_external_target(
        base_url=_VPS,
        database_url=None,
        env_name=None,
        allow_production=True,
        rate=1,
        concurrency=1,
        duration=10,
        players=1,
        failures=False,
    )


def test_failure_injection_is_refused_even_with_the_flag() -> None:
    with pytest.raises(LoadSafetyError, match="failure injection"):
        ensure_external_target(
            base_url="http://127.0.0.1:8741",
            database_url=None,
            env_name="development",
            allow_production=True,
            rate=1,
            concurrency=1,
            duration=10,
            players=1,
            failures=True,
        )


def test_local_live_database_is_refused() -> None:
    with pytest.raises(LoadSafetyError, match="live"):
        ensure_local_target("postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore")


def test_local_load_test_database_is_accepted() -> None:
    host, name = ensure_local_target(
        "postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore_load_test"
    )
    assert host == "127.0.0.1"
    assert name == "simcore_load_test"


def test_probe_refuses_the_vps_without_the_flag() -> None:
    with pytest.raises(LoadSafetyError, match="probe"):
        ensure_probe_target(_VPS, allow_production=False)


def test_probe_allows_the_vps_with_the_flag() -> None:
    ensure_probe_target(_VPS, allow_production=True)


def test_probe_allows_loopback_without_the_flag() -> None:
    ensure_probe_target("http://127.0.0.1:8741", allow_production=False)


def test_bounds_reject_a_huge_local_job() -> None:
    with pytest.raises(LoadSafetyError):
        ensure_bounds(mode="local", players=100, rate=1, concurrency=1, duration=1)


def test_overall_incomplete_is_not_a_pass() -> None:
    assert overall([{"name": "a", "status": "PASS", "required": True}]) == "PASS"
    assert overall([{"name": "a", "status": "INCOMPLETE", "required": True}]) == "INCOMPLETE"
    assert overall([{"name": "a", "status": "FAIL", "required": True}]) == "FAIL"
    assert (
        overall(
            [
                {"name": "a", "status": "INCOMPLETE", "required": True},
                {"name": "b", "status": "FAIL", "required": True},
            ]
        )
        == "FAIL"
    )
    assert (
        overall(
            [
                {"name": "a", "status": "PASS", "required": True},
                {"name": "b", "status": "INCOMPLETE", "required": False},
            ]
        )
        == "PASS"
    )
