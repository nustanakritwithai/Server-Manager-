# Unit tests for bootstrap secret repair, installer argument checks, and disk budget.
#   pwsh -NoProfile -File deploy/windows/bootstrap.tests.ps1

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\Common.ps1"

$script:Failed = 0

function Assert-True {
    param([string]$Name, $Condition)
    if (-not $Condition) {
        Write-Host "FAIL $Name"
        $script:Failed++
    }
}

function Assert-Equal {
    param([string]$Name, $Actual, $Expected)
    if ($Actual -ne $Expected) {
        Write-Host "FAIL $Name"
        Write-Host "EXPECTED:"
        Write-Host $Expected
        Write-Host "ACTUAL:"
        Write-Host $Actual
        $script:Failed++
    }
}

function Assert-Throws {
    param([string]$Name, [scriptblock]$Action, [string]$Pattern)
    $threw = $false
    $message = ""
    try {
        & $Action
    } catch {
        $threw = $true
        $message = [string]$_
    }
    if (-not $threw) {
        Write-Host "FAIL $Name (no exception)"
        $script:Failed++
        return
    }
    if ($Pattern -and $message -notmatch $Pattern) {
        Write-Host "FAIL $Name"
        Write-Host "MESSAGE: $message"
        $script:Failed++
    }
}

$empty = Complete-SimcoreProductionEnv -Map @{} -InstallRoot "C:\simcore" -ApiDomain "157-85-96-139.sslip.io" -ApiPort "8741" -AcmeEmail ""
Assert-True "empty map changed" $empty.Changed
Assert-True "super generated" (-not [string]::IsNullOrWhiteSpace([string]$empty.Map["POSTGRES_SUPER_PASSWORD"]))
Assert-True "db generated" (-not [string]::IsNullOrWhiteSpace([string]$empty.Map["SIMCORE_DB_PASSWORD"]))
Assert-True "admin generated" (-not [string]::IsNullOrWhiteSpace([string]$empty.Map["SIMCORE_ADMIN_TOKEN"]))
Assert-True "secrets differ" (
    $empty.Map["POSTGRES_SUPER_PASSWORD"] -ne $empty.Map["SIMCORE_DB_PASSWORD"] -and
    $empty.Map["SIMCORE_DB_PASSWORD"] -ne $empty.Map["SIMCORE_ADMIN_TOKEN"]
)
$escaped = [uri]::EscapeDataString([string]$empty.Map["SIMCORE_DB_PASSWORD"])
Assert-Equal "canonical url" $empty.Map["SIMCORE_DATABASE_URL"] "postgresql+psycopg://simcore:${escaped}@127.0.0.1:5432/simcore"
Assert-Equal "default env" $empty.Map["SIMCORE_ENV"] "production"
Assert-Equal "default domain" $empty.Map["API_DOMAIN"] "157-85-96-139.sslip.io"

$again = Complete-SimcoreProductionEnv -Map $empty.Map -InstallRoot "C:\simcore" -ApiDomain "157-85-96-139.sslip.io" -ApiPort "8741" -AcmeEmail ""
Assert-True "second pass keeps values" (-not $again.Changed)
Assert-Equal "super stable" $again.Map["POSTGRES_SUPER_PASSWORD"] $empty.Map["POSTGRES_SUPER_PASSWORD"]
Assert-Equal "admin stable" $again.Map["SIMCORE_ADMIN_TOKEN"] $empty.Map["SIMCORE_ADMIN_TOKEN"]

$partial = @{
    SIMCORE_ADMIN_TOKEN = "keep-admin"
    SIMCORE_DB_PASSWORD = "keep-db"
    POSTGRES_SUPER_PASSWORD = ""
    SIMCORE_DATABASE_URL = "postgresql+psycopg://simcore:other@10.0.0.5:5432/simcore"
    SIMCORE_ENABLE_ADMIN = "true"
}
$filled = Complete-SimcoreProductionEnv -Map $partial -InstallRoot "C:\simcore" -ApiDomain "game.example" -ApiPort "8741" -AcmeEmail "ops@example.com"
Assert-Equal "admin kept" $filled.Map["SIMCORE_ADMIN_TOKEN"] "keep-admin"
Assert-Equal "db kept" $filled.Map["SIMCORE_DB_PASSWORD"] "keep-db"
Assert-True "blank super replaced" (-not [string]::IsNullOrWhiteSpace([string]$filled.Map["POSTGRES_SUPER_PASSWORD"]))
Assert-Equal "url password updated" $filled.Map["SIMCORE_DATABASE_URL"] "postgresql+psycopg://simcore:keep-db@10.0.0.5:5432/simcore"
Assert-Equal "admin flag kept" $filled.Map["SIMCORE_ENABLE_ADMIN"] "true"

