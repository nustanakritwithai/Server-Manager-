# Put Caddy on public ports 80 and 443 without taking the existing Apache site offline.
# Caddy terminates TLS. 157-85-96-139.sslip.io is proxied to the simcore API.
# Every other HTTP host is proxied to Apache on 127.0.0.1:8080.
# https://<public-ip>/ is proxied to Apache's SSL vhost on 127.0.0.1:8443, with the
# original Host header, so redirects such as PocketMonster stay on Apache.
# Caddy obtains the Let's Encrypt short-lived IP certificate itself (Caddy 2.10.1+).
# Re-running this script is safe: config edits are idempotent, and the first backup
# remains the rollback target.

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$InstallRoot = "",
    [string]$PublicIp = ""
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
if (-not (Test-Path -LiteralPath $envFile)) {
    throw ".env.prod is missing at $envFile. Run deploy\windows\bootstrap.ps1 once before sharing ports with Apache."
}
$cfg = Read-SimcoreEnv $envFile
if (-not $InstallRoot) {
    $InstallRoot = [string]$cfg["SIMCORE_INSTALL_ROOT"]
}
if (-not $InstallRoot) { $InstallRoot = $script:DefaultInstallRoot }

$logDir = Join-Path $InstallRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
try { Start-Transcript -Path (Join-Path $logDir "coexist-apache.log") -Append | Out-Null } catch { }

$rollback = Get-SimcoreRollbackCommand -ScriptRoot $PSScriptRoot
Write-Host "Rollback (works after the backup below is written):"
Write-Host "  $rollback"
Write-Host ""

$envWritten = $false
$apacheRestarted = $false
$safetyBackup = ""
$phase = "edit"
$apache = $null
$acme = $null

