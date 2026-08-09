# StockBot Agent Export — Frozen Production Snapshot Contract

Status: **implemented, local export only. NOT wired into the daily pipeline.**
Added 2026-08-09. Module: `portfolio_automation/agent_export/`.
CLI: `python scripts/run_agent_export.py`.

---

## 1. Purpose

The Agent Lab needs to compare three things:

```text
CODE INTENT        what the repository says the system should do
PRODUCTION RUNTIME what actually executed, on which commit, with which config
ACTUAL OUTPUTS     the artifacts that run produced
```

Doing that today would mean handing an analysis agent broad read access to
`/opt/stockbot` — which is also where `.env`, the broker credentials, the live
SQLite databases, and the mutable artifacts of the *next* run live. That is far
more authority than the task needs.

This subsystem removes the need for that access. It converts one completed
production run into a package that is:

```text
frozen          immutable after finalisation
versioned       explicit schema_version, refused if unrecognised
hash-verified   SHA-256 per artifact + two manifest fingerprints
read-only       a pure downstream sink; nothing in the pipeline reads it
sanitized       allowlist-only; secrets structurally cannot enter
self-describing the manifest states what it contains, omits, and came from
```

The Agent Lab then analyses the snapshot, not the server.

---

## 2. Trust model

```text
   Production StockBot (/opt/stockbot, root, credentials, live DBs)
            │
            │  allowlist-only copy · SHA-256 · atomic finalise
            ▼
   Frozen snapshot  (outputs/agent_export/snapshots/<snapshot_id>/)
            │
            │  ← trust boundary. Everything downstream is untrusted-by-default
            │     and MUST call validate_agent_snapshot() before believing it.
            ▼
   Agent Lab / Prime  (analysis only, no production authority)
```

Direction of authority is one-way. A snapshot carries **evidence**, never
**permission**. There is no code path from the export lane back into a decision,
allocation, score, approval, or portfolio mutation, and every manifest asserts
this in fields the validator enforces:

```json
{ "observe_only": true, "feeds_decision_engine": false,
  "grants_production_authority": false }
```

Flipping any of them makes the snapshot fail validation.

### Prime authority

**Prime remains A0 and cannot execute production through this feature.** This
subsystem grants read-only evidence. It does not create a queue, a trigger, a
callback, or any writable channel back into production. Nothing here changes the
human-gated production model described in `CLAUDE.md` → Observe-Only Default.

---

## 3. What crosses the boundary

The allowlist is **ALLOW-explicit, DENY-everything-else**. It is the inverse of
"copy the tree and filter out secrets" — a deny-list silently leaks every
sensitive file nobody thought to name, which is unacceptable for data that will
eventually leave the host.

Source of truth: `portfolio_automation/agent_export/allowlist.py`
(`ARTIFACT_ALLOWLIST`). Currently **58 artifacts, 10 of them required**, grouped
by category:

| Category | Contents |
|---|---|
| `core_decision` | `decision_plan.json`†, `decision_plan.md`†, `system_decision_summary.json`†, `daily_memo.md`†, decision explanations, triage, memo datasets, decision authority |
| `portfolio_risk` | `portfolio_snapshot.json`†, `risk_delta.json`†, capital plan, cash deployment, scenario risk, correlation/exit/earnings/kelly/vol advisors |
| `watchlist_discovery` | watchlist signals, watch candidates, market opportunities, top100 daily, theme signals, `discovery_pulse_status.json`†, scraped-intel run summary, scanner recovery canary |
| `governance` | `run_manifest.json`†, `artifact_registry_status.json`†, operator action queue, strategy review queue, daily input snapshot, auto-approval + auto-apply audit summaries, active strategy selection |
| `health` | `daily_run_status.json`†, pipeline wiring, semantic liveness, quant watch, data quality, regime coverage, memo coherence, pipeline run status |
| `outcome_learning` | decision outcome summary, recommendation evaluation, confidence calibration, alpha attribution, quant feedback, retune impact, pattern efficacy, strategy catalog, improvement scorecard |
| `context` | crowd intelligence (+unified), news intelligence, market narrative, institutional intelligence, AI budget summary, FMP budget status |

† = **required**. A missing required artifact refuses the build outright.

Only paths under these roots are eligible at all:

```text
outputs/latest  outputs/policy  outputs/portfolio  outputs/performance
outputs/sandbox outputs/simulation  outputs/weekly_etf_bundles
```

Notably **not** eligible: the repo root (`.env`, `config.json`), `data/`,
`logs/`, `.git/`, `.ssh/`, or anything outside the repo.

