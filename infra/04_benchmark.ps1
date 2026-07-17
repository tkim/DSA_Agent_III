# 04_benchmark.ps1
# Measure generation speed of a Lemonade model via the OpenAI /v1 API.
# Reports tokens/sec from usage.completion_tokens over wall-clock time.
#
#   .\infra\04_benchmark.ps1                       # benchmarks AGENT_MODEL (iGPU)
#   .\infra\04_benchmark.ps1 -Model <hybrid-name>  # benchmarks the NPU model

param(
    [string]$Model = $env:AGENT_MODEL,
    [int]$MaxTokens = 256
)

if (-not $Model) { $Model = "Qwen3-Coder-30B-A3B-Instruct-GGUF" }
$Base = $env:LEMONADE_BASE_URL
if (-not $Base) { $Base = "http://localhost:13305/v1" }
$Key = $env:LEMONADE_API_KEY
if (-not $Key) { $Key = "lemonade" }

$body = @{
    model      = $Model
    messages   = @(@{ role = "user"; content = "Write a haiku about local AI on AMD hardware." })
    max_tokens = $MaxTokens
    stream     = $false
} | ConvertTo-Json -Depth 6

Write-Host "Benchmarking $Model ($MaxTokens max tokens) ..." -ForegroundColor Cyan
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$resp = Invoke-RestMethod -Uri "$Base/chat/completions" -Method Post -Body $body `
    -ContentType "application/json" -Headers @{ Authorization = "Bearer $Key" } -TimeoutSec 180
$sw.Stop()

$secs = $sw.Elapsed.TotalSeconds
$out  = $resp.usage.completion_tokens
if (-not $out) { $out = $MaxTokens }
$tps = [math]::Round($out / $secs, 1)

Write-Host ""
Write-Host ("Model:            {0}" -f $Model)
Write-Host ("Completion tokens: {0}" -f $out)
Write-Host ("Wall time:         {0}s" -f ([math]::Round($secs, 2)))
Write-Host ("Throughput:        {0} tok/s" -f $tps) -ForegroundColor Green
Write-Host ""
Write-Host "Reference: DSA_Agent_II on Ollama ~48 tok/s (30B, ROCm). Lemonade ROCm ~38 / Vulkan ~46." -ForegroundColor Gray
