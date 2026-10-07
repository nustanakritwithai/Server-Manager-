# Player auth / การยืนยันตัวผู้เล่น

Phase 7 adds accounts for players. Admin sign-in is a separate system (`SIMCORE_ADMIN_PASSWORD_HASH`, `SIMCORE_ADMIN_SESSION_SECRET`, `X-Admin-Token`). This document does not change that.

The API is JSON. Cookies are not required. Send `Authorization: Bearer <access_token>` on player routes. A future Godot client can use the calls below as-is.

เฟส 7 เพิ่มบัญชีผู้เล่น ระบบแอดมินแยกต่างหาก (`SIMCORE_ADMIN_PASSWORD_HASH`, `SIMCORE_ADMIN_SESSION_SECRET`, `X-Admin-Token`) และเอกสารนี้ไม่ยุบสองระบบเข้าด้วยกัน

API เป็น JSON ไม่บังคับคุกกี้ ส่ง `Authorization: Bearer <access_token>` ในเส้นทางของผู้เล่น ไคลเอนต์ Godot ในอนาคตเรียกได้ตามตัวอย่างด้านล่าง

## Accounts / บัญชี

`POST /v1/auth/register` with `{"username","password","email"?}`.

- Username: 3–32 characters, starts with a letter or digit, then letters, digits, `.`, `_`, `-`. Stored with its original case. Uniqueness is case-insensitive (`Alice` and `alice` are the same).
- Email is optional. Uniqueness is case-insensitive. There is no verification mail and no password-reset mail (no SMTP in this phase).
- Password: at least 10 characters, at most 1024, not equal to the username or email, and not one of a short list of common passwords. It is hashed with scrypt (`N=2**15`, `r=8`, `p=1`, 32-byte key), the same KDF the admin password uses. Verification is constant-time. A login for an unknown name still runs a hash compare so the timing does not reveal whether the account exists.
- A taken username or email returns `409` (`username_taken` or `email_taken`). That is the one response that reveals the name is in use. Login does not.

ชื่อผู้ใช้ 3–32 ตัว ขึ้นต้นด้วยตัวอักษรหรือตัวเลข ไม่สนใจตัวพิมพ์ใหญ่เล็กตอนตรวจซ้ำ อีเมลไม่บังคับและยังไม่มีการยืนยันทางจดหมาย รหัสผ่านอย่างน้อย 10 ตัว ไม่ใช่รหัสที่พบบ่อย และถูกแฮชด้วย scrypt แบบเดียวกับรหัสแอดมิน การเข้าสู่ระบบที่ชื่อไม่มีอยู่จริงก็ยังเทียบแฮช เพื่อไม่ให้เวลาตอบบอกว่ามีบัญชีหรือไม่

`POST /v1/auth/login` with `{"username","password"}`. `username` may be the username or the email. Failures (unknown user, wrong password, admin lock, temporary lockout) all return:

```json
{"error":{"code":"invalid_credentials","message":"invalid username or password"}}
```

HTTP 401. The body does not say which case it was.

## Tokens / โทเคน

A successful register, login, refresh, or change-password returns:

| Field | Meaning |
| --- | --- |
| `access_token` | HMAC-signed bearer token. Default lifetime 15 minutes (`SIMCORE_PLAYER_ACCESS_TTL_SECONDS`, wall clock, not game clock). |
| `refresh_token` | Opaque token. The server stores only its SHA-256. Default lifetime 30 days. |
| `expires_in` | Seconds until the access token expires. |
| `refresh_expires_in` | Refresh lifetime in seconds. |
| `must_change_password` | When true, game routes return 403 `password_change_required` until `POST /v1/auth/change-password`. Logout and `GET /v1/auth/me` still work. |
| `player_id`, `player_name`, `username` | The player this token acts as. |

`POST /v1/auth/refresh` with `{"refresh_token"}` rotates the refresh token: the old row is revoked and a new row in the same family is issued. Presenting a refresh token that was already rotated revokes the whole family, writes `auth.refresh_reuse` to the audit log, and returns 401. Later tokens in that family stop working.

`POST /v1/auth/logout` revokes the session in the access token. `POST /v1/auth/logout-all` revokes every session for that account.

The acting player on every player route comes from the access token. A `player_id` in a command body is ignored. Ownership of armies and cities is still checked on the server.

