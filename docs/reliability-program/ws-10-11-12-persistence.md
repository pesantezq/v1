# Reliability Audit — WS10/11/12: Approval, Revocation & Concurrency Integrity

**Scope:** `portfolio_automation/sim_governance/` — durable production state (WS10),
approvals append-only safety (WS11), audit-log growth (WS12).
**Method:** read-only. Static reading of source + tests, plus controlled read-only
Python experiments run against a scratch tmp dir
(`/tmp/claude-.../scratchpad/ws11race/`) and read-only inspection of real
`/opt/stockbot/outputs/promotion_approvals/*` artifacts. No file under `outputs/`
or `data/` was written by this audit.
**Repo state audited:** branch `main`, includes commit `bd7cd518` (repo-root
guard) and the 2026-07-28 promotion-overlay-durability work
(`docs/superpowers/specs/2026-07-28-promotion-overlay-durability-design.md`).

---

## WS10 — Durable production state

### 10.1 Complete proposal-type inventory (`schemas.py:29-82`)

| Constant | Value | Workflow (`workflow_for_proposal_type`) | Durable? (`_DURABLE_PROPOSAL_TYPES`, `production_application.py:77-82`) | Producer |
|---|---|---|---|---|
| `PROPOSAL_ADVISORY_STRATEGY` | `advisory_strategy_change` | advisory | no | none found (constructed nowhere as `proposal_type=`) |
| `PROPOSAL_ADVISORY_RANKING` | `advisory_ranking_change` | advisory | no | none found |
| `PROPOSAL_ADVISORY_CONTEXT` | `advisory_context_change` | advisory | no | none found |
| `PROPOSAL_WATCHLIST_ADD` | `watchlist_add` | watchlist | **yes** | `simulation_lane.py:291` |
| `PROPOSAL_WATCHLIST_REMOVE` | `watchlist_remove` | watchlist | **yes** | none found as a live constructor call — only referenced in `_rollback_plan_for`/durability sets; watchlist-removal producer is the held `feat/watchlist-decay-removals` branch, not on `main` |
| `PROPOSAL_WATCHLIST_RANK` | `watchlist_rank_change` | watchlist | **yes** | `simulation_lane.py:325` |
| `PROPOSAL_WATCHLIST_TAG` | `watchlist_tag_change` | watchlist | **yes** | none found as a live constructor call on `main` |
| `PROPOSAL_CROWD_CONTEXT` | `crowd_context_change` | advisory | no | `simulation_lane.py:364` (but `promotion_proposals.py:104-111` explicitly skips it from ever entering the proposal queue — "observe-only, self-refreshing", never gated) |
| `PROPOSAL_DISCOVERY_PROMOTION` | `discovery_candidate_promotion` | watchlist | **no** (deliberately excluded) | **none** |
| `PROPOSAL_FLOCK_CONTEXT_DISPLAY` | `flock_context_production_display` | advisory | no | none found as constructor call on `main` |
| `PROPOSAL_FLOCK_WATCHLIST_LOGIC` | `flock_watchlist_candidate_logic` | watchlist | no (state-derived) | `simulation_lane.py:196` |
| `PROPOSAL_FLOCK_ADVISORY_CONTEXT` | `flock_advisory_context_logic` | advisory | no | `simulation_lane.py:226` |
| `PROPOSAL_FLOCK_SCORING_ADJUSTMENT` | `flock_simulation_scoring_adjustment` | advisory | no | `simulation_lane.py:258` |
| `PROPOSAL_FLOCK_RISK_OVERLAY` | `flock_risk_overlay` | advisory | no | `simulation_lane.py:243` |

Every proposal type's workflow routing is confirmed by `_ADVISORY_PROPOSALS` /
`_WATCHLIST_PROPOSALS` (`schemas.py:65-82`). All proposal objects are actually
minted by `promotion_proposals.generate_proposals` (`promotion_proposals.py:74-136`),
which copies `proposal_type` straight off whatever `SimulationCandidate` the
simulation lane produced (`cand.get("proposal_type")`, line 100) — so the
"producer" column above is the candidate constructor site, one hop upstream of
the proposal.

### 10.2 `discovery_candidate_promotion` — durable or state-derived? Confirmed: **neither is live; it has no producer at all.**

- `grep -rn "proposal_type=" portfolio_automation/sim_governance/*.py` (excluding
  tests) returns exactly 8 constructor call sites, none of which pass
  `S.PROPOSAL_DISCOVERY_PROMOTION`. It appears **only** inside membership/filter
  frozensets: `schemas.py:80` (`_WATCHLIST_PROPOSALS`), `production_application.py:90`
  (`_MEMBERSHIP_PROPOSAL_TYPES`), `production_overlays.py:69` (apply-time
  ADD/DISCOVERY_PROMOTION branch), `promotion_proposals.py:45` (rollback-plan
  text), `daily_simulation_bundle.py:93` (a bundle filter), and
  `auto_approval.py:64` (`_WATCHLIST_ELIGIBLE_TYPES` — the GPT sim auto-approval
  channel is *willing* to act on it, but nothing ever emits a candidate of this
  type for it to act on).
