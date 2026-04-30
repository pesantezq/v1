# Codex Skill: portfolio-review

## Purpose

Code review, regression review, and artifact contract audit for the Portfolio Automation System.
Codex reviews the diff after Claude implements a feature.

## When to Use

- After Claude completes a new module and commits
- When a new namespace is used for the first time
- When output artifact schemas might have changed
- When a new dependency was added
- When main.py was modified
- When test coverage is uncertain

## When NOT to Use

- For trivial docs-only changes
- For test-only changes that don't touch production code
- For YAML-only changes (use agent_context_check.py instead)
- When the user explicitly says review is not needed

## Instructions

1. **Read `AGENTS.md`** — understand the review role and forbidden changes.
2. **Read `.agent/project_state.yaml`** — get forbidden_changes and current step.
3. **Use `.agent/task_templates/codex_review_prompt.md`** — work through every checklist item.
4. Inspect the diff or changed files provided by the user.
5. Flag any issue with file name, function, and brief description.
6. Do not implement fixes — recommend them.

### Review Areas

- Forbidden change check (scoring, allocation, recommendation, observe_only, FMP)
- Output schema compatibility (no removals, types preserved, observe_only: true)
- Namespace violations (replay in latest, live in backtest, raw open() without governance)
- Hidden behavior changes (env vars, conditionals, non-blocking guard, dry_run guard)
- Dependency impact (requirements.txt, VPS install note)
- Test coverage (happy path, edge cases, namespace assertions, artifact field assertions)
- Security risks (injection, credentials, raw path construction)
- Roadmap drift (feature matches current step, no premature Discovery Engine)

## Final Output Format

```
## Codex Review Response

Step reviewed: [step name]
Files inspected: [list]

Forbidden change check: [pass | issues — describe]
Output schema compatibility: [pass | issues — describe]
Namespace violations: [pass | violations — describe]
Hidden behavior changes: [pass | issues — describe]
Dependency impact: [none | new packages, VPS install note]
Test coverage: [adequate | gaps — list]
Security risks: [none | issues — describe]
Roadmap drift: [pass | drift — describe]

Overall: [clean | issues found]
Blocking issues: [list or none]
Recommended fixes: [list or none]
Recommended next step: [from project_state.yaml:next_official_step only]
```
