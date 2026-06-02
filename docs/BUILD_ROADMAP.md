# Build Roadmap — What's Left & Operating Cadence

**Date:** 2026-06-01 · A single map of what's done, what's in flight, what's left
to build, and the daily/monthly/yearly rhythm that keeps the system healthy.
Grounded in `.agent/project_state.yaml`, `docs/roadmap.md`, the audit, and the
plans produced alongside this file.

Legend: ✅ done · 🔧 in progress · 🔲 not started · 🔒 protected (needs owner
approval) · ⏸ deferred by design.

---

## 1. Where the system is today

The core is **feature-complete and operating** (`next_official_step:
observe_and_iterate`). Mature and in place: the deterministic decision engine,
signal scanner + scoring, conviction/allocation overlays, output-namespace
governance, the two-lane (official/research) model, discovery engine, FMP news
intelligence, confidence calibration, the agent-orchestration layer, the
**doc-audit system** (just landed), the `gui_v2` read-only cockpit, and a solid
operator tool belt (`tools.status`, `smoke_test`, `env_check`, `backup_portfolio_db`, …).

What remains is mostly **evidence, hardening, finishing migrations, and the
operating cadence** — not new core features.

---

## 2. What's left to build

### A. Learning / pattern-improvement loop 🔲 (Step 5 🔒)
Turn the observe-only POC into a real loop on your history. Spec:
[`PATTERN_LOOP_IMPLEMENTATION_SPEC.md`](PATTERN_LOOP_IMPLEMENTATION_SPEC.md)
(Steps 0–4 observe-only; Step 5 applies weights and is protected).
Prompt: `.agent/task_templates/learning_loop_prompt.md`.

### B. Production hardening 🔲
Work the prioritized list in [`TECH_DEBT_AUDIT.md`](TECH_DEBT_AUDIT.md): verify
empty-DB producers, tame the silent `except` sites (start `gui_operator_data.py`),
finish the `v2-data-governance` writer migration (3 writers), add tests to the
largest untested modules. Prompt: `.agent/task_templates/production_hardening_prompt.md`.

### C. Finish the `gui_v2` migration 🔧
Read pages are migrated; the **write** pages still live in legacy Streamlit
(`gui/app.py`, 7,181 LOC). Remaining per `project_state.gui_v2.remaining_streamlit_pages_writes`:
`run_controls`, `config_editor`, `prompts`, `watchlist_manager_add_remove_import`,
`outputs_download_preview`. Finishing these retires the biggest file in the repo.

### D. Multi-user readiness 🔲 (`v2-user-scope`)
Two aggregate queries need `user_id` filtering before multi-user is safe:
`state_store.py:444`, `policy_evaluator/outcome_attributor.py:360`.

### E. Data substrate — fundamentals collector ⏸ (deferred by design)
`weekend_historical_backfill_collector` is live; quarterly **fundamentals**
collection is intentionally deferred until a *consumer* exists (e.g., a
financial-health filter for discovery, or a conviction signal). Don't pre-build.

### F. Read-only-ops end state 🔲 (operating milestone)
Flip the VPS from `dev_on_vps` to the locked `read_only_ops` mode once the
advisory layers are confirmed stable (`CLAUDE.md`, `docs/CLAUDE_VPS_MODES.md`).

### G. Open defects 🔧
Tracked in `docs/roadmap.md` "Known Issues" and
`project_state.open_defects_surfaced`. Keep draining these.

---

## 3. Operating cadence — recommended agents per interval

The cadence harnesses are `.claude/commands/{daily,monthly,yearly}-tool-analysis.md`;
they dispatch the `portfolio-*` agents. Current wiring + recommended additions:

| Cadence | Dispatches today | Lens coverage | Recommended additions |
|---|---|---|---|
| **Daily** | attribution-analyst, resolver-investigator, render-reviewer, memo-reviewer, learning-loop-health, discovery-health, **+ doc-audit skill** | Dev, Quant, Market | **Add `portfolio-test-reviewer`** (Dev lens — test/coverage health is currently not on any cadence) |
| **Monthly** | memo-reviewer, learning-loop-health, doc-writer, discovery-health, attribution-analyst, **+ doc-audit-monthly** | Dev, Quant, Process, Market | Confirm `portfolio-test-reviewer` runs at least here if not daily |
| **Yearly** | + architect (architecture review) | All four | **Add `portfolio-backtest-health`** (Quant lens — pairs with the learning-loop harness; doesn't exist yet) |

**The four lenses** (from `CLAUDE.md`) and their agents:
- **Developer** — resolver-investigator, **test-reviewer**, render-reviewer, discovery-health, doc-auditor
- **Quant** — attribution-analyst, learning-loop-health, *(backtest-health — to build)*
- **Process** — learning-loop-health
- **Market** — memo-reviewer, attribution-analyst

**Two concrete gaps to close** (both via `analysis_cadence_prompt.md`):
1. `portfolio-test-reviewer` exists but no cadence dispatches it → wire it in.
2. No `portfolio-backtest-health` agent yet → create it when the learning loop
   ships, and dispatch it yearly. This is also the analysis-health *pairing*
   that `CLAUDE.md` requires for the new POC harness.

---

## 4. The continuous-improvement rhythm

Once the loop exists, the steady-state cycle is:

```
   daily/monthly outcome tracking  →  calibration + attribution review
            │                                   │
            ▼                                   ▼
   per-pattern efficacy (backtest)  →  tuning PROPOSALS (observe-only)
                                              │
                                       owner approval 🔒
                                              ▼
                                   apply weight changes (audited, reversible)
```

Everything left of the approval gate runs automatically and safely; only the
apply step touches protected scoring. Prompt:
`.agent/task_templates/continuous_improvement_prompt.md`.

---

## 5. Upkeep / housekeeping

Routine health that prevents slow rot: dependency drift, dead code, the
empty-DB producer check, output-namespace hygiene, cron/run-health, log noise,
and running the doc-audit. Prompt: `.agent/task_templates/upkeep_prompt.md`.

---

## 6. Task-prompt backlog (paste into Claude Code)

All live in `.agent/task_templates/` — open one, copy the fenced block, paste
into Claude Code. Each plans first, waits for approval, and goes one step at a time.

| Prompt | Purpose | Lens | Touches protected? |
|---|---|---|---|
| `learning_loop_prompt.md` | Build the pattern-improvement loop (spec Steps 0–4) | Quant | Stops before Step 5 |
| `production_hardening_prompt.md` | Work the tech-debt audit toward production-ready | Developer | No |
| `doc_cleanup_prompt.md` | Curate/retire stale docs using the doc-audit findings | Process | No |
| `analysis_cadence_prompt.md` | Wire test-reviewer + build backtest-health; verify cadences | Dev/Quant | No |
| `continuous_improvement_prompt.md` | Stand up the outcomes→calibration→proposal rhythm | Quant/Process | Proposes only |
| `upkeep_prompt.md` | Routine maintenance / housekeeping pass | Developer | No |

---

## 7. Suggested sequence

1. **Upkeep pass** (`upkeep_prompt.md`) + **Step 0 baseline run** — cheap, and it
   produces the real artifacts everything else needs.
2. **Production hardening** items 1–4 (`production_hardening_prompt.md`) — get a
   safety net and kill silent-failure risk first.
3. **Learning loop Steps 1–4** (`learning_loop_prompt.md`) — build the evidence
   layer on real data; stop at the protected gate.
4. **Analysis cadence** (`analysis_cadence_prompt.md`) — wire test-reviewer and
   the new backtest-health so the loop is monitored.
5. **Doc cleanup** (`doc_cleanup_prompt.md`) — run after the docs have churned.
6. **Continuous improvement** (`continuous_improvement_prompt.md`) — turn it on
   as the steady state; bring Step 5 (apply) to you for approval when the
   evidence is strong.
7. Finish **gui_v2 write pages** and, when stable, flip to **read_only_ops**.

---

## 8. Boundaries (unchanged)

No broker/execution. No changes to `decision_engine.py`, `scoring.py`,
`allocation_engine.py`, or the six protected scores without explicit owner
approval. New layers are additive, `observe_only: true`, and write only to their
declared namespace. Replay/backtest never writes to `outputs/latest/`.

> Related: [`ARCHITECTURE_MAP.md`](ARCHITECTURE_MAP.md) ·
> [`TECH_DEBT_AUDIT.md`](TECH_DEBT_AUDIT.md) ·
> [`PRODUCTION_READINESS_PLAN.md`](PRODUCTION_READINESS_PLAN.md) ·
> [`PATTERN_LOOP_IMPLEMENTATION_SPEC.md`](PATTERN_LOOP_IMPLEMENTATION_SPEC.md)
