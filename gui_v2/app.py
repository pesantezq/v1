"""GUI v2 — FastAPI application."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
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
from gui_v2.data.shared import REDIRECT_MAP


# ---------------------------------------------------------------------------
# Persona dashboard routes (canonical)
# ---------------------------------------------------------------------------


@app.get("/dashboard/today", response_class=HTMLResponse)
def page_dash_today(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    return _render(request, "dashboard/today.html", **_dash_today(REPO_ROOT))


@app.get("/dashboard/portfolio", response_class=HTMLResponse)
def page_dash_portfolio(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    return _render(request, "dashboard/portfolio.html", **_dash_portfolio(REPO_ROOT))


@app.get("/dashboard/quant", response_class=HTMLResponse)
def page_dash_quant(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    return _render(request, "dashboard/quant.html", **_dash_quant(REPO_ROOT))


@app.get("/dashboard/system", response_class=HTMLResponse)
def page_dash_system(
    request: Request, _a: str | None = Depends(_require_auth)
) -> HTMLResponse:
    return _render(request, "dashboard/system.html", **_dash_system(REPO_ROOT))


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
