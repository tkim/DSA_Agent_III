#Requires -Version 5.1
Write-Host "=== Ryzen AI MAX+ 395 (Strix Halo) - Windows Hardware Check ===" -ForegroundColor Cyan

Write-Host "`n--- CPU ---"
(Get-WmiObject Win32_Processor).Name

Write-Host "`n--- GPU ---"
Get-WmiObject Win32_VideoController |
    Select-Object Name, AdapterRAM, DriverVersion |
    Format-Table -AutoSize

Write-Host "`n--- Total RAM ---"
$gb = (Get-WmiObject Win32_ComputerSystem).TotalPhysicalMemory / 1GB
"Total physical memory: {0:N1} GB" -f $gb

Write-Host "`n--- GPU VRAM (VGM) ---"
$vram = (Get-WmiObject Win32_VideoController |
    Where-Object { $_.Name -match "AMD|Radeon" }).AdapterRAM
if ($vram) {
    "Reported GPU VRAM: {0:N1} GB (target: ~96 GB after Adrenalin VGM setup)" -f ($vram / 1GB)
} else {
    "Cannot read GPU VRAM via WMI - check AMD Adrenalin app directly"
}

Write-Host "`n--- NPU (XDNA2) ---"
$npu = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "NPU|AI Engine|IPU|XDNA" }
if ($npu) {
    $npu | Select-Object -First 1 -ExpandProperty Name
    Write-Host "NPU present - Lemonade RyzenAI hybrid models can target it." -ForegroundColor Green
} else {
    Write-Host "NPU not detected via PnP (may still work). Ensure Ryzen AI driver is installed." -ForegroundColor Yellow
}

Write-Host "`n--- AMD Driver ---"
$drv = (Get-WmiObject Win32_VideoController |
    Where-Object { $_.Name -match "AMD|Radeon" }).DriverVersion
"Driver: $drv  (need Adrenalin 25.8.1 WHQL or later for gfx1151)"

Write-Host "`n--- Lemonade server (:13305) ---"
try {
    $r = Invoke-WebRequest -Uri "http://localhost:13305/v1/models" -TimeoutSec 2 -UseBasicParsing
    Write-Host "Lemonade reachable (HTTP $($r.StatusCode)) - OK" -ForegroundColor Green
} catch {
    if ($_.Exception.Response) {
        Write-Host "Lemonade responded (non-200) - server is up" -ForegroundColor Green
    } else {
        Write-Host "Lemonade NOT reachable on :13305 - run 01_install_lemonade.ps1" -ForegroundColor Red
    }
}

Write-Host "`nNote: DSA_Agent_III uses Lemonade only (no Ollama). A separate Ollama install"
Write-Host "on :11434 (e.g. DSA_Agent_II) is unaffected and can run alongside this." -ForegroundColor Gray
