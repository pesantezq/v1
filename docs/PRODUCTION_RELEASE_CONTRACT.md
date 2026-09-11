# Production Release Contract

The rule this document exists to make achievable:

```text
production_code_sha == approved_release_sha
AND no unauthorized tracked code drift
```

Before this contract, that rule was **unreachable** — not hard, unreachable.
Two repository-level defects guaranteed drift, and this document plus
`portfolio_automation/release/` closes both.

Nothing here has been deployed. This is the repository half of a cutover that
requires separate human authorization.

---

## Defect 1 — production wrote to tracked files

Twelve generated artifacts under `outputs/` were tracked in git while
production rewrote them on every run. Eleven of the twelve were observed
modified during one ordinary production day. `.gitignore` already covered all
four parent directories and had no effect, because **ignore rules do not apply
to a file git already tracks**.

So a release worktree would go dirty within a single run, and
`no unauthorized tracked code drift` could never hold.

**Eleven of the twelve are now untracked.** Each was verified
`RUNTIME_GENERATED` — producer in production code, consumers that tolerate
absence, no test reading the committed copy, no human-authored content. The
full classification is in `portfolio_automation/release/contracts.py`.

**The twelfth is VS-001's frozen evidence, and it stays tracked.**

`outputs/performance/signal_outcomes.csv` was the sharp case, because it held
two incompatible roles at once. Its own source comment states the first:

> *"Its content hash participates in the freeze, so editing the data
> invalidates the preregistration rather than silently changing the experiment
> underneath it."*

`vertical_slice.preregistration.evidence_digest()` is sha256 of that file as it
sits on disk, so the committed bytes **are** VS-001's evidence of record. But
the daily pipeline **rewrote the same path on every run** — meaning that on the
live host the VS-001 freeze was already being invalidated daily.

### Resolved by moving the writer, not the evidence

Re-aiming `EVIDENCE_REL` would have changed what a completed experiment
hashes, so the split went the other way:

| | Path | Status |
|---|---|---|
| **Frozen evidence** | `outputs/performance/signal_outcomes.csv` | tracked, `IMMUTABLE_EXPERIMENT_EVIDENCE`, **production never writes it** |
| **Live projection** | `outputs/runtime/performance/signal_outcomes.csv` | never tracked, written by the producer, read by every operational consumer |

VS-001's `EVIDENCE_REL`, evidence bytes, `evidence_sha256`
(`960f7f42…f955b7`) and `freeze_digest` (`vsfreeze_f1c6ad41…`) are all
unchanged, and `verify_freeze` still passes.

No history is lost by moving the live path: the producer rebuilds the CSV in
full from `WatchlistStateStore` on every run and never reads it back, so the
database is the source of truth and the first run after a cutover repopulates
the runtime path completely.

There is deliberately **no fallback** from the runtime path to the frozen file.
A fallback would let production render 2026-06 frozen evidence as current data
— a silent-staleness defect. Absent runtime data reports as absent (every
consumer already degrades gracefully) and self-heals on the next run.

`outputs/portfolio/portfolio_snapshot.json` is plain runtime state. It was only
still tracked because one dashboard test read the committed copy out of the
real repository; that test now installs
`tests/fixtures/portfolio_snapshot_sample.json` into a temporary root, and a
companion test covers the absent-snapshot case.

### How the classification was established

Not by inspection. An earlier pass untracked all twelve on the strength of a
`grep` whose result list was capped at six matches, concluded no test read the
committed copy, and CI then failed 5 tests and errored 12 more. `git rm
--cached` leaves working copies on disk, so a local full-suite run cannot see
CI's view, and a hand-picked "affected suites" subset only re-tests what was
already suspected. The classification now rests on running the **full** suite
in a clean checkout where the untracked files are genuinely absent.

## Defect 2 — production was a git author

`scripts/run_doc_audit.sh` ran on production cron every Monday at 07:00 UTC and
committed three times per run: auto-fixed docs, pipeline-regenerated docs, and
`.agent/doc_audit_state.yaml`. It **never pushed**. That produced ten unpushed
commits between 2026-08-10 and 2026-09-07 and put an autonomous committer
inside the protected `.agent/` namespace.

The consequence for certification is the part that matters: even a perfect
cutover would decay the following Monday, so `production_code_sha ==
approved_release_sha` had a one-week half-life.

The invariant is now:

```text
PRODUCTION_RUNTIME_DOES_NOT_CREATE_GIT_COMMITS
```

