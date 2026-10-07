# Set the admin password hash in .env.prod and restart the API.
# The plaintext password is never printed, logged, or written to the file.
#
#   powershell -ExecutionPolicy Bypass -File C:\simcore\app\deploy\windows\set-admin-password.ps1
#   powershell -ExecutionPolicy Bypass -File C:\simcore\app\deploy\windows\set-admin-password.ps1 -Revoke
#
# -Revoke does not ask for a password. It increases SIMCORE_ADMIN_SESSION_VERSION
# and restarts simcore-api, which invalidates every existing session token.
# Rotating SIMCORE_ADMIN_SESSION_SECRET by hand does the same thing.

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$EnvFile = "",
    [switch]$Revoke
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
if (-not $EnvFile) {
    $EnvFile = Join-Path $RepoRoot ".env.prod"
}
if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw ".env.prod is missing at $EnvFile. Run deploy\windows\bootstrap.ps1 first."
}

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not $Revoke -and -not (Test-Path -LiteralPath $python)) {
    throw "The project virtualenv was not found at $python. Run deploy\windows\update.ps1 once so the venv exists."
}

function ConvertFrom-SimcoreSecureString {
    param([Security.SecureString]$Secure)
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Get-NextAdminSessionVersion {
    param([hashtable]$Map)
    $current = 1
    $raw = [string]$Map["SIMCORE_ADMIN_SESSION_VERSION"]
    if ($raw -match '^\d+$') {
        $current = [int]$raw
    }
    return ($current + 1)
}

function Update-SimcoreEnvKeys {
    param(
        [string]$Path,
        [hashtable]$Updates
    )
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $lines.Add($line)
    }
    $pending = @{}
    foreach ($key in $Updates.Keys) {
        $pending[$key] = [string]$Updates[$key]
    }
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $trim = $lines[$i].Trim()
        if (-not $trim -or $trim.StartsWith("#")) { continue }
        $eq = $trim.IndexOf("=")
        if ($eq -lt 1) { continue }
        $key = $trim.Substring(0, $eq).Trim()
        if ($pending.ContainsKey($key)) {
            $lines[$i] = "$key=$($pending[$key])"
            [void]$pending.Remove($key)
        }
    }
    foreach ($key in ($Updates.Keys | Sort-Object)) {
        if ($pending.ContainsKey($key)) {
            $lines.Add("$key=$($pending[$key])")
        }
    }
    $backup = "{0}.bak-{1}" -f $Path, (Get-Date -Format "yyyyMMdd-HHmmss-fff")
    Copy-Item -LiteralPath $Path -Destination $backup -Force
    $directory = Split-Path -Parent $Path
    if (-not $directory) { $directory = [System.IO.Directory]::GetCurrentDirectory() }
    $temp = Join-Path $directory (".env.prod.{0}.tmp" -f ([guid]::NewGuid().ToString("N")))
    try {
        [System.IO.File]::WriteAllLines($temp, $lines.ToArray(), $utf8)
        Move-Item -LiteralPath $temp -Destination $Path -Force
    } catch {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        }
        throw
    }
    Write-Host "Updated $Path. A backup was written beside it. Secrets were not printed."
}

function Get-AdminPasswordHash {
    param(
        [string]$PythonExe,
        [string]$WorkingDirectory,
        [string]$Password
    )
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $PythonExe
    $start.Arguments = "-m simcore.admin_password"
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $start
    if (-not $proc.Start()) {
        throw "Could not start the project Python. The password was not written."
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $bytes = $utf8.GetBytes($Password)
    $proc.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $proc.StandardInput.Close()
    $stdout = $proc.StandardOutput.ReadToEnd()
    $null = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    $hash = $stdout.Trim()
    if ($proc.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($hash) -or -not $hash.StartsWith("scrypt$")) {
        throw "Could not hash the admin password. Nothing was written."
    }
    return $hash
}

function Restart-SimcoreApiService {
    param([string]$Path)
    $svc = Get-Service -Name "simcore-api" -ErrorAction SilentlyContinue
    if (-not $svc) {
        throw "simcore-api is not installed. The env file was updated. Install the services, then start simcore-api."
    }
    Write-Host "Restarting simcore-api"
    Restart-Service -Name "simcore-api" -Force
    $cfg = Read-SimcoreEnv $Path
    $port = [string]$cfg["API_PORT"]
    if (-not $port) { $port = "8741" }
    Wait-SimcoreApi -Port $port
}

$map = Read-SimcoreEnv $EnvFile
$updates = @{}
$updates["SIMCORE_ADMIN_SESSION_VERSION"] = [string](Get-NextAdminSessionVersion -Map $map)

if ($Revoke) {
    Update-SimcoreEnvKeys -Path $EnvFile -Updates $updates
    Write-Host "Existing admin sessions were revoked. The password hash was not changed."
    Restart-SimcoreApiService -Path $EnvFile
    return
}

$first = Read-Host -Prompt "Admin password" -AsSecureString
$second = Read-Host -Prompt "Repeat admin password" -AsSecureString
$plain = ConvertFrom-SimcoreSecureString -Secure $first
$again = ConvertFrom-SimcoreSecureString -Secure $second
if ($plain -cne $again) {
    throw "The two passwords did not match. Nothing was written."
}
if ([string]::IsNullOrWhiteSpace($plain)) {
    throw "The password is empty. Nothing was written."
}

$hash = Get-AdminPasswordHash -PythonExe $python -WorkingDirectory $RepoRoot -Password $plain
$plain = $null
$again = $null
$updates["SIMCORE_ADMIN_PASSWORD_HASH"] = $hash
$hash = $null

$existingSecret = [string]$map["SIMCORE_ADMIN_SESSION_SECRET"]
if ([string]::IsNullOrWhiteSpace($existingSecret)) {
    $updates["SIMCORE_ADMIN_SESSION_SECRET"] = New-SimcoreSecret -Length 48
    Write-Host "A new admin session secret was stored in .env.prod. It was not printed."
} else {
    Write-Host "The existing admin session secret was left in place."
}

Update-SimcoreEnvKeys -Path $EnvFile -Updates $updates
Write-Host "The admin password hash was stored. Existing admin sessions were revoked. The password was not printed."
Restart-SimcoreApiService -Path $EnvFile
