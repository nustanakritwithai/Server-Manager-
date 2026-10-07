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
    # The pull may have changed these helpers. Load the new copies before migrations.
    . "$PSScriptRoot\Common.ps1"
    Ensure-Venv -RepoRoot $RepoRoot
    $cfg = Read-SimcoreEnv $envFile
    if (-not $cfg["SIMCORE_INSTALL_ROOT"]) { $cfg["SIMCORE_INSTALL_ROOT"] = $installRoot }
    $playerSecretReady = Test-SimcorePlayerTokenSecret ([string]$cfg["SIMCORE_PLAYER_TOKEN_SECRET"])
    $completed = Complete-SimcoreProductionEnv -Map $cfg -InstallRoot $installRoot -ApiDomain ([string]$cfg["API_DOMAIN"]) -ApiPort ([string]$cfg["API_PORT"]) -AcmeEmail ([string]$cfg["ACME_EMAIL"])
    if ($completed.Changed) {
        Write-SimcoreEnv -Path $envFile -Map $completed.Map
    }
    if (-not $playerSecretReady) {
        Write-Host "A player token signing secret was stored in .env.prod. It was not printed."
    }
    $cfg = $completed.Map
    Import-SimcoreEnvToProcess -Path $envFile
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
    # Checked after Caddy is stopped. Ports are free when this host already fronts Apache,
    # and occupied when Apache still owns 80/443. In that case do not start Caddy.
    $foreignPorts = @(Get-ForeignWebListeners)
    if ($foreignPorts.Count -gt 0) {
        Set-SimcoreCaddyStartup -Mode Manual -Stop
        Start-SimcoreStack -SkipCaddy
        $started = $true
        Wait-SimcoreApi -Port $cfg["API_PORT"]
        Write-ForeignWebPortHelp -Foreign $foreignPorts -ScriptRoot $PSScriptRoot
        Write-Warning "Update finished without starting Caddy. The public API is not on port 443 until coexist-apache.ps1 succeeds."
        return
    }
    Start-SimcoreStack
    $started = $true
    Wait-SimcoreApi -Port $cfg["API_PORT"]
    Write-Host "Update complete. Public API: https://$($cfg['API_DOMAIN'])/health"
} finally {
    if (-not $started) {
        Write-Warning "Update failed. Attempting to start the services again."
        try {
            $foreignPorts = @(Get-ForeignWebListeners)
            if ($foreignPorts.Count -gt 0) {
                Set-SimcoreCaddyStartup -Mode Manual -Stop
                Start-SimcoreStack -SkipCaddy
            } else {
                Start-SimcoreStack
            }
        } catch { Write-Warning $_ }
    }
}
