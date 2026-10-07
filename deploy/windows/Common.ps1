# Shared helpers for bootstrap.ps1 and update.ps1. Dot-source this file; do not run it directly.

$ErrorActionPreference = "Stop"

function Assert-SimcoreElevated {
    # LocalSystem is not in the Administrators group, but the runner service uses it
    # and can still install services and change the firewall.
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin -and -not $identity.IsSystem) {
        throw "Run this from an elevated PowerShell (Administrator). The GitHub runner service must run as LocalSystem."
    }
}

$script:DefaultInstallRoot = "C:\simcore"
$script:DefaultDomain = "157-85-96-139.sslip.io"
$script:DefaultRepoUrl = "https://github.com/nustanakritwithai/Server-Manager-.git"
$script:ServiceIds = @("simcore-api", "simcore-worker", "simcore-caddy")

function New-SimcoreSecret {
    param([int]$Length = 40)
    $alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789".ToCharArray()
    $bytes = New-Object byte[] $Length
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    $chars = foreach ($b in $bytes) { $alphabet[$b % $alphabet.Length] }
    return -join $chars
}

function Update-SimcorePath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Test-SimcoreDownloadComplete {
    param([long]$ActualBytes, [long]$ExpectedBytes, [long]$MinimumBytes)
    if ($ExpectedBytes -gt 0) { return $ActualBytes -eq $ExpectedBytes }
    if ($MinimumBytes -lt 1) { $MinimumBytes = 1 }
    return $ActualBytes -ge $MinimumBytes
}

function Test-SimcoreFileMagic {
    param([int]$First, [int]$Second, [string]$Kind)
    if ($Kind -eq "exe") { return ($First -eq 0x4D -and $Second -eq 0x5A) }
    if ($Kind -eq "zip") { return ($First -eq 0x50 -and $Second -eq 0x4B) }
    return $true
}

function Get-SimcoreDownloadKind {
    param([string]$Path)
    $ext = [System.IO.Path]::GetExtension($Path)
    if ($ext -eq ".exe") { return "exe" }
    if ($ext -eq ".zip") { return "zip" }
    return "any"
}

function Get-SimcoreFileMagicBytes {
    param([string]$Path)
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        return @($stream.ReadByte(), $stream.ReadByte())
    } finally {
        $stream.Dispose()
    }
}

function Get-SimcoreContentLength {
    param([string]$Url)
    try {
        $head = Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing
        $value = [string]$head.Headers["Content-Length"]
        $parsed = [long]0
        if ($value -and [long]::TryParse($value, [ref]$parsed) -and $parsed -gt 0) { return $parsed }
    } catch {
        Write-Host "Could not read Content-Length for $Url. The download will be checked against the minimum size."
    }
    return [long]0
}

function Save-SimcoreDownload {
    param(
        [string]$Url,
        [string]$Destination,
        [long]$MinimumBytes = 1
    )
    $dir = Split-Path -Parent $Destination
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $kind = Get-SimcoreDownloadKind $Destination
    $expected = Get-SimcoreContentLength $Url
    if ($expected -gt 0 -and (Test-Path -LiteralPath $Destination)) {
        $existingLength = [long](Get-Item -LiteralPath $Destination).Length
        $existingMagic = Get-SimcoreFileMagicBytes $Destination
        $existingOk = (Test-SimcoreDownloadComplete -ActualBytes $existingLength -ExpectedBytes $expected -MinimumBytes $MinimumBytes) -and
            (Test-SimcoreFileMagic -First $existingMagic[0] -Second $existingMagic[1] -Kind $kind)
        if ($existingOk) {
            Write-Host "Using complete download $Destination ($existingLength bytes)"
            return
        }
    }
    $partial = "$Destination.partial"
    Write-Host "Downloading $Url"
    try {
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
        $response = Invoke-WebRequest -Uri $Url -OutFile $partial -UseBasicParsing -PassThru
        $actual = [long](Get-Item -LiteralPath $partial).Length
        $declared = [long]0
        $headerLength = [string]$response.Headers["Content-Length"]
        if ($headerLength) { [void][long]::TryParse($headerLength, [ref]$declared) }
        if ($declared -le 0) { $declared = $expected }
        if (-not (Test-SimcoreDownloadComplete -ActualBytes $actual -ExpectedBytes $declared -MinimumBytes $MinimumBytes)) {
            $wanted = $(if ($declared -gt 0) { "$declared" } else { "at least $MinimumBytes" })
            throw "Download of $Url is $actual bytes (expected $wanted). The incomplete file was discarded."
        }
        $magic = Get-SimcoreFileMagicBytes $partial
        if (-not (Test-SimcoreFileMagic -First $magic[0] -Second $magic[1] -Kind $kind)) {
            throw "Download of $Url is not a valid $kind file. The file was discarded."
        }
        if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
        [System.IO.File]::Move($partial, $Destination)
    } catch {
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue }
        throw
    }
}