$keptUrl = Get-SimcoreDatabaseUrlForPassword -Url "postgresql+psycopg://simcore:keep-db@127.0.0.1:5432/simcore" -Password "keep-db"
Assert-Equal "matching url unchanged" $keptUrl "postgresql+psycopg://simcore:keep-db@127.0.0.1:5432/simcore"

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-env-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $root | Out-Null
$envPath = Join-Path $root ".env.prod"
[System.IO.File]::WriteAllText($envPath, "")
$firstWrite = New-SimcoreProductionEnv -Path $envPath -InstallRoot "C:\simcore" -ApiDomain "157-85-96-139.sslip.io" -ApiPort "8741" -AcmeEmail ""
$secondWrite = New-SimcoreProductionEnv -Path $envPath -InstallRoot "C:\simcore" -ApiDomain "157-85-96-139.sslip.io" -ApiPort "8741" -AcmeEmail ""
Assert-Equal "file repair does not rotate" $secondWrite["POSTGRES_SUPER_PASSWORD"] $firstWrite["POSTGRES_SUPER_PASSWORD"]
Assert-Equal "file repair keeps admin" $secondWrite["SIMCORE_ADMIN_TOKEN"] $firstWrite["SIMCORE_ADMIN_TOKEN"]
$roundTrip = Read-SimcoreEnv $envPath
Assert-Equal "round trip super" $roundTrip["POSTGRES_SUPER_PASSWORD"] $firstWrite["POSTGRES_SUPER_PASSWORD"]
$temps = @(Get-ChildItem -LiteralPath $root -Force -Filter "*.tmp")
Assert-Equal "no temp env left" $temps.Count 0

$presetPath = Join-Path $root "preset.env"
[System.IO.File]::WriteAllText($presetPath, "POSTGRES_SUPER_PASSWORD=keep-super`r`nSIMCORE_DB_PASSWORD=keep-db`r`nSIMCORE_ADMIN_TOKEN=keep-admin`r`nSIMCORE_DATABASE_URL=postgresql+psycopg://simcore:keep-db@127.0.0.1:5432/simcore`r`n")
$preset = New-SimcoreProductionEnv -Path $presetPath -InstallRoot "C:\simcore" -ApiDomain "157-85-96-139.sslip.io" -ApiPort "8741" -AcmeEmail ""
Assert-Equal "preset super" $preset["POSTGRES_SUPER_PASSWORD"] "keep-super"
Assert-Equal "preset db" $preset["SIMCORE_DB_PASSWORD"] "keep-db"
Assert-Equal "preset admin" $preset["SIMCORE_ADMIN_TOKEN"] "keep-admin"
Assert-Equal "preset url" $preset["SIMCORE_DATABASE_URL"] "postgresql+psycopg://simcore:keep-db@127.0.0.1:5432/simcore"
Remove-Item -LiteralPath $root -Recurse -Force
Assert-Throws "null env map" {
    Write-SimcoreEnv -Path (Join-Path ([System.IO.Path]::GetTempPath()) "simcore-null.env") -Map $null
} "env map is null"

