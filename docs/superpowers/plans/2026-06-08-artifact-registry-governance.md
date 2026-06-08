# Artifact Registry & Probe Governance Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create one machine-readable registry describing every `outputs/latest` (and the few non-latest tracked) artifacts, a cadence-aware validator that emits `artifact_registry_status.json`, and invert `daily_run_status` to read the registry — making artifact coverage/freshness/ownership a single governed contract.

**Architecture:** A declarative YAML contract (`portfolio_automation/artifact_registry.yaml`) is the single source of truth. A pure-function module (`portfolio_automation/artifact_registry.py`) loads it, exposes `required_artifacts()` (consumed by `daily_run_status`, replacing its hardcoded `_EXPECTED_ARTIFACTS`), validates the corpus (cadence-derived staleness, severity rollup, producer-without-consumer gaps), and writes an observe-only status artifact. `/daily-tool-analysis` reads that status first and gates confidence by each artifact's `role`.

**Tech Stack:** Python 3 stdlib + PyYAML 6.0.1 (`yaml.safe_load`, already a repo dep — used by `signal_registry.py`). pytest.

**Spec:** `docs/superpowers/specs/2026-06-08-artifact-registry-governance-design.md`

**Branch:** `feat/artifact-registry-governance` (created; spec committed there as `0cdd81f9`).

### Refinements over the spec (discovered while reading `daily_run_status.py:248`)
1. **Rows carry an explicit `path`.** `_EXPECTED_ARTIFACTS` includes non-`outputs/latest` files (`outputs/portfolio/portfolio_snapshot.json`, `outputs/performance/approved_*.json`) and non-JSON (`decision_plan.md`, `daily_memo.md`, `daily_memo.txt`). So a row's `path` is explicit; it defaults to `outputs/latest/<key>` only when omitted.
2. **An ordered `daily_run_status_tracked` list** pins the exact subset + order `daily_run_status` emits, so the inverted output is byte-identical regardless of catalog ordering.
3. **Total rows ≈ 52**: the ~45 `outputs/latest/*.json` catalog **plus** the 7 extra tracked entries (`decision_plan.md`, `daily_memo.md`, `daily_memo.txt`, `portfolio_snapshot.json`, `approved_ranking_config.json`, `approved_allocation_policy.json`, `theme_opportunities.json`).

### Critical discipline for this branch
- **Never `git commit -am`.** The working tree carries unrelated modified tracked files. Stage explicit paths only. Before any push, run `git diff main HEAD --stat` and confirm only intended files changed.
- Observe-only: no decision/score/allocation/portfolio mutation. `observe_only: true` hardcoded in the status artifact.

---

## File Structure

| File | Responsibility | Path |
|---|---|---|
| Registry contract | Declarative per-artifact metadata + ordered tracked list | `portfolio_automation/artifact_registry.yaml` (create) |
| Registry module | load / required_artifacts / staleness / validate / orchestrate | `portfolio_automation/artifact_registry.py` (create) |
| daily_run_status invert | read `required_artifacts()` instead of `_EXPECTED_ARTIFACTS` | `portfolio_automation/daily_run_status.py` (modify) |
| daily skill wiring | read status first; gate confidence by role; heartbeat | `.claude/commands/daily-tool-analysis.md` (modify) |
| Tests | loader/registry-schema/required/staleness/validator/orchestrator/golden | `tests/test_artifact_registry.py` (create) |
| Golden fixture | captured pre-invert daily_run_status payload | `tests/fixtures/daily_run_status_golden.json` (create) |
| Docs | module doc + changelog + roadmap step | `docs/artifact_registry.md`, `docs/CHANGELOG_DECISIONS.md`, `.agent/project_state.yaml` (modify) |

---

## Task 1: Module scaffold — constants + fault-tolerant loader

**Files:**
- Create: `portfolio_automation/artifact_registry.py`
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_artifact_registry.py
from pathlib import Path

from portfolio_automation import artifact_registry as ar


def test_load_registry_missing_returns_empty(tmp_path):
    assert ar.load_registry(tmp_path / "nope.yaml") == {}


