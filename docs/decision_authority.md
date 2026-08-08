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

`reconcile_capital_authority(decision_plan, capital_plan, rendered_surfaces)` is
pure over its inputs and returns one of four statuses.

| Status | Grade | Meaning |
|---|---|---|
| `CONSISTENT` | GREEN | No unconstrained sizing and no rendered leak. |
| `CONSISTENT_WITH_UNCONSTRAINED_SIZING` | GREEN | The artifact carries sizing the capital plan did not fund, but **no investor surface renders it as money**. Today's expected steady state. |
| `BLOCKED_BY_CONSISTENCY` | RED | A rendered investor surface leaked funded-sounding money language for an unfunded symbol. A renderer regression. |
| `INSUFFICIENT_DATA` | AMBER | An authority is absent, empty, or `available: false`. |

### Why the grade is taken at the CONSUMER boundary

The first version of this gate graded the raw artifact and therefore returned RED
on every run, because `capital_action` is a legacy unconstrained-sizing field
that will keep carrying unfunded numbers for as long as it exists. A permanent
RED is not an invariant — it trains the operator to ignore the gate.

A sizing number in `decision_plan.json` is not an instruction to anyone. It
becomes one only when an investor-facing product renders it. So the gate reads
the rendered surfaces (`INVESTOR_SURFACES`) and grades those, while still
reporting every unconstrained-sizing entry in the payload. **Detection is
retained; only the grade moved.**

Verified 2026-08-08 across every consumer of `capital_action`:

| Consumer | Renders it as funded? |
|---|---|
| `watchlist_scanner/daily_memo.py` | **No** — labels the total *"NOT a spend-today budget"* and prints *"Funded today: $0"* |
| `gui_v2/data/today.py` | Computes `capital_actions`; **no gui_v2 template renders it** |
| `gui_operator_data.py` | Feeds `gui/app.py` only |
| `gui/app.py` | Renders `"Total: $4,890"` — but Streamlit is **retired** (`docs/STREAMLIT_RETIREMENT.md`) |
| Finance Digest | **Does not exist** — no module, no cron |

Live result: `CONSISTENT_WITH_UNCONSTRAINED_SIZING`, 6 unconstrained symbols,
**0 rendered leaks**.

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

- **Leak detection is textual.** `find_rendered_instructions` matches money
  phrasings (`_INSTRUCTION_PATTERNS`) against rendered text. A renderer that
  invented a novel phrasing could evade it. It is a regression tripwire on the
  known legacy sentences, not a proof of renderer purity.
- **`INVESTOR_SURFACES` is a fixed list.** A new investor-facing product must be
  added to it, or it is not covered. `surfaces_checked` is reported on every run
  so that coverage is auditable rather than assumed.
- **`capital_action` itself is unchanged.** Making it funding-aware would mean
  editing `decision_engine._build_legacy_capital_action`, which is protected
  under CLAUDE.md → Protected Semantics. It remains available as unconstrained
  sizing context, which is the operator-approved strategy (2026-08-08).
- It does not verify that a funded action's `funding_source` is itself solvent —
  the capital plan owns that.

## Tests

`tests/test_decision_authority.py` — 18 tests, including the live VFH-vs-$0
regression, the `WAIT`-row false-positive guard, `SELL` exclusion, rounding
tolerance, and four fail-closed cases.