### What this candidate changes

Repository authorship in `run_doc_audit.sh` is **opt-in**:

```bash
STOCKBOT_DOC_AUDIT_GIT_AUTHORSHIP=1   # engineering / QPC worker / CI
unset or 0                            # everywhere else (the default)
```

Mutation is gated **before it occurs**, not reverted afterwards. A
non-authoring run stops at an explicit mutation boundary having applied
nothing:

| Phase | Non-authoring | Authoring |
|---|---|---|
| resolve git range, run the auditor | ✅ | ✅ |
| write `outputs/latest/doc_audit_status.json` (ignored) | ✅ | ✅ |
| report suggested fixes | ✅ | ✅ |
| apply tracked doc auto-fixes | ❌ | ✅ |
| rewrite tracked generated docs | ❌ | ✅ |
| advance `.agent/doc_audit_state.yaml` | ❌ | ✅ |
| stage / commit | ❌ | ✅ |

An earlier version of this gate wrapped only `git commit` and then ran `git
reset` on the index. That was wrong twice over: unstaging does not undo file
writes, so the worktree was left dirty and the no-drift invariant was violated
anyway; and an index reset is not evidence that the tree is unchanged. There is
now **no** `git reset`, `git checkout` or `git restore` anywhere in the script —
cleanup of that kind could also destroy pre-existing operator changes.

One subtlety worth recording, because assuming otherwise is what broke the
first attempt: `write_doc_audit_status` looked read-only but persisted
coverage-gap bookkeeping into the **tracked** `.agent/doc_audit_state.yaml`.
That write is now opt-in (`persist_state=`), so the untracked status artifact is
still produced while the tracked file is left alone.

Verified end-to-end against the real repository: a non-authoring run leaves the
index hash, the tracked-worktree hash, `HEAD` and `doc_audit_state.yaml` all
byte-identical.

### What this candidate deliberately does not do

The QPC doc-audit worker **does not exist yet**. This candidate does not
pretend otherwise and does not build it. The target remains:

```text
QPC engineering/control plane → doc-audit task → isolated worktree
                              → candidate → PR → CI
```

Two things are still required and are explicitly deferred:

1. **The cutover must remove the production cron entry.** The env-var default
   makes an accidental invocation safe; it does not unschedule anything. Until
   the crontab line is gone, production still runs the audit weekly — it just
   no longer commits.
2. **Some plane must own doc-audit authorship**, or documentation drift stops
   being corrected. Deferring authorship is not the same as deferring the work.

## Production must not schedule repository-authoring doc-audit

Stated plainly, because it is the mandatory contract of this document:

> Production **must not schedule** `run_doc_audit.sh`, or any other task that
> creates git commits. Repository authorship belongs to the engineering /
> control plane, never to a production host.

---

## Scheduler identity

Repointing the two systemd GUI units is **not sufficient**. Root cron drives
production through thirteen further schedules that invoke `/opt/stockbot/scripts/*`
directly. Repointing systemd alone leaves the entire batch lane on legacy code
while the GUI serves the release: a mixed-release production, which is worse
than either release alone because no single SHA describes what ran.

Certification therefore covers all three together:

```text
every StockBot systemd ExecStart
+ every active StockBot cron entry
+ every StockBot timer/service
```

Each must resolve **either** directly to the approved release **or** through a
stable pointer whose target is the approved release.

### `ExecStart` alone is not enough

The live dashboard unit is the proof:

```ini
WorkingDirectory=/opt/stockbot
ExecStart=/opt/stockbot/current/.venv/bin/uvicorn gui_v2.app:app
```

`uvicorn gui_v2.app:app` names the application module **relatively**, so Python
resolves `gui_v2` from the process working directory. That unit runs the
*release interpreter* against **legacy application code**, and an
`ExecStart`-only parser certifies it as aligned. So `WorkingDirectory` and
`RootDirectory` are part of the execution surface, not metadata.

`EnvironmentFile` is parsed too, but classified separately: a credential file is
not release code, so `EnvironmentFile=/opt/stockbot/.env` is **surfaced** —
the cutover must re-provision it — without being counted as application-code
drift. A unit whose only legacy path is a secret still certifies `OK`, and the
secret is reported under `secret_paths_outside_release`.
`portfolio_automation/release/scheduler.py` parses unit and crontab content and
reports any surface that stays bound to the legacy checkout. It checks
arguments as well as the executable, because a unit whose `ExecStart` is
correct but whose `--config` still names `/opt/stockbot` runs new code against
old configuration — and would otherwise pass every SHA check.

