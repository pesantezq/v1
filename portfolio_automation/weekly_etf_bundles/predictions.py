"""
Frozen prediction ledger for the weekly ETF bundle subsystem.

Every weekly run freezes each ranking as an immutable, point-in-time prediction
record. Records are:

  * IMMUTABLE — once a market-data date is frozen it is never overwritten or
    recomputed with later data.
  * IDEMPOTENT — keyed on market_data_date, so rerunning the same Friday close
    produces the same records and does not duplicate them.
  * TRACEABLE — each record stores strategy_id, model_version, and the config
    content hash that produced it.

Predictions live under OutputNamespace.WEEKLY_ETF_BUNDLES:
  predictions/<market_data_date>.json                      (champion lane)
  predictions/challengers/<variant>__<market_data_date>.json (challenger lanes)

Champion predictions are the only ones surfaced in the operator email;
challenger predictions stay in simulation artifacts (see strat_lab_adapter).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation import weekly_etf_bundles as _pkg
from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
)

logger = logging.getLogger("stockbot.weekly_etf_bundles.predictions")

_CHAMPION_LANE = "champion"


def make_prediction_id(market_data_date: str, bundle_id: str, symbol: str) -> str:
    """Deterministic, market-data-date-keyed id (idempotent across reruns)."""
    return f"{market_data_date}:{bundle_id}:{symbol.upper()}"


def _predictions_rel(market_data_date: str, *, lane: str, variant: str | None) -> str:
    if lane == _CHAMPION_LANE:
        return f"predictions/{market_data_date}.json"
    v = variant or "challenger"
    return f"predictions/challengers/{v}__{market_data_date}.json"


def build_predictions(
    analysis_payload: dict[str, Any],
    *,
    lane: str = _CHAMPION_LANE,
    strategy_variant: str | None = None,
) -> list[dict[str, Any]]:
    """Pure: derive immutable prediction records from an analysis payload.
    Returns [] if the payload has no market_data_date / rankings."""
    mdd = analysis_payload.get("market_data_date")
    if not mdd or analysis_payload.get("status") != "ok":
        return []

    # symbol -> price_at_prediction, from the per-bundle member metrics.
    price_by_symbol: dict[str, float | None] = {}
    for b in analysis_payload.get("bundles", []):
        for m in b.get("members", []):
            met = m.get("metrics", {})
            if met.get("available"):
                price_by_symbol[m["symbol"]] = met.get("price")

    ctx = analysis_payload.get("market_context", {}) or {}
    market_regime = ctx.get("market_regime", "unknown")
    volatility_regime = ctx.get("volatility_regime", "unknown")

    records: list[dict[str, Any]] = []
    for r in analysis_payload.get("ranking_global", []):
        sym = r["symbol"]
        records.append({
            "prediction_id": make_prediction_id(mdd, r["bundle_id"], sym),
            "generated_at": analysis_payload.get("generated_at"),
            "market_data_date": mdd,
            "lane": lane,
            "strategy_variant": strategy_variant or analysis_payload.get("strategy_id"),
            "bundle_id": r["bundle_id"],
            "symbol": sym,
            "benchmark": r.get("benchmark"),
            "watch_score": r["watch_score"],
            "label": r["label"],
            "rank_in_bundle": r.get("rank_in_bundle"),
            "rank_global": r.get("rank_global"),
            "expected_direction": r.get("expected_direction"),
            "market_regime": market_regime,
            "volatility_regime": volatility_regime,
            "price_at_prediction": price_by_symbol.get(sym),
            "score_components": r.get("components", {}),
            "strategy_id": analysis_payload.get("strategy_id", _pkg.STRATEGY_ID),
            "model_version": analysis_payload.get("model_version", _pkg.MODEL_VERSION),
            "config_version": analysis_payload.get("config_version"),
            "horizons_weeks": [1, 4, 12, 26],
            "observe_only": True,
        })
    return records


def _content_hash(records: list[dict[str, Any]]) -> str:
    # Hash the semantic content, excluding generated_at (which is wall-clock and
    # would otherwise make an identical-market-data rerun look like a conflict).
    stripped = [{k: v for k, v in rec.items() if k != "generated_at"} for rec in records]
    blob = json.dumps(stripped, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def freeze_predictions(
    analysis_payload: dict[str, Any],
    *,
    root: str | Path = ".",
    lane: str = _CHAMPION_LANE,
    strategy_variant: str | None = None,
    write_files: bool = True,
) -> dict[str, Any]:
    """Freeze predictions for the payload's market_data_date. Immutable +
    idempotent:
      * new date          → write file, status="frozen"
      * same date, same   → skip write, status="idempotent_skip"
      * same date, differ → REFUSE to overwrite, status="conflict"
    """
    records = build_predictions(analysis_payload, lane=lane, strategy_variant=strategy_variant)
    mdd = analysis_payload.get("market_data_date")
    if not records or not mdd:
        return {"status": "no_predictions", "count": 0, "market_data_date": mdd}

    new_hash = _content_hash(records)
    rel = _predictions_rel(mdd, lane=lane, variant=strategy_variant)
    doc = {
        "market_data_date": mdd,
        "generated_at": analysis_payload.get("generated_at"),
        "lane": lane,
        "strategy_variant": strategy_variant or analysis_payload.get("strategy_id"),
        "content_hash": new_hash,
        "observe_only": True,
        "schema_version": _pkg.SCHEMA_VERSION,
        "source": _pkg.SOURCE_LABEL,
        "count": len(records),
        "predictions": records,
    }

    if not write_files:
        return {"status": "dry_run", "count": len(records),
                "market_data_date": mdd, "content_hash": new_hash, "document": doc}

    root_path = Path(root).resolve()
    existing_path = get_output_path(
        OutputNamespace.WEEKLY_ETF_BUNDLES, rel, base_dir=root_path / "outputs"
    )
    if existing_path.exists():
        try:
            prev = json.loads(existing_path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        prev_hash = prev.get("content_hash")
        if prev_hash == new_hash:
            return {"status": "idempotent_skip", "count": len(records),
                    "market_data_date": mdd, "content_hash": new_hash,
                    "path": str(existing_path)}
        # Immutability guard — never overwrite a frozen date with different data.
        logger.warning(
            "weekly_etf predictions conflict for %s: existing hash %s != new %s; "
            "keeping the original frozen record", mdd, prev_hash, new_hash,
        )
        return {"status": "conflict", "count": len(records),
                "market_data_date": mdd, "existing_hash": prev_hash,
                "new_hash": new_hash, "path": str(existing_path)}

    path = safe_write_json(
        OutputNamespace.WEEKLY_ETF_BUNDLES, rel, doc, base_dir=root_path / "outputs"
    )
    return {"status": "frozen", "count": len(records), "market_data_date": mdd,
            "content_hash": new_hash, "path": str(path)}


def load_predictions_for_date(
    root: str | Path, market_data_date: str, *,
    lane: str = _CHAMPION_LANE, variant: str | None = None,
) -> list[dict[str, Any]]:
    rel = _predictions_rel(market_data_date, lane=lane, variant=variant)
    path = get_output_path(
        OutputNamespace.WEEKLY_ETF_BUNDLES, rel, base_dir=Path(root).resolve() / "outputs"
    )
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return doc.get("predictions", []) if isinstance(doc, dict) else []


def list_prediction_dates(root: str | Path, *, lane: str = _CHAMPION_LANE) -> list[str]:
    """Sorted market-data dates that have frozen champion predictions."""
    base = get_output_path(
        OutputNamespace.WEEKLY_ETF_BUNDLES, "predictions", base_dir=Path(root).resolve() / "outputs"
    )
    if not base.exists():
        return []
    out: list[str] = []
    for p in base.glob("*.json"):
        stem = p.stem
        if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
            out.append(stem)
    return sorted(out)