try {
    try {
        Assert-PostgresLocalOnly
        Write-Host "PostgreSQL is listening on localhost only."
    } catch {
        if ($_ -match "must stay on localhost") { throw }
        Write-Warning "Could not verify that PostgreSQL is localhost-only: $_"
    }

    $caddy = Join-Path $InstallRoot "tools\caddy.exe"
    if (-not (Test-Path -LiteralPath $caddy)) {
        throw "Caddy was not found at $caddy. Run deploy\windows\bootstrap.ps1 once first."
    }
    Assert-CaddyIpCertificateSupport -CaddyPath $caddy

    foreach ($id in @("simcore-api", "simcore-worker", "simcore-caddy")) {
        if (-not (Get-Service -Name $id -ErrorAction SilentlyContinue)) {
            throw "Windows service $id is not installed. Run deploy\windows\bootstrap.ps1 once first."
        }
    }

    if (-not $PublicIp) { $PublicIp = Get-SimcorePublicIp $cfg }
    if ($PublicIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
        throw "Could not derive the public IPv4 address from API_DOMAIN. Re-run with -PublicIp 157.85.96.139."
    }

    Write-Host "Stopping Caddy so it cannot bind 80 or 443 while Apache moves."
    Set-SimcoreCaddyStartup -Mode Manual -Stop

    $apache = Find-ApacheInstall
    Write-Host "Apache executable: $($apache.Executable)"
    Write-Host "Apache service:    $(if ($apache.ServiceName) { $apache.ServiceName } else { '(no Windows service; process only)' })"
    Write-Host "Server root:       $($apache.ServerRoot)"
    Write-Host "Config file:       $($apache.ConfigFile)"

    $configFiles = @(Get-ApacheConfigClosure -ServerRoot $apache.ServerRoot -MainConfig $apache.ConfigFile)
    if ($configFiles.Count -eq 0) { throw "No readable Apache configuration files were found." }
    Write-Host "Configuration files:"
    foreach ($file in $configFiles) { Write-Host "  $file" }

    $listensOn80 = $false
    $listensOn443 = $false
    $usesServerPort = $false
    $serverNames = New-Object System.Collections.Generic.List[string]
    foreach ($file in $configFiles) {
        $text = [System.IO.File]::ReadAllText($file)
        if (Test-ApacheTextListensOnPort -Text $text -Port 80) { $listensOn80 = $true }
        if (Test-ApacheTextListensOnPort -Text $text -Port 443) { $listensOn443 = $true }
        if (Test-ApacheUsesServerPort -Text $text) { $usesServerPort = $true }
        foreach ($name in @(Get-ApacheServerNamesFromText $text)) { $serverNames.Add($name) }
    }
    if (-not $listensOn80) {
        throw "Apache has no uncommented Listen or VirtualHost on port 80. Refusing to rewrite the configuration."
    }
    if (-not $listensOn443) {
        throw "Apache has no uncommented Listen or VirtualHost on port 443. Refusing to continue, because https://$PublicIp/ would stop working."
    }
    if ($usesServerPort) {
        Write-Warning "An Apache config uses SERVER_PORT. After this change that value is 8080 or 8443, not 80 or 443. Check any redirect that prints the port."
    }

    $proxyHosts = @(Select-ApacheProxyHosts -Names $serverNames -ApiDomain ([string]$cfg["API_DOMAIN"]) -PublicIp $PublicIp)
    if ($proxyHosts.Count -gt 0) {
        Write-Host "Extra Apache hostnames that will get their own HTTPS site:"
        foreach ($name in $proxyHosts) { Write-Host "  $name" }
    } else {
        Write-Host "No extra Apache ServerName needs its own certificate. The public IP and plain HTTP still reach Apache."
    }

    $safetyBackup = Backup-ApacheFiles -InstallRoot $InstallRoot -Files $configFiles
    Write-Host "Backed up Apache config to $safetyBackup"
    $existing = Read-ApacheCoexistState -InstallRoot $InstallRoot
    $rollbackBackup = $safetyBackup
    $createdAt = (Get-Date).ToUniversalTime().ToString("o")
    if ($existing -and $existing.backupDir -and (Test-Path -LiteralPath ([string]$existing.backupDir))) {
        $rollbackBackup = [string]$existing.backupDir
        if ($existing.createdAt) { $createdAt = [string]$existing.createdAt }
        Write-Host "Keeping the original rollback backup at $rollbackBackup"
    }

    $changed = $false
    foreach ($file in $configFiles) {
        $original = [System.IO.File]::ReadAllText($file)
        $updated = Update-ApacheCoexistText $original
        if ($updated -ne $original) {
            $utf8 = New-Object System.Text.UTF8Encoding $false
            [System.IO.File]::WriteAllText($file, $updated, $utf8)
            Write-Host "Updated $file"
            $changed = $true
        } else {
            Write-Host "Unchanged $file"
        }
    }

    Invoke-ApacheConfigTest -Apache $apache

    $planned = Copy-SimcoreHashtable $cfg
    $planned["SIMCORE_APACHE_HTTP"] = "127.0.0.1:8080"
    $planned["SIMCORE_APACHE_HTTPS"] = "127.0.0.1:8443"
    $planned["SIMCORE_PUBLIC_IP"] = $PublicIp
    $planned["SIMCORE_APACHE_NAMES"] = ($proxyHosts -join ",")
    $caddyText = New-SimcoreCaddyfileText -EnvMap $planned -InstallRoot $InstallRoot
    $tempCaddy = Join-Path $env:TEMP ("simcore-Caddyfile-" + [guid]::NewGuid().ToString("N"))
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($tempCaddy, $caddyText, $utf8)
    Write-Host "Validating the Caddyfile"
    $validate = Invoke-SimcoreNative -FilePath $caddy -ArgumentList @("validate", "--config", ($tempCaddy -replace '\\', '/'))
    Remove-Item -LiteralPath $tempCaddy -Force -ErrorAction SilentlyContinue
    $validateOk = ($validate.ExitCode -eq 0) -or ($validate.Output -match 'Valid configuration')
    if (-not $validateOk) {
        throw "caddy validate failed.`n$($validate.Output)"
    }
    if ($validate.Output) { Write-Host $validate.Output }

    $phase = "publish"
    $acme = Disable-CompetingAcmeClients
    $keptTasks = @()
    $keptServices = @()
    if ($existing) {
        $keptTasks = @(ConvertTo-CoexistList $existing.disabledTasks)
        $keptServices = @(ConvertTo-CoexistList $existing.disabledServices)
    }
    $taskIds = @{}
    $mergedTasks = @()
    foreach ($task in @($keptTasks + @(ConvertTo-CoexistList $acme.Tasks))) {
        if ($null -eq $task -or -not $task.Name) { continue }
        $id = "$($task.Path)|$($task.Name)"
        if ($taskIds.ContainsKey($id)) { continue }
        $taskIds[$id] = $true
        $mergedTasks += $task
    }
    $serviceIds = @{}
    $mergedServices = @()
    foreach ($svc in @($keptServices + @(ConvertTo-CoexistList $acme.Services))) {
        if ($null -eq $svc -or -not $svc.Name) { continue }
        $id = [string]$svc.Name
        if ($serviceIds.ContainsKey($id)) { continue }
        $serviceIds[$id] = $true
        $mergedServices += $svc
    }
    if ($mergedTasks.Count -eq 0 -and $mergedServices.Count -eq 0) {
        Write-Host "No win-acme, certbot, or Certify scheduled task or service was found."
        Write-Host "Commented mod_md directives, if any, stay commented. Caddy renews the public IP certificate from now on."
        Write-Host "If another ACME client is still renewing the IP certificate, stop it before it can bind ports 80 or 443."
    } else {
        Write-Host "Caddy now requests the public certificate for $PublicIp (Let's Encrypt short-lived IP certificate) and for $($cfg['API_DOMAIN'])."
        Write-Host "Apache's existing certificate files stay on disk only for the localhost TLS hop. Leave the old ACME client disabled."
    }

    $state = [ordered]@{
        backupDir = $rollbackBackup
        httpd = [string]$apache.Executable
        serverRoot = [string]$apache.ServerRoot
        configFile = [string]$apache.ConfigFile
        serviceName = [string]$apache.ServiceName
        createdAt = $createdAt
        disabledTasks = @($mergedTasks)
        disabledServices = @($mergedServices)
    }
    Write-ApacheCoexistState -InstallRoot $InstallRoot -State $state | Out-Null

    Write-SimcoreEnv -Path $envFile -Map $planned
    $envWritten = $true
    Write-SimcoreCaddyfile -EnvMap $planned -InstallRoot $InstallRoot | Out-Null
    Enable-WebFirewall

    $alreadyLoopback = $false
    if (-not $changed) {
        $httpLocal = $false
        $httpsLocal = $false
        $httpdPublic = $false
        $httpConns = @(Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue)
        $httpsConns = @(Get-NetTCPConnection -LocalPort 8443 -State Listen -ErrorAction SilentlyContinue)
        foreach ($conn in $httpConns) {
            if ([string]$conn.LocalAddress -in @("127.0.0.1", "::1")) { $httpLocal = $true }
        }
        foreach ($conn in $httpsConns) {
            if ([string]$conn.LocalAddress -in @("127.0.0.1", "::1")) { $httpsLocal = $true }
        }
        foreach ($port in @(80, 443)) {
            foreach ($conn in @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
                $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
                if ($proc -and $proc.ProcessName -match '^(?i)httpd$') { $httpdPublic = $true }
            }
        }
        $alreadyLoopback = $httpLocal -and $httpsLocal -and -not $httpdPublic
    }

    $phase = "apache"
    if ($alreadyLoopback) {
        Write-Host "Apache is already on 127.0.0.1:8080 and 127.0.0.1:8443. Not restarting it."
    } else {
        $apacheRestarted = $true
        Restart-ApacheServer -Apache $apache
        Assert-ApacheLoopbackPorts
    }

    foreach ($id in @("simcore-api", "simcore-worker")) {
        $svc = Get-Service -Name $id
        if ($svc.Status -ne "Running") {
            Write-Host "Starting $id"
            Start-Service -Name $id
        }
    }

    $phase = "caddy"
    Set-Service -Name "simcore-caddy" -StartupType Automatic
    $caddySvc = Get-Service -Name "simcore-caddy"
    if ($caddySvc.Status -eq "Running") {
        Restart-Service -Name "simcore-caddy" -Force
    } else {
        Start-Service -Name "simcore-caddy"
    }
    Wait-SimcoreCaddyPort -RollbackCommand $rollback
    Wait-SimcoreApi -Port $planned["API_PORT"]

    Write-Host ""
    Write-Host "Coexistence is in place."
    Write-Host "Caddy owns public ports 80 and 443."
    Write-Host "  $($cfg['API_DOMAIN'])  ->  simcore API on 127.0.0.1:$($planned['API_PORT'])"
    Write-Host "  any other HTTP host    ->  Apache on 127.0.0.1:8080 (Apache's own redirects stay)"
    Write-Host "  https://$PublicIp/     ->  Apache SSL on 127.0.0.1:8443 (Host header preserved)"
    Write-Host "PostgreSQL stays on localhost. Firewall rules were not added for 8080 or 8443."
    Write-Host ""
    Write-Host "Verify from this machine:"
    Write-Host "  curl.exe -fsS https://$($cfg['API_DOMAIN'])/health"
    Write-Host "  curl.exe -sI https://$PublicIp/"
    Write-Host "The health URL should return {`"status`":`"ok`"}. The first request can take about a minute while certificates are issued."
    Write-Host "https://$PublicIp/ should still be the same Apache redirect as before."
    Write-Host "If those names do not hairpin back to this server, use:"
    Write-Host "  curl.exe --resolve $($cfg['API_DOMAIN']):443:127.0.0.1 https://$($cfg['API_DOMAIN'])/health"
    Write-Host "  curl.exe -sI --resolve ${PublicIp}:443:127.0.0.1 https://$PublicIp/"
    Write-Host ""
    Write-Host "Rollback:"
    Write-Host "  $rollback"
} catch {
    Write-Host ""
    Write-Host "Coexistence failed during phase '$phase': $_"
    if ($phase -eq "caddy") {
        Write-Host "Apache is already on localhost. Caddy did not take ports 80 and 443."
        Write-Host "Rollback:"
        Write-Host "  $rollback"
    } else {
        if ($safetyBackup -and (Test-Path -LiteralPath (Join-Path $safetyBackup "manifest.txt"))) {
            Write-Host "Restoring the Apache files from this attempt: $safetyBackup"
            Restore-ApacheBackup -BackupDir $safetyBackup
            try { Invoke-ApacheConfigTest -Apache $apache } catch { Write-Warning $_ }
        }
        if ($envWritten) {
            $restoredEnv = Copy-SimcoreHashtable (Read-SimcoreEnv $envFile)
            Remove-SimcoreApacheCoexistKeys -Map $restoredEnv | Out-Null
            Write-SimcoreEnv -Path $envFile -Map $restoredEnv
            Write-SimcoreCaddyfile -EnvMap $restoredEnv -InstallRoot $InstallRoot | Out-Null
        }
        if ($acme) {
            Enable-CompetingAcmeClients -State ([pscustomobject]@{
                disabledTasks = $acme.Tasks
                disabledServices = $acme.Services
            })
        }
        if ($apacheRestarted) {
            try {
                Restart-ApacheServer -Apache $apache
                Write-Host "Apache was restarted on its previous configuration."
            } catch {
                Write-Warning "Apache did not restart after the restore: $_"
                Write-Host "Rollback:"
                Write-Host "  $rollback"
            }
        } else {
            Write-Host "Apache was not restarted. The previous process is still the one listening."
            Write-Host "Caddy remains stopped so it does not fight for ports 80 and 443."
        }
    }
    throw
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}
