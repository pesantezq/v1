# Portfolio Automation System — Workflow + Coverage Map

**Last updated:** 2026-05-28

This document is the canonical system overview. It shows every cron-scheduled
producer in the Portfolio Automation System, the artifacts each writes, and
the analysis / agent / health-check coverage that watches each one.

Per `CLAUDE.md` "Analysis + Health Coverage Requirement": every producer
must be paired with an analysis check at the appropriate cadence (daily /
monthly / yearly), with at least one lens (developer / quant / process
analyst / market expert) owning the inspection.

---

## 1. Five operating cadences (cron map)

```mermaid
flowchart LR
    classDef daily   fill:#dbeafe,stroke:#1e40af,color:#1e3a8a
    classDef pulse   fill:#fef3c7,stroke:#92400e,color:#78350f
    classDef weekly  fill:#dcfce7,stroke:#15803d,color:#14532d
    classDef monthly fill:#f3e8ff,stroke:#7e22ce,color:#581c87
    classDef yearly  fill:#fce7f3,stroke:#be185d,color:#831843
    classDef weekend fill:#e0e7ff,stroke:#3730a3,color:#1e1b4b

    UTC[/"UTC clock"/] --> D["09:00 daily<br/>run_daily_safe.sh"]:::daily
    UTC --> DC["09:15 weekdays<br/>daily_check.sh"]:::daily
    UTC --> P1["11/15/19/23 weekday<br/>discovery_pulse.sh"]:::pulse
    UTC --> P2["12/20 weekend<br/>discovery_pulse.sh"]:::pulse
    UTC --> W["08:00 Mon<br/>run_weekly_safe.sh"]:::weekly
    UTC --> M["09:30 1st of month<br/>monthly_check.sh"]:::monthly
    UTC --> Y["10:00 Jan 1<br/>yearly_check.sh"]:::yearly
    UTC -.- WK["07:00 Sat/Sun (deferred)<br/>historical_backfill.sh"]:::weekend
```

Lock-file contention: `discovery_pulse.sh`, `run_weekly_safe.sh`, and
`historical_backfill.sh` all share `/var/lock/stockbot-discovery-pulse.lock`
(non-blocking flock) — they can't run concurrently against FMP.

---

## 2. Daily pipeline (09:00 UTC) — the heaviest cadence

