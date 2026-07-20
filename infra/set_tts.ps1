# set_tts.ps1 -- swap the TTS voice model used by /speak, in one command.
#
#   .\infra\set_tts.ps1 kokoro-v1        # small/fast, default (0.35 GB)
#   .\infra\set_tts.ps1 MOSS-VoiceGen    # voice cloning/design (7.3 GB)
#   .\infra\set_tts.ps1 OpenMOSS-TTS     # full OpenMOSS TTS (12.5 GB)
#
# Pulls the model if needed, then rewrites TTS_MODEL (and ENABLE_TTS=1) in .env.
# Takes effect the next time you launch cli.py. Run `lemonade list` to see all
# TTS-capable models (recipe: kokoro / openmoss).

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Model,
    [string]$Voice = ""   # optional; kokoro has named voices e.g. af_bella, am_adam
)

$ErrorActionPreference = "Stop"
$lem = "$env:LOCALAPPDATA\lemonade_server\bin\lemonade.exe"
if (-not (Test-Path $lem)) { $lem = "lemonade-server" }

$repo = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repo ".env"
if (-not (Test-Path $envFile)) {
    Write-Host ".env not found at $envFile -- copy .env.example to .env first." -ForegroundColor Red
    exit 1
}

# --- Pull the model if it is not already downloaded --------------------------
$downloaded = (& $lem list --downloaded 2>&1 | Out-String)
if ($downloaded -match [regex]::Escape($Model)) {
    Write-Host "$Model already downloaded." -ForegroundColor Green
} else {
    Write-Host "Pulling $Model ..." -ForegroundColor Cyan
    & $lem pull $Model
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Pull failed. Check the exact name with: $lem list" -ForegroundColor Red
        exit 1
    }
}

# --- Rewrite TTS_MODEL / TTS_VOICE / ENABLE_TTS in .env ----------------------
function Set-EnvKey([string]$content, [string]$key, [string]$value) {
    if ($content -match "(?m)^\s*$key\s*=.*$") {
        return [regex]::Replace($content, "(?m)^\s*$key\s*=.*$", "$key=$value")
    }
    return $content.TrimEnd() + "`n$key=$value`n"
}

$content = Get-Content $envFile -Raw
$content = Set-EnvKey $content "TTS_MODEL" $Model
$content = Set-EnvKey $content "ENABLE_TTS" "1"
$content = Set-EnvKey $content "TTS_VOICE" $Voice
Set-Content -Path $envFile -Value $content -Encoding UTF8 -NoNewline

Write-Host "`n.env updated:" -ForegroundColor Green
Write-Host "  TTS_MODEL=$Model"
Write-Host "  ENABLE_TTS=1"
if ($Voice) { Write-Host "  TTS_VOICE=$Voice" }
Write-Host "`nRestart cli.py, then use /speak. First /speak loads the new voice model." -ForegroundColor Cyan
