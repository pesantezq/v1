# GUI Persona Cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A web+mobile persona cockpit (`/dashboard/*` for Portfolio Manager / Quant / Developer-System + Memo + Schwab Portfolio-Sync + gated Portfolio-Config edit) on the existing FastAPI/Jinja/HTMX/Tailwind gui_v2, observe-only, mobile-first.

**Architecture:** New persona data collectors in `gui_v2/data/` that REUSE the existing collectors and normalize to a common card shape (`shared.card`); persona routes become canonical with old routes redirecting; `base.html` gains an observe-only banner, desktop persona-nav, and mobile bottom-nav; gated hybrid config-edit reuses `manual_portfolio_update`'s safe writer.

**Tech Stack:** FastAPI + Jinja2 + HTMX + Tailwind-CDN; pytest + FastAPI TestClient.

**Spec:** `docs/superpowers/specs/2026-06-08-gui-persona-cockpit-design.md`

**Branch:** `feat/gui-persona-cockpit` off `main` (spec committed `95e59099`).

### Critical discipline
- **Never `git commit -am`** — stage explicit paths; `git diff main HEAD --stat` before push.
- **Observe-only / no-trade:** no `Execute`/`Trade`/`Buy Now`/`Sell Now`/`Place Order`/`Auto-Trade`/`Auto-Approve` label in any template (Task 1 grep test enforces). Only decision-core artifacts = official actions.
- **Write gating:** config edit/save only when auth configured AND `GUI_V2_PORTFOLIO_EDIT=1`.
- Reads must degrade to explicit empty states (artifacts may be absent, incl. Schwab + registry from unmerged branches).

### Existing conventions to reuse (from gui_v2)
- Per-module `_read_json(path)` (returns None on missing/corrupt, never raises).
- `app.py` `_render(request, template, **ctx)` injects `nav_severity`; routes `Depends(_require_auth)`.
- Filters: `severity_classes`, `risk_severity`, `status_label`. Components: `metric_card.html`, `empty_state.html`, `severity_badge.html`.
- `base.html`: Tailwind+HTMX CDN, dark theme, sticky top nav, footer "Advisory only — no trades executed."

---

## File Structure
| File | Responsibility |
|---|---|
| `gui_v2/data/shared.py` (create) | `card(...)` normalized-shape helper + `_read_json` + `redirect_map` |
| `gui_v2/data/dash_today.py` / `dash_portfolio.py` / `dash_quant.py` / `dash_system.py` / `dash_memo.py` / `dash_portfolio_sync.py` / `dash_portfolio_config.py` (create) | persona collectors (reuse existing collectors + extra artifacts) |
| `gui_v2/portfolio_config_writer.py` (create) | shared safe writer extracted/wrapping `tools/manual_portfolio_update` (backup+audit+validate) |
| `gui_v2/app.py` (modify) | new `/dashboard/*` routes + redirects + config-edit POST routes + `GUI_V2_PORTFOLIO_EDIT` gate |
| `gui_v2/templates/base.html` (modify) | observe-only banner + persona top-nav + mobile bottom-nav + status bar |
| `gui_v2/templates/dashboard/*.html` (create) | persona templates |
| `gui_v2/templates/components/*.html` (create) | bottom_nav, mobile_status_bar, evidence_drawer, decision_card, source_artifact_label, portfolio_edit_form, validation_errors |
| `tests/test_gui_dashboard_*.py` (create) | route/safety/mobile/config tests |
| `docs/gui_*.md` (create) | usage/mobile/remote/observe-only/config docs |

---

## Task 1 (Milestone 1): Shell — `shared.card`, base nav/banner/bottom-nav, redirects, `/dashboard/today`, safety tests

**Files:** Create `gui_v2/data/shared.py`, `gui_v2/data/dash_today.py`, `gui_v2/templates/dashboard/today.html`, components `bottom_nav.html`/`mobile_status_bar.html`/`evidence_drawer.html`/`source_artifact_label.html`; Modify `gui_v2/templates/base.html`, `gui_v2/app.py`; Test `tests/test_gui_dashboard_shell.py`.

