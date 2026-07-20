# Memo Datasets Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate the monolithic daily memo into 5 observe-only domain datasets (portfolio, crowd_watchlist, institutional, risk, system) exposed via one `memo_datasets.json` + per-domain briefs + GUI sub-tabs, leaving `daily_memo.md` untouched as the email roll-up.

**Architecture:** A new observe-only producer `portfolio_automation/memo_datasets.py` purely reassembles fields from existing producer artifacts into a domain-keyed dataset (no recompute, no `daily_memo.py` rewrite). Renderers + a GUI loader derive from that dataset. Mirrors the `capital_plan_view.py` pattern.

**Tech Stack:** Python 3.12, `pytest`, `portfolio_automation.data_governance.safe_write_json`, FastAPI/Jinja2 (gui_v2), `.venv/bin/python`.

## Global Constraints

- Observe-only; `feeds_decision_engine=false` on every artifact; never writes `outputs/latest/decision_plan.json`; no change to `decision_engine.py` or the six protected scores.
- No rewrite of `watchlist_scanner/daily_memo.py`; `daily_memo.md`/`.txt` and the `/dashboard/memo` existing behavior stay intact (additive only).
- Pure reassembly of existing artifacts — no new computation of money/scores.
- Writes go through `safe_write_json` / `safe_write_text` to `OutputNamespace.LATEST` only.
- Run interpreter as `.venv/bin/python` (bare `python` is not on PATH).
- Domains: `portfolio, crowd_watchlist, institutional, risk, system`. Each section is `{title, lines: [str], severity}`; each domain is `{headline, status, sections, source_artifacts, warnings}`; `status ∈ {ok, degraded, unavailable}`.

---

### Task 1: `build_memo_datasets` — pure domain assembly

**Files:**
- Create: `portfolio_automation/memo_datasets.py`
- Test: `tests/test_memo_datasets.py`

**Interfaces:**
- Produces: `build_memo_datasets(sources: dict, *, domains: list[str] | None = None) -> dict` returning `{"schema_version":"1","source":"memo_datasets","observe_only":True,"no_trade":True,"feeds_decision_engine":False,"generated_at":<passed>,"domains":{<domain>:{"headline":str,"status":str,"sections":[{"title":str,"lines":[str],"severity":str}],"source_artifacts":[str],"warnings":[str]}}}`. `sources` is a dict keyed by artifact stem (e.g. `"daily_capital_plan"`, `"risk_delta"`, `"unified_crowd_status"`, `"institutional_intelligence"`, `"system_decision_summary"`, `"daily_run_status"`, `"decision_plan"`, `"correlation_risk_advisor"`, `"watch_candidates"`), each value the loaded dict (or `None`/absent when missing).
- Constants: `DOMAINS = ["portfolio","crowd_watchlist","institutional","risk","system"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memo_datasets.py
from portfolio_automation import memo_datasets as md


def _sources():
    return {
        "daily_capital_plan": {"available": True, "capital_summary": {
            "funded_capital": {"amount": 104.0, "state": "confirmed"},
            "funded_count": 2, "deferred_count": 20},
            "bottom_line": "You have $104 to deploy today."},
        "system_decision_summary": {"top_theme": {"label": "Energy Transition"},
            "top_opportunity": {"ticker": "MSFT"}},
        "decision_plan": {"decisions": [{"decision": "BUY"}, {"decision": "SELL"}]},
        "risk_delta": {"overall_status": "ok", "concentration": {"top_position":
            {"symbol": "QQQ", "weight": 0.42, "cap": 0.6}}, "leverage": {"total_exposure": 0.145}},
        "correlation_risk_advisor": {"effective_independent_bets": 1.23},
        "unified_crowd_status": {"overall_status": "ok", "state_counts":
            {"market_context_only": 27}, "top_confirmed_attention": [{"ticker": "AAPL"}]},
        "watch_candidates": {"candidates": [{"symbol": "XOM"}]},
        "institutional_intelligence": {"records": [{"symbol": "BE",
            "consensus_state": "moderate_accumulation", "filing_age_days": 24}]},
        "daily_run_status": {"overall_status": "ok", "content_warn_count": 0},
    }


def test_build_produces_all_five_domains():
    d = md.build_memo_datasets(_sources())
    assert set(d["domains"]) == set(md.DOMAINS)
    assert d["feeds_decision_engine"] is False and d["observe_only"] is True
    port = d["domains"]["portfolio"]
    assert port["status"] == "ok" and port["sections"]
    assert any("104" in ln for s in port["sections"] for ln in s["lines"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_memo_datasets.py::test_build_produces_all_five_domains -v`