function Read-SimcoreEnv {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path $Path)) { return $map }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $eq = $trimmed.IndexOf("=")
        if ($eq -lt 1) { continue }
        $key = $trimmed.Substring(0, $eq).Trim()
        $value = $trimmed.Substring($eq + 1)
        $map[$key] = $value
    }
    return $map
}

function Write-SimcoreEnv {
    param([string]$Path, [hashtable]$Map)
    $order = @(
        "SIMCORE_ENV",
        "SIMCORE_DATABASE_URL",
        "SIMCORE_ADMIN_TOKEN",
        "SIMCORE_ENABLE_ADMIN",
        "SIMCORE_EMBEDDED_WORKER",
        "SIMCORE_CORS_ORIGINS",
        "SIMCORE_WORKER_POLL_SECONDS",
        "API_DOMAIN",
        "API_PORT",
        "ACME_EMAIL",
        "POSTGRES_SUPER_PASSWORD",
        "SIMCORE_DB_PASSWORD",
        "SIMCORE_INSTALL_ROOT",
        "SIMCORE_APACHE_HTTP",
        "SIMCORE_APACHE_HTTPS",
        "SIMCORE_PUBLIC_IP",
        "SIMCORE_APACHE_NAMES"
    )
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Generated by deploy/windows/bootstrap.ps1. Do not commit this file.")
    foreach ($key in $order) {
        if ($Map.ContainsKey($key)) {
            $lines.Add("$key=$($Map[$key])")
        }
    }
    foreach ($key in ($Map.Keys | Sort-Object)) {
        if ($order -notcontains $key) {
            $lines.Add("$key=$($Map[$key])")
        }
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $directory = Split-Path -Parent $Path
    if (-not $directory) { $directory = [System.IO.Directory]::GetCurrentDirectory() }
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    # Write the whole file beside the destination, then replace it. A full disk
    # throws while the temporary file is incomplete and the previous .env.prod stays.
    $temp = Join-Path $directory (".{0}.{1}.tmp" -f (Split-Path -Leaf $Path), ([guid]::NewGuid().ToString("N")))
    try {
        [System.IO.File]::WriteAllLines($temp, $lines.ToArray(), $utf8)
        $written = [System.IO.File]::ReadAllText($temp, $utf8)
        if ([string]::IsNullOrWhiteSpace($written)) {
            throw "Refusing to replace $Path with an empty file."
        }
        if (Test-Path -LiteralPath $Path) {
            [System.IO.File]::Replace($temp, $Path, [NullString]::Value)
        } else {
            [System.IO.File]::Move($temp, $Path)
        }
    } catch {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
        if ($_.Exception.Message -match "not enough space|enough space on the disk|disk full|No space left") {
            throw "Could not write $Path because the disk is full. Any previous copy of the file was left in place. Free space, then run bootstrap again."
        }
        throw
    }
    $icacls = Get-Command icacls.exe -ErrorAction SilentlyContinue
    if ($icacls) {
        & $icacls.Source $Path /inheritance:r /grant:r "SYSTEM:(R)" "Administrators:(F)" | Out-Null
    }
}

function Import-SimcoreEnvToProcess {
    param([string]$Path)
    $map = Read-SimcoreEnv $Path
    foreach ($key in $map.Keys) {
        [Environment]::SetEnvironmentVariable($key, [string]$map[$key], "Process")
    }
}

function Find-Python312 {
    Update-SimcorePath
    $candidates = @(
        "$env:ProgramFiles\Python312\python.exe",
        "${env:ProgramFiles(x86)}\Python312\python.exe",
        "$env:LocalAppData\Programs\Python\Python312\python.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        $version = & $cmd.Source -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
        if ($version -eq "3.12") { return $cmd.Source }
    }
    return $null
}

function Find-Git {
    Update-SimcorePath
    $candidates = @(
        "$env:ProgramFiles\Git\cmd\git.exe",
        "${env:ProgramFiles(x86)}\Git\cmd\git.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    $cmd = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Find-Psql {
    $roots = Get-ChildItem "$env:ProgramFiles\PostgreSQL" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending
    foreach ($root in $roots) {
        $psql = Join-Path $root.FullName "bin\psql.exe"
        if (Test-Path $psql) { return $psql }
    }
    return $null
}

function Find-PostgresLayout {
    $psql = Find-Psql
    if (-not $psql) { return $null }
    $root = Split-Path (Split-Path $psql -Parent) -Parent
    $service = Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "postgresql*" } |
        Sort-Object Name -Descending |
        Select-Object -First 1
    return @{
        Psql = $psql
        Root = $root
        Data = Join-Path $root "data"
        ServiceName = $(if ($service) { $service.Name } else { $null })
    }
}

function Install-WithWinget {
    param([string]$Id, [string]$Override = "")
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    Write-Host "winget install $Id"
    $args = @(
        "install", "--id", $Id, "-e",
        "--accept-package-agreements", "--accept-source-agreements",
        "--disable-interactivity"
    )
    if ($Override) { $args += @("--override", $Override) }
    & winget.exe @args
    # 0 = installed. -1978335189 (0x8A15002B) = no update / already installed.
    if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335189) { return $true }
    Write-Warning (Format-WingetFailureMessage -Id $Id -ExitCode $LASTEXITCODE)
    return $false
}

