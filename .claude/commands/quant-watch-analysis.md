# Quant Watch Analysis

Operational function + health check of the quant-watch probe ledger: a
self-managing list of sub-RED quant concerns. Auto-registers a probe when a
deterministic quant condition fires below the daily-tool-analysis RED
trip-wires, re-checks each open probe, and auto-archives it on resolution.
On-demand; delegated to daily by `/daily-tool-analysis`. Working dir
`/opt/stockbot`.

Module of record: `portfolio_automation/quant_watch_probes.py`. Do NOT
re-derive detector/resolution logic in this prose — the module owns it.

---

## Step 1 — Run the loop (deterministic)

Run the module orchestrator. It loads the ledger + source artifacts, evaluates
open probes (escalate-before-resolve), detects new ones, archives the resolved,
and writes both the ledger (`data/quant_watch_ledger.json`) and the status
artifact (`outputs/latest/quant_watch_status.json`):

```bash
python3 -c "import json; from portfolio_automation.quant_watch_probes import run_quant_watch; print(json.dumps(run_quant_watch(root='.', created_run='quant-watch-analysis'), indent=2))"
```

Read the returned JSON: `overall_status`, `active_count`, `active[]`,
`registered_today`, `resolved_today`, `escalated_today`, `ledger_liveness`.

## Step 2 — Manual judgment layer (optional)

Skim today's `outputs/latest/daily_memo.md` + `retune_impact.json` +
`pattern_efficacy_monthly.json` for a *novel* quant concern NOT covered by the
three detectors (prior-gauge underperformance, negative mean-return, sector
drag). If you find one worth tracking, register it through the schema-correct
API (WS16 — replaces hand-editing the ledger JSON) — pick a `concern_class`
from `quant_watch_probes.CONCERN_CLASSES` (or a new detector-specific one) so
the concern is queryable by taxonomy, not just free text:

```bash
python3 -c "
from portfolio_automation.quant_watch_probes import register_manual_concern, CONCERN_CLASS_REGIME_CONCENTRATION
result = register_manual_concern(
    root='.', concern='<one-line concern>',
    concern_class=CONCERN_CLASS_REGIME_CONCENTRATION,  # or another CONCERN_CLASS_* constant
    scope_key='<short-slug>',
    evidence_artifact='outputs/latest/<source_artifact>.json',
    affected_component='<module/producer this concern is about>',
    owner='<agent or role, e.g. portfolio-attribution-analyst>',
    created_run='quant-watch-analysis')
print(result)
"
```

A concern_class in `TRUST_BOUNDARY_CONCERN_CLASSES` (timestamp leakage,
revocation resurrection, decision/presentation divergence) may register
directly at RED severity (`severity=qwp.RED`) — a single CONFIRMED occurrence
of a trust-boundary breach does not need to wait for persistence; everything
else is capped at AMBER regardless of the requested severity.

Manual probes are NEVER auto-resolved by age or by re-evaluation (no detector
exists for `manual`-class concerns — they re-enter `active` every run,
by design, until explicitly closed). Retire one only via
`record_closure(root, concern_id, note=..., closed_by=..., evidence_artifact=...,
regression_test_reference=...)` — recording closure evidence is what
actually resolves a manual concern (this is now schema-correct, not raw
JSON surgery). For a detector-tracked (D1/D2/D3) concern, `record_closure`
pre-attaches evidence but does NOT resolve it alone — the owning detector
must ALSO confirm on its next evaluation that the condition no longer
fires (WS16: "detector no longer fires AND a closure record exists", never
age alone).

## Step 3 — Triage

- **GREEN** — `overall_status == "green"` (no active probes).
- **AMBER** — `overall_status == "amber"` (≥1 active probe; the sub-RED band).
- **RED** — `overall_status == "red"` (≥1 probe escalated this run — persistence
  + impact for D1/D2/D3, or a directly-registered trust-boundary concern's own
  `severity=="red"` — see WS16 below). The escalation is, by construction,
  also a daily RED key — daily-tool-analysis owns the RED *response* + agent
  dispatch.

If `ledger_liveness.status == "warn"`, note the stale/empty-ledger condition.

**WS16 (2026-07-28) — escalation and closure no longer key on age.** All
three detectors' pure 60-day TTL auto-resolve is REMOVED: a concern closes
only when its detector confirms the condition no longer fires AND a
`closure_evidence` record exists (auto-populated from the resolving
transition, or pre-attached via `record_closure`). `MAX_PROBE_AGE_DAYS` is now
purely an operator-visibility marker — a probe past that age with no closure
renders `stale_unresolved: true` in its `active[]` entry; it does NOT resolve.
Escalation for D2/D3 now mirrors D1's persistence+impact pattern (a stricter
sample floor + `consecutive_observations >= 3`), never age. Treat any
`stale_unresolved: true` probe as worth a closer look — it has been open a
long time with nothing recorded against it.

## Step 4 — Heartbeat (emit every run)

Lead line:

`[GREEN|AMBER|RED] quant-watch YYYY-MM-DD: {active_count} active · {len(registered_today)} registered · {len(resolved_today)} resolved · {len(escalated_today)} escalated`

Then one line per active probe:
`- {detector}: {concern} (age {age_days}d{, STALE-UNRESOLVED if stale_unresolved}, last {last_observation}, remediation {remediation_status})`

And, when present:
`- resolved today: {id} ({resolution})`
`- ESCALATED today: {id} → now daily-RED-tracked; see daily-tool-analysis dispatch`

## Step 5 — Notes

The ledger + status artifact are already written by Step 1. Nothing else to
persist. The archive (`data/quant_watch_ledger.json:archive`) is the
retrospective trail consumed by the monthly/yearly tool-analysis skills.
