# Sub-project E — Full Auto-Apply via GPT Approver — Design

- **Date:** 2026-06-05
- **Branch:** feature/pattern-improvement-loop
- **Status:** Design; autonomous build authorized ("complete through E"). **Changes documented hard invariants** — ships INERT (`enabled=False`), cannot fire until OOS maturity (~2027). Stop at production-activation boundary.
- **Sequence:** Foundation (merged) → D (on branch) → **E (this)**.

## ⚠️ Invariant change (operator-approved 2026-06-05)

CLAUDE.md currently states: registry apply is "protected, owner-gated"; `observe_only`
must not be made conditional without approval; protected scoring weights must not change
without approval. The operator explicitly chose **full auto-apply** (remove the human from
the registry-weight apply path) with a **GPT-API approver** layered on the deterministic
gates. E therefore **amends CLAUDE.md + docs** to sanction this behavior — otherwise the
daily/monthly health agents would (correctly, under the old rules) flag it as a violation
and try to revert it. The amendment is scoped narrowly to the auto-apply path and preserves
every other observe-only guarantee.

## Design principles (the safety is the gates, not the human)

1. **Fail-closed everywhere.** Any uncertainty — disabled, no OOS evidence, gate not GREEN,
   budget exceeded, LLM unreachable, unparseable verdict, drift cap hit — results in
   NO apply. The default of every branch is "do nothing".
2. **GPT can only veto or approve-within-bounds; never widen.** The deterministic Step-4
   proposal (already bounded by `max_abs_delta`, `min_n`, CI-excludes-50%) defines the
   delta. The GPT approver returns approve/veto + reason; an "approve" applies exactly the
   pre-bounded delta. The LLM cannot propose a larger move or a different signal.
3. **Reuse the protected, reversible path.** E writes `config/approved_weight_changes.json`
   (the artifact `registry_apply.apply_approved_changes` already consumes) and calls it —
   it does not re-implement registry mutation. Byte-for-byte snapshot + `revert_last`
   already exist.
4. **Inert by default.** `enabled=False`; cannot fire until `oos_window.folds_possible`.
5. **Auditable + reversible.** Every decision (approve/veto/apply/rollback) is appended to
   `outputs/policy/auto_apply_audit.json` with full provenance; a post-apply score-gate
   regression triggers automatic `revert_last`.

## Architecture

New module `backtesting/auto_apply.py`. One orchestrator `maybe_auto_apply(...)` plus small
pure helpers. Provider-agnostic GPT call via `agent.llm_adapters.call_provider`, gated by
`portfolio_automation.ai_budget.with_ai_budget`. Reuses `registry_apply` (apply + revert)
and `score_invariance_gate` (GREEN precondition + post-apply check).

### Gate sequence (ALL must pass, in order; first failure → no-op with a reason)
```
maybe_auto_apply(enabled=False, ...):
  G0 enabled is True .............................. else status="disabled"
  G1 kill-switch absent (config/auto_apply.DISABLED file AND env
     STOCKBOT_AUTO_APPLY_DISABLED unset) ........... else status="kill_switched"
  G2 oos_window.folds_possible is True ............. else status="oos_immature"   (until ~2027)
  G3 proposals: >=1 proposal with proposed_delta != 0 and status indicating
     significant edge .............................. else status="no_actionable_proposal"
  G4 drift cap: cumulative |Δ| this month + this Δ <= max_monthly_drift
     (state in data/auto_apply_state.json) ......... else status="drift_capped"
  G5 score_invariance_gate (pre) == GREEN .......... else status="score_gate_blocked"
  G6 AI budget allows the approver call ............ else status="budget_exceeded"
  G7 GPT approver verdict == approve (within_bounds) else status="gpt_vetoed"
  → write config/approved_weight_changes.json (bounded deltas + provenance)
  → registry_apply.apply_approved_changes(...)
  → score_invariance_gate (post): RED → revert_last() + status="rolled_back"
  → else status="applied"
  (audit every terminal status to outputs/policy/auto_apply_audit.json)
```

### GPT approver
- `_gpt_approve(proposal_item, *, provider, model, approver=None) -> dict` returns
  `{decision: "approve"|"veto", within_bounds: bool, reason: str}`.
- Default `approver` uses `call_provider` with a STRICT prompt: it is given the signal_id,
  current_weight, proposed_weight, proposed_delta, oos_hit_rate + CI, avg_return, and is
  told it may ONLY approve (apply the given delta) or veto, and must return a one-line JSON
  verdict. Response parsed defensively; non-JSON / missing keys / `within_bounds=false`
  → treated as veto (fail-closed).
- `approver` is INJECTABLE so tests never call a real LLM. The real call is wrapped in
  `with_ai_budget(observe_only=False?...)` — but to stay fail-closed we use observe-only
  budget semantics and veto if `not budget_event.allowed`.

