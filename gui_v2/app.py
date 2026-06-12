"""GUI v2 — FastAPI application."""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="StockBot Dashboard v2")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.on_event("startup")
def _stamp_running_sha() -> None:
    """Record the git SHA this dashboard process is serving (for deploy status)."""
    try:
        from gui_v2.data.deploy_status import write_running_sha
        write_running_sha(REPO_ROOT)
    except Exception:
        pass


def _deploy_apply_enabled() -> bool:
    """Phase C gate for the one-click 'Apply update' button. Default OFF.

    Requires GUI_V2_DEPLOY_APPLY=1 AND no kill-switch file. Mirrors the
    autonomous-worker gating: detection/Mode-B work without this; only the
    privileged self-restart apply is gated.
    """
    if (REPO_ROOT / "config" / "dashboard_update.DISABLED").exists():
        return False
    return os.environ.get("GUI_V2_DEPLOY_APPLY", "").strip() == "1"


# ---------------------------------------------------------------------------
# Optional HTTP basic auth
# ---------------------------------------------------------------------------
#
# When BOTH GUI_V2_AUTH_USER and GUI_V2_AUTH_PASS are set in the environment,
# every route requires HTTP Basic credentials matching those values. When
# either is unset, the dashboard is open (current default behavior).
#
# Credentials are compared with constant-time comparison so the response
# time does not leak whether the username matches.
# ---------------------------------------------------------------------------

# auto_error=False so the dependency runs even when no Authorization header
# is sent.  This lets us decide at request time whether auth is required.
_security = HTTPBasic(auto_error=False)


def _require_auth(
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> str | None:
    """
    Route gate. Returns the username when auth succeeds OR when auth is
    not configured (open mode).  Raises 401 when auth IS configured and
    the credentials are missing/wrong.

    Decision happens at request time so the operator can flip env vars
    without restarting the process (though systemd-managed env requires
    a restart — this still tests cleanly under monkeypatch).
    """
    expected_user = os.environ.get("GUI_V2_AUTH_USER", "").strip()
    expected_pass = os.environ.get("GUI_V2_AUTH_PASS", "").strip()
    auth_enabled = bool(expected_user and expected_pass)

    if not auth_enabled:
        return None  # open mode

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="StockBot Dashboard"'},
        )

    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        expected_user.encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        expected_pass.encode("utf-8"),
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="StockBot Dashboard"'},
        )
    return credentials.username


_SEVERITY_PALETTE = {
    "OK":   "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30",
    "INFO": "bg-sky-500/15 text-sky-300 border border-sky-500/30",
    "WARN": "bg-amber-500/15 text-amber-300 border border-amber-500/30",
    "FAIL": "bg-rose-500/15 text-rose-300 border border-rose-500/30",
}
_SEVERITY_NEUTRAL = "bg-zinc-800 text-zinc-300 border border-zinc-700"


def _severity_classes(severity: str) -> str:
    return _SEVERITY_PALETTE.get((severity or "").upper(), _SEVERITY_NEUTRAL)


templates.env.filters["severity_classes"] = _severity_classes


def _risk_severity(status: str | None) -> str:
    """Map risk_delta-style status strings to the OK/WARN/FAIL palette."""
    s = (status or "").strip().lower()
    return {
        "ok":               "OK",
        "ok_with_warnings": "INFO",
        "news_empty":       "INFO",
        "near_cap":         "WARN",
        "partial":          "WARN",
        "warn":             "WARN",
        "breach":           "FAIL",
        "exhausted":        "FAIL",
        "failed":           "FAIL",
        "unknown":          "INFO",
    }.get(s, "INFO")


templates.env.filters["risk_severity"] = _risk_severity


# Mapping of raw snake_case enum values to human-readable labels.
# Union of the memo-readability pass (D-M1) and the persona-cockpit (M3) label sets.
_STATUS_LABEL_MAP: dict[str, str] = {
    "ok":               "OK",
    "ok_with_warnings": "OK · warnings",
    "news_empty":       "No news",
    "near_cap":         "Near cap",
    "partial":          "Partial",
    "warn":             "Warning",
    "breach":           "Breach",
    "exhausted":        "Exhausted",
    "failed":           "Failed",
    "degraded":         "Degraded",
    "coverage_gap":     "Coverage gap",
    "unconfigured":     "Unconfigured",
    "not_configured":   "Not configured",
    "unknown":          "Unknown",
}


