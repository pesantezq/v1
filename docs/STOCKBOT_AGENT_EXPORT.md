# StockBot Agent Production Export

Status: **implemented, inert** (not wired into the daily cron). Local development
only. No network transport, no Hetzner integration, no Prime invocation, no
production invocation, no production behaviour change.

Module: `portfolio_automation/agent_export.py`
CLI: `scripts/run_agent_export.py`
Tests: `tests/test_agent_export.py`

## Purpose

Freeze an allowlisted, sanitized, hash-verified, immutable snapshot of a
completed StockBot run so a future Agent Lab / Prime consumer can be handed a
**deterministic, trustworthy, read-only** production-state input. This subsystem
builds and validates snapshots; transport is a later phase.

## Trust model

```
StockBot runtime artifacts (outputs/**)
   │  allowlist  +  secret guard  +  path guard
   ▼
Agent Export builder  (portfolio_automation.agent_export.build_snapshot)
   │  atomic temp build → sha256 per file → manifest fingerprint → atomic rename
   ▼
immutable snapshot dir + manifest.json   (read-only after finalize)
   │
   ▼
validate_agent_snapshot()   ← the consumer's proof the input is trustworthy
```

The consumer NEVER trusts a snapshot it has not validated. Validation recomputes
every file hash and the snapshot fingerprint; any mismatch → `valid: False`.

## Snapshot layout

```
<output_root>/agent_export/
  latest.json                     # metadata pointer (id, hash, git sha) — NOT a copy
  snapshots/
    <snapshot_id>/                # immutable (files 0444, dirs 0555)
      manifest.json
      artifacts/
        <logical_name>/<original_filename>
  .tmp/                           # transient build area (atomic rename source)
```

`snapshot_id` = `snap-<sanitized production_run_id>` (falls back to
`snap-<git_sha[:12]>`). Deterministic given the run identity.

## Allowlist (exactly what can cross)

Allowlist-based, never denylist. Defined in `ALLOWLIST` in the module. Categories
and logical names (source paths relative to the artifacts root):

| Category | Logical names |
|---|---|
| core_decisions | decision_plan* (required), system_decision_summary* (required), decision_plan_md, decision_explanations, portfolio_snapshot |
| discovery | watchlist_signals, theme_signals, news_intelligence, market_narrative_daily |
| governance | artifact_registry_status, active_strategy_selection, approved_ranking_config, approved_allocation_policy |
| system_health | daily_run_status, pipeline_run_status, fmp_budget_status, risk_delta |
| learning | retune_impact, confidence_calibration, quant_watch_status, pattern_efficacy_monthly |
| context | crowd_intelligence, ai_budget_summary |
| memo | daily_memo_md, daily_memo_txt, memo_datasets |

`*` = **required**: a snapshot cannot be built without `decision_plan.json` and
`system_decision_summary.json` (the two `source_of_truth` criticals). Every other
entry is optional — its absence is a recorded **gap** (AMBER), not a failure.

Adding an artifact = add one `AllowlistEntry`. Prefer sanitized *summary/status*
artifacts over raw mutable ledgers.

## Exclusions (secret boundary)

The export can NEVER contain secrets. Enforced by `is_forbidden_name()` applied
to allowlist entries, to every file written, and during validation:

- Forbidden basenames: `.env`, `auth.json`, `credentials.json`, `id_rsa`,
  `id_ed25519`, `.netrc`, `.git-credentials`, `.bash_history`, `secrets.json`, …
- Forbidden suffixes: `.pem`, `.key`, `.p12`, `.pfx`, `.crt`, `.cer`, …
- Forbidden substrings: `secret`, `token`, `password`, `credential`, `api_key`,
  `oauth`, `.env`, …

Values are never read or logged — only filenames are inspected. Because the
export is allowlist-based, no secret-bearing file is even a candidate; the name
checks are defence-in-depth against a bad allowlist edit.

## Path boundary

`_resolve_within(root, path)` realpath-resolves every source (following symlinks)
and requires it to stay inside the approved artifacts root. This fails closed on:
`../` traversal, absolute paths pointing outside the root, and symlinks whose
target escapes the root.

## Manifest schema

