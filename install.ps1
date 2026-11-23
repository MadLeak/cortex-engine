# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
# Installs Cortex on Windows with Docker Desktop alone. A normal PowerShell, no administrator:
#   powershell -ExecutionPolicy Bypass -File install.ps1            # this machine: http://cortex.localhost:8790
#   ... install.ps1 -Lan        # also the local network, HTTPS by the hub's own CA (each device accepts it once)
#   ... install.ps1 -Trusted    # administrator: https://cortex.local with no warning, through NRPT DNS rules and
#                               # the hub's CA in the machine's root store (what this script did before)
#   ... install.ps1 -Remove     # administrator: takes the -Trusted DNS rules and CA off this machine
#   ... install.ps1 -MoveFrom OLD   # copies the vaults from where they were (a folder, or cortex-vaults for the Docker
#                               # volume) to where CORTEX_VAULTS in .env says now; the old copy is left as it was
# It writes .env when there is none, creates %USERPROFILE%\Vaults and starts the container. After that, an agent is
# connected from the hub's "Connect an agent" form, and .\cortex.ps1 is the command line (in the container too).
param([switch]$Lan, [switch]$Trusted, [switch]$Remove, [string]$MoveFrom)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Get-EnvValue($k) { if (Test-Path .env) { (Select-String -Path .env -Pattern "^$k=(.*)$" | Select-Object -Last 1).Matches.Groups[1].Value } }
function Test-Admin { ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators') }
if (($Trusted -or $Remove) -and -not (Test-Admin)) {
  throw 'Run -Trusted and -Remove in an administrator PowerShell: DNS rules and the root store need it.' }
if ($Remove) {
  Get-DnsClientNrptRule | Where-Object { $_.Comment -eq 'Cortex hub' } | ForEach-Object { Remove-DnsClientNrptRule -Name $_.Name -Force }
  Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like '*Cortex hub local CA*' } | Remove-Item
  'removed'; return }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'missing: Docker Desktop (docker)' }
docker compose version | Out-Null; if ($LASTEXITCODE) { throw 'missing: docker compose v2' }

if (-not (Test-Path .env)) {
  $name = ''; $email = ''
  if (Get-Command git -ErrorAction SilentlyContinue) { $name = (git config --global user.name); $email = (git config --global user.email) }
  $text = (Get-Content .env.example -Raw) -replace '(?m)^CORTEX_OWNER=.*$', "CORTEX_OWNER=$(if ($name) { $name } else { 'the owner' })" `
    -replace '(?m)^CORTEX_GIT_NAME=.*$', "CORTEX_GIT_NAME=$name" -replace '(?m)^CORTEX_GIT_EMAIL=.*$', "CORTEX_GIT_EMAIL=$email"
  $files = 'compose.yaml'
  if ($Lan) {
    $addr = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -match '^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)' -and $_.PrefixOrigin -ne 'WellKnown' } |
      Sort-Object InterfaceMetric | Select-Object -First 1).IPAddress
    if (-not $addr) { throw '-Lan: no private IPv4 address found on this machine' }
    $hostName = $env:COMPUTERNAME.ToLower() -replace '[^a-z0-9-]', '-'
    $text += "`n# LAN listener, found by install.ps1`nCORTEX_LAN_ADDRESS=$addr`nCORTEX_LAN_HOST=$hostName`n"
    $files += ';compose.lan.yaml'
    "LAN: https://$addr/v/<agent>/ and https://<agent>.$hostName.cortex.home (the cortexes you mark LAN)" }
  if ($Trusted) {
    $text += "`n# host-trusted mode, set up by install.ps1 -Trusted`nCORTEX_DOMAIN=cortex.local`nCORTEX_PORT=443`nCORTEX_HTTP_PORT=80`nCORTEX_DNS_ADDRESS=127.0.0.153`n"
    $files += ';compose.trusted.yaml' }
  if ($files -ne 'compose.yaml') { $text += "COMPOSE_PATH_SEPARATOR=;`nCOMPOSE_FILE=$files`n" }
  Set-Content -Path .env -Value $text -NoNewline
  'wrote .env' }
if ($Trusted -and -not (Select-String -Path .env -Pattern 'compose.trusted.yaml' -Quiet)) {
  throw '-Trusted: this .env has no compose.trusted.yaml in COMPOSE_FILE (see .env.example), or remove .env and run again' }

$vaults = Get-EnvValue CORTEX_VAULTS; if (-not $vaults) { $vaults = Join-Path $env:USERPROFILE 'Vaults' }
# CORTEX_VAULTS is a folder (a path) or the Docker volume cortex-vaults (no folder on this machine)
$project = if ($env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME } else { 'cortex' }
$uid = Get-EnvValue CORTEX_UID; $gid = Get-EnvValue CORTEX_GID
$owner = "$(if ($uid) { $uid } else { 1000 }):$(if ($gid) { $gid } else { 1000 })"
function Get-Mount($place) {   # what docker run mounts for a vaults place: the folder itself, or the volume's full name
  if ($place -match '[\\/]') { return $place }
  if ($place -eq 'cortex-vaults') { return "${project}_cortex-vaults" }
  throw "CORTEX_VAULTS must be a folder path or cortex-vaults, not: $place" }