def _status_label(value: str | None) -> str:
    """Humanize a raw snake_case status enum to operator-readable text.

    Known enums are mapped explicitly; unknown/empty values fall back to a
    title-cased form with underscores replaced by spaces. Merges the
    memo-readability (D-M1) and persona-cockpit (M3) label sets.
    """
    if not value:
        return ""
    mapped = _STATUS_LABEL_MAP.get(str(value).strip().lower())
    if mapped is not None:
        return mapped
    # Fallback: title-case, replace underscores
    return str(value).replace("_", " ").title()


templates.env.filters["status_label"] = _status_label


def _human_time(value: str | None) -> str:
    """Humanize a raw timestamp for display (presentation-only).

    Accepts ISO-8601 datetimes ("2026-06-08T09:00:00", with or without a
    trailing "Z"/offset) or date-only strings ("2026-06-08") as emitted by the
    artifact ``generated_at`` fields. Returns a compact, scannable label like
    "Jun 08, 09:00" (datetime) or "Jun 08, 2026" (date-only).

    On any parse failure the raw input is returned unchanged so nothing is ever
    hidden from the operator. Empty/None yields "". The raw value remains
    available to templates for a tooltip (``title=``) alongside this label.
    """
    if not value:
        return ""
    raw = str(value).strip()
    from datetime import datetime

    iso = raw.replace("Z", "+00:00")
    # Full datetime first, then date-only.
    try:
        dt = datetime.fromisoformat(iso)
        # Date-only inputs parse to midnight; show just the date in that case.
        if "T" not in raw and " " not in raw:
            return dt.strftime("%b %d, %Y")
        return dt.strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        pass
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
        return dt.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return raw


templates.env.filters["human_time"] = _human_time


def _overall_severity_for_nav() -> str:
    """Compute overall severity for the nav badge. Best-effort; never raises."""
    try:
        from gui_v2.data.health import collect_health_view, overall_severity
        return overall_severity(collect_health_view(REPO_ROOT))
    except Exception:
        return "INFO"


def _render(request: Request, template_name: str, **context) -> HTMLResponse:
    ctx = {"nav_severity": _overall_severity_for_nav()}
    ctx.update(context)
    return templates.TemplateResponse(request, template_name, ctx)


from gui_v2.data.today import collect_today_view
from gui_v2.data.health import collect_health_view, overall_severity
from gui_v2.data.portfolio import collect_portfolio_view
from gui_v2.data.research import collect_research_view
from gui_v2.data.operations import collect_operations_view
from gui_v2.data.risk_impact import collect_risk_impact_view
from gui_v2.data.dash_today import collect_today_view as _dash_today
from gui_v2.data.dash_portfolio import collect_portfolio_view as _dash_portfolio
from gui_v2.data.dash_quant import collect_quant_view as _dash_quant
from gui_v2.data.dash_system import collect_system_view as _dash_system
from gui_v2.data.dash_memo import collect_memo_view as _dash_memo
from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view as _dash_portfolio_sync
from gui_v2.data.dash_portfolio_config import collect_portfolio_config_view as _dash_portfolio_config
from gui_v2.data.operator_control import operator_control_context, today_operator_summary
from gui_v2.data.shared import REDIRECT_MAP


# ---------------------------------------------------------------------------
# Portfolio config edit gating
# ---------------------------------------------------------------------------


def _edit_enabled() -> bool:
    """
    Returns True only when BOTH auth env vars are set AND
    GUI_V2_PORTFOLIO_EDIT=1.  Evaluated at request time so env changes
    (with process restart) take effect without code changes.
    """
    expected_user = os.environ.get("GUI_V2_AUTH_USER", "").strip()
    expected_pass = os.environ.get("GUI_V2_AUTH_PASS", "").strip()
    auth_configured = bool(expected_user and expected_pass)
    edit_flag = os.environ.get("GUI_V2_PORTFOLIO_EDIT", "").strip() == "1"
    return auth_configured and edit_flag


