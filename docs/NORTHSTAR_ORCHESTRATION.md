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
| `.github/workflows/northstar-orchestrator.yml` | Read-only post-merge certify/propose job **+ a separate `governance-pr-effect` write job** that materializes the transition and opens ONE governance PR, unreachable in shadow. |
| `.github/workflows/claude-authorized-mission.yml` | Read-only `build-packet` job **+ a separate `claude-dispatch` write job** that invokes the pinned `anthropics/claude-code-action` via Anthropic WIF, unreachable in shadow. |
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
| `post-merge-certify` (PROPOSE) | `governance-pr-effect` (`contents:write`,`pull-requests:write`) | materialize + push governance branch + open ONE PR (never main) |
| `build-packet` (DISPATCH) | `claude-dispatch` (`contents:write`,`pull-requests:write`,`id-token:write`) | invoke pinned Claude action via WIF, tool-constrained, open PR only |

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
`pull_request_target`, uses a non-SHA-pinned action, ships the registry in a non-shadow
mode, grants write-all, etc.). After merge + exact-SHA post-merge certification,
controller N+1 becomes the durable evaluator for subsequent PRs. No human, no Codex.

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
- Prefer OIDC/WIF over a long-lived API secret for Claude dispatch.
- Fail closed on API/query ambiguity.

## Input-assembly hardening (fail-closed on partial data)

`scripts/northstar_assemble_gate_inputs.py` feeds the gate and is hardened so a
security decision is never made on partial or tautological data (and never fetches,
polls, or waits on Codex):
- the candidate's claimed mission is derived INDEPENDENTLY from the PR (a
  `MISSION = <id>` contract line or a `mission:<id>` label), never copied from
  protected state, so `protected_mission_authorizes_pr` is a real comparison;
- authority is RECONCILED across all protected sources (project_state, phase_status,
  ew0a_runtime) and must match roadmap_guard, else fail-closed;
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

## Activation contract (configuration-only — no further code PR)

Both the deterministic decision layer AND the gated effect layer ship in this PR.
After it is durable on `main` and post-merge-certified, activation is
**repository-administration / configuration work only** — it adds no missing
controller behavior:

1. Enable GitHub auto-merge (`allow_auto_merge=true`).
2. Add a `main` ruleset: require the `Northstar merge gate` check (pin its
   source/app where supported), require PRs (no direct push), force-push protection.
3. Configure Anthropic WIF repository variables: `ANTHROPIC_FEDERATION_RULE_ID`,
   `ANTHROPIC_ORGANIZATION_ID`, `ANTHROPIC_SERVICE_ACCOUNT_ID`.
4. Configure a `NORTHSTAR_BOT_TOKEN` secret (a GitHub App installation token or a
   fine-scoped PAT) used by the governance-PR and Claude-dispatch effect jobs — a PR
   created with the default `GITHUB_TOKEN` does not trigger downstream CI, which would
   stall the handoff. (In shadow, no PR is created, so the fallback to `github.token`
   is inert.)
5. Configure an E4 protected GitHub Environment with required reviewers.
6. Open a governed activation PR flipping `.agent/mission_registry.yaml`
   `controller.mode` `shadow → enabled`; certify + manually merge it.

Once `enabled` is durable, the already-implemented effectors run with no further
development PR: the merge gate enables native auto-merge on PASS, the orchestrator
materializes the transition and opens the governance PR on PROPOSE, and the
dispatcher invokes Claude on DISPATCH.
