"""
Discovery Pulse — off-hours theme discovery + scraped intelligence runner.

Runs theme_engine + scraped_intel pipeline on a schedule that avoids the
daily portfolio cron window (09:00 UTC). Gated by monthly bandwidth caps —
either OpenAI cost ceiling OR FMP call ceiling acts as a trip-wire.

Tiers:
  - Tier A (cheap): theme_engine.daily — refresh RSS + canonical themes via OpenAI
  - Tier B (medium): scraped_intel.pipeline — SEC EDGAR + RSS news adapters (free)
  - Tier C (expensive, gated, not yet wired): candidate_scanner FMP refresh

Hard guarantees:
  - observe_only=True hardcoded.
  - Lock-file gated externally (scripts/discovery_pulse.sh acquires flock).
  - Skips cleanly when caps reached — never errors the cron.
  - Writes telemetry to outputs/latest/discovery_pulse_status.json + .md.
  - State persisted to data/discovery_pulse_state.json (monthly counters,
    per-day run counters, last-run timestamps).

Public API:
  load_state(root) -> dict
  evaluate_caps(state, caps) -> tuple[bool, str | None]   # (allowed, skip_reason)
  run_discovery_pulse(root, write_files=True) -> dict
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.discovery_pulse")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "discovery_pulse"
_OBSERVE_ONLY = True
_STATE_REL = ("data", "discovery_pulse_state.json")

_DISCLAIMER = (
    "Observe-only off-hours discovery runner. Triggers theme_engine + "
    "scraped_intel pipelines on a budget-gated schedule. Does not modify "
    "portfolio, allocation, scoring, or decision state."
)

_DEFAULT_CAPS = {
    # Project-wide monthly OpenAI cap (matches portfolio_automation.ai_budget
    # AIBudgetConfig defaults). Set 2026-05-28 to $20/mo per operator brief
    # ($30 FMP + $20 OpenAI + $5 hosting = $55/mo target spend).
    "openai_cost_usd_max": 20.0,
    "fmp_calls_max": 5000,
    "theme_runs_per_day_max": 8,
    "scraped_intel_runs_per_day_max": 6,
}


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _current_month_key(now: datetime | None = None) -> str:
    n = now or datetime.now(timezone.utc)
    return f"{n.year:04d}-{n.month:02d}"


def _current_date_key(now: datetime | None = None) -> str:
    n = now or datetime.now(timezone.utc)
    return n.date().isoformat()


def _empty_state(now: datetime | None = None) -> dict[str, Any]:
    month = _current_month_key(now)
    today = _current_date_key(now)
    return {
        "month": month,
        "openai_cost_usd_month": 0.0,
        "fmp_calls_month": 0,
        "theme_runs_today": 0,
        "theme_runs_date": today,
        "scraped_intel_runs_today": 0,
        "scraped_intel_runs_date": today,
        "total_runs_month": 0,
        "skipped_runs_month": 0,
        "last_run_at": None,
        "last_skip_reason": None,
        "caps": dict(_DEFAULT_CAPS),
    }


def load_state(root: str | Path = ".") -> dict[str, Any]:
    """Load (or initialize) the persistent discovery_pulse state.

    Roll over month and daily counters automatically based on the current
    UTC clock so stale counters don't block new runs after a date change.
    """
    root_path = Path(root)
    state_path = root_path.joinpath(*_STATE_REL)
    now = datetime.now(timezone.utc)
    month = _current_month_key(now)
    today = _current_date_key(now)

    if not state_path.exists():
        return _empty_state(now)

    try:
        state = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return _empty_state(now)

    # Monthly rollover: zero counters, preserve caps
    if state.get("month") != month:
        fresh = _empty_state(now)
        fresh["caps"] = state.get("caps") or dict(_DEFAULT_CAPS)
        return fresh

    # Daily rollover for per-day counters
    if state.get("theme_runs_date") != today:
        state["theme_runs_today"] = 0
        state["theme_runs_date"] = today
    if state.get("scraped_intel_runs_date") != today:
        state["scraped_intel_runs_today"] = 0
        state["scraped_intel_runs_date"] = today

    # Backfill caps if a new key has been added since the file was written
    state.setdefault("caps", {})
    for k, v in _DEFAULT_CAPS.items():
        state["caps"].setdefault(k, v)
    return state


def _write_state(root: Path, state: dict[str, Any]) -> None:
    state_path = root.joinpath(*_STATE_REL)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Cap evaluation
# ---------------------------------------------------------------------------


def evaluate_caps(state: dict[str, Any]) -> tuple[bool, str | None]:
    """Return (allowed_to_run, skip_reason). Both caps act as trip-wires —
    either hitting 100% blocks further runs in the month."""
    caps = state.get("caps") or _DEFAULT_CAPS

    cost = float(state.get("openai_cost_usd_month") or 0.0)
    if cost >= float(caps.get("openai_cost_usd_max", _DEFAULT_CAPS["openai_cost_usd_max"])):
        return (False, f"monthly_openai_cap_reached:${cost:.2f}")

    fmp = int(state.get("fmp_calls_month") or 0)
    if fmp >= int(caps.get("fmp_calls_max", _DEFAULT_CAPS["fmp_calls_max"])):
        return (False, f"monthly_fmp_cap_reached:{fmp}")

    theme_today = int(state.get("theme_runs_today") or 0)
    if theme_today >= int(caps.get("theme_runs_per_day_max", _DEFAULT_CAPS["theme_runs_per_day_max"])):
        return (False, f"daily_theme_cap_reached:{theme_today}")

    si_today = int(state.get("scraped_intel_runs_today") or 0)
    if si_today >= int(caps.get("scraped_intel_runs_per_day_max", _DEFAULT_CAPS["scraped_intel_runs_per_day_max"])):
        return (False, f"daily_scraped_intel_cap_reached:{si_today}")

    return (True, None)


def _refresh_monthly_openai_cost(root: Path) -> float:
    """Read current month's cumulative OpenAI spend from ai_budget tracker."""
    try:
        from portfolio_automation.ai_budget import load_recent_ai_usage_events, _parse_ts
        events = load_recent_ai_usage_events(base_dir=str(root / "outputs"))
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        monthly = sum(
            e.estimated_cost_usd for e in events
            if _parse_ts(e.timestamp) >= month_start
        )
        return float(monthly)
    except Exception as exc:
        logger.debug("discovery_pulse: ai_budget read failed (%s) — using state copy", exc)
        return -1.0  # sentinel: caller keeps existing state value


