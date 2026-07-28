# WS10/WS11 Implementation Report — Persistence Integrity Fixes

Branch: `fix/ws10-11-persistence-integrity` (off `main` @ `8b5573a2`)
Commits:
- `826d15d8` — fix(sim-gov): serialize record_approval's read-modify-write to stop lost-update races (WS11)
- `bb0e8349` — fix(sim-gov): fail closed when production_application_audit.jsonl is unreadable (WS10)

**Note on environment:** mid-task, the shared `/opt/stockbot` checkout was
switched to a different branch by a concurrent session (this VPS runs several
parallel workstream fixes against the same repo). My in-progress edits were
recovered byte-for-byte from `git stash show`/working-tree copies and
re-applied in an isolated `git worktree` at
`/opt/stockbot-worktrees/ws10-11-persistence-integrity` (still on
`fix/ws10-11-persistence-integrity`, still branched off the same `main` commit),
where all remaining work, tests, and commits were done. Diff stats were
verified identical (105/101 lines changed) before and after recovery. Two
stray copies accidentally written to the shared `/opt/stockbot` checkout by an
early tool-path mistake were deleted before they could pollute the other
session's working tree.

---

## Fix 1 (WS11) — lost-update race in `record_approval`

**File:** `portfolio_automation/sim_governance/promotion_approvals.py`

**Mechanism chosen: advisory file lock (`fcntl.flock`)**, not append-only
migration. Rationale:
- The append-only migration would require rewriting `gui_v2/data/dash_governance.py`,
  which reads `approved_proposals.json` directly and reimplements its own fold
  (bypassing `is_valid_approval_record`) — the audit's own instruction was to
  prefer the lock in that case and record the migration as a follow-up.
- `revoke_application`'s existing JSONL append pattern already proves
  append-only *would* work eventually, but migrating the write path alone
  without touching the reader would leave `dash_governance.py` reading a
  format it doesn't understand — worse than today's (already-flagged, low
  severity) drift.
- The lock is the smallest robust mechanism: zero schema change, zero reader
  change, closes the exact race the auditor reproduced.

**Implementation:** `_approvals_write_lock(base_dir)`, a context manager that
opens a dedicated `approved_proposals.json.lock` sidecar (never the document
itself), takes `fcntl.LOCK_EX`, and releases + closes in `finally` on every
exit path including exceptions. `record_approval` now performs the
`approvals_log_unreadable` check *and* the read-modify-write inside this one
lock acquisition (closing a check-then-write TOCTOU window too). Each call
opens a fresh fd and never re-enters the lock while holding it, so there is no
self-deadlock path. `is_human_approver`, the repo-root guard, and the
unreadable-log refusal are all unchanged in substance, just moved inside the
lock where needed.

**Tests** (`tests/test_promotion_approvals_concurrency.py`, 6 tests): two
threads racing approvals for different proposals (deterministic — forces the
race via a barrier inside `_load_raw`, not scheduling luck), a 12-way
high-contention variant, approval-racing-a-revocation, single- and
concurrent-duplicate-submission idempotency, and unreadable-log refusal under
concurrency.

**Regression proof (temporarily reverted, then restored):**
```
=== BEFORE fix (reverted code) ===
t1 result ok: True
t2 result ok: True
final approvals on disk: ['prop_AAA']

=== AFTER fix (current code) ===
t1 result ok: True
t2 result ok: True
final approvals on disk: ['prop_AAA', 'prop_BBB']
```
The 12-way variant on reverted code collapsed 12 approvals to 1 survivor,
confirming the race is not a one-off.

---

## Fix 2 (WS10) — no fail-closed guard on `production_application_audit.jsonl`

**File:** `portfolio_automation/sim_governance/production_application.py`

