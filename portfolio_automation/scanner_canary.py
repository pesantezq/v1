"""Scanner-recovery acceptance canary — observe-only.

Purpose: make the next weekly run judgeable at a glance, without log archaeology.
It reads the scanner-quality contract already published in
``outputs/latest/scraped_intel_run_summary.json`` and renders one explicit verdict
per dimension.

Design rules this module obeys:
  * **Transport, never recompute.** Every verdict is derived from a published
    field. No freshness is re-derived from a file mtime, no coverage from a row
    count, no score or rank is touched.
  * **`n/a`, never inference.** A dimension whose input is absent renders ``n/a``
    and cannot contribute a PASS. The previous candidate count is shown only when
    an authoritative prior artifact exists under ``outputs/history/``.
  * **Unavailable certification is not success.** ``overall`` can only be PASS
    when all four mandatory dimensions actually reported.

Observe-only: reads artifacts, optionally writes one POLICY artifact, and mutates
no decision, allocation, score, watchlist, or approval state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("portfolio_automation.scanner_canary")

SCHEMA_VERSION = "1"
NA = "n/a"

_RUN_SUMMARY_REL = ("outputs", "latest", "scraped_intel_run_summary.json")


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, IsADirectoryError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _previous_candidate_count(root: Path) -> Any:
    """Most recent prior run's candidate count, or ``n/a``.

    Deliberately does NOT infer. If no dated history artifact carries a
    ``symbol_count`` we say so rather than reconstructing a plausible number —
    a fabricated "previous" value would make a recovery look proven when it is
    merely assumed.
    """
    hist = root / "outputs" / "history"
    if not hist.is_dir():
        return NA
    try:
        dated = sorted((d for d in hist.iterdir() if d.is_dir()), reverse=True)
    except OSError:
        return NA
    for day in dated:
        data = _load(day / "scraped_intel_run_summary.json")
        scanner = (data or {}).get("scanner")
        if isinstance(scanner, dict) and isinstance(scanner.get("symbol_count"), int):
            return scanner["symbol_count"]
    return NA


def build_scanner_canary(root: Path | str = ".", *, now: str | None = None) -> dict[str, Any]:
    """Assemble the acceptance canary from the published run summary."""
    root = Path(root)
    # ``now`` was optional with no default, so every real caller left
    # assessed_at null and a reader could not tell WHEN the verdict was formed
    # — the same blind spot as discovery_pulse.last_run_at. Stamped here so the
    # field is always populated; callers may still pin it for tests.
    now = now or datetime.now(timezone.utc).isoformat()
    summary = _load(root.joinpath(*_RUN_SUMMARY_REL))
    reasons: list[str] = []

    if summary is None:
        return {
            "schema_version": SCHEMA_VERSION, "observe_only": True,
            "assessed_at": now, "overall": "UNKNOWN",
            "reasons": ["run_summary_missing"],
        }

    scanner = summary.get("scanner") if isinstance(summary.get("scanner"), dict) else {}
    cr = scanner.get("constituent_resolution") if isinstance(scanner.get("constituent_resolution"), dict) else None
    ss = scanner.get("screening_sufficiency") if isinstance(scanner.get("screening_sufficiency"), dict) else None
    us = scanner.get("universe_sufficiency") if isinstance(scanner.get("universe_sufficiency"), dict) else None
    rq = scanner.get("ranking_quality") if isinstance(scanner.get("ranking_quality"), dict) else None
    fl = scanner.get("factor_liveness") if isinstance(scanner.get("factor_liveness"), dict) else None

    # ── 1. Constituent resolution ──────────────────────────────────────────
    if cr is None:
        constituent = {"source": NA, "resolved": NA, "plausibility": NA,
                       "freshness": NA, "age_days": NA, "degraded": NA}
        reasons.append("constituent_resolution_absent")
    else:
        count = cr.get("count")
        floor = cr.get("plausibility_floor")
        plausible = (isinstance(count, int) and isinstance(floor, int) and count >= floor)
        freshness = str(cr.get("freshness") or "unknown").upper()
        constituent = {
            "source": cr.get("source") or NA,
            "resolved": count if count is not None else NA,
            "plausibility": "PASS" if plausible else "FAIL",
            "freshness": freshness,
            "age_days": cr.get("age_days") if cr.get("age_days") is not None else NA,
            "degraded": cr.get("degraded"),
            "cache_write": "PASS" if cr.get("fetched_at") else NA,
        }
        if not plausible:
            reasons.append("constituent_implausible")
        if freshness == "EXPIRED":
            reasons.append("constituent_cache_expired")
        elif freshness == "UNKNOWN":
            reasons.append("constituent_age_unknown")

    # ── 2. Screening coverage ──────────────────────────────────────────────
    if ss is None:
        screening = {"eligible": NA, "fundamentals_resolved": NA, "coverage": NA,
                     "verdict": NA, "status": NA}
        reasons.append("screening_sufficiency_absent")
    else:
        cov = ss.get("screening_coverage")
        screening = {
            "eligible": ss.get("eligible_symbols", NA),
            "fundamentals_requested": ss.get("fundamentals_requested", NA),
            "fundamentals_resolved": ss.get("fundamentals_resolved", NA),
            "coverage": cov if cov is not None else NA,
            "unscreened": ss.get("unscreened_count", NA),
            "status": ss.get("status", NA),
            "verdict": "PASS" if ss.get("sufficient") else "FAIL",
            "minimum_threshold": ss.get("minimum_threshold", NA),
        }
        if not ss.get("sufficient"):
            reasons.append("insufficient_screening_coverage")

    # ── 3. Final dataset ───────────────────────────────────────────────────
    us_reasons = list(us.get("reasons") or []) if us else []
    if us is None:
        watchlist = {"previous_candidate_count": _previous_candidate_count(root),
                     "current_candidate_count": NA,
                     "universe_sufficiency": NA, "small_dataset": NA}
        reasons.append("universe_sufficiency_absent")
    else:
        watchlist = {
            "previous_candidate_count": _previous_candidate_count(root),
            "current_candidate_count": us.get("candidate_count", NA),
            "trust_floor": us.get("trust_floor", NA),
            "universe_sufficiency": "PASS" if us.get("sufficient") else "FAIL",
            "small_dataset": "PRESENT" if "small_dataset" in us_reasons else "CLEARED",
        }
        if not us.get("sufficient"):
            reasons.append("insufficient_dataset")

    # ── 4. Ranking quality (observability: WARN, never FAIL) ───────────────
    if rq is None:
        ranking = {"unique_score_count": NA, "largest_tie_fraction": NA,
                   "alphabetical_tie_tail_count": NA, "degeneracy": NA}
        reasons.append("ranking_quality_absent")
    else:
        degenerate = bool(rq.get("degenerate_ranking"))
        ranking = {
            "candidate_count": rq.get("candidate_count", NA),
            "unique_score_count": rq.get("distinct_score_count", NA),
            "largest_tie_fraction": rq.get("largest_tie_fraction", NA),
            "alphabetical_tie_tail_count": rq.get("alphabetical_tie_tail_count", NA),
            "degeneracy": "WARN" if degenerate else "PASS",
        }
        if degenerate:
            reasons.append("degenerate_ranking")

    # ── 4b. Factor/filter liveness (reported INDEPENDENTLY; never a hard fail) ──
    # A documented component being inert is a real finding, but making it a FAIL
    # would change production authority semantics — PE has been inert all along
    # and the sleeve was permitted. So it degrades the canary to WARN at most.
    if fl is None:
        factors = {"status": NA, "inert": NA, "detail": NA}
        reasons.append("factor_liveness_absent")
    else:
        inert = list(fl.get("inert_components") or [])
        factors = {
            "status": str(fl.get("status") or "unknown").upper(),
            "inert": inert or "none",
            "inert_count": len(inert),
            "suppresses_sleeve": fl.get("suppresses_sleeve"),
            "detail": ", ".join(fl.get("reasons") or []) or "all components live",
        }
        if inert:
            reasons.append(f"inert_factors:{','.join(inert)}")

    # ── Downstream consequence ─────────────────────────────────────────────
    suppressed = scanner.get("safe_mode")
    suppression_reasons = list(scanner.get("safe_mode_reasons") or [])
    downstream = {
        "speculative_sleeve_suppressed": suppressed,
        "suppression_reasons": suppression_reasons,
        "suppression_cleared_because": (
            "scanner quality guards satisfied" if suppressed is False else NA),
    }

    # ── Overall ────────────────────────────────────────────────────────────
    hard_fail = {"constituent_implausible", "constituent_cache_expired",
                 "insufficient_screening_coverage", "insufficient_dataset"}
    absent = {"constituent_resolution_absent", "screening_sufficiency_absent",
              "universe_sufficiency_absent", "ranking_quality_absent",
              "factor_liveness_absent", "constituent_age_unknown"}
    if hard_fail & set(reasons):
        overall = "FAIL"
    elif absent & set(reasons):
        # Cannot certify the chain -> not a PASS. Not a FAIL either: the missing
        # dimension may simply not apply to this run mode (a daily quote refresh
        # resolves no constituents).
        overall = "WARN"
    elif (any(r.startswith("inert_factors:") for r in reasons)
          or "degenerate_ranking" in reasons
          or constituent.get("freshness") == "STALE"):
        overall = "WARN"
    else:
        overall = "PASS"

    return {
        "schema_version": SCHEMA_VERSION,
        "observe_only": True,
        "assessed_at": now,
        "run_mode": summary.get("run_mode"),
        "run_timestamp": summary.get("timestamp"),
        "overall": overall,
        "reasons": reasons,
        "constituent": constituent,
        "screening": screening,
        "watchlist": watchlist,
        "ranking": ranking,
        "factors": factors,
        "downstream": downstream,
    }


def render_canary_text(canary: dict[str, Any]) -> str:
    """Render the canary as the operator-facing acceptance block."""
    c = canary.get("constituent") or {}
    s = canary.get("screening") or {}
    w = canary.get("watchlist") or {}
    r = canary.get("ranking") or {}
    d = canary.get("downstream") or {}

    def _pct(value):
        return f"{value:.1%}" if isinstance(value, (int, float)) else str(value)

    lines = [
        "SCANNER RECOVERY CANARY",
        f"overall: {canary.get('overall')}"
        + (f"  ({', '.join(canary.get('reasons') or [])})" if canary.get("reasons") else ""),
        f"run: {canary.get('run_mode') or NA} @ {canary.get('run_timestamp') or NA}",
        "",
        "Constituent resolution",
        f"- source: {c.get('source')}",
        f"- resolved: {c.get('resolved')}",
        f"- plausibility: {c.get('plausibility')}",
        f"- freshness: {c.get('freshness')} (age {c.get('age_days')}d)",
        f"- cache write: {c.get('cache_write', NA)}",
        "",
        "Screening",
        f"- eligible: {s.get('eligible')}",
        f"- fundamentals requested: {s.get('fundamentals_requested', NA)}",
        f"- fundamentals resolved: {s.get('fundamentals_resolved')}",
        f"- coverage: {_pct(s.get('coverage'))}",
        f"- screening sufficiency: {s.get('verdict')}",
        "",
        "Watchlist",
        f"- previous candidate count: {w.get('previous_candidate_count')}",
        f"- current candidate count: {w.get('current_candidate_count')}",
        f"- universe sufficiency: {w.get('universe_sufficiency')}",
        f"- small_dataset: {w.get('small_dataset')}",
        "",
        "Ranking quality",
        f"- unique score count: {r.get('unique_score_count')}",
        f"- largest tie fraction: {r.get('largest_tie_fraction')}",
        f"- alphabetical tie tail: {r.get('alphabetical_tie_tail_count')}",
        f"- degeneracy: {r.get('degeneracy')}",
        "",
        "Factor/filter liveness",
        f"- status: {(canary.get('factors') or {}).get('status')}",
        f"- inert components: {(canary.get('factors') or {}).get('inert')}",
        f"- detail: {(canary.get('factors') or {}).get('detail')}",
        f"- suppresses sleeve: {(canary.get('factors') or {}).get('suppresses_sleeve', NA)}",
        "",
        "Downstream",
        f"- speculative sleeve suppressed: {d.get('speculative_sleeve_suppressed')}",
        f"- suppression reasons: {', '.join(d.get('suppression_reasons') or []) or 'none'}",
        f"- suppression cleared because: {d.get('suppression_cleared_because')}",
    ]
    return "\n".join(lines)


def run_scanner_canary(root: Path | str = ".", now: str | None = None,
                       *, write: bool = True) -> dict[str, Any]:
    """Entry point: build, optionally persist, and return the canary."""
    canary = build_scanner_canary(root, now=now)
    if write:
        try:
            from portfolio_automation.data_governance import (
                OutputNamespace, ensure_output_dir, get_output_path,
            )
            base_dir = Path(root) / "outputs"
            ensure_output_dir(OutputNamespace.POLICY, base_dir=base_dir)
            out = get_output_path(OutputNamespace.POLICY, "scanner_recovery_canary.json",
                                  base_dir=base_dir)
            out.write_text(json.dumps(canary, indent=1), encoding="utf-8")
            out.with_suffix(".md").write_text(
                "```\n" + render_canary_text(canary) + "\n```\n", encoding="utf-8")
        except Exception as exc:
            logger.warning("scanner_canary: write failed: %s", exc)
    return canary
