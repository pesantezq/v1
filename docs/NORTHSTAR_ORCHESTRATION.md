# Northstar Continuous Mission Orchestration

Added 2026-10-04 by `northstar_continuous_mission_orchestration_foundation`.

This document specifies the repo-native control plane that automates the chain
from a Claude implementation PR to the next already-authorized mission, WITHOUT
weakening Northstar's authority model. It is a **deterministic** control plane:
no LLM writes protected state, selects a mission, or merges a PR. GitHub owns
waiting and deterministic gate evaluation; a human/operator owns final authority.

> **Bootstrap status: SHADOW.** This foundation ships in `shadow` mode. It
> computes and emits decisions but performs no merge, no governance-state
> mutation, and no Claude dispatch. It may never switch itself to `enabled`
> (see *Bootstrap rule*). Activation is an operator/GPT action on durable `main`
> after post-merge certification.

## Pipeline

```
protected main state  ─┐
mission registry       │
                       ▼
Claude PR → northstar-ci (exact head) ─┐
          → base-main authority + default-deny change envelope ─┤
          → control-plane conformance (for control-plane class) ┤
                                       ▼
            NORTHSTAR_DETERMINISTIC_ASSURANCE_GATE  (scripts/northstar_pr_gate.py)
                     (no Codex / no human in PASS/FAIL)
                                       │ PASS
                                       ▼
                             GitHub auto-merge (enabled mode only)
                                       ▼
                             northstar-ci on main (push, exact merge SHA)
                                       ▼
                             post-merge orchestrator  (northstar-orchestrator.yml)
                                       │  exact SHA / state verification
                                       ▼
                             deterministic transition engine  (scripts/northstar_transition.py)
                               │ preauthorized_auto        │ human_required
                               ▼                            ▼
                     governance transition PR              STOP
                               ▼
                     merge + post-merge CI
                               ▼
                     next authorized Claude mission  (claude-authorized-mission.yml
                                                      + scripts/northstar_mission_packet.py)
```

## Components

| Artifact | Role |
|---|---|
| `.agent/mission_registry.yaml` | Declarative routing contract (NOT authority). Risk class, executor, prompt source, auto-dispatch eligibility, transition policy, next/resume target, human boundary. Controller mode. |
| `.agent/missions/<id>.md` | Protected prompt source for each mission. |
| `scripts/northstar_pr_gate.py` | `NORTHSTAR_DETERMINISTIC_ASSURANCE_GATE` — the autonomous merge authority (no AI/human in PASS/FAIL). Pure `evaluate_merge_gate` / `evaluate_change_envelope` / `check_control_plane_conformance`. |
| `scripts/northstar_transition.py` | Deterministic transition proposer. Pure `propose_transition(inputs)`. Emits an allowlisted governance-PR proposal or STOP. |
| `scripts/northstar_mission_packet.py` | Deterministic Claude dispatch packet builder. Pure `build_mission_packet(inputs)`. |
| `scripts/northstar_materialize_transition.py` | Deterministic transition MATERIALIZER. Surgically applies a trusted proposal to the allowlisted protected fields (comment-preserving), restoring the paused authorization EXACTLY; fail-closed; no main push. |
| `.github/workflows/northstar-pr-controller.yml` | Read-only `Northstar merge gate` job **+ a separate `auto-merge-effect` write job** (native GitHub auto-merge), unreachable in shadow. |
| `.github/workflows/northstar-orchestrator.yml` | Read-only post-merge certify/propose job **+ a separate `governance-pr-effect`** that keeps the default `GITHUB_TOKEN` read-only and mints a short-lived, repo-scoped GitHub App token only for the bounded governance branch/PR write, unreachable in shadow. |
| `.github/workflows/claude-authorized-mission.yml` | Read-only `build-packet` job **+ a separate `claude-dispatch` effect job** that uses `CLAUDE_CODE_OAUTH_TOKEN` for Claude and a short-lived GitHub App installation token for branch/PR writes; includes a manual non-mutating GitHub App credential preflight; unreachable in shadow. |
| `.github/scripts/northstar_build_{transition,packet}_inputs.sh` | Trusted-main IO glue that assembles controller inputs for the effect/decision jobs. |

### Decision → effect (privilege separation)
Every decision is computed by a **read-only** job (least-privilege token, trusted
main code) that emits `mode`/`decision`/`sha` outputs. A **separate write effect
job** — with only the minimal permissions it needs — runs ONLY when those trusted
outputs prove `mode == enabled` and the exact decision, and re-validates state
immediately before acting. The write jobs are **unreachable while mode is shadow**:

| Decision job (read-only) | Effect job (write, enabled-only) | Bounded effect |
|---|---|---|
| `northstar-merge-gate` (PASS) | `auto-merge-effect` (`pull-requests:write`) | enable GitHub-native auto-merge (ruleset stays authoritative) |
| `post-merge-certify` (PROPOSE) | `governance-pr-effect` (default token read-only; App token: `contents:write`,`pull-requests:write`) | materialize + push governance branch + open ONE PR (never main) |
| `build-packet` (DISPATCH) | `claude-dispatch` (default token read-only; App token: `contents:write`,`pull-requests:write`) | invoke pinned Claude action via `CLAUDE_CODE_OAUTH_TOKEN`, tool-constrained, open PR only |

## Authority precedence (fail closed)

The AUTHORITATIVE source of which mission is dispatchable, and of every bounded
authorization, is protected state on base `main`:
`.agent/project_state.yaml`, `.agent/phase_status.yaml`,
`config/ew0a_runtime.json`, `config/agent_policy.yaml`. The registry may **narrow**
but can never **widen** authority. Every controller re-derives its decision from
base-main protected state at evaluation time; a registry/state disagreement is a
hard FAIL. A candidate branch editing the registry (or any protected state) cannot
gain authority: the gate rejects a PR whose base-main authority differs, and
dispatch/transition read authority from base main, not from the PR.

## Deterministic assurance gate (merge authority)

> **Codex = optional advisory semantic reviewer. The Deterministic Assurance Gate
> = merge authority.** NO AI review (Codex or any LLM) and NO human participates in
> PASS/FAIL. A Codex outage/quota/absence/stale-review has ZERO effect on an
> otherwise-valid autonomous E1–E3 merge; Codex comments are advisory telemetry only.

`evaluate_merge_gate(inputs)` returns a machine-readable audit with a per-check
breakdown and an overall `PASS`/`FAIL`, failing closed on any missing/ambiguous
input. All of the following must hold (no AI/human input anywhere):

- PR open, not draft, base == `main`, mergeable.
- Candidate mission is INDEPENDENTLY declared (a `MISSION = <id>` body line or
  `mission:<id>` label) and equals the protected base-main current mission.
- `northstar-ci` conclusion == success, completed, **bound to the current PR head SHA**.
- The mission's **change class** is known, and the changed files satisfy that class:
  - `normal` / `control_plane`: the **DEFAULT-DENY change envelope** (below);
  - `control_plane` additionally: base-main authorizes control-plane scope AND the
    candidate passes **control-plane security conformance**;
  - `governance_transition`: the PR diff **exactly** equals a re-derived materialized
    transition.
- Required mission **test groups** exist (and ran, via the exact-head CI success).
- No forbidden authority introduced (auto_* flips, C1, Phase 0D, broker/capital).
- Current remote `main` has not advanced since the snapshot.

### Default-deny change envelope
Every mission has a machine-readable `change_policy` in the PROTECTED base-main
registry (class + `allowed_paths` + `allowed_path_prefixes` + `forbidden_path_prefixes`
+ `required_test_groups` + `semantic_guards`). `evaluate_change_envelope(changed,
envelope)` DENIES BY DEFAULT: any changed path not explicitly allowed → FAIL; an
empty envelope denies everything. A global absolute-forbidden set
(`.agent/project_state.yaml`, `.agent/phase_status.yaml`, `config/ew0a_runtime.json`,
`config/agent_policy.yaml`, secrets, broker, systemd) can never be allowed by any
`normal`/`control_plane` envelope — those are mutated ONLY by an authenticated
`governance_transition`. Authority precedence: **protected base main > registry
routing > candidate declaration**; a candidate may narrow but never widen its own
envelope (the gate reads the envelope from base main, not the PR).