Expected: FAIL (`ModuleNotFoundError` / `AttributeError: build_memo_datasets`).

- [ ] **Step 3: Write minimal implementation**

```python
# portfolio_automation/memo_datasets.py
"""Observe-only memo datasets: reassemble existing memo-producer artifacts into
domain-keyed datasets (portfolio / crowd_watchlist / institutional / risk /
system). Pure reassembly — no recompute; feeds_decision_engine=false; never
writes decision_plan.json. Source of truth for per-domain briefs + GUI sub-tabs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1"
DOMAINS = ["portfolio", "crowd_watchlist", "institutional", "risk", "system"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _section(title: str, lines: list[str], severity: str = "info") -> dict:
    return {"title": title, "lines": [l for l in lines if l], "severity": severity}


def _domain(headline: str, sections: list[dict], source_artifacts: list[str],
            warnings: list[str] | None = None) -> dict:
    present = [s for s in sections if s["lines"]]
    status = "ok" if present else "unavailable"
    return {"headline": headline, "status": status, "sections": present,
            "source_artifacts": source_artifacts, "warnings": warnings or []}


def _fmt_money(field) -> str:
    if isinstance(field, dict):
        amt, state = field.get("amount"), field.get("state")
        if state == "confirmed" and amt is not None:
            return f"${amt:,.0f}"
        return state or "unavailable"
    v = _num(field)
    return f"${v:,.0f}" if v is not None else "—"


def _build_portfolio(s: dict) -> dict:
    cp = s.get("daily_capital_plan") or {}
    sd = s.get("system_decision_summary") or {}
    dp = s.get("decision_plan") or {}
    cs = cp.get("capital_summary") or {}
    sections = []
    to = (sd.get("top_opportunity") or {}).get("ticker")
    tt = (sd.get("top_theme") or {}).get("label")
    if to or tt:
        sections.append(_section("Verdict", [
            f"Lead opportunity: {to or '—'} · dominant theme: {tt or '—'}"]))
    if cs:
        sections.append(_section("Today's Capital Plan", [
            f"Funded today: {_fmt_money(cs.get('funded_capital'))} "
            f"({cs.get('funded_count', 0)} funded / {cs.get('deferred_count', 0)} deferred)"]))
    if cp.get("bottom_line"):
        sections.append(_section("Bottom Line", [cp["bottom_line"]]))
    decs = dp.get("decisions") or []
    if decs:
        from collections import Counter
        c = Counter(str(x.get("decision")) for x in decs)
        sections.append(_section("Action counts", [
            " · ".join(f"{k}: {v}" for k, v in sorted(c.items()))]))
    return _domain("Portfolio & Capital", sections,
                   ["daily_capital_plan.json", "system_decision_summary.json", "decision_plan.json"])


def _build_crowd(s: dict) -> dict:
    sd = s.get("system_decision_summary") or {}
    uc = s.get("unified_crowd_status") or {}
    wc = s.get("watch_candidates") or {}
    sections = []
    tt = (sd.get("top_theme") or {}).get("label")
    if tt:
        sections.append(_section("Top Insight", [f"Dominant theme: {tt}"]))
    if uc:
        sc = uc.get("state_counts") or {}
        conf = uc.get("top_confirmed_attention") or []
        sections.append(_section("Unified crowd", [
            f"Status {uc.get('overall_status', '—')} · "
            f"market-context-only {sc.get('market_context_only', 0)} · "
            f"confirmed {', '.join(t.get('ticker', '') for t in conf[:5]) or 'none'}"]))
    cand = wc.get("candidates") or wc.get("watch_candidates") or []
    if cand:
        sections.append(_section("Watchlist candidates", [
            f"{len(cand)} candidate(s): "
            + ", ".join(c.get('symbol', '') for c in cand[:8])]))
    return _domain("Crowd & Watchlist", sections,
                   ["system_decision_summary.json", "unified_crowd_intelligence_status.json",
                    "watch_candidates.json"])


def _build_institutional(s: dict) -> dict:
    ii = s.get("institutional_intelligence") or {}
    recs = ii.get("records") or []
    sections = []
    for r in recs[:5]:
        sections.append(_section(r.get("symbol", "?"), [
            f"{r.get('consensus_state', '—')} · "
            f"filing {r.get('filing_age_days', '—')}d old · "
            f"eff mgrs {r.get('effective_independent_managers', '—')}"]))
    dom = _domain("Institutional (13F)", sections, ["institutional_intelligence.json"])
    if not sections:
        dom["warnings"] = ["inert / no material institutional signal"]
    return dom


def _build_risk(s: dict) -> dict:
    rd = s.get("risk_delta") or {}
    corr = s.get("correlation_risk_advisor") or {}
    sections = []
    conc = (rd.get("concentration") or {}).get("top_position") or {}
    if conc:
        w = _num(conc.get("weight"))
        cap = _num(conc.get("cap"))
        sections.append(_section("Concentration", [
            f"Top: {conc.get('symbol', '—')} "
            f"{w * 100:.1f}% (cap {cap * 100:.0f}%)" if w is not None and cap is not None
            else f"Top: {conc.get('symbol', '—')}"]))
    lev = _num((rd.get("leverage") or {}).get("total_exposure"))
    if lev is not None:
        sections.append(_section("Leverage", [f"{lev * 100:.1f}% total exposure"]))
    eib = _num(corr.get("effective_independent_bets"))
    if eib is not None:
        sections.append(_section("Correlation", [f"~{eib:.2f} effective independent bets"]))
    return _domain("Risk", sections, ["risk_delta.json", "correlation_risk_advisor.json"])


def _build_system(s: dict) -> dict:
    rs = s.get("daily_run_status") or {}
    sections = []
    if rs:
        sections.append(_section("System / Data Health", [
            f"Run status {rs.get('overall_status', '—')} · "
            f"content warnings {rs.get('content_warn_count', 0)}"]))
    return _domain("System & Ops", sections, ["daily_run_status.json"])


_BUILDERS = {
    "portfolio": _build_portfolio, "crowd_watchlist": _build_crowd,
    "institutional": _build_institutional, "risk": _build_risk, "system": _build_system,
}


def build_memo_datasets(sources: dict[str, Any], *, domains: list[str] | None = None,
                        generated_at: str | None = None) -> dict:
    domains = domains or DOMAINS
    out_domains = {}
    for d in domains:
        builder = _BUILDERS.get(d)
        if builder is None:
            continue
        try:
            out_domains[d] = builder(sources)
        except Exception as exc:  # noqa: BLE001 - one domain never breaks others
            out_domains[d] = {"headline": d, "status": "unavailable", "sections": [],
                              "source_artifacts": [], "warnings": [f"build_error:{exc}"]}
    return {
        "schema_version": SCHEMA_VERSION, "source": "memo_datasets",
        "observe_only": True, "no_trade": True, "feeds_decision_engine": False,
        "generated_at": generated_at or _now_iso(), "domains": out_domains,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_memo_datasets.py::test_build_produces_all_five_domains -v`
