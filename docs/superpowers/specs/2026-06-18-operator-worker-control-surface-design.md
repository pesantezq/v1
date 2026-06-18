# Operator-Worker Control Surface — Design Spec

Date: 2026-06-18
Status: approved (brainstorm + hardening refinements) → pending implementation plan
Route: `GET /dashboard/operator` (+ two action endpoints)
Lens: developer · Cadence: GUI (live) + daily-check health line

## Purpose

Give the operator a single dashboard home for the **operator worker** — the
component named (2026-06-18) as the next major dev milestone ("controlled
execution and remediation"). It surfaces worker mode, the 5-gate readiness
state, the work-order queue, the quarantine inventory, and lifetime cost; and
exposes the two **safe** actions (cancel a not-started order, view a quarantine
diff). It does NOT enable autonomous execution — that switch stays CLI-only
until the hardening phases land (`docs/operator_worker_hardening_spec.md`).

## Governance frame

The operator worker is the one component that can change *code*, so it is held
to a stricter bar than the advisory lanes. This surface is **read + safe
actions** only:

- It never runs a worker, enables autonomous mode, merges, pushes, deletes
  worktrees/branches, approves, or promotes to production.
- The only state mutation is **cancel** — a legal, audited terminal transition
  already supported by the domain model (`queued|awaiting_approval|approved →
  cancelled`, audit event `work_order_cancelled`), invoked ONLY through the
  validated domain API (`work_orders.transition_work_order`). No ledger editing.
- **Readiness is advisory health state, NOT authorization to execute workers.**
  Nothing on this surface can grant execution rights.
- `decision_engine.py`, scoring, allocation, and `outputs/latest/decision_plan.json`
  are untouched.

## Locked decisions (brainstorm)

1. Dedicated `/dashboard/operator` route + nav entry.
2. Read + safe actions (cancel, view quarantine diff); no autonomous-enable.
3. Hybrid readiness assessor (auto-detect what's verifiable; evidence-backed
   declared attestations for the qualitative gates).
4. Readiness **computed live** by the loader — no persisted artifact, no
   pipeline wiring or staleness debt.
5. Reuse `_require_auth` + the existing `gui_v2/data/operator_control.py` loader.

## Architecture

### Component 1 — `portfolio_automation/operator_worker_readiness.py` (NEW, observe-only)

`operator_worker_readiness(root) -> dict`. **Five primary gates**; cost is a
separate telemetry/warning line, NOT a gate.

```jsonc
{
  "observe_only": true,
  "gates": {
    "auth":        {"status": "amber", "reason": "runs as root, no container", "source": "auto"},
    "bounded_cmd": {"status": "amber", "reason": "...", "source": "declared",
                    "declared_by": "operator", "declared_at": "2026-06-18T...",
                    "evidence": ["operator_control/policy.py", "tests/test_operator_command_policy.py"]},
    "audit":       {"status": "green", "reason": "audit_log.jsonl + worker_cost_log.jsonl present", "source": "auto"},
    "rollback":    {"status": "amber", "reason": "...", "source": "declared", "...": "attestation fields"},
    "quarantine":  {"status": "green", "reason": "protected-path guard implemented + tested", "source": "auto"}
  },
  "overall_ready": "2/5",
  "autonomous_enabled": false,
  "cost": {"lifetime_usd": 5.43, "cap_usd": null, "cap_pct": null, "cap_configured": false}
}
```

**Auto gates:**
- `auth`: amber if `os.geteuid()==0`; note "no container" unless `/.dockerenv`
  exists or `/proc/1/cgroup` indicates containerization. Green only when
  non-root AND containerized.
- `audit`: green when both `outputs/operator_control/audit_log.jsonl` and
  `worker_cost_log.jsonl` exist.
- `quarantine`: green when the protected-path/isolation guard is **implemented
  and tested** — verified by importability of the guard symbol from
  `operator_control` AND presence of its test module. This gate evaluates
  whether the *control* exists; it does NOT read the quarantine inventory (the
  inventory is operational state shown separately, never proof the control works).

**Declared gates (`bounded_cmd`, `rollback`)** — evidence-backed attestations
read from `config.json:operator_worker.readiness_declared.<gate>`:
```json
"bounded_cmd": {
  "status": "green", "declared_by": "operator", "declared_at": "2026-06-18T...",
  "evidence": ["operator_control/policy.py", "tests/test_operator_command_policy.py"],
  "note": "Allowlisted commands + argument validation implemented."
}
```
Validation rules (a declaration is only honored as green/its stated status when
ALL hold; otherwise it **defaults to AMBER**):
- the block exists and is well-formed (dict with required keys),
- `status` is one of the recognized statuses (`green|amber|red`),
- `declared_by`, `declared_at` (ISO) present,
- `evidence` is a non-empty list of repo-relative paths **that exist on disk**
  (evidence-free or dangling-evidence declarations → amber).
The GUI displays `status`, `declared_by`, `declared_at`, and the `evidence`
references. **The dashboard is read-only for these declarations** — operators
change them only through reviewed config/code changes (never via a GUI control).

`overall_ready` = "`<green gate count>`/5". Degraded dict on any exception
(`{"observe_only": true, "error": "...", "gates": {}, "overall_ready": "0/5"}`).

### Component 2 — `gui_v2/data/operator_control.py` (EXTEND)

`operator_worker_view(root) -> dict` composes:
- `readiness` ← `operator_worker_readiness(root)`
- `orders` ← work orders folded by id (reuse existing fold + `_summarize`);
  per-order: id, status, age, probe_id/skill_id, report link, `cancellable`
  bool, and a `stale` bool (open/awaiting-approval beyond a bounded age).
- `quarantine` ← inventory of `.worktrees/wo_*` worktrees on `operator/*`
  branches, each reporting **separate facts** (see Quarantine inspection below).
  Operational state, shown separately from the quarantine *gate*.
- `cost` ← from readiness.

Degrades gracefully (missing dirs/worktrees/git → empty lists + a degraded note;
never raises).

### Component 3 — Quarantine inspection (safe git access)

For each quarantine worktree, report these facts **separately** (do not collapse
into one diff):
- `is_ancestor_of_main`: branch tip is an ancestor of `main` (fully merged).
- `unique_commits`: count/short-list of commits unique to the branch
  (`main..branch`).
- `changed_paths` + stat vs the **merge base** (`git merge-base main branch`),
  bounded summary only.
- `patch_equivalent_in_main`: heuristic — when determinable, whether the branch's
  net changes already appear in main (e.g. `git cherry` / per-file content
  compare). **Labeled HEURISTIC, not guaranteed.**
- `already_in_main`: first-class derived rollup = `is_ancestor_of_main OR
  patch_equivalent_in_main` (with the heuristic caveat surfaced).

Safe invocation rules (MANDATORY):
- All worktree paths, branch names, and work-order IDs come from **validated
  repository/domain records** (the work_orders ledger + `git worktree list`
  porcelain), never from user-provided values.
- Never interpolate any value into a shell string. Use **argument arrays**
  (`subprocess.run([...], shell=False)`).
- Repo-bound path validation: resolved worktree path must be inside the repo's
  `.worktrees/`; reject otherwise.
- Per-command **timeout** and **output-size cap**; truncate with a marker.
- The GUI shows a **bounded summary by default**. It must NOT expose secrets,
  `.env`/credentials, or unrestricted repository file contents — diff stat and
  bounded path lists only, never raw file bodies of sensitive paths.

### Component 4 — Routes (`gui_v2/app.py`)

- `GET /dashboard/operator` — `Depends(_require_auth)` → render `operator.html`.
- `POST /dashboard/operator/cancel` — `Depends(_require_auth)`, plus mutation
  gating + CSRF-equivalent (below). Behavior:
  - **Authorization:** require auth AND a dedicated mutation flag
    `GUI_V2_OPERATOR_EDIT=1` (mirrors `GUI_V2_PORTFOLIO_EDIT`). The app's
    `_require_auth` is single-principal with no roles, so this flag is how a
    read-only authenticated viewer is prevented from gaining cancel rights. If
    the auth context later gains roles, gate on a mutation role instead.
  - **CSRF-equivalent:** the app has no token framework; require a **same-origin
    check** (validate `Origin`/`Referer` against the request host) on this POST —
    the strongest equivalent already achievable — in addition to the edit-flag.
  - **Actor identity:** `actor` = the principal returned by `_require_auth`
    (the Basic-auth username); `actor_source = "dashboard_auth"`. Fall back to
    `actor="dashboard-manual"`, `actor_source="dashboard_open_mode"` ONLY when
    auth is unconfigured (open mode exposes no identity). **Never** accept the
    actor from a form field. Generate a request/correlation id and record it.
  - **Required bounded reason:** Form `reason` is required, trimmed, length-capped
    (e.g. ≤280 chars); reject empty.
  - **Server-side cancellable-state validation + race safety:** re-read the
    order's CURRENT status at submission; call
    `transition_work_order(new_status="cancelled", ...)` which validates against
    the policy graph. On `WorkOrderValidationError` (illegal source / unknown id /
    state changed since render) → audit a **failure** event + redirect with a
    visible error result. No ledger write outside the validated API.
  - **Idempotency:** if the order is already `cancelled`, treat as success
    (no-op), audit an idempotent-noop event, redirect with an informational
    result — do not error.
  - **Audit:** success path emits `work_order_cancelled` (via the domain API);
    every rejected/failed/no-op attempt emits an explicit audit event too.
  - **Result:** 303 redirect to `/dashboard/operator` with a visible
    success/error/info banner.
- `GET /dashboard/operator/quarantine/{work_order_id}/diff` —
  `Depends(_require_auth)` → bounded, read-only diff stat for that order's
  validated worktree; 404 when absent. Subject to the safe-git rules above.

### Component 5 — Template + nav

- `gui_v2/templates/operator.html` (NEW) using `_ui` macros. Sections:
  **Readiness** (5 gate rows: status badge + source + for declared gates the
  `declared_by`/`declared_at`/`evidence` + `N/5 ready`), **Cost** (separate
  line: lifetime, cap configured?, utilization — warning/telemetry, not a gate),
  **Orders** (table; `Cancel` button rendered only when `cancellable` AND
  `GUI_V2_OPERATOR_EDIT`; submit triggers an explicit confirm dialog + reason
  input), **Quarantine inventory** (table: worktree · branch · ancestor? ·
  unique-commits · changed-paths stat · patch-equivalent (heuristic) · in-main? ·
  bounded view-diff link).
- `base.html`: add `("/dashboard/operator","Operator")` to the nav tuple.

## Data flow

Browser → `GET /dashboard/operator` → `_require_auth` → `operator_worker_view`
(reads config + `outputs/operator_control/*.jsonl` + validated git worktree
records, all live, bounded) → `operator.html`. Cancel: confirmed form POST →
`_require_auth` + `GUI_V2_OPERATOR_EDIT` + same-origin check → re-read status →
`transition_work_order` (validated + audited) → 303 redirect with result. No
producer/pipeline involvement.

## Error handling

- Readiness + loader wrap IO/git in try/except → degraded structures, never 500.
- Cancel on illegal/unknown/raced id → audited failure event + redirect with
  error; already-cancelled → audited no-op + info redirect.
- Missing git executable or repository → quarantine section degrades to a note,
  page still renders.
- Auth: open mode when `GUI_V2_AUTH_USER/PASS` unset; 401 when configured;
  cancel additionally 403/blocked without `GUI_V2_OPERATOR_EDIT` or on
  cross-origin.

## Testing

- `tests/test_operator_worker_readiness.py`: healthy vs degraded; auto branches
  (root/non-root, container present/absent, logs present/absent); **malformed/
  evidence-free/dangling-evidence declarations → amber**; unrecognized status →
  amber; declared green honored only with existing evidence; `overall_ready`
  count; cost cap configured/unconfigured.
- `tests/test_dash_operator_worker.py`: fold-by-status, `stale` flag,
  `cancellable` flag; quarantine facts for **fully-merged, partially-merged,
  diverged, and patch-equivalent** synthetic branches; missing-git/repo degrade;
  bounded output (no raw sensitive file bodies); malicious id/branch/path-
  traversal rejected; command timeout + oversized-output truncation.
- `tests/test_operator_routes.py`: GET renders; **actor derived from auth, not
  form**; CSRF/same-origin rejection; missing `GUI_V2_OPERATOR_EDIT` → blocked;
  401 when auth configured; cancel legal → 303 + transition + success audit;
  cancel illegal/unknown/raced → failure audit, no write; repeated cancellation
  → idempotent no-op; missing reason → rejected; quarantine-diff 404 when absent
  + bounded; assert **no mutation of `decision_plan`, decision-engine inputs,
  git refs, or worktrees** by any route.
- Reuse existing gui_v2 test harness/fixtures.

## Health pairing (repo requirement)

Extend `/daily-tool-analysis` line 6g (operator-control) to summarize, observe-
only, with **no new persisted artifact**: readiness `N/5`; cost cap
configured/unconfigured + utilization; open / awaiting-approval / failed /
quarantined counts; stale-order count. Add a test asserting the line renders
under healthy and degraded readiness fixtures.

## Authorization scope (explicit)

- Read: `_require_auth` (open mode or authenticated).
- Mutate (cancel only): auth + `GUI_V2_OPERATOR_EDIT=1` + same-origin. A
  read-only authenticated user does NOT gain cancellation rights.
- **Do NOT add** worker execution, autonomous-enable, merge, push, worktree
  deletion, branch deletion, approval, or production-promotion controls to this
  surface.

## Out of scope

- Autonomous-enable toggle (CLI-only until hardening Phases 1–3).
- Any worker *execution* trigger from the GUI.
- A persisted readiness artifact / new pipeline stage (live computation only).
- `decision_engine`, scoring, allocation, or decision_plan changes.

## Follow-on (noted, not in this spec)

The CLI `cancel` gap (domain supports `cancelled`; `worker_runner` lacks a
`cancel` subcommand — only `fail`) is tracked in
`docs/operator_worker_hardening_spec.md` Phase 3.
