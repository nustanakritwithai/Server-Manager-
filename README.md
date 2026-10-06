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

Dev login is a **placeholder**. `POST /v1/auth/dev-login` returns `dev:{player_id}`. It is not signed. The web client in `web/` uses it so people can play the seeded world (Alice / Oakhold, Bob / Ironford). Anyone who can reach the API can act as those players. On the VPS the API process listens on localhost only; Caddy is what publishes HTTPS. This is not an account system.

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

Postgres must already be running. Settings come from the environment (`SIMCORE_` prefix). Copy the template and edit it if you are not using the defaults:

```bash
cp .env.example .env
```

`.env` is gitignored. The API and the worker read `.env` and, when it exists, `.env.prod`. Environment variables win over both files. The template only contains local placeholders: database user `simcore`, password `simcore`, and admin token `dev-admin`. With no `.env` and no variables set, the URL is `postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore`. A `postgres://` or `postgresql://` URL is rewritten to `postgresql+psycopg://`.

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

`SIMCORE_EMBEDDED_WORKER=true` runs that same loop inside the API process instead. Leave it false for normal use, including the VPS, where `simcore-worker` is its own Windows service. Production (`SIMCORE_ENV=production`) refuses to start if `SIMCORE_ADMIN_TOKEN` is empty or still `dev-admin`. Local docker-compose keeps `dev-admin`.

To click through the vertical slice against a local API:

```bash
cd web
python3 -m http.server 8080
```

Open http://127.0.0.1:8080 and point the API field at http://127.0.0.1:8741. Development CORS allows that origin plus `https://nustanakritwithai.github.io`.

## Deploy on the Windows VPS

Production is the Windows Server 2025 machine at **157.85.96.139** (sign in as **Administrator** over Remote Desktop). Docker Compose stays the local development setup. Production does not run Linux containers.

The machine runs PostgreSQL 16 (localhost only), the API on `127.0.0.1:8741`, the standalone worker, and Caddy on public ports 80 and 443. WinSW registers `simcore-api`, `simcore-worker`, and `simcore-caddy` as automatic services that restart after a crash. There is no domain yet, so the default public name is **`157-85-96-139.sslip.io`**. That name resolves to `157.85.96.139`, which is enough for Caddy to get a Let's Encrypt certificate.

