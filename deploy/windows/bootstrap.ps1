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

try {

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

Assert-SimcoreBootstrapDisk -InstallRoot $InstallRoot -RepoRoot $RepoRoot
Write-Host "Removing leftover installer downloads from TEMP"
Clear-SimcoreInstallerCache -TempRoot ([System.IO.Path]::GetTempPath())

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

Write-Host "Installing Git if it is missing"
Ensure-Git
Write-Host "Installing Python 3.12 if it is missing"
Ensure-Python
Write-Host "Installing Caddy if it is missing"
Ensure-Caddy -InstallRoot $InstallRoot | Out-Null
Write-Host "Installing WinSW if it is missing"
Ensure-WinSW -InstallRoot $InstallRoot | Out-Null
Write-Host "Checking the git checkout"
Ensure-Repo -RepoRoot $RepoRoot -RepoUrl $RepoUrl
& icacls $RepoRoot /grant "SYSTEM:(OI)(CI)M" | Out-Null

$envFile = Join-Path $RepoRoot ".env.prod"
Write-Host "Writing the production env"
$cfg = New-SimcoreProductionEnv -Path $envFile -InstallRoot $InstallRoot -ApiDomain $ApiDomain -ApiPort $ApiPort -AcmeEmail $AcmeEmail
if ($null -eq $cfg) { throw "Production env was not loaded from $envFile." }
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

Write-Host "Installing PostgreSQL 16 if it is missing"
Ensure-PostgresInstalled -SuperPassword $cfg["POSTGRES_SUPER_PASSWORD"]
Write-Host "Closing public PostgreSQL firewall rules"
Disable-PublicPostgres
Write-Host "Finding the PostgreSQL layout"
$layout = Find-PostgresLayout
if (-not $layout) { throw "PostgreSQL layout was not found after install." }
if (-not $layout.ServiceName) { throw "The PostgreSQL Windows service was not found." }
Write-Host "Pointing PostgreSQL at localhost"
Set-PostgresListenLocalhost -DataDir $layout.Data
Write-Host "Restarting the PostgreSQL service"
Restart-Service -Name $layout.ServiceName -Force
Start-Sleep -Seconds 3
Write-Host "Checking that PostgreSQL is localhost-only"
Assert-PostgresLocalOnly
Write-Host "Creating the simcore role and database"
Initialize-SimcoreDatabase -Psql $layout.Psql -EnvMap $cfg

Write-Host "Creating the virtualenv"
Ensure-Venv -RepoRoot $RepoRoot
Write-Host "Loading the production env"
Import-SimcoreEnvToProcess -Path $envFile
Write-Host "Running migrations and seed"
Invoke-SimcoreMigrations -RepoRoot $RepoRoot

Write-Host "Checking ports 80 and 443"
$foreignPorts = @(Get-ForeignWebListeners)
if ($foreignPorts.Count -eq 0) {
    Write-Host "Stopping IIS site bindings"
    Stop-SiteBindings
} else {
    Write-Host "Ports 80 and 443 are already taken. IIS will not be stopped, and Caddy will not be started."
}
Write-Host "Opening the web firewall"
Enable-WebFirewall
Write-Host "Writing the Caddyfile"
Write-SimcoreCaddyfile -EnvMap $cfg -InstallRoot $InstallRoot | Out-Null
Write-Host "Installing Windows services"
Install-SimcoreWindowsServices -RepoRoot $RepoRoot -InstallRoot $InstallRoot -EnvMap $cfg -Reinstall

[Environment]::SetEnvironmentVariable("SIMCORE_ROOT", $RepoRoot, "Machine")
$env:SIMCORE_ROOT = $RepoRoot

if ($foreignPorts.Count -gt 0) {
    Set-SimcoreCaddyStartup -Mode Manual -Stop
    Start-SimcoreStack -SkipCaddy
    Wait-SimcoreApi -Port $cfg["API_PORT"]
    Write-ForeignWebPortHelp -Foreign $foreignPorts -ScriptRoot $PSScriptRoot
    Write-Host "Bootstrap installed the API and the worker, then stopped before publishing Caddy."
    Write-Host "Local health:  http://127.0.0.1:$($cfg['API_PORT'])/health/ready"
    Write-Host "Secrets file:  $envFile"
    exit 2
}

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

} catch {
    Write-SimcoreFailure -Context "bootstrap.ps1" -ErrorRecord $_
    throw
}
