# Claude Code — System Improvement Implementation Prompt (Type C)

Template for turning an approved **system-improvement idea** into a Claude Code
implementation prompt. Generated programmatically by
`portfolio_automation.claude_code_prompts.generate_system_improvement_prompt(idea)`.

A system improvement makes the *system* better (reliability, observability, UX,
data quality, contracts, tests, docs, …). It is **not** a market recommendation.

Every generated prompt MUST contain:
- Repo context (read CLAUDE.md, ARCHITECTURE_MAP, the next-stage spec)
- Exact problem + evidence
- Affected modules / artifacts
- Implementation scope (smallest additive change)
- Files to inspect
- Acceptance criteria
- Tests to run
- Docs to update
- **Forbidden block** (no auto-trading / order placement / broker writes / money
  movement / allocation changes / protected-logic edits / unrelated refactors)
- Final report format

Approval only generates this prompt — the operator launches Claude Code manually;
the system never edits code on its own.