function Get-NoPsqlRows { }
$castRole = [string](Get-NoPsqlRows)
Assert-True "empty psql cast stays null" ($null -eq $castRole)
$emptyRole = Convert-SimcoreCommandText $castRole
Assert-Equal "empty psql role" $emptyRole ""
Assert-True "empty psql role is absent" ($emptyRole -ne "1")
$emptyDb = Convert-SimcoreCommandText (Get-NoPsqlRows)
Assert-True "empty psql database is absent" ([string]::IsNullOrWhiteSpace($emptyDb) -or $emptyDb -notmatch "1")
Assert-Equal "psql role row" (Convert-SimcoreCommandText " 1 `r") "1"
Assert-True "psql database row" ((Convert-SimcoreCommandText "1") -match "1")
Assert-True "null firewall filter is not port 5432" (-not (Test-SimcoreFirewallMatchesPort -Filter $null -Port 5432))
Assert-True "null firewall port is not 5432" (-not (Test-SimcoreFirewallMatchesPort -Filter ([pscustomobject]@{ LocalPort = $null }) -Port 5432))
Assert-True "firewall port 5432 matches" (Test-SimcoreFirewallMatchesPort -Filter ([pscustomobject]@{ LocalPort = 5432 }) -Port 5432)
Assert-SimcoreServiceSpec @{
    Id = "simcore-api"
    Name = "Simcore API"
    Description = "api"
    Executable = "python.exe"
    Arguments = "-m simcore"
}
Assert-Throws "null service spec" { Assert-SimcoreServiceSpec $null } "empty"
Assert-Throws "service spec missing id" {
    Assert-SimcoreServiceSpec @{ Name = "Simcore API"; Description = "api"; Executable = "python.exe"; Arguments = "-m simcore" }
} "Id"
Assert-Throws "missing service executable" {
    Assert-SimcoreServiceExecutable -Path (Join-Path ([System.IO.Path]::GetTempPath()) ("missing-python-" + [guid]::NewGuid().ToString("N") + ".exe")) -Name "Python virtualenv"
} "Python virtualenv"

$cache = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-cache-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $cache | Out-Null
[System.IO.File]::WriteAllText((Join-Path $cache "postgresql-16.exe.partial"), "x")
[System.IO.File]::WriteAllText((Join-Path $cache "python-3.12.10-amd64.exe"), "x")
[System.IO.File]::WriteAllText((Join-Path $cache "caddy_2.11.7.zip"), "x")
[System.IO.File]::WriteAllText((Join-Path $cache "WinSW.NET4.exe"), "x")
[System.IO.File]::WriteAllText((Join-Path $cache "keep.txt"), "keep")
Clear-SimcoreInstallerCache -TempRoot $cache
Assert-True "installer cache removed" (-not (Test-Path -LiteralPath (Join-Path $cache "postgresql-16.exe.partial")))
Assert-True "python installer removed" (-not (Test-Path -LiteralPath (Join-Path $cache "python-3.12.10-amd64.exe")))
Assert-True "caddy cache removed" (-not (Test-Path -LiteralPath (Join-Path $cache "caddy_2.11.7.zip")))
Assert-True "winsw cache removed" (-not (Test-Path -LiteralPath (Join-Path $cache "WinSW.NET4.exe")))
Assert-True "unrelated temp file kept" (Test-Path -LiteralPath (Join-Path $cache "keep.txt"))
Remove-Item -LiteralPath $cache -Recurse -Force

