# portfolio_automation/brokers/schwab_sync.py
"""Schwab sync orchestrator + CLI. Observe-only; read-only; never raises.

Data flow (v2 foundation):

    SchwabAuthManager.acquire()          structured auth state, one bounded refresh
      -> SchwabClient GETs               accounts/accountNumbers + accounts?fields=positions
      -> broker_models.normalize_accounts
      -> BrokerPortfolioSnapshot         versioned, secret-free contract
      -> schwab_evidence_adapter         canonical EvidenceSnapshot
      -> evidence_gateway.admit          ADMITTED | REFUSED
      -> broker_evidence_store           latest_attempt (always) / latest_admitted (admitted only)
      -> compatibility projections       schwab_portfolio_snapshot / schwab_positions, DERIVED
                                         from the admitted evidence (never written otherwise)

Writes broker_sync_status / schwab_token_lifecycle / broker_evidence_* /
schwab_portfolio_snapshot / schwab_positions / portfolio_reconciliation /
portfolio_config_update_proposal. Never config.json. Never a trade.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.brokers import broker_evidence as BE
from portfolio_automation.brokers import broker_evidence_store as ES
from portfolio_automation.brokers import broker_models as bm
from portfolio_automation.brokers import broker_status as bstat
from portfolio_automation.brokers import broker_reconciliation as brec
from portfolio_automation.brokers import schwab_evidence_adapter as AD
from portfolio_automation.brokers import schwab_oauth as oauth
from portfolio_automation.brokers.schwab_auth_manager import AuthResult, SchwabAuthManager

TOKEN_LIFECYCLE_FILE = "schwab_token_lifecycle.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _write(root: Path, name: str, payload: dict) -> Path:
    return safe_write_json(OutputNamespace.LATEST, name, payload, base_dir=root / "outputs")


def _read_json(root: Path, name: str) -> Any:
    try:
        data = json.loads((root / "outputs/latest" / name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _enabled() -> bool:
    return oauth.read_only_mode()  # layer is active in read-only mode; inert without creds


def _acquire_auth() -> AuthResult:
    """The ONE place the sync asks for a token. Seam for tests."""
    return SchwabAuthManager().acquire()


def _source_commit(root: Path) -> str:
    """Best-effort code identity for provenance; never raises, never secrets."""
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5, check=False)
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and len(sha) == 40 else "UNAVAILABLE"
    except Exception:
        return "UNAVAILABLE"


def _sync_id(ts: str) -> str:
    compact = "".join(ch for ch in ts[:19] if ch.isdigit())
    return f"schwab-sync-{compact}-{os.getpid()}"


def _write_token_lifecycle(root: Path, auth: AuthResult, ts: str) -> None:
    """Non-secret token lifecycle telemetry (G4). Best-effort, never raises."""
    payload = {"generated_at": ts, "observe_only": True, "source": "schwab",
               "read_only_mode": True, "trading_enabled": False, **auth.to_dict()}
    try:
        _write(root, TOKEN_LIFECYCLE_FILE, payload)
    except Exception:
        pass


def _evidence_summary(root: Path) -> dict[str, Any]:
    try:
        return ES.read_state(root).to_dict()
    except Exception:
        return {}


def run_status(*, root: Path = Path("."), now: str | None = None,
               last_error: str | None = None, account_count: int = 0,
               position_count: int = 0, authenticated: bool | None = None,
               auth_state: str | None = None) -> dict:
    root = Path(root)
    ts = now or _now()
    configured = oauth.is_configured()
    auth = bool(authenticated) if authenticated is not None else (configured and oauth.load_token() is not None)
    try:
        reauth = oauth.refresh_token_status() if configured else None
    except Exception:
        reauth = None  # observe-only: expiry telemetry never breaks the status write
    st = bstat.build_status(enabled=_enabled(), configured=configured, authenticated=auth,
                            account_count=account_count, position_count=position_count,
                            last_success_at=(ts if (auth and not last_error) else None),
                            last_error=last_error, now_iso=ts, reauth=reauth,
                            auth_state=auth_state, evidence=_evidence_summary(root))
    try:
        _write(root, "broker_sync_status.json", st)
    except Exception:
        pass
    return st


def run_sync(*, root: Path = Path("."), now: str | None = None) -> dict:
    root = Path(root)
    ts = now or _now()
    # One sync, one logical instant. PR #50 put the ADMITTED record and its
    # write-once archive day on this clock; the attempt record is the other half
    # of the same observation and must not be stamped from the wall clock, or a
    # replayed or backdated sync emits two disagreeing dates for one run.
    # `_parse_ts` is total -- it falls back to now() on an unparseable value --
    # so hoisting it here introduces no new failure path.
    ts_dt = _parse_ts(ts)
    if not (oauth.is_configured() and _enabled()):
        ES.record_attempt(root, sync_id=_sync_id(ts), outcome=ES.SyncOutcome.UNCONFIGURED,
                          auth_state="UNCONFIGURED", now=ts_dt)
        return run_status(root=root, now=ts, auth_state="UNCONFIGURED")  # fail-closed: unconfigured/disabled
    sync_id = _sync_id(ts)
    auth = _acquire_auth()
    _write_token_lifecycle(root, auth, ts)
    if not auth.ok:
        outcome = ES.SyncOutcome.REAUTH_REQUIRED if auth.reauth_required else ES.SyncOutcome.AUTH_UNAVAILABLE
        ES.record_attempt(root, sync_id=sync_id, outcome=outcome, auth_state=auth.state.value,
                          error=auth.detail, now=ts_dt)
        hint = "run the interactive re-auth" if auth.reauth_required else "transient; will retry next run"
        return run_status(root=root, now=ts, auth_state=auth.state.value,
                          last_error=f"{auth.state.value}: {bm.redact(auth.detail)} ({hint})")
    try:
        from portfolio_automation.brokers.schwab_client import SchwabClient
        client = SchwabClient(access_token=auth.access_token)
        nums = client.get_account_numbers()
        raw = client.get_accounts(positions=True)
    except Exception as exc:
        ES.record_attempt(root, sync_id=sync_id, outcome=ES.SyncOutcome.ACQUISITION_FAILED,
                          auth_state=auth.state.value, error=str(exc), now=ts_dt)
        return run_status(root=root, now=ts, auth_state=auth.state.value,
                          last_error=f"ACQUISITION_FAILED: {bm.redact(str(exc))}")
    # A malformed or empty response is NOT an empty portfolio.
    if not isinstance(raw, list) or not isinstance(nums, list):
        ES.record_attempt(root, sync_id=sync_id, outcome=ES.SyncOutcome.SCHEMA_DRIFT,
                          auth_state=auth.state.value,
                          error=f"unexpected response types: accounts={type(raw).__name__} numbers={type(nums).__name__}",
                          now=ts_dt)
        return run_status(root=root, now=ts, auth_state=auth.state.value,
                          last_error="SCHEMA_DRIFT: accounts response is not a list")
    try:
        snap = bm.normalize_accounts(raw, nums, now_iso=ts)
        if not snap.accounts:
            raise ValueError("no accounts could be normalized from the response")
        bps = BE.BrokerPortfolioSnapshot.from_normalized(
            snap, sync_id=sync_id, retrieved_at=_parse_ts(ts), source_commit=_source_commit(root))
        evidence = AD.to_evidence_snapshot(bps)
    except Exception as exc:
        ES.record_attempt(root, sync_id=sync_id, outcome=ES.SyncOutcome.NORMALIZATION_FAILED,
                          auth_state=auth.state.value, error=str(exc), now=ts_dt)
        return run_status(root=root, now=ts, auth_state=auth.state.value,
                          last_error=f"NORMALIZATION_FAILED: {bm.redact(str(exc))}")

    decision = AD.admit_broker_snapshot(evidence, as_of=evidence.pit.retrieved_at)
    if not decision.admitted:
        ES.record_attempt(root, sync_id=sync_id, outcome=ES.SyncOutcome.EVIDENCE_REFUSED,
                          auth_state=auth.state.value, admission=decision, error=decision.detail,
                          now=ts_dt)
        return run_status(root=root, now=ts, auth_state=auth.state.value,
                          last_error=f"EVIDENCE_REFUSED: {decision.reason.value}")

    try:
        # Persist under the SAME deterministic instant the evidence snapshot
        # already carries (its possession PIT), so the write-once archive day,
        # admitted_at and generated_at agree with the sync/evidence time rather
        # than the wall clock at persist time.
        ES.record_admitted(root, snapshot=evidence, decision=decision,
                           sync_id=sync_id, now=evidence.pit.retrieved_at)
        ES.record_attempt(root, sync_id=sync_id, outcome=ES.SyncOutcome.ADMITTED,
                          auth_state=auth.state.value, admission=decision,
                          snapshot_id=evidence.snapshot_id, now=ts_dt)
        sd = AD.project_snapshot_dict(evidence, decision)
        pr = AD.project_positions_dict(evidence, decision)
        _write(root, "schwab_portfolio_snapshot.json", sd)
        _write(root, "schwab_positions.json", pr)
    except Exception as exc:
        return run_status(root=root, now=ts, auth_state=auth.state.value,
                          last_error=f"EVIDENCE_PERSIST_FAILED: {bm.redact(str(exc))}")
    # per-lot tax data (best-effort; degrades to has_lots:false when absent)
    try:
        from portfolio_automation.brokers.schwab_tax_lots import normalize_tax_lots
        flat = {"positions": [p for acct in raw
                              for p in ((acct.get("securitiesAccount") or {}).get("positions") or [])]}
        _write(root, "schwab_tax_lots.json", normalize_tax_lots(flat, now_iso=ts))
    except Exception:
        pass
    _archive(root, ts, sd, pr)
    return run_status(root=root, now=ts, authenticated=True, auth_state=auth.state.value,
                      account_count=len(sd["accounts"]), position_count=len(pr["positions"]))


def run_reconcile(*, root: Path = Path("."), now: str | None = None) -> dict:
    root = Path(root)
    ts = now or _now()
    snap = _read_json(root, "schwab_portfolio_snapshot.json") or {"totals": {}}
    pos = _read_json(root, "schwab_positions.json") or {"positions": []}
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except Exception:
        config = {}
    try:
        recon = brec.reconcile(snap, pos, config)
        recon.setdefault("generated_at", ts)
        proposal = brec.build_proposal(recon, config, now_iso=ts)
        try:
            _write(root, "portfolio_reconciliation.json", recon)
            _write(root, "portfolio_config_update_proposal.json", proposal)
        except Exception:
            pass
        return recon
    except Exception as exc:
        return {"generated_at": ts, "source": "schwab", "summary_status": "error",
                "operator_review_message": bm.redact(str(exc))}


def _archive(root: Path, ts: str, *payloads: dict) -> None:
    try:
        day = ts[:10]
        adir = root / "outputs/archive/broker_sync" / day
        adir.mkdir(parents=True, exist_ok=True)
        for name, payload in zip(("schwab_portfolio_snapshot.json", "schwab_positions.json"), payloads):
            (adir / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m portfolio_automation.brokers.schwab_sync",
                                 description="Schwab READ-ONLY sync (no trading).")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--reconcile", action="store_true")
    args = ap.parse_args(argv)
    print("READ-ONLY MODE ACTIVE — no trading endpoints are called.")
    if args.sync and args.reconcile:
        # combined: live sync then reconcile from the just-written snapshot
        st = run_sync()
        run_reconcile()
    elif args.sync:
        st = run_sync()
    elif args.reconcile:
        # reconcile-only: use cached snapshot/positions; no network sync
        run_reconcile()
        st = run_status()
    else:
        st = run_status()
    # print status WITHOUT secrets
    print(f"schwab: configured={st['configured']} authenticated={st['authenticated']} "
          f"status={st['overall_status']} accounts={st['account_count']} positions={st['position_count']} "
          f"auth_state={st.get('auth_state')} evidence={st.get('evidence_truth_status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
