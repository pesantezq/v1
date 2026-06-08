# Artifact Registry & Probe Governance Layer — Design Spec

**Date:** 2026-06-08
**Status:** Approved (design); pending implementation plan
**Author:** Claude Code (brainstormed with operator; layer proposed by GPT roadmap-control, operator-approved)
**Lens:** Developer / pipeline-ops health (with meta-governance role)
**Roadmap:** supersedes the stale `next_official_step: observe_and_iterate` note via GPT-proposed → operator-approved → Claude-implemented; record as step `artifact_registry_and_probe_governance_layer`.

---

## 1. Problem

The system now emits ~45 artifacts under `outputs/latest/`, but there is **no single
machine-readable contract** describing them. The contract that *does* exist is
fragmented and duplicated:

- `portfolio_automation/daily_run_status.py:_EXPECTED_ARTIFACTS` hardcodes a
  `(path, label, required)` tuple for ~11 artifacts and computes
  `required_missing_count` / `optional_missing_count` / `fresh_today` /
  `content_liveness`.
- `/daily-tool-analysis` re-enumerates 16 artifacts by hand in its Step-1 prose.
- CLAUDE.md states "every artifact under `outputs/latest/*.json` should be consumed
  by AT LEAST ONE check … producers without consumers are debt" — but this is
  enforced only by human review.

There is no canonical answer to: which artifacts are required vs optional, which are
stale (cadence-aware), which lens owns each, which downstream skill/agent consumes
each, which represent official decisions vs observe-only health, and which failures
are critical/warning/info.

The bottleneck is no longer *more probes* — it is **governance**: knowing whether the
system itself is complete, fresh, and coherent. This is the missing fifth layer
(producers → analysis skills → agents → builder skills → **artifact governance**).

## 2. Goals / Non-Goals

**Goals**
- One declarative, human-edited, machine-readable contract for every `outputs/latest` artifact.
- A validator that emits a daily governance artifact: missing / stale (cadence-aware) / invalid-JSON / producer-without-consumer gaps / severity rollup / operator message.
- **Deduplicate, not add a layer:** the registry becomes the single source of truth that `daily_run_status` reads (its `_EXPECTED_ARTIFACTS` is removed), with the existing output schema preserved byte-for-byte.
- Codify the observe-only invariant via a `role` field (only `source_of_truth` artifacts represent official portfolio actions).
- Make CLAUDE.md's "every artifact has a consumer" rule machine-checkable.

**Non-Goals (deferred to follow-ups, per "don't add more artifacts")**
- The four lens-rollup summary producers (`developer_health_summary`, etc.).
- The GUI "System Coverage" card (consumes this layer later).
- Any change to decision/scoring/allocation behavior. Strictly observe-only.
- JSON-schema validation of each artifact's *internal* shape (v1 validates presence/freshness/parseability + contract integrity, not per-artifact field schemas).

## 3. Architecture

```
portfolio_automation/artifact_registry.yaml   ← THE contract (≈45 rows, commented)
                │ loaded by
                ▼
portfolio_automation/artifact_registry.py     ← loader + validator + orchestrator (observe-only)
    ├── load_registry(path) -> dict            (fault-tolerant)
    ├── required_artifacts() -> list           (the daily_run_status replacement feed)
    ├── validate_registry(registry, root, now) -> status dict
    └── run_artifact_registry(root, write_files) -> status dict   (never raises)
        ┌──────────────────────────────┬───────────────────────────────────────┐
        ▼                              ▼                                         ▼
daily_run_status.py reads          outputs/latest/artifact_registry_status.json   /daily-tool-analysis
required_artifacts()               (observe_only:true; severity rollup)          reads status FIRST →
(removes _EXPECTED_ARTIFACTS)                                                     gates confidence by role
```

Single source of truth = the YAML. `daily_run_status` and the validator both consume
it; the daily skill consumes the validator's status artifact. No duplicated contract.

## 4. Components

| Unit | Responsibility | Path |
|---|---|---|
| Registry contract | Declarative per-artifact metadata (≈45 rows), hand-edited, commented | `portfolio_automation/artifact_registry.yaml` |
| Registry module | Load, validate, orchestrate; expose `required_artifacts()` | `portfolio_automation/artifact_registry.py` |
| Status artifact | Observe-only governance snapshot | `outputs/latest/artifact_registry_status.json` (LATEST namespace) |
| daily_run_status edit | Consume `required_artifacts()` instead of `_EXPECTED_ARTIFACTS`; output schema unchanged | `portfolio_automation/daily_run_status.py` |
| daily skill edit | Read status artifact first; gate confidence by `role` | `.claude/commands/daily-tool-analysis.md` |
| Tests | Registry/loader/validator/cadence/golden-output | `tests/test_artifact_registry.py` |
| Docs | Module doc + CHANGELOG + project_state step | `docs/artifact_registry.md`, etc. |

