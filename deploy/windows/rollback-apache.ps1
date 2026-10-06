# One-command undo for coexist-apache.ps1.
# Restores the Apache config backup, puts Apache back on ports 80 and 443,
# re-enables the ACME tasks this deploy disabled, and leaves simcore-caddy Disabled
# so a reboot or a later service start cannot take those ports again.

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$InstallRoot = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\Common.ps1"
Assert-SimcoreElevated

if (-not $RepoRoot) {
    $fromScript = Resolve-Path (Join-Path $PSScriptRoot "..\..")
    if (Test-Path (Join-Path $fromScript ".git")) {
        $RepoRoot = $fromScript.Path
    } elseif ($env:SIMCORE_ROOT) {
        $RepoRoot = $env:SIMCORE_ROOT
    } else {
        $RepoRoot = "C:\simcore\app"
    }
}

$envFile = Join-Path $RepoRoot ".env.prod"
if (-not $InstallRoot -and (Test-Path -LiteralPath $envFile)) {
    $cfg = Read-SimcoreEnv $envFile
    $InstallRoot = [string]$cfg["SIMCORE_INSTALL_ROOT"]
}
if (-not $InstallRoot) { $InstallRoot = $script:DefaultInstallRoot }

$logDir = Join-Path $InstallRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
try { Start-Transcript -Path (Join-Path $logDir "rollback-apache.log") -Append | Out-Null } catch { }

try {
    $state = Read-ApacheCoexistState -InstallRoot $InstallRoot
    if (-not $state -or -not $state.backupDir) {
        throw "No coexistence state was found at $(Get-ApacheCoexistStatePath $InstallRoot). There is nothing to roll back."
    }
    $backup = [string]$state.backupDir
    if (-not (Test-Path -LiteralPath (Join-Path $backup "manifest.txt"))) {
        throw "The Apache backup manifest is missing: $backup"
    }

    Write-Host "Stopping Caddy and disabling its service so Apache can bind ports 80 and 443."
    Set-SimcoreCaddyStartup -Mode Disabled -Stop

    Write-Host "Restoring Apache configuration from $backup"
    Restore-ApacheBackup -BackupDir $backup

    $apache = [pscustomobject]@{
        Executable = [string]$state.httpd
        ServerRoot = [string]$state.serverRoot
        ConfigFile = [string]$state.configFile
        ServiceName = [string]$state.serviceName
    }
    if (-not $apache.Executable -or -not (Test-Path -LiteralPath $apache.Executable)) {
        $apache = Find-ApacheInstall
    }
    Invoke-ApacheConfigTest -Apache $apache
    Restart-ApacheServer -Apache $apache

    $listeners = @(Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue)
    $httpdOn80 = $false
    foreach ($conn in $listeners) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -match '^(?i)httpd$') { $httpdOn80 = $true }
    }
    if (-not $httpdOn80) {
        throw "Apache config was restored, but httpd is not listening on port 80."
    }
    Write-Host "Apache is listening on port 80 again."

    Enable-CompetingAcmeClients -State $state

    if (Test-Path -LiteralPath $envFile) {
        $cfg = Read-SimcoreEnv $envFile
        Remove-SimcoreApacheCoexistKeys -Map $cfg | Out-Null
        Write-SimcoreEnv -Path $envFile -Map $cfg
        Write-SimcoreCaddyfile -EnvMap $cfg -InstallRoot $InstallRoot | Out-Null
        Write-Host "Removed the Apache upstreams from .env.prod and wrote a single-site Caddyfile."
        Write-Host "simcore-caddy is Disabled, so that file is not serving traffic."
    }

    $statePath = Get-ApacheCoexistStatePath $InstallRoot
    $retired = Join-Path $InstallRoot ("apache-coexist-state.rolled-back-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".json")
    Move-Item -LiteralPath $statePath -Destination $retired -Force
    Write-Host "Moved coexistence state to $retired"
    Write-Host ""
    Write-Host "Rollback finished. Apache is back on public ports 80 and 443."
    Write-Host "Caddy will not start on boot. A later update.ps1 from this branch also leaves Caddy stopped while Apache holds those ports."
    Write-Host "Do not start the simcore-caddy service until you run coexist-apache.ps1 again."
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}
