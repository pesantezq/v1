# Artifact Contract Checklist

Use when a new module writes output artifacts, or when reviewing a diff that touches file output paths.

Reference: `docs/OUTPUT_ARTIFACT_CONTRACTS.md`

---

## Output Path Validation

- [ ] New artifact path is documented in `docs/OUTPUT_ARTIFACT_CONTRACTS.md`
- [ ] Path uses a declared namespace (`outputs/latest/`, `outputs/policy/`, `outputs/backtest/`, `outputs/sandbox/`, `outputs/portfolio/`)
- [ ] Path does not use a raw string — uses `get_output_path()` or `safe_write_*`

---

## Namespace Correctness

| Namespace | Correct use |
|-----------|-------------|
| `outputs/latest/` | Per-run live artifacts: reports, summaries, decision plans |
| `outputs/policy/` | Budget, governance, audit logs |
| `outputs/backtest/` | Historical replay artifacts only |
| `outputs/sandbox/` | Exploratory; not consumed by GUI or decisions |
| `outputs/portfolio/` | Portfolio snapshots |

- [ ] Module uses the correct namespace for its artifact type
- [ ] Live pipeline does not write to `outputs/backtest/`
- [ ] Replay does not write to `outputs/latest/`, `outputs/policy/`, or `outputs/portfolio/`

---

## JSON Schema Preserved

- [ ] Existing keys are present in new output (no removals)
- [ ] Existing key types unchanged (no string→int, no list→dict, etc.)
- [ ] `generated_at` is an ISO 8601 UTC timestamp string
- [ ] `observe_only: true` is present and hardcoded (not a variable)
- [ ] `available: true` or `available: false` is present (not missing)
- [ ] `summary_line` is a human-readable string if present
- [ ] No new required keys that would break existing consumers

---

## Markdown Report Preserved

If a `.md` artifact is written alongside `.json`:

- [ ] Markdown file exists at expected path
- [ ] Markdown file content is human-readable
- [ ] Markdown file does not duplicate exact JSON content (is a rendered summary)
- [ ] Section headers match previous format (`## Summary`, `## Issues`, etc.)

---

## JSONL Append-Only Log (if applicable)

- [ ] JSONL file is append-only (not overwritten)
- [ ] Each line is a valid JSON object
- [ ] `load_recent_*` function tolerates missing file (returns `[]`)
- [ ] `load_recent_*` function tolerates malformed lines (skips with debug log)

---

## GUI Consumer Check

- [ ] GUI Decision Center pages that consume `decision_plan.json` still load
- [ ] Data Quality card (if wired) still loads
- [ ] AI Budget card (if wired) still loads
- [ ] No GUI page raises a `KeyError` on new artifact payload

---

## Replay Isolation

- [ ] Replay artifacts are written under `outputs/backtest/` only
- [ ] `_assert_safe_replay_output_dir` guard is present in replay write paths
- [ ] Live pipeline does not call any historical replay write functions
- [ ] `outputs/latest/` does not contain replay artifacts after a run

---

## Write Function Check

- [ ] JSON artifacts use `safe_write_json(namespace, filename, data, base_dir=...)`
- [ ] Text/Markdown artifacts use `safe_write_text(namespace, filename, content, base_dir=...)`
- [ ] JSONL append uses `get_output_path()` + `open('a')` pattern (safe_write_text overwrites)
- [ ] `ensure_output_dir(namespace)` called before first JSONL write

---

## Contract Checklist Pass Criteria

All namespace, schema, and consumer checks pass = artifact contract clean.

Any failure = do not mark step complete. Fix the artifact path or schema before proceeding.
