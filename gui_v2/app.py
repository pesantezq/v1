"""
GUI v2 — FastAPI application.

Single uvicorn-served HTTP app. Reads outputs/* and registry data; never
writes. See gui_v2/__init__.py docstring for context.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

REPO_ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="StockBot Dashboard v2")


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return "<!doctype html><html><body><h1>GUI v2</h1></body></html>"
