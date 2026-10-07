# Apache/Caddy coexistence helpers. Dot-sourced from Common.ps1. Do not run directly.
# Pure text functions are covered by coexist.tests.ps1 and do not need Administrator.

function Get-ApacheListenPort {
    param([string]$Token)
    if (-not $Token) { return $null }
    if ($Token -match '^\[.+\]:(\d+)$') { return [int]$Matches[1] }
    if ($Token -match ':(\d+)$') { return [int]$Matches[1] }
    if ($Token -match '^\d+$') { return [int]$Token }
    return $null
}

function Get-ApacheMovedListen {
    param([string]$Token)
    $port = Get-ApacheListenPort $Token
    if ($port -eq 80) { return "127.0.0.1:8080" }
    if ($port -eq 443) { return "127.0.0.1:8443" }
    return $null
}

function Get-ApacheTextLines {
    param([string]$Text)
    if ($null -eq $Text) { $Text = "" }
    # Emit one string per line. Callers wrap with @() so a single line stays an array.
    $Text -split "`r?`n"
}

function Join-ApacheTextLines {
    param([string]$Text, [System.Collections.Generic.List[string]]$Lines)
    $newline = "`n"
    if ($Text -match "`r`n") { $newline = "`r`n" }
    return [string]::Join($newline, $Lines.ToArray())
}

function Update-ApacheBindingText {
    param([string]$Text)
    if ($null -eq $Text) { return "" }
    $lines = @(Get-ApacheTextLines $Text)
    $seenListen = @{}
    $result = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match '^\s*#') {
            $result.Add($line)
            continue
        }
        if ($line -match '^(\s*)Listen\s+(\S+)(.*)$') {
            $indent = $Matches[1]
            $token = $Matches[2]
            $rest = $Matches[3]
            $moved = Get-ApacheMovedListen $token
            $final = $token
            $output = $line
            if ($moved) {
                $final = $moved
                $output = "${indent}Listen $moved$rest"
            }
            if ($seenListen.ContainsKey($final)) { continue }
            $seenListen[$final] = $true
            $result.Add($output)
            continue
        }
        if ($line -match '^(\s*)(<VirtualHost\s+)([^>]+)(>.*)$') {
            $indent = $Matches[1]
            $prefix = $Matches[2]
            $addrs = $Matches[3]
            $suffix = $Matches[4]
            $parts = @($addrs.Trim() -split '\s+')
            $changed = $false
            $newParts = New-Object System.Collections.Generic.List[string]
            foreach ($part in $parts) {
                $moved = Get-ApacheMovedListen $part
                if ($moved) {
                    $changed = $true
                    $newParts.Add($moved)
                } else {
                    $newParts.Add($part)
                }
            }
            if ($changed) {
                $result.Add("$indent$prefix$($newParts -join ' ')$suffix")
            } else {
                $result.Add($line)
            }
            continue
        }
        if ($line -match '^(\s*)(NameVirtualHost\s+)(\S+)(.*)$') {
            $indent = $Matches[1]
            $prefix = $Matches[2]
            $token = $Matches[3]
            $rest = $Matches[4]
            $moved = Get-ApacheMovedListen $token
            if ($moved) {
                $result.Add("$indent$prefix$moved$rest")
            } else {
                $result.Add($line)
            }
            continue
        }
        $result.Add($line)
    }
    return Join-ApacheTextLines -Text $Text -Lines $result
}

function Disable-ApacheManagedDomainText {
    param([string]$Text)
    if ($null -eq $Text) { return "" }
    $lines = @(Get-ApacheTextLines $Text)
    $result = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match '^\s*#') {
            $result.Add($line)
            continue
        }
        if ($line -match '^\s*(LoadModule\s+md_module\b|</?MDomain\b|MD[A-Z][A-Za-z0-9]*\b)') {
            $result.Add("# simcore-coexist: $line")
            continue
        }
        $result.Add($line)
    }
    return Join-ApacheTextLines -Text $Text -Lines $result
}

function Update-ApacheCoexistText {
    param([string]$Text)
    return Disable-ApacheManagedDomainText (Update-ApacheBindingText $Text)
}

function Test-ApacheUsesServerPort {
    param([string]$Text)
    foreach ($line in @(Get-ApacheTextLines $Text)) {
        if ($line -match '^\s*#') { continue }
        if ($line -match 'SERVER_PORT') { return $true }
    }
    return $false
}

function Test-ApacheTextListensOnPort {
    param([string]$Text, [int]$Port)
    foreach ($line in @(Get-ApacheTextLines $Text)) {
        if ($line -match '^\s*#') { continue }
        $tokens = @()
        if ($line -match '^\s*Listen\s+(\S+)') {
            $tokens += $Matches[1]
        } elseif ($line -match '^\s*<VirtualHost\s+([^>]+)>') {
            $tokens = @($Matches[1].Trim() -split '\s+')
        } elseif ($line -match '^\s*NameVirtualHost\s+(\S+)') {
            $tokens += $Matches[1]
        }
        foreach ($token in $tokens) {
            $parsed = Get-ApacheListenPort $token
            if ($parsed -eq $Port) { return $true }
        }
    }
    return $false
}

