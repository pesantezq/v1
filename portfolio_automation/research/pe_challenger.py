"""RESEARCH-ONLY champion/challenger for the inert PE component.

Question: *what happens if the intended PE component is restored?* Nothing here
runs in production, writes a production artifact, or changes scoring.

  * **Champion**  = current production scanner, exactly as deployed. No PE source.
  * **Challenger** = identical configuration and identical frozen inputs, with the
    research PE field populated so the PE>50 guard becomes evaluable and the
    15-point PE attractiveness factor becomes evaluable.

No other change: no new thresholds, no weight retuning, no factor rebalancing.

Same-input guarantee
--------------------
Both arms consume the SAME universe, profiles, metrics and quotes objects captured
once into a frozen snapshot. The challenger cannot fetch later data than the
champion because it fetches nothing — PE values are supplied to
``build_snapshot`` up front and merged into a *copy* of the metrics rows. A
snapshot fingerprint is recorded so the pairing is auditable, and a test asserts
the champion arm's input rows are byte-identical to the originals.

Attribution
-----------
The two PE effects are reported separately, because they are separate decisions:

  * **hard-filter effect** — a name the champion admitted that the challenger
    rejects on ``pe > 50`` (or vice versa). Changes MEMBERSHIP.
  * **score effect** — a name present in both whose score moved purely by PE
    points. Changes RANK.

Conflating them would hide whether any future benefit comes from excluding
expensive stocks or from ranking cheap ones higher.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger("portfolio_automation.research.pe_challenger")

SCHEMA_VERSION = "1"
PE_BANDS = (15.0, 12.0, 8.0, 3.0, 0.0)


def _fingerprint(payload: Any) -> str:
    """Stable hash over the frozen inputs, for experiment lineage."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def build_snapshot(*, symbols: list[str], profiles: list[dict], metrics: list[dict],
                   quotes: dict[str, dict], pe_by_symbol: dict[str, Any],
                   as_of: str) -> dict[str, Any]:
    """Freeze one point-in-time input set shared by both arms.

    ``pe_by_symbol`` maps symbol -> a ``pe_resolver`` result dict. Only results
    whose ``quality`` is ``direct`` or ``derived`` contribute a usable PE;
    ``negative_earnings``/``invalid``/``unavailable`` deliberately inject NOTHING,
    so a missing PE stays missing in the challenger rather than becoming a fake 0
    (which `_score` would read via its ``or 100`` default and silently band).
    """
    usable = {}
    skipped: dict[str, str] = {}
    for symbol, res in (pe_by_symbol or {}).items():
        if not isinstance(res, dict):
            continue
        if res.get("quality") in ("direct", "derived") and res.get("pe_ratio") is not None:
            usable[str(symbol)] = float(res["pe_ratio"])
        else:
            skipped[str(symbol)] = str(res.get("quality") or "unknown")

    return {
        "as_of": as_of,
        "symbols": list(symbols),
        "profiles": profiles,
        "metrics": metrics,
        "quotes": quotes,
        "pe_usable": usable,
        "pe_skipped": skipped,
        "fingerprint": _fingerprint({
            "symbols": sorted(symbols),
            "metrics": sorted((m.get("symbol"), sorted(m.items(), key=lambda kv: kv[0]))
                              for m in metrics if isinstance(m, dict)),
            "pe": sorted(usable.items()),
            "as_of": as_of,
        }),
        "research_only": True,
    }


def _challenger_metrics(snapshot: dict[str, Any]) -> list[dict]:
    """Deep-copy the metrics rows and merge in the research PE. Never mutates."""
    rows = copy.deepcopy(snapshot["metrics"])
    usable = snapshot["pe_usable"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        pe = usable.get(str(row.get("symbol") or ""))
        if pe is not None:
            row["peRatio"] = pe
    return rows


def _pe_points(pe: float | None) -> float:
    """Mirror of the scanner's PE band table. Missing PE -> 0 (its `or 100` path)."""
    if pe is None or pe <= 0:
        return 0.0
    if pe <= 15:
        return 15.0
    if pe <= 25:
        return 12.0
    if pe <= 35:
        return 8.0
    if pe <= 50:
        return 3.0
    return 0.0


def _rank_map(candidates: list[dict]) -> dict[str, int]:
    return {str(c.get("symbol")): i + 1 for i, c in enumerate(candidates)}


def _spearman(pairs: list[tuple[int, int]]) -> float | None:
    """Rank correlation over symbols present in BOTH arms. None if n < 3."""
    n = len(pairs)
    if n < 3:
        return None
    d2 = sum((a - b) ** 2 for a, b in pairs)
    return round(1 - (6 * d2) / (n * (n * n - 1)), 4)


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"mean": None, "median": None, "stdev": None, "n": 0}
    s = sorted(values)
    n = len(s)
    mean = sum(s) / n
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    var = sum((v - mean) ** 2 for v in s) / n
    return {"mean": round(mean, 4), "median": round(median, 4),
            "stdev": round(var ** 0.5, 4), "n": n}


