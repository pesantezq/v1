"""GUI v2 — FastAPI application."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="StockBot Dashboard v2")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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


def _render(request: Request, template_name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, template_name, context)


from gui_v2.data.today import collect_today_view
from gui_v2.data.health import collect_health_view, overall_severity


@app.get("/", response_class=HTMLResponse)
def page_today(request: Request) -> HTMLResponse:
    return _render(request, "today.html", **collect_today_view(REPO_ROOT))


@app.get("/portfolio", response_class=HTMLResponse)
def page_portfolio(request: Request) -> HTMLResponse:
    return _render(request, "portfolio.html")


@app.get("/research", response_class=HTMLResponse)
def page_research(request: Request) -> HTMLResponse:
    return _render(request, "research.html")


@app.get("/health", response_class=HTMLResponse)
def page_health(request: Request) -> HTMLResponse:
    view = collect_health_view(REPO_ROOT)
    return _render(request, "health.html", overall=overall_severity(view), **view)


@app.get("/operations", response_class=HTMLResponse)
def page_operations(request: Request) -> HTMLResponse:
    return _render(request, "operations.html")
