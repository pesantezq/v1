"""Load real artifacts and map them onto the pure flock-metric inputs.

Every loader is defensive: a missing/malformed artifact degrades to an empty
result, never raises. Reused upstream artifacts (no new paid data):
  * crowd velocity / breadth   -> outputs/sandbox/discovery/crowd_multi_source_velocity.json
                                  (+ public_knowledge_velocity.json when present)
  * theme grouping             -> outputs/latest/theme_signals.json  (themes[].tickers)
  * sector grouping            -> data/fmp_cache/profile_stable_<TICKER>.json (data[0].sector)
  * price returns              -> outputs/performance/signal_outcomes.csv (outcome_return_1d)
  * prior flock states/vol     -> outputs/simulation/flock_state_history.json (this layer writes it)
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


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

def load_crowd_metrics(root: Path) -> dict[str, dict[str, float]]:
    """Per-ticker {velocity, breadth, mentions} from existing crowd artifacts.

    Prefers the active multi-source velocity artifact (ApeWisdom etc.); merges
    the public-knowledge-velocity per-ticker features when present.
    """
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
