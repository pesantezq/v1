#!/usr/bin/env python3
"""
run_daily_pipeline.py — one-command daily pipeline orchestrator.

Runs all analysis stages in dependency order.  Every step is wrapped in an
isolated try/except so a failure in any one step never prevents the rest from
running.  A structured summary is printed (and logged) at the end.

Steps:
  1  theme_discovery    RSS + LLM theme detection         (skipped when disabled)
  2  watchlist_scan     Alpha Vantage scan + scoring       (skipped when key absent)
  3  weight_tuning      Evaluate ranking weight candidates
  4  policy_eval        Evaluate recommendation history quality
  5  alloc_preview      Observe-only rank-aware allocation preview
  6  alloc_simulation   Baseline vs rank-aware capital simulation
  7  policy_activation  Advisory gate check (approve=False — never auto-approves)
  8  system_summary     Consolidate all artifacts → JSON + Markdown
  9  daily_memo         Human-readable memo → .txt + .md

Usage:
    python run_daily_pipeline.py
    python run_daily_pipeline.py --dry-run
    python run_daily_pipeline.py --skip-theme-engine --skip-scan
    python run_daily_pipeline.py --send-email
    python run_daily_pipeline.py --debug

Logs are written to logs/pipeline_YYYY-MM-DD.log in addition to stdout.
Exit code: 0 when all executed steps succeed, 1 when any step failed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# Ensure project root is importable regardless of CWD
ROOT = Path(__file__).parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOGS_DIR = ROOT / "logs"

_OK      = "ok"
_SKIPPED = "skipped"
_FAILED  = "failed"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(debug: bool = False) -> logging.Logger:
    """Configure root logger: INFO to stdout + DEBUG to daily pipeline log file."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"pipeline_{datetime.now().strftime('%Y-%m-%d')}.log"

    level = logging.DEBUG if debug else logging.INFO
    fmt   = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    dfmt  = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter(fmt, dfmt))
    root.addHandler(ch)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, dfmt))
    root.addHandler(fh)

    return logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# Step result
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    name: str
    status: str           # ok | skipped | failed
    duration_sec: float
    notes: str = ""


# ---------------------------------------------------------------------------
# Isolated step runner
# ---------------------------------------------------------------------------

def _run_step(
    name: str,
    fn: Callable[[], str],
    *,
    log: logging.Logger,
    skip: bool = False,
    skip_reason: str = "",
) -> StepResult:
    """Run fn() in isolation.  Returns a StepResult; never raises."""
    if skip:
        log.info("[%-20s]  SKIPPED  %s", name, skip_reason)
        return StepResult(name, _SKIPPED, 0.0, skip_reason)

    log.info("[%-20s]  starting …", name)
    t0 = time.monotonic()
    try:
        notes = fn() or ""
        dur = time.monotonic() - t0
        log.info("[%-20s]  ok  (%.1fs)  %s", name, dur, notes)
        return StepResult(name, _OK, dur, notes)
    except Exception as exc:
        dur = time.monotonic() - t0
        log.error("[%-20s]  FAILED  (%.1fs): %s", name, dur, exc, exc_info=True)
        return StepResult(name, _FAILED, dur, str(exc)[:120])


# ---------------------------------------------------------------------------
# Config helpers (no external deps)
# ---------------------------------------------------------------------------