```mermaid
flowchart TB
    classDef producer fill:#fff,stroke:#374151,color:#111
    classDef artifact fill:#fef3c7,stroke:#a16207,color:#451a03
    classDef analysis fill:#dcfce7,stroke:#15803d,color:#14532d

    subgraph S1["Stage 1 — preflight"]
        PF[preflight.sh<br/>env + deps check]:::producer
    end

    subgraph S2["Stage 2 — pre-pipeline news"]
        NI1[news_intelligence<br/>RSS + FMP news]:::producer
        --> NI_ART[news_intelligence.json]:::artifact
    end

    subgraph S3["Stage 3 — theme engine"]
        TE[theme_engine.daily<br/>RSS → OpenAI gpt-4o-mini]:::producer
        --> TE_ART[theme_signals.json<br/>watch_candidates.json]:::artifact
    end

    subgraph S4["Stage 4 — main.py daily pipeline"]
        MN[main.py --run-mode daily]:::producer
        --> EW[ExtendedWatchlist<br/>promotion]:::producer
        --> CS[candidate_scanner.daily<br/>quote refresh]:::producer
        --> WS[watchlist_scanner<br/>scoring]:::producer
        --> SG[signal generation]:::producer
        --> DE[decision_engine]:::producer
        --> DP_ART[decision_plan.json<br/>decision_plan.md]:::artifact
        DE --> PC[portfolio_construction]:::producer
        --> PS_ART[portfolio_snapshot.json]:::artifact
        DE --> SI[scraped_intel<br/>SEC + RSS news]:::producer
        --> SI_ART[scraped_intel_run_summary.json]:::artifact
    end

    subgraph S5["Stage 5 — outcome resolution"]
        SO[signal_outcomes resolver<br/>forward 1d/3d/7d returns]:::producer
        --> SO_ART[signal_outcomes.csv]:::artifact
    end

    subgraph S6["Stage 6 — performance + policy"]
        WT[weight_tuning]:::producer --> WT_ART[weight_tuning_suggestions.json]:::artifact
        PE[policy_evaluator]:::producer --> PE_ART[performance_summary.json]:::artifact
        AP[allocation_preview + simulation + activation]:::producer
        --> AP_ART[allocation_policy_*.json]:::artifact
        SDS[system_decision_summary]:::producer --> SDS_ART[system_decision_summary.json]:::artifact
    end

    subgraph S7["Stage 7 — observability"]
        RD[risk_delta_advisor]:::producer --> RD_ART[risk_delta.json]:::artifact
        RIT[retune_impact_tracker<br/>+ sector_composition]:::producer --> RIT_ART[retune_impact.json]:::artifact
        FBT[fmp_budget_telemetry]:::producer --> FBT_ART[fmp_budget_status.json]:::artifact
        RDP[resolution_due_probe]:::producer --> RDP_ART[decisions_due_for_resolution.json]:::artifact
        AB[ai_budget_summary]:::producer --> AB_ART[ai_budget_summary.json]:::artifact
        ADV[ai_decision_validator<br/>+ LLM enhance]:::producer --> ADV_ART[ai_decision_validation.json]:::artifact
        AA[alpha_attribution_report]:::producer --> AA_ART[alpha_attribution_report.json]:::artifact
        CR[correlation_risk_advisor]:::producer --> CR_ART[correlation_risk_advisor.json]:::artifact
        CC[confidence_calibration]:::producer --> CC_ART[confidence_calibration.json]:::artifact
    end

    subgraph S8["Stage 8 — discovery + sandbox"]
        DN[discovery_news_integration]:::producer
        AG[automatic_promotion_governance]:::producer
        --> APC_ART[automatic_promotion_candidates.json]:::artifact
        SL[sandbox lane runs]:::producer
    end

    subgraph S9["Stage 9 — memo + email"]
        DM[daily_memo + email_sender]:::producer
        --> DM_ART[daily_memo.md / .txt]:::artifact
    end

    subgraph S10["Stage 10 — final telemetry"]
        DRS[daily_run_status<br/>+ 8 content_liveness checks]:::producer
        --> DRS_ART[daily_run_status.json]:::artifact
    end

    S1 --> S2 --> S3 --> S4 --> S5 --> S6 --> S7 --> S8 --> S9 --> S10

    DRS_ART -.consumed by.-> DTA["09:15 daily-tool-analysis<br/>(daily_check.sh)"]:::analysis
```

---

## 3. Discovery pulse (4×/day weekday + 2×/day weekend)

```mermaid
flowchart TB
    classDef producer fill:#fff,stroke:#374151,color:#111
    classDef artifact fill:#fef3c7,stroke:#a16207,color:#451a03
    classDef gate fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d

    LK[acquire /var/lock/stockbot-discovery-pulse.lock]:::gate
    --> CAPS{evaluate_caps<br/>$20/mo OpenAI · 5000/mo FMP<br/>8/day theme · 6/day scrape}:::gate
    CAPS -- skip --> SKIP[record skip + exit]:::gate
    CAPS -- ok --> TA[Tier A: theme_engine.daily]:::producer
    TA --> TA_ART[theme_signals.json<br/>watch_candidates.json]:::artifact
    TA --> PROMO[ExtendedWatchlist promotion<br/>≥2 themes OR direct]:::producer
    PROMO --> EW_DB[(data/portfolio.db<br/>extended_watchlist)]:::artifact
    PROMO --> UNI[resolve dynamic universe<br/>static ∪ extended ∪ candidates<br/>cap 50]:::producer
    UNI --> TB[Tier B: scraped_intel.pipeline<br/>SEC EDGAR + RSS news]:::producer
    TB --> TB_ART[evidence rows in DB]:::artifact
    TB --> SAN[universe_sanitation.daily<br/>5 source dimensions]:::producer
    SAN --> SAN_ART[top100_daily.json<br/>+ rationale_tags per row]:::artifact
    SAN --> STATUS[discovery_pulse_status.json]:::artifact
```