### Provenance is joined, not restated

Copied artifacts are **not** re-registered as new producers. For each exported
artifact the manifest joins `producer`, `lens`, and `role` from
`artifact_registry.yaml`; entries not yet in the registry declare an explicit
`producer` in the allowlist. A test enforces that every entry is attributable
one way or the other, so there stays exactly one source of truth for who
produces what.

---

## 4. What is deliberately excluded

Recorded in `manifest.excluded` with a reason code — as **path patterns and
prose**, never values. The exporter never reads an excluded file in order to
report that it withheld it.

| Reason | Withheld |
|---|---|
| `SECRET` | `.env`, `.env.*`, `.env.bak-*`; `config.json` (raw config may embed endpoint credentials — the run's `config_hash` is carried instead, which proves *which* config produced the run without disclosing it) |
| `CREDENTIAL` | private keys, `id_*`, `*.pem`, `*.key`, deploy identities |
| `PII` | `schwab_positions.json`, `schwab_portfolio_snapshot.json`, `schwab_tax_lots.json` (raw broker state can carry account identifiers and tax lots — the sanitized `portfolio_snapshot` goes instead); email views, prompts, and delivery logs (recipient addresses) |
| `MUTABLE_INTERNAL_STATE` | `outputs/policy/*.jsonl` append-only ledgers, `data/*.db` — their **derived summaries** (`*_audit.json`, `decision_outcome_summary.json`) are exported instead |
| `NOT_AGENT_RELEVANT` | `logs/*` (tracebacks can incidentally embed secrets), `.git/*`, `outputs/history/*` |

### Structural enforcement

The boundary is enforced on **names and resolved locations**, not on content, so
refusing a secret never requires handling one. `resolve_source_path()` rejects,
in order: absolute/drive-qualified paths; traversal (`..`) and dotfile
components; forbidden names and glob patterns; paths outside the permitted
roots; **anything that escapes containment after full symlink resolution**; and
non-regular files.

That symlink step is the important one. A symlink at
`outputs/latest/harmless.json` pointing to `/opt/stockbot/.env` passes every
name check — it is caught because the *resolved* path is not inside a permitted
root.

---

## 5. Manifest schema (`schema_version` `"1.0"`)

```jsonc
{
  "schema_version": "1.0",
  "artifact_type": "agent_production_snapshot",
  "observe_only": true,                 // governance invariants — validator-enforced
  "feeds_decision_engine": false,
  "grants_production_authority": false,
  "finalized": true,

  "snapshot_id": "2026-08-08_daily_official__55205f56b319",
  "created_at": "2026-08-09T00:52:11+00:00",

  "production": {                       // STABLE: fixed when the run finished
    "run_id": "2026-08-08_daily_official",
    "run_started_at": "...", "run_completed_at": "...",
    "run_status": "complete",
    "pipeline_mode": "daily",
    "config_hash": "e9da1c3b…",
    "runtime": { "python": "3.12.3", "platform": "Linux", "host": "stockbot-vps" },
    "production_git_sha": "55205f56b319a63ebaac1b09ba9ecdd7882d5047",
    "production_git_sha_recorded": "55205f56",
    "production_git_sha_source": "run_manifest"
  },

  "export_context": {                   // VOLATILE: observed when the EXPORT ran
    "head_git_sha_at_export": "dc0070a8…",
    "git_branch": "main",
    "code_moved_since_run": true,
    "working_tree": { "code_clean": false, "code_modified_paths": ["…"],
                      "artifact_churn_count": 11, "untracked_count": 6 },
    "degradations": ["working_tree_code_modified", "head_moved_since_run"]
  },

  "health": {                           // health of the RUN, not of the export
    "status": "GREEN|AMBER|RED",
    "critical_failures": [], "warnings": ["daily_run_status:ok_with_warnings"]
  },

  "artifacts": [ {
    "name": "decision_plan",
    "source_path": "outputs/latest/decision_plan.json",
    "snapshot_path": "artifacts/core_decision/decision_plan.json",
    "sha256": "…", "size_bytes": 116821, "required": true,
    "category": "core_decision", "producer": "decision_engine",
    "lens": "decision_core", "role": "source_of_truth",
    "generated_at": "…", "note": "…"
  } ],

  "excluded": [ { "name": "env_files", "source_pattern": ".env, .env.*",
                  "reason": "SECRET", "detail": "…" } ],
  "missing_optional": [], "missing_required": [],
  "counts": { "artifacts": 58, "required_present": 10, "total_bytes": 3275053 },

  "content_sha256": "…",
  "snapshot_sha256": "…"
}
```

Ordering is deterministic throughout (artifacts by `name`, exclusions by
`(reason, name)`, missing lists sorted). All timestamps are UTC ISO-8601 and are
**injected by the caller** — no module here calls `datetime.now()` internally, so
builds are reproducible.

### Why two fingerprints

| | Covers | Job |
|---|---|---|
| `snapshot_sha256` | whole manifest except `created_at` + the digest fields | **Tamper detection.** Altering *any* recorded fact — an artifact hash, the run id, the git SHA, the health verdict, an exclusion reason, the working-tree observation — invalidates the snapshot. |
| `content_sha256` | additionally excludes `export_context` | **Content identity.** A pure function of (which run, which code, which artifacts, what health). This is what the duplicate gate compares. |

A single digest over everything would be wrong here. The VPS doubles as a dev
environment (`CLAUDE.md` → Operating Mode: `dev_on_vps`), so HEAD and the working
tree routinely move between the 09:00 cron run and any later export. Folding
those observations into content identity would make an ordinary re-export of an
*unchanged* run look like tampering — a false integrity alarm, and a false alarm
is how a real one gets ignored. Splitting the digests keeps re-export idempotent
while `export_context` stays cryptographically sealed against edits.

For the same reason, `health` grades **the run** only. A run that finished
cleanly at 09:00 does not become less healthy because somebody edited a file at
17:00; export-time degradations surface in `export_context.degradations` and in
the export lane's own health artifact.

---

## 6. Provenance: which code produced this?

Two SHAs are recorded, and they are not the same question:

- **`production.production_git_sha`** — resolved from
  `run_manifest.source_commit`, the commit recorded when the run *started*. This
  is the authoritative "what produced these artifacts" answer, expanded from the
  8-char recorded form to the full 40-char SHA via a local `git rev-parse`.
  `production_git_sha_source` says how it was obtained:
  `run_manifest` / `run_manifest_unverified` / `repo_head` / `unknown`.
- **`export_context.head_git_sha_at_export`** — HEAD when the exporter ran.

Working-tree state is classified rather than judged: modifications under
`outputs/`, `data/`, `logs/`, `scraped_intel/`, `.agent/` are **expected artifact
churn** (writing artifacts is what a run does); anything else is source
contamination and yields a `working_tree_code_modified` degradation. Only
**paths** are recorded, never diffs or contents — a path list is safe to publish,
a diff could contain anything.

Contamination is AMBER, not fatal, because the current deployment contract says
the VPS *is* an active dev environment. If that contract changes to
`read_only_ops`, tighten this to a refusal.

An undeterminable `production_git_sha` **is** fatal: a snapshot that cannot say
which code produced it has failed at its one job.

---

## 7. Atomicity — how partial snapshots are prevented

```text
temp build dir (.build-XXXX, inside the snapshots root)
  → copy each allowlisted artifact, hash source, re-hash the COPY, compare
  → gate: all required artifacts present?
  → gate: run health not RED?
  → write manifest.json
  → re-read from disk and run the FULL validator against the temp dir
  → os.rename(temp, snapshots/<snapshot_id>)     ← atomic, indivisible
  → write latest.json pointer
```

Governing property: **a snapshot directory exists if and only if it is complete
and verified.**

- The validator runs against the temp directory *before* promotion, so a snapshot
  cannot reach its final path unless it would already pass the exact check a
  consumer will run. The build gate and the trust gate are the same code, so they
  cannot drift apart.
- `os.rename` within one filesystem is indivisible — the directory is never
  observable half-populated under its final name.
- Any failure removes the temp directory (`finally`), so an aborted build leaves
  nothing behind. Should a process be hard-killed mid-build, the surviving
  `.build-*` directory is excluded from `list_snapshots()` and fails validation
  (no `manifest.json`), so it can never be mistaken for a valid export.
- Copies are verified: the source is hashed, copied, and the copy re-hashed. The
  manifest records the hash of what is actually *inside* the snapshot.

### Refuse vs degrade

The rule is: **refuse when the snapshot would be misleading, degrade when it
would merely be incomplete.**

| Refused (`SnapshotBuildError`, nothing written) | Degraded (AMBER snapshot still built) |
|---|---|
| run is not `status="complete"` | an optional artifact was absent |
| `run_id` undeterminable | HEAD moved on since the run |
| `production_git_sha` unknown | uncommitted source edits present |
| a required artifact missing / unreadable / copy mismatch | run reported `ok_with_warnings` |
| any secret-boundary violation | |
| run health RED | |
| same `snapshot_id` already exists with different content | |

### Immutability

A finalised snapshot is never modified. Re-exporting the same (run, commit)
recomputes the id deterministically and then:

- **identical `content_sha256`** → idempotent no-op, the directory is not
  rewritten (verified: mtime unchanged);
- **different `content_sha256`** → hard stop with both digests. A *completed*
  run's artifacts changing underneath us is an integrity anomaly the operator
  must see, not something to silently overwrite. The temp build is discarded;
  the existing snapshot is left exactly as it was.

---

## 8. Validation — what makes a snapshot trustworthy

`validate_agent_snapshot(snapshot_dir)` is the consumer's gate. It is
**strictly read-only** (verified by a test comparing sizes, mtimes and bytes
before and after repeated runs) and **fails closed** — there is deliberately no
"warn and continue" tier, because a warning tier is how a corrupt snapshot gets
analysed anyway.

It checks:

1. directory exists; `manifest.json` present, parses, is an object;
2. all required top-level fields present;
3. `schema_version` is recognised (an unknown version is refused, not
   best-effort parsed);
4. `artifact_type` correct; `finalized` is `true`;
5. governance invariants (`observe_only` / `feeds_decision_engine` /
   `grants_production_authority`) hold;
6. `snapshot_id` non-empty, matches the directory name, matches
   `expect_snapshot_id` when supplied;
7. `production.run_id` present; `production_git_sha` present and not `unknown`;
8. `health.status` ∈ {GREEN, AMBER, RED}; `missing_required` empty;
9. **both** fingerprints recompute correctly;
10. every artifact: declared inside `artifacts/`, no forbidden path component,
    resolves inside the snapshot, exists, size matches, **SHA-256 matches**;
11. required-artifact coverage against the allowlist;
12. **no unexpected files anywhere** in the tree — an undeclared file is exactly
    how something unreviewed rides across the boundary;
13. no forbidden filename anywhere in the tree.

Defence in depth: a forger who rewrites an artifact hash must also refresh both
digests, and the per-artifact rehash still catches them.

---

## 9. Runtime/code consistency (`consistency.py`)

`compare_shadow_to_production(shadow_sha, production_sha, ...)` classifies the
Agent Lab's local shadow checkout against a snapshot:

| Status | Meaning |
|---|---|
| `MATCH` | identical commit — `analysis_safe: true` |
| `SHADOW_BEHIND` | shadow is an ancestor of production |
| `SHADOW_AHEAD` | production is an ancestor of shadow |
| `UNKNOWN` | missing SHA, no ancestry probe, diverged histories, or commits absent locally |

Abbreviated SHAs compare on the shorter width, so an 8-char
`run_manifest.source_commit` does not read as a divergence. `analysis_safe` is
`true` only for `MATCH`.

**It never fetches, pulls, checks out, or writes.** Every git call is read-only
against the local object store. Refreshing the shadow is a separate future step
(§12) — keeping comparison and mutation apart means an analysis run can never
move the code out from under itself. The probe also distinguishes "that commit
isn't in my object store" (`UNKNOWN`) from "not an ancestor" (a real
divergence), so a shallow clone cannot masquerade as diverged history.

