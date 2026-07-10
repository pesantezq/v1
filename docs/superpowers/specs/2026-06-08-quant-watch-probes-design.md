# Quant Watch Probes — Design Spec

**Date:** 2026-06-08
**Status:** Approved (design); pending implementation plan
**Author:** Claude Code (brainstormed with operator)
**Lens:** Quant (primary), with Process-analyst overlap (lifecycle/audit trail)

---

## 1. Problem

The `daily-tool-analysis` skill triages the whole system GREEN/AMBER/RED against
fixed trip-wires. Real quant concerns routinely sit **below** those RED
trip-wires yet are "the real signal" for the day. The motivating example
(2026-06-08):

> Current gauge `d95e` is **−24.1pp vs the prior gauge era** `f60e`
> (44.9% vs 68.9% hit-rate_1d at n=176) with negative mean returns across all
> windows (−1.18 / −2.20 / −7.90). Daily RED never fires because RED keys on the
> delta-vs-`pre_tracker` baseline (only +4.3pp, under the ≥10pp gate). The
> concern is genuine but unowned — it surfaces in the body, then evaporates next
> run with no continuity, no resolution tracking, and no auto-cleanup once it
> stops mattering.

We need a lightweight, **self-managing watch list** of sub-RED quant concerns
that: (a) auto-registers a probe when a known condition fires, (b) re-checks
each open probe every run, (c) **auto-retires** the probe (archives it) once the
concern resolves or becomes irrelevant, and (d) cleanly hands off to daily's
existing RED logic if a concern worsens past a RED trip-wire.

This is the *quant-band* complement to `applied_fix_verifier` (which tracks
*applied fixes*); this tracks *open concerns*.

## 2. Goals / Non-Goals

**Goals**
- Continuity: a concern registered today is re-evaluated tomorrow with the same identity.
- Auto-retire: resolved/irrelevant probes leave the active list without operator action.
- Audit trail: archive what was watched and how it ended (for monthly/yearly retrospectives).
- Deterministic + testable: detection/resolution logic in a pure-function module with tests.
- Judgment escape hatch: Claude can register a manual probe for novel concerns.
- Zero production-pipeline risk: no changes to `run_daily_safe.sh` / preflight in v1.

**Non-Goals**
- Not a second RED authority. Daily-tool-analysis owns the RED *response* and agent dispatch.
- Not a decision/score/allocation mutator. Strictly observe-only.
- Not a general anomaly detector across all four lenses (v1 is quant-scoped; framework is lens-tagged so it can extend later).
- No new cron entry (rides the existing 09:15 daily delegation).

## 3. Architecture

Mirrors the established producer/skill split and the `applied_fix_verifier` ledger pattern.

```
retune_impact.json ─┐
pattern_efficacy_   │   ┌─────────────────────────────┐
  monthly.json ─────┼──▶│ quant_watch_probes.py        │
gauge_versions.jsonl┘   │  detect()   → new probes     │
                        │  evaluate() → active/         │      data/quant_watch_ledger.json
data/quant_watch_  ────▶│             resolved/escalated│◀────▶  { active:[...], archive:[...] }
  ledger.json (prev)    │  update_ledger()              │
                        │  render_status()              │──────▶ outputs/latest/
                        └─────────────────────────────┘         quant_watch_status.json
                                     ▲                                   │
                                     │ one-liner call                    │ read
                        ┌────────────┴───────────────┐                  ▼
                        │ /quant-watch-analysis skill │  ── heartbeat ──▶ folded into
                        │ (Step 1–5, manual layer)    │                  daily-tool-analysis
                        └─────────────────────────────┘
```

**Who writes the ledger:** the **skill drives the module** via a single
deterministic call (like the existing `drop_resolved` one-liner in
daily-tool-analysis Step 5). `daily-tool-analysis` already runs daily at 09:15
via `/schedule` and delegates here, so probes update every day **without
touching the production pipeline**. (Alternative considered: a hardened
non-blocking pipeline producer stage — rejected for v1 to avoid
`run_daily_safe.sh`/preflight changes and VPS validation risk. The module is
written pipeline-ready so this can be promoted later with no logic change.)

