# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
# Installs the Cortex hub on Windows (Docker Desktop) in one go. Run in an administrator PowerShell:
#   powershell -ExecutionPolicy Bypass -File install.ps1          # -NoLan: this machine only
# It writes .env when there is none, creates %USERPROFILE%\Vaults, starts the container, routes cortex.local (and
# <machine>.cortex.home) to the hub's DNS with NRPT rules and trusts the hub's CA in the machine's root store.
# After that, connecting an agent is the hub's form alone: https://cortex.local
param([switch]$NoLan, [switch]$Remove)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Get-EnvValue($k) { if (Test-Path .env) { (Select-String -Path .env -Pattern "^$k=(.*)$" | Select-Object -Last 1).Matches.Groups[1].Value } }
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators')) {
  throw 'Run it in an administrator PowerShell: DNS rules and the root store need it.' }
if ($Remove) {
  Get-DnsClientNrptRule | Where-Object { $_.Comment -eq 'Cortex hub' } | ForEach-Object { Remove-DnsClientNrptRule -Name $_.Name -Force }
  Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like '*Cortex hub local CA*' } | Remove-Item
  'removed'; return }
foreach ($c in 'docker', 'python', 'git') { if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { throw "missing: $c" } }

if (-not (Test-Path .env)) {
  $name = (git config --global user.name); $email = (git config --global user.email)
  $text = (Get-Content .env.example -Raw) -replace '(?m)^CORTEX_OWNER=.*$', "CORTEX_OWNER=$(if ($name) { $name } else { 'the owner' })" `
    -replace '(?m)^CORTEX_GIT_NAME=.*$', "CORTEX_GIT_NAME=$name" -replace '(?m)^CORTEX_GIT_EMAIL=.*$', "CORTEX_GIT_EMAIL=$email"
  $addr = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -match '^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)' -and $_.PrefixOrigin -ne 'WellKnown' } |
    Sort-Object InterfaceMetric | Select-Object -First 1).IPAddress
  if (-not $NoLan -and $addr) {
    $hostName = $env:COMPUTERNAME.ToLower() -replace '[^a-z0-9-]', '-'
    $text += "`n# LAN listener, found by install.ps1`nCORTEX_LAN_ADDRESS=$addr`nCORTEX_LAN_HOST=$hostName`nCOMPOSE_PATH_SEPARATOR=;`nCOMPOSE_FILE=compose.yaml;compose.lan.yaml`n"
    "LAN: https://<agent>.$hostName.cortex.home on $addr (only the cortexes you mark LAN)" }
  Set-Content -Path .env -Value $text -NoNewline
  'wrote .env' }

$vaults = Get-EnvValue CORTEX_VAULTS; if (-not $vaults) { $vaults = Join-Path $env:USERPROFILE 'Vaults' }
New-Item -ItemType Directory -Force -Path $vaults | Out-Null
docker compose up -d --build; if ($LASTEXITCODE) { throw 'docker compose failed' }
$ca = Join-Path $vaults '.cortex-hub\tls\ca.crt'
for ($i = 0; $i -lt 60 -and -not (Test-Path $ca); $i++) { Start-Sleep 1 }
if (-not (Test-Path $ca)) { throw 'the hub did not start: docker compose logs' }

$domain = Get-EnvValue CORTEX_DOMAIN; if (-not $domain) { $domain = 'cortex.local' }
$dns = Get-EnvValue CORTEX_DNS_ADDRESS; if (-not $dns) { $dns = '127.0.0.153' }
$zones = @($domain); $lanHost = Get-EnvValue CORTEX_LAN_HOST
if ($lanHost) { $lanDomain = Get-EnvValue CORTEX_LAN_DOMAIN; if (-not $lanDomain) { $lanDomain = 'cortex.home' }; $zones += "$lanHost.$lanDomain" }
Get-DnsClientNrptRule | Where-Object { $_.Comment -eq 'Cortex hub' } | ForEach-Object { Remove-DnsClientNrptRule -Name $_.Name -Force }
foreach ($z in $zones) { Add-DnsClientNrptRule -Namespace ".$z", $z -NameServers $dns -Comment 'Cortex hub' }
Clear-DnsClientCache
Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like '*Cortex hub local CA*' } | Remove-Item
Import-Certificate -FilePath $ca -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
"Ready: https://$domain - fill in 'Connect an agent' and paste the prompt to your agent."
