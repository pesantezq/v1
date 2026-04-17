param(
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [string]$OllamaBaseUrl = "http://localhost:11434/v1",
    [string]$OllamaModel = "gemma3:4b",
    [string]$OllamaApiKey = "ollama",
    [string]$Python = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Step {
    param(
        [string]$Label,
        [scriptblock]$Action
    )
    Write-Host "[RUN] $Label" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        Write-Error "$Label failed with exit code $LASTEXITCODE"
        exit $LASTEXITCODE
    }
}

Push-Location $Root
try {
    Remove-Item Env:\STOCKBOT_LLM_PROVIDER -ErrorAction SilentlyContinue

    $env:OLLAMA_BASE_URL = $OllamaBaseUrl
    $env:OLLAMA_MODEL = $OllamaModel
    $env:OLLAMA_API_KEY = $OllamaApiKey

    Invoke-Step "Ollama smoke test" { & $Python -m tools.llm_smoke_test --provider ollama }
    Invoke-Step "Daily main workflow" { & $Python main.py --run-mode daily --llm-provider ollama }
    Invoke-Step "Daily agent workflow" { & $Python -m agent --mode daily --provider ollama }
}
finally {
    Pop-Location
}