## 4. Components

| Unit | Purpose | Path |
|---|---|---|
| `quant_watch_probes.py` | Pure functions; observe-only; no I/O side effects beyond the explicit `update_ledger`/`write` helpers | `portfolio_automation/quant_watch_probes.py` |
| Ledger | Active probes + resolved archive | `data/quant_watch_ledger.json` |
| Status artifact | Consumer-facing heartbeat snapshot, `observe_only: true` hardcoded | `outputs/latest/quant_watch_status.json` (LATEST namespace) |
| Skill | Detect/evaluate/update orchestration + manual-probe judgment layer + heartbeat | `.claude/commands/quant-watch-analysis.md` |
| Daily hook | Delegate + one folded heartbeat line | edit `.claude/commands/daily-tool-analysis.md` |
| Tests | Detector fire/quiet, resolution/archive, escalation, idempotency, manual retention | `tests/test_quant_watch_probes.py` |

### Module API (pure functions)

```python
def detect(artifacts_root: str | Path, ledger: dict, now_iso: str) -> list[dict]:
    """Run all deterministic detectors; return NEW probe dicts not already active."""

def evaluate(artifacts_root: str | Path, ledger: dict, now_iso: str) -> list[dict]:
    """Re-check each active probe; return a transition per probe:
       {id, status: active|resolved|escalated, resolution?, detail, observation}."""

def update_ledger(ledger: dict, new_probes: list[dict], transitions: list[dict],
                  now_iso: str) -> dict:
    """Return a new ledger: add new_probes to active; move resolved/escalated to
       archive with resolved_at + resolution; append capped observations to
       still-active probes. Pure (does not mutate input, does not write disk)."""

def render_status(ledger: dict, new_probes, transitions, now_iso: str) -> dict:
    """Return the observe_only status artifact dict (overall_status + summaries)."""

def overall_status(ledger: dict, transitions: list[dict]) -> str:
    """GREEN (no active) | AMBER (>=1 active) | RED (>=1 escalated this run)."""
```

Disk I/O lives in two thin helpers (`load_ledger(path)`, `write_status(path, dict)`)
plus the skill's write-back of the ledger — keeping the core functions pure and
unit-testable without a filesystem.

## 5. Data Model

### Active probe

```json
{
  "id": "prior_gauge_underperformance:d95e3096443925b0",
  "detector": "prior_gauge_underperformance",
  "lens": "quant",
  "scope_key": "d95e3096443925b0",
  "created_at": "2026-06-08T13:30:00Z",
  "created_run": "2026-06-08 daily-tool-analysis",
  "severity": "amber",
  "concern": "current-fp d95e -24.1pp vs prior gauge f60e at n=176, mean_return_1d -1.18",
  "trigger_snapshot": {
    "current_hit_rate_1d": 0.4489, "prior_hit_rate_1d": 0.6894,
    "delta_vs_prior_pp": -24.1, "delta_vs_pretracker_pp": 4.3,
    "resolved_1d": 176, "mean_return_1d": -1.18, "prior_fp": "f60e0b9d51bec808"
  },
  "resolve_when": { "kind": "...", "...": "..." },
  "escalate_when": { "kind": "...", "...": "..." },
  "last_evaluated_at": "2026-06-08T13:30:00Z",
  "observations": [ { "run": "2026-06-08", "delta_vs_prior_pp": -24.1 } ]
}
```

- `observations` is capped (e.g. last 14) so the ledger does not grow unbounded.
- `resolve_when` / `escalate_when` are machine-checkable specs (kinds defined in §6).
- Manual probes use `resolve_when: {"kind": "manual"}` and have no `escalate_when`.

### Resolved (archive) probe

Active fields **plus**:

```json
{
  "resolved_at": "2026-06-20T13:30:00Z",
  "resolved_run": "2026-06-20 daily-tool-analysis",
  "resolution": "recovered",
  "resolution_detail": "delta_vs_prior_pp recovered to -1.4 (>= -2.0)",
  "lifetime_days": 12
}
```

`resolution` ∈ `recovered` | `scope_changed` | `sample_collapsed` |
`escalated_to_red` | `ttl_expired` | `manual`.

