"""Load real artifacts and map them onto the pure flock-metric inputs.

Every loader is defensive: a missing/malformed artifact degrades to an empty
result, never raises. Reused upstream artifacts (no new paid data):
  * crowd velocity / breadth   -> the unified crowd bus
                                  (outputs/latest/unified_crowd_intelligence.json) when
                                  present, ELSE the legacy ApeWisdom multi-source +
                                  public-knowledge velocity artifacts.
  * theme grouping             -> outputs/latest/theme_signals.json  (themes[].tickers)
  * sector grouping            -> data/fmp_cache/profile_stable_<TICKER>.json (data[0].sector)
  * price returns              -> outputs/performance/signal_outcomes.csv (outcome_return_1d)
  * prior flock states/vol     -> outputs/simulation/flock_state_history.json (this layer writes it)

Crowd-source preference (2026-06-16): ``load_crowd_metrics`` now PREFERS the
unified crowd bus (``read_unified_crowd``). When the unified artifact is present
(source == 'unified') each per-ticker entry keeps the original contract keys
(``velocity`` / ``breadth`` / ``mentions``) so all existing callers keep working,
but is additionally ENRICHED with the unified fields (retail/fmp attention,
cross-source confirmation/divergence, crowd_state, news/analyst/insider/congress
context). When the unified bus is unavailable, the legacy ApeWisdom +
public-knowledge code path is used unchanged. This is purely additive and
observe-only: it never feeds the decision engine.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from portfolio_automation.crowd_intelligence.unified_loader import read_unified_crowd


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Grouping: theme -> sector -> ticker-only fallback
# ---------------------------------------------------------------------------

def load_theme_groups(root: Path) -> list[tuple[str, str, list[str]]]:
    """Return [(group_name, 'theme', [tickers])] from theme_signals.json."""
    doc = _read_json(root / "outputs" / "latest" / "theme_signals.json") or {}
    groups: list[tuple[str, str, list[str]]] = []
    for t in doc.get("themes", []) or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        tickers = [str(s).upper() for s in (t.get("tickers") or []) if s]
        if name and len(tickers) >= 2:
            groups.append((name, "theme", sorted(set(tickers))))
    return groups


def load_ticker_sector(root: Path, ticker: str) -> str | None:
    """Resolve sector from the FMP profile cache; None on miss."""
    doc = _read_json(root / "data" / "fmp_cache" / f"profile_stable_{ticker.upper()}.json")
    try:
        data = (doc or {}).get("data") or []
        sec = data[0].get("sector") if data and isinstance(data[0], dict) else None
        return str(sec) if sec else None
    except Exception:
        return None


def load_sector_groups(root: Path, universe: list[str]) -> list[tuple[str, str, list[str]]]:
    """Group the universe by FMP sector (only sectors with >=2 resolvable tickers)."""
    by_sector: dict[str, list[str]] = {}
    for tk in universe:
        sec = load_ticker_sector(root, tk)
        if sec:
            by_sector.setdefault(sec, []).append(tk.upper())
    return [(f"Sector: {sec}", "sector", sorted(set(tks)))
            for sec, tks in by_sector.items() if len(set(tks)) >= 2]


def load_universe(root: Path) -> list[str]:
    """Best-effort union of watchlist + advisory + crowd tickers (uppercase)."""
    uni: set[str] = set()
    cfg = _read_json(root / "config.json") or {}
    pf = cfg.get("portfolio", {}) if isinstance(cfg, dict) else {}
    for t in (pf.get("watchlist") or []):
        if t:
            uni.add(str(t).upper())
    for h in (pf.get("holdings") or []):
        sym = h.get("symbol") if isinstance(h, dict) else None
        if sym:
            uni.add(str(sym).upper())
    return sorted(uni)


# ---------------------------------------------------------------------------
# Crowd metrics (velocity / breadth / mentions) — reuse existing artifacts
# ---------------------------------------------------------------------------

# Maps the unified retail_attention_score (0..1) onto a velocity-comparable
# magnitude. The unified bus normalizes ApeWisdom mention velocity onto 0..1 by
# dividing the excess-over-flat by RETAIL_ATTENTION_FULL_SCALE (=5.0); we invert
# that here so the flock metrics see a velocity on the same order of magnitude as
# the legacy mention-velocity path (a full-attention ticker -> ~5.0).
_RETAIL_ATTENTION_VELOCITY_SCALE = 5.0


def load_crowd_metrics(root: Path) -> dict[str, dict[str, float]]:
    """Per-ticker {velocity, breadth, mentions} crowd metrics.

    PREFERS the unified crowd bus (``read_unified_crowd``). When the unified
    artifact is present (source == 'unified'), entries keep the original contract
    keys (``velocity`` / ``breadth`` / ``mentions``) and are enriched with the
    unified cross-source fields. Otherwise falls back to the legacy multi-source
    velocity artifact (ApeWisdom etc.), merging public-knowledge-velocity features.
    """
    unified = _load_unified_crowd_metrics(root)
    if unified is not None:
        return unified

    disc = root / "outputs" / "sandbox" / "discovery"
    out: dict[str, dict[str, float]] = {}

    ms = _read_json(disc / "crowd_multi_source_velocity.json") or {}
    for rec in ms.get("records", []) or []:
        if not isinstance(rec, dict):
            continue
        tk = str(rec.get("ticker") or "").upper()
        if not tk:
            continue
        out[tk] = {
            "velocity": float(rec.get("mention_velocity") or 0.0),
            "breadth": float(rec.get("source_breadth") or 0.0),
            "mentions": float(rec.get("mention_velocity") or 0.0),
        }

    pk = _read_json(disc / "public_knowledge_velocity.json") or {}
    for rec in pk.get("records", []) or []:
        if not isinstance(rec, dict):
            continue
        tk = str(rec.get("ticker") or "").upper()
        if not tk:
            continue
        cur = out.setdefault(tk, {"velocity": 0.0, "breadth": 0.0, "mentions": 0.0})
        # Reddit features are richer; prefer z-score velocity + author breadth when present.
        z = rec.get("mention_velocity_zscore")
        if isinstance(z, (int, float)):
            cur["velocity"] = float(z)
        authors = rec.get("unique_author_count")
        if isinstance(authors, (int, float)) and authors:
            cur["breadth"] = max(cur["breadth"], float(authors))
        mc = rec.get("mention_count")
        if isinstance(mc, (int, float)) and mc:
            cur["mentions"] = float(mc)
    return out


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _load_unified_crowd_metrics(root: Path) -> dict[str, dict[str, float]] | None:
    """Build per-ticker metrics from the unified crowd bus, or None when the
    unified artifact is unavailable (so callers fall back to the legacy path).

    Keeps the legacy contract keys (velocity/breadth/mentions) and additively
    enriches each entry with the unified cross-source fields. Never raises.
    """
    try:
        unified = read_unified_crowd(root)
    except Exception:
        return None
    if not isinstance(unified, dict) or unified.get("source") != "unified":
        return None

    by_ticker = unified.get("by_ticker") or {}
    if not isinstance(by_ticker, dict) or not by_ticker:
        return None

    out: dict[str, dict[str, float]] = {}
    for tk, row in by_ticker.items():
        if not isinstance(row, dict):
            continue
        ticker = str(tk or "").upper()
        if not ticker:
            continue

        retail = _as_float(row.get("retail_attention_score"))
        fmp = _as_float(row.get("fmp_attention_score"))
        breadth_total = _as_float(row.get("source_breadth_total")) or 0.0

        # velocity: prefer retail attention (scaled to legacy magnitude); when
        # retail is absent (FMP-only ticker) use the fmp attention so the group
        # no longer goes dark just because ApeWisdom had no read.
        if retail is not None:
            velocity = retail * _RETAIL_ATTENTION_VELOCITY_SCALE
        elif fmp is not None:
            velocity = fmp * _RETAIL_ATTENTION_VELOCITY_SCALE
        else:
            velocity = 0.0

        entry: dict[str, float] = {
            "velocity": velocity,
            "breadth": breadth_total,
            "mentions": velocity,
            # --- enriched unified fields (additive; callers may ignore) ---
            "retail_attention": retail if retail is not None else 0.0,
            "fmp_attention": fmp if fmp is not None else 0.0,
            "confirmation": _as_float(row.get("cross_source_confirmation_score")) or 0.0,
            "divergence": _as_float(row.get("cross_source_divergence_score")) or 0.0,
            "source_breadth_total": breadth_total,
            "source_breadth_social": _as_float(row.get("source_breadth_social")) or 0.0,
            "source_breadth_fmp": _as_float(row.get("source_breadth_fmp")) or 0.0,
            "news": _as_float(row.get("news_score")) or 0.0,
            "analyst": _as_float(row.get("analyst_score")) or 0.0,
            "insider": _as_float(row.get("insider_score")) or 0.0,
            "congress": _as_float(row.get("congress_score")) or 0.0,
            "crowd_confidence": _as_float(row.get("crowd_confidence")) or 0.0,
        }
        # crowd_state is a label (string), kept alongside the numeric metrics.
        entry["crowd_state"] = row.get("crowd_state") or ""  # type: ignore[assignment]
        out[ticker] = entry
    return out or None


# ---------------------------------------------------------------------------
# Price returns -> per-ticker date-indexed return series
# ---------------------------------------------------------------------------

def load_returns(root: Path, return_col: str = "outcome_return_1d") -> dict[str, dict[str, float]]:
    """Per-ticker {date: return} from signal_outcomes.csv (non-null rows only)."""
    path = root / "outputs" / "performance" / "signal_outcomes.csv"
    out: dict[str, dict[str, float]] = {}
    if not path.exists():
        return out
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                tk = str(row.get("ticker") or "").upper()
                raw = row.get(return_col)
                ts = (row.get("signal_time") or "")[:10]
                if not tk or not ts or raw in (None, ""):
                    continue
                try:
                    out.setdefault(tk, {})[ts] = float(raw)
                except (TypeError, ValueError):
                    continue
    except Exception:
        return {}
    return out


def aligned_group_returns(returns: dict[str, dict[str, float]],
                          tickers: list[str]) -> dict[str, list[float]]:
    """Return {ticker: [returns over the dates common to >=2 group tickers]}.

    Aligns by the intersection of dates so pairwise correlation is well-defined.
    """
    per = {tk: returns.get(tk.upper(), {}) for tk in tickers if returns.get(tk.upper())}
    if len(per) < 2:
        return {tk: list(d.values()) for tk, d in per.items()}
    common = set.intersection(*[set(d) for d in per.values()]) if per else set()
    common_sorted = sorted(common)
    if len(common_sorted) >= 3:
        return {tk: [per[tk][dt] for dt in common_sorted] for tk in per}
    # Not enough common dates — fall back to each ticker's own ordered series.
    return {tk: [d[k] for k in sorted(d)] for tk, d in per.items()}


def latest_returns(returns: dict[str, dict[str, float]], tickers: list[str]) -> dict[str, float]:
    """Per-ticker most-recent return (by date)."""
    out: dict[str, float] = {}
    for tk in tickers:
        d = returns.get(tk.upper())
        if d:
            out[tk.upper()] = d[max(d)]
    return out


# ---------------------------------------------------------------------------
# Prior-state ledger (this layer owns it; lets us detect dispersion/broken)
# ---------------------------------------------------------------------------

def load_prior_states(root: Path) -> dict[str, dict[str, Any]]:
    """{group: {state, avg_correlation, volatility}} from the prior run, or {}."""
    doc = _read_json(root / "outputs" / "simulation" / "flock_state_history.json") or {}
    groups = doc.get("groups") if isinstance(doc, dict) else None
    return groups if isinstance(groups, dict) else {}