function Format-WingetFailureMessage {
    param([string]$Id, $ExitCode)
    $numeric = 0
    if (-not [int]::TryParse([string]$ExitCode, [ref]$numeric)) {
        return "winget could not install ${Id}: exit $ExitCode."
    }
    $hex = "0x{0:X8}" -f $numeric
    # 0x8A150006 is APPINSTALLER_CLI_ERROR_SHELLEXEC_INSTALL_FAILED: winget started
    # the package's installer and that process returned an error. For PostgreSQL
    # the usual causes are an empty superuser password, a half-finished install,
    # or a full disk. winget's own disk-full code is the different value 0x8A150105.
    if ($hex -eq "0x8A150006") {
        return "winget could not install ${Id}: exit $numeric ($hex, SHELLEXEC_INSTALL_FAILED). The installer program ran and returned an error. An empty superuser password, a half-finished PostgreSQL install, or a full disk are the usual causes."
    }
    return "winget could not install ${Id}: exit $numeric ($hex)."
}

function Assert-SimcoreSecretPresent {
    param([string]$Value, [string]$Name)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is empty. Refusing to run an installer without it. Run deploy\windows\bootstrap.ps1 again so it can fill a blank .env.prod, or restore that file from a backup."
    }
}

function Assert-SimcoreInstallerArguments {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )
    if ([string]::IsNullOrWhiteSpace($FilePath)) {
        throw "Installer path is empty."
    }
    if ($null -eq $ArgumentList) { return }
    foreach ($arg in $ArgumentList) {
        if ($null -eq $arg -or [string]::IsNullOrWhiteSpace([string]$arg)) {
            throw "Refusing to start $FilePath because an installer argument is empty. This usually means a password in .env.prod was blank."
        }
    }
}

function Test-SimcoreInstallerExit {
    param($ExitCode)
    if ($null -eq $ExitCode -or [string]$ExitCode -eq "") { return $false }
    $code = [int]$ExitCode
    # 3010 is the Windows installer code for success plus a reboot.
    return ($code -eq 0 -or $code -eq 3010)
}

function Start-SimcoreInstaller {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )
    Assert-SimcoreInstallerArguments -FilePath $FilePath -ArgumentList $ArgumentList
    if ($null -eq $ArgumentList) { $ArgumentList = @() }
    return Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -Wait -PassThru
}

function Ensure-Git {
    if (Find-Git) { return }
    Install-WithWinget -Id "Git.Git" | Out-Null
    Update-SimcorePath
    if (Find-Git) { return }
    throw "Git is not installed and winget could not install Git.Git. Install Git for Windows, reopen PowerShell, and run bootstrap again."
}

function Ensure-Python {
    if (Find-Python312) { return }
    Install-WithWinget -Id "Python.Python.3.12" | Out-Null
    Update-SimcorePath
    if (Find-Python312) { return }
    $installer = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
    Save-SimcoreDownload "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" $installer 1000000
    $proc = Start-SimcoreInstaller -FilePath $installer -ArgumentList @(
        "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0", "Include_pip=1"
    )
    if (-not (Test-SimcoreInstallerExit $proc.ExitCode)) {
        throw "Python installer failed with exit code $($proc.ExitCode)."
    }
    Update-SimcorePath
    if (-not (Find-Python312)) { throw "Python 3.12 installed but python.exe was not found." }
}

function Ensure-Caddy {
    param([string]$InstallRoot)
    $dest = Join-Path $InstallRoot "tools\caddy.exe"
    if (Test-Path $dest) { return $dest }
    $zip = Join-Path $env:TEMP "caddy_2.11.7_windows_amd64.zip"
    $extract = Join-Path $env:TEMP "caddy-extract"
    Save-SimcoreDownload "https://github.com/caddyserver/caddy/releases/download/v2.11.7/caddy_2.11.7_windows_amd64.zip" $zip 1000000
    if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $extract -Force
    $found = Get-ChildItem $extract -Filter "caddy.exe" -Recurse | Select-Object -First 1
    if (-not $found) { throw "caddy.exe was not in the zip" }
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Copy-Item $found.FullName $dest -Force
    return $dest
}

function Ensure-WinSW {
    param([string]$InstallRoot)
    $dest = Join-Path $InstallRoot "tools\WinSW.NET4.exe"
    if (-not (Test-Path $dest)) {
        Save-SimcoreDownload "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW.NET4.exe" $dest 1000000
    }
    return $dest
}

