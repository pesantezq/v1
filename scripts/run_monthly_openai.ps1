param(
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [string]$OpenAIApiKey = $env:OPENAI_API_KEY,
    [string]$OpenAIModel = "gpt-4o-mini",
    [string]$OpenAIBaseUrl = $(if ($env:OPENAI_BASE_URL) { $env:OPENAI_BASE_URL } else { "https://api.openai.com/v1" }),
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

    if (-not $OpenAIApiKey) {
        Write-Error "OPENAI_API_KEY is required for monthly OpenAI runs."
        exit 1
    }

    $env:OPENAI_API_KEY = $OpenAIApiKey
    $env:OPENAI_MODEL = $OpenAIModel
    $env:OPENAI_BASE_URL = $OpenAIBaseUrl

    Invoke-Step "OpenAI preflight" { & $Python -m tools.llm_smoke_test --provider openai }
    Invoke-Step "Monthly main workflow" { & $Python main.py --run-mode monthly --llm-provider openai }
    Invoke-Step "Monthly agent workflow" { & $Python -m agent --mode monthly --provider openai }
}
finally {
    Pop-Location
}
