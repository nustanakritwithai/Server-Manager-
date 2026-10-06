# Pull main, install, migrate, seed if the world is empty, and restart the Windows services.
# The self-hosted runner calls this on every push to main.

[CmdletBinding()]
param(
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\Common.ps1"
Assert-SimcoreElevated

if (-not $RepoRoot) {
    if ($env:SIMCORE_ROOT) {
        $RepoRoot = $env:SIMCORE_ROOT
    } else {
        $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
    }
}

$envFile = Join-Path $RepoRoot ".env.prod"
if (-not (Test-Path $envFile)) {
    throw ".env.prod is missing at $envFile. Run deploy\windows\bootstrap.ps1 once before updating."
}

$cfg = Read-SimcoreEnv $envFile
$installRoot = $cfg["SIMCORE_INSTALL_ROOT"]
if (-not $installRoot) { $installRoot = $script:DefaultInstallRoot }

$started = $false
try {
    Stop-SimcoreStack
    Write-Host "Updating $RepoRoot"
    Invoke-SimcoreGitPull -RepoRoot $RepoRoot
    Ensure-Venv -RepoRoot $RepoRoot
    Import-SimcoreEnvToProcess -Path $envFile
    # Re-read after pull in case the script itself changed, but keep the secrets file as the source of truth.
    $cfg = Read-SimcoreEnv $envFile
    if (-not $cfg["SIMCORE_INSTALL_ROOT"]) { $cfg["SIMCORE_INSTALL_ROOT"] = $installRoot }
    Invoke-SimcoreMigrations -RepoRoot $RepoRoot
    Disable-PublicPostgres
    Enable-WebFirewall
    $layout = Find-PostgresLayout
    if ($layout) {
        $localOnly = $true
        try {
            Assert-PostgresLocalOnly
        } catch {
            $localOnly = $false
        }
        if (-not $localOnly) {
            Set-PostgresListenLocalhost -DataDir $layout.Data
            Restart-Service -Name $layout.ServiceName -Force
            Start-Sleep -Seconds 3
            Assert-PostgresLocalOnly
        }
    }
    Write-SimcoreCaddyfile -EnvMap $cfg -InstallRoot $installRoot | Out-Null
    Install-SimcoreWindowsServices -RepoRoot $RepoRoot -InstallRoot $installRoot -EnvMap $cfg
    Start-SimcoreStack
    $started = $true
    Wait-SimcoreApi -Port $cfg["API_PORT"]
    Write-Host "Update complete. Public API: https://$($cfg['API_DOMAIN'])/health"
} finally {
    if (-not $started) {
        Write-Warning "Update failed. Attempting to start the services again."
        try { Start-SimcoreStack } catch { Write-Warning $_ }
    }
}