Exempt, and named explicitly rather than silently skipped:

| Path | Why |
|---|---|
| `/usr/local/bin/cloudflared` | external tunnel infrastructure |
| `/usr/local/sbin/stockbot-engineer-read` | system-installed, transitional evidence bridge |
| `/usr/local/sbin/stockbot-evidence-publish` | system-installed evidence publisher |

Those three are not in git and are not release code. They must survive the
cutover untouched; the transitional bridge is not to be rebuilt or expanded.

## Release path model — `current` pointer recommended

| | **A. Direct SHA paths** | **B. Stable `current` pointer** |
|---|---|---|
| Layout | schedulers name `releases/<sha>` | `current` → `releases/<sha>` |
| Code identity | maximally explicit | explicit via one readlink |
| Release cost | edit 15+ scheduler entries | one atomic symlink swap |
| Rollback | edit them all back | reverse the swap |
| Mixed-release risk | **high** — a partial edit splits the lanes | **impossible** — single switch |
| Old releases | untouched | untouched |

**Recommendation: B.** The deciding factor is not convenience but the mixed
-release failure mode. Option A makes every release a fifteen-point edit where a
single missed entry produces exactly the split-lane state described above, and
that state is undetectable from a SHA check alone. Option B has one switch, so
the lanes cannot disagree.

```text
/opt/stockbot/releases/<approved-sha>/     immutable, never mutated after creation
/opt/stockbot/current -> releases/<sha>    the only path any scheduler names
/opt/stockbot/                             legacy checkout, retained as rollback
```

`scheduler.ExecutionSurface.resolves_to_release` accepts either shape, so a
host part-way through migration can be certified truthfully rather than having
to lie in one direction.

### Path alignment is not release identity

Scheduler certification proves only that scheduler strings sit *lexically*
beneath `/opt/stockbot/current`. It never resolves the symlink, so every
surface could report `OK` while `current` still targets an older or
unauthorized release — and the certification would not establish the one thing
it claims. `certify_scheduler_identity` therefore reports
`scope: path_alignment_only`, and `release/pointer.py` supplies the missing
proof:

```text
/opt/stockbot/current      exists, and IS a symlink
  -> releases/<sha>        resolves, and lives under the releases root
  -> git HEAD there        the resolved tree's actual commit, full 40-hex
  == approved_release_sha  which equals the approved release
```

Every link is a separate failure mode and every one fails closed — including a
*missing* SHA, because "we could not determine what is deployed" must never
certify as "the right thing is deployed". Abbreviated SHAs are rejected: a
prefix comparison invites a false match. `certify_release_identity` requires
scheduler alignment **and** pointer proof together.

## Runtime path contract

| Class | Paths | Rule |
|---|---|---|
| `RUNTIME_MUTABLE` | `data/`, `outputs/`, `logs/`, caches | production writes freely; the release must **not** track any of it |
| `IMMUTABLE_EXPERIMENT_EVIDENCE` | `outputs/performance/signal_outcomes.csv` | committed evidence of a completed experiment that sits under a runtime root — tracked on purpose, and production must never write it |
| `RELEASE_IMMUTABLE` | `portfolio_automation/`, `watchlist_scanner/`, `gui/`, `gui_v2/`, `scripts/`, `config/`, `docs/`, `evals/`, `tests/`, `tools/`, `agent/`, `policy_evaluator/`, `theme_engine/`, `main.py`, `requirements.txt`, `.gitignore` | read-only at runtime; a write here is drift |

At the time of writing the three runtime roots held 12 / 0 / 0 tracked files.
Ten were untracked, so `data/` and `logs/` are fully clean and enforced as
such. `outputs/` is **not** clean — it still tracks the two source-required
exceptions above — so the guard is written as an explicit allowlist rather than
a zero-tracked-files claim. Asserting cleanliness that the repository has not
reached would make the test a false witness, which is worse than no test.

The eventual `/var/lib/stockbot` state migration is **not** part of this
contract or the first cutover. For the first cutover, runtime state stays where
it is and is attached into the release tree, which also means rollback needs no
data movement — both trees reference the same databases.

## Venv preflight

`requirements.txt` being byte-identical between the live commit and the release
does **not** prove the existing `.venv` is safe. That file describes
third-party dependency versions; it says nothing about where the interpreter
resolves *project* modules from. A venv can bind the application to a specific
source tree without touching requirements at all:

- an editable install writing a `.pth` or `__editable__*` finder;
- a `*.egg-link` recording an absolute source directory;
- `dist-info/direct_url.json` recording the tree it was built from;
- a stray `.pth` adding the legacy checkout unconditionally.

Any of those lets a cutover repoint systemd and cron while the interpreter
keeps importing `portfolio_automation` from `/opt/stockbot`. Production would
report the new SHA and run the old code — the most deceptive available outcome,
because every identity check passes.

So the gate is not "requirements unchanged" but:

```text
application imports executed with the selected release environment
resolve project modules from the approved release, not the legacy checkout
```

`release/preflight.py` provides both halves: `scan_environment` finds the
static bindings, and `verify_import_origin` proves the dynamic outcome from the
`__file__` an interpreter actually resolved. A module missing from the evidence
is a failure, not a pass. Collect it during cutover with the release
environment:

```bash
/opt/stockbot/current/.venv/bin/python -c \
  'import json, portfolio_automation, watchlist_scanner, policy_evaluator, agent, theme_engine
   print(json.dumps({m.__name__: m.__file__ for m in (
       portfolio_automation, watchlist_scanner, policy_evaluator, agent, theme_engine)}))'
```

## Production identity requires three independent gates

`PRODUCTION_RELEASE_IDENTITY` is eligible for PASS only when all three hold:

```text
SYSTEMD_UNIT_VALIDITY      = PASS     real systemd-analyze verify
SCHEDULER_ALIGNMENT        = PASS     portfolio_automation.release.scheduler
RELEASE_POINTER_IDENTITY   = PASS     portfolio_automation.release.pointer
----------------------------------
PRODUCTION_RELEASE_IDENTITY eligible for PASS
```

**None of the three may infer another.** They fail independently and for
unrelated reasons:

| gate | the question it answers | what it cannot tell you |
|---|---|---|
| `SYSTEMD_UNIT_VALIDITY` | would systemd accept and load these units? | which release the commands point at |
| `SCHEDULER_ALIGNMENT` | do the effective executable paths resolve to the approved release? | whether the unit is loadable at all |
| `RELEASE_POINTER_IDENTITY` | does `current` resolve to the approved SHA? | whether anything actually uses `current` |

A unit can be flawless and still execute the previous release. It can name the
release correctly and still be a unit systemd refuses to load. The pointer can
be perfect while half the schedulers ignore it.

### Why validity is delegated rather than implemented

An earlier revision of the scheduler certifier emulated systemd's acceptance
rules. That emulation drew seven consecutive review findings — including two
false *rejections* that would have blocked a correct cutover — and was removed
in PR #40 rather than extended. Reproducing another program's acceptance
semantics from documentation is a losing game when the program itself is
available to ask. Removing it left the question genuinely uncovered, which this
gate closes with the real verifier.

### The verifier invocation is fixed, and why

```bash
systemd-analyze verify --recursive-errors=no <unit>.service
```

**By unit NAME, never by path.** Verifying `FragmentPath` reports on the base
fragment; every production application unit here carries a `zz-release.conf`
drop-in, and `stockbot-daily.service` carries two. Name-based lookup applies
drop-ins with normal precedence. Measured on systemd 255 in both directions: a
valid base broken by its drop-in fails and names the drop-in's binary; a broken
base repaired by its drop-in passes silently.

**`--recursive-errors=no` is required.** It is not a weakening — it is what
makes the exit status mean anything. Per `systemd-analyze(1)`: "If this option
is not specified, zero is returned as the exit status regardless whether
warnings arise during verification or not." Measured:

```text
unit                                (none)   no    one   yes
pb-valid                              0      0     0     0
pb-badval  (bad TimeoutStartSec)      0      1     1     1
pb-badsec  (unknown section)          0      1     1     1
pb-clean-with-dep                     0      0     0     1
```

Rows two and three are the trap: the verifier complains on stderr and still
exits 0. Row four is why the mode is `no` and not `yes` — that unit is itself
clean and merely `Requires=` a unit with a warning, so `yes` would fail our
gate for a defect owned by an unrelated system unit.

`--man=no` is deliberately **not** passed: it changed no verdict in any
measured case, and suppressing a class of check to keep a gate green is how
gates stop meaning anything.

### Loaded-state currency is part of the verdict

