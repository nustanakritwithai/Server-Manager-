# Unit tests for Apache/Caddy coexistence. No Administrator and no Windows services required.
#   pwsh -NoProfile -File deploy/windows/coexist.tests.ps1

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

$one = Update-ApacheBindingText "Listen 80"
Assert-Equal "single listen" $one "Listen 127.0.0.1:8080"

$crlf = Update-ApacheBindingText "Listen 80`r`nListen 443`r`n"
Assert-Equal "crlf listen" $crlf "Listen 127.0.0.1:8080`r`nListen 127.0.0.1:8443`r`n"

$fixture = @(
    "# Listen 80",
    "Listen 80",
    "Listen 80",
    "Listen 443",
    "Listen 8080",
    "NameVirtualHost *:80",
    "<VirtualHost *:80>",
    "    ServerName localhost",
    "    Redirect / https://157.85.96.139/",
    "</VirtualHost>",
    "<VirtualHost _default_:443>",
    "    ServerName 157.85.96.139",
    "    RewriteRule ^/$ https://pocketmonster-game.web.app/ [R=302,L]",
    "</VirtualHost>",
    "<VirtualHost 157.85.96.139:443>",
    "    ServerName pocket.example.com",
    "    ServerAlias www.pocket.example.com localhost",
    "</VirtualHost>",
    "LoadModule md_module modules/mod_md.so",
    "MDomain 157.85.96.139",
    "# MDomain already-commented.example"
) -join "`n"

$expected = @(
    "# Listen 80",
    "Listen 127.0.0.1:8080",
    "Listen 127.0.0.1:8443",
    "Listen 8080",
    "NameVirtualHost 127.0.0.1:8080",
    "<VirtualHost 127.0.0.1:8080>",
    "    ServerName localhost",
    "    Redirect / https://157.85.96.139/",
    "</VirtualHost>",
    "<VirtualHost 127.0.0.1:8443>",
    "    ServerName 157.85.96.139",
    "    RewriteRule ^/$ https://pocketmonster-game.web.app/ [R=302,L]",
    "</VirtualHost>",
    "<VirtualHost 127.0.0.1:8443>",
    "    ServerName pocket.example.com",
    "    ServerAlias www.pocket.example.com localhost",
    "</VirtualHost>",
    "# simcore-coexist: LoadModule md_module modules/mod_md.so",
    "# simcore-coexist: MDomain 157.85.96.139",
    "# MDomain already-commented.example"
) -join "`n"

$once = Update-ApacheCoexistText $fixture
Assert-Equal "fixture rewrite" $once $expected
Assert-Equal "fixture idempotent" (Update-ApacheCoexistText $once) $once
Assert-True "server port untouched fixture" (-not (Test-ApacheUsesServerPort $fixture))
Assert-True "server port detected" (Test-ApacheUsesServerPort "RewriteCond %{SERVER_PORT} ^80$")
Assert-True "original listens 443" (Test-ApacheTextListensOnPort $fixture 443)
Assert-True "rewritten leaves public 443" (-not (Test-ApacheTextListensOnPort $once 443))
Assert-True "rewritten listens 8443" (Test-ApacheTextListensOnPort $once 8443)

$oneName = @(Get-ApacheServerNamesFromText "ServerName pocket.example.com")
Assert-Equal "one server name" ($oneName -join ",") "pocket.example.com"
$names = @(Get-ApacheServerNamesFromText $fixture)
$proxy = @(Select-ApacheProxyHosts -Names $names -ApiDomain "157-85-96-139.sslip.io" -PublicIp "157.85.96.139")
Assert-Equal "proxy hosts" (($proxy | Sort-Object) -join ",") "pocket.example.com,www.pocket.example.com"

Assert-Equal "public ip from sslip" (Get-SimcorePublicIp @{ API_DOMAIN = "157-85-96-139.sslip.io" }) "157.85.96.139"
Assert-Equal "public ip override" (Get-SimcorePublicIp @{ API_DOMAIN = "157-85-96-139.sslip.io"; SIMCORE_PUBLIC_IP = "203.0.113.10" }) "203.0.113.10"
Assert-Equal "public ip missing" (Get-SimcorePublicIp @{ API_DOMAIN = "game.example.com" }) ""

