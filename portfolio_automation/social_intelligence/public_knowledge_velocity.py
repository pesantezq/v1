"""
Public Knowledge Velocity Layer — top-level orchestrator (Crowd Radar).

Reads config + the known-symbol universe, (optionally) fetches public-discussion
posts, aggregates per-ticker features, classifies crowd-knowledge states, runs an
efficacy backtest from any available signal history, and writes five sandbox
artifacts. Sandbox-only, observe-only, default-disabled, fail-safe.

GOVERNANCE GUARANTEES (enforced at runtime, not just by convention):
- Writes go through ``OutputNamespace.SANDBOX`` + ``assert_can_write_namespace``,
  so DAILY / MANUAL_UPDATE / WEEKLY_REVIEW modes cannot write these artifacts.
- ``recommended_next_step`` values are asserted to be research verbs only; any
  forbidden trade verb raises before a single artifact is written.
- Never touches outputs/latest/decision_plan.json or the signal registry.
- Never raises into the pipeline: every failure mode returns a status dict and
  writes a degraded artifact.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)
from portfolio_automation.run_mode_governance import (
    RunMode,
    assert_can_write_namespace,
    normalize_run_mode,
)
from portfolio_automation.social_intelligence.base import (
    FORBIDDEN_TRADE_VERBS,
    KILL_SWITCH_ENV,
    KILL_SWITCH_FILE,
    SourceStatus,
    base_envelope,
    utc_now_iso,
)
from portfolio_automation.social_intelligence.context_join import (
    MENTION_HISTORY_REL,
    build_history_payload,
    build_market_context,
    load_mention_history,
    update_mention_history,
)
from portfolio_automation.social_intelligence.crowd_state_classifier import (
    ClassifierThresholds,
    classify_all,
)
from portfolio_automation.social_intelligence.feature_aggregation import (
    aggregate_ticker_features,
)
from portfolio_automation.social_intelligence.reddit_connector import (
    FetchResult,
    fetch_subreddit_posts,
)
from portfolio_automation.social_intelligence.social_signal_backtest import (
    SignalObservation,
    build_social_signal_backtest,
)
from portfolio_automation.social_intelligence.source_registry import (
    build_source_compliance,
)

logger = logging.getLogger("stockbot.social_intelligence.public_knowledge_velocity")

# Artifact paths (relative to OutputNamespace.SANDBOX root → outputs/sandbox/).
_COMPLIANCE_PATH = "discovery/social_source_compliance.json"
_VELOCITY_PATH = "discovery/public_knowledge_velocity.json"
_STATE_PATH = "discovery/crowd_knowledge_state.json"
_BACKTEST_PATH = "discovery/social_signal_backtest.json"
_SUMMARY_MD_PATH = "discovery/crowd_radar_summary.md"

_DEFAULT_CONFIG = {
    "enabled": False,
    "sources": ["reddit"],
    "subreddits": ["wallstreetbets", "stocks", "investing"],
    "max_posts_per_source": 200,
    "min_mentions_for_state": 3,
    "min_backtest_sample": 20,
    "research_priority_cap": 10.0,
    "mention_history_window": 20,
}


# ---------------------------------------------------------------------------
# Config / gating
# ---------------------------------------------------------------------------

def _load_config(root: Path) -> dict[str, Any]:
    try:
        raw = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
        cfg = dict(_DEFAULT_CONFIG)
        cfg.update(raw.get("crowd_radar") or {})
        return cfg
    except Exception as exc:
        logger.debug("crowd_radar: config load failed (%s) — using defaults", exc)
        return dict(_DEFAULT_CONFIG)


def _kill_switched(root: Path) -> bool:
    if (os.environ.get(KILL_SWITCH_ENV) or "").strip() in ("1", "true", "True"):
        return True
    return (root / KILL_SWITCH_FILE).exists()


def _known_universe(root: Path) -> set[str]:
    """Best-effort symbol allowlist from the approved watchlist + top100 lists."""
    universe: set[str] = set()
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
        universe.update(str(s).upper() for s in (cfg.get("watchlist_scanner", {}) or {}).get("watchlist", []))
    except Exception:
        pass
    for rel in ("outputs/sandbox/top100_daily.json", "outputs/latest/watchlist_signals.json"):
        try:
            data = json.loads((root / rel).read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                for key in ("symbols", "tickers", "watchlist"):
                    universe.update(str(s).upper() for s in (data.get(key) or []))
        except Exception:
            continue
    return universe


def _load_signal_history(root: Path) -> list[SignalObservation]:
    """
    Load resolved historical social signals for the backtest, if any exist.

    Looks for outputs/sandbox/discovery/social_signal_history.json (an append-only
    ledger written by future resolved-outcome joins). Absent → empty (the backtest
    then reports insufficient_data, which is correct for a brand-new layer).
    """
    path = root / "outputs/sandbox/discovery/social_signal_history.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    out: list[SignalObservation] = []
    for rec in (data.get("records") if isinstance(data, dict) else data) or []:
        try:
            out.append(SignalObservation(
                ticker=str(rec["ticker"]).upper(),
                crowd_state=str(rec["crowd_state"]),
                signal_date=str(rec.get("signal_date", "")),
                returns=rec.get("returns") or {},
                raw_returns=rec.get("raw_returns") or {},
                max_drawdown=rec.get("max_drawdown"),
                volatility=rec.get("volatility"),
            ))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Governance guard
# ---------------------------------------------------------------------------

def _assert_no_trade_verbs(states: list[dict[str, Any]]) -> None:
    """Raise if any classification smuggled a trade verb into next_step. The
    cascade only emits :class:`NextStep` values, so this is a defense-in-depth
    assertion against a future edit, surfaced loudly rather than silently."""
    for s in states:
        step = str(s.get("recommended_next_step", "")).lower()
        if step in FORBIDDEN_TRADE_VERBS:
            raise AssertionError(
                f"crowd_radar emitted forbidden trade verb {step!r} for "
                f"{s.get('ticker')}; this layer is research-only."
            )


# ---------------------------------------------------------------------------
# Markdown summary
# ---------------------------------------------------------------------------

def render_crowd_radar_summary_md(
    states: list[dict[str, Any]],
    *,
    source_status: str,
    data_quality_status: str,
    compliance_status: str,
) -> str:
    lines: list[str] = ["# Crowd Radar — Sandbox Research", ""]
    lines.append("_Sandbox research intelligence only. Not a trade recommendation._")
    lines.append("")
    lines.append(f"- Source status: **{source_status}**")
    lines.append(f"- Data quality: **{data_quality_status}**")
    lines.append(f"- Compliance: **{compliance_status}**")
    lines.append("")

    def _bucket(name: str) -> list[str]:
        return [f"{s['ticker']} (conf {s['confidence']:.2f})"
                for s in states if s["crowd_state"] == name]

    sections = [
        ("Emerging DD", "emerging_dd"),
        ("Crowd Validation", "crowd_validation"),
        ("Hype Acceleration Warning", "hype_acceleration"),
        ("Reflexive Squeeze Risk", "reflexive_squeeze_risk"),
        ("Known News Echo", "known_news_echo"),
        ("Crowd Exhaustion", "crowd_exhaustion"),
        ("Contrarian Neglect", "contrarian_neglect"),
    ]
    for label, key in sections:
        tickers = _bucket(key)
        if tickers:
            lines.append(f"- **{label}:** {', '.join(tickers[:6])}")
    if len(lines) and not any(l.startswith("- **") for l in lines):
        lines.append("- No actionable crowd-knowledge states this run.")
    lines.append("")
    lines.append("---")
    lines.append("_Crowd signals adjust research priority only; they cannot trigger any trade._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_public_knowledge_velocity(
    root: str | Path = ".",
    run_mode: str | RunMode = "discovery",
    run_id: str | None = None,
    *,
    write_files: bool = True,
    posts_override: list | None = None,
    fetch_fn=None,
) -> dict[str, Any]:
    """
    Run the Crowd Radar layer. Always returns a dict; never raises.

    Parameters
    ----------
    run_mode:
        Only DISCOVERY / BACKTEST may write the sandbox artifacts. Other modes
        compute but skip writes (behaving like a dry run).
    posts_override / fetch_fn:
        Test seams. ``posts_override`` supplies RawPost objects directly;
        ``fetch_fn(subreddits, limit_per_sub, ...) -> FetchResult`` replaces the
        live Reddit fetch.
    """
    root_path = Path(root)
    run_id = run_id or utc_now_iso()
    try:
        mode = normalize_run_mode(run_mode)
    except Exception:
        mode = RunMode.DISCOVERY
    cfg = _load_config(root_path)

    warnings: list[str] = []
    source_status = SourceStatus.OK
    posts: list = []

    # --- gating ---------------------------------------------------------------
    if _kill_switched(root_path):
        source_status = SourceStatus.DISABLED
        warnings.append("kill_switch_active")
    elif not cfg.get("enabled"):
        source_status = SourceStatus.DISABLED
        warnings.append("crowd_radar.enabled=false")

    # --- ingestion ------------------------------------------------------------
    if source_status == SourceStatus.OK:
        if posts_override is not None:
            posts = list(posts_override)
            if not posts:
                source_status = SourceStatus.INSUFFICIENT_DATA
        else:
            fetch = (fetch_fn or fetch_subreddit_posts)(
                list(cfg.get("subreddits") or []),
                limit_per_sub=int(cfg.get("max_posts_per_source", 200)),
            )
            if isinstance(fetch, FetchResult):
                posts = fetch.posts
                source_status = fetch.status
                warnings.extend(fetch.warnings)
            else:  # pragma: no cover - defensive
                source_status = SourceStatus.ERROR
                warnings.append("fetch_fn returned non-FetchResult")

    # --- feature aggregation + classification --------------------------------
    universe = _known_universe(root_path) or None
    history_window = int(cfg.get("mention_history_window", 20))
    prior_history = load_mention_history(root_path)
    market_context = build_market_context(root_path)
    features = aggregate_ticker_features(
        posts,
        known_universe=universe,
        mention_history=prior_history,
        market_context=market_context,
        min_detection_confidence=0.5,
    ) if posts else []

    thresholds = ClassifierThresholds(min_mentions=int(cfg.get("min_mentions_for_state", 3)))
    states = classify_all(
        features,
        thresholds,
        research_priority_cap=float(cfg.get("research_priority_cap", 10.0)),
    )
    _assert_no_trade_verbs(states)  # defense-in-depth before any write

    if source_status == SourceStatus.OK and not states:
        # Fetched but nothing classifiable.
        source_status = SourceStatus.INSUFFICIENT_DATA
        warnings.append("no_classifiable_tickers")

    data_quality = "ok" if states else (
        "disabled" if source_status == SourceStatus.DISABLED else "insufficient_data"
    )

    # --- backtest -------------------------------------------------------------
    history = _load_signal_history(root_path)
    backtest = build_social_signal_backtest(
        history,
        run_id=run_id,
        run_mode=mode.value,
        min_sample=int(cfg.get("min_backtest_sample", 20)),
    )

    # --- payloads -------------------------------------------------------------
    compliance = build_source_compliance(
        run_id=run_id,
        run_mode=mode.value,
        enabled_sources=list(cfg.get("sources") or []),
        overall_status=source_status.value,
        warnings=warnings,
    )
    compliance_status = "ok" if compliance.get("review_needed_count", 0) == 0 else "review_needed"
    if source_status == SourceStatus.DISABLED:
        compliance_status = "disabled"

    velocity_env = base_envelope(
        run_id=run_id, run_mode=mode.value,
        source_status=source_status.value, data_quality_status=data_quality,
        warnings=warnings,
    )
    velocity_env.update({
        "ticker_count": len(features),
        "post_count": len(posts),
        "subreddits": list(cfg.get("subreddits") or []),
        "records": [f.to_dict() for f in features],
    })

    state_env = base_envelope(
        run_id=run_id, run_mode=mode.value,
        source_status=source_status.value, data_quality_status=data_quality,
        warnings=warnings,
    )
    state_env.update({
        "research_priority_cap": float(cfg.get("research_priority_cap", 10.0)),
        "state_count": len(states),
        "records": states,
    })

    summary_md = render_crowd_radar_summary_md(
        states,
        source_status=source_status.value,
        data_quality_status=data_quality,
        compliance_status=compliance_status,
    )

    # --- write ----------------------------------------------------------------
    artifacts: dict[str, str] = {}
    wrote = False
    if write_files:
        try:
            assert_can_write_namespace(mode, OutputNamespace.SANDBOX)
            artifacts["social_source_compliance"] = str(
                safe_write_json(OutputNamespace.SANDBOX, _COMPLIANCE_PATH, compliance, base_dir=root_path / "outputs"))
            artifacts["public_knowledge_velocity"] = str(
                safe_write_json(OutputNamespace.SANDBOX, _VELOCITY_PATH, velocity_env, base_dir=root_path / "outputs"))
            artifacts["crowd_knowledge_state"] = str(
                safe_write_json(OutputNamespace.SANDBOX, _STATE_PATH, state_env, base_dir=root_path / "outputs"))
            artifacts["social_signal_backtest"] = str(
                safe_write_json(OutputNamespace.SANDBOX, _BACKTEST_PATH, backtest, base_dir=root_path / "outputs"))
            artifacts["crowd_radar_summary_md"] = str(
                safe_write_text(OutputNamespace.SANDBOX, _SUMMARY_MD_PATH, summary_md, base_dir=root_path / "outputs"))
            # Persist the rolling mention-history ledger so the NEXT run can
            # compute velocity z-scores. Only when we actually have posts this
            # run (a disabled/no-credentials run must not append a row of zeros).
            if posts:
                today_counts = {f.ticker: f.mention_count for f in features}
                updated_history = update_mention_history(
                    prior_history, today_counts, window=history_window
                )
                artifacts["crowd_mention_history"] = str(safe_write_json(
                    OutputNamespace.SANDBOX, MENTION_HISTORY_REL,
                    build_history_payload(updated_history, window=history_window, created_at=run_id),
                    base_dir=root_path / "outputs"))
            wrote = True
        except Exception as exc:
            logger.warning("crowd_radar: artifact write skipped/failed (%s)", exc)
            warnings.append(f"write_skipped:{exc}")

    return {
        "status": source_status.value,
        "data_quality_status": data_quality,
        "compliance_status": compliance_status,
        "run_mode": mode.value,
        "post_count": len(posts),
        "ticker_count": len(features),
        "state_count": len(states),
        "wrote_files": wrote,
        "artifacts": artifacts,
        "warnings": warnings,
        "observe_only": True,
        "sandbox_only": True,
    }


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Crowd Radar / Public Knowledge Velocity Layer")
    parser.add_argument("--root", default=".")
    parser.add_argument("--run-mode", default="discovery")
    args = parser.parse_args()
    result = run_public_knowledge_velocity(root=args.root, run_mode=args.run_mode)
    print(json.dumps(result, indent=2, default=str))