Assert-True "download exact" (Test-SimcoreDownloadComplete -ActualBytes 404741880 -ExpectedBytes 404741880 -MinimumBytes 314572800)
Assert-True "download short of declared" (-not (Test-SimcoreDownloadComplete -ActualBytes 1000 -ExpectedBytes 404741880 -MinimumBytes 1))
Assert-True "download under minimum" (-not (Test-SimcoreDownloadComplete -ActualBytes 1000 -ExpectedBytes 0 -MinimumBytes 314572800))
Assert-True "download meets minimum" (Test-SimcoreDownloadComplete -ActualBytes 314572800 -ExpectedBytes 0 -MinimumBytes 314572800)
Assert-Equal "null PassThru response has no length" (Get-SimcoreHeaderContentLength -Response $null) ([long]0)
$nullHeaders = [pscustomobject]@{ Headers = $null }
Assert-Equal "null headers have no length" (Get-SimcoreHeaderContentLength -Response $nullHeaders) ([long]0)
$withLength = [pscustomobject]@{ Headers = @{ "Content-Length" = "404741880" } }
Assert-Equal "header content length" (Get-SimcoreHeaderContentLength -Response $withLength) ([long]404741880)
$fromNull = Get-SimcoreDeclaredDownloadBytes -Response $null -ExpectedBytes 404741880
Assert-Equal "null response uses expected length" $fromNull ([long]404741880)
Assert-True "file matching expected passes" (Test-SimcoreDownloadComplete -ActualBytes 404741880 -ExpectedBytes $fromNull -MinimumBytes 314572800)
$noLength = Get-SimcoreDeclaredDownloadBytes -Response $null -ExpectedBytes 0
Assert-Equal "null response without expected length" $noLength ([long]0)
Assert-True "file length meets minimum" (Test-SimcoreDownloadComplete -ActualBytes 314572800 -ExpectedBytes $noLength -MinimumBytes 314572800)
$lengthError = Format-SimcoreDownloadLengthError -Url "https://example.invalid/postgresql.exe" -ActualBytes 1000 -DeclaredBytes $noLength -MinimumBytes 314572800
Assert-Equal "length error names the minimum" $lengthError "Download of https://example.invalid/postgresql.exe is 1000 bytes (expected at least 314572800). The incomplete file was discarded."
Assert-True "null installer process fails" (-not (Test-SimcoreInstallerExit (Get-SimcoreProcessExitCode $null)))
$magicDir = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-magic-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $magicDir | Out-Null
$magicFile = Join-Path $magicDir "installer.exe"
[System.IO.File]::WriteAllBytes($magicFile, [byte[]](0x4D, 0x5A, 0x00))
$magic = Get-SimcoreFileMagicBytes $magicFile
Assert-True "read mz bytes" (Test-SimcoreFileMagic -First $magic[0] -Second $magic[1] -Kind "exe")
Remove-Item -LiteralPath $magicDir -Recurse -Force
Assert-True "mz header" (Test-SimcoreFileMagic -First 0x4D -Second 0x5A -Kind "exe")
Assert-True "html is not exe" (-not (Test-SimcoreFileMagic -First 0x3C -Second 0x68 -Kind "exe"))
Assert-True "zip header" (Test-SimcoreFileMagic -First 0x50 -Second 0x4B -Kind "zip")

Assert-True "exit 0" (Test-SimcoreInstallerExit 0)
Assert-True "exit 3010" (Test-SimcoreInstallerExit 3010)
Assert-True "exit 1 fails" (-not (Test-SimcoreInstallerExit 1))
Assert-True "missing exit fails" (-not (Test-SimcoreInstallerExit $null))

$winget = Format-WingetFailureMessage -Id "PostgreSQL.PostgreSQL.16" -ExitCode -1978335226
Assert-True "winget names the installer failure" ($winget.Contains("SHELLEXEC_INSTALL_FAILED") -and $winget.Contains("0x8A150006"))

Assert-Throws "blank password" { Assert-SimcoreSecretPresent -Value "  " -Name "POSTGRES_SUPER_PASSWORD" } "POSTGRES_SUPER_PASSWORD"
Assert-Throws "null installer arg" {
    Assert-SimcoreInstallerArguments -FilePath "C:\temp\postgresql.exe" -ArgumentList @("--superpassword", $null)
} "empty"

$floor = [long]1288490189
$free257 = [long]2759516483
$lowDisk = Format-SimcoreLowDiskMessage -Root "C:\" -AvailableBytes 1073741824 -MinimumBytes $floor
Assert-True "low disk names the drive" ($lowDisk.Contains("Not enough free space on C:\"))
Assert-Equal "low disk prints free and required" $lowDisk "Not enough free space on C:\. FreeGB=1.00 RequiredGB=1.20. Free space, then run bootstrap again."
Assert-True "1.00 GB is under the floor" (-not (Test-SimcoreDiskBudget -AvailableBytes 1073741824 -MinimumBytes $floor))
Assert-True "2.57 GB meets the floor" (Test-SimcoreDiskBudget -AvailableBytes $free257 -MinimumBytes $floor)
Assert-SimcoreFreeDisk -Path ([System.IO.Path]::GetTempPath()) -MinimumBytes 1 | Out-Null
Assert-Throws "huge disk requirement" {
    Assert-SimcoreFreeDisk -Path ([System.IO.Path]::GetTempPath()) -MinimumBytes ([int64]::MaxValue)
} "Not enough free space"

if ($script:Failed -gt 0) {
    Write-Host "$($script:Failed) assertion(s) failed"
    exit 1
}
Write-Host "bootstrap.tests.ps1 passed"
exit 0
