"""Scripted traffic, the due-event burst, and one check per failure.

Invariant checks run after the scenario has drained the worker. A scenario that
cannot be performed is INCOMPLETE. It is not reported as a pass.
"""

from __future__ import annotations

import os
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from simcore.load.httpapi import HttpApi
from simcore.load.invariants import due_spread, event_counts, open_readonly, with_invariants
from simcore.load.metrics import Recorder
from simcore.load.processes import (
    ProcessSet,
    postgres_is_up,
    restart_postgres,
    terminate_backends,
    wait_http_ok,
    wait_until,
)

_READS = (
    ("GET", "/v1/time"),
    ("GET", "/v1/me"),
    ("GET", "/v1/me/cities"),
    ("GET", "/v1/me/armies"),
    ("GET", "/v1/me/reports"),
    ("GET", "/v1/map/cities"),
)
_REQUIRED_ROUTES = (
    "GET /v1/time",
    "GET /v1/me",
    "GET /v1/me/cities",
    "GET /v1/me/armies",
    "GET /v1/me/reports",
    "GET /v1/map/cities",
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
_BUILDINGS = ("lumber_camp", "farm", "iron_mine", "warehouse", "barracks")
_TECHS = ("forestry", "husbandry", "metallurgy", "logistics")


@dataclass
class PlayerSlot:
    name: str
    password: str
    player_id: int
    access_token: str
    refresh_token: str
    city_ids: list[int] = field(default_factory=list)
    army_ids: list[int] = field(default_factory=list)
    enemy_city_id: int | None = None


@dataclass
class RunContext:
    api: HttpApi
    players: list[PlayerSlot]
    processes: ProcessSet
    checks: list[dict[str, Any]]
    database_url: str
    seed: int
    samples: list[dict[str, Any]] = field(default_factory=list)
    next_wood: int = 0
    manage_processes: bool = True

    def add(self, name: str, status: str, detail: str, **data: Any) -> None:
        body: dict[str, Any] = {"name": name, "status": status, "detail": detail, "required": True}
        if data:
            body["data"] = data
        self.checks.append(body)

    def reconnect(self) -> None:
        from simcore.config import get_settings
        from simcore.db import reset_engine

        get_settings.cache_clear()
        reset_engine()

    def counts(self) -> dict[str, int]:
        try:
            return self._counts()
        except Exception:
            self.reconnect()
            return self._counts()

    def _counts(self) -> dict[str, int]:
        session = open_readonly()
        try:
            return event_counts(session)
        finally:
            session.rollback()
            session.close()

    def invariants(self, prefix: str) -> None:
        self.reconnect()
        self.checks.extend(with_invariants(prefix))

    def replace_client(self) -> None:
        recorder = self.api.recorder
        token = self.api.admin_token
        self.api.close()
        self.api = HttpApi(self.processes.base_url, token, recorder)


def _auth(player: PlayerSlot, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {player.access_token}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _home(player: PlayerSlot) -> int:
    if not player.city_ids:
        raise RuntimeError(f"{player.name} has no city")
    return player.city_ids[0]


def _camp(player: PlayerSlot) -> int:
    if len(player.city_ids) < 2:
        return _home(player)
    return player.city_ids[1]


def refresh_views(ctx: RunContext) -> None:
    for player in ctx.players:
        status, cities = ctx.api.call("GET", "/v1/me/cities", phase="setup", headers=_auth(player))
        if status != 200 or not isinstance(cities, dict):
            raise RuntimeError(f"cities for {player.name} returned HTTP {status}")
        status, armies = ctx.api.call("GET", "/v1/me/armies", phase="setup", headers=_auth(player))
        if status != 200 or not isinstance(armies, dict):
            raise RuntimeError(f"armies for {player.name} returned HTTP {status}")
        status, world = ctx.api.call("GET", "/v1/map/cities", phase="setup", headers=_auth(player))
        if status != 200 or not isinstance(world, dict):
            raise RuntimeError(f"map for {player.name} returned HTTP {status}")
        player.city_ids = [int(row["id"]) for row in cities.get("cities") or []]
        player.army_ids = [int(row["id"]) for row in armies.get("armies") or []]
    by_player = {player.player_id: player.city_ids[0] for player in ctx.players if player.city_ids}
    ordered = sorted(ctx.players, key=lambda item: item.player_id)
    for index, player in enumerate(ordered):
        nxt = ordered[(index + 1) % len(ordered)]
        player.enemy_city_id = by_player.get(nxt.player_id)


def scripted_mix(ctx: RunContext) -> None:
    """Call every player route once. Rejections are recorded; the route still counts."""

    player = ctx.players[0]
    headers = _auth(player)
    for method, path in _READS:
        ctx.api.call(method, path, phase="scripted", headers=headers)
    if not player.city_ids or not player.army_ids:
        _scripted_without_holdings(ctx, player)
        return
    home = _home(player)
    camp = _camp(player)
    ctx.api.call(
        "POST",
        "/v1/commands/build",
        phase="scripted",
        headers=_auth(player, f"script-build-{ctx.seed}"),
        json={"city_id": home, "building": "lumber_camp"},
    )
    ctx.api.call(
        "POST",
        "/v1/commands/research",
        phase="scripted",
        headers=_auth(player, f"script-research-{ctx.seed}"),
        json={"tech": "forestry"},
    )
    ctx.api.call(
        "POST",
        "/v1/commands/train",
        phase="scripted",
        headers=_auth(player, f"script-train-{ctx.seed}"),
        json={"city_id": home, "unit_type": "militia", "count": 1},
    )
    ctx.api.call(
        "POST",
        "/v1/commands/transfer",
        phase="scripted",
        headers=_auth(player, f"script-transfer-{ctx.seed}"),
        json={"source_city_id": home, "destination_city_id": camp, "wood": 10},
    )
    ctx.api.call(
        "POST",
        "/v1/commands/found-city",
        phase="scripted",
        headers=_auth(player, f"script-found-{ctx.seed}"),
        json={"source_city_id": home, "x": 220, "y": 220, "name": f"{player.name} Outpost"},
    )
    army = player.army_ids[0]
    ctx.api.call(
        "POST",
        "/v1/commands/recall",
        phase="scripted",
        headers=_auth(player, f"script-recall-{ctx.seed}"),
        json={"army_id": army},
    )
    ctx.api.call(
        "POST",
        "/v1/commands/garrison",
        phase="scripted",
        headers=_auth(player, f"script-garrison-{ctx.seed}"),
        json={"army_id": army, "city_id": home},
    )
    ctx.api.call(
        "POST",
        "/v1/commands/move",
        phase="scripted",
        headers=_auth(player, f"script-move-{ctx.seed}"),
        json={"army_id": army, "destination_city_id": home, "relocate": False},
    )


def _scripted_without_holdings(ctx: RunContext, player: PlayerSlot) -> None:
    """Hit each command route once when this process did not seed cities."""

    headers = _auth(player)
    bodies: dict[str, dict[str, Any]] = {
        "/v1/commands/build": {"city_id": 0, "building": "lumber_camp"},
        "/v1/commands/research": {"tech": "forestry"},
        "/v1/commands/train": {"city_id": 0, "unit_type": "militia", "count": 1},
        "/v1/commands/transfer": {"source_city_id": 0, "destination_city_id": 0, "wood": 1},
        "/v1/commands/found-city": {"source_city_id": 0, "x": 1, "y": 1, "name": "Nowhere"},
        "/v1/commands/recall": {"army_id": 0},
        "/v1/commands/garrison": {"army_id": 0, "city_id": 0},
        "/v1/commands/move": {"army_id": 0, "destination_city_id": 0, "relocate": False},
        "/v1/commands/attack": {"army_id": 0, "target_city_id": 0},
    }
    for path, body in bodies.items():
        ctx.api.call(
            "POST",
            path,
            phase="scripted",
            headers=_auth(player, f"script-empty-{ctx.seed}-{path.rsplit('/', 1)[-1]}"),
            json=body,
        )
    del headers


def queue_attacks(ctx: RunContext) -> int:
    accepted = 0
    for index, player in enumerate(ctx.players):
        if not player.army_ids or player.enemy_city_id is None:
            continue
        status, _body = ctx.api.call(
            "POST",
            "/v1/commands/attack",
            phase="scripted",
            headers=_auth(player, f"burst-attack-{ctx.seed}-{index}"),
            json={"army_id": player.army_ids[0], "target_city_id": player.enemy_city_id},
        )
        if status == 200:
            accepted += 1
    return accepted


def queue_transfers(ctx: RunContext, count: int) -> int:
    """Queue transfer arrivals. Amounts differ so same-second event keys do not collide."""

    accepted = 0
    for index, player in enumerate(ctx.players):
        if len(player.city_ids) < 2:
            continue
        for _step in range(count):
            ctx.next_wood += 1
            amount = ctx.next_wood
            status, _body = ctx.api.call(
                "POST",
                "/v1/commands/transfer",
                phase="failure",
                headers=_auth(player, f"pile-{ctx.seed}-{amount}"),
                json={
                    "source_city_id": player.city_ids[0],
                    "destination_city_id": player.city_ids[1],
                    "wood": amount,
                },
            )
            if status == 200:
                accepted += 1
    return accepted


def make_due(ctx: RunContext) -> dict[str, Any]:
    """Advance the game clock past every pending due_at so they become due together."""

    ctx.reconnect()
    session = open_readonly()
    try:
        before = event_counts(session)
        spread = due_spread(session)
    finally:
        session.rollback()
        session.close()
    token = ctx.players[0].access_token
    status, body = ctx.api.call(
        "GET",
        "/v1/time",
        phase="failure",
        headers={"Authorization": f"Bearer {token}"},
        record=False,
    )
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("server_time"), str):
        raise RuntimeError(f"GET /v1/time returned HTTP {status}")
    game_now = datetime.fromisoformat(body["server_time"])
    if game_now.tzinfo is None:
        game_now = game_now.replace(tzinfo=timezone.utc)
    ctx.reconnect()
    session = open_readonly()
    try:
        latest = session.execute(text("SELECT max(due_at) FROM events WHERE status = 'pending'")).scalar_one()
    finally:
        session.rollback()
        session.close()
    seconds = 1
    if latest is not None:
        if latest.tzinfo is None:
            from datetime import timezone

            latest = latest.replace(tzinfo=timezone.utc)
        delay = (latest - game_now).total_seconds()
        if delay > 0:
            seconds = int(delay) + 1
    advanced = ctx.api.call(
        "POST",
        "/v1/admin/clock/advance",
        phase="failure",
        headers={**ctx.api.admin_headers, "content-type": "application/json"},
        json={"seconds": seconds, "minutes": 0, "hours": 0},
    )
    if advanced[0] != 200:
        raise RuntimeError(f"clock advance returned HTTP {advanced[0]}: {advanced[1]}")
    after = ctx.counts()
    return {"before": before, "after": after, "spread": spread, "advanced_seconds": seconds}


def _observe_mid_batch(ctx: RunContext, timeout: float = 5.0) -> dict[str, int] | None:
    baseline = ctx.counts().get("completed", 0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = ctx.counts()
        new_completed = current.get("completed", 0) - baseline
        remaining = current.get("pending", 0) + current.get("processing", 0)
        if new_completed >= 1 and remaining >= 1:
            return {
                "new_completed": new_completed,
                "pending": current.get("pending", 0),
                "processing": current.get("processing", 0),
            }
        time.sleep(0.005)
    return None


def drain(ctx: RunContext, timeout: float = 90.0) -> bool:
    if not ctx.processes.worker_pids():
        ctx.processes.start_worker("load-a")

    def done() -> bool:
        current = ctx.counts()
        return current.get("due", 0) == 0 and current.get("processing", 0) == 0

    return wait_until(done, timeout, interval=0.05)


def scenario_kill_worker(ctx: RunContext) -> None:
    ctx.processes.stop_workers(kill=True)
    attacks = queue_attacks(ctx)
    transfers = queue_transfers(ctx, count=4)
    info = make_due(ctx)
    due = int(info["after"].get("due", 0))
    spread = info["spread"]
    spread_text = spread.get("spread_seconds")
    if due >= 4:
        ctx.add(
            "concurrent_arrivals",
            "PASS",
            f"{due} events were due after one clock advance of {info['advanced_seconds']}s; "
            f"attacks accepted {attacks}; transfers accepted {transfers}; "
            f"pending due_at spread before the advance was {spread_text} seconds",
            due=due,
            spread=spread,
            advanced_seconds=info["advanced_seconds"],
        )
    else:
        ctx.add(
            "concurrent_arrivals",
            "FAIL",
            f"only {due} events were due after the clock advance",
            due=due,
            spread=spread,
        )
    observed = None
    for attempt in range(2):
        if attempt:
            ctx.processes.stop_workers(kill=True)
            queue_transfers(ctx, count=4)
            make_due(ctx)
        proc = ctx.processes.start_worker("load-a")
        observed = _observe_mid_batch(ctx)
        ctx.processes.kill_worker(proc)
        if observed is not None:
            break
    if observed is None:
        ctx.add(
            "kill_worker_mid_batch",
            "INCOMPLETE",
            "the worker drained the due batch before a pending event was observed beside a completion",
        )
    else:
        ctx.add(
            "kill_worker_mid_batch",
            "PASS",
            f"killed the worker after {observed['new_completed']} new completions "
            f"with {observed['pending']} pending and {observed['processing']} processing",
            observed=observed,
        )
    ctx.processes.start_worker("load-a")
    if not drain(ctx):
        ctx.add("kill_worker_drain", "FAIL", "due events remained after the worker was restarted")
    else:
        ctx.add("kill_worker_drain", "PASS", "restarted worker drained every due event")
    ctx.processes.stop_workers(kill=False)
    ctx.invariants("kill_worker")


def scenario_two_workers(ctx: RunContext) -> None:
    ctx.processes.stop_workers(kill=True)
    queued = queue_transfers(ctx, count=4)
    ctx.processes.start_worker("load-a")
    ctx.processes.start_worker("load-b")
    heartbeats = wait_until(lambda: _heartbeat_count(ctx) >= 2, 10)
    info = make_due(ctx)
    drained = drain(ctx)
    marks = _marks_by_worker(ctx)
    ctx.processes.stop_workers(kill=False)
    if not heartbeats:
        ctx.add("two_workers", "FAIL", "the second worker did not write a heartbeat")
    elif not drained:
        ctx.add("two_workers", "FAIL", "due events remained while two workers were running")
    else:
        ctx.add(
            "two_workers",
            "PASS",
            f"two workers ran; processed marks by worker {marks}; transfers queued {queued}; "
            f"due after the shared advance {info['after'].get('due')}",
            marks=marks,
        )
    ctx.invariants("two_workers")


def scenario_api_restart(ctx: RunContext) -> None:
    player = ctx.players[0]
    camp = _camp(player)
    key = f"api-restart-{ctx.seed}"
    started = threading.Event()
    holder: dict[str, Any] = {}

    def hammer() -> None:
        started.set()
        for _ in range(30):
            ctx.api.call("GET", "/v1/map/cities", phase="failure", headers=_auth(player))

    def command() -> None:
        started.wait(timeout=5)
        status, body = ctx.api.call(
            "POST",
            "/v1/commands/build",
            phase="failure",
            headers=_auth(player, key),
            json={"city_id": camp, "building": "farm"},
        )
        holder["first"] = (status, body)

    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
    threads.append(threading.Thread(target=command, daemon=True))
    for thread in threads:
        thread.start()
    if not started.wait(timeout=5):
        ctx.add("api_restart", "INCOMPLETE", "in-flight requests did not start before the deadline")
        for thread in threads:
            thread.join(timeout=5)
        return
    ctx.processes.kill_api()
    for thread in threads:
        thread.join(timeout=10)
    try:
        ctx.processes.start_api(ctx.processes.api_port)
    except Exception as exc:
        ctx.add("api_restart", "FAIL", f"API did not return after SIGKILL: {exc.__class__.__name__}: {exc}")
        return
    ctx.replace_client()
    first = ctx.api.call(
        "POST",
        "/v1/commands/build",
        phase="failure",
        headers=_auth(player, key),
        json={"city_id": camp, "building": "farm"},
    )
    second = ctx.api.call(
        "POST",
        "/v1/commands/build",
        phase="failure",
        headers=_auth(player, key),
        json={"city_id": camp, "building": "farm"},
    )
    if first[0] == second[0] and first[1] == second[1] and first[0] in {200, 409}:
        ctx.add(
            "api_restart",
            "PASS",
            f"API restarted; replaying the in-flight key twice returned HTTP {first[0]} with the same body",
        )
    else:
        ctx.add(
            "api_restart",
            "FAIL",
            f"replay after restart differed: {first[0]} then {second[0]}",
        )
    if not ctx.processes.worker_pids():
        ctx.processes.start_worker("load-a")
    drain(ctx)
    ctx.processes.stop_workers(kill=False)
    ctx.invariants("api_restart")


def scenario_postgres_drop(ctx: RunContext) -> None:
    if not ctx.processes.worker_pids():
        ctx.processes.start_worker("load-a")
    try:
        dropped = terminate_backends(ctx.database_url)
    except Exception as exc:
        ctx.add("postgres_connection_drop", "FAIL", f"pg_terminate_backend failed: {exc.__class__.__name__}")
        return
    ctx.reconnect()
    ready = wait_http_ok(ctx.processes.base_url, "/health/ready", timeout=20)
    if not ready:
        ctx.add(
            "postgres_connection_drop",
            "FAIL",
            f"terminated {dropped} backends and /health/ready did not return 200",
        )
        return
    ctx.add(
        "postgres_connection_drop",
        "PASS",
        f"terminated {dropped} client backends; /health/ready returned 200 afterward",
        terminated=dropped,
    )
    if not drain(ctx, timeout=60):
        ctx.add("postgres_connection_drop_drain", "FAIL", "due events remained after connections were dropped")
    else:
        ctx.add("postgres_connection_drop_drain", "PASS", "worker drained due events after the connection drop")
    ctx.processes.stop_workers(kill=False)
    ctx.invariants("postgres_drop")


def scenario_postgres_restart(ctx: RunContext) -> None:
    if not ctx.processes.worker_pids():
        ctx.processes.start_worker("load-a")
    worker_pid = ctx.processes.worker_pids()[0]
    api_pid = ctx.processes.api_pid()
    ok, method = restart_postgres(ctx.database_url)
    if not ok:
        ctx.add(
            "postgres_restart",
            "INCOMPLETE",
            f"postgres was not restarted ({method})",
            method=method,
        )
        return
    up = wait_until(lambda: postgres_is_up(ctx.database_url), 60, interval=0.2)
    ctx.reconnect()
    ready = wait_http_ok(ctx.processes.base_url, "/health/ready", timeout=30) if up else False
    worker_alive = _pid_alive(worker_pid)
    api_alive = _pid_alive(api_pid) if api_pid is not None else False
    if not up or not ready or not api_alive:
        ctx.add(
            "postgres_restart",
            "FAIL",
            f"restart via {method} left postgres_up={up} api_ready={ready} api_alive={api_alive} worker_alive={worker_alive}",
            method=method,
        )
    else:
        detail = f"restarted postgres via {method}; API /health/ready returned 200"
        status = "PASS"
        if not worker_alive:
            status = "FAIL"
            detail += "; the worker process exited"
        else:
            detail += "; the worker process stayed up"
        ctx.add("postgres_restart", status, detail, method=method, worker_alive=worker_alive)
    if not worker_alive:
        ctx.processes.workers.clear()
        ctx.processes.start_worker("load-a")
    if not drain(ctx, timeout=90):
        ctx.add("postgres_restart_drain", "FAIL", "due events remained after postgres restarted")
    else:
        ctx.add("postgres_restart_drain", "PASS", "due events drained after postgres restarted")
    ctx.processes.stop_workers(kill=False)
    ctx.invariants("postgres_restart")


def scenario_idempotency(ctx: RunContext) -> None:
    player = ctx.players[0]
    status, cities = ctx.api.call("GET", "/v1/me/cities", phase="failure", headers=_auth(player))
    if status != 200 or not isinstance(cities, dict) or not cities.get("cities"):
        ctx.add("idempotency_replay", "FAIL", f"could not read cities (HTTP {status})")
        ctx.invariants("idempotency")
        return
    city_id = max(int(row["id"]) for row in cities["cities"])
    seq_key = f"idem-seq-{ctx.seed}"
    body = {"city_id": city_id, "building": "barracks"}
    first = ctx.api.call(
        "POST", "/v1/commands/build", phase="failure", headers=_auth(player, seq_key), json=body
    )
    second = ctx.api.call(
        "POST", "/v1/commands/build", phase="failure", headers=_auth(player, seq_key), json=body
    )
    sequential_ok = first[0] == second[0] and first[1] == second[1] and first[0] == 200
    barrier = threading.Barrier(8)
    results: list[tuple[int, Any]] = []
    lock = threading.Lock()
    conc_key = f"idem-conc-{ctx.seed}"
    conc_body = {"city_id": city_id, "building": "warehouse"}

    def once() -> None:
        barrier.wait(timeout=5)
        outcome = ctx.api.call(
            "POST",
            "/v1/commands/build",
            phase="failure",
            headers=_auth(player, conc_key),
            json=conc_body,
        )
        with lock:
            results.append(outcome)

    workers = [threading.Thread(target=once) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    alive = [worker for worker in workers if worker.is_alive()]
    same = len(results) == 8 and all(item[0] == results[0][0] and item[1] == results[0][1] for item in results)
    statuses = {item[0] for item in results}
    concurrent_ok = not alive and same and statuses <= {200}
    if sequential_ok and concurrent_ok:
        ctx.add(
            "idempotency_replay",
            "PASS",
            "sequential replay matched, and 8 concurrent calls with one key returned one shared body",
        )
    else:
        ctx.add(
            "idempotency_replay",
            "FAIL",
            f"sequential_ok={sequential_ok} first={first[0]} second={second[0]} "
            f"concurrent_results={len(results)} statuses={sorted(statuses)}",
        )
    ctx.invariants("idempotency")


def scenario_refresh_race(ctx: RunContext) -> None:
    player = ctx.players[-1]
    token = player.refresh_token
    barrier = threading.Barrier(8)
    results: list[tuple[int, Any]] = []
    lock = threading.Lock()

    def once() -> None:
        barrier.wait(timeout=5)
        outcome = ctx.api.call(
            "POST",
            "/v1/auth/refresh",
            phase="failure",
            json={"refresh_token": token},
        )
        with lock:
            results.append(outcome)

    workers = [threading.Thread(target=once) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    winners = [item for item in results if item[0] == 200 and isinstance(item[1], dict)]
    losers = [item for item in results if item[0] == 401]
    follow = None
    if len(winners) == 1:
        follow = ctx.api.call(
            "POST",
            "/v1/auth/refresh",
            phase="failure",
            json={"refresh_token": winners[0][1].get("refresh_token")},
        )
    if len(results) == 8 and len(winners) == 1 and len(losers) == 7 and follow is not None and follow[0] == 401:
        ctx.add(
            "refresh_reuse_race",
            "PASS",
            "one refresh succeeded and the rotated token was revoked after the overlapping reuse",
        )
    else:
        ctx.add(
            "refresh_reuse_race",
            "FAIL",
            f"results={len(results)} winners={len(winners)} losers={len(losers)} follow={None if follow is None else follow[0]}",
        )
    ctx.invariants("refresh_reuse")


def scenario_clock_during_processing(ctx: RunContext) -> None:
    ctx.processes.stop_workers(kill=True)
    queued = queue_transfers(ctx, count=4)
    make_due(ctx)
    proc = ctx.processes.start_worker("load-a")
    observed = _observe_mid_batch(ctx)
    if observed is None:
        ctx.processes.kill_worker(proc)
        ctx.add(
            "clock_advance_during_processing",
            "INCOMPLETE",
            f"could not observe the worker mid-batch (queued transfers {queued})",
        )
        ctx.processes.start_worker("load-a")
        drain(ctx)
        ctx.processes.stop_workers(kill=False)
        ctx.invariants("clock_advance")
        return
    status, body = ctx.api.call(
        "POST",
        "/v1/admin/clock/advance",
        phase="failure",
        headers={**ctx.api.admin_headers, "content-type": "application/json"},
        json={"seconds": 60, "minutes": 0, "hours": 0},
    )
    if status != 200:
        ctx.add("clock_advance_during_processing", "FAIL", f"clock advance returned HTTP {status}: {body}")
    else:
        ctx.add(
            "clock_advance_during_processing",
            "PASS",
            f"advanced 60s while {observed['pending']} events were still pending "
            f"and {observed['new_completed']} had completed in this batch",
            observed=observed,
        )
    if not drain(ctx):
        ctx.add("clock_advance_drain", "FAIL", "due events remained after the clock advance")
    else:
        ctx.add("clock_advance_drain", "PASS", "due events drained after the clock advance")
    ctx.processes.stop_workers(kill=False)
    ctx.invariants("clock_advance")


def scenario_snapshot_during_load(ctx: RunContext) -> None:
    if not ctx.processes.worker_pids():
        ctx.processes.start_worker("load-a")
    stop = threading.Event()
    player = ctx.players[0]

    def loop() -> None:
        while not stop.is_set():
            ctx.api.call("GET", "/v1/map/cities", phase="failure", headers=_auth(player))
            ctx.api.call("GET", "/v1/me", phase="failure", headers=_auth(player))

    threads = [threading.Thread(target=loop, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    status, body = ctx.api.call(
        "POST",
        "/v1/admin/snapshots",
        phase="failure",
        headers={**ctx.api.admin_headers, "content-type": "application/json"},
        json={"reason": "MANUAL"},
        timeout=60,
    )
    stop.set()
    for thread in threads:
        thread.join(timeout=5)
    if status != 200 or not isinstance(body, dict) or not body.get("snapshot_id"):
        ctx.add("snapshot_during_load", "FAIL", f"snapshot create returned HTTP {status}: {body}")
        ctx.processes.stop_workers(kill=False)
        ctx.invariants("snapshot")
        return
    snapshot_id = int(body["snapshot_id"])
    inspected = ctx.api.call(
        "GET",
        f"/v1/admin/snapshots/{snapshot_id}/inspect",
        phase="failure",
        headers=ctx.api.admin_headers,
    )
    checksum_ok = isinstance(inspected[1], dict) and inspected[1].get("checksum_ok") is True
    if inspected[0] == 200 and checksum_ok:
        ctx.add(
            "snapshot_during_load",
            "PASS",
            f"snapshot {snapshot_id} checksum_ok while map reads were in flight",
            snapshot_id=snapshot_id,
            checksum=body.get("checksum"),
        )
    else:
        ctx.add(
            "snapshot_during_load",
            "FAIL",
            f"inspect HTTP {inspected[0]} checksum_ok={checksum_ok}",
        )
    drain(ctx)
    ctx.processes.stop_workers(kill=False)
    ctx.invariants("snapshot")


def run_mixed_load(ctx: RunContext, *, duration: float, rate: float, concurrency: int) -> float:
    """Send mixed traffic for duration seconds. Returns measured elapsed seconds."""

    if ctx.manage_processes and not ctx.processes.worker_pids():
        ctx.processes.start_worker("load-a")
    q: queue.Queue = queue.Queue(maxsize=concurrency)
    rng = random.Random(ctx.seed + 17)

    def consumer() -> None:
        while True:
            item = q.get()
            try:
                if item is None:
                    return
                item()
            finally:
                q.task_done()

    threads = [threading.Thread(target=consumer, daemon=True) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    started = time.perf_counter()
    deadline = started + duration
    next_at = started
    interval = 1.0 / rate
    sequence = 0
    while time.perf_counter() < deadline:
        sequence += 1
        player = ctx.players[rng.randrange(len(ctx.players))]
        action = rng.randrange(len(_REQUIRED_ROUTES))
        q.put(lambda p=player, action=action, sequence=sequence: _load_action(ctx, p, action, sequence))
        next_at += interval
        delay = next_at - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    for _thread in threads:
        q.put(None)
    for thread in threads:
        thread.join(timeout=30)
    return time.perf_counter() - started


def _load_action(ctx: RunContext, player: PlayerSlot, action: int, sequence: int) -> None:
    headers = _auth(player, f"load-{ctx.seed}-{sequence}")
    route = _REQUIRED_ROUTES[action % len(_REQUIRED_ROUTES)]
    method, path = route.split(" ", 1)
    if method == "GET":
        ctx.api.call(method, path, phase="load", headers=headers)
        return
    home = player.city_ids[0] if player.city_ids else 0
    camp = player.city_ids[1] if len(player.city_ids) > 1 else home
    army = player.army_ids[0] if player.army_ids else 0
    enemy = player.enemy_city_id or camp
    bodies: dict[str, dict[str, Any]] = {
        "/v1/commands/move": {"army_id": army, "destination_city_id": camp, "relocate": False},
        "/v1/commands/attack": {"army_id": army, "target_city_id": enemy},
        "/v1/commands/recall": {"army_id": army},
        "/v1/commands/build": {"city_id": home, "building": _BUILDINGS[sequence % len(_BUILDINGS)]},
        "/v1/commands/research": {"tech": _TECHS[sequence % len(_TECHS)]},
        "/v1/commands/train": {"city_id": home, "unit_type": "militia", "count": 1 + (sequence % 3)},
        "/v1/commands/found-city": {
            "source_city_id": home,
            "x": -200 + (sequence % 50),
            "y": -180 - (sequence % 20),
            "name": f"L{sequence % 1000}",
        },
        "/v1/commands/garrison": {"army_id": army, "city_id": camp},
        "/v1/commands/transfer": {"source_city_id": home, "destination_city_id": camp, "food": 1 + (sequence % 5)},
    }
    ctx.api.call(method, path, phase="load", headers=headers, json=bodies[path])


def check_route_mix(ctx: RunContext) -> None:
    seen = {str(row["route"]) for row in ctx.api.recorder.rows}
    missing = [route for route in _REQUIRED_ROUTES if route not in seen]
    if missing:
        ctx.add("command_mix", "INCOMPLETE", "routes not called: " + ", ".join(missing))
    else:
        ctx.add("command_mix", "PASS", "every listed read and command route was called")


def check_failure_server_errors(recorder: Recorder) -> dict[str, Any]:
    """5xx during failure scenarios is a server error. Connection loss is expected."""

    rows = [row for row in recorder.rows if row["phase"] == "failure"]
    server_errors = [row for row in rows if int(row["status"]) >= 500]
    dropped = sum(1 for row in rows if int(row["status"]) == 0)
    if server_errors:
        return {
            "name": "failure_http_5xx",
            "status": "FAIL",
            "detail": f"{len(server_errors)} failure-phase calls returned HTTP 5xx",
            "required": True,
            "data": {"server_errors": len(server_errors), "connection_errors": dropped, "calls": len(rows)},
        }
    return {
        "name": "failure_http_5xx",
        "status": "PASS",
        "detail": (
            f"{len(rows)} failure-phase calls had no HTTP 5xx; "
            f"{dropped} connection errors were recorded while processes were killed"
        ),
        "required": True,
        "data": {"connection_errors": dropped, "calls": len(rows)},
    }


def check_load_errors(recorder: Recorder) -> dict[str, Any]:
    rows = [row for row in recorder.rows if row["phase"] in {"load", "scripted"}]
    bad = [row for row in rows if int(row["status"]) >= 500 or int(row["status"]) == 0]
    if not rows:
        return {
            "name": "load_http",
            "status": "INCOMPLETE",
            "detail": "scripted and load phases recorded no HTTP calls",
            "required": True,
        }
    if bad:
        return {
            "name": "load_http",
            "status": "FAIL",
            "detail": f"{len(bad)} scripted or load calls were 5xx or connection errors",
            "required": True,
            "data": {"bad": len(bad), "calls": len(rows)},
        }
    return {
        "name": "load_http",
        "status": "PASS",
        "detail": f"{len(rows)} scripted and load calls completed without a 5xx or a connection error",
        "required": True,
    }


def _heartbeat_count(ctx: RunContext) -> int:
    ctx.reconnect()
    session = open_readonly()
    try:
        value = session.execute(
            text(
                """
                SELECT count(*)::int FROM worker_heartbeats
                WHERE worker_id IN ('load-a', 'load-b')
                """
            )
        ).scalar_one()
        return int(value or 0)
    finally:
        session.rollback()
        session.close()


def _marks_by_worker(ctx: RunContext) -> dict[str, int]:
    ctx.reconnect()
    session = open_readonly()
    try:
        rows = session.execute(
            text(
                """
                SELECT worker_id, count(*)::int
                FROM worker_process_marks
                WHERE outcome = 'processed'
                GROUP BY worker_id
                """
            )
        ).all()
    finally:
        session.rollback()
        session.close()
    return {str(worker_id): int(count) for worker_id, count in rows}


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
