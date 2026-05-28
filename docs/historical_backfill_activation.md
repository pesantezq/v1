# Historical Backfill — Cron Activation Checklist

**Status:** module shipped 2026-05-28; cron entry deliberately NOT installed yet.

The historical backfill collector (`portfolio_automation/historical_backfill.py`)
is Step 2 of the FMP-capacity roadmap sequence per
`.agent/project_state.yaml:queued_after_n100_confirmation`. It is fully
implemented, fully tested, and the wrapper script
`scripts/historical_backfill.sh` is ready to install.

The roadmap blocks activation on "one week of attribution data on raised
budget" — meaning a full week after `raise_fmp_daily_budget` (Step 1,
shipped 2026-05-28) must elapse before the weekend cron is installed.

## Pre-activation checklist

Before running the activation command below, verify:

- [ ] At least 7 calendar days have passed since 2026-05-28 (Step 1 ship date)
- [ ] `outputs/latest/retune_impact.json:current_fingerprint` is still
      `f60e0b9d51bec808` AND `current_fp_resolved_1d ≥ 200` (gauge is
      still validated after a week at the raised budget)
- [ ] `outputs/latest/fmp_budget_status.json:budget.status` has been
      reporting `ok` consistently this week (no exhausted days)
- [ ] No structural issues flagged in any daily-tool-analysis report this week
- [ ] The portfolio-attribution-analyst verdict on the most recent run is
      still PROMOTE-THEN-OBSERVE or stronger

Soonest viable activation: **2026-06-04 (Thursday)** if all checks pass.

## Activation command

```bash
( crontab -l ; cat <<'EOF'

# Historical Backfill — weekend FMP collector for 5y price history
# Step 2 of FMP-capacity roadmap. Sat + Sun 07:00 UTC, before discovery pulse.
0 7 * * 6,0  /opt/stockbot/scripts/historical_backfill.sh
EOF
) | crontab -
```

## Post-activation verification

Within 1 hour of the first Sat 07:00 UTC run after activation:

```bash
# 1. Wrapper completed cleanly
tail -20 /opt/stockbot/logs/historical_backfill_$(date -u +%Y-%m-%d).log

# 2. Status artifact reports the universe was processed
cat /opt/stockbot/outputs/latest/historical_backfill_status.json | jq '{
  universe_size, fetched, skipped_fresh, skipped_budget, errored
}'

# 3. At least one HISTORICAL archive was written
ls -la /opt/stockbot/outputs/backtest/historical/ | head -5

# 4. content_liveness check is clean for historical_backfill.last_run
.venv/bin/python -c "
from pathlib import Path
from portfolio_automation.daily_run_status import scan_content_liveness
import json
r = [x for x in scan_content_liveness(Path('.')) if x['name'] == 'historical_backfill.last_run']
print(json.dumps(r, indent=2))
"

# 5. FMP budget after the run should still have headroom
cat /opt/stockbot/data/fmp_cache/call_counter.json
```

## Rollback

If anything goes sideways the first weekend:

```bash
crontab -l | grep -v "historical_backfill.sh" | crontab -
```

The archives at `outputs/backtest/historical/*.json` are observe-only and
can stay in place; no other producer depends on them yet (the
historical_replay loader will pick them up automatically the next time
it runs, and falls back to live FMP fetch if the archive is missing).

## Why deferred a week

The raised FMP daily cap (500) is itself an unproven change as of
2026-05-28. The roadmap reasoning: keep the data substrate stable while
the gauge attribution is at its noisiest (n=176, still climbing toward
n=300). A second substrate change (5y backfill) on top of an unproven
budget raise would make any future regression hard to attribute.

One week of clean daily runs at the raised budget gives confidence that
the substrate is stable, then the weekend collector activates against a
known-good baseline.
