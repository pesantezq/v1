# Final Report Template

Claude must return this report at the end of every implementation task.
Fill in every section. Do not omit sections — write "none" if not applicable.

---

## Final Report — {{STEP_NAME}}

### 1. Files Created

| File | Purpose |
|------|---------|
| `{{path}}` | {{purpose}} |

### 2. Files Modified

| File | Change |
|------|--------|
| `{{path}}` | {{what changed}} |

### 3. Behavior Implemented

{{Description of what the module does, its integration point, and its observe-only boundaries.}}

Key behaviors:
- {{behavior 1}}
- {{behavior 2}}
- {{behavior 3}}

### 4. Artifacts Written

| Artifact | Path | Namespace | Format |
|----------|------|-----------|--------|
| {{name}} | `outputs/{{namespace}}/{{filename}}` | OutputNamespace.{{NAMESPACE}} | JSON/MD/JSONL |

Key payload fields:
- `generated_at`: ISO 8601 UTC timestamp
- `observe_only`: true (hardcoded)
- `available`: true \| false
- {{other key fields}}

### 5. Tests Added

| File | Tests Added |
|------|------------|
| `tests/{{test_file}}.py` | N passed |

Coverage includes:
- {{test category 1}}
- {{test category 2}}
- {{test category 3}}

### 6. Test Commands Run

```bash
python -m py_compile {{changed_files}}
python -m pytest -q tests/{{test_file}}.py
python -m pytest -q \
  --ignore=tests/test_gui_api_health.py \
  --ignore=tests/test_gui_insight_cards.py
```

### 7. Test Results

```
{{test output here}}
N passed, M skipped — all pass
```

### 8. Assumptions

- {{assumption 1 — e.g., field names match current scanner output format}}
- {{assumption 2}}

### 9. Risks

- {{risk 1 — e.g., pricing table is static and may lag provider changes}}
- {{risk 2 — or "none"}}

### 10. VPS Validation Commands

Claude does not run on the VPS. Run these commands manually on the production server.

```bash
cd /path/to/repo
source .venv/bin/activate

git pull

pip install -r requirements.txt

python -m py_compile {{changed_files}}

python -m pytest -q tests/{{test_file}}.py

python -m pytest -q \
  --ignore=tests/test_gui_api_health.py \
  --ignore=tests/test_gui_insight_cards.py

# Verify artifacts (after a live run, not dry run):
ls -la outputs/latest/
ls -la outputs/policy/

cat outputs/latest/{{new_artifact}}.json | python -m json.tool | head -20
```

### 11. Recommended Next Step

From `.agent/project_state.yaml:next_official_step`:

**Primary:** {{next_official_step_primary}}

**Secondary options:**
- {{next_official_step_secondary_1}}
- {{next_official_step_secondary_2}}

Note: This recommendation is advisory. The authoritative next step is controlled by
`.agent/project_state.yaml`. Do not start Discovery Engine work unless it is listed
in `next_official_step`.

---

## Checklist

- [ ] py_compile passes on all changed files
- [ ] Targeted tests pass
- [ ] Full suite passes
- [ ] No forbidden changes made
- [ ] Artifacts written to correct namespaces
- [ ] `observe_only: true` in all new artifact payloads
- [ ] Pipeline integration is non-blocking (try/except)
- [ ] docs updated or created
- [ ] `docs/roadmap.md` updated
- [ ] VPS validation commands provided