def _parse_holdings_from_form(form_data) -> list[dict[str, Any]]:
    """
    Parse multi-value form data into a list of holding dicts.

    The form sends parallel arrays: ``symbol[]``, ``shares[]``, etc.
    FastAPI FormData supports ``getlist`` to retrieve all values for a key.
    """
    symbols = form_data.getlist("symbol")
    shares_list = form_data.getlist("shares")
    target_weights = form_data.getlist("target_weight")
    asset_classes = form_data.getlist("asset_class")
    leverage_factors = form_data.getlist("leverage_factor")

    # is_leveraged checkboxes: keyed as is_leveraged_0, is_leveraged_1, ...
    # Build a set of indices that are checked
    checked_indices: set[int] = set()
    for key in form_data.keys():
        if key.startswith("is_leveraged_"):
            try:
                idx = int(key.split("_", 2)[2])
                checked_indices.add(idx)
            except (ValueError, IndexError):
                pass

    holdings: list[dict[str, Any]] = []
    for i, sym in enumerate(symbols):
        sym = str(sym or "").strip().upper()
        if not sym:
            continue

        def _get(lst, idx, default=""):
            try:
                return lst[idx] if idx < len(lst) else default
            except (IndexError, TypeError):
                return default

        shares_raw = _get(shares_list, i, "0")
        try:
            shares = float(shares_raw)
        except (TypeError, ValueError):
            shares = 0.0

        tw_raw = _get(target_weights, i, "")
        target_weight = None
        if str(tw_raw).strip() not in ("", "None"):
            try:
                target_weight = float(tw_raw)
            except (TypeError, ValueError):
                target_weight = None

        asset_class = str(_get(asset_classes, i, "us_equity")).strip() or "us_equity"

        lf_raw = _get(leverage_factors, i, "1")
        try:
            leverage_factor = int(float(lf_raw))
            if leverage_factor < 1:
                leverage_factor = 1
        except (TypeError, ValueError):
            leverage_factor = 1

        is_leveraged = i in checked_indices

        row: dict[str, Any] = {
            "symbol": sym,
            "shares": shares,
            "asset_class": asset_class,
            "is_leveraged": is_leveraged,
            "leverage_factor": leverage_factor,
        }
        if target_weight is not None:
            row["target_weight"] = target_weight

        holdings.append(row)

    return holdings


# ---------------------------------------------------------------------------
# Persona dashboard routes (canonical)
# ---------------------------------------------------------------------------


def _with_operator(view_ctx: dict[str, Any], view: str) -> dict[str, Any]:
    """Merge operator-control keys into a persona view context (additive, safe).

    Best-effort: any failure leaves the persona view untouched so the
    operator-control plane can never break an existing dashboard page.
    """
    try:
        view_ctx.update(operator_control_context(REPO_ROOT, view))
    except Exception:
        pass
    return view_ctx


