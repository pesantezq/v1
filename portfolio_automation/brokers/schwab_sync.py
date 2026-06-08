# portfolio_automation/brokers/schwab_sync.py
"""Schwab sync orchestrator + CLI. Observe-only; read-only; never raises.
Writes broker_sync_status / schwab_portfolio_snapshot / schwab_positions /
portfolio_reconciliation / portfolio_config_update_proposal."""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.brokers import broker_models as bm
from portfolio_automation.brokers import broker_status as bstat
from portfolio_automation.brokers import broker_reconciliation as brec
from portfolio_automation.brokers import schwab_oauth as oauth


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(root: Path, name: str, payload: dict) -> Path:
    return safe_write_json(OutputNamespace.LATEST, name, payload, base_dir=root / "outputs")


def _read_json(root: Path, name: str) -> Any:
    try:
        return json.loads((root / "outputs/latest" / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def _enabled() -> bool:
    return oauth.read_only_mode()  # layer is active in read-only mode; inert without creds


def run_status(*, root: Path = Path("."), now: str | None = None,
               last_error: str | None = None, account_count: int = 0,
               position_count: int = 0, authenticated: bool | None = None) -> dict:
    root = Path(root)
    ts = now or _now()
    configured = oauth.is_configured()
    auth = bool(authenticated) if authenticated is not None else (configured and oauth.load_token() is not None)
    st = bstat.build_status(enabled=_enabled(), configured=configured, authenticated=auth,
                            account_count=account_count, position_count=position_count,
                            last_success_at=(ts if (auth and not last_error) else None),
                            last_error=last_error, now_iso=ts)
    try:
        _write(root, "broker_sync_status.json", st)
    except Exception:
        pass
    return st


def run_sync(*, root: Path = Path("."), now: str | None = None) -> dict:
    root = Path(root)
    ts = now or _now()
    if not (oauth.is_configured() and _enabled()):
        return run_status(root=root, now=ts)  # fail-closed: unconfigured/disabled
    try:
        token = oauth.valid_access_token()
        if not token:
            return run_status(root=root, now=ts, last_error="unauthenticated: run OAuth flow")
        from portfolio_automation.brokers.schwab_client import SchwabClient
        client = SchwabClient(access_token=token)
        nums = client.get_account_numbers()
        raw = client.get_accounts(positions=True)
        snap = bm.normalize_accounts(raw, nums, now_iso=ts)
        sd, pr = bm.snapshot_dict(snap), bm.positions_dict(snap)
        _write(root, "schwab_portfolio_snapshot.json", sd)
        _write(root, "schwab_positions.json", pr)
        _archive(root, ts, sd, pr)
        return run_status(root=root, now=ts, authenticated=True,
                          account_count=len(sd["accounts"]), position_count=len(pr["positions"]))
    except Exception as exc:
        return run_status(root=root, now=ts, last_error=bm.redact(str(exc)))


def run_reconcile(*, root: Path = Path("."), now: str | None = None) -> dict:
    root = Path(root)
    ts = now or _now()
    snap = _read_json(root, "schwab_portfolio_snapshot.json") or {"totals": {}}
    pos = _read_json(root, "schwab_positions.json") or {"positions": []}
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except Exception:
        config = {}
    recon = brec.reconcile(snap, pos, config)
    recon.setdefault("generated_at", ts)
    proposal = brec.build_proposal(recon, config, now_iso=ts)
    try:
        _write(root, "portfolio_reconciliation.json", recon)
        _write(root, "portfolio_config_update_proposal.json", proposal)
    except Exception:
        pass
    return recon


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
    if args.sync:
        st = run_sync()
    elif args.reconcile:
        run_sync()
        st = run_status()
        run_reconcile()
    else:
        st = run_status()
    # print status WITHOUT secrets
    print(f"schwab: configured={st['configured']} authenticated={st['authenticated']} "
          f"status={st['overall_status']} accounts={st['account_count']} positions={st['position_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