Expected: PASS.

- [ ] **Step 5: Add degradation + no-mutation + determinism tests**

```python
import copy


def test_missing_source_degrades_only_that_domain():
    s = _sources(); del s["risk_delta"]; del s["correlation_risk_advisor"]
    d = md.build_memo_datasets(s)
    assert d["domains"]["risk"]["status"] == "unavailable"
    assert d["domains"]["portfolio"]["status"] == "ok"        # others intact


def test_institutional_inert_is_unavailable_not_error():
    s = _sources(); s["institutional_intelligence"] = {"records": []}
    inst = md.build_memo_datasets(s)["domains"]["institutional"]
    assert inst["status"] == "unavailable" and inst["warnings"]


def test_no_mutation_of_inputs():
    s = _sources(); before = copy.deepcopy(s)
    md.build_memo_datasets(s)
    assert s == before


def test_deterministic():
    s = _sources()
    a = md.build_memo_datasets(s, generated_at="t"); b = md.build_memo_datasets(s, generated_at="t")
    assert a == b


def test_domains_filter():
    d = md.build_memo_datasets(_sources(), domains=["risk"])
    assert list(d["domains"]) == ["risk"]
```

- [ ] **Step 6: Run all Task-1 tests**

Run: `.venv/bin/python -m pytest tests/test_memo_datasets.py -v`
Expected: 6 PASS.