`systemd-analyze verify` validates unit files **on disk**. If PID 1 reports
`NeedDaemonReload=yes`, the on-disk text is not what the manager has loaded, so
a clean result would be evidence about a configuration that is not running:

```text
NeedDaemonReload=yes  ->  SYSTEMD_UNIT_VALIDITY = NOT_CERTIFIABLE
```

Escalate. **Do not run `systemctl daemon-reload` to make this pass** — that is
mutating production to manufacture the answer you wanted.

### The three gates must describe one observation

Being green is not enough. The three results must be *about the same
observation of the same host*, because each is collected separately:

```text
production observation begins
        ↓
observation_id = immutable identifier for this evidence run
host           = the observed production host
        ↓
pointer evidence    -> host + observation_id
scheduler evidence  -> host + observation_id
systemd validity    -> host + observation_id
        ↓
aggregate requires an exact match on (host, observation_id)
```

Without this, a *genuine* `SYSTEMD_UNIT_VALIDITY = PASS` collected on a staging
box covers the same unit **names** as production, so name-level coverage cannot
tell the two apart:

```text
staging    systemd validity     PASS
production scheduler alignment  PASS   ->  false production_release_identity PASS
production pointer identity     PASS
```

Host identity alone does not fix it, and neither does a timestamp. Two
observations of the same production host minutes apart are still two
observations: a pointer read taken before a deploy and a unit verification
taken after it are each individually truthful and jointly describe a system
that never existed. So the binding is `(host, observation_id)`.

**The id is issued by the collection flow and passed to each collector.** It is
never minted by a collector and never stamped onto results by the aggregator —
an aggregator that invents a shared id would be manufacturing exactly the
agreement it is supposed to be checking. `release.observation` therefore
imports nothing that could generate one, and a test pins that.

```bash
export STOCKBOT_OBSERVATION_ID="obs-$(date -u +%Y%m%dT%H%M%SZ)-$$"
```

**A shared token is not a snapshot.** The id proves the collectors were told
they belong to one run; it cannot prove the system held still during it. If a
deployment lands between the pointer gate's reading and the unit verification,
both legitimately carry the same id while describing different releases. So the
gates are bound to observed **state** as well: the collector records the release
the host was running, bracketing its whole run, and the aggregate requires it to
equal the pointer gate's `target_sha`.

```text
validity evidence saw release X
pointer gate certified release Y
X != Y  ->  production_release_identity = NOT_ESTABLISHED
```

A deployment landing *inside* the validity collection makes that capture
`NOT_CERTIFIABLE` outright — its unit evidence spans two releases.

Evidence that cannot say which run it belongs to is **not** assumed to be
co-located:

```text
missing / malformed / mismatched provenance
    ->  production_release_identity = NOT_ESTABLISHED
```

That is a deliberate cost. A flow that does not yet thread one observation id
through all three gates cannot certify production identity until it does. The
individual gates are unaffected — whether systemd would accept these units is
true of the host regardless of what else was observed alongside, so
`SYSTEMD_UNIT_VALIDITY` does not require the id for its own verdict.

### Loaded-state currency must hold ACROSS the verification window

`NeedDaemonReload=no` captured before the verifier runs is not enough. The
collector observes each unit **twice** — once before verification (`##SHOW`)
and once after (`##RECHECK`) — and each observation records a digest of the
unit's fragment and its drop-ins:

```text
##SHOW <unit>      LoadState, NeedDaemonReload, FragmentPath, DropInPaths,
                   NorthstarFragmentDigest, NorthstarDropInDigest
      systemd-analyze verify --recursive-errors=no <unit>
##RECHECK <unit>   the same properties, observed again
```

Certification requires the two observations to agree, and requires
`NeedDaemonReload=no` in both. Measured on systemd 255:

| what happened in the window | `NeedDaemonReload` before / after | caught by |
|---|---|---|
| nothing | `no` / `no` | — (PASS) |
| unit rewritten, not reloaded | `no` / `yes` | reload-state recheck |
| unit rewritten **and** reloaded | `no` / `no` | configuration digest |
| unit changed **and restored** (A→B→A) | `no` / `no` | inode/mtime/ctime signature |
| drop-in added **and removed** | `no` / `no` | search-path directory anchor |

The fourth row is why content hashing alone is not enough: restoring identical
bytes leaves both endpoint digests equal while the verifier read the transient.
Each observation therefore also records inode, size and nanosecond mtime/ctime
via `stat`, and a rewrite advances ctime even when the content is unchanged.

