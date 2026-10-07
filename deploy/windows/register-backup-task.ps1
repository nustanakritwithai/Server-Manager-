# Register or remove the daily simcore backup task.
# The task runs as SYSTEM whether or not anyone is logged on.
# It does not stop the game. pg_dump runs against the live local database.
# No password is stored in this script. SYSTEM reads C:\simcore\backup\rclone.conf.

[CmdletBinding()]
param(
    [string]$Time = "03:15",
    [string]$TaskName = "simcore-backup",
    [string]$RepoRoot = "",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\Common.ps1"
Assert-SimcoreElevated

if ($TaskName -notmatch '^[A-Za-z0-9_-]{1,64}$') {
    throw "Task name must be letters, numbers, dash, or underscore."
}

if ($Unregister) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "Scheduled task $TaskName is not registered."
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task $TaskName."
    exit 0
}

if ($Time -notmatch '^\d{2}:\d{2}$') {
    throw "Time must be HH:mm in the VPS local clock. Example: -Time 03:15"
}

if (-not $RepoRoot) {
    if ($env:SIMCORE_ROOT) { $RepoRoot = $env:SIMCORE_ROOT }
    else { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
}
$scriptPath = Join-Path $RepoRoot "deploy\windows\backup.ps1"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "backup.ps1 was not found at $scriptPath."
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`"" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -Description "Online pg_dump of the simcore database and upload through rclone. Does not stop simcore-api, simcore-worker, or simcore-caddy." | Out-Null
Write-Host "Registered scheduled task $TaskName daily at $Time local time, as SYSTEM, whether or not a user is logged on."
Write-Host "The task runs $scriptPath and does not stop the game services."