function Get-ApacheServerNamesFromText {
    param([string]$Text)
    $names = New-Object System.Collections.Generic.List[string]
    foreach ($line in @(Get-ApacheTextLines $Text)) {
        if ($line -match '^\s*#') { continue }
        if ($line -match '^\s*ServerName\s+(\S+)') {
            $names.Add($Matches[1])
            continue
        }
        if ($line -match '^\s*ServerAlias\s+(.+)$') {
            foreach ($part in @($Matches[1].Trim() -split '\s+')) {
                if ($part -and -not $part.StartsWith("#")) { $names.Add($part) }
            }
        }
    }
    foreach ($name in $names) { $name }
}

function ConvertTo-ApacheHostName {
    param([string]$Token)
    if (-not $Token) { return "" }
    $name = $Token.Trim().TrimEnd(".")
    if ($name -match '^https?://') { $name = $name -replace '^https?://', '' }
    if ($name -match '^\[') { return $name }
    if ($name -match '^([^:]+):\d+$') { return $Matches[1] }
    return $name
}

function Select-ApacheProxyHosts {
    param($Names, [string]$ApiDomain, [string]$PublicIp)
    $chosen = New-Object System.Collections.Generic.List[string]
    if ($null -eq $Names) { return }
    $skip = @("localhost", "127.0.0.1", "::1", "*", "_default_")
    $seen = @{}
    foreach ($raw in @($Names)) {
        $name = ConvertTo-ApacheHostName ([string]$raw)
        if (-not $name) { continue }
        $key = $name.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        if ($skip -contains $key) { continue }
        if ($PublicIp -and $key -eq $PublicIp.ToLowerInvariant()) { continue }
        if ($ApiDomain -and $key -eq $ApiDomain.ToLowerInvariant()) { continue }
        if ($name -match '^\d{1,3}(\.\d{1,3}){3}$') { continue }
        if ($name -notmatch '^[A-Za-z0-9.-]+$') { continue }
        if ($name -notmatch '\.') { continue }
        $chosen.Add($name)
    }
    foreach ($name in $chosen) { $name }
}

function Get-ApacheDefineMap {
    param([string]$Text)
    $map = @{}
    foreach ($line in @(Get-ApacheTextLines $Text)) {
        if ($line -match '^\s*#') { continue }
        if ($line -match '^\s*Define\s+([A-Za-z0-9_]+)\s+("([^"]*)"|''([^'']*)''|(\S+))\s*$') {
            $value = $Matches[3]
            if (-not $value) { $value = $Matches[4] }
            if (-not $value) { $value = $Matches[5] }
            if ($null -eq $value) { $value = "" }
            $map[$Matches[1]] = [string]$value
        }
    }
    return $map
}

function Get-ApacheIncludeDirectives {
    param([string]$Text)
    $paths = New-Object System.Collections.Generic.List[string]
    foreach ($line in @(Get-ApacheTextLines $Text)) {
        if ($line -match '^\s*#') { continue }
        if ($line -match '^\s*Include(?:Optional)?\s+(.+?)\s*$') {
            $raw = $Matches[1].Trim()
            if ($raw -match '^(?:"([^"]+)"|''([^'']+)''|(\S+))') {
                $path = $Matches[1]
                if (-not $path) { $path = $Matches[2] }
                if (-not $path) { $path = $Matches[3] }
                if ($path) { $paths.Add($path) }
            }
        }
    }
    foreach ($path in $paths) { $path }
}

function Expand-ApacheDefinedPath {
    param([string]$Path, [hashtable]$Defines)
    $expanded = $Path
    if (-not $Defines) { return $expanded }
    foreach ($key in @($Defines.Keys)) {
        $expanded = $expanded.Replace('${' + $key + '}', [string]$Defines[$key])
    }
    return $expanded
}

function Test-ApacheAbsolutePath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    $text = $Path.Trim().Trim('"')
    if ($text -match '^[A-Za-z]:[\\/]') { return $true }
    if ($text -match '^[\\/]{2}[^\\/]') { return $true }
    # XAMPP's compiled HTTPD_ROOT is /apache. On Windows that is printed as \apache.
    # A single leading slash is not an install directory.
    $unix = ($text -replace '\\', '/').TrimEnd('/')
    if ($unix -eq '/apache' -or $unix.StartsWith('/apache/')) { return $false }
    if ($text.StartsWith('/')) { return $true }
    return $false
}

function Get-ApacheExecutableRoot {
    param([string]$Executable)
    if ([string]::IsNullOrWhiteSpace($Executable)) { return "" }
    $bin = Split-Path -Parent $Executable
    if ([string]::IsNullOrWhiteSpace($bin)) { return "" }
    if ((Split-Path -Leaf $bin) -eq "bin") {
        $parent = Split-Path -Parent $bin
        if (-not [string]::IsNullOrWhiteSpace($parent)) { return $parent }
    }
    return $bin
}