---

## 10. Health integration

`outputs/policy/agent_export_health.json` — producer
`portfolio_automation/agent_export/health.py`, registered in
`artifact_registry.yaml`.

| Status | When |
|---|---|
| `GREEN` | a snapshot exists, validates, and is current (≤ 36 h) |
| `AMBER` | no snapshot yet; snapshot stale; optional artifacts absent; run reported warnings; export-time provenance degradations; pointer target missing |
| `RED` | a snapshot exists but **fails validation** — hash mismatch, invalid manifest, missing required artifact, unexpected file, or a secret-boundary breach |

RED is reserved for *verifiable corruption*. "Nothing exported yet" is AMBER:
grading a fresh install RED would train the operator to ignore the signal.

Tracked fields: `latest_snapshot_id`, `latest_snapshot_age_hours`,
`production_git_sha`, `production_run_id`, `required_artifacts_present` /
`_expected`, `hash_validation`, `schema_validation`, `snapshot_health_status`,
`snapshot_count`, `status`, `warnings`, `errors`.

### Layout

```text
outputs/agent_export/
  latest.json                       ← POINTER record, never a copy
  snapshots/
    <run_id>__<git_sha12>/
      manifest.json
      artifacts/<category>/<filename>
```

`latest` is a pointer, not a mirrored directory. A mutable `latest/` holding
duplicated artifacts would be a second, silently-drifting source of truth and
would break the immutability guarantee the moment it refreshed. The pointer keeps
exactly one copy of every artifact and makes the indirection explicit. It is the
only mutable element of the export lane.

