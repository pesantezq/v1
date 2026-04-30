# Data Governance — Output Namespace Utilities

## Purpose

This module provides a reusable governance layer that prevents accidental contamination
between different categories of system output. It defines named output namespaces,
enforces path containment, and supplies safe writer helpers that future modules should
use instead of raw `Path.write_text()` calls.

The module is **additive and non-breaking**: existing production writers are not changed.
They carry `TODO(v2-data-governance)` comments marking where future migration should happen.

---

## Namespace Table

| Namespace | Directory | User-scoped | Purpose |
|-----------|-----------|-------------|---------|
| `LIVE` | `outputs/live/{user_id}/` | Yes | Future live pipeline outputs, scoped per user |
| `HISTORICAL` | `outputs/backtest/` | No | Historical replay outputs — never mixed with live |
| `SANDBOX` | `outputs/sandbox/` | No | Discovery / test outputs pending promotion to live |
| `POLICY` | `outputs/policy/` | No | Policy evaluation, calibration, and coverage reports |
| `PORTFOLIO` | `outputs/portfolio/` | No | Portfolio snapshot and summary reports |
| `LATEST` | `outputs/latest/` | No | Current-run decision artifacts (decision plan, memo, etc.) |
| `USER` | `outputs/users/{user_id}/` | Yes | Future per-user scoped outputs |

---

## Usage Examples

### Getting a path without writing

```python
from portfolio_automation.data_governance import OutputNamespace, get_output_path

path = get_output_path(OutputNamespace.POLICY, "coverage_evaluation.json")
# → Path("outputs/policy/coverage_evaluation.json")

path = get_output_path(OutputNamespace.LIVE, "summary.json", user_id="alice")
# → Path("outputs/live/alice/summary.json")
```

### Writing JSON safely

```python
from portfolio_automation.data_governance import OutputNamespace, safe_write_json

path = safe_write_json(
    OutputNamespace.HISTORICAL,
    "historical_calibration.json",
    payload,
    base_dir="outputs",
)
# Validates namespace containment, creates directory, writes, returns path
```

### Writing text safely

```python
from portfolio_automation.data_governance import OutputNamespace, safe_write_text

path = safe_write_text(
    OutputNamespace.POLICY,
    "coverage_evaluation.md",
    markdown_content,
)
```

### Ensuring a directory exists

```python
from portfolio_automation.data_governance import OutputNamespace, ensure_output_dir

d = ensure_output_dir(OutputNamespace.SANDBOX)
# → Path("outputs/sandbox") — created if missing
```

### Detecting namespace from an existing path

```python
from portfolio_automation.data_governance import namespace_for_existing_path

ns = namespace_for_existing_path("outputs/policy/recommendation_outcomes.json")
# → OutputNamespace.POLICY
```

---

## Historical Replay Must Use `HISTORICAL`

All outputs from `portfolio_automation/historical_replay/` must be written under
`HISTORICAL` (`outputs/backtest/`).

**Hard rule:** historical replay must never write to `outputs/live/`, `outputs/latest/`,
`outputs/policy/`, or `outputs/portfolio/`. Mixing historical and live artifacts would
corrupt calibration metrics and produce incorrect recommendations.

The replay runner currently writes to `outputs/backtest/` directly. When it migrates to
safe writers it should use:

```python
safe_write_json(OutputNamespace.HISTORICAL, "decision_outcomes_historical.jsonl", rows)
safe_write_json(OutputNamespace.HISTORICAL, "historical_calibration.json", calibration)
safe_write_json(OutputNamespace.HISTORICAL, "historical_performance_attribution.json", attribution)
```

---

## Live Pipeline: Current Paths Remain Unchanged

The live pipeline currently writes to `outputs/latest/`, `outputs/policy/`, and
`outputs/portfolio/` using direct `Path.write_text()` calls. These paths are correct
and are not changed by this module.

When the live pipeline is eventually migrated to namespace-aware writes, it should use:

| Current path | Target namespace |
|---|---|
| `outputs/latest/*` | `OutputNamespace.LATEST` |
| `outputs/policy/*` | `OutputNamespace.POLICY` |
| `outputs/portfolio/*` | `OutputNamespace.PORTFOLIO` |

