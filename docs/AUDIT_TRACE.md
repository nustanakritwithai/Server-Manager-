# Audit log and event trace

Phase 4 records two different things.

- An **event trace** follows one accepted player command through the movements, events, battle, ledger rows, and report that came from it, including the walk home.
- An **audit log** records admin and system actions, plus player-auth events (`auth.register`, `auth.login`, `auth.lockout`, `auth.refresh_reuse`, `auth.logout_all`, `auth.dev_login` when production refuses it, and admin account lock, unlock, revoke, and temporary password). It is append-only and hash-chained. It is not part of the world snapshot. Passwords, password hashes, access tokens, and refresh tokens are not written into it.

The server computes both. The admin page only displays the JSON.

## What is traced

Every accepted command (`attack`, `move`, `recall`, `garrison`, `train_units`, `found_city`, `transfer_resources`, `build`, `research`) gets a new `trace_id` (a UUID). That id is copied onto:

- the `player_commands` row
- the movement and the event the command schedules
- a follow-on return movement and its `ARMY_RETURN` event
- the battle report
- ledger rows the command itself applies (`loot_lost`, `loot_gained`, `train`, `found_city`, `transfer_out`, `transfer_in`, build and research effects)
- production and upkeep rows written while that command is accruing a city

A city read that accrues with no command still stores a null `trace_id`. Those rows are not attached to a later command. When a trace has no production or upkeep rows, `production_upkeep` is **NOT CHECKED**. It is not PASS. When the rows are present, the check recomputes `rate * elapsed_seconds // 3600` from the idempotency key (and catalog upkeep from the stored garrison composition) and returns PASS or FAIL. A legacy key that does not record the rate or the composition stays NOT CHECKED unless the row itself contradicts a value that can be recomputed, in which case it is FAIL.

Rows that already existed before this migration, and any later row whose `trace_id` is null, are **LEGACY / NOT TRACED**. The search API does not invent a link for them. Looking up a legacy event does not attach it to some other command that happens to share an army.

## Verdicts

`GET /v1/admin/trace/{trace_id}` returns one verdict for the trace:

| Verdict | Meaning |
| --- | --- |
| `PASS` | Every check that was actually run passed, and nothing on the trace is still open. |
| `FAIL` | At least one check found a specific problem. The `reasons` list says which. |
| `INCOMPLETE` | The command is still in progress (for example the army is en route). This is never `PASS`. |

Checks that were not run are listed under `integrity.not_checked` with status `NOT CHECKED`. They are not given `PASS`.

The checks that do run:

- **ledger_conservation.** For each of wood, food, iron, and gold on this trace, resources out equal resources in plus recorded losses. "Out" is the sum of negative deltas. "In" is the sum of positive deltas. Recorded losses are the battle report's loot when the army was destroyed before it could deposit that loot. A hole in the ledger is not relabeled as a loss. While the march is still open the check stays `INCOMPLETE` unless the rows already contradict the report.
- **missing_links.** A battle report must point at an event and a movement in the trace. A traced transaction must point at an event in the trace. A return movement's `cause_event_id` must be an event in the trace. A completed build or research event must have its one effect row.
- **duplicate_processing.** The same event, city, resource, and reason must not appear twice. Two cities accrued by one transfer are not a duplicate. The same movement must not have two live events of the same type. One event must not have two battle reports.
- **production_upkeep.** Scored only for accrual rows on this trace, using the rate, window, and garrison composition stored on the ledger key.
- **army_resolution.** An army that departed must still be en route (`INCOMPLETE`), or have returned, died, or arrived. A cancelled march is finished. Build and research do not march an army, so this check is `NOT CHECKED` for them.

The timeline is ordered by game time. When two steps share a timestamp, the order is command, outbound movement, arrival event, battle, loot taken, report, return departure, return arrival, loot deposited.

## HTTP API

Same admin auth as the other `/v1/admin` routes: a bearer session from `POST /v1/admin/login`, or `X-Admin-Token`.

| Request | What it returns |
| --- | --- |
| `GET /v1/admin/trace/{trace_id}` | The command, the ordered steps, the verdict, the checks, and `not_checked`. `404` if that id is not on any row. |
| `GET /v1/admin/trace?player=&army=&event=&command=` | Matching traces, paginated with `limit` and `offset`. An entry point whose `trace_id` is null is returned under `legacy` as `LEGACY` / `NOT TRACED` and is not joined to another trace. |
| `GET /v1/admin/audit?actor=&action=&result=&target=` | One page of the audit log, plus `chain`, which is the verification of the **whole** log, not just the page. |

There is no create, update, or delete route for the audit log or for a trace. The admin UI does not add one.

### Audit row

| Field | Stored value |
| --- | --- |
| actor | `admin` for the static token, `session:<jti>` for a signed-in session, or `system` |
| action | For example `admin.login`, `admin.logout`, `admin.sessions.revoke`, `snapshot.create`, `snapshot.inspect`, `snapshot.restore`, `clock.advance`, `event.run`, `worker.tick` |
| target | The object the action named, such as `snapshot:3` or `clock` |
| timestamp | `occurred_at`, UTC |
| source_ip | The client address the API already uses for login limits |
| result | `success` or `failure` |
| reason | Short text when there is something to say. Empty when there is not |

The password, the bearer token, the session secret, and the password hash are not written. A value that contains a `simadm1.` token or a `scrypt$` hash is refused.

### Hash chain

Each row stores `prev_hash` and `row_hash`.

```
content   = canonical JSON of id, actor, action, target, occurred_at, source_ip, result, reason
row_hash  = hex(sha256(prev_hash + content))
prev_hash = previous row's row_hash, or 64 zero digits for the first row
```

Canonical JSON is the same function the world snapshot uses: UTF-8, sorted keys, no extra whitespace. `occurred_at` is a UTC ISO-8601 string.

`chain.status` is `PASS` only after every row has been recomputed. An empty log has `checked_rows: 0` and `PASS`, because the check ran and found no break. A changed column, a replaced `row_hash`, or a `prev_hash` that no longer matches the previous row makes `chain.status` `FAIL` and lists the row ids.

To verify from the API:

```bash
curl -s http://127.0.0.1:8741/v1/admin/audit -H 'x-admin-token: dev-admin'
```

Read `chain.status`, `chain.checked_rows`, and `chain.reasons`. Do not trust a page of rows by itself. The chain field covers the full table.

## Migration 0003

`0003_audit_trace` is additive:

- nullable `trace_id` on `movements`, `events`, `battle_reports`, and `transactions`, plus an index on each
- new `player_commands` table
- new `audit_log` table

Existing rows are not rewritten. The new columns have no server default, so they stay null. Downgrade drops only those columns, indexes, and the two new tables.

World snapshots use schema version **2**. The captured document now includes `player_commands` and the `trace_id` columns. The audit log is not in the snapshot: restoring a world must not erase the record that the restore happened. Older snapshots stay listed and are refused on restore, the same rule as any other schema version change.

## Admin page

The control center has an **Event trace** view and an **Audit log** tab. Both call the routes above. A trace id, or a link from an event, army, movement, or report, opens the timeline with the verdict at the top. A null `trace_id` is shown as `LEGACY / NOT TRACED`.