This VPS already runs Apache (XAMPP: Apache 2.4, OpenSSL, PHP) for PocketMonster. Apache has to keep serving `https://157.85.96.139/` and its other hosts. Caddy sits in front: it owns ports 80 and 443, serves the game API on `157-85-96-139.sslip.io`, and reverse-proxies every other host to Apache on localhost. See [Apache is already serving this VPS](#apache-is-already-serving-this-vps) before expecting the public health URL to work.

The real system clock is what production uses. `POST /v1/admin/clock/advance` stays behind the admin token, and the whole admin API is off unless `SIMCORE_ENABLE_ADMIN=true`.

### 1. GitHub settings (any computer)

1. Merge the deploy branch so `main` contains `deploy/windows` and `web/`.
2. In the repo, open **Settings → Pages → Build and deployment** and set **Source** to **GitHub Actions**.
3. Open the **Deploy GitHub Pages** workflow and re-run it if the run that landed with the merge failed before Pages was switched to GitHub Actions. The site is https://nustanakritwithai.github.io/Server-Manager-/

`web/config.js` already sets the API to `https://157-85-96-139.sslip.io`. Do not change it unless you change `API_DOMAIN` on the server.

### 2. On the VPS, over RDP, in this order

1. Connect with Remote Desktop to `157.85.96.139` as `Administrator`.
2. Open **Windows PowerShell as Administrator**.
3. Install Git and clone `main`:

```powershell
winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
New-Item -ItemType Directory -Force -Path C:\simcore | Out-Null
git clone https://github.com/nustanakritwithai/Server-Manager-.git C:\simcore\app
```

4. Run the one-time bootstrap:

```powershell
powershell -ExecutionPolicy Bypass -File C:\simcore\app\deploy\windows\bootstrap.ps1
```

That installs Python 3.12, PostgreSQL 16, Caddy 2.11.7, and WinSW; creates the `simcore` database role with a random password; generates `SIMCORE_ADMIN_TOKEN`; writes `C:\simcore\app\.env.prod`; opens Windows Firewall for inbound TCP **80** and **443** only; removes any inbound **5432** rule and binds PostgreSQL to localhost; runs Alembic; seeds Alice and Bob only when the database has no players; then starts the three services. RDP (3389) is left alone. The script does not print the password or the admin token.

If another program is already listening on port 80 or 443, bootstrap does not stop it, does not disable IIS, and does not start Caddy. The API and the worker still start on localhost, and the script exits with code 2. The message points at `deploy/windows/coexist-apache.ps1`. Run that next. Re-running bootstrap is safe: it keeps the existing `.env.prod` secrets.

5. On the VPS, open `https://157-85-96-139.sslip.io/health`. The first request can take about a minute while the certificate is issued. A healthy process returns `{"status":"ok"}`. If bootstrap exited with code 2, this URL stays on Apache until the coexistence script below has finished. If it does not, read `C:\simcore\logs`. If the hosting panel has a firewall in front of Windows, allow inbound TCP 80 and 443 there too. Do not allow 5432.

### Apache is already serving this VPS

Apache 2.4.58 (the XAMPP build with PHP 8.0) already listens on ports 80 and 443. `https://157.85.96.139/` redirects to PocketMonster, and other paths may be a PHP API. Those responses have to stay as they are. A short-lived Let's Encrypt certificate for the IP address is what Apache presents today.

`deploy/windows/coexist-apache.ps1` is the one-time step. It detects `httpd.exe` (Windows service such as `Apache2.4`, a running process, or `C:\xampp`), backs up every config file it edits under `C:\simcore\apache-backups\<timestamp>`, moves Apache's public listeners to `127.0.0.1:8080` (HTTP) and `127.0.0.1:8443` (HTTPS), runs `httpd -t`, and only then restarts Apache. Caddy binds 80 and 443 and terminates TLS:

- `https://157-85-96-139.sslip.io` is proxied to the simcore API on `127.0.0.1:8741`.
- Any other plain HTTP host is proxied to Apache on `127.0.0.1:8080`, with the original `Host` header, so Apache's existing port-80 redirects still run.
- `https://157.85.96.139` is proxied to Apache's SSL vhost on `127.0.0.1:8443`, again with the original `Host`. PocketMonster's redirect stays an Apache response. Caddy 2.11.7 obtains the Let's Encrypt short-lived IP certificate itself (`profile shortlived`, renewed about halfway through the six-day lifetime). The script disables a win-acme, certbot, or Certify renewal task when it finds one, and comments Apache `mod_md` directives, so the old client does not take ports 80 and 443 back. Apache's certificate files remain only for that localhost hop.

PostgreSQL stays on localhost. The firewall is still only public 80 and 443. The script does not print secrets. It is safe to run again: a second run keeps the first backup as the rollback target.

The commands below assume the repo is already at `C:\simcore\app`, `main` already contains the first Windows deploy, and bootstrap has already been run once. Paste them into an Administrator PowerShell over RDP. Do not run `update.ps1`, and do not dispatch **Deploy to Windows VPS**, until this coexistence change is on `main`. An older `update.ps1` rewrites `C:\simcore\Caddyfile` without the Apache routes.

```powershell
cd C:\simcore\app
git fetch origin cursor/apache-caddy-coexist-31fc
git checkout cursor/apache-caddy-coexist-31fc
powershell -ExecutionPolicy Bypass -File C:\simcore\app\deploy\windows\coexist-apache.ps1
```

`.env.prod` is gitignored, so the checkout leaves the database password and admin token in place.

Verify both sites from that same window. The first health request can take about a minute while Caddy issues certificates.

```powershell
curl.exe -fsS https://157-85-96-139.sslip.io/health
curl.exe -sI https://157.85.96.139/
```

Health should print `{"status":"ok"}`. `https://157.85.96.139/` should still be a 302 whose `Location` is `https://pocketmonster-game.web.app/`. If the VPS cannot reach its own public addresses, check through the local Caddy instead:

```powershell
curl.exe --resolve 157-85-96-139.sslip.io:443:127.0.0.1 https://157-85-96-139.sslip.io/health
curl.exe -sI --resolve 157.85.96.139:443:127.0.0.1 https://157.85.96.139/
```

Rollback puts Apache back on ports 80 and 443 and leaves the `simcore-caddy` service disabled so a reboot does not take those ports again:

```powershell
powershell -ExecutionPolicy Bypass -File C:\simcore\app\deploy\windows\rollback-apache.ps1
```

After this branch is merged, return the checkout to `main` with `git checkout main` and `git pull`. Later deploys keep the Apache upstreams because `.env.prod` records them.

6. Install a GitHub Actions self-hosted runner so a push to `main` updates the server. In GitHub open **Settings → Actions → Runners → New self-hosted runner → Windows**, and copy the token that page shows. Back in the elevated PowerShell window:

```powershell
mkdir C:\simcore\actions-runner
cd C:\simcore\actions-runner
# Paste the download and Expand-Archive commands from that GitHub page, then:
.\config.cmd --url https://github.com/nustanakritwithai/Server-Manager- --token PASTE_THE_TOKEN --name simcore-vps --labels simcore --unattended
.\svc.cmd install
.\svc.cmd start
$runner = (Get-Service actions.runner.* | Select-Object -First 1).Name
sc.exe config $runner obj= LocalSystem
Restart-Service $runner
```

The space after `obj=` is required. LocalSystem is what lets the job restart the game services. The workflow is **push to main** and **manual dispatch** only. Do not add a pull-request trigger: this runner can restart Windows services, and a public pull request would run on the VPS.

7. After the coexistence change is on `main`, open **Actions → Deploy to Windows VPS** and run it, or push a commit to `main`. The job runs `deploy/windows/update.ps1`: `git pull`, `pip install`, `alembic upgrade`, seed if the world is still empty, then restart the services. `update.ps1` rewrites the Caddyfile from `.env.prod` and keeps the Apache upstreams. Until the runner is online the job waits in the queue. Leave this job queued until that merge; an older copy of the script would publish a Caddyfile that does not proxy Apache.

8. Open https://nustanakritwithai.github.io/Server-Manager-/ and sign in as Alice or Bob. The page counts down from `arrive_at` on its own. Battle reports appear only after the worker has applied the arrival. If the services are down, the page says the API is unreachable instead of failing silently.

### Values

| What | Where | Value |
| --- | --- | --- |
| Public API origin | `web/config.js` key `apiBaseUrl`, and the URL field in the page | `https://157-85-96-139.sslip.io` |
| `API_DOMAIN` | `C:\simcore\app\.env.prod` (created by bootstrap, not committed) | `157-85-96-139.sslip.io` |
| Database password and `SIMCORE_ADMIN_TOKEN` | the same `.env.prod` | generated on the server; do not copy them into GitHub |
| Runner registration token | `config.cmd` only, from the GitHub runners page | one-time; do not commit it |

No repository secret is required for deploy. There is no SSH key.

To move off sslip.io later: set `API_DOMAIN` in `.env.prod` to your hostname, point that name's A record at `157.85.96.139`, run `update.ps1`, and set `apiBaseUrl` to `https://` plus that exact host (no path, no trailing slash) before pushing `web/config.js`.

### Limits

- The VPS does not sleep the way a free application host does. If Windows or the services are stopped, the page says it cannot reach the API and keeps retrying.
- sslip.io is a public DNS shortcut. If it is unavailable, the hostname and certificate renewal fail. A domain you control avoids that.
- Let's Encrypt needs port 80 reachable from the internet and the name pointing at this IP. It will not issue a certificate before that is true.
- Dev login stays a placeholder on the public URL. Treat the world as a shared demo.
- The free-tier notes that apply to some hosts (the process sleeping when idle, a database that expires) do not apply here. You are paying for this VPS; disk is 60 GB and RAM is 8 GB, which is enough for this slice. PostgreSQL still needs backups, and this repo does not configure them yet.

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
docker-compose.yml        local Postgres + API + worker
web/                      static client for GitHub Pages
deploy/windows/           VPS bootstrap, Apache coexistence, update, and Caddy example
.github/workflows/        Pages deploy and the self-hosted Windows update
```

## Clock

`OffsetClock` is `base.now() + world_state.offset_seconds`. The API and the worker both read that row, so advancing time in one process is visible to the other. Tests inject a `FrozenClock` as the base. Production does not: the base clock is the real system clock. Event effects use the event's `due_at`, so a worker that wakes up late does not stretch the march. Advancing the offset is `POST /v1/admin/clock/advance`, and that route is admin-only. In production the admin API is disabled unless `SIMCORE_ENABLE_ADMIN=true`.

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
