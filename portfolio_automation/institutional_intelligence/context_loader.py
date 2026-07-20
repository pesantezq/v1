"""
Institutional Intelligence orchestrator — the pipeline entry point.

Ties the subsystem together for one run: load the manager registry -> for each
ENABLED manager (effective as-of the run date) discover 13F filings via the
governed SEC client -> parse the latest two holdings filings -> store (PIT) ->
compute position changes -> score each change -> aggregate per-symbol consensus
-> write the observe-only artifacts.

Inert by default: with no enabled managers (the shipped state) it writes an
honest ``disabled`` / ``insufficient_data`` artifact and returns — it never
raises into the pipeline. Offline/fixture-driven: all I/O goes through the
governed SEC client, which resolves fixtures first.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import PACKAGE_SOURCE
from . import artifact_writer as aw
from . import consensus as cons
from . import filing_discovery as fd
from . import filing_parser as fp
from . import manager_registry as mr
from . import manager_scoring as ms
from . import options_interpretation as oi
from . import position_changes as pcm
from .holdings_store import HoldingsStore
from .sec_client import GovernedSECClient, SECClientConfig, cik_to_padded
from .security_identity import SecurityIdentityResolver


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_config(root: Path) -> dict[str, Any]:
    try:
        base = json.loads((root / "config" / "base.json").read_text(encoding="utf-8"))
        return base.get("institutional_intelligence") or {}
    except Exception:
        return {}


def _infotable_url(cik: str, accession: str, primary_doc: str | None) -> str:
    acc_nodash = accession.replace("-", "")
    doc = primary_doc or "infotable.xml"
    return f"{fd.__dict__.get('ARCHIVES_BASE', 'https://www.sec.gov/Archives/edgar/data')}/{int(cik)}/{acc_nodash}/{doc}"


def run_institutional_intelligence(
    root: str = ".",
    *,
    client: GovernedSECClient | None = None,
    resolver: SecurityIdentityResolver | None = None,
    registry_path: str | Path | None = None,
    store: HoldingsStore | None = None,
    now: date | None = None,
    write: bool = True,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one institutional-intelligence cycle. Never raises."""
    root_path = Path(root)
    generated_at = _now_iso()
    as_of = now or datetime.now(timezone.utc).date()
    data_as_of = as_of.isoformat()
    cfg = config if config is not None else _load_config(root_path)
    resolver = resolver or SecurityIdentityResolver()

    enabled = bool(cfg.get("enabled", False))

    def _write(records: list[dict], status: str, live_ready: bool) -> dict[str, Any]:
        art = aw.build_intelligence_artifact(records=records, generated_at=generated_at,
                                             data_as_of=data_as_of)
        st = aw.build_status_artifact(status=status, records=records,
                                      generated_at=generated_at, data_as_of=data_as_of,
                                      enabled=enabled, live_ready=live_ready)
        if write:
            try:
                from portfolio_automation.data_governance import (
                    OutputNamespace, safe_write_json,
                )
                base_dir = root_path / "outputs"
                safe_write_json(OutputNamespace.LATEST, "institutional_intelligence.json",
                                art, base_dir=base_dir)
                safe_write_json(OutputNamespace.LATEST, "institutional_intelligence_status.json",
                                st, base_dir=base_dir)
                safe_write_json(OutputNamespace.LATEST, "institutional_consensus.json",
                                {"records": records, **{k: art[k] for k in
                                 ("schema_version", "observe_only", "feeds_decision_engine",
                                  "generated_at", "data_as_of")}}, base_dir=base_dir)
            except Exception:
                pass
        return {"status": status, "records": records, "artifact": art, "status_artifact": st}

    try:
        if not enabled:
            return _write([], aw.STATUS_DISABLED, live_ready=False)

        registry = mr.load_registry(registry_path or cfg.get(
            "manager_registry_path", "config/institutional_managers.yaml"))
        managers = registry.effective_on(as_of, enabled_only=True)
        if not managers:
            # Enabled feature but no enabled managers -> honest insufficient_data.
            return _write([], aw.STATUS_INSUFFICIENT, live_ready=False)

        client = client or GovernedSECClient(SECClientConfig(
            live_enabled=bool(cfg.get("live_sec_ingestion_enabled", False)),
            requests_per_second=int(cfg.get("sec_requests_per_second", 5)),
            db_path=root_path / "data" / "institutional_intelligence.db"))
        store = store or HoldingsStore(root_path / "data" / "institutional_intelligence.db")

        # Collect per-symbol manager signals across all managers.
        by_symbol: dict[str, list[cons.ManagerConsensusInput]] = {}
        symbol_meta: dict[str, dict] = {}
        for mgr in managers:
            signals = _process_manager(mgr, client, store, resolver, as_of)
            for sym, sig in signals.items():
                by_symbol.setdefault(sym, []).append(sig)
                symbol_meta.setdefault(sym, {})

        records: list[dict] = []
        for sym, mgr_inputs in by_symbol.items():
            c = cons.build_symbol_consensus(
                sym, mgr_inputs,
                min_effective=float(cfg.get("minimum_effective_managers", 1.5)),
                min_confidence=float(cfg.get("minimum_consensus_confidence", 0.55)))
            from dataclasses import asdict
            rec = aw.build_symbol_record(
                symbol=sym, as_of=data_as_of, consensus=asdict(c),
                latest_report_period=None, filing_age_days=c.filing_age_max,
                manager_signals=[], evidence_refs=[])
            records.append(rec)

        status = aw.determine_status(
            enabled=True, failed=False, records=records,
            stale_after_days=int(cfg.get("stale_after_days", 140)),
            min_confidence=float(cfg.get("minimum_consensus_confidence", 0.55)))
        return _write(records, status, live_ready=client.live_ready)

    except Exception as exc:  # pragma: no cover - defensive; never break pipeline
        res = _write([], aw.STATUS_FAILED, live_ready=False)
        res["error"] = f"{type(exc).__name__}: {exc}"
        return res


