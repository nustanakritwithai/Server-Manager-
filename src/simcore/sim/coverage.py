"""Scripted coverage of every public player endpoint and command.

The scene is fixed: four bots, a coverage roster, and the frozen CI clock.
It does not draw from the bot RNG. Valid calls must succeed. Invalid calls
must be 4xx and must not change city or army state. Combat is forced:
militia versus militia draws, militia versus cavalry loses, and cavalry
against an empty city wins.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Callable

from simcore.game.catalog import MAX_CITIES_PER_PLAYER
from simcore.game.travel import interpolate, travel_progress
from simcore.sim.http import ApiClient

PLAYER_ENDPOINTS: tuple[str, ...] = (
    "POST /v1/auth/dev-login",
    "GET /v1/time",
    "GET /v1/me",
    "GET /v1/me/cities",
    "GET /v1/me/cities/{city_id}",
    "GET /v1/map/cities",
    "GET /v1/me/armies",
    "GET /v1/me/reports",
    "GET /v1/me/reports/{report_id}",
    "POST /v1/commands/move",
    "POST /v1/commands/attack",
    "POST /v1/commands/recall",
    "POST /v1/commands/build",
    "POST /v1/commands/research",
    "POST /v1/commands/train",
    "POST /v1/commands/found-city",
    "POST /v1/commands/garrison",
    "POST /v1/commands/transfer",
)

COMMANDS: tuple[str, ...] = (
    "move",
    "attack",
    "recall",
    "reinforce",
    "train_units",
    "found_city",
    "garrison",
    "transfer_resources",
    "build",
    "research",
)

ADMIN_ENDPOINTS: tuple[str, ...] = (
    "GET /v1/admin/trace",
    "GET /v1/admin/trace/{trace_id}",
    "GET /v1/admin/audit",
    "GET /v1/admin/monitoring",
    "GET /v1/admin/monitoring/history",
    "GET /v1/admin/world-map",
)

Advance = Callable[[ApiClient, int], None]
Drain = Callable[[ApiClient], dict[str, Any]]
SampleLag = Callable[[ApiClient], float]


def run_coverage(
    api: ApiClient,
    bots: list[dict[str, Any]],
    *,
    advance: Advance,
    drain: Drain,
    sample_lag: SampleLag,
) -> dict[str, Any]:
    """Play the scripted scene. Raises when a required outcome does not happen."""

    if len(bots) < 4:
        raise RuntimeError("coverage needs 4 players")
    by_name = {str(bot["name"]): bot for bot in bots}
    actors = [by_name[f"Bot0{index}"] for index in range(1, 5)]
    script = _Script(api, actors, advance, drain, sample_lag)
    script.play()
    return script.result()


def check_admin(api: ApiClient, state: dict[str, Any], *, trace_ids: list[str]) -> dict[str, Any]:
    """Read-only admin checks after the world has been caught up."""

    checks: list[dict[str, Any]] = []
    admin = api.admin_headers
    endpoints = state.setdefault("endpoints", {})
    for method, path, params in (
        ("GET", "/v1/admin/trace", {"limit": 200}),
        ("GET", "/v1/admin/audit", {"limit": 1}),
        ("GET", "/v1/admin/monitoring", None),
        ("GET", "/v1/admin/monitoring/history", {"metric": "event_lag_seconds", "window": "24h"}),
        ("GET", "/v1/admin/world-map", None),
    ):
        status, body = api.json(method, path, headers=admin, params=params)
        checks.append(_admin_row(f"{method} {path}", status, body, expect=200))
        if status == 200 and f"{method} {path}" in endpoints:
            endpoints[f"{method} {path}"]["valid"] = True
        denied, _ = api.json(method, path, params=params)
        checks.append(
            {
                "name": f"{method} {path} unauthenticated",
                "status": "PASS" if 400 <= denied < 500 else "FAIL",
                "detail": f"HTTP {denied}",
            }
        )

    missing, _ = api.json("GET", "/v1/admin/monitoring/history", headers=admin)
    checks.append(
        {
            "name": "GET /v1/admin/monitoring/history missing metric",
            "status": "PASS" if missing == 400 else "FAIL",
            "detail": f"HTTP {missing}",
        }
    )

    unresolved: list[str] = []
    for trace_id in trace_ids:
        status, body = api.json("GET", f"/v1/admin/trace/{trace_id}", headers=admin)
        verdict = body.get("verdict") if isinstance(body, dict) else None
        if status != 200 or verdict != "PASS":
            unresolved.append(f"{trace_id} HTTP {status} verdict {verdict}")
    if trace_ids and not unresolved and "GET /v1/admin/trace/{trace_id}" in endpoints:
        endpoints["GET /v1/admin/trace/{trace_id}"]["valid"] = True
    checks.append(
        {
            "name": "command_traces",
            "status": "PASS" if trace_ids and not unresolved else "FAIL",
            "detail": (
                f"{len(trace_ids)} accepted traces resolve to PASS"
                if not unresolved
                else "; ".join(unresolved[:8])
            ),
        }
    )
    unknown, _ = api.json("GET", "/v1/admin/trace/does-not-exist", headers=admin)
    checks.append(
        {
            "name": "GET /v1/admin/trace/{trace_id} unknown",
            "status": "PASS" if unknown == 404 else "FAIL",
            "detail": f"HTTP {unknown}",
        }
    )

    map_status, world = api.json("GET", "/v1/admin/world-map", headers=admin)
    map_problems = _world_map_problems(world) if map_status == 200 and isinstance(world, dict) else ["world-map HTTP"]
    checks.append(
        {
            "name": "world_map_consistency",
            "status": "PASS" if map_status == 200 and not map_problems else "FAIL",
            "detail": "positions match stored movements" if not map_problems else "; ".join(map_problems),
        }
    )
    audit_status, audit = api.json("GET", "/v1/admin/audit", headers=admin, params={"limit": 1})
    chain = audit.get("chain") if isinstance(audit, dict) else {}
    chain_status = chain.get("status") if isinstance(chain, dict) else None
    checks.append(
        {
            "name": "audit_chain_read",
            "status": "PASS" if audit_status == 200 and chain_status == "PASS" else "FAIL",
            "detail": f"HTTP {audit_status} chain {chain_status}",
        }
    )
    _pulse_worker(api)
    mon_status, mon = api.json("GET", "/v1/admin/monitoring", headers=admin)
    critical = []
    if isinstance(mon, dict):
        for check in mon.get("checks") or []:
            if isinstance(check, dict) and check.get("status") == "CRITICAL":
                critical.append(str(check.get("name")))
    checks.append(
        {
            "name": "monitoring_no_critical",
            "status": "PASS" if mon_status == 200 and not critical else "FAIL",
            "detail": "no CRITICAL check" if not critical else ", ".join(critical),
        }
    )
    return {"checks": checks}


# The lab has no Google Drive upload. This check stays UNKNOWN, which is not a
# pass. It is the one monitoring invariant full mode cannot make true without
# inventing an off-site backup. Every other non-PASS invariant is still a gap.
_EXPECTED_UNMEASURED = frozenset({"monitoring.backup.last_success"})


def _expected_unmeasured(item: dict[str, Any]) -> bool:
    return str(item.get("status") or "") == "UNKNOWN" and item.get("invariant") in _EXPECTED_UNMEASURED


def coverage_report(state: dict[str, Any], invariants: list[dict[str, Any]], admin: dict[str, Any]) -> dict[str, Any]:
    """COMPLETE only when every endpoint, command, and invariant was actually proven."""

    matrix = _matrix_rows(state)
    gaps: list[str] = []
    for row in matrix:
        if row["valid"] != "tested":
            gaps.append(f"{row['name']} valid is NOT TESTED")
        if row["invalid"] != "tested":
            gaps.append(f"{row['name']} invalid is NOT TESTED")
    invariant_rows: list[dict[str, Any]] = []
    for item in invariants:
        status = str(item.get("status") or "NOT CHECKED")
        invariant_rows.append(
            {
                "name": item.get("invariant"),
                "status": status,
                "detail": item.get("detail"),
            }
        )
        if status != "PASS" and not _expected_unmeasured(item):
            gaps.append(f"invariant {item.get('invariant')} is {status}: {item.get('detail')}")
    for check in admin.get("checks") or []:
        status = str(check.get("status") or "FAIL")
        invariant_rows.append({"name": check.get("name"), "status": status, "detail": check.get("detail")})
        if status != "PASS":
            gaps.append(f"{check.get('name')} is {status}: {check.get('detail')}")
    combat = state.get("combat") or {}
    for outcome in ("win", "lose", "draw"):
        if not combat.get(outcome):
            gaps.append(f"combat {outcome} was NOT TESTED")
    verdict = "COMPLETE" if not gaps else "INCOMPLETE"
    return {
        "verdict": verdict,
        "gaps": gaps,
        "matrix": matrix,
        "invariants": invariant_rows,
        "combat": combat,
        "commands_by_type": state.get("commands_by_type") or {},
    }


class _Script:
    def __init__(
        self,
        api: ApiClient,
        actors: list[dict[str, Any]],
        advance: Advance,
        drain: Drain,
        sample_lag: SampleLag,
    ) -> None:
        self.api = api
        self.actors = actors
        self.advance = advance
        self.drain = drain
        self.sample_lag = sample_lag
        self.sequence: list[dict[str, Any]] = []
        self.traces: list[str] = []
        self.endpoints = {name: {"valid": False, "invalid": False} for name in (*PLAYER_ENDPOINTS, *ADMIN_ENDPOINTS)}
        self.commands = {name: {"valid": False, "invalid": False} for name in COMMANDS}
        self.combat = {"win": False, "lose": False, "draw": False}
        self.drain_failures: list[int] = []
        self.max_lag = 0.0
        self.notes: list[str] = []

    def result(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "trace_ids": self.traces,
            "endpoints": self.endpoints,
            "commands": self.commands,
            "combat": self.combat,
            "drain_failures": self.drain_failures,
            "max_lag": self.max_lag,
            "notes": self.notes,
        }

    def play(self) -> None:
        bot1, bot2, bot3, bot4 = self.actors
        cities = self._cities(bot1)
        home = {bot["name"]: cities[f"{bot['name']} Home"] for bot in self.actors}
        camp = {bot["name"]: cities[f"{bot['name']} Camp"] for bot in self.actors}
        army = {bot["name"]: self._army(bot) for bot in self.actors}

        self._reads(bot1, int(home[bot1["name"]]["id"]))
        self._invalid_opening(bot1, bot2, home, camp, army)
        self._mark_admin_invalid_placeholders()

        self.advance(self.api, 3600)
        self.max_lag = max(self.max_lag, self.sample_lag(self.api))

        garrison_1 = self._accept(
            bot1,
            "garrison",
            "/v1/commands/garrison",
            {"army_id": army[bot1["name"]], "city_id": int(camp[bot1["name"]]["id"])},
        )
        garrison_4 = self._accept(
            bot4,
            "garrison",
            "/v1/commands/garrison",
            {"army_id": army[bot4["name"]], "city_id": int(camp[bot4["name"]]["id"])},
        )
        if garrison_1["arrive_at"] != garrison_4["arrive_at"]:
            raise RuntimeError("concurrent garrisons did not share an arrive_at")
        attack = self._accept(
            bot2,
            "attack",
            "/v1/commands/attack",
            {"army_id": army[bot2["name"]], "target_city_id": int(home[bot3["name"]]["id"])},
        )
        self._reject(
            bot2,
            "attack",
            "POST",
            "/v1/commands/attack",
            {"army_id": army[bot2["name"]], "target_city_id": int(home[bot3["name"]]["id"])},
            "replayed attack",
        )

        depart = _parse_time(garrison_1["depart_at"])
        arrive = _parse_time(garrison_1["arrive_at"])
        half = int((arrive - depart).total_seconds() // 2)
        self.advance(self.api, half)
        self._assert_world_map()
        rest = int((arrive - depart).total_seconds() - half)
        self.advance(self.api, rest)
        tick = self._tick_once()
        arrived = {int(garrison_1["event_id"]), int(garrison_4["event_id"])}
        if not arrived <= set(tick):
            raise RuntimeError(f"concurrent arrivals were not processed together: {tick}")
        self._remember_drain(self.drain(self.api))

        self._found_until_limit(bot4, int(home[bot4["name"]]["id"]))
        self._accept(
            bot1,
            "transfer_resources",
            "/v1/commands/transfer",
            {
                "source_city_id": int(home[bot1["name"]]["id"]),
                "destination_city_id": int(camp[bot1["name"]]["id"]),
                "wood": 100,
                "food": 0,
                "iron": 0,
                "gold": 0,
            },
        )
        self._reject(
            bot1,
            "transfer_resources",
            "POST",
            "/v1/commands/transfer",
            {
                "source_city_id": int(home[bot1["name"]]["id"]),
                "destination_city_id": int(camp[bot1["name"]]["id"]),
                "wood": 100,
                "food": 0,
                "iron": 0,
                "gold": 0,
            },
            "replayed transfer",
        )
        self._accept(
            bot1,
            "build",
            "/v1/commands/build",
            {"city_id": int(home[bot1["name"]]["id"]), "building": "farm"},
        )
        self._accept(bot1, "research", "/v1/commands/research", {"tech": "forestry"})

        recalled = self._accept(bot1, "recall", "/v1/commands/recall", {"army_id": army[bot1["name"]]})
        self._wait(_parse_time(recalled["arrive_at"]))
        self._reject(
            bot1,
            "recall",
            "POST",
            "/v1/commands/recall",
            {"army_id": army[bot1["name"]]},
            "recall after the army has arrived home",
        )
        moved = self._accept(
            bot1,
            "move",
            "/v1/commands/move",
            {
                "army_id": army[bot1["name"]],
                "destination_city_id": int(camp[bot1["name"]]["id"]),
                "relocate": True,
            },
        )
        self._wait(_parse_time(moved["arrive_at"]))
        bot1_army = self._army_row(bot1)
        if int(bot1_army["home_city_id"]) != int(camp[bot1["name"]]["id"]):
            raise RuntimeError("relocate did not change the home city")
        if str(bot1_army["status"]) != "garrisoned":
            raise RuntimeError("relocated army is not garrisoned")

        trained = self._accept(
            bot1,
            "train_units",
            "/v1/commands/train",
            {
                "city_id": int(camp[bot1["name"]]["id"]),
                "unit_type": "militia",
                "count": 1,
                "army_id": army[bot1["name"]],
            },
        )
        self._wait(_parse_time(trained["due_at"]))
        self._wait(_parse_time(attack["arrive_at"]))
        self._require_winner(bot2, "draw")
        self.combat["draw"] = True
        self._catch_pending()
        self._accept(
            bot3,
            "reinforce",
            "/v1/commands/move",
            {
                "army_id": army[bot3["name"]],
                "destination_city_id": int(camp[bot3["name"]]["id"]),
                "relocate": False,
            },
        )
        self._catch_pending()

        reinforced = self._army_row(bot3)
        if int(reinforced.get("location_city_id") or 0) != int(camp[bot3["name"]]["id"]):
            raise RuntimeError("reinforce did not garrison the army in its camp")
        if int(reinforced["home_city_id"]) != int(home[bot3["name"]]["id"]):
            raise RuntimeError("reinforce changed the home city")

        loser = self._accept(
            bot2,
            "attack",
            "/v1/commands/attack",
            {"army_id": army[bot2["name"]], "target_city_id": int(camp[bot1["name"]]["id"])},
        )
        self._wait(_parse_time(loser["arrive_at"]))
        self._require_winner(bot2, "defender")
        self.combat["lose"] = True
        destroyed = self._army_row(bot2)
        if str(destroyed["status"]) != "destroyed":
            raise RuntimeError(f"losing army status is {destroyed['status']}")

        winner = self._accept(
            bot4,
            "attack",
            "/v1/commands/attack",
            {"army_id": army[bot4["name"]], "target_city_id": int(camp[bot2["name"]]["id"])},
        )
        self._wait(_parse_time(winner["arrive_at"]))
        report = self._require_winner(bot4, "attacker")
        loot = report.get("loot") or {}
        if not any(int(loot.get(name) or 0) > 0 for name in ("wood", "food", "iron", "gold")):
            raise RuntimeError(f"attacker win carried no loot: {loot}")
        self.combat["win"] = True
        self._catch_pending()
        self._read_reports(bot2, bot4)

    def _reads(self, bot: dict[str, Any], city_id: int) -> None:
        headers = _bearer(bot)
        for path, template in (
            ("/v1/time", "GET /v1/time"),
            ("/v1/me", "GET /v1/me"),
            ("/v1/me/cities", "GET /v1/me/cities"),
            (f"/v1/me/cities/{city_id}", "GET /v1/me/cities/{city_id}"),
            ("/v1/map/cities", "GET /v1/map/cities"),
            ("/v1/me/armies", "GET /v1/me/armies"),
            ("/v1/me/reports", "GET /v1/me/reports"),
        ):
            status, body = self.api.json("GET", path, headers=headers)
            if status != 200:
                raise RuntimeError(f"GET {path} returned HTTP {status}: {body}")
            self.endpoints[template]["valid"] = True
            if path == "/v1/time":
                continue
            denied, _ = self.api.json("GET", path)
            if not 400 <= denied < 500:
                raise RuntimeError(f"GET {path} without a token returned HTTP {denied}")
            self.endpoints[template]["invalid"] = True
        status, _ = self.api.json("POST", "/v1/time", json={})
        if status != 405:
            raise RuntimeError(f"POST /v1/time returned HTTP {status}")
        self.endpoints["GET /v1/time"]["invalid"] = True

    def _invalid_opening(
        self,
        bot1: dict[str, Any],
        bot2: dict[str, Any],
        home: dict[str, dict[str, Any]],
        camp: dict[str, dict[str, Any]],
        army: dict[str, int],
    ) -> None:
        own_camp = int(camp[bot1["name"]]["id"])
        enemy_home = int(home[bot2["name"]]["id"])
        own_home = int(home[bot1["name"]]["id"])
        own_army = army[bot1["name"]]
        self._reject(bot1, "attack", "POST", "/v1/commands/attack", {"army_id": own_army, "target_city_id": own_camp}, "attack own city")
        self._reject(bot1, "attack", "POST", "/v1/commands/attack", {"army_id": 999999, "target_city_id": enemy_home}, "unknown army")
        self._reject(bot1, "attack", "POST", "/v1/commands/attack", {"army_id": own_army, "target_city_id": 999999}, "unknown city")
        self._reject(
            bot1,
            "move",
            "POST",
            "/v1/commands/move",
            {"army_id": own_army, "destination_city_id": enemy_home, "relocate": True},
            "move to another player",
        )
        self._reject(bot1, "move", "POST", "/v1/commands/move", {"army_id": own_army}, "move missing destination")
        self._reject(bot1, "recall", "POST", "/v1/commands/recall", {"army_id": own_army}, "recall an army already home")
        self._reject(bot1, "recall", "POST", "/v1/commands/recall", {"army_id": 999999}, "unknown army recall")
        self._reject(
            bot1,
            "train_units",
            "POST",
            "/v1/commands/train",
            {"city_id": own_home, "unit_type": "cavalry", "count": 100},
            "insufficient gold",
        )
        self._reject(
            bot1,
            "train_units",
            "POST",
            "/v1/commands/train",
            {"city_id": own_home, "unit_type": "dragon", "count": 1},
            "unknown unit",
        )
        self._reject(
            bot1,
            "train_units",
            "POST",
            "/v1/commands/train",
            {"city_id": own_home, "unit_type": "militia", "count": 101},
            "count above the maximum",
        )
        self._reject(
            bot1,
            "train_units",
            "POST",
            "/v1/commands/train",
            {"city_id": enemy_home, "unit_type": "militia", "count": 1},
            "train in another player's city",
        )
        self._reject(
            bot1,
            "found_city",
            "POST",
            "/v1/commands/found-city",
            {"source_city_id": own_home, "x": 501, "y": 0, "name": "Outside"},
            "out of bounds",
        )
        self._reject(
            bot1,
            "found_city",
            "POST",
            "/v1/commands/found-city",
            {"source_city_id": own_home, "x": int(home[bot1["name"]]["x"]), "y": int(home[bot1["name"]]["y"]), "name": "Overlap"},
            "tile occupied",
        )
        self._reject(
            bot1,
            "found_city",
            "POST",
            "/v1/commands/found-city",
            {"source_city_id": enemy_home, "x": 40, "y": 40, "name": "Stolen"},
            "found from another player's city",
        )
        self._reject(
            bot1,
            "garrison",
            "POST",
            "/v1/commands/garrison",
            {"army_id": own_army, "city_id": enemy_home},
            "garrison in an enemy city",
        )
        self._reject(
            bot1,
            "garrison",
            "POST",
            "/v1/commands/garrison",
            {"army_id": own_army, "city_id": own_home},
            "garrison where the army already stands",
        )
        self._reject(
            bot1,
            "transfer_resources",
            "POST",
            "/v1/commands/transfer",
            {
                "source_city_id": own_home,
                "destination_city_id": own_camp,
                "wood": 999999,
                "food": 0,
                "iron": 0,
                "gold": 0,
            },
            "insufficient wood",
        )
        self._reject(
            bot1,
            "transfer_resources",
            "POST",
            "/v1/commands/transfer",
            {
                "source_city_id": own_home,
                "destination_city_id": enemy_home,
                "wood": 10,
                "food": 0,
                "iron": 0,
                "gold": 0,
            },
            "transfer to another player",
        )
        self._reject(
            bot1,
            "transfer_resources",
            "POST",
            "/v1/commands/transfer",
            {
                "source_city_id": own_home,
                "destination_city_id": own_home,
                "wood": 10,
                "food": 0,
                "iron": 0,
                "gold": 0,
            },
            "transfer to the same city",
        )
        self._reject(
            bot1,
            "build",
            "POST",
            "/v1/commands/build",
            {"city_id": own_home, "building": "castle"},
            "unknown building",
        )
        self._reject(bot1, "research", "POST", "/v1/commands/research", {"tech": "alchemy"}, "unknown tech")
        self._reject(
            bot1,
            "reinforce",
            "POST",
            "/v1/commands/move",
            {"army_id": own_army, "destination_city_id": enemy_home, "relocate": False},
            "reinforce an enemy city",
        )
        status, body = self.api.json("POST", "/v1/auth/dev-login", json={"name": "NoSuchPlayer"})
        if not 400 <= status < 500:
            raise RuntimeError(f"unknown dev-login returned HTTP {status}: {body}")
        self.endpoints["POST /v1/auth/dev-login"]["invalid"] = True
        self.endpoints["POST /v1/auth/dev-login"]["valid"] = True
        other = int(home[bot2["name"]]["id"])
        denied, _ = self.api.json("GET", f"/v1/me/cities/{other}", headers=_bearer(bot1))
        if denied != 403:
            raise RuntimeError(f"another player's city returned HTTP {denied}")
        missing, _ = self.api.json("GET", "/v1/me/cities/999999", headers=_bearer(bot1))
        if missing != 404:
            raise RuntimeError(f"unknown city returned HTTP {missing}")

    def _found_until_limit(self, bot: dict[str, Any], source_city_id: int) -> None:
        cities = self._cities(bot)
        owned = sum(1 for city in cities.values() if int(city["player_id"]) == int(bot["id"]))
        x = int(cities[f"{bot['name']} Home"]["x"])
        slot = 0
        while owned < MAX_CITIES_PER_PLAYER:
            slot += 1
            self._accept(
                bot,
                "found_city",
                "/v1/commands/found-city",
                {"source_city_id": source_city_id, "x": x, "y": 4 + slot * 2, "name": f"{bot['name']} Colony {slot}"},
            )
            owned += 1
        self._reject(
            bot,
            "found_city",
            "POST",
            "/v1/commands/found-city",
            {"source_city_id": source_city_id, "x": x, "y": 40, "name": f"{bot['name']} Extra"},
            "city limit",
        )

    def _read_reports(self, participant: dict[str, Any], outsider: dict[str, Any]) -> None:
        status, body = self.api.json("GET", "/v1/me/reports", headers=_bearer(participant))
        if status != 200 or not isinstance(body, dict) or not body.get("reports"):
            raise RuntimeError(f"reports list HTTP {status}")
        report_id = int(body["reports"][0]["id"])
        one, detail = self.api.json("GET", f"/v1/me/reports/{report_id}", headers=_bearer(participant))
        if one != 200 or not isinstance(detail, dict):
            raise RuntimeError(f"report detail HTTP {one}")
        self.endpoints["GET /v1/me/reports/{report_id}"]["valid"] = True
        missing, _ = self.api.json("GET", "/v1/me/reports/999999", headers=_bearer(participant))
        if missing != 404:
            raise RuntimeError(f"unknown report HTTP {missing}")
        forbidden, _ = self.api.json("GET", f"/v1/me/reports/{report_id}", headers=_bearer(outsider))
        if forbidden != 403:
            raise RuntimeError(f"another player's report HTTP {forbidden}")
        self.endpoints["GET /v1/me/reports/{report_id}"]["invalid"] = True
        self.endpoints["GET /v1/me/reports"]["valid"] = True

    def _require_winner(self, bot: dict[str, Any], winner: str) -> dict[str, Any]:
        status, body = self.api.json("GET", "/v1/me/reports", headers=_bearer(bot))
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"reports HTTP {status}")
        reports = list(body.get("reports") or [])
        if not reports:
            raise RuntimeError(f"no battle report for {bot['name']}")
        latest = reports[-1]
        if latest.get("winner") != winner:
            raise RuntimeError(f"expected winner {winner}, got {latest.get('winner')}")
        return latest

    def _assert_world_map(self) -> None:
        status, body = self.api.json("GET", "/v1/admin/world-map", headers=self.api.admin_headers)
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"world-map HTTP {status}")
        problems = _world_map_problems(body)
        movements = body.get("movements") or []
        if len(movements) < 2:
            problems.append(f"expected overlapping marches, saw {len(movements)}")
        if problems:
            raise RuntimeError("; ".join(problems))
        self.endpoints["GET /v1/admin/world-map"]["valid"] = True

    def _accept(self, bot: dict[str, Any], action: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        status, response = self.api.json("POST", path, headers=_bearer(bot), json=body)
        self._record(bot, action, body, status, response)
        if status != 200 or not isinstance(response, dict):
            raise RuntimeError(f"{action} {path} returned HTTP {status}: {response}")
        trace_id = response.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            raise RuntimeError(f"{action} did not return a trace_id")
        self.traces.append(trace_id)
        self.commands[action]["valid"] = True
        self.endpoints[f"POST {path}"]["valid"] = True
        return response

    def _reject(
        self,
        bot: dict[str, Any],
        action: str,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        note: str,
    ) -> None:
        before = self._fingerprint()
        status, response = self.api.json(method, path, headers=_bearer(bot), json=body)
        self._record(bot, action, body, status, response, note=note)
        if not 400 <= status < 500:
            raise RuntimeError(f"{note} returned HTTP {status}: {response}")
        after = self._fingerprint()
        if before != after:
            raise RuntimeError(f"{note} changed world state")
        if action in self.commands:
            self.commands[action]["invalid"] = True
        self.endpoints[f"{method} {path}"]["invalid"] = True

    def _record(
        self,
        bot: dict[str, Any],
        action: str,
        body: dict[str, Any] | None,
        status: int,
        response: Any,
        *,
        note: str | None = None,
    ) -> None:
        trace_id = response.get("trace_id") if isinstance(response, dict) else None
        if status == 200:
            result = "accepted"
        elif status >= 500:
            result = "error"
        else:
            result = "rejected"
        self.sequence.append(
            {
                "tick": 0,
                "player_id": int(bot["id"]),
                "player_name": bot["name"],
                "profile": "coverage",
                "action": action,
                "body": body,
                "result": result,
                "http_status": status,
                "trace_id": trace_id,
                "note": note,
            }
        )

    def _fingerprint(self) -> str:
        parts = []
        for bot in self.actors:
            headers = _bearer(bot)
            _, cities = self.api.json("GET", "/v1/me/cities", headers=headers)
            _, armies = self.api.json("GET", "/v1/me/armies", headers=headers)
            parts.append({"cities": cities, "armies": armies})
        _, clock = self.api.json("GET", "/v1/time")
        parts.append({"time": clock})
        return json.dumps(parts, sort_keys=True, default=str)

    def _cities(self, bot: dict[str, Any]) -> dict[str, dict[str, Any]]:
        status, body = self.api.json("GET", "/v1/map/cities", headers=_bearer(bot))
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"map HTTP {status}")
        return {str(city["name"]): city for city in body.get("cities") or []}

    def _army(self, bot: dict[str, Any]) -> int:
        row = self._army_row(bot)
        return int(row["id"])

    def _army_row(self, bot: dict[str, Any]) -> dict[str, Any]:
        status, body = self.api.json("GET", "/v1/me/armies", headers=_bearer(bot))
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"armies HTTP {status}")
        rows = [row for row in body.get("armies") or [] if str(row.get("name", "")).endswith("Army")]
        if not rows:
            rows = list(body.get("armies") or [])
        if not rows:
            raise RuntimeError(f"{bot['name']} has no army")
        return rows[0]

    def _wait(self, when: datetime) -> None:
        now = _server_now(self.api)
        if when > now:
            seconds = max(1, math.ceil((when - now).total_seconds()))
            self.advance(self.api, seconds)
        self.max_lag = max(self.max_lag, self.sample_lag(self.api))
        self._remember_drain(self.drain(self.api))

    def _catch_pending(self) -> None:
        for _ in range(40):
            now = _server_now(self.api)
            status, body = self.api.json(
                "GET",
                "/v1/admin/events",
                headers=self.api.admin_headers,
                params={"status": "pending", "limit": 1},
            )
            if status != 200 or not isinstance(body, dict):
                raise RuntimeError(f"pending events HTTP {status}")
            events = body.get("events") or []
            if not events:
                return
            due = _parse_time(events[0]["due_at"])
            if due > now:
                seconds = max(1, math.ceil((due - now).total_seconds()))
                self.advance(self.api, seconds)
            self.max_lag = max(self.max_lag, self.sample_lag(self.api))
            drained = self.drain(self.api)
            self._remember_drain(drained)
            if not drained["processed"] and not drained["failed"]:
                return
        raise RuntimeError("coverage catch-up did not finish")

    def _tick_once(self) -> list[int]:
        status, body = self.api.json(
            "POST",
            "/v1/admin/worker/tick",
            headers=self.api.admin_headers,
            params={"limit": 50},
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"worker tick HTTP {status}")
        failed = [int(event_id) for event_id in body.get("failed_ids") or []]
        self.drain_failures.extend(failed)
        return [int(event_id) for event_id in body.get("event_ids") or []]

    def _remember_drain(self, drained: dict[str, Any]) -> None:
        self.drain_failures.extend(int(event_id) for event_id in drained.get("failed") or [])
        if drained.get("paused"):
            raise RuntimeError("worker paused during coverage")

    def _mark_admin_invalid_placeholders(self) -> None:
        """Unauthenticated admin calls are the invalid column. Valid calls happen after catch-up."""

        for template, path in (
            ("GET /v1/admin/trace", "/v1/admin/trace"),
            ("GET /v1/admin/audit", "/v1/admin/audit"),
            ("GET /v1/admin/monitoring", "/v1/admin/monitoring"),
            ("GET /v1/admin/monitoring/history", "/v1/admin/monitoring/history"),
            ("GET /v1/admin/world-map", "/v1/admin/world-map"),
        ):
            status, _ = self.api.json("GET", path)
            if not 400 <= status < 500:
                raise RuntimeError(f"GET {path} without admin auth returned HTTP {status}")
            self.endpoints[template]["invalid"] = True
        status, _ = self.api.json("GET", "/v1/admin/trace/missing-trace-id")
        if not 400 <= status < 500:
            raise RuntimeError(f"unknown trace without admin auth returned HTTP {status}")
        self.endpoints["GET /v1/admin/trace/{trace_id}"]["invalid"] = True


def _pulse_worker(api: ApiClient) -> None:
    """One empty tick refreshes the heartbeat so a long read phase is not DOWN."""

    status, body = api.json(
        "POST",
        "/v1/admin/worker/tick",
        headers=api.admin_headers,
        params={"limit": 1},
    )
    if status != 200:
        raise RuntimeError(f"heartbeat tick HTTP {status}: {body}")


def _bearer(bot: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {bot['token']}"}


def _parse_time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _server_now(api: ApiClient) -> datetime:
    status, body = api.json("GET", "/v1/time")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError("GET /v1/time failed")
    return _parse_time(body.get("server_time"))


def _world_map_problems(body: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    now = _parse_time(body.get("server_time"))
    for movement in body.get("movements") or []:
        if not isinstance(movement, dict):
            continue
        depart = _parse_time(movement.get("depart_at"))
        arrive = _parse_time(movement.get("arrive_at"))
        origin = movement.get("origin") or {}
        destination = movement.get("destination") or {}
        reported = movement.get("position") or {}
        progress = travel_progress(depart, arrive, now)
        x, y = interpolate(
            float(origin.get("x") or 0),
            float(origin.get("y") or 0),
            float(destination.get("x") or 0),
            float(destination.get("y") or 0),
            progress,
        )
        if round(x, 4) != reported.get("x") or round(y, 4) != reported.get("y"):
            problems.append(
                f"movement {movement.get('id')} position {reported} != interpolated ({round(x, 4)}, {round(y, 4)})"
            )
    for city in body.get("cities") or []:
        if not isinstance(city, dict):
            continue
        if city.get("x") is None or city.get("y") is None:
            problems.append(f"city {city.get('id')} has no stored position")
    return problems


def _admin_row(name: str, status: int, body: Any, *, expect: int) -> dict[str, Any]:
    ok = status == expect and isinstance(body, dict)
    return {"name": name, "status": "PASS" if ok else "FAIL", "detail": f"HTTP {status}"}


def _matrix_rows(state: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    endpoints = state.get("endpoints") or {}
    commands = state.get("commands") or {}
    for name in (*PLAYER_ENDPOINTS, *ADMIN_ENDPOINTS):
        flags = endpoints.get(name) or {}
        rows.append(
            {
                "kind": "endpoint",
                "name": name,
                "valid": "tested" if flags.get("valid") else "NOT TESTED",
                "invalid": "tested" if flags.get("invalid") else "NOT TESTED",
            }
        )
    for name in COMMANDS:
        flags = commands.get(name) or {}
        rows.append(
            {
                "kind": "command",
                "name": name,
                "valid": "tested" if flags.get("valid") else "NOT TESTED",
                "invalid": "tested" if flags.get("invalid") else "NOT TESTED",
            }
        )
    return rows
