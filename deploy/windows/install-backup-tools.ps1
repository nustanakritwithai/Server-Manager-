# Install a pinned rclone build under C:\simcore\tools\rclone.
# The zip is checked against the SHA-256 published for that release, then deleted.
# This script does not contain an admin password, an OAuth token, or a database password.
# The owner configures the Google Drive remote afterwards with rclone config.

[CmdletBinding()]
param(
    [string]$InstallRoot = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\Common.ps1"
Assert-SimcoreElevated

# rclone v1.75.1 windows-amd64, from the release SHA256SUMS.
$PinnedVersion = "1.75.1"
$PinnedSha256 = "200eb602c126d82aa38b51e0f6b9ae837473ff99b51278d3f6f837574c494d6e"
$DownloadUrl = "https://github.com/rclone/rclone/releases/download/v$PinnedVersion/rclone-v$PinnedVersion-windows-amd64.zip"

if (-not $InstallRoot) {
    if ($env:SIMCORE_INSTALL_ROOT) { $InstallRoot = $env:SIMCORE_INSTALL_ROOT }
    else { $InstallRoot = $script:DefaultInstallRoot }
}

$destDir = Join-Path $InstallRoot "tools\rclone"
$dest = Join-Path $destDir "rclone.exe"
$backupDir = Join-Path $InstallRoot "backup"
$dumpsDir = Join-Path $InstallRoot "backups"
New-Item -ItemType Directory -Force -Path $destDir, $backupDir, $dumpsDir | Out-Null

$already = $false
if (Test-Path -LiteralPath $dest) {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $versionText = & $dest version 2>&1 | Out-String
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($versionText -match [regex]::Escape("v$PinnedVersion")) {
        Write-Host "rclone $PinnedVersion is already installed at $dest"
        $already = $true
    }
}

if (-not $already) {
    $zip = Join-Path $env:TEMP "rclone-v$PinnedVersion-windows-amd64.zip"
    $extract = Join-Path $env:TEMP "rclone-extract-$PinnedVersion"
    try {
        # 20 MB rejects an HTML error page. The real zip is about 30 MB.
        Save-SimcoreDownload $DownloadUrl $zip 20971520
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
        if ($hash -ne $PinnedSha256) {
            Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
            throw "rclone zip SHA-256 is $hash, expected $PinnedSha256. The file was deleted."
        }
        if (Test-Path -LiteralPath $extract) { Remove-Item -LiteralPath $extract -Recurse -Force }
        Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
        $found = Get-ChildItem -LiteralPath $extract -Filter "rclone.exe" -Recurse | Select-Object -First 1
        if (-not $found) { throw "rclone.exe was not in the zip." }
        Copy-Item -LiteralPath $found.FullName -Destination $dest -Force
        Write-Host "Installed rclone $PinnedVersion at $dest"
    } finally {
        if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $extract) { Remove-Item -LiteralPath $extract -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

$icacls = Get-Command icacls.exe -ErrorAction SilentlyContinue
if ($icacls) {
    & $icacls.Source $backupDir /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
    & $icacls.Source $dumpsDir /inheritance:r /grant:r "SYSTEM:(OI)(CI)M" "Administrators:(OI)(CI)F" | Out-Null
    if (Test-Path -LiteralPath $dest) {
        & $icacls.Source $dest /inheritance:r /grant:r "SYSTEM:(RX)" "Administrators:(F)" | Out-Null
    }
}

$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $dest version
    if ($LASTEXITCODE -ne 0) { throw "rclone version failed (exit $LASTEXITCODE)." }
} finally {
    $ErrorActionPreference = $previousPreference
}

$psql = Find-Psql
if (-not $psql) {
    throw "rclone is installed, but psql.exe was not found. Run deploy\windows\bootstrap.ps1 so PostgreSQL 16 and pg_dump are installed. PostgreSQL must stay on localhost."
}
$pgDump = Join-Path (Split-Path -Parent $psql) "pg_dump.exe"
if (-not (Test-Path -LiteralPath $pgDump)) {
    throw "pg_dump.exe was not found next to $psql."
}
Write-Host "pg_dump: $pgDump"
& $pgDump --version
if ($LASTEXITCODE -ne 0) { throw "pg_dump --version failed." }
Write-Host "Next: configure the Google Drive remote. See docs/BACKUP_DR.md. Do not put the OAuth token in the repo."