def test_load_registry_corrupt_returns_empty(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(":\n  - [unbalanced", encoding="utf-8")
    assert ar.load_registry(p) == {}


def test_load_registry_parses_minimal(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(
        "schema_version: 1\n"
        "daily_run_status_tracked: [a.json]\n"
        "artifacts:\n"
        "  a.json:\n"
        "    path: outputs/latest/a.json\n"
        "    label: A\n"
        "    lens: developer\n"
        "    role: telemetry\n"
        "    required: true\n"
        "    cadence: daily\n"
        "    producer: prod_a\n"
        "    consumers: [daily-tool-analysis]\n"
        "    severity_if_missing: warning\n",
        encoding="utf-8",
    )
    reg = ar.load_registry(p)
    assert reg["schema_version"] == 1
    assert reg["daily_run_status_tracked"] == ["a.json"]
    assert reg["artifacts"]["a.json"]["lens"] == "developer"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest -q tests/test_artifact_registry.py`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError`.

- [ ] **Step 3: Write minimal implementation**

```python
# portfolio_automation/artifact_registry.py
"""artifact_registry — observe-only governance of outputs/* artifacts.

Single machine-readable contract (artifact_registry.yaml) describing every
tracked artifact: lens, role, required, cadence, producer, consumers, severity.
The validator classifies the live corpus (present / stale / invalid / unattributed)
and writes outputs/latest/artifact_registry_status.json. daily_run_status consumes
required_artifacts() instead of a hardcoded list — single source of truth.

Observe-only: reads the registry + artifact mtimes; writes only its status
artifact. Never mutates decision/score/allocation/portfolio state. See
docs/superpowers/specs/2026-06-08-artifact-registry-governance-design.md.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

GREEN, AMBER, RED = "green", "amber", "red"

DEFAULT_REGISTRY_PATH = Path(__file__).with_name("artifact_registry.yaml")
_STATUS_REL = "artifact_registry_status.json"  # under outputs/latest/

# allowed enum values (a row with anything else is flagged schema_invalid)
LENSES = {"developer", "quant_learning", "market_discovery", "risk_action",
          "decision_core", "meta_governance"}
ROLES = {"source_of_truth", "advisor", "probe", "telemetry", "narrative"}
CADENCES = {"daily", "weekend", "weekly", "monthly", "yearly", "on_demand"}
SEVERITIES = {"critical", "warning", "info"}
_REQUIRED_ROW_FIELDS = ("lens", "role", "required", "cadence", "producer",
                        "consumers", "severity_if_missing")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> dict:
    """Parse the YAML registry; return {} on missing/corrupt (fault-tolerant)."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        data.setdefault("artifacts", {})
        data.setdefault("daily_run_status_tracked", [])
        if not isinstance(data["artifacts"], dict):
            return {}
        return data
    except Exception:
        return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest -q tests/test_artifact_registry.py`
Expected: PASS (3 passed).

- [ ] **Step 5: Compile + commit**

```bash
python3 -m py_compile portfolio_automation/artifact_registry.py
git add portfolio_automation/artifact_registry.py tests/test_artifact_registry.py
git commit -m "feat(artifact-registry): module scaffold + fault-tolerant YAML loader"
```

---

## Task 2: The registry contract (`artifact_registry.yaml`) + schema-validation test

**Files:**
- Create: `portfolio_automation/artifact_registry.yaml`
- Modify: `portfolio_automation/artifact_registry.py`
- Test: `tests/test_artifact_registry.py`

This task populates the full contract. Build it from the live artifact set + the
exact tracked list below, then add a test that every row is schema-valid and the
tracked list resolves.

**The `daily_run_status_tracked` ordered list (VERBATIM from `_EXPECTED_ARTIFACTS`, order preserved):**
```
decision_plan.json, decision_plan.md, system_decision_summary.json, daily_memo.md,
daily_memo.txt, news_intelligence.json, risk_delta.json, portfolio_snapshot.json,
approved_ranking_config.json, approved_allocation_policy.json, theme_opportunities.json
```
Their exact `path` + `label` + `required` (must match `daily_run_status.py:248-262` exactly):

| key | path | label | required |
|---|---|---|---|
| decision_plan.json | outputs/latest/decision_plan.json | decision plan | true |
| decision_plan.md | outputs/latest/decision_plan.md | decision plan (md) | true |
| system_decision_summary.json | outputs/latest/system_decision_summary.json | system decision summary | true |
| daily_memo.md | outputs/latest/daily_memo.md | daily memo (md) | true |
| daily_memo.txt | outputs/latest/daily_memo.txt | daily memo (txt) | true |
| news_intelligence.json | outputs/latest/news_intelligence.json | news intelligence | true |
| risk_delta.json | outputs/latest/risk_delta.json | risk delta panel | true |
| portfolio_snapshot.json | outputs/portfolio/portfolio_snapshot.json | portfolio snapshot | true |
| approved_ranking_config.json | outputs/performance/approved_ranking_config.json | approved ranking config | false |
| approved_allocation_policy.json | outputs/performance/approved_allocation_policy.json | approved allocation policy | false |
| theme_opportunities.json | outputs/latest/theme_opportunities.json | theme opportunities | false |

**The ~45 `outputs/latest/*.json` to catalog** (from `ls outputs/latest/*.json`):
```
ai_budget_summary, ai_decision_validation, alpha_attribution_report, cash_deployment_plan,
confidence_calibration, correlation_risk_advisor, daily_run_status, data_quality_report,
decision_explanations, decision_plan, decisions_due_for_resolution, decision_triage,
discovery_pulse_status, doc_audit_status, earnings_gate, exit_advisor, fmp_budget_status,
gate_retune_suggestions, historical_backfill_status, kelly_sizing_advisor,
market_narrative_daily, market_narrative_monthly, market_narrative_weekly,
market_opportunities, memo_delivery_status, news_evidence_layer, news_intelligence,
pattern_efficacy_monthly, pattern_efficacy_weekly, pattern_efficacy_yearly,
pipeline_run_status, quant_watch_status, retune_impact, risk_delta,
scraped_intel_comparison, scraped_intel_run_summary, system_decision_summary,
tax_harvest_advisor, theme_engine_llm_metadata, theme_signals, top100_daily,
top100_monthly, top100_weekly, vol_regime_advisor, watch_candidates, watchlist_signals
```

**Field-derivation rules (apply per row, then hand-verify):**
- `lens`: decision_core = {decision_plan, system_decision_summary, decision_explanations, decision_triage}; risk_action = {risk_delta, correlation_risk_advisor, vol_regime_advisor, earnings_gate, exit_advisor, cash_deployment_plan, tax_harvest_advisor}; quant_learning = {retune_impact, pattern_efficacy_*, gate_retune_suggestions, confidence_calibration, alpha_attribution_report, kelly_sizing_advisor, quant_watch_status}; market_discovery = {theme_signals, watch_candidates, watchlist_signals, top100_*, market_opportunities, market_narrative_*, news_intelligence, news_evidence_layer, scraped_intel_*, discovery_pulse_status}; developer = {daily_run_status, pipeline_run_status, fmp_budget_status, ai_budget_summary, decisions_due_for_resolution, historical_backfill_status, memo_delivery_status, data_quality_report, theme_engine_llm_metadata, ai_decision_validation}; meta_governance = {doc_audit_status, artifact_registry_status}.
- `role`: source_of_truth = {decision_plan, decision_plan.md, system_decision_summary}; probe = {quant_watch_status, decisions_due_for_resolution}; narrative = {market_narrative_*, daily_memo.*}; telemetry = {*_status, *_summary, fmp_budget_status, ai_budget_summary, *_run_status, theme_engine_llm_metadata}; advisor = everything else (the *_advisor, gate_*, pattern_efficacy_*, retune_impact, exit/earnings/cash/tax, kelly, confidence_calibration, alpha_attribution, top100_*, theme_signals, watch_*, market_opportunities, news_*, scraped_intel_*).
- `required`: true for the 8 required tracked rows above + {daily_run_status, fmp_budget_status, risk_delta, retune_impact, pattern_efficacy_monthly, discovery_pulse_status, quant_watch_status}; false for everything else (advisory/gated). When unsure → false (a false-negative miss is info, not a false alarm).
- `cadence`: by name — `*_weekly`→weekly, `*_monthly`→monthly, `*_yearly`→yearly, `historical_backfill_status`→weekend, `doc_audit_status`→weekly, `approved_*`/`theme_opportunities`→on_demand; everything else→daily.
- `producer`: the module that writes it (e.g. quant_watch_status→quant_watch_probes, risk_delta→risk_delta_advisor, retune_impact→retune_impact_tracker, daily_run_status→daily_run_status). Where unknown, set the best-guess module name and add `notes: producer-unverified`.
- `consumers`: derive by grep (Task is mechanical):
  `grep -rl "<filename>.json" .claude/commands .claude/agents .claude/skills gui_v2 2>/dev/null`
  → list the skill/agent/template basenames. **If zero hits → `consumers: [UNATTRIBUTED]`.**
- `severity_if_missing`: critical for `role: source_of_truth`; warning for `required: true` non-source-of-truth; info otherwise.

- [ ] **Step 1: Write the registry file**

Create `portfolio_automation/artifact_registry.yaml` with this exact shape (full
example rows shown; produce ALL rows by the rules above). Order the `artifacts:`
block however is readable; the tracked subset's order is fixed by the list.

```yaml
# Artifact governance contract. One row per tracked artifact. Hand-edited on every
# new artifact. Consumed by portfolio_automation/artifact_registry.py (validator)
# and daily_run_status.py (required_artifacts). See docs/artifact_registry.md.
schema_version: 1

# Exact subset + ORDER that daily_run_status emits (was _EXPECTED_ARTIFACTS).
# Do not reorder without updating the golden-output fixture.
daily_run_status_tracked:
  - decision_plan.json
  - decision_plan.md
  - system_decision_summary.json
  - daily_memo.md
  - daily_memo.txt
  - news_intelligence.json
  - risk_delta.json
  - portfolio_snapshot.json
  - approved_ranking_config.json
  - approved_allocation_policy.json
  - theme_opportunities.json

artifacts:
  decision_plan.json:
    path: outputs/latest/decision_plan.json
    label: decision plan
    lens: decision_core
    role: source_of_truth
    required: true
    cadence: daily
    producer: decision_engine
    consumers: [daily-tool-analysis, portfolio-memo-reviewer]
    severity_if_missing: critical
  decision_plan.md:
    path: outputs/latest/decision_plan.md
    label: decision plan (md)
    lens: decision_core
    role: source_of_truth
    required: true
    cadence: daily
    producer: decision_engine
    consumers: [UNATTRIBUTED]
    severity_if_missing: critical
  # ... portfolio_snapshot.json (path outputs/portfolio/...), approved_*.json
  #     (path outputs/performance/..., required:false, cadence:on_demand,
  #     severity:info), and ALL ~45 outputs/latest rows by the rules above ...
  quant_watch_status.json:
    path: outputs/latest/quant_watch_status.json
    label: quant watch status
    lens: quant_learning
    role: probe
    required: true
    cadence: daily
    producer: quant_watch_probes
    consumers: [quant-watch-analysis, daily-tool-analysis]
    severity_if_missing: warning
  artifact_registry_status.json:
    path: outputs/latest/artifact_registry_status.json
    label: artifact registry status
    lens: meta_governance
    role: telemetry
    required: true
    cadence: daily
    producer: artifact_registry
    consumers: [daily-tool-analysis]
    severity_if_missing: warning
```

- [ ] **Step 2: Write the failing schema test**

```python
def test_shipped_registry_schema_valid():
    reg = ar.load_registry()  # the real artifact_registry.yaml
    assert reg, "registry failed to load"
    arts = reg["artifacts"]
    # every tracked key exists in artifacts
    for key in reg["daily_run_status_tracked"]:
        assert key in arts, f"tracked key missing from artifacts: {key}"
    # every row has the 7 required fields with in-enum values
    bad = ar.schema_errors(reg)
    assert bad == [], f"schema errors: {bad}"
    # coverage: all 45 outputs/latest json names cataloged
    expected_latest = {
        "ai_budget_summary","ai_decision_validation","alpha_attribution_report",
        "cash_deployment_plan","confidence_calibration","correlation_risk_advisor",
        "daily_run_status","data_quality_report","decision_explanations","decision_plan",
        "decisions_due_for_resolution","decision_triage","discovery_pulse_status",
        "doc_audit_status","earnings_gate","exit_advisor","fmp_budget_status",
        "gate_retune_suggestions","historical_backfill_status","kelly_sizing_advisor",
        "market_narrative_daily","market_narrative_monthly","market_narrative_weekly",
        "market_opportunities","memo_delivery_status","news_evidence_layer",
        "news_intelligence","pattern_efficacy_monthly","pattern_efficacy_weekly",
        "pattern_efficacy_yearly","pipeline_run_status","quant_watch_status",
        "retune_impact","risk_delta","scraped_intel_comparison","scraped_intel_run_summary",
        "system_decision_summary","tax_harvest_advisor","theme_engine_llm_metadata",
        "theme_signals","top100_daily","top100_monthly","top100_weekly","vol_regime_advisor",
        "watch_candidates","watchlist_signals",
    }
    cataloged = {k[:-5] for k in arts if k.endswith(".json")}
    missing = expected_latest - cataloged
    assert missing == set(), f"uncataloged outputs/latest artifacts: {missing}"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k schema_valid`
Expected: FAIL — `schema_errors` not defined (and/or rows missing).

- [ ] **Step 4: Add `schema_errors` to the module**

```python
def schema_errors(registry: dict) -> list[str]:
    """Return a list of human-readable schema problems (empty == valid)."""
    errs: list[str] = []
    arts = registry.get("artifacts", {})
    for key, row in arts.items():
        if not isinstance(row, dict):
            errs.append(f"{key}: row is not a mapping")
            continue
        for f in _REQUIRED_ROW_FIELDS:
            if f not in row:
                errs.append(f"{key}: missing field {f}")
        if row.get("lens") not in LENSES:
            errs.append(f"{key}: bad lens {row.get('lens')!r}")
        if row.get("role") not in ROLES:
            errs.append(f"{key}: bad role {row.get('role')!r}")
        if row.get("cadence") not in CADENCES:
            errs.append(f"{key}: bad cadence {row.get('cadence')!r}")
        if row.get("severity_if_missing") not in SEVERITIES:
            errs.append(f"{key}: bad severity {row.get('severity_if_missing')!r}")
        if not isinstance(row.get("consumers"), list) or not row.get("consumers"):
            errs.append(f"{key}: consumers must be a non-empty list")
    for key in registry.get("daily_run_status_tracked", []):
        if key not in arts:
            errs.append(f"tracked key not in artifacts: {key}")
    return errs
```

- [ ] **Step 5: Iterate the YAML until the test passes**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k schema_valid`
Expected: PASS once all ~52 rows are present and valid. Fix reported rows until green.

- [ ] **Step 6: Commit**

```bash
python3 -m py_compile portfolio_automation/artifact_registry.py
git add portfolio_automation/artifact_registry.yaml portfolio_automation/artifact_registry.py tests/test_artifact_registry.py
git commit -m "feat(artifact-registry): full contract YAML (~52 rows) + schema validation"
```

---

## Task 3: `required_artifacts()` + exact equivalence to `_EXPECTED_ARTIFACTS`

**Files:**
- Modify: `portfolio_automation/artifact_registry.py`
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Write the failing test**

```python
def test_required_artifacts_matches_legacy_expected():
    # The exact tuples daily_run_status historically used (order matters).
    legacy = [
        ("outputs/latest/decision_plan.json", "decision plan", True),
        ("outputs/latest/decision_plan.md", "decision plan (md)", True),
        ("outputs/latest/system_decision_summary.json", "system decision summary", True),
        ("outputs/latest/daily_memo.md", "daily memo (md)", True),
        ("outputs/latest/daily_memo.txt", "daily memo (txt)", True),
        ("outputs/latest/news_intelligence.json", "news intelligence", True),
        ("outputs/latest/risk_delta.json", "risk delta panel", True),
        ("outputs/portfolio/portfolio_snapshot.json", "portfolio snapshot", True),
        ("outputs/performance/approved_ranking_config.json", "approved ranking config", False),
        ("outputs/performance/approved_allocation_policy.json", "approved allocation policy", False),
        ("outputs/latest/theme_opportunities.json", "theme opportunities", False),
    ]
    assert ar.required_artifacts() == legacy
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k required_artifacts`
Expected: FAIL — `required_artifacts` not defined.

- [ ] **Step 3: Implement**

```python
def required_artifacts(registry: dict | None = None) -> list[tuple[str, str, bool]]:
    """Return (rel_path, label, required) triples for the daily_run_status-tracked
    subset, in tracked order — the exact shape of the legacy _EXPECTED_ARTIFACTS."""
    reg = registry if registry is not None else load_registry()
    arts = reg.get("artifacts", {})
    out: list[tuple[str, str, bool]] = []
    for key in reg.get("daily_run_status_tracked", []):
        row = arts.get(key)
        if not isinstance(row, dict):
            continue
        path = row.get("path") or f"outputs/latest/{key}"
        out.append((path, row.get("label", key), bool(row.get("required", False))))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k required_artifacts`
Expected: PASS. (If not, fix the path/label/required values of the 11 tracked rows in the YAML to match exactly.)

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/artifact_registry.py tests/test_artifact_registry.py
git commit -m "feat(artifact-registry): required_artifacts() == legacy _EXPECTED_ARTIFACTS"
```

---

## Task 4: Cadence-derived staleness

**Files:**
- Modify: `portfolio_automation/artifact_registry.py`
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Write the failing test**

```python
def test_max_age_hours_by_cadence():
    assert ar.max_age_hours({"cadence": "daily"}) == 30
    assert ar.max_age_hours({"cadence": "weekly"}) == 192
    assert ar.max_age_hours({"cadence": "monthly"}) == 768
    assert ar.max_age_hours({"cadence": "weekend"}) == 100
    assert ar.max_age_hours({"cadence": "yearly"}) == 9000
    assert ar.max_age_hours({"cadence": "on_demand"}) is None
    # override wins
    assert ar.max_age_hours({"cadence": "daily", "staleness_hours_override": 50}) == 50


def test_is_stale_respects_cadence():
    # weekly artifact 40h old is NOT stale; daily artifact 51h old IS stale
    assert ar.is_stale({"cadence": "weekly"}, age_hours=40) is False
    assert ar.is_stale({"cadence": "daily"}, age_hours=51) is True
    assert ar.is_stale({"cadence": "on_demand"}, age_hours=10_000) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k "max_age or is_stale"`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Implement**

```python
CADENCE_MAX_AGE_HOURS = {
    "daily": 30, "weekend": 100, "weekly": 192,
    "monthly": 768, "yearly": 9000, "on_demand": None,
}


def max_age_hours(row: dict) -> int | None:
    """Staleness window for a row: explicit override, else cadence default,
    else None (never auto-stale)."""
    ov = row.get("staleness_hours_override")
    if isinstance(ov, (int, float)):
        return int(ov)
    return CADENCE_MAX_AGE_HOURS.get(row.get("cadence"), None)


def is_stale(row: dict, age_hours: float) -> bool:
    mx = max_age_hours(row)
    return mx is not None and age_hours > mx
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k "max_age or is_stale"`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/artifact_registry.py tests/test_artifact_registry.py
git commit -m "feat(artifact-registry): cadence-derived staleness"
```

---

## Task 5: `validate_registry()` — classification + rollups

**Files:**
- Modify: `portfolio_automation/artifact_registry.py`
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Write the failing test**

```python
import json as _json


def _mini_registry():
    return {"schema_version": 1, "daily_run_status_tracked": [],
            "artifacts": {
                "sot.json": {"path": "outputs/latest/sot.json", "label": "sot",
                    "lens": "decision_core", "role": "source_of_truth", "required": True,
                    "cadence": "daily", "producer": "p", "consumers": ["daily-tool-analysis"],
                    "severity_if_missing": "critical"},
                "probe.json": {"path": "outputs/latest/probe.json", "label": "probe",
                    "lens": "quant_learning", "role": "probe", "required": True,
                    "cadence": "daily", "producer": "p", "consumers": ["UNATTRIBUTED"],
                    "severity_if_missing": "warning"},
            }}


def _write(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(obj), encoding="utf-8")


def test_validate_red_when_critical_missing(tmp_path):
    reg = _mini_registry()
    # only the probe exists, fresh; sot (critical) missing
    _write(tmp_path / "outputs/latest/probe.json", {"x": 1})
    now = ar.datetime.now(ar.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    assert st["overall_status"] == "red"
    assert "sot.json" in st["missing"]
    assert "probe.json" in st["unattributed"]


def test_validate_green_when_all_present_fresh(tmp_path):
    reg = _mini_registry()
    reg["artifacts"]["probe.json"]["consumers"] = ["daily-tool-analysis"]  # attributed
    _write(tmp_path / "outputs/latest/sot.json", {"x": 1})
    _write(tmp_path / "outputs/latest/probe.json", {"x": 1})
    now = ar.datetime.now(ar.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    assert st["overall_status"] == "green"
    assert st["counts"]["present"] == 2


def test_validate_amber_when_warning_stale(tmp_path):
    reg = _mini_registry()
    reg["artifacts"]["probe.json"]["consumers"] = ["daily-tool-analysis"]
    _write(tmp_path / "outputs/latest/sot.json", {"x": 1})
    pf = tmp_path / "outputs/latest/probe.json"
    _write(pf, {"x": 1})
    import os
    old = (ar.datetime.now(ar.timezone.utc).timestamp()) - 60 * 3600  # 60h old
    os.utime(pf, (old, old))
    now = ar.datetime.now(ar.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    assert st["overall_status"] == "amber"
    assert any(s["artifact"] == "probe.json" for s in st["stale"])


def test_validate_invalid_json_listed(tmp_path):
    reg = _mini_registry()
    reg["artifacts"]["probe.json"]["consumers"] = ["daily-tool-analysis"]
    reg["artifacts"]["probe.json"]["severity_if_missing"] = "info"
    _write(tmp_path / "outputs/latest/sot.json", {"x": 1})
    bad = tmp_path / "outputs/latest/probe.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    now = ar.datetime.now(ar.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    assert "probe.json" in st["invalid_json"]


def test_validate_flags_schema_invalid_row(tmp_path):
    reg = _mini_registry()
    reg["artifacts"]["bad.json"] = {"path": "outputs/latest/bad.json", "lens": "nope",
        "role": "probe", "required": False, "cadence": "daily", "producer": "p",
        "consumers": ["x"], "severity_if_missing": "info"}
    now = ar.datetime.now(ar.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    assert "bad.json" in st["schema_invalid"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k validate`
Expected: FAIL — `validate_registry` not defined.

- [ ] **Step 3: Implement**

```python
def _row_schema_ok(row: dict) -> bool:
    return (isinstance(row, dict)
            and all(f in row for f in _REQUIRED_ROW_FIELDS)
            and row.get("lens") in LENSES and row.get("role") in ROLES
            and row.get("cadence") in CADENCES
            and row.get("severity_if_missing") in SEVERITIES
            and isinstance(row.get("consumers"), list) and bool(row.get("consumers")))


def validate_registry(registry: dict, artifacts_root, now) -> dict:
    """Classify every cataloged artifact and roll up to an observe-only status dict."""
    root = Path(artifacts_root)
    arts = registry.get("artifacts", {})
    rows = []
    missing, stale, invalid_json, unattributed, schema_invalid = [], [], [], [], []
    sev_counts = {"critical": 0, "warning": 0, "info": 0}
    by_lens: dict[str, dict] = {}

    for key, row in arts.items():
        if not _row_schema_ok(row):
            schema_invalid.append(key)
            continue
        path = root / (row.get("path") or f"outputs/latest/{key}")
        sev = row.get("severity_if_missing", "info")
        lens = row["lens"]
        lens_bucket = by_lens.setdefault(lens, {"total": 0, "present": 0, "issues": 0})
        lens_bucket["total"] += 1

        if "UNATTRIBUTED" in row.get("consumers", []):
            unattributed.append(key)

        exists = path.exists()
        is_missing = not exists
        is_stale_flag = False
        is_bad_json = False
        if exists:
            try:
                age_h = (now.timestamp() - path.stat().st_mtime) / 3600.0
                is_stale_flag = is_stale(row, age_h)
                if is_stale_flag:
                    stale.append({"artifact": key, "cadence": row["cadence"],
                                  "age_hours": round(age_h, 1)})
            except Exception:
                pass
            if str(path).endswith(".json"):
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    is_bad_json = True
                    invalid_json.append(key)

        problem = is_missing or is_stale_flag or is_bad_json
        if is_missing:
            missing.append(key)
        if problem:
            lens_bucket["issues"] += 1
            sev_counts[sev] = sev_counts.get(sev, 0) + 1
        else:
            lens_bucket["present"] += 1
        rows.append((key, sev, problem))

    if sev_counts["critical"] > 0:
        overall = RED
    elif sev_counts["warning"] > 0:
        overall = AMBER
    else:
        overall = GREEN

    present = sum(1 for _, _, prob in rows if not prob)
    msg_bits = []
    if missing:
        msg_bits.append(f"{len(missing)} missing")
    if stale:
        msg_bits.append(f"{len(stale)} stale")
    if invalid_json:
        msg_bits.append(f"{len(invalid_json)} invalid-json")
    if unattributed:
        msg_bits.append(f"{len(unattributed)} unattributed")
    operator_message = "; ".join(msg_bits) or "all artifacts present, fresh, attributed"

    return {
        "generated_at": now.isoformat(),
        "observe_only": True,
        "schema_version": "1",
        "source": "artifact_registry",
        "overall_status": overall,
        "counts": {"total": len(arts), "present": present, "stale": len(stale),
                   "invalid_json": len(invalid_json), "missing": len(missing),
                   "missing_required": sum(1 for k in missing
                                           if arts.get(k, {}).get("required")),
                   "unattributed": len(unattributed), "schema_invalid": len(schema_invalid)},
        "missing": missing, "stale": stale, "invalid_json": invalid_json,
        "unattributed": unattributed, "schema_invalid": schema_invalid,
        "severity": sev_counts, "by_lens": by_lens,
        "operator_message": operator_message,
        "disclaimer": ("Observe-only artifact-governance validator. Reads the registry "
                       "+ artifact mtimes; classifies coverage/freshness. Does not call "
                       "APIs or mutate any decision, allocation, score, or portfolio state."),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k validate`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
python3 -m py_compile portfolio_automation/artifact_registry.py
git add portfolio_automation/artifact_registry.py tests/test_artifact_registry.py
git commit -m "feat(artifact-registry): validate_registry classification + severity rollup"
```

---

## Task 6: `run_artifact_registry()` orchestrator + status write

**Files:**
- Modify: `portfolio_automation/artifact_registry.py`
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Write the failing test**

```python
def test_run_writes_status_and_never_raises(tmp_path):
    # point the orchestrator at the SHIPPED registry but a tmp artifacts root
    st = ar.run_artifact_registry(root=tmp_path, write_files=True)
    assert st["observe_only"] is True
    assert st["source"] == "artifact_registry"
    # most artifacts absent under tmp root → status produced, no raise
    out = tmp_path / "outputs/latest/artifact_registry_status.json"
    assert out.exists()
    written = _json.loads(out.read_text())
    assert written["overall_status"] in ("green", "amber", "red")


def test_run_degrades_on_bad_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "load_registry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    st = ar.run_artifact_registry(root=tmp_path, write_files=False)
    assert st["observe_only"] is True
    assert st["overall_status"] == "green"  # degraded-but-valid
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k run_`
Expected: FAIL — `run_artifact_registry` not defined.

- [ ] **Step 3: Implement**

```python
from portfolio_automation.data_governance import OutputNamespace, safe_write_json


def run_artifact_registry(*, root: str | Path = ".", now=None,
                          write_files: bool = True) -> dict:
    """Load registry → validate corpus → write status artifact. Never raises."""
    root_path = Path(root).resolve()
    ts = now or datetime.now(timezone.utc)
    try:
        registry = load_registry()
        status = validate_registry(registry, root_path, ts)
        if write_files:
            safe_write_json(OutputNamespace.LATEST, _STATUS_REL, status,
                            base_dir=root_path / "outputs")
        return status
    except Exception as exc:
        return {"generated_at": ts.isoformat(), "observe_only": True,
                "schema_version": "1", "source": "artifact_registry",
                "overall_status": GREEN, "counts": {}, "missing": [], "stale": [],
                "invalid_json": [], "unattributed": [], "schema_invalid": [],
                "severity": {"critical": 0, "warning": 0, "info": 0}, "by_lens": {},
                "operator_message": f"degraded: {exc}",
                "disclaimer": "Observe-only artifact-governance validator (degraded)."}
```

Move the `from portfolio_automation.data_governance import ...` line to the top of the
module with the other imports (do not leave it inside the function).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k run_`
Expected: PASS (2 passed). Then run the whole file: `python3 -m pytest -q tests/test_artifact_registry.py` → all green.

- [ ] **Step 5: Commit**

```bash
python3 -m py_compile portfolio_automation/artifact_registry.py
git add portfolio_automation/artifact_registry.py tests/test_artifact_registry.py
git commit -m "feat(artifact-registry): run_artifact_registry orchestrator + status write"
```

---

## Task 7: Invert `daily_run_status` to read the registry (schema-preserving)

**Files:**
- Modify: `portfolio_automation/daily_run_status.py:248-262` (and the `scan_expected_artifacts` loop reference)
- Create: `tests/fixtures/daily_run_status_golden.json`
- Test: `tests/test_artifact_registry.py`

- [ ] **Step 1: Capture the golden output BEFORE changing anything**

Run (captures the current payload structure from the live tree, normalizing volatile fields):
```bash
cd /opt/stockbot && python3 -c "
import json
from pathlib import Path
from portfolio_automation import daily_run_status as d
rows = d.scan_expected_artifacts(Path('.'))
# normalize volatile fields so the golden compares structure+contract, not mtimes
norm = [{'path': r['path'], 'label': r['label'], 'required': r['required']} for r in rows]
Path('tests/fixtures').mkdir(parents=True, exist_ok=True)
Path('tests/fixtures/daily_run_status_golden.json').write_text(json.dumps(norm, indent=2))
print('captured', len(norm), 'rows')
"
```
Expected: `captured 11 rows`.

- [ ] **Step 2: Write the failing equivalence test**

```python
def test_daily_run_status_tracks_same_artifacts_via_registry():
    import json as J
    from pathlib import Path
    from portfolio_automation import daily_run_status as d
    golden = J.loads(Path("tests/fixtures/daily_run_status_golden.json").read_text())
    rows = d.scan_expected_artifacts(Path("."))
    got = [{"path": r["path"], "label": r["label"], "required": r["required"]} for r in rows]
    assert got == golden
```

(This passes NOW against the old code — it is the guard. Run it, confirm PASS, THEN
do the invert in Step 3 and confirm it STILL passes.)

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k tracks_same`
Expected: PASS (against the un-inverted code).

- [ ] **Step 3: Invert — replace the hardcoded list with the registry feed**

In `portfolio_automation/daily_run_status.py`, replace the `_EXPECTED_ARTIFACTS = [...]`
block (lines 248-262) with a registry-backed builder + a built-in fallback:

```python
# Expected artifacts the official-lane run should produce. Sourced from the
# artifact registry (single source of truth); falls back to a built-in copy if
# the registry is unavailable so this module never hard-fails.
_FALLBACK_EXPECTED_ARTIFACTS = [
    ("outputs/latest/decision_plan.json",                  "decision plan",                True),
    ("outputs/latest/decision_plan.md",                    "decision plan (md)",           True),
    ("outputs/latest/system_decision_summary.json",        "system decision summary",      True),
    ("outputs/latest/daily_memo.md",                       "daily memo (md)",              True),
    ("outputs/latest/daily_memo.txt",                      "daily memo (txt)",             True),
    ("outputs/latest/news_intelligence.json",              "news intelligence",            True),
    ("outputs/latest/risk_delta.json",                     "risk delta panel",             True),
    ("outputs/portfolio/portfolio_snapshot.json",          "portfolio snapshot",           True),
    ("outputs/performance/approved_ranking_config.json",   "approved ranking config",      False),
    ("outputs/performance/approved_allocation_policy.json","approved allocation policy",   False),
    ("outputs/latest/theme_opportunities.json",            "theme opportunities",          False),
]


def _expected_artifacts() -> list[tuple[str, str, bool]]:
    try:
        from portfolio_automation.artifact_registry import required_artifacts
        rows = required_artifacts()
        if rows:
            return rows
    except Exception:
        pass
    return _FALLBACK_EXPECTED_ARTIFACTS
```

Then in `scan_expected_artifacts`, change the loop header from:
```python
    for rel_path, label, required in _EXPECTED_ARTIFACTS:
```
to:
```python
    for rel_path, label, required in _expected_artifacts():
```

- [ ] **Step 4: Run the equivalence test to verify it STILL passes**

Run: `python3 -m pytest -q tests/test_artifact_registry.py -k tracks_same`
Expected: PASS — identical output via the registry. If it fails, the YAML's 11
tracked rows don't exactly match; fix them (Task 3's test will also point at the diff).

- [ ] **Step 5: Run the existing daily_run_status tests + compile**

Run: `python3 -m pytest -q tests/ -k daily_run_status` (run whatever daily_run_status tests exist; if none collect, skip)
Run: `python3 -m py_compile portfolio_automation/daily_run_status.py`
Expected: no regressions; compile clean.

- [ ] **Step 6: Commit**

```bash
git add portfolio_automation/daily_run_status.py tests/fixtures/daily_run_status_golden.json tests/test_artifact_registry.py
git commit -m "refactor(daily-run-status): read expected artifacts from registry (schema-preserving)"
```

---

## Task 8: Wire `/daily-tool-analysis` to read the registry status first

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md`

Three additive edits. Read the file first for exact anchors.

- [ ] **Step 1: Add to Step 1 artifact reads (as the FIRST item)**

In the "Step 1 — Load state + read artifacts" numbered list, add a new item 0 (before item 1 `daily_run_status.json`):

```markdown
0. `outputs/latest/artifact_registry_status.json` → overall_status, counts, missing[], stale[], invalid_json[], unattributed[], severity, operator_message (added 2026-06-08; artifact-governance validator — READ FIRST, it gates confidence in everything below). If absent, fall back to daily_run_status as before and note the registry validator did not run.
```

- [ ] **Step 2: Add a confidence-gating rule near the top of Step 2 (Triage)**

Immediately under the "## Step 2 — Triage" heading, add:

```markdown
**Artifact-governance gate (run before GREEN/AMBER/RED):**
- Read `artifact_registry_status.json` first. If a `role: source_of_truth` artifact is in `missing` or `stale` → **downgrade confidence and cap the run at AMBER at best** (the decision core is not trustworthy); never infer portfolio actions from a degraded decision core.
- If a `required` `role: probe` artifact is missing/stale → mark the analysis **partial** for that lens.
- `unattributed` entries are debt, not failures: note them, route to `portfolio-discovery-health` if non-empty, do not change status on their account.
- Only `source_of_truth` artifacts represent official actions; probe/advisor/telemetry/narrative artifacts inform confidence and explanation only.
```

- [ ] **Step 3: Add a Step-4 heartbeat line**

In "Step 4 — Output", as the FIRST body item (before the current item 1 Attribution snapshot), add:

```markdown
0. Artifact governance (always, first): `"Coverage: {present}/{total} present · {missing} missing ({missing_required} required) · {stale} stale · {unattributed} unattributed · {overall_status}"` — from artifact_registry_status.json. RED here (critical/source-of-truth missing) forces the daily lead line to RED.
```

- [ ] **Step 4: Verify the edits**

Run: `cd /opt/stockbot && grep -n "artifact_registry_status\|Artifact-governance\|Artifact governance\|Coverage:" .claude/commands/daily-tool-analysis.md`
Expected: 4+ hits across Step 1, Step 2, Step 4.

- [ ] **Step 5: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md
git commit -m "feat(artifact-registry): wire governance gate + coverage heartbeat into daily-tool-analysis"
```

---

## Task 9: Docs + roadmap step + full validation (STOP before push/PR)

**Files:**
- Create: `docs/artifact_registry.md`
- Modify: `docs/CHANGELOG_DECISIONS.md`, `.agent/project_state.yaml`

- [ ] **Step 1: Write `docs/artifact_registry.md`**

```markdown
# artifact_registry

Single machine-readable contract for every tracked artifact + an observe-only
validator. The governance layer that turns ~52 artifacts into one governed corpus.

## Files
- `portfolio_automation/artifact_registry.yaml` — the contract (one row per artifact).
- `portfolio_automation/artifact_registry.py` — loader, `required_artifacts()`,
  `validate_registry()`, `run_artifact_registry()` (never raises).
- `outputs/latest/artifact_registry_status.json` — observe-only governance snapshot.

## Row schema
`lens` (developer | quant_learning | market_discovery | risk_action | decision_core |
meta_governance) · `role` (source_of_truth | advisor | probe | telemetry | narrative) ·
`required` · `cadence` (daily | weekend | weekly | monthly | yearly | on_demand) ·
`producer` · `consumers` (or `[UNATTRIBUTED]`) · `severity_if_missing`
(critical | warning | info) · optional `staleness_hours_override` / `notes`.

## Staleness
Cadence-derived: daily 30h · weekend 100h · weekly 192h · monthly 768h · yearly 9000h ·
on_demand never. Per-row override available. (This is why weekly/monthly artifacts
don't false-alarm at a flat 30h.)

## Severity → status
Any critical missing/stale → red · any warning → amber · else green.

## The invariant
Only `role: source_of_truth` artifacts represent official portfolio actions.
probe/advisor/telemetry/narrative artifacts inform analysis, warnings, explanations,
and confidence — they never independently create or override a buy/sell/hold.

## Single source of truth
`daily_run_status.py` reads `required_artifacts()` (no hardcoded list); the daily
skill reads `artifact_registry_status.json` first and gates confidence by `role`.

Observe-only: mutates only its status artifact; never decision/score/allocation state.
```

- [ ] **Step 2: Add a CHANGELOG entry**

Read the top of `docs/CHANGELOG_DECISIONS.md` for format, then add a dated (2026-06-08)
entry: shipped the artifact registry & probe governance layer (declarative YAML
contract for ~52 artifacts, cadence-aware validator → artifact_registry_status.json,
daily_run_status inverted to read the registry with a golden-output guard, observe-only
invariant codified via `role`, daily-tool-analysis gates confidence by governance).
Reference the spec + plan paths.

- [ ] **Step 3: Record the roadmap step**

In `.agent/project_state.yaml`, append to the `completed_steps` list (match existing
comment style):
```yaml
  - artifact_registry_and_probe_governance_layer  # 2026-06-08 — declarative artifact_registry.yaml contract for ~52 artifacts + cadence-aware validator (artifact_registry_status.json) + daily_run_status inverted to read the registry (golden-output guard, no schema break) + observe-only invariant codified via role field + daily-tool-analysis confidence gate. GPT-proposed, operator-approved; supersedes the standing observe_and_iterate hold for this governance step. Observe-only; next_official_step unchanged (observe_and_iterate).
```
Leave `next_official_step.primary: observe_and_iterate` unchanged (this was a sanctioned governance insertion, not a new roadmap phase).

- [ ] **Step 4: Full validation**

Run:
```bash
cd /opt/stockbot
python3 -m pytest -q tests/test_artifact_registry.py     # expect all green
python3 -m py_compile portfolio_automation/artifact_registry.py portfolio_automation/daily_run_status.py
python3 -c "import json; from portfolio_automation.artifact_registry import run_artifact_registry; s=run_artifact_registry(root='.', write_files=False); print('overall', s['overall_status'], '| counts', json.dumps(s['counts']))"
python3 -m pytest -q   # FULL suite — report result; 19 pre-existing python-dotenv collection errors are unrelated and expected
```
Expected: targeted suite green; live validator prints an overall status + counts over the real corpus (this is the first true coverage read — note any `unattributed`/`missing` for the operator).

- [ ] **Step 5: Commit (do NOT push / open PR — controller handles the production boundary)**

```bash
git add docs/artifact_registry.md docs/CHANGELOG_DECISIONS.md .agent/project_state.yaml
git commit -m "docs(artifact-registry): module doc + changelog + roadmap step"
git diff main HEAD --stat   # CONFIRM only intended files changed (no config.json / outputs ride-alongs)
```

---

## Self-Review

**Spec coverage:**
- §3 architecture (registry → module → daily_run_status + status artifact + daily skill) → Tasks 1–8 ✓
- §4 module API (load_registry/required_artifacts/validate_registry/run_artifact_registry) → Tasks 1,3,5,6 ✓
- §5 row schema + UNATTRIBUTED + schema validation → Tasks 2,5 ✓
- §6 cadence-staleness + severity→status → Tasks 4,5 ✓
- §7 invariant via `role` → encoded in YAML (Task 2) + enforced in daily skill (Task 8) ✓
- §8 invert + golden guard + runtime fallback → Task 7 ✓
- §9 observe-only/namespace/never-raise → Tasks 5,6 ✓
- §10 test matrix (11 cases) → Tasks 1–7 (loader, schema, required-equivalence, cadence, validator red/amber/green/invalid/schema_invalid, orchestrator+degraded, golden) ✓
- §11 health pairing (daily skill reads first + heartbeat) → Task 8 ✓
- §12 consumer attribution by grep → Task 2 Step 1 rules ✓
- §13/§14 deferrals → not implemented (correct) ✓

**Placeholder scan:** The YAML in Task 2 shows representative rows + explicit derivation rules + the full filename list + a completeness test that fails until all ~45 are present — the engineer produces every row mechanically and the test enforces it. `UNATTRIBUTED` is an intentional sentinel. No "TODO/TBD/handle-edge-cases".

**Type/name consistency:** `load_registry`/`required_artifacts`/`schema_errors`/`max_age_hours`/`is_stale`/`validate_registry`/`run_artifact_registry` used consistently across tasks; `_REQUIRED_ROW_FIELDS`/`LENSES`/`ROLES`/`CADENCES`/`SEVERITIES` defined in Task 1, reused in Tasks 2 & 5; status-dict keys (`overall_status`, `counts`, `missing`, `stale`, `invalid_json`, `unattributed`, `schema_invalid`, `severity`, `by_lens`, `operator_message`) consistent between Task 5 and the daily-skill consumption in Task 8. `_expected_artifacts()` (Task 7) consumes `required_artifacts()` (Task 3). ✓