@app.get("/dashboard/today", response_class=HTMLResponse)
def page_dash_today(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    ctx = _dash_today(REPO_ROOT)
    try:
        ctx.update(today_operator_summary(REPO_ROOT))
    except Exception:
        pass
    return _render(request, "dashboard/today.html", **ctx)


@app.get("/dashboard/portfolio", response_class=HTMLResponse)
def page_dash_portfolio(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    return _render(
        request, "dashboard/portfolio.html",
        **_with_operator(_dash_portfolio(REPO_ROOT), "portfolio"),
    )


@app.get("/dashboard/quant", response_class=HTMLResponse)
def page_dash_quant(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    return _render(
        request, "dashboard/quant.html",
        **_with_operator(_dash_quant(REPO_ROOT), "quant"),
    )


@app.get("/dashboard/system", response_class=HTMLResponse)
def page_dash_system(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    ctx = _with_operator(_dash_system(REPO_ROOT), "system")
    # Deploy status (auto-update detection) — read-only; surfaced on System.
    try:
        from gui_v2.data import deploy_status as _ds
        st = _ds.collect_deploy_status(REPO_ROOT)
        ctx["deploy_status"] = st
        ctx["deploy_card"] = _ds.deploy_card(st)
        ctx["deploy_apply_enabled"] = _deploy_apply_enabled()
    except Exception:
        pass
    return _render(request, "dashboard/system.html", **ctx)


@app.get("/dashboard/strategy-lab", response_class=HTMLResponse)
def page_dash_strategy_lab(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    # Next-stage research/strategy/improvement surface (observe-only, tolerant of
    # absent artifacts). Additive — does not alter the existing persona tabs.
    try:
        from gui_v2.data.dash_next_stage import collect_strategy_lab_view
        ctx = collect_strategy_lab_view(REPO_ROOT)
    except Exception:
        ctx = {"persona": "strategy_lab", "observe_only": True, "cards": []}
    return _render(request, "dashboard/strategy_lab.html", **ctx)


@app.get("/dashboard/crowd-radar", response_class=HTMLResponse)
def page_dash_crowd_radar(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    # Public Knowledge Velocity Layer (Crowd Radar): sandbox-only, observe-only
    # crowd-knowledge state classifier. Tolerant of absent / disabled / degraded
    # artifacts — never feeds the decision plan.
    try:
        from gui_v2.data.dash_crowd_radar import collect_crowd_radar_view
        ctx = collect_crowd_radar_view(REPO_ROOT)
    except Exception:
        ctx = {"persona": "crowd_radar", "observe_only": True, "cards": [],
               "sections": [], "warnings": [], "has_data": False}
    return _render(request, "dashboard/crowd_radar.html", **ctx)


@app.get("/dashboard/strategy-tax", response_class=HTMLResponse)
def page_dash_strategy_tax(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    # Tax/strategy hardening artifacts: scorecard, harvest advisor, strategy
    # comparison. Sandbox + latest namespaces only; observe-only; never feeds
    # the decision plan.
    try:
        from gui_v2.data.dash_strategy_tax import load_strategy_tax_context
        ctx = load_strategy_tax_context(REPO_ROOT / "outputs")
    except Exception:
        ctx = {"available": False, "observe_only": True,
               "scorecard": {}, "harvest": {}, "strategy": {}, "lots": {}}
    return _render(request, "dashboard/strategy_tax.html", **ctx)


@app.get("/dashboard/memo", response_class=HTMLResponse)
def page_dash_memo(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    return _render(
        request, "dashboard/memo.html",
        **_with_operator(_dash_memo(REPO_ROOT), "memo"),
    )


@app.get("/dashboard/portfolio-sync", response_class=HTMLResponse)
def page_dash_portfolio_sync(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    return _render(
        request,
        "dashboard/portfolio_sync.html",
        **_dash_portfolio_sync(REPO_ROOT),
    )


@app.post("/dashboard/portfolio-sync/reconcile", response_class=HTMLResponse)
def page_dash_portfolio_sync_reconcile(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    """
    Read-only reconcile: compares Schwab account to local config and writes the
    proposal artifact only.  Never mutates config.json.

    If the brokers module is not importable (this branch), returns the view with
    a "not installed" message and the button still disabled.
    """
    reconcile_message: str | None = None

    try:
        from portfolio_automation.brokers.schwab_sync import run_reconcile
        try:
            run_reconcile(root=REPO_ROOT)
            reconcile_message = "Reconcile completed. Proposal artifact updated."
        except Exception as exc:
            reconcile_message = f"Reconcile failed: {exc}"
    except ImportError:
        reconcile_message = (
            "Schwab sync layer not installed on this build "
            "(merge feat/schwab-readonly-sync)."
        )

    view = _dash_portfolio_sync(REPO_ROOT)
    view["reconcile_message"] = reconcile_message
    return _render(request, "dashboard/portfolio_sync.html", **view)


@app.get("/dashboard/portfolio-config", response_class=HTMLResponse)
def page_dash_portfolio_config(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    """
    GET /dashboard/portfolio-config

    When editing is enabled (auth + GUI_V2_PORTFOLIO_EDIT=1): renders the
    holdings + cash edit form.
    When editing is disabled: renders a read-only "editing disabled" state.
    """
    enabled = _edit_enabled()
    view = _dash_portfolio_config(REPO_ROOT, edit_enabled=enabled)
    return _render(request, "dashboard/portfolio_config.html", **view)


@app.post("/dashboard/portfolio-config/validate", response_class=HTMLResponse)
async def page_dash_portfolio_config_validate(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    """
    POST /dashboard/portfolio-config/validate (HTMX)

    Validates the submitted form data and returns a dry-run diff fragment.
    Never writes anything.
    """
    from gui_v2.portfolio_config_writer import validate_config_edit, diff_config_edit

    form_data = await request.form()
    holdings = _parse_holdings_from_form(form_data)

    cash_raw = form_data.get("cash_available", "0")
    try:
        cash = float(str(cash_raw).strip() or "0")
    except (TypeError, ValueError):
        cash = 0.0

    # Load config for cap checking
    config_path = REPO_ROOT / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except Exception:
        config = {}

    validation = validate_config_edit(holdings, cash, config)

    diff = None
    if validation["ok"]:
        diff = diff_config_edit(config, holdings, cash)

    ctx = {"validation": validation, "diff": diff}
    return templates.TemplateResponse(
        request,
        "components/validation_errors.html",
        ctx,
    )


@app.post("/dashboard/portfolio-config/save", response_class=HTMLResponse)
async def page_dash_portfolio_config_save(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    """
    POST /dashboard/portfolio-config/save

    Gated: if NOT _edit_enabled() → 403 refused (no write).
    If enabled: validate, backup, write, audit → success page.
    """
    from gui_v2.portfolio_config_writer import validate_config_edit, apply_config_edit

    if not _edit_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Portfolio config editing is disabled. "
                "Set GUI_V2_AUTH_USER, GUI_V2_AUTH_PASS, and "
                "GUI_V2_PORTFOLIO_EDIT=1 to enable."
            ),
        )

    form_data = await request.form()
    holdings = _parse_holdings_from_form(form_data)

    cash_raw = form_data.get("cash_available", "0")
    try:
        cash = float(str(cash_raw).strip() or "0")
    except (TypeError, ValueError):
        cash = 0.0

    # Load config for cap checking
    config_path = REPO_ROOT / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except Exception:
        config = {}

    # Validate before writing
    validation = validate_config_edit(holdings, cash, config)
    if not validation["ok"]:
        # Re-render the form with validation errors shown
        enabled = _edit_enabled()
        view = _dash_portfolio_config(REPO_ROOT, edit_enabled=enabled)
        view["save_result"] = {"ok": False, "error": "; ".join(validation["errors"])}
        return _render(request, "dashboard/portfolio_config.html", **view)

    # Apply
    result = apply_config_edit(REPO_ROOT, holdings, cash)

    enabled = _edit_enabled()
    view = _dash_portfolio_config(REPO_ROOT, edit_enabled=enabled)
    view["save_result"] = result
    return _render(request, "dashboard/portfolio_config.html", **view)


# ---------------------------------------------------------------------------
# Operator-control: create a work order (CREATE-ONLY — never executes a worker)
# ---------------------------------------------------------------------------


@app.post("/dashboard/operator/create")
async def page_create_work_order(
    request: Request, _a: str | None = Depends(_require_auth)
):
    """
    POST /dashboard/operator/create

    Endpoint path deliberately avoids the token "order" (reserved for trade
    orders in this system) — it creates an operator-control work order, the
    only mutating operator action.

    Creates a single allowlisted work order from a (probe_id, skill_id, mode)
    tuple submitted by a dashboard action button. This is the ONLY mutating
    operator endpoint and it does exactly one thing: append a work-order record.

    It NEVER executes a worker, runs shell commands, touches broker/trade logic,
    or stores arbitrary command text — the request carries only registry ids,
    which are validated against the probe/skill registries + repair policy.
    """
    from operator_control import work_orders as _wo
    from operator_control.repair_policies import WorkOrderValidationError
    from operator_control.probe_registry import get_probe

    form = await request.form()
    probe_id = str(form.get("probe_id", "")).strip()
    skill_id = str(form.get("skill_id", "")).strip()
    mode = str(form.get("mode", "")).strip()

    actor = _a or "dashboard"
    probe = get_probe(probe_id)
    source_view = probe.source_view if probe is not None else "system"

    try:
        _wo.create_work_order(
            REPO_ROOT,
            probe_id=probe_id,
            skill_id=skill_id,
            mode=mode,
            created_by=actor,
        )
    except WorkOrderValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )

    # POST→redirect→GET so a refresh does not resubmit.
    return RedirectResponse(f"/dashboard/{source_view}", status_code=303)


@app.post("/dashboard/operator/dispatch")
async def page_dispatch_worker(
    request: Request, _a: str | None = Depends(_require_auth)
):
    """
    POST /dashboard/operator/dispatch — the "Repair" button.

    Creates a work order, APPROVES it (the deliberate GUI click is the
    approval — the only gate), and launches a DETACHED autonomous worker that
    auto-diagnoses then fixes in an isolated worktree. The web process never
    blocks: it spawns a background runner and returns immediately.

    The worker can NEVER reach production: it works in a throwaway worktree, the
    runner never merges/pushes, the protected-path guard quarantines protected
    changes, and the production-impact gate fails any run that touches main or a
    live production file. The kill-switch (config/operator_worker.DISABLED)
    still forces a safe fallback to scaffolding.
    """
    from operator_control import work_orders as _wo
    from operator_control.repair_policies import WorkOrderValidationError
    from operator_control.probe_registry import get_probe

    form = await request.form()
    probe_id = str(form.get("probe_id", "")).strip()
    skill_id = str(form.get("skill_id", "")).strip()
    mode = str(form.get("mode", "")).strip()

    actor = _a or "dashboard"
    probe = get_probe(probe_id)
    source_view = probe.source_view if probe is not None else "system"

    try:
        order = _wo.create_work_order(
            REPO_ROOT, probe_id=probe_id, skill_id=skill_id, mode=mode,
            created_by=actor,
        )
        wid = order["work_order_id"]
        # The GUI click IS the approval — clear awaiting_approval so the runner
        # (which only accepts queued/approved) can pick it up.
        if order["status"] == "awaiting_approval":
            _wo.transition_work_order(
                REPO_ROOT, wid, new_status="approved", actor=actor,
                note="approved via GUI dispatch (operator click)",
            )
    except WorkOrderValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # Launch the worker detached so the web request returns immediately and the
    # worker runs unattended. STOCKBOT_OPERATOR_WORKER_AUTONOMOUS=1 is set HERE
    # (the click authorizes this run) so the operator need not set it globally.
    child_env = dict(os.environ)
    child_env["STOCKBOT_OPERATOR_WORKER_AUTONOMOUS"] = "1"
    log_dir = REPO_ROOT / "outputs" / "operator_control"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_dir / f"dispatch_{wid}.log", "ab") as logf:
            subprocess.Popen(
                [sys.executable, "-m", "operator_control.worker_runner",
                 "run", "--id", wid, "--actor", actor],
                cwd=str(REPO_ROOT), env=child_env,
                stdout=logf, stderr=logf, start_new_session=True,
            )
    except Exception as exc:  # spawning failed — surface, don't crash the page
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to launch worker: {exc}",
        )

    return RedirectResponse(f"/dashboard/{source_view}", status_code=303)


# ---------------------------------------------------------------------------
# Auto-update with manual intervention (Phase B record-only + Phase C apply)
# ---------------------------------------------------------------------------


@app.post("/dashboard/operator/request-update")
async def page_request_update(
    request: Request, _a: str | None = Depends(_require_auth)
):
    """
    Mode B (zero privilege): record that the operator wants to update. Executes
    NOTHING — it only appends an audit event so the intent is logged. The
    operator runs the documented git-pull + restart command themselves.
    """
    try:
        from operator_control import audit_log
        from gui_v2.data import deploy_status as _ds
        st = _ds.collect_deploy_status(REPO_ROOT, fetch=False)
        audit_log.record_event(
            REPO_ROOT, event_type="deploy_update_requested",
            actor=(_a or "dashboard"),
            details={"running_sha": st.get("running_sha"),
                     "latest_sha": st.get("latest_sha"),
                     "state": st.get("state")},
            safety_result="recorded_intent_only_no_execution",
        )
    except Exception:
        pass
    return RedirectResponse("/dashboard/system", status_code=303)


@app.post("/dashboard/operator/apply-update")
async def page_apply_update(
    request: Request, _a: str | None = Depends(_require_auth)
):
    """
    Mode C (gated): the one-click "Apply update" button. Spawns a DETACHED
    privileged updater (scripts/dashboard_update.sh) that fast-forwards to
    origin/main and restarts the service. The web process never restarts itself.

    Gated by _deploy_apply_enabled() (GUI_V2_DEPLOY_APPLY=1 + no kill-switch).
    Refuses unless the running code is a clean fast-forward behind origin/main.
    """
    if not _deploy_apply_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=("One-click apply is disabled. Set GUI_V2_DEPLOY_APPLY=1 "
                    "(and remove config/dashboard_update.DISABLED), or use the "
                    "manual command shown on the Deployment card."),
        )

    from gui_v2.data import deploy_status as _ds
    from operator_control import audit_log

    st = _ds.collect_deploy_status(REPO_ROOT)
    if st.get("state") != "update_available" or not st.get("fast_forward"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"refusing apply: state={st.get('state')} (need a clean "
                   f"fast-forward update). Resolve manually.",
        )

    script = REPO_ROOT / "scripts" / "dashboard_update.sh"
    log_dir = REPO_ROOT / "outputs" / "operator_control"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_dir / "dashboard_update.log", "ab") as logf:
            subprocess.Popen(
                ["bash", str(script)],
                cwd=str(REPO_ROOT), stdout=logf, stderr=logf,
                start_new_session=True,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to launch updater: {exc}",
        )

    audit_log.record_event(
        REPO_ROOT, event_type="deploy_update_applied",
        actor=(_a or "dashboard"),
        details={"from_sha": st.get("running_sha"), "to_sha": st.get("latest_sha")},
        safety_result="ff_only_detached_restart",
    )
    # The service will restart momentarily; redirect (the new process answers).
    return RedirectResponse("/dashboard/system", status_code=303)


# Work-order ids look like wo_<timestamp>_<hex>. Validate strictly before using
# the id in a filesystem path — blocks any traversal into reports/.
_WO_ID_RE = re.compile(r"^wo_[0-9A-Za-z_]+$")


@app.get("/dashboard/operator/report/{work_order_id}", response_class=HTMLResponse)
def page_operator_report(
    work_order_id: str, request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    """
    GET /dashboard/operator/report/{id} — read-only view of a worker's result
    report (Phase 3). No controls. The id is regex-validated so it can never be
    used to traverse out of outputs/operator_control/reports/.
    """
    if not _WO_ID_RE.match(work_order_id):
        raise HTTPException(status_code=404, detail="not found")
    from operator_control import work_orders as _wo, report_path

    order = _wo.get_work_order(REPO_ROOT, work_order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="work order not found")
    rp = report_path(REPO_ROOT, work_order_id)
    report_text = rp.read_text(encoding="utf-8") if rp.exists() else ""
    return _render(
        request,
        "dashboard/operator_report.html",
        work_order=order,
        report_text=report_text,
        work_order_id=work_order_id,
    )


# ---------------------------------------------------------------------------
# Root redirect: / → /dashboard/today
# ---------------------------------------------------------------------------


@app.get("/")
def root_redirect(_a: str | None = Depends(_require_auth)):
    return RedirectResponse("/dashboard/today", status_code=302)


# ---------------------------------------------------------------------------
# Old-route redirects via REDIRECT_MAP
# ---------------------------------------------------------------------------


def _mk_redirect(target: str):
    """Factory returning a redirect handler for a given target path."""
    def _handler(_a: str | None = Depends(_require_auth)):
        return RedirectResponse(target, status_code=302)
    return _handler


for _old_path, _new_target in REDIRECT_MAP.items():
    app.get(_old_path)(_mk_redirect(_new_target))
