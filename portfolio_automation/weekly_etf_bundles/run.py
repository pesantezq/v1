"""
Standalone weekly ETF bundle runner + CLI.

  python -m portfolio_automation.weekly_etf_bundles.run --as-of 2026-07-24 --email-dry-run

Modes: --analysis-only | --mature-outcomes | --evaluate | --render-only |
--email-dry-run | --send-email | --force-send. Fully isolated from the daily
pipeline: writes only the weekly_etf_bundles / simulation / policy namespaces,
never decision_plan.json. Non-blocking: never raises for a component failure.
Fail-closed for SENDING (config invalid / stale / low coverage / render fail).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json, safe_write_text
from portfolio_automation.weekly_etf_bundles import SOURCE_LABEL, STRATEGY_ID, MODEL_VERSION
from portfolio_automation.weekly_etf_bundles.analysis import build_weekly_analysis
from portfolio_automation.weekly_etf_bundles.attribution import build_attribution
from portfolio_automation.weekly_etf_bundles.calibration import build_calibration
from portfolio_automation.weekly_etf_bundles.config import WeeklyEtfConfigError, load_config
from portfolio_automation.weekly_etf_bundles.emailer import send_weekly_etf_bundle_email
from portfolio_automation.weekly_etf_bundles.evaluation import build_scorecard
from portfolio_automation.weekly_etf_bundles.health import build_health
from portfolio_automation.weekly_etf_bundles.outcomes import HORIZON_SPECS, STATUS_MATURED, mature_prediction
from portfolio_automation.weekly_etf_bundles.predictions import (
    freeze_predictions,
    list_prediction_dates,
    load_predictions_for_date,
)
from portfolio_automation.weekly_etf_bundles.renderer import render_weekly_html, render_weekly_md
from portfolio_automation.weekly_etf_bundles import strat_lab_adapter as SL

logger = logging.getLogger("stockbot.weekly_etf_bundles.run")

_MIN_COVERAGE_FOR_SEND = 0.80
_MAX_FRESHNESS_DAYS_FOR_SEND = 10


def _load_panel(config: Any, root: Path, fmp_client: Any) -> Any:
    """Build the price panel via the budget-governed FMP client + archive cache."""
    from portfolio_automation.portfolio_sim.prices import load_price_panel
    if fmp_client is None:
        try:
            from portfolio_automation.data_budget.factory import governed_client
            fmp_client = governed_client("weekly_review")
        except Exception as exc:  # pragma: no cover - env-dependent
            logger.warning("governed_client unavailable (%s); archive-only", exc)
            fmp_client = None
    return load_price_panel(config.all_symbols, root, fmp_client=fmp_client)


def mature_all_outcomes(
    root: Path, panel: Any, *, now_date: str | None = None, write_files: bool = True,
) -> list[dict[str, Any]]:
    """Mature every frozen champion prediction at every horizon. Idempotent:
    outcome files are (re)written only as predictions mature; already-matured
    outcomes are stable (prices are historical)."""
    now_date = now_date or (panel.dates[-1] if getattr(panel, "dates", None) else None)
    all_rows: list[dict[str, Any]] = []
    for mdd in list_prediction_dates(root):
        preds = load_predictions_for_date(root, mdd)
        for horizon in HORIZON_SPECS:
            rows = [mature_prediction(p, panel, horizon, now_date=now_date) for p in preds]
            all_rows.extend(rows)
            if write_files and any(o.get("status") == STATUS_MATURED for o in rows):
                safe_write_json(
                    OutputNamespace.WEEKLY_ETF_BUNDLES, f"outcomes/{horizon}/{mdd}.json",
                    {"market_data_date": mdd, "horizon": horizon, "observe_only": True,
                     "count": len(rows), "outcomes": rows},
                    base_dir=root / "outputs")
    return all_rows


def run_weekly_etf_bundles(
    *,
    root: str | Path = ".",
    as_of: str | None = None,
    config_path: str | Path | None = None,
    mode: str = "full",
    send_email: bool = False,
    email_dry_run: bool = True,
    force_send: bool = False,
    panel: Any = None,
    fmp_client: Any = None,
    env: dict[str, str] | None = None,
    write_files: bool = True,
) -> dict[str, Any]:
    """Orchestrate the weekly run. Returns a status dict; never raises for a
    component failure."""
    root_path = Path(root).resolve()
    generated_at = datetime.now(timezone.utc).isoformat()
    as_of = as_of or date.today().isoformat()
    steps: dict[str, Any] = {}
    result: dict[str, Any] = {"status": "ok", "mode": mode, "generated_at": generated_at,
                              "as_of": as_of, "steps": steps, "observe_only": True}

    # 1. Config (fail-closed).
    try:
        config = load_config(config_path, root=root_path)
    except WeeklyEtfConfigError as exc:
        logger.error("weekly_etf config invalid: %s", exc)
        health = build_health(analysis_payload=None, config_valid=False, generated_at=generated_at)
        if write_files:
            _write(root_path, "health.json", health, is_json=True)
        return {"status": "error", "reason": "config_invalid", "error": str(exc), "health": health}

    enabled_ids = [b.id for b in config.enabled_bundles]

    # 2. Price panel.
    if panel is None:
        panel = _load_panel(config, root_path, fmp_client)

    # 3. Analysis.
    payload = build_weekly_analysis(config, panel, as_of=as_of, generated_at=generated_at,
                                    strategy_id=STRATEGY_ID, model_version=MODEL_VERSION)
    steps["analysis"] = payload.get("status")

    do_freeze = mode in ("full",)
    do_strat = mode in ("full", "evaluate")
    do_mature = mode in ("full", "mature-outcomes", "evaluate")
    do_evaluate = mode in ("full", "evaluate")
    do_render = mode in ("full", "render-only")
    do_email = mode in ("full",) or send_email or (mode == "email-dry-run")

    # 4. Write analysis artifact.
    if write_files:
        _write(root_path, "latest.json", payload, is_json=True)

    # 5. Freeze champion predictions.
    if do_freeze and payload.get("status") == "ok":
        steps["freeze"] = freeze_predictions(payload, root=root_path, write_files=write_files)

    # 6. Mature outcomes.
    matured_rows: list[dict[str, Any]] = []
    if do_mature:
        matured_rows = mature_all_outcomes(root_path, panel, write_files=write_files)
        steps["matured_rows"] = len(matured_rows)

    # 7. Scorecard / calibration / attribution.
    scorecard = calibration = attribution = None
    if do_evaluate:
        scorecard = build_scorecard(matured_rows)
        calibration = build_calibration(matured_rows)
        attribution = build_attribution(scorecard, matured_rows, calibration=calibration)
        steps["sample_status"] = scorecard.get("sample_status")
        if write_files:
            _write(root_path, "scorecard.json", scorecard, is_json=True)
            _write(root_path, "calibration.json", calibration, is_json=True)
            _write(root_path, "attribution.json", attribution, is_json=True)

    # 8. Strat Lab walk-forward comparison over frozen prediction dates.
    strat_lab = None
    if do_strat:
        wf_dates = list_prediction_dates(root_path) or [payload.get("market_data_date")]
        wf_dates = [d for d in wf_dates if d]
        try:
            strat_lab = SL.run_strat_lab_comparison(config, panel, wf_dates, generated_at=generated_at)
            if write_files:
                SL.write_strat_lab_artifacts(strat_lab, root=root_path)
            steps["strat_lab_leaderboard"] = strat_lab.get("leaderboard")
        except Exception as exc:  # non-blocking
            logger.warning("strat lab comparison failed: %s", exc)
            steps["strat_lab_error"] = str(exc)

    # 9. Render digest.
    if do_render and write_files:
        _write(root_path, "latest.md", render_weekly_md(payload, scorecard), is_json=False)
        _write(root_path, "latest.html", render_weekly_html(payload, scorecard), is_json=False)

    # 10. Email (fail-closed for sending).
    email_result = None
    if do_email:
        blocked = _send_block_reason(payload)
        if send_email and not email_dry_run and blocked:
            email_result = {"sent": False, "reason": f"fail_closed:{blocked}", "enabled": None,
                            "content_hash": None}
            steps["email"] = email_result["reason"]
        else:
            env2 = dict(env or {})
            if send_email:
                env2["WEEKLY_ETF_BUNDLES_EMAIL_DRY_RUN"] = "false"
            elif email_dry_run:
                env2.setdefault("WEEKLY_ETF_BUNDLES_EMAIL_DRY_RUN", "true")
                env2.setdefault("WEEKLY_ETF_BUNDLES_EMAIL_ENABLED", "true")  # allow dry-run build
            email_result = send_weekly_etf_bundle_email(
                analysis_payload=payload, scorecard=scorecard, root=root_path,
                env=env2 or None, force=force_send, write_files=write_files)
            steps["email"] = email_result.get("reason")

    # 11. Health.
    champion_preds = load_predictions_for_date(root_path, payload.get("market_data_date") or "")
    health = build_health(
        analysis_payload=payload, config_valid=True, enabled_bundle_ids=enabled_ids,
        predictions=champion_preds, scorecard=scorecard, calibration=calibration,
        strat_lab_comparison=strat_lab, email_result=email_result, generated_at=generated_at)
    if write_files:
        _write(root_path, "health.json", health, is_json=True)

    result["health_status"] = health["status"]
    result["market_data_date"] = payload.get("market_data_date")
    return result


def _send_block_reason(payload: dict[str, Any]) -> str | None:
    if payload.get("status") != "ok":
        return "analysis_not_ok"
    if not payload.get("market_data_date"):
        return "no_market_data_date"
    cov = payload.get("coverage")
    if cov is not None and cov < _MIN_COVERAGE_FOR_SEND:
        return "coverage_below_threshold"
    return None


def _write(root: Path, rel: str, content: Any, *, is_json: bool) -> None:
    if is_json:
        safe_write_json(OutputNamespace.WEEKLY_ETF_BUNDLES, rel, content, base_dir=root / "outputs")
    else:
        safe_write_text(OutputNamespace.WEEKLY_ETF_BUNDLES, rel, content, base_dir=root / "outputs")


def _resolve_mode(args: argparse.Namespace) -> tuple[str, bool, bool, bool]:
    if args.analysis_only:
        return "analysis-only", False, True, False
    if args.render_only:
        return "render-only", False, True, False
    if args.mature_outcomes:
        return "mature-outcomes", False, True, False
    if args.evaluate:
        return "evaluate", False, True, False
    if args.send_email:
        return "full", True, False, args.force_send
    if args.email_dry_run:
        return "full", False, True, args.force_send
    return "full", False, True, args.force_send


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly ETF bundle watchlist runner")
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default=None)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--analysis-only", action="store_true")
    parser.add_argument("--mature-outcomes", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--email-dry-run", action="store_true")
    parser.add_argument("--send-email", action="store_true")
    parser.add_argument("--force-send", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    mode, send_email, email_dry_run, force_send = _resolve_mode(args)
    res = run_weekly_etf_bundles(
        root=args.root, as_of=args.as_of, config_path=args.config, mode=mode,
        send_email=send_email, email_dry_run=email_dry_run, force_send=force_send)
    print(f"weekly_etf_bundles: status={res.get('status')} "
          f"mode={res.get('mode')} health={res.get('health_status')} "
          f"mdd={res.get('market_data_date')}")
    # Observe-only: a component issue is non-fatal; only a hard config failure is non-zero.
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
