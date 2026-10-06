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

Assert-True "download exact" (Test-SimcoreDownloadComplete -ActualBytes 404741880 -ExpectedBytes 404741880 -MinimumBytes 314572800)
Assert-True "download short of declared" (-not (Test-SimcoreDownloadComplete -ActualBytes 1000 -ExpectedBytes 404741880 -MinimumBytes 1))
Assert-True "download under minimum" (-not (Test-SimcoreDownloadComplete -ActualBytes 1000 -ExpectedBytes 0 -MinimumBytes 314572800))
Assert-True "download meets minimum" (Test-SimcoreDownloadComplete -ActualBytes 314572800 -ExpectedBytes 0 -MinimumBytes 314572800)
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

$lowDisk = Format-SimcoreLowDiskMessage -Root "C:\" -AvailableBytes 1610612736 -MinimumBytes 3221225472
Assert-True "low disk names the drive" ($lowDisk.Contains("Not enough free space on C:\"))
Assert-True "low disk asks for 3 GB" ($lowDisk.Contains("3 GB"))
Assert-True "2 GB is under budget" (-not (Test-SimcoreDiskBudget -AvailableBytes 1610612736 -MinimumBytes 3221225472))
Assert-True "3 GB meets budget" (Test-SimcoreDiskBudget -AvailableBytes 3221225472 -MinimumBytes 3221225472)
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