The fifth row is why per-file anchors are not enough either. A drop-in that
appears and disappears inside the window is absent from `DropInPaths` at both
ends, so its digest and its stat signature each read `none` twice — while
`systemd-analyze verify` reads the search path from **disk**, not from the
loaded manager state, and parsed it. Measured on systemd 255. Each observation
therefore also stats the unit search directories and each unit's `<unit>.d`
directory, recording a non-existent path as `absent`.

### Why endpoint anchors, and not a change monitor

The question this has to answer is not "is the state the same at both ends" —
it is "did the relevant state mutate at any point while the three gates were
collected". Those differ, and only the second is sound.

Endpoint anchoring answers the second question **when the quantity compared is
a mutation witness rather than a state value**, which is what these three
anchors are chosen to be:

| witness | what it proves | why it cannot be restored |
|---|---|---|
| `ctime` (ns) | the inode was modified | no userspace API sets ctime; `touch`/`utimensat` set atime/mtime only |
| inode number | the file was replaced | an atomic rename installs a different inode |
| directory mtime/ctime | an entry was added or removed | the containing directory is stamped by the kernel on link/unlink |

Each was measured on this host rather than assumed: rewriting a file with
identical bytes keeps the inode and advances mtime and ctime; `touch -r`
restores mtime and leaves ctime advanced; an atomic rename changes the inode;
creating and removing a drop-in advances the containing directory's timestamps.

A filesystem change monitor (`inotify`) was considered and **rejected**:

* `inotifywait` is not installed on the reference host, so it would become an
  unproven production dependency — and the rule is to prove availability
  before depending on a tool, not to assume it;
* it requires a concurrent long-lived process inside what is otherwise a
  strictly read-only single-shot SSH collection;
* it has a silent-loss failure mode (`IN_Q_OVERFLOW`) that must be detected
  and handled or events are simply missed.

`stat` is already a dependency of this collector, needs no daemon, cannot
overflow, and — because ctime and inode are not restorable — answers the
interval question rather than the endpoint question. So the mechanism is
endpoint-*sampled* but interval-*sound*.

**A narrowed anchor set blocks rather than being merely noted.**
`STOCKBOT_UNIT_SEARCH_PATH` changes only which directories the anchors
observe — it cannot constrain where the verifier actually resolves units from.
So the collector records systemd's effective path (`search_path_effective`)
*unconditionally*, alongside what the anchors covered, and certification fails
closed when the anchored set is narrower. A documented blind spot still
certifies unless it blocks.

**A capture is one run, and one terminator does not prove it.** A run
truncated before its `##END` can be prepended to a complete capture: the
combined stream has exactly one terminal marker, so a terminator count sees
nothing, while the reader keeps units from both runs and lets the later
scalars overwrite the earlier host, id and release — attributing one host's
unit evidence to another. Run-level sections (`HOST`, `OBSERVATION_ID`,
`CHECKED_AT`, `SYSTEMD_VERSION`, the release pointers, the search paths, and
the inventories) may therefore appear exactly once, and a duplicate is a
defect regardless of how the stream terminates.

**The anchored directories are asked of systemd, not assumed.** The effective
unit load path is wider than the three obvious directories: on systemd 255
`systemd-analyze unit-paths` also lists `/etc/systemd/system.control`,
`/run/systemd/system.control`, `/run/systemd/transient`, the `.attached` and
generator directories, and `/usr/local/lib/systemd/system`. Measured: a
transient drop-in created under `/usr/local/lib/systemd/system` is read by the
verifier while a three-directory anchor stays **unchanged** — a false PASS. The
collector therefore asks `systemd-analyze unit-paths` (read-only) and anchors
the complete set; if systemd cannot be asked it falls back to the full static
list *and records that it did*, so a narrowed anchor set is visible in the
artifact (`search_path_source`, `search_path`) rather than silent.

**Stated limits.** This detects mutation, not intent, and its soundness rests
on the kernel maintaining ctime monotonically. It does not cover: an actor with
root who moves the system clock backwards (which would itself surface as a
mismatch, since the endpoints would differ); mutation through a path outside
the anchored set; or a change to state this gate does not claim to cover. The
anchored set is derived from the certification inputs — the release pointer,
the unit fragments, their drop-ins, and the unit search directories — not from
watching the filesystem wholesale.