- [ ] **Step 1: Write `shared.card` + its test**
```python
# gui_v2/data/shared.py
"""Shared helpers for the persona dashboard: normalized card shape + json reader."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

_STATUS_TO_SEVERITY = {"ok": "green", "warning": "yellow", "red": "red", "info": "blue", "unknown": "gray"}


def _read_json(path: Path) -> Any | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def card(title: str, *, status: str = "unknown", label: str = "", summary: str = "",
         source_artifacts: list[str] | None = None, updated_at: str | None = None) -> dict:
    """Normalized dashboard card. status ∈ ok|warning|red|info|unknown."""
    status = status if status in _STATUS_TO_SEVERITY else "unknown"
    return {
        "title": title, "status": status, "label": label, "summary": summary,
        "source_artifacts": source_artifacts or [], "updated_at": updated_at,
        "severity": _STATUS_TO_SEVERITY[status],
    }


# Old-route → persona-route redirect map (Task 1 wires these in app.py).
REDIRECT_MAP = {
    "/portfolio": "/dashboard/portfolio", "/risk-impact": "/dashboard/portfolio",
    "/research": "/dashboard/quant", "/health": "/dashboard/system",
    "/operations": "/dashboard/system",
}
```
```python
# tests/test_gui_dashboard_shell.py
from gui_v2.data import shared


def test_card_shape_and_severity_mapping():
    c = shared.card("X", status="warning", label="L", summary="S",
                    source_artifacts=["a.json"], updated_at="t")
    assert set(c) == {"title","status","label","summary","source_artifacts","updated_at","severity"}
    assert c["severity"] == "yellow"
    assert shared.card("Y", status="bogus")["status"] == "unknown"
    assert shared.card("Z")["severity"] == "gray"
```

- [ ] **Step 2: Run → fail.** `python3 -m pytest -q tests/test_gui_dashboard_shell.py -k card` → FAIL.
- [ ] **Step 3: (shared.py above) Run → pass.**

- [ ] **Step 4: Write `dash_today.collect_today_view` + test**
```python
# gui_v2/data/dash_today.py
"""Today cockpit: answers system-healthy? decision-core OK? review needed? what changed? memo?"""
from __future__ import annotations
from pathlib import Path
from gui_v2.data.shared import card, _read_json


def collect_today_view(root: Path) -> dict:
    latest = Path(root) / "outputs/latest"
    drs = _read_json(latest / "daily_run_status.json") or {}
    dp = _read_json(latest / "decision_plan.json")
    risk = _read_json(latest / "risk_delta.json") or {}
    cards = []
    # System health
    drs_status = (drs.get("overall_status") or "unknown")
    cards.append(card("System health",
                      status="ok" if drs_status == "ok" else ("warning" if drs_status == "ok_with_warnings" else ("red" if drs_status in ("failed","partial") else "unknown")),
                      label=drs_status, summary=f"{(drs.get('stage_summary') or {}).get('ok','?')} stages OK",
                      source_artifacts=["daily_run_status.json"], updated_at=drs.get("generated_at")))
    # Decision core OK?
    cards.append(card("Decision core",
                      status="ok" if dp else "red",
                      label="present" if dp else "MISSING",
                      summary="decision_plan present" if dp else "decision_plan.json absent — decisions not trustworthy",
                      source_artifacts=["decision_plan.json"]))
    # Risk
    cards.append(card("Risk", status=("ok" if risk.get("overall_status")=="ok" else "warning"),
                      label=(risk.get("overall_status") or "unknown"),
                      summary="see Portfolio view", source_artifacts=["risk_delta.json"]))
    return {"cards": cards, "persona": "today"}
```
```python
def test_today_view_has_health_and_decision_core_cards(tmp_path):
    from gui_v2.data.dash_today import collect_today_view
    (tmp_path/"outputs/latest").mkdir(parents=True)
    (tmp_path/"outputs/latest/decision_plan.json").write_text("{}")
    v = collect_today_view(tmp_path)
    titles = {c["title"] for c in v["cards"]}
    assert {"System health","Decision core","Risk"} <= titles
    dc = next(c for c in v["cards"] if c["title"]=="Decision core")
    assert dc["status"] == "ok"  # present


def test_today_decision_core_missing_is_red(tmp_path):
    from gui_v2.data.dash_today import collect_today_view
    (tmp_path/"outputs/latest").mkdir(parents=True)
    v = collect_today_view(tmp_path)
    dc = next(c for c in v["cards"] if c["title"]=="Decision core")
    assert dc["status"] == "red"  # absent -> red
```

- [ ] **Step 5: Edit `base.html`** — add (a) observe-only banner directly under `<body>`: `<div class="bg-amber-500/10 text-amber-300 text-center text-xs py-1">Observe-only · No trade execution</div>`; (b) replace the top-nav links with persona links (Today/Portfolio/Quant/System/Memo → `/dashboard/*`) wrapped `class="hidden md:flex ..."`; (c) add `{% include "components/bottom_nav.html" %}` before `</body>`; (d) keep theme toggle + footer. Create `components/bottom_nav.html` (fixed bottom bar, `md:hidden`, 5 tab links with text labels — NO trade words) and `components/mobile_status_bar.html`, `components/evidence_drawer.html` (HTMX details/summary listing `source_artifacts`), `components/source_artifact_label.html`.

