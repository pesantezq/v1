"""
Retune Impact Tracker — observe-only gauge-version ledger.

When the operator tunes a gauge knob, we want to know — once outcomes
resolve — whether the new setting was better than the old one. That
requires *tagging* every decision with the gauge state that produced it,
so we can later group hit rates / mean returns / drawdowns by gauge
version.

This module is the substrate for that attribution. v1 responsibilities:

  1. Compute a deterministic hash ("gauge fingerprint") of the current
     gauge state across all four surfaces:
        - allocation_engine.DEFAULT_CONFIG          (5 retuned knobs)
        - portfolio_construction.DEFAULT_..._CONFIG (4 retuned knobs)
        - config.json growth_mode.*                 (concentration_cap,
                                                     leverage_cap)
        - config.json ml_advisor.enabled
  2. Compare current fingerprint to a hardcoded BASELINE captured from
     commit 4223654c (the last commit before the 2026-05-18 retune
     session). Surface which knobs differ, by how much, and when each
     diverged.
  3. Append today's snapshot to `data/gauge_versions.jsonl` so a
     forward-looking outcome ledger can join on the version hash.
  4. Write `outputs/latest/retune_impact.json` and `.md` summarising the
     current vs baseline state.

What v1 does NOT do (intentional simplifications):
  - It does NOT (yet) join resolved outcomes to gauge versions. That
    requires the outcome tracker to also write the gauge_version with
    each resolved row, which is a separate change in the decision
    outcome path. v1 lays the substrate; v2 adds the join.
  - It does NOT modify decision_plan.json or any score. All writes are
    observe-only artifacts.

Hard guarantees:
  - observe_only=True hardcoded in every artifact.
  - No mutation of decision/score/allocation/recommendation state.
  - Degrades to status="insufficient_data" when essential inputs are missing.

Public API:
  compute_gauge_fingerprint(root) -> dict
  diff_against_baseline(current) -> list[dict]
  append_to_history(payload, root) -> bool
  build_retune_impact(root) -> dict
  run_retune_impact_tracker(root, write_files) -> dict
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)
from portfolio_automation.sector_mapping import normalize_sector

logger = logging.getLogger("stockbot.portfolio_automation.retune_impact_tracker")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "retune_impact_tracker"
_OBSERVE_ONLY = True

_DISCLAIMER = (
    "Observe-only gauge versioning ledger. Captures current gauge state and "
    "compares to a hardcoded pre-retune baseline. Does not modify portfolio, "
    "allocation, scoring, decision, or recommendation state."
)

# ---------------------------------------------------------------------------
# BASELINE — captured from commit 4223654c (last commit before 2026-05-18
# retune session). When the operator retunes a knob, the diff in
# retune_impact.md tells them exactly what changed vs this baseline.
# ---------------------------------------------------------------------------

_BASELINE_LABEL = "pre_retune_2026_05_18"
_BASELINE_COMMIT = "4223654c"
_BASELINE_GAUGE = {
    "allocation_engine": {
        "compounder_base_pct": 0.05,
        "momentum_base_pct": 0.03,
        "max_position_cap": 0.08,
        "sector_cap": 0.20,
        "low_confidence_multiplier": 0.50,
    },
    "portfolio_construction": {
        "baseline_position_pct": 0.02,
        "max_total_allocation": 0.10,
        "max_ticker_allocation": 0.02,
        "max_sector_allocation": 0.04,
    },
    "structural_caps": {
        "concentration_cap": 0.40,
        "leverage_cap": 0.15,
    },
    "feature_flags": {
        "ml_advisor_enabled": False,
    },
    "api_limits": {
        "fmp_daily_calls_budget": 230,
    },
}

# Knobs we track per surface — must match the BASELINE keys above.
_TRACKED_KNOBS = {
    "allocation_engine": [
        "compounder_base_pct",
        "momentum_base_pct",
        "max_position_cap",
        "sector_cap",
        "low_confidence_multiplier",
    ],
    "portfolio_construction": [
        "baseline_position_pct",
        "max_total_allocation",
        "max_ticker_allocation",
        "max_sector_allocation",
    ],
    "structural_caps": [
        "concentration_cap",
        "leverage_cap",
    ],
    "feature_flags": [
        "ml_advisor_enabled",
    ],
    "api_limits": [
        "fmp_daily_calls_budget",
    ],
}

# Where the history ledger lives. Append-only JSONL — one row per run that
# produces a new fingerprint (we don't append duplicates).
_HISTORY_REL = ("data", "gauge_versions.jsonl")

# Source for outcome attribution.
_SIGNAL_OUTCOMES_REL = ("outputs", "performance", "signal_outcomes.csv")

# Sentinel for signals that predate the gauge_versions ledger (we cannot
# attribute them retroactively without git-history walking).
_PRE_TRACKER_LABEL = "pre_tracker_unknown"

# Artifacts.
_OUTPUT_JSON_REL = "retune_impact.json"
_OUTPUT_MD_REL = "retune_impact.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("retune_impact: failed to load %s — %s", path, exc)
        return {}


def _fingerprint(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonicalised gauge dict, truncated to 16 chars."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def compute_gauge_fingerprint(root: str | Path = ".") -> dict[str, Any]:
    """
    Read the current gauge state from disk + imports, return a dict whose
    shape mirrors `_BASELINE_GAUGE`. Falls back to None values for any
    surface that can't be read.
    """
    root_path = Path(root).resolve()

    # allocation_engine + portfolio_construction live in code — import the
    # current DEFAULT_CONFIG dicts so the fingerprint reflects what would
    # run, not what's in some config file.
    try:
        from allocation_engine import DEFAULT_CONFIG as _AE_CFG
    except Exception as exc:
        logger.warning("retune_impact: could not import allocation_engine — %s", exc)
        _AE_CFG = {}

    try:
        from watchlist_scanner.portfolio_construction import (
            DEFAULT_PORTFOLIO_CONSTRUCTION_CONFIG as _PC_CFG,
        )
    except Exception as exc:
        logger.warning("retune_impact: could not import portfolio_construction — %s", exc)
        _PC_CFG = {}

    cfg = _load_json_safe(root_path / "config.json")
    growth = (cfg.get("growth_mode") or {}) if isinstance(cfg, dict) else {}
    api_limits = (cfg.get("api_limits") or {}) if isinstance(cfg, dict) else {}

    # ml_advisor lives in config/base.json (per the post-retune flip).
    base_cfg = _load_json_safe(root_path / "config" / "base.json")
    ml_cfg = (base_cfg.get("ml_advisor") or {}) if isinstance(base_cfg, dict) else {}

    snapshot: dict[str, dict[str, Any]] = {
        "allocation_engine": {
            k: _AE_CFG.get(k) for k in _TRACKED_KNOBS["allocation_engine"]
        },
        "portfolio_construction": {
            k: _PC_CFG.get(k) for k in _TRACKED_KNOBS["portfolio_construction"]
        },
        "structural_caps": {
            k: growth.get(k) for k in _TRACKED_KNOBS["structural_caps"]
        },
        "feature_flags": {
            "ml_advisor_enabled": bool(ml_cfg.get("enabled", False)),
        },
        "api_limits": {
            k: api_limits.get(k) for k in _TRACKED_KNOBS["api_limits"]
        },
    }
    return {
        "fingerprint": _fingerprint(snapshot),
        "snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# Diff against baseline
# ---------------------------------------------------------------------------


def diff_against_baseline(current: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return a list of changed knobs. Each row:
      {surface, knob, baseline_value, current_value, delta_pct, delta_abs}
    Unchanged knobs are omitted. Knobs missing from current snapshot
    (None) are reported as "unavailable" rather than diffed.
    """
    snapshot = current.get("snapshot") or {}
    changes: list[dict[str, Any]] = []

    for surface, knobs in _TRACKED_KNOBS.items():
        baseline_surface = _BASELINE_GAUGE.get(surface, {})
        current_surface = snapshot.get(surface, {}) or {}
        for knob in knobs:
            baseline_value = baseline_surface.get(knob)
            current_value = current_surface.get(knob)
            if current_value is None:
                changes.append({
                    "surface": surface,
                    "knob": knob,
                    "baseline_value": baseline_value,
                    "current_value": None,
                    "status": "unavailable",
                })
                continue
            if current_value == baseline_value:
                continue
            row: dict[str, Any] = {
                "surface": surface,
                "knob": knob,
                "baseline_value": baseline_value,
                "current_value": current_value,
                "status": "changed",
            }
            # Compute deltas when both values are numeric.
            try:
                bv = float(baseline_value)
                cv = float(current_value)
                row["delta_abs"] = round(cv - bv, 6)
                row["delta_pct"] = (
                    round((cv - bv) / bv, 4) if bv not in (0.0, 0) else None
                )
            except (TypeError, ValueError):
                # e.g. bool flags
                row["delta_abs"] = None
                row["delta_pct"] = None
            changes.append(row)
    return changes


