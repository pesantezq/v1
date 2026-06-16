"""Broker-aware holdings resolver + portfolio side-panel (Phase 10, spec §6).

Provides the user's *actual* holdings as an OPTIONAL input: a fresh Schwab
read-only snapshot is preferred when available + configured + the feature flag is
on; otherwise it falls back to ``config.json`` holdings. Stale/missing broker data
lowers a confidence modifier.

Decision §23.10: ``broker_aware_portfolio.json`` is a **read-only side-panel** —
it NEVER feeds ``decision_plan`` inputs (that would be a separate, default-off,
owner-approved wiring step). This module trades nothing, writes no broker, and
mutates no holdings.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import observe_only_envelope

# Broker snapshot is considered stale beyond this age (seconds).
_STALE_AFTER_S = 24 * 3600


def _load_json_safe(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def _config(root: Path) -> dict[str, Any]:
    return _load_json_safe(root / "config.json") or {}


def _broker_aware_enabled(root: Path) -> bool:
    cfg = _config(root)
    try:
        return bool(cfg.get("portfolio", {}).get("broker_aware", {}).get("enabled", False))
    except Exception:
        return False


def _parse_age_s(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds())
    except Exception:
        return None


def resolve_holdings(root: Path, prefer_broker: bool | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """Resolve holdings + cash, preferring a fresh broker snapshot when allowed.

    Returns ``{holdings, cash, holdings_source, broker_freshness_age_s,
    confidence_modifier, reason}``. Never raises.
    """
    now = now or datetime.now(timezone.utc)
    if prefer_broker is None:
        prefer_broker = _broker_aware_enabled(root)
    L = root / "outputs" / "latest"
    cfg = _config(root)
    cfg_holdings = (cfg.get("portfolio", {}) or {}).get("holdings", []) or []
    cfg_cash = (cfg.get("portfolio", {}) or {}).get("cash_available", 0.0)

    def _config_result(reason: str, conf: float) -> dict[str, Any]:
        return {"holdings": cfg_holdings, "cash": cfg_cash, "holdings_source": "config",
                "broker_freshness_age_s": None, "confidence_modifier": conf, "reason": reason}

    if not prefer_broker:
        return _config_result("broker_aware_disabled", 1.0)

    positions = _load_json_safe(L / "schwab_positions.json")
    snapshot = _load_json_safe(L / "schwab_portfolio_snapshot.json")
    if not isinstance(positions, dict) or not isinstance(snapshot, dict):
        return _config_result("broker_data_missing", 0.85)

    age = _parse_age_s(snapshot.get("snapshot_timestamp") or snapshot.get("generated_at"), now)
    if age is not None and age > _STALE_AFTER_S:
        return _config_result("broker_data_stale", 0.8)

    pos = positions.get("positions", []) or []
    if not pos:
        return _config_result("broker_no_positions", 0.85)

    def _cost_basis(p):
        q, ac = p.get("quantity"), p.get("average_cost")
        try:
            return round(float(q) * float(ac), 2) if q is not None and ac is not None else None
        except (TypeError, ValueError):
            return None
    holdings = [{"symbol": str(p.get("symbol", "")).upper(),
                 "quantity": p.get("quantity"),
                 "market_value": p.get("market_value"),
                 "average_cost": p.get("average_cost"),
                 "cost_basis": _cost_basis(p)} for p in pos if p.get("symbol")]
    cash = (snapshot.get("totals", {}) or {}).get("cash", cfg_cash)
    return {"holdings": holdings, "cash": cash, "holdings_source": "broker",
            "broker_freshness_age_s": age, "confidence_modifier": 1.0,
            "reason": "fresh_broker_snapshot"}


def _config_leveraged(root: Path) -> set[str]:
    cfg = _config(root)
    out = set()
    for h in (cfg.get("portfolio", {}) or {}).get("holdings", []) or []:
        if h.get("is_leveraged"):
            out.add(str(h.get("symbol", "")).upper())
    return out


def build_broker_aware_portfolio(root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Build the read-only broker-aware portfolio side-panel. Never feeds decision_plan."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    res = resolve_holdings(root, now=now)
    payload = observe_only_envelope(now_iso, source="holdings_resolver",
                                    side_panel_only=True,
                                    feeds_decision_plan=False)
    payload["holdings_source"] = res["holdings_source"]
    payload["freshness"] = {"broker_freshness_age_s": res["broker_freshness_age_s"],
                            "reason": res["reason"]}
    payload["confidence_modifier"] = res["confidence_modifier"]

    holdings = res["holdings"]
    leveraged = _config_leveraged(root)

    if res["holdings_source"] == "broker":
        mvs = {h["symbol"]: float(h.get("market_value") or 0) for h in holdings}
        total = sum(mvs.values()) + float(res["cash"] or 0)
        weights = {s: round(mv / total, 4) for s, mv in mvs.items()} if total > 0 else {}
        payload["allocation"] = weights
        payload["concentration"] = {"max_weight": round(max(weights.values()), 4) if weights else 0.0,
                                     "flag": (max(weights.values()) > 0.40) if weights else False}
        lev_mv = sum(mv for s, mv in mvs.items() if s in leveraged)
        payload["leverage"] = {"leveraged_exposure": round(lev_mv / total, 4) if total > 0 else 0.0}
        payload["cash_drag"] = round(float(res["cash"] or 0) / total, 4) if total > 0 else 0.0
        # config-vs-broker drift (symbol set + cash)
        cfg_syms = {str(h.get("symbol", "")).upper()
                    for h in (_config(root).get("portfolio", {}) or {}).get("holdings", []) or []}
        broker_syms = set(mvs)
        payload["config_vs_broker_drift"] = {
            "only_in_broker": sorted(broker_syms - cfg_syms),
            "only_in_config": sorted(cfg_syms - broker_syms),
        }
        payload["degraded_mode"] = False
    else:
        # config fallback: no live prices → market-value metrics unavailable (degrade honestly)
        payload["allocation"] = {}
        payload["concentration"] = {"available": False, "reason": "no_market_value_without_broker"}
        payload["leverage"] = {"available": False}
        payload["cash_drag"] = None
        payload["config_vs_broker_drift"] = {"available": False}
        payload["degraded_mode"] = True
    return payload


def write_broker_aware_portfolio(root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Write the side-panel artifact (PORTFOLIO namespace). Non-fatal."""
    now = now or datetime.now(timezone.utc)
    base = root / "outputs"
    try:
        payload = build_broker_aware_portfolio(root, now)
    except Exception as exc:
        payload = observe_only_envelope(now.isoformat(), source="holdings_resolver",
                                        degraded_mode=True, degraded_reason=str(exc),
                                        side_panel_only=True, feeds_decision_plan=False)
        payload["holdings_source"] = "config"
        payload["freshness"] = {"reason": "error"}
    safe_write_json(OutputNamespace.PORTFOLIO, "broker_aware_portfolio.json", payload, base_dir=base)
    return {"holdings_source": payload.get("holdings_source"),
            "degraded": bool(payload.get("degraded_mode"))}


