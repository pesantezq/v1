---
name: portfolio-docs
description: Update documentation after Claude builds a feature, fixes a resolver, retunes a gauge, or ships an observability module. Touches docs only — no runtime, test, or schema changes. Use for any change that needs docs sync, including the CHANGELOG and project_state.yaml.
---

# Skill: portfolio-docs

## Purpose

Update documentation after Claude ships a feature or fix. Does not change runtime behavior.

## When to Use

- After a new observability v2 module is implemented and tests pass
- After a gauge knob or structural cap is changed (CHANGELOG + ALLOCATION_POLICY)
- After a resolver / data-flow fix is shipped (FEEDBACK_LOOP + EVALUATION_AND_LEARNING_LOOP)
- After a feature flag is flipped (e.g. `ml_advisor.enabled`)
- When `docs/<MODULE_NAME>.md` is missing or outdated
- When `docs/roadmap.md` needs a completion entry
- When `docs/OUTPUT_ARTIFACT_CONTRACTS.md` needs a new artifact entry
- When `docs/ARCHITECTURE.md` needs a brief addition for a new pipeline component
- When `.agent/project_state.yaml:next_official_step` needs to advance

## When NOT to Use

- To generate new Python code → use `portfolio-feature` skill
- To update tests → tests are part of the feature implementation
- To make roadmap decisions → the user controls this
- To update docs for a feature that hasn't shipped (read `git log` and the actual files first)

## Step-by-Step Process

1. **Read the final report from Claude** — files created, files modified, artifacts written, behavior implemented.

2. **Cross-check with `git log` + `git diff`** — never document hypothetical behavior. The CHANGELOG should only describe what's actually committed.

3. **Read the new module(s)** — confirm public API, artifacts, namespace usage. Read the existing doc (if any) and preserve its voice/structure.

4. **Update or create `docs/<MODULE_NAME>.md`** — use the standard module doc template. Include: Purpose, Observe-Only Behavior, Artifacts, JSON Contract, API, Pipeline Integration, Tests.

5. **Update `docs/roadmap.md`** — add a completion entry with: what was built, key files, test count. Don't change status of unfinished steps.

6. **Update `docs/OUTPUT_ARTIFACT_CONTRACTS.md`** — for any new artifact: path, namespace, format, key fields, written by, read by.

7. **Update `docs/CHANGELOG_DECISIONS.md`** — for retunes, structural cap changes, feature-flag flips, or resolver fixes. Use the template in `portfolio-doc-writer` agent.

8. **Update `docs/ARCHITECTURE.md`** — only if a new pipeline component was added. Brief sentence/bullet, not a rewrite.

9. **Update `.agent/project_state.yaml`** — add to `completed_steps`, advance `next_official_step` if appropriate. Validate YAML parses cleanly before returning.

10. **Refresh numerical values** — if any gauge knob changed, sync `docs/ALLOCATION_POLICY.md`. The post-2026-05-18 retune values are the current baseline.

11. **Return doc update response** with file list, line counts, and one-sentence summary per doc.

## Current Numerical Baseline (post-2026-05-18 retune)

Use these as the documented current state. Anything in the docs referencing pre-retune values is stale.

| Surface | Knob | Current |
|---|---|---|
| `allocation_engine.DEFAULT_CONFIG.compounder_base_pct` | 0.10 |
| `allocation_engine.DEFAULT_CONFIG.momentum_base_pct` | 0.06 |
| `allocation_engine.DEFAULT_CONFIG.max_position_cap` | 0.15 |
| `allocation_engine.DEFAULT_CONFIG.sector_cap` | 0.35 |
| `allocation_engine.DEFAULT_CONFIG.low_confidence_multiplier` | 0.65 |
| `portfolio_construction.max_total_allocation` | 0.30 |
| `portfolio_construction.max_ticker_allocation` | 0.05 |
| `portfolio_construction.max_sector_allocation` | 0.10 |
| `config.json:growth_mode.concentration_cap` | 0.60 |
| `config.json:growth_mode.leverage_cap` | 0.25 |
| `config.json:api_limits.fmp_daily_calls_budget` | 250 |

## Required Final Output

Structured response listing:
- Files updated (with +/- line counts)
- Files created (or none)
- Sections changed
- Whether artifact contract was updated
- Whether roadmap was updated
- Whether CHANGELOG was updated
- Confirmation that no runtime behavior was changed
- YAML parse status (if `.agent/project_state.yaml` touched)
