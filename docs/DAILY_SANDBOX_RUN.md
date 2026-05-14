# Daily Sandbox Run

A safe, observe-only orchestration layer for the sandbox/research lane.
Refreshes discovery news enrichment and automatic-promotion governance
artifacts on a daily cadence — without touching the official daily
pipeline, the email memo, the official portfolio, the official watchlist,
allocation policy, scoring logic, recommendation logic, or any trade
behavior.

> The sandbox runner is research-only.  It does not execute trades, call
> broker APIs, mutate official portfolio or watchlist state, change
> allocation policy, or emit BUY/SELL/HOLD recommendations.  All outputs
> are sandbox/discovery research artifacts.

## Entry points

| Surface | Command |
|---|---|
| Python CLI | `python -m tools.daily_sandbox_run` |
| Shell wrapper | `bash scripts/run_daily_sandbox_safe.sh` |
| Module API | `tools.daily_sandbox_run.run_daily_sandbox(...)` |

CLI flags:

| Flag | Default | Effect |
|---|---|---|
| `--base-dir <path>` | `.` | Project root that contains (or will create) `outputs/` |
| `--run-id <id>` | timestamp | Override the run identifier in the status artifact |
| `--dry-run` | off | Run module steps but skip writing the `sandbox_run_status` artifacts |
| `-v / -vv` | off | Increase logging verbosity (`WARNING`/`INFO`/`DEBUG`) |

The CLI always returns exit code 0 unless a top-level wrapper error occurs.
Per-step success/failure is recorded in the status artifact, so a failed
sandbox step never blocks the systemd timer or the official daily pipeline.

## Steps

The runner calls existing module entry points only.  It does not
reimplement their logic.

1. **`discovery_news_integration`**
   Calls `run_discovery_news_integration(run_mode="discovery")`.
   Reads `outputs/latest/news_intelligence.json` and existing sandbox
   discovery candidates; writes
   `outputs/sandbox/discovery/news_enriched_candidates.json` and
   `outputs/sandbox/discovery/news_integration_summary.md`.

2. **`automatic_promotion_governance`**
   Calls `run_automatic_promotion_governance(run_mode="discovery",
   write_files=True)`.  Evaluates sandbox candidates against deterministic
   gates and writes
   `outputs/sandbox/discovery/automatic_promotion_candidates.json`,
   `outputs/sandbox/discovery/automatic_promotion_decisions.jsonl`, and
   `outputs/sandbox/discovery/automatic_promotion_summary.md`.

3. **`discovery_replay`** (optional)
   Calls `run_discovery_replay(run_mode="discovery")` only if
   `outputs/sandbox/discovery/replay_price_outcomes.json` exists and
   contains a non-empty `price_outcomes` mapping.  Otherwise the step is
   marked `skipped` — the runner never fetches price data.

Each step is wrapped in a try/except so a single module failure cannot
abort sibling steps.  A failed step is recorded in the status artifact
with its error message.

## Output artifacts

| Path | Format | Purpose |
|---|---|---|
| `outputs/sandbox/discovery/sandbox_run_status.json` | JSON | Machine-readable run summary |
| `outputs/sandbox/discovery/sandbox_run_status.md` | Markdown | Operator-facing summary |

The status payload always contains the following hardcoded safety flags:

```json
{
  "observe_only": true,
  "no_trade": true,
  "not_recommendation": true,
  "discovery_only": true,
  "no_portfolio_mutation": true,
  "no_watchlist_mutation": true,
  "no_allocation_policy_change": true,
  "no_decision_override": true,
  "no_score_mutation": true,
  "run_mode": "discovery",
  "source": "daily_sandbox_run"
}
```

Plus the run summary:

```json
{
  "generated_at": "<ISO timestamp>",
  "run_id": "<run identifier>",
  "disclaimer": "...",
  "steps_attempted": 3,
  "steps_succeeded": 2,
  "steps_skipped": 1,
  "steps_failed": 0,
  "steps": [
    {"name": "discovery_news_integration", "status": "succeeded", ...},
    {"name": "automatic_promotion_governance", "status": "succeeded", ...},
    {"name": "discovery_replay", "status": "skipped", "skip_reason": "..."}
  ],
  "errors": [],
  "candidate_counts":   {"emerging": 12, "rejected": 4, "enriched": 12},
  "news_evidence_counts": {"evidence_packets": 87, "with_news": 9},
  "automatic_promotion_counts": {
    "decision_count": 12, "monitor": 3, "needs_review": 2,
    "rejected": 5, "expired": 2, "decisions_jsonl_lines": 42
  },
  "artifact_paths_written": ["outputs/sandbox/discovery/..."]
}
```

## Safety invariants

- **Observe-only.** No mutation of `config.json`, the official watchlist,
  allocation policy, or any decision artifact.
- **No trade execution.** No broker integration, no order placement.
- **No LLM/AI calls** inside this orchestrator (downstream modules retain
  their existing behavior).
- **Run mode hardcoded** to `RunMode.DISCOVERY` for every step.
- **Namespace boundary.** The runner only writes to `OutputNamespace.SANDBOX`.
  This is enforced by `safe_write_json` / `safe_write_text` from
  `portfolio_automation/data_governance.py`.
- **No forbidden tokens.** The runner code and its artifacts must not
  contain `BUY`/`SELL`/`HOLD`/`ACTIONABLE`/`PROMOTED`/`VALIDATED`/`APPROVED`/
  `TRADE`/`RECOMMENDATION` outside the fixed safety disclaimer.  Tests
  enforce this.
- **Non-blocking.** A failed sandbox step does not abort the wrapper, does
  not change the wrapper's exit code, and does not touch the official
  daily pipeline.

## Scheduling

The sandbox lane is intentionally a separate timer from the official daily
pipeline.  See [`deploy/systemd/`](../deploy/systemd/) for example unit
and timer files (not enabled by default):

- `stockbot-sandbox-daily.service`
- `stockbot-sandbox-daily.timer`

The official pipeline (`stockbot-daily.*`) is unaffected by sandbox lane
state.  The sandbox lane can be enabled or disabled independently with
standard `systemctl` commands.

## Tests

`tests/test_daily_sandbox_run.py` covers:

- Happy path with all modules available
- Safe degradation when optional inputs (news intelligence, candidates,
  replay inputs) are missing or malformed
- Namespace boundaries: no writes to `outputs/portfolio`, no edits to
  `config.json` or any watchlist file, no writes outside
  `outputs/sandbox/discovery/`
- `--dry-run` skips the status-artifact write
- No trading-action tokens leak into the status JSON, markdown, or module
  source (other than the fixed safety disclaimer)
- Module-level exceptions are recorded as failed steps, not raised
- CLI smoke test for `python -m tools.daily_sandbox_run`

Run them with:

```
python -m pytest -q tests/test_daily_sandbox_run.py
```

## VPS validation

After deploying, run on the VPS:

```bash
source .venv/bin/activate
python -m py_compile tools/daily_sandbox_run.py
python -m pytest -q tests/test_daily_sandbox_run.py
python -m pytest -q tests/discovery/
bash scripts/run_daily_sandbox_safe.sh
test -f outputs/sandbox/discovery/sandbox_run_status.json && echo "STATUS OK"
test -f outputs/sandbox/discovery/sandbox_run_status.md   && echo "MD OK"
```

The official daily pipeline should remain unaffected:

```bash
bash scripts/preflight.sh
bash scripts/run_daily_safe.sh
```
