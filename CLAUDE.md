# Claude Code Instructions

This repo is an advisory-only portfolio automation system. It produces analysis, recommendations, and operator artifacts; it does not execute trades.

## Read First
- `docs/ARCHITECTURE.md`
- `docs/decision_engine.md`
- `docs/gui_decision_center.md`
- `docs/daily_memo.md`

## Hard Boundaries
- `outputs/latest/decision_plan.json` is the decision source of truth.
- GUI, memo, and explanation layers are artifact consumers only.
- Do not recompute decisions outside core decision layers.
- Do not introduce broker integration, execution logic, or auto-trading behavior.
- Keep all new features additive and backward compatible.

## Protected Semantics
- Do not change `signal_score`, `confidence_score`, `effective_score`, `conviction_score`, `final_rank_score`, or `recommendation_score` semantics without explicit user approval.
- Do not modify `decision_engine.py`, scoring logic, or recommendation logic unless the user explicitly approves that scope.
- Do not bypass FMP registry/compliance rules for endpoint work.

## Working Style
- Trace the exact source-to-artifact path before editing.
- Name exact files and functions before changing behavior.
- Prefer the smallest patch that preserves explainability and contracts.
- Lower certainty when data is stale or degraded; do not invent conviction.

## Output Contracts
- Decision artifacts: `outputs/latest/decision_plan.json`, `outputs/latest/decision_plan.md`
- Memo: compact brief only
  - max 5 decisions
  - max 3 risk items
  - max 3 changes
- GUI Decision Center: same compact contract, full detail below

## Validation
- Targeted tests first, then broader suites when scope expands.
- Compile touched Python files:
  - `python -m py_compile <files>`
- Run repo tests:
  - `pytest -q`
- For production-run changes, respect:
  - `bash scripts/preflight.sh`
  - `bash scripts/run_daily_safe.sh`

## Reference Docs
- `docs/OUTPUT_ARTIFACT_CONTRACTS.md`
- `docs/PIPELINE_RUNBOOK.md`
- `docs/REGRESSION_CHECKLIST.md`
- `docs/CLAUDE_AGENT_RULES.md`

---

## Agent Orchestration Context

This repo uses a repo-native orchestration layer in `.agent/`. Read these before starting any feature:

```bash
python scripts/agent_context_check.py   # prints current phase, step, next steps
cat .agent/project_state.yaml           # full machine-readable project state
cat .agent/phase_status.yaml            # per-step roadmap status
```

## Roadmap Discipline

- Implement only the step explicitly requested by the user.
- Do not recommend Discovery Engine as the next step if a named roadmap step (e.g., Confidence Calibration, GUI panels) is still pending.
- The authoritative next step is `next_official_step` in `.agent/project_state.yaml`.
- If you are unsure whether a step is in scope, ask before implementing.

## Observe-Only Default

- All new observability layers must set `observe_only: true` as a hardcoded field in output artifacts.
- Do not remove or make `observe_only` conditional unless explicitly approved.
- Non-blocking pipeline integration: wrap all new calls in `try/except`.

## Output Namespace Rules

- Use `OutputNamespace` from `portfolio_automation/data_governance.py` for all file writes.
- Live pipeline artifacts → `OutputNamespace.LATEST` (`outputs/latest/`)
- Budget/governance/audit artifacts → `OutputNamespace.POLICY` (`outputs/policy/`)
- Replay artifacts → `OutputNamespace.HISTORICAL` (`outputs/backtest/`) — never from live pipeline
- Never write to namespaces outside the module's declared purpose.

## Operating Mode

Claude Code runs in two environments. Behavior differs by environment.

### Operator laptop (Windows, C:\PersonalWork\v1)
Primary dev environment. Full write access. Validation commands that need
the production VPS are returned as a copyable block for the operator to
run manually on the VPS — do NOT claim VPS test results from the laptop.

### Production VPS (Linux, /opt/stockbot)
Claude Code runs here too. Switchable between two modes by swapping
`.claude/settings.json`:

- **dev_on_vps** (current default): full edit / commit / push access.
  Used while hardening the system toward production-grade. Claude can
  edit code, run pytest, push to main, and validate directly on the
  production filesystem. The VPS is treated as a second dev environment,
  not yet as untouchable production.
- **read_only_ops** (target end state): can read artifacts, run pytest,
  run validation scripts; cannot edit code, cannot mutate
  `outputs/latest/`, cannot push to git. Switch into this mode once the
  advisory layers are confirmed stable and the cron pipeline is treated
  as ground truth.

To switch modes on the VPS, tell Claude (or any operator) to apply the
corresponding mode block from `docs/CLAUDE_VPS_MODES.md` into
`.claude/settings.json`:

- "Apply dev_on_vps mode" → copy the dev block into `.claude/settings.json`
- "Apply read_only_ops mode" → copy the read-only block into `.claude/settings.json`