### Module API

```python
def load_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> dict:
    """Parse the YAML registry; return {} on missing/corrupt (fault-tolerant)."""

def required_artifacts(registry: dict | None = None) -> list[tuple[str, str, bool]]:
    """Return the (rel_path, label, required) triples daily_run_status needs —
    the exact shape of the old _EXPECTED_ARTIFACTS, derived from the registry."""

def validate_registry(registry: dict, artifacts_root: str | Path, now: datetime) -> dict:
    """Per-artifact {exists, fresh, stale, valid_json, status} + rollups
    (missing/stale/invalid/unattributed lists, severity counts, overall_status,
    operator_message). Pure; no I/O beyond reading the artifacts it checks."""

def run_artifact_registry(*, root=".", now=None, write_files=True) -> dict:
    """Load → validate → write status artifact. Never raises; degraded dict on error."""
```

`daily_run_status` imports `required_artifacts` and builds its `_EXPECTED_ARTIFACTS`
equivalent from it (or calls it directly where the tuple was used).

## 5. Registry row schema

Keyed by artifact filename (e.g. `quant_watch_status.json`):

```yaml
quant_watch_status.json:
  lens: quant_learning          # developer | quant_learning | market_discovery | risk_action | decision_core | meta_governance
  role: probe                   # source_of_truth | advisor | probe | telemetry | narrative
  required: true
  cadence: daily                # daily | weekend | weekly | monthly | yearly | on_demand
  producer: quant_watch_probes  # module/function that writes it
  consumers: [quant-watch-analysis, daily-tool-analysis]
  severity_if_missing: warning  # critical | warning | info
  # staleness_hours_override: 36   # optional; else derived from cadence
```

**Required fields:** `lens`, `role`, `required`, `cadence`, `producer`, `consumers`,
`severity_if_missing`. **Optional:** `staleness_hours_override`, `label` (defaults to a
title-cased filename), `notes`.

`consumers` may contain the sentinel `UNATTRIBUTED` where no consumer could be found —
this is surfaced debt, deliberately visible, not a guess.

Schema validation at load: every row has the 7 required fields with values in the
allowed enums; unknown lens/role/cadence/severity → the row is flagged
`schema_invalid` in the status artifact (not a crash).

## 6. The two models

### Cadence-derived staleness (Correction 3)

A single `CADENCE_MAX_AGE_HOURS` map drives staleness so multi-cadence artifacts don't
false-alarm:

| cadence | max age (h) | rationale |
|---|---|---|
| daily | 30 | ~1 cron cycle + slack |
| weekend | 100 | Fri→Mon gap |
| weekly | 192 | 8 days |
| monthly | 768 | 32 days |
| yearly | 9000 | ~375 days |
| on_demand | None | never auto-stale |

`stale = exists AND (now - mtime) > max_age`, where `max_age` is the per-row
`staleness_hours_override` if set, else the cadence default, else (on_demand) never.

### Severity → overall_status

- Any `severity_if_missing: critical` artifact missing-or-stale ⇒ `overall_status: red`.
- Else any `warning` missing-or-stale ⇒ `amber`.
- Else `green`.
- `info` artifacts contribute to counts but never raise status.

Status artifact shape:

```json
{
  "generated_at": "...", "observe_only": true, "schema_version": "1",
  "source": "artifact_registry",
  "overall_status": "amber",
  "counts": {"total": 45, "present": 43, "stale": 2, "invalid_json": 0,
             "missing_required": 0, "unattributed": 1, "schema_invalid": 0},
  "missing": [], "stale": [{"artifact":"...","cadence":"daily","age_hours":51}],
  "invalid_json": [], "unattributed": ["..."],
  "severity": {"critical": 0, "warning": 2, "info": 1},
  "by_lens": {"developer": {...}, "quant_learning": {...}, ...},
  "operator_message": "2 stale (warning): quant_watch_status, memo_delivery_status",
  "disclaimer": "Observe-only artifact-governance validator. Reads the registry + artifact mtimes; classifies coverage/freshness. Does not call APIs or mutate any decision, allocation, score, or portfolio state."
}
```

## 7. The observe-only invariant (codified, not reinvented)

