# decision_authority — capital-authority consistency gate

Module: `portfolio_automation/decision_authority.py`
Stage: `scripts/run_daily_safe.sh` Stage 9e3 (daily, non-blocking)
Artifact: `outputs/latest/decision_authority.json` (`OutputNamespace.LATEST`)
Posture: **observe-only**. Never recomputes a decision, never resizes, never
writes `decision_plan.json`, never executes.

## The problem this exists to catch

The system has two capital authorities. Each is internally coherent. Until
2026-08-08 nothing compared them.

**Authority 1 — `outputs/latest/decision_plan.json`** (the declared decision
source of truth). Every row carries `recommended_amount`, computed by
`adjustment.py` as drift-to-target sizing. `decision_engine._build_legacy_capital_action`
(`portfolio_automation/decision_engine.py:246`) renders that number into an
**imperative sentence**:

```
"Scale existing position — add about $1,588."
"Open new position — deploy about $106."
```

That sentence is built from `decision` and `recommended_amount` alone. It has
**no input from cash, protected reserve, contributions, or weekly pacing**.

**Authority 2 — `outputs/latest/daily_capital_plan.json`** (the funding
authority). It applies the capital waterfall and pacing, and may legitimately
fund nothing.

### Observed live, 2026-08-08

| Artifact | What it said |
|---|---|
| `daily_capital_plan.json` | `funded_actions: []` · *"No capital is funded for deployment today ($0 available after pacing)… the $4,890 unconstrained total is not an instruction to invest that amount today."* |
| `cash_deployment_plan.md` | *"Cash deployment: $0.00 across 0 position(s)"*, every row `DEFERRED_BY_WEEKLY_PACING` |
| `decision_plan.json` | VFH `capital_action: "Scale existing position — add about $1,588."` (+ QQQ $1,357, VXUS $1,058, NASA $675, PLTR $106, NVDA $106) |

Those six instructions total ≈ $4,890 — the exact figure the capital plan names
in its own warning. The capital layer knew the number was not deployable; the
decision plan still rendered each component as an instruction.

Any investor-facing consumer that renders `capital_action` verbatim therefore
issues a funded-sounding dollar instruction the capital layer already denied.
Known consumers of `capital_action`: `watchlist_scanner/daily_memo.py`,
`gui_v2/data/today.py`, `gui_operator_data.py`, `gui/app.py`.

## What the gate does

`reconcile_capital_authority(decision_plan, capital_plan)` is pure over its
inputs and returns one of three statuses.

| Status | Meaning |
|---|---|
| `CONSISTENT` | Every deploying instruction is matched by a funded action of the same size. |
| `BLOCKED_BY_CONSISTENCY` | At least one instruction is unfunded, or funded at a materially different amount. |
| `INSUFFICIENT_DATA` | An authority is absent, empty, or `available: false`. |

It **fails closed**. A missing or degraded capital plan yields
`INSUFFICIENT_DATA`, never `CONSISTENT` — otherwise a broken funding authority
would read as "nothing conflicts", which is the precise failure mode the module
exists to prevent. It deliberately does **not** decide which number the operator
should follow.

### What counts as an instruction

Only `DEPLOYING_DECISIONS = {BUY, SCALE}` with `recommended_amount > 0`.

Deliberately excluded:

- **`WAIT` / `HOLD` / `AVOID`** — live data carries `recommended_amount: 105.85`
  on `WAIT` rows whose rendered sentence is *"Stand by — do not deploy capital
  until conditions improve."* A sizing hint on a stand-down row is not an
  instruction to deploy. Counting these would make the gate fire on every run
  and train the operator to ignore it.
- **`SELL`** — releases capital rather than consuming deployable capital, so the
  deployment gate does not demand a funding source for it.

`AMOUNT_TOLERANCE = 1.0` dollar, because `capital_action` renders whole dollars
(`$106` for `105.85`); a sub-dollar delta is presentation, not disagreement.

## Conflict record

```json
{
  "symbol": "VFH",
  "decision": "SCALE",
  "kind": "unfunded_capital_instruction",
  "decision_plan_amount": 1587.696,
  "capital_plan_funded": 0.0,
  "instruction": "Scale existing position — add about $1,588.",
  "detail": "decision_plan instructs deploying $1,587.70 to VFH, but the funding authority funded $0.00"
}
```

`kind` is `unfunded_capital_instruction` or `amount_disagreement`.

## Provenance

`provenance` carries `decision_plan_run_id`, `decision_plan_generated_at`, and
`capital_plan_generated_at`, so a conflict can be tied to the exact pair of
artifacts that produced it — and so a stale-pairing cause (one authority from an
older run) is distinguishable from a genuine logic conflict.

## Health coverage

`.claude/commands/daily-tool-analysis.md` reads this artifact (item 14b) and
triages **RED** on `BLOCKED_BY_CONSISTENCY`, **AMBER** on `INSUFFICIENT_DATA`.

## Known limitations

- **The gate reports; it does not repair.** Making `capital_action` funding-aware
  means changing `decision_engine._build_legacy_capital_action`, which is
  protected under CLAUDE.md → Protected Semantics and needs explicit operator
  approval. Until that approval, a standing RED is the expected state and should
  be read as *known-structural*, not re-investigated daily.
- It compares symbol and amount only. It does not verify that a funded action's
  `funding_source` is itself solvent — the capital plan owns that.
- It does not inspect rendered Markdown/HTML. A consumer that invents a dollar
  figure not present in `decision_plan.json` would not be caught here;
  renderer purity is a separate concern.

## Tests

`tests/test_decision_authority.py` — 18 tests, including the live VFH-vs-$0
regression, the `WAIT`-row false-positive guard, `SELL` exclusion, rounding
tolerance, and four fail-closed cases.
