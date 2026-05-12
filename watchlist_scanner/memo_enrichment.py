"""
Daily memo enrichment — operator-readable sections.
====================================================

Adds four sections to the daily memo:

  - Portfolio Growth      (total value over time from SQLite snapshots)
  - Top Movers            (held positions' 1-day price changes)
  - Decision Hit Rate     (predicted-correctly-vs-not feedback loop)
  - What To Watch         (sandbox research candidates with news context)

Module layout:

  * compute_*    : pure functions that read structured inputs and return a
                   dict.  Easy to test.  Never raise on bad input — always
                   return a dict with ``available=False`` and a ``reason``.
  * render_*_text/_md : pure formatting helpers that turn a compute dict
                   into a list of body lines.  Renderers degrade gracefully:
                   if ``available=False``, they emit a single "data not
                   yet available" line.
  * load_enrichment_data : convenience loader that reads all four sources
                   from disk and returns a single dict for the daily memo
                   to consume.

Safety:

  * Read-only.  No file writes from this module.
  * No trading-instruction language in any rendered output.
  * "What To Watch" carries the sandbox-only disclaimer.
  * Section headers and bullet formatting match the existing daily_memo
    pattern (48-char separator, "  - " bullets, plain text + Markdown
    variants).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_USER_ID = "owner"
_DEFAULT_WINDOW_DAYS = 30
_DEFAULT_RECENT_DAYS = 7
_DEFAULT_TOP_N = 3
_DEFAULT_WHAT_TO_WATCH_N = 5

_SANDBOX_DISCLAIMER = (
    "Sandbox research only — not a buy/sell/hold recommendation."
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("memo_enrichment: failed to load %s: %s", path, exc)
        return None


def _safe_load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                pass
    except Exception as exc:
        logger.warning("memo_enrichment: failed to load JSONL %s: %s", path, exc)
    return out


def _parse_ts(value: Any) -> datetime | None:
    """Best-effort parse of an ISO timestamp into a naive datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    s = str(value).strip()
    if not s:
        return None
    # Strip trailing Z or timezone offset for naive comparison
    for trim in ("Z", "+00:00"):
        if s.endswith(trim):
            s = s[: -len(trim)]
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except Exception:
            return None