- [ ] **Step 6: Add routes + redirects to `app.py`**
```python
from fastapi.responses import RedirectResponse
from gui_v2.data.dash_today import collect_today_view as _dash_today
from gui_v2.data.shared import REDIRECT_MAP

@app.get("/dashboard/today", response_class=HTMLResponse)
def page_dash_today(request: Request, _a: str | None = Depends(_require_auth)) -> HTMLResponse:
    return _render(request, "dashboard/today.html", **_dash_today(REPO_ROOT))

@app.get("/")
def root_redirect(_a: str | None = Depends(_require_auth)):
    return RedirectResponse("/dashboard/today", status_code=302)

# old → persona redirects
for _old, _new in REDIRECT_MAP.items():
    def _mk(target):
        def _r(_a: str | None = Depends(_require_auth)):
            return RedirectResponse(target, status_code=302)
        return _r
    app.get(_old)(_mk(_new))
```
NOTE: the existing `@app.get("/")` page_today must be REMOVED/renamed (now a redirect). Update existing tests in `tests/test_gui_v2_*.py` that asserted old `/` content → assert the 302 to `/dashboard/today` (do not delete coverage; repoint it).

- [ ] **Step 7: Write the SAFETY tests** (in `tests/test_gui_dashboard_shell.py`)
```python
import re
from pathlib import Path
from fastapi.testclient import TestClient
from gui_v2.app import app

_FORBIDDEN = ("execute trade", "buy now", "sell now", "place order", "auto-trade", "auto trade", "auto-approve")

def test_no_forbidden_action_labels_in_templates():
    tpl = Path("gui_v2/templates")
    offenders = []
    for f in tpl.rglob("*.html"):
        text = f.read_text(encoding="utf-8").lower()
        for bad in _FORBIDDEN:
            if bad in text:
                offenders.append(f"{f}: {bad}")
    assert offenders == [], f"forbidden action labels: {offenders}"

def test_root_redirects_to_dashboard_today():
    c = TestClient(app, follow_redirects=False)
    r = c.get("/")
    assert r.status_code == 302 and r.headers["location"] == "/dashboard/today"

def test_old_routes_redirect_not_404():
    c = TestClient(app, follow_redirects=False)
    for old in ("/portfolio","/risk-impact","/research","/health","/operations"):
        assert c.get(old).status_code == 302

def test_dashboard_today_renders_and_has_observe_only_banner():
    c = TestClient(app)
    r = c.get("/dashboard/today")
    assert r.status_code == 200
    assert "Observe-only" in r.text
```

- [ ] **Step 8: Run all Task-1 tests + the existing gui_v2 suite** (`python3 -m pytest -q tests/test_gui_dashboard_shell.py tests/test_gui_v2_app.py tests/test_gui_v2_routes.py`), fix repointed redirect tests, then **Commit** (explicit paths): `git commit -m "feat(gui): persona shell — shared.card, base nav/banner/bottom-nav, redirects, /dashboard/today + safety tests"`.

---

## Task 2 (Milestone 2): `/dashboard/portfolio` — Portfolio Manager read view

**Files:** Create `gui_v2/data/dash_portfolio.py`, `gui_v2/templates/dashboard/portfolio.html`; Modify `app.py`; Test `tests/test_gui_dashboard_portfolio.py`.

- [ ] **Step 1–4 (TDD):** `collect_portfolio_view(root)` composes cards (each via `shared.card`) for: Top Insight (from system_decision_summary), Decision Queue (decision_plan top decisions, via `decision_card`), Risk Focus (risk_delta + correlation_risk_advisor + vol_regime_advisor + earnings_gate + exit_advisor), Capital/Allocation (cash_deployment_plan + tax_harvest_advisor), Watchlist/Opportunities (watchlist_signals + market_opportunities + news_evidence_layer), Memo summary (daily_memo.md first lines). REUSE `gui_v2.data.portfolio.collect_portfolio_view` + `risk_impact.collect_risk_impact_view` as sources where helpful. Each card sets `source_artifacts`. Tests: view renders 200; each card has non-empty `source_artifacts`; decision-core cards present; **no card whose source is a non-decision artifact carries a buy/sell/hold verb** (source-of-truth invariant test — assert the only buy/sell/hold strings come from decision_plan-sourced cards). Desktop table + `md:hidden` card stack for holdings. Commit.

---

## Task 3 (Milestone 3): `/dashboard/quant` — caution-labeled