### Ledger file

```json
{ "schema_version": "1", "active": [ ...active probes... ],
  "archive": [ ...resolved probes... ] }
```

Archive may be capped/rolled (e.g. keep last 200) to bound file size; the
monthly/yearly skills consume it for retrospectives before any roll-off.

### Status artifact (`outputs/latest/quant_watch_status.json`)

```json
{
  "generated_at": "2026-06-08T13:30:00Z",
  "observe_only": true,
  "schema_version": "1",
  "source": "quant_watch_probes",
  "overall_status": "amber",
  "active_count": 2,
  "active": [ { "id": "...", "detector": "...", "concern": "...", "age_days": 0,
               "severity": "amber", "last_observation": {...} } ],
  "registered_today": [ "prior_gauge_underperformance:d95e3096443925b0" ],
  "resolved_today": [ { "id": "...", "resolution": "recovered" } ],
  "escalated_today": [],
  "ledger_liveness": { "status": "ok", "active_count": 2, "stale_active": 0 },
  "disclaimer": "Observe-only quant watch ledger. Tracks sub-RED quant concerns; re-checks and auto-retires them. Does not modify portfolio, allocation, scoring, or decision state."
}
```

## 6. Detectors (v1: flagship + 2) and Resolution Spec Kinds

All three are **quant** lens and read only existing artifacts. Thresholds are
module constants (config-overridable later; not hardcoded into scoring math).

### D1 — `prior_gauge_underperformance` (flagship)

- **Source:** `retune_impact.json` → `outcome_attribution.by_fingerprint`.
- **Prior gauge** = the `by_fingerprint` entry that is neither the current
  fingerprint nor `pre_tracker_unknown`, with the latest `last_signal_time`
  (same selection rule as `daily_memo._retune_prior_gauge_delta`).
- **Fire when:** `current.resolved_1d >= 30` AND a prior gauge exists AND
  `(current.hit_rate_1d − prior.hit_rate_1d) * 100 <= -10`
  AND `abs((current.hit_rate_1d − pre_tracker.hit_rate_1d) * 100) < 10`
  (daily RED would otherwise own it).
- **scope_key:** current fingerprint.
- **resolve_when:** `delta_vs_prior_pp >= -2` (recovered) — kind
  `metric_recovered`. Also auto-resolves via `scope_changed` (fingerprint no
  longer current) and `sample_collapsed` (`resolved_1d == 0`).
- **escalate_when:** `abs(delta_vs_pretracker_pp) >= 10` AND `resolved_1d >= 30`
  — kind `daily_red_gate` (matches daily's own RED key, so escalation is
  definitionally aligned with daily RED).

### D2 — `negative_mean_return_persistence`

- **Source:** `retune_impact.json` current-fp `mean_return_1d`.
- **Fire when:** `current.resolved_1d >= 30` AND `current.mean_return_1d < 0`.
- **scope_key:** current fingerprint.
- **resolve_when:** `mean_return_1d >= 0` (kind `metric_recovered`); plus
  `scope_changed`.
- **escalate_when:** none (mean-return is not a daily RED trip-wire — stays
  AMBER until resolved).

### D3 — `sector_drag`

- **Source:** `pattern_efficacy_monthly.json` → `by_tag`, keys matching `sector:*`.
- **Fire when:** a `sector:*` tag has `significance == "loser"` AND
  `n_samples >= 30` (reuses the producer's own Wilson-CI significance — no new
  stats invented).
- **scope_key:** the sector name (one probe per dragging sector).
- **resolve_when:** that sector's tag is no longer `loser` OR the tag is absent
  — kind `tag_no_longer_loser`.
- **escalate_when:** none.

### Resolution spec kinds (checked by `evaluate`)

| kind | resolves when | source |
|---|---|---|
| `metric_recovered` | `{artifact, path, field, op (>=/<=), threshold}` satisfied | JSON artifact |
| `scope_changed` | current fingerprint != probe `scope_key` | `gauge_versions.jsonl` tail / retune_impact `current_fingerprint` |
| `sample_collapsed` | `{artifact, path, field}` == 0 | JSON artifact |
| `tag_no_longer_loser` | `by_tag[tag].significance != "loser"` or tag absent | pattern_efficacy |
| `ttl_expired` | `created_at` older than `MAX_PROBE_AGE_DAYS` (e.g. 60) and no fresh trigger | clock |
| `manual` | never auto — operator/Claude clears | — |