---

## 4. Weekly chain (Monday 08:00 UTC)

```mermaid
flowchart TB
    classDef producer fill:#fff,stroke:#374151,color:#111
    classDef artifact fill:#fef3c7,stroke:#a16207,color:#451a03
    classDef gate fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d

    PF[preflight.sh]:::producer
    --> WK[main.py --run-mode weekly<br/>FMP scoring of S&P 500]:::producer
    --> TOP[top100_watchlist.json<br/>real FMP scores]:::artifact
    --> SAN_W[universe_sanitation weekly]:::producer
    --> TOP_W[top100_weekly.json]:::artifact
    SAN_W --> SAN_M[universe_sanitation monthly<br/>30-day rolling]:::producer
    --> TOP_M[top100_monthly.json]:::artifact
    SAN_M --> PL_W[pattern_learning weekly<br/>n≥30, Wilson CI]:::producer
    --> PE_W[pattern_efficacy_weekly.json]:::artifact
    PL_W --> PL_M[pattern_learning monthly]:::producer
    --> PE_M[pattern_efficacy_monthly.json]:::artifact
    PL_M --> PL_Y[pattern_learning yearly<br/>partition by gauge × regime]:::producer
    --> PE_Y[pattern_efficacy_yearly.json]:::artifact
    PL_Y --> RS[retune_suggestions<br/>weight + threshold proposals]:::producer
    --> RS_ART[gate_retune_suggestions.json]:::artifact
    RS --> AAP{retune_auto_apply<br/>6 guardrails}:::gate
    AAP -- queued --> STATE1[(state.pending_confirmations)]:::artifact
    AAP -- applied --> CFG[(config.json mutated)]:::artifact
    AAP -- applied --> AUDIT[(retune_audit_log.jsonl)]:::artifact
    AAP -- skipped --> NOOP[no change<br/>reason logged]:::gate
```

---

## 5. Monthly + Yearly (multi-lens retrospectives)

```mermaid
flowchart LR
    classDef cron fill:#dbeafe,stroke:#1e40af,color:#1e3a8a
    classDef skill fill:#dcfce7,stroke:#15803d,color:#14532d
    classDef lens fill:#fef3c7,stroke:#a16207,color:#451a03

    MC["1st of month<br/>monthly_check.sh"]:::cron
    --> MS["/monthly-tool-analysis<br/>(reads 30-day history)"]:::skill
    MS --> L1["Developer<br/>cron health, apply rate"]:::lens
    MS --> L2["Quant<br/>tag efficacy drift"]:::lens
    MS --> L3["Process<br/>drift cap, pending queue"]:::lens
    MS --> L4["Market<br/>memo hit-rate, sector rotation"]:::lens
    MS --> MR[docs/monthly_reports/YYYY-MM.md]

    YC["Jan 1<br/>yearly_check.sh"]:::cron
    --> YS["/yearly-tool-analysis<br/>(reads 365-day + lifetime)"]:::skill
    YS --> L1y["Developer<br/>silent_failures, cron_uptime"]:::lens
    YS --> L2y["Quant<br/>regime × tag matrix, gauge era"]:::lens
    YS --> L3y["Process<br/>audit consistency, rollbacks"]:::lens
    YS --> L4y["Market<br/>discovery yield, sector perf"]:::lens
    YS --> YR[docs/yearly_reports/YYYY.md]
```

---

## 6. Weekend backfill (deferred, ≥2026-06-04)

