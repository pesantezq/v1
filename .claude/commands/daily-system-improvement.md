---
description: Daily system-improvement review — "what should we improve in the Portfolio Automation System today?" Reads the deterministic system_improvement_ideas.json, presents the ranked engineering/product/ops backlog, and routes accepted ideas to the improvement approval queue. This is NOT a market-opportunity prompt and NEVER produces buy/sell/hold recommendations. Observe-only.
---

# Daily System Improvement

Type **C** prompt (system improvement) — distinct from Type A (health remediation,
owned by `operator_control`) and Type B (market opportunity). This skill answers
*"what should we improve in the system itself today?"* — reliability, observability,
dashboard/mobile UX, data quality, artifact contracts, scanner/sandbox coverage,
pattern memory, calibration reporting, docs, tests, security/privacy, performance,
cost, roadmap alignment, developer experience.

## Hard boundaries
- Observe-only. This skill **never** changes code, config, holdings, or the decision
  plan. It reads artifacts and presents/queues ideas.
- It **never** emits market buy/sell/hold/trade recommendations (the producer
  sanitizes these out; flag any that slip through as a bug).
- Approving an idea only generates a Claude Code implementation prompt / queue item
  (Phase 4). Code changes happen only when the operator explicitly launches Claude Code.

## Step 1 — Produce / refresh ideas (deterministic)
The daily pipeline runs the producer; to refresh on demand:
```bash
cd /opt/stockbot && .venv/bin/python -c "from pathlib import Path; \
from datetime import datetime, timezone; \
import portfolio_automation.system_improvement as si; \
print(si.write_system_improvement_artifacts(Path('.'), datetime.now(timezone.utc)))"
```
Artifacts (observe-only):
- `outputs/latest/system_improvement_ideas.json` — ranked ideas
- `outputs/latest/system_improvement_brief.md` — operator brief
- `outputs/latest/system_improvement_scorecard.json` — counts by category/priority
- `outputs/policy/system_improvement_history.jsonl` — append-only (dedup/cooldown source)

## Step 2 — Read + triage
Read `system_improvement_brief.md` and `system_improvement_ideas.json`. For each idea:
- Note `category`, `priority`, `final_rank_score`, `summary`, `evidence`,
  `proposed_change`, `affected_modules`, `acceptance_criteria`, `suggested_tests`.
- Ideas in cooldown (rejected/deferred recently) or `completed` are already suppressed
  by the producer; `duplicate_of` marks same-key repeats.

## Step 3 — Present the top ideas
Emit a compact, scannable list: top 5 by `final_rank_score`, each one line
(`[PRIORITY] category — title — one-line why`), then offer the operator the
artifact-based decisions (Phase 4 queue): **approve_for_implementation, reject,
defer, request_more_detail, mark_duplicate, mark_completed, create_claude_code_prompt**.

## Step 4 — Route decisions (artifact-based; executes nothing)
Record operator decisions to `outputs/policy/system_improvement_decisions.jsonl`
(append-only) and refresh `outputs/latest/system_improvement_action_queue.json`.
For an `approve_for_implementation` / `create_claude_code_prompt` decision, generate a
Type-C Claude Code implementation prompt (see
`docs/prompts/CLAUDE_CODE_SYSTEM_IMPROVEMENT_PROMPT.md`) and present it for the
operator to copy/launch — do not auto-launch.

## Cadence
Designed to run daily alongside `daily-tool-analysis` (which owns health). Keep the
two separate: health = "what's broken", system-improvement = "what could be better".