function Ensure-PostgresInstalled {
    param([string]$SuperPassword)
    if (Find-Psql) { return }
    Assert-SimcoreSecretPresent -Value $SuperPassword -Name "POSTGRES_SUPER_PASSWORD"
    $override = "--mode unattended --unattendedmodeui none --superpassword $SuperPassword --serverport 5432 --enable-components server,commandlinetools"
    $wingetOk = Install-WithWinget -Id "PostgreSQL.PostgreSQL.16" -Override $override
    Update-SimcorePath
    if (Find-Psql) { return }
    if (-not $wingetOk) {
        Write-Host "winget did not install PostgreSQL. Downloading the EnterpriseDB installer instead."
    }
    $installer = Join-Path $env:TEMP "postgresql-16.15-5-windows-x64.exe"
    # postgresql-16.15-5-windows-x64.exe was 404741880 bytes on 2026-10-06.
    # 300 MB rejects an error page or a short download when Content-Length is missing.
    Save-SimcoreDownload "https://get.enterprisedb.com/postgresql/postgresql-16.15-5-windows-x64.exe" $installer 314572800
    $proc = Start-SimcoreInstaller -FilePath $installer -ArgumentList @(
        "--mode", "unattended",
        "--unattendedmodeui", "none",
        "--superpassword", $SuperPassword,
        "--serverport", "5432",
        "--enable-components", "server,commandlinetools"
    )
    if (-not (Test-SimcoreInstallerExit $proc.ExitCode)) {
        $codeText = "(no exit code)"
        if ($null -ne $proc -and $null -ne $proc.ExitCode) { $codeText = [string]$proc.ExitCode }
        throw "PostgreSQL installer failed with exit code $codeText. The download size was checked before it ran. Log: $env:TEMP\install-postgresql.log"
    }
    Update-SimcorePath
    if (-not (Find-Psql)) { throw "PostgreSQL installed but psql.exe was not found." }
}

function Set-PostgresListenLocalhost {
    param([string]$DataDir)
    $conf = Join-Path $DataDir "postgresql.conf"
    if (-not (Test-Path $conf)) { throw "postgresql.conf not found at $conf" }
    $lines = [System.Collections.Generic.List[string]]::new()
    $found = $false
    foreach ($line in [System.IO.File]::ReadAllLines($conf)) {
        if ($line -match '^\s*#?\s*listen_addresses\s*=') {
            if (-not $found) { $lines.Add("listen_addresses = 'localhost'") }
            $found = $true
        } else {
            $lines.Add($line)
        }
    }
    if (-not $found) { $lines.Add("listen_addresses = 'localhost'") }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($conf, $lines, $utf8)
}

function Disable-PublicPostgres {
    $named = Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {
        $_.DisplayName -match 'PostgreSQL|postgres'
    }
    foreach ($rule in $named) {
        Disable-NetFirewallRule -Name $rule.Name
        Write-Host "Disabled firewall rule: $($rule.DisplayName)"
    }
    $inbound = Get-NetFirewallRule -Direction Inbound -Action Allow -ErrorAction SilentlyContinue
    foreach ($rule in $inbound) {
        $filter = $rule | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
        $ports = @($filter.LocalPort)
        if ($ports -contains 5432 -or $ports -contains "5432") {
            Disable-NetFirewallRule -Name $rule.Name
            Write-Host "Disabled inbound 5432 rule: $($rule.DisplayName)"
        }
    }
}

function Enable-WebFirewall {
    foreach ($spec in @(
        @{ Name = "Simcore HTTP (80)"; Port = 80 },
        @{ Name = "Simcore HTTPS (443)"; Port = 443 }
    )) {
        $existing = Get-NetFirewallRule -DisplayName $spec.Name -ErrorAction SilentlyContinue
        if (-not $existing) {
            New-NetFirewallRule -DisplayName $spec.Name -Direction Inbound -Action Allow -Protocol TCP -LocalPort $spec.Port | Out-Null
            Write-Host "Opened TCP $($spec.Port)"
        } else {
            Enable-NetFirewallRule -DisplayName $spec.Name
        }
    }
}

function Stop-SiteBindings {
    $iis = Get-Service -Name "W3SVC" -ErrorAction SilentlyContinue
    if ($iis) {
        if ($iis.Status -ne "Stopped") {
            Write-Host "Stopping IIS (W3SVC) so Caddy can listen on 80 and 443"
            Stop-Service -Name "W3SVC" -Force
        }
        Set-Service -Name "W3SVC" -StartupType Disabled
    }
}

function Assert-PostgresLocalOnly {
    $listeners = @(Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue)
    if (-not $listeners) {
        throw "PostgreSQL is not listening on port 5432."
    }
    foreach ($listener in $listeners) {
        $address = [string]$listener.LocalAddress
        if ($address -notin @("127.0.0.1", "::1")) {
            throw "PostgreSQL is listening on ${address}:5432. It must stay on localhost."
        }
    }
}

function Invoke-Psql {
    param(
        [string]$Psql,
        [string]$Database = "postgres",
        [string]$Command,
        [hashtable]$Variables
    )
    # -w fails instead of prompting if the password is wrong, so the script cannot hang on a password prompt.
    $args = @("-w", "-U", "postgres", "-h", "127.0.0.1", "-d", $Database, "-v", "ON_ERROR_STOP=1", "-tA")
    if ($Variables) {
        foreach ($key in $Variables.Keys) {
            $args += @("-v", "${key}=$($Variables[$key])")
        }
    }
    if ($Command) { $args += @("-c", $Command) }
    $output = & $Psql @args
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $Command" }
    return $output
}