Added `audit_log_unreadable(base_dir) -> str | None`, mirroring
`revocations_log_unreadable`'s exact torn-tail-vs-total-corruption rule:
absent → `None`; ≥1 parseable line → `None` (torn trailing line tolerated,
already `_prior_durable_ops`'s own line-skip behavior); present, non-empty,
zero parseable lines → a `wholly_corrupt: ...` reason. Wired into
`apply_approved_proposals`'s existing single refusal path alongside
`approvals_log_unreadable` / `revocations_log_unreadable` — same `reason`
field, same `overlay_rebuild_skipped` flag, new `audit_log_unreadable` field
in the returned/persisted state.

**Tests** (`tests/test_audit_log_fail_closed.py`, 8 tests): wholly-corrupt
audit log → refusal + durable op not dropped + reason set; torn final line →
tolerated; absent log → normal; reason surfaced for all three conditions
independently (each fires alone, without falsely blaming the other two logs).

**Regression proof:**
```
=== BEFORE corruption ===
watchlist_applied: 1 durably_live_count: 1
overlay_rebuild_skipped: False

=== AFTER wholly-corrupting the audit log ===
watchlist_applied: 1 durably_live_count: 1
overlay_rebuild_skipped: True
audit_log_unreadable: wholly_corrupt: 0 of 2 line(s) parsed as a JSON object
reason: wholly_corrupt: 0 of 2 line(s) parsed as a JSON object
```
(Pre-fix behavior, exactly as the audit reported: `watchlist_applied`/
`durably_live_count` silently dropped 1→0 with `overlay_rebuild_skipped`
staying `False` — see the audit doc §10.4 for that transcript, reproduced
identically before this fix.)

---

## Verification against the REAL `/opt/stockbot/outputs` (`write_files=False`)

```
watchlist_applied: 0
durably_live_count: 0
advisory_applied: 2
overlay_rebuild_skipped: False
approvals_log_unreadable: None
revocations_log_unreadable: None
audit_log_unreadable: None
valid approval records: 43
effective approvals (unique proposal_ids): 43
approved_proposal_ids count: 43
```
Matches the audit's real-data findings (zero durable ops ever applied in this
environment → `durably_live_count: 0`; 43 approval records; no corruption on
any of the three logs today). Behavior is unchanged on real data — the new
guards are inert until a log is actually corrupted, and no lock sidecar file
was created under `outputs/` (only `write_files=True` / `record_approval`
create it, and this verification never called `record_approval`).

---

## Test commands run

```
.venv/bin/python -m py_compile portfolio_automation/sim_governance/promotion_approvals.py \
    portfolio_automation/sim_governance/production_application.py

.venv/bin/python -m pytest -q \
  tests/test_promotion_approval_identity.py \
  tests/test_promotion_approvals_unreadable_log_guard.py \
  tests/test_promotion_approvals_writepath_guard.py \
  tests/test_revocation_log_fail_closed.py \
  tests/test_overlay_durability_hardening.py \
  tests/test_overlay_watchlist_durability.py \
  tests/test_overlay_either_id_matching.py \
  tests/test_sim_governance.py \
  tests/test_sim_governance_pipeline.py \
  tests/test_flock_sim_governance.py \
  tests/test_dash_governance_auto_approval.py \
  tests/test_dash_governance_durable_counts.py \
  tests/test_gui_governance_approve.py \
  tests/test_gui_governance_veto.py \
  tests/test_gui_governance_confirm_xss.py \
  tests/test_governance_digest.py \
  tests/test_governance_digest_wiring.py \
  tests/test_run_mode_governance.py \
  tests/test_simgov_backlog_review.py \
  tests/test_audit_log_fail_closed.py \
  tests/test_promotion_approvals_concurrency.py
```
Result: **306 passed**, 0 failed (40 pre-existing FastAPI deprecation
warnings, unrelated). Full suite was NOT run per task constraints.

## Assumptions / scope notes

- `gui_v2/data/dash_governance.py`'s independent fold (audit item 6) was left
  untouched — the lock preserves the exact on-disk document shape, so no new
  disagreement is introduced; the pre-existing validation-bypass drift is
  unchanged and remains a follow-up (append-only migration would need to
  rewrite that reader, as the audit noted).
- No changes to `decision_engine.py`, scoring semantics, or `_TRACKED_KNOBS`.
- No new production mutating path; both fixes are defensive/serialization
  only, inert on already-correct data.
