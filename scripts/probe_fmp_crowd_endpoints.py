#!/usr/bin/env python3
"""FMP crowd-endpoint capability probe (observe-only, manual diagnostic).

Probes the net-new crowd-intelligence candidate endpoints against the live FMP key
(hard cap 80 calls, tiny requests), classifies each, and writes:
  outputs/latest/fmp_endpoint_capabilities.json
  outputs/latest/fmp_crowd_probe_summary.md
plus the fmp_endpoint_capabilities table in data/crowd_intelligence.db.

Direct HTTP (NOT the cache/governor path) — a capability check needs the raw
status code and must not be cached. Never raises on a single endpoint failure.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.env import get_secret
from portfolio_automation.crowd_intelligence import endpoint_registry as reg
from portfolio_automation.crowd_intelligence.fmp_capability_probe import (
    probe_all, summarize_by_status,
)
from portfolio_automation.crowd_intelligence.capability_store import CapabilityStore

_DOMAIN = "https://financialmodelingprep.com"
_MAX_CALLS = 80


def _make_http_get_status(api_key: str):
    def http_get_status(path: str, params: dict) -> tuple[int | None, Any, str]:
        q = {**params, "apikey": api_key}
        url = f"{_DOMAIN}{path}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(url, headers={"User-Agent": "PortfolioBot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    body = None
                return resp.status, body, ""
        except urllib.error.HTTPError as exc:
            body = None
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                pass
            return exc.code, body, f"HTTP {exc.code}"
        except Exception as exc:  # URLError / timeout / DNS
            return -1, None, f"{type(exc).__name__}: {exc}"
    return http_get_status


def main(root: str = ".") -> int:
    root_path = Path(root)
    api_key = (get_secret("FMP_API_KEY") or "").strip()
    if not api_key:
        print("FMP_API_KEY not set — cannot probe.", file=sys.stderr)
        return 2

    now_iso = datetime.now(timezone.utc).isoformat()
    targets = reg.probe_targets()
    results = probe_all(targets, _make_http_get_status(api_key),
                        max_calls=_MAX_CALLS, symbol="AAPL", now_iso=now_iso)
    summary = summarize_by_status(results)

    # Persist to SQLite.
    store = CapabilityStore(root_path / "data" / "crowd_intelligence.db")
    store.upsert(results)

    # Artifacts.
    latest = root_path / "outputs" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    payload = {
        "observe_only": True,
        "source": "fmp_capability_probe",
        "generated_at": now_iso,
        "max_calls": _MAX_CALLS,
        "calls_made": sum(1 for r in results if r["status"] != "SKIPPED_CAP"),
        "confirmed_baseline": sorted(reg.CONFIRMED_BASELINE),
        "summary": summary,
        "records": results,
    }
    (latest / "fmp_endpoint_capabilities.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")

    md = [f"# FMP Crowd Endpoint Probe — {now_iso}", "",
          "Observe-only capability discovery. Not a trade signal.", "",
          f"Confirmed Starter baseline (not probed): {', '.join(sorted(reg.CONFIRMED_BASELINE))}", "",
          "## Status summary", ""]
    for st, n in sorted(summary.items(), key=lambda kv: -kv[1]):
        md.append(f"- **{st}**: {n}")
    md += ["", "## Per-endpoint", "",
           "| endpoint_id | category | status | http | sample_fields |",
           "|---|---|---|---|---|"]
    for r in results:
        sf = ", ".join((r.get("sample_fields") or [])[:5])
        md.append(f"| {r['endpoint_id']} | {r.get('category','')} | {r['status']} | "
                  f"{r.get('http_status')} | {sf} |")
    (latest / "fmp_crowd_probe_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"probe complete: {payload['calls_made']} calls · " +
          " · ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
