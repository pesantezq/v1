"""Broad-market universe scanner (Phase 5, spec §7).

Extends the existing watchlist/signal scanner into a broad opportunity universe:
approved watchlist + broad-market ETFs + sector ETFs + commodity proxies + theme
baskets + private/IPO watch items (from ``config/universe_lists.yaml``). It builds
candidate rows (typed + access-routed), scores them via
:mod:`portfolio_automation.opportunity_scoring`, and writes the radar + supporting
artifacts to the **sandbox** namespace.

Hard rules (tested):
* Writes ``outputs/sandbox/*`` only — universe candidates NEVER enter
  ``decision_plan.json``.
* Private companies are typed ``private_ipo`` with an access route — never as a
  tradeable ticker, never priced as one.
* Degrades to valid empty artifacts; observe_only; trades nothing.

Dimension inputs are derived deterministically from the candidate's class plus
optional enrichment from existing signal artifacts (``theme_signals.json``,
``market_opportunities.json``, ``watchlist_signals.json``) when present.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import (
    CandidateType, AccessRoute, observe_only_envelope,
)
from portfolio_automation.opportunity_scoring import score_candidates


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_json_safe(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def load_universe_lists(root: Path) -> dict[str, Any]:
    """Load config/universe_lists.yaml; degrade to minimal defaults on failure."""
    path = root / "config" / "universe_lists.yaml"
    try:
        if path.exists() and yaml is not None:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"broad_market_etfs": [], "sector_etfs": [], "commodity_proxies": [],
            "theme_baskets": {}, "private_ipo_watch": [], "user_themes": []}


def _approved_watchlist(root: Path) -> list[str]:
    cfg = _load_json_safe(root / "config.json") or _load_json_safe(root / "config" / "config.json")
    try:
        return list((cfg or {}).get("watchlist_scanner", {}).get("watchlist", []))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Enrichment from existing signal artifacts (all optional)
# ---------------------------------------------------------------------------


def _signal_hints(root: Path) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """Return (per-symbol dimension hints, per-theme catalyst hints)."""
    L = root / "outputs" / "latest"
    sym: dict[str, dict[str, float]] = {}
    theme: dict[str, float] = {}

    mo = _load_json_safe(L / "market_opportunities.json")
    if isinstance(mo, dict):
        for row in mo.get("promoted", []) or []:
            s = str(row.get("symbol", "")).upper()
            if s:
                sc = float(row.get("score", 0) or 0)
                sc = sc / 100.0 if sc > 1 else sc
                sym.setdefault(s, {}).update(
                    catalyst_strength=min(1.0, 0.5 + sc / 2),
                    evidence_quality=min(1.0, 0.5 + sc / 2),
                    price_volume_confirmation=min(1.0, 0.4 + sc / 2))

    ws = _load_json_safe(L / "watchlist_signals.json")
    rows = ws.get("results", []) if isinstance(ws, dict) else (ws if isinstance(ws, list) else [])
    for row in rows or []:
        s = str(row.get("symbol", "")).upper()
        if not s:
            continue
        ss = row.get("signal_score")
        cf = row.get("confidence")
        h = sym.setdefault(s, {})
        try:
            if ss is not None:
                h["price_volume_confirmation"] = max(h.get("price_volume_confirmation", 0.0),
                                                     min(1.0, float(ss)))
        except (TypeError, ValueError):
            pass
        try:
            if cf is not None:
                h["data_quality"] = max(h.get("data_quality", 0.0), min(1.0, float(cf)))
        except (TypeError, ValueError):
            pass

    ts = _load_json_safe(L / "theme_signals.json")
    if isinstance(ts, dict):
        for t in ts.get("themes", []) or []:
            name = str(t.get("name", "")).strip()
            try:
                conf = float(t.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if name:
                theme[name.lower()] = conf
    return sym, theme


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------


# Class baselines: (access_investability, liquidity_quality, base_evidence).
_CLASS_BASELINE = {
    CandidateType.PUBLIC_TICKER.value: (0.8, 0.7, 0.35),
    CandidateType.ETF.value:           (0.9, 0.85, 0.4),
    CandidateType.COMMODITY_PROXY.value: (0.85, 0.7, 0.35),
    CandidateType.THEME_BASKET.value:  (0.6, 0.6, 0.3),
    CandidateType.PRIVATE_IPO.value:   (0.2, 0.2, 0.25),
}
_ETFS = set()  # populated per-run from the lists to type symbols correctly


def _mk_candidate(symbol, ctype, access_route, theme, sym_hints, theme_hint) -> dict[str, Any]:
    inv, liq, ev = _CLASS_BASELINE.get(ctype, (0.5, 0.5, 0.3))
    cand = {
        "candidate": symbol, "candidate_type": ctype, "access_route": access_route,
        "theme": theme or "",
        "catalyst_strength": round(min(1.0, 0.25 + theme_hint * 0.5), 4),
        "price_volume_confirmation": 0.3,
        "fundamental_support": 0.4,
        "market_regime_fit": round(min(1.0, 0.4 + theme_hint * 0.3), 4),
        "portfolio_diversification_value": 0.5,
        "access_investability": inv,
        "risk_adjusted_timing": 0.5,
        "boom_potential": round(min(1.0, 0.3 + theme_hint * 0.5), 4),
        "evidence_quality": ev,
        "liquidity_quality": liq,
        "data_quality": 0.5,
        # penalties default low; single-headline penalty raised when evidence is thin
        "hype_penalty": 0.0, "crowded_trade_penalty": 0.0,
        "single_headline_penalty": 0.0, "portfolio_overlap_penalty": 0.0,
    }
    h = sym_hints.get(str(symbol).upper(), {})
    for k, v in h.items():
        cand[k] = max(cand.get(k, 0.0), v)
    # thin-evidence guard: a candidate with no real signal corroboration is single-headline-ish
    if cand["evidence_quality"] < 0.4 and cand["price_volume_confirmation"] < 0.4:
        cand["single_headline_penalty"] = 0.5
    return cand


def build_universe_candidates(root: Path) -> dict[str, Any]:
    """Build typed candidate rows from the universe lists + enrichment."""
    lists = load_universe_lists(root)
    sym_hints, theme_hints = _signal_hints(root)
    etfs = set(lists.get("broad_market_etfs", []) or []) | set(lists.get("sector_etfs", []) or [])
    commodities = set(lists.get("commodity_proxies", []) or [])

    public: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(sym, ctype, route, theme=""):
        key = (str(sym).upper(), ctype)
        if key in seen:
            return
        seen.add(key)
        th = theme_hints.get(theme.lower(), 0.0) if theme else 0.0
        public.append(_mk_candidate(str(sym).upper(), ctype, route, theme, sym_hints, th))

    for s in _approved_watchlist(root):
        add(s, CandidateType.PUBLIC_TICKER.value, AccessRoute.ETF.value if s in etfs else AccessRoute.PUBLIC_SUPPLIER.value)
    for s in lists.get("broad_market_etfs", []) or []:
        add(s, CandidateType.ETF.value, AccessRoute.ETF.value)
    for s in lists.get("sector_etfs", []) or []:
        add(s, CandidateType.ETF.value, AccessRoute.ETF.value)
    for s in commodities:
        add(s, CandidateType.COMMODITY_PROXY.value, AccessRoute.PROXY.value)

    # Theme baskets → one theme_basket candidate per theme + its member tickers.
    theme_candidates: list[dict[str, Any]] = []
    for theme, members in (lists.get("theme_baskets", {}) or {}).items():
        th = theme_hints.get(str(theme).lower(), 0.0)
        theme_candidates.append(_mk_candidate(theme, CandidateType.THEME_BASKET.value,
                                              AccessRoute.ETF.value, theme, {}, th))
        for m in members or []:
            ctype = CandidateType.ETF.value if m in etfs else CandidateType.PUBLIC_TICKER.value
            add(m, ctype, AccessRoute.ETF.value if m in etfs else AccessRoute.PUBLIC_SUPPLIER.value, theme)

    # Private / IPO watch — NEVER tradeable tickers.
    private: list[dict[str, Any]] = []
    for item in lists.get("private_ipo_watch", []) or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        route = item.get("access_route", AccessRoute.WATCH_ONLY.value)
        theme = item.get("theme", "")
        th = theme_hints.get(str(theme).lower(), 0.0)
        c = _mk_candidate(name, CandidateType.PRIVATE_IPO.value, route, theme, {}, th)
        c["proxies"] = list(item.get("proxies", []) or [])
        c["note"] = item.get("note", "")
        private.append(c)

    return {"public": public, "themes": theme_candidates, "private": private}


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write_universe_artifacts(root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Build candidates, score them into the radar, write sandbox artifacts.

    Returns a summary dict. Never raises; never writes outside the sandbox.
    """
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    base = root / "outputs"
    try:
        built = build_universe_candidates(root)
        all_candidates = built["public"] + built["themes"] + built["private"]

        scored = [s.to_dict() for s in score_candidates(all_candidates)]
        scored.sort(key=lambda s: s.get("opportunity_score", 0.0), reverse=True)

        cand_payload = observe_only_envelope(now_iso, source="universe_scanner")
        cand_payload["candidates"] = all_candidates
        cand_payload["candidate_count"] = len(all_candidates)

        radar_payload = observe_only_envelope(now_iso, source="universe_scanner")
        radar_payload["opportunities"] = scored
        radar_payload["opportunity_count"] = len(scored)

        theme_payload = observe_only_envelope(now_iso, source="universe_scanner")
        theme_payload["themes"] = built["themes"]

        private_payload = observe_only_envelope(now_iso, source="universe_scanner")
        # private items keep ONLY watch metadata — never a tradeable price/quantity
        private_payload["items"] = [
            {k: v for k, v in p.items()
             if k in ("candidate", "candidate_type", "access_route", "theme",
                      "proxies", "note")} for p in built["private"]]

        safe_write_json(OutputNamespace.SANDBOX, "universe_scan_candidates.json", cand_payload, base_dir=base)
        safe_write_json(OutputNamespace.SANDBOX, "opportunity_radar.json", radar_payload, base_dir=base)
        safe_write_json(OutputNamespace.SANDBOX, "theme_candidates.json", theme_payload, base_dir=base)
        safe_write_json(OutputNamespace.SANDBOX, "private_ipo_watchlist.json", private_payload, base_dir=base)
        return {"candidate_count": len(all_candidates), "scored": len(scored),
                "private_count": len(built["private"]), "degraded": False}
    except Exception as exc:
        for fn, key in (("universe_scan_candidates.json", "candidates"),
                        ("opportunity_radar.json", "opportunities"),
                        ("theme_candidates.json", "themes"),
                        ("private_ipo_watchlist.json", "items")):
            deg = observe_only_envelope(now_iso, source="universe_scanner",
                                        degraded_mode=True, degraded_reason=str(exc))
            deg[key] = []
            try:
                safe_write_json(OutputNamespace.SANDBOX, fn, deg, base_dir=base)
            except Exception:
                pass
        return {"candidate_count": 0, "scored": 0, "private_count": 0, "degraded": True}
