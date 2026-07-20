# 02_pull_models.ps1
# Phase-0: configure Lemonade for hybrid execution and pull the models
# DSA_Agent_III uses, from Hugging Face via Lemonade.
#
#   - iGPU coder:  Qwen3-Coder-30B-A3B-Instruct-GGUF (llamacpp)          [Gate A]
#   - NPU hybrid:  a RyzenAI *Hybrid* small model for routing/planning   [Gate B]
#   - TTS:         kokoro-v1 (small/fast) for /speak                     [Gate C]
#
# Uses the `lemonade` CLI at %LOCALAPPDATA%\lemonade_server\bin. Model names can
# drift between builds -- if a pull 404s, run `lemonade list` and adjust below.

$ErrorActionPreference = "Continue"
$lem = "$env:LOCALAPPDATA\lemonade_server\bin\lemonade.exe"
if (-not (Test-Path $lem)) { $lem = "lemonade-server" }  # fall back to PATH

# --- Server config required for the iGPU+NPU hybrid --------------------------
# max_loaded_models >= 2 lets the 30B coder (iGPU) and the NPU router stay
# resident together; without it, loading one evicts the other. backend=auto
# because forcing rocm makes the 30B load 500 on this gfx1151 build.
Write-Host "=== Configure Lemonade (hybrid concurrency + backend) ===" -ForegroundColor Cyan
& $lem config set max_loaded_models=2
& $lem config set llamacpp.backend=auto

$Coder = $env:AGENT_MODEL
if (-not $Coder) { $Coder = "Qwen3-Coder-30B-A3B-Instruct-GGUF" }

# Candidate NPU hybrid names to try (first that succeeds wins). Qwen3-1.7B-Hybrid
# is the validated default: small, fast on the NPU, ideal for classification.
$HybridCandidates = @("Qwen3-1.7B-Hybrid", "Qwen3-4B-Hybrid", "Llama-3.2-3B-Instruct-Hybrid")

Write-Host "`n=== Pull iGPU coder model (~18.6 GB) ===" -ForegroundColor Cyan
& $lem pull $Coder

Write-Host "`n=== Pull NPU hybrid model (routing/planning) ===" -ForegroundColor Cyan
$hybridPulled = $null
foreach ($m in $HybridCandidates) {
    Write-Host "Trying: pull $m" -ForegroundColor Gray
    & $lem pull $m
    if ($LASTEXITCODE -eq 0) { $hybridPulled = $m; break }
}

Write-Host "`n=== Pull TTS model for /speak (kokoro-v1, ~0.35 GB) ===" -ForegroundColor Cyan
& $lem pull kokoro-v1

Write-Host "`n=== Downloaded models ===" -ForegroundColor Cyan
& $lem list --downloaded

Write-Host ""
if ($hybridPulled) {
    Write-Host "NPU hybrid model pulled: $hybridPulled" -ForegroundColor Green
    Write-Host "Set in .env:  ROUTER_MODEL=$hybridPulled  (and PLANNER_MODEL=$hybridPulled)" -ForegroundColor Yellow
} else {
    Write-Host "No hybrid model pulled. Run '$lem list', pick a *Hybrid* model, pull it," -ForegroundColor Yellow
    Write-Host "and set ROUTER_MODEL in .env. The app still runs iGPU-only until then." -ForegroundColor Gray
}
Write-Host "TTS: set TTS_MODEL=kokoro-v1 and ENABLE_TTS=1 in .env." -ForegroundColor Yellow
Write-Host "`nNext: python infra\03_verify_toolcall.py   (Gate A -- must pass 3/3)" -ForegroundColor Cyan