- `daily_governance_run.py:_enrich_baseline` (lines 80-109) reads
  `outputs/sandbox/discovery/automatic_promotion_candidates.json` and folds each
  row into `baseline["discovery_candidates"]` as a **plain dict** — it does *not*
  construct a `SimulationCandidate` or set `proposal_type` on it. Nothing in
  `simulation_lane.py` reads `baseline["discovery_candidates"]` and turns it into
  a candidate (confirmed: `grep -n "discovery_candidates" simulation_lane.py`
  returns no hits). So the field is populated but structurally dead — a baseline
  key with no consumer.
- **Verdict:** `production_application.py:74-76`'s comment is accurate as written:
  "it is not in the operator's named durable set and no producer currently emits
  it, so it keeps refresh semantics." In practice this is moot today because
  refresh-vs-durable is unobservable — the type never appears in
  `pending_proposals.json` at all. It is dead code / a placeholder for a future
  discovery-promotion producer, not an active gap. **Blast radius: none today.**
  Risk if a producer is added later without revisiting this: it would silently
  get refresh (non-durable) semantics unless someone remembers to add it to
  `_DURABLE_PROPOSAL_TYPES` — a landmine for future work, not a present defect.

### 10.3 `_prior_durable_ops` / `_resolve_durable_ops` (`production_application.py:193-326`)

- **Source of truth:** the append-only `production_application_audit.jsonl`
  (`_prior_durable_ops`, lines 220-257). It folds every `applied_to_production`
  row whose `proposal_type` is durable into `seen[key]`, keyed by
  `_op_identity_key` (line 246) — `candidate_id` when present, else the fallback
  fact key `f"fact:{proposal_type}:{symbol}"` (line 114). A `rolled_back` event
  removes matching entries from `seen` via `_drop_rolled_back` (lines 163-190)
  **as the log is folded**, so a rollback recorded before a later re-application
  does not clobber the re-application (chronological order matters and is
  respected because the file is read top-to-bottom and `seen[key] = ...`
  overwrites).
- **Authority filter runs AFTER the fold** (lines 259-270): a candidate op
  survives into `out` only if its `pid`/`cid` is **not** in `revoked`, **not** in
  `rejected`/`rejected_cands`, and **is** in `approved`/`approved_cands`. This
  exactly mirrors the per-proposal loop's authority checks for today's proposals
  (lines 477-486) — same three-gate order (revoke → reject → approve), so a
  prior op and a fresh proposal are held to an identical bar. Confirmed
  symmetric by reading both blocks side by side.
- **Conflict resolution** (`_resolve_durable_ops`, lines 273-326): today's ops
  always outrank carried ops for the same `(symbol, proposal_type)` fact (rank
  tuple `(1, ...)` vs `(0, ts, ...)`, line 296-299); among carried ops, later
  `ts` wins. A second pass (lines 310-322) enforces "one membership direction
  per symbol" so an ADD and a REMOVE for the same symbol cannot both survive —
  whichever has the higher rank wins and the other is dropped. Final list is
  re-sorted oldest-first (line 325) specifically so the downstream
  last-writer-wins overlay fold (`production_overlays.apply_approved_watchlist`)
  agrees with recency.
- **Tolerance:** a missing audit file → `[]` (line 222-223, "no prior ops", not
  an error). Individual corrupt lines are skipped silently (`except: continue`,
  line 233-234). **There is no "audit log wholly corrupt" fail-closed guard** —
  see 10.4, this is the single most important gap found in this audit.

### 10.4 CONFIRMED BY EXPERIMENT — no fail-closed guard on `production_application_audit.jsonl` corruption

`production_application.py` has two explicit fail-closed guards —
`promotion_approvals.approvals_log_unreadable` and
`promotion_approvals.revocations_log_unreadable` — checked at the very top of
`apply_approved_proposals` (lines 402-449) before any overlay rebuild. **No
equivalent `audit_log_unreadable` guard exists for
`production_application_audit.jsonl` itself**, even though it is the *sole*
source of truth `_prior_durable_ops` reconstructs durable membership from.

I verified this with a scratch-dir experiment (never touching real
`outputs/`/`data/`): seeded a valid audit log with one durable `watchlist_add`
op for `XOM`, backed by a valid `approved_proposals.json` approval. First call
to `apply_approved_proposals` correctly reports `watchlist_applied: 1,
durably_live_count: 1, overlay_rebuild_skipped: False`. I then overwrote the
audit log with two lines of pure garbage (simulating a torn/overwritten file —
zero parseable lines, the same "wholly corrupt" shape that
`revocations_log_unreadable` explicitly detects and refuses on) and called
`apply_approved_proposals` again with the **same, still-valid** approvals log:

```
=== BEFORE corruption ===
watchlist_applied: 1 durably_live_count: 1
overlay_rebuild_skipped: False
=== AFTER wholly-corrupting the audit log ===
watchlist_applied: 0 durably_live_count: 0
overlay_rebuild_skipped: False
Any corruption signal in state dict keys? ['advisory_applied', 'applied', 'applied_count',
'applied_today_count', 'durably_live_count', 'generated_at', 'ignored', 'ignored_count',
'overlay_rebuild_skipped', 'overlays', 'schema', 'snapshots', 'watchlist_applied',
'watchlist_applied_today', 'watchlist_carried_forward']
```

`overlay_rebuild_skipped` stays `False` — there is **no field anywhere in the
returned state that distinguishes "nothing durable is currently approved" from
"the audit log that proves durable approvals exist just got destroyed."** With
`write_files=True` this would silently rewrite
`outputs/latest/approved_watchlist_proposals.json` to drop every previously
carried-forward durable op, with the write **succeeding** — the exact failure
mode `approvals_log_unreadable`/`revocations_log_unreadable` were built to
close (see the docstring at `promotion_approvals.py:64-78`), left open one file
over.

- **Test coverage:** `tests/test_overlay_watchlist_durability.py:136-147`
  (`test_corrupt_audit_lines_are_skipped`) only exercises *partial* corruption
  (one bad line + one good line survives) — it asserts the tolerant behavior is
  correct, but there is no test for *wholly* corrupt audit log content, and (by
  construction, since no such guard exists) no test could assert a fail-closed
  refusal here today.
- **Blast radius today:** zero live-production impact, because (a) production
  overlay consumption (`production_overlays.load_production_watchlist` /
  `load_production_advisory`) is gated OFF by default
  (`production_application.apply_watchlist_overlay` / `apply_advisory_overlay`
  both default `false`, `daily_governance_run.py:56-59`), and (b) no durable-type
  op has actually been applied in the real `production_application_audit.jsonl`
  yet (see 10.5). If either changes, this becomes a live silent-membership-loss
  path with no signal in `daily_governance_status.json` or
  `production_application_state.json`. **Confirmed by reading + confirmed by
  experiment; not covered by any test.**

### 10.5 Real artifact evidence

`outputs/promotion_approvals/production_application_audit.jsonl` (17 rows,
2026-06-21 → 2026-07-28):
- `Counter({'applied_to_production': 17})` — only one event type has ever fired
  in production; `rolled_back` has never occurred.
- `Counter({'flock_advisory_context_logic': 10, 'flock_watchlist_candidate_logic': 6,
  'crowd_context_change': 1})` — **no durable type (`watchlist_add/remove/rank/tag`)
  has ever been applied in this environment.** The durable carry-forward path is
  exercised by tests but has zero real-world mileage yet.
- **Every one of the 17 real rows has no `candidate_id` key at all** (not
  `null` from an empty candidate — the key is absent, verified via
  `row.get('candidate_id')` returning `None` for all 17 and via `sorted(rows[0].keys())`
  omitting `candidate_id`). This is exactly the "every audit row written before
  2026-07-28" case `_op_identity_key`'s docstring (`production_application.py:107-114`)
  calls out — confirmed empirically, not just theoretically: the fallback-to-fact
  code path is live and load-bearing on real data, not dead defensive code.
