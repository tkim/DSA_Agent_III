# 01_install_lemonade.ps1
# Phase-0: install AMD Lemonade Server 11.0 on Windows 11.
#
# Lemonade is the ONLY runtime for DSA_Agent_III. It installs as its own service
# on port 13305 and does NOT touch any existing Ollama install (DSA_Agent_II keeps
# working). Do NOT reconfigure Lemonade to bind Ollama's port 11434.
#
# This script tries winget first, then points you at the official installer.

$ErrorActionPreference = "Stop"

Write-Host "=== Lemonade Server 11.0 install ===" -ForegroundColor Cyan

# 1. Try winget (id may vary by build; adjust if winget reports not found).
$installed = $false
try {
    winget install --id AMD.Lemonade -e --accept-package-agreements --accept-source-agreements
    if ($?) { $installed = $true }
} catch {
    Write-Host "winget path unavailable: $($_.Exception.Message)" -ForegroundColor Yellow
}

if (-not $installed) {
    Write-Host ""
    Write-Host "Automatic install not available. Download the Windows installer from:" -ForegroundColor Yellow
    Write-Host "  https://lemonade-server.ai/  (Get Started -> Windows installer)" -ForegroundColor Cyan
    Write-Host "  or GitHub releases: https://github.com/lemonade-sdk/lemonade/releases" -ForegroundColor Cyan
    Write-Host "The installer auto-detects hardware, pulls backends, and registers the service." -ForegroundColor Gray
    Write-Host ""
    Read-Host "Press Enter once Lemonade is installed to verify the server"
}

# 2. Verify the server answers on :13305.
Write-Host "`nVerifying Lemonade server on :13305 ..." -ForegroundColor Cyan
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:13305/v1/models" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -ge 200) { $ok = $true; break }
    } catch {
        # 4xx still means the server is up
        if ($_.Exception.Response) { $ok = $true; break }
    }
    Start-Sleep -Milliseconds 500
}

if ($ok) {
    Write-Host "Lemonade server is reachable on http://localhost:13305/v1" -ForegroundColor Green
    Write-Host "Next: .\infra\02_pull_models.ps1" -ForegroundColor Cyan
} else {
    Write-Host "Server not reachable yet. Start it via the Lemonade app / 'lemonade-server serve'." -ForegroundColor Red
    exit 1
}