```mermaid
flowchart LR
    classDef producer fill:#fff,stroke:#374151,color:#111
    classDef artifact fill:#fef3c7,stroke:#a16207,color:#451a03
    classDef deferred fill:#e5e7eb,stroke:#6b7280,color:#374151,stroke-dasharray: 5 5

    SAT["Sat/Sun 07:00 UTC<br/>(cron not installed yet)"]:::deferred
    --> HB[historical_backfill]:::producer
    HB --> UNI[build_universe<br/>33 tickers]:::producer
    UNI --> FETCH[per-ticker:<br/>get_historical_prices years=5]:::producer
    FETCH --> ARCH[outputs/backtest/historical/<br/>TICKER_5y.json]:::artifact
    FETCH --> STAT[outputs/latest/<br/>historical_backfill_status.json]:::artifact
```

---

## 7. The four analytical lenses

Every analysis check belongs to one or more of these lenses. New checks
should pick a lens explicitly per CLAUDE.md's coverage requirement.

```mermaid
flowchart TB
    classDef lens fill:#f3e8ff,stroke:#7e22ce,color:#581c87
    classDef agent fill:#dcfce7,stroke:#15803d,color:#14532d

    DEV[Developer lens<br/>cron, errors, deps, silent zeros]:::lens
    --> RI[portfolio-resolver-investigator]:::agent
    DEV --> TR[portfolio-test-reviewer]:::agent
    DEV --> RR[portfolio-render-reviewer]:::agent
    DEV --> DH[portfolio-discovery-health]:::agent

    QUANT[Quant lens<br/>hit-rate, Sharpe, attribution]:::lens
    --> AA[portfolio-attribution-analyst]:::agent
    QUANT --> LLH[portfolio-learning-loop-health]:::agent

    PROC[Process analyst lens<br/>workflow, audit, drift]:::lens
    --> LLH

    MKT[Market expert lens<br/>sectors, regime, memo accuracy]:::lens
    --> MR[portfolio-memo-reviewer]:::agent
    MKT --> AA
```

---

## 8. Coverage matrix — every producer × agent × health check

The big table. Sorted by cadence. **An empty cell means coverage is missing
and is a debt item** per CLAUDE.md.

### Daily-cadence producers (Stage 1–10 of daily pipeline)

| Producer | Artifact (latest) | Liveness check | Dispatched agent (daily) |
|---|---|---|---|
| `preflight.sh` | — (logs only) | log scan | resolver-investigator (on RED) |
| `news_intelligence` | `news_intelligence.json` | `news_intelligence.article_count_raw` | discovery-health |
| `theme_engine.daily` | `theme_signals.json`, `watch_candidates.json` | `theme_signals.themes` | discovery-health |
| `ExtendedWatchlist.evaluate_candidates` | `extended_watchlist` DB | (inferred via discovery-health DB query) | discovery-health |
| `candidate_scanner.daily` | `top100_watchlist.json` mtime check | (via scraped_intel.degraded_mode warn) | discovery-health |
| `watchlist_scanner` | `candidates_top20.csv` | — | memo-reviewer (indirect) |
| `decision_engine` | `decision_plan.json`, `.md` | (required-artifact freshness check) | memo-reviewer, attribution-analyst |
| `portfolio_construction` | `portfolio_snapshot.json` | (required-artifact freshness check) | memo-reviewer |
| `scraped_intel.pipeline` | `scraped_intel_run_summary.json` | `scraped_intel.degraded_mode` | discovery-health |
| `signal_outcomes` resolver | `signal_outcomes.csv` | (via resolution_due_probe.stuck_count) | resolver-investigator |
| `weight_tuning` | `weight_tuning_suggestions.json` | — | (none — debt) |
| `policy_evaluator` | `performance_summary.json` | — | (none — debt) |
| `allocation_preview/sim/activation` | `allocation_policy_*.json` | — | (none — debt) |
| `system_decision_summary` | `system_decision_summary.json` | (required-artifact freshness check) | memo-reviewer |
| `risk_delta_advisor` | `risk_delta.json` | (required-artifact freshness check) | resolver-investigator |
| `retune_impact_tracker` | `retune_impact.json` (+ sector_composition) | (via attribution-analyst dispatch) | **attribution-analyst** |
| `fmp_budget_telemetry` | `fmp_budget_status.json` | (via budget.status field) | discovery-health |
| `resolution_due_probe` | `decisions_due_for_resolution.json` | `stuck_count` threshold | resolver-investigator |
| `ai_budget_summary` | `ai_budget_summary.json` | `ai_budget.event_count` | discovery-health |
| `ai_decision_validator` | `ai_decision_validation.json` (LLM enhanced) | — | memo-reviewer (indirect) |
| `alpha_attribution_report` | `alpha_attribution_report.json` | — | (none — debt) |
| `correlation_risk_advisor` | `correlation_risk_advisor.json` | — | (none — debt) |
| `confidence_calibration` | `confidence_calibration.json` | — | (none — debt) |
| `automatic_promotion_governance` | `automatic_promotion_candidates.json` | — | discovery-health (indirect) |
| `daily_memo + email_sender` | `daily_memo.md`, `daily_memo.txt` | (required-artifact freshness) | **memo-reviewer (ALWAYS)** |
| `daily_run_status` | `daily_run_status.json` (8 liveness checks inside) | **self** | resolver-investigator |