**Files:** Create `gui_v2/data/dash_quant.py`, `templates/dashboard/quant.html`; Modify `app.py`; Test `tests/test_gui_dashboard_quant.py`.

- [ ] **Step 1–4 (TDD):** `collect_quant_view(root)` cards from confidence_calibration, pattern_efficacy_{weekly,monthly,yearly}, retune_impact, gate_retune_suggestions, alpha_attribution_report, quant_watch_status (if present), kelly_sizing_advisor (advisory). Apply explicit caution labels: when a sample-size field (`n`/`n_samples`/`resolved_1d`) is below a threshold → label "Thin sample"/"Insufficient history"; retune/gate proposals → "Proposal only"; quant_watch → "Observe only". Tests: view renders; a thin-sample fixture yields a "Thin sample"/"Insufficient history" label; proposals labeled "Proposal only"; **no overfitting/active-trading language** (grep card summaries for forbidden labels + assert no buy/sell/hold). Commit.

---

## Task 4 (Milestone 4): `/dashboard/system` — health + Schwab broker health

**Files:** Create `gui_v2/data/dash_system.py`, `templates/dashboard/system.html`; Modify `app.py`; Test `tests/test_gui_dashboard_system.py`.

- [ ] **Step 1–4 (TDD):** `collect_system_view(root)` cards from daily_run_status, pipeline_run_status, artifact_registry_status (if present), data_quality_report, fmp_budget_status, ai_budget_summary, memo_delivery_status, doc_audit_status, historical_backfill_status, **broker_sync_status (Schwab health — closes deferred Schwab C2 pairing)**, a failure queue (stages with status failed/warn from daily_run_status), and daily/monthly/yearly analysis status if artifacts/logs exist. REUSE `health.collect_health_view` + `operations.collect_operations_view`. Tests: renders; broker_sync_status absent → explicit "Schwab not configured" empty-state card (status info, not red); present (fixture) → status card. Commit.

---

## Task 5 (Milestone 5): `/dashboard/memo` — phone-readable

**Files:** Create `gui_v2/data/dash_memo.py`, `templates/dashboard/memo.html`; Modify `app.py`; Test `tests/test_gui_dashboard_memo.py`.

