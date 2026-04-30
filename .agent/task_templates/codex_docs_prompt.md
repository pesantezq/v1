# Codex Documentation Update Prompt Template

Use this template to prompt Codex after a Claude feature is complete.
Codex should update documentation only — no runtime behavior changes.

---

## Task: Documentation Update — {{STEP_NAME}}

Read `AGENTS.md` before starting.
Read `.agent/project_state.yaml` to confirm context.

Your role is documentation update only. Do not modify any Python files
that affect runtime behavior. Do not modify tests. Do not modify output schemas.

---

## What Claude Built

{{CLAUDE_FINAL_REPORT_SUMMARY}}

Files created:
{{FILES_CREATED}}

Files modified:
{{FILES_MODIFIED}}

---

## Docs to Inspect

Review these docs for accuracy against the new module:

- `docs/{{MODULE_NAME}}.md` (may need creation or update)
- `docs/OUTPUT_ARTIFACT_CONTRACTS.md` (add new artifact if applicable)
- `docs/roadmap.md` (mark step complete)
- `docs/ARCHITECTURE.md` (only if a new architectural component was added)

---

## Docs to Update

### docs/{{MODULE_NAME}}.md

If this file does not exist, create it with:
- Purpose
- Observe-only behavior section
- Artifacts (paths, namespaces, JSON contract)
- Issue types or config (if applicable)
- Module API
- Pipeline integration snippet
- Test command

If it exists, update:
- Any section that changed in Claude's implementation
- Do not remove content that is still accurate

### docs/roadmap.md

Mark `{{STEP_NAME}}` as Complete in the roadmap.
Add a brief entry with:
- What was built
- Key files
- Tests added

Do not change the status of other steps.

### docs/OUTPUT_ARTIFACT_CONTRACTS.md

If Claude's feature writes new output files:
- Add new entries for each new artifact path
- Include: path, namespace, format, key fields, who writes it, who reads it
- Preserve existing entries

---

## Artifact Contract Sections

For each new artifact Claude wrote, document:

```markdown
### <artifact_name>

- **Path:** `outputs/<namespace>/<filename>`
- **Namespace:** OutputNamespace.<NAMESPACE>
- **Format:** JSON | Markdown | JSONL
- **Written by:** `portfolio_automation/<module>.py`
- **Read by:** GUI | memo | decision engine | operator only
- **Key fields:**
  - `generated_at`: ISO timestamp
  - `observe_only`: true (hardcoded)
  - `available`: true | false
  - [other fields]
```

---

## Architecture Notes

Only update `docs/ARCHITECTURE.md` if:
- A new module is wired into the main pipeline
- A new namespace is used for the first time
- A new observable layer was added

If updating, add a brief bullet or sentence only — do not rewrite architecture sections.

---

## Changelog Notes

If `docs/CHANGELOG_DECISIONS.md` exists, add an entry:

```markdown
## {{DATE}} — {{STEP_NAME}} Complete

- [list key additions]
- Tests: N passed
- Artifacts: [list]
```

---

## Rules

- Do not change any Python source files
- Do not change any test files
- Do not change output artifact schemas
- Do not invent architectural decisions not present in the code
- Do not remove or contradict existing doc content without verification
- Do not mark Discovery Engine as complete or started unless confirmed

---

## Final Response Format

```
## Codex Docs Update Response

Files updated: [list]
Files created: [list or none]
Sections changed: [describe]
Artifact contract additions: [list or none]
Architecture doc changes: [list or none]
Roadmap update: [step marked complete: yes/no]
Issues found in docs: [list or none]
Runtime behavior changes: none
```
