# Pattern-Improvement Loop — Full Project Workflow

> End-to-end reference for the advisory, self-tuning signal-weight loop. Advisory-only:
> it tunes `signal_registry.yaml` `default_weight` values within bounds — it does NOT
> change scoring math, the decision engine, or execute trades.

## 1. Purpose

A self-tuning loop that learns which signal patterns actually predict returns and nudges
their **registry weights** — bounded, gated, reversible, and (optionally) hands-off —
without touching protected scoring/decision logic.

## 2. Lifecycle

```
 NOW ───────────────────────────────────────────────────────────► ~2027 (live) / NOW (reconstructed)
 ┌──────────┐   ┌──────────┐   ┌───────────┐   ┌──────────┐   ┌──────────┐
 │ ACCRUE   │──►│ MATURE   │──►│ PROPOSE   │──►│ APPROVE  │──►│ APPLY    │
 │ signals  │   │ OOS      │   │ bounded   │   │ gates +  │   │ registry │
 │ history  │   │ window   │   │ Δweight   │   │ GPT/human│   │ weight   │
 └──────────┘   └──────────┘   └───────────┘   └──────────┘   └──────────┘
   daily          315 cal-days   walk-forward    fail-closed     reversible
   pipeline       (live) OR       OOS hit-rate    every step      + audited
                  reconstructed   + Wilson CI                     + alerted
```

## 3. Pipeline (data flow, each run)

```
outputs/history/<date>/        ← daily pipeline emits live signals (accrues slowly)
   OR
5y FMP price archive ──► historical_signal_recon ──► outputs/backtest/recon/<date>/   [F: point-in-time]
                              │
                              └─► assert_no_lookahead ──► reconstruction_audit.json  [look_ahead_clean?]
   │
   ▼
run_loop  (backtesting/run_loop.py)
   ├─ Step 1/1b/3  POC sim + oos_window maturity countdown        → poc_simulation_results.json
   ├─ Step 2       walk-forward OUT-OF-SAMPLE efficacy (Wilson CI)
   ├─ Step 4       bounded weight PROPOSALS (≤0.05, CI must exclude 50%) → signal_weight_proposals.json
   ├─ D            calibration_proposer + tagging_proposer (observe-only) → outputs/policy/*proposal.json
   └─ E            maybe_auto_apply ──► 8 fail-closed gates ──► registry_apply (reversible)
                                                                → config/signal_registry.yaml
                                                                → outputs/policy/auto_apply_audit.json
```

## 4. Components

| Sub-project | What it added | Module(s) |
|---|---|---|
| **Foundation (A+B+C)** | monthly recompute + `oos_window` maturity countdown + monthly-analysis wiring | `walk_forward.oos_window_status`, `scripts/pattern_loop_recheck.sh` |
| **D — feedback proposers** | calibration-correction map + tagging coverage proposals (observe-only) | `backtesting/calibration_proposer.py`, `backtesting/tagging_proposer.py` |
| **E — auto-apply** | GPT approver + 8 gates + auto-rollback + kill-switch + audit (sanctioned exception) | `backtesting/auto_apply.py`, `backtesting/registry_apply.py`, `backtesting/score_invariance_gate.py` |
| **F — reconstruction** | point-in-time historical signals from 5y prices + truncation-equality look-ahead audit | `backtesting/historical_signal_recon.py`, `scripts/pattern_loop_reconstruct.sh` |
| **Analysis skill** | operational + health readout; wired into daily check | `.claude/commands/pattern-loop-analysis.md` |
| **Watcher** | alert (log + email) on any weight change + armed-state | `scripts/pattern_loop_check.sh` |

## 5. Operational cadence (cron)

```
0  9  * * *      run_daily_safe.sh          → accrues live signals (history snapshots)
15 9  * * 1-5    daily_check.sh             → /daily-tool-analysis (incl. pattern-loop heartbeat)
30 9  1 * *      monthly_check.sh           → pattern_loop_recheck.sh (LIVE recompute; OOS-immature until ~2027) + /monthly-tool-analysis
50 9  1 * *      pattern_loop_reconstruct.sh → RECONSTRUCT → audit → run_loop(auto-apply)   ← the autonomous loop
15 10 1 * *      pattern_loop_check.sh      → watcher: alert (email + log) on any weight change
```

## 6. Safety gates (auto-apply, in order — first failure = no-op)

```
G0  enabled (config.json backtesting.auto_apply.enabled)
G1  no kill-switch (config/auto_apply.DISABLED file / STOCKBOT_AUTO_APPLY_DISABLED env)
G2  OOS window mature (oos_window.folds_possible)
G2b reconstructed evidence → reconstruction_audit.look_ahead_clean == true
G3  ≥1 actionable proposal (proposed_delta ≠ 0, status "proposed")
G4  monthly drift + Δ ≤ max_monthly_drift (0.10)
G5  pre-apply score-invariance gate == GREEN
G6  AI budget allows the approver call
G7  GPT approver verdict == approve (veto / approve-pre-bounded-delta ONLY — never widens)
       ↓ all pass
 write config/approved_weight_changes.json → registry_apply (byte-for-byte snapshot)
   → POST-apply score-invariance gate → RED ⇒ auto-rollback (revert_last) ⇒ status "rolled_back"
   → else status "applied"   (every terminal state appended to auto_apply_audit.json)
```

**Bounds:** ≤0.05 per change, ≤0.10/month cumulative drift. The LLM cannot change the delta
magnitude or pick a different signal — only approve the pre-bounded proposal or veto.

## 7. Oversight & notifications

- `logs/pattern_loop_alerts.log` — every weight change (owner-gated OR autonomous), always written.
- **Email** — `[StockBot] Pattern-Loop weight change` via the daily-memo SMTP path
  (`SMTP_SERVER`/`EMAIL_USER`/`EMAIL_PASS`/`EMAIL_TO`); delivery tested.
- **Armed-state** (`enabled` + reconstruct-cron + kill-switch) reported every watcher run.
- `backtest_health` flags: `auto_apply_rolled_back` (RED), `auto_apply_active` (AMBER),
  `reconstruction_lookahead_dirty` (RED), `calibration_correction_available` /
  `high_untagged_rate` (AMBER).
- `/pattern-loop-analysis` on demand; dispatched by the daily/monthly checks on RED.

## 8. Operator controls

| Action | Command |
|---|---|
| Halt instantly | `touch config/auto_apply.DISABLED` |
| Undo last weight change | `python -c "from backtesting.registry_apply import revert_last; print(revert_last())"` |
| Disarm | set `config.json backtesting.auto_apply.enabled=false` |
| Inspect health | `/pattern-loop-analysis` (or `claude --print /pattern-loop-analysis`) |
| Re-run reconstruction manually | `scripts/pattern_loop_reconstruct.sh` |

## 9. Key invariants

- Advisory-only; no broker/execution. Tunes registry `default_weight` data ONLY.
- Auto-apply is the single sanctioned mutating path (CLAUDE.md, operator-approved 2026-06-05);
  everything else is observe-only.
- Arming the autonomous loop and pushing the enablement to `main` are **operator actions**
  (deliberate human hand) — not performed autonomously by the agent.
- Every weight change is bounded, reversible (snapshot + `revert_last`), audited, and alerted.