---

## 11. Proposed daily-pipeline integration — NOT ENABLED

**Nothing was added to `scripts/run_daily_safe.sh`.** The daily schedule is
unchanged. The recommended insertion point, for separate approval:

Immediately **after Stage 14 — "Run context (manifest complete)"** (currently the
final stage, `scripts/run_daily_safe.sh:517-523`), as a new Stage 15:

```bash
# Stage 15 — Agent Lab export (observe-only, non-blocking). Runs AFTER the
# manifest is stamped complete, because the builder REFUSES any run that is not
# status="complete" — placing it earlier would make it refuse every time.
run_aux_stage "Agent Lab export" \
    python "${REPO_ROOT}/scripts/run_agent_export.py" --root "${REPO_ROOT}"
```

Why there:

- every artifact the allowlist reads has been written by then (the last artifact
  producer is Stage 13b);
- `run_manifest.status` only flips to `complete` at Stage 14, and the builder
  gates on exactly that, so any earlier placement fails by construction;
- `run_aux_stage` makes it non-blocking — an export failure can never break the
  decision pipeline, matching the observe-only rule for new layers.

**On activation**, also: flip both registry rows to `cadence: daily` with
`consumers: [daily-tool-analysis]` / `consumer_status: consumed`, and add
`agent_export_health.json` to `.claude/commands/daily-tool-analysis.md`
(Step 1 artifacts read + Step 3 dispatch + Step 4 body grammar) per the
Analysis+Health requirement in `CLAUDE.md`. Until then the rows are honestly
marked `on_demand` / `diagnostic_only` rather than claiming coverage that does
not exist.