### Control-plane autonomous trust rotation
A PR that changes the controller/governance implementation passes automatically ONLY
when protected base main's registry marks the current mission `change_policy.class ==
control_plane`. **The candidate's controller code never evaluates its own PR** — the
pr-controller checks out base `main` and runs `main`'s gate/assembler against the
candidate's fetched files. `check_control_plane_conformance` deterministically rejects
a control-plane candidate that weakens a constitutional invariant (adds
`pull_request_target`, uses a non-SHA-pinned action, ships the registry in a controller
mode that DIFFERS from the trusted base-main mode (base-relative: a candidate can never
change the rollout mode -- shadow->enabled self-enable and enabled->shadow self-disable both
fail; only an operator bootstrap changes it), grants write-all, etc.). After merge + exact-SHA post-merge certification,
controller N+1 becomes the durable evaluator for subsequent PRs. No human, no Codex.

Beyond `controller.mode`, `check_registry_authority_narrowing` (trusted base-main code) requires the
CANDIDATE mission registry to be IDENTICAL to, or strictly NARROWER than, trusted base main — candidate
tests are never an authority source. `schema_version`, the `controller` scalars, per-mission
`executor`/`risk_class`/`prompt_source`/`completion_gate`, and `on_success` routing are pinned to exact
equality; `auto_dispatch_allowed_risk_classes` may only shrink and `human_required_risk_classes` only
grow; per mission, `auto_dispatch` false->true, `allowed_authority`/`allowed_path(_prefixes)` additions,
`forbidden_authority`/`forbidden_path_prefixes`/`required_test_groups`/`semantic_guards` removals,
`human_boundary_required` true->false, and `transition_policy` human_required->preauthorized_auto all
FAIL; a new mission id, a removed mission id, or any unknown authority-bearing field FAILS CLOSED. This
holds AFTER activation: an `enabled` base with an `enabled` candidate that attempts any such widening is
still rejected (closes the registry-authority-widening P1).

### Codex (advisory only)
Codex is NOT requested, polled, waited on, or required by orchestration. If an
operator or a background process leaves Codex comments, they are recorded as
`advisory_codex` telemetry and never alter `PASS`/`FAIL`/`would_merge`/auto-merge/
transition/dispatch eligibility.

## Post-merge orchestrator semantics

Triggers from successful completion of `northstar-ci` on `main`. Fails closed
unless: workflow conclusion == success, event == push, head branch == main, and
workflow head SHA == current remote main SHA. Executes only trusted controller
code already on certified `main`; never runs untrusted PR artifacts. Emits a
durable machine-readable certification record.

## Transition semantics

`propose_transition(inputs)` reads only trusted inputs (protected state, certified
main SHA, completed mission, registry, merge/post-merge evidence, preserved paused
authorization). For a `preauthorized_auto` edge it emits a proposal that edits only
an explicit allowlist of protected-state fields (the dispatch fields + the bounded
authorization swap) — delivered as a governance branch/PR that goes through ordinary
CI/review/merge. It never pushes protected state directly to `main`. For
`human_required` it emits no proposal and STOPs. The paused VS-002 adapter
authorization is restored byte/structure-equivalently from base-main
`phase_status.yaml`; it is never reconstructed or widened.

## Claude dispatch semantics

A future Claude mission is dispatched only when protected base-main state says
all of: executor == claude, auto_dispatch == true, risk class permits automatic
dispatch, the transition is already authorized, and the current exact main SHA is
certified. Prompt content comes only from the protected `prompt_source`; a
caller-supplied mission id or prompt can never override protected state or widen
authority. Claude receives the minimum GitHub permissions and never receives PR
approval or merge authority.

## Human-required wall

Automation STOPs (no merge, no transition, no dispatch) for: E4 missions; real
frozen evidence access/execution; production deployment; broker write; capital/
trading action; C1 advancement; Phase 0D or later; ambiguous/contradictory
protected state; inability to prove exact-head CI; inability to prove current
remote-main identity. (There is no human/Codex gate — these are deterministic STOPs.)

## Controller modes

- `shadow` — compute decisions; emit `MERGE_READY`, `POST_MERGE_CERTIFIED`,
  `NEXT_MISSION_READY`; perform NO merge, NO state mutation, NO dispatch.
- `enabled` — may act only where every deterministic gate passes and registry
  policy allows. Activation is an operator/GPT action; this PR ships `shadow`.

## Bootstrap rule

This implementation PR must not auto-merge itself and must not self-bootstrap
authority. `enabled` capabilities activate only after this PR passes exact-head
CI and the deterministic assurance gate, is manually merged under existing
governance (the ONE final bootstrap-era manual merge, needed only because the
controller is not yet durable on `main`), post-merge main CI succeeds on the exact
merge SHA, and the readiness checks prove base-main controller N evaluates candidate
N+1. After that, E1–E3 progression is autonomous — no human reviewer and no Codex.

## GitHub Actions threat model

- Third-party actions pinned to immutable commit SHAs.
- Minimal `permissions:` per workflow/job; secrets unavailable until needed.
- No `pull_request_target` execution of untrusted candidate code.
- The privileged post-merge workflow runs trusted `main` code only; never
  downloads/executes untrusted PR artifacts.
- Untrusted PR fields are never interpolated into shell unsafely.
- E4 / production secrets sit behind a GitHub Environment with required reviewers.
- Claude authentication uses the operator-provisioned `CLAUDE_CODE_OAUTH_TOKEN`; GitHub writes use short-lived GitHub App installation tokens that are revoked at job end.
- Fail closed on API/query ambiguity.

## Input-assembly hardening (fail-closed on partial data)

`scripts/northstar_assemble_gate_inputs.py` feeds the gate and is hardened so a
security decision is never made on partial or tautological data (and never fetches,
polls, or waits on Codex):
- the candidate's claimed mission is derived INDEPENDENTLY from the PR (a
  `MISSION = <id>` contract line or a `mission:<id>` label), never copied from
  protected state, so `protected_mission_authorizes_pr` is a real comparison;
- authority is RECONCILED across all protected sources (project_state, phase_status,
  ew0a_runtime) and must match roadmap_guard, else fail-closed (the assembler puts the
  repo root on sys.path so the guard import works when the gate runs it as a standalone
  script, and emits secret-free `protected.reconciliation` diagnostics naming the failing
  source instead of a bare None);
- the changed-file set is fully PAGINATED and run through the DEFAULT-DENY envelope;
- the mission's change class + envelope come ONLY from the protected base-main
  registry (the candidate cannot widen its own envelope);
- control-plane conformance is computed on the candidate's OWN fetched files by
  base-main code (the candidate never evaluates its own PR);
- `now_main_sha` is a FRESH remote read distinct from the checked-out
  `protected.main_sha`, so `main_not_advanced` can genuinely fail.

The PR controller triggers only on PR-state events + manual recovery dispatch and
polls exact-head `northstar-ci` to its terminal result in-job. The Claude dispatch
workflow verifies a REAL successful `northstar-ci` push run for the current main SHA
before dispatching, so a manual `workflow_dispatch` cannot manufacture certification.

## Activation contract

The deterministic decision layer and gated effect layer are already implemented.
Before switching from `shadow` to `enabled`, the repository configuration must
prove the following:

1. GitHub auto-merge is enabled (`allow_auto_merge=true`).
2. The `main` ruleset requires pull requests and the `Northstar merge gate`,
   blocks force pushes/deletion, and grants no bypass to the Northstar GitHub App.
3. `CLAUDE_CODE_OAUTH_TOKEN` exists as a GitHub Actions secret for Claude Code
   authentication.
4. A dedicated Northstar GitHub App is installed only on the intended repository,
   with repository permissions limited to Contents: read/write, Pull requests:
   read/write, and Metadata: read-only. Its credentials are stored as
   `NORTHSTAR_APP_ID` and `NORTHSTAR_APP_PRIVATE_KEY` Actions secrets.
5. Run the manual `claude-authorized-mission` workflow once while still in
   `shadow`. The `Verify Northstar GitHub App credentials` job must succeed. It
   first uses an installation-wide **read-only** token to require that the App
   installation selects exactly this repository, then separately mints the
   current-repository bounded write token to prove Contents/Pull-request write
   permissions. The preflight performs no repository mutation.
6. Open the governed operator-bootstrap activation PR that flips
   `.agent/mission_registry.yaml` from `shadow` to `enabled` and updates only
   the bootstrap constitutional tests required to represent a legitimate enabled
   state. Merge it only through the explicit operator/admin bootstrap path after
   exact-head validation.

GitHub App installation tokens are short-lived (about one hour) and are revoked by
the token action at job completion. The default `GITHUB_TOKEN` remains read-only in
the App-backed effect jobs. E4 / real-evidence / production / broker / capital / C1
boundaries remain deterministic human-required STOPs; an optional protected GitHub
Environment can be added later as defense in depth when an E4 workflow actually
consumes it.

Once `enabled` is durable, the merge gate may enable native auto-merge on PASS,
the orchestrator may open the deterministic governance-transition PR on PROPOSE,
and the dispatcher may invoke Claude only for already-authorized E1–E3 missions.
