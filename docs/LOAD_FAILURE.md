# Load and failure tests

`python -m simcore.load` registers players through `POST /v1/auth/register`, attaches a home, a camp, and an army on a fresh local database, then sends mixed reads and commands. In local mode it also kills and restarts the processes it started and checks the world after each failure.

Nothing in the report is guessed. A latency, a pool figure, or a lag that this run did not measure is `UNKNOWN` or `NOT INSTRUMENTED`.

There is no new database migration. Nothing new is required on the VPS. The tool is not a WinSW service.

## What a passing run proves

Each failure scenario is followed by the same read-only checks. A check is `PASS`, `FAIL`, or `INCOMPLETE`. `INCOMPLETE` is not a pass. Any required `FAIL` makes the run `FAIL`. Any other required status that is not `PASS` makes the run `INCOMPLETE`.

| Check | What it proves |
| --- | --- |
| `ledger_conservation` | City stocks match the ledger. |
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

NOT YET MEASURED. This section is updated from the `load-failure-report` artifact after that job finishes on the commit that added the tool. Until then there is no latency, throughput, lag, pool, or CPU number here.

### Local rehearsal

NOT YET MEASURED. A local run, when recorded, is labeled local and is not a substitute for the CI artifact.

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

ผลที่วัดได้จาก CI จะถูกคัดลอกลงส่วนภาษาอังกฤษด้านบนหลังงานจบ ก่อนหน้านั้นส่วนนั้นคือ NOT YET MEASURED ไม่ใส่ตัวเลขคาดเดา