### Discovery-pulse cadence (4×/day weekday + 2×/day weekend)

| Producer | Artifact (latest) | Liveness check | Dispatched agent |
|---|---|---|---|
| `discovery_pulse` orchestrator | `discovery_pulse_status.json` | `discovery_pulse.last_run_age`, `discovery_pulse.monthly_cap_status` | discovery-health |
| Tier A: `theme_engine + promotion` | (overwrites `theme_signals.json`) | `theme_signals.themes` | discovery-health |
| Tier B: `scraped_intel` over dynamic union | (DB writes) | `scraped_intel.degraded_mode` | discovery-health |
| Final: `universe_sanitation.daily` | `top100_daily.json` (+ rationale_tags) | `universe_sanitation.top100_daily` | discovery-health |

### Weekly cadence (Monday 08:00 UTC)

| Producer | Artifact | Liveness check | Dispatched agent |
|---|---|---|---|
| `main.py --run-mode weekly` (FMP scoring) | `top100_watchlist.json` (real scores) | (via scraped_intel.degraded_mode clear) | discovery-health |
| `universe_sanitation` weekly | `top100_weekly.json` | (via daily check on top100_daily) | discovery-health |
| `universe_sanitation` monthly | `top100_monthly.json` | (consumed by monthly-tool-analysis) | (monthly cadence) |
| `pattern_learning` weekly | `pattern_efficacy_weekly.json` | (via match_rate in monthly check) | **learning-loop-health** |
| `pattern_learning` monthly | `pattern_efficacy_monthly.json` | — | **learning-loop-health** |
| `pattern_learning` yearly | `pattern_efficacy_yearly.json` (partitioned) | — | (yearly cadence) |
| `retune_suggestions` | `gate_retune_suggestions.json` | (`auto_applicable_count` threshold) | **learning-loop-health** |
| `retune_auto_apply` | `retune_audit_log.jsonl`, config.json mutation | (drift cap + rollback rate) | **learning-loop-health** |

### Weekend cadence (deferred; activation ≥2026-06-04)

| Producer | Artifact | Liveness check | Dispatched agent |
|---|---|---|---|
| `historical_backfill` | `outputs/backtest/historical/*_5y.json`, `historical_backfill_status.json` | `historical_backfill.last_run` | discovery-health |

### Monthly retrospective cadence (1st of month 09:30 UTC)

| Producer | Artifact | Inputs |
|---|---|---|
| `/monthly-tool-analysis` skill | `docs/monthly_reports/YYYY-MM.md` | pattern_efficacy_monthly + audit log + 30d archives |
| Always-dispatched: `portfolio-doc-writer` | `.agent/project_state.yaml` (roadmap touch) | the month's shipped features |
| Threshold: `portfolio-learning-loop-health` | (audit) | rollback ratio, drift cap, tag drift |
| Threshold: `portfolio-attribution-analyst` | (audit) | fingerprint changes, memo hit-rate dip |
| Threshold: `portfolio-memo-reviewer` | (audit) | memo decision quality |
| Threshold: `portfolio-discovery-health` | (audit) | discovery yield, pulse skip rate |