- [ ] **Step 7: Commit**

```bash
git add portfolio_automation/memo_datasets.py tests/test_memo_datasets.py
git commit -m "feat(memo): Task 1 — build_memo_datasets pure domain assembly"
```

---

### Task 2: `render_domain_brief` — pure renderer

**Files:**
- Modify: `portfolio_automation/memo_datasets.py`
- Test: `tests/test_memo_datasets.py`

**Interfaces:**
- Produces: `render_domain_brief(dataset: dict, domain: str, *, markdown: bool = True) -> list[str]` — returns memo lines for one domain (`[]` when the domain is `unavailable`).

- [ ] **Step 1: Write the failing test**

```python
def test_render_domain_brief_populated_and_empty():
    d = md.build_memo_datasets(_sources())
    lines = md.render_domain_brief(d, "portfolio", markdown=True)
    assert lines and any("Portfolio & Capital" in l for l in lines)
    # unavailable domain -> no brief
    s = _sources(); s["institutional_intelligence"] = {"records": []}
    d2 = md.build_memo_datasets(s)
    assert md.render_domain_brief(d2, "institutional") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_memo_datasets.py::test_render_domain_brief_populated_and_empty -v`
Expected: FAIL (`AttributeError: render_domain_brief`).

- [ ] **Step 3: Write minimal implementation** (append to `memo_datasets.py`)

```python
def render_domain_brief(dataset: dict, domain: str, *, markdown: bool = True) -> list[str]:
    dom = (dataset.get("domains") or {}).get(domain)
    if not dom or dom.get("status") == "unavailable":
        return []
    out: list[str] = []
    head = dom.get("headline", domain)
    out.append(f"## {head}" if markdown else head.upper())
    for sec in dom.get("sections", []):
        out.append(f"### {sec['title']}" if markdown else f"  {sec['title']}")
        for line in sec.get("lines", []):
            out.append(f"- {line}" if markdown else f"    {line}")
    for w in dom.get("warnings", []):
        out.append(f"> {w}" if markdown else f"  note: {w}")
    out.append("_Observe-only — reassembled from source artifacts; no funded-action override._"
               if markdown else "  Observe-only — no funded-action override.")
    out.append("")
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_memo_datasets.py::test_render_domain_brief_populated_and_empty -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/memo_datasets.py tests/test_memo_datasets.py
git commit -m "feat(memo): Task 2 — render_domain_brief"
```

---