- `outputs/promotion_approvals/approved_proposals.json` holds **43** approval
  records (matches the design doc's Defect-A evidence figure).
- `outputs/promotion_approvals/production_revocations.jsonl` **does not exist**
  on this host — zero revocations have ever been recorded.
- `outputs/promotion_approvals/snapshots/` holds 108 files, 448K total — one
  snapshot pair (`approved_watchlist_proposals.json.*` +
  `approved_advisory_proposals.json.*`) per `write_files=True` run of
  `apply_approved_proposals`, going back to 2026-06-16. No pruning logic exists
  (grep for cleanup/retention in `production_application.py` returns nothing) —
  this is a second, independent unbounded-growth surface adjacent to WS12's
  audit-log finding, growing once per daily-governance run regardless of
  whether anything changed.

### 10.6 Existing test coverage vs the six required scenarios

| Required scenario | Covered? | Test(s) |
|---|---|---|
| Durable op surviving absence from today's pending | **yes** | `test_overlay_watchlist_durability.py:42` `test_prior_applied_removal_persists_with_no_pending_proposal` |
| Unchanged fact not needing re-approval | **yes** | `test_promotion_approval_identity.py:82` `test_approve_under_a_new_proposal_id_keeps_the_candidate_approved`; `test_overlay_either_id_matching.py:35` |
| Proposal-id churn not invalidating a candidate-tied approval | **yes** | same two tests above, plus `test_overlay_either_id_matching.py:48` `test_proposal_id_approval_still_works` |
| Revoked op not resurrecting | **yes** | `test_overlay_durability_hardening.py:185,200` (`test_revoked_target_still_pending_today_is_not_applied`, `..._proposal_id_...`); `test_overlay_watchlist_durability.py:58,70` |
| Corrupt approval/revocation state failing closed | **yes, for the two files it covers** | `test_overlay_durability_hardening.py:233` `test_corrupt_approvals_log_refuses_to_rebuild_the_overlay`; `test_revocation_log_fail_closed.py:121` `test_wholly_corrupt_revocation_ledger_refuses_overlay_rebuild_and_does_not_resurrect`. **NOT covered for the audit log itself** — see 10.4. |
| Torn final JSONL line vs total corruption | **yes, for revocations** | `test_revocation_log_fail_closed.py:97` `test_torn_final_line_only_is_tolerated` vs `:88` `test_wholly_corrupt_revocation_ledger_is_detected`. **The audit log has no "wholly corrupt" detector to test** (10.4) — only the tolerant partial-corruption case is tested (`test_overlay_watchlist_durability.py:136`). |

**Net WS10 assessment:** the 2026-07-28 durability work is unusually
well-tested for the scenarios it explicitly targeted (approvals log,
revocations log). The one structural gap is that `production_application_audit.jsonl`
— the actual source of durable state — was not given the same
exists-but-corrupt fail-closed treatment as the two files that gate it,
and no test exercises that gap because no guard exists to test.

---

## WS11 — Approvals append-only safety

### 11.1 Exact write path (`promotion_approvals.py:99-169`)

`record_approval`:
1. Build `record` dict, validate via `schemas.is_valid_approval_record` (line 130) — rejects non-human approvers, unknown decisions, missing timestamp/proposal_id. Nothing is written if invalid.
2. Repo-root misdirection guard (`_looks_like_repo_root`, lines 37-50; checked at 135-141) — rejects if `base_dir` looks like the project root instead of `<root>/outputs`.
3. `approvals_log_unreadable` guard (line 144) — refuses to write if the on-disk file exists but fails to parse.
4. **Read-modify-write, no lock, no CAS:** `data = _load_raw(base_dir)` (line 154) → `approvals.append(record)` (156) → `safe_write_json(...)` (164). Between step 4's read and its write there is no exclusion mechanism of any kind (no `fcntl`/`flock`/`threading.Lock`/version check). `grep -rn "fcntl|filelock|FileLock|threading.Lock|multiprocessing.Lock|flock(" portfolio_automation/sim_governance/*.py portfolio_automation/data_governance.py` returns **zero matches**.

`safe_write_json` → `safe_write_text` (`data_governance.py:226-268`): writes to a
`tempfile.mkstemp` in the **same directory**, then `os.replace(tmp_name, out_path)`
(line 260). This gives **atomicity of the single write** — a reader can never see
a torn/partial file, and a crash mid-write leaves the prior good file untouched
(temp is unlinked on any exception, lines 261-266). **It provides zero protection
against two concurrent read-modify-write cycles** — `os.replace` is atomic per
call, but there is nothing that makes "read old content, append, write" atomic
as a unit across two callers.

### 11.2 CONFIRMED BY EXPERIMENT — concurrent `record_approval` calls silently lose an approval

Reproduced with a controlled thread interleaving (scratch dir only): two threads
both call `record_approval` for **different** `proposal_id`s
(`prop_AAA`, `prop_BBB`) against the same `base_dir`, with a `threading.Barrier`
forcing both threads to complete their read of the existing file before either
writes (this reproduces the true concurrent-process shape without needing two
OS processes):

```
t1 result ok: True
t2 result ok: True
final approvals on disk: ['prop_BBB']
```

Both calls report `ok: True` — **there is no error, no exception, no log line
indicating a problem** — yet only one of the two approvals survives on disk.
This is a genuine, reproducible lost-update: a human approves proposal A, a
second human (or a second GUI tab, or a cron-triggered path) approves proposal
B moments later, and one approval silently vanishes with no evidence it ever
happened beyond in-memory state the caller already discarded. Given
`record_approval`'s own docstring frames "AI cannot self-approve" and "invalid
metadata is ignored" as *structural guarantees*, a silently-dropped *valid*
human approval is a materially worse failure than either of those documented
concerns, and it is currently unguarded.

- **No test covers this.** All existing concurrency-adjacent tests
  (`test_promotion_approvals_unreadable_log_guard.py`,
  `test_promotion_approvals_writepath_guard.py`) are sequential, single-threaded
  correctness tests of the fail-closed guards, not races.
- **Blast radius:** every caller of `record_approval` is affected identically —
  see 11.3. The GUI is the only live caller today (`gui_v2/app.py`), running
  inside a long-lived `stockbot-dashboard.service` uvicorn process. The daily
  cron (`run_daily_safe.sh` → `run_daily_governance`) calls
  `apply_approved_proposals` (a **reader** of the approvals log, not a writer of
  it) — so the realistic race is GUI-vs-GUI (two tabs / two rapid clicks / bulk
  approve racing a single-item approve), not GUI-vs-cron for this specific file.
  `record_approval` itself is never invoked by any cron-triggered path (grep
  confirms `record_approval` callers are exactly: `gui_v2/app.py` ×2 call sites,
  and test files).

### 11.3 Every caller of `record_approval`

Production:
- `gui_v2/app.py:763` — `page_governance_decide` (`POST /dashboard/governance/decide`), single-item approve/reject.
- `gui_v2/app.py:872` (`_promotion_approvals_record`) — used by `_apply_approval_action` (line 891-908), which is called from `page_governance_approve` (`POST /dashboard/governance/approve`) for **both** single-item and bulk (`approve_all`/`reject_all`) actions. **Bulk mode loops per-`proposal_id` and calls `record_approval` once per item** (line 905-907) — for N items that is N independent full read-modify-write cycles of the whole document, each one it's own race window. There is no batched/single-write bulk path.

Test-only: `tests/test_simgov_backlog_review.py`, `test_promotion_approvals_writepath_guard.py`, `test_sim_governance.py`, `test_promotion_approval_identity.py`, `test_promotion_approvals_unreadable_log_guard.py`, `test_flock_sim_governance.py`.

Non-callers worth noting: `backlog_review.py` (line 103) only prints the
*string* `promotion_approvals.record_approval(...)` as an operator hint — it
never calls it (matches the memory note "read-only backlog_review.py routes...
never approves").

### 11.4 Behavior under the specific stress scenarios

| Scenario | Behavior (confirmed by reading / experiment) |
|---|---|
| Two simultaneous approvals (different proposals) | **Data loss — confirmed by experiment (11.2).** Last writer wins; the other's approval vanishes with `ok: True` reported to both. |
| Approval racing a revocation | Different files (`approved_proposals.json` vs `production_revocations.jsonl`), so no *file-level* corruption race. But no cross-file ordering guarantee exists either — `apply_approved_proposals` reads `revoked_ids()` (line 466) once, then loops proposals; if a revoke lands on-disk between that read and the write, it is simply picked up on the *next* run. This is an eventual-consistency gap, not a corruption risk — acceptable, but worth naming since WS10's authority-ordering guarantees ("revoke wins") only hold per-run, not intra-run. |
| Duplicate submission (same proposal approved twice) | **Harmless.** `record_approval` has no idempotency check and will happily append a second identical record; `effective_approvals`/`effective_approvals_by_candidate` fold to the *latest* record per id (`promotion_approvals.py:183-192`, `204-219`), so a duplicate "approve" after an "approve" is a no-op in effect, just extra bytes. The GUI's bulk path (`_decided_ids`, line 885-888) pre-filters already-decided ids so bulk mode won't even attempt this; the single-item route has no such guard, so a double-click/replayed POST on `/dashboard/governance/decide` can append a duplicate — benign. |
| Interrupted write (process killed mid-`os.replace`) | Protected by `safe_write_text`'s tempfile+`os.replace` pattern (`data_governance.py:254-267`) — `os.replace` is a single atomic syscall on POSIX; there is no window where the target file is half-written. Worst case: the temp file is orphaned (cleanup only runs on a *caught* exception, not on SIGKILL) — cosmetic debris, not corruption. |
| Truncated final record | Not directly applicable to `approved_proposals.json` — it's a single JSON *document*, not JSONL, so a truncated write (if it ever escaped the atomic-replace protection, e.g. disk full mid-`fh.write` before `os.replace` is reached) simply fails to write and the old file is untouched (`os.replace` is never called if `fh.write` raises); the exception propagates to `record_approval`'s `except Exception as exc:` (line 165-167) which returns `ok: False`, `write_failed: ...`. Surfaced to the operator (`page_governance_decide` raises `HTTPException(400, ...)`, `_apply_approval_action` puts the id in `failed`, which the redirect message and audit_log entry surface — verified at `gui_v2/app.py:966-974`). **Not silent.** |
| Permission error | Same path as above — caught, `ok: False`, surfaced. Not silent. |
| Mid-file invalid JSON (approvals log corrupted between reads) | Caught by `approvals_log_unreadable` **before** any write is attempted (`record_approval` line 144-151) — refuses cleanly with a clear reason string, does not attempt a read-modify-write through a log it can't trust. Well-guarded; has dedicated tests (`test_promotion_approvals_unreadable_log_guard.py`). |
| Stale process overwriting newer state | This **is** the lost-update scenario in 11.2, generalized: any writer that read an older version of the document and later writes it back will clobber whatever was appended in between, regardless of "staleness" being from a slow process, a long-running bulk loop, or a genuine race. No version/ETag/CAS check exists anywhere in this write path. |

### 11.5 Do `production_revocations.jsonl` and `approved_proposals.json` ever disagree, and which is load-bearing?

They are two **independent** authority sources that `apply_approved_proposals`
consults every run (lines 452-466: `approved`, `rejected`, `approved_cands`,
`rejected_cands` from the approvals doc; `revoked` from the revocations JSONL).
They cannot literally "disagree" in a way the code detects or reconciles — there
is no cross-check that a revoked id was ever actually approved, for instance.
Precedence is fixed and explicit, applied identically in both the per-run loop
(lines 477-486) and the prior-ops filter (lines 263-269): **revoke beats
reject beats approve** — i.e. revocation is unconditionally load-bearing over
the approvals doc. A proposal/candidate could theoretically be simultaneously
"approved" in `approved_proposals.json` and "revoked" in
`production_revocations.jsonl`; the code resolves this deterministically (revoke
wins) rather than flagging it as an inconsistency. This is a reasonable design
choice, not a bug, but it means the two logs are allowed to drift apart forever
with no reconciliation or alert — a human could revoke something, then approve
a *new* proposal for the same fact (new `candidate_id`), and nothing surfaces
that an older revocation and a newer approval coexist for what a human might
perceive as "the same symbol."

### 11.6 Migration cost estimate to append-only

**Who reads the log, and what shape do they expect:**
1. `promotion_approvals.py` itself — `_load_raw` (line 53-61), `load_valid_approvals`
   (172-180), `effective_approvals`/`effective_approvals_by_candidate`
   (183-192, 204-219) — all internal, all already iterate a list; a JSONL
   migration only changes how that list is *obtained* (stream-parse lines
   instead of `json.loads` one document), not how it's folded. Low cost.
2. `gui_v2/data/dash_governance.py:37,51-53` — **reads the raw file directly**
   (`_read_json(appr_dir / "approved_proposals.json")`) and **reimplements its
   own fold** (`{r.get("proposal_id") for r in approval_recs if r.get("decision")
   == "approve"}`) rather than calling `promotion_approvals.effective_approvals`.
   This is a second, independent, **unvalidated** copy of the fold logic — it
   does not run records through `schemas.is_valid_approval_record`/
   `is_human_approver` the way `load_valid_approvals` does, so if the file ever
   contained a malformed or AI-marker record (never possible via `record_approval`
   today, but possible via hand-editing or a future writer), this GUI reader
   would count it while the production-application logic would not. **This
   reader would need to be rewritten to consume the module's loader function
   (or a JSONL-aware equivalent) rather than parsing the document shape itself**
   — the single highest-cost item in a migration, and also a latent
   validation-drift bug independent of any migration.
3. No other production reader exists (`grep -rln "approved_proposals.json\|_APPROVALS_FILE\|load_valid_approvals\|effective_approvals\b"` outside tests returns only `promotion_approvals.py`, `gui_v2/app.py` (doc-comment only, no direct parse), and `dash_governance.py`).

**Real record volume:** 43 records today (`outputs/promotion_approvals/approved_proposals.json`),
accumulated since 2026-06-16 — small enough that a one-time rewrite-to-JSONL
migration is trivial; this is not a data-volume problem, it is a
consumer-shape problem (item 2 above).

**Estimated migration shape (analysis only — no implementation performed,
per audit scope):** append-only JSONL for the write path (mirroring
`revoke_application`'s existing pattern at `promotion_approvals.py:275-291`,
which already demonstrates the correct shape: `ensure_output_dir` +
open-append + one `json.dumps(...) + "\n"`, no read-before-write at all) would
close 11.2's race entirely for `record_approval`, at the cost of updating the
three read-side functions to stream-parse lines and rewriting
`dash_governance.py`'s direct-document read.

---

## WS12 — Audit-log growth

### 12.1 Exact append site and payload (`production_application.py:551-569`)

```python
try:
    ensure_output_dir(OutputNamespace.PROMOTION_APPROVALS, _AUDIT_FILE, base_dir=base_dir)
    audit_path = get_output_path(OutputNamespace.PROMOTION_APPROVALS, _AUDIT_FILE, base_dir=base_dir)
    with Path(audit_path).open("a", encoding="utf-8") as fh:
        for o in watchlist_ops + advisory_ops:
            fh.write(json.dumps({
                "ts": now,
                "event": "applied_to_production",
                "proposal_id": o["proposal_id"],
                "candidate_id": o.get("candidate_id"),
                "proposal_type": o["proposal_type"],
                "change": o["change"],
                "rollback_plan": o["rollback_plan"],
                "snapshots": snapshots,
            }, default=str) + "\n")
```

`watchlist_ops` here is the **already-resolved, post-`_resolve_durable_ops`**
list (line 510: `watchlist_ops = _resolve_durable_ops(watchlist_ops, _prior)`)
— i.e. it includes every durable op carried forward from prior runs, not just
newly-applied ones. **Confirmed by reading:** the loop at line 557 iterates
`watchlist_ops + advisory_ops` unconditionally, with no branch distinguishing
"this op is new this run" from "this op survived from a prior run." So a
single durable `watchlist_add` that a human approves once and never revokes
gets a brand-new `applied_to_production` row, byte-for-byte identical in shape
to the original application row (differing only in `ts` and the `snapshots`
paths), on **every** run of `apply_approved_proposals` for as long as it stays
approved — which, for durable membership, is meant to be indefinitely.

**`snapshots` dict re-embedded per row — confirmed and quantified.** The
`snapshots` dict (both overlay snapshot paths, ~250-300 bytes) is captured once
per *run* (lines 542-544) and then embedded identically **into every op's audit
row from that run** (line 566 references the same `snapshots` variable for
every iteration of the loop) — for a run applying 5 ops, the same
snapshot-path pair is duplicated 5 times across 5 rows.

### 12.2 Consumers — none count rows as applications

`grep -rln "production_application_audit\|production_application_state"` across
the whole repo (excluding tests) surfaces: `gui_v2/app.py` (no direct read of
the audit file — comment only), `gui_v2/data/dash_governance.py` (reads
`production_application_state.json` only, line 38), `daily_input_snapshot.py:66`
(references `production_application_state.json`, not the audit log),
`scripts/verify_sqg_post_run.py:133` (same, `production_application_state.json`
only). **No production code path reads `production_application_audit.jsonl` and
counts its rows as an application count.** The only two consumers of the raw
audit log are internal to `production_application.py` itself
(`_prior_durable_ops` for durable-set reconstruction, and `rollback_last` for
locating the latest snapshot) plus the analysis-layer documentation.

`.claude/commands/daily-tool-analysis.md` (lines 107-184, 242, 396) **already
explicitly documents this exact growth/counting hazard** — added the same day
as the durability work (2026-07-28): *"Do not count rows as a live-application
count: the audit write iterates ops after the durable union, so a
carried-forward op gets a fresh applied_to_production row on every run it
survives, not only when newly applied."* and *"a naive row count grows daily
for a single unchanged op and does not mean 'N new applications.'"* The
analysis layer correctly derives its live-count instead from
`outputs/latest/approved_watchlist_proposals.json:ops` (the current overlay),
using the audit log **only** for regression/rollback detection
(`durability_regression_ids`, `rolled_back_ids`). **This is a pre-emptively
closed loop at the analysis layer — the underlying producer behavior (WS12's
subject) is unchanged, but nothing downstream currently misreads it.**

### 12.3 Event types today vs. the richer set wanted

Today, exactly **two** event types are ever written to this file:
- `applied_to_production` (`production_application.py:560`)
- `rolled_back` (`production_application.py:635-637`, written by `rollback_last`)

Mapped against the richer 8-event set named in scope:

| Wanted event | Exists today? | Where (if anywhere) |
|---|---|---|
| proposal created | not in this log | `outputs/promotion_review/proposals_log.jsonl` (`promotion_proposals.py:152-156`) — a **separate** append-only log |
| approval recorded | not in this log | `outputs/promotion_approvals/approved_proposals.json` — a **separate**, non-append-only document (WS11) |
| first applied | **not distinguished** — see below | `applied_to_production`, indistinguishable from "refreshed" |
| confirmed active / refreshed (carried-forward, unchanged) | **not distinguished** | same `applied_to_production` row, no flag differentiating "new" vs "carried" |
| revoked | recorded, but not as an "event" field in *this* log | `production_revocations.jsonl` (`promotion_approvals.py:275-291`) — records have no `event` key at all, just `{target_id, approver, timestamp, notes}` |
| rebuild skipped | not in this log | `production_application_state.json:overlay_rebuild_skipped` (a state snapshot field, not an audit-log event) |
| rebuild failed | **does not exist anywhere** | An uncaught exception inside `apply_approved_proposals` would propagate to `daily_governance_run.py`'s Step 7 try/except (lines 256-265), which records only `{"ok": False, "error": str(exc)}` into `daily_governance_status.json`'s `stages.production_application` — never into the audit log, and (in practice) unlikely to fire at all since every sub-call `apply_approved_proposals` makes is already independently defensive (`load_pending_proposals`, `_load_raw`, etc. all catch-and-degrade internally) |

**Net:** the audit log currently records one fact ("this op is live as of this
run") using an event name (`applied_to_production`) that reads as "this was
just applied," which is the exact ambiguity the daily-tool-analysis doc has
already had to work around at the consumption layer rather than at the source.

### 12.4 Current size, row count, and growth projection

Measured directly from the real file:
- `outputs/promotion_approvals/production_application_audit.jsonl`: **17 rows,
  12,449 bytes**, spanning 2026-06-21T21:38 → 2026-07-28T13:38 (~37 days).
- Average row size: **732 bytes** (min 633, max 816).
- Event breakdown: 17× `applied_to_production`, 0× `rolled_back`.
- Type breakdown: `flock_watchlist_candidate_logic` (6), `flock_advisory_context_logic`
  (10), `crowd_context_change` (1) — **zero rows are of a durable type**
  (`watchlist_add/remove/rank/tag`) in the real dataset. All 17 real rows are
  legacy pre-durability rows and, confirmed by direct inspection, **every one
  is missing the `candidate_id` key entirely** (not merely `null`).

**Projection (analytical, based on the confirmed re-append-per-survivor
mechanism):** the growth-per-durable-op formula is straightforward because the
mechanism iterates the full post-resolution `watchlist_ops` list every run: for
one durable op that stays approved and is never revoked, and assuming the daily
cron (`run_daily_safe.sh` → `run_daily_governance`, Step 7) runs once per day,
that is **1 new row/day ≈ 365 rows/year ≈ 267 KB/year for a single unchanged
fact**. For N concurrently-live durable ops, growth is `N × 365` rows/year
(`≈ N × 267 KB/year`) — **linear in both the number of durable ops and the
number of days they stay approved, unbounded for as long as durability is
working as designed** (an op that is *supposed* to persist forever generates a
never-ending stream of rows, one per day it persists). This is a direct
consequence of the mechanism in 12.1, not a hypothetical: the mechanism has
simply not yet been exercised by a real durable-type application in this
environment (12.2 shows zero durable rows exist today), so the growth has not
yet begun, but the code path that would cause it is confirmed live and
untested for real durable data.

### 12.5 Does anything depend on the duplicate rows?

No. `rollback_last` (lines 607-640) only needs the **snapshot files on disk**
(`snap_dir.glob(f"{filename}.*.json")`, line 615) — it never reads the audit
log to find what to restore, only to *record* the rollback event afterward
(line 635-637). `_prior_durable_ops`'s fold (`seen[key] = (ts, op)`, line 247)
explicitly **overwrites** on each matching key, so only the *most recent*
occurrence of a given fact's row is ever actually used — every earlier
duplicate row for the same still-live fact is fold-collapsed away and
functionally inert. **The duplicate rows are pure historical trail with zero
functional dependency; they exist only as an audit/forensic byproduct of the
current "re-log everything that's still true" mechanism**, and could in
principle be pruned/deduplicated without breaking `_prior_durable_ops` or
`rollback_last` — that determination is left to the spec phase per the audit's
scope boundary.

---

## Summary of confirmed findings, ranked by blast radius

1. **[WS11] Lost-update race in `record_approval`** — confirmed by controlled
   experiment, not by inference. Two concurrent human approvals can silently
   collapse to one, both reporting success, with no error surfaced anywhere.
   No lock/CAS exists in the write path (`promotion_approvals.py:143-168`); the
   underlying `safe_write_text` atomic-replace (`data_governance.py:254-267`)
   protects single-writer integrity but does nothing for concurrent writers.
   Not covered by any existing test.
2. **[WS10] No fail-closed guard on whole-corruption of
   `production_application_audit.jsonl`** — confirmed by experiment. This file
   is the sole source `_prior_durable_ops` reconstructs durable watchlist
   membership from, yet unlike the approvals log and revocations log (both of
   which have dedicated `*_unreadable` guards checked before any overlay
   rebuild), total corruption of the audit log silently degrades to "no prior
   ops" with `overlay_rebuild_skipped` staying `False` — no signal anywhere.
   Zero live blast radius today only because production overlay consumption is
   gated off and no durable op has yet been applied for real.
3. **[WS12] Audit log conflates "first applied" with "still alive," and has no
   `rebuild_failed`/`confirmed_active`/`revoked`/`rebuild_skipped` event types
   of its own** — confirmed by reading the append site and by the real data
   (zero durable rows exist yet, so the projected N×365-rows/year growth is
   unrealized but the mechanism causing it is live and unguarded). Already
   flagged and worked around at the analysis-consumption layer
   (`daily-tool-analysis.md`), so no current consumer misreports it — but the
   producer-side ambiguity remains.
4. **[WS10, low severity]** `PROPOSAL_DISCOVERY_PROMOTION` has no producer
   anywhere on `main` — confirmed dead code / forward-looking placeholder, not
   an active gap. Landmine for whoever adds a discovery-promotion producer
   later without revisiting `_DURABLE_PROPOSAL_TYPES`.
5. **[WS10, low severity]** `outputs/promotion_approvals/snapshots/` (108
   files, 448K, unbounded, no retention policy) — an adjacent unbounded-growth
   surface discovered incidentally while auditing WS12, one snapshot pair per
   `write_files=True` run regardless of whether anything changed.
6. **[WS11, low severity]** `gui_v2/data/dash_governance.py:37,51-53`
   reimplements the approval-fold logic independently of
   `promotion_approvals.effective_approvals`, bypassing
   `is_valid_approval_record` validation — a latent double-implementation drift
   risk, and the highest-cost item in any future append-only migration.
