# Codex Skill: portfolio-docs

## Purpose

Update documentation after Claude builds a feature. Codex handles the docs layer
so Claude can stay focused on implementation.

## When to Use

- After Claude returns a final report for a completed step
- When `docs/<MODULE>.md` is missing or outdated
- When `docs/roadmap.md` needs a completion entry
- When `docs/OUTPUT_ARTIFACT_CONTRACTS.md` needs new artifact entries
- When `docs/ARCHITECTURE.md` needs a brief addition

## When NOT to Use

- To write Python implementation (Claude's role)
- To write tests (Claude's role)
- To make roadmap decisions (user's role)
- To update docs for an incomplete or untested feature

## Instructions

1. **Read `AGENTS.md`** and `.agent/project_state.yaml`.
2. **Read Claude's final report** — what was built, what artifacts were written.
3. **Use `.agent/task_templates/codex_docs_prompt.md`** — work through each docs section.
4. Update or create `docs/<MODULE_NAME>.md` with: Purpose, Observe-Only Behavior, Artifacts, JSON Contract, Module API, Pipeline Integration, Tests.
5. Add a completion entry to `docs/roadmap.md`.
6. Update `docs/OUTPUT_ARTIFACT_CONTRACTS.md` if new artifacts were added.
7. Update `docs/ARCHITECTURE.md` only if a new pipeline component was added.
8. Do not modify Python files, test files, or output schemas.

## Rules

- Do not invent architectural decisions not present in the code
- Do not mark Discovery Engine as started or complete unless confirmed
- Do not change runtime behavior
- Do not remove existing doc content without verifying it changed in the code

## Final Output Format

```
## Codex Docs Response

Files updated: [list]
Files created: [list or none]
Sections changed: [describe]
Artifact contract additions: [list or none]
Roadmap entry added: [yes/no — step name]
Architecture doc changes: [none | brief description]
Runtime behavior changes: none
```
