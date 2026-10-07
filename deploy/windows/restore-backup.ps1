# Restore a pg_dump custom-format backup into a new database, or replace the live
# database only with -ReplaceLive and the exact confirmation phrase.
# -Drill restores the latest backup into a temporary database, checks it, then drops it.
# Credentials come from .env.prod and are not printed.
#
# Exit codes: 0 ok, 2 disk, 3 dump or checksum unreadable, 4 download or safety backup, 5 restore refused or drill FAIL, 1 other.

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$DumpPath = "",
    [string]$RemoteStem = "",
    [string]$TargetDatabase = "simcore_restore_test",
    [switch]$ReplaceLive,
    [string]$Confirm = "",
    [switch]$Drill,
    [string]$BackupDir = "",
    [string]$Remote = "",
    [string]$RcloneConfig = "",
    [string]$LogPath = "",
    [long]$MinimumFreeBytes = 0,
    [double]$DumpSizeMargin = 0
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\BackupLib.ps1"
Assert-SimcoreElevated

$script:FailureCode = 5
$script:TempDownload = ""
$script:PgPass = ""
$script:DrillDatabase = ""
$script:IncomingDatabase = ""
$script:PreviousDatabase = ""
$script:RenamedAway = $false
$script:Swapped = $false
$script:StoppedGame = $false

function Fail-Restore {
    param([int]$Code, [string]$Message)
    $script:FailureCode = $Code
    throw $Message
}

function Invoke-RestorePython {
    param([string[]]$ArgumentList, [string]$StdinText = "", [int]$ExitCode = 0)
    $result = Invoke-SimcoreBackupPython -ArgumentList $ArgumentList -StdinText $StdinText
    if ($result.ExitCode -ne 0) {
        $code = $ExitCode
        if ($code -eq 0) { $code = $script:FailureCode }
        Fail-Restore $code ($result.Stderr + " " + $result.Stdout).Trim()
    }
    return $result
}