Restart the Claude Code session afterward so the new permissions take
effect. See `docs/CLAUDE_VPS_MODES.md` for both full JSON blocks and the
rationale for every allow/deny pattern.

### Validation reporting rules

- When Claude runs on the VPS, validation commands are executed there;
  the reported test results are real.
- When Claude runs on the laptop, validation commands are returned as a
  copyable block for the operator to run on the VPS manually. Do NOT
  claim those tests passed.
- Use `.agent/task_templates/vps_validation_prompt.md` as the template
  when returning manual VPS commands.

## Test Requirements

- Add tests for every new module in `tests/`.
- Run targeted tests before the full suite.
- Full suite (now passing — 2026-05-28 datetime tz fix in gui_operator_data.py):
  ```
  python -m pytest -q
  ```

## Analysis + Health Coverage Requirement

Any new function, producer, or feature shipped to this repo MUST be
paired with an analysis-and-health check. The check goes EITHER into
an existing agent/skill OR into a new one — never into nothing. Without
this pairing the feature is considered incomplete and should not be
merged.

**Match the cadence to the feature's runtime cadence:**

| Feature cadence | Owning skill | When to extend |
|---|---|---|
| Runs daily or sub-daily | `.claude/commands/daily-tool-analysis.md` | Add to Step 1 artifacts read + Step 3 dispatch logic + Step 4 body grammar. Add a content_liveness check if the producer can emit "looks-fresh-but-empty" failures. |
| Runs weekly/monthly | `.claude/commands/monthly-tool-analysis.md` | Add to the monthly review's trend section + dispatch logic. |
| Runs quarterly/yearly OR is lifetime-only | `.claude/commands/yearly-tool-analysis.md` | Add to the yearly retrospective + dispatch logic. |

**Choose the lens(es) the new check should embody:**

The system is now analyzed from four expert lenses, each implemented by
one or more agents. When adding a check, pick the lens that fits and
extend the corresponding agent (or create a new one in the same lens):

- **Developer lens** — cron health, error rates, test coverage, dependency drift, silent zeros. Existing agents: `portfolio-resolver-investigator`, `portfolio-test-reviewer`, `portfolio-render-reviewer`, `portfolio-discovery-health`.
- **Quant lens** — hit-rate, Sharpe, regime performance, pattern efficacy, gauge attribution. Existing agents: `portfolio-attribution-analyst`, `portfolio-learning-loop-health`.
- **Process analyst lens** — workflow health, audit log activity, drift cap utilization, operator decision queue. Existing agents: `portfolio-learning-loop-health` (overlaps quant lens).
- **Market expert lens** — sector rotation, regime calls, memo accuracy vs reality, decision-vs-outcome alignment. Existing agents: `portfolio-memo-reviewer`, `portfolio-attribution-analyst`.

**Workflow when shipping a new feature:**

1. Identify the feature's cadence → pick the owning skill.
2. Pick the lens(es) the new check belongs to → identify the existing
   agent OR draft a new agent under the right lens.
3. Add: artifacts-read entry, computed signal(s), dispatch trigger,
   body-grammar line, content_liveness check (if applicable),
   RED-template line (if applicable).
4. Add a test that asserts the new check produces the expected status
   under both healthy and degraded fixture states.
5. Confirm the full suite passes (`python -m pytest -q`).

**The corollary:** every artifact under `outputs/latest/*.json` should
be consumed by AT LEAST ONE check at the appropriate cadence. If no
check reads it, either add one or delete the artifact. Producers
without consumers are debt.

## Agent + Skill Loading Behavior

Repo-local agents live in `.claude/agents/*.md` and skills in
`.claude/skills/<name>/SKILL.md`. They are loaded asymmetrically:

- **Skills live-reload mid-session.** A new or edited skill is available
  via the `Skill` tool within seconds of being written to disk.
- **Agents are snapshotted at session start.** A new agent file written
  during the session will NOT appear in the Agent dispatcher until the
  next session. Refreshing an existing agent's body works fine because
  the dispatcher routes by name, but the *list of available agent names*
  is fixed for the life of the session.

When you ship a new agent, write its file, commit/push it, and then
either:
1. Restart the session before claiming the agent is "usable", OR
2. Tell the user explicitly that the new agent is committed but needs a
   session restart to dispatch.

Existing agents whose markdown body was edited can be smoke-tested
immediately (the dispatcher picks up the refreshed body on the next call).

This was learned the hard way on 2026-05-20 when three new agents
(portfolio-resolver-investigator, portfolio-attribution-analyst,
portfolio-render-reviewer) were created and could not be dispatched in
the same session.

## Final Report Format

End every implementation task with this report:

```
## Final Report

Files created: [list]
Files modified: [list]
Behavior implemented: [description]
Artifacts written: [paths + namespaces]
Tests added: [file + count]
Test commands run: [commands]
Test results: [pass/fail summary]
Assumptions: [list]
Risks: [list or none]
VPS validation commands: [copyable block]
Recommended next step: [from .agent/project_state.yaml:next_official_step]
```
