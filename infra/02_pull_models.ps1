# 02_pull_models.ps1
# Phase-0: pull the models DSA_Agent_III uses, from Hugging Face via Lemonade.
#
#   - iGPU coder:  Qwen3-Coder-30B-A3B-Instruct-GGUF (llamacpp, ROCm)  [Gate A]
#   - NPU hybrid:  a RyzenAI *Hybrid* small model for routing/planning  [Gate B]
#
# Uses the `lemonade-server` CLI. Model/registry names can drift between Lemonade
# builds -- if a pull 404s, run `lemonade-server list` and adjust the names below
# (and set ROUTER_MODEL in .env to whatever hybrid model you land on).

$ErrorActionPreference = "Continue"

$Coder = $env:AGENT_MODEL
if (-not $Coder) { $Coder = "Qwen3-Coder-30B-A3B-Instruct-GGUF" }

# Candidate NPU hybrid names to try (first that succeeds wins).
$HybridCandidates = @("Qwen3-8B-Hybrid", "Qwen3-4B-Hybrid", "Llama-3.2-3B-Instruct-Hybrid")

Write-Host "=== Pull iGPU coder model ===" -ForegroundColor Cyan
Write-Host "lemonade-server pull $Coder"
lemonade-server pull $Coder

Write-Host "`n=== Pull NPU hybrid model (routing/planning) ===" -ForegroundColor Cyan
$hybridPulled = $null
foreach ($m in $HybridCandidates) {
    Write-Host "Trying: lemonade-server pull $m" -ForegroundColor Gray
    lemonade-server pull $m
    if ($LASTEXITCODE -eq 0) { $hybridPulled = $m; break }
}

Write-Host "`n=== Available models ===" -ForegroundColor Cyan
lemonade-server list

Write-Host ""
if ($hybridPulled) {
    Write-Host "NPU hybrid model pulled: $hybridPulled" -ForegroundColor Green
    Write-Host "Set this in .env:  ROUTER_MODEL=$hybridPulled" -ForegroundColor Yellow
} else {
    Write-Host "No hybrid model from the candidate list pulled. Run 'lemonade-server list'," -ForegroundColor Yellow
    Write-Host "pick a *Hybrid* model, pull it, and set ROUTER_MODEL in .env." -ForegroundColor Yellow
    Write-Host "The app still runs iGPU-only until then (ROUTER_MODEL falls back to AGENT_MODEL)." -ForegroundColor Gray
}

Write-Host "`nNext: python infra\03_verify_toolcall.py   (Gate A -- must pass 3/3)" -ForegroundColor Cyan
