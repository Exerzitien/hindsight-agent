param(
    [switch]$SkipDocker,
    [switch]$SkipModels,
    [int]$BootstrapLimit = 200
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPath = Join-Path $ProjectRoot ".venv"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"

Set-Location $ProjectRoot

if (-not (Test-Path -LiteralPath $PythonPath)) {
    python -m venv $VenvPath
}

& $PythonPath -m pip install --index-url https://pypi.org/simple --upgrade pip setuptools
& $PythonPath -m pip install --index-url https://pypi.org/simple -e ".[dev,observability]"

if (-not $SkipDocker) {
    docker compose up -d
    docker compose ps
}

if (-not $SkipModels) {
    ollama pull nomic-embed-text-v2-moe
    ollama pull qwen3:4b-instruct-2507-q4_K_M
}

& (Join-Path $VenvPath "Scripts\hindsight-factory-create-zh.exe")
& $PythonPath -m hindsight_agent.daily_pipeline --backfill-limit $BootstrapLimit

Write-Host ""
Write-Host "Hindsight local environment is ready."
Write-Host "Business console: hindsight-dashboard"
Write-Host "Agent traces: http://localhost:16686"
Write-Host "Milvus: http://localhost:19531"
Write-Host "Milvus visual console: http://localhost:8001"