The third row is why the digest exists. Each observation is internally
consistent, so reload state reads `no` at both ends while
`systemd-analyze verify` read bytes PID 1 never loaded. Hashing the effective
configuration is what pins *which* bytes the exit status describes.

A configuration that moved while the gate was looking at it is
`NOT_CERTIFIABLE`. **Collect again once the deployment has settled — do not
`daemon-reload`**, which would be mutating production to produce the answer we
wanted.

`sha256sum` is read-only, like every other command the collector runs.

### Verdicts

`PASS` requires all of: complete expected inventory; every required unit
`LoadState=loaded`; every required unit `NeedDaemonReload=no` **both before and
after verification**; every required unit's effective configuration digest
**unchanged across the verification window**; a real verifier result for every
required unit obtained with the required flag; every such result clean; no
required unit skipped; no relevant discovered unit left unclassified.

`production_release_identity` additionally requires all three gates to carry a
matching `(host, observation_id)`.

Anything preventing those facts from being established is `NOT_CERTIFIABLE`;
anything establishing them as false is `FAIL`. **"Could not verify" is never
`PASS`.**

### How it runs — observation and decision are separated

```bash
# One id per observation, issued here and reused by every gate collected in
# this run. Without it the three gates cannot be bound and the aggregate is
# NOT_ESTABLISHED.
export STOCKBOT_OBSERVATION_ID="obs-$(date -u +%Y%m%dT%H%M%SZ)-$$"

ssh <host> "STOCKBOT_OBSERVATION_ID=$STOCKBOT_OBSERVATION_ID bash -s" \
    < scripts/collect_systemd_validity_evidence.sh \
    | python3 scripts/certify_systemd_validity.py --out evidence.json
```

### The aggregate checks the artifact, not just its verdict

`SYSTEMD_UNIT_VALIDITY: PASS` is a conclusion the artifact asserts **about
itself**, and that artifact is a file that travels between processes, hosts and
runs. Reading the verdict without checking the fields it was supposedly derived
from would let a stale, truncated, hand-edited or differently-versioned artifact
certify production purely by asserting its own conclusion. So before the verdict
is trusted, the aggregate requires the artifact to be self-consistent:

| checked | why |
|---|---|
| `schema` / `schema_version` | a differently-versioned artifact may use the same field names for different meanings |
| `observe_only` is `true` | this gate is observation-only by contract, so an artifact saying otherwise did not come from it |
| `blockers` / `errors` empty when PASS | a verdict cannot outrank the reasons recorded against it |
| per-unit exit status agrees with `verified_units` | nor outrank its own per-unit results |
| artifact present at all | missing evidence is a defect, not silence |
| one successful record per `verified_units` entry | otherwise an artifact can *assert* verification with no exit status recorded anywhere, and there is nothing to contradict |

Every item is an **internal** inconsistency — the artifact disagreeing with
itself — which is why each fails closed rather than warning: evidence that
cannot be self-consistent cannot be selectively believed. The reasons are
reported in `validity_contract_defects`.

The collector only observes: `systemd-analyze --version|verify|unit-paths`,
`systemctl show|list-unit-files`, `sha256sum`, `stat`, `readlink -f`,
`git rev-parse`. Every one of those is read-only, and no new tool was
introduced to establish snapshot consistency — which is also why choosing this
mechanism required no reconnaissance against production. It never reloads,
starts, stops, enables,
masks, or writes anything, and a test asserts that. The certifier decides and
touches no host. So the production side of this gate is strictly read-only, and
the decision logic stays unit-testable without a VPS, root, or systemd.

A truncated capture is `NOT_CERTIFIABLE`, not a clean host.

## Backup contract — unchanged

PR #37 behaviour is preserved exactly. `BACKUP_REQUIRED` is a *classification*,
not `required=True`:

- hard-required: `data/portfolio.db`, `data/finance_history.json`;
- optional, absence recorded as a gap: the other four databases.

`rd_control.db` and `institutional_intelligence.db` do not exist on the live
host — the release creates them. They must stay optional so their absence
cannot block a cutover, and a test now enforces that.

Untracking the twelve creates no backup gap: `outputs/` is classified
`REGENERABLE`, and the one artifact that could have carried unique history
(`signal_outcomes.csv`) is a projection of a `BACKUP_REQUIRED` database.

**DISASTER RECOVERY IS NOT YET CERTIFIED** — no current off-box encrypted
backup is proven, and no independent off-box copy of the backup key is proven.