---

## 12. Future transport — NOT IMPLEMENTED HERE

> **Transport to the home Agent Lab is NOT implemented in this feature.**
> There is no SSH, rsync, scp, HTTP, tunnel, upload, or any other network code in
> `portfolio_automation/agent_export/` or `scripts/run_agent_export.py`. This
> subsystem only ever writes to the local filesystem.

The intended future flow, for design reference only:

```text
1. production builds the frozen export          (implemented — this feature)
2. an observer exposes the snapshot read-only   (not implemented)
3. the home Agent Lab pulls the exact completed snapshot
4. the manifest is validated                    (implemented — validator.py)
5. the local Git shadow is refreshed to production_git_sha
6. SHA comparison must return MATCH             (implemented — consistency.py)
7. code + runtime state are frozen together
8. Prime begins analysis
```

Prime must not improvise SSH/git/rsync operations. Orchestration should be one
deterministic `refresh-agent-inputs` command with a narrow contract, not
ad-hoc agent-issued shell.

Explicitly deferred: Hetzner observer user, SSH transfer, Tailscale, Prime
connectivity, remote rsync/scp, Prime-triggered production runs, GitHub write,
agent job queue, A1 worktrees, A2 remediation, Docker, MLflow, TradingAgents,
FinRobot, OpenHands.

---

## 13. Known gaps

Stated explicitly rather than left for a reader to discover:

- **No intra-run trace.** The snapshot captures inputs and outputs, not the
  execution path between them (stage timings, branch decisions inside
  `decision_engine.py`). Prime can compare intent to outcome, but cannot observe
  *how* a decision was reached beyond what `decision_explanations.json` and
  `decision_triage.json` record.
- **Config content is withheld by design.** Only `config_hash` crosses. Prime can
  detect that config changed between runs, but cannot see *what* changed. If
  config-diff analysis is later required, the answer is a sanitized config
  projection with credential fields dropped — not exporting `config.json`.
- **Append-only ledgers are summarised, not exported.** Analyses needing
  event-level granularity (per-decision outcome sequences) will need a purpose-
  built sanitized projection.
- **Single-run scope.** Cross-run trend analysis needs multiple snapshots
  retained; no retention or pruning policy is implemented yet.
- **No retention limit.** Snapshots accumulate at ~3.3 MB each. A pruning policy
  should be added before this runs daily.

## 14. Tests

```bash
python -m pytest -q tests/test_agent_export_allowlist.py \
                   tests/test_agent_export_builder.py \
                   tests/test_agent_export_validator.py \
                   tests/test_agent_export_health.py
```

149 tests, fully hermetic — each builds its own repo, git checkout, and run
manifest under `tmp_path`. Nothing reads `/opt/stockbot`, which is deliberate:
this code is meant to run on the Agent Lab machine too, and the test suite is
where that portability is proven.
