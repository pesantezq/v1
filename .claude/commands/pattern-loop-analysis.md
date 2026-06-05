---
description: Pattern-Improvement Loop analysis — operational function + health check of the backtesting loop tool (Foundation OOS-maturity + D feedback proposers + E auto-apply). Reads the loop's artifacts + recompute log, runs the deterministic backtest_health assessor, triages GREEN/AMBER/RED, dispatches portfolio-backtest-health on RED, and emits a heartbeat + structured body. On-demand; complements the monthly-tool-analysis threshold dispatch.
---

# Pattern-Loop Analysis

Focused operational + health review of ONE tool: the Pattern-Improvement Loop
(`backtesting/run_loop.py` and its layers). Companion to `daily-tool-analysis` and
`monthly-tool-analysis` (which threshold-dispatch into this tool); use this for a
deep, single-tool readout — after a recompute, before trusting a proposal, or when
investigating the loop specifically. Working dir: `/opt/stockbot`. Interpreter:
`/opt/stockbot/.venv/bin/python`.

The loop has three layers, all observe-only EXCEPT the gated auto-apply mutator:
- **Foundation** — monthly recompute (`scripts/pattern_loop_recheck.sh`) + OOS-window
  maturity countdown (`poc_simulation_results.json.oos_window`).
- **D — feedback proposers** — `calibration_correction_proposal.json`,
  `signal_tagging_proposal.json` (proposes-only, owner/OOS-gated apply).
- **E — auto-apply** — `backtesting/auto_apply.py`, default-INERT; `auto_apply_audit.json`.

---

## Step 1 — Read artifacts + run the deterministic assessor

Run the deterministic backbone (never raises; degrades to flags):

```bash
/opt/stockbot/.venv/bin/python - <<'PY'
import json, os, time
from backtesting.backtest_health import assess_backtest_health
print(json.dumps(assess_backtest_health(), indent=2, default=str))
# recompute freshness (cron writes a monthly log)
import glob
logs = sorted(glob.glob("logs/pattern_loop_recheck_*.log"))
if logs:
    age_h = (time.time() - os.path.getmtime(logs[-1])) / 3600.0
    print("RECHECK_LOG", logs[-1], f"age_h={age_h:.1f}")
    print("LAST_LINES:")
    print("".join(open(logs[-1]).readlines()[-3:]))
else:
    print("RECHECK_LOG none — monthly recompute has not run yet")
PY
```

Then read (degrade gracefully on any miss):
1. `outputs/backtest/poc_simulation_results.json` → `mode`, `performance.evaluated`,
   `oos_window` (the maturity block), `calibration.{calibration_slope,well_calibrated}`,
   `added_metrics.per_regime`.
2. `outputs/policy/signal_weight_proposals.json` → `summary.proposed_count`, per-proposal
   `status` + `oos_n`.
3. `outputs/policy/calibration_correction_proposal.json` → `inverted`, `apply_gate`, `bands`.
4. `outputs/policy/signal_tagging_proposal.json` → `untagged_pct`,
   `families_missing_registry_id`, `proposals`.
5. `outputs/policy/auto_apply_audit.json` → last entry `status` + provenance (absent is normal).
6. `config.json` → `backtesting.auto_apply.enabled` (expected `false` until activated).

---

## Step 2 — Compute signals (four lenses)

**Developer lens (operational function):**
- `recompute_fresh` = recheck-log age ≤ ~35 days (monthly cadence) AND last line `exit=0`.
- `run_mode_ok` = `mode == "real_signals_live"` (a `*_offline` mode means FMP key/price
  fetch failed → the numbers are synthetic, NOT a real read).
- `evaluated_nonzero` = `performance.evaluated > 0` (else looks-fresh-but-empty).

**Quant lens (evidence quality):**
- `oos_maturity` = `oos_window.calendar_days_observed / 315`; `folds_possible` bool;
  `full_window_eta`. While `folds_possible == false`, `proposed_count == 0` is EXPECTED
  and healthy (the loop cannot produce out-of-sample evidence yet).
- `proposal_readiness` = count of proposals with `status == "proposed"` and `oos_n ≥ min_n`.
- `calibration_inverted` = `calibration.calibration_slope < 0` (confidence anti-predictive).

