"""End-to-end governed pipeline: request → provider → raw → canonical → features.

The durable path derives provenance rather than accepting it. `build_features`
takes the CanonicalDataset object and reads identity off it, so a caller cannot
pair bars from dataset A with the identity of dataset B — an argument-level API
that accepts `(bars, dataset_id, fingerprint)` separately permits exactly that
mis-binding, and it is the kind of error no test would notice later.

Research-only. HISTORICAL namespace. No strategies, fills, costs or risk.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Sequence

from portfolio_automation.intraday_lab import features as F
from portfolio_automation.intraday_lab import storage as ST
from portfolio_automation.intraday_lab.data import normalize_fmp_rows, fetch_status
from portfolio_automation.intraday_lab.dataset import (
    DatasetRequest, CanonicalDataset, build_canonical_dataset, dataset_manifest,
    rejection_report, calendar_fingerprint,
)

SCHEMA_VERSION = "1"


def plan_request(request: DatasetRequest) -> dict:
    """Dry-run view: what would be asked for, and of whom. Writes nothing."""
    items = request.resolved_items()
    return {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.pipeline",
        "observe_only": True,
        "dry_run": True,
        "request": request.to_dict(),
        "calendar_fingerprint": calendar_fingerprint(),
        "resolved_items": [{"symbol": s, "market_date": d.isoformat(),
                            "calendar_status": st} for s, d, st in items],
        "provider_calls_planned": sorted({s for s, _ in request.certified_sessions()}),
        "writes": [],
    }


def acquire(request: DatasetRequest, fetcher: Callable[[str, str, str], tuple[Any, int]],
            *, root: str = ".", now: datetime | None = None) -> dict:
    """Fetch through a supplied sanctioned fetcher and persist raw evidence.

    `fetcher(symbol, start, end) -> (rows, http_status)` is injected so the
    pipeline is testable without network and so the caller supplies the
    repo-governed client rather than this module opening its own connection.

    A provider error or empty response is recorded, never raised away: the
    requested session must survive into the reconciliation trail.
    """
    now = now or datetime.now(timezone.utc)
    symbols = sorted({s for s, _ in request.certified_sessions()})
    acquisitions: list[dict] = []
    bars_by_date: dict[tuple[str, date], list] = {}

    for symbol in symbols:
        rows, http = None, None
        error = None
        try:
            rows, http = fetcher(symbol, request.start.isoformat(),
                                 request.end.isoformat())
        except Exception as exc:                      # provider failure is evidence
            error = f"{type(exc).__name__}: {str(exc)[:160]}"

        status = fetch_status(rows, http_status=http) if error is None else "PROVIDER_ERROR"
        payload_hash = (ST.raw_payload_hash(rows, symbol=symbol,
                                            timeframe=request.timeframe)
                        if isinstance(rows, list) else None)
        record = {
            "schema_version": SCHEMA_VERSION,
            "request_fingerprint": request.fingerprint(),
            "provider": "fmp",
            "endpoint": f"/stable/historical-chart/{request.timeframe}",
            "symbol": symbol, "timeframe": request.timeframe,
            "requested_start": request.start.isoformat(),
            "requested_end": request.end.isoformat(),
            "retrieved_at": now.isoformat(),
            "provider_status": status, "http_status": http,
            "row_count": len(rows) if isinstance(rows, list) else 0,
            "raw_payload_hash": payload_hash,
            "error_code": None if error is None else "PROVIDER_EXCEPTION",
            "error_message_safe": error,
            "normalization_status": "PENDING",
        }

        if isinstance(rows, list) and rows:
            try:
                bars = normalize_fmp_rows(rows, symbol=symbol,
                                          timeframe=request.timeframe,
                                          retrieved_at=now)
                record["normalization_status"] = "OK"
                for b in bars:
                    from zoneinfo import ZoneInfo
                    d = b.bar_start_at.astimezone(ZoneInfo("America/New_York")).date()
                    bars_by_date.setdefault((symbol, d), []).append(b)
            except Exception as exc:
                record["normalization_status"] = f"FAILED: {type(exc).__name__}"
            # CONTENT object: observations only. No retrieved_at, no request id
            # -- those would make an identical refetch look like corruption.
            ST.write_snapshot(ST.RAW, payload_hash, {
                "payload.json": rows,
                "content_manifest.json": {
                    "schema_version": SCHEMA_VERSION,
                    "provider": "fmp",
                    "endpoint": record["endpoint"],
                    "symbol": symbol,
                    "timeframe": request.timeframe,
                    "row_count": len(rows),
                    "raw_content_fingerprint": payload_hash,
                },
            }, root=root)
            record["raw_content_path"] = f"outputs/backtest/intraday/raw/content/{payload_hash}"

        # EVENT object: one per provider call, keyed by the call itself. Two
        # fetches of identical data at different times => one content object,
        # two events. That is the desired outcome, not a collision.
        event_id = ST.content_hash({
            "request_fingerprint": request.fingerprint(), "symbol": symbol,
            "requested_start": request.start.isoformat(),
            "requested_end": request.end.isoformat(),
            "retrieved_at": record["retrieved_at"],
            "provider_status": status, "raw_content_fingerprint": payload_hash,
        })
        record["acquisition_event_id"] = event_id
        ST.write_snapshot(ST.RAW_EVENTS, event_id,
                          {"acquisition_event.json": record}, root=root)
        acquisitions.append(record)

    failures = {a["symbol"] for a in acquisitions
                if a["provider_status"] in ("PROVIDER_ERROR", "DATA_UNAVAILABLE")}
    return {"acquisitions": acquisitions, "bars_by_date": bars_by_date,
            "provider_failures": failures}


def build_features(ds: CanonicalDataset, *, lookback: int = 3) -> list[F.FeatureValue]:
    """Derive features with provenance taken FROM the dataset, not from args."""
    out: list[F.FeatureValue] = []
    did, content_fp = ds.dataset_id(), ds.fingerprint()
    manifest_fp = ds.manifest_fingerprint()
    for (_symbol, _tf), series in F.group_series(ds.bars).items():
        for i in range(len(series)):
            v = F.compute_return_nbar(series, i, lookback, dataset_id=did,
                                      fingerprint=content_fp,
                                      manifest_fingerprint=manifest_fp)
            if v:
                out.append(v)
    return out


def build_historical_research_dataset(
    request: DatasetRequest, fetcher: Callable[[str, str, str], tuple[Any, int]],
    *, root: str = ".", dry_run: bool = False, lookback: int = 3,
) -> dict:
    """The full governed chain. Returns identities and artifact paths."""
    if dry_run:
        return plan_request(request)

    acq = acquire(request, fetcher, root=root)
    ds = build_canonical_dataset(acq["bars_by_date"], request=request,
                                 provider_failures=acq["provider_failures"])

    manifest = dataset_manifest(ds)
    rejects = rejection_report(ds)
    content_fp, manifest_fp = ds.fingerprint(), ds.manifest_fingerprint()

    # CONTENT: canonical bars only, deduplicated across research requests.
    ST.write_snapshot(ST.DATASETS, content_fp, {
        "canonical_bars.json": ST.bars_to_rows(ds.bars),
        "content_manifest.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_fingerprint": content_fp,
            "timeframe": ds.timeframe,
            "adjustment_state": ds.adjustment_state,
            "bar_count": len(ds.bars),
        },
    }, root=root)

    # MANIFEST: the research interpretation. Two requests producing identical
    # bars share the content object above but keep separate manifests here.
    ST.write_snapshot(ST.DATASET_MANIFESTS, manifest_fp, {
        "dataset_manifest.json": manifest,
        "reconciliation.json": [r.detail() for r in ds.reconciliations],
        "request_manifest.json": {
            **request.to_dict(),
            "calendar_fingerprint": calendar_fingerprint(),
            "canonical_content_fingerprint": content_fp,
            "manifest_fingerprint": manifest_fp,
            # Raw CONTENT fingerprints are stable across reruns and belong to
            # the manifest. Acquisition EVENT ids legitimately differ per run
            # (same observations, two fetches) and live in the build event below
            # -- keeping them here made an idempotent rerun look like corruption.
            "raw_content_fingerprints": sorted(
                {a["raw_payload_hash"] for a in acq["acquisitions"]
                 if a.get("raw_payload_hash")}),
        },
    }, root=root)

    # DATASET BUILD EVENT: per-run provenance, deliberately outside the
    # content-addressed manifest object.
    ST.write_snapshot(ST.DATASET_EVENTS, ST.content_hash({
        "manifest_fingerprint": manifest_fp,
        "acquisition_event_ids": [a.get("acquisition_event_id")
                                  for a in acq["acquisitions"]],
    }), {"build_event.json": {
        "schema_version": SCHEMA_VERSION,
        "manifest_fingerprint": manifest_fp,
        "canonical_content_fingerprint": content_fp,
        "request_fingerprint": request.fingerprint(),
        "acquisition_event_ids": [a.get("acquisition_event_id")
                                  for a in acq["acquisitions"]],
        "acquisitions": acq["acquisitions"],
    }}, root=root)

    values = build_features(ds, lookback=lookback)
    feature_fp = F.feature_fingerprint(values)
    fmanifest = F.feature_manifest(values, dataset_id=ds.dataset_id(),
                                   dataset_fingerprint=content_fp,
                                   manifest_fingerprint=manifest_fp)
    ST.write_snapshot(ST.FEATURES, feature_fp, {
        "features.json": [v.to_dict() for v in values],
        # generated_at stays OUT of the content object; it would make every
        # rebuild look like a collision.
        "feature_content_manifest.json": {
            "schema_version": SCHEMA_VERSION,
            "feature_fingerprint": feature_fp,
            "feature_set_version": F.FEATURE_SET_VERSION,
            "source_dataset_fingerprint": content_fp,
            "source_dataset_manifest_fingerprint": manifest_fp,
            "observation_count": len(values),
            "features_enabled": list(F.ENABLED_FEATURES),
        },
    }, root=root)
    ST.write_snapshot(ST.FEATURE_EVENTS, ST.content_hash(
        {"feature_fingerprint": feature_fp, "generated_at": fmanifest["generated_at"]}),
        {"build_event.json": fmanifest}, root=root)

    verification = ST.verify_canonical_snapshot(content_fp, root=root)
    return {
        "schema_version": SCHEMA_VERSION,
        "request_fingerprint": request.fingerprint(),
        "calendar_fingerprint": calendar_fingerprint(),
        "acquisitions": acq["acquisitions"],
        "requested_symbol_dates": len(request.resolved_items()),
        **request.calendar_resolution_summary(),
        "sessions_reconciled": len(ds.reconciliations),
        "sessions_admitted": len(ds.admitted),
        "sessions_rejected": len(ds.rejected),
        "sessions_not_trading": len(ds.not_trading),
        "bars_admitted": len(ds.bars),
        "dataset_fingerprint": content_fp,
        "manifest_fingerprint": manifest_fp,
        "adjustment_state": ds.adjustment_state,
        "acquisition_event_ids": [a.get("acquisition_event_id") for a in acq["acquisitions"]],
        "raw_content_fingerprints": [a.get("raw_payload_hash") for a in acq["acquisitions"]],
        "canonical_snapshot_path": f"outputs/backtest/intraday/datasets/content/{content_fp}",
        "dataset_manifest_path": f"outputs/backtest/intraday/datasets/manifests/{manifest_fp}",
        "feature_observations": len(values),
        "feature_fingerprint": feature_fp,
        "feature_snapshot_path": f"outputs/backtest/intraday/features/content/{feature_fp}",
        "feature_verification": ST.verify_feature_snapshot(feature_fp, root=root),
        "canonical_verification": verification,
        "rejections": rejects,
        "strategy_validation_allowed": False,
    }