### Task 3: `run_memo_datasets` — loader/runner + artifact + briefs

**Files:**
- Modify: `portfolio_automation/memo_datasets.py`
- Test: `tests/test_memo_datasets.py`

**Interfaces:**
- Produces: `run_memo_datasets(root: str = ".", *, write: bool = True, config: dict | None = None) -> dict` — loads the source artifacts from `outputs/latest/` + `config/base.json:memo_datasets`, builds the dataset, and (when `write`) writes `outputs/latest/memo_datasets.json` and `outputs/latest/memo/<domain>_brief.md`. Never raises; returns the dataset (with `status:"error"` on failure).

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path


def _seed_latest(tmp_path):
    latest = tmp_path / "outputs" / "latest"; latest.mkdir(parents=True)
    (latest / "daily_capital_plan.json").write_text(json.dumps(
        {"available": True, "capital_summary": {"funded_capital":
        {"amount": 104.0, "state": "confirmed"}, "funded_count": 2, "deferred_count": 20},
         "bottom_line": "Deploy $104."}))
    (latest / "risk_delta.json").write_text(json.dumps(
        {"overall_status": "ok", "leverage": {"total_exposure": 0.145}}))
    return tmp_path


def test_run_writes_artifact_and_briefs(tmp_path):
    root = _seed_latest(tmp_path)
    res = md.run_memo_datasets(str(root), write=True)
    assert res["feeds_decision_engine"] is False
    art = json.loads((root / "outputs" / "latest" / "memo_datasets.json").read_text())
    assert set(art["domains"]) == set(md.DOMAINS)
    assert (root / "outputs" / "latest" / "memo" / "portfolio_brief.md").exists()
    # governance: decision_plan.json is never written by this producer
    assert not (root / "outputs" / "latest" / "decision_plan.json").exists()


def test_run_never_raises_on_garbage(tmp_path):
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    res = md.run_memo_datasets(str(tmp_path), write=False)
    assert res["feeds_decision_engine"] is False   # honest empty, no crash
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_memo_datasets.py::test_run_writes_artifact_and_briefs -v`
Expected: FAIL (`AttributeError: run_memo_datasets`).

- [ ] **Step 3: Write minimal implementation** (append to `memo_datasets.py`)

```python
import json
from pathlib import Path

# artifact stem -> outputs/latest filename
_SOURCE_FILES = {
    "daily_capital_plan": "daily_capital_plan.json",
    "system_decision_summary": "system_decision_summary.json",
    "decision_plan": "decision_plan.json",
    "risk_delta": "risk_delta.json",
    "correlation_risk_advisor": "correlation_risk_advisor.json",
    "unified_crowd_status": "unified_crowd_intelligence_status.json",
    "watch_candidates": "watch_candidates.json",
    "institutional_intelligence": "institutional_intelligence.json",
    "daily_run_status": "daily_run_status.json",
}


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_memo_datasets(root: str = ".", *, write: bool = True,
                      config: dict | None = None) -> dict:
    try:
        root_path = Path(root)
        if config is None:
            base = _load_json(root_path / "config" / "base.json") or {}
            config = base.get("memo_datasets") or {}
        domains = config.get("domains") or DOMAINS
        write_briefs = config.get("write_briefs", True)
        latest = root_path / "outputs" / "latest"
        sources = {k: _load_json(latest / fn) for k, fn in _SOURCE_FILES.items()}
        dataset = build_memo_datasets(sources, domains=domains)
        if write:
            from portfolio_automation.data_governance import OutputNamespace, safe_write_json
            base_dir = root_path / "outputs"
            safe_write_json(OutputNamespace.LATEST, "memo_datasets.json", dataset,
                            base_dir=base_dir)
            if write_briefs:
                from portfolio_automation.data_governance import safe_write_text
                for d in dataset["domains"]:
                    lines = render_domain_brief(dataset, d, markdown=True)
                    if lines:
                        safe_write_text(OutputNamespace.LATEST, f"memo/{d}_brief.md",
                                        "\n".join(lines), base_dir=base_dir)
        return dataset
    except Exception as exc:  # noqa: BLE001
        return {"schema_version": SCHEMA_VERSION, "source": "memo_datasets",
                "observe_only": True, "feeds_decision_engine": False,
                "status": "error", "error": str(exc), "domains": {}}
