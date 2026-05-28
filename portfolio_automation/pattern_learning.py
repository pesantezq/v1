"""
Pattern Learning — joins archived top100_daily snapshots to signal outcomes
and computes per-tag efficacy at weekly / monthly / yearly cadence.

For each `rationale_tag` produced by universe_sanitation._build_rationale,
the engine reports:
  - n_samples            : tag-row observations in the lookback window
  - hit_rate_{1d,3d,7d}  : fraction of forward outcomes that were positive
  - mean_return_{1d,3d,7d}
  - wilson_ci_{1d,3d,7d} : 95% Wilson confidence interval on hit_rate_1d
  - vs_baseline_pp       : tag's hit_rate_1d minus universe-wide baseline (in pp)
  - significance_flag    : "strong_winner" / "winner" / "neutral" / "loser" /
                            "strong_loser" / "insufficient_sample"

Yearly view also partitions by (gauge_fingerprint, volatility_regime) so
the operator can see whether a tag works only in specific eras / regimes.

Hard guarantees:
  - observe_only=True hardcoded
  - Pure read of archived artifacts + signal_outcomes.csv
  - No portfolio / scoring / decision-state mutation
  - Degrades safely on missing history (returns empty efficacy, no error)

Public API:
  build_pattern_efficacy(root, lookback_days, partition=False) -> dict
  run_pattern_learning(root, cadence) -> dict
"""
from __future__ import annotations

import csv
import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.pattern_learning")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "pattern_learning"
_OBSERVE_ONLY = True
_MIN_N_SIGNIFICANT = 30      # below this, tag is "insufficient_sample"
_STRONG_DELTA_PP = 15.0      # |Δ vs baseline| ≥ 15pp → strong winner/loser
_WINNER_DELTA_PP = 5.0       # |Δ vs baseline| ≥ 5pp → winner/loser

_DISCLAIMER = (
    "Observe-only pattern-learning telemetry. Joins archived top-100 "
    "snapshots to signal_outcomes; computes per-tag efficacy with Wilson "
    "CIs. Does not modify portfolio, allocation, scoring, or decision state."
)


# ---------------------------------------------------------------------------
# Stats helpers — pure
# ---------------------------------------------------------------------------


