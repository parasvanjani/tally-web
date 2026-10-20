<#
  Tally Web Gateway — one-time setup on the Windows PC (or VM) that runs TallyPrime.
  Run in an elevated PowerShell:  powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1

  What it does:
    1. Creates a Python virtual environment and installs the gateway.
    2. Blocks inbound network access to Tally's XML port (9000) — loopback still works.
    3. Registers a scheduled task that starts the gateway at boot and restarts it on failure.
  Cloudflare Tunnel is installed separately (see README, step 4).
#>
param(
  [string]$InstallDir = "C:\TallyWeb",
  [int]$TallyPort = 9000
)
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $PSScriptRoot

Write-Host "Copying gateway to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Recurse -Force "$src\gateway", "$src\web", "$src\scripts", "$src\requirements.txt" $InstallDir
if (-not (Test-Path "$InstallDir\.env")) {
  Copy-Item "$src\.env.example" "$InstallDir\.env"
  Write-Warning "Edit $InstallDir\.env before starting (company name, Cloudflare AUD, users)."
}
New-Item -ItemType Directory -Force -Path "$InstallDir\logs" | Out-Null

# Lock the folder down to Administrators + SYSTEM (it holds .env and the audit log).
icacls $InstallDir /inheritance:r /grant:r "Administrators:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null

Write-Host "Creating virtual environment"
if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv "$InstallDir\.venv" }
else { python -m venv "$InstallDir\.venv" }
& "$InstallDir\.venv\Scripts\python.exe" -m pip install --upgrade pip -q
& "$InstallDir\.venv\Scripts\python.exe" -m pip install -r "$InstallDir\requirements.txt" -q

Write-Host "Blocking inbound TCP $TallyPort (Tally XML) from the network"
Get-NetFirewallRule -DisplayName "Block Tally XML port" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "Block Tally XML port" -Direction Inbound -Protocol TCP `
  -LocalPort $TallyPort -Action Block -Profile Any | Out-Null
# Windows Firewall does not filter loopback, so the gateway on 127.0.0.1 still reaches Tally.

Write-Host "Registering startup task"
$action  = New-ScheduledTaskAction -Execute "$InstallDir\.venv\Scripts\python.exe" `
             -Argument "-m gateway" -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
             -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName "TallyWebGateway" -Action $action -Trigger $trigger -Settings $settings `
  -User "SYSTEM" -RunLevel Highest -Force | Out-Null

Write-Host ""
Write-Host "Done. Start it now with:  Start-ScheduledTask -TaskName TallyWebGateway"
Write-Host "Check locally:            Invoke-RestMethod http://127.0.0.1:8787/healthz"
