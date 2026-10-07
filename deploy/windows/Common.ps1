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

function Get-SimcoreHeaderContentLength {
    param($Response)
    # Windows PowerShell often returns $null from Invoke-WebRequest -OutFile -PassThru.
    if ($null -eq $Response) { return [long]0 }
    $headers = $Response.Headers
    if ($null -eq $headers) { return [long]0 }
    $value = [string]$headers["Content-Length"]
    $parsed = [long]0
    if ($value -and [long]::TryParse($value, [ref]$parsed) -and $parsed -gt 0) { return $parsed }
    return [long]0
}

function Get-SimcoreDeclaredDownloadBytes {
    param($Response, [long]$ExpectedBytes)
    $fromResponse = Get-SimcoreHeaderContentLength -Response $Response
    if ($fromResponse -gt 0) { return $fromResponse }
    if ($ExpectedBytes -gt 0) { return [long]$ExpectedBytes }
    return [long]0
}

function Format-SimcoreDownloadLengthError {
    param([string]$Url, [long]$ActualBytes, [long]$DeclaredBytes, [long]$MinimumBytes)
    $wanted = $(if ($DeclaredBytes -gt 0) { "$DeclaredBytes" } else { "at least $MinimumBytes" })
    return "Download of $Url is $ActualBytes bytes (expected $wanted). The incomplete file was discarded."
}

function Get-SimcoreContentLength {
    param([string]$Url)
    try {
        $head = Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing
        $parsed = Get-SimcoreHeaderContentLength -Response $head
        if ($parsed -gt 0) { return $parsed }
    } catch {
        Write-Host "Could not read Content-Length for $Url. The download will be checked against the minimum size."
    }
    return [long]0
}

function Find-CurlExe {
    $cmd = Get-Command -Name "curl.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd -and $cmd.Source) { return [string]$cmd.Source }
    return $null
}