function Test-PostgresLogin {
    param([string]$Psql, [string]$Password)
    $previous = $env:PGPASSWORD
    $env:PGPASSWORD = $Password
    & $Psql -w -U postgres -h 127.0.0.1 -d postgres -c "SELECT 1" | Out-Null
    $ok = ($LASTEXITCODE -eq 0)
    if ($null -ne $previous) { $env:PGPASSWORD = $previous } else { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
    return $ok
}

function Initialize-SimcoreDatabase {
    param([string]$Psql, [hashtable]$EnvMap)
    $super = $EnvMap["POSTGRES_SUPER_PASSWORD"]
    $dbPass = $EnvMap["SIMCORE_DB_PASSWORD"]
    if (-not (Test-PostgresLogin -Psql $Psql -Password $super)) {
        if (Test-PostgresLogin -Psql $Psql -Password "postgres") {
            $env:PGPASSWORD = "postgres"
            Invoke-Psql -Psql $Psql -Command "ALTER ROLE postgres WITH PASSWORD :'simpass';" -Variables @{ simpass = $super } | Out-Null
            Write-Host "Rotated the postgres superuser password away from the installer default."
        } else {
            throw "Could not sign in as postgres. Set POSTGRES_SUPER_PASSWORD in .env.prod to the current password and run bootstrap again."
        }
    }
    $env:PGPASSWORD = $super
    $role = [string](Invoke-Psql -Psql $Psql -Command "SELECT 1 FROM pg_roles WHERE rolname = 'simcore'")
    if ($role.Trim() -eq "1") {
        Invoke-Psql -Psql $Psql -Command "ALTER ROLE simcore WITH LOGIN PASSWORD :'simpass';" -Variables @{ simpass = $dbPass } | Out-Null
    } else {
        Invoke-Psql -Psql $Psql -Command "CREATE ROLE simcore LOGIN PASSWORD :'simpass';" -Variables @{ simpass = $dbPass } | Out-Null
    }
    $db = [string](Invoke-Psql -Psql $Psql -Command "SELECT 1 FROM pg_database WHERE datname = 'simcore'")
    if ($db -notmatch "1") {
        Invoke-Psql -Psql $Psql -Command "CREATE DATABASE simcore OWNER simcore;" | Out-Null
    }
    Invoke-Psql -Psql $Psql -Database "simcore" -Command "ALTER SCHEMA public OWNER TO simcore;" | Out-Null
}

function Write-SimcoreCaddyfile {
    param([hashtable]$EnvMap, [string]$InstallRoot)
    # When coexist-apache.ps1 has recorded Apache upstreams, keep proxying every
    # non-API host to Apache. update.ps1 rewrites this file on every deploy.
    $body = New-SimcoreCaddyfileText -EnvMap $EnvMap -InstallRoot $InstallRoot
    $path = Join-Path $InstallRoot "Caddyfile"
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($path, $body, $utf8)
    return $path
}

function Write-WinSwServiceXml {
    param(
        [string]$XmlPath,
        [string]$Id,
        [string]$DisplayName,
        [string]$Description,
        [string]$Executable,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$LogDir,
        [string]$Depend = ""
    )
    $dependXml = ""
    if ($Depend) { $dependXml = "  <depend>$([System.Security.SecurityElement]::Escape($Depend))</depend>`r`n" }
    $xml = @"
<service>
  <id>$Id</id>
  <name>$([System.Security.SecurityElement]::Escape($DisplayName))</name>
  <description>$([System.Security.SecurityElement]::Escape($Description))</description>
  <executable>$([System.Security.SecurityElement]::Escape($Executable))</executable>
  <arguments>$([System.Security.SecurityElement]::Escape($Arguments))</arguments>
  <workingdirectory>$([System.Security.SecurityElement]::Escape($WorkingDirectory))</workingdirectory>
  <env name="PYTHONUNBUFFERED" value="1" />
  <env name="SIMCORE_ENV" value="production" />
$dependXml  <logpath>$([System.Security.SecurityElement]::Escape($LogDir))</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>8</keepFiles>
  </log>
  <onfailure action="restart" delay="5 sec" />
  <onfailure action="restart" delay="15 sec" />
  <onfailure action="restart" delay="30 sec" />
  <resetfailure>1 hour</resetfailure>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <stoptimeout>20 sec</stoptimeout>
</service>
"@
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($XmlPath, $xml.Trim() + "`r`n", $utf8)
}

function Install-SimcoreWindowsServices {
    param(
        [string]$RepoRoot,
        [string]$InstallRoot,
        [hashtable]$EnvMap,
        [switch]$Reinstall
    )
    $winsw = Ensure-WinSW -InstallRoot $InstallRoot
    $caddy = Join-Path $InstallRoot "tools\caddy.exe"
    $python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $port = $EnvMap["API_PORT"]
    if (-not $port) { $port = "8741" }
    $layout = Find-PostgresLayout
    $depend = ""
    if ($layout -and $layout.ServiceName) { $depend = $layout.ServiceName }
    $logDir = Join-Path $InstallRoot "logs"
    $caddyFile = Join-Path $InstallRoot "Caddyfile"
    $servicesDir = Join-Path $InstallRoot "services"
    New-Item -ItemType Directory -Force -Path $servicesDir, $logDir | Out-Null

    $specs = @(
        @{
            Id = "simcore-api"
            Name = "Simcore API"
            Description = "Strategy game simulation API (localhost only; Caddy publishes HTTPS)"
            Executable = $python
            Arguments = "-m uvicorn simcore.main:app --host 127.0.0.1 --port $port"
            Depend = $depend
        },
        @{
            Id = "simcore-worker"
            Name = "Simcore Worker"
            Description = "Claims due world events with SKIP LOCKED"
            Executable = $python
            Arguments = "-m simcore.worker"
            Depend = $depend
        },
        @{
            Id = "simcore-caddy"
            Name = "Simcore Caddy"
            Description = "HTTPS reverse proxy and Let's Encrypt for the game API"
            Executable = $caddy
            Arguments = "run --config " + ($caddyFile -replace '\\', '/')
            Depend = ""
        }
    )

    foreach ($spec in $specs) {
        $exe = Join-Path $servicesDir "$($spec.Id).exe"
        $xml = Join-Path $servicesDir "$($spec.Id).xml"
        $installed = Get-Service -Name $spec.Id -ErrorAction SilentlyContinue
        if ($installed -and $installed.Status -ne "Stopped") {
            Stop-Service -Name $spec.Id -Force
        }
        if ($installed -and $Reinstall -and (Test-Path $exe)) {
            & $exe uninstall | Out-Null
            Start-Sleep -Seconds 1
            $installed = $null
        }
        Write-WinSwServiceXml -XmlPath $xml -Id $spec.Id -DisplayName $spec.Name -Description $spec.Description `
            -Executable $spec.Executable -Arguments $spec.Arguments -WorkingDirectory $RepoRoot `
            -LogDir $logDir -Depend $spec.Depend
        Copy-Item $winsw $exe -Force
        if (-not $installed) {
            & $exe install
            if ($LASTEXITCODE -ne 0) { throw "Could not install Windows service $($spec.Id)" }
            & sc.exe @("failure", $spec.Id, "reset=", "86400", "actions=", "restart/5000/restart/15000/restart/30000") | Out-Null
        }
    }
}

function Stop-SimcoreStack {
    foreach ($id in @("simcore-caddy", "simcore-worker", "simcore-api")) {
        $svc = Get-Service -Name $id -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -ne "Stopped") {
            Write-Host "Stopping $id"
            Stop-Service -Name $id -Force
        }
    }
}