$simple = New-SimcoreCaddyfileText -EnvMap @{ API_DOMAIN = "157-85-96-139.sslip.io"; API_PORT = "8741" } -InstallRoot "C:\simcore"
Assert-True "simple has api upstream" ($simple.Contains("reverse_proxy 127.0.0.1:8741"))
Assert-True "simple omits apache" (-not $simple.Contains("shortlived"))
Assert-True "simple omits catch-all" (-not $simple.Contains("http://"))
$bareLf = ([regex]"(?<!`r)`n").IsMatch($simple)
Assert-True "simple has no bare lf" (-not $bareLf)

$coexistMap = @{
    API_DOMAIN = "157-85-96-139.sslip.io"
    API_PORT = "8741"
    ACME_EMAIL = "ops@example.com"
    SIMCORE_APACHE_HTTP = "127.0.0.1:8080"
    SIMCORE_APACHE_HTTPS = "127.0.0.1:8443"
    SIMCORE_PUBLIC_IP = "157.85.96.139"
    SIMCORE_APACHE_NAMES = "pocket.example.com, localhost, 157-85-96-139.sslip.io, 157.85.96.139"
}
$coexist = New-SimcoreCaddyfileText -EnvMap $coexistMap -InstallRoot "C:\simcore"
Assert-True "api proxy" ($coexist.Contains("reverse_proxy 127.0.0.1:8741"))
Assert-True "apache http" ($coexist.Contains("reverse_proxy 127.0.0.1:8080"))
Assert-True "apache https" ($coexist.Contains("reverse_proxy https://127.0.0.1:8443"))
Assert-True "host header" ($coexist.Contains("header_up Host {http.request.host}"))
Assert-True "skip verify" ($coexist.Contains("tls_insecure_skip_verify"))
Assert-True "shortlived" ($coexist.Contains("profile shortlived"))
Assert-True "default sni" ($coexist.Contains("default_sni 157.85.96.139"))
Assert-True "ip site" ($coexist.Contains("https://157.85.96.139 {"))
Assert-True "http catch-all" ($coexist.Contains("http:// {"))
Assert-True "extra name" ($coexist.Contains("pocket.example.com {"))
Assert-True "email kept" ($coexist.Contains("email ops@example.com"))
Assert-True "localhost not a site" (-not $coexist.Contains("localhost {"))
$shortlivedCount = ([regex]::Matches($coexist, "profile shortlived")).Count
Assert-Equal "one shortlived profile" $shortlivedCount 1

$rejected = $false
try {
    New-SimcoreCaddyfileText -EnvMap @{
        API_DOMAIN = "157-85-96-139.sslip.io"
        SIMCORE_APACHE_HTTP = "157.85.96.139:80"
        SIMCORE_APACHE_HTTPS = "127.0.0.1:8443"
        SIMCORE_PUBLIC_IP = "157.85.96.139"
    } -InstallRoot "C:\simcore" | Out-Null
} catch {
    $rejected = $true
}
Assert-True "rejects public apache upstream" $rejected

Assert-True "httpd is foreign" (Test-ForeignWebListener @([pscustomobject]@{ ProcessName = "httpd"; Path = "C:\xampp\apache\bin\httpd.exe" }))
Assert-True "our caddy is not foreign" (-not (Test-ForeignWebListener @([pscustomobject]@{ ProcessName = "caddy"; Path = "C:\simcore\tools\caddy.exe" })))
Assert-True "null owners are not foreign" (-not (Test-ForeignWebListener $null))
Assert-True "empty owners are not foreign" (-not (Test-ForeignWebListener @()))
Assert-True "simcore path is ours" (Test-SimcoreWebProcess -ProcessName "httpd" -Path "C:\simcore\tools\caddy.exe")