def _fmt_money(value: float | int | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    try:
        f = float(value)
    except Exception:
        return "—"
    if signed:
        sign = "+" if f >= 0 else "-"
        return f"{sign}${abs(f):,.2f}"
    return f"${f:,.2f}"


def _fmt_pct(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    try:
        f = float(value)
    except Exception:
        return "—"
    if signed:
        sign = "+" if f >= 0 else "-"
        return f"{sign}{abs(f):.2f}%"
    return f"{f:.2f}%"


# ---------------------------------------------------------------------------
# Compute: Portfolio Growth (SQLite snapshots → value over time)
# ---------------------------------------------------------------------------

def compute_portfolio_growth(
    db_path: str | Path,
    *,
    user_id: str = _DEFAULT_USER_ID,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Query the ``snapshots`` table and compute portfolio-level growth metrics.

    Returns a dict with::

        {
          "available": bool,
          "today_value": float,
          "today_cash": float,
          "as_of": str (ISO),
          "delta_day":   (delta_$, delta_pct) | None,
          "delta_week":  (delta_$, delta_pct) | None,
          "delta_month": (delta_$, delta_pct) | None,
          "delta_ytd":   (delta_$, delta_pct) | None,
        }

    Degrades gracefully when the database is missing, empty, or unreadable
    (returns ``available: False`` with a ``reason``).
    """
    now = now or datetime.now()
    db = Path(db_path)
    if not db.exists():
        return {"available": False, "reason": "db_missing"}

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db))
        latest = conn.execute(
            "SELECT total_value, cash, recorded_at FROM snapshots "
            "WHERE user_id=? AND total_value IS NOT NULL "
            "ORDER BY recorded_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not latest:
            return {"available": False, "reason": "no_snapshots"}

        latest_value, latest_cash, latest_at = latest
        latest_value = float(latest_value or 0)
        latest_cash = float(latest_cash or 0)
        anchor_dt = _parse_ts(latest_at) or now

        def _snapshot_before(target_dt: datetime) -> float | None:
            row = conn.execute(
                "SELECT total_value FROM snapshots "
                "WHERE user_id=? AND total_value IS NOT NULL AND recorded_at < ? "
                "ORDER BY recorded_at DESC LIMIT 1",
                (user_id, target_dt.isoformat()),
            ).fetchone()
            if not row or row[0] is None:
                return None
            try:
                return float(row[0])
            except Exception:
                return None

        # Look "just before now" for "yesterday" (anything older than 12h)
        yesterday_val = _snapshot_before(anchor_dt - timedelta(hours=12))
        week_val = _snapshot_before(anchor_dt - timedelta(days=7))
        month_val = _snapshot_before(anchor_dt - timedelta(days=30))
        ytd_val = _snapshot_before(datetime(anchor_dt.year, 1, 1))

        def _delta(prior: float | None) -> tuple[float, float] | None:
            if prior is None or prior == 0:
                return None
            d_dollar = latest_value - prior
            d_pct = (d_dollar / prior) * 100.0
            return (round(d_dollar, 2), round(d_pct, 2))

        return {
            "available": True,
            "today_value": round(latest_value, 2),
            "today_cash": round(latest_cash, 2),
            "as_of": str(latest_at),
            "delta_day": _delta(yesterday_val),
            "delta_week": _delta(week_val),
            "delta_month": _delta(month_val),
            "delta_ytd": _delta(ytd_val),
        }
    except Exception as exc:
        logger.warning("memo_enrichment: compute_portfolio_growth failed: %s", exc)
        return {"available": False, "reason": f"error: {exc.__class__.__name__}"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Compute: Top Movers (held positions × 1-day price change)
# ---------------------------------------------------------------------------

def compute_top_movers(
    holdings: list[dict] | None,
    watchlist_signals: dict | list | None,
    *,
    top_n: int = _DEFAULT_TOP_N,
) -> dict[str, Any]:
    """
    For each held symbol that also has a watchlist signal with a 1-day price
    change, rank top winners and losers.

    Parameters
    ----------
    holdings:
        List of dicts with keys ``symbol`` (str) and ``shares`` (number).
    watchlist_signals:
        Either a dict with a ``signals`` list, or the list directly.  Each
        signal dict should have ``symbol``, ``price``, and ``price_change_1d``.
    top_n:
        How many winners and how many losers to surface.
    """
    if not isinstance(holdings, list) or not holdings:
        return {"available": False, "reason": "no_holdings"}

    # Normalize signal list shape
    if isinstance(watchlist_signals, dict):
        signal_list = watchlist_signals.get("signals") or watchlist_signals.get("symbols") or []
    elif isinstance(watchlist_signals, list):
        signal_list = watchlist_signals
    else:
        signal_list = []

    if not signal_list:
        return {"available": False, "reason": "no_signals"}

    signal_index: dict[str, dict] = {}
    for s in signal_list:
        if not isinstance(s, dict):
            continue
        sym = s.get("symbol") or s.get("ticker")
        if sym:
            signal_index[str(sym).upper().strip()] = s

    enriched: list[dict[str, Any]] = []
    for h in holdings:
        if not isinstance(h, dict):
            continue
        sym = str(h.get("symbol") or h.get("ticker") or "").upper().strip()
        shares_raw = h.get("shares")
        if not sym or shares_raw is None:
            continue
        try:
            shares = float(shares_raw)
        except Exception:
            continue
        sig = signal_index.get(sym)
        if sig is None:
            continue
        # Watchlist signal shapes vary — try a few common keys
        price_raw = sig.get("price")
        if price_raw is None:
            tech = sig.get("technicals") if isinstance(sig.get("technicals"), dict) else {}
            price_raw = tech.get("price") if isinstance(tech, dict) else None
        change_raw = sig.get("price_change_1d")
        if change_raw is None:
            tech = sig.get("technicals") if isinstance(sig.get("technicals"), dict) else {}
            change_raw = tech.get("price_change_1d") if isinstance(tech, dict) else None
        if price_raw is None or change_raw is None:
            continue
        try:
            price = float(price_raw)
            change_pct = float(change_raw)
        except Exception:
            continue
        # Approximate prior-close: price / (1 + pct/100); $-change = (price - prior) * shares
        if (1 + change_pct / 100) == 0:
            continue
        prior_price = price / (1 + change_pct / 100)
        change_dollar = (price - prior_price) * shares

        enriched.append({
            "symbol": sym,
            "shares": shares,
            "price": round(price, 4),
            "change_1d_pct": round(change_pct, 2),
            "change_1d_dollar": round(change_dollar, 2),
            "position_value": round(price * shares, 2),
        })

    if not enriched:
        return {
            "available": False,
            "reason": "no_price_data_for_held",
            "total_held": len(holdings),
        }

    sorted_by_pct = sorted(enriched, key=lambda x: x["change_1d_pct"], reverse=True)

    return {
        "available": True,
        "total_held": len(holdings),
        "total_covered": len(enriched),
        "winners": sorted_by_pct[:top_n],
        "losers": list(reversed(sorted_by_pct[-top_n:])),
    }


# ---------------------------------------------------------------------------
# Compute: Decision Hit Rate (decision_outcomes.jsonl + calibration)
# ---------------------------------------------------------------------------

def compute_decision_hit_rate(
    decision_outcomes: list[dict] | None,
    calibration: dict | None,
    *,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    recent_days: int = _DEFAULT_RECENT_DAYS,
    top_n: int = _DEFAULT_TOP_N,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Aggregate predicted-vs-actual outcomes from ``decision_outcomes.jsonl``
    and surface bucket hit rates from ``confidence_calibration.json``.

    Returns a dict with overall hit rate over ``window_days``, recent
    correct/missed calls over ``recent_days``, and per-bucket hit rates if
    available from the calibration artifact.
    """
    now = now or datetime.now()
    outcomes: list[dict] = decision_outcomes if isinstance(decision_outcomes, list) else []
    cal: dict = calibration if isinstance(calibration, dict) else {}

    cutoff_window = now - timedelta(days=window_days)
    cutoff_recent = now - timedelta(days=recent_days)

    resolved = [
        r for r in outcomes
        if isinstance(r, dict)
        and r.get("resolved")
        and r.get("direction_correct") is not None
    ]

    window_records: list[dict] = []
    very_recent: list[dict] = []
    for r in resolved:
        ts = _parse_ts(r.get("resolved_at") or r.get("timestamp") or r.get("date"))
        if ts is None:
            continue
        if ts >= cutoff_window:
            window_records.append(r)
            if ts >= cutoff_recent:
                very_recent.append(r)

    correct_count = sum(1 for r in window_records if r.get("direction_correct"))
    total_count = len(window_records)
    hit_rate = (correct_count / total_count * 100.0) if total_count else None

    correct_calls = [r for r in very_recent if r.get("direction_correct")]
    missed_calls = [r for r in very_recent if r.get("direction_correct") is False]

    # Sort by return_pct magnitude (best wins / worst misses first)
    def _abs_return(r: dict) -> float:
        try:
            return abs(float(r.get("return_pct") or 0))
        except Exception:
            return 0.0

    correct_calls.sort(key=_abs_return, reverse=True)
    missed_calls.sort(key=_abs_return, reverse=True)

    # Bucket hit rates from calibration
    bucket_hits: dict[str, dict[str, Any]] = {}
    buckets_raw = cal.get("confidence_buckets") if isinstance(cal.get("confidence_buckets"), dict) else {}
    for name, data in buckets_raw.items():
        if not isinstance(data, dict):
            continue
        hr = data.get("hit_rate")
        if hr is None:
            continue
        bucket_hits[name] = {
            "hit_rate": float(hr),
            "count": int(data.get("count") or 0),
            "avg_return": (float(data.get("avg_return")) if data.get("avg_return") is not None else None),
        }

    available = total_count > 0 or bool(bucket_hits)
    return {
        "available": available,
        "window_days": window_days,
        "resolved_count": total_count,
        "correct_count": correct_count,
        "hit_rate_pct": (round(hit_rate, 2) if hit_rate is not None else None),
        "bucket_hit_rates": bucket_hits,
        "recent_correct": [
            {
                "symbol": str(r.get("symbol") or ""),
                "decision": str(r.get("decision") or ""),
                "return_pct": (round(float(r["return_pct"]), 2) if r.get("return_pct") is not None else None),
            }
            for r in correct_calls[:top_n]
        ],
        "recent_missed": [
            {
                "symbol": str(r.get("symbol") or ""),
                "decision": str(r.get("decision") or ""),
                "return_pct": (round(float(r["return_pct"]), 2) if r.get("return_pct") is not None else None),
            }
            for r in missed_calls[:top_n]
        ],
        "calibration_total_resolved": cal.get("total_resolved"),
        "calibration_overall_hit_rate": cal.get("overall_hit_rate"),
    }


# ---------------------------------------------------------------------------
# Compute: What To Watch (sandbox research candidates with news context)
# ---------------------------------------------------------------------------

def compute_what_to_watch(
    auto_promotion: dict | None,
    news_evidence: dict | None,
    *,
    top_n: int = _DEFAULT_WHAT_TO_WATCH_N,
) -> dict[str, Any]:
    """
    Surface MONITOR-status sandbox candidates with their news context.

    Strictly research-only.  No promotion implied.  Output carries the
    sandbox disclaimer text.
    """
    auto = auto_promotion if isinstance(auto_promotion, dict) else {}
    news = news_evidence if isinstance(news_evidence, dict) else {}

    decisions = auto.get("decisions") if isinstance(auto.get("decisions"), list) else []
    monitor = [
        d for d in decisions
        if isinstance(d, dict) and str(d.get("proposed_status") or "").upper() == "MONITOR"
    ]
    needs_review = [
        d for d in decisions
        if isinstance(d, dict) and str(d.get("proposed_status") or "").upper() == "NEEDS_REVIEW"
    ]

    # News context index
    news_index: dict[str, dict] = {}
    contexts = news.get("ticker_contexts") if isinstance(news.get("ticker_contexts"), list) else []
    for tc in contexts:
        if not isinstance(tc, dict):
            continue
        sym = str(tc.get("ticker") or tc.get("symbol") or "").upper().strip()
        if sym:
            news_index[sym] = tc

    def _entry(d: dict) -> dict[str, Any]:
        sym = str(d.get("ticker") or "").upper().strip()
        ne = news_index.get(sym, {})
        catalyst_flags = d.get("catalyst_flags") or []
        if not isinstance(catalyst_flags, list):
            catalyst_flags = []
        return {
            "ticker": sym,
            "evidence_score": (
                round(float(d["evidence_score"]), 3)
                if d.get("evidence_score") is not None else None
            ),
            "corroboration_score": (
                round(float(d["corroboration_score"]), 3)
                if d.get("corroboration_score") is not None else None
            ),
            "news_relevance_score": (
                round(float(d["news_relevance_score"]), 3)
                if d.get("news_relevance_score") is not None else None
            ),
            "news_evidence_strength": ne.get("evidence_strength"),
            "news_context_effect": ne.get("context_effect"),
            "catalyst_flags": [str(f) for f in catalyst_flags[:3] if str(f)],
            "reason": str(d.get("reason") or "")[:140],
        }

    def _sort_key(d: dict) -> float:
        try:
            return float(d.get("evidence_score") or 0)
        except Exception:
            return 0.0

    monitor_sorted = sorted(monitor, key=_sort_key, reverse=True)[:top_n]
    review_sorted = sorted(needs_review, key=_sort_key, reverse=True)[:top_n]

    return {
        "available": bool(monitor or needs_review),
        "monitor_count": len(monitor),
        "needs_review_count": len(needs_review),
        "monitor_top": [_entry(d) for d in monitor_sorted],
        "needs_review_top": [_entry(d) for d in review_sorted],
        "safety_disclaimer": _SANDBOX_DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Renderers — plain text
# ---------------------------------------------------------------------------

def render_growth_text(growth: dict[str, Any]) -> list[str]:
    if not growth.get("available"):
        return ["Portfolio growth data not yet available."]
    lines: list[str] = []
    val = growth.get("today_value")
    cash = growth.get("today_cash")
    lines.append(f"Total value: {_fmt_money(val)}   (cash: {_fmt_money(cash)})")
    for label, key in (
        ("Today vs prior", "delta_day"),
        ("Past 7 days", "delta_week"),
        ("Past 30 days", "delta_month"),
        ("Year to date", "delta_ytd"),
    ):
        d = growth.get(key)
        if d is None:
            continue
        d_dollar, d_pct = d
        lines.append(
            f"{label}: {_fmt_money(d_dollar, signed=True)} "
            f"({_fmt_pct(d_pct, signed=True)})"
        )
    return lines


def render_top_movers_text(movers: dict[str, Any]) -> list[str]:
    if not movers.get("available"):
        if movers.get("reason") == "no_holdings":
            return ["No portfolio holdings configured."]
        if movers.get("reason") == "no_price_data_for_held":
            held = movers.get("total_held", 0)
            return [f"Price data not yet available for {held} held position(s)."]
        return ["Top movers data not yet available."]

    lines: list[str] = []
    coverage = f"{movers.get('total_covered', 0)}/{movers.get('total_held', 0)}"
    lines.append(f"Coverage: {coverage} held positions have price signal today.")
    winners = movers.get("winners") or []
    losers = movers.get("losers") or []
    if winners:
        lines.append("Top up:")
        for w in winners:
            lines.append(
                f"  {w['symbol']}: "
                f"{_fmt_pct(w['change_1d_pct'], signed=True)}  "
                f"({_fmt_money(w['change_1d_dollar'], signed=True)} on "
                f"{w['shares']:g} shares)"
            )
    if losers:
        lines.append("Top down:")
        for losr in losers:
            lines.append(
                f"  {losr['symbol']}: "
                f"{_fmt_pct(losr['change_1d_pct'], signed=True)}  "
                f"({_fmt_money(losr['change_1d_dollar'], signed=True)} on "
                f"{losr['shares']:g} shares)"
            )
    return lines


def render_hit_rate_text(hr: dict[str, Any]) -> list[str]:
    if not hr.get("available"):
        return ["Decision hit-rate data not yet available."]

    lines: list[str] = []
    n = hr.get("resolved_count", 0)
    rate = hr.get("hit_rate_pct")
    if n > 0 and rate is not None:
        lines.append(
            f"Past {hr.get('window_days', 30)} days: "
            f"{hr.get('correct_count', 0)} of {n} resolved decisions correct "
            f"({rate:.1f}%)."
        )
    elif hr.get("calibration_overall_hit_rate") is not None:
        cal_rate = hr.get("calibration_overall_hit_rate")
        cal_n = hr.get("calibration_total_resolved")
        try:
            cal_pct = float(cal_rate) * 100.0 if cal_rate <= 1.0 else float(cal_rate)
            lines.append(
                f"Calibration: {cal_pct:.1f}% overall hit rate "
                f"({cal_n or 0} resolved)."
            )
        except Exception:
            pass
    else:
        lines.append("Not yet enough resolved decisions for a hit rate.")

    buckets = hr.get("bucket_hit_rates") or {}
    if buckets:
        for name in ("very_low", "low", "medium", "high", "very_high"):
            if name in buckets:
                b = buckets[name]
                hr_val = b.get("hit_rate")
                if hr_val is None:
                    continue
                try:
                    pct = float(hr_val) * 100.0 if hr_val <= 1.0 else float(hr_val)
                    lines.append(
                        f"  {name:>10s} confidence: {pct:.1f}% "
                        f"({b.get('count', 0)} resolved)"
                    )
                except Exception:
                    pass

    correct = hr.get("recent_correct") or []
    if correct:
        lines.append("Recent correct calls:")
        for c in correct:
            lines.append(
                f"  {c['decision']} {c['symbol']}: "
                f"{_fmt_pct(c['return_pct'], signed=True)}"
            )
    missed = hr.get("recent_missed") or []
    if missed:
        lines.append("Recent missed calls:")
        for m in missed:
            lines.append(
                f"  {m['decision']} {m['symbol']}: "
                f"{_fmt_pct(m['return_pct'], signed=True)}"
            )
    return lines


def render_what_to_watch_text(wtw: dict[str, Any]) -> list[str]:
    if not wtw.get("available"):
        return ["No sandbox research candidates in MONITOR or NEEDS_REVIEW state."]

    lines: list[str] = []
    lines.append(
        f"Monitor: {wtw.get('monitor_count', 0)}  |  "
        f"Needs review: {wtw.get('needs_review_count', 0)}"
    )
    monitor = wtw.get("monitor_top") or []
    if monitor:
        lines.append("Monitor (top by evidence):")
        for e in monitor:
            ticker = e.get("ticker") or "?"
            score = e.get("evidence_score")
            score_str = f"score={score:.2f}" if score is not None else "score=—"
            cats = e.get("catalyst_flags") or []
            cat_str = f" | {', '.join(cats)}" if cats else ""
            lines.append(f"  {ticker}: {score_str}{cat_str}")
    review = wtw.get("needs_review_top") or []
    if review:
        lines.append("Needs review (operator inspection):")
        for e in review:
            ticker = e.get("ticker") or "?"
            score = e.get("evidence_score")
            score_str = f"score={score:.2f}" if score is not None else "score=—"
            lines.append(f"  {ticker}: {score_str}")
    lines.append(f"[ {wtw.get('safety_disclaimer') or _SANDBOX_DISCLAIMER} ]")
    return lines


# ---------------------------------------------------------------------------
# Renderers — Markdown
# ---------------------------------------------------------------------------

def render_growth_md(growth: dict[str, Any]) -> list[str]:
    if not growth.get("available"):
        return ["_Portfolio growth data not yet available._"]
    lines: list[str] = []
    val = growth.get("today_value")
    cash = growth.get("today_cash")
    lines.append(
        f"- **Total value:** {_fmt_money(val)}  (cash: {_fmt_money(cash)})"
    )
    for label, key in (
        ("Today vs prior", "delta_day"),
        ("Past 7 days", "delta_week"),
        ("Past 30 days", "delta_month"),
        ("Year to date", "delta_ytd"),
    ):
        d = growth.get(key)
        if d is None:
            continue
        d_dollar, d_pct = d
        lines.append(
            f"- **{label}:** {_fmt_money(d_dollar, signed=True)} "
            f"({_fmt_pct(d_pct, signed=True)})"
        )
    return lines


def render_top_movers_md(movers: dict[str, Any]) -> list[str]:
    if not movers.get("available"):
        if movers.get("reason") == "no_holdings":
            return ["_No portfolio holdings configured._"]
        if movers.get("reason") == "no_price_data_for_held":
            held = movers.get("total_held", 0)
            return [f"_Price data not yet available for {held} held position(s)._"]
        return ["_Top movers data not yet available._"]

    lines: list[str] = []
    coverage = f"{movers.get('total_covered', 0)}/{movers.get('total_held', 0)}"
    lines.append(f"- _Coverage:_ {coverage} held positions have price signal today.")
    winners = movers.get("winners") or []
    losers = movers.get("losers") or []
    if winners:
        lines.append("- **Top up:**")
        for w in winners:
            lines.append(
                f"  - `{w['symbol']}`: "
                f"{_fmt_pct(w['change_1d_pct'], signed=True)}  "
                f"({_fmt_money(w['change_1d_dollar'], signed=True)} on "
                f"{w['shares']:g} shares)"
            )
    if losers:
        lines.append("- **Top down:**")
        for losr in losers:
            lines.append(
                f"  - `{losr['symbol']}`: "
                f"{_fmt_pct(losr['change_1d_pct'], signed=True)}  "
                f"({_fmt_money(losr['change_1d_dollar'], signed=True)} on "
                f"{losr['shares']:g} shares)"
            )
    return lines


def render_hit_rate_md(hr: dict[str, Any]) -> list[str]:
    if not hr.get("available"):
        return ["_Decision hit-rate data not yet available._"]

    lines: list[str] = []
    n = hr.get("resolved_count", 0)
    rate = hr.get("hit_rate_pct")
    if n > 0 and rate is not None:
        lines.append(
            f"- **Past {hr.get('window_days', 30)} days:** "
            f"{hr.get('correct_count', 0)} of {n} resolved decisions correct "
            f"({rate:.1f}%)."
        )
    elif hr.get("calibration_overall_hit_rate") is not None:
        cal_rate = hr.get("calibration_overall_hit_rate")
        cal_n = hr.get("calibration_total_resolved")
        try:
            cal_pct = float(cal_rate) * 100.0 if cal_rate <= 1.0 else float(cal_rate)
            lines.append(
                f"- **Calibration:** {cal_pct:.1f}% overall hit rate "
                f"({cal_n or 0} resolved)."
            )
        except Exception:
            pass
    else:
        lines.append("- _Not yet enough resolved decisions for a hit rate._")

    buckets = hr.get("bucket_hit_rates") or {}
    if buckets:
        bucket_lines = []
        for name in ("very_low", "low", "medium", "high", "very_high"):
            if name in buckets:
                b = buckets[name]
                hr_val = b.get("hit_rate")
                if hr_val is None:
                    continue
                try:
                    pct = float(hr_val) * 100.0 if hr_val <= 1.0 else float(hr_val)
                    bucket_lines.append(
                        f"  - `{name}`: {pct:.1f}% ({b.get('count', 0)} resolved)"
                    )
                except Exception:
                    pass
        if bucket_lines:
            lines.append("- **By confidence bucket:**")
            lines.extend(bucket_lines)

    correct = hr.get("recent_correct") or []
    if correct:
        lines.append("- **Recent correct calls:**")
        for c in correct:
            lines.append(
                f"  - `{c['decision']}` `{c['symbol']}`: "
                f"{_fmt_pct(c['return_pct'], signed=True)}"
            )
    missed = hr.get("recent_missed") or []
    if missed:
        lines.append("- **Recent missed calls:**")
        for m in missed:
            lines.append(
                f"  - `{m['decision']}` `{m['symbol']}`: "
                f"{_fmt_pct(m['return_pct'], signed=True)}"
            )
    return lines


def render_what_to_watch_md(wtw: dict[str, Any]) -> list[str]:
    if not wtw.get("available"):
        return ["_No sandbox research candidates in MONITOR or NEEDS_REVIEW state._"]

    lines: list[str] = []
    lines.append(
        f"- _Monitor:_ {wtw.get('monitor_count', 0)}  |  "
        f"_Needs review:_ {wtw.get('needs_review_count', 0)}"
    )
    monitor = wtw.get("monitor_top") or []
    if monitor:
        lines.append("- **Monitor (top by evidence):**")
        for e in monitor:
            ticker = e.get("ticker") or "?"
            score = e.get("evidence_score")
            score_str = f"score={score:.2f}" if score is not None else "score=—"
            cats = e.get("catalyst_flags") or []
            cat_str = f" — {', '.join(cats)}" if cats else ""
            lines.append(f"  - `{ticker}`: {score_str}{cat_str}")
    review = wtw.get("needs_review_top") or []
    if review:
        lines.append("- **Needs review (operator inspection):**")
        for e in review:
            ticker = e.get("ticker") or "?"
            score = e.get("evidence_score")
            score_str = f"score={score:.2f}" if score is not None else "score=—"
            lines.append(f"  - `{ticker}`: {score_str}")
    lines.append(f"- _{wtw.get('safety_disclaimer') or _SANDBOX_DISCLAIMER}_")
    return lines


# ---------------------------------------------------------------------------
# Convenience loader — load all four data sources at once
# ---------------------------------------------------------------------------

@dataclass
class EnrichmentSources:
    """All inputs the enrichment functions need, in one container."""
    db_path: Path
    holdings: list[dict]
    watchlist_signals: dict | list
    decision_outcomes: list[dict]
    calibration: dict
    auto_promotion: dict
    news_evidence: dict


def load_enrichment_data(
    repo_root: str | Path,
    *,
    config_path: str | Path | None = None,
) -> EnrichmentSources:
    """
    Read the four data sources required by the compute functions.

    Missing files degrade silently — the compute functions handle empty inputs.

    Parameters
    ----------
    repo_root:
        Project root containing ``data/``, ``outputs/``, ``config.json``.
    config_path:
        Override for the config file (defaults to ``repo_root/config.json``).
    """
    root = Path(repo_root)
    cfg_path = Path(config_path) if config_path else (root / "config.json")
    config = _safe_load_json(cfg_path) or {}
    portfolio = config.get("portfolio") if isinstance(config.get("portfolio"), dict) else {}
    holdings = portfolio.get("holdings") if isinstance(portfolio.get("holdings"), list) else []
    holdings = [h for h in holdings if isinstance(h, dict)]

    return EnrichmentSources(
        db_path=root / "data" / "portfolio.db",
        holdings=holdings,
        watchlist_signals=_safe_load_json(root / "outputs" / "latest" / "watchlist_signals.json") or {},
        decision_outcomes=_safe_load_jsonl(root / "outputs" / "policy" / "decision_outcomes.jsonl"),
        calibration=_safe_load_json(root / "outputs" / "latest" / "confidence_calibration.json") or {},
        auto_promotion=_safe_load_json(
            root / "outputs" / "sandbox" / "discovery" / "automatic_promotion_candidates.json"
        ) or {},
        news_evidence=_safe_load_json(root / "outputs" / "latest" / "news_evidence_layer.json") or {},
    )


def build_enrichment(repo_root: str | Path) -> dict[str, dict[str, Any]]:
    """
    One-shot helper: load all sources, compute all four sections, return
    a dict the daily memo can render directly.
    """
    src = load_enrichment_data(repo_root)
    return {
        "growth": compute_portfolio_growth(src.db_path),
        "movers": compute_top_movers(src.holdings, src.watchlist_signals),
        "hit_rate": compute_decision_hit_rate(src.decision_outcomes, src.calibration),
        "what_to_watch": compute_what_to_watch(src.auto_promotion, src.news_evidence),
    }