function Set-SimcoreCaddyStartup {
    param(
        [ValidateSet("Automatic", "Manual", "Disabled")]
        [string]$Mode,
        [switch]$Stop
    )
    $svc = Get-Service -Name "simcore-caddy" -ErrorAction SilentlyContinue
    if (-not $svc) { return }
    if ($Stop -and $svc.Status -ne "Stopped") {
        Write-Host "Stopping simcore-caddy"
        Stop-Service -Name "simcore-caddy" -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
    Set-Service -Name "simcore-caddy" -StartupType $Mode
    if ($Stop) {
        $procs = @(Get-Process -Name "caddy" -ErrorAction SilentlyContinue)
        foreach ($proc in $procs) {
            $path = ""
            try { $path = [string]$proc.Path } catch { $path = "" }
            if (Test-SimcoreWebProcess -ProcessName "caddy" -Path $path) {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Start-SimcoreStack {
    param([switch]$SkipCaddy)
    if (-not $SkipCaddy) {
        $caddy = Get-Service -Name "simcore-caddy" -ErrorAction SilentlyContinue
        if ($caddy) { Set-Service -Name "simcore-caddy" -StartupType Automatic }
    }
    $ids = @("simcore-api", "simcore-worker")
    if (-not $SkipCaddy) { $ids += "simcore-caddy" }
    foreach ($id in $ids) {
        $svc = Get-Service -Name $id -ErrorAction SilentlyContinue
        if (-not $svc) { throw "Windows service $id is not installed. Run deploy\windows\bootstrap.ps1 first." }
        Write-Host "Starting $id"
        Start-Service -Name $id
    }
}

function Wait-SimcoreApi {
    param([string]$Port = "8741")
    if (-not $Port) { $Port = "8741" }
    $url = "http://127.0.0.1:$Port/health/ready"
    for ($i = 1; $i -le 30; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                Write-Host "API is ready at $url"
                return
            }
        } catch {
            Write-Host "Waiting for the API ($i/30)"
        }
        Start-Sleep -Seconds 2
    }
    throw "The API did not become ready at $url. Check C:\simcore\logs."
}

function Ensure-Repo {
    param([string]$RepoRoot, [string]$RepoUrl)
    if (Test-Path (Join-Path $RepoRoot ".git")) { return }
    if (Test-Path $RepoRoot) { throw "$RepoRoot exists but is not a git checkout." }
    $parent = Split-Path -Parent $RepoRoot
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $git = Find-Git
    & $git clone $RepoUrl $RepoRoot
    if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
}

function Ensure-Venv {
    param([string]$RepoRoot)
    $python = Find-Python312
    if (-not $python) { throw "Python 3.12 is not installed." }
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        & $python -m venv (Join-Path $RepoRoot ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Could not create the virtualenv" }
    }
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
    & $venvPython -m pip install -e $RepoRoot
    if ($LASTEXITCODE -ne 0) { throw "pip install -e . failed" }
}

function Invoke-SimcoreMigrations {
    param([string]$RepoRoot)
    $python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    Push-Location $RepoRoot
    try {
        & $python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }
        & $python -m simcore.seed
        if ($LASTEXITCODE -ne 0) { throw "seed failed" }
    } finally {
        Pop-Location
    }
}

function Invoke-SimcoreGitPull {
    param([string]$RepoRoot)
    $git = Find-Git
    if (-not $git) { throw "git.exe was not found. Run bootstrap.ps1 once." }
    $safe = @(& $git config --global --get-all safe.directory 2>$null)
    if (@($safe) -notcontains $RepoRoot) {
        & $git config --global --add safe.directory $RepoRoot
    }
    & $git -C $RepoRoot fetch origin main
    if ($LASTEXITCODE -ne 0) { throw "git fetch origin main failed" }
    & $git -C $RepoRoot checkout main
    if ($LASTEXITCODE -ne 0) { throw "git checkout main failed" }
    & $git -C $RepoRoot pull --ff-only origin main
    if ($LASTEXITCODE -ne 0) { throw "git pull --ff-only origin main failed" }
}

function Get-SimcoreDatabaseUrlForPassword {
    param([string]$Url, [string]$Password)
    $escaped = [uri]::EscapeDataString([string]$Password)
    $canonical = "postgresql+psycopg://simcore:${escaped}@127.0.0.1:5432/simcore"
    if ([string]::IsNullOrWhiteSpace($Url)) { return $canonical }
    if ($Url -match '^(?<prefix>postgresql(?:\+[A-Za-z0-9]+)?:\/\/[^:/?#]+:)(?<secret>[^@]*)(?<suffix>@.+)$') {
        if ($Matches["secret"] -eq $escaped) { return $Url }
        return "$($Matches['prefix'])$escaped$($Matches['suffix'])"
    }
    return $canonical
}

function Complete-SimcoreProductionEnv {
    param(
        [hashtable]$Map,
        [string]$InstallRoot,
        [string]$ApiDomain,
        [string]$ApiPort,
        [string]$AcmeEmail
    )
    if ($null -eq $Map) { $Map = @{} }
    $changed = $false
    if ([string]::IsNullOrWhiteSpace([string]$Map["POSTGRES_SUPER_PASSWORD"])) {
        $Map["POSTGRES_SUPER_PASSWORD"] = New-SimcoreSecret
        $changed = $true
    }
    if ([string]::IsNullOrWhiteSpace([string]$Map["SIMCORE_DB_PASSWORD"])) {
        $Map["SIMCORE_DB_PASSWORD"] = New-SimcoreSecret
        $changed = $true
    }
    if ([string]::IsNullOrWhiteSpace([string]$Map["SIMCORE_ADMIN_TOKEN"])) {
        $Map["SIMCORE_ADMIN_TOKEN"] = New-SimcoreSecret
        $changed = $true
    }
    $databaseUrl = Get-SimcoreDatabaseUrlForPassword -Url ([string]$Map["SIMCORE_DATABASE_URL"]) -Password ([string]$Map["SIMCORE_DB_PASSWORD"])
    if ([string]$Map["SIMCORE_DATABASE_URL"] -ne $databaseUrl) {
        $Map["SIMCORE_DATABASE_URL"] = $databaseUrl
        $changed = $true
    }
    $defaults = @{
        SIMCORE_ENV = "production"
        SIMCORE_ENABLE_ADMIN = "false"
        SIMCORE_EMBEDDED_WORKER = "false"
        SIMCORE_CORS_ORIGINS = "https://nustanakritwithai.github.io,http://127.0.0.1:8080,http://localhost:8080"
        SIMCORE_WORKER_POLL_SECONDS = "1.0"
    }
    foreach ($key in @($defaults.Keys)) {
        if ([string]::IsNullOrWhiteSpace([string]$Map[$key])) {
            $Map[$key] = [string]$defaults[$key]
            $changed = $true
        }
    }
    if ([string]::IsNullOrWhiteSpace([string]$Map["API_DOMAIN"]) -and -not [string]::IsNullOrWhiteSpace($ApiDomain)) {
        $Map["API_DOMAIN"] = $ApiDomain
        $changed = $true
    }
    if ([string]::IsNullOrWhiteSpace([string]$Map["API_PORT"]) -and -not [string]::IsNullOrWhiteSpace($ApiPort)) {
        $Map["API_PORT"] = $ApiPort
        $changed = $true
    }
    if ([string]::IsNullOrWhiteSpace([string]$Map["SIMCORE_INSTALL_ROOT"]) -and -not [string]::IsNullOrWhiteSpace($InstallRoot)) {
        $Map["SIMCORE_INSTALL_ROOT"] = $InstallRoot
        $changed = $true
    }
    if (-not $Map.ContainsKey("ACME_EMAIL")) {
        $Map["ACME_EMAIL"] = [string]$AcmeEmail
        $changed = $true
    }
    return [pscustomobject]@{ Map = $Map; Changed = [bool]$changed }
}

function New-SimcoreProductionEnv {
    param(
        [string]$Path,
        [string]$InstallRoot,
        [string]$ApiDomain,
        [string]$ApiPort,
        [string]$AcmeEmail
    )
    $existed = Test-Path -LiteralPath $Path
    $map = @{}
    if ($existed) { $map = Read-SimcoreEnv $Path }
    $completed = Complete-SimcoreProductionEnv -Map $map -InstallRoot $InstallRoot -ApiDomain $ApiDomain -ApiPort $ApiPort -AcmeEmail $AcmeEmail
    if ((-not $existed) -or $completed.Changed) {
        Write-SimcoreEnv -Path $Path -Map $completed.Map
        if ($existed) {
            Write-Host "Filled blank values in $Path. Passwords and the admin token that already had a value were left unchanged."
        } else {
            Write-Host "Wrote $Path"
            Write-Host "The database password and admin token were generated into that file. They are not printed here."
        }
    }
    return $completed.Map
}

function Format-SimcoreGigabytes {
    param([long]$Bytes)
    $gb = $Bytes / 1GB
    return $gb.ToString("0.00", [System.Globalization.CultureInfo]::InvariantCulture)
}

function Format-SimcoreLowDiskMessage {
    param([string]$Root, [long]$AvailableBytes, [long]$MinimumBytes)
    $freeGb = Format-SimcoreGigabytes $AvailableBytes
    $requiredGb = Format-SimcoreGigabytes $MinimumBytes
    return "Not enough free space on $Root. FreeGB=$freeGb RequiredGB=$requiredGb. Free space, then run bootstrap again."
}

function Test-SimcoreDiskBudget {
    param([long]$AvailableBytes, [long]$MinimumBytes)
    return $AvailableBytes -ge $MinimumBytes
}

function Get-SimcoreDriveFreeSpace {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { throw "No path was given for the disk space check." }
    $root = [System.IO.Path]::GetPathRoot($Path)
    if ([string]::IsNullOrWhiteSpace($root)) { throw "Could not find the drive for $Path." }
    $info = New-Object System.IO.DriveInfo ($root)
    if (-not $info.IsReady) { throw "Drive $root is not ready. Bootstrap cannot check free space." }
    return [pscustomobject]@{ Root = [string]$info.Name; AvailableBytes = [long]$info.AvailableFreeSpace }
}

function Assert-SimcoreFreeDisk {
    param(
        [string]$Path,
        [long]$MinimumBytes = 1288490189
    )
    $drive = Get-SimcoreDriveFreeSpace $Path
    if (-not (Test-SimcoreDiskBudget -AvailableBytes $drive.AvailableBytes -MinimumBytes $MinimumBytes)) {
        throw (Format-SimcoreLowDiskMessage -Root $drive.Root -AvailableBytes $drive.AvailableBytes -MinimumBytes $MinimumBytes)
    }
    Write-Host ("{0} has {1:N1} GB free." -f $drive.Root, ($drive.AvailableBytes / 1GB))
}

function Assert-SimcoreBootstrapDisk {
    param([string]$InstallRoot, [string]$RepoRoot)
    if ([string]::IsNullOrWhiteSpace($InstallRoot)) { throw "Install root is empty." }
    Assert-SimcoreFreeDisk -Path $InstallRoot
    $installRootDrive = [System.IO.Path]::GetPathRoot($InstallRoot)
    $seen = @{}
    if (-not [string]::IsNullOrWhiteSpace($installRootDrive)) {
        $seen[$installRootDrive.TrimEnd('\').TrimEnd('/').ToLowerInvariant()] = $true
    }
    foreach ($path in @($RepoRoot, [string]$env:TEMP)) {
        if ([string]::IsNullOrWhiteSpace($path)) { continue }
        $root = [System.IO.Path]::GetPathRoot($path)
        if ([string]::IsNullOrWhiteSpace($root)) { continue }
        $key = $root.TrimEnd('\').TrimEnd('/').ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        Assert-SimcoreFreeDisk -Path $path
    }
}

. "$PSScriptRoot\CoexistLib.ps1"