### Yearly retrospective cadence (Jan 1 10:00 UTC)

| Producer | Artifact | Inputs |
|---|---|---|
| `/yearly-tool-analysis` skill | `docs/yearly_reports/YYYY.md` | 12 monthly reports + pattern_efficacy_yearly + lifetime audit |
| Always-dispatched: `portfolio-attribution-analyst` | (audit) | lifetime tag × regime matrix |
| Always-dispatched: `portfolio-learning-loop-health` | (audit) | audit log consistency, rollback clusters |
| Always-dispatched: `portfolio-architect` | `.agent/project_state.yaml` update | next year's roadmap, debt items |
| Always-dispatched: `portfolio-doc-writer` | architecture + decision_engine docs touch | major shifts |
| Threshold: `portfolio-discovery-health` | (audit) | discovery yield funnel < 5% |
| Threshold: `portfolio-memo-reviewer` | (audit) | memo lifetime hit-rate < 0.55 |

---

## 9. Quick reference — if X is broken, who fires?

```mermaid
flowchart LR
    classDef fault fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d
    classDef agent fill:#dcfce7,stroke:#15803d,color:#14532d

    F1[stuck signals]:::fault --> A1[resolver-investigator]:::agent
    F2[theme_signals empty]:::fault --> A2[discovery-health]:::agent
    F3[scraped_intel degraded]:::fault --> A2
    F4[discovery_pulse stale > 8h]:::fault --> A2
    F5[delta_hit_rate > 10pp]:::fault --> A3[attribution-analyst]:::agent
    F6[fingerprint changed]:::fault --> A3
    F7[memo decisions wrong shape]:::fault --> A4[memo-reviewer]:::agent
    F8[render code touched today]:::fault --> A5[render-reviewer]:::agent
    F9[pattern match_rate < 30%]:::fault --> A6[learning-loop-health]:::agent
    F10[retune drift > 60% of cap]:::fault --> A6
    F11[retune rollbacks ≥ 1 last 7d]:::fault --> A6
    F12[ai_budget > 80% of $20 cap]:::fault --> A6
    F13[stuck pending_confirmations > 14d]:::fault --> A6
```

---

## 10. Coverage debt (producers without dedicated analysis)

Per CLAUDE.md "every artifact must have a consumer at the right cadence,"
these producers currently lack a dedicated daily-tier check. They have
no urgent risk because each is observe-only and writes to LATEST only,
but they're debt-tracked here:

- `weight_tuning` → `weight_tuning_suggestions.json`
- `policy_evaluator` → `performance_summary.json`
- `allocation_preview / simulation / activation` → `allocation_policy_*.json`
- `alpha_attribution_report` → `alpha_attribution_report.json`
- `correlation_risk_advisor` → `correlation_risk_advisor.json`
- `confidence_calibration` → `confidence_calibration.json`
- `ai_decision_validator` (only indirectly covered via memo-reviewer)

**Resolution path:** add a `daily_run_status` content_liveness check
for each, OR explicitly accept the debt with a comment in the producer
module noting why (e.g., "advisory metric only; operator reviews via
GUI, no agent needed").

---

## 11. Source-of-truth files

- `CLAUDE.md` — Analysis + Health Coverage Requirement (the rule)
- `.agent/project_state.yaml` — roadmap + deferred steps
- `.claude/commands/{daily,monthly,yearly}-tool-analysis.md` — the three tier skills
- `.claude/agents/portfolio-*.md` — the 9 analysis agents
- `portfolio_automation/daily_run_status.py` — content_liveness scanner (8 checks today)
- `scripts/{run_daily_safe,daily_check,discovery_pulse,run_weekly_safe,monthly_check,yearly_check,historical_backfill}.sh` — cron entrypoints
- `crontab -l` — production schedule (7 active + 1 deferred entry)

---

_Generated 2026-05-28. Update this file whenever a new producer or
analysis check is added (per CLAUDE.md coverage rule)._
