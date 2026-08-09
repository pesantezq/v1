# StockBot Northstar Redesign — Program Charter

Status: **ACTIVE** (program `stockbot_northstar_redesign`, opened 2026-08-09)
Authoritative machine-readable state: `.agent/project_state.yaml` (program /
phase / step) + `.agent/phase_status.yaml` (per-phase status) +
`config/agent_policy.yaml` (agent authority).
This document explains that state; where prose and machine-readable state
disagree, the machine-readable state wins.

---

## 1. Governing North Star (operator-approved)

> StockBot continually seeks to improve its ability to estimate investment
> outcomes and retains improvements only when they demonstrate incremental
> predictive value. Predictions are never portfolio actions. An independently
> governed capital-allocation and risk-management system determines whether,
> when, and how demonstrated investment insights deserve capital. Prediction
> quality, allocation quality, exit quality, and end-to-end portfolio
> performance are evaluated separately through controlled attribution using
> point-in-time backtests, frozen live-shadow evidence, resolved outcomes,
> counterfactual simulations, and realized portfolio results. Improvements
> earn, retain, or lose influence according to evidence. Proven research
> capabilities may be reused to create watchlists, ETF and basket strategies,
> investor intelligence, software, data products, and other intellectual
> property, provided commercialization remains downstream of the research
> process and cannot weaken StockBot's evidence standards, governance, risk
> controls, or production boundaries.

This goal supersedes `observe_and_iterate` as the **sole** top-level roadmap
objective. It does not erase it: the observe-and-iterate phase (2026-05-20 →
2026-08-09) produced the outcome history, governance layers, and health
tooling this program builds on, and **production observation/outcome
collection continues** as a parallel workstream (§8).

## 2. Roadmap hierarchy

```
Strategic Goal   the North Star statement above
  → Program      stockbot_northstar_redesign
    → Phase      northstar_phase_0a … northstar_phase_11
      → Milestone  named, gated steps inside a phase
        → Deliverable  concrete artifacts (docs, contracts, code, tests)
```

Status vocabulary (extends the repo's existing lowercase enum — `complete`,
`active`, `deferred`, `superseded` — do not invent competing vocabularies):

`complete` · `active` · `ready` · `blocked` · `waiting_for_evidence` ·
`deferred` · `not_started` · `superseded`

## 3. Phases

Statuses below mirror `.agent/phase_status.yaml:stockbot_northstar_redesign`.
**No future phase is implemented; nothing below Phase 0A milestone 1 has
started.**

### Foundation

