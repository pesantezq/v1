# Sub-project E — Auto-Apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** `backtesting/auto_apply.py` — a fail-closed orchestrator that, only when all gates pass (enabled + no kill-switch + OOS mature + actionable proposal + drift cap + pre score-gate GREEN + AI budget + GPT approver = approve), writes `config/approved_weight_changes.json` and invokes the reversible protected apply, with a post-apply score-gate auto-rollback and full audit. Ships INERT (`enabled=False`). Amends CLAUDE.md to sanction the scoped invariant change.

**Spec (authoritative, incl. gate sequence + test matrix):** `docs/superpowers/specs/2026-06-05-pattern-loop-E-auto-apply-design.md`

**Conventions:** interpreter `/opt/stockbot/.venv/bin/python`; branch `feature/pattern-improvement-loop`; tests inject the approver + use a tmp registry copy (NEVER touch `config/signal_registry.yaml`); no real LLM/network in tests.

---

### Task E1: auto_apply orchestrator + tests (TDD)
**Files:** Create `backtesting/auto_apply.py`; Test `tests/test_auto_apply.py`

Public API:
- `maybe_auto_apply(*, enabled=False, poc, proposals, registry_path, approval_path=None, history_dir=None, base_dir="outputs", state_path=None, max_monthly_drift=0.10, max_abs_delta=0.05, provider=None, model=None, approver=None, now_iso=None, write=True) -> dict`
  returns `{observe_only:False, status, reason?, gate, applied?, audit_path?}` where `status` ∈ {disabled, kill_switched, oos_immature, no_actionable_proposal, drift_capped, score_gate_blocked, budget_exceeded, gpt_vetoed, applied, rolled_back, error}.
  NOTE `observe_only:False` is correct and intentional here — this is the one sanctioned mutating path; it is gated, not observing.
- helpers: `_kill_switched()`, `_actionable_proposals(proposals)`, `_gpt_approve(item, *, provider, model, approver) -> dict`, `_write_approval(...)`, `_audit(base_dir, entry)`, `_load_state/_save_state(state_path)`.

Implement the gate sequence exactly as the spec's pseudocode (G0..G7 → write approval → apply → post-gate → rollback-or-applied → audit). Every gate fail-closed; the whole function try/excepted → `{status:"error"}`. The GPT call wrapped so any exception/empty/non-JSON/`within_bounds!=true` → veto. `approver` injectable (signature `approver(prompt:str)->str`); default builds the strict prompt + calls `agent.llm_adapters.call_provider` inside `portfolio_automation.ai_budget.with_ai_budget`.

Tests (all on tmp registry copy; assert real registry untouched): disabled→disabled+no write; enabled+immature→oos_immature; kill-switch file→kill_switched; kill-switch env→kill_switched; all-pass+approver=approve→applied (+weight changed, approval written, audit entry); approver=veto→gpt_vetoed (registry byte-identical); pre-gate RED (monkeypatch)→score_gate_blocked; post-gate GREEN-then-RED (monkeypatch)→rolled_back (registry restored); drift cap exceeded→drift_capped; approver raises→fail-closed no-op.

- [ ] Write tests → run → fail (ImportError).
- [ ] Implement module → run → pass; `py_compile`; commit.

### Task E2: run_loop inert integration
**Files:** Modify `backtesting/run_loop.py`; Test extend `tests/test_run_loop.py`
- [ ] Test: `out["auto_apply"]["status"] == "disabled"` by default.
- [ ] Implement: `_auto_apply_enabled()` reads `config.json` `backtesting.auto_apply.enabled` (default False); after the D block (non-blocking try/except) call `maybe_auto_apply(enabled=_auto_apply_enabled(), poc=poc, proposals=proposals, registry_path=registry_path, base_dir=base_dir, write=write)`; add `"auto_apply": auto` to the ok dict.
- [ ] Run → pass; commit.

### Task E3: backtest_health flags + skills
**Files:** Modify `backtesting/backtest_health.py`, `.claude/commands/monthly-tool-analysis.md`, `.claude/commands/daily-tool-analysis.md`; Test extend `tests/test_backtest_health.py`
- [ ] Test: audit fixture last status `rolled_back`→RED `auto_apply_rolled_back`; `applied`→AMBER `auto_apply_active`; absent→neither.
- [ ] Implement: add `auto_apply_audit_path="outputs/policy/auto_apply_audit.json"` param; read last entry; set flags + `details["auto_apply"]`.
- [ ] Skills: read the audit; report last status; dispatch backtest-health + attribution-analyst on rolled_back/applied.
- [ ] Run → pass; commit.

### Task E4: config + CLAUDE.md invariant amendment + docs + full suite
**Files:** `config.json`, `CLAUDE.md`, `docs/PATTERN_LOOP_AUTO_APPLY.md` (new), `docs/CHANGELOG_DECISIONS.md`, `.agent/project_state.yaml`
- [ ] `config.json`: add `"backtesting": {"auto_apply": {"enabled": false, "max_monthly_drift": 0.10, "max_abs_delta": 0.05}}` (merge into existing structure; do not clobber).
- [ ] `CLAUDE.md`: under Protected Semantics / Observe-Only, add the scoped sanction: "Auto-apply (`backtesting/auto_apply.py`) MAY, when `enabled=true`, author `config/approved_weight_changes.json` and invoke the registry apply — gated by deterministic gates + a GPT approver (veto/approve-bounded only) + pre/post score-invariance gate + kill-switch + audit + auto-rollback. This is the single operator-approved exception (2026-06-05) to owner-gated Step 5; `enabled` defaults false and cannot fire until OOS maturity. Health agents must NOT flag the auto-apply path as an observe-only violation."
- [ ] `docs/PATTERN_LOOP_AUTO_APPLY.md`: document gates, kill-switch (`config/auto_apply.DISABLED` / `STOCKBOT_AUTO_APPLY_DISABLED=1`), rollback (`registry_apply.revert_last`), audit path, activation runbook.
- [ ] CHANGELOG entry (area: scoring/architecture — invariant change, inert).
- [ ] `.agent/project_state.yaml` note (next_official_step unchanged).
- [ ] `/opt/stockbot/.venv/bin/python -m pytest -q` → all pass; commit.

## Self-Review
Spec gates G0–G7 → E1 tests one-per-gate. observe_only:False intentional + documented. Reuses registry_apply (reversible) + score_invariance_gate; LLM injected in tests. Inert default + OOS gate ⇒ no real-world effect until activated. CLAUDE.md amended so health agents don't fight it. Names consistent: `maybe_auto_apply`, statuses, `auto_apply_audit.json`, flags `auto_apply_rolled_back`/`auto_apply_active`.

## Production boundary (operator go-ahead — EXTRA gated)
Beyond merge-to-main: activation also requires flipping `config.json backtesting.auto_apply.enabled=true` AND removing the kill-switch — both deliberate operator acts, and moot until OOS matures (~2027).