รีเฟรชโทเคนถูกหมุนทุกครั้งที่ใช้ซ้ำ ถ้าเอาโทเคนเก่าที่ถูกหมุนไปแล้วมาใช้ เซสชันทั้งตระกูลถูกเพิกถอน ผู้เล่นที่กระทำคำสั่งมาจากโทเคนเท่านั้น ไม่ใช่จากบอดี้

## Godot client / ไคลเอนต์ Godot

```text
POST /v1/auth/register
{"username":"ada","password":"correct-horse-battery","email":"ada@example.com"}

POST /v1/auth/login
{"username":"ada","password":"correct-horse-battery"}

POST /v1/auth/refresh
{"refresh_token":"<refresh_token>"}

GET /v1/auth/me
Authorization: Bearer <access_token>

POST /v1/auth/change-password
Authorization: Bearer <access_token>
{"current_password":"...","new_password":"..."}

POST /v1/auth/logout
Authorization: Bearer <access_token>

POST /v1/auth/logout-all
Authorization: Bearer <access_token>

GET /v1/time
GET /v1/me
GET /v1/me/cities
GET /v1/me/armies
GET /v1/me/reports
GET /v1/map/cities
Authorization: Bearer <access_token>

POST /v1/commands/attack
Authorization: Bearer <access_token>
Idempotency-Key: <client-generated unique string>
{"army_id":1,"target_city_id":2}
```

`GET /health` and `GET /health/ready` stay public. `GET /v1/time` requires a token so the client can sync after login. Store the refresh token in the client. When a call returns 401, refresh once and retry. If refresh returns 401, send the player to login.

`Idempotency-Key` is optional. When it is present, a replay of the same player, key, and body returns the stored status and body and does not run the command again. The same key with a different body returns 409 `idempotency_conflict`. A missing key runs the command once. The header is 1–200 visible ASCII characters.

คีย์ Idempotency ไม่บังคับ ถ้าส่งมา การส่งซ้ำด้วยคีย์และบอดี้เดิมจะได้ผลลัพธ์เดิมโดยไม่เดินทัพซ้ำ

## Abuse protection / การกันการใช้เกิน

| Limit | Default | Env |
| --- | --- | --- |
| Failed logins per account, then temporary lockout | 5 failures, 15 min window, 15 min lockout | `SIMCORE_PLAYER_LOGIN_MAX_FAILURES`, `SIMCORE_PLAYER_LOGIN_WINDOW_SECONDS`, `SIMCORE_PLAYER_LOGIN_LOCKOUT_SECONDS` |
| Failed logins per IP | 20 / 15 min, then HTTP 429 `rate_limited` | `SIMCORE_PLAYER_LOGIN_IP_MAX_FAILURES` |
| Commands per player | 30 / 60 seconds, then HTTP 429 `too many commands` | `SIMCORE_COMMAND_RATE_LIMIT`, `SIMCORE_COMMAND_RATE_WINDOW_SECONDS` |

The per-IP limiter answers 429 before it looks up the account. A temporary lockout still answers 401 with `invalid_credentials`, and the audit log records `auth.lockout`. An admin lock answers 401 on login with that same body. The lock endpoint also revokes refresh sessions, so an access token that was already issued stops working (401). A live session for a locked account is rejected with 403 `account_locked`. Rate limits live in the API process. One API service is the deployment this repo runs.

## Dev-login / ล็อกอินทดลอง

`POST /v1/auth/dev-login` with `{"name"}` is on only when `SIMCORE_ENV` is `dev`, `development`, `test`, or `testing`, or when `SIMCORE_ENABLE_DEV_LOGIN=true`. Otherwise it returns 404 `not_found`, a warning is logged, and `auth.dev_login` / `denied` is appended to the audit log. CI and local pytest keep `SIMCORE_ENV=development`, so existing tests still log in as Alice and Bob.

A `dev:{id}` bearer token is accepted only while dev-login is enabled, and only if that player is not admin-locked.

เปิดใช้เมื่อสภาพแวดล้อมเป็น development หรือ test หรือเมื่อตั้ง `SIMCORE_ENABLE_DEV_LOGIN=true` เท่านั้น บนโปรดักชันจะได้ 404

## Existing players / ผู้เล่นเดิม

Players created by the seed or by dev-login have rows in `players` and no row in `player_accounts`. Their cities, armies, and ledger stay. They cannot register the same name, and they cannot log in until an admin sets a temporary password:

`POST /v1/admin/accounts/temporary-password` with `{"player_id","password"}` (admin auth). The player's name becomes the username when it matches the username rules. `must_change_password` is set, existing sessions are revoked, and the password is not echoed and not written to the audit log. The player logs in with that password and must call `POST /v1/auth/change-password` before commands, the map, or `GET /v1/me`.

There is no account delete and no ledger edit on this API.

ผู้เล่นที่สร้างจาก dev-login ไม่มีรหัสผ่าน ข้อมูลเดิมยังอยู่ แอดมินต้องตั้งรหัสชั่วคราวให้ จึงจะเข้าสู่ระบบได้ และต้องเปลี่ยนรหัสทันที

## Admin Accounts tab / แท็บบัญชี

`web/admin` has an Accounts view, behind the existing admin session or `X-Admin-Token`:

| Call | Effect |
| --- | --- |
| `GET /v1/admin/accounts?q=` | Search players and accounts. Players with no password are listed. |
| `GET /v1/admin/accounts/{id}/sessions` | Session rows. Token hashes are not included. |
| `POST /v1/admin/accounts/{id}/revoke-sessions` | Revoke every refresh session. |
| `POST /v1/admin/accounts/{id}/lock` | Lock and revoke sessions. |
| `POST /v1/admin/accounts/{id}/unlock` | Unlock and clear the temporary login counter. |
| `POST /v1/admin/accounts/temporary-password` | Set or reset a temporary password. |

## Audit / บันทึก

These actions go on the existing hash-chained `audit_log`: `auth.register`, `auth.login` (success and failure), `auth.lockout`, `auth.refresh_reuse`, `auth.logout_all`, `auth.password_change`, `auth.dev_login` when the route is disabled, and `account.temporary_password`, `account.lock`, `account.unlock`, `account.revoke_sessions`. Accepted commands still carry `trace_id`. The log never stores passwords, hashes, or tokens.

## Migration and snapshots / ไมเกรชันและสแนปชอต

`0005_player_auth` revises `0004_monitoring`. It creates `player_accounts`, `player_refresh_sessions`, and `command_idempotency_keys`. `player_accounts.player_id` is nullable and has no foreign key, so a world restore can delete and reinsert `players`. Downgrade drops only those three tables.

World snapshots do **not** include the auth tables, the same way they omit `audit_log`. Restore deletes `command_idempotency_keys` and leaves accounts and sessions. Checksums stay about the simulation.

## Secrets / ความลับ

`SIMCORE_PLAYER_TOKEN_SECRET` signs access tokens. In production the process refuses to start if it is missing, shorter than 32 characters, or a known placeholder (including `dev-player-token-secret-not-for-production`). The API does not invent a secret at startup.

`deploy/windows/bootstrap.ps1` and `deploy/windows/update.ps1` generate a random value into `.env.prod` when the key is missing or weak, and they do not print it. The first update after this change also covers the copy of `update.ps1` already on the VPS: `alembic/env.py` writes the same secret into an existing `.env.prod` before migrations, and only when `SIMCORE_ENV=production`. Development and tests keep the default and do not create `.env.prod`.

Do not commit `.env` or `.env.prod`.

บนโปรดักชัน ถ้าความลับนี้ไม่มีหรืออ่อนเกินไป เซิร์ฟเวอร์จะไม่สตาร์ต `update.ps1` สร้างค่าสุ่มลง `.env.prod` ให้เองและไม่พิมพ์ค่าออกมา

## After merge, on the VPS / หลังเมอร์จบน VPS

1. Back up `C:\simcore\app\.env.prod`. Do not put that file in git.
2. Let the usual update run (`deploy/windows/update.ps1`, or the GitHub action that calls it). You do not type a player-token secret. The script, or the migration step if the script on disk is still the previous copy, writes `SIMCORE_PLAYER_TOKEN_SECRET` when it is absent or weak.
3. Confirm the API is up: `https://157-85-96-139.sslip.io/health`. `POST /v1/auth/dev-login` should return 404.
4. Sign in to the admin page. Open Accounts. For each seeded player (Alice, Bob, and anyone else created only by dev-login), set a temporary password and give it to that person some other way. They sign in on the game page and must change it before they can play. There is no email reset.
5. New players can `POST /v1/auth/register` themselves.

สำรอง `.env.prod` แล้วรันอัปเดตตามปกติ ไม่ต้องตั้งค่าความลับเอง จากนั้นในแท็บ Accounts ตั้งรหัสชั่วคราวให้ Alice และ Bob เพราะผู้เล่นเดิมยังไม่มีรหัสผ่าน
