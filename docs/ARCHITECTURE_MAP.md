# Architecture Map

> One readable map of the Portfolio Automation System, distilled from the
> documentation set (51 docs in `docs/`, ~84 markdown files repo-wide) and the
> code. Written for a mixed audience — engineers, operators, and stakeholders.
> For the authoritative deep dives, follow the links in the
> [Documentation index](#documentation-index) at the end.

---

## 1. What this system is, in one paragraph

This is an **advisory-only** portfolio intelligence system. Every day it pulls
market data, news, and your portfolio holdings, scores what it sees, and
produces a ranked list of *suggested* actions plus a short operator brief — a
"here's what changed and what you might do about it" report. It **does not place
trades, connect to a broker, or move money.** All of the scoring, ranking, and
sizing math is plain deterministic Python (rules decide); the AI layers only
explain, validate, and summarize what the rules already decided (AI advises).
Think of it as a very disciplined research analyst that writes you a memo each
morning and never touches the "buy" button.

---

## 2. The 60-second mental model

```
   MARKET DATA          NEWS               YOUR PORTFOLIO
   (prices, fundamentals) (headlines)       (holdings, cash, 401k)
        │                   │                     │
        └─────────┬─────────┴──────────┬──────────┘
                  ▼                     ▼
            SCORING & SIGNALS     GUARDRAILS / DRIFT
       (how attractive? how       (am I overweight, over-
        trustworthy? a 0–100       concentrated, over-
        score for each idea)       leveraged, drifting?)
                  │                     │
                  └──────────┬──────────┘
                             ▼
                     DECISION ENGINE
            (combine everything, rank it, and write
             ONE source-of-truth action plan)
                             │
                             ▼
                  decision_plan.json   ◄── the single source of truth
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                     ▼
   DAILY MEMO            OPERATOR GUI          EMAIL DIGEST
   (compact brief)     (read-only cockpit)    (only when it matters)
                             │
                             ▼
                    OUTCOME TRACKING & LEARNING
        (was the call right after 1/3/7 days? recalibrate confidence)
```

The golden rule of the architecture: **the decision plan is computed once, in
the core decision layers, and everything downstream only reads it.** The memo,
the GUI, and the email never recompute decisions — they just present what the
decision engine already produced.

---

## 3. The layers, in plain language

The system is organized as a pipeline of layers. Each has a single job and
hands off to the next.

**Data ingestion** — Fetches prices, fundamentals, and news. The primary market
data source today is **Financial Modeling Prep (FMP)**, accessed through a single
gated client (`fmp_client.py`) that caches results, respects a daily call budget,
and rate-limits requests. (Alpha Vantage is also supported for core price fetch.)
Every FMP endpoint must be pre-registered for compliance — see
[FMP_COMPLIANCE.md](FMP_COMPLIANCE.md).

**Portfolio & guardrails** — Computes your holdings, allocations, drift from
target weights, and drawdown regime, then checks structural safety rules
(concentration caps, leverage caps). This is where "VFH is 15% overweight" or
"leveraged exposure too high" comes from.

**Scoring & signals** — Turns raw data into numbers. A watchlist scanner and the
finance scorer produce a family of 0–100 scores (explained in
[section 5](#5-the-scores-explained-simply)) plus typed signals like
`STRONG_MOVE_UP`, `VOLUME_SPIKE`, or `BREAKOUT_PROXY` (catalogued in
`config/signal_registry.yaml`).

**Decision engine** — The brain. It consolidates four streams — structural
violations, portfolio adjustments, finance recommendations, and
watchlist/market opportunities — ranks them, and writes the **single
source-of-truth** artifact `outputs/latest/decision_plan.json`. This logic is
**protected** (see [section 7](#7-guardrails--governance)).

**Advisory outputs** — The consumer layers. A **daily memo** (a deliberately
compact brief: max 5 decisions, 3 risks, 3 changes), an **email digest** that
only sends when thresholds are met (anti-spam), and a **read-only operator GUI**
("Decision Center" cockpit). None of these change decisions.

**Learning loop** — Closes the feedback cycle. An outcome tracker resolves each
decision after 1, 3, and 7 days (was the direction right? what was the return?),
and a confidence-calibration layer checks whether high-confidence calls actually
did better than low-confidence ones, feeding tuning suggestions. ML *advises*;
it never predicts prices or overrides rules.

**Discovery engine (research lane)** — A sandbox that mines news for brand-new
candidate ideas and labels them WATCH / DISCOVERED / REJECTED. Its outputs are
flagged research-only and are **never** promoted into official recommendations
automatically.

**Governance** — The rails that keep the lanes separate: output namespacing,
the two-lane (official vs. research) operating model, FMP endpoint compliance,
and the "observe-only" default for new layers.

---

## 4. A day in the life (the pipeline)

The daily run is a multi-stage "safe wrapper" — each stage is wrapped so that a
failure in a later, non-critical stage doesn't crash the core decision. Roughly:

1. **News intelligence** (pre-pipeline) — ingest and classify headlines.
2. **Core pipeline** (fail-fast) — portfolio + guardrails + scanner + decision
   engine → write `decision_plan.json` / `.md`.
3. **Weight tuning & policy evaluation** — score recent recommendation quality.
4. **Allocation preview** — observe-only sizing suggestions.
5. **System summary** — roll-up for the memo and GUI.
6. **Observability** — risk-delta monitor, retune-impact tracker, FMP budget
   telemetry.
7. **Discovery integration** — fold in research-lane candidates (research
   namespace only).
8. **Daily memo + email** — write the compact brief; send only if it clears the
   anti-spam thresholds.
9. **Daily run status** — an overall health report for the run.

Entry point: `main.py --run-mode daily|weekly|monthly`. A file lock prevents
overlapping runs; the full stage list lives in
[PIPELINE_RUNBOOK.md](PIPELINE_RUNBOOK.md).

---

## 5. The scores, explained simply

The system protects six named scores. Their meanings must stay stable so that
the memo, GUI, and history all mean the same thing over time.

| Score | Plain-English question it answers |
|---|---|
| **signal_score** | *How attractive is this idea?* (raw appeal, before any trust or sizing adjustment) |
| **confidence_score** | *How much can we trust the data behind it?* (freshness, completeness — **not** how good the idea is) |
| **effective_score** | *How actionable is it right now?* (signal + confidence + filters/cooldown) |
| **conviction_score** | *How strongly should we lean in?* (maps to bands: defer / observe / starter / normal / high-conviction) |
| **final_rank_score** | *Where does it sit in today's priority order?* |
| **recommendation_score** | *For a whole policy profile, how strong is the advisory case?* (separate from per-idea signal_score) |

A key subtlety the docs stress: **confidence is about data trust, not
attractiveness.** A great-looking idea built on stale data should score high on
signal and low on confidence. See
[SCORING_AND_CONFIDENCE.md](SCORING_AND_CONFIDENCE.md) and
[CONFIDENCE_CALIBRATION.md](CONFIDENCE_CALIBRATION.md).

---

## 6. Where the outputs live (namespaces)

All writes go through a governed namespace system
(`portfolio_automation/data_governance.py`) so that live, research, and
historical outputs never contaminate each other.

| Namespace | Folder | Holds | Who writes it |
|---|---|---|---|
| **LATEST** | `outputs/latest/` | Today's decision plan, memo, validation, status | Live pipeline only |
| **POLICY** | `outputs/policy/` | Outcome history, evaluations, calibration, budget/audit | Governance & learning layers |
| **PORTFOLIO** | `outputs/portfolio/` | Portfolio snapshots | Pipeline |
| **SANDBOX** | `outputs/sandbox/` | Discovery research candidates | Research lane only (never official) |
| **HISTORICAL** | `outputs/backtest/` | Replay & backtest results | Offline analysis only (never the live pipeline) |

The cardinal rule: **the live pipeline never writes to the historical namespace,
and replay/backtest never writes to the live namespace.** See
[DATA_GOVERNANCE.md](DATA_GOVERNANCE.md) and
[RUN_MODE_GOVERNANCE.md](RUN_MODE_GOVERNANCE.md).

---

## 7. Guardrails & governance

This system is deliberately constrained. The constraints *are* the design.

- **Advisory-only.** No broker integration, no execution, no auto-trading —
  ever. (`CLAUDE.md`, `AGENTS.md`)
- **Protected logic.** The six scores above and the decision/scoring/allocation
  logic don't change without explicit owner approval.
- **One source of truth.** `outputs/latest/decision_plan.json` is *the*
  decision; downstream layers are consumers only.
- **Two lanes.** "Official" (daily/weekly/monthly) and "research"
  (discovery/backtest) are kept strictly separate by namespace and run mode.
- **Observe-only by default.** New observability layers ship with
  `observe_only: true` hardcoded and wrapped in `try/except` so they can't break
  the pipeline.
- **FMP compliance.** Every external endpoint is pre-registered; caching and
  budget guardrails apply to all callers.
- **Graceful degradation.** When data is stale or an API fails, the system
  enters a *degraded mode* that lowers certainty and caps risky suggestions
  rather than inventing conviction.

The system also runs in two operating environments — an operator laptop (full
dev access) and a production VPS that can be switched between `dev_on_vps` and a
locked `read_only_ops` mode. See `CLAUDE.md` and
[CLAUDE_VPS_MODES.md](CLAUDE_VPS_MODES.md).

---

## 8. How the agent orchestration layer fits

The repo is built feature-by-feature against a tracked roadmap in `.agent/`.
`project_state.yaml` records the current phase
(`post_phase_0_governance`) and the authoritative
**`next_official_step`** (currently `observe_and_iterate` — i.e., the system is
feature-complete enough to run day-to-day and accumulate outcome history). A
companion AI ("Codex") handles documentation, review, and changelog upkeep
*after* features are built. See
[AGENT_OPERATING_MODEL.md](AGENT_OPERATING_MODEL.md).

---

## 9. Documentation index

The docs cluster into seven themes. Start with the bold ones.

**Core architecture & flow**
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the canonical architecture
- [system_workflow.md](system_workflow.md), **[PIPELINE_RUNBOOK.md](PIPELINE_RUNBOOK.md)**,
  [OUTPUT_ARTIFACT_CONTRACTS.md](OUTPUT_ARTIFACT_CONTRACTS.md), [STATE_SCHEMA.md](STATE_SCHEMA.md)

**Decisions, scoring & allocation**
- **[decision_engine.md](decision_engine.md)**, [SCORING_AND_CONFIDENCE.md](SCORING_AND_CONFIDENCE.md),
  [SIGNAL_REGISTRY.md](SIGNAL_REGISTRY.md), [ALLOCATION_POLICY.md](ALLOCATION_POLICY.md),
  [CONFIDENCE_CALIBRATION.md](CONFIDENCE_CALIBRATION.md), [PNL_ADVISORS.md](PNL_ADVISORS.md)

**Data, news & market**
- [DATA_AND_FMP_ENDPOINTS.md](DATA_AND_FMP_ENDPOINTS.md), **[FMP_COMPLIANCE.md](FMP_COMPLIANCE.md)**,
  [fmp_endpoint_inventory.md](fmp_endpoint_inventory.md), [NEWS_INTELLIGENCE.md](NEWS_INTELLIGENCE.md),
  [NEWS_EVIDENCE_LAYER.md](NEWS_EVIDENCE_LAYER.md), [MARKET_NARRATIVES.md](MARKET_NARRATIVES.md),
  [DATA_QUALITY_MONITOR.md](DATA_QUALITY_MONITOR.md)

**Outputs, memo & GUI**
- **[daily_memo.md](daily_memo.md)**, [gui_decision_center.md](gui_decision_center.md),
  [GUI_OPERATOR_COCKPIT.md](GUI_OPERATOR_COCKPIT.md), [ALERT_LIFECYCLE.md](ALERT_LIFECYCLE.md),
  [AI_VALIDATION_LAYER.md](AI_VALIDATION_LAYER.md)

**Learning, discovery & feedback**
- [EVALUATION_AND_LEARNING_LOOP.md](EVALUATION_AND_LEARNING_LOOP.md), [FEEDBACK_LOOP.md](FEEDBACK_LOOP.md),
  **[DISCOVERY_ENGINE.md](DISCOVERY_ENGINE.md)**, [DISCOVERY_NEWS_INTEGRATION.md](DISCOVERY_NEWS_INTEGRATION.md),
  [AUTOMATIC_PROMOTION_GOVERNANCE.md](AUTOMATIC_PROMOTION_GOVERNANCE.md),
  [HISTORICAL_REPLAY_BACKTEST.md](HISTORICAL_REPLAY_BACKTEST.md), [learning_loop_plan.md](learning_loop_plan.md)

**Governance & operations**
- **[DATA_GOVERNANCE.md](DATA_GOVERNANCE.md)**, [RUN_MODE_GOVERNANCE.md](RUN_MODE_GOVERNANCE.md),
  [AGENT_OPERATING_MODEL.md](AGENT_OPERATING_MODEL.md), [CLAUDE_AGENT_RULES.md](CLAUDE_AGENT_RULES.md),
  [CLAUDE_VPS_MODES.md](CLAUDE_VPS_MODES.md), [operator_runbook.md](operator_runbook.md),
  [CRON_AND_PREFLIGHT_RUNBOOK.md](CRON_AND_PREFLIGHT_RUNBOOK.md), [deployment.md](deployment.md),
  [AI_BUDGET.md](AI_BUDGET.md), [AI_COLLABORATION_RUNBOOK.md](AI_COLLABORATION_RUNBOOK.md)

**Audits, history & maintenance**
- [PRODUCTION_HARDENING_AUDIT.md](PRODUCTION_HARDENING_AUDIT.md), [REGRESSION_CHECKLIST.md](REGRESSION_CHECKLIST.md),
  [REPO_CLEANUP_AUDIT.md](REPO_CLEANUP_AUDIT.md), [CHANGELOG_DECISIONS.md](CHANGELOG_DECISIONS.md),
  [MIGRATION_NOTES.md](MIGRATION_NOTES.md), [roadmap.md](roadmap.md),
  [applied_fix_verifier.md](applied_fix_verifier.md)

> Companion documents produced alongside this map:
> [TECH_DEBT_AUDIT.md](TECH_DEBT_AUDIT.md) and
> [PRODUCTION_READINESS_PLAN.md](PRODUCTION_READINESS_PLAN.md).

---

*Glossary — Drift: how far a holding has moved from its target weight. Regime:
the current market state (e.g., normal vs. drawdown). Degraded mode: reduced-
certainty operation when data is stale or an API fails. Two-lane: the strict
separation of official decisions from research/discovery. Observe-only: a layer
that watches and reports but never changes a decision.*
