# Monitoring

The operator page answers two questions with measurements taken on the server: is the process healthy right now, and was it healthy recently. The browser shows that payload. It does not score the checks.

`GET /health` stays `{"status":"ok"}`. It does not include queue depth, disk, or the worker. `GET /health/ready` still only checks that `SELECT 1` works. The detail is on the admin API.

## Endpoints

Both routes use the same admin auth as the rest of `/v1/admin` (session bearer or `X-Admin-Token`). They are read-only.

| Method | Path | What it returns |
| --- | --- | --- |
| GET | `/v1/admin/monitoring` | Current checks, overall status, API process note |
| GET | `/v1/admin/monitoring/history?metric=&window=` | Stored samples for one metric |

`window` is `15m`, `1h`, `6h`, `24h` (the default), or `7d`. `metric` is required. An unknown name returns an empty `points` list. The server does not fill gaps with zeros.

## States

| State | Meaning |
| --- | --- |
| OK | The check ran and the value is inside the threshold |
| WARN | The check ran and the value crossed the warn threshold |
| CRITICAL | The check ran and the value crossed the critical threshold |
| UNKNOWN | The check could not be measured. This is not a pass |
| NOT INSTRUMENTED | This server does not measure that thing |

Overall status uses the checks marked `affects_overall`. `CRITICAL` wins, then `WARN`, then `UNKNOWN`, then `OK`. `NOT INSTRUMENTED` does not by itself make the server look healthy or unhealthy. A check that is `UNKNOWN` is still shown; it is never relabeled `OK`.

Some `UNKNOWN` checks do not change overall status, because a quiet process has not had a request to time yet, or because nobody has taken a snapshot. The check itself stays `UNKNOWN`, and `overall.unknown_checks` lists it.

## Where the numbers come from

### Event queue

Read from the `events` table at request time.

| Check | Measurement |
| --- | --- |
| `event_queue.pending` | Rows with status `pending` |
| `event_queue.due` | Rows that are `pending` or `processing` and `due_at` is at or before game time |
| `event_queue.lag` | Game time minus `due_at` of the oldest of those due rows. Zero when there is no such row |
| `event_queue.oldest_due_age` | Wall clock minus that same `due_at`. This is not the same number when the game clock has an offset |
| `event_queue.failed` | Rows with status `failed` (the worker gave up after the attempt limit) |
| `event_queue.retried` | `pending` rows with `attempts > 0` (a claim was returned to the queue) |
| `event_queue.processing` | Age of the oldest committed `processing` row. A normal tick does not leave one, because claim and resolve share a transaction |
| `event_queue.processing_rate` | `worker_process_marks` with outcome `processed` over the last 1, 5, and 15 minutes, divided by the window length in minutes |