# ---------------------------------------------------------------------------
# History ledger (append-only JSONL)
# ---------------------------------------------------------------------------


def append_to_history(
    payload: dict[str, Any],
    *,
    root: str | Path = ".",
) -> bool:
    """
    Append the current fingerprint to data/gauge_versions.jsonl when it
    differs from the most recent recorded fingerprint. Returns True when
    a new row was written; False when the current state matches the last
    recorded row (no-op).
    """
    root_path = Path(root).resolve()
    history_path = root_path.joinpath(*_HISTORY_REL)
    history_path.parent.mkdir(parents=True, exist_ok=True)

    current_fp = payload.get("fingerprint")
    last_fp: str | None = None
    if history_path.exists():
        try:
            with history_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw)
                        last_fp = row.get("fingerprint") or last_fp
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            logger.warning("retune_impact: history read failed — %s", exc)

    if current_fp == last_fp and last_fp is not None:
        return False

    row = {
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": current_fp,
        "snapshot": payload.get("snapshot"),
    }
    try:
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return True
    except Exception as exc:
        logger.warning("retune_impact: history append failed — %s", exc)
        return False


# ---------------------------------------------------------------------------
# Outcome attribution — join signal_outcomes.csv to gauge_versions.jsonl
# by timestamp range, group resolved outcomes per fingerprint.
# ---------------------------------------------------------------------------