def _process_manager(mgr, client, store, resolver, as_of: date
                     ) -> dict[str, cons.ManagerConsensusInput]:
    """Discover + parse + store + score one manager; return per-symbol signals."""
    out: dict[str, cons.ManagerConsensusInput] = {}
    filings = fd.discover_filings(client, mgr.cik)
    holdings_filings = sorted([f for f in filings if f.is_holdings],
                              key=lambda f: f.filed_at)
    if not holdings_filings:
        return out
    latest = holdings_filings[-1]
    prev = holdings_filings[-2] if len(holdings_filings) >= 2 else None

    cur_parsed = _fetch_parse(client, store, mgr.cik, latest)
    prev_parsed = _fetch_parse(client, store, mgr.cik, prev) if prev else None

    cur = [(h, resolver.resolve(cusip=h.cusip, figi=h.figi, issuer_name=h.issuer_name,
                                as_of=as_of)) for h in (cur_parsed.holdings if cur_parsed else [])]
    prv = ([(h, resolver.resolve(cusip=h.cusip, figi=h.figi, issuer_name=h.issuer_name,
                                 as_of=as_of)) for h in prev_parsed.holdings]
           if prev_parsed else None)

    changes = pcm.compute_position_changes(cur, prv, as_of=as_of,
                                           current_filed_at=latest.filed_at)
    for ch in changes.changes:
        if not ch.identity_resolved or ch.symbol is None:
            continue
        opt_ctx = oi.classify_option_context(
            ch.put_call, manager_options_complexity=mgr.options_complexity)
        score = ms.score_manager_symbol(
            ch, manager_quality_prior=mgr.manager_quality_prior,
            cloneability=mgr.cloneability, option_ctx=opt_ctx,
            manager_specialization=mgr.specialization, is_amendment=latest.is_amendment)
        if score.final_score == 0.0:
            continue
        out[ch.symbol] = cons.ManagerConsensusInput(
            internal_id=mgr.internal_id, archetype=mgr.strategy_archetype,
            cloneability=mgr.cloneability, final_score=score.final_score,
            filing_age_days=ch.filing_age_days,
            data_quality=score.data_quality_score,
            market_maker=mgr.market_maker,
            options_dominated=(ch.put_call != "none"),
            is_amended=latest.is_amendment)
    return out


def _fetch_parse(client, store, cik: str, ref):
    if ref is None:
        return None
    url = _infotable_url(cik, ref.accession, ref.primary_doc)
    resp = client.fetch(url)
    parsed = fp.parse_information_table(
        resp.body or "", accession=ref.accession, form_type=ref.form_type,
        is_notice=ref.is_notice)
    try:
        store.import_filing(
            cik=cik, accession=ref.accession, form_type=ref.form_type,
            filed_at=ref.filed_at, report_period=ref.report_period,
            is_amendment=ref.is_amendment, parsed=parsed,
            content_hash=resp.content_hash)
    except Exception:
        pass
    return parsed