function Resolve-ApacheInstallPaths {
    param(
        [string]$Executable,
        [string]$ReportedRoot,
        [string]$ReportedConfig
    )
    $fromExe = Get-ApacheExecutableRoot -Executable $Executable
    $reported = ([string]$ReportedRoot).Trim().Trim('"')
    $root = ""
    if (Test-ApacheAbsolutePath $reported) {
        $slash = [System.IO.Path]::DirectorySeparatorChar
        $candidate = [System.IO.Path]::GetFullPath(($reported -replace '[\\/]', $slash))
        if (Test-Path -LiteralPath $candidate) { $root = $candidate }
    }
    if (-not $root) {
        if ([string]::IsNullOrWhiteSpace($fromExe) -or -not (Test-Path -LiteralPath $fromExe)) {
            throw "httpd -V reported ServerRoot '$reported', which is not a usable absolute directory, and httpd.exe is not inside an install folder."
        }
        $root = [System.IO.Path]::GetFullPath($fromExe)
    }
    $config = ([string]$ReportedConfig).Trim().Trim('"')
    if ([string]::IsNullOrWhiteSpace($config)) { $config = "conf/httpd.conf" }
    return [pscustomobject]@{
        ServerRoot = $root
        ConfigFile = $config
        ReportedRoot = $reported
        UsedExecutableRoot = ($root -eq [System.IO.Path]::GetFullPath($fromExe))
    }
}

function Join-ApacheServerPath {
    param([string]$ServerRoot, [string]$Relative)
    if ([string]::IsNullOrWhiteSpace($ServerRoot) -or -not (Test-ApacheAbsolutePath $ServerRoot)) {
        throw "Apache ServerRoot '$ServerRoot' is not an absolute directory."
    }
    $slash = [System.IO.Path]::DirectorySeparatorChar
    $relativeText = ([string]$Relative).Trim().Trim('"')
    if (Test-ApacheAbsolutePath $relativeText) {
        $absolute = $relativeText -replace '[\\/]', $slash
        if ($absolute -match '[\*\?]') { return $absolute }
        return [System.IO.Path]::GetFullPath($absolute)
    }
    $unix = $relativeText -replace '\\', '/'
    if ($unix -eq '/apache') { $unix = "" }
    elseif ($unix.StartsWith('/apache/')) { $unix = $unix.Substring('/apache/'.Length) }
    $unix = $unix.TrimStart('/')
    $normalized = $unix -replace '/', $slash
    $combined = $ServerRoot
    foreach ($part in ($normalized.Split($slash))) {
        if ($part -eq "" -or $part -eq ".") { continue }
        $combined = Join-Path $combined $part
    }
    if ($combined -match '[\*\?]') { return $combined }
    return [System.IO.Path]::GetFullPath($combined)
}

function Get-ApacheConfigClosure {
    param([string]$ServerRoot, [string]$MainConfig)
    $files = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    $defines = @{}
    $queue = New-Object System.Collections.Generic.Queue[string]
    $queue.Enqueue((Join-ApacheServerPath -ServerRoot $ServerRoot -Relative $MainConfig))
    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        $key = $current.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        if (-not (Test-Path -LiteralPath $current)) { continue }
        $seen[$key] = $true
        $files.Add($current)
        $text = [System.IO.File]::ReadAllText($current)
        foreach ($entry in (Get-ApacheDefineMap $text).GetEnumerator()) {
            $defines[$entry.Key] = [string]$entry.Value
        }
        foreach ($inc in @(Get-ApacheIncludeDirectives $text)) {
            $expanded = Expand-ApacheDefinedPath -Path $inc -Defines $defines
            if ($expanded -match '[\*\?]') {
                $fullPattern = Join-ApacheServerPath -ServerRoot $ServerRoot -Relative $expanded
                $dir = [System.IO.Path]::GetDirectoryName($fullPattern)
                $leaf = [System.IO.Path]::GetFileName($fullPattern)
                if ($dir -and (Test-Path -LiteralPath $dir)) {
                    foreach ($match in @([System.IO.Directory]::GetFiles($dir, $leaf))) {
                        $queue.Enqueue($match)
                    }
                }
            } else {
                $queue.Enqueue((Join-ApacheServerPath -ServerRoot $ServerRoot -Relative $expanded))
            }
        }
    }
    foreach ($file in $files) { $file }
}

function Get-SimcorePublicIp {
    param([hashtable]$EnvMap)
    if (-not $EnvMap) { return "" }
    $configured = [string]$EnvMap["SIMCORE_PUBLIC_IP"]
    if ($configured) { return $configured.Trim() }
    $domain = [string]$EnvMap["API_DOMAIN"]
    if ($domain -match '^(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})\.sslip\.io$') {
        $octets = @([int]$Matches[1], [int]$Matches[2], [int]$Matches[3], [int]$Matches[4])
        foreach ($octet in $octets) {
            if ($octet -gt 255) { return "" }
        }
        return "$($octets[0]).$($octets[1]).$($octets[2]).$($octets[3])"
    }
    return ""
}

function Test-SimcoreLocalUpstream {
    param([string]$Value)
    return [bool]($Value -match '^127\.0\.0\.1:\d+$')
}

