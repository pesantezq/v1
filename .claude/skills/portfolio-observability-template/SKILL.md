---
name: portfolio-observability-template
description: Clone-ready scaffold for a Portfolio Automation System observability v2 producer module. Use when adding a new observe-only advisor / probe / telemetry module that follows the established pattern (pure functions, JSON+MD artifacts under outputs/latest/, observe_only hardcoded, degraded-state dict on failure, non-blocking pipeline integration). Cuts new-module time roughly in half by providing the exact pattern from risk_delta_advisor, retune_impact_tracker, fmp_budget_telemetry, daily_run_status, and resolution_due_probe.
---

# Skill: portfolio-observability-template

## Purpose

Provide a clone-ready scaffold for the observability v2 producer pattern.
Five modules in the project already follow this shape exactly. New modules
should follow it too — both for consistency and to inherit the wired-in
non-blocking wrapper integration + preflight smoke checks.

## When to Use

- Adding a new observe-only advisor / probe / telemetry / health-check module
- After a roadmap step asks for "surface X" or "track Y" or "make Z visible"
- When the user wants a producer that runs every cron, writes a JSON + MD
  artifact, and degrades gracefully when its inputs are missing

## When NOT to Use

- For modules that need to mutate scoring, allocation, or recommendation state — that's protected semantics, requires explicit user approval
- For one-shot scripts that don't fit the daily-cron rhythm
- For sandbox-only producers — they live under `outputs/sandbox/` with a different namespace + safety-flag set (see `portfolio_automation/discovery/`)
- For GUI pages — use `gui_v2/templates/` patterns instead

## The Pattern (all 5 reference modules follow this)

```
portfolio_automation/<module_name>.py
├── docstring (purpose, inputs, outputs, hard guarantees)
├── constants:
│   _SCHEMA_VERSION = "1"
│   _SOURCE_LABEL = "<module_name>"
│   _OBSERVE_ONLY = True
│   _DISCLAIMER = "..."
├── helper functions:
│   _safe_float, _safe_int, _load_json_safe, etc.
├── pure compute functions (one per metric):
│   def compute_<metric>(inputs) -> dict[str, Any]:
│       # Pure. No I/O. Returns dict.
│       # Failure mode: returns {"available": False, "reason": "..."}
├── render function:
│   def render_<module_name>_md(payload: dict) -> str:
│       # Pure. Markdown string out.
├── (optional) history-append function:
│   def append_to_history(payload, *, root) -> bool:
│       # Dedups against last row.
├── top-level orchestrator:
│   def run_<module_name>(*, root=".", write_files=True) -> dict[str, Any]:
│       # The wrapper-stage entry point.
│       # Wrapped in try/except → returns {"status": "error", "error": ...}
└── __main__ guard:
    if __name__ == "__main__":
        result = run_<module_name>(root=...)
        print(...)
```

## Reference Implementations

Read these in order — they progress from simplest to most complex:

| Module | Why read it |
|---|---|
| `portfolio_automation/risk_delta_advisor.py` | Simplest: 3 pure compute functions, status badging, no history ledger |
| `portfolio_automation/resolution_due_probe.py` | CSV scan + group-by aggregation, calendar-day threshold math |
| `portfolio_automation/fmp_budget_telemetry.py` | Multi-source read + appendable JSONL history pattern |
| `portfolio_automation/daily_run_status.py` | Log-file scan, regex-driven, artifact-freshness check |
| `portfolio_automation/retune_impact_tracker.py` | Most complex: fingerprint + history + CSV→JSONL timestamp-range join |

## Step-by-Step

1. **Pick a name** — `<module_name>` should be a snake_case noun phrase
   like `correlation_drift_probe` or `signal_age_telemetry`. Avoid verbs.

2. **Copy a reference**. Start from the closest-shaped of the five above.
   `risk_delta_advisor.py` is the easiest base.

3. **Replace constants:**
   - `_SOURCE_LABEL = "<module_name>"`
   - `_DISCLAIMER` — one sentence on what it does + reaffirms observe-only.

