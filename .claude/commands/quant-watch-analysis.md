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
drag). If you find one worth tracking, append a manual probe to the active
ledger so it persists across runs:

```bash
python3 -c "
import json
from portfolio_automation.quant_watch_probes import load_ledger, write_ledger, _now_iso
p='data/quant_watch_ledger.json'; led=load_ledger(p)
led['active'].append({
  'id':'manual:<short-slug>','detector':'manual','lens':'quant',
  'scope_key':'<slug>','created_at':_now_iso(),'created_run':'quant-watch-analysis',
  'severity':'amber','concern':'<one-line concern>',
  'trigger_snapshot':{},'resolve_hint':'<how an operator will know it cleared>',
  'observations':[]})
write_ledger(p, led); print('appended manual:<short-slug>')
"
```

Manual probes are NEVER auto-resolved — retire one only when you (or the
operator) judge it cleared, by removing it from `active` (optionally moving it
to `archive` with `resolution:'manual'`).

## Step 3 — Triage

- **GREEN** — `overall_status == "green"` (no active probes).
- **AMBER** — `overall_status == "amber"` (≥1 active probe; the sub-RED band).
- **RED** — `overall_status == "red"` (≥1 probe escalated this run; it crossed a
  daily RED gate). The escalation is, by construction, also a daily RED key —
  daily-tool-analysis owns the RED *response* + agent dispatch.

If `ledger_liveness.status == "warn"`, note the stale/empty-ledger condition.

## Step 4 — Heartbeat (emit every run)

Lead line:

`[GREEN|AMBER|RED] quant-watch YYYY-MM-DD: {active_count} active · {len(registered_today)} registered · {len(resolved_today)} resolved · {len(escalated_today)} escalated`

Then one line per active probe:
`- {detector}: {concern} (age {age_days}d, last {last_observation})`

And, when present:
`- resolved today: {id} ({resolution})`
`- ESCALATED today: {id} → now daily-RED-tracked; see daily-tool-analysis dispatch`

## Step 5 — Notes

The ledger + status artifact are already written by Step 1. Nothing else to
persist. The archive (`data/quant_watch_ledger.json:archive`) is the
retrospective trail consumed by the monthly/yearly tool-analysis skills.
