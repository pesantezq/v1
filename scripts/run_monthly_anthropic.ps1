param(
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [string]$AnthropicApiKey = $env:ANTHROPIC_API_KEY,
    [string]$AnthropicModel = "claude-haiku-4-5-20251001",
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

    if (-not $AnthropicApiKey) {
        Write-Error "ANTHROPIC_API_KEY is required for monthly Anthropic runs."
        exit 1
    }

    $env:ANTHROPIC_API_KEY = $AnthropicApiKey
    $env:ANTHROPIC_MODEL = $AnthropicModel

    Invoke-Step "Anthropic preflight" { & $Python -m tools.llm_smoke_test --provider anthropic }
    Invoke-Step "Monthly main workflow" { & $Python main.py --run-mode monthly --llm-provider anthropic }
    Invoke-Step "Monthly agent workflow" { & $Python -m agent --mode monthly --provider anthropic }
}
finally {
    Pop-Location
}