def _parse_iso_to_utc_naive(value: Any) -> datetime | None:
    """
    Parse an ISO timestamp into a tz-naive UTC datetime so we can compare
    signal_time (which is currently emitted timezone-naive in local time)
    with gauge_version.first_seen_at (which is tz-aware UTC).
    Returns None for any unparseable input.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        # Convert to naive UTC.
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _load_gauge_history(root: Path) -> list[dict[str, Any]]:
    """Read gauge_versions.jsonl in chronological order. Parse timestamps."""
    path = root.joinpath(*_HISTORY_REL)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = _parse_iso_to_utc_naive(row.get("first_seen_at"))
            if ts is None:
                continue
            row["_first_seen_dt"] = ts
            rows.append(row)
    except Exception as exc:
        logger.debug("retune_impact: history read failed — %s", exc)
        return []
    rows.sort(key=lambda r: r["_first_seen_dt"])
    return rows


def _attribute_signal(signal_time_str: str, history: list[dict[str, Any]]) -> str:
    """Return the gauge fingerprint active at signal_time, or _PRE_TRACKER_LABEL."""
    sig_dt = _parse_iso_to_utc_naive(signal_time_str)
    if sig_dt is None or not history:
        return _PRE_TRACKER_LABEL
    active = _PRE_TRACKER_LABEL
    for row in history:
        if row["_first_seen_dt"] <= sig_dt:
            active = row.get("fingerprint") or _PRE_TRACKER_LABEL
        else:
            break
    return active


def _safe_float_csv(v: Any) -> float | None:
    if v in (None, "", "—"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int_csv(v: Any) -> int | None:
    """For 0/1 success/direction_correct flags stored as ints in CSV."""
    f = _safe_float_csv(v)
    return int(f) if f is not None else None


_FMP_PROFILE_CACHE_REL = ("data", "fmp_cache")
_UNKNOWN_SECTOR = "Unknown"


def _load_ticker_sector(root: Path, ticker: str) -> str:
    """Resolve a ticker's sector from the FMP profile cache. Returns "Unknown"
    if the cache is missing or malformed.

    Reads `data/fmp_cache/profile_stable_<TICKER>.json` `data[0].sector`, with
    one normalization: FMP files funds (`isEtf`/`isFund`) under their *issuer*
    sector ("Financial Services / Asset Management"), which is useless for
    sector attribution — it would fold an energy ETF, a tech ETF, and crypto
    into one bogus "Financial Services" bucket. Normalization (sector-exposure
    ETFs → exposure, other funds → "ETF/Index", non-funds keep raw sector) is
    delegated to `sector_mapping.normalize_sector`.
    """
    safe_ticker = (ticker or "").strip().upper()
    if not safe_ticker:
        return _UNKNOWN_SECTOR
    cache_path = root.joinpath(*_FMP_PROFILE_CACHE_REL) / f"profile_stable_{safe_ticker}.json"
    if not cache_path.exists():
        return _UNKNOWN_SECTOR
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return _UNKNOWN_SECTOR
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data:
        first = data[0]
    elif isinstance(data, dict):
        first = data
    else:
        first = payload if isinstance(payload, dict) else {}
    first = first or {}
    return normalize_sector(
        safe_ticker,
        first.get("sector"),
        is_etf=bool(first.get("isEtf")),
        is_fund=bool(first.get("isFund")),
        unknown=_UNKNOWN_SECTOR,
    )


def compute_outcome_attribution(
    *,
    root: str | Path = ".",
) -> dict[str, Any]:
    """
    Join signal_outcomes.csv → gauge_versions.jsonl by timestamp range.

    Groups rows by gauge_version fingerprint and computes per-group:
      - count           : total signals
      - resolved_Nd     : rows with outcome_return_Nd populated (1d/3d/7d)
      - hit_rate_Nd     : direction_correct_Nd success ratio over resolved
      - mean_return_Nd  : mean of outcome_return_Nd over resolved

    Returns dict shape:
      {
        "available": True,
        "method": "timestamp_range_join",
        "total_signals": int,
        "by_fingerprint": {fp: {count, resolved_1d, hit_rate_1d, ...}}
      }
    """
    import csv

    root_path = Path(root).resolve()
    csv_path = root_path.joinpath(*_SIGNAL_OUTCOMES_REL)
    if not csv_path.exists():
        return {"available": False, "reason": "no_signal_outcomes_csv"}

    history = _load_gauge_history(root_path)

    by_fp: dict[str, dict[str, Any]] = {}
    total_signals = 0
    sector_cache: dict[str, str] = {}

    def _sector_for(ticker: str) -> str:
        ticker = (ticker or "").strip().upper()
        if ticker not in sector_cache:
            sector_cache[ticker] = _load_ticker_sector(root_path, ticker)
        return sector_cache[ticker]

    try:
        with csv_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_signals += 1
                fp = _attribute_signal(row.get("signal_time", ""), history)
                bucket = by_fp.setdefault(fp, {
                    "count": 0,
                    "resolved_1d": 0, "resolved_3d": 0, "resolved_7d": 0,
                    "hits_1d": 0,     "hits_3d": 0,     "hits_7d": 0,
                    "sum_return_1d": 0.0, "sum_return_3d": 0.0, "sum_return_7d": 0.0,
                    "first_signal_time": None,
                    "last_signal_time": None,
                    "_tickers": set(),
                    "_by_sector": {},
                })
                bucket["count"] += 1
                st = row.get("signal_time") or ""
                if st:
                    if bucket["first_signal_time"] is None or st < bucket["first_signal_time"]:
                        bucket["first_signal_time"] = st
                    if bucket["last_signal_time"] is None or st > bucket["last_signal_time"]:
                        bucket["last_signal_time"] = st

                ticker = (row.get("ticker") or "").strip().upper()
                if ticker:
                    bucket["_tickers"].add(ticker)
                sector = _sector_for(ticker) if ticker else _UNKNOWN_SECTOR
                sec_bucket = bucket["_by_sector"].setdefault(sector, {
                    "count": 0,
                    "resolved_1d": 0,
                    "hits_1d": 0,
                    "sum_return_1d": 0.0,
                    "tickers": set(),
                })
                sec_bucket["count"] += 1
                if ticker:
                    sec_bucket["tickers"].add(ticker)

                for w in ("1d", "3d", "7d"):
                    ret = _safe_float_csv(row.get(f"outcome_return_{w}"))
                    if ret is None:
                        continue
                    bucket[f"resolved_{w}"] += 1
                    bucket[f"sum_return_{w}"] += ret
                    correct = _safe_int_csv(row.get(f"direction_correct_{w}"))
                    if correct:
                        bucket[f"hits_{w}"] += 1
                    if w == "1d":
                        sec_bucket["resolved_1d"] += 1
                        sec_bucket["sum_return_1d"] += ret
                        if correct:
                            sec_bucket["hits_1d"] += 1
    except Exception as exc:
        logger.warning("retune_impact: outcome attribution failed — %s", exc)
        return {"available": False, "reason": f"csv_parse_error: {exc}"}

    # Finalize: convert running sums into hit_rate + mean_return per window.
    for fp, b in by_fp.items():
        for w in ("1d", "3d", "7d"):
            resolved = b[f"resolved_{w}"]
            hits = b.pop(f"hits_{w}")
            sum_ret = b.pop(f"sum_return_{w}")
            b[f"hit_rate_{w}"] = round(hits / resolved, 4) if resolved else None
            b[f"mean_return_{w}"] = round(sum_ret / resolved, 6) if resolved else None

        # Finalize sector composition: convert per-sector running sums to
        # hit_rate / mean_return / ticker_count, and compute share-of-pool.
        sector_rows: dict[str, dict[str, Any]] = {}
        total_count = max(b["count"], 1)
        for sector, sec in b.pop("_by_sector").items():
            resolved = sec["resolved_1d"]
            sector_rows[sector] = {
                "count": sec["count"],
                "pct_of_signals": round(sec["count"] / total_count, 4),
                "distinct_tickers": len(sec["tickers"]),
                "resolved_1d": resolved,
                "hit_rate_1d": round(sec["hits_1d"] / resolved, 4) if resolved else None,
                "mean_return_1d": round(sec["sum_return_1d"] / resolved, 6) if resolved else None,
            }
        b["sector_composition"] = sector_rows
        b["distinct_tickers"] = len(b.pop("_tickers"))

        # Attach known snapshot for this fingerprint (if any).
        snapshot = next(
            (r.get("snapshot") for r in history if r.get("fingerprint") == fp),
            None,
        )
        if snapshot is not None:
            b["snapshot_known"] = True
        else:
            b["snapshot_known"] = fp == _PRE_TRACKER_LABEL

    return {
        "available": True,
        "method": "timestamp_range_join",
        "total_signals": total_signals,
        "attributed_signals": sum(
            v["count"] for k, v in by_fp.items() if k != _PRE_TRACKER_LABEL
        ),
        "unattributed_signals": by_fp.get(_PRE_TRACKER_LABEL, {}).get("count", 0),
        "fingerprint_count": len(by_fp),
        "by_fingerprint": by_fp,
        "pre_tracker_label": _PRE_TRACKER_LABEL,
        "sector_source": "fmp_profile_cache",
    }


# ---------------------------------------------------------------------------
# Build artifact
# ---------------------------------------------------------------------------


def build_retune_impact(
    *,
    root: str | Path = ".",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Compose the full artifact payload. No file writes."""
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    current = compute_gauge_fingerprint(root=root)
    changes = diff_against_baseline(current)

    # Read history for "how many distinct gauge versions have we seen?"
    history_path = Path(root).resolve().joinpath(*_HISTORY_REL)
    distinct_versions = 0
    history_size = 0
    if history_path.exists():
        try:
            seen: set[str] = set()
            for raw in history_path.read_text(encoding="utf-8", errors="replace").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                history_size += 1
                fp = row.get("fingerprint")
                if fp:
                    seen.add(fp)
            distinct_versions = len(seen)
        except Exception as exc:
            logger.debug("retune_impact: history read for stats failed — %s", exc)

    # Outcome attribution v2 — joins signal_outcomes.csv to this ledger.
    outcome_attribution = compute_outcome_attribution(root=root)

    return {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "baseline_label": _BASELINE_LABEL,
        "baseline_commit": _BASELINE_COMMIT,
        "current_fingerprint": current.get("fingerprint"),
        "current_snapshot": current.get("snapshot"),
        "baseline_snapshot": _BASELINE_GAUGE,
        "changes_vs_baseline": changes,
        "changes_count": sum(1 for c in changes if c.get("status") == "changed"),
        "history_size": history_size,
        "distinct_versions_seen": distinct_versions,
        "outcome_attribution": outcome_attribution,
        "disclaimer": _DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------


def _fmt_value(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        # Pct-like values look right at 2 decimal places; budgets are ints.
        if abs(v) < 10:
            return f"{v:.4f}".rstrip("0").rstrip(".")
        return f"{v:g}"
    return str(v)


def render_retune_impact_md(payload: dict[str, Any]) -> str:
    """Render the artifact as a compact Markdown report."""
    lines: list[str] = []
    a = lines.append

    a(f"# Retune Impact — {payload.get('generated_at', '')[:10]}")
    a("")
    a(f"**Generated:** {payload.get('generated_at', '')}  ")
    a(f"**Current fingerprint:** `{payload.get('current_fingerprint')}`  ")
    a(f"**Baseline:** `{payload.get('baseline_label')}` (commit `{payload.get('baseline_commit')}`)  ")
    a(
        f"**Distinct gauge versions recorded:** {payload.get('distinct_versions_seen', 0)} "
        f"(history rows: {payload.get('history_size', 0)})"
    )
    a("")
    a(f"> {payload.get('disclaimer', _DISCLAIMER)}")
    a("")

    changes = payload.get("changes_vs_baseline") or []
    real_changes = [c for c in changes if c.get("status") == "changed"]
    unavailable = [c for c in changes if c.get("status") == "unavailable"]

    if real_changes:
        a(f"## Changes vs Baseline — {len(real_changes)} knob(s) retuned")
        a("")
        a("| Surface | Knob | Baseline | Current | Δ |")
        a("|---|---|---|---|---|")
        for c in real_changes:
            delta_abs = c.get("delta_abs")
            delta_pct = c.get("delta_pct")
            delta_str = ""
            if delta_abs is not None and delta_pct is not None:
                delta_str = f"{delta_abs:+g} ({delta_pct:+.1%})"
            elif delta_abs is not None:
                delta_str = f"{delta_abs:+g}"
            a(
                f"| `{c.get('surface')}` | `{c.get('knob')}` | "
                f"{_fmt_value(c.get('baseline_value'))} | "
                f"{_fmt_value(c.get('current_value'))} | "
                f"{delta_str} |"
            )
        a("")
    else:
        a("## Changes vs Baseline")
        a("")
        a("_No knobs have moved from baseline._")
        a("")

    if unavailable:
        a("## Unavailable Knobs")
        a("")
        for c in unavailable:
            a(f"- `{c.get('surface')}.{c.get('knob')}` — could not read")
        a("")

    attribution = payload.get("outcome_attribution") or {}
    if attribution.get("available"):
        a("## Outcome Attribution (signal_outcomes joined to gauge_versions)")
        a("")
        a(
            f"- Total signals: **{attribution.get('total_signals', 0)}** — "
            f"attributed: {attribution.get('attributed_signals', 0)}, "
            f"unattributed (pre-tracker): {attribution.get('unattributed_signals', 0)}"
        )
        a(f"- Distinct fingerprints in outcomes: **{attribution.get('fingerprint_count', 0)}**")
        a("")
        by_fp = attribution.get("by_fingerprint") or {}
        if by_fp:
            a("| Fingerprint | Count | Tickers | Resolved 1d | Hit rate 1d | Mean return 1d |")
            a("|---|---|---|---|---|---|")
            for fp, b in sorted(
                by_fp.items(),
                key=lambda kv: (kv[0] == _PRE_TRACKER_LABEL, -kv[1]["count"]),
            ):
                hr = b.get("hit_rate_1d")
                mr = b.get("mean_return_1d")
                # hit_rate is a fraction (0.50 = 50%); mean_return is already
                # in percent units per performance_feedback.evaluate_pending_*
                # (which multiplies the raw fraction by 100 before storing).
                hr_str = f"{hr * 100:.1f}%" if hr is not None else "—"
                mr_str = f"{mr:+.2f}%" if mr is not None else "—"
                a(
                    f"| `{fp[:16]}` | {b.get('count', 0)} | "
                    f"{b.get('distinct_tickers', 0)} | "
                    f"{b.get('resolved_1d', 0)} | {hr_str} | {mr_str} |"
                )
            a("")

            # Sector composition breakdown — discloses universe shape so
            # readers can judge regime correlation, not just raw lift.
            sector_source = attribution.get("sector_source", "fmp_profile_cache")
            a(f"### Universe sector composition (per-fingerprint, source: `{sector_source}`)")
            a("")
            a(
                "> Sector resolved dynamically from FMP profile cache. ETFs are "
                "reported under FMP's classification (often Financial Services / "
                "Asset Management) — interpret accordingly."
            )
            a("")
            for fp, b in sorted(
                by_fp.items(),
                key=lambda kv: (kv[0] == _PRE_TRACKER_LABEL, -kv[1]["count"]),
            ):
                comp = b.get("sector_composition") or {}
                if not comp:
                    continue
                a(f"**`{fp[:16]}`** — {b.get('distinct_tickers', 0)} distinct tickers, "
                  f"{b.get('count', 0)} signals:")
                a("")
                a("| Sector | Signals | Share | Tickers | Resolved 1d | Hit rate 1d | Mean return 1d |")
                a("|---|---|---|---|---|---|---|")
                for sector, sec in sorted(comp.items(), key=lambda kv: -kv[1]["count"]):
                    hr = sec.get("hit_rate_1d")
                    mr = sec.get("mean_return_1d")
                    hr_str = f"{hr * 100:.1f}%" if hr is not None else "—"
                    mr_str = f"{mr:+.2f}%" if mr is not None else "—"
                    pct = sec.get("pct_of_signals") or 0
                    a(
                        f"| {sector} | {sec.get('count', 0)} | {pct * 100:.1f}% | "
                        f"{sec.get('distinct_tickers', 0)} | {sec.get('resolved_1d', 0)} | "
                        f"{hr_str} | {mr_str} |"
                    )
                a("")
    else:
        a("## Outcome Attribution")
        a("")
        reason = attribution.get("reason", "unknown")
        a(f"_Not available: {reason}._")
        a("")

    a("## Current Gauge Snapshot")
    a("")
    snapshot = payload.get("current_snapshot") or {}
    for surface, knobs in snapshot.items():
        a(f"**{surface}:**")
        for k, v in (knobs or {}).items():
            a(f"  - `{k}`: {_fmt_value(v)}")
        a("")

    a("---")
    a("_Advisory only — substrate for future outcome attribution._")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def run_retune_impact_tracker(
    *,
    root: str | Path = ".",
    write_files: bool = True,
) -> dict[str, Any]:
    """Read inputs, build payload, append history, optionally write artifacts."""
    root_path = Path(root).resolve()
    try:
        payload = build_retune_impact(root=root_path)
        new_row = append_to_history(
            {"fingerprint": payload["current_fingerprint"], "snapshot": payload["current_snapshot"]},
            root=root_path,
        )
        payload["history_row_appended"] = new_row

        artifacts: dict[str, str] = {}
        if write_files:
            md = render_retune_impact_md(payload)
            json_path = safe_write_json(
                OutputNamespace.LATEST,
                _OUTPUT_JSON_REL,
                payload,
                base_dir=root_path / "outputs",
            )
            md_path = safe_write_text(
                OutputNamespace.LATEST,
                _OUTPUT_MD_REL,
                md,
                base_dir=root_path / "outputs",
            )
            artifacts = {
                "retune_impact_json": str(json_path),
                "retune_impact_md": str(md_path),
            }

        return {
            "status": "ok",
            "fingerprint": payload["current_fingerprint"],
            "changes_count": payload["changes_count"],
            "history_row_appended": new_row,
            "artifacts": artifacts,
        }
    except Exception as exc:
        logger.error("retune_impact_tracker failed: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}


if __name__ == "__main__":
    import sys
    r = run_retune_impact_tracker(root=Path(__file__).resolve().parents[1])
    print(
        f"retune_impact: status={r.get('status')}"
        f" fingerprint={r.get('fingerprint')}"
        f" changes={r.get('changes_count')}"
        f" appended={r.get('history_row_appended')}"
    )
    sys.exit(0)