def _load_raw_config() -> dict[str, Any]:
    path = ROOT / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def _load_dot_env() -> None:
    """Parse .env without any third-party library."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k:
                os.environ.setdefault(k, v)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Step implementations — each returns a short notes string or raises
# ---------------------------------------------------------------------------

def _step_theme_discovery(raw_cfg: dict[str, Any], dry_run: bool) -> str:
    from types import SimpleNamespace
    from theme_engine.__main__ import run as _run_theme

    te_cfg = raw_cfg.get("theme_engine", {})
    config_ns = SimpleNamespace(
        theme_engine=te_cfg,
        theme_engine_enabled=te_cfg.get("enabled", False),
    )
    result = _run_theme(mode="daily", config=config_ns, dry_run=dry_run, root=str(ROOT))
    n_themes = len(result.get("themes") or [])
    n_cands  = len(result.get("watch_candidates") or [])
    return f"{n_themes} themes, {n_cands} watch candidates"


def _step_watchlist_scan(raw_cfg: dict[str, Any], dry_run: bool) -> str:
    from watchlist_scanner.__main__ import run as _run_scan

    ws_cfg = raw_cfg.get("watchlist_scanner", {})
    result = _run_scan(
        config=ws_cfg,
        dry_run=dry_run,
        output_dir=str(ROOT / "outputs" / "latest"),
        extended_watchlist_config=raw_cfg.get("extended_watchlist"),
        portfolio_context=raw_cfg.get("portfolio"),
        signals_config=raw_cfg.get("signals"),
        ranking_config=raw_cfg.get("ranking"),
        scraped_intel_config=raw_cfg.get("scraped_intel"),
        data_sources_config=raw_cfg.get("data_sources", {}),
    )
    alerts  = len(result.get("alerts") or [])
    signals = len(result.get("results") or [])
    return f"{signals} signals, {alerts} alerts"


def _step_weight_tuning() -> str:
    from watchlist_scanner.weight_tuning import generate_weight_tuning_report

    result = generate_weight_tuning_report(
        db_path=ROOT / "data" / "portfolio.db",
        output_dir=ROOT / "outputs" / "performance",
    )
    suggestions = result.get("suggestions") or {}
    candidate   = suggestions.get("recommended_candidate") or "current"
    return f"recommended: {candidate}"


def _step_policy_eval() -> str:
    from policy_evaluator.evaluator import evaluate_history
    from policy_evaluator.report_writer import write_evaluation_reports

    result = evaluate_history(history_path=None)
    write_evaluation_reports(result, policy_dir=None)
    n_rec  = getattr(result, "total_records", 0)
    n_runs = getattr(result, "total_runs", 0)
    return f"{n_rec} records, {n_runs} runs"


def _step_alloc_preview() -> str:
    from watchlist_scanner.allocation_preview import generate_allocation_preview_report

    preview = generate_allocation_preview_report(root=ROOT)
    n = int(preview.get("candidate_count") or len(preview.get("opportunities") or []))
    return f"{n} candidates"


def _step_alloc_simulation() -> str:
    from watchlist_scanner.allocation_policy_simulation import (
        generate_allocation_policy_simulation_report,
    )

    sim = generate_allocation_policy_simulation_report(root=ROOT)
    n   = sim.get("sample_size", 0)
    eff = (sim.get("delta") or {}).get("efficiency_delta", 0.0)
    try:
        eff_str = f"{float(eff):+.4f}"
    except (TypeError, ValueError):
        eff_str = "—"
    return f"sample={n}, efficiency_delta={eff_str}"


def _step_policy_activation() -> str:
    from watchlist_scanner.allocation_policy_activation import run_activation_check

    # approve=False — pipeline is advisory only, never auto-approves
    report = run_activation_check(root=ROOT, approve=False)
    passed = report.get("all_rules_passed", False)
    return f"all_rules_passed={passed}  (advisory only)"


def _step_system_summary() -> str:
    from watchlist_scanner.system_summary import generate_system_decision_summary

    summary = generate_system_decision_summary(root=ROOT, write_files=True)
    theme = (summary.get("top_theme") or {}).get("name") or "—"
    opp   = (summary.get("top_opportunity") or {}).get("ticker") or "—"
    chg   = (summary.get("changes") or {}).get("change_count", 0)
    return f"top_theme={theme}, top_opp={opp}, changes={chg}"


def _step_daily_memo(send_email_flag: bool) -> str:
    from watchlist_scanner.daily_memo import generate_daily_memo
    from watchlist_scanner.daily_memo import send_email

    memo_txt, _ = generate_daily_memo(root=ROOT, write_files=True)
    notes = "memo written"
    if send_email_flag:
        ok     = send_email(memo_txt)
        result = "sent" if ok else "send failed (check SMTP env vars)"
        notes += f", email {result}"
    return notes


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(
    *,
    dry_run: bool = False,
    skip_theme_engine: bool = False,
    skip_scan: bool = False,
    send_email_flag: bool = False,
    debug: bool = False,
) -> list[StepResult]:
    """Execute all pipeline steps and return their results."""
    log = _setup_logging(debug=debug)
    started_at = datetime.now()

    _banner(log, started_at, dry_run=dry_run, skip_theme=skip_theme_engine,
            skip_scan=skip_scan, send_email=send_email_flag)

    # Load config + env once; non-fatal if missing
    raw_cfg: dict[str, Any] = {}
    try:
        _load_dot_env()
        raw_cfg = _load_raw_config()
        log.info("Config loaded from config.json")
    except Exception as exc:
        log.warning("Config load failed (non-fatal): %s", exc)

    # Evaluate skip conditions
    te_cfg      = raw_cfg.get("theme_engine", {})
    te_runnable = te_cfg.get("enabled", False) or te_cfg.get("testing_mode", False)
    av_key_set  = bool(os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip())

    steps: list[StepResult] = []

    # 1. Theme discovery
    steps.append(_run_step(
        "theme_discovery",
        lambda: _step_theme_discovery(raw_cfg, dry_run),
        log=log,
        skip=skip_theme_engine or not te_runnable,
        skip_reason=(
            "--skip-theme-engine"
            if skip_theme_engine
            else "theme_engine.enabled=false (or testing_mode=false)"
        ),
    ))

    # 2. Watchlist scan
    steps.append(_run_step(
        "watchlist_scan",
        lambda: _step_watchlist_scan(raw_cfg, dry_run),
        log=log,
        skip=skip_scan or not av_key_set,
        skip_reason=(
            "--skip-scan"
            if skip_scan
            else "ALPHA_VANTAGE_API_KEY not set in .env or environment"
        ),
    ))

    # 3–9: local-only steps, always execute regardless of dry_run
    steps.append(_run_step("weight_tuning",      _step_weight_tuning,      log=log))
    steps.append(_run_step("policy_eval",         _step_policy_eval,        log=log))
    steps.append(_run_step("alloc_preview",       _step_alloc_preview,      log=log))
    steps.append(_run_step("alloc_simulation",    _step_alloc_simulation,   log=log))
    steps.append(_run_step("policy_activation",   _step_policy_activation,  log=log))
    steps.append(_run_step("system_summary",      _step_system_summary,     log=log))
    steps.append(_run_step(
        "daily_memo",
        lambda: _step_daily_memo(send_email_flag and not dry_run),
        log=log,
    ))

    _print_summary(steps, (datetime.now() - started_at).total_seconds(),
                   log=log, dry_run=dry_run)
    return steps


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _banner(
    log: logging.Logger,
    started_at: datetime,
    *,
    dry_run: bool,
    skip_theme: bool,
    skip_scan: bool,
    send_email: bool,
) -> None:
    log.info("=" * 62)
    log.info("  Daily Pipeline — %s%s",
             started_at.strftime("%Y-%m-%d %H:%M:%S"),
             "  [DRY-RUN]" if dry_run else "")
    log.info("  skip_theme=%s  skip_scan=%s  send_email=%s",
             skip_theme, skip_scan, send_email)
    log.info("=" * 62)


def _print_summary(
    steps: list[StepResult],
    elapsed: float,
    *,
    log: logging.Logger,
    dry_run: bool,
) -> None:
    _ICONS = {_OK: "ok    ", _SKIPPED: "skip  ", _FAILED: "FAILED"}

    log.info("")
    log.info("=" * 62)
    log.info("  PIPELINE SUMMARY%s", "  [DRY-RUN]" if dry_run else "")
    log.info("  %-22s  %-6s  %s", "Step", "Status", "Notes")
    log.info("  %s  %s  %s", "-" * 22, "-" * 6, "-" * 28)
    for r in steps:
        icon  = _ICONS.get(r.status, r.status[:6])
        notes = (r.notes or "")[:50]
        log.info("  %-22s  %s  %s", r.name, icon, notes)

    n_ok   = sum(1 for r in steps if r.status == _OK)
    n_skip = sum(1 for r in steps if r.status == _SKIPPED)
    n_fail = sum(1 for r in steps if r.status == _FAILED)

    log.info("")
    log.info("  Elapsed %.1fs  —  %d ok  %d skipped  %d failed",
             elapsed, n_ok, n_skip, n_fail)
    if n_fail:
        failed_names = ", ".join(r.name for r in steps if r.status == _FAILED)
        log.warning("  FAILED steps: %s", failed_names)
        log.warning("  Check logs above and logs/pipeline_*.log for details.")
    else:
        log.info("  All executed steps completed successfully.")
    log.info("=" * 62)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    # Avoid cp1252 encoding errors when printing help on Windows consoles
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(
        prog="python run_daily_pipeline.py",
        description=(
            "One-command daily pipeline: theme discovery -> watchlist scan -> "
            "weight tuning -> policy eval -> allocation preview/simulation -> "
            "system summary -> daily memo. "
            "Every step is non-fatal; the pipeline always runs to completion."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python run_daily_pipeline.py                       # full run\n"
            "  python run_daily_pipeline.py --dry-run             # no API calls\n"
            "  python run_daily_pipeline.py --skip-theme-engine   # skip Ollama\n"
            "  python run_daily_pipeline.py --skip-scan           # skip Alpha Vantage\n"
            "  python run_daily_pipeline.py --send-email          # + email memo\n"
            "  python run_daily_pipeline.py --debug               # verbose logs"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Pass dry_run=True to theme engine and watchlist scan (no API calls, no external writes)",
    )
    parser.add_argument(
        "--skip-theme-engine", action="store_true",
        help="Skip step 1 (theme discovery) — useful when Ollama is unavailable",
    )
    parser.add_argument(
        "--skip-scan", action="store_true",
        help="Skip step 2 (watchlist scan) — useful when AV budget is exhausted",
    )
    parser.add_argument(
        "--send-email", action="store_true",
        help=(
            "Send daily memo via email. Requires SMTP_SERVER, EMAIL_USER, "
            "EMAIL_PASS, EMAIL_TO env vars (legacy aliases also accepted)."
        ),
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG-level logging to stdout (file always gets DEBUG)",
    )
    args = parser.parse_args()

    steps = run_pipeline(
        dry_run=args.dry_run,
        skip_theme_engine=args.skip_theme_engine,
        skip_scan=args.skip_scan,
        send_email_flag=args.send_email,
        debug=args.debug,
    )

    sys.exit(1 if any(r.status == _FAILED for r in steps) else 0)


if __name__ == "__main__":
    _main()
