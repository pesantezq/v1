# Skill: portfolio-docs

## Purpose

Update documentation after Claude builds a feature. Does not change runtime behavior.

## When to Use

- After a new module is implemented and tests pass
- When `docs/<MODULE_NAME>.md` is missing or outdated
- When `docs/roadmap.md` needs a completion entry
- When `docs/OUTPUT_ARTIFACT_CONTRACTS.md` needs a new artifact entry
- When `docs/ARCHITECTURE.md` needs a brief addition for a new pipeline component

## When NOT to Use

- To generate new Python code (use `portfolio-feature` skill)
- To update tests (tests are part of the feature implementation)
- To make roadmap decisions (the user controls this)
- To update docs for a feature that is not yet complete

## Step-by-Step Process

1. **Read the final report from Claude**
   - Files created, files modified, artifacts written, behavior implemented

2. **Read the new module**
   - `portfolio_automation/<module>.py`
   - Confirm public API, artifacts, and namespace usage

3. **Update or create `docs/<MODULE_NAME>.md`**
   - Use the standard module doc template
   - Include: Purpose, Observe-Only Behavior, Artifacts, JSON Contract, API, Pipeline Integration, Tests
   - Do not contradict the actual code

4. **Update `docs/roadmap.md`**
   - Add a completion entry with: what was built, key files, test count
   - Do not change the status of steps not yet completed
   - Do not mark Discovery Engine as complete or started

5. **Update `docs/OUTPUT_ARTIFACT_CONTRACTS.md`** (if new artifacts added)
   - Add entries for each new artifact path
   - Include: path, namespace, format, key fields, written by, read by

6. **Update `docs/ARCHITECTURE.md`** (only if a new pipeline component was added)
   - Add a brief bullet or sentence only
   - Do not rewrite existing architecture sections

7. **Return doc update response**

## Required Final Output

Structured response listing:
- Files updated
- Files created
- Sections changed
- Whether artifact contract was updated
- Whether roadmap was updated
- Confirmation that no runtime behavior was changed