A probe resolves on the **first** matching condition. `escalate_when` is checked
**before** `resolve_when` (a worsening probe escalates rather than silently
lingering).

## 7. Escalation — the AMBER/RED hybrid

- Status artifact `overall_status`: **GREEN** (no active probes) · **AMBER**
  (≥1 active) · **RED** (≥1 probe escalated *this run*).
- On escalation a probe is archived with `resolution: escalated_to_red`, listed
  in `escalated_today`, and the quant-watch **heartbeat shows RED**.
- Because `escalate_when` is defined as crossing **daily's own RED gate**, the
  same underlying condition also trips `daily-tool-analysis` RED in the same run
  and dispatches the right agent (e.g. `portfolio-attribution-analyst`). Shared
  threshold ⇒ **single source of RED truth**, no duplicate/conflicting alerts.
- Division of labor: the probe self-raises **RED visibility** (no
  under-reporting); daily owns the **RED response** (dispatch + action-template).

## 8. Skill `/quant-watch-analysis` (Steps)

1. **Read** ledger (`data/quant_watch_ledger.json`; create empty default if
   missing) + the detector source artifacts.
2. **Detect + evaluate + update** via one module call; write the new ledger and
   `quant_watch_status.json`. (Idempotent: re-running same-day adds nothing new.)
3. **Manual layer:** Claude reviews active probes + today's body for any *novel*
   quant concern not covered by a detector; if found, append a `manual` probe
   (with a clear `concern` + operator-review note). Claude may also retire a
   `manual` probe it judges resolved.
4. **Heartbeat** (emit every run):
   `[GREEN|AMBER|RED] quant-watch YYYY-MM-DD: {active_count} active · {registered_today} registered · {resolved_today} resolved · {escalated_today} escalated`
   followed by one line per active probe (concern + age + latest observation).
5. **Write back** ledger (already written in Step 2; Step 5 confirms + records
   `last_run_at`).

### Daily-tool-analysis integration (edit)

- **Step 1 artifacts read:** add `outputs/latest/quant_watch_status.json`.
- **Sub-check (like pattern-loop):** delegate to `/quant-watch-analysis` Step
  1–2 backbone; do **not** re-derive detector logic in the daily skill.
- **Step 4 body:** fold one line —
  `"quant-watch: {overall_status} · {active_count} active ({top probe concern}); {registered/resolved/escalated today}"`.
- **RED coupling:** daily escalates to RED only on the probe's own RED condition
  (`escalated_today` non-empty), which by construction already coincides with an
  existing daily RED key — so this adds *visibility*, not a new RED authority.

## 9. Observe-only / contract compliance

- `observe_only: true` hardcoded in the status artifact; module mutates only the
  ledger + its own status artifact — never decision/score/allocation/portfolio
  state.
- Output namespace: status artifact → `OutputNamespace.LATEST`; ledger → `data/`
  (state, like `daily_check_state.json`). No writes outside declared purpose.
- All skill-driven calls wrapped in try/except — a probe failure never aborts
  the daily summary (matches "Non-blocking pipeline integration").
- No protected-semantics surfaces touched. No scoring/decision-engine edits.

## 10. Testing (`tests/test_quant_watch_probes.py`)

Each as a fixture pair (degraded → fires/active; healthy → quiet/resolved):

1. D1 fires on the 2026-06-08-style fixture; stays quiet when delta-vs-prior ≥ −2pp.
2. D1 **escalates** (RED) when delta-vs-pretracker ≥ 10pp at n≥30; archived `escalated_to_red`.
3. D1 resolves `scope_changed` when fingerprint changes; resolves `recovered` when metric recovers.
4. D2 fires on `mean_return_1d < 0`; resolves on ≥ 0.
5. D3 fires on a `sector:*` `loser` at n≥30; resolves when no longer loser / tag absent.
6. **Idempotency:** running `detect` twice same-day yields no duplicate active probe.
7. **Manual probe** is never auto-dropped by `evaluate`/`update_ledger`.
8. `overall_status` mapping: none→GREEN, active→AMBER, escalated→RED.
9. Corrupt/missing ledger → reset to empty default, one-time degraded note (no crash).
10. `ledger_liveness` flags a `stale_active` probe (active but its source artifact missing/stale).