The boundary already lives in CLAUDE.md ("decision artifacts are the source of truth;
GUI/memo/explanation are consumers only"). The registry makes it a *field*: `role`.

> **Invariant:** Only artifacts with `role: source_of_truth` may represent official
> portfolio actions. `probe` / `advisor` / `telemetry` / `narrative` artifacts inform
> analysis, warnings, explanations, and confidence — they may never independently
> create or override a buy/sell/hold.

`/daily-tool-analysis` reads `role` to enforce: downgrade confidence if a
`source_of_truth` row is missing/stale; mark the analysis *partial* if a required
probe is missing; never infer portfolio actions from a non-`source_of_truth` artifact.

## 8. daily_run_status invert + schema guard

- Remove `_EXPECTED_ARTIFACTS`; `daily_run_status` builds its expected-artifact list
  from `artifact_registry.required_artifacts()` (the `(rel_path, label, required)`
  triples), preserving its existing per-artifact row computation
  (`exists`/`mtime_iso`/`fresh_today`) and its counts.
- **Forbidden-change guard (`breaking_output_artifact_schemas`):** the registry's
  required-subset MUST reproduce the current `_EXPECTED_ARTIFACTS` set exactly. A
  golden-output test captures today's `daily_run_status` payload (minus volatile
  fields like `generated_at`/`mtime_iso`) and asserts it is unchanged after the
  invert. If the registry is missing/unloadable at runtime, `daily_run_status` falls
  back to a small built-in copy of the required list (degrade, never crash).

## 9. Observe-only / namespace / non-blocking

- `observe_only: true` hardcoded in the status artifact; module reads artifacts +
  registry, writes only `outputs/latest/artifact_registry_status.json`
  (`OutputNamespace.LATEST`). No decision/score/allocation/portfolio mutation.
- `run_artifact_registry` never raises (degraded status dict on any error).
- Pipeline integration is additive + try/except wrapped; the registry validator can be
  driven by the daily skill (like quant-watch) — no `run_daily_safe.sh` change required
  in v1, though the module is written pipeline-ready.

## 10. Testing (`tests/test_artifact_registry.py`)

1. `load_registry` returns {} on missing/corrupt YAML (no raise).
2. Every row in the shipped `artifact_registry.yaml` has the 7 required fields with
   in-enum values (guards future hand-edits).
3. `required_artifacts()` returns the `(path,label,required)` shape.
4. **Golden output:** `daily_run_status` payload is byte-identical (minus volatile
   fields) before/after the invert — captured as a fixture.
5. Validator flags a missing `critical` artifact → `overall_status red`.
6. Validator flags a missing/stale `warning` artifact → `amber`; clean → `green`.
7. **Cadence-staleness:** a `weekly` artifact aged 40h is NOT stale; a `daily` artifact
   aged 51h IS stale; an `on_demand` artifact is never stale.
8. Invalid-JSON artifact → listed in `invalid_json`.
9. `consumers: [UNATTRIBUTED]` row → listed in `unattributed`.
10. `run_artifact_registry` writes the status artifact (observe_only true) and never
    raises on missing artifacts (degraded path).
11. Schema-invalid row (bad enum) → flagged `schema_invalid`, no crash.

Targeted first (`pytest -q tests/test_artifact_registry.py`), then full suite.

## 11. Analysis + Health pairing (CLAUDE.md requirement)

- Cadence: daily → owning skill `/daily-tool-analysis`.
- Lens: developer / meta-governance.
- Pairing: `artifact_registry_status.json` becomes the FIRST artifact the daily skill
  reads (Step 1) + a Step-4 heartbeat line; the `unattributed` + `missing`/`stale`
  lists route to `portfolio-discovery-health` / `portfolio-resolver-investigator` on
  non-green. The registry also satisfies the "every artifact has a consumer" corollary
  for the whole corpus.

## 12. Consumer attribution method

For each artifact, derive `consumers` by grep'ing `.claude/commands/*.md`,
`.claude/agents/*.md`, `.claude/skills/**`, and `gui_v2/**` for the artifact filename.
Anything with no hit → `consumers: [UNATTRIBUTED]`. This produces the first real
producer-without-consumer debt list as a side effect of building the registry.

## 13. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Invert breaks daily_run_status schema | Golden-output test (#4) + runtime fallback list |
| Registry drifts from reality as new artifacts ship | Validator's `unattributed` + a future test that every `outputs/latest/*.json` has a registry row (corollary enforcement) |
| Flat staleness false-alarms | Cadence-derived map (§6) + per-row override |
| Curation effort for 45 rows | Mostly mechanical; consumers grep-derived; UNATTRIBUTED for unknowns |
| YAML parse dependency | `yaml.safe_load` already a repo dep (signal_registry.yaml precedent); fault-tolerant load |
| Scope creep (rollups/GUI) | Explicitly deferred (§2) |

## 14. Out of scope / follow-ups
- Lens-rollup summary producers (`developer_health_summary`, …) generated *from* the registry.
- GUI "System Coverage" card consuming `artifact_registry_status.json`.
- A test asserting every `outputs/latest/*.json` has a registry row (full corollary enforcement) — once the registry is proven.
- Per-artifact internal JSON-schema validation.