function Add-SimcoreApacheHttpProxy {
    param($Lines, [string]$Upstream)
    $Lines.Add("    reverse_proxy $Upstream {")
    $Lines.Add("        header_up Host {http.request.host}")
    $Lines.Add("        header_up X-Forwarded-For {http.request.remote.host}")
    $Lines.Add("        header_up X-Forwarded-Proto http")
    $Lines.Add("        header_up X-Forwarded-Host {http.request.host}")
    $Lines.Add("    }")
}

function Add-SimcoreApacheHttpsProxy {
    param($Lines, [string]$Upstream, [string]$ServerName)
    $Lines.Add("    reverse_proxy https://$Upstream {")
    $Lines.Add("        header_up Host {http.request.host}")
    $Lines.Add("        header_up X-Forwarded-For {http.request.remote.host}")
    $Lines.Add("        header_up X-Forwarded-Proto https")
    $Lines.Add("        header_up X-Forwarded-Host {http.request.host}")
    $Lines.Add("        transport http {")
    $Lines.Add("            tls")
    $Lines.Add("            tls_insecure_skip_verify")
    $Lines.Add("            tls_server_name $ServerName")
    $Lines.Add("        }")
    $Lines.Add("    }")
}

function Get-CaddyValidateArguments {
    param([string]$ConfigPath)
    $config = ([string]$ConfigPath) -replace '\\', '/'
    # A temp path is not named Caddyfile, so Caddy would parse it as JSON.
    return @("validate", "--config", $config, "--adapter", "caddyfile")
}

function New-SimcoreCaddyfileText {
    param([hashtable]$EnvMap, [string]$InstallRoot)
    $domain = [string]$EnvMap["API_DOMAIN"]
    if (-not $domain) { throw "API_DOMAIN is missing from .env.prod" }
    if ($domain -notmatch '^[A-Za-z0-9.-]+$') { throw "API_DOMAIN contains characters this Caddyfile writer will not emit." }
    $port = [string]$EnvMap["API_PORT"]
    if (-not $port) { $port = "8741" }
    if ($port -notmatch '^\d+$') { throw "API_PORT must be a number." }
    $email = [string]$EnvMap["ACME_EMAIL"]
    if ($email -and $email -notmatch '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$') {
        throw "ACME_EMAIL is not a plain email address."
    }
    $data = ($InstallRoot + "\caddy-data") -replace "\\", "/"
    $apacheHttp = [string]$EnvMap["SIMCORE_APACHE_HTTP"]
    if (-not $apacheHttp) {
        $simple = New-Object System.Collections.Generic.List[string]
        $simple.Add("{")
        $simple.Add("    storage file_system {")
        $simple.Add("        root $data")
        $simple.Add("    }")
        if ($email) { $simple.Add("    email $email") }
        $simple.Add("}")
        $simple.Add("")
        $simple.Add("$domain {")
        $simple.Add("    encode gzip")
        $simple.Add("    reverse_proxy 127.0.0.1:$port")
        $simple.Add("}")
        return ($simple -join "`r`n") + "`r`n"
    }
    if (-not (Test-SimcoreLocalUpstream $apacheHttp)) {
        throw "SIMCORE_APACHE_HTTP must stay on 127.0.0.1. Refusing to publish Apache."
    }
    $apacheHttps = [string]$EnvMap["SIMCORE_APACHE_HTTPS"]
    if (-not (Test-SimcoreLocalUpstream $apacheHttps)) {
        throw "SIMCORE_APACHE_HTTPS must stay on 127.0.0.1. Refusing to publish Apache."
    }
    $publicIp = Get-SimcorePublicIp $EnvMap
    if ($publicIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
        throw "SIMCORE_PUBLIC_IP must be an IPv4 address so Caddy can serve the existing IP certificate."
    }
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("{")
    $lines.Add("    storage file_system {")
    $lines.Add("        root $data")
    $lines.Add("    }")
    $lines.Add("    default_sni $publicIp")
    if ($email) { $lines.Add("    email $email") }
    $lines.Add("}")
    $lines.Add("")
    $lines.Add("$domain {")
    $lines.Add("    encode gzip")
    $lines.Add("    reverse_proxy 127.0.0.1:$port")
    $lines.Add("}")
    $lines.Add("")
    $lines.Add("http:// {")
    Add-SimcoreApacheHttpProxy -Lines $lines -Upstream $apacheHttp
    $lines.Add("}")
    $lines.Add("")
    $lines.Add("https://$publicIp {")
    $lines.Add("    tls {")
    $lines.Add("        issuer acme {")
    $lines.Add("            profile shortlived")
    $lines.Add("        }")
    $lines.Add("        renewal_window_ratio 0.5")
    $lines.Add("    }")
    Add-SimcoreApacheHttpsProxy -Lines $lines -Upstream $apacheHttps -ServerName $publicIp
    $lines.Add("}")
    $extraRaw = @()
    if ($EnvMap["SIMCORE_APACHE_NAMES"]) {
        $extraRaw = @(([string]$EnvMap["SIMCORE_APACHE_NAMES"]) -split ',')
    }
    $extra = @(Select-ApacheProxyHosts -Names $extraRaw -ApiDomain $domain -PublicIp $publicIp)
    foreach ($name in $extra) {
        $lines.Add("")
        $lines.Add("$name {")
        Add-SimcoreApacheHttpsProxy -Lines $lines -Upstream $apacheHttps -ServerName $name
        $lines.Add("}")
    }
    return ($lines -join "`r`n") + "`r`n"
}

