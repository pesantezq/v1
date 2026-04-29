# AI Agent Instructions

## Project Overview
- Advisory-only portfolio system
- No trade execution
- Decision Engine produces `outputs/latest/decision_plan.json`

## Critical Rules
- `decision_plan.json` is the source of truth
- GUI and memo must not recompute decisions
- AI must not change scores, ranks, or decisions
- All features must be additive
- System is observe-only

## Development Rules

DO:
- read artifacts
- write new artifacts
- add tests
- follow compact output contracts

DO NOT:
- modify `decision_engine.py` logic
- introduce trade execution
- duplicate scoring logic in GUI/memo
- mutate existing output schemas

## Output Contracts
- Decision: `outputs/latest/decision_plan.json`
- Memo: compact brief (max 5 decisions, 3 risks, 3 changes)
- GUI: compact summary + full detail below

## Commands
Run tests:
`pytest -q`

Compile:
`python -m py_compile <files>`

## Docs
See:
- `docs/ARCHITECTURE.md`
- `docs/decision_engine.md`
- `docs/gui_decision_center.md`
- `docs/daily_memo.md`
