# Run Manifest, Artifact Lineage & Atomic Writes (Phase 1)

Status: **foundation shipped** on `feat/complete-simulation-quant-governance-loop`.
Observe-only. No decision/allocation/score/portfolio state is mutated.

This is the run-integrity foundation for the simulation/quant/governance program:
every pipeline run is reproducible and every critical artifact is traceable to
one coherent run, so mixed-run / stale / partial artifacts can be rejected.

## 1. Run identity

`portfolio_automation/run_manifest.py` reuses the stable id from
`run_status.make_run_id(mode, generated_at=…)` →
`YYYY-MM-DD_<mode>_official`. It is **deterministic for a given (date, mode)**,
so a same-day rerun is last-wins (idempotent), not a new run (Iron rule 8).

## 2. Run manifest

`build_manifest(...)` / `begin_run(...)` / `complete_run(...)` produce
`outputs/policy/run_manifest.json` with:

| field | meaning |
|---|---|
| `run_id` | the coherent id every artifact of the run carries |
| `started_at` / `completed_at` | ISO timestamps (caller-supplied; pure) |
| `data_as_of` | point-in-time the inputs represent |
| `source_commit` | git HEAD short sha (`unknown` if git absent) |
| `config_hash` | sha256 of `config.json` (`missing`/`unreadable` degrade) |
| `pipeline_mode` | `daily` / `weekly` / … |
| `runtime` | python + platform + host fingerprint |
| `upstream_freshness` | per-source freshness map |
| `status` | `running` → `complete` \| `failed` |
| `failure_stage` | stage name when `status == failed` |

**Lifecycle:** `begin_run()` writes `status="running"`; `complete_run()` stamps
the terminal status. `is_complete(manifest)` is **True only for `complete`** — a
`failed` (or still-`running`) manifest is never treated as authoritative. This
is the **complete-run guard**: consumers check `is_complete(read_manifest(root))`
before trusting `outputs/latest`.

## 3. Mixed-run guard

`coherent_run_ids(expected_run_id, artifacts)` returns False if any artifact
carries a different `run_id` — or none at all (degrade honestly). Production
consumers use it to refuse silently combining artifacts from different runs
(Iron rules 4, 7).

## 4. Artifact lineage envelope

`portfolio_automation/next_stage/contracts.lineage(...)` returns the canonical
provenance keys to splat into `observe_only_envelope`:

```python
env = observe_only_envelope(now, **lineage(
    run_id=rid, data_as_of=as_of, producer="decision_engine",
    source_commit=sha, config_hash=h, upstream_refs=["decision_plan.json"]))
```

Keys: `run_id, data_as_of, producer, source_commit, config_hash,
upstream_refs, quality, freshness`. Additive — existing envelope callers are
unaffected; the safety flags (`observe_only`, `no_trade`) can never be flipped
by lineage fields.

## 5. Atomic writes

`data_governance.safe_write_text/json` now serialize to a temp file in the
**same directory**, then `os.replace()` (atomic rename). An interrupted write
never leaves a valid-looking partial artifact and never clobbers a prior good
artifact; the temp is cleaned up on any failure (no `.tmp` debris).

## Tests

- `tests/test_run_manifest.py` — identity determinism, config-hash sensitivity,
  manifest fields, begin/complete lifecycle, failed-run-not-complete,
  mixed-run detection, lineage helper + envelope-safety.
- `tests/test_atomic_writes.py` — roundtrip, no temp leftover, and the keystone
  interrupted-`os.replace` test (original intact + temp cleaned).

## Remaining Phase 1 wiring (next commits)

1. `begin_run()` at the start of `run_daily_safe.sh` + `complete_run()` at the
   end (records `failure_stage` on early exit) — Phase 12 cadence integration.
2. Stamp `lineage(...)` onto the critical production artifacts
   (`decision_plan`, `system_decision_summary`, `risk_delta`, memo).
3. Extend the artifact-registry validator to check lineage presence + run_id
   coherence (validate **more than existence**) and surface it in
   `daily_run_status` / pipeline status.