function Copy-SimcoreHashtable {
    param([hashtable]$Map)
    $copy = @{}
    if (-not $Map) { return $copy }
    foreach ($key in @($Map.Keys)) { $copy[$key] = $Map[$key] }
    return $copy
}

function Remove-SimcoreApacheCoexistKeys {
    param([hashtable]$Map)
    foreach ($key in @("SIMCORE_APACHE_HTTP", "SIMCORE_APACHE_HTTPS", "SIMCORE_PUBLIC_IP", "SIMCORE_APACHE_NAMES")) {
        if ($Map.ContainsKey($key)) { [void]$Map.Remove($key) }
    }
    return $Map
}

function Test-SimcoreWebProcess {
    param([string]$ProcessName, [string]$Path)
    if ($ProcessName -and $ProcessName -match '^(?i)(caddy|simcore-caddy)$') { return $true }
    if ($Path -and $Path -match '(?i)[/\\]simcore[/\\]') { return $true }
    return $false
}

function Test-ForeignWebListener {
    param($Owners)
    if ($null -eq $Owners) { return $false }
    foreach ($owner in @($Owners)) {
        if ($null -eq $owner) { continue }
        $name = [string]$owner.ProcessName
        $path = [string]$owner.Path
        if (-not (Test-SimcoreWebProcess -ProcessName $name -Path $path)) { return $true }
    }
    return $false
}

function Test-CompetingAcmeName {
    param([string]$Name)
    if (-not $Name) { return $false }
    return [bool]($Name -match '(?i)(win-?acme|simple-acme|certbot|letsencrypt|certify|\bwacs\b)')
}

function Backup-ApacheFiles {
    param([string]$InstallRoot, [string[]]$Files)
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $dir = Join-Path (Join-Path $InstallRoot "apache-backups") $stamp
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $lines = New-Object System.Collections.Generic.List[string]
    $index = 0
    foreach ($file in @($Files)) {
        if (-not $file) { continue }
        $index++
        $dest = "{0:D3}-{1}" -f $index, (Split-Path -Leaf $file)
        Copy-Item -LiteralPath $file -Destination (Join-Path $dir $dest) -Force
        $lines.Add("$file`t$dest")
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines((Join-Path $dir "manifest.txt"), $lines.ToArray(), $utf8)
    return $dir
}

function Restore-ApacheBackup {
    param([string]$BackupDir)
    $manifest = Join-Path $BackupDir "manifest.txt"
    if (-not (Test-Path -LiteralPath $manifest)) { throw "Apache backup manifest is missing: $manifest" }
    foreach ($line in @([System.IO.File]::ReadAllLines($manifest))) {
        if (-not $line -or -not $line.Trim()) { continue }
        $tab = $line.IndexOf("`t")
        if ($tab -lt 1) { throw "Apache backup manifest line is invalid: $line" }
        $original = $line.Substring(0, $tab)
        $dest = $line.Substring($tab + 1)
        $parent = Split-Path -Parent $original
        if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Copy-Item -LiteralPath (Join-Path $BackupDir $dest) -Destination $original -Force
    }
}

function Get-ApacheCoexistStatePath {
    param([string]$InstallRoot)
    return Join-Path $InstallRoot "apache-coexist-state.json"
}

function Read-ApacheCoexistState {
    param([string]$InstallRoot)
    $path = Get-ApacheCoexistStatePath $InstallRoot
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json)
}

function Write-ApacheCoexistState {
    param([string]$InstallRoot, $State)
    $path = Get-ApacheCoexistStatePath $InstallRoot
    $json = $State | ConvertTo-Json -Depth 6
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($path, $json.Trim() + "`r`n", $utf8)
    return $path
}

function ConvertTo-CoexistList {
    param($Value)
    if ($null -eq $Value) { return }
    foreach ($item in @($Value)) {
        if ($null -ne $item) { $item }
    }
}

function Format-WebListener {
    param($Owner)
    $path = [string]$Owner.Path
    if (-not $path) { $path = "(path unavailable)" }
    return "port $($Owner.Port): $($Owner.ProcessName) pid $($Owner.ProcessId) $path"
}

function Write-ForeignWebPortHelp {
    param($Foreign, [string]$ScriptRoot)
    Write-Host ""
    Write-Host "Another program is already listening on TCP 80 or 443. Caddy was not started."
    foreach ($item in @(ConvertTo-CoexistList $Foreign)) {
        if ($null -eq $item) { continue }
        Write-Host ("  " + (Format-WebListener $item))
    }
    Write-Host "That program was left running. IIS was not stopped or disabled."
    Write-Host "The API and the worker can still run on localhost."
    $script = Join-Path $ScriptRoot "coexist-apache.ps1"
    Write-Host "To share ports 80 and 443 with the existing Apache, run:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$script`""
    Write-Host ""
}