| Phase | Name | Status | Objective | Depends on | Unlocks | Exit gate |
|---|---|---|---|---|---|---|
| 0A | Architecture, Authority & CI Foundation | **active** | Make the North Star authoritative; one coherent authority model; CI that proves invariants on every change | topology stabilization (done 2026-08-09) | every later phase | authority reconciled (milestone 1, **complete**) AND CI foundation live (milestone 2, **ready**, next) |
| 0B | Canonical Evidence, Prediction & Worker Contracts | not_started | Define the canonical PredictionRecord / evidence / AI-worker contracts (schemas, not runtimes) | 0A | 0C, 0D, worker admission later | contracts reviewed + versioned + test-covered |
| 0C | Point-in-Time EvidenceGateway & Research Store | not_started | PIT, identity-bound, provenance-aware evidence access (generalizing Intraday Lab's preregistration/identity-era patterns) | 0B | all certification and engines | lookahead-audited PIT reads over the research store |
| 0D | Certification, Champion/Challenger & Incremental-Value Foundation | not_started | The mechanism that decides whether anything (predictor, allocator, exit method, AI worker, strategy) demonstrates incremental value | 0B, 0C | Phases 1–8 admission | reproducible certification verdicts with controlled attribution |

### Core intelligence

| Phase | Name | Status | Objective | Depends on | Unlocks | Exit gate |
|---|---|---|---|---|---|---|
| 1 | Prediction Engine v2 | not_started | Estimates future investment outcomes; never allocates capital | 0C, 0D | 2, 3, 4 | shadow predictions certified for incremental predictive value |
| 2 | Capital & Risk Engine v2 | not_started | Independently governed decision of whether/when/how demonstrated insights deserve capital; must not modify predictions | 0D, 1 (shadow) | 3, 4, 10 | shadow allocations certified separately from prediction quality |
| 3 | Exit & Replacement Engine v2 | not_started | Continuation / trim / exit / replacement evaluated independently | 0D, 1–2 shadow | 4, 10 | exit quality attributed separately |
| 4 | Evidence-Weighted Influence Engine | not_started | Governed mechanism by which improvements earn, retain, lose, or are denied influence. **Not implemented in this session or any 0x phase.** | 0D, 1–3 | 10, 11 | influence changes are evidence-derived, bounded, reversible, audited |

### AI research plane

| Phase | Name | Status | Objective | Depends on | Unlocks | Exit gate |
|---|---|---|---|---|---|---|
| 5 | AI Worker Runtime Foundation | not_started | Runtime plumbing for AI workers under `config/agent_policy.yaml` authority (budgets, adapters, sandboxing) | 0B (worker contracts) | 6–8 | workers run only inside declared authority |
| 6 | Prime + Local Worker | not_started | Research orchestrator + cheap local workers | 5, 0D | 7, 8 | Prime output routed through StratLab for quantitative claims |
| 7 | TradingAgents Certification & Shadow Integration | not_started | Broad research worker admitted research-only, then certified | 5, 6, 0D | richer hypothesis flow | certification evidence of incremental value |
| 8 | FinRobot / Additional Specialists | not_started (**conditional**) | Deep fundamentals/valuation specialists — only on demonstrated need | 7 evidence | deeper fundamental coverage | demonstrated need + certification |

**Contract-first nuance:** AI **worker contracts** are authored in the early
contract phases (0B), and worker **authority** is already modeled now in
`config/agent_policy.yaml` (roles carry `runtime_status:
defined_not_integrated`). Actual Prime/TradingAgents **runtime integration**
happens in Phases 5–7, after the evidence/certification foundations exist.

### Downstream reuse

| Phase | Name | Status | Objective | Depends on | Unlocks | Exit gate |
|---|---|---|---|---|---|---|
| 9 | Strategy & Product Factory | not_started | Watchlists, ETF/basket strategies, investor intelligence, software/data products from validated research | 0D + certified upstream | commercialization | products consume certified research; cannot alter evidence, passports, confidence, or promotion standards |
| 10 | End-to-End Shadow Certification | not_started | Whole-chain (predict → allocate → exit) certified in shadow with controlled attribution | 1–4 | 11 | end-to-end shadow evidence meets gates |
| 11 | Human-Gated Production Advisory Migration | not_started | Migrate production advisory to the certified chain, human-gated, reversible | 10 | new production baseline | human approval; old chain retained as fallback |

## 4. Critical path

```
Northstar authority (0A m1, complete)
  → CI foundation (0A m2, next)
  → canonical contracts (0B)
  → PIT EvidenceGateway (0C)
  → certification / Champion-Challenger (0D)
  → Prediction + Capital + Exit shadow engines (1–3)
  → Influence Engine (4)
  → AI worker admission/integration (5–8; contracts drafted back in 0B)
  → downstream Product Factory (9)
  → end-to-end shadow certification (10)
  → human-gated production-advisory migration (11)
```

## 5. Target architectural separation (planes)

These are **design boundaries made authoritative now**; the runtimes arrive in
their named phases. Today's production system remains the incumbent
(docs/ARCHITECTURE.md) and is untouched by this phase.

| Plane | Responsibility | Hard boundary |
|---|---|---|
| **Evidence Plane** | Point-in-time, identity-bound, provenance-aware information (0C generalizes Intraday Lab's preregistration / identity-era / frozen-evidence patterns) | no lookahead; provenance mandatory; evidence is immutable once frozen |
| **Prediction Engine** | Estimates future investment outcomes | **does not allocate capital**; predictions are never portfolio actions |
| **Capital & Risk Engine** | Whether / when / how demonstrated insights deserve capital; independently governed | **must not modify predictions** |
| **Exit & Replacement Engine** | Continuation, trimming, exit, replacement — evaluated independently | attributed separately from entry quality |
| **StratLab / Certification Plane** | Decides whether predictors, allocators, exit methods, AI workers, and strategies demonstrate **incremental** value | the only path by which quantitative claims become certified evidence |
| **Influence Engine** (future, Phase 4) | Governed mechanism by which demonstrated improvements earn / retain / lose / are denied influence | evidence-derived only; bounded; reversible; audited. **Not implemented.** |
| **AI Worker Plane** | Research, investigate, extract, challenge, explain, synthesize, propose experiments — runs in the **home Agent Lab** on frozen/sanitized/hash-verified production exports (the VPS stays the cheap production/control plane) | **no direct capital authority; no production approval authority; no broad `/opt/stockbot` access** (`config/agent_policy.yaml`) |
| **Product Factory** (future, Phase 9) | Downstream consumer of validated research | commercial attractiveness cannot change research evidence, Strategy Passports, confidence, or promotion standards |
| **Human / Production Governance** | Final production-advisory promotion boundary | unchanged; **no broker/trade execution is introduced anywhere in this program** |

## 6. AI worker role classes

Defined normatively in `config/agent_policy.yaml` (schema `policy_version
1.0.0`; validated by `portfolio_automation/agent_policy.py` +
`tests/test_agent_policy.py`). Summary:

| Role | Environment | May | May not | Runtime today |
|---|---|---|---|---|
| **Prime** | home Agent Lab | classify research tasks, build investigation plans, delegate, reconcile evidence, identify disagreements, route quantitative claims to StratLab, synthesize supported findings, abstain | allocate capital authoritatively; approve production; bypass StratLab where quantitative certification is required | defined, not integrated |
| **TradingAgents** | home Agent Lab | market/company hypothesis + research | anything beyond research until separately certified | defined, not integrated |
| **FinRobot** | home Agent Lab | deep fundamentals/valuation (conditional specialist) | be treated as required infrastructure | defined, not integrated, conditional |
| **Local/cheap LLM workers** | home Agent Lab | extraction, classification, summarization, tagging | exceed capability-specific authority | defined, not integrated |
| **Evidence Auditor** | home Agent Lab | claim/source/timing/entity verification | alter the evidence it audits | defined, not integrated |
| **Quant Router** | home Agent Lab | recognize a claim needs quantitative validation; construct/submit the validation request; attach evidence/context | certify quantitative truth; issue incremental-value verdicts; allocate capital; approve production | defined, not integrated |
| **StratLab / Certification Plane** | VPS + lab | deterministic/reproducible quantitative evaluation; controlled-experiment evaluation; issue certification evidence; Champion/Challenger adjudication under the future 0D framework | allocate capital; modify predictions; approve production; cause real portfolio actions | **plane, not an AI worker**; defined, not integrated (existing Strategy Lab / walk-forward subsystem is reused foundation, not the 0D certification system) |
| **Memo/Product workers** | home Agent Lab | communicate validated research | alter underlying authoritative results | defined, not integrated (the production memo/digest layer is a deterministic reusable subsystem, not this AI worker) |
| **Claude Code Builder** | laptop + VPS | authorized engineering tasks | real portfolio-action authority of any kind | active (this agent) |
| **Claude Code Reviewer(s)** | laptop + VPS | independent architecture/test/governance/quant/scope review | merge or promotion authority | active (`.claude/agents/`) |
| **Human/operator** | all | scope, production promotion, final authority | — | active; **the only role with production-promotion or real portfolio-action authority** |

### The Agent Lab execution boundary

```text
Production StockBot / VPS  (cheap production/control plane)
        |
        | sanitized, frozen, hash-verified export
        | (Agent Export lane — docs/STOCKBOT_AGENT_EXPORT.md,
        |  preserved unmerged on feature/agent-production-export @ 66ecc281)
        v
Home Agent Lab  (heavy AI/research workloads)
        +-- Prime
        +-- local LLM workers
        +-- TradingAgents
        +-- FinRobot (when justified)
        +-- evidence/research workers (Evidence Auditor, Quant Router, memo/product)
```

Research workers analyze the **snapshot, not the server**: they consume
frozen/sanitized production evidence and never receive broad `/opt/stockbot`
access. `resolve_authority(<research worker>, "vps_dev_on_vps")` fails closed
(`permitted_in_environment: false`) — a session running on the VPS does not
make a research worker permitted there. The home Agent Lab is never a
production authority environment.

### Capital authority — advisory determination vs real portfolio action

```text
Capital & Risk Engine:
  future authoritative advisory allocator after certification
  (Phase 2 + 0D; owns whether/when/how demonstrated insights deserve
   capital, subject to deterministic risk and governance constraints;
   NOT real-world execution authority; NOT an AI worker; must not
   modify predictions; not implemented in Phase 0A)

Human/operator:
  production promotion + real portfolio-action authority
  (whether an advisory proposal becomes a real portfolio action;
   any future real capital action; kill-switch/final governance)

AI research workers:
  neither
```

The policy field `real_portfolio_action_authority` encodes the second concept
only — it is deliberately narrower than "who owns allocation logic", so it
cannot be misread as forbidding the future certified Capital & Risk Engine
from owning the authoritative *advisory* allocation determination
(`global_invariants.capital_authority_model`). Predictions are never
portfolio actions; StockBot remains advisory-only; no broker execution
exists.

**Execution environments and governance authority are independent concepts**
in the policy, resolved by two mechanically distinct resolvers:

- **Execution environments** (`environments:` — `operator_laptop`,
  `vps_dev_on_vps`, `vps_read_only_ops`, `home_agent_lab`) answer *"what can
  this agent/tooling session do from here?"*: code writes, git writes,
  production filesystem/service mutation (`production_mutation_allowed` —
  agent/tool execution capability, **not** human production-approval
  authority), validation-claim type, worker placement. `dev_on_vps` vs
  `read_only_ops` is a Claude execution-permission distinction
  (`docs/CLAUDE_VPS_MODES.md`). Resolved by `resolve_authority(role, environment)`.
- **Operational authority domains** (`operational_authority_domains:`) answer
  *"in what operational domain can this role exercise governance authority?"*:
  - `production_control_plane` — where production-advisory promotion
    (`promotion_approvals.record_approval`, `docs/SIM_GOVERNANCE.md`), real
    portfolio-action final authority, and kill-switch/final governance take
    effect. Membership is restricted to `human_operator`
    (validator-enforced).
  - `research_plane` — the home Agent Lab's operational domain; permanently
    non-production (validator-enforced: it can never confer promotion or
    action authority on any role).

  Resolved by `resolve_operational_authority(role, domain)`: authority = role
  grant AND domain membership AND domain capability — absence of any of the
  three is denial, fail closed.

**Consequence:** switching Claude's VPS session from `dev_on_vps` to
`read_only_ops` changes Claude's execution permissions only — it cannot
revoke or alter the human operator's production-control authority, which
resolves through `production_control_plane` regardless of Claude's mode. Even
`human_operator`, who holds the global action grant, gets no promotion or
action authority through `research_plane`. The human's narrow responsibility
term is `real_portfolio_action_final_authority` (the former
`capital_and_risk_final_authority` wording was removed as ambiguous).

## 6b. CI foundation (Phase 0A milestone 2)

Status: **implemented on `feature/northstar-0a-ci`; remote_run_pending** —
Phase 0A stays `active` until the remote GitHub Actions run is inspected
green and GPT closes the phase. Do not treat the existence of the YAML as an
operational CI claim.

Workflow: `.github/workflows/northstar-ci.yml` (`northstar-ci`).

- **When it runs:** `push` to `main` and `feature/northstar-**`,
  `pull_request` targeting `main`, and manual `workflow_dispatch`. Superseded
  runs for the same non-main ref are cancelled. No scheduled runs.
- **Runner/deps:** GitHub-hosted `ubuntu-latest`, Python 3.12, plain
  `pip install -r requirements.txt pytest` (pytest is a test-only addition;
  no uv/poetry). `permissions: contents: read` — CI has **no write path**.
- **Job `northstar-governance` (required, hermetic):** compile of the policy
  and context modules → `scripts/agent_context_check.py` →
  `tests/test_agent_policy.py` (authority model: AI roles can never gain
  promotion/action authority, Agent Lab + read-only + research-plane
  invariants, execution/governance decoupling, capital-authority model,
  Quant Router vs StratLab, runtime-status accuracy) →
  `tests/test_northstar_authority.py` + `tests/test_agent_context_check.py`
  (program/phase/step correctness, future phases not falsely complete,
  observe-and-iterate history preserved) → `tests/test_doc_audit.py` →
  `tests/test_operator_control.py`.
- **Job `tests` (broad hermetic regression):** the repository's declared
  full-suite command (`.agent/project_state.yaml:required_test_policy`) with
  its two pre-existing declared exclusions (`tests/test_gui_api_health.py`,
  `tests/test_gui_insight_cards.py`) plus 10 deselected test IDs in two
  documented non-hermetic classes (all keep running in the VPS-side full
  suite):
  1. **Live-runtime-artifact validation (5)** — the artifact-registry corpus
     check, 3 GUI routes rendered against live `outputs/latest` artifacts,
     the live sandbox active-strategy anchor. Evidence: the full 10,252-test
     suite run in a bare worktree passed 10,246 with exactly these 5
     failing; all 5 then passed against the live VPS corpus.
  2. **Production-host assumptions (5)** — exposed by remote run
     31336323571: `test_broker_overlay` (4) hardcodes the absolute path
     `/opt/stockbot/config.json`; one operator-worker test requires a
     `claude` binary on PATH. Hermetic rewrites are a follow-up
     qualification, not a CI weakening.

  The job also sets a standard CI git identity (hosted runners have none;
  `test_worker_workspace`'s clone-isolation test makes real commits).
  `tests/conftest.py` guards the protected scoring registry during the run.
- **Deliberately NOT in CI:** the production pipeline, anything requiring
  `.env`/credentials/FMP/broker/network, live `outputs/` artifacts,
  production databases, cron/systemd behavior, VPS SSH, Agent Export against
  production. Those validations require **real VPS evidence** and remain
  operator/VPS-side (`scripts/preflight.sh`, `run_daily_safe.sh`, the
  daily/weekly analysis skills).

## 7. Reused foundations (preserve, do not rebuild)

The redesign builds ON these existing systems. None of them is being replaced
by this phase; engines that eventually supersede an incumbent do so only
through the certification path (0D → 10 → 11).

- **Governance/evidence:** `portfolio_automation/data_governance.py`
  (OutputNamespace), run-mode governance, `artifact_registry.yaml` +
  validator, historical replay, outcome tracking
  (`decision_outcome_tracker.py`, `outputs/policy/*.jsonl` append-only event
  stores), simulation governance (`sim_governance/`, two-lane, human-gated).
- **Research/quant:** Strategy Lab, `portfolio_sim/` (backtests, shadow
  portfolios, projections), walk-forward evaluation, factor attribution,
  Strategy Catalog discipline, **Intraday Lab's immutable
  evidence/preregistration/identity-era patterns** (the template for the
  Evidence Plane).
- **Portfolio management (protected incumbent):** the current Decision Engine
  (`decision_engine.py` — protected semantics, unchanged), allocation/risk/
  exit advisors (`exit_advisor.py`, `risk_delta_advisor.py`,
  `correlation_risk_advisor.py`, kelly/scenario advisors), shadow portfolios,
  opportunity radar (`opportunity_scoring.py`).
- **AI/agent infrastructure:** `agent/llm_adapters.py`,
  `portfolio_automation/ai_budget.py`, `.agent/` orchestration state,
  `.claude/agents/` + `.claude/commands/`, operator-control/work-order
  infrastructure (`operator_control/`, worker readiness).
- **Adjacent, preserved but NOT integrated:** Agent Export
  (`feature/agent-production-export`, commit `66ecc281`) — a frozen
  production-snapshot export lane, preserved on its own branch 2026-08-09.
  **Not merged, not production-integrated**; its review/integration decision
  is an independent track (§8).

## 8. Parallel workstreams (non-blocking, non-redefining)

These continue without blocking Northstar foundation work — but none of them
may silently redefine Northstar contracts; contract changes route through this
program's phases:

1. **Intraday Lab** — proceeds through its own existing gates (PR #10 /
   Session 3.x lineage).
2. **Production observation / outcome collection** — the daily pipeline keeps
   accumulating resolved-outcome history (the observe-and-iterate mission
   continues here).
3. **Agent Export review/integration decision** — independent; branch stays
   preserved until explicitly reviewed.
4. **Engineering hardening** — where explicitly authorized by the operator.

## 9. Authority model & VPS-mode status (reconciled 2026-08-09)

**One clear current rule:** Claude Code runs in TWO environments — the
operator laptop and the production VPS — and the VPS session's authority is
whatever `.claude/settings.json` declares per `docs/CLAUDE_VPS_MODES.md`
(`dev_on_vps` while hardening, `read_only_ops` as end state).
`config/agent_policy.yaml` is the machine-readable role/environment authority
model; prose documents explain it. Older statements that "Claude does not run
on the VPS / VPS validation is manual" describe the pre-2026-05 operating
model and are **superseded** (kept in their documents as marked historical
context — the transition itself is meaningful history).

**Accurately documented live discrepancy (NOT changed by this session):** as
of the 2026-08-09 inspection the VPS has **no** `.claude/settings.json` — only
a stale `.claude/settings.local.json` — so neither documented mode is formally
declared and the effective behavior is default permission-prompting (de-facto
dev_on_vps). The expected declaration is the `dev_on_vps` JSON block from
`docs/CLAUDE_VPS_MODES.md` written to `/opt/stockbot/.claude/settings.json`.
That file is intentionally untracked/runtime-local (per that doc), so
**activation is a separate, explicit operational step by the operator** — this
feature branch does not and cannot change live VPS permissions.

## 10. Out of scope for Phase 0A milestone 1 (this session)

Not implemented here (deliberately): GitHub Actions/CI (milestone 2, next),
PredictionRecord code, EvidenceGateway, DuckDB, SEC integration, Strategy
Passport runtime, Champion/Challenger runtime, Prediction/Capital/Exit Engine
v2, Influence Engine, Prime/TradingAgents/FinRobot runtimes, LLM runtime
changes, Agent Export merge/integration, UI changes, production scheduling
changes, Streamlit retirement cleanup, swap changes, branch/worktree/stash
cleanup, broker execution, auto-trading, score-semantic changes, Decision
Engine changes, allocation changes, production configuration changes.

## 11. Operational follow-ups (known, deferred — not this program's phase 0A)

- Draft PR #10 remains open/unmerged (Intraday gates decide).
- `feature/agent-production-export` remains separately preserved, unmerged.
- No CI exists yet (Phase 0A milestone 2 — the next authorized step).
- Generated tracked runtime artifacts may remain dirty in `/opt/stockbot`.
- Dual cron/systemd daily scheduling remains unresolved.
- Streamlit service remains active despite retirement docs.
- The VPS has no swap.
- Old local branches/worktrees/stashes remain (deliberately untouched).
- Live `.claude/settings.json` mode declaration is an explicit operator step (§9).
