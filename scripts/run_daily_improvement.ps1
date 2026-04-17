<#
.SYNOPSIS
    Daily improvement pipeline for stock_bot v1.

.DESCRIPTION
    Orchestrates the full daily improvement workflow:
      1. Refresh repo overview
      2. Build daily engineering packet
      3. Rank next improvement task
      4. Build Claude Code prompt
      5. Print next-step instructions

    Run this script at the start of a dev session to get context
    and a ready-to-use Claude prompt.

.PARAMETER Root
    Root of the repo (default: parent directory of this script).

.PARAMETER SkipOverview
    Skip the repo overview refresh (faster if overview is recent).

.PARAMETER SkipTests
    Skip running the test suite in the daily packet step.

.PARAMETER OpenPrompt
    Open the generated Claude prompt in Notepad after the run.

.PARAMETER ReviewMode
    Run in review mode: generate review_packet instead of the daily pipeline.
    Use this AFTER Claude has made changes.

.EXAMPLE
    .\run_daily_improvement.ps1
    .\run_daily_improvement.ps1 -SkipOverview -SkipTests
    .\run_daily_improvement.ps1 -ReviewMode
    .\run_daily_improvement.ps1 -OpenPrompt
#>

param(
    [string]$Root        = (Split-Path $PSScriptRoot -Parent),
    [switch]$SkipOverview,
    [switch]$SkipTests,
    [switch]$OpenPrompt,
    [switch]$ReviewMode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$date    = Get-Date -Format "yyyy-MM-dd"
$time    = Get-Date -Format "HH:mm"
$python  = "python"

# Colours
function Write-Step  { param($msg) Write-Host "  $msg" -ForegroundColor Cyan }
function Write-OK    { param($msg) Write-Host "  OK  $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "  WARN $msg" -ForegroundColor Yellow }
function Write-Fail  { param($msg) Write-Host "  FAIL $msg" -ForegroundColor Red }
function Write-Ruler { Write-Host ("=" * 64) -ForegroundColor DarkGray }

Write-Ruler
Write-Host "  Stock Bot v1 - Daily Improvement Pipeline" -ForegroundColor White
Write-Host "  Date: $date $time" -ForegroundColor DarkGray
Write-Host "  Root: $Root" -ForegroundColor DarkGray
Write-Ruler

Push-Location $Root

try {
    # -----------------------------------------------------------------------
    # REVIEW MODE
    # -----------------------------------------------------------------------
    if ($ReviewMode) {
        Write-Host ""
        Write-Host "  [REVIEW MODE] Generating review packet..." -ForegroundColor Magenta
        Write-Host ""

        Write-Step "Running tests + gathering diff..."
        & $python -m tools.review_packet --root $Root
        if ($LASTEXITCODE -ne 0) { Write-Warn "review_packet exited with code $LASTEXITCODE" }

        Write-Host ""
        Write-Ruler
        Write-Host "  Review packet ready:" -ForegroundColor Green
        Write-Host "    daily_update\review_packet.md" -ForegroundColor White
        Write-Host ""
        Write-Host "  DECISION CHECKLIST:" -ForegroundColor Yellow
        Write-Host "    [ ] Open  daily_update\review_packet.md"
        Write-Host "    [ ] Verify tests pass"
        Write-Host "    [ ] Verify diff looks correct"
        Write-Host "    [ ] Mark: ACCEPT / REVISE / REJECT"
        Write-Host "    [ ] If ACCEPT: git add + git commit"
        Write-Host "    [ ] If REJECT: git checkout -- ."
        Write-Ruler
        Pop-Location
        exit 0
    }

    # -----------------------------------------------------------------------
    # DAILY PIPELINE
    # -----------------------------------------------------------------------
    $step = 0

    # Step 1: Repo overview
    $step++
    Write-Host ""
    Write-Host "  [$step/4] Repo Overview" -ForegroundColor Yellow
    if ($SkipOverview) {
        Write-Warn "Skipped (--SkipOverview). Using cached overview."
    } else {
        Write-Step "Refreshing repo overview (AST scan)..."
        & $python -m tools.repo_overview --root $Root
        if ($LASTEXITCODE -eq 0) {
            Write-OK "repo_overview/ updated"
        } else {
            Write-Warn "repo_overview exited $LASTEXITCODE - continuing with cached version"
        }
    }

    # Step 2: Daily packet
    $step++
    Write-Host ""
    Write-Host "  [$step/4] Daily Packet" -ForegroundColor Yellow
    Write-Step "Gathering git status, logs, backlog..."
    if ($SkipTests) {
        & $python -m tools.daily_packet --root $Root --skip-tests
    } else {
        Write-Step "Running tests (may take ~30s)..."
        & $python -m tools.daily_packet --root $Root
    }
    if ($LASTEXITCODE -eq 0) {
        Write-OK "daily_update\daily_packet.md written"
    } else {
        Write-Warn "daily_packet exited $LASTEXITCODE"
    }

    # Step 3: Task ranker
    $step++
    Write-Host ""
    Write-Host "  [$step/4] Task Ranker" -ForegroundColor Yellow
    Write-Step "Scoring backlog items..."
    & $python -m tools.task_ranker --root $Root
    if ($LASTEXITCODE -eq 0) {
        Write-OK "daily_update\proposed_task.md written"
    } else {
        Write-Warn "task_ranker exited $LASTEXITCODE"
    }

    # Step 4: Prompt builder
    $step++
    Write-Host ""
    Write-Host "  [$step/4] Prompt Builder" -ForegroundColor Yellow
    Write-Step "Building Claude Code prompt..."
    & $python -m tools.build_prompt --root $Root
    if ($LASTEXITCODE -eq 0) {
        Write-OK "prompts\claude_today.txt written"
    } else {
        Write-Warn "build_prompt exited $LASTEXITCODE"
    }

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Ruler
    Write-Host "  PIPELINE COMPLETE" -ForegroundColor Green
    Write-Ruler
    Write-Host ""
    Write-Host "  Generated files:" -ForegroundColor White
    Write-Host "    daily_update\daily_packet.md    - today's repo health snapshot"
    Write-Host "    daily_update\proposed_task.md   - recommended next improvement"
    Write-Host "    prompts\claude_today.txt        - ready-to-use Claude prompt"
    Write-Host ""
    Write-Host "  WORKFLOW:" -ForegroundColor Yellow
    Write-Host "    1. Review  daily_update\daily_packet.md    (any surprises?)"
    Write-Host "    2. Review  daily_update\proposed_task.md   (agree with priority?)"
    Write-Host "    3. Edit    prompts\claude_today.txt        (optional tweaks)"
    Write-Host "    4. Paste prompt into Claude Code and run the task"
    Write-Host "    5. After Claude makes changes, run:"
    Write-Host "         .\scripts\run_daily_improvement.ps1 -ReviewMode"
    Write-Host "    6. Review  daily_update\review_packet.md   (accept / reject)"
    Write-Host ""

    # Optionally open the prompt
    if ($OpenPrompt) {
        $promptFile = Join-Path $Root "prompts\claude_today.txt"
        if (Test-Path $promptFile) {
            Write-Step "Opening prompt in Notepad..."
            Start-Process notepad $promptFile
        }
    }

    Write-Ruler

} finally {
    Pop-Location
}