function Get-WebPortOwners {
    $owners = New-Object System.Collections.Generic.List[object]
    $seen = @{}
    foreach ($port in @(80, 443)) {
        $conns = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
        foreach ($conn in $conns) {
            if ($null -eq $conn) { continue }
            $key = "$port|$($conn.OwningProcess)"
            if ($seen.ContainsKey($key)) { continue }
            $seen[$key] = $true
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            $path = ""
            $name = ""
            if ($proc) {
                $name = [string]$proc.ProcessName
                try { $path = [string]$proc.Path } catch { $path = "" }
            }
            $owners.Add([pscustomobject]@{
                Port = $port
                ProcessId = [int]$conn.OwningProcess
                ProcessName = $name
                Path = $path
                Address = [string]$conn.LocalAddress
            })
        }
    }
    foreach ($owner in $owners) { $owner }
}

function Get-ForeignWebListeners {
    $foreign = New-Object System.Collections.Generic.List[object]
    foreach ($owner in @(Get-WebPortOwners)) {
        if ($null -eq $owner) { continue }
        if (-not (Test-SimcoreWebProcess -ProcessName $owner.ProcessName -Path $owner.Path)) {
            $foreign.Add($owner)
        }
    }
    foreach ($item in $foreign) { $item }
}

function Resolve-HttpdExecutableFromCommand {
    param([string]$CommandLine)
    if (-not $CommandLine) { return "" }
    if ($CommandLine -match '"([^"]*httpd\.exe)"') { return $Matches[1] }
    if ($CommandLine -match '(\S*httpd\.exe)') { return $Matches[1] }
    return ""
}

function Find-ApacheInstall {
    $serviceName = ""
    $exe = ""
    $services = @()
    try {
        $services = @(Get-CimInstance Win32_Service -ErrorAction Stop | Where-Object {
            $_.PathName -and $_.PathName -match 'httpd\.exe'
        })
    } catch {
        $services = @()
    }
    $portOwners = @(Get-WebPortOwners)
    $httpdPids = @()
    foreach ($owner in $portOwners) {
        if ($owner.ProcessName -match '^(?i)httpd$') { $httpdPids += [int]$owner.ProcessId }
    }
    $chosen = $null
    foreach ($service in $services) {
        if ($httpdPids -contains [int]$service.ProcessId) { $chosen = $service; break }
    }
    if (-not $chosen -and $services.Count -gt 0) { $chosen = $services[0] }
    if ($chosen) {
        $serviceName = [string]$chosen.Name
        $exe = Resolve-HttpdExecutableFromCommand $chosen.PathName
    }
    if (-not $exe) {
        $processes = @()
        try {
            $processes = @(Get-CimInstance Win32_Process -Filter "Name = 'httpd.exe'" -ErrorAction Stop)
        } catch {
            $processes = @()
        }
        foreach ($proc in $processes) {
            if ($httpdPids.Count -eq 0 -or $httpdPids -contains [int]$proc.ProcessId) {
                $exe = [string]$proc.ExecutablePath
                break
            }
        }
    }
    if (-not $exe) {
        $candidates = @(
            "C:\xampp\apache\bin\httpd.exe",
            "C:\Apache24\bin\httpd.exe",
            (Join-Path $env:ProgramFiles "Apache Software Foundation\Apache2.4\bin\httpd.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Apache Software Foundation\Apache2.4\bin\httpd.exe")
        )
        foreach ($candidate in $candidates) {
            if ($candidate -and (Test-Path -LiteralPath $candidate)) { $exe = $candidate; break }
        }
    }
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
        throw "Apache (httpd.exe) was not found as a Windows service, a running process, or under C:\xampp, C:\Apache24, or Program Files. Install path detection stopped rather than guessing."
    }
    $versionOutput = ""
    $serverRoot = ""
    $configFile = ""
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $versionOutput = & $exe -V 2>&1 | Out-String
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($versionOutput -match 'HTTPD_ROOT="([^"]+)"') { $serverRoot = $Matches[1] }
    if ($versionOutput -match 'SERVER_CONFIG_FILE="([^"]+)"') { $configFile = $Matches[1] }
    $paths = Resolve-ApacheInstallPaths -Executable $exe -ReportedRoot $serverRoot -ReportedConfig $configFile
    if ($paths.ReportedRoot -and -not (Test-ApacheAbsolutePath $paths.ReportedRoot)) {
        Write-Host "httpd -V reported ServerRoot '$($paths.ReportedRoot)', which is not an absolute directory. Using $($paths.ServerRoot) from httpd.exe."
    }
    $mainConfig = Join-ApacheServerPath -ServerRoot $paths.ServerRoot -Relative $paths.ConfigFile
    if (-not (Test-Path -LiteralPath $mainConfig)) {
        throw "No readable Apache configuration was found at $mainConfig. httpd -V reported ServerRoot '$serverRoot' and SERVER_CONFIG_FILE '$configFile'."
    }
    return [pscustomobject]@{
        Executable = $exe
        ServiceName = $serviceName
        ServerRoot = $paths.ServerRoot
        ConfigFile = $mainConfig
        VersionOutput = $versionOutput.Trim()
    }
}

