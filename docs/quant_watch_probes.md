# quant_watch_probes

Observe-only ledger of **sub-RED quant concerns** ("watch probes"). Companion to
`applied_fix_verifier`: that module tracks *applied fixes*; this tracks *open
concerns* that sit below the `daily-tool-analysis` RED trip-wires yet are worth
watching with continuity.

## Lifecycle

1. **Register** — a deterministic detector fires and a probe is added to
   `data/quant_watch_ledger.json:active`, keyed `detector_id:scope_key`
   (idempotent — re-running never duplicates).
2. **Re-check** — each run a paired evaluator recomputes from current artifacts
   and returns `active` / `resolved` / `escalated` (escalate is checked first).
3. **Retire** — resolved/escalated probes move to `archive` with `resolved_at`,
   `resolution`, and `lifetime_days`. Resolutions: `recovered`, `scope_changed`,
   `sample_collapsed`, `escalated_to_red`, `ttl_expired`, `manual`.

## Detectors (v1)

| id | source | fires when | resolves when | escalates when |
|---|---|---|---|---|
| `prior_gauge_underperformance` | `retune_impact.json` | current-fp ≤ −10pp vs prior gauge at n≥30 AND `|Δ vs pre_tracker|` < 10pp | Δ vs prior ≥ −2pp / fp change / n→0 | `|Δ vs pre_tracker|` ≥ 10pp at n≥30 (daily RED gate) |
| `negative_mean_return_persistence` | `retune_impact.json` | current-fp `mean_return_1d` < 0 at n≥30 | `mean_return_1d` ≥ 0 / fp change | — |
| `sector_drag` | `pattern_efficacy_monthly.json` | a `sector:*` tag is `loser` at n≥30 | no longer `loser` / tag absent | — |

Plus a **manual** probe path (`detector: "manual"`) for novel concerns; manual
probes are never auto-resolved.

## Artifacts

- `data/quant_watch_ledger.json` — state: `{schema_version, active[], archive[]}`.
- `outputs/latest/quant_watch_status.json` — observe-only heartbeat snapshot
  (`overall_status` green/amber/red, active[], registered/resolved/escalated
  _today, ledger_liveness).

Both are runtime-generated and git-ignored (like `data/daily_check_state.json`); they are regenerated every run and are not committed to the repo.

## Status levels

`green` (no active probes) · `amber` (≥1 active) · `red` (≥1 escalated this
run). RED escalation is, by construction, also a daily RED key — daily owns the
RED response; this module adds continuity + visibility.

## Entry point

`run_quant_watch(root, now_iso=None, created_run=..., write_files=True)` — loads,
evaluates, detects, updates, writes, returns the status dict. Never raises.

## Consumers

- `/quant-watch-analysis` skill (daily, on-demand) — drives the loop + heartbeat.
- `/daily-tool-analysis` — delegates the sub-check + folds the heartbeat.
- Monthly/yearly tool-analysis — mine `archive[]` for retrospectives (follow-up).

Observe-only: mutates only its ledger + status artifact; never decision, score,
allocation, or portfolio state.