`events.processed_at` is game time (the event's `due_at`), so it is not used as a wall-clock rate. A rate of 0 means the worker recorded no completions in that window. Idle is `OK`. The rate does not affect overall status.

### Worker

After each tick the worker upserts `worker_heartbeats` in a separate transaction. The row has worker id (`hostname:pid`), pid, hostname, package version, commit if one could be read, process start, last tick time, tick duration, events handled by that tick, tick status (`processed`, `empty`, `paused`, `failed`), and the worker's SQLAlchemy pool counters when the pool exposes them.

Liveness uses the newest heartbeat's age against the wall clock:

| Age | Liveness | Status |
| --- | --- | --- |
| No row | UNKNOWN | UNKNOWN |
| Negative (tick time is ahead of this clock) | UNKNOWN | UNKNOWN |
| Up to the warn threshold (default 15s) | UP | OK |
| Past warn, up to critical (default 60s) | STALE | WARN |
| Past critical | DOWN | CRITICAL |

A restart changes the pid, so the new process is a new row. Status follows the newest row. Older rows are kept until retention, then deleted if a newer heartbeat exists. The newest row is kept even when it is old, so a dead worker stays `DOWN` instead of disappearing into `UNKNOWN`.

`worker.pool` is the utilization stored on that newest heartbeat. `database.pool` is the API process pool, measured in the process that answered the request. They are different pools.

The sampler appends an audit row when liveness changes: `monitor.worker.up` the first time a heartbeat is seen, `monitor.worker.stale`, `monitor.worker.down`, `monitor.worker.recovered`, and `monitor.worker.unknown`. The actor is `system`. The hash chain is the same one the audit tab already checks. The first observation of "no heartbeat" is stored and not audited, so a fresh database does not grow a row on every sample.

### API

A middleware in the API process records each response: status code and latency. Counters and the latency window live in memory. They reset when the API process restarts. The payload says `resets_on_restart: true`. The worker process does not copy these numbers; a zero from the worker would be a lie about the API.

The status window is the last 5 minutes (also reported: 1 minute and 15 minutes inside the process snapshot).

| Check | Measurement |
| --- | --- |
| `api.requests` | Count since process start |
| `api.uptime` | Seconds since process start |
| `api.5xx` | Responses with status >= 500 in the 5 minute window |
| `api.error_rate` | Those 5xx responses divided by requests in the window. Undefined when the window has no requests, so the check is UNKNOWN |
| `api.latency_p50` / `api.latency_p95` | Nearest-rank percentile of latencies in the window. UNKNOWN when the window is empty. Only p95 affects overall status |

### Database

| Check | Measurement |
| --- | --- |
| `database.connectivity` | `SELECT 1`. Failure is CRITICAL. The exception class name is the reason; the URL is not |
| `database.rtt` | Milliseconds around that `SELECT 1` |
| `database.size` | `pg_database_size(current_database())`, plus the five largest `public` tables by `pg_total_relation_size` |
| `database.pool` | API process `checked_out / (pool size + max overflow)` |

If the probe fails, queue, heartbeat, and game checks that need the database are UNKNOWN, not zero.

### Disk

`shutil.disk_usage` on `SIMCORE_MONITOR_DISK_PATH`, or on the drive of the process working directory when that variable is empty. On the VPS that drive is `C:\` when the service runs from `C:\simcore\app`. The check reports total, used, and free bytes. If the path is missing or `disk_usage` raises, the status is NOT INSTRUMENTED and the value is null.

### Game

| Check | Measurement |
| --- | --- |
| `game.clock` | Game time (`base clock + world_state.offset_seconds`) and the offset |
| `game.world_version` | `world_state.world_version`, including 0 |
| `game.last_snapshot` | Latest `world_snapshots` row. UNKNOWN when the table has no row. WARN when that row is `CREATING`, `FAILED`, or `RESTORING`. A world snapshot is not a database backup |
| `backup.last_success` | Age of `last_verified_at` in the status file written by `deploy/windows/backup.ps1` after a dump is verified off-site. Default file on Windows: `C:\simcore\backups\backup-status.json`. WARN when the age is greater than 26 hours, CRITICAL when it is greater than 50 hours. UNKNOWN when the file is missing, unreadable, or has never recorded a verified upload. UNKNOWN does not by itself change overall status |

### Not measured

`host.cpu` and `host.memory` are NOT INSTRUMENTED. `build.commit` is NOT INSTRUMENTED when `SIMCORE_GIT_COMMIT` is unset and `git rev-parse HEAD` does not return a SHA. The package version is still reported.

## History and retention

About every `SIMCORE_MONITOR_SAMPLE_SECONDS` (default 60) the worker loop and the API sampler write one row per metric into `monitoring_samples`, if that metric's latest row is older than 90% of the interval. The API sampler is what records `api_*` series, because those numbers exist only in the API process. The worker records queue, database, disk, and heartbeat age. A metric is skipped when its value was not measured (no fake zero).

Each sample pass deletes `monitoring_samples` and `worker_process_marks` older than `SIMCORE_MONITOR_RETENTION_DAYS` (default 7, allowed 1–30). Heartbeats older than that are deleted only when a newer heartbeat exists.

Migration `0004_monitoring` creates these tables and their indexes only:

- `worker_heartbeats`
- `worker_process_marks`
- `monitoring_samples`
- `monitoring_check_state`

It does not add columns to existing tables and it does not update existing rows. Downgrade drops only those four tables. They are not in the world snapshot document, so they do not change combat, the ledger, or snapshot checksums. `worker_process_marks.event_id` is not a foreign key; snapshot restore can replace events without touching the marks.

Set `SIMCORE_MONITOR_SAMPLE_SECONDS=0` to stop periodic samples. Heartbeats are still written on each worker tick. Set `SIMCORE_MONITOR_API_SAMPLER=false` to stop the API process sampler. The worker sampler still runs.

## Thresholds

All of these are optional. Unset means the default. For every pair except disk free, warn must be below critical (a higher measurement is worse). Disk free is the opposite: warn must be above critical, because less free space is worse. The values are documented again in `.env.prod.example`.

| Variable | Default | Check |
| --- | --- | --- |
| `SIMCORE_MONITOR_HEARTBEAT_WARN_SECONDS` | 15 | STALE |
| `SIMCORE_MONITOR_HEARTBEAT_CRITICAL_SECONDS` | 60 | DOWN |
| `SIMCORE_MONITOR_EVENT_LAG_WARN_SECONDS` | 15 | game-time lag |
| `SIMCORE_MONITOR_EVENT_LAG_CRITICAL_SECONDS` | 120 | game-time lag |
| `SIMCORE_MONITOR_OLDEST_DUE_WARN_SECONDS` | 15 | wall-clock due age |
| `SIMCORE_MONITOR_OLDEST_DUE_CRITICAL_SECONDS` | 120 | wall-clock due age |
| `SIMCORE_MONITOR_QUEUE_PENDING_WARN` | 500 | pending rows |
| `SIMCORE_MONITOR_QUEUE_PENDING_CRITICAL` | 5000 | pending rows |
| `SIMCORE_MONITOR_QUEUE_DUE_WARN` | 25 | due unprocessed |
| `SIMCORE_MONITOR_QUEUE_DUE_CRITICAL` | 200 | due unprocessed |
| `SIMCORE_MONITOR_FAILED_WARN` | 1 | failed rows |
| `SIMCORE_MONITOR_FAILED_CRITICAL` | 10 | failed rows |
| `SIMCORE_MONITOR_RETRIED_WARN` | 1 | pending with attempts |
| `SIMCORE_MONITOR_RETRIED_CRITICAL` | 20 | pending with attempts |
| `SIMCORE_MONITOR_PROCESSING_WARN_SECONDS` | 30 | stuck processing |
| `SIMCORE_MONITOR_PROCESSING_CRITICAL_SECONDS` | 120 | stuck processing |
| `SIMCORE_MONITOR_API_ERROR_RATE_WARN` | 0.01 | 5 minute 5xx rate |
| `SIMCORE_MONITOR_API_ERROR_RATE_CRITICAL` | 0.05 | 5 minute 5xx rate |
| `SIMCORE_MONITOR_API_5XX_WARN` | 1 | 5xx count |
| `SIMCORE_MONITOR_API_5XX_CRITICAL` | 10 | 5xx count |
| `SIMCORE_MONITOR_API_P95_WARN_MS` | 300 | p95 latency |
| `SIMCORE_MONITOR_API_P95_CRITICAL_MS` | 1000 | p95 latency |
| `SIMCORE_MONITOR_DB_RTT_WARN_MS` | 50 | `SELECT 1` |
| `SIMCORE_MONITOR_DB_RTT_CRITICAL_MS` | 200 | `SELECT 1` |
| `SIMCORE_MONITOR_DB_POOL_WARN` | 0.8 | pool utilization |
| `SIMCORE_MONITOR_DB_POOL_CRITICAL` | 0.95 | pool utilization |
| `SIMCORE_MONITOR_DB_SIZE_WARN_MB` | 10240 | database size |
| `SIMCORE_MONITOR_DB_SIZE_CRITICAL_MB` | 40960 | database size |
| `SIMCORE_MONITOR_DISK_FREE_WARN_MB` | 5120 | free bytes |
| `SIMCORE_MONITOR_DISK_FREE_CRITICAL_MB` | 2048 | free bytes |
| `SIMCORE_BACKUP_WARN_HOURS` | 26 | age of last verified off-site backup |
| `SIMCORE_BACKUP_CRITICAL_HOURS` | 50 | age of last verified off-site backup |

A bad value (warn above critical, retention outside 1–30, sample interval other than 0 or 10–3600) stops the process at startup with a validation error. Nothing is invented in its place.

## What to do

### WARN or CRITICAL on the worker

`STALE` means ticks are late. `DOWN` means the last tick is older than the critical age, or there is no recent tick.

1. On the VPS, check the service: `Get-Service simcore-worker`.
2. Read `C:\simcore\logs` for the worker. A paused worker (snapshot restore) heartbeats with tick status `paused` and sleeps the poll interval; a long pause is the restore, not a crash.
3. `deploy/windows/update.ps1` stops the stack, migrates, and starts `simcore-api` and `simcore-worker` again. Heartbeats start when that worker process is running. Back up `C:\simcore\app\.env.prod` before you run it. The script reads that file and does not rotate the secrets.

`UNKNOWN` with no heartbeat row means this database has not seen a worker tick yet (or the probe could not read the table). Do not treat that as up.

### WARN or CRITICAL on lag, due count, or pending

The worker is behind the game clock, or events are due and not completed.

1. Confirm the worker is `UP`. A `DOWN` worker explains the lag.
2. Open the Events tab and filter `failed`. One failed row is already WARN. Read `last_error` on that event. The row stays failed so it does not block the queue; it will not clear itself.
3. Pending above the warn threshold (default 500) means a backlog that is not necessarily due yet. Due count and lag are the "late" signals.

### WARN or CRITICAL on API 5xx or latency

These numbers belong to the API process that served the monitoring request, since that process started. A restart clears them, which is why a restart can look healthy even if the previous process was not.

1. Read the API log in `C:\simcore\logs`.
2. If the database checks are also bad, fix connectivity before chasing the API.
3. p95 UNKNOWN with no requests is an empty window, not a fast API.

### WARN or CRITICAL on the database

`database.connectivity` CRITICAL means `SELECT 1` failed. The reason is the exception class, not the URL.

1. Check the PostgreSQL Windows service and that it is still listening on localhost only.
2. Round trip above the critical threshold (default 200 ms) on an idle localhost database is a stuck disk or an overloaded host.
3. Pool utilization near 1 means this process is out of connections. The API pool and the worker pool are separate; read both checks.

### WARN or CRITICAL on disk

Free space at or below 5 GiB is WARN. At or below 2 GiB is CRITICAL. This host has been tight on disk before. Snapshots, logs, and PostgreSQL all sit on that drive unless you set `SIMCORE_MONITOR_DISK_PATH` to the data volume.

1. Check `C:\simcore\logs` and old PostgreSQL logs.
2. Monitoring retention is what keeps `monitoring_samples` from growing without a limit. Do not raise retention on a small disk without looking at free space.
3. World snapshots are not deleted by monitoring. Pruning those is a separate operator decision; this page does not delete them.

### WARN or CRITICAL on backup.last_success

This is the age of the last dump that `deploy/windows/backup.ps1` verified on Google Drive. It is not `game.last_snapshot`.

1. Read `C:\simcore\logs\backup.log`. The log does not contain the database password or the Drive token.
2. A WARN above 26 hours means today's run did not finish a verified upload. A CRITICAL above 50 hours means more than one daily run was missed.
3. UNKNOWN means the status file has never recorded a verified upload. Run the setup in [BACKUP_DR.md](BACKUP_DR.md), then run `deploy/windows/backup.ps1` once by hand.
4. The C: drive is small. If the log says the disk check refused the dump, free space before the next run. The script does not delete older local dumps to make room for a dump that has not been uploaded yet.

### NOT INSTRUMENTED

CPU, memory, a commit SHA the process could not read, or a disk path that does not exist. Set `SIMCORE_GIT_COMMIT` if the service host has no `git` command and you want the SHA recorded. Set `SIMCORE_MONITOR_DISK_PATH` if the default drive is the wrong volume. Leaving them unset is valid; the UI shows NOT INSTRUMENTED rather than a guessed number.
