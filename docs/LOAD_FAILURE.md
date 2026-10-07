# Load and failure tests

`python -m simcore.load` registers players through `POST /v1/auth/register`. The server grants the home, the army, and the starting resources in that call. The tool then founds a camp with `POST /v1/commands/found-city` so transfers have a second city. It does not insert cities or armies. In local mode it also kills and restarts the processes it started and checks the world after each failure.

Nothing in the report is guessed. A latency, a pool figure, or a lag that this run did not measure is `UNKNOWN` or `NOT INSTRUMENTED`.

There is no new database migration. Nothing new is required on the VPS. The tool is not a WinSW service.

## What a passing run proves

Each failure scenario is followed by the same read-only checks. A check is `PASS`, `FAIL`, or `INCOMPLETE`. `INCOMPLETE` is not a pass. Any required `FAIL` makes the run `FAIL`. Any other required status that is not `PASS` makes the run `INCOMPLETE`.

| Check | What it proves |
| --- | --- |
| `ledger_conservation` | City wood, food, iron, and gold match the ledger. Building levels match `building:<name>` rows. Research levels match `research:<name>` rows. `unit:<type>` rows record an army count and are not compared to a city column. Any other resource name fails. |
| `events_exactly_once` | No event is due, `processing`, or `failed`. No event idempotency key is duplicated. No event has two `processed` worker marks. |
| `battle_reports_unique` | No event has two battle reports. |
| `army_one_place` | No army has two in-progress movements, and a garrisoned army is not also marching. |
| `world_checksum` | Two reads of `world_checksum()` in one transaction match. |
| `audit_hash_chain` | `verify_chain()` reports `PASS`. |
| `world_gates` | Commands are open and the worker is not left paused. |
| `negative_resources` | No city stock is negative. |
| `concurrent_arrivals` | One clock advance makes at least four already-scheduled events due together. The spread of their `due_at` values is reported as measured. The tool does not rewrite `due_at`. |
| `kill_worker_mid_batch` | The worker was `SIGKILL`ed after it had completed at least one new event while another was still pending or processing, then a new worker drained the queue. If the batch finished before that overlap was observed, the check is `INCOMPLETE` (and the run is not a pass). |
| `two_workers` | Two worker processes heartbeated and the queue drained. Exactly-once is the invariant above, not a requirement that both workers took a share. |
| `api_restart` | The API was `SIGKILL`ed during in-flight calls. Replaying the same `Idempotency-Key` twice after restart returned the same status and body. |
| `postgres_connection_drop` | Other client backends were terminated with `pg_terminate_backend`. `/health/ready` returned 200 afterward and due events still drained. |
| `postgres_restart` | PostgreSQL was restarted with a real command (`docker restart` or `pg_ctlcluster`). The API process stayed up and `/health/ready` returned 200. If the worker process exited, the check is `FAIL`. |
| `idempotency_replay` | The same key sent twice in sequence, and eight concurrent calls with one key, each returned one shared body and did not apply the build twice. |
| `refresh_reuse_race` | Eight concurrent refreshes of one refresh token produced one 200 and seven 401s, and the winner's new refresh token was then 401. |
| `clock_advance_during_processing` | The game clock was advanced 60 seconds while the worker still had pending events, and the queue then drained. |
| `snapshot_during_load` | `POST /v1/admin/snapshots` completed while map and profile reads were in flight, and inspect reported `checksum_ok`. |
| `command_mix` | Every listed read and command route was called: time, me, cities, armies, reports, map, move, attack, recall, build, research, train, found-city, garrison, transfer. |
| `load_http` | Scripted and load-phase calls had no HTTP 5xx and no connection error. Game-rule rejections (409 and similar) are counted in the error table and do not fail this check. |
| `failure_http_5xx` | Failure-phase calls had no HTTP 5xx. Connection errors while a process was killed are expected and are counted, not treated as a pass of the request. |

Latency percentiles are reported for the load phase only. They are not a pass/fail threshold. GitHub-hosted runners vary, and a tight latency limit would fail for reasons that are not correctness.

The child processes this tool starts use `SIMCORE_COMMAND_RATE_LIMIT=1000` so the run measures command handling. That value is set only in the child environment. It is not written to a file. Production stays at its own limit (30 per 60 seconds unless the VPS config says otherwise).

