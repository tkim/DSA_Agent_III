# start_server.ps1
# Idempotently ensure the Lemonade server is up on :13305.
#
# IMPORTANT: Lemonade installs a tray/autostart that launches the server as
# `LemonadeServer.exe --silent`. Do NOT launch a second copy by hand -- two
# instances fight over port 13305 and one dies, which shows up as a transient
# "connection refused" (WinError 10061). This script only starts the server if
# nothing is already listening, so it is safe to run anytime.

$ErrorActionPreference = "Stop"
$Port = 13305
$Bin  = "$env:LOCALAPPDATA\lemonade_server\bin\LemonadeServer.exe"

function Test-Listening { [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) }

if (Test-Listening) {
    Write-Host "Lemonade already listening on :$Port -- nothing to do." -ForegroundColor Green
    return
}

if (-not (Test-Path $Bin)) {
    Write-Host "LemonadeServer.exe not found at $Bin" -ForegroundColor Red
    Write-Host "Install first: .\infra\01_install_lemonade.ps1" -ForegroundColor Yellow
    exit 1
}

Write-Host "No server on :$Port -- starting one instance ($Bin --silent) ..." -ForegroundColor Cyan
Start-Process -FilePath $Bin -ArgumentList "--silent" -WindowStyle Hidden

for ($i = 0; $i -lt 30; $i++) {
    if (Test-Listening) {
        $listenPid = (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue).OwningProcess | Select-Object -First 1
        Write-Host "Lemonade is up on :$Port (pid $listenPid)." -ForegroundColor Green
        return
    }
    Start-Sleep -Seconds 1
}
Write-Host "Server did not come up within 30s. Check the tray app, or run the binary manually:" -ForegroundColor Yellow
Write-Host "  $Bin --silent" -ForegroundColor White
exit 1