function Test-ApacheConfigOk {
    param($ExitCode, [string]$Output)
    if ($ExitCode -eq 0) { return $true }
    if ($Output -and $Output.Contains("Syntax OK")) { return $true }
    return $false
}

function Convert-SimcoreNativeText {
    param($Value)
    if ($null -eq $Value) { return "" }
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($item in @($Value)) {
        if ($null -eq $item) { continue }
        $parts.Add(([string]$item).TrimEnd())
    }
    return (($parts.ToArray()) -join "`n").Trim()
}

function Invoke-SimcoreNative {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )
    if ([string]::IsNullOrWhiteSpace($FilePath)) { throw "Native command path is empty." }
    if ($null -eq $ArgumentList) { $ArgumentList = @() }
    # Windows PowerShell turns native stderr into a terminating NativeCommandError
    # when ErrorActionPreference is Stop and stderr is merged with 2>&1.
    # httpd -t writes "Syntax OK" to stderr.
    $previous = $ErrorActionPreference
    $hadNative = Test-Path variable:PSNativeCommandUseErrorActionPreference
    $previousNative = $null
    if ($hadNative) { $previousNative = $PSNativeCommandUseErrorActionPreference }
    $ErrorActionPreference = "Continue"
    if ($hadNative) { $PSNativeCommandUseErrorActionPreference = $false }
    try {
        $raw = & $FilePath @ArgumentList 2>&1
        $code = $LASTEXITCODE
        return [pscustomobject]@{
            ExitCode = $code
            Output = (Convert-SimcoreNativeText $raw)
        }
    } catch {
        $message = ""
        if ($_.Exception) { $message = [string]$_.Exception.Message }
        $id = [string]$_.FullyQualifiedErrorId
        if ($id -match 'NativeCommandError' -or $message.Contains("Syntax OK")) {
            return [pscustomobject]@{
                ExitCode = $LASTEXITCODE
                Output = (Convert-SimcoreNativeText $message)
            }
        }
        throw
    } finally {
        $ErrorActionPreference = $previous
        if ($hadNative) { $PSNativeCommandUseErrorActionPreference = $previousNative }
    }
}

function Invoke-ApacheConfigTest {
    param($Apache)
    if ($null -eq $Apache -or [string]::IsNullOrWhiteSpace([string]$Apache.Executable)) {
        throw "Apache executable was not found."
    }
    $result = Invoke-SimcoreNative -FilePath $Apache.Executable -ArgumentList @("-t", "-d", [string]$Apache.ServerRoot)
    if (-not (Test-ApacheConfigOk -ExitCode $result.ExitCode -Output $result.Output)) {
        throw "httpd -t failed, so Apache was not restarted.`n$($result.Output)"
    }
    if ($result.Output) { Write-Host $result.Output }
}

function Restart-ApacheServer {
    param($Apache)
    Write-Host "Restarting Apache so it binds localhost only"
    Invoke-SimcoreNative -FilePath $Apache.Executable -ArgumentList @("-k", "stop", "-d", [string]$Apache.ServerRoot) | Out-Null
    Start-Sleep -Seconds 2
    if ($Apache.ServiceName) {
        Set-Service -Name $Apache.ServiceName -StartupType Automatic
        $svc = Get-Service -Name $Apache.ServiceName
        if ($svc.Status -eq "Stopped") {
            Start-Service -Name $Apache.ServiceName
        } else {
            Restart-Service -Name $Apache.ServiceName -Force
        }
        return
    }
    Write-Warning "Apache is not installed as a Windows service. It was started in the background and will not come back on its own after a reboot. Start it from XAMPP, or install the service with httpd -k install, after you confirm the localhost ports."
    Assert-SimcoreInstallerArguments -FilePath $Apache.Executable -ArgumentList @("-d", $Apache.ServerRoot)
    Start-Process -FilePath $Apache.Executable -ArgumentList @("-d", $Apache.ServerRoot) -WindowStyle Hidden | Out-Null
}

function Assert-ApacheLoopbackPorts {
    param([int]$HttpPort = 8080, [int]$HttpsPort = 8443)
    $httpOk = $false
    $httpsOk = $false
    for ($try = 1; $try -le 15; $try++) {
        $http = @(Get-NetTCPConnection -LocalPort $HttpPort -State Listen -ErrorAction SilentlyContinue)
        $https = @(Get-NetTCPConnection -LocalPort $HttpsPort -State Listen -ErrorAction SilentlyContinue)
        $httpOk = $false
        $httpsOk = $false
        foreach ($conn in $http) {
            if ([string]$conn.LocalAddress -in @("127.0.0.1", "::1")) { $httpOk = $true }
            elseif ($conn.OwningProcess) {
                $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
                if ($proc -and $proc.ProcessName -match '^(?i)httpd$') {
                    throw "Apache HTTP is listening on $($conn.LocalAddress):$HttpPort. It must stay on 127.0.0.1."
                }
            }
        }
        foreach ($conn in $https) {
            if ([string]$conn.LocalAddress -in @("127.0.0.1", "::1")) { $httpsOk = $true }
            elseif ($conn.OwningProcess) {
                $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
                if ($proc -and $proc.ProcessName -match '^(?i)httpd$') {
                    throw "Apache HTTPS is listening on $($conn.LocalAddress):$HttpsPort. It must stay on 127.0.0.1."
                }
            }
        }
        if ($httpOk -and $httpsOk) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $httpOk -or -not $httpsOk) {
        throw "Apache did not listen on 127.0.0.1:$HttpPort and 127.0.0.1:$HttpsPort."
    }
    foreach ($port in @(80, 443)) {
        $conns = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
        foreach ($conn in $conns) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -match '^(?i)httpd$') {
                throw "httpd is still listening on port $port."
            }
        }
    }
    Write-Host "Apache is listening on 127.0.0.1:$HttpPort and 127.0.0.1:$HttpsPort only."
}