**Process analyst lens (the mutator's governance):**
- `auto_apply_state` = config `enabled` + last audit `status`
  (disabled / oos_immature / gpt_vetoed / drift_capped / score_gate_blocked / applied / rolled_back).
- `drift_headroom` = from `data/auto_apply_state.json:monthly_drift` vs `max_monthly_drift`
  (only relevant once enabled + applying).
- `kill_switch_present` = `config/auto_apply.DISABLED` exists OR `STOCKBOT_AUTO_APPLY_DISABLED` set.

**Market expert lens (data hygiene feeding the loop):**
- `untagged_rate` = `signal_tagging_proposal.untagged_pct` (high = attribution starved).
- `families_missing_registry_id` (e.g. `SIGNAL_SCORE`) — signals that can never receive a weight.
- `regime_degenerate` = every `added_metrics.per_regime[].regime == "unknown"`.

---

## Step 3 — Triage

**GREEN** (loop operating correctly, accruing):
- `recompute_fresh` AND `run_mode_ok` AND `evaluated_nonzero`.
- `auto_apply_state` ∈ {disabled, oos_immature} (the expected inert steady state).
- No RED conditions below. (Pre-maturity `proposed_count == 0` + inverted calibration +
  high untagged are EXPECTED accruing-state AMBERs, not REDs — see below.)

**AMBER** (worth surfacing; no urgent action — the normal pre-2027 state):
- `folds_possible == false` (accruing toward `full_window_eta`) — report the countdown.
- `calibration_inverted` (D1 provisional map available; `apply_gate == oos_unconfirmed`).
- `untagged_rate ≥ 0.50` OR `families_missing_registry_id` non-empty (route D2 proposal to owner).
- recheck-log age in (35d, 45d] (recompute slightly overdue).

**RED** (operator must act):
- `auto_apply_state == rolled_back` — a post-apply score-invariance regression auto-reverted.
  A coupling slipped the pre-gate; investigate immediately.
- `auto_apply_state == applied` AND the change is unreviewed — a registry weight was
  auto-changed; verify the applied delta and its outcome.
- `run_mode == "*_offline"` on a run that should be live (FMP failure → fake numbers).
- `evaluated == 0` (looks-fresh-but-empty) OR `regime_degenerate`.
- recheck-log missing for > 45 days (monthly recompute stopped) OR last line `exit != 0`.
- config `enabled == true` while `folds_possible == false` (auto-apply armed with NO
  out-of-sample evidence — premature activation; recommend `enabled=false` until maturity).

---

## Step 4 — Agent dispatch

`portfolio-backtest-health` IF any RED, OR `auto_apply_state` ∈ {applied, rolled_back}, OR
`run_mode == "*_offline"`. (It re-runs the deterministic assessor + score-invariance gate
and gives a trust verdict on the evidence.)

`portfolio-attribution-analyst` ADDITIONALLY IF `auto_apply_state == applied` — verify the
applied weight change's downstream outcome by gauge/regime.

Neither fires merely because `folds_possible == false` / `proposed_count == 0` — that is the
expected pre-maturity state.

---

## Step 5 — Output

**Heartbeat** (one line, always):
```
[GREEN|AMBER|RED] pattern-loop YYYY-MM-DD: <mode>, OOS {observed}/315 (~{eta}), proposals {n}, auto-apply {state}
```

**Body** (≤ 250 words):
1. **Operational** — recompute freshness + exit, run mode, evaluated count.
2. **Evidence** — OOS maturity countdown (accruing until ~2027 is healthy), proposal
   readiness, calibration inversion (provisional map gated).
3. **Mutator governance** — auto-apply config `enabled` + last audit status + kill-switch;
   confirm inert OR (if active) what changed and whether it was reviewed.
4. **Data hygiene** — untagged rate, families missing a registry id, regime health.
5. **Agent dispatch results** (one line per fired agent).
6. For RED: the named action (e.g. "set kill-switch + investigate rollback").

---

## Notes
- This tool is observe-only EXCEPT `backtesting/auto_apply.py` (the sanctioned gated
  mutator — see CLAUDE.md Protected Semantics → Sanctioned exception, and
  `docs/PATTERN_LOOP_AUTO_APPLY.md`). Treat any auto-apply event as something to VERIFY, not
  to silently revert.
- Until the OOS window matures (~2027-03), the steady state is: live mode, 0 proposals,
  inverted calibration, ~70% untagged, auto-apply inert. That is GREEN/AMBER, not a fault.
