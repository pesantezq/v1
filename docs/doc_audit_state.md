# doc_audit_state — Auditor State File

## Purpose

`portfolio_automation/doc_audit_state.py` manages the small committed-state
file that lets the documentation auditor resume correctly across workstations
and sessions.

---

## State File Location

`.agent/doc_audit_state.yaml`

The `.agent/` directory is tracked by git (unlike `data/`, which is gitignored).
Any workstation that pulls the branch gets the same last-audited baseline,
ensuring the coverage-gap check produces consistent results everywhere.

---

## Keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `last_audited_sha` | `str \| null` | `null` | Git SHA through which the last full audit ran; used to compute `git diff <sha>..HEAD` for coverage-gap detection |
| `last_run_at` | `str \| null` | `null` | ISO-8601 timestamp of the last audit invocation |
| `apply_enabled` | `bool` | `true` | When `false`, auto-fix is suppressed for the next run (see `docs/doc_audit.md`) |
| `fixes_last_run` | `int` | `0` | Count of auto-fixes applied during the most recent run |

---

## API

- `load_state(root: str) -> dict` — reads the file and merges with defaults;
  returns defaults if the file is absent or unparseable.
- `save_state(root: str, state: dict) -> None` — merges with defaults and
  writes; creates `.agent/` if needed.
- `state_path(root: str) -> str` — returns the absolute path to the state file.

---

## Further Reading

See `docs/doc_audit.md` for the full documentation-auditor design, check
families, anchor registry, and auto-fix guardrails.