Run targeted first (`pytest -q tests/test_quant_watch_probes.py`), then full suite.

## 11. Analysis + Health Coverage pairing (CLAUDE.md requirement)

- **Cadence:** daily → owning skill is `daily-tool-analysis` (this spec wires it in).
- **Lens:** Quant (overlaps Process-analyst for lifecycle/audit).
- **Pairing artifacts:** Step 1 artifact-read entry + Step 4 body line +
  `ledger_liveness` content-liveness-style guard (catches a stuck/empty ledger).
- **Consumer corollary:** `quant_watch_status.json` is consumed by the daily
  heartbeat; the ledger archive is consumed by the monthly/yearly retrospectives
  (follow-up wiring noted, not required for v1 merge).

## 12. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Probe noise (too many fires) | Conservative thresholds + n≥30 gates; idempotent keys; 3 detectors only in v1 |
| Detector overlap (D1 & D2 both fire on same gauge) | Distinct concerns by design (relative hit-rate vs absolute return); both archiving independently is acceptable and informative |
| Ledger unbounded growth | `observations` capped (14); archive rolled (200) after retrospective consumption |
| Stale source artifact mis-resolves a probe | `ledger_liveness.stale_active` guard + staleness check before declaring `recovered` |
| Double RED (probe + daily) | Shared threshold by construction; probe defers response to daily |
| Production pipeline risk | v1 is skill-driven only; no `run_daily_safe.sh`/preflight changes |

## 12a. D3 cross-gauge pooling guard (added 2026-07-10)

`detect_sector_drag` fires off `pattern_efficacy_monthly.by_tag["sector:*"]`, whose
sector outcomes are **pooled across every gauge era**. A sector a retired gauge
handled badly therefore reads `significance == "loser"` indefinitely, even after the
current gauge started treating it as a winner — a stale, self-perpetuating AMBER.
(Observed 2026-07-10: `Communication_Services` was pooled `loser` at −12.28pp/n=62
while on the current fingerprint alone it was the *best* sector — 84.6% hit /
+1.97% mean / n=26.)

Fix: cross-check the pooled verdict against the **current fingerprint's own**
`retune_impact.json → outcome_attribution.by_fingerprint[current_fp].sector_composition`:

- `_current_fp_sector_verdict(retune, sector)` → `contradicts` (live gauge
  `mean_return_1d >= 0` at `resolved_1d >= SECTOR_XCHECK_MIN_N=20`) / `confirms`
  (live gauge also negative) / `unknown` (thin or absent slice). `_norm_sector`
  reconciles the label mismatch between the two artifacts (`Communication_Services`
  vs `Communication Services`, `ETF_Index` vs `ETF/Index`).
- `detect_sector_drag` **suppresses** registration when the current fp contradicts.
- `_eval_sector_drag` **auto-resolves** an existing probe with resolution
  `current_fp_contradicts` under the same condition.

Both sides are required: without the detector suppression, a probe resolved by the
evaluator would immediately re-register the next run (flip-flop). The pooled signal
remains the trigger; the live-gauge slice is a **veto used only when the two
genuinely disagree**, so true single-gauge drags (verdict `confirms`/`unknown`) still
fire and stay. Backward compatible: `retune=None` ⇒ `unknown` ⇒ prior behaviour.

## 13. Out of scope / follow-ups
- Promote the module to a non-blocking pipeline producer stage (cron-hardened).
- Additional detectors: horizon-decay (1d→7d collapse), persistent-loser-tag across K runs, concentration-drift.
- Monthly/yearly retrospective sections that mine the archive (lifetime probe efficacy, mean lifetime-to-resolution).
- Extend the framework to non-quant lenses (developer/process/market) reusing the same ledger machinery.