```jsonc
{
  "schema_version": "1",
  "snapshot_id": "snap-<run-id>",
  "created_at": "2026-08-08T09:05:12Z",     // UTC; excluded from fingerprint
  "observe_only": true,
  "code_identity":  { "production_git_sha": "<40-hex>" },
  "run_identity":   { "production_run_id": "...", "run_started_at": "...", "run_completed_at": "..." },
  "health":         { "overall_status": "GREEN|AMBER|RED", "warnings": [...], "critical_failures": [...] },
  "artifacts": [                             // sorted by logical_name
    { "logical_name": "decision_plan", "category": "core_decisions",
      "producer": "decision_engine", "source_path": "outputs/latest/decision_plan.json",
      "snapshot_path": "artifacts/decision_plan/decision_plan.json",
      "sha256": "sha256:...", "size_bytes": 1234, "required": true,
      "generated_at": "2026-08-08T09:04:59Z" }
  ],
  "gaps": [ { "logical_name": "...", "reason": "source_absent", "required": false } ],
  "snapshot_hash": "sha256:..."             // the fingerprint (see below)
}
```

## Hash contract

- **Per artifact:** `sha256:` of the copied file bytes.
- **Snapshot fingerprint** (`snapshot_hash`): SHA-256 over canonical JSON
  (sorted keys, compact) of:
  `{schema_version, code_identity, run_identity, artifacts:[{logical_name,
  snapshot_path, sha256, size_bytes, required} sorted by logical_name],
  gaps:[logical_name sorted]}`.
  **Excludes** `created_at` (wall clock) and `snapshot_hash` itself (no
  self-reference). Therefore identical frozen inputs always produce an identical
  fingerprint (determinism), while any tamper of identity, an artifact hash, or
  the file set is detected.

Validation recomputes both and compares; mismatch ⇒ invalid.

## Atomicity

Build happens entirely inside `agent_export/.tmp/<uuid>/`. Only after all files
are copied, hashed, the manifest written, and the manifest self-validated does a
single `os.replace(tmp, final)` publish it (atomic rename on the same
filesystem). A crash at any earlier point leaves nothing under `snapshots/`, and
the temp dir is removed in a `finally`. Immutability (read-only mode bits) is
applied to the finalized dir *after* the rename.

Existing id: identical content ⇒ idempotent no-op; different content ⇒
`SnapshotExistsError` (never a silent overwrite).

## Provenance

`code_identity.production_git_sha` and the `run_identity` block are recorded in
the manifest and folded into the fingerprint. The CLI captures the git sha from
`--git-sha` or a LOCAL `git rev-parse HEAD` (no network). A consumer must record
the exact `snapshot_hash` + `production_git_sha` it analyzed — never analyze one
SHA and act on another.

## Consumer validation

`validate_agent_snapshot(dir) -> {"valid": bool, "errors": [...], "snapshot_id"}`
verifies schema version, required manifest fields, required artifacts present,
per-file hash + size, expected path set, no unexpected files, no forbidden
filenames, the recomputed fingerprint, git-sha/run-id presence, and health
metadata. It is strictly read-only (a test asserts it does not mutate mtimes).

`compare_shas(production_sha, shadow_sha, ancestry=None)` returns
`MATCH | SHADOW_BEHIND | SHADOW_AHEAD | UNKNOWN` — pure, runs no git and no
network (ordering needs an explicit `ancestry` list).

## Health artifact

`outputs/policy/agent_export_health.json` (producer
`agent_export.run_agent_export_health`), registered in both registries. Status:
GREEN (current valid snapshot) / AMBER (none yet, stale, or optional gap) / RED
(validation failure or security-boundary violation).

## Explicit deferrals

- **No network transport** — building only; secure copy to the home lab is a
  later phase.
- **No Hetzner integration** — nothing reads or writes production.
- **No Prime execution** — Prime is not invoked.
- **No production invocation** — the exporter never runs the pipeline.
- **Not wired into the daily cron** — see the pipeline hook below; it ships OFF.

## Future daily-pipeline hook (documented, NOT enabled)

`scripts/run_daily_safe.sh` finalizes the run manifest at **Stage 14**
(`run_manifest.json` flips to complete only when the whole run finished). The
exporter should run as a new **Stage 15 — Agent export**, gated behind an
env flag that defaults OFF, and only when Stage 14 reported the run complete:

```bash
# Stage 15 — Agent export (DEFAULT-OFF; observe-only; reads the finalized corpus)
if [ "${STOCKBOT_AGENT_EXPORT_ENABLED:-0}" = "1" ]; then
  python scripts/run_agent_export.py \
      --run-id "${RUN_ID}" --write-health || true   # non-blocking
fi
```

Rationale: it must run last, after every artifact is finalized and health is
validated, so the snapshot captures a whole, self-consistent run. It writes only
its own snapshot tree + health artifact and never mutates decision/score state.
Enabling it is a separate, sanctioned change — not part of this mission.