function Disable-CompetingAcmeClients {
    $patternTasks = @()
    $tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue)
    foreach ($task in $tasks) {
        if ($null -eq $task) { continue }
        $label = "$($task.TaskPath)$($task.TaskName)"
        if (-not (Test-CompetingAcmeName $label)) { continue }
        if ([string]$task.State -eq "Disabled") { continue }
        Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath | Out-Null
        Write-Host "Disabled scheduled task $label"
        $patternTasks += [pscustomobject]@{ Name = [string]$task.TaskName; Path = [string]$task.TaskPath }
    }
    $serviceHits = @()
    $services = @(Get-Service -ErrorAction SilentlyContinue)
    foreach ($svc in $services) {
        if ($null -eq $svc) { continue }
        $label = "$($svc.Name) $($svc.DisplayName)"
        if (-not (Test-CompetingAcmeName $label)) { continue }
        if ($svc.Name -match '^(?i)simcore-') { continue }
        $previous = [string]$svc.StartType
        if ($svc.Status -ne "Stopped") {
            Stop-Service -Name $svc.Name -Force -ErrorAction SilentlyContinue
        }
        if ($previous -eq "Automatic") {
            Set-Service -Name $svc.Name -StartupType Manual
        }
        Write-Host "Stopped ACME service $($svc.Name) (was $previous)"
        $serviceHits += [pscustomobject]@{ Name = [string]$svc.Name; PreviousStartType = $previous }
    }
    return [pscustomobject]@{ Tasks = $patternTasks; Services = $serviceHits }
}

function Enable-CompetingAcmeClients {
    param($State)
    foreach ($task in @(ConvertTo-CoexistList $State.disabledTasks)) {
        if ($null -eq $task -or -not $task.Name) { continue }
        try {
            Enable-ScheduledTask -TaskName $task.Name -TaskPath $task.Path | Out-Null
            Write-Host "Re-enabled scheduled task $($task.Path)$($task.Name)"
        } catch {
            Write-Warning "Could not re-enable scheduled task $($task.Name): $_"
        }
    }
    foreach ($svc in @(ConvertTo-CoexistList $State.disabledServices)) {
        if ($null -eq $svc -or -not $svc.Name) { continue }
        $mode = [string]$svc.PreviousStartType
        if ($mode -notin @("Automatic", "Manual", "Disabled")) { $mode = "Manual" }
        try {
            Set-Service -Name $svc.Name -StartupType $mode
            if ($mode -eq "Automatic") { Start-Service -Name $svc.Name }
            Write-Host "Restored service $($svc.Name) to $mode"
        } catch {
            Write-Warning "Could not restore service $($svc.Name): $_"
        }
    }
}

function Wait-SimcoreCaddyPort {
    param([string]$RollbackCommand)
    for ($try = 1; $try -le 20; $try++) {
        $owned = $false
        foreach ($owner in @(Get-WebPortOwners)) {
            if ($owner.Port -eq 443 -and (Test-SimcoreWebProcess -ProcessName $owner.ProcessName -Path $owner.Path)) {
                $owned = $true
            }
        }
        if ($owned) {
            Write-Host "Caddy is listening on port 443."
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Caddy did not bind port 443. See C:\simcore\logs. Rollback: $RollbackCommand"
}

function Get-SimcoreRollbackCommand {
    param([string]$ScriptRoot)
    return "powershell -ExecutionPolicy Bypass -File `"$(Join-Path $ScriptRoot 'rollback-apache.ps1')`""
}

function Assert-CaddyIpCertificateSupport {
    param([string]$CaddyPath)
    $output = & $CaddyPath version 2>&1 | Out-String
    if ($output -notmatch 'v(\d+)\.(\d+)\.(\d+)') {
        throw "Could not read the Caddy version from: $output"
    }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    $patch = [int]$Matches[3]
    $supportsIp = ($major -gt 2) -or ($major -eq 2 -and $minor -gt 10) -or ($major -eq 2 -and $minor -eq 10 -and $patch -ge 1)
    if (-not $supportsIp) {
        throw "Caddy $output is too old for a Let's Encrypt IP certificate. bootstrap installs Caddy 2.11.7, which supports the shortlived profile. Replace C:\simcore\tools\caddy.exe with that build and run this script again."
    }
    Write-Host ("Caddy {0}.{1}.{2} can obtain a short-lived Let's Encrypt IP certificate." -f $major, $minor, $patch)
}