---

## Discovery Sandbox Outputs

New analysis or experimental features should write to `SANDBOX` until they are
validated and promoted:

```python
safe_write_json(OutputNamespace.SANDBOX, "experiment_results.json", results)
```

Only after review should sandbox outputs be promoted to `POLICY`, `LATEST`, or another
permanent namespace.

---

## Future Migration Path to `LIVE` and `USER`

Phase 0 establishes the infrastructure. Actual migration follows this sequence:

1. **Phase 0 (complete):** `user_id` column added to SQLite state tables; namespace
   utilities created; existing writers carry `TODO(v2-data-governance)` comments.

2. **Phase 1:** Convert new modules to use `safe_write_*` from day one. No existing
   writer is changed yet.

3. **Phase 2:** Migrate policy/portfolio/latest writers to namespace-aware helpers,
   keeping paths identical (the helpers produce the same paths as today).

4. **Phase 3:** Introduce `LIVE/{user_id}/` paths as multi-user support is added.
   The `user_id` column in SQLite + the `USER` and `LIVE` namespaces are already
   ready for this.

---

## Security: Path Traversal Prevention

`validate_output_path()` resolves both the expected namespace root and the given path
to absolute form using `Path.resolve()`. A path containing `../` components is caught
because the resolved path will fall outside the namespace root.

`user_id` is validated against `[a-zA-Z0-9_\-\.]+` before it is used as a path
component. Slashes, null bytes, and traversal sequences are rejected with
`DataGovernanceError`.

---

---

## Historical Replay Integration

Historical Replay is the first real consumer of the data governance layer (Phase 0 Step 2b).

### Canonical path

All Historical Replay outputs go to `outputs/backtest/` — the `HISTORICAL` namespace.

### What was migrated

| File | Change |
|------|--------|
| `portfolio_automation/historical_replay/replay_reports.py` | `write_calibration` and `write_attribution` now use `safe_write_json` / `safe_write_text` with `OutputNamespace.HISTORICAL` |
| `portfolio_automation/historical_replay/replay_runner.py` | JSONL write replaced with `safe_write_text(OutputNamespace.HISTORICAL, ...)` |

### Hard rules enforced

1. **Historical replay must never write to live, latest, policy, portfolio, or users paths.**
   `_assert_safe_replay_output_dir()` raises `DataGovernanceError` if the `output_dir`
   contains any of: `latest`, `live`, `policy`, `portfolio`, `users`, `sandbox`.

2. **The governance module validates path containment.**
   `safe_write_json` and `safe_write_text` call `validate_output_path(HISTORICAL, ...)` internally,
   which resolves paths to absolute form and confirms they fall under `outputs/backtest/`.

3. **Historical replay outputs are isolated from live artifacts.**
   A write to `outputs/policy/` or `outputs/latest/` from a replay writer is impossible
   without explicitly defeating both the live-path guard and the namespace validator.

### Backward compatibility

Existing callers pass `output_dir = .../<base>/backtest`. The writers derive
`base_dir = output_dir.parent` and call governance with `base_dir`. Because
`safe_write_json(HISTORICAL, filename, payload, base_dir=<base>)` produces
`<base>/backtest/<filename>`, all artifact paths remain identical to before the migration.

### Artifact filenames (unchanged)

```
outputs/backtest/decision_outcomes_historical.jsonl
outputs/backtest/historical_calibration.json
outputs/backtest/historical_calibration.md
outputs/backtest/historical_performance_attribution.json
outputs/backtest/historical_performance_attribution.md
```

### Tests

```bash
python -m pytest -q tests/historical_replay/test_replay_data_governance.py
# 41 tests covering: path isolation, filename preservation,
# live-path rejection, governance function invocation, full run integrity
```

---

## Module Location

```
portfolio_automation/data_governance.py
```

Importable from any module in the repo:

```python
from portfolio_automation.data_governance import (
    OutputNamespace,
    DataGovernanceError,
    OutputPathPolicy,
    get_output_path,
    validate_output_path,
    ensure_output_dir,
    safe_write_text,
    safe_write_json,
    namespace_for_existing_path,
    get_policies,
)
```