def _read_fmp_call_count_today(root: Path) -> int:
    """Read today's FMP call count from the existing counter file."""
    counter_path = root / "data" / "fmp_cache" / "call_counter.json"
    if not counter_path.exists():
        return 0
    try:
        d = json.loads(counter_path.read_text(encoding="utf-8", errors="replace"))
        return int(d.get("count_today") or d.get("count") or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Tier runners
# ---------------------------------------------------------------------------


def _run_tier_a_theme(root: Path) -> dict[str, Any]:
    """Tier A — theme_engine.daily + ExtendedWatchlist promotion.

    Cheap (OpenAI only). Returns result + counts. Promoted/reinforced/expired
    counts surface tickers that just became live in the dynamic universe.
    """
    started = datetime.now(timezone.utc)
    promotion: dict[str, Any] = {"promoted": [], "reinforced": [], "expired": [], "skipped": []}
    try:
        from types import SimpleNamespace
        from theme_engine.__main__ import run as run_theme
        raw_cfg = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
        te_cfg = raw_cfg.get("theme_engine", {})
        config_ns = SimpleNamespace(theme_engine=te_cfg, theme_engine_enabled=te_cfg.get("enabled", False))
        result = run_theme(mode="daily", config=config_ns, dry_run=False, root=str(root))
        themes_n = len(result.get("themes") or [])
        candidates = result.get("watch_candidates") or []
        cands_n = len(candidates)

        # Promotion step (item a from operator brief): run the ExtendedWatchlist
        # gate so theme-discovered tickers enter the dynamic universe in the
        # same pulse run instead of waiting for the morning cron.
        try:
            ew_cfg = raw_cfg.get("extended_watchlist") or {}
            if ew_cfg.get("enabled", False) and candidates:
                from watchlist_scanner.extended_watchlist import ExtendedWatchlist
                ew = ExtendedWatchlist(
                    db_path=str(root / (ew_cfg.get("db_path") or "data/portfolio.db")),
                    ttl_days=int(ew_cfg.get("ttl_days", 7)),
                    max_symbols=int(ew_cfg.get("max_symbols", 3)),
                    confidence_threshold=float(ew_cfg.get("confidence_threshold", 0.80)),
                )
                ws_cfg = raw_cfg.get("watchlist_scanner") or {}
                static_wl = ws_cfg.get("watchlist") or []
                promo_result = ew.evaluate_candidates(
                    candidates=candidates,
                    static_watchlist=static_wl,
                )
                promotion = {
                    "promoted": list(promo_result.get("promoted") or []),
                    "reinforced": list(promo_result.get("reinforced") or []),
                    "expired": list(promo_result.get("expired") or []),
                    "skipped": list(promo_result.get("skipped") or []),
                }
        except Exception as promo_exc:
            logger.warning("discovery_pulse tier-A promotion: %s", promo_exc)
            promotion["error"] = f"{type(promo_exc).__name__}: {promo_exc}"

        return {
            "status": "ok",
            "started_at": started.isoformat(),
            "themes_count": themes_n,
            "watch_candidates_count": cands_n,
            "promotion": promotion,
            "error": None,
        }
    except Exception as exc:
        logger.warning("discovery_pulse tier-A: %s", exc)
        return {
            "status": "error",
            "started_at": started.isoformat(),
            "themes_count": 0,
            "watch_candidates_count": 0,
            "promotion": promotion,
            "error": f"{type(exc).__name__}: {exc}",
        }


_TIER_B_SYMBOL_CAP = 50  # SEC EDGAR politeness rate-limit guardrail


def _resolve_dynamic_universe(root: Path) -> dict[str, Any]:
    """Item (b) — return the union of: static watchlist + active extended
    watchlist + today's theme-derived candidates. Cap at _TIER_B_SYMBOL_CAP
    to respect SEC EDGAR + RSS source politeness.
    """
    raw_cfg = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
    ws_cfg = raw_cfg.get("watchlist_scanner") or {}
    ew_cfg = raw_cfg.get("extended_watchlist") or {}

    static_wl = [s.upper() for s in (ws_cfg.get("watchlist") or [])]
    ordered: list[str] = list(static_wl)
    seen = set(static_wl)
    source_map: dict[str, list[str]] = {sym: ["static"] for sym in static_wl}

    # Extended watchlist active tickers
    try:
        from watchlist_scanner.extended_watchlist import ExtendedWatchlist
        ew = ExtendedWatchlist(
            db_path=str(root / (ew_cfg.get("db_path") or "data/portfolio.db")),
            ttl_days=int(ew_cfg.get("ttl_days", 7)),
            max_symbols=int(ew_cfg.get("max_symbols", 3)),
            confidence_threshold=float(ew_cfg.get("confidence_threshold", 0.80)),
        )
        for sym in (ew.get_active_tickers() or []):
            sym_upper = sym.upper()
            if sym_upper not in seen:
                ordered.append(sym_upper)
                seen.add(sym_upper)
                source_map[sym_upper] = ["extended_watchlist"]
            else:
                source_map.setdefault(sym_upper, []).append("extended_watchlist")
    except Exception as exc:
        logger.debug("discovery_pulse: extended_watchlist read failed (%s)", exc)

    # Today's theme-derived candidates
    try:
        wc_path = root / "outputs" / "latest" / "watch_candidates.json"
        if wc_path.exists():
            d = json.loads(wc_path.read_text(encoding="utf-8", errors="replace"))
            cands = d if isinstance(d, list) else (d.get("candidates") or d.get("watch_candidates") or [])
            for c in cands:
                if not isinstance(c, dict):
                    continue
                sym = (c.get("ticker") or c.get("symbol") or "").upper()
                if not sym:
                    continue
                if sym not in seen:
                    ordered.append(sym)
                    seen.add(sym)
                    source_map[sym] = ["theme_candidate"]
                else:
                    source_map.setdefault(sym, []).append("theme_candidate")
    except Exception as exc:
        logger.debug("discovery_pulse: watch_candidates read failed (%s)", exc)

    return {
        "symbols": ordered[:_TIER_B_SYMBOL_CAP],
        "total_before_cap": len(ordered),
        "source_map": source_map,
        "cap": _TIER_B_SYMBOL_CAP,
    }


def _run_tier_b_scraped_intel(root: Path) -> dict[str, Any]:
    """Tier B — scraped_intel.pipeline against the DYNAMIC universe. SEC + RSS,
    both free; rate-limited only by source politeness. Universe = union of
    static watchlist + extended_watchlist + theme candidates (item b)."""
    started = datetime.now(timezone.utc)
    try:
        raw_cfg = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
        si_cfg = raw_cfg.get("scraped_intel") or {}
        if not si_cfg.get("enabled", False):
            return {
                "status": "skipped",
                "started_at": started.isoformat(),
                "reason": "scraped_intel.enabled=false",
                "symbols_processed": 0,
                "evidence_count": 0,
                "universe_size": 0,
                "error": None,
            }
        universe = _resolve_dynamic_universe(root)
        symbols = universe["symbols"]
        if not symbols:
            return {
                "status": "skipped",
                "started_at": started.isoformat(),
                "reason": "empty_universe",
                "symbols_processed": 0,
                "evidence_count": 0,
                "universe_size": 0,
                "error": None,
            }
        from scraped_intel.pipeline import run_scraped_intel
        bundles = run_scraped_intel(
            symbols=symbols,
            config=si_cfg,
            dry_run=False,
        )
        evidence_count = sum(
            len(getattr(b, "evidence", []) or []) for b in (bundles or [])
        )
        return {
            "status": "ok",
            "started_at": started.isoformat(),
            "symbols_processed": len(bundles or []),
            "evidence_count": evidence_count,
            "universe_size": universe["total_before_cap"],
            "universe_capped_at": universe["cap"],
            "error": None,
        }
    except Exception as exc:
        logger.warning("discovery_pulse tier-B: %s", exc)
        return {
            "status": "error",
            "started_at": started.isoformat(),
            "symbols_processed": 0,
            "evidence_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _run_sanitation_daily(root: Path) -> dict[str, Any]:
    """Final sanitation step — aggregate the dynamic universe (static +
    extended_watchlist + theme candidates + recent signals + fmp_top100)
    into a ranked top-100 view and persist top100_daily.json."""
    started = datetime.now(timezone.utc)
    try:
        from portfolio_automation.universe_sanitation import run_universe_sanitation
        r = run_universe_sanitation(root=root, cadence="daily", write_files=True)
        return {
            "status": r.get("status", "error"),
            "started_at": started.isoformat(),
            "total_distinct_tickers": r.get("total_distinct_tickers", 0),
            "top_count": r.get("top_count", 0),
            "error": r.get("error"),
        }
    except Exception as exc:
        logger.warning("discovery_pulse sanitation: %s", exc)
        return {
            "status": "error",
            "started_at": started.isoformat(),
            "total_distinct_tickers": 0,
            "top_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def run_discovery_pulse(
    *,
    root: str | Path = ".",
    write_files: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Top-level discovery-pulse run. Always returns a dict; never raises.

    If caps are reached, records a skip and returns without invoking tiers.
    """
    root_path = Path(root).resolve()
    ts = datetime.now(timezone.utc).isoformat()
    state = load_state(root_path)

    # Refresh cumulative OpenAI cost from ai_budget (source of truth)
    fresh_cost = _refresh_monthly_openai_cost(root_path)
    if fresh_cost >= 0:
        state["openai_cost_usd_month"] = round(fresh_cost, 6)

    allowed, skip_reason = evaluate_caps(state)
    payload: dict[str, Any] = {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "month": state["month"],
        "caps": dict(state["caps"]),
        "usage": {
            "openai_cost_usd_month": state["openai_cost_usd_month"],
            "fmp_calls_month": state["fmp_calls_month"],
            "theme_runs_today": state["theme_runs_today"],
            "scraped_intel_runs_today": state["scraped_intel_runs_today"],
            "total_runs_month": state.get("total_runs_month", 0),
            "skipped_runs_month": state.get("skipped_runs_month", 0),
        },
        "tier_a": None,
        "tier_b": None,
        "sanitation": None,
        "skipped": False,
        "skip_reason": None,
        "disclaimer": _DISCLAIMER,
    }

    if not allowed:
        state["skipped_runs_month"] = int(state.get("skipped_runs_month", 0)) + 1
        state["last_skip_reason"] = skip_reason
        state["last_run_at"] = ts
        payload["skipped"] = True
        payload["skip_reason"] = skip_reason
        payload["usage"]["skipped_runs_month"] = state["skipped_runs_month"]
        if write_files and not dry_run:
            _write_state(root_path, state)
            _write_artifacts(root_path, payload)
        return payload

    fmp_calls_before = _read_fmp_call_count_today(root_path)

    if not dry_run:
        payload["tier_a"] = _run_tier_a_theme(root_path)
        state["theme_runs_today"] = int(state.get("theme_runs_today", 0)) + 1

        payload["tier_b"] = _run_tier_b_scraped_intel(root_path)
        state["scraped_intel_runs_today"] = int(state.get("scraped_intel_runs_today", 0)) + 1

        # Final sanitation — aggregate the dynamic universe into top100_daily
        payload["sanitation"] = _run_sanitation_daily(root_path)

    fmp_calls_after = _read_fmp_call_count_today(root_path)
    delta_fmp = max(0, fmp_calls_after - fmp_calls_before)
    state["fmp_calls_month"] = int(state.get("fmp_calls_month", 0)) + delta_fmp

    # Refresh OpenAI cost again after the tier-A run
    post_cost = _refresh_monthly_openai_cost(root_path)
    if post_cost >= 0:
        state["openai_cost_usd_month"] = round(post_cost, 6)

    state["total_runs_month"] = int(state.get("total_runs_month", 0)) + 1
    state["last_run_at"] = ts
    state["last_skip_reason"] = None

    # Snapshot post-run usage into the payload too
    payload["usage"].update({
        "openai_cost_usd_month": state["openai_cost_usd_month"],
        "fmp_calls_month": state["fmp_calls_month"],
        "fmp_calls_delta_this_run": delta_fmp,
        "theme_runs_today": state["theme_runs_today"],
        "scraped_intel_runs_today": state["scraped_intel_runs_today"],
        "total_runs_month": state["total_runs_month"],
        "skipped_runs_month": state.get("skipped_runs_month", 0),
    })

    if write_files and not dry_run:
        _write_state(root_path, state)
        _write_artifacts(root_path, payload)

    return payload


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a(f"# Discovery Pulse — {payload.get('generated_at', '')[:19]}")
    a("")
    a(f"**Month:** `{payload.get('month','?')}` · **Skipped:** `{payload.get('skipped')}`")
    if payload.get("skip_reason"):
        a(f"**Skip reason:** `{payload['skip_reason']}`")
    a("")
    a(f"> {payload.get('disclaimer', _DISCLAIMER)}")
    a("")

    usage = payload.get("usage") or {}
    caps = payload.get("caps") or {}

    def _pct(used: float, cap: float) -> str:
        return f"{(used / cap * 100):.1f}%" if cap else "—"

    a("## Bandwidth usage")
    a("")
    a("| Resource | Used (month) | Cap | Used % |")
    a("|---|---|---|---|")
    a(
        f"| OpenAI cost | ${usage.get('openai_cost_usd_month', 0):.4f} | "
        f"${caps.get('openai_cost_usd_max', 0):.2f} | "
        f"{_pct(usage.get('openai_cost_usd_month', 0), caps.get('openai_cost_usd_max', 0))} |"
    )
    a(
        f"| FMP calls | {usage.get('fmp_calls_month', 0)} | "
        f"{caps.get('fmp_calls_max', 0)} | "
        f"{_pct(usage.get('fmp_calls_month', 0), caps.get('fmp_calls_max', 0))} |"
    )
    a(
        f"| Theme runs (today) | {usage.get('theme_runs_today', 0)} | "
        f"{caps.get('theme_runs_per_day_max', 0)} | "
        f"{_pct(usage.get('theme_runs_today', 0), caps.get('theme_runs_per_day_max', 0))} |"
    )
    a(
        f"| Scraped-intel runs (today) | {usage.get('scraped_intel_runs_today', 0)} | "
        f"{caps.get('scraped_intel_runs_per_day_max', 0)} | "
        f"{_pct(usage.get('scraped_intel_runs_today', 0), caps.get('scraped_intel_runs_per_day_max', 0))} |"
    )
    a("")
    a(f"**Month-to-date:** {usage.get('total_runs_month', 0)} runs completed · "
      f"{usage.get('skipped_runs_month', 0)} runs skipped (caps reached).")
    a("")

    if payload.get("tier_a"):
        ta = payload["tier_a"]
        a("## Tier A — theme_engine.daily")
        a("")
        a(f"- status: `{ta.get('status')}`")
        a(f"- themes: {ta.get('themes_count', 0)}")
        a(f"- watch_candidates: {ta.get('watch_candidates_count', 0)}")
        if ta.get("error"):
            a(f"- error: `{ta['error']}`")
        a("")

    if payload.get("tier_b"):
        tb = payload["tier_b"]
        a("## Tier B — scraped_intel.pipeline")
        a("")
        a(f"- status: `{tb.get('status')}`")
        a(f"- symbols_processed: {tb.get('symbols_processed', 0)}")
        a(f"- evidence_count: {tb.get('evidence_count', 0)}")
        if tb.get("reason"):
            a(f"- reason: `{tb['reason']}`")
        if tb.get("error"):
            a(f"- error: `{tb['error']}`")
        a("")

    a("---")
    a("_Observe-only off-hours discovery runner._")
    return "\n".join(lines)


def _write_artifacts(root: Path, payload: dict[str, Any]) -> None:
    try:
        safe_write_json(
            OutputNamespace.LATEST,
            "discovery_pulse_status.json",
            payload,
            base_dir=root / "outputs",
        )
        safe_write_text(
            OutputNamespace.LATEST,
            "discovery_pulse_status.md",
            _render_md(payload),
            base_dir=root / "outputs",
        )
    except Exception as exc:
        logger.warning("discovery_pulse: artifact write failed: %s", exc)


if __name__ == "__main__":
    import sys
    root_arg = Path(__file__).resolve().parents[1]
    # Load .env via the project's own parser (handles values with spaces /
    # special chars that bash sourcing would mangle).
    try:
        sys.path.insert(0, str(root_arg))
        from utils import load_env
        load_env(str(root_arg / ".env"))
    except Exception as exc:
        logger.warning("discovery_pulse: load_env failed (%s) — continuing with shell env", exc)
    result = run_discovery_pulse(root=root_arg)
    skipped = result.get("skipped")
    if skipped:
        print(f"discovery_pulse: SKIPPED ({result.get('skip_reason')})")
    else:
        ta = result.get("tier_a") or {}
        tb = result.get("tier_b") or {}
        sa = result.get("sanitation") or {}
        promo = (ta.get("promotion") or {})
        promo_str = (
            f"promoted={len(promo.get('promoted') or [])}, "
            f"reinforced={len(promo.get('reinforced') or [])}"
        )
        print(
            f"discovery_pulse: ran — "
            f"tier_a={ta.get('status','?')} ({ta.get('themes_count',0)} themes, {promo_str}), "
            f"tier_b={tb.get('status','?')} ({tb.get('evidence_count',0)} evidence, "
            f"universe={tb.get('universe_size','?')}), "
            f"sanitation={sa.get('status','?')} (top={sa.get('top_count',0)})"
        )
    sys.exit(0)
