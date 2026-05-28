"""
Universe Sanitation — aggregate and rank the dynamic ticker universe at
daily / weekly / monthly cadence.

Inputs (all read-only, no API calls):
  - config.json:watchlist_scanner.watchlist             (static seed)
  - data/portfolio.db:extended_watchlist (active rows)  (theme-promoted)
  - outputs/latest/watch_candidates.json                (today's theme candidates)
  - outputs/latest/signal_outcomes.csv (last N days)    (live signal pool)
  - data/fmp_cache/top100_watchlist.json (candidates)   (FMP-scored top-100)
  - outputs/history/<date>/top100_daily.json            (historical snapshots for weekly/monthly rollups)
  - data/fmp_cache/profile_stable_<TICKER>.json         (sector enrichment)

Output artifacts:
  - outputs/latest/top100_daily.json + .md   (refreshed by discovery_pulse + daily cron)
  - outputs/latest/top100_weekly.json + .md  (refreshed by run_weekly_safe.sh)
  - outputs/latest/top100_monthly.json + .md (refreshed by run_weekly_safe.sh; 30d rolling)

Hard guarantees:
  - observe_only=True hardcoded
  - Never modifies portfolio, allocation, scoring, or decision state
  - Degrades safely when any input is missing
  - Top-100 cap enforced; ties broken deterministically

Ranking score (per ticker):
    score = 0.40 * presence_in_sources
          + 0.30 * max_theme_confidence
          + 0.20 * recent_hit_rate
          + 0.10 * in_fmp_top100
where presence_in_sources is the count of distinct sources the ticker
appears in (static, extended_watchlist, theme_candidate, recent_signal,
fmp_top100), normalized to [0, 1].

Public API:
  build_top100_daily(root, *, lookback_days=1) -> dict
  build_top100_weekly(root) -> dict
  build_top100_monthly(root) -> dict
  run_universe_sanitation(root, cadence, write_files=True) -> dict
"""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.universe_sanitation")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "universe_sanitation"
_OBSERVE_ONLY = True
_TOP_N = 100

_DISCLAIMER = (
    "Observe-only universe sanitation. Aggregates dynamic ticker sources "
    "into a ranked top-{N} view at the named cadence. Does not modify "
    "portfolio, allocation, scoring, or decision state."
).replace("{N}", str(_TOP_N))

# Score weights — keep these summing to 1.0
_W_SOURCES = 0.40
_W_THEME_CONF = 0.30
_W_RECENT_HITRATE = 0.20
_W_FMP_TOP100 = 0.10

_KNOWN_SOURCES = ("static", "extended_watchlist", "theme_candidate", "recent_signal", "fmp_top100")


# ---------------------------------------------------------------------------
# Input loaders (each is safe-by-default)
# ---------------------------------------------------------------------------


