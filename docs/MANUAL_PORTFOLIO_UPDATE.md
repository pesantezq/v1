# Manual Portfolio Update — Operator Runbook

## What this is

A small CLI tool that updates the operator's **current holdings** and **cash** in `config.json` from a simple CSV (or JSON) file, without using the GUI.

This is the **only** sanctioned path for mutating official portfolio state outside the daily pipeline's price-refresh step.

## Safety boundaries (hardcoded)

| Boundary | Value |
|---|---|
| `observe_only` | true |
| `no_trade` | true |
| `not_recommendation` | true |
| `no_allocation_policy_change` | true |
| `no_watchlist_mutation` | true |
| `no_discovery_promotion` | true |
| Run mode | `RunMode.MANUAL_UPDATE` (hardcoded) |
| Approval | explicit `--approve` CLI flag required |
| Broker / API | none |
| LLM / AI | none |

Governance enforcement is delegated to the existing `assert_can_update_portfolio_state(mode, approved=True)` in [portfolio_automation/run_mode_governance.py](../portfolio_automation/run_mode_governance.py); the tool does not invent a parallel approval system.

## Files the tool writes

| File | Path |
|---|---|
| Updated config | `config.json` (only `portfolio.holdings` and `portfolio.cash_available` change; all other keys preserved byte-for-byte) |
| Backup | `outputs/policy/portfolio_backups/config.<YYYYMMDD_HHMMSS>.json` |
| Audit (append-only) | `outputs/policy/manual_portfolio_updates.jsonl` |

Atomically written: the new `config.json` is staged to a temp file in the same directory and `os.replace`d into place, so an interrupted run cannot corrupt the file.

## What the tool will NOT touch

- `outputs/latest/*` (no LATEST writes)
- `outputs/sandbox/*` (no SANDBOX writes)
- `outputs/portfolio/*` (no PORTFOLIO snapshot writes — those come from the daily pipeline)
- Any allocation policy, scoring, recommendation, discovery, or watchlist artifact
- The `investor`, `providers`, `rebalance_rules`, `target_cash_weight` etc. sections of `config.json`

## Input format

Required columns (CSV header): `symbol,shares`
Optional columns: `target_weight`, `asset_class`, `is_leveraged`, `leverage_factor`
Any other column **rejects the file**.

For existing symbols, missing optional columns preserve the prior value.
For new symbols, missing optional columns get conservative defaults:
`target_weight=0`, `asset_class="us_equity"`, `is_leveraged=False`, `leverage_factor=1`.

### Example CSV

```csv
symbol,shares
QQQ,6
NASA,14
GLD,4
SLV,3
SCHD,5
VFH,12
```

### JSON alternative

```json
{
  "holdings": [
    {"symbol": "QQQ", "shares": 6},
    {"symbol": "GLD", "shares": 4}
  ]
}
```

## CLI

```
python -m tools.manual_portfolio_update \
    --input inputs/my_portfolio.csv \
    --cash 464.16 \
    --as-of 2026-05-12 \
    --approve
```

| Flag | Required? | Description |
|---|---|---|
| `--input <path>` | yes | CSV (preferred) or JSON file |
| `--cash <number>` | yes | New `cash_available` value (non-negative) |
| `--as-of <YYYY-MM-DD>` | yes | Operator's as-of date for this update |
| `--approve` | yes | Explicit approval; without it the tool exits with `rc=2` |
| `--config <path>` | no | Path to `config.json` (default: `./config.json`) |
| `--base-dir <path>` | no | Project root containing `outputs/` (default: config dir) |
| `--run-id <id>` | no | Audit run identifier (default: timestamp-based) |
| `--dry-run` | no | Validate + print diff without writing |

## Validation rules

| Field | Rule |
|---|---|
| Header | must contain `symbol` and `shares`; no other columns except the documented optional set |
| `symbol` | 1–10 chars, starts with `A–Z`, allows `A–Z 0–9 . -`; auto-uppercased |
| `shares` | non-negative number |
| `target_weight` (optional) | `[0.0, 1.0]` |
| `leverage_factor` (optional) | integer ≥ 1 |
| Duplicates | rejected |
| Blank rows | skipped |
| Empty file | rejected |
| `--cash` | non-negative number |
| `--as-of` | strict `YYYY-MM-DD` (e.g. `2026-05-12`); other formats rejected |
| `--approve` | must be present |
| Missing input file | rejected |
| Wrong run mode (forced) | `RunModeViolation` (this never happens via the CLI because the tool hardcodes the mode) |

All validation errors exit with `rc=2` and a clear stderr message; **nothing is written to disk on validation failure**.

## Audit record fields

Each run appends one JSONL line with these fields:

| Field | Description |
|---|---|
| `run_id`, `timestamp`, `as_of` | identifiers |
| `mode` | always `"manual_update"` |
| `approved` | always `true` (the only path that reaches the writer) |
| `dry_run` | true when `--dry-run` was passed |
| `source_input_path`, `config_path`, `backup_path`, `audit_path` | absolute paths |
| `prior_cash`, `new_cash`, `cash_delta` | numbers |
| `prior_holdings_count`, `new_holdings_count` | counts |
| `added` | list of newly-added symbols |
| `removed` | list of symbols that disappeared |
| `changed` | list of `{symbol, prior_shares, new_shares, delta}` |
| `unchanged_count` | number of symbols whose shares didn't change |
| `observe_only`, `no_trade`, `not_recommendation`, `no_allocation_policy_change`, `no_watchlist_mutation`, `no_discovery_promotion` | all `true`, hardcoded |
| `source` | `"manual_portfolio_update"` |
| `safety_disclaimer` | fixed disclaimer text |

The audit record is verified by automated tests to never contain standalone trading-action tokens (BUY/SELL/HOLD/ACTIONABLE/PROMOTED/VALIDATED).

## Typical workflow

1. **Prepare** a CSV file `inputs/my_portfolio.csv` with your current holdings.
2. **Dry-run first** to preview the diff:
   ```
   python -m tools.manual_portfolio_update \
       --input inputs/my_portfolio.csv \
       --cash 464.16 --as-of 2026-05-12 \
       --approve --dry-run
   ```
3. **Inspect the diff** printed to stdout. Look for:
   - Cash delta
   - Added / removed / changed symbols
   - Unchanged count
4. **Apply for real** (drop the `--dry-run` flag):
   ```
   python -m tools.manual_portfolio_update \
       --input inputs/my_portfolio.csv \
       --cash 464.16 --as-of 2026-05-12 \
       --approve
   ```
5. **Confirm**:
   - `cat outputs/policy/manual_portfolio_updates.jsonl | tail -1 | python -m json.tool`
   - `ls outputs/policy/portfolio_backups/`
6. **Run the daily pipeline** the normal way — it will pick up the updated holdings from `config.json` automatically:
   ```
   bash scripts/run_daily_safe.sh
   ```

## Rollback

The previous `config.json` is preserved in `outputs/policy/portfolio_backups/`. To roll back:

```bash
cp outputs/policy/portfolio_backups/config.<TIMESTAMP>.json config.json
```

The audit log is append-only and is **not** modified by a rollback. To document the rollback, run another manual update (with a CSV that matches the prior state) so a fresh audit line is recorded.

## Common errors

| Error message | Cause | Fix |
|---|---|---|
| `--approve is required` | Missing `--approve` flag | Add `--approve` |
| `Input file not found: ...` | Wrong path | Check the `--input` path |
| `Missing required column(s): ['symbol']` (or 'shares') | CSV header is wrong | Add the missing column |
| `Unsupported column(s): ['price']` | Extra column in CSV | Remove it; only the documented columns are accepted |
| `invalid symbol 'abc1$'` | Bad symbol format | Use `A–Z 0–9 . -` only |
| `shares must be non-negative` | Negative shares value | Use 0 or positive |
| `duplicate symbol 'QQQ'` | Two rows for same ticker | Consolidate to one row |
| `--cash must be non-negative` | Negative cash | Use 0 or positive |
| `--as-of must be YYYY-MM-DD` | Wrong date format | Use ISO date like `2026-05-12` |
| `RunModeViolation: …requires explicit manual approval…` | `approved=False` reached the orchestrator (would only happen if the tool is called programmatically) | Pass `approved=True` |

## Tests

File: `tests/test_manual_portfolio_update.py`
Count: 50 tests across 10 test classes (CSV parsing, JSON parsing, date/cash parsers, run-mode governance enforcement, end-to-end behavior, backup behavior, audit log, dry-run, CLI entrypoint, no-mutation-beyond-scope invariants).

## Local validation commands

```bash
python -m py_compile tools/manual_portfolio_update.py
python -m pytest -q tests/test_manual_portfolio_update.py
python -m pytest -q --ignore=tests/test_gui_api_health.py --ignore=tests/test_gui_insight_cards.py
python scripts/agent_context_check.py
```

## VPS validation commands

```bash
source .venv/bin/activate
python -m py_compile tools/manual_portfolio_update.py
python -m pytest -q tests/test_manual_portfolio_update.py
python -m pytest -q --ignore=tests/test_gui_api_health.py --ignore=tests/test_gui_insight_cards.py

# Dry-run sanity check against the current config (no writes):
echo 'symbol,shares
QQQ,6
GLD,4' > /tmp/sample_update.csv
python -m tools.manual_portfolio_update \
    --input /tmp/sample_update.csv \
    --cash 464.16 --as-of 2026-05-12 \
    --approve --dry-run
```
