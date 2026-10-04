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
          → Codex review (exact head) ─┤
          → review-thread state ───────┤
                                       ▼
                             NORTHSTAR_MERGE_GATE  (scripts/northstar_pr_gate.py)
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
| `scripts/northstar_pr_gate.py` | `NORTHSTAR_MERGE_GATE` — the single deterministic pre-merge gate. Pure `evaluate_merge_gate(inputs)`. |
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

## NORTHSTAR_MERGE_GATE semantics

`evaluate_merge_gate(inputs)` returns a machine-readable audit with a per-check
breakdown and an overall `PASS`/`FAIL`, failing closed on any missing/ambiguous
input. All of the following must hold:

- PR is open, not draft, base == `main`, mergeable.
- Protected base-main current mission authorizes this PR's mission.
- `northstar-ci` conclusion == success, completed, **bound to the current PR head SHA**.
- Codex review **bound to the current PR head** (see below).
- Zero unresolved **material** (P1/P2) Codex review threads.
- Candidate did not mutate authority beyond the authorized mission.
- Forbidden/protected-path policy satisfied.
- No real-evidence / production / broker / capital / trading / C1 / Phase-0D authority introduced.
- Current remote `main` has not advanced since authorization (no invalidation).

### Codex exact-head binding
Observed `chatgpt-codex-connector[bot]` protocol (PRs #60/#64):
- A **review** is a PR review (`state: COMMENTED`, never `APPROVED`) whose
  `commit_id` is the exact head it reviewed; the body carries
  `**Reviewed commit:** \`<short-sha>\``. Binding uses `commit_id`.
- **Material findings** are inline review comments carrying a `P1`/`P2` severity
  badge; each has its own `commit_id`. These must reach zero-unresolved.
- A **clean** signal is a `+1` issue reaction from the bot, created AFTER the
  exact-head review/request anchor, with no newer material finding at that head.
- A review is REQUESTED at an exact head by commenting `@codex review` (also auto
  on open / ready-for-review).

The gate (`evaluate_codex_binding`) therefore requires: a `+1` reaction bound to
the exact head (via an exact-head review request naming the full SHA, or the
latest head review) created after that anchor, AND zero unresolved P1/P2 threads.
A stale review/reaction against an older head never satisfies the gate; if a new
commit lands, any prior signal is stale and the gate resets; clean is never
inferred from silence.

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
protected state; inability to prove exact-head CI/review; inability to prove
current remote-main identity.

## Controller modes

- `shadow` — compute decisions; emit `MERGE_READY`, `POST_MERGE_CERTIFIED`,
  `NEXT_MISSION_READY`; perform NO merge, NO state mutation, NO dispatch.
- `enabled` — may act only where every deterministic gate passes and registry
  policy allows. Activation is an operator/GPT action; this PR ships `shadow`.

## Bootstrap rule

This implementation PR must not auto-merge itself and must not self-bootstrap
authority. `enabled` capabilities activate only after this PR passes exact-head
CI + clean exact-head Codex review + resolved threads, is manually merged under
existing governance, post-merge main CI succeeds on the exact merge SHA, and
bootstrap/readiness checks prove the controller evaluates the same gates we
enforce manually today.

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
security decision is never made on partial or tautological data:
- the candidate's claimed mission is derived INDEPENDENTLY from the PR (a
  `MISSION = <id>` contract line or a `mission:<id>` label), never copied from
  protected state, so `protected_mission_authorizes_pr` is a real comparison;
- all policy-relevant API queries are fully PAGINATED (a finding or protected-path
  edit on a later page cannot be missed);
- review-thread resolution is read from the GraphQL `reviewThreads.isResolved`
  (fixing a finding and resolving the thread actually clears the gate);
- `now_main_sha` is a FRESH remote read distinct from the checked-out
  `protected.main_sha`, so `main_not_advanced` can genuinely fail;
- the protected-path allowlist applies ONLY to files the PR ADDs; MODIFYING the
  registry/scripts/workflows later is a protected-path violation.

The PR controller re-evaluates on `pull_request_review` /
`pull_request_review_comment` (so the required check updates after Codex responds,
not only before). The Claude dispatch workflow verifies a REAL successful
`northstar-ci` push run for the current main SHA before dispatching, so a manual
`workflow_dispatch` cannot manufacture certification for an uncertified SHA.

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
4. Configure an E4 protected GitHub Environment with required reviewers.
5. Open a governed activation PR flipping `.agent/mission_registry.yaml`
   `controller.mode` `shadow → enabled`; certify + manually merge it.

Once `enabled` is durable, the already-implemented effectors run with no further
development PR: the merge gate enables native auto-merge on PASS, the orchestrator
materializes the transition and opens the governance PR on PROPOSE, and the
dispatcher invokes Claude on DISPATCH.
