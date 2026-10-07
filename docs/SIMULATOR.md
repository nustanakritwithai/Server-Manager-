# Simulator

Seeded bot players play the world through the public HTTP API. After the run, the server's own trace verdicts, audit chain, monitoring report, and snapshot checksum are read back, and a few read-only queries check the ledger, armies, and events. The job fails when any of those checks fail.

The simulator does not add gameplay endpoints. If a bot would need an action the API does not have, that action is skipped and listed in the report. It does not run arbitrary SQL, and it does not delete or truncate rows.

Phase 8 (load and failure injection) is not this tool. Command submission is a single function, `submit_commands`, with overlap fixed at 1 so a CI checksum does not depend on which request arrived first. A later load test can reuse the planner and the checker.

## What the bots can do

CI registers each bot with `POST /v1/auth/register`, refreshes once, and then uses only the player routes. The password is derived from the seed and the bot name inside the process. It is not stored in the world snapshot. Staging does not call dev-login. It sets a temporary password on each existing player through the admin accounts API, changes that password, and refreshes. That replaces any password those players already had. Every command sends an `Idempotency-Key`. After the scenario, CI runs an auth coverage matrix (wrong password, expired and forged tokens, a token for player A used on player B's army, refresh reuse, login and command rate limits, an idempotent replay, and dev-login disabled in production mode). The run is FAIL unless that matrix is `COMPLETE` and every invariant is PASS.

| Action | Endpoint |
| --- | --- |
| Read self, cities, armies, the map, reports | `GET /v1/me`, `/v1/me/cities`, `/v1/me/armies`, `/v1/map/cities`, `/v1/me/reports` |
| Attack | `POST /v1/commands/attack` |
| Reinforce another city you own | `POST /v1/commands/move` |
| Recall | `POST /v1/commands/recall` |
| Build | `POST /v1/commands/build` |
| Research | `POST /v1/commands/research` |

Profiles, assigned by player order (not by the seed):

| Profile | Behaviour |
| --- | --- |
| aggressive | Attacks an enemy city while the army is garrisoned |
| defensive | Moves to the other own city (that is the reinforce/garrison action), then recalls home. Builds when the army is already marching |
| random | Picks uniformly from the legal attacks, moves, recalls, builds, and research |

There is no endpoint to recruit units, found a city, issue a separate garrison order, or transfer resources. Those are listed under **Actions with no endpoint** in every report. A destroyed army stays destroyed.

The same `--seed` produces the same command sequence. The seed drives target choice and the random profile. It does not change the clock epoch or the profile assignment. CI also draws trace ids from that seed inside the simulator process, and sets the worker id to `simulator`, because the world snapshot checksum includes both. Staging talks to a server you already started, so it does not patch trace ids and does not claim the checksum will match a second run.

## Install

From the repository root, with PostgreSQL 16:

```bash
pip install -e ".[dev]"
```

The admin token is read from `SIMCORE_ADMIN_TOKEN` at runtime. The simulator does not contain a token. The database URL is `SIMCORE_DATABASE_URL` or `--database-url`.

## CI mode (local)

CI mode migrates the database, starts an API on `127.0.0.1`, registers `Bot01` … over HTTP, then attaches cities and armies if the database has **no** cities, and plays. Time moves by `POST /v1/admin/clock/advance`. Due events are applied by `POST /v1/admin/worker/tick`, which is the same worker path the process already uses. The base clock is the test `FrozenClock` fixed at `2026-01-01T00:00:00Z`, so a run finishes in well under two minutes of wall time. Results are not invented: every battle and ledger row is produced by that API and that worker.

Use a fresh database whose name contains `test` or `_sim`. `simcore_test` and `simcore_sim` are the usual names. The simulator will not seed `simcore` (the docker-compose and VPS database) and it will not delete players that are already there.

```bash
createdb -h 127.0.0.1 -U simcore simcore_sim
export SIMCORE_DATABASE_URL=postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore_sim
export SIMCORE_ADMIN_TOKEN=dev-admin   # local placeholder only; set whatever this database's API expects
export SIMCORE_ENV=development

python -m simcore.sim --mode ci --players 4 --seed 8741 --ticks 4 --command-rate 1 --report-dir sim-reports/a
```

`--ticks` is the number of decision rounds. With no `--duration`, each round then advances one game hour (`3600` seconds), which is enough for the seeded marches. `--duration` is the total game seconds; together with `--ticks` it sets the step to `duration / ticks`.

A second run on another fresh database with the same seed must print the same `checksum=`.

```bash
createdb -h 127.0.0.1 -U simcore simcore_sim_b
SIMCORE_DATABASE_URL=postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore_sim_b \
  python -m simcore.sim --mode ci --players 4 --seed 8741 --ticks 4 --command-rate 1 --report-dir sim-reports/b
```

Exit code `0` is PASS, `1` is a failed check or a runtime error, `2` is a safety refusal.

## GitHub Actions

The Tests workflow has a `simulator` job on PostgreSQL 16. It runs the scenario above twice, on `simcore_sim` and `simcore_sim_b`, and fails the job if either report is not PASS or the snapshot checksums differ. The pytest job is unchanged and includes a smaller two-run check (`tests/test_simulator.py`). Reports are uploaded as the `simulator-reports` artifact.

The admin token in that job is the same development placeholder the pytest job already puts in the environment. It is not a production credential.

## Staging mode

Staging sends player commands to a server that is already running, in real time. It does not advance the clock and it does not tick the worker. Your worker has to be running. It does not seed players; `--players N` uses the first N players already in that world.

```bash
export SIMCORE_ADMIN_TOKEN=...          # the token for that server, from the environment, not from this repo
export SIMCORE_DATABASE_URL=...         # read-only checks. Use a credential that can SELECT.
python -m simcore.sim \
  --mode staging \
  --base-url http://127.0.0.1:8741 \
  --players 2 \
  --seed 8741 \
  --ticks 4 \
  --duration 60 \
  --command-rate 1 \
  --report-dir sim-reports/staging
```

`--duration` here is wall-clock seconds for the whole run, split across the ticks. Marches that have not arrived stay `INCOMPLETE`. That is allowed only while the trace still has a pending event or an in-progress movement. `FAIL` is still a failed run.

Take a snapshot before you point this at any world you care about:

```bash
curl -s -X POST "$BASE/v1/admin/snapshots" \
  -H 'content-type: application/json' -H "x-admin-token: $SIMCORE_ADMIN_TOKEN" \
  -d '{"reason":"MANUAL"}'
```

The simulator will not do that for you in staging, and it will not restore one.

## Production

The run is refused when any of these are true, unless you pass `--i-understand-this-is-production`:

- `SIMCORE_ENV=production`
- the database name is `simcore`, `postgres`, `production`, `prod`, or contains `prod`
- the database host is not loopback and the name does not look like a test or sim database
- the base URL host is `157-85-96-139.sslip.io`, `157.85.96.139`, any `*.sslip.io` host, or contains `prod`

CI mode has an extra rule: the database must be on loopback and its name must contain `test` or `_sim`. CI still will not seed a live database name, even with the flag. Use staging for an existing world.

**Take a snapshot first** if you pass the flag. The flag only lifts the refusal. It does not back up the database.

## How to read the report

`report.json` is the full document. `report.md` is the same result in a table. Both are written to `--report-dir`.

| Field | Meaning |
| --- | --- |
| `result` | `PASS` or `FAIL` against the thresholds in `src/simcore/sim/thresholds.py` |
| `counts` | Commands accepted, rejected, and skipped; events; battles |
| `latency_ms` | Average and p95 of the HTTP calls this process made |
| `max_event_lag_seconds` | Largest game-time lag seen after a clock step and before the worker drained it |
| `end_event_lag_seconds` | `event_queue.lag` from `GET /v1/admin/monitoring` after the run |
| `trace_verdicts` | Counts of `PASS`, `FAIL`, `INCOMPLETE`, and `LEGACY` |
| `failed_invariants` | Each break, with `trace_id` when the server has one |
| `world_checksum` | The checksum from `POST /v1/admin/snapshots`, which is `world_checksum()` |
| `audit_chain` | `chain` from `GET /v1/admin/audit` |
| `monitoring` | Critical, unknown, and not-instrumented checks from `GET /v1/admin/monitoring` |
| `command_sequence` | The commands, in order, including skips and rejections |
| `skipped_actions` | Gameplay the API does not offer |

Thresholds, written down before the run is scored:

- any trace verdict `FAIL` fails the run
- `INCOMPLETE` fails the run when that trace has no pending event and no in-progress movement
- `LEGACY` / `NOT TRACED` is reported and is not counted as `PASS`. A player command with a null `trace_id` fails CI, because this server stamps every new command
- production and upkeep are `NOT CHECKED` on traces. The global ledger check covers them separately. `NOT CHECKED` is not `PASS`
- global ledger: city stocks equal the seeded opening plus ledger deltas; loot taken equals loot deposited plus cargo still on a return plus loot recorded as lost when it was never deposited; no resource reason outside production, upkeep, and loot
- no negative city stock or `balance_after`
- no second effect for the same event, resource, and reason; no second live event of the same type on one movement; no event left `failed` or `processing`
- army count, unit totals (alive plus battle casualties), and in-progress movements match the seed
- the audit hash chain status is `PASS`
- a monitoring check whose status is `CRITICAL` fails the run. `UNKNOWN` and `NOT INSTRUMENTED` are copied into the report under those names. Monitoring is read before the verification snapshot is written, so `game.last_snapshot` is `UNKNOWN` on a fresh database. That is not a pass and it does not fail the run
- end-of-run event lag is `0` in CI (staging allows the monitoring critical lag, 120 seconds)
- peak lag in CI may be as large as one clock step, not two
- client p95 latency at most 5000 ms, average at most 2000 ms

`GET /v1/admin/trace/{trace_id}` is what decides `PASS`, `FAIL`, and `INCOMPLETE`. The checker does not reimplement that verdict.
