# World snapshots

A snapshot freezes the simulation so it can be inspected or restored. Use it for debugging, replay, tests, and rolling the world back to a known point.

## Snapshot vs backup

| | Snapshot | Backup |
| --- | --- | --- |
| What it is | A checksummed copy of the game world inside PostgreSQL | A dump of the database (for example `pg_dump`) or disk-level / point-in-time recovery |
| What it covers | Players, cities, armies, movements, events, battle reports, the resource ledger, and the simulation clock (`offset_seconds`, `world_version`) | The whole database: roles, catalogs, every table, and anything else stored in Postgres |
| What it is for | Debug, replay, test, undo a bad stretch of play | Disaster recovery: lost disk, dropped database, corrupted cluster |
| What this repo does | `world_snapshots` + `world_snapshot_payloads`, admin HTTP API | Nothing. Production still needs its own backups. |

Restoring a snapshot does not reload Postgres. It replaces the simulation rows in the existing database. Snapshot rows themselves are kept, including the safety copy taken just before a restore.

## What is stored

Each snapshot row has:

- `snapshot_id`
- `created_at` (wall clock, UTC, when the row was written)
- `world_time` (simulated clock at capture: base clock + `offset_seconds`)
- `schema_version` (currently `2`)
- `world_version` (monotonic counter on `world_state`)
- `checksum`
- `reason`: `AUTO`, `MANUAL`, or `SAFETY`
- `status`: `CREATING`, `READY`, `FAILED`, or `RESTORING`

`MANUAL` is the default for `POST /v1/admin/snapshots`. `AUTO` is accepted for a future scheduler; nothing in this server creates `AUTO` snapshots on a timer. `SAFETY` is written only by restore.

The payload is canonical JSON in `world_snapshot_payloads.body`. It includes every column of:

- `players`
- `cities`
- `armies`
- `player_commands` (accepted commands and their `trace_id`)
- `movements` (including nullable `trace_id`)
- `events` (pending, processing, completed, failed, cancelled, including nullable `trace_id`)
- `battle_reports` (including nullable `trace_id`)
- `transactions` (including nullable `trace_id`)
- `world_state` fields `id`, `offset_seconds`, and `world_version`

It does not include other snapshot rows or the `audit_log`. It also does not include the operational gates `commands_open` and `worker_paused`. Those gates are how restore pauses the world; they are not part of the world you roll back to. `world_time` is metadata on the snapshot row. The hashed clock state is `offset_seconds`. Schema version 2 is the document that includes command traces. A snapshot written at version 1 stays listed and is not restored by this server.

Rows are ordered by primary key. A successful restore puts those primary keys back and moves each id sequence to `MAX(id)`.

`schema_version` changes only when the captured document changes shape. This server refuses to restore a different version. Old rows stay listed.

## Checksum

Create and restore call the same function, `world_checksum` in `src/simcore/snapshot.py`.

```
checksum = "sha256:" + hex(SHA-256(UTF-8 canonical JSON))
```

Canonical JSON is `json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)`.

- Object keys are sorted at every level. List order is kept (it is primary-key order for rows, and the stored order for JSON arrays such as units and battle rounds).
- Datetimes are timezone-aware UTC ISO-8601 strings with a `+00:00` offset, produced before encoding.
- Floats use Python's standard JSON encoding (shortest round-trip).
- The digest is 64 lowercase hex characters prefixed with `sha256:`.

Before restore, the server hashes the stored payload text and compares it to `checksum`. After the rows are replaced, and before that transaction commits, it captures the live world again and requires the same checksum.

## Admin API

Same admin token as the rest of `/v1/admin` (`X-Admin-Token`). No auth change.

| Action | Request |
| --- | --- |
| Create | `POST /v1/admin/snapshots` with `{"reason": "MANUAL"}`. Omit `reason` for `MANUAL`. |
| List | `GET /v1/admin/snapshots` |
| One snapshot | `GET /v1/admin/snapshots/{id}` — metadata and the counts stored at capture |
| Inspect | `GET /v1/admin/snapshots/{id}/inspect` — recomputed counts, payload checksum, `checksum_ok` |
| Restore | `POST /v1/admin/snapshots/{id}/restore` with `{"confirm": true}` |

Player routes are unchanged. While restore is in progress, player commands return `503` `maintenance`, the worker will not claim events, and the admin clock will not advance.

## Safe restore

Restore is not a single blind write.

1. **Stop player commands.** `world_state.commands_open` is set false and committed.
2. **Pause the worker.** `world_state.worker_paused` is set true. The worker holds a shared lock for the event it is currently applying. Restore takes that lock exclusively, which waits for the in-flight event, then checks that no event is left `processing`.
3. **Safety snapshot.** The current world is captured with reason `SAFETY` before the target is applied.
4. **Verify the target.** Status must be `READY`, `schema_version` must match this server, and `sha256` of the stored payload must equal `checksum`. A checksum or canonical-JSON mismatch marks the target `FAILED` and does not apply it.
5. **Restore.** In one transaction the server deletes and reinserts the covered rows, writes `offset_seconds` and `world_version`, and resets id sequences. Snapshot tables are not deleted.
6. **Integrity check.** The live checksum must equal the snapshot checksum. If it does not, the transaction rolls back.
7. **Start the worker.** `worker_paused` is cleared.
8. **Open commands.** `commands_open` is set true.

Two restores cannot run at once. `world_state.restore_active` is set for the attempt and cleared when it finishes (`restore_in_progress` if a second one starts). The worker holds a shared transaction lock while it handles an event; restore takes the exclusive lock, so it waits for that event without stopping two workers from claiming different events.

If a step fails before step 5 commits, the replacement is not visible. The server opens commands again when the live checksum still matches the safety snapshot (or when the safety snapshot was never created, because only the gates changed). If step 5 committed and step 7 or 8 fails, the gates stay closed so a half-open world is not served. The error says which of those states you are in.

There is no admin screen in this phase. If the gates stay closed and you have confirmed the live checksum is the world you intend to run:

```sql
UPDATE world_state
SET commands_open = true, worker_paused = false, restore_active = false
WHERE id = 1;
```

## Proof test

```bash
pytest tests/test_snapshots.py::test_snapshot_mutate_restore_hash_equality
```

That test builds a world, snapshots it, marches an army through a battle (movement, losses, ledger, clock), restores the snapshot, and asserts `hash(before) == hash(restored)` plus the entity counts and key fields.

## Not in this phase

- An Admin UI (Phase 3): list, inspect, create, and a restore button that sends `confirm: true`, plus a maintenance banner and the safety snapshot id.
- A timer that writes `AUTO` snapshots.
- Deleting or expiring old snapshots.
- Upgrading a snapshot from an older `schema_version`.
- `pg_dump` or any other disaster-recovery backup.