4. **Define pure compute functions** for each metric the module surfaces.
   Each accepts the raw inputs (dicts/lists, not Path objects when avoidable)
   and returns a dict. Standard failure shape:
   ```python
   return {"available": False, "reason": "<what's missing>"}
   ```

5. **Define `build_<module>` orchestrator** that composes the compute
   functions into a single payload dict. Standard top-level fields:
   ```python
   {
       "generated_at": ts,
       "observe_only": _OBSERVE_ONLY,
       "schema_version": _SCHEMA_VERSION,
       "source": _SOURCE_LABEL,
       "overall_status": <derived>,
       "<metric_1>": {...},
       "<metric_2>": {...},
       "disclaimer": _DISCLAIMER,
   }
   ```

6. **Define `render_<module>_md`** — pure Markdown string builder. Use
   the badging vocabulary the project already uses (🔴 BREACH, 🟡 near
   cap, 🟢 ok) when status applies.

7. **Define `run_<module>`** — top-level orchestrator. Reads inputs, calls
   `build_*`, optionally writes artifacts via `safe_write_json` /
   `safe_write_text` under `OutputNamespace.LATEST`. Wrap entire body in
   `try/except` and return a degraded-state dict on any exception.

8. **Add `if __name__ == "__main__"` guard** so you can dry-run with
   `python -m portfolio_automation.<module_name>`.

9. **Write tests** at `tests/test_<module_name>.py`. Required:
   - pure compute tests (no filesystem) for each metric
   - degraded-mode tests (missing artifacts → `available: False`)
   - end-to-end test via tempfile + `run_*` → asserts both `.json` and `.md` written + `observe_only` true
   - no-mutation invariant — read decision_plan.json before/after, assert unchanged

10. **Wire into `scripts/run_daily_safe.sh`** as a new `run_aux_stage`
    line. Pick a stage number that respects dependencies (e.g. read
    `system_decision_summary.json`? → must run after Stage 7).

11. **Wire into `scripts/preflight.sh`** — add to both the `python -m
    py_compile` list and the "Advisor Smoke Imports" Python block.

12. **Document** — follow the existing v2 pattern. Add the JSON + MD
    contract to `docs/OUTPUT_ARTIFACT_CONTRACTS.md` under "Observability
    v2 Artifacts". Brief note in `docs/ARCHITECTURE.md`. Roadmap entry in
    `docs/roadmap.md`.

13. **(Optional) Surface in the memo.** If the output is glanceable, add
    a one-line summary helper in `watchlist_scanner/daily_memo.py` and
    call it from `_advisor_stack_items` (or appropriate section).

## Anti-Patterns to Avoid

- **Don't** mix `observe_only=False` payloads — every v2 producer is
  observe-only. If you need a non-observe-only producer, that's a different
  pattern requiring explicit user approval.
- **Don't** raise from `run_*`. Always return a degraded-state dict.
- **Don't** silently swallow exceptions in compute functions — log them
  at DEBUG / WARNING and return `available: False` with a reason string.
- **Don't** hardcode gauge / cap / threshold values in renderers — read
  from config at render time so retunes propagate (see daily_memo's
  sector cap fix, 2026-05-19).
- **Don't** add a new producer without an `if __name__ == "__main__"`
  guard — preflight's advisor-smoke-import section runs each module's
  import path; the guard lets you dry-run end-to-end with one command.

## Output Convention

| Layer | Convention |
|---|---|
| File paths | `outputs/latest/<module_name>.json` + `.md` |
| Namespace | `OutputNamespace.LATEST` (use `safe_write_json` / `safe_write_text`) |
| Schema marker | `{"schema_version": "1", "source": "<module_name>", "observe_only": true}` |
| History (if applicable) | `data/<module_name>_history.jsonl` (append-only, dedup by relevant key) |

## Final Test Command

```bash
python -m py_compile portfolio_automation/<module_name>.py
python -m pytest -q tests/test_<module_name>.py
python -m portfolio_automation.<module_name>
python -m pytest -q \
  --ignore=tests/test_gui_api_health.py \
  --ignore=tests/test_gui_insight_cards.py
```

Then run `bash scripts/preflight.sh` to confirm the new module passes
compile-check and smoke-import.
