# Codex Code Review Prompt Template

Use this template to prompt Codex after Claude implements a feature.
Codex reviews for correctness, contract compliance, and hidden risks.

---

## Task: Code Review — {{STEP_NAME}}

Read `AGENTS.md` before starting.
Read `.agent/project_state.yaml` to confirm context and forbidden changes.

Your role is review only. Do not implement changes. Flag issues and recommend fixes.

---

## Changed Files

{{CHANGED_FILES_LIST}}

```
# Diff to review:
{{DIFF_OR_FILE_CONTENTS}}
```

---

## Review Checklist

Work through each item. Flag any failure with the file name, line number or function,
and a brief description of the issue.

### 1. Forbidden Change Check

- [ ] No scoring behavior changed (signal_score, confidence_score, effective_score, etc.)
- [ ] No allocation behavior changed
- [ ] No recommendation behavior changed
- [ ] No broker API calls added
- [ ] No auto-trading behavior added
- [ ] No `observe_only` flag removed or made conditional
- [ ] No FMP compliance rules bypassed
- [ ] Decision engine not modified unless explicitly authorized

### 2. Output Schema Compatibility

- [ ] New artifact JSON keys match `docs/OUTPUT_ARTIFACT_CONTRACTS.md`
- [ ] Existing artifact keys preserved (no renames, no removals)
- [ ] `observe_only: true` present in all new artifact payloads
- [ ] `available: true/false` pattern consistent with existing artifacts
- [ ] GUI consumer fields not broken

### 3. Namespace Violations

- [ ] Replay code does not write to `outputs/latest/`, `outputs/policy/`, `outputs/portfolio/`
- [ ] Live pipeline code does not write to `outputs/backtest/`
- [ ] All writes use `safe_write_json` or `safe_write_text` from `data_governance.py`
- [ ] `get_output_path` or `ensure_output_dir` used for JSONL append paths
- [ ] No raw `open()` writes outside the intended namespace pattern

### 4. Hidden Behavior Changes

- [ ] No silent fallback that changes recommendation or scoring outcomes
- [ ] No new environment variable that changes behavior if unset
- [ ] No conditional logic that could gate existing behavior
- [ ] Non-blocking integration: `try/except` wrapping in `main.py`
- [ ] `dry_run` guard present if new artifacts are written from `main.py`

### 5. Dependency Impact

- [ ] New packages listed in `requirements.txt`
- [ ] No packages with license restrictions for commercial use (check if applicable)
- [ ] VPS install requires: `pip install -r requirements.txt` (note in response if dependencies changed)

### 6. Test Coverage

- [ ] New module has a corresponding `tests/test_<module>.py`
- [ ] All core functions are tested (cost estimation, budget checking, event persistence, etc.)
- [ ] Edge cases covered: empty inputs, missing files, malformed data, zero-event case
- [ ] Test count reasonable for module complexity (rough guide: 1 test per logical path)
- [ ] Tests do not mock behavior that should be tested end-to-end
- [ ] No tests skipped without documented reason

### 7. Security Risks

- [ ] No user input passed unsanitized to shell commands
- [ ] No credentials, API keys, or tokens in new code
- [ ] File paths not constructed from external input without validation
- [ ] No `eval()` or `exec()` calls
- [ ] JSON parsing includes error handling for malformed input

### 8. Roadmap Drift

- [ ] Feature matches `{{STEP_NAME}}` in `.agent/project_state.yaml`
- [ ] No Discovery Engine code introduced
- [ ] No calibration behavior changed prematurely
- [ ] Recommended next step in Claude's report matches `next_official_step`

---

## Diff-Specific Questions

1. Does the new code integrate cleanly with the existing pipeline without changing call order or results?
2. Are there any import cycles introduced?
3. Are there any `# type: ignore` or `# noqa` suppressions that hide real issues?
4. Is the module API (function signatures, return types) consistent with existing modules?

---

## Response Format

```
## Codex Review Response

Step reviewed: {{STEP_NAME}}
Files inspected: [list]

### Forbidden Change Check
[pass | issues found — list by file:line]

### Output Schema Compatibility
[pass | issues found — describe]

### Namespace Violations
[pass | violations — describe]

### Hidden Behavior Changes
[pass | issues found — describe]

### Dependency Impact
[none | new dependencies: list — VPS install: pip install -r requirements.txt]

### Test Coverage
[adequate | gaps — list missing cases]

### Security Risks
[none | issues — describe]

### Roadmap Drift
[pass | drift detected — describe]

### Summary
Overall assessment: [clean | issues found]
Blocking issues: [list or none]
Recommended fixes: [list or none]
Recommended next step: [from project_state.yaml:next_official_step only]
```
