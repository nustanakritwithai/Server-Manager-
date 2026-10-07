# Shared helpers for backup.ps1 and restore-backup.ps1. Dot-source this file; do not run it directly.
# Secrets from .env.prod stay in process memory. Do not print them.

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\Common.ps1"

function Hide-SimcoreSecret {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return "" }
    $cleaned = [regex]::Replace($Text, '(?i)(postgres(?:ql)?(?:\+[A-Za-z0-9]+)?:\/\/[^:\/\s]+:)[^@\s]+@', '$1***@')
    $cleaned = [regex]::Replace($cleaned, '(?i)\b(password|passwd|token|secret|pgpassword|api_key|authorization)\b(\s*[=:]\s*)(\S+)', '$1$2***')
    $cleaned = [regex]::Replace($cleaned, 'ya29\.[A-Za-z0-9_\-]+', 'ya29.***')
    return $cleaned
}

function Write-BackupLog {
    param([string]$Message, [string]$Level = "INFO")
    $safe = Hide-SimcoreSecret $Message
    $stamp = [datetime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    $line = "$stamp $Level $safe"
    if ($script:BackupLogPath) {
        $directory = Split-Path -Parent $script:BackupLogPath
        if ($directory -and -not (Test-Path -LiteralPath $directory)) {
            New-Item -ItemType Directory -Force -Path $directory | Out-Null
        }
        $utf8 = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::AppendAllText($script:BackupLogPath, $line + "`r`n", $utf8)
    }
    Write-Host $line
}

function Reset-BackupLogSize {
    if (-not $script:BackupLogPath) { return }
    if (-not (Test-Path -LiteralPath $script:BackupLogPath)) { return }
    $item = Get-Item -LiteralPath $script:BackupLogPath
    if ($item.Length -le 5MB) { return }
    $old = "$($script:BackupLogPath).1"
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Force }
    Move-Item -LiteralPath $script:BackupLogPath -Destination $old -Force
}

function Get-SimcoreEnvValue {
    param([string]$Name, [string]$Default)
    if ($null -eq $script:BackupEnv) { return $Default }
    $value = [string]$script:BackupEnv[$Name]
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function Resolve-SimcoreRepoRoot {
    param([string]$RepoRoot)
    if ($RepoRoot) { return $RepoRoot }
    if ($env:SIMCORE_ROOT) { return $env:SIMCORE_ROOT }
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Initialize-SimcoreBackupContext {
    param([string]$RepoRoot)
    $script:BackupRepo = Resolve-SimcoreRepoRoot $RepoRoot
    $envFile = Join-Path $script:BackupRepo ".env.prod"
    if (-not (Test-Path -LiteralPath $envFile)) {
        throw ".env.prod is missing at $envFile. Run deploy\windows\bootstrap.ps1 once."
    }
    $script:BackupEnv = Read-SimcoreEnv $envFile
    $script:BackupDatabaseUrl = [string]$script:BackupEnv["SIMCORE_DATABASE_URL"]
    if ([string]::IsNullOrWhiteSpace($script:BackupDatabaseUrl)) {
        throw "SIMCORE_DATABASE_URL is missing from .env.prod."
    }
    $script:BackupSuperPassword = [string]$script:BackupEnv["POSTGRES_SUPER_PASSWORD"]
    $install = Get-SimcoreEnvValue "SIMCORE_INSTALL_ROOT" "C:\simcore"
    $script:BackupInstallRoot = $install
    $script:BackupPython = Join-Path $script:BackupRepo ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $script:BackupPython)) {
        throw "Python virtualenv was not found at $($script:BackupPython). Run deploy\windows\update.ps1 once."
    }
}

function Find-SimcorePgTools {
    $psql = Find-Psql
    if (-not $psql) { throw "psql.exe was not found. Install PostgreSQL 16 first (deploy\windows\bootstrap.ps1)." }
    $bin = Split-Path -Parent $psql
    $dump = Join-Path $bin "pg_dump.exe"
    $restore = Join-Path $bin "pg_restore.exe"
    if (-not (Test-Path -LiteralPath $dump)) { throw "pg_dump.exe was not found next to $psql." }
    if (-not (Test-Path -LiteralPath $restore)) { throw "pg_restore.exe was not found next to $psql." }
    return [pscustomobject]@{ Psql = $psql; Dump = $dump; Restore = $restore }
}

function Find-SimcoreRclone {
    param([string]$InstallRoot)
    $path = Join-Path $InstallRoot "tools\rclone\rclone.exe"
    if (-not (Test-Path -LiteralPath $path)) {
        throw "rclone.exe was not found at $path. Run deploy\windows\install-backup-tools.ps1."
    }
    return $path
}