def _load_json_safe(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _load_static_watchlist(root: Path) -> list[str]:
    cfg = _load_json_safe(root / "config.json") or {}
    return [
        s.upper()
        for s in (cfg.get("watchlist_scanner") or {}).get("watchlist") or []
        if isinstance(s, str)
    ]


def _load_extended_active(root: Path) -> list[dict[str, Any]]:
    """Return active rows from extended_watchlist DB. Empty list on any failure."""
    db_path = root / "data" / "portfolio.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT symbol, theme_confidence, theme_name, promoted_at "
            "FROM extended_watchlist WHERE is_active=1"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("universe_sanitation: extended_watchlist read failed: %s", exc)
        return []


def _load_theme_candidates(root: Path) -> list[dict[str, Any]]:
    d = _load_json_safe(root / "outputs" / "latest" / "watch_candidates.json")
    if d is None:
        return []
    if isinstance(d, list):
        return [c for c in d if isinstance(c, dict)]
    if isinstance(d, dict):
        cands = d.get("candidates") or d.get("watch_candidates") or []
        return [c for c in cands if isinstance(c, dict)]
    return []


def _load_fmp_top100(root: Path) -> list[dict[str, Any]]:
    d = _load_json_safe(root / "data" / "fmp_cache" / "top100_watchlist.json")
    if d is None:
        return []
    cands = d.get("candidates") if isinstance(d, dict) else None
    if not cands:
        return []
    # Exclude fallback-only entries — they're zero-score and uninformative
    real = [c for c in cands if c.get("watchlist_source") != "fallback"]
    return real or cands  # if everything's fallback, still surface so reader knows


def _load_recent_signals(root: Path, lookback_days: int) -> dict[str, dict[str, Any]]:
    """Aggregate signal_outcomes.csv into per-ticker stats over the lookback
    window. Returns {ticker: {count, hits_1d, resolved_1d, last_signal_time}}.
    """
    csv_path = root / "outputs" / "performance" / "signal_outcomes.csv"
    if not csv_path.exists():
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    out: dict[str, dict[str, Any]] = {}
    try:
        with csv_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            r = csv.DictReader(f)
            for row in r:
                st = row.get("signal_time") or ""
                if st < cutoff:
                    continue
                t = (row.get("ticker") or "").upper()
                if not t:
                    continue
                bucket = out.setdefault(t, {
                    "count": 0, "resolved_1d": 0, "hits_1d": 0, "last_signal_time": None,
                })
                bucket["count"] += 1
                if bucket["last_signal_time"] is None or st > bucket["last_signal_time"]:
                    bucket["last_signal_time"] = st
                ret = row.get("outcome_return_1d") or ""
                if ret not in ("", None, "—"):
                    try:
                        _ = float(ret)
                        bucket["resolved_1d"] += 1
                        if row.get("direction_correct_1d") in ("1", "1.0", "True", "true"):
                            bucket["hits_1d"] += 1
                    except ValueError:
                        pass
    except Exception as exc:
        logger.debug("universe_sanitation: signal_outcomes read failed: %s", exc)
    return out


def _load_sector(root: Path, ticker: str) -> str:
    """Reuse the same FMP profile cache convention as retune_impact_tracker."""
    p = root / "data" / "fmp_cache" / f"profile_stable_{ticker}.json"
    if not p.exists():
        return "Unknown"
    d = _load_json_safe(p)
    if not isinstance(d, dict):
        return "Unknown"
    data = d.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        s = data.get("sector")
        if isinstance(s, str) and s.strip():
            return s.strip()
    return "Unknown"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _new_rec() -> dict[str, Any]:
    return {
        "sources": [],
        "theme_confidence": 0.0,
        "theme_names": [],          # theme names that surfaced this ticker
        "extended_themes": [],      # theme_name field from extended_watchlist rows
        "fmp_score": None,          # candidate row's score field if available
        "fmp_sector": None,
    }


def _aggregate_universe(
    root: Path,
    *,
    lookback_days: int,
) -> dict[str, dict[str, Any]]:
    """Combine all sources into a per-ticker record. Returns
    {ticker: {sources, theme_confidence, theme_names, extended_themes,
    fmp_score, fmp_sector, signal}}.
    """
    by_sym: dict[str, dict[str, Any]] = {}

    for sym in _load_static_watchlist(root):
        rec = by_sym.setdefault(sym, _new_rec())
        rec["sources"].append("static")

    for row in _load_extended_active(root):
        sym = (row.get("symbol") or "").upper()
        if not sym:
            continue
        rec = by_sym.setdefault(sym, _new_rec())
        rec["sources"].append("extended_watchlist")
        rec["theme_confidence"] = max(
            rec["theme_confidence"], float(row.get("theme_confidence") or 0.0)
        )
        theme_name = row.get("theme_name")
        if isinstance(theme_name, str) and theme_name not in rec["extended_themes"]:
            rec["extended_themes"].append(theme_name)

    for c in _load_theme_candidates(root):
        sym = (c.get("ticker") or c.get("symbol") or "").upper()
        if not sym:
            continue
        rec = by_sym.setdefault(sym, _new_rec())
        rec["sources"].append("theme_candidate")
        rec["theme_confidence"] = max(
            rec["theme_confidence"], float(c.get("confidence") or 0.0)
        )
        for tn in (c.get("themes") or []):
            if isinstance(tn, str) and tn not in rec["theme_names"]:
                rec["theme_names"].append(tn)

    for fmp_row in _load_fmp_top100(root):
        sym = (fmp_row.get("symbol") or "").upper()
        if not sym:
            continue
        rec = by_sym.setdefault(sym, _new_rec())
        rec["sources"].append("fmp_top100")
        if rec["fmp_score"] is None:
            try:
                rec["fmp_score"] = float(fmp_row.get("score") or 0.0)
            except (TypeError, ValueError):
                rec["fmp_score"] = 0.0
        rec["fmp_sector"] = rec["fmp_sector"] or fmp_row.get("sector") or None

    sigs = _load_recent_signals(root, lookback_days)
    for sym, sb in sigs.items():
        rec = by_sym.setdefault(sym, _new_rec())
        rec["sources"].append("recent_signal")
        rec["signal"] = sb

    return by_sym


def _score(rec: dict[str, Any]) -> float:
    """Combine the four scoring factors into a 0..1 score."""
    distinct_sources = len(set(rec.get("sources") or []))
    presence = min(distinct_sources / len(_KNOWN_SOURCES), 1.0)
    theme_conf = max(0.0, min(1.0, float(rec.get("theme_confidence") or 0.0)))

    sig = rec.get("signal") or {}
    resolved = int(sig.get("resolved_1d") or 0)
    hit_rate = (int(sig.get("hits_1d") or 0) / resolved) if resolved else 0.0

    in_fmp = 1.0 if "fmp_top100" in (rec.get("sources") or []) else 0.0

    return round(
        _W_SOURCES * presence
        + _W_THEME_CONF * theme_conf
        + _W_RECENT_HITRATE * hit_rate
        + _W_FMP_TOP100 * in_fmp,
        4,
    )


def _build_rationale(
    rec: dict[str, Any],
    sources: list[str],
    hit_rate_1d: float | None,
    resolved_1d: int,
    sector: str,
) -> tuple[str, list[str], dict[str, str]]:
    """Return (human_reason, machine_tags, per_source_contribution).

    The tags are designed for the pattern-recognition learning loop: each
    is a stable categorical label so the engine can group winners/losers
    by tag and learn which patterns predict outcomes. See
    docs/learning_loop_plan.md for the consumer side.
    """
    tags: list[str] = []
    for src in sources:
        tags.append(f"source:{src}")

    theme_conf = float(rec.get("theme_confidence") or 0.0)
    if theme_conf >= 0.80:
        tags.append("high_theme_confidence")
    elif theme_conf >= 0.60 and theme_conf < 0.80:
        tags.append("medium_theme_confidence")

    if hit_rate_1d is not None:
        if hit_rate_1d >= 0.70:
            tags.append("high_hit_rate_1d")
        elif hit_rate_1d <= 0.30:
            tags.append("low_hit_rate_1d")
        elif 0.45 <= hit_rate_1d <= 0.55:
            tags.append("coin_flip_hit_rate_1d")

    n_sources = len(sources)
    if n_sources >= 3:
        tags.append("multi_source_confluence")
    elif n_sources == 1:
        tags.append("single_source")

    if "static" in sources:
        tags.append("established_static_seed")
    if "fmp_top100" in sources:
        tags.append("fmp_scored")
    if "theme_candidate" in sources and "static" not in sources:
        tags.append("net_new_discovery")
    if "extended_watchlist" in sources:
        tags.append("promoted_to_extended")

    if sector and sector != "Unknown":
        # Normalize to underscore-form for stable matching
        sector_tag = sector.replace(" ", "_").replace("/", "_")
        tags.append(f"sector:{sector_tag}")

    # Per-source contribution detail (for the human reason + downstream auditors)
    contrib: dict[str, str] = {}
    if "static" in sources:
        contrib["static"] = "Hardcoded seed in config.json watchlist_scanner.watchlist"
    if "extended_watchlist" in sources:
        themes = rec.get("extended_themes") or []
        theme_str = f" themes={','.join(themes[:3])}" if themes else ""
        contrib["extended_watchlist"] = (
            f"Active in extended_watchlist DB (theme_confidence "
            f"{theme_conf:.2f}{theme_str})"
        )
    if "theme_candidate" in sources:
        themes = rec.get("theme_names") or []
        theme_str = ", ".join(themes[:3]) if themes else "(theme name unrecorded)"
        contrib["theme_candidate"] = (
            f"Surfaced today by theme engine: {theme_str} (confidence {theme_conf:.2f})"
        )
    if "recent_signal" in sources:
        hr_str = f"{hit_rate_1d * 100:.0f}% over {resolved_1d}" if hit_rate_1d is not None else "unresolved"
        contrib["recent_signal"] = (
            f"Live signal pool: hit_rate_1d {hr_str} signal(s) in lookback window"
        )
    if "fmp_top100" in sources:
        fmp_score = rec.get("fmp_score")
        score_str = f"{fmp_score:.1f}/100" if fmp_score else "(score n/a)"
        contrib["fmp_top100"] = f"FMP-scored top-100 member: {score_str}"

    # Compose a single human-readable sentence
    parts = []
    if n_sources >= 3:
        parts.append(f"Multi-source confluence ({n_sources} signals)")
    if "theme_candidate" in sources:
        themes = rec.get("theme_names") or []
        if themes:
            parts.append(f"theme: {themes[0]} (conf {theme_conf:.2f})")
    if hit_rate_1d is not None and hit_rate_1d >= 0.70:
        parts.append(f"strong 1d hit-rate {hit_rate_1d * 100:.0f}% on n={resolved_1d}")
    elif hit_rate_1d is not None and hit_rate_1d <= 0.30 and resolved_1d >= 5:
        parts.append(f"weak 1d hit-rate {hit_rate_1d * 100:.0f}% on n={resolved_1d}")
    if "fmp_top100" in sources:
        parts.append("FMP-scored top-100 member")
    if "static" in sources and n_sources == 1:
        parts.append("static-seed only (no live confirming signals today)")
    if "theme_candidate" in sources and "static" not in sources:
        parts.append("NET-NEW discovery (not in static watchlist)")

    if not parts:
        parts.append("Present in the universe with weak corroborating signals")

    reason = ". ".join(parts) + "."
    return reason, tags, contrib


def _rank_candidates(
    by_sym: dict[str, dict[str, Any]],
    root: Path,
) -> list[dict[str, Any]]:
    """Materialize ranked candidate rows with sector + score + rationale."""
    rows: list[dict[str, Any]] = []
    for sym, rec in by_sym.items():
        sources = sorted(set(rec.get("sources") or []))
        sig = rec.get("signal") or {}
        resolved = int(sig.get("resolved_1d") or 0)
        hit_rate = round(int(sig.get("hits_1d") or 0) / resolved, 4) if resolved else None
        sector = _load_sector(root, sym)
        # Backfill sector from FMP if profile cache miss but row carries it
        if sector == "Unknown" and rec.get("fmp_sector"):
            sector = str(rec["fmp_sector"])
        reason, tags, contrib = _build_rationale(rec, sources, hit_rate, resolved, sector)
        rows.append({
            "symbol": sym,
            "sources": sources,
            "score": _score(rec),
            "theme_confidence_max": round(float(rec.get("theme_confidence") or 0.0), 4),
            "theme_names": list(rec.get("theme_names") or []),
            "recent_signal_count": int(sig.get("count") or 0),
            "recent_resolved_1d": resolved,
            "recent_hit_rate_1d": hit_rate,
            "last_signal_time": sig.get("last_signal_time"),
            "sector": sector,
            "fmp_score": rec.get("fmp_score"),
            "reason": reason,
            "rationale_tags": tags,
            "contributing_signals": contrib,
        })

    rows.sort(
        key=lambda r: (
            -r["score"],
            -len(r["sources"]),
            -(r["theme_confidence_max"] or 0.0),
            r["symbol"],
        )
    )
    for i, row in enumerate(rows[:_TOP_N], start=1):
        row["rank"] = i
    return rows[:_TOP_N]


def _source_breakdown(by_sym: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {s: 0 for s in _KNOWN_SOURCES}
    for rec in by_sym.values():
        for src in set(rec.get("sources") or []):
            if src in counts:
                counts[src] += 1
    return counts


# ---------------------------------------------------------------------------
# Cadence builders
# ---------------------------------------------------------------------------


def _build_payload(
    root: Path,
    cadence: str,
    lookback_days: int,
    by_sym: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ts = datetime.now(timezone.utc).isoformat()
    if by_sym is None:
        by_sym = _aggregate_universe(root, lookback_days=lookback_days)
    candidates = _rank_candidates(by_sym, root)
    return {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "schema_version": _SCHEMA_VERSION,
        "source": f"{_SOURCE_LABEL}.{cadence}",
        "cadence": cadence,
        "lookback_days": lookback_days,
        "total_distinct_tickers": len(by_sym),
        "top_n": _TOP_N,
        "candidates": candidates,
        "source_breakdown": _source_breakdown(by_sym),
        "score_weights": {
            "sources_presence": _W_SOURCES,
            "theme_confidence": _W_THEME_CONF,
            "recent_hit_rate": _W_RECENT_HITRATE,
            "fmp_top100_presence": _W_FMP_TOP100,
        },
        "disclaimer": _DISCLAIMER,
    }


def build_top100_daily(root: str | Path = ".", *, lookback_days: int = 1) -> dict[str, Any]:
    return _build_payload(Path(root).resolve(), cadence="daily", lookback_days=lookback_days)


def build_top100_weekly(root: str | Path = ".") -> dict[str, Any]:
    return _build_payload(Path(root).resolve(), cadence="weekly", lookback_days=7)


def build_top100_monthly(root: str | Path = ".") -> dict[str, Any]:
    return _build_payload(Path(root).resolve(), cadence="monthly", lookback_days=30)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_top100_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    cadence = payload.get("cadence", "?")
    a(f"# Universe — Top {payload.get('top_n', _TOP_N)} ({cadence})")
    a("")
    a(f"**Generated:** {payload.get('generated_at', '')}  ")
    a(f"**Lookback:** {payload.get('lookback_days', '?')} day(s)  ")
    a(f"**Distinct tickers across all sources:** {payload.get('total_distinct_tickers', 0)}")
    a("")
    a(f"> {payload.get('disclaimer', _DISCLAIMER)}")
    a("")

    sb = payload.get("source_breakdown") or {}
    if sb:
        a("## Source breakdown")
        a("")
        a("| Source | Distinct tickers |")
        a("|---|---|")
        for src in _KNOWN_SOURCES:
            a(f"| {src} | {sb.get(src, 0)} |")
        a("")

    cands = payload.get("candidates") or []
    if cands:
        a(f"## Top {min(len(cands), _TOP_N)} ranked")
        a("")
        a("| Rank | Symbol | Score | Sources | Sector | Theme conf | Hit-rate 1d | Last signal |")
        a("|---|---|---|---|---|---|---|---|")
        for row in cands:
            hr = row.get("recent_hit_rate_1d")
            hr_str = f"{hr * 100:.0f}%" if hr is not None else "—"
            tc = row.get("theme_confidence_max") or 0
            tc_str = f"{tc:.2f}" if tc else "—"
            last_sig = (row.get("last_signal_time") or "")[:10] or "—"
            a(
                f"| {row.get('rank')} | `{row.get('symbol')}` | "
                f"{row.get('score'):.3f} | {','.join(row.get('sources') or [])} | "
                f"{row.get('sector')} | {tc_str} | {hr_str} | {last_sig} |"
            )
        a("")
        # Reason narratives for the top 20 — keeps the md scannable
        a("## Rationale (top 20)")
        a("")
        for row in cands[:20]:
            tags = row.get("rationale_tags") or []
            tag_str = ", ".join(f"`{t}`" for t in tags[:8])
            a(f"**{row.get('rank')}. {row.get('symbol')}** — {row.get('reason','')}")
            if tags:
                a(f"  Tags: {tag_str}")
            a("")
    a("---")
    a("_Observe-only universe sanitation. `rationale_tags` are stable categorical "
      "labels designed for downstream pattern-recognition learning — see "
      "`docs/learning_loop_plan.md`._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


_CADENCE_BUILDERS = {
    "daily":   build_top100_daily,
    "weekly":  build_top100_weekly,
    "monthly": build_top100_monthly,
}


def run_universe_sanitation(
    *,
    root: str | Path = ".",
    cadence: str = "daily",
    write_files: bool = True,
) -> dict[str, Any]:
    """Top-level entry point. Never raises — returns a dict with status."""
    root_path = Path(root).resolve()
    cadence = (cadence or "daily").lower().strip()
    if cadence not in _CADENCE_BUILDERS:
        return {"status": "error", "error": f"unknown_cadence:{cadence}"}
    try:
        payload = _CADENCE_BUILDERS[cadence](root_path)
        artifacts: dict[str, str] = {}
        if write_files:
            json_path = safe_write_json(
                OutputNamespace.LATEST,
                f"top100_{cadence}.json",
                payload,
                base_dir=root_path / "outputs",
            )
            md_path = safe_write_text(
                OutputNamespace.LATEST,
                f"top100_{cadence}.md",
                render_top100_md(payload),
                base_dir=root_path / "outputs",
            )
            artifacts = {
                f"top100_{cadence}_json": str(json_path),
                f"top100_{cadence}_md": str(md_path),
            }
        return {
            "status": "ok",
            "cadence": cadence,
            "total_distinct_tickers": payload.get("total_distinct_tickers", 0),
            "top_count": len(payload.get("candidates") or []),
            "artifacts": artifacts,
        }
    except Exception as exc:
        logger.error("universe_sanitation: %s run failed: %s", cadence, exc, exc_info=True)
        return {"status": "error", "cadence": cadence, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import sys
    cadence = (sys.argv[1] if len(sys.argv) > 1 else "daily").lower()
    root_arg = Path(__file__).resolve().parents[1]
    r = run_universe_sanitation(root=root_arg, cadence=cadence)
    print(
        f"universe_sanitation: {cadence} → status={r.get('status')} "
        f"distinct={r.get('total_distinct_tickers', 0)} "
        f"top={r.get('top_count', 0)}"
    )
    sys.exit(0 if r.get("status") == "ok" else 1)