```

Note: confirm `safe_write_text` accepts a `memo/<name>` subpath under LATEST; if the namespace validator rejects a subdir, write briefs with a `memo_<domain>_brief.md` flat name instead (adjust the test path accordingly).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_memo_datasets.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/memo_datasets.py tests/test_memo_datasets.py
git commit -m "feat(memo): Task 3 — run_memo_datasets writer + briefs"
```

---

### Task 4: GUI domain sub-tabs

**Files:**
- Create: `gui_v2/data/dash_memo_datasets.py`
- Modify: `gui_v2/templates/dashboard/memo.html` (add a domain sub-tab bar reading the new view)
- Modify: `gui_v2/app.py` (pass the datasets view into the memo route ctx)
- Test: `tests/test_memo_datasets_gui.py`

**Interfaces:**
- Consumes: `outputs/latest/memo_datasets.json`.
- Produces: `collect_memo_datasets_view(root: Path) -> dict` returning `{"has_datasets": bool, "domains": [{"key": str, "headline": str, "status": str, "sections": [...]}]}`; null-tolerant (absent artifact → `{"has_datasets": False, "domains": []}`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memo_datasets_gui.py
import json
from pathlib import Path
from gui_v2.data.dash_memo_datasets import collect_memo_datasets_view


def test_view_absent_is_null_tolerant(tmp_path):
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    v = collect_memo_datasets_view(tmp_path)
    assert v["has_datasets"] is False and v["domains"] == []


def test_view_shapes_domains(tmp_path):
    latest = tmp_path / "outputs" / "latest"; latest.mkdir(parents=True)
    (latest / "memo_datasets.json").write_text(json.dumps({
        "feeds_decision_engine": False, "domains": {
            "portfolio": {"headline": "Portfolio & Capital", "status": "ok",
                          "sections": [{"title": "Bottom Line", "lines": ["x"], "severity": "info"}],
                          "warnings": []},
            "risk": {"headline": "Risk", "status": "unavailable", "sections": [], "warnings": []}}}))
    v = collect_memo_datasets_view(tmp_path)
    assert v["has_datasets"] is True
    keys = {d["key"] for d in v["domains"]}
    assert "portfolio" in keys
    port = next(d for d in v["domains"] if d["key"] == "portfolio")
    assert port["status"] == "ok" and port["sections"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_memo_datasets_gui.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write the loader**

```python
# gui_v2/data/dash_memo_datasets.py
"""Observe-only domain sub-tab view over outputs/latest/memo_datasets.json."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from gui_v2.data.shared import _read_json


def collect_memo_datasets_view(root: Path) -> dict[str, Any]:
    art = _read_json(Path(root) / "outputs" / "latest" / "memo_datasets.json")
    if not art or not art.get("domains"):
        return {"has_datasets": False, "domains": [], "feeds_decision_engine": False}
    domains = []
    for key, dom in art["domains"].items():
        domains.append({"key": key, "headline": dom.get("headline", key),
                        "status": dom.get("status", "unavailable"),
                        "sections": dom.get("sections", []),
                        "warnings": dom.get("warnings", [])})
    return {"has_datasets": True, "domains": domains,
            "feeds_decision_engine": False, "generated_at": art.get("generated_at")}
```

- [ ] **Step 4: Run loader tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_memo_datasets_gui.py -v`
Expected: PASS.

- [ ] **Step 5: Wire the view into the memo route**

In `gui_v2/app.py`, in `page_dash_memo` (route `/dashboard/memo`), merge the datasets view into the context:

```python
from gui_v2.data.dash_memo_datasets import collect_memo_datasets_view
# inside page_dash_memo, before _render:
ctx = _with_operator(_dash_memo(REPO_ROOT), "memo")
ctx["memo_datasets"] = collect_memo_datasets_view(REPO_ROOT)
return _render(request, "dashboard/memo.html", **ctx)
```

- [ ] **Step 6: Add the sub-tab bar to `memo.html`**

Append a domain sub-tab section (guarded by `{% if memo_datasets and memo_datasets.has_datasets %}`) that loops `memo_datasets.domains`, rendering a tab per domain with its `headline`, `status` badge, and section lines. Keep the existing memo sections above it (additive). Include a visible "observe-only · feeds_decision_engine=false" note.

- [ ] **Step 7: Smoke-render the memo template under the app env**

Run:
```bash
.venv/bin/python -c "from gui_v2.app import templates; print(templates.get_template('dashboard/memo.html').name, 'compiles')"
```
Expected: prints `dashboard/memo.html compiles`.

- [ ] **Step 8: Run the GUI regression + commit**

Run: `.venv/bin/python -m pytest -q tests/gui_v2/ tests/test_memo_datasets_gui.py`
Expected: all PASS.
```bash
git add gui_v2/data/dash_memo_datasets.py gui_v2/app.py gui_v2/templates/dashboard/memo.html tests/test_memo_datasets_gui.py
git commit -m "feat(memo): Task 4 — GUI domain sub-tabs over memo_datasets"
```

---

### Task 5: Wiring — pipeline stage, config, registry, preflight, health

**Files:**
- Modify: `scripts/run_daily_safe.sh` (new Stage 10c after Stage 10)
- Modify: `config/base.json` (add `memo_datasets` block)
- Modify: `portfolio_automation/artifact_registry.yaml` (register `memo_datasets.json`)
- Modify: `scripts/preflight.sh` (compile + import lists)
- Modify: `.claude/commands/daily-tool-analysis.md` (one-line read-only consumer heartbeat)
- Modify: `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/CHANGELOG_DECISIONS.md`

**Interfaces:** consumes `run_memo_datasets` from Task 3.

- [ ] **Step 1: Add the config block** to `config/base.json` (before the closing brace of the last block):

```json
  "memo_datasets": {
    "enabled": true,
    "domains": ["portfolio", "crowd_watchlist", "institutional", "risk", "system"],
    "write_briefs": true
  }
```
Validate: `.venv/bin/python -c "import json; json.load(open('config/base.json')); print('ok')"`

- [ ] **Step 2: Add Stage 10c** to `scripts/run_daily_safe.sh`, immediately AFTER the Stage 10 "Daily memo" block:

```bash
# Stage 10c — Memo datasets (observe-only): reassemble the memo-producer
# artifacts into per-domain datasets + briefs for the GUI sub-tabs. Runs after
# Stage 10 so the memo artifacts are fresh. Non-blocking; feeds_decision_engine=false.
run_aux_stage "Memo datasets" \
    python -c "import os; os.chdir('${REPO_ROOT}'); from portfolio_automation.memo_datasets import run_memo_datasets; r = run_memo_datasets('.'); print('domains:', list((r.get('domains') or {}).keys()), 'feeds_decision_engine:', r.get('feeds_decision_engine'))"
```
Validate: `bash -n scripts/run_daily_safe.sh`

- [ ] **Step 3: Register the artifact** in `portfolio_automation/artifact_registry.yaml` (near the other `outputs/latest` narrative artifacts):

```yaml
  memo_datasets.json:
    path: outputs/latest/memo_datasets.json
    label: memo domain datasets
    lens: decision_core
    role: narrative
    required: false
    cadence: daily
    producer: memo_datasets
    consumers: [gui_operator_data, daily-tool-analysis]
    severity_if_missing: info
    consumer_status: consumed
    notes: Observe-only per-domain reassembly (portfolio/crowd_watchlist/institutional/risk/system) of the memo-producer artifacts. Source of truth for per-domain briefs + GUI sub-tabs. Stage 10c. feeds_decision_engine=false.
```
Validate: `.venv/bin/python -c "from portfolio_automation.artifact_registry import validate_registry; validate_registry(); print('registry ok')"` (or the repo's registry-validation test).

- [ ] **Step 4: Add to preflight** `scripts/preflight.sh` compile list (`portfolio_automation/memo_datasets.py`) and the import smoke list (`portfolio_automation.memo_datasets`). Validate: `bash -n scripts/preflight.sh`.

- [ ] **Step 5: Add a read-only heartbeat** to `.claude/commands/daily-tool-analysis.md` — a one-line body item reading `memo_datasets.json` domain statuses (e.g. `Memo-datasets: {n} domains · {ok}/{unavailable}`), documented as observe-only (no dispatch/mutation). Also add a content-liveness note: fresh but all domains `unavailable` while sources exist → warn.

- [ ] **Step 6: Docs** — add the `memo_datasets.json` contract to `docs/OUTPUT_ARTIFACT_CONTRACTS.md` and a `docs/CHANGELOG_DECISIONS.md` entry (area: output_contract; invariants: observe-only, feeds_decision_engine=false, daily_memo unchanged).

- [ ] **Step 7: End-to-end live smoke** (real repo):

Run:
```bash
.venv/bin/python -c "from portfolio_automation.memo_datasets import run_memo_datasets; r=run_memo_datasets('.'); import json; print(json.dumps({k:v['status'] for k,v in r['domains'].items()}))"
ls outputs/latest/memo_datasets.json outputs/latest/memo/ 2>/dev/null
```
Expected: prints a status per domain; artifact + brief files exist; `decision_plan.json` mtime unchanged.

- [ ] **Step 8: Full regression + commit**

Run: `.venv/bin/python -m pytest -q tests/test_memo_datasets.py tests/test_memo_datasets_gui.py tests/test_daily_memo.py tests/gui_v2/ tests/test_artifacts_registry.py`
Expected: all PASS (existing memo/GUI unaffected).
```bash
git add scripts/run_daily_safe.sh config/base.json portfolio_automation/artifact_registry.yaml scripts/preflight.sh .claude/commands/daily-tool-analysis.md docs/OUTPUT_ARTIFACT_CONTRACTS.md docs/CHANGELOG_DECISIONS.md
git commit -m "feat(memo): Task 5 — wiring (Stage 10c, config, registry, preflight, docs, health)"
```

---

## Self-Review

**Spec coverage:** dataset source-of-truth (Task 1) ✓ · render briefs (Task 2) ✓ · runner + artifact + brief files (Task 3) ✓ · GUI sub-tabs (Task 4) ✓ · pipeline/config/registry/preflight/health/docs (Task 5) ✓ · combined `daily_memo.md` untouched (no task modifies `daily_memo.py`) ✓ · 5 domains + section shape ✓ · observe-only / feeds_decision_engine=false / no decision_plan write (tested in Tasks 1 & 3) ✓.

**Placeholder scan:** Step 6 of Task 4 and Steps 5–6 of Task 5 describe HTML/skill/doc edits prose-style rather than full literal content — these are template/markdown edits where the exact surrounding file context must be read at execution time; the required fields + guards + validation commands are specified. All code steps carry complete code.

**Type consistency:** `build_memo_datasets(sources, domains=…)` / `render_domain_brief(dataset, domain, markdown=…)` / `run_memo_datasets(root, write=…, config=…)` / `collect_memo_datasets_view(root)` and the domain/section shapes (`{headline,status,sections,source_artifacts,warnings}`, `{title,lines,severity}`) are consistent across all tasks.