_OVERLAY_DEFAULTS = {"target_weight": 0.0, "asset_class": "us_equity",
                     "is_leveraged": False, "leverage_factor": 1}


def broker_overlaid_portfolio(portfolio_block: dict, root: "Path | str",
                              now: "datetime | None" = None) -> dict:
    """Return a COPY of the config portfolio block with holdings shares + cash overlaid
    from the live broker snapshot (Schwab-preferred), preserving config per-symbol
    strategy metadata. Config fallback on stale/missing/disabled. Runtime-only; never
    writes config.json; never raises. Adds holdings_source + confidence_modifier."""
    block = dict(portfolio_block or {})
    cfg_holdings = block.get("holdings") if isinstance(block.get("holdings"), list) else []
    try:
        res = resolve_holdings(Path(root), now=now)
    except Exception:
        res = {"holdings_source": "config", "confidence_modifier": 1.0}
    if res.get("holdings_source") != "broker":
        block["holdings_source"] = "config"
        block["confidence_modifier"] = res.get("confidence_modifier", 1.0)
        block["reason"] = res.get("reason")
        return block
    by_sym = {str(h.get("symbol", "")).upper(): dict(h) for h in cfg_holdings if isinstance(h, dict)}
    merged: list = []
    broker_syms: set = set()
    for bh in res.get("holdings", []) or []:
        sym = str(bh.get("symbol", "")).upper()
        if not sym:
            continue
        broker_syms.add(sym)
        base = dict(by_sym.get(sym, {"symbol": sym, **_OVERLAY_DEFAULTS}))
        base["symbol"] = sym
        base["shares"] = bh.get("quantity")
        for k, v in _OVERLAY_DEFAULTS.items():
            base.setdefault(k, v)
        merged.append(base)
    for sym, h in by_sym.items():
        if sym not in broker_syms:
            merged.append(h)
    block["holdings"] = merged
    block["cash_available"] = res.get("cash", block.get("cash_available"))
    block["holdings_source"] = "broker"
    block["confidence_modifier"] = res.get("confidence_modifier", 1.0)
    block["reason"] = res.get("reason")
    return block


