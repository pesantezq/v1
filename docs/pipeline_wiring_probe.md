# Pipeline Wiring Probe

Last verified against `portfolio_automation/pipeline_wiring_probe.py` and
`scripts/run_daily_safe.sh` (Stage 13). Last updated 2026-06-12.

## Purpose

Observe-only **root-cause layer for stale producers**. The artifact-registry
validator (`artifact_registry_status.json`) answers *"is artifact X stale?"* but
not *"why"*. The 2026-06-11 stale-producer audit found that several distinct
failure modes all look identical to the registry — just "stale". This probe
classifies each declared producer so an operator (and the daily check) sees the
root cause, not just the symptom.

It crosses two signals per registry producer:

- **freshness** (authoritative) — artifact mtime vs its declared cadence window.
- **static caller-grep** (classifier, best-effort) — which cadence's cron script
  names the producer's module token. `main.py` plus the orchestrator modules it
  calls are treated as the daily "core" corpus.

## Status Taxonomy

A fresh producer is `healthy`. A stale one is classified into:

| Status | Meaning |
|---|---|
| `unwired` | No cron/script invokes the producer at all. |
| `cadence_mismatch` | Invoked, but by a script of a *different* cadence than the registry declares (e.g. declared daily, only called in `run_weekly_safe.sh`). |
| `silently_skipped` | Wired for the right cadence, but a config gate / missing param makes it a no-op (e.g. `main.py` omitting a producer's config). |
| `fresh_but_empty` | Produced on cadence, but its content is degenerate (empty/zeroed) per a per-producer content predicate. |

Non-problem states that are reported but never alerted: `disabled` (config-gated
off by design), `event_log_idle` (append-only telemetry logs), `not_audited`
(`on_demand` producers with no fixed cadence).

## Observe-Only Behavior

`observe_only=True` is hardcoded; the probe never writes decision, score, or
allocation data. `overall_status` is **AMBER-max — never RED** (it is a
meta-monitor, not the decision core). Caller detection is a best-effort token
grep, so a flagged producer is a *thing to verify*, not ground truth — the
`portfolio-discovery-health` agent confirms the producer→caller chain.

## Public API

| Function | Role |
|---|---|
| `run_pipeline_wiring_probe(root, write_files, now)` | Orchestrator — loads the registry, builds the script corpus, computes artifact ages, classifies, and writes the artifacts. |
| `classify_producers(registry, script_texts, artifact_ages_hours, *, content_flags, config_gates)` | Pure classifier over injected inputs — returns `{producers, summary, overall_status}`. |
| `render_pipeline_wiring_md(payload)` | Renders the Markdown companion artifact. |

## Artifacts

Written to `OutputNamespace.LATEST` (`outputs/latest/`):

- `pipeline_wiring_status.json` — `overall_status`, `summary.{total_audited,
  healthy, unwired, cadence_mismatch, silently_skipped, fresh_but_empty,
  disabled, not_audited, event_log_idle}`, and `producers[]` (each with
  `artifact`, `status`, `caller_cadences`).
- `pipeline_wiring_status.md` — operator-readable companion.

## Pipeline Integration

Runs as **`run_daily_safe.sh` Stage 13**, after the registry-governance stage so
it layers root-cause analysis on top of the freshness verdict. The call is
wrapped non-blocking (`try`/`except`) per the observe-only default — a probe
failure never aborts the pipeline.

## Health Pairing

Consumed by `/daily-tool-analysis` (developer lens): the skill reads
`pipeline_wiring_status.json` into `wiring_*` signals, emits the per-run
heartbeat line ("Pipeline-wiring: …"), and dispatches `portfolio-discovery-health`
with the flagged producers whenever `wiring_problems ≥ 1`. This generalizes the
2026-06-11 audit — it catches the *next* unwired producer, not just the ones
already fixed.