function Get-SimcoreHeadCommit {
    param([string]$RepoRoot)
    $git = Find-Git
    if (-not $git) { return "unknown" }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $sha = & $git -C $RepoRoot rev-parse HEAD 2>$null
        if ($LASTEXITCODE -ne 0) { return "unknown" }
        $text = ("" + $sha).Trim().ToLowerInvariant()
        if ($text -match '^[0-9a-f]{7,40}$') { return $text }
        return "unknown"
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Read-SimcoreTempText {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    return [System.IO.File]::ReadAllText($Path)
}

function Invoke-SimcoreBackupPython {
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [string]$StdinText = ""
    )
    if ([string]::IsNullOrWhiteSpace($script:BackupPython)) { throw "Backup Python is not initialized." }
    $stamp = [guid]::NewGuid().ToString("N")
    $outFile = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-backup-out-" + $stamp + ".txt")
    $errFile = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-backup-err-" + $stamp + ".txt")
    $hadUrl = Test-Path Env:SIMCORE_DATABASE_URL
    $previousUrl = $env:SIMCORE_DATABASE_URL
    $hadSuper = Test-Path Env:POSTGRES_SUPER_PASSWORD
    $previousSuper = $env:POSTGRES_SUPER_PASSWORD
    $previousPreference = $ErrorActionPreference
    try {
        if ($script:BackupDatabaseUrl) { $env:SIMCORE_DATABASE_URL = [string]$script:BackupDatabaseUrl }
        if (-not [string]::IsNullOrWhiteSpace([string]$script:BackupSuperPassword)) {
            $env:POSTGRES_SUPER_PASSWORD = [string]$script:BackupSuperPassword
        }
        $ErrorActionPreference = "Continue"
        if ([string]::IsNullOrEmpty($StdinText)) {
            & $script:BackupPython -m simcore.backup @ArgumentList 1> $outFile 2> $errFile
        } else {
            $StdinText | & $script:BackupPython -m simcore.backup @ArgumentList 1> $outFile 2> $errFile
        }
        $exitCode = $LASTEXITCODE
        return [pscustomobject]@{
            ExitCode = [int]$exitCode
            Stdout = (Hide-SimcoreSecret (Read-SimcoreTempText $outFile))
            Stderr = (Hide-SimcoreSecret (Read-SimcoreTempText $errFile))
        }
    } finally {
        $ErrorActionPreference = $previousPreference
        if ($hadUrl) { $env:SIMCORE_DATABASE_URL = $previousUrl } else { Remove-Item Env:SIMCORE_DATABASE_URL -ErrorAction SilentlyContinue }
        if ($hadSuper) { $env:POSTGRES_SUPER_PASSWORD = $previousSuper } else { Remove-Item Env:POSTGRES_SUPER_PASSWORD -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $outFile) { Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $errFile) { Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue }
    }
}

function Invoke-SimcoreRclone {
    param([Parameter(Mandatory = $true)][string[]]$ArgumentList)
    if ([string]::IsNullOrWhiteSpace($script:BackupRclone)) { throw "rclone is not initialized." }
    if ([string]::IsNullOrWhiteSpace($script:BackupRcloneConfig)) { throw "rclone config path is empty." }
    $stamp = [guid]::NewGuid().ToString("N")
    $outFile = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-rclone-out-" + $stamp + ".txt")
    $errFile = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-rclone-err-" + $stamp + ".txt")
    $had = Test-Path Env:RCLONE_CONFIG
    $previous = $env:RCLONE_CONFIG
    $previousPreference = $ErrorActionPreference
    try {
        $env:RCLONE_CONFIG = $script:BackupRcloneConfig
        $ErrorActionPreference = "Continue"
        & $script:BackupRclone @ArgumentList 1> $outFile 2> $errFile
        $exitCode = $LASTEXITCODE
        return [pscustomobject]@{
            ExitCode = [int]$exitCode
            Stdout = (Hide-SimcoreSecret (Read-SimcoreTempText $outFile))
            Stderr = (Hide-SimcoreSecret (Read-SimcoreTempText $errFile))
        }
    } finally {
        $ErrorActionPreference = $previousPreference
        if ($had) { $env:RCLONE_CONFIG = $previous } else { Remove-Item Env:RCLONE_CONFIG -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $outFile) { Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $errFile) { Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue }
    }
}

function Convert-SimcoreNameListJson {
    param([string[]]$Names)
    $quoted = @()
    foreach ($name in @($Names)) {
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        $escaped = ([string]$name).Replace('\', '\\').Replace('"', '\"')
        $quoted += ('"' + $escaped + '"')
    }
    return "[" + ($quoted -join ",") + "]"
}

function Get-SimcoreBackupStemsFromNames {
    param([string[]]$FileNames)
    $stems = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    foreach ($fileName in @($FileNames)) {
        $leaf = [System.IO.Path]::GetFileName([string]$fileName)
        if ($leaf -match '^(simcore-[A-Za-z0-9_-]+)\.(dump|sha256|json)$') {
            $stem = $Matches[1]
            if (-not $seen.ContainsKey($stem)) {
                $seen[$stem] = $true
                $stems.Add($stem)
            }
        }
    }
    return @($stems)
}

function Assert-SimcoreRemoteName {
    param([string]$Remote)
    if ($Remote -notmatch '^[A-Za-z0-9_-]+:[A-Za-z0-9_./-]+$') {
        throw "Remote must look like gdrive:simcore-backups. Refusing '$Remote'."
    }
}