function Invoke-SimcoreFileDownload {
    param([string]$Url, [string]$Destination)
    # curl.exe is more reliable than Invoke-WebRequest -OutFile -PassThru on Windows Server.
    $curl = Find-CurlExe
    if ($curl) {
        & $curl -L --fail --retry 3 -o $Destination $Url 1>$null
        if ($LASTEXITCODE -ne 0) {
            throw "curl.exe failed to download $Url (exit $LASTEXITCODE)."
        }
        return $null
    }
    return Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -PassThru
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
        $response = Invoke-SimcoreFileDownload -Url $Url -Destination $partial
        $item = Get-Item -LiteralPath $partial -ErrorAction SilentlyContinue
        if ($null -eq $item) { throw "Download of $Url did not create a file." }
        $actual = [long]$item.Length
        $declared = Get-SimcoreDeclaredDownloadBytes -Response $response -ExpectedBytes $expected
        if (-not (Test-SimcoreDownloadComplete -ActualBytes $actual -ExpectedBytes $declared -MinimumBytes $MinimumBytes)) {
            throw (Format-SimcoreDownloadLengthError -Url $Url -ActualBytes $actual -DeclaredBytes $declared -MinimumBytes $MinimumBytes)
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
    if ($null -eq $Map) { throw "Refusing to write $Path because the env map is null." }
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
    # Write the whole file beside the destination, then move it into place.
    # A full disk throws while the temporary file is incomplete and the previous .env.prod stays.
    # Move-Item -Force replaces the destination without [NullString]::Value, which Windows PowerShell can reject.
    $temp = Join-Path $directory (".{0}.{1}.tmp" -f (Split-Path -Leaf $Path), ([guid]::NewGuid().ToString("N")))
    try {
        [System.IO.File]::WriteAllLines($temp, $lines.ToArray(), $utf8)
        $written = [System.IO.File]::ReadAllText($temp, $utf8)
        if ([string]::IsNullOrWhiteSpace($written)) {
            throw "Refusing to replace $Path with an empty file."
        }
        Move-Item -LiteralPath $temp -Destination $Path -Force
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

function Get-SimcoreProcessExitCode {
    param($Process)
    if ($null -eq $Process) { return $null }
    return $Process.ExitCode
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
    $exitCode = Get-SimcoreProcessExitCode $proc
    if (-not (Test-SimcoreInstallerExit $exitCode)) {
        $codeText = "(no exit code)"
        if ($null -ne $exitCode) { $codeText = [string]$exitCode }
        throw "Python installer failed with exit code $codeText."
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
    $exitCode = Get-SimcoreProcessExitCode $proc
    if (-not (Test-SimcoreInstallerExit $exitCode)) {
        $codeText = "(no exit code)"
        if ($null -ne $exitCode) { $codeText = [string]$exitCode }
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

function Test-SimcoreFirewallMatchesPort {
    param($Filter, $Port)
    if ($null -eq $Filter) { return $false }
    $localPort = $Filter.LocalPort
    if ($null -eq $localPort) { return $false }
    $ports = @($localPort)
    return ($ports -contains $Port -or $ports -contains "$Port")
}

function Disable-PublicPostgres {
    $named = @(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {
        $_.DisplayName -match 'PostgreSQL|postgres'
    })
    foreach ($rule in $named) {
        if ($null -eq $rule -or [string]::IsNullOrWhiteSpace([string]$rule.Name)) { continue }
        Disable-NetFirewallRule -Name $rule.Name
        Write-Host "Disabled firewall rule: $($rule.DisplayName)"
    }
    $inbound = @(Get-NetFirewallRule -Direction Inbound -Action Allow -ErrorAction SilentlyContinue)
    foreach ($rule in $inbound) {
        if ($null -eq $rule -or [string]::IsNullOrWhiteSpace([string]$rule.Name)) { continue }
        $filter = $rule | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
        if (-not (Test-SimcoreFirewallMatchesPort -Filter $filter -Port 5432)) { continue }
        Disable-NetFirewallRule -Name $rule.Name
        Write-Host "Disabled inbound 5432 rule: $($rule.DisplayName)"
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

function Convert-SimcoreCommandText {
    param($Value)
    # An empty native command emits no object. Casting that to [string] stays $null, and .Trim() then throws.
    return ("" + $Value).Trim()
}

function Convert-SimcoreSqlLiteral {
    param([string]$Value)
    if ($null -eq $Value) { $Value = "" }
    return "'" + $Value.Replace("'", "''") + "'"
}

function Get-SimcoreAlterRolePasswordSql {
    param([string]$Role, [string]$Password)
    if ($Role -ne "postgres" -and $Role -ne "simcore") {
        throw "Refusing to change the password for role $Role."
    }
    $literal = Convert-SimcoreSqlLiteral $Password
    return "ALTER ROLE $Role WITH LOGIN PASSWORD $literal;"
}

function Get-SimcoreEnsureRoleSql {
    param([string]$Password)
    $literal = Convert-SimcoreSqlLiteral $Password
    $marker = '$simcore$'
    if ($literal.Contains($marker)) {
        $marker = '$sim' + ([guid]::NewGuid().ToString("N")) + '$'
    }
    return @(
        "DO $marker",
        "BEGIN",
        "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'simcore') THEN",
        "    EXECUTE format('ALTER ROLE simcore WITH LOGIN PASSWORD %L', $literal);",
        "  ELSE",
        "    EXECUTE format('CREATE ROLE simcore LOGIN PASSWORD %L', $literal);",
        "  END IF;",
        "END",
        "$marker;"
    ) -join "`n"
}

function Get-SimcoreCreateDatabaseSql {
    return "CREATE DATABASE simcore OWNER simcore;"
}

function Get-SimcorePublicSchemaOwnerSql {
    return "ALTER SCHEMA public OWNER TO simcore;"
}

function Test-SimcoreCatalogRowPresent {
    param($Output)
    return (Convert-SimcoreCommandText $Output) -eq "1"
}

function Format-SimcorePsqlFailure {
    param($ExitCode, [string]$Database, [string]$Output)
    $codeText = "(no exit code)"
    if ($null -ne $ExitCode -and "$ExitCode" -ne "") { $codeText = [string]$ExitCode }
    $body = Convert-SimcoreCommandText $Output
    if ([string]::IsNullOrWhiteSpace($body)) { $body = "(no output)" }
    return "psql failed (exit $codeText) on database ${Database}: $body"
}

function Read-SimcoreTextFile {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) { return "" }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -eq 0) { return "" }
    return [System.Text.Encoding]::UTF8.GetString($bytes)
}

function Invoke-Psql {
    param(
        [string]$Psql,
        [string]$Database = "postgres",
        [string]$Command
    )
    if ([string]::IsNullOrWhiteSpace($Psql)) { throw "psql.exe path is empty." }
    if ([string]::IsNullOrWhiteSpace($Command)) { throw "psql command is empty." }
    if ([string]::IsNullOrWhiteSpace($Database)) { $Database = "postgres" }
    # Passwords are already SQL literals inside $Command. Do not use psql -v or :'var'.
    $psqlArgs = @(
        "-w", "-X",
        "-U", "postgres",
        "-h", "127.0.0.1",
        "-d", $Database,
        "--set=ON_ERROR_STOP=1",
        "-tA",
        "-c", $Command
    )
    $stamp = [guid]::NewGuid().ToString("N")
    $outFile = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-psql-out-" + $stamp + ".txt")
    $errFile = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-psql-err-" + $stamp + ".txt")
    $hadEncoding = Test-Path Env:PGCLIENTENCODING
    $previousEncoding = $env:PGCLIENTENCODING
    $previousPreference = $ErrorActionPreference
    $env:PGCLIENTENCODING = "UTF8"
    $ErrorActionPreference = "Continue"
    try {
        & $Psql @psqlArgs 1> $outFile 2> $errFile
        $exitCode = $LASTEXITCODE
        $stdout = Read-SimcoreTextFile $outFile
        $stderr = Read-SimcoreTextFile $errFile
        if ($exitCode -ne 0) {
            $combined = (@($stdout, $stderr) -join "`n")
            throw (Format-SimcorePsqlFailure -ExitCode $exitCode -Database $Database -Output $combined)
        }
        return (Convert-SimcoreCommandText $stdout)
    } finally {
        $ErrorActionPreference = $previousPreference
        if ($hadEncoding) { $env:PGCLIENTENCODING = $previousEncoding } else { Remove-Item Env:PGCLIENTENCODING -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $outFile) { Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $errFile) { Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue }
    }
}

function Test-PsqlSuperuserLogin {
    param([string]$Psql)
    if ([string]::IsNullOrWhiteSpace($Psql)) { return $false }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Psql -w -X -U postgres -h 127.0.0.1 -d postgres -tA -c "SELECT 1" 1>$null 2>$null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Test-PostgresLogin {
    param([string]$Psql, [string]$Password)
    $had = Test-Path Env:PGPASSWORD
    $previous = $env:PGPASSWORD
    $env:PGPASSWORD = $Password
    $ok = Test-PsqlSuperuserLogin -Psql $Psql
    if ($had) { $env:PGPASSWORD = $previous } else { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
    return $ok
}

function Initialize-SimcoreDatabase {
    param([string]$Psql, [hashtable]$EnvMap)
    if ($null -eq $EnvMap) { throw "Production env map is empty. Cannot initialize the database." }
    $super = [string]$EnvMap["POSTGRES_SUPER_PASSWORD"]
    $dbPass = [string]$EnvMap["SIMCORE_DB_PASSWORD"]
    Assert-SimcoreSecretPresent -Value $super -Name "POSTGRES_SUPER_PASSWORD"
    Assert-SimcoreSecretPresent -Value $dbPass -Name "SIMCORE_DB_PASSWORD"
    if ([string]::IsNullOrWhiteSpace($Psql)) { throw "psql.exe path is empty." }

    $hadPassword = Test-Path Env:PGPASSWORD
    $previousPassword = $env:PGPASSWORD
    try {
        $env:PGPASSWORD = $super
        if (-not (Test-PsqlSuperuserLogin -Psql $Psql)) {
            $env:PGPASSWORD = "postgres"
            if (-not (Test-PsqlSuperuserLogin -Psql $Psql)) {
                throw "Could not sign in as postgres. Set POSTGRES_SUPER_PASSWORD in .env.prod to the current password and run bootstrap again."
            }
            Invoke-Psql -Psql $Psql -Command (Get-SimcoreAlterRolePasswordSql -Role "postgres" -Password $super) | Out-Null
            $env:PGPASSWORD = $super
            if (-not (Test-PsqlSuperuserLogin -Psql $Psql)) {
                throw "The postgres superuser password was changed but the new password was rejected."
            }
            Write-Host "Rotated the postgres superuser password away from the installer default."
        }
        Write-Host "Ensuring the simcore login role"
        Invoke-Psql -Psql $Psql -Command (Get-SimcoreEnsureRoleSql -Password $dbPass) | Out-Null
        $database = Invoke-Psql -Psql $Psql -Command "SELECT 1 FROM pg_database WHERE datname = 'simcore'"
        if (-not (Test-SimcoreCatalogRowPresent $database)) {
            Write-Host "Creating the simcore database"
            Invoke-Psql -Psql $Psql -Command (Get-SimcoreCreateDatabaseSql) | Out-Null
        } else {
            Write-Host "The simcore database already exists"
        }
        Write-Host "Giving simcore ownership of the public schema"
        Invoke-Psql -Psql $Psql -Database "simcore" -Command (Get-SimcorePublicSchemaOwnerSql) | Out-Null
    } finally {
        if ($hadPassword) { $env:PGPASSWORD = $previousPassword } else { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
    }
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
    $text = ("" + $xml).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { throw "Refusing to write an empty WinSW service file for $Id." }
    [System.IO.File]::WriteAllText($XmlPath, $text + "`r`n", $utf8)
}

function Write-SimcoreFailure {
    param([string]$Context, $ErrorRecord)
    Write-Host "$Context failed."
    if ($null -eq $ErrorRecord) { return }
    $exception = $ErrorRecord.Exception
    if ($null -ne $exception) {
        Write-Host ("Exception: " + $exception.GetType().FullName)
        Write-Host $exception.Message
    }
    $info = $ErrorRecord.InvocationInfo
    if ($null -ne $info -and $info.PositionMessage) {
        Write-Host $info.PositionMessage
    }
    if ($ErrorRecord.ScriptStackTrace) {
        Write-Host $ErrorRecord.ScriptStackTrace
    }
}

function Assert-SimcoreServiceExecutable {
    param($Path, [string]$Name)
    $text = [string]$Path
    if ([string]::IsNullOrWhiteSpace($text) -or -not (Test-Path -LiteralPath $text)) {
        throw "$Name was not found at $text."
    }
}

function Assert-SimcoreServiceSpec {
    param($Spec)
    if ($null -eq $Spec) { throw "A Windows service spec was empty." }
    foreach ($field in @("Id", "Name", "Description", "Executable", "Arguments")) {
        if ([string]::IsNullOrWhiteSpace([string]$Spec.$field)) {
            throw "Windows service spec is missing $field."
        }
    }
}

function Install-SimcoreWindowsServices {
    param(
        [string]$RepoRoot,
        [string]$InstallRoot,
        [hashtable]$EnvMap,
        [switch]$Reinstall
    )
    try {
        if ([string]::IsNullOrWhiteSpace($RepoRoot)) { throw "Repo root is empty. Cannot install Windows services." }
        if ([string]::IsNullOrWhiteSpace($InstallRoot)) { throw "Install root is empty. Cannot install Windows services." }
        if ($null -eq $EnvMap) { throw "Production env map is empty. Cannot install Windows services." }
        $winsw = Ensure-WinSW -InstallRoot $InstallRoot
        Assert-SimcoreServiceExecutable -Path $winsw -Name "WinSW.NET4.exe"
        $caddy = Join-Path $InstallRoot "tools\caddy.exe"
        Assert-SimcoreServiceExecutable -Path $caddy -Name "caddy.exe"
        $python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
        Assert-SimcoreServiceExecutable -Path $python -Name "Python virtualenv"
        $port = [string]$EnvMap["API_PORT"]
        if ([string]::IsNullOrWhiteSpace($port)) { $port = "8741" }
        $layout = Find-PostgresLayout
        $depend = ""
        if ($null -ne $layout -and -not [string]::IsNullOrWhiteSpace([string]$layout.ServiceName)) {
            $depend = [string]$layout.ServiceName
        }
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
            Assert-SimcoreServiceSpec $spec
            $id = [string]$spec.Id
            Write-Host "Installing Windows service $id"
            $exe = Join-Path $servicesDir "$id.exe"
            $xml = Join-Path $servicesDir "$id.xml"
            $installed = Get-Service -Name $id -ErrorAction SilentlyContinue
            if ($null -ne $installed) {
                $status = [string]$installed.Status
                if ($status -ne "Stopped") {
                    Stop-Service -Name $id -Force
                }
            }
            if ($null -ne $installed -and $Reinstall -and (Test-Path -LiteralPath $exe)) {
                & $exe uninstall | Out-Null
                Start-Sleep -Seconds 1
                $installed = $null
            }
            $dependOn = ""
            if (-not [string]::IsNullOrWhiteSpace([string]$spec.Depend)) { $dependOn = [string]$spec.Depend }
            Write-WinSwServiceXml -XmlPath $xml -Id $id -DisplayName ([string]$spec.Name) -Description ([string]$spec.Description) `
                -Executable ([string]$spec.Executable) -Arguments ([string]$spec.Arguments) -WorkingDirectory $RepoRoot `
                -LogDir $logDir -Depend $dependOn
            Copy-Item -LiteralPath ([string]$winsw) -Destination $exe -Force
            if ($null -eq $installed) {
                & $exe install
                if ($LASTEXITCODE -ne 0) { throw "Could not install Windows service $id" }
                & sc.exe @("failure", $id, "reset=", "86400", "actions=", "restart/5000/restart/15000/restart/30000") | Out-Null
            }
        }
    } catch {
        Write-SimcoreFailure -Context "Install-SimcoreWindowsServices" -ErrorRecord $_
        throw
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
            if ($null -ne $response -and $response.StatusCode -eq 200) {
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

function Clear-SimcoreInstallerCache {
    param([string]$TempRoot)
    if ([string]::IsNullOrWhiteSpace($TempRoot)) { return }
    if (-not (Test-Path -LiteralPath $TempRoot)) { return }
    $children = @(Get-ChildItem -LiteralPath $TempRoot -Force -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        if ($null -eq $child) { continue }
        $name = [string]$child.Name
        $matchesCache = (
            $name -like "postgresql*.exe*" -or
            $name -like "python*.exe*" -or
            $name -like "caddy*" -or
            $name -like "WinSW*" -or
            $name -like "*.partial"
        )
        if (-not $matchesCache) { continue }
        try {
            Remove-Item -LiteralPath $child.FullName -Force -Recurse -ErrorAction Stop
            Write-Host "Removed leftover installer cache $($child.FullName)"
        } catch {
            Write-Host "Could not remove leftover installer cache $($child.FullName): $($_.Exception.Message)"
        }
    }
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