function Clear-RestoreSecrets {
    if ($script:PgPass -and (Test-Path -LiteralPath $script:PgPass)) {
        Remove-Item -LiteralPath $script:PgPass -Force -ErrorAction SilentlyContinue
    }
    $script:PgPass = ""
    if (Test-Path Env:PGPASSFILE) { Remove-Item Env:PGPASSFILE -ErrorAction SilentlyContinue }
    if (Test-Path Env:PGPASSWORD) { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
}

function Get-BackupStemFromDump {
    param([string]$Path)
    $leaf = [System.IO.Path]::GetFileNameWithoutExtension($Path)
    if ($leaf -notmatch '^simcore-\d{8}T\d{6}Z-([0-9a-f]{7,40}|nogit)-[A-Za-z0-9_]+$') {
        Fail-Restore 5 "Dump name '$leaf' is not a simcore backup name."
    }
    return $leaf
}

function Get-NewestStem {
    param([string[]]$Stems)
    $best = ""
    $bestTime = ""
    foreach ($stem in @($Stems)) {
        if ($stem -match '^simcore-(\d{8}T\d{6}Z)-') {
            $stamp = $Matches[1]
            if ($stamp -gt $bestTime) {
                $bestTime = $stamp
                $best = $stem
            }
        }
    }
    return $best
}

try {
    if ($Drill -and $ReplaceLive) {
        Fail-Restore 5 "Drill and ReplaceLive cannot be used together."
    }
    Initialize-SimcoreBackupContext -RepoRoot $RepoRoot
    if (-not $BackupDir) { $BackupDir = Get-SimcoreEnvValue "SIMCORE_BACKUP_DIR" "C:\simcore\backups" }
    if (-not $Remote) { $Remote = Get-SimcoreEnvValue "SIMCORE_BACKUP_REMOTE" "gdrive:simcore-backups" }
    if (-not $RcloneConfig) { $RcloneConfig = Get-SimcoreEnvValue "SIMCORE_BACKUP_RCLONE_CONFIG" (Join-Path $script:BackupInstallRoot "backup\rclone.conf") }
    if (-not $LogPath) { $LogPath = Get-SimcoreEnvValue "SIMCORE_BACKUP_LOG" (Join-Path $script:BackupInstallRoot "logs\backup.log") }
    if ($MinimumFreeBytes -lt 1) {
        $minMb = [int](Get-SimcoreEnvValue "SIMCORE_BACKUP_MIN_FREE_MB" "1024")
        $MinimumFreeBytes = [int64]$minMb * 1024 * 1024
    }
    if ($DumpSizeMargin -le 0) { $DumpSizeMargin = [double](Get-SimcoreEnvValue "SIMCORE_BACKUP_DUMP_MARGIN" "1.5") }
    $script:BackupLogPath = $LogPath
    $script:BackupRcloneConfig = $RcloneConfig
    Assert-SimcoreRemoteName $Remote
    $tools = Find-SimcorePgTools
    $script:BackupPgRestore = $tools.Restore
    $script:BackupRclone = Find-SimcoreRclone -InstallRoot $script:BackupInstallRoot
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

    $info = Invoke-RestorePython -ArgumentList @("connection-info")
    $db = $info.Stdout | ConvertFrom-Json
    $live = [string]$db.database
    Write-BackupLog "restore start live=$live drill=$Drill replace=$ReplaceLive target=$TargetDatabase"

    if ($ReplaceLive) {
        $expected = "REPLACE LIVE $live"
        if ([string]::IsNullOrWhiteSpace($Confirm)) {
            Write-Host "Type $expected to replace the live database. Any other text cancels."
            $Confirm = Read-Host
        }
        if ($Confirm -cne $expected) {
            Fail-Restore 5 "Refusing to replace the live database. Re-run with -ReplaceLive -Confirm '$expected'."
        }
        if ([string]::IsNullOrWhiteSpace($DumpPath) -and [string]::IsNullOrWhiteSpace($RemoteStem)) {
            Fail-Restore 5 "ReplaceLive needs -DumpPath or -RemoteStem. It will not guess which backup to put over the live database."
        }
    }

    $downloaded = $false
    if ($DumpPath) {
        if (-not (Test-Path -LiteralPath $DumpPath)) { Fail-Restore 3 "Dump not found at $DumpPath." }
    } elseif ($RemoteStem -or $Drill) {
        if (-not (Test-Path -LiteralPath $RcloneConfig)) {
            Fail-Restore 4 "rclone config was not found at $RcloneConfig."
        }
        $stemToFetch = $RemoteStem
        if (-not $stemToFetch) {
            $localDumps = @(Get-ChildItem -LiteralPath $BackupDir -Filter "simcore-*.dump" -File -ErrorAction SilentlyContinue | ForEach-Object { $_.BaseName })
            $stemToFetch = Get-NewestStem $localDumps
            if ($stemToFetch) {
                $DumpPath = Join-Path $BackupDir "$stemToFetch.dump"
                Write-BackupLog "drill using local $DumpPath"
            }
        }
        if (-not $DumpPath) {
            $remoteList = Invoke-SimcoreRclone -ArgumentList @("lsf", $Remote)
            if ($remoteList.ExitCode -ne 0) { Fail-Restore 4 "rclone lsf failed. $($remoteList.Stderr)" }
            if (-not $stemToFetch) {
                $remoteStems = @(Get-SimcoreBackupStemsFromNames ($remoteList.Stdout -split "`r?`n"))
                $stemToFetch = Get-NewestStem $remoteStems
            }
            if (-not $stemToFetch) { Fail-Restore 4 "No local or remote simcore backup was found." }
            $script:TempDownload = Join-Path $BackupDir ("download-" + [guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Force -Path $script:TempDownload | Out-Null
            foreach ($ext in @(".dump", ".sha256", ".json")) {
                $fetched = Invoke-SimcoreRclone -ArgumentList @("copyto", "$Remote/$stemToFetch$ext", (Join-Path $script:TempDownload "$stemToFetch$ext"))
                if ($fetched.ExitCode -ne 0) { Fail-Restore 4 "Could not download $stemToFetch$ext. $($fetched.Stderr)" }
            }
            $DumpPath = Join-Path $script:TempDownload "$stemToFetch.dump"
            $downloaded = $true
            Write-BackupLog "downloaded $stemToFetch"
        }
    } else {
        Fail-Restore 5 "Pass -DumpPath or -RemoteStem. A restore does not pick a backup for you. -Drill may use the latest backup."
    }

    $stem = Get-BackupStemFromDump $DumpPath
    $shaPath = Join-Path (Split-Path -Parent $DumpPath) "$stem.sha256"
    $manifestPath = Join-Path (Split-Path -Parent $DumpPath) "$stem.json"
    if (-not (Test-Path -LiteralPath $shaPath)) { Fail-Restore 3 "Checksum file is missing: $shaPath" }
    if (-not (Test-Path -LiteralPath $manifestPath)) { Fail-Restore 3 "Manifest is missing: $manifestPath" }
    $verified = Invoke-RestorePython -ArgumentList @("verify-checksum", "--dump", $DumpPath, "--checksum", $shaPath) -ExitCode 3
    Write-BackupLog "checksum ok $($verified.Stdout.Trim())"

    $listOut = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-restore-list-" + [guid]::NewGuid().ToString("N") + ".txt")
    $listErr = $listOut + ".err"
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $tools.Restore --list $DumpPath 1> $listOut 2> $listErr
        $listCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    $listText = Read-SimcoreTempText $listOut
    if ($listCode -ne 0 -or [string]::IsNullOrWhiteSpace($listText)) {
        Fail-Restore 3 ("pg_restore --list rejected the dump. " + (Hide-SimcoreSecret (Read-SimcoreTempText $listErr)))
    }
    Remove-Item -LiteralPath $listOut, $listErr -Force -ErrorAction SilentlyContinue
    Write-BackupLog "pg_restore --list ok"

    $sizeResult = Invoke-RestorePython -ArgumentList @("database-size")
    $databaseBytes = [int64]($sizeResult.Stdout.Trim())
    $drive = Get-SimcoreDriveFreeSpace $BackupDir
    $extra = $databaseBytes
    if ($ReplaceLive) {
        $marginText = $DumpSizeMargin.ToString([System.Globalization.CultureInfo]::InvariantCulture)
        $budget = Invoke-SimcoreBackupPython -ArgumentList @(
            "disk-budget", "--free", [string]$drive.AvailableBytes,
            "--database-bytes", [string]$databaseBytes,
            "--margin", $marginText,
            "--minimum-free", [string]$MinimumFreeBytes
        )
        if ($budget.ExitCode -ne 0) { Fail-Restore 2 ($budget.Stderr + " " + $budget.Stdout) }
        $budgetDoc = $budget.Stdout | ConvertFrom-Json
        $extra = [int64]$budgetDoc.estimated_bytes + $databaseBytes
        $remaining = [int64]$drive.AvailableBytes - $extra
        if ($remaining -lt $MinimumFreeBytes) {
            Fail-Restore 2 "Not enough free disk for a safety dump plus a second copy of the database. FreeBytes=$($drive.AvailableBytes) NeedBytes=$extra MinimumFreeBytes=$MinimumFreeBytes."
        }
    } else {
        $remaining = [int64]$drive.AvailableBytes - $databaseBytes
        if ($remaining -lt $MinimumFreeBytes) {
            Fail-Restore 2 "Not enough free disk to restore into another database. FreeBytes=$($drive.AvailableBytes) DatabaseBytes=$databaseBytes MinimumFreeBytes=$MinimumFreeBytes."
        }
    }

    if ($ReplaceLive) {
        $backupScript = Join-Path $PSScriptRoot "backup.ps1"
        Write-BackupLog "taking a safety backup before touching the live database"
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $backupScript -RepoRoot $script:BackupRepo
        if ($LASTEXITCODE -ne 0) {
            Fail-Restore 4 "Safety backup failed (exit $LASTEXITCODE). The live database was not changed and services were not stopped."
        }
        foreach ($id in @("simcore-worker", "simcore-api")) {
            $svc = Get-Service -Name $id -ErrorAction SilentlyContinue
            if (-not $svc) { Fail-Restore 5 "Windows service $id is not installed. The live database was not changed." }
        }
        Write-BackupLog "stopping simcore-worker and simcore-api. Caddy and PostgreSQL stay up."
        foreach ($id in @("simcore-worker", "simcore-api")) {
            $svc = Get-Service -Name $id
            if ($svc.Status -ne "Stopped") { Stop-Service -Name $id -Force }
        }
        $script:StoppedGame = $true
    }

    $stamp = [datetime]::UtcNow.ToString("yyyyMMdd't'HHmmss'z'")
    if ($Drill) {
        $target = "simcore_drill_$stamp"
    } elseif ($ReplaceLive) {
        $target = "simcore_incoming_$stamp"
        $script:IncomingDatabase = $target
        $script:PreviousDatabase = "simcore_pre_restore_$stamp"
    } else {
        $target = $TargetDatabase
        if ($target -eq $live) {
            Fail-Restore 5 "Refusing to restore into the live database $live without -ReplaceLive and the confirmation phrase."
        }
    }
    if ($target -notmatch '^[a-z][a-z0-9_]{0,62}$') {
        Fail-Restore 5 "Refusing database name $target."
    }
    $exists = Invoke-RestorePython -ArgumentList @("database-exists", "--name", $target)
    if ($exists.Stdout.Trim() -eq "yes") {
        Fail-Restore 5 "Database $target already exists. Refusing to overwrite it. Drop that test database yourself if you mean to reuse the name. The live database was not changed."
    }

    Write-BackupLog "creating database $target"
    Invoke-RestorePython -ArgumentList @("create-database", "--name", $target, "--owner", [string]$db.user) | Out-Null
    if ($Drill) { $script:DrillDatabase = $target }

    $script:PgPass = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-pgpass-" + [guid]::NewGuid().ToString("N"))
    Invoke-RestorePython -ArgumentList @("write-pgpass", "--output", $script:PgPass, "--database", $target) | Out-Null
    $env:PGPASSFILE = $script:PgPass
    if (Test-Path Env:PGPASSWORD) { Remove-Item Env:PGPASSWORD }
    $restoreOut = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-pg-restore-" + [guid]::NewGuid().ToString("N") + ".txt")
    $restoreErr = $restoreOut + ".err"
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $tools.Restore -w --no-owner --no-privileges --exit-on-error --clean --if-exists -h ([string]$db.host) -p ([string]$db.port) -U ([string]$db.user) -d $target $DumpPath 1> $restoreOut 2> $restoreErr
        $restoreCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
        Clear-RestoreSecrets
    }
    if ($restoreCode -ne 0) {
        Fail-Restore 5 ("pg_restore failed. " + (Hide-SimcoreSecret (Read-SimcoreTempText $restoreErr)))
    }
    Remove-Item -LiteralPath $restoreOut, $restoreErr -Force -ErrorAction SilentlyContinue
    Write-BackupLog "pg_restore loaded $target"

    # $Drill is the script switch. PowerShell variable names are not case-sensitive,
    # so a result named $drill would be assigned onto that [switch] and Windows
    # PowerShell 5.1 refuses the PSCustomObject. A drill is not an off-site backup
    # and does not rewrite backup-status.json.
    $drillReport = Invoke-SimcoreBackupPython -ArgumentList @("drill", "--manifest", $manifestPath, "--database-name", $target)
    $verdict = ""
    foreach ($line in ([string]$drillReport.Stdout -split "`r?`n")) {
        if (-not [string]::IsNullOrWhiteSpace($line)) { $verdict = $line.Trim(); break }
    }
    $passed = ($drillReport.ExitCode -eq 0 -and $verdict -eq "PASS")
    if ($passed) {
        Write-BackupLog "drill result PASS"
    } else {
        Write-BackupLog "drill result FAIL"
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$drillReport.Stdout)) {
        Write-BackupLog ([string]$drillReport.Stdout).Trim()
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$drillReport.Stderr)) {
        Write-BackupLog ([string]$drillReport.Stderr).Trim() "ERROR"
    }
    if (-not $passed) {
        Fail-Restore 5 "Drill checks returned FAIL for $target. This is not a successful restore."
    }
    Write-BackupLog "PASS $target"

    if ($ReplaceLive) {
        Write-BackupLog "renaming $live to $($script:PreviousDatabase)"
        Invoke-RestorePython -ArgumentList @("rename-database", "--source", $live, "--dest", $script:PreviousDatabase, "--allow-live") | Out-Null
        $script:RenamedAway = $true
        Write-BackupLog "renaming $target to $live"
        Invoke-RestorePython -ArgumentList @("rename-database", "--source", $target, "--dest", $live, "--allow-live") | Out-Null
        $script:Swapped = $true
        $script:IncomingDatabase = ""
        Write-BackupLog "alembic upgrade head"
        $hadUrl = Test-Path Env:SIMCORE_DATABASE_URL
        $previousUrl = $env:SIMCORE_DATABASE_URL
        $env:SIMCORE_DATABASE_URL = [string]$script:BackupDatabaseUrl
        Push-Location $script:BackupRepo
        try {
            & $script:BackupPython -m alembic upgrade head
            if ($LASTEXITCODE -ne 0) { Fail-Restore 5 "alembic upgrade head failed (exit $LASTEXITCODE). The restored database is live. Previous database: $($script:PreviousDatabase)." }
        } finally {
            Pop-Location
            if ($hadUrl) { $env:SIMCORE_DATABASE_URL = $previousUrl } else { Remove-Item Env:SIMCORE_DATABASE_URL -ErrorAction SilentlyContinue }
        }
        Write-BackupLog "live database replaced. Previous database kept as $($script:PreviousDatabase). Drop that name yourself after you confirm the game."
    } elseif ($Drill) {
        Write-BackupLog "drill PASS. Dropping $target."
    } else {
        Write-BackupLog "restored into $target. The live database $live was not changed."
    }
    exit 0
} catch {
    $message = Hide-SimcoreSecret ([string]$_.Exception.Message)
    Write-BackupLog $message "ERROR"
    if ($script:RenamedAway -and -not $script:Swapped -and $script:PreviousDatabase) {
        Write-BackupLog "trying to rename $($script:PreviousDatabase) back to the live name"
        try {
            Invoke-SimcoreBackupPython -ArgumentList @("rename-database", "--source", $script:PreviousDatabase, "--dest", $live, "--allow-live") | Out-Null
        } catch {
            Write-BackupLog "could not rename the previous database back" "ERROR"
        }
    }
    exit $script:FailureCode
} finally {
    Clear-RestoreSecrets
    if ($script:DrillDatabase) {
        try {
            Invoke-SimcoreBackupPython -ArgumentList @("drop-database", "--name", $script:DrillDatabase) | Out-Null
            Write-BackupLog "dropped $($script:DrillDatabase)"
        } catch {
            Write-BackupLog "could not drop $($script:DrillDatabase)" "ERROR"
        }
    }
    if ($script:IncomingDatabase -and -not $script:Swapped) {
        try {
            Invoke-SimcoreBackupPython -ArgumentList @("drop-database", "--name", $script:IncomingDatabase) | Out-Null
            Write-BackupLog "dropped $($script:IncomingDatabase)"
        } catch {
            Write-BackupLog "could not drop $($script:IncomingDatabase)" "ERROR"
        }
    }
    if ($script:TempDownload -and (Test-Path -LiteralPath $script:TempDownload)) {
        Remove-Item -LiteralPath $script:TempDownload -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($script:StoppedGame) {
        foreach ($id in @("simcore-api", "simcore-worker")) {
            try {
                $svc = Get-Service -Name $id -ErrorAction SilentlyContinue
                if ($svc -and $svc.Status -eq "Stopped") {
                    Write-BackupLog "starting $id"
                    Start-Service -Name $id
                }
            } catch {
                Write-BackupLog "could not start $id" "ERROR"
            }
        }
    }
}
