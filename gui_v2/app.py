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


def _render(request: Request, template_name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, template_name, context)


@app.get("/", response_class=HTMLResponse)
def page_today(request: Request) -> HTMLResponse:
    return _render(request, "today.html")


@app.get("/portfolio", response_class=HTMLResponse)
def page_portfolio(request: Request) -> HTMLResponse:
    return _render(request, "portfolio.html")


@app.get("/research", response_class=HTMLResponse)
def page_research(request: Request) -> HTMLResponse:
    return _render(request, "research.html")


@app.get("/health", response_class=HTMLResponse)
def page_health(request: Request) -> HTMLResponse:
    return _render(request, "health.html")


@app.get("/operations", response_class=HTMLResponse)
def page_operations(request: Request) -> HTMLResponse:
    return _render(request, "operations.html")