def run_pe_experiment(scanner_factory, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Run both arms over the frozen snapshot and report structural differences.

    ``scanner_factory`` is a zero-arg callable returning a fresh
    ``CandidateScanner`` configured EXACTLY as production. Two independent
    instances are used so neither arm can carry state into the other.
    """
    champ_scanner = scanner_factory()
    chal_scanner = scanner_factory()

    champ, champ_debug = champ_scanner.full_scan(
        snapshot["symbols"], snapshot["profiles"], snapshot["metrics"], snapshot["quotes"])
    chal, chal_debug = chal_scanner.full_scan(
        snapshot["symbols"], snapshot["profiles"], _challenger_metrics(snapshot),
        snapshot["quotes"])

    champ_syms = [str(c.get("symbol")) for c in champ]
    chal_syms = [str(c.get("symbol")) for c in chal]
    champ_set, chal_set = set(champ_syms), set(chal_syms)
    champ_rank, chal_rank = _rank_map(champ), _rank_map(chal)
    champ_score = {str(c.get("symbol")): float(c.get("score") or 0.0) for c in champ}
    chal_score = {str(c.get("symbol")): float(c.get("score") or 0.0) for c in chal}
    usable = snapshot["pe_usable"]

    # PE-guard rejections: names the challenger's debug rows failed on PE.
    guard_rejected = sorted(
        str(r.get("symbol")) for r in chal_debug
        if isinstance(r, dict) and "pe=" in str(r.get("failed_filters") or "")
    )

    dropped = sorted(champ_set - chal_set)
    added = sorted(chal_set - champ_set)
    both = sorted(champ_set & chal_set)

    # Attribution — the two effects are never merged.
    hard_filter_effect = [
        {"symbol": s, "pe": usable.get(s), "champion_rank": champ_rank.get(s),
         "effect": "hard_filter_exclusion"}
        for s in dropped if s in guard_rejected
    ]
    dropped_other = [s for s in dropped if s not in guard_rejected]

    score_effect = []
    for s in both:
        delta = round(chal_score[s] - champ_score[s], 6)
        if delta == 0:
            continue
        score_effect.append({
            "symbol": s, "pe": usable.get(s),
            "pe_points": _pe_points(usable.get(s)),
            "score_champion": round(champ_score[s], 4),
            "score_challenger": round(chal_score[s], 4),
            "score_delta": delta,
            "rank_champion": champ_rank.get(s),
            "rank_challenger": chal_rank.get(s),
            "rank_delta": (champ_rank.get(s, 0) - chal_rank.get(s, 0)),
            "effect": "score_only",
        })
    score_effect.sort(key=lambda r: -abs(r["score_delta"]))

    displacements = [abs(champ_rank[s] - chal_rank[s]) for s in both]
    band_counts = {f"{int(p)}pts": 0 for p in PE_BANDS}
    for s in chal_syms:
        band_counts[f"{int(_pe_points(usable.get(s)))}pts"] += 1

    def _overlap(k):
        return len(set(champ_syms[:k]) & set(chal_syms[:k]))

    return {
        "schema_version": SCHEMA_VERSION,
        "research_only": True,
        "feeds_production": False,
        "as_of": snapshot["as_of"],
        "snapshot_fingerprint": snapshot["fingerprint"],
        "counts": {
            "champion_candidates": len(champ),
            "challenger_candidates": len(chal),
            "pe_usable": len(usable),
            "pe_skipped": len(snapshot["pe_skipped"]),
            "pe_guard_rejections": len(guard_rejected),
        },
        "overlap": {"top_10": _overlap(10), "top_20": _overlap(20), "top_50": _overlap(50)},
        "rank": {
            "spearman": _spearman([(champ_rank[s], chal_rank[s]) for s in both]),
            "median_abs_displacement": (sorted(displacements)[len(displacements) // 2]
                                        if displacements else None),
            "max_displacement": (max(displacements) if displacements else None),
        },
        "scores": {"champion": _stats(list(champ_score.values())),
                   "challenger": _stats(list(chal_score.values())),
                   "delta": _stats([chal_score[s] - champ_score[s] for s in both])},
        "pe_band_distribution": band_counts,
        "membership": {
            "dropped_by_challenger": dropped,
            "dropped_via_pe_guard": [r["symbol"] for r in hard_filter_effect],
            "dropped_other_reason": dropped_other,
            "added_by_challenger": added,
        },
        "attribution": {
            "hard_filter_effect": hard_filter_effect,
            "score_effect_top": score_effect[:20],
            "score_effect_count": len(score_effect),
        },
        "guard_rejected_symbols": guard_rejected,
    }
