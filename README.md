# Server Simulation Core

A single-process game server for a Travian / Rise of Kingdoms style world. The player sends an intent. The server schedules the march. A worker applies the result when the army arrives, even if the player is offline. When they reconnect, the city, the army, and the battle report are already updated.

PostgreSQL is the only source of truth. There is one API process and one worker process. No Redis, no microservices, no load balancer.

```
Godot client
    |  HTTPS   (intent only: "send army 12 to city 8")
    v
Game server  (FastAPI)
    |-- Commands        validate ownership, write Movement + Event
    |-- Game rules      travel time, production, combat
    |-- Event scheduler due_at rows in Postgres
    v
PostgreSQL
    ^
Worker
    |-- claim due events   SELECT ... FOR UPDATE SKIP LOCKED
    |-- resolve arrival    battle report, ledger, return march
    |-- update the world   one database transaction per event
```

The client counts down from `arrive_at` itself. Real casualties and loot appear only after the worker runs.

## Stack

- Python 3.12, FastAPI, Pydantic
- SQLAlchemy 2 and Alembic
- PostgreSQL 16
- Pytest

Combat is a pure function, `resolve_battle(attacker, defender, seed)`, with its own LCG so a report can be replayed. Resource changes go through the `transactions` ledger. Retrying an event cannot grant loot twice.

Dev login is a **placeholder**. `POST /v1/auth/dev-login` returns `dev:{player_id}`. It is not signed. Do not expose this port publicly.

## Run locally (Docker)

```bash
docker compose up --build
```

That starts Postgres, runs migrations, seeds Alice and Bob, serves the API on port **8741**, and starts the worker.

- API docs: http://127.0.0.1:8741/docs
- Health: http://127.0.0.1:8741/health
- Admin token: `dev-admin` (header `X-Admin-Token`)

Alice's Oakhold is at (10, 10). Bob's Ironford is at (40, 50). The distance is 50 tiles and Alice's army moves at 6 tiles/hour, so an attack takes 30000 seconds (8h 20m).

```bash
# Placeholder login. The warning in the body is intentional.
curl -s -X POST http://127.0.0.1:8741/v1/auth/dev-login \
  -H 'content-type: application/json' -d '{"name":"Alice"}'

# Send Oak Company (army 1) to Ironford (city 2). The response includes depart_at and arrive_at.
curl -s -X POST http://127.0.0.1:8741/v1/commands/attack \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer dev:1' \
  -d '{"army_id":1,"target_city_id":2}'

# Fast-forward simulated time and let the worker catch up.
# 17 hours covers the march out and the march home.
curl -s -X POST http://127.0.0.1:8741/v1/admin/clock/advance \
  -H 'content-type: application/json' \
  -H 'x-admin-token: dev-admin' \
  -d '{"hours":17}'

curl -s -X POST http://127.0.0.1:8741/v1/admin/worker/tick \
  -H 'x-admin-token: dev-admin'

curl -s http://127.0.0.1:8741/v1/me/reports -H 'authorization: Bearer dev:1'
curl -s http://127.0.0.1:8741/v1/me/cities -H 'authorization: Bearer dev:1'
```

Useful admin and CLI entry points:

| Action | HTTP | CLI |
| --- | --- | --- |
| Server time | `GET /v1/time` | `simcore-cli clock now` |
| Pending events | `GET /v1/admin/events?status=pending` | `simcore-cli events --status pending` |
| Army positions and ETA | `GET /v1/admin/armies` | `simcore-cli armies` |
| Advance the clock | `POST /v1/admin/clock/advance` | `simcore-cli clock advance --hours 1` |
| Run one event now | `POST /v1/admin/events/{id}/run` | `simcore-cli run-event --id 1` |
| Ledger | `GET /v1/admin/transactions` | `simcore-cli ledger` |
| Drain due events | `POST /v1/admin/worker/tick` | `python -m simcore.worker --once` |

Admin routes are on unless `SIMCORE_ENV=production`. In production set `SIMCORE_ENABLE_ADMIN=1` to turn them back on.

## Run without Docker

Postgres must already be running. The default URL is `postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

alembic upgrade head
python -m simcore.seed
uvicorn simcore.main:app --host 0.0.0.0 --port 8741
```

In another shell:

```bash
source .venv/bin/activate
python -m simcore.worker
```

## Tests

Integration tests need a database named `simcore_test` on the same server. They refuse to truncate any other database name.

```bash
# With the compose Postgres already publishing 5432:
docker compose up -d db
# or any local Postgres, then:
#   CREATE USER simcore WITH PASSWORD 'simcore';
#   CREATE DATABASE simcore_test OWNER simcore;

source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Override the URL with `SIMCORE_TEST_DATABASE_URL` if needed.

The suite covers:

- Travel time, including mixed-army speed and the 3-4-5 and 30-40-50 distances.
- `resolve_battle` determinism, loot, and a fixed casualty case.
- The vertical slice over HTTP: attack, ETA, fast-forward, worker tick, battle report, return home, resource totals, and a second pass that changes nothing.
- A worker crash before commit, then a retry, then a forced replay: loot is applied once.
- `SELECT … FOR UPDATE SKIP LOCKED` so two workers claim different events.

## Layout

```
src/simcore/
  main.py                 HTTP app
  worker.py               claims and commits one event at a time
  cli.py                  admin shell
  seed.py                 Alice and Bob
  clock.py                SystemClock, FrozenClock, database offset
  game/combat.py          resolve_battle
  game/travel.py          distance and ETA
  game/commands.py        MOVE, ATTACK, RECALL, BUILD, RESEARCH
  game/processor.py       arrival, return, build, research
  game/queue.py           SKIP LOCKED claim
  game/ledger.py          transactions + idempotency keys
docs/GAME_RULES.md        the rules this server enforces
alembic/                  schema migrations
docker-compose.yml        Postgres + API + worker
```

## Clock

`OffsetClock` is `base.now() + world_state.offset_seconds`. The API and the worker both read that row, so advancing time in one process is visible to the other. Tests inject a `FrozenClock` as the base. Event effects use the event's `due_at`, so a worker that wakes up late does not stretch the march.

## Left for later

- Real authentication and sessions
- Backups and point-in-time recovery
- Monitoring and metrics
- A full dead-letter workflow for failed events (this MVP marks an event `failed` after 5 attempts so one poison row cannot block the queue)
- Rate limiting
- Fog of war
- Alliances
- Market
- Supply lines (upkeep is only charged while garrisoned)
- Horizontal scaling (the claim query is already safe for more than one worker; running more than one is not the target)

Game rules that are intentionally thin — no city capture, no build costs, no wounded troops — are listed at the bottom of `docs/GAME_RULES.md`.
