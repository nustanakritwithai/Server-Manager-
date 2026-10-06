# One-time setup for the Windows Server VPS.
# Run from an elevated PowerShell after the repo is on the machine, or let this script clone it.

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$InstallRoot = "C:\simcore",
    [string]$ApiDomain = "157-85-96-139.sslip.io",
    [string]$ApiPort = "8741",
    [string]$AcmeEmail = "",
    [string]$RepoUrl = "https://github.com/nustanakritwithai/Server-Manager-.git"
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\Common.ps1"
Assert-SimcoreElevated

if (-not $RepoRoot) {
    $fromScript = Resolve-Path (Join-Path $PSScriptRoot "..\..")
    if (Test-Path (Join-Path $fromScript ".git")) {
        $RepoRoot = $fromScript.Path
    } else {
        $RepoRoot = Join-Path $InstallRoot "app"
    }
}

Write-Host "Install root: $InstallRoot"
Write-Host "Repo root:    $RepoRoot"
Write-Host "API domain:   $ApiDomain"

foreach ($dir in @(
    $InstallRoot,
    (Join-Path $InstallRoot "tools"),
    (Join-Path $InstallRoot "logs"),
    (Join-Path $InstallRoot "services"),
    (Join-Path $InstallRoot "caddy-data")
)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

# SYSTEM runs the services and the GitHub Actions runner, so it must be able to update the checkout.
& icacls $InstallRoot /grant "SYSTEM:(OI)(CI)F" | Out-Null

Write-Host "Installing Git, Python 3.12, Caddy, and WinSW if they are missing"
Ensure-Git
Ensure-Python
Ensure-Caddy -InstallRoot $InstallRoot | Out-Null
Ensure-WinSW -InstallRoot $InstallRoot | Out-Null
Ensure-Repo -RepoRoot $RepoRoot -RepoUrl $RepoUrl
& icacls $RepoRoot /grant "SYSTEM:(OI)(CI)M" | Out-Null

$envFile = Join-Path $RepoRoot ".env.prod"
$cfg = New-SimcoreProductionEnv -Path $envFile -InstallRoot $InstallRoot -ApiDomain $ApiDomain -ApiPort $ApiPort -AcmeEmail $AcmeEmail
# Re-running bootstrap must not rotate secrets. The file wins over the script parameters after the first write,
# except an explicit -ApiDomain on a later run is how you point Caddy at a real domain.
if ($PSBoundParameters.ContainsKey("ApiDomain") -and $cfg["API_DOMAIN"] -ne $ApiDomain) {
    $cfg["API_DOMAIN"] = $ApiDomain
    Write-SimcoreEnv -Path $envFile -Map $cfg
}
if ($PSBoundParameters.ContainsKey("AcmeEmail")) {
    $cfg["ACME_EMAIL"] = $AcmeEmail
    Write-SimcoreEnv -Path $envFile -Map $cfg
}
if ($PSBoundParameters.ContainsKey("InstallRoot") -or -not $cfg["SIMCORE_INSTALL_ROOT"]) {
    $cfg["SIMCORE_INSTALL_ROOT"] = $InstallRoot
}
if ($PSBoundParameters.ContainsKey("ApiPort") -or -not $cfg["API_PORT"]) {
    $cfg["API_PORT"] = $ApiPort
}
if (-not $cfg["API_DOMAIN"]) { $cfg["API_DOMAIN"] = $ApiDomain }
Write-SimcoreEnv -Path $envFile -Map $cfg

Write-Host "Installing PostgreSQL 16 if it is missing, then keeping it on localhost"
Ensure-PostgresInstalled -SuperPassword $cfg["POSTGRES_SUPER_PASSWORD"]
Disable-PublicPostgres
$layout = Find-PostgresLayout
if (-not $layout) { throw "PostgreSQL layout was not found after install." }
if (-not $layout.ServiceName) { throw "The PostgreSQL Windows service was not found." }
Set-PostgresListenLocalhost -DataDir $layout.Data
Restart-Service -Name $layout.ServiceName -Force
Start-Sleep -Seconds 3
Assert-PostgresLocalOnly
Initialize-SimcoreDatabase -Psql $layout.Psql -EnvMap $cfg

Write-Host "Creating the virtualenv, migrating, and seeding Alice/Bob if the database is empty"
Ensure-Venv -RepoRoot $RepoRoot
Import-SimcoreEnvToProcess -Path $envFile
Invoke-SimcoreMigrations -RepoRoot $RepoRoot

Stop-SiteBindings
Enable-WebFirewall
Write-SimcoreCaddyfile -EnvMap $cfg -InstallRoot $InstallRoot | Out-Null
Install-SimcoreWindowsServices -RepoRoot $RepoRoot -InstallRoot $InstallRoot -EnvMap $cfg -Reinstall

[Environment]::SetEnvironmentVariable("SIMCORE_ROOT", $RepoRoot, "Machine")
$env:SIMCORE_ROOT = $RepoRoot

Start-SimcoreStack
Wait-SimcoreApi -Port $cfg["API_PORT"]

Write-Host ""
Write-Host "Bootstrap finished."
Write-Host "Local health:  http://127.0.0.1:$($cfg['API_PORT'])/health/ready"
Write-Host "Public API:    https://$($cfg['API_DOMAIN'])/health"
Write-Host "Secrets file:  $envFile"
Write-Host "Service logs:  $(Join-Path $InstallRoot 'logs')"
Write-Host ""
Write-Host "PostgreSQL listens on 127.0.0.1 only. Windows Firewall allows inbound 80 and 443 and does not allow 5432."
Write-Host "RDP (3389) was not changed. If the hosting panel has its own firewall, open TCP 80 and 443 there too."
Write-Host "Let's Encrypt needs port 80 reachable from the internet. The first HTTPS request can take a minute."
Write-Host ""
Write-Host "Next, over RDP, install the GitHub Actions runner (see the README Deploy section)."
Write-Host "The web client default is https://$($script:DefaultDomain) when API_DOMAIN stays at that host."