## How to run

From the repository root, with PostgreSQL 16 already running and a database whose name contains `test` or `_sim`:

```bash
export SIMCORE_ENV=development
export SIMCORE_ADMIN_TOKEN=dev-admin
export SIMCORE_DATABASE_URL=postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore_load_test
python -m simcore.load --profile ci --report-dir load-reports
```

`--profile ci` is 4 players, 8 requests/second, 5 seconds, concurrency 4, seed 8741, failures on. `--profile long` is 12 players, 20 requests/second, 30 seconds, concurrency 8, the same seed, failures on. Flags after the profile replace one field.

The database must be empty of players. The tool migrates it and then refuses to continue if any player row exists. It does not delete or truncate.

Exit codes: `0` pass, `1` fail or incomplete, `2` the safety gate refused the target.

Reports: `load-reports/report.json` and `load-reports/report.md`. Every check has a status. Host CPU count and memory are measured in that report.

Local mode starts `uvicorn` and `python -m simcore.worker` itself, on 127.0.0.1, and `SIGKILL`s them. It will not do that to a process it did not start.

PostgreSQL restart uses, in order: `SIMCORE_LOAD_PG_RESTART_CMD` if set, otherwise `docker restart` of a container publishing port 5432, otherwise `sudo -n pg_ctlcluster` for the online cluster on port 5432, otherwise `sudo -n systemctl restart postgresql`. If none of those work, `postgres_restart` is `INCOMPLETE`.

### CI

Every pull request and every push to `main` runs the `load-failure` job in `.github/workflows/test.yml`. It starts `postgres:16` in Docker (database `simcore_load_test`), then runs `--profile ci`. The job uploads `report.json` and `report.md` as the `load-failure-report` artifact even when the run fails.

`.github/workflows/load-long.yml` is `workflow_dispatch` only. It runs `--profile long` and uploads `load-long-report`. It does not run on every pull request.

### External load

`--mode external --base-url http://127.0.0.1:PORT --no-failures` sends the same mixed traffic to a server that is already running. It does not kill processes. A URL that is not loopback, or any URL the simulator already treats as production, is refused unless both of these are true:

- `--i-understand-this-is-production` is present
- the rate is at most 2 requests/second, concurrency at most 2, duration at most 30 seconds, and players at most 2

The flag does not raise that cap, and it does not enable failure injection.

### Optional read-only probe

This is the only client in this tool that is documented for the public game host, and it is still optional. It sends two GETs and cannot be turned up:

```bash
python -m simcore.load probe \
  --base-url https://157-85-96-139.sslip.io \
  --i-understand-this-is-production
```

Those two requests are `GET /health` and `GET /health/ready`. There is no register, no command, and no database call. A non-localhost host still needs the flag. This repository's agent runs do not execute that probe.

## Measured results

Numbers below are copied from a report this tool wrote. They are not estimates.

### CI hardware is not the VPS

The pull-request job runs on a GitHub-hosted `ubuntu-latest` runner. The production server is a Windows VPS. The disk there is 64 GB and, as recorded in [BACKUP_DR.md](BACKUP_DR.md), free space has been about 2.26 GB. A load test writes players, events, logs, and a snapshot. Do not run `--profile ci`, `--profile long`, or any failure injection against `https://157-85-96-139.sslip.io`. Correctness checks (ledger, exactly-once events, battle reports, army placement, checksum, audit chain) do not depend on the runner's CPU. Latency, throughput, pool pressure, and CPU do. A p95 measured on the GitHub runner is not a prediction of the VPS.

The runner specs in the CI subsection are the `host` object from that job's `report.json` (CPU count and total memory). They are not taken from the `ubuntu-latest` name alone.

### GitHub Actions `load-failure`