def _write_holdings_source_telemetry(root: "Path | str", *, source: str,
                                     confidence_modifier: float,
                                     reason: str | None) -> None:
    """Record which holdings source drove the current decision run (observe-only).

    Written on BOTH the broker and config-fallback branches so the artifact
    always reflects THIS run — never a stale value carried from a prior
    broker-success run. Never raises (telemetry must not break a run).
    """
    try:
        from portfolio_automation.data_governance import OutputNamespace, safe_write_json
        safe_write_json(OutputNamespace.LATEST, "decision_holdings_source.json",
                        {"observe_only": True, "holdings_source": source,
                         "confidence_modifier": confidence_modifier, "reason": reason},
                        base_dir=str(Path(root) / "outputs"))
    except Exception:
        pass


def apply_broker_overlay_to_config(config, root: "Path | str", now=None):
    """Overlay broker holdings/cash onto a utils.Config object (runtime, in place).
    Rebuilds config.holdings as the same Holding dataclass, preserving config metadata.
    Schwab-preferred; config fallback on stale/missing/disabled. Never raises; returns config."""
    try:
        block = {"holdings": [{"symbol": getattr(h, "symbol", None), "shares": getattr(h, "shares", None),
                               "target_weight": getattr(h, "target_weight", 0.0),
                               "asset_class": getattr(h, "asset_class", "us_equity"),
                               "is_leveraged": getattr(h, "is_leveraged", False),
                               "leverage_factor": getattr(h, "leverage_factor", 1.0)}
                              for h in getattr(config, "holdings", []) or []],
                 "cash_available": getattr(config, "cash_available", 0.0)}
        overlaid = broker_overlaid_portfolio(block, root, now=now)
        if overlaid.get("holdings_source") != "broker" or not getattr(config, "holdings", None):
            # Decisions ran on CONFIG holdings (broker disabled/stale/missing, or
            # no config holdings to overlay). Record honestly so the daily check's
            # decision_on_config_while_broker_ok signal reflects THIS run instead
            # of a stale broker value left by a prior successful overlay.
            _write_holdings_source_telemetry(
                root, source="config",
                confidence_modifier=overlaid.get("confidence_modifier", 1.0),
                reason=overlaid.get("reason"))
            return config
        HoldingCls = type(config.holdings[0])
        new_holdings = []
        for h in overlaid["holdings"]:
            new_holdings.append(HoldingCls(
                symbol=h["symbol"], shares=float(h.get("shares") or 0),
                target_weight=float(h.get("target_weight", 0.0) or 0.0),
                asset_class=h.get("asset_class", "us_equity"),
                is_leveraged=bool(h.get("is_leveraged", False)),
                leverage_factor=float(h.get("leverage_factor", 1.0) or 1.0)))
        config.holdings = new_holdings
        config.cash_available = overlaid.get("cash_available", config.cash_available)
        # observe-only telemetry: which source drove this run (non-fatal)
        _write_holdings_source_telemetry(
            root, source=overlaid.get("holdings_source"),
            confidence_modifier=overlaid.get("confidence_modifier", 1.0),
            reason=overlaid.get("reason"))
    except Exception:
        return config
    return config