function Test-Volume($name) {   # Continue: under Stop, Windows PowerShell 5.1 throws on a native command's stderr
  $ErrorActionPreference = 'Continue'; docker volume inspect $name 2>$null | Out-Null; return -not $LASTEXITCODE }
function Stop-Hub { $ErrorActionPreference = 'Continue'; docker compose stop hub 2>$null | Out-Null }
# the container runs as CORTEX_UID: the vaults place must be its, and a new Docker volume starts out as root's.
# The sh scripts carry no double quotes: Windows PowerShell 5.1 would mangle them on the way to docker.
function Invoke-InBox { docker run --rm --user 0:0 --entrypoint sh @args
  if ($LASTEXITCODE -eq 3) { Write-Host 'stopped: nothing was changed'; exit 3 }
  if ($LASTEXITCODE) { throw "docker run failed ($LASTEXITCODE)" } }
$folder = $vaults -match '[\\/]'
$target = Get-Mount $vaults
if ($folder) { New-Item -ItemType Directory -Force -Path $vaults | Out-Null }
else {
  if ($Trusted) { throw '-Trusted needs the vaults in a folder (CORTEX_VAULTS is a Docker volume): it installs the CA from there' }
  if (-not (Test-Volume $target)) {
    docker volume create --label "com.docker.compose.project=$project" --label com.docker.compose.volume=cortex-vaults $target | Out-Null } }
docker compose build hub; if ($LASTEXITCODE) { throw 'docker compose build failed' }
$image = docker compose config --images hub

if ($MoveFrom) {
  $source = Get-Mount $MoveFrom
  if ($source -eq $target) { throw "-MoveFrom: the vaults are already there ($vaults)" }
  if ($MoveFrom -match '[\\/]') { if (-not (Test-Path -PathType Container $MoveFrom)) { throw "-MoveFrom: no folder $MoveFrom" } }
  elseif (-not (Test-Volume $source)) { throw "-MoveFrom: no Docker volume $source" }
  $empty = 'if ls -A /vaults | grep -q .; then echo -MoveFrom: the new place is not empty, nothing was copied; exit 3; fi'
  Invoke-InBox -v "${target}:/vaults" $image -c $empty   # checked before the hub is stopped
  Stop-Hub   # nothing writes the old place while it is copied
  "copying the vaults from $MoveFrom to $vaults ..."
  Invoke-InBox -v "${source}:/from:ro" -v "${target}:/vaults" $image -c `
    "$empty; cp -a /from/. /vaults/ && chown -R `$1 /vaults" sh $owner
  "moved. The old copy in $MoveFrom was left as it was: remove it yourself once the hub shows your cortexes."
}
if (-not $folder) { Invoke-InBox -v "${target}:/vaults" $image -c 'chown $1 /vaults' sh $owner }
docker compose up -d; if ($LASTEXITCODE) { throw 'docker compose failed' }
for ($i = 0; $i -lt 60 -and -not (docker compose logs hub 2>$null | Select-String -SimpleMatch 'MCP at'); $i++) { Start-Sleep 1 }
if (-not (docker compose logs hub 2>$null | Select-String -SimpleMatch 'MCP at')) { throw 'the hub did not start: docker compose logs hub' }

if ($Trusted) {
  $ca = Join-Path $vaults '.cortex-hub\tls\ca.crt'
  for ($i = 0; $i -lt 30 -and -not (Test-Path $ca); $i++) { Start-Sleep 1 }
  if (-not (Test-Path $ca)) { throw 'the hub made no CA: docker compose logs hub' }
  $domain = Get-EnvValue CORTEX_DOMAIN; if (-not $domain) { $domain = 'cortex.local' }
  $dns = Get-EnvValue CORTEX_DNS_ADDRESS; if (-not $dns) { $dns = '127.0.0.153' }
  $zones = @($domain); $lanHost = Get-EnvValue CORTEX_LAN_HOST
  if ($lanHost) { $lanDomain = Get-EnvValue CORTEX_LAN_DOMAIN; if (-not $lanDomain) { $lanDomain = 'cortex.home' }; $zones += "$lanHost.$lanDomain" }
  Get-DnsClientNrptRule | Where-Object { $_.Comment -eq 'Cortex hub' } | ForEach-Object { Remove-DnsClientNrptRule -Name $_.Name -Force }
  foreach ($z in $zones) { Add-DnsClientNrptRule -Namespace ".$z", $z -NameServers $dns -Comment 'Cortex hub' }
  Clear-DnsClientCache
  Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like '*Cortex hub local CA*' } | Remove-Item
  Import-Certificate -FilePath $ca -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
  $url = "https://$domain"
} else {
  $domain = Get-EnvValue CORTEX_DOMAIN; if (-not $domain) { $domain = 'cortex.localhost' }
  $port = Get-EnvValue CORTEX_PORT; if (-not $port) { $port = '8790' }
  $url = "http://${domain}:$port"
}
"Ready: $url - 'Connect an agent' gives the line your agent's MCP client needs. Command line: .\cortex.ps1 list"
