# Online custom-format pg_dump of the simcore database, then upload to rclone.
# Does not stop simcore-api, simcore-worker, or simcore-caddy.
# Credentials are read from .env.prod at runtime and are not printed.
#
# Exit codes: 0 ok, 2 disk, 3 dump or local verify, 4 upload or remote verify, 1 other.

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$BackupDir = "",
    [string]$Remote = "",
    [string]$RcloneConfig = "",
    [string]$LogPath = "",
    [string]$StatusPath = "",
    [int]$KeepLocal = 0,
    [int]$KeepDaily = 0,
    [int]$KeepWeekly = 0,
    [long]$MinimumFreeBytes = 0,
    [double]$DumpSizeMargin = 0
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\BackupLib.ps1"
Assert-SimcoreElevated

$script:FailureCode = 1
$partials = New-Object System.Collections.Generic.List[string]

function Fail-Backup {
    param([int]$Code, [string]$Message)
    $script:FailureCode = $Code
    throw $Message
}

function Remove-BackupPartial {
    foreach ($path in @($partials)) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Write-BackupStatus {
    param([switch]$Ok, [string]$ErrorMessage, [hashtable]$Meta)
    if (-not $script:BackupStatusPath) { return }
    $args = @("write-status", "--path", $script:BackupStatusPath)
    if ($Ok) {
        $args += @("--ok", "--uploaded")
    } elseif ($ErrorMessage) {
        $args += @("--error", (Hide-SimcoreSecret $ErrorMessage))
    } else {
        $args += @("--error", "backup failed")
    }
    if ($Meta) {
        foreach ($key in @("database", "dump-name", "sha256", "alembic-revision", "git-commit", "remote")) {
            $lookup = $key
            if ($Meta.ContainsKey($lookup) -and $Meta[$lookup]) { $args += @("--$key", [string]$Meta[$lookup]) }
        }
        if ($Meta.ContainsKey("size-bytes")) { $args += @("--size-bytes", [string]$Meta["size-bytes"]) }
    }
    $result = Invoke-SimcoreBackupPython -ArgumentList $args
    if ($result.ExitCode -ne 0) {
        Write-BackupLog "Could not write the backup status file. $($result.Stderr)" "ERROR"
    }
}

try {
    Initialize-SimcoreBackupContext -RepoRoot $RepoRoot
    if (-not $BackupDir) { $BackupDir = Get-SimcoreEnvValue "SIMCORE_BACKUP_DIR" "C:\simcore\backups" }
    if (-not $Remote) { $Remote = Get-SimcoreEnvValue "SIMCORE_BACKUP_REMOTE" "gdrive:simcore-backups" }
    if (-not $RcloneConfig) { $RcloneConfig = Get-SimcoreEnvValue "SIMCORE_BACKUP_RCLONE_CONFIG" (Join-Path $script:BackupInstallRoot "backup\rclone.conf") }
    if (-not $LogPath) { $LogPath = Get-SimcoreEnvValue "SIMCORE_BACKUP_LOG" (Join-Path $script:BackupInstallRoot "logs\backup.log") }
    if (-not $StatusPath) { $StatusPath = Get-SimcoreEnvValue "SIMCORE_BACKUP_STATUS_PATH" (Join-Path $script:BackupInstallRoot "backups\backup-status.json") }
    if ($KeepLocal -lt 1) { $KeepLocal = [int](Get-SimcoreEnvValue "SIMCORE_BACKUP_KEEP_LOCAL" "2") }
    if ($KeepDaily -lt 1) { $KeepDaily = [int](Get-SimcoreEnvValue "SIMCORE_BACKUP_KEEP_DAILY" "14") }
    if ($KeepWeekly -lt 1) { $KeepWeekly = [int](Get-SimcoreEnvValue "SIMCORE_BACKUP_KEEP_WEEKLY" "8") }
    if ($MinimumFreeBytes -lt 1) {
        $minMb = [int](Get-SimcoreEnvValue "SIMCORE_BACKUP_MIN_FREE_MB" "1024")
        $MinimumFreeBytes = [int64]$minMb * 1024 * 1024
    }
    if ($DumpSizeMargin -le 0) { $DumpSizeMargin = [double](Get-SimcoreEnvValue "SIMCORE_BACKUP_DUMP_MARGIN" "1.5") }

    $script:BackupLogPath = $LogPath
    $script:BackupStatusPath = $StatusPath
    $script:BackupRcloneConfig = $RcloneConfig
    Reset-BackupLogSize
    New-Item -ItemType Directory -Force -Path $BackupDir, (Split-Path -Parent $StatusPath) | Out-Null
    Assert-SimcoreRemoteName $Remote
    Write-BackupLog "backup start dir=$BackupDir remote=$Remote keep_local=$KeepLocal"

    $tools = Find-SimcorePgTools
    $script:BackupPgRestore = $tools.Restore
    $script:BackupRclone = Find-SimcoreRclone -InstallRoot $script:BackupInstallRoot
    if (-not (Test-Path -LiteralPath $RcloneConfig)) {
        Fail-Backup 4 "rclone config was not found at $RcloneConfig. Run rclone config once. See docs/BACKUP_DR.md. No dump was taken."
    }

    $infoResult = Invoke-SimcoreBackupPython -ArgumentList @("connection-info")
    if ($infoResult.ExitCode -ne 0) { Fail-Backup 1 $infoResult.Stderr }
    $db = $infoResult.Stdout | ConvertFrom-Json
    if ([string]$db.host -notin @("127.0.0.1", "localhost", "::1")) {
        Fail-Backup 1 "Refusing to dump host $($db.host). PostgreSQL must stay on localhost."
    }
    Write-BackupLog "database=$($db.database) host=$($db.host) port=$($db.port) user=$($db.user)"

    $remoteName = ($Remote -split ":", 2)[0] + ":"
    $listed = Invoke-SimcoreRclone -ArgumentList @("listremotes")
    if ($listed.ExitCode -ne 0) { Fail-Backup 4 "rclone listremotes failed. $($listed.Stderr)" }
    if ($listed.Stdout -notmatch ("(?m)^" + [regex]::Escape($remoteName) + "\s*$")) {
        Fail-Backup 4 "rclone has no remote $remoteName in $RcloneConfig. Configure it before the first backup. See docs/BACKUP_DR.md."
    }

    $sizeResult = Invoke-SimcoreBackupPython -ArgumentList @("database-size")
    if ($sizeResult.ExitCode -ne 0) { Fail-Backup 1 "Could not read pg_database_size. $($sizeResult.Stderr)" }
    $databaseBytes = [int64]($sizeResult.Stdout.Trim())
    $drive = Get-SimcoreDriveFreeSpace $BackupDir
    $marginText = $DumpSizeMargin.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    $budget = Invoke-SimcoreBackupPython -ArgumentList @(
        "disk-budget",
        "--free", [string]$drive.AvailableBytes,
        "--database-bytes", [string]$databaseBytes,
        "--margin", $marginText,
        "--minimum-free", [string]$MinimumFreeBytes
    )
    if ($budget.ExitCode -eq 2) {
        Fail-Backup 2 $budget.Stderr
    }
    if ($budget.ExitCode -ne 0) { Fail-Backup 2 "Disk check failed. $($budget.Stderr)" }
    Write-BackupLog "disk ok $($budget.Stdout.Trim())"

    $partial = Join-Path $BackupDir ("partial-" + [guid]::NewGuid().ToString("N") + ".dump")
    $factsPath = Join-Path $BackupDir ("partial-" + [guid]::NewGuid().ToString("N") + ".facts.json")
    $partials.Add($partial)
    $partials.Add($factsPath)
    Write-BackupLog "running pg_dump -Fc (custom format, compressed) without stopping the game"
    $dumped = Invoke-SimcoreBackupPython -ArgumentList @("dump", "--pg-dump", $tools.Dump, "--output", $partial, "--facts", $factsPath)
    if ($dumped.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $partial)) {
        Fail-Backup 3 "pg_dump failed. $($dumped.Stderr)"
    }
    if ($dumped.Stdout.Trim()) { Write-BackupLog "pg_dump used snapshot $($dumped.Stdout.Trim())" }

    $listOut = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-pg-restore-list-" + [guid]::NewGuid().ToString("N") + ".txt")
    $listErr = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-pg-restore-list-err-" + [guid]::NewGuid().ToString("N") + ".txt")
    $partials.Add($listOut)
    $partials.Add($listErr)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $tools.Restore --list $partial 1> $listOut 2> $listErr
        $listCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    $listText = Read-SimcoreTempText $listOut
    if ($listCode -ne 0 -or [string]::IsNullOrWhiteSpace($listText)) {
        Fail-Backup 3 ("pg_restore --list rejected the dump. " + (Hide-SimcoreSecret (Read-SimcoreTempText $listErr)))
    }
    Write-BackupLog "pg_restore --list ok"

    $facts = (Read-SimcoreTempText $factsPath) | ConvertFrom-Json
    $commit = Get-SimcoreHeadCommit -RepoRoot $script:BackupRepo
    $taken = [datetime]::UtcNow
    $stamp = $taken.ToString("yyyyMMddTHHmmssZ")
    $shortCommit = "nogit"
    if ($commit -match '^[0-9a-f]{7,40}$') { $shortCommit = $commit.Substring(0, [Math]::Min(12, $commit.Length)) }
    $revision = [regex]::Replace([string]$facts.alembic_revision, "[^A-Za-z0-9_]", "_")
    if (-not $revision) { $revision = "norev" }
    $stem = "simcore-$stamp-$shortCommit-$revision"
    $dumpName = "$stem.dump"
    $finalDump = Join-Path $BackupDir $dumpName
    $shaPath = Join-Path $BackupDir "$stem.sha256"
    $manifestPath = Join-Path $BackupDir "$stem.json"
    Move-Item -LiteralPath $partial -Destination $finalDump -Force
    $partials.Remove($partial) | Out-Null
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $finalDump).Hash.ToLowerInvariant()
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($shaPath, "$hash  $dumpName`n", $utf8)
    $sizeBytes = [int64](Get-Item -LiteralPath $finalDump).Length
    $manifestTime = $taken.ToString("yyyy-MM-ddTHH:mm:ssZ")
    $built = Invoke-SimcoreBackupPython -ArgumentList @(
        "build-manifest",
        "--facts", $factsPath,
        "--output", $manifestPath,
        "--time", $manifestTime,
        "--size-bytes", [string]$sizeBytes,
        "--sha256", $hash,
        "--git-commit", $commit
    )
    if ($built.ExitCode -ne 0) { Fail-Backup 3 "Could not write the manifest. $($built.Stderr)" }
    if (Test-Path -LiteralPath $factsPath) { Remove-Item -LiteralPath $factsPath -Force }
    Write-BackupLog "local dump $dumpName bytes=$sizeBytes sha256=$hash revision=$($facts.alembic_revision) commit=$commit"

    $stage = Join-Path $BackupDir ("stage-" + [guid]::NewGuid().ToString("N"))
    $partials.Add($stage)
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Copy-Item -LiteralPath $finalDump -Destination (Join-Path $stage $dumpName) -Force
    Copy-Item -LiteralPath $shaPath -Destination (Join-Path $stage "$stem.sha256") -Force
    Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $stage "$stem.json") -Force
    $madeDir = Invoke-SimcoreRclone -ArgumentList @("mkdir", $Remote)
    if ($madeDir.ExitCode -ne 0) { Fail-Backup 4 "rclone mkdir failed. $($madeDir.Stderr)" }
    $copied = Invoke-SimcoreRclone -ArgumentList @("copy", $stage, $Remote, "--checksum")
    if ($copied.ExitCode -ne 0) { Fail-Backup 4 "rclone copy failed. $($copied.Stderr)" }
    $checked = Invoke-SimcoreRclone -ArgumentList @("check", $stage, $Remote, "--one-way")
    if ($checked.ExitCode -ne 0) {
        Invoke-SimcoreRclone -ArgumentList @("deletefile", "$Remote/$dumpName") | Out-Null
        Invoke-SimcoreRclone -ArgumentList @("deletefile", "$Remote/$stem.sha256") | Out-Null
        Invoke-SimcoreRclone -ArgumentList @("deletefile", "$Remote/$stem.json") | Out-Null
        Fail-Backup 4 "rclone check failed. The remote copy was not kept. $($checked.Stderr)"
    }
    $remoteList = Invoke-SimcoreRclone -ArgumentList @("lsl", "$Remote/$dumpName")
    if ($remoteList.ExitCode -ne 0) { Fail-Backup 4 "rclone lsl failed. $($remoteList.Stderr)" }
    $remoteLine = ($remoteList.Stdout -split "`r?`n" | Where-Object { $_ -match [regex]::Escape($dumpName) } | Select-Object -First 1)
    if (-not $remoteLine) { Fail-Backup 4 "rclone lsl did not list $dumpName." }
    $remoteSize = [int64](($remoteLine.Trim() -split '\s+', 2)[0])
    if ($remoteSize -ne $sizeBytes) {
        Fail-Backup 4 "Remote size $remoteSize does not match local size $sizeBytes."
    }
    Write-BackupLog "upload verified remote=$Remote/$dumpName bytes=$remoteSize"

    $meta = @{
        "database" = [string]$db.database
        "dump-name" = $dumpName
        "size-bytes" = [string]$sizeBytes
        "sha256" = $hash
        "alembic-revision" = [string]$facts.alembic_revision
        "git-commit" = $commit
        "remote" = "$Remote/$dumpName"
    }
    Write-BackupStatus -Ok -Meta $meta

    $remoteNames = Invoke-SimcoreRclone -ArgumentList @("lsf", $Remote)
    if ($remoteNames.ExitCode -ne 0) { Fail-Backup 4 "Uploaded, but rclone lsf failed so remote retention did not run. $($remoteNames.Stderr)" }
    $remoteStems = @(Get-SimcoreBackupStemsFromNames ($remoteNames.Stdout -split "`r?`n"))
    $remoteNamesFile = Join-Path $BackupDir ("partial-" + [guid]::NewGuid().ToString("N") + ".names.json")
    $partials.Add($remoteNamesFile)
    $utf8Names = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($remoteNamesFile, (Convert-SimcoreNameListJson $remoteStems), $utf8Names)
    $remotePlan = Invoke-SimcoreBackupPython -ArgumentList @("retention", "--keep-daily", [string]$KeepDaily, "--keep-weekly", [string]$KeepWeekly, "--names-file", $remoteNamesFile)
    if ($remotePlan.ExitCode -ne 0) { Fail-Backup 4 "Uploaded, but remote retention failed. $($remotePlan.Stderr)" }
    $remoteParsed = $remotePlan.Stdout | ConvertFrom-Json
    $remoteDelete = @()
    if ($remoteParsed.delete) { $remoteDelete = @($remoteParsed.delete) }
    if ($remoteDelete -contains $stem) { Fail-Backup 4 "Remote retention tried to delete the backup that was just uploaded. Nothing was deleted." }
    foreach ($old in $remoteDelete) {
        if ($old -eq $stem) { continue }
        Write-BackupLog "prune remote $old"
        foreach ($ext in @(".dump", ".sha256", ".json")) {
            $gone = Invoke-SimcoreRclone -ArgumentList @("deletefile", "$Remote/$old$ext")
            if ($gone.ExitCode -ne 0 -and $gone.Stderr -notmatch "not found|doesn't exist|object not found") {
                Fail-Backup 4 "Could not delete remote $old$ext. $($gone.Stderr)"
            }
        }
    }

    $localFiles = @(Get-ChildItem -LiteralPath $BackupDir -Filter "simcore-*.dump" -File | ForEach-Object { $_.Name })
    $localStems = @(Get-SimcoreBackupStemsFromNames $localFiles)
    $localNamesFile = Join-Path $BackupDir ("partial-" + [guid]::NewGuid().ToString("N") + ".names.json")
    $partials.Add($localNamesFile)
    [System.IO.File]::WriteAllText($localNamesFile, (Convert-SimcoreNameListJson $localStems), $utf8Names)
    $localPlan = Invoke-SimcoreBackupPython -ArgumentList @("local-retention", "--keep", [string]$KeepLocal, "--names-file", $localNamesFile)
    if ($localPlan.ExitCode -ne 0) { Fail-Backup 1 "Uploaded, but local retention failed. $($localPlan.Stderr)" }
    $localParsed = $localPlan.Stdout | ConvertFrom-Json
    $localDelete = @()
    if ($localParsed.delete) { $localDelete = @($localParsed.delete) }
    if ($localDelete -contains $stem) { Fail-Backup 1 "Local retention tried to delete the new backup. Nothing else was deleted." }
    foreach ($old in $localDelete) {
        if ($old -eq $stem) { continue }
        Write-BackupLog "prune local $old"
        foreach ($ext in @(".dump", ".sha256", ".json")) {
            $path = Join-Path $BackupDir ($old + $ext)
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
        }
    }
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    Write-BackupLog "backup ok $dumpName"
    exit 0
} catch {
    $message = Hide-SimcoreSecret ([string]$_.Exception.Message)
    Write-BackupLog $message "ERROR"
    try { Write-BackupStatus -ErrorMessage $message } catch { Write-BackupLog "status update failed" "ERROR" }
    exit $script:FailureCode
} finally {
    Remove-BackupPartial
}