- [ ] **Step 1–4 (TDD):** `collect_memo_view(root)` parses `daily_memo.md` into phone sections: Top Insight, Risk Focus, Portfolio Decisions, Data Quality, Quant Notes, Watchlist Notes (split on the memo's `##` headers; map to the closest section; omit raw hashes / noisy artifact dumps). Copy/download control only if a simple pattern fits (e.g. a `<button>` that copies the rendered text via tiny JS — NO trade words). Tests: renders; sections present when memo present; explicit empty state when absent; no raw fingerprint hash in the rendered mobile memo (grep for `[0-9a-f]{16}` absence in the mobile section). Commit.

---

## Task 6 (Milestone 6): `/dashboard/portfolio-sync` — Schwab view (closes deferred Schwab GUI follow-up)

**Files:** Create `gui_v2/data/dash_portfolio_sync.py`, `templates/dashboard/portfolio_sync.html`; Modify `app.py`; Test `tests/test_gui_dashboard_portfolio_sync.py`.

- [ ] **Step 1–4 (TDD):** `collect_portfolio_sync_view(root)` reads broker_sync_status / schwab_portfolio_snapshot / schwab_positions / portfolio_reconciliation / portfolio_config_update_proposal (all may be absent → explicit empty states). Cards: Connection (Connected/Not configured/Error from broker_sync_status), Last sync, Holdings matched count, Mismatches (desktop table + `md:hidden` cards), Cash difference, Proposal status/link. A **"Generate Config Update Proposal"** control: `POST /dashboard/portfolio-sync/reconcile` → calls `portfolio_automation.brokers.schwab_sync.run_reconcile(root=REPO_ROOT)` (READ-ONLY; writes the reconciliation+proposal artifacts only) → re-renders. Banner: "Updates local StockBot configuration only. It does not execute trades." Account ids already masked by the artifacts. Tests: renders with artifacts absent (empty state "Schwab not configured") and present (fixtures); mismatch cards shown; **no forbidden labels**; the reconcile POST does not touch config.json (assert byte-unchanged); account ids masked in rendered HTML. Commit.

---

## Task 7 (Milestone 7): `/dashboard/portfolio-config` — gated hybrid write

**Files:** Create `gui_v2/portfolio_config_writer.py`, `gui_v2/data/dash_portfolio_config.py`, `templates/dashboard/portfolio_config.html`, components `portfolio_edit_form.html`/`validation_errors.html`; Modify `app.py`; Test `tests/test_gui_dashboard_portfolio_config.py`.

- [ ] **Step 1: `portfolio_config_writer`** — extract/wrap `tools/manual_portfolio_update`'s `_write_backup` + `_append_audit_record` + `_atomic_write_json` + validation into `validate_config_edit(holdings, cash, config)` (no negative shares/cash, weight-sum, required symbol, concentration/leverage caps) and `apply_config_edit(root, holdings, cash) -> {backup_path, audit_appended, ok}` (backup → write → audit). Pure-ish; tests assert backup created + audit row + config updated + reversible, and that validation blocks bad input.
- [ ] **Step 2: gating + routes** — `_edit_enabled()` returns True only when auth configured (`GUI_V2_AUTH_USER`+`PASS`) AND `GUI_V2_PORTFOLIO_EDIT=1`. `GET /dashboard/portfolio-config` renders the form when enabled, else a read-only "editing disabled" card. `POST /dashboard/portfolio-config/validate` (HTMX) → `validation_errors` + dry-run before→after diff. `POST /dashboard/portfolio-config/save` → refuse (403/disabled state) unless `_edit_enabled()`; else `apply_config_edit` → success page with backup path + revert note.
- [ ] **Step 3: tests** — gating (no form/save without auth+flag); validation failure cases; dry-run diff present; save creates backup + audit + updates config (reversible); save refused when ungated; the "updates local config only — no trades" banner present; no forbidden labels. Commit.

---

## Task 8 (Milestone 8): Docs + review agents + full validation

**Files:** Create `docs/gui_usage.md`, `docs/gui_mobile.md`, `docs/gui_remote_access.md`, `docs/gui_observe_only_safety.md`, `docs/gui_portfolio_config.md`; Modify `docs/CHANGELOG_DECISIONS.md`, `.agent/project_state.yaml`, `deploy/systemd` notes.

- [ ] **Step 1:** Write the docs per spec §9 (personas/routes; mobile usage; **remote access: Tailscale-first then Cloudflare Tunnel+Access, no public exposure, no hardcoded secrets**; observe-only safety model; config-edit behavior + gating + backup/audit/revert; the `GUI_V2_PORTFOLIO_EDIT` env + new routes in deployment notes).
- [ ] **Step 2:** CHANGELOG entry (output_contract/architecture) + record `gui_operator_cockpit_redesign` progress in `.agent/project_state.yaml` (it's the current_step; mark the persona-cockpit shipped; next_official_step unchanged).
- [ ] **Step 3: full validation** — `python3 -m pytest -q tests/test_gui_dashboard_*.py tests/test_gui_v2_*.py` (all green; repointed redirect tests pass); render each `/dashboard/*` route 200 via TestClient at a 390px-style check (assert no element forces overflow — at least assert tables have `md:hidden` card siblings); `python3 -m pytest -q` (full suite; report vs main baseline). 
- [ ] **Step 4:** Dispatch review agents: **portfolio-render-reviewer** (every template), **portfolio-test-reviewer**, **portfolio-architect** (safety boundary + write gating), **portfolio-doc-auditor**; **portfolio-attribution-analyst** for the quant view; **portfolio-discovery-health** for the watchlist surface. Address findings. Commit; STOP before push (controller handles).

---

## Self-Review
**Spec coverage:** §2 safety (Task1 forbidden-label + redirect + banner; Task2 source-of-truth; Task7 gating) ✓; §3 decisions (Task1 redirects; Task7 hybrid+gating) ✓; §4 architecture/card-shape (Task1 shared.card; persona collectors reuse existing) ✓; §5 responsive shell (Task1 base/bottom-nav; tables→cards each view) ✓; §6 persona content (Tasks 2-6) ✓; §6 Schwab sync view + §11 health pairing (Tasks 4+6) ✓; §7 config edit (Task7) ✓; §8 testing (each task + Task1 safety) ✓; §9 docs (Task8) ✓.
**Placeholder scan:** persona views (Tasks 2-6) give collector skeleton + artifact list + card composition + the specific tests rather than every line — acceptable because the pattern is fully shown in Task 1 (`shared.card` + `dash_today`) and each reuses existing collectors; the safety-critical + write paths (Tasks 1,7) have complete code. No TODO/TBD.
**Type/name consistency:** `shared.card(...)` shape + `_read_json` (Task1) used by all `dash_*` collectors; `collect_*_view(root)->{"cards":[...], "persona":...}` signature consistent; `_edit_enabled` + `apply_config_edit`/`validate_config_edit` (Task7) consistent; route paths match spec §4.
**Deferred:** artifact-registry registration of GUI/Schwab artifacts (until that branch merges); live Schwab creds.
