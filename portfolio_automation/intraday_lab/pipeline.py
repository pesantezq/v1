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
from portfolio_automation.intraday_lab import providers as PR
from portfolio_automation.intraday_lab import storage as ST
from portfolio_automation.intraday_lab.data import normalize_fmp_rows, fetch_status
from portfolio_automation.intraday_lab.dataset import (
    DatasetRequest, CanonicalDataset, build_canonical_dataset, dataset_manifest,
    rejection_report, calendar_fingerprint, _calendar_identity,
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


def acquire(request: DatasetRequest, source: Any,
            *, root: str = ".", now: datetime | None = None) -> dict:
    """Fetch through a governed provider and persist raw evidence.

    `source` is an `IntradayProvider` — an object that KNOWS its own identity.
    A bare `fetcher(symbol, start, end)` callable is still accepted and adapted,
    but it is labelled `callable:unspecified` rather than being assumed to be
    FMP: provider + endpoint are part of raw identity now, so stamping an
    unverified claim there would mis-address the immutable evidence.

    A provider error or empty response is recorded, never raised away: the
    requested session must survive into the reconciliation trail.
    """
    now = now or datetime.now(timezone.utc)
    provider = PR.coerce_provider(source, timeframe=request.timeframe)
    provider_name = provider.provider_id
    symbols = sorted({s for s, _ in request.certified_sessions()})
    acquisitions: list[dict] = []
    bars_by_date: dict[tuple[str, date], list] = {}

    try:
        endpoint = provider.endpoint_for(request.timeframe)
        endpoint_error = None
    except Exception as exc:
        # An unentitled/unregistered timeframe must not silently become a data
        # gap. Every requested symbol is recorded as a provider error instead.
        endpoint = f"/unresolved/{request.timeframe}"
        endpoint_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    for symbol in symbols:
        rows, http = None, None
        error = endpoint_error
        error_code = "PROVIDER_ENDPOINT_UNRESOLVED" if endpoint_error else None
        if endpoint_error is None:
            try:
                rows, http = provider.fetch(symbol, request.start.isoformat(),
                                            request.end.isoformat(),
                                            request.timeframe)
            except PR.ProviderBudgetRefusal as exc:   # our refusal, not their silence
                error = f"{type(exc).__name__}: {str(exc)[:160]}"
                error_code = "PROVIDER_BUDGET_REFUSED"
            except Exception as exc:                  # provider failure is evidence
                error = f"{type(exc).__name__}: {str(exc)[:160]}"
                error_code = "PROVIDER_EXCEPTION"

        status = fetch_status(rows, http_status=http) if error is None else "PROVIDER_ERROR"
        payload_hash = (ST.raw_payload_hash(rows, symbol=symbol,
                                            timeframe=request.timeframe,
                                            provider=provider_name,
                                            endpoint=endpoint)
                        if isinstance(rows, list) else None)
        record = {
            "schema_version": SCHEMA_VERSION,
            "request_fingerprint": request.fingerprint(),
            "provider": provider_name,
            "endpoint": endpoint,
            "symbol": symbol, "timeframe": request.timeframe,
            "requested_start": request.start.isoformat(),
            "requested_end": request.end.isoformat(),
            "retrieved_at": now.isoformat(),
            "provider_status": status, "http_status": http,
            "row_count": len(rows) if isinstance(rows, list) else 0,
            "raw_payload_hash": payload_hash,
            "error_code": error_code,
            "error_message_safe": error,
            "normalization_status": "PENDING",
            "provider_provenance": provider.provenance(),
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
                "content_manifest.json": ST.raw_content_manifest(
                    rows, symbol=symbol, timeframe=request.timeframe,
                    provider=provider_name, endpoint=endpoint,
                    identity=payload_hash),
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
    norm_failures = {a["symbol"] for a in acquisitions
                     if str(a.get("normalization_status", "")).startswith("FAILED")}
    return {"acquisitions": acquisitions, "bars_by_date": bars_by_date,
            "provider_failures": failures, "normalization_failures": norm_failures,
            "provider_provenance": provider.provenance()}


def features_from_bars(bars: Sequence[Any], *, dataset_id: str, fingerprint: str,
                       manifest_fingerprint: str,
                       lookback: int = 3) -> list[F.FeatureValue]:
    """The feature derivation algorithm, as a pure function of bars + identity.

    Extracted so identity migration REMINTS features with exactly this code
    rather than a parallel implementation. Feature identity binds to the source
    dataset, so a migrated dataset must produce numerically identical values
    under a different feature fingerprint — provable only if both paths share
    one algorithm.
    """
    out: list[F.FeatureValue] = []
    for (_symbol, _tf), series in F.group_series(bars).items():
        for i in range(len(series)):
            v = F.compute_return_nbar(series, i, lookback, dataset_id=dataset_id,
                                      fingerprint=fingerprint,
                                      manifest_fingerprint=manifest_fingerprint)
            if v:
                out.append(v)
    return out


def build_features(ds: CanonicalDataset, *, lookback: int = 3) -> list[F.FeatureValue]:
    """Derive features with provenance taken FROM the dataset, not from args."""
    return features_from_bars(ds.bars, dataset_id=ds.dataset_id(),
                              fingerprint=ds.fingerprint(),
                              manifest_fingerprint=ds.manifest_fingerprint(),
                              lookback=lookback)


def build_historical_research_dataset(
    request: DatasetRequest, source: Any,
    *, root: str = ".", dry_run: bool = False, lookback: int = 3,
) -> dict:
    """The full governed chain. Returns identities and artifact paths."""
    if dry_run:
        return plan_request(request)

    acq = acquire(request, source, root=root)
    ds = build_canonical_dataset(acq["bars_by_date"], request=request,
                                 provider_failures=acq["provider_failures"],
                                 normalization_failures=acq["normalization_failures"])

    manifest = dataset_manifest(ds)
    rejects = rejection_report(ds)
    content_fp, manifest_fp = ds.fingerprint(), ds.manifest_fingerprint()

    # CONTENT: canonical bars only, deduplicated across research requests.
    canonical_rows = ST.bars_to_rows(ds.bars)
    ST.write_snapshot(ST.DATASETS, content_fp, {
        "canonical_bars.json": canonical_rows,
        "content_manifest.json": ST.canonical_content_manifest(
            canonical_rows, identity=content_fp, timeframe=ds.timeframe,
            adjustment_state=ds.adjustment_state),
    }, root=root)

    # MANIFEST: the research interpretation. Two requests producing identical
    # bars share the content object above but keep separate manifests here.
    ST.write_snapshot(ST.DATASET_MANIFESTS, manifest_fp, {
        "dataset_manifest.json": manifest,
        "reconciliation.json": [r.detail() for r in ds.reconciliations],
        "request_manifest.json": {
            **request.to_dict(),
            "calendar_fingerprint": calendar_fingerprint(),
            # The full calendar IDENTITY, not just its hash. The manifest
            # fingerprint is computed over this object, so without it a later
            # remint could only reproduce the manifest by consulting the LIVE
            # calendar — which silently reinterprets archived research the first
            # time the calendar changes. Persisting it keeps an old manifest
            # reproducible under the semantics it was actually built with.
            "calendar_identity": _calendar_identity(),
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
        # Only real raw evidence. A provider failure has no payload, so a None
        # here would be indexed by callers as if it were a content id.
        "raw_content_fingerprints": [a["raw_payload_hash"] for a in acq["acquisitions"]
                                     if a.get("raw_payload_hash")],
        "provider_provenance": acq.get("provider_provenance"),
        "canonical_snapshot_path": f"outputs/backtest/intraday/datasets/content/{content_fp}",
        "dataset_manifest_path": f"outputs/backtest/intraday/datasets/manifests/{manifest_fp}",
        "feature_observations": len(values),
        "feature_fingerprint": feature_fp,
        "feature_snapshot_path": f"outputs/backtest/intraday/features/content/{feature_fp}",
        "feature_verification": ST.verify_feature_snapshot(feature_fp, root=root),
        "canonical_verification": verification,
        "provenance_verification": ST.verify_dataset_provenance(manifest_fp, root=root),
        "rejections": rejects,
        "strategy_validation_allowed": False,
    }
