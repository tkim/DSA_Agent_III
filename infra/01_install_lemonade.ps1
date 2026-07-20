# 01_install_lemonade.ps1
# Phase-0: install AMD Lemonade Server 11.0 on Windows 11.
#
# Lemonade is the ONLY runtime for DSA_Agent_III. It installs as its own service
# on port 13305 and does NOT touch any existing Ollama install (DSA_Agent_II keeps
# working). Do NOT reconfigure Lemonade to bind Ollama's port 11434.
#
# Official Windows install is the MSI (there is no winget package). This script
# downloads the latest lemonade.msi and launches the installer, then verifies the
# server answers on :13305.

$ErrorActionPreference = "Stop"

Write-Host "=== Lemonade Server 11.0 install (Windows MSI) ===" -ForegroundColor Cyan

$MsiUrl = "https://github.com/lemonade-sdk/lemonade/releases/latest/download/lemonade.msi"
$MsiPath = Join-Path $env:TEMP "lemonade.msi"

Write-Host "Downloading installer from:" -ForegroundColor Gray
Write-Host "  $MsiUrl"
try {
    Invoke-WebRequest -Uri $MsiUrl -OutFile $MsiPath -UseBasicParsing
    Write-Host "Saved to $MsiPath" -ForegroundColor Green
} catch {
    Write-Host "Download failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Download it manually from https://lemonade-server.ai/ (Windows / MSI) and run it." -ForegroundColor Yellow
    exit 1
}

Write-Host "`nLaunching the MSI installer (accept the UAC / installer prompts)..." -ForegroundColor Cyan
# Interactive install so you can see the prompts; the installer adds
# `lemonade-server` to PATH and registers the service.
Start-Process msiexec.exe -ArgumentList "/i `"$MsiPath`"" -Wait

Write-Host "`nThe installer registers a tray autostart that runs the server as" -ForegroundColor Gray
Write-Host "'LemonadeServer.exe --silent'. Do NOT start a second copy by hand -- two" -ForegroundColor Gray
Write-Host "instances fight over :13305 and one dies (transient 'connection refused')." -ForegroundColor Gray
Write-Host "If it is ever down, bring it up idempotently with:" -ForegroundColor Gray
Write-Host "  .\infra\start_server.ps1" -ForegroundColor White

# Verify the server answers on :13305 (v11 default; changed from 8000 in v10.1).
Write-Host "`nVerifying Lemonade server on :13305 ..." -ForegroundColor Cyan
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:13305/v1/models" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -ge 200) { $ok = $true; break }
    } catch {
        # A 4xx still means the server is answering.
        if ($_.Exception.Response) { $ok = $true; break }
    }
    Start-Sleep -Milliseconds 500
}

if ($ok) {
    Write-Host "Lemonade server is reachable on http://localhost:13305/v1" -ForegroundColor Green
    Write-Host "Next: .\infra\02_pull_models.ps1" -ForegroundColor Cyan
} else {
    Write-Host "Server not reachable yet -- that's expected if you still need to open a" -ForegroundColor Yellow
    Write-Host "new terminal and run 'lemonade-server serve'. Re-run this script or just" -ForegroundColor Yellow
    Write-Host "check: netstat -ano | findstr :13305" -ForegroundColor Yellow
    exit 1
}
