"""GUI v2 — FastAPI application."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="StockBot Dashboard v2")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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


_STATUS_LABELS: dict[str, str] = {
    "ok":               "OK",
    "ok_with_warnings": "OK · warnings",
    "news_empty":       "No news",
    "near_cap":         "Near cap",
    "partial":          "Partial",
    "warn":             "Warning",
    "breach":           "Breach",
    "exhausted":        "Exhausted",
    "failed":           "Failed",
    "unknown":          "Unknown",
}


def _status_label(status: str | None) -> str:
    """D-M1: humanize snake_case status enums to operator-readable text."""
    s = (status or "").strip().lower()
    if s in _STATUS_LABELS:
        return _STATUS_LABELS[s]
    # Safe default: title-case with spaces instead of underscores.
    return s.replace("_", " ").title()


templates.env.filters["status_label"] = _status_label


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


@app.get("/", response_class=HTMLResponse)
def page_today(request: Request, _auth: str | None = Depends(_require_auth)) -> HTMLResponse:
    return _render(request, "today.html", **collect_today_view(REPO_ROOT))


@app.get("/portfolio", response_class=HTMLResponse)
def page_portfolio(request: Request, _auth: str | None = Depends(_require_auth)) -> HTMLResponse:
    return _render(request, "portfolio.html", **collect_portfolio_view(REPO_ROOT))


@app.get("/research", response_class=HTMLResponse)
def page_research(request: Request, _auth: str | None = Depends(_require_auth)) -> HTMLResponse:
    return _render(request, "research.html", **collect_research_view(REPO_ROOT))


@app.get("/health", response_class=HTMLResponse)
def page_health(request: Request, _auth: str | None = Depends(_require_auth)) -> HTMLResponse:
    view = collect_health_view(REPO_ROOT)
    return _render(request, "health.html", overall=overall_severity(view), **view)


@app.get("/operations", response_class=HTMLResponse)
def page_operations(
    request: Request,
    log: str | None = None,
    tail: int = 200,
    _auth: str | None = Depends(_require_auth),
) -> HTMLResponse:
    # Clamp tail to a sane range so a hostile querystring can't OOM the host.
    tail = max(10, min(5000, int(tail or 200)))
    return _render(
        request, "operations.html",
        **collect_operations_view(REPO_ROOT, log_tail_n=tail, log_name=log),
    )


@app.get("/risk-impact", response_class=HTMLResponse)
def page_risk_impact(request: Request, _auth: str | None = Depends(_require_auth)) -> HTMLResponse:
    return _render(request, "risk_impact.html", **collect_risk_impact_view(REPO_ROOT))