def wilson_ci_95(successes: int, n: int) -> tuple[float, float]:
    """Two-sided Wilson 95% CI on a binomial proportion. Returns (lo, hi)."""
    if n <= 0:
        return (0.0, 0.0)
    z = 1.96
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _safe_float(v: Any) -> float | None:
    if v in (None, "", "—"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------


def _snapshot_dates(root: Path, lookback_days: int) -> list[str]:
    """Return ISO date strings (YYYY-MM-DD) for archived snapshots in the
    lookback window. Most recent first."""
    today = datetime.now(timezone.utc).date()
    dates: list[str] = []
    for i in range(lookback_days + 1):
        d = today - timedelta(days=i)
        dates.append(d.isoformat())
    return dates


def _load_snapshot(root: Path, date_iso: str) -> list[dict[str, Any]]:
    """Load top100_daily.json for a date. Empty list on any miss."""
    p = root / "outputs" / "history" / date_iso / "top100_daily.json"
    if not p.exists():
        # Today's data may still be in outputs/latest/ (not yet archived)
        today = datetime.now(timezone.utc).date().isoformat()
        if date_iso == today:
            p = root / "outputs" / "latest" / "top100_daily.json"
            if not p.exists():
                return []
        else:
            return []
    try:
        d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        return list(d.get("candidates") or [])
    except Exception as exc:
        logger.debug("pattern_learning: snapshot read failed %s: %s", p, exc)
        return []


# ---------------------------------------------------------------------------
# Outcome loading + join
# ---------------------------------------------------------------------------


def _load_outcomes_window(
    root: Path,
    start_iso: str,
) -> dict[str, list[dict[str, Any]]]:
    """Read signal_outcomes.csv, return {ticker: [row_dict, ...]} for rows
    whose signal_time >= start_iso. Pre-parses outcome fields."""
    csv_path = root / "outputs" / "performance" / "signal_outcomes.csv"
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not csv_path.exists():
        return {}
    try:
        with csv_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            r = csv.DictReader(f)
            for row in r:
                t = (row.get("ticker") or "").upper()
                st = row.get("signal_time") or ""
                if not t or not st or st < start_iso:
                    continue
                out[t].append({
                    "signal_time": st,
                    "regime_label": row.get("regime_label") or "unknown",
                    "outcome_return_1d": _safe_float(row.get("outcome_return_1d")),
                    "outcome_return_3d": _safe_float(row.get("outcome_return_3d")),
                    "outcome_return_7d": _safe_float(row.get("outcome_return_7d")),
                    "direction_correct_1d": _safe_float(row.get("direction_correct_1d")),
                    "direction_correct_3d": _safe_float(row.get("direction_correct_3d")),
                    "direction_correct_7d": _safe_float(row.get("direction_correct_7d")),
                })
    except Exception as exc:
        logger.debug("pattern_learning: signal_outcomes read failed: %s", exc)
        return {}
    return dict(out)


def _gauge_fingerprint_for_ts(
    iso_ts: str,
    history: list[dict[str, Any]],
) -> str:
    """Resolve which gauge fingerprint was active at the given timestamp."""
    if not history:
        return "pre_tracker_unknown"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return "pre_tracker_unknown"
    active = "pre_tracker_unknown"
    for row in history:
        fs = row.get("_first_seen_dt")
        if fs is not None and fs <= dt:
            active = row.get("fingerprint") or "pre_tracker_unknown"
    return active


def _load_gauge_history(root: Path) -> list[dict[str, Any]]:
    """Load gauge_versions.jsonl with parsed first_seen_dt; sorted ascending."""
    p = root / "data" / "gauge_versions.jsonl"
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            fs = d.get("first_seen_at")
            try:
                dt = datetime.fromisoformat(fs.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                d["_first_seen_dt"] = dt
                rows.append(d)
            except Exception:
                continue
    except Exception as exc:
        logger.debug("pattern_learning: gauge_versions read failed: %s", exc)
        return []
    rows.sort(key=lambda r: r["_first_seen_dt"])
    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _match_outcome(
    snapshot_date_iso: str,
    ticker: str,
    outcomes_by_ticker: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Find the signal_outcomes row whose signal_time is closest to (and not
    before) the snapshot date for the given ticker. Returns the row dict or
    None if no match."""
    rows = outcomes_by_ticker.get(ticker.upper()) or []
    if not rows:
        return None
    snap_prefix = snapshot_date_iso[:10]
    same_day = [r for r in rows if r["signal_time"][:10] == snap_prefix]
    if same_day:
        # Use the earliest signal for that day (most relevant to the morning snapshot)
        return min(same_day, key=lambda r: r["signal_time"])
    # Otherwise the first row >= snapshot date
    future = [r for r in rows if r["signal_time"][:10] >= snap_prefix]
    if not future:
        return None
    return min(future, key=lambda r: r["signal_time"])


def _accumulate(
    bucket: dict[str, Any],
    outcome: dict[str, Any],
) -> None:
    """Add one outcome's contribution to a per-tag stats bucket."""
    bucket["n_samples"] += 1
    for w in ("1d", "3d", "7d"):
        ret = outcome.get(f"outcome_return_{w}")
        if ret is None:
            continue
        bucket[f"resolved_{w}"] += 1
        bucket[f"sum_return_{w}"] += float(ret)
        correct = outcome.get(f"direction_correct_{w}")
        if correct in (1, 1.0, "1", True):
            bucket[f"hits_{w}"] += 1


def _new_bucket() -> dict[str, Any]:
    b = {"n_samples": 0}
    for w in ("1d", "3d", "7d"):
        b[f"resolved_{w}"] = 0
        b[f"hits_{w}"] = 0
        b[f"sum_return_{w}"] = 0.0
    return b


def _finalize(bucket: dict[str, Any]) -> dict[str, Any]:
    """Turn running sums into hit_rate / mean_return / CIs."""
    out = {"n_samples": bucket["n_samples"]}
    for w in ("1d", "3d", "7d"):
        resolved = bucket[f"resolved_{w}"]
        hits = bucket[f"hits_{w}"]
        sum_ret = bucket[f"sum_return_{w}"]
        out[f"resolved_{w}"] = resolved
        if resolved > 0:
            out[f"hit_rate_{w}"] = round(hits / resolved, 4)
            out[f"mean_return_{w}"] = round(sum_ret / resolved, 6)
        else:
            out[f"hit_rate_{w}"] = None
            out[f"mean_return_{w}"] = None
    # Wilson CI on 1d hit-rate
    r1 = bucket["resolved_1d"]
    h1 = bucket["hits_1d"]
    if r1 > 0:
        lo, hi = wilson_ci_95(h1, r1)
        out["wilson_ci_1d"] = [round(lo, 4), round(hi, 4)]
    else:
        out["wilson_ci_1d"] = None
    return out


def _classify(
    tag_stats: dict[str, Any],
    baseline_hit_rate_1d: float | None,
) -> str:
    """Map per-tag stats to a categorical significance flag."""
    n = tag_stats.get("n_samples", 0)
    if n < _MIN_N_SIGNIFICANT:
        return "insufficient_sample"
    hr = tag_stats.get("hit_rate_1d")
    if hr is None or baseline_hit_rate_1d is None:
        return "neutral"
    delta_pp = (hr - baseline_hit_rate_1d) * 100
    if delta_pp >= _STRONG_DELTA_PP:
        return "strong_winner"
    if delta_pp >= _WINNER_DELTA_PP:
        return "winner"
    if delta_pp <= -_STRONG_DELTA_PP:
        return "strong_loser"
    if delta_pp <= -_WINNER_DELTA_PP:
        return "loser"
    return "neutral"


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


def build_pattern_efficacy(
    *,
    root: str | Path = ".",
    lookback_days: int = 7,
    partition: bool = False,
) -> dict[str, Any]:
    """Join archived top100_daily snapshots to forward outcomes and compute
    per-tag efficacy. Returns the full payload dict.

    Args:
        root:          repo root
        lookback_days: how many days of archived snapshots to consume
        partition:     when True, also emit per-(gauge_fingerprint,
                       volatility_regime) per-tag stats. Used by the yearly
                       view; off for weekly/monthly to keep sample sizes up.
    """
    root_path = Path(root).resolve()
    ts = datetime.now(timezone.utc).isoformat()
    start_iso = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    dates = _snapshot_dates(root_path, lookback_days)
    outcomes = _load_outcomes_window(root_path, start_iso)
    gauge_hist = _load_gauge_history(root_path) if partition else []

    by_tag: dict[str, dict[str, Any]] = defaultdict(_new_bucket)
    universe_bucket = _new_bucket()
    by_tag_partitioned: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(_new_bucket)
    snapshots_seen = 0
    rows_seen = 0
    matched = 0

    for date_iso in dates:
        rows = _load_snapshot(root_path, date_iso)
        if not rows:
            continue
        snapshots_seen += 1
        for row in rows:
            rows_seen += 1
            sym = (row.get("symbol") or "").upper()
            tags = row.get("rationale_tags") or []
            if not sym or not tags:
                continue
            outcome = _match_outcome(date_iso, sym, outcomes)
            if outcome is None:
                continue
            matched += 1
            _accumulate(universe_bucket, outcome)
            for tag in tags:
                _accumulate(by_tag[tag], outcome)
            if partition:
                fp = _gauge_fingerprint_for_ts(outcome["signal_time"], gauge_hist)
                regime = outcome.get("regime_label") or "unknown"
                for tag in tags:
                    _accumulate(by_tag_partitioned[(tag, fp, regime)], outcome)

    universe_stats = _finalize(universe_bucket)
    baseline_hr = universe_stats.get("hit_rate_1d")

    tags_out: dict[str, dict[str, Any]] = {}
    for tag, bucket in by_tag.items():
        stats = _finalize(bucket)
        stats["significance"] = _classify(stats, baseline_hr)
        if baseline_hr is not None and stats.get("hit_rate_1d") is not None:
            stats["vs_baseline_pp"] = round((stats["hit_rate_1d"] - baseline_hr) * 100, 2)
        else:
            stats["vs_baseline_pp"] = None
        tags_out[tag] = stats

    partitioned_out: list[dict[str, Any]] = []
    if partition:
        for (tag, fp, regime), bucket in by_tag_partitioned.items():
            stats = _finalize(bucket)
            if stats["n_samples"] == 0:
                continue
            stats["tag"] = tag
            stats["gauge_fingerprint"] = fp
            stats["volatility_regime"] = regime
            stats["significance"] = _classify(stats, baseline_hr)
            if baseline_hr is not None and stats.get("hit_rate_1d") is not None:
                stats["vs_baseline_pp"] = round((stats["hit_rate_1d"] - baseline_hr) * 100, 2)
            else:
                stats["vs_baseline_pp"] = None
            partitioned_out.append(stats)
        partitioned_out.sort(
            key=lambda r: (
                -(r.get("n_samples") or 0),
                -(r.get("vs_baseline_pp") or 0.0),
                r.get("tag", ""),
            )
        )

    return {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "lookback_days": lookback_days,
        "snapshots_consumed": snapshots_seen,
        "rows_consumed": rows_seen,
        "rows_matched_to_outcomes": matched,
        "match_rate": round(matched / rows_seen, 4) if rows_seen else None,
        "universe_baseline": universe_stats,
        "by_tag": tags_out,
        "partitioned_by_fingerprint_regime": partitioned_out if partition else None,
        "thresholds": {
            "min_n_significant": _MIN_N_SIGNIFICANT,
            "strong_delta_pp": _STRONG_DELTA_PP,
            "winner_delta_pp": _WINNER_DELTA_PP,
        },
        "disclaimer": _DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_pattern_efficacy_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    a(f"# Pattern Efficacy — {payload.get('generated_at', '')[:19]}")
    a("")
    a(
        f"**Lookback:** {payload.get('lookback_days', '?')} day(s) · "
        f"**Snapshots consumed:** {payload.get('snapshots_consumed', 0)} · "
        f"**Match rate:** {(payload.get('match_rate') or 0) * 100:.1f}%"
    )
    a("")
    a(f"> {payload.get('disclaimer', _DISCLAIMER)}")
    a("")

    ub = payload.get("universe_baseline") or {}
    a("## Universe baseline")
    a("")
    a(f"- n_samples: {ub.get('n_samples', 0)}")
    hr = ub.get("hit_rate_1d")
    a(f"- hit_rate_1d: {hr * 100:.1f}%" if hr is not None else "- hit_rate_1d: —")
    mr = ub.get("mean_return_1d")
    a(f"- mean_return_1d: {mr:+.2f}%" if mr is not None else "- mean_return_1d: —")
    a("")

    by_tag = payload.get("by_tag") or {}
    if by_tag:
        winners = [(t, s) for t, s in by_tag.items() if s.get("significance") in ("strong_winner", "winner")]
        losers = [(t, s) for t, s in by_tag.items() if s.get("significance") in ("strong_loser", "loser")]
        winners.sort(key=lambda kv: -(kv[1].get("vs_baseline_pp") or 0))
        losers.sort(key=lambda kv: (kv[1].get("vs_baseline_pp") or 0))

        a(f"## Winners ({len(winners)})")
        a("")
        if winners:
            a("| Tag | n | Hit-rate 1d | Δ vs baseline | Wilson 95% CI | Significance |")
            a("|---|---|---|---|---|---|")
            for tag, s in winners[:15]:
                hr = s.get("hit_rate_1d")
                hr_str = f"{hr * 100:.1f}%" if hr is not None else "—"
                delta = s.get("vs_baseline_pp")
                delta_str = f"{delta:+.1f}pp" if delta is not None else "—"
                ci = s.get("wilson_ci_1d") or [None, None]
                ci_str = f"[{ci[0]*100:.0f}, {ci[1]*100:.0f}]%" if ci[0] is not None else "—"
                a(f"| `{tag}` | {s.get('n_samples', 0)} | {hr_str} | {delta_str} | {ci_str} | {s.get('significance')} |")
            a("")
        else:
            a("_No winning tags in this window._")
            a("")

        a(f"## Losers ({len(losers)})")
        a("")
        if losers:
            a("| Tag | n | Hit-rate 1d | Δ vs baseline | Wilson 95% CI | Significance |")
            a("|---|---|---|---|---|---|")
            for tag, s in losers[:15]:
                hr = s.get("hit_rate_1d")
                hr_str = f"{hr * 100:.1f}%" if hr is not None else "—"
                delta = s.get("vs_baseline_pp")
                delta_str = f"{delta:+.1f}pp" if delta is not None else "—"
                ci = s.get("wilson_ci_1d") or [None, None]
                ci_str = f"[{ci[0]*100:.0f}, {ci[1]*100:.0f}]%" if ci[0] is not None else "—"
                a(f"| `{tag}` | {s.get('n_samples', 0)} | {hr_str} | {delta_str} | {ci_str} | {s.get('significance')} |")
            a("")
        else:
            a("_No losing tags in this window._")
            a("")

    part = payload.get("partitioned_by_fingerprint_regime")
    if part:
        a(f"## Partitioned (gauge × regime, top 20)")
        a("")
        a("| Tag | Gauge fp | Regime | n | Hit-rate 1d | Δ vs baseline |")
        a("|---|---|---|---|---|---|")
        for r in part[:20]:
            hr = r.get("hit_rate_1d")
            hr_str = f"{hr * 100:.1f}%" if hr is not None else "—"
            delta = r.get("vs_baseline_pp")
            delta_str = f"{delta:+.1f}pp" if delta is not None else "—"
            fp = (r.get("gauge_fingerprint") or "?")[:12]
            a(f"| `{r.get('tag')}` | `{fp}` | {r.get('volatility_regime')} | {r.get('n_samples', 0)} | {hr_str} | {delta_str} |")
        a("")

    a("---")
    a("_Observe-only pattern-learning telemetry._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


_CADENCE_TO_LOOKBACK = {"weekly": 7, "monthly": 30, "yearly": 365}


def run_pattern_learning(
    *,
    root: str | Path = ".",
    cadence: str = "weekly",
    write_files: bool = True,
) -> dict[str, Any]:
    """Top-level orchestrator. Never raises."""
    root_path = Path(root).resolve()
    cadence = (cadence or "weekly").lower().strip()
    if cadence not in _CADENCE_TO_LOOKBACK:
        return {"status": "error", "error": f"unknown_cadence:{cadence}"}
    try:
        lookback = _CADENCE_TO_LOOKBACK[cadence]
        partition = (cadence == "yearly")
        payload = build_pattern_efficacy(
            root=root_path, lookback_days=lookback, partition=partition
        )
        artifacts: dict[str, str] = {}
        if write_files:
            json_path = safe_write_json(
                OutputNamespace.LATEST,
                f"pattern_efficacy_{cadence}.json",
                payload,
                base_dir=root_path / "outputs",
            )
            md_path = safe_write_text(
                OutputNamespace.LATEST,
                f"pattern_efficacy_{cadence}.md",
                render_pattern_efficacy_md(payload),
                base_dir=root_path / "outputs",
            )
            artifacts = {
                f"pattern_efficacy_{cadence}_json": str(json_path),
                f"pattern_efficacy_{cadence}_md": str(md_path),
            }
        return {
            "status": "ok",
            "cadence": cadence,
            "snapshots_consumed": payload.get("snapshots_consumed", 0),
            "rows_matched": payload.get("rows_matched_to_outcomes", 0),
            "tag_count": len(payload.get("by_tag") or {}),
            "artifacts": artifacts,
        }
    except Exception as exc:
        logger.error("pattern_learning: %s run failed: %s", cadence, exc, exc_info=True)
        return {"status": "error", "cadence": cadence, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import sys
    cadence = (sys.argv[1] if len(sys.argv) > 1 else "weekly").lower()
    r = run_pattern_learning(root=Path(__file__).resolve().parents[1], cadence=cadence)
    print(
        f"pattern_learning: {cadence} → status={r.get('status')} "
        f"snapshots={r.get('snapshots_consumed', 0)} matched={r.get('rows_matched', 0)} "
        f"tags={r.get('tag_count', 0)}"
    )
    sys.exit(0 if r.get("status") == "ok" else 1)