Headline numbers are copied from `load-failure-report` on workflow run [37662605881](https://github.com/nustanakritwithai/Server-Manager-/actions/runs/37662605881), the Tests run for pull request head `8b998c5b787dfc79cd026e48699d141e478f28df`. `actions/checkout` on a pull request checks out the merge ref, so `report.json` `git_commit` is `17dd0a29949a0458d171204a6ec20f86479ed265`. Result `PASS`, 106 of 106 checks, wall time 15.018 seconds.

Runner, from that report's `host` object: `runner_os` Linux, `runner_name` GitHub Actions 1000010879, platform `Linux-6.17.0-1022-azure-x86_64-with-glibc2.39`, Python 3.12.14, `cpu_count` 4, memory 16765374464 bytes. This is a GitHub-hosted Linux VM. It is not the Windows VPS.

Load phase: 40 requests, target 8/s, achieved 7.999 requests/second over 5.001 seconds. All-route latency p50 15.912 ms, p95 38.512 ms, p99 44.395 ms (full values in the artifact are 15.911832000000459, 38.51244800000586, and 44.39489799999308). Errors by code: `http_200` 36, `conflict` 4. HTTP 5xx: 0. Connection errors: 0.

Load-phase `POST /v1/commands/attack` and `POST /v1/commands/garrison` were not drawn (n=0), so their load-phase latency is UNKNOWN. Scripted phase did call them: attack n=4, p50 17.234 ms, p95/p99 118.194 ms; garrison n=1, 10.536 ms.

| Series | Status | What it is |
| --- | --- | --- |
| Steady `event_queue.lag` | MEASURED, n=18, p50/p95/p99/max 0 s | Game time minus the oldest due event while the admin offset was not changing, and not while a clock-advance backlog was still positive. |
| `event_queue.lag` across an admin clock advance | MEASURED, n=6, p50 4681.551 s, p95/p99/max 5371.139 s | The same gauge after `POST /v1/admin/clock/advance`, until that backlog returned to zero. This is the jump, in game seconds. It is not wall-clock worker delay, and it is not included in the steady percentiles. |
| `processed_at - due_at` | MEASURED, n=67, p50/p95/p99/max 0 s | Stored game timestamp. The worker sets `processed_at` to `due_at`. |
| `wall_at - due_at` | NOT INSTRUMENTED | `offset_seconds` was 12657, so wall time and game time are not comparable. |

4 monitoring samples did not include both a lag value and an offset, so they are in neither series.

API pool utilization: n=24, p50 0.067, p95 0.133, p99/max 0.467. Worker pool utilization: n=20, p50/p95/p99/max 0. `pg_stat_activity` backends: n=27, p50 7, p95 8, max 8.

Host CPU percent: n=28, p50 27.8, p95 53.7, max 57.0. Host memory percent: n=28, p50 8.4, p95 8.9, max 8.9. Child API+worker CPU percent: n=26, p50 22.8, p95 109.3, max 186.4. Child RSS bytes: n=26, p50 157995008, p95 181006336, max 257478656.

An earlier artifact, run [37661993726](https://github.com/nustanakritwithai/Server-Manager-/actions/runs/37661993726) for head `f6eb137`, stored one mixed `event_queue.lag` series: n=23, p50 0 s, p95 4681.504 s, p99 5370.669 s. That p99 mixes the clock jump into the same percentiles. It is not the steady lag. That run's host had 4 CPUs and 16766414848 bytes of memory (`runner_name` GitHub Actions 1000010876). Load-phase p50/p95/p99 there were 11.596 / 36.426 / 45.510 ms.

### Local rehearsal

This is one local run of `--profile ci` on the development VM, not the GitHub runner and not the VPS. Result `PASS`, 106 of 106 checks, wall time 14.231 seconds. Host: Linux, Python 3.12.3, `cpu_count` 4, memory 16791945216 bytes. Seed 8741.

Load phase (40 requests, target 8/s, achieved 7.999 requests/second over 5.001 seconds):

| | p50 | p95 | p99 |
| --- | --- | --- | --- |
| All load-phase calls | 12.805 ms | 22.753 ms | 40.708 ms |

Load-phase errors by code: `http_200` 36, `conflict` 4. HTTP 5xx: 0. Connection errors: 0.

Load-phase attack and garrison were not drawn by the random mix in this run, so their load-phase latency is UNKNOWN. They were called in the scripted phase (attack n=4, p50 11.242 ms, p95/p99 75.061 ms; garrison n=1, 8.755 ms). A sample of one is the whole measurement.

Worker lag during the whole run, sampled about every 0.4s (25 samples, 3 misses):

| Series | Status | n | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- | --- |
| `processed_at - due_at` (game time stored on the event) | MEASURED | 67 | 0 s | 0 s | 0 s | 0 s |
| `event_queue.lag` from monitoring, one mixed series | MEASURED | 25 | 0 s | 4681.068 s | 5370.658 s | 5370.658 s |
| `wall_at - due_at` | NOT INSTRUMENTED | | | | | |

The mixed `event_queue.lag` p99 is samples taken while an admin clock advance still had events overdue. It is the jump, measured in game seconds, not wall-clock worker delay. A later local run of the split report, on this same VM, measured steady-clock `event_queue.lag` at n=20, p50/p95/p99/max 0 s, and the clock-advance series at n=5, p50 4678.751 s, p95/p99/max 5370.664 s. That second run is still this VM, not GitHub. Lag that grows while `offset_seconds` stays constant remains in the steady series. `wall_at - due_at` is NOT INSTRUMENTED because `offset_seconds` was 12658 after those advances.

API pool utilization (`database.pool`): n=25, p50 0.067, p95 0.133, max 0.133. Worker pool utilization: n=22, p50 0, p95 0, max 0. `pg_stat_activity` backends: n=27, p50 6, p95 8, max 8. That backend count is not pool utilization.

Host CPU percent: n=28, p50 8.7, p95 34.9, max 50.0. Host memory percent: n=28, p50 70.3, p95 70.4, max 70.7. Child API+worker CPU percent: n=26, p50 14.2, p95 99.4, max 172.0 (more than 100 because it sums processes). Child RSS bytes: n=26, p50 178712576, p95 183353344, max 258633728.

## Server bug fixed here

`ledger_failures` treated `unit:`, `building:`, and `research:` ledger rows as unknown resources. Those rows are written when training, building, and research complete. A restore drill on a world that had done any of those would report a broken ledger even when city stocks matched. The check now compares wood, food, iron, and gold to the city columns, building rows to `cities.buildings`, and research rows to `players.research`. Unit rows stay in the ledger as an army count and are not compared to a city column. A resource name outside those forms still fails. No migration.

## ภาษาไทย

เครื่องมือ `python -m simcore.load` สมัครผู้เล่นผ่าน `POST /v1/auth/register` จริง แล้วส่งคำสั่งปนกัน (เดิน โจมตี เรียกกลับ สร้างอาคาร วิจัย ฝึกหน่วย สร้างเมือง ตั้งกองรักษา ส่งทรัพยากร และอ่านแผนที่กับข้อมูลผู้เล่น) โหมด local จะเริ่ม API กับ worker เอง แล้วฆ่าโปรเซสที่มันเริ่ม เพื่อตรวจว่าโลกยังถูกต้องหลังคอมโพเนนต์ล้ม

ตัวเลขที่ไม่ได้วัดจะเป็น `UNKNOWN` หรือ `NOT INSTRUMENTED` ไม่มีการประมาณ

ไม่มีการเพิ่ม migration ไม่ต้องเปลี่ยนอะไรบน VPS และเครื่องมือนี้ไม่ใช่เซอร์วิส WinSW

การรันที่ผ่านต้องได้ `PASS` ทุกข้อที่บังคับ ข้อ `INCOMPLETE` ไม่นับว่าผ่าน สิ่งที่แต่ละข้อพิสูจน์:

- บัญชีทรัพยากรตรงกับคลัง (`ledger`)
- อีเวนต์ที่ถึงกำหนดถูกประมวลผลครั้งเดียว ไม่มีรายงานรบซ้ำ และไม่มีกองทัพอยู่สองที่
- เช็กซัมของโลกอ่านสองครั้งแล้วตรงกัน และห่วงแฮชของ audit ยังครบ
- มีอีเวนต์หลายรายการถึงกำหนดพร้อมกันหลังเลื่อนนาฬิกาครั้งเดียว
- ฆ่า worker กลางชุดแล้วเริ่มใหม่ แล้วคิวหมด
- รัน worker สองตัวพร้อมกันโดยอีเวนต์ไม่ถูกทำสองครั้ง
- รีสตาร์ท API กลางคำขอ แล้วส่ง `Idempotency-Key` เดิมได้ผลเดียวกัน
- ตัดการเชื่อมต่อ PostgreSQL แล้วรีสตาร์ททั้งตัวเซิร์ฟเวอร์ฐานข้อมูล
- ลอง `Idempotency-Key` เดิมทั้งแบบทีละครั้งและแปดเธรดพร้อมกัน
- แย่งกันใช้ refresh token เดิม แล้วโทเคนที่ชนะก็ใช้ต่อไม่ได้
- เลื่อนนาฬิกาขณะ worker ยังทำงาน และสร้าง snapshot ขณะมีคนอ่านแผนที่

วิธีรันจากรากของรีโป (ฐานข้อมูลต้องว่าง และชื่อต้องมี `test` หรือ `_sim`):

```bash
export SIMCORE_ENV=development
export SIMCORE_ADMIN_TOKEN=dev-admin
export SIMCORE_DATABASE_URL=postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore_load_test
python -m simcore.load --profile ci --report-dir load-reports
```

โปรไฟล์ `ci` คือผู้เล่น 4 คน 8 คำขอต่อวินาที นาน 5 วินาที ทำงานพร้อมกัน 4 งาน ซีด 8741 พร้อมการทดสอบความล้มเหลว โปรไฟล์ `long` คือผู้เล่น 12 คน 20 คำขอต่อวินาที นาน 30 วินาที ทำงานพร้อมกัน 8 งาน ซีดเดิม ใช้กับ `workflow_dispatch` ไม่ได้รันทุก pull request

งาน `load-failure` ใน GitHub Actions รันโปรไฟล์ `ci` ทุก pull request แล้วอัปโหลด `report.json` กับ `report.md`

ห้ามยิงโหลดหรือการจำลองความล้มเหลวไปที่ `https://157-85-96-139.sslip.io` เครื่อง CI เป็น Linux ของ GitHub ไม่ใช่ VPS Windows ดิสก์ของ VPS ขนาด 64 GB และเหลือที่ว่างน้อย (เคยวัดได้ประมาณ 2.26 GB ตามเอกสารสำรองข้อมูล) ความถูกต้องของกฎเกมย้ายตามโค้ดได้ แต่ค่าหน่วงเวลาและความเร็วที่วัดบน CI ไม่ได้บอกความเร็วบน VPS

โพรบที่อนุญาตสำหรับโฮสต์สาธารณะมีเพียงสองคำขอ และต้องใส่แฟล็กยืนยัน:

```bash
python -m simcore.load probe \
  --base-url https://157-85-96-139.sslip.io \
  --i-understand-this-is-production
```

คำขอนั้นคือ `GET /health` และ `GET /health/ready` เท่านั้น

ตัวเลขหลักในหัวข้อ GitHub Actions คัดลอกจาก artifact ของ run 37662605881 (หัว `8b998c5`) บน runner Linux 4 CPU หน่วยความจำ 16765374464 ไบต์ ไม่ใช่ VPS ช่วงที่นาฬิกาคงที่ `event_queue.lag` เป็น 0 วินาที (n=18) ช่วงเลื่อนนาฬิกาแยกไว้ p99 5371.139 วินาที ซึ่งเป็นขนาดการกระโดดของเวลาเกม ไม่ใช่เวลาที่ worker ช้าบนนาฬิกาจริง ถ้า worker ช้าขณะ offset ไม่เปลี่ยน ค่านั้นยังอยู่ในชุดปกติ ตัวเลข Local rehearsal เป็นเครื่องพัฒนา ไม่ใช่ CI

ช่องโหว่ที่แก้ในชุดนี้: `ledger_failures` เคยมองแถว `unit:` `building:` และ `research:` ว่าเป็นทรัพยากรที่ไม่รู้จัก ทั้งที่เซิร์ฟเวอร์เขียนแถวพวกนั้นเมื่อฝึกหน่วย สร้างอาคาร และวิจัยเสร็จ การตรวจสำรองข้อมูลจึงล้มบนโลกที่ปกติ ตอนนี้เทียบไม้ อาหาร เหล็ก และทองกับคอลัมน์เมือง เทียบอาคารกับ `cities.buildings` และเทียบการวิจัยกับ `players.research` แถวหน่วยทหารเป็นจำนวนในกองทัพ ไม่ได้เทียบกับคลังเมือง ไม่มี migration