### Integration (run_loop)
A non-blocking call after the D block, default-inert:
```python
try:
    from backtesting.auto_apply import maybe_auto_apply
    auto = maybe_auto_apply(enabled=_auto_apply_enabled(), poc=poc, proposals=proposals,
                            registry_path=registry_path, base_dir=base_dir, write=write)
except Exception:
    auto = {"status": "error"}
```
`_auto_apply_enabled()` reads `config.json` (e.g. `backtesting.auto_apply.enabled`, default
False). Added to the returned summary as `auto_apply`. Because `enabled` defaults False and
OOS is immature, this is a guaranteed no-op today.

### Health pairing (cadence-matched: the loop runs monthly; auto-apply is consequential → also daily-tool-analysis)
- `backtest_health`: read `outputs/policy/auto_apply_audit.json`; surface
  `details["auto_apply"]` (last status + counts). Add RED flag `auto_apply_rolled_back`
  if the most recent terminal status is `rolled_back` (a coupling regression slipped
  through pre-gate — must alert). Add AMBER `auto_apply_active` when status is `applied`
  (informational: the system changed a weight).
- `.claude/commands/monthly-tool-analysis.md` + `daily-tool-analysis.md`: read the audit,
  report last auto-apply status; dispatch `portfolio-backtest-health` + `portfolio-attribution-analyst`
  on `auto_apply_rolled_back` or any `applied` (verify the applied change's outcome).

## Non-goals
- No broker/execution/trading. E mutates registry *config weights* only, via the existing
  reversible path.
- No widening of bounds; no new scoring math; no change to Steps 1–4 or D.
- Not enabled in this sub-project (ships `enabled=False`; activation is the operator's
  production go-ahead).

## Error handling
`maybe_auto_apply` never raises (returns a status dict). Each gate degrades to a labeled
no-op. The GPT call, apply, and gate checks are individually try/excepted → fail-closed.
run_loop integration is try/except non-blocking.

## Testing (no real LLM/network; injected approver + tmp registry)
- `tests/test_auto_apply.py`:
  - disabled (default) → `status="disabled"`, no write, registry byte-identical.
  - enabled but `oos_immature` → `status="oos_immature"`, no apply.
  - kill-switch file/env present → `status="kill_switched"`.
  - all gates pass + injected approver returns approve → `status="applied"`, registry weight
    changed, `approved_weight_changes.json` written, audit entry present.
  - injected approver returns veto → `status="gpt_vetoed"`, registry byte-identical.
  - pre score-gate RED (monkeypatched) → `status="score_gate_blocked"`.
  - post score-gate RED (monkeypatched to GREEN-then-RED) → `status="rolled_back"`,
    registry restored, audit shows rollback.
  - drift cap exceeded → `status="drift_capped"`.
  - fail-closed: approver raises → veto/no-op.
  - All on a tmp registry copy; assert the real `config/signal_registry.yaml` is never
    touched.
- `tests/test_backtest_health.py`: `auto_apply_rolled_back` RED + `auto_apply_active` AMBER
  fire on fixtures; absent otherwise.
- `tests/test_run_loop.py`: summary carries `auto_apply` key with `status="disabled"` by
  default (inert).
- Full suite green.

## Files
**New:** `backtesting/auto_apply.py`, `tests/test_auto_apply.py`, this spec + the plan.
**Modified:** `backtesting/run_loop.py` (inert integration + summary key),
`backtesting/backtest_health.py` (audit read + flags),
`.claude/commands/monthly-tool-analysis.md`, `.claude/commands/daily-tool-analysis.md`,
`config.json` (`backtesting.auto_apply.enabled=false` + budget/drift knobs),
`CLAUDE.md` (sanction the scoped invariant change), `docs/CHANGELOG_DECISIONS.md`,
`docs/PATTERN_LOOP_STEP5_GATE.md` (or a new `docs/PATTERN_LOOP_AUTO_APPLY.md`),
`.agent/project_state.yaml`.

## Risks
- **Highest-risk module in the system** — it can mutate protected scoring weights without a
  human. Mitigations: fail-closed gates, inert default, OOS gate (no fire until 2027),
  GPT-can-only-veto-or-apply-bounded, pre+post score-invariance gate, auto-rollback,
  kill-switch, full audit, reuse of the reversible protected path. Activation is a separate
  operator go-ahead.
- LLM non-determinism on a config-mutation path — mitigated: the LLM cannot change the delta
  magnitude (bounded upstream) and can only veto/approve; fail-closed on any anomaly.
- AI cost — mitigated: `with_ai_budget` gate; and it only calls when OOS-mature + all prior
  gates pass (rare).