Assert-True "win-acme matches" (Test-CompetingAcmeName "\win-acme\renew")
Assert-True "wacs matches" (Test-CompetingAcmeName "wacs")
Assert-True "certbot matches" (Test-CompetingAcmeName "Certbot Renew")
Assert-True "simcore does not match acme" (-not (Test-CompetingAcmeName "simcore-caddy"))
Assert-True "mywacs does not match" (-not (Test-CompetingAcmeName "mywacs"))

$kept = Remove-SimcoreApacheCoexistKeys -Map @{
    SIMCORE_ADMIN_TOKEN = "secret"
    SIMCORE_APACHE_HTTP = "127.0.0.1:8080"
    SIMCORE_APACHE_HTTPS = "127.0.0.1:8443"
    SIMCORE_PUBLIC_IP = "157.85.96.139"
    SIMCORE_APACHE_NAMES = "pocket.example.com"
}
Assert-Equal "token kept" $kept["SIMCORE_ADMIN_TOKEN"] "secret"
Assert-True "apache key removed" (-not $kept.ContainsKey("SIMCORE_APACHE_HTTP"))

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("simcore-coexist-" + [guid]::NewGuid().ToString("N"))
$confDir = Join-Path $root "conf"
$extraDir = Join-Path $confDir "extra"
$enabledDir = Join-Path $extraDir "enabled"
New-Item -ItemType Directory -Force -Path $enabledDir | Out-Null
$main = Join-Path $confDir "httpd.conf"
$ssl = Join-Path $extraDir "httpd-ssl.conf"
$vhost = Join-Path $extraDir "httpd-vhosts.conf"
$enabled = Join-Path $enabledDir "one.conf"
$srv = $root -replace '\\', '/'
@(
    "Define SRVROOT `"$srv`"",
    "Include `"`${SRVROOT}/conf/extra/httpd-ssl.conf`"",
    "#Include `"`${SRVROOT}/conf/extra/httpd-vhosts.conf`"",
    "IncludeOptional conf/extra/enabled/*.conf"
) | Set-Content -LiteralPath $main -Encoding utf8
Set-Content -LiteralPath $ssl -Value "Listen 443" -Encoding utf8
Set-Content -LiteralPath $vhost -Value "Listen 9" -Encoding utf8
Set-Content -LiteralPath $enabled -Value "Listen 8080" -Encoding utf8
$closure = @(Get-ApacheConfigClosure -ServerRoot $root -MainConfig (Join-Path "conf" "httpd.conf"))
$leaves = @($closure | ForEach-Object { Split-Path -Leaf $_ } | Sort-Object)
Assert-Equal "closure files" ($leaves -join ",") "httpd-ssl.conf,httpd.conf,one.conf"

$originalSsl = [System.IO.File]::ReadAllText($ssl)
$backup = Backup-ApacheFiles -InstallRoot $root -Files @($main, $ssl)
Set-Content -LiteralPath $ssl -Value "Listen 127.0.0.1:8443" -Encoding utf8
Restore-ApacheBackup -BackupDir $backup
$restored = [System.IO.File]::ReadAllText($ssl)
Assert-Equal "backup restore" $restored.Trim() $originalSsl.Trim()
Remove-Item -LiteralPath $root -Recurse -Force

$example = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "Caddyfile.example"))
foreach ($snippet in @(
    "profile shortlived",
    "reverse_proxy 127.0.0.1:8741",
    "reverse_proxy 127.0.0.1:8080",
    "reverse_proxy https://127.0.0.1:8443",
    "header_up Host {http.request.host}",
    "tls_insecure_skip_verify",
    "default_sni 157.85.96.139",
    "157-85-96-139.sslip.io"
)) {
    Assert-True "example contains $snippet" ($example.Contains($snippet))
}

if ($script:Failed -gt 0) {
    Write-Host "$($script:Failed) assertion(s) failed"
    exit 1
}
Write-Host "coexist.tests.ps1 passed"
exit 0
