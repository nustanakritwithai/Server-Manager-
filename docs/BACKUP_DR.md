# Database backup and restore

A world snapshot is not a backup. A snapshot copies the simulation rows inside the database that is already running. This page is the disaster-recovery copy: `pg_dump` of the `simcore` database, a SHA-256 checksum, a small manifest, and an upload to the owner's Google Drive.

The game stays up while `pg_dump` runs. PostgreSQL is not opened to the network. The scripts connect only to `127.0.0.1`, `localhost`, or `::1`.

There is no new database migration. Nothing new is required in `.env.prod`. Optional settings are listed at the bottom and in `.env.prod.example`.

## What you get

| | |
| --- | --- |
| Dump | `pg_dump -Fc` (custom format, compressed) |
| Local files | `C:\simcore\backups\simcore-<UTC time>-<commit>-<alembic revision>.dump` plus `.sha256` and `.json` |
| Local copies kept | 2 (the newest). Older local files are deleted only after the new dump is verified and uploaded |
| Remote | rclone remote `gdrive:simcore-backups` |
| Remote copies kept | newest backup from each of the last 14 UTC days that have one, plus the newest backup from each of the last 8 ISO weeks that have one |
| Status | `C:\simcore\backups\backup-status.json` |
| Monitoring | `backup.last_success` on the admin Monitoring tab. WARN after 26 hours, CRITICAL after 50 hours, UNKNOWN if a verified upload has never been recorded |
| Log | `C:\simcore\logs\backup.log` |

The manifest records the time, dump size, database name, alembic revision, git commit, and `pg_dump` version, plus the SHA-256, row counts, and the world checksum. The world checksum is the same function the snapshot code uses. It is stored so a restore drill can compare the restored database to the dump. It does not make the dump a snapshot.

## Disk space

The VPS C: drive has about 2.26 GB free. The backup script reads `pg_database_size`, multiplies by 1.5, and refuses to dump if the free space left after that estimate would be under 1 GB. It does not delete older local dumps to make room. Those dumps are deleted only after a newer dump has been checked with `pg_restore --list` and the upload has been checked with rclone.

A restore into a second database needs about one extra copy of the database, plus the 1 GB floor. Replacing the live database also takes a fresh safety backup first, so it needs room for that dump and the second copy. If the check fails, nothing is overwritten.

Do not lower the 1 GB floor unless you have looked at free space that day. Do not point the backup folder at a drive that is not backed up off the machine and then delete the Drive remote.

## One-time setup

Do this on the VPS after this change is on `main` and `deploy/windows/update.ps1` has finished. Use an elevated PowerShell (Run as Administrator), or the session that already runs as Administrator over RDP.

1. Install the pinned rclone and confirm `pg_dump` is present.

```powershell
cd C:\simcore\app
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\install-backup-tools.ps1
```

2. Configure the Google Drive remote. This is the step that needs your browser. The script does not contain a Google password or token. You sign in once. rclone writes the refresh token to `C:\simcore\backup\rclone.conf` on the VPS. Do not set a rclone configuration password. A password would make the daily task stop and wait for input. The file ACL below is what keeps the token local.

```powershell
$env:RCLONE_CONFIG = "C:\simcore\backup\rclone.conf"
C:\simcore\tools\rclone\rclone.exe config
```

In that menu:

- `n` New remote
- name: `gdrive`
- Storage: `drive` (Google Drive)
- `client_id`: press Enter (rclone's own client)
- `client_secret`: press Enter
- scope: choose **drive.file**, the line that says access to files created by rclone only. In rclone 1.75.1 that line is option `3`. If the numbers differ, match the text `drive.file`, not the number.
- `root_folder_id`: press Enter
- `service_account_file`: press Enter
- Edit advanced config? `n`
- Use auto config? `n`

rclone then prints a command that starts with `rclone authorize "drive"`. Run that exact command on your own computer (install the same rclone version if you can), sign in to Google in your browser, and paste the token it prints back into the VPS prompt. Do not paste the token into chat, git, or the backup log.

If you are already in an RDP session on the VPS and a browser is installed there, you can answer `y` to auto config instead and sign in in that browser. `n` is the path that uses the browser on your own computer.

Leave the remote name `gdrive`. The scripts upload to `gdrive:simcore-backups`.

3. Let rclone create the folder. Do this before you create a folder by hand in the Drive website. With `drive.file`, rclone can only see files it created.

```powershell
$env:RCLONE_CONFIG = "C:\simcore\backup\rclone.conf"
C:\simcore\tools\rclone\rclone.exe mkdir gdrive:simcore-backups
C:\simcore\tools\rclone\rclone.exe lsf gdrive:simcore-backups
```

If `mkdir` or the first backup says the folder is not visible, run `rclone config` again, delete the `gdrive` remote, and create it with scope `drive` (full access to your Drive). Prefer `drive.file` when `mkdir` works.

4. Lock the token file so only SYSTEM and Administrators can read it. The scheduled task runs as SYSTEM.

```powershell
icacls C:\simcore\backup\rclone.conf /inheritance:r /grant:r "SYSTEM:(R)" "Administrators:(F)"
```

5. Register the daily task. The default time is 03:15 on the VPS clock. It runs whether or not you are logged on. It does not stop `simcore-api`, `simcore-worker`, or `simcore-caddy`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\register-backup-task.ps1 -Time 03:15
```

Remove it later with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\register-backup-task.ps1 -Unregister
```

6. Take the first backup and wait until the command exits 0.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\backup.ps1
```

7. Run a drill. This restores the latest backup into a temporary database, checks it, prints `PASS` or `FAIL`, and drops the temporary database. It does not touch the live database.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\restore-backup.ps1 -Drill
```

`PASS` is printed only when the alembic revision, row counts, world checksum, and ledger chain matched, and any READY world snapshot in that database still matches its stored checksum. Then open the admin Monitoring tab. `backup.last_success` should be `OK`.

## Restore

### Into a separate database

This is the normal restore. The live database keeps serving the game.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\restore-backup.ps1 -DumpPath C:\simcore\backups\simcore-YYYYMMDDTHHMMSSZ-commit-revision.dump
```

Or download one object from Drive (the stem is the file name without `.dump`):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\restore-backup.ps1 -RemoteStem simcore-YYYYMMDDTHHMMSSZ-commit-revision
```

The default target name is `simcore_restore_test`. The script checks the SHA-256, runs `pg_restore --list`, restores into that new database, and runs the same checks as the drill. It refuses if that database already exists. Drop a leftover test database yourself before reusing the name. It will not restore on top of `simcore` unless you use the flag in the next section.

### Over the live database

There is no one-step replace. The command has to include `-ReplaceLive` and the confirmation text `REPLACE LIVE simcore` (the name is the live database name). Before it stops anything, it runs a new backup and that backup has to upload successfully. It then stops `simcore-worker` and `simcore-api` only. Caddy and PostgreSQL stay up. It restores into `simcore_incoming_<time>`, and it renames the databases only if the drill checks print `PASS`. The previous database is left on disk as `simcore_pre_restore_<time>`. Drop that name yourself after you have played on the restored world and you are sure you do not need it. `alembic upgrade head` runs after the swap, then the API and the worker start again.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\restore-backup.ps1 -ReplaceLive -Confirm "REPLACE LIVE simcore" -DumpPath C:\simcore\backups\simcore-YYYYMMDDTHHMMSSZ-commit-revision.dump
```

If you omit `-Confirm`, the script asks you to type the phrase. A wrong phrase stops the script before the safety backup and before any service stops.

## RPO and RTO

RPO is the amount of play you can lose. The task runs once a day, so a successful verified upload is at most about 24 hours old when the next one is due. Monitoring warns after 26 hours and goes critical after 50 hours so a missed day is visible. This is not point-in-time recovery. There is no WAL archive. Changes after the last verified upload are not in Drive.

RTO is how long a restore takes once you are on the VPS and you have chosen a dump. For a database this size, download, `pg_restore`, the checks, and the service restart are minutes, not a second site that is already warm. Replacing the live database also waits for the safety backup upload, so that path is longer by one backup. Nothing fails over by itself.

## Exit codes

`backup.ps1`: 0 success, 2 not enough disk, 3 dump or `pg_restore --list` failed, 4 upload or remote check failed, 1 anything else. A non-zero exit does not clear `last_verified_at` from the previous good upload. The Monitoring age keeps counting from that upload.

`restore-backup.ps1`: 0 success (a drill prints `PASS`), 2 not enough disk, 3 dump or checksum unreadable, 4 download or safety backup failed, 5 restore refused or the drill printed `FAIL`.

## Optional settings

Unset means the default. None of these are required.

| Variable | Default | Who reads it |
| --- | --- | --- |
| `SIMCORE_BACKUP_STATUS_PATH` | `C:\simcore\backups\backup-status.json` on Windows | API and `backup.ps1` |
| `SIMCORE_BACKUP_WARN_HOURS` | 26 | API |
| `SIMCORE_BACKUP_CRITICAL_HOURS` | 50 | API |
| `SIMCORE_BACKUP_DIR` | `C:\simcore\backups` | scripts |
| `SIMCORE_BACKUP_REMOTE` | `gdrive:simcore-backups` | scripts |
| `SIMCORE_BACKUP_RCLONE_CONFIG` | `C:\simcore\backup\rclone.conf` | scripts |
| `SIMCORE_BACKUP_LOG` | `C:\simcore\logs\backup.log` | scripts |
| `SIMCORE_BACKUP_KEEP_LOCAL` | 2 | `backup.ps1` |
| `SIMCORE_BACKUP_KEEP_DAILY` | 14 | `backup.ps1` |
| `SIMCORE_BACKUP_KEEP_WEEKLY` | 8 | `backup.ps1` |
| `SIMCORE_BACKUP_MIN_FREE_MB` | 1024 | scripts |
| `SIMCORE_BACKUP_DUMP_MARGIN` | 1.5 | scripts |

If you change `SIMCORE_BACKUP_DIR`, also set `SIMCORE_BACKUP_STATUS_PATH` to the status file you want the API to read. The API does not guess a custom dump folder.

---

# สำรองฐานข้อมูลและการกู้คืน

สแนปชอตของโลกเกมไม่ใช่การสำรองข้อมูล สแนปชอตคัดลอกแถวของเกมที่อยู่ในฐานข้อมูลที่กำลังทำงานอยู่ หน้านี้คือชุดกู้คืนระบบ: `pg_dump` ของฐานข้อมูล `simcore` ไฟล์เช็กซัม SHA-256 ไฟล์ manifest ขนาดเล็ก แล้วอัปโหลดขึ้น Google Drive ของเจ้าของเครื่อง

เกมไม่หยุดระหว่าง `pg_dump` PostgreSQL ไม่ถูกเปิดออกสู่อินเทอร์เน็ต สคริปต์ต่อเฉพาะ `127.0.0.1`, `localhost`, หรือ `::1`

ไม่มีไมเกรชันใหม่ ไม่มีตัวแปรสภาพแวดล้อมที่บังคับเพิ่มใน `.env.prod` ค่าที่ใส่หรือไม่ใส่ก็ได้มีอยู่ท้ายหน้านี้และใน `.env.prod.example`

## สิ่งที่ได้

| | |
| --- | --- |
| ไฟล์ดัมป์ | `pg_dump -Fc` (รูปแบบ custom และบีบอัด) |
| ไฟล์ในเครื่อง | `C:\simcore\backups\simcore-<เวลา UTC>-<commit>-<alembic revision>.dump` พร้อม `.sha256` และ `.json` |
| จำนวนชุดในเครื่อง | 2 ชุดล่าสุด ชุดเก่าถูกลบหลังชุดใหม่ตรวจแล้วและอัปโหลดแล้วเท่านั้น |
| ที่เก็บนอกเครื่อง | rclone ชื่อ `gdrive:simcore-backups` |
| จำนวนชุดนอกเครื่อง | สำรองล่าสุดของแต่ละวัน UTC 14 วันที่มีไฟล์ และสำรองล่าสุดของแต่ละสัปดาห์ ISO 8 สัปดาห์ที่มีไฟล์ |
| สถานะ | `C:\simcore\backups\backup-status.json` |
| การเฝ้าดู | `backup.last_success` ในแท็บ Monitoring เตือนหลัง 26 ชั่วโมง วิกฤตหลัง 50 ชั่วโมง เป็น UNKNOWN ถ้ายังไม่เคยมีอัปโหลดที่ตรวจแล้ว |
| ล็อก | `C:\simcore\logs\backup.log` |

manifest เก็บเวลา ขนาดไฟล์ ชื่อฐานข้อมูล alembic revision git commit และรุ่นของ `pg_dump` รวมทั้ง SHA-256 จำนวนแถว และ world checksum checksum นี้เป็นฟังก์ชันเดียวกับโค้ดสแนปชอต เก็บไว้ให้การซ้อมกู้คืนเทียบฐานข้อมูลที่กู้แล้วกับดัมป์ มันไม่ทำให้ดัมป์กลายเป็นสแนปชอต

## พื้นที่ดิสก์

ไดรฟ์ C: ของ VPS ว่างประมาณ 2.26 GB สคริปต์อ่าน `pg_database_size` คูณ 1.5 แล้วปฏิเสธการดัมป์ถ้าพื้นที่ว่างที่เหลือหลังขนาดที่ประมาณไว้นั้นต่ำกว่า 1 GB สคริปต์ไม่ลบดัมป์เก่าในเครื่องเพื่อทำที่ว่าง ดัมป์เก่าถูกลบเมื่อดัมป์ใหม่ผ่าน `pg_restore --list` และอัปโหลดผ่านการตรวจของ rclone แล้วเท่านั้น

การกู้ลงฐานข้อมูลที่สองต้องมีที่ว่างประมาณอีกหนึ่งเท่าของฐานข้อมูล และยังต้องเหลืออย่างน้อย 1 GB การเขียนทับฐานข้อมูลจริงจะสำรองความปลอดภัยรอบใหม่ก่อน ดังนั้นต้องมีที่ทั้งสำหรับดัมป์นั้นและสำหรับสำเนาที่สอง ถ้าการตรวจพื้นที่ไม่ผ่าน จะไม่มีการเขียนทับ

อย่าลดพื้น 1 GB ถ้ายังไม่ได้ดูพื้นที่ว่างในวันนั้น อย่าย้ายโฟลเดอร์สำรองไปไดรฟ์ที่ไม่มีสำเนานอกเครื่องแล้วไปลบ remote บน Drive

## ตั้งค่าครั้งเดียว

ทำบน VPS หลังจากโค้ดนี้อยู่บน `main` และ `deploy/windows/update.ps1` ทำงานจบแล้ว เปิด PowerShell แบบผู้ดูแล (Run as Administrator) หรือใช้เซสชัน RDP ที่เป็นผู้ดูแลอยู่แล้ว

1. ติดตั้ง rclone รุ่นที่ปักไว้ และตรวจว่ามี `pg_dump`

```powershell
cd C:\simcore\app
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\install-backup-tools.ps1
```

2. ตั้ง remote ของ Google Drive ขั้นตอนนี้ต้องใช้เบราว์เซอร์ของคุณ สคริปต์ไม่มีรหัส Google หรือโทเคน คุณลงชื่อเข้าใช้ครั้งเดียว rclone เขียน refresh token ลง `C:\simcore\backup\rclone.conf` บน VPS อย่าตั้งรหัสผ่านของไฟล์คอนฟิก rclone ถ้ารหัสนั้นถูกตั้ง งานรายวันจะหยุดรอข้อมูล ไฟล์ถูกล็อกด้วย ACL ในขั้นถัดไป

```powershell
$env:RCLONE_CONFIG = "C:\simcore\backup\rclone.conf"
C:\simcore\tools\rclone\rclone.exe config
```

ในเมนู:

- `n` สร้าง remote ใหม่
- ชื่อ: `gdrive`
- Storage: `drive` (Google Drive)
- `client_id`: กด Enter (ใช้ไคลเอนต์ของ rclone)
- `client_secret`: กด Enter
- scope: เลือก **drive.file** บรรทัดที่บอกว่าเข้าถึงไฟล์ที่ rclone สร้างเท่านั้น ใน rclone 1.75.1 บรรทัดนั้นคือตัวเลือก `3` ถ้าเลขไม่ตรงกัน ให้เลือกข้อความ `drive.file` ไม่ใช่เลข
- `root_folder_id`: กด Enter
- `service_account_file`: กด Enter
- Edit advanced config? `n`
- Use auto config? `n`

rclone จะพิมพ์คำสั่งที่ขึ้นต้นด้วย `rclone authorize "drive"` ให้นำคำสั่งนั้นไปรันบนคอมพิวเตอร์ของคุณ (ติดตั้ง rclone รุ่นเดียวกันถ้าทำได้) ลงชื่อเข้า Google ในเบราว์เซอร์ของคุณ แล้ววางโทเคนที่พิมพ์ออกมากลับที่พรอมต์บน VPS อย่าวางโทเคนในแชท ใน git หรือในล็อกการสำรอง

ถ้าคุณอยู่ใน RDP บน VPS และมีเบราว์เซอร์บนเครื่องนั้น ตอบ `y` ที่ auto config แล้วลงชื่อในเบราว์เซอร์นั้นได้ คำตอบ `n` คือทางที่ใช้เบราว์เซอร์บนคอมพิวเตอร์ของคุณ

ชื่อ remote ต้องเป็น `gdrive` สคริปต์อัปโหลดไปที่ `gdrive:simcore-backups`

3. ให้ rclone สร้างโฟลเดอร์เอง ทำก่อนที่จะสร้างโฟลเดอร์ด้วยมือในเว็บ Drive เมื่อใช้ `drive.file` rclone มองเห็นเฉพาะไฟล์ที่ตัวเองสร้าง

```powershell
$env:RCLONE_CONFIG = "C:\simcore\backup\rclone.conf"
C:\simcore\tools\rclone\rclone.exe mkdir gdrive:simcore-backups
C:\simcore\tools\rclone\rclone.exe lsf gdrive:simcore-backups
```

ถ้า `mkdir` หรือการสำรองครั้งแรกระบุว่ามองไม่เห็นโฟลเดอร์ ให้รัน `rclone config` อีกครั้ง ลบ remote `gdrive` แล้วสร้างใหม่ด้วย scope `drive` (เข้าถึง Drive ทั้งก้อน) ใช้ `drive.file` ก่อนถ้า `mkdir` สำเร็จ

4. ล็อกไฟล์โทเคน ให้อ่านได้เฉพาะ SYSTEM และ Administrators งานตามเวลาทำงานในชื่อ SYSTEM

```powershell
icacls C:\simcore\backup\rclone.conf /inheritance:r /grant:r "SYSTEM:(R)" "Administrators:(F)"
```

5. ลงทะเบียนงานรายวัน เวลาเริ่มต้นคือ 03:15 ตามนาฬิกาของ VPS งานรันแม้ไม่มีคนล็อกอิน และไม่หยุด `simcore-api`, `simcore-worker`, หรือ `simcore-caddy`

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\register-backup-task.ps1 -Time 03:15
```

เอาออกภายหลังด้วย:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\register-backup-task.ps1 -Unregister
```

6. สำรองครั้งแรก และรอจนคำสั่งจบด้วยรหัส 0

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\backup.ps1
```

7. ซ้อมกู้คืน สคริปต์กู้สำรองล่าสุดลงฐานข้อมูลชั่วคราว ตรวจ แล้วพิมพ์ `PASS` หรือ `FAIL` จากนั้นลบฐานข้อมูลชั่วคราว ไม่แตะฐานข้อมูลจริง

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\restore-backup.ps1 -Drill
```

คำว่า `PASS` พิมพ์เมื่อ alembic revision จำนวนแถว world checksum และสาย ledger ตรงกัน และสแนปชอตโลกที่สถานะ READY ในฐานนั้นยังตรงกับเช็กซัมที่เก็บไว้ จากนั้นเปิดแท็บ Monitoring `backup.last_success` ควรเป็น `OK`

## การกู้คืน

### ลงฐานข้อมูลแยก

นี่คือทางปกติ ฐานข้อมูลจริงยังให้บริการเกมต่อ

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\restore-backup.ps1 -DumpPath C:\simcore\backups\simcore-YYYYMMDDTHHMMSSZ-commit-revision.dump
```

หรือดึงจาก Drive (stem คือชื่อไฟล์ที่ไม่มี `.dump`):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\restore-backup.ps1 -RemoteStem simcore-YYYYMMDDTHHMMSSZ-commit-revision
```

ชื่อเป้าหมายเริ่มต้นคือ `simcore_restore_test` สคริปต์ตรวจ SHA-256 รัน `pg_restore --list` กู้ลงฐานใหม่ แล้วตรวจแบบเดียวกับ drill ถ้าฐานชื่อนั้นมีอยู่แล้วจะไม่เขียนทับ ให้คุณลบฐานทดสอบที่ค้างเองก่อนใช้ชื่อซ้ำ สคริปต์จะไม่กู้ทับ `simcore` ถ้าไม่ใช้แฟล็กในหัวข้อถัดไป

### ทับฐานข้อมูลจริง

ไม่มีคำสั่งเดียวที่ทับให้ ต้องมี `-ReplaceLive` และข้อความยืนยัน `REPLACE LIVE simcore` (ชื่อคือชื่อฐานข้อมูลจริง) ก่อนหยุดบริการใดๆ สคริปต์จะสำรองรอบใหม่ และการอัปโหลดรอบนั้นต้องสำเร็จ จากนั้นหยุดเฉพาะ `simcore-worker` และ `simcore-api` Caddy กับ PostgreSQL ยังทำงาน มันกู้ลง `simcore_incoming_<เวลา>` และเปลี่ยนชื่อฐานข้อมูลเมื่อการตรวจพิมพ์ `PASS` เท่านั้น ฐานเดิมถูกคงไว้ในชื่อ `simcore_pre_restore_<เวลา>` ให้คุณลบชื่อนั้นเองหลังจากเล่นบนโลกที่กู้แล้วและแน่ใจว่าไม่ต้องใช้ของเก่า `alembic upgrade head` รันหลังสลับชื่อ แล้ว API กับ worker จะเริ่มใหม่

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\restore-backup.ps1 -ReplaceLive -Confirm "REPLACE LIVE simcore" -DumpPath C:\simcore\backups\simcore-YYYYMMDDTHHMMSSZ-commit-revision.dump
```

ถ้าไม่ใส่ `-Confirm` สคริปต์จะให้พิมพ์ประโยคนั้น ถ้าพิมพ์ไม่ตรง สคริปต์หยุดก่อนการสำรองความปลอดภัยและก่อนหยุดบริการ

## RPO และ RTO

RPO คือช่วงการเล่นที่อาจหายได้ งานรันวันละครั้ง ดังนั้นอัปโหลดที่ตรวจแล้วจะเก่าไม่เกินประมาณ 24 ชั่วโมงเมื่อถึงรอบถัดไป การเฝ้าดูเตือนหลัง 26 ชั่วโมง และเป็นวิกฤตหลัง 50 ชั่วโมง เพื่อให้วันที่พลาดไปปรากฏ นี่ไม่ใช่การกู้คืน ณ จุดเวลา ไม่มีคลัง WAL สิ่งที่เกิดหลังอัปโหลดที่ตรวจแล้วครั้งล่าสุดไม่อยู่บน Drive

RTO คือเวลาที่ใช้กู้เมื่อคุณอยู่บน VPS และเลือกดัมป์แล้ว สำหรับฐานข้อมูลขนาดนี้ การดาวน์โหลด `pg_restore` การตรวจ และการเริ่มบริการใหม่ใช้เวลาเป็นนาที ไม่ใช่ไซต์สำรองที่พร้อมอยู่แล้ว การทับฐานข้อมูลจริงยังต้องรออัปโหลดสำรองความปลอดภัย ดังนั้นทางนั้นยาวกว่าหนึ่งรอบสำรอง ไม่มีการสลับเครื่องให้อัตโนมัติ

## รหัสออก

`backup.ps1`: 0 สำเร็จ, 2 ดิสก์ไม่พอ, 3 ดัมป์หรือ `pg_restore --list` ไม่ผ่าน, 4 อัปโหลดหรือการตรวจฝั่ง remote ไม่ผ่าน, 1 อย่างอื่น รหัสที่ไม่ใช่ศูนย์จะไม่ล้าง `last_verified_at` ของรอบที่ดีครั้งก่อน อายุในหน้า Monitoring นับจากอัปโหลดนั้นต่อ

`restore-backup.ps1`: 0 สำเร็จ (drill พิมพ์ `PASS`), 2 ดิสก์ไม่พอ, 3 ดัมป์หรือเช็กซัมอ่านไม่ได้, 4 ดาวน์โหลดหรือสำรองความปลอดภัยไม่ผ่าน, 5 ปฏิเสธการกู้ หรือ drill พิมพ์ `FAIL`

## ค่าที่ใส่หรือไม่ใส่ก็ได้

ไม่ตั้งค่าหมายถึงใช้ค่าเริ่มต้น ไม่มีตัวใดที่บังคับ

| ตัวแปร | ค่าเริ่มต้น | ใครอ่าน |
| --- | --- | --- |
| `SIMCORE_BACKUP_STATUS_PATH` | `C:\simcore\backups\backup-status.json` บน Windows | API และ `backup.ps1` |
| `SIMCORE_BACKUP_WARN_HOURS` | 26 | API |
| `SIMCORE_BACKUP_CRITICAL_HOURS` | 50 | API |
| `SIMCORE_BACKUP_DIR` | `C:\simcore\backups` | สคริปต์ |
| `SIMCORE_BACKUP_REMOTE` | `gdrive:simcore-backups` | สคริปต์ |
| `SIMCORE_BACKUP_RCLONE_CONFIG` | `C:\simcore\backup\rclone.conf` | สคริปต์ |
| `SIMCORE_BACKUP_LOG` | `C:\simcore\logs\backup.log` | สคริปต์ |
| `SIMCORE_BACKUP_KEEP_LOCAL` | 2 | `backup.ps1` |
| `SIMCORE_BACKUP_KEEP_DAILY` | 14 | `backup.ps1` |
| `SIMCORE_BACKUP_KEEP_WEEKLY` | 8 | `backup.ps1` |
| `SIMCORE_BACKUP_MIN_FREE_MB` | 1024 | สคริปต์ |
| `SIMCORE_BACKUP_DUMP_MARGIN` | 1.5 | สคริปต์ |

ถ้าเปลี่ยน `SIMCORE_BACKUP_DIR` ให้ตั้ง `SIMCORE_BACKUP_STATUS_PATH` เป็นไฟล์สถานะที่ต้องการให้ API อ่านด้วย API ไม่เดาโฟลเดอร์ดัมป์ที่คุณย้ายเอง
