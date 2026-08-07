from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULT_STALE_DAYS = 7
MIN_TRUSTED_DATASET_SIZE = 5

# --- Screening-coverage policy ---------------------------------------------
# Coverage = (eligible symbols whose PRIMARY screen field actually resolved) /
#            (eligible symbols).
#
# These are POLICY DEFAULTS, not empirically validated constants. Rationale:
# the observed live yield after the 2026-08-03 repair was 503/503 eligible with
# full fundamentals coverage, so ~1.0 is the achievable normal. 0.90 leaves room
# for the handful of names whose annual filings legitimately lack a comparable
# prior period, while 0.50 marks the point at which the majority of the universe
# would enter the ranking unscreened — the exact condition that produced the
# alphabetical tie tail under v3_max_symbols=100 (24/100 screened = 0.24).
# Adjust here; nothing downstream hardcodes them.
SCREENING_HEALTHY_COVERAGE = 0.90
SCREENING_MIN_COVERAGE = 0.50

# The fields CandidateScanner._passes_hard_filters actually reads from a metrics
# row. `revenueGrowth` is PRIMARY: it is the only discriminating fundamental
# screen (min_rev_growth, default 0.15). peRatio (>50 bubble guard) and
# freeCashFlowYield (>=0 guard) are secondary rejection guards. All three are
# non-fatal when absent BY DESIGN, which is precisely why their absence has to be
# measured rather than assumed away.
SCREENING_PRIMARY_FIELD = "revenueGrowth"
SCREENING_SECONDARY_FIELDS = ("peRatio", "freeCashFlowYield")
SCREENING_FIELDS = (SCREENING_PRIMARY_FIELD,) + SCREENING_SECONDARY_FIELDS


def _is_present(value: Any) -> bool:
    """True only for a usable numeric value.

    A field that is absent, None, or non-numeric cannot make the hard filter
    bind, so it must not be credited as screened. ``bool`` is excluded because
    True/False is never a legitimate fundamentals value.
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(str(value))
    except (TypeError, ValueError):
        return False
    return True


def assess_screening_sufficiency(
    *,
    eligible_symbols: Any,
    requested_symbols: Any,
    metrics_rows: Any,
    healthy_threshold: float = SCREENING_HEALTHY_COVERAGE,
    minimum_threshold: float = SCREENING_MIN_COVERAGE,
) -> dict[str, Any]:
    """Measure whether the fundamental screen actually bound on the universe.

    This answers a DIFFERENT question from ``assess_scanner_dataset_sufficiency``:
    that one asks "are there enough candidate rows?", this one asks "did enough of
    the eligible universe receive the inputs the screen needs?". A scanner can
    emit 100 candidates while only 24 were ever screened.

    Denominators (all explicit, per the implementation-discipline rules):
      * ``eligible_symbols``      — passed the profile/market-cap stage, i.e. the
                                    set that legitimately qualified for a
                                    fundamentals lookup. THIS is the denominator.
      * ``fundamentals_requested``— symbols actually submitted for lookup. Lower
                                    than eligible whenever ``v3_max_symbols``
                                    binds; the gap is unscreened by construction.
      * ``fundamentals_resolved`` — eligible symbols whose PRIMARY field resolved
                                    to a usable number. NOT "a row came back":
                                    ``get_fundamentals_v3`` appends a row for
                                    every symbol regardless, so row count is
                                    100% by construction and meaningless here.

    Fails closed: an empty eligible set, a missing metrics input, or garbage rows
    yield ``sufficient=False`` and never a coverage of 1.0.
    """
    eligible = [str(s) for s in eligible_symbols if s] if isinstance(eligible_symbols, (list, tuple, set)) else []
    requested = [str(s) for s in requested_symbols if s] if isinstance(requested_symbols, (list, tuple, set)) else []
    eligible_set = set(eligible)

    rows = metrics_rows if isinstance(metrics_rows, (list, tuple)) else []
    field_hits: dict[str, int] = {f: 0 for f in SCREENING_FIELDS}
    complete = partial = 0
    resolved_syms: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        # Rows for symbols outside the eligible set must not inflate coverage.
        if not symbol or symbol not in eligible_set:
            continue
        present = [f for f in SCREENING_FIELDS if _is_present(row.get(f))]
        for field in present:
            field_hits[field] = field_hits.get(field, 0) + 1
        if len(present) == len(SCREENING_FIELDS):
            complete += 1
        elif present:
            partial += 1
        if _is_present(row.get(SCREENING_PRIMARY_FIELD)):
            resolved_syms.add(symbol)

    resolved = len(resolved_syms)
    reasons: list[str] = []

    if not eligible:
        coverage: float | None = None
        status = "unknown"
        reasons.append("no_eligible_universe")
    else:
        coverage = resolved / len(eligible)
        if metrics_rows is None:
            reasons.append("fundamentals_input_missing")
        if coverage >= healthy_threshold:
            status = "healthy"
        elif coverage >= minimum_threshold:
            status = "degraded"
            reasons.append(
                f"degraded_screening_coverage:{coverage:.3f}<{healthy_threshold}")
        else:
            status = "unsafe"
            reasons.append(
                f"insufficient_screening_coverage:{coverage:.3f}<{minimum_threshold}")

    return {
        "eligible_symbols": len(eligible),
        "fundamentals_requested": len(requested),
        "fundamentals_resolved": resolved,
        "fundamentals_missing": max(0, len(eligible) - resolved),
        "screening_coverage": (round(coverage, 4) if coverage is not None else None),
        "unscreened_count": max(0, len(eligible) - resolved),
        "rows_complete": complete,
        "rows_partial": partial,
        "rows_missing": max(0, len(eligible) - complete - partial),
        "status": status,
        "sufficient": status in ("healthy", "degraded"),
        "reasons": reasons,
        # Per-field resolution counts. This is how a SILENTLY INERT guard becomes
        # visible: a field resolving for 0 of N eligible symbols means the hard
        # filter that reads it can never bind, no matter how healthy everything
        # else looks. Live 2026-08-03: peRatio resolved 0/503 because
        # stable/key-metrics returns `earningsYield` (the reciprocal) and no
        # peRatio/priceEarningsRatio, while the v3 fallback in
        # get_fundamentals_v3 only runs when key-metrics returns nothing at all —
        # and it returns plenty. So the PE>50 bubble guard has been inert for the
        # whole universe. MEASURED here, deliberately NOT fixed: repairing it
        # would change which candidates pass, i.e. scanner behaviour, which is out
        # of scope for a measurement task.
        "field_resolution": dict(field_hits),
        "inert_fields": sorted(f for f, c in field_hits.items() if c == 0),
        "primary_field": SCREENING_PRIMARY_FIELD,
        "screening_fields": list(SCREENING_FIELDS),
        "healthy_threshold": healthy_threshold,
        "minimum_threshold": minimum_threshold,
    }


def assess_scanner_dataset_sufficiency(
    candidate_count: Any,
    *,
    min_size: int = MIN_TRUSTED_DATASET_SIZE,
) -> list[str]:
    """Return safe-mode reasons implied by the SIZE of the candidate set.

    Deliberately independent of ``degraded_mode``. Until 2026-08-03 this exact
    comparison lived inside ``if degraded_mode:`` in main.py, which made it
    unreachable whenever FMP was healthy — so a 3-candidate universe produced no
    reason at all, because nothing had "fallen back". Sufficiency is a property
    of the dataset, not of how the dataset was obtained.

    Note this does NOT compare against ``scanner.top_k_watchlist`` (100). That
    value is a CAP, not a target: ``min_rev_growth`` is a hard filter applied over
    only the top ``v3_max_symbols`` by market cap, so the true filter-passing
    yield is far below 100 and comparing to it would raise a permanent false
    alarm. The floor is an absolute trust threshold instead.

    Fails closed: a missing or non-integer count reads as empty, never as
    sufficient.
    """
    if not isinstance(candidate_count, int) or isinstance(candidate_count, bool):
        return ["empty_dataset"]
    if candidate_count <= 0:
        return ["empty_dataset"]
    if candidate_count < min_size:
        return ["small_dataset"]
    return []


def infer_degraded_reason(
    *,
    fmp_attempted: bool = False,
    fmp_succeeded: bool = False,
    fmp_error: str | None = None,
    fallback_used: bool = False,
    watchlist_source: str = "none",
    scan_status: str | None = None,
    missing_data_ratio: float | None = None,
    missing_data_threshold: float = 0.50,
) -> str | None:
    """
    Infer a degraded-mode reason from scan / API context.

    New parameters (backward-compatible defaults):
        missing_data_ratio:    Fraction of symbols with missing critical data.
        missing_data_threshold: Ratio above which 'missing_critical_data' fires.
    """
    err = (fmp_error or "").lower()
    if "circuit breaker" in err:
        return "circuit_breaker"
    if "http 403" in err:
        return "fmp_403"
    if "http 401" in err:
        return "fmp_401"
    if "http 429" in err:
        return "fmp_429"
    if "budget" in err:
        return "budget_exhausted"
    if "http 5" in err:
        return "fmp_5xx"
    if (
        missing_data_ratio is not None
        and missing_data_ratio >= missing_data_threshold
    ):
        return "missing_critical_data"
    if scan_status == "cache_only":
        return "cache_only"
    if scan_status == "degraded":
        return "partial_cache"
    if fallback_used or (watchlist_source or "").startswith("fallback"):
        return "fallback_watchlist"
    if fmp_attempted and not fmp_succeeded:
        return "fmp_failed"
    return None


def infer_data_sources_used(
    *,
    fmp_succeeded: bool = False,
    fallback_used: bool = False,
    watchlist_source: str = "none",
    scan_status: str | None = None,
    extra_sources: list[str] | None = None,
) -> list[str]:
    sources: list[str] = []
    if fmp_succeeded:
        sources.append("fmp")
    if fallback_used or (watchlist_source or "").startswith("fallback"):
        sources.append("fallback")
    if scan_status in {"cache_only", "degraded"}:
        sources.append("cache")
    for src in extra_sources or []:
        if src and src not in sources:
            sources.append(src)
    if not sources:
        sources.append("live")
    return sources


def infer_data_mode(data_sources_used: list[str]) -> str:
    normalized = [s for s in data_sources_used if s]
    if not normalized or normalized == ["live"] or normalized == ["fmp"]:
        return "live"
    if all(src in {"fallback", "cache"} for src in normalized):
        return "fallback"
    if "fmp" in normalized or "live" in normalized:
        return "mixed"
    return "fallback"


def infer_confidence_penalty(
    *,
    degraded_mode: bool,
    degraded_reason: str | None,
    stale_cache_days: int | None = None,
) -> float:
    if not degraded_mode:
        return 0.0
    if degraded_reason == "circuit_breaker":
        penalty = 0.30
    elif degraded_reason in {"fmp_401", "fmp_403", "fmp_429", "fmp_5xx"}:
        penalty = 0.25
    elif degraded_reason in {"cache_only", "budget_exhausted", "missing_critical_data"}:
        penalty = 0.20
    else:
        penalty = 0.15
    if stale_cache_days is not None and stale_cache_days > DEFAULT_STALE_DAYS:
        penalty = max(penalty, 0.30)
    return round(min(penalty, 0.5), 3)


def compute_fallback_depth(data_sources_used: list[str]) -> int:
    return len([src for src in data_sources_used if src not in {"live", "fmp", "rss", "sp500_cache"}])


def build_data_health_context(
    *,
    fmp_attempted: bool = False,
    fmp_succeeded: bool = False,
    fmp_error: str | None = None,
    fallback_used: bool = False,
    watchlist_source: str = "none",
    scan_status: str | None = None,
    data_latency_ms: int | None = None,
    stale_cache_days: int | None = None,
    extra_sources: list[str] | None = None,
) -> dict[str, Any]:
    degraded_reason = infer_degraded_reason(
        fmp_attempted=fmp_attempted,
        fmp_succeeded=fmp_succeeded,
        fmp_error=fmp_error,
        fallback_used=fallback_used,
        watchlist_source=watchlist_source,
        scan_status=scan_status,
    )
    data_sources_used = infer_data_sources_used(
        fmp_succeeded=fmp_succeeded,
        fallback_used=fallback_used,
        watchlist_source=watchlist_source,
        scan_status=scan_status,
        extra_sources=extra_sources,
    )
    degraded_mode = bool(degraded_reason)
    data_mode = infer_data_mode(data_sources_used)
    return {
        "degraded_mode": degraded_mode,
        "degraded_reason": degraded_reason,
        "data_sources_used": data_sources_used,
        "data_mode": data_mode,
        "data_fallback_triggered": fallback_used or watchlist_source.startswith("fallback"),
        "llm_fallback_triggered": False,
        "data_latency_ms": data_latency_ms,
        "fallback_depth": compute_fallback_depth(data_sources_used),
        "degraded_confidence_penalty": infer_confidence_penalty(
            degraded_mode=degraded_mode,
            degraded_reason=degraded_reason,
            stale_cache_days=stale_cache_days,
        ),
        "stale_cache_days": stale_cache_days,
    }


def stale_cache_days_for_path(path: str | Path | None) -> int | None:
    if not path:
        return None
    try:
        p = Path(path)
        if not p.exists():
            return None
        delta = __import__("datetime").datetime.now() - __import__("datetime").datetime.fromtimestamp(p.stat().st_mtime)
        return delta.days
    except Exception:
        return None


def summarize_data_health(ctx: dict[str, Any]) -> str:
    return (
        f"degraded mode: {'yes' if ctx.get('degraded_mode') else 'no'}, "
        f"data={ctx.get('data_mode', 'live')}, "
        f"reason={ctx.get('degraded_reason') or 'none'}, "
        f"fallback_depth={ctx.get('fallback_depth', 0)}, "
        f"latency={ctx.get('data_latency_ms', '(n/a)')}ms"
    )


def check_scan_data_quality(results: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Compute a data-quality audit from a completed scan's results list.

    Counts per-symbol data provenance and returns a summary dict that can
    feed back into build_data_health_context via missing_data_ratio.

    Returns:
        {
          "total":                   int,
          "fresh_count":             int,
          "cached_count":            int,
          "fmp_fallback_count":      int,   # any source came from FMP
          "missing_price_count":     int,
          "missing_fundamentals_count": int,
          "missing_data_ratio":      float, # fraction with missing price OR fundamentals
          "fmp_fallback_ratio":      float,
          "data_quality_assessment": "good" | "degraded" | "poor",
        }
    """
    if not results:
        return {
            "total": 0,
            "fresh_count": 0,
            "cached_count": 0,
            "fmp_fallback_count": 0,
            "missing_price_count": 0,
            "missing_fundamentals_count": 0,
            "missing_data_ratio": 0.0,
            "fmp_fallback_ratio": 0.0,
            "data_quality_assessment": "good",
        }

    total = len(results)
    fresh = sum(1 for r in results if r.get("data_quality") == "fresh")
    cached = sum(1 for r in results if r.get("data_quality") in {"cached", "partial", "budget_skipped"})
    fmp_fb = sum(1 for r in results if r.get("fallback_used"))
    miss_price = sum(1 for r in results if r.get("price_data_source") == "missing")
    miss_fund = sum(1 for r in results if r.get("fundamentals_source") in {"missing", None})

    # A symbol is "critically missing" when BOTH price and fundamentals are absent
    critical_missing = sum(
        1 for r in results
        if r.get("price_data_source") == "missing"
        and r.get("fundamentals_source") in {"missing", None}
    )

    missing_ratio = round(critical_missing / total, 4) if total else 0.0
    fmp_ratio = round(fmp_fb / total, 4) if total else 0.0

    if missing_ratio >= 0.50 or fmp_ratio >= 0.80:
        assessment = "poor"
    elif missing_ratio >= 0.20 or cached / total >= 0.50:
        assessment = "degraded"
    else:
        assessment = "good"

    return {
        "total":                      total,
        "fresh_count":                fresh,
        "cached_count":               cached,
        "fmp_fallback_count":         fmp_fb,
        "missing_price_count":        miss_price,
        "missing_fundamentals_count": miss_fund,
        "missing_data_ratio":         missing_ratio,
        "fmp_fallback_ratio":         fmp_ratio,
        "data_quality_assessment":    assessment,
    }


# ---------------------------------------------------------------------------
# Scanner ranking quality (observability only — never changes score or rank)
# ---------------------------------------------------------------------------

RANKING_DEGENERATE_TIE_FRACTION = 0.50


def assess_ranking_quality(candidates: Any) -> dict[str, Any]:
    """Measure whether the scanner produced a DIFFERENTIATED ranking.

    Answers: are these scores carrying information, or is this a technically
    populated list whose tail is alphabetical filler?

    Deliberately scanner-specific rather than reusing
    ``universe_sanitation._diagnose_ranking``: that detector's tie key and
    zero-information terms read ``sources`` / ``theme_confidence_max`` /
    ``recent_hit_rate``, which do not exist on scanner rows — running it here
    would flag those terms as zero-information every time, by construction.

    ``alphabetical_tie_tail_count`` is the length of the longest SUFFIX that both
    shares one score and is ascending by symbol. ``full_scan`` sorts by score
    alone with no explicit tiebreak, and Python's sort is stable over an
    alphabetically sorted input universe, so an alphabetical tail is the direct
    signature of scores that stopped discriminating — which is what partial
    fundamentals coverage produced.

    Pure and read-only: never reorders, rescores, or mutates the input. The
    0.50 threshold matches ``universe_sanitation``'s existing degeneracy
    convention so the two surfaces agree on what "degenerate" means.
    """
    rows = [c for c in (candidates or []) if isinstance(c, dict)] \
        if isinstance(candidates, (list, tuple)) else []
    n = len(rows)

    scores: list[float | None] = []
    unparseable = 0
    for row in rows:
        raw = row.get("score")
        if raw is None or isinstance(raw, bool):
            scores.append(None)
            unparseable += 1
            continue
        try:
            scores.append(round(float(raw), 6))
        except (TypeError, ValueError):
            scores.append(None)
            unparseable += 1

    present = [s for s in scores if s is not None]
    distinct = sorted(set(present))

    if n == 0:
        largest_size, largest_fraction, largest_score = 0, 0.0, None
    else:
        counts: dict[Any, int] = {}
        for s in scores:
            counts[s] = counts.get(s, 0) + 1
        largest_score, largest_size = max(counts.items(), key=lambda kv: (kv[1], kv[0] is not None))
        largest_fraction = round(largest_size / n, 4)

    # Longest suffix sharing one score AND ascending by symbol.
    tail = 0
    if n >= 2 and scores[-1] is not None:
        last = scores[-1]
        i = n - 1
        while i >= 0 and scores[i] == last:
            i -= 1
        block = rows[i + 1:]
        if len(block) >= 2:
            symbols = [str(r.get("symbol") or "") for r in block]
            if symbols == sorted(symbols) and len(set(symbols)) == len(symbols):
                tail = len(block)

    insufficient = n < 2
    zero_variance = n > 1 and len(distinct) <= 1
    degenerate = bool(
        not insufficient
        and (zero_variance or largest_fraction >= RANKING_DEGENERATE_TIE_FRACTION)
    )

    return {
        "candidate_count": n,
        "distinct_score_count": len(distinct),
        "scores_unparseable": unparseable,
        "zero_variance": zero_variance,
        "largest_tie_group_size": largest_size,
        "largest_tie_fraction": largest_fraction,
        "largest_tie_score": largest_score,
        "alphabetical_tie_tail_count": tail,
        "alphabetical_tiebreak_detected": tail >= 2,
        "insufficient_sample": insufficient,
        "degenerate_ranking": degenerate,
        "degenerate_tie_fraction_threshold": RANKING_DEGENERATE_TIE_FRACTION,
        "observe_only": True,
    }


# ---------------------------------------------------------------------------
# Factor / filter liveness (observability only — never changes scoring)
# ---------------------------------------------------------------------------

FACTOR_STATUSES = ("live", "low_information", "degraded", "inert",
                   "not_applicable", "unknown")

# A factor whose input is present for fewer than this fraction of the eligible
# universe is DEGRADED rather than live: the transformation runs, but only for a
# minority. Policy default, aligned with SCREENING_HEALTHY_COVERAGE.
FACTOR_LIVE_MIN_COVERAGE = 0.90


def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)


def _factor_status(coverage: float | None, variance: float,
                   nonzero: int, applicable: bool = True) -> str:
    """Status for a SCORING factor.

    A factor is not LIVE merely because its field exists. It must (a) have input,
    (b) actually contribute points to someone, and (c) VARY across the candidate
    set — a constant contribution cannot discriminate, so it is
    ``low_information`` rather than live. This is the distinction that turns
    "peRatio missing" into "a documented 15-point factor contributes 0 to every
    candidate and could never change a rank".
    """
    if not applicable:
        return "not_applicable"
    if coverage is None:
        return "unknown"
    if coverage <= 0.0 or nonzero == 0:
        return "inert"
    if variance <= 0.0:
        return "low_information"
    if coverage < FACTOR_LIVE_MIN_COVERAGE:
        return "degraded"
    return "live"


def assess_factor_liveness(
    *,
    eligible_symbols: Any,
    metrics_by_symbol: Any,
    quotes_by_symbol: Any,
    candidates: Any,
    trend_filter_enabled: bool = True,
    min_rev_growth: float = 0.15,
) -> dict[str, Any]:
    """Can each documented scanner component actually influence the result?

    Measured from the EXACT metrics/quote inputs handed to ``CandidateScanner``,
    not reconstructed from final candidate rows — a candidate row has already had
    missing values coerced to 0 by ``_build_row``, so it cannot distinguish
    "absent" from "genuinely zero".

    Scoring contributions come from ``scanner.candidate_scanner.score_breakdown``,
    a read-only mirror of ``_score`` pinned by test. Nothing here feeds scoring,
    ranking, allocation, or the speculative-sleeve gate: PE being inert produces a
    DEGRADED observability finding, never a suppression, because suppressing on it
    would change current production authority semantics.
    """
    from scanner.candidate_scanner import score_breakdown

    _FACTOR_KEYS = ("revenue_growth", "fcf_yield", "roe", "pe", "trend")
    _FILTER_KEYS = ("rev_growth_min", "pe_bubble_guard",
                    "fcf_negative_guard", "trend_200dma")

    # ``None`` means the fundamentals were NEVER FETCHED on this cadence — a
    # different claim from ``{}``, which means they were fetched and came back
    # empty. Collapsing the two would report nine inert components on every
    # daily run (bulk metrics are only pulled in the weekly/monthly branches),
    # raising a permanent false alarm and burying the genuinely-inert PE factor
    # that experiment pe_restoration_full_2026_08 tracks. "We did not look" must
    # never render as "we looked and it is dead" (2026-08-07).
    if metrics_by_symbol is None:
        na_factor = {"input_coverage": None, "field_resolution": None,
                     "eligible": None, "score_nonzero_count": None,
                     "score_variance": None, "status": "not_assessable"}
        return {
            "observe_only": True, "status": "not_assessable",
            "factors": {k: dict(na_factor) for k in _FACTOR_KEYS},
            "filters": {k: {"evaluable": None, "rejections": None,
                            "status": "not_assessable"} for k in _FILTER_KEYS},
            "inert_components": [], "suppresses_sleeve": False,
            "reasons": ["metrics_not_fetched_this_cadence"],
        }

    eligible = [str(s) for s in eligible_symbols if s] \
        if isinstance(eligible_symbols, (list, tuple, set)) else []
    metrics = metrics_by_symbol if isinstance(metrics_by_symbol, dict) else {}
    quotes = quotes_by_symbol if isinstance(quotes_by_symbol, dict) else {}
    cand_rows = [c for c in (candidates or []) if isinstance(c, dict)] \
        if isinstance(candidates, (list, tuple)) else []
    n = len(eligible)

    if not n:
        unknown = {"input_coverage": None, "field_resolution": 0, "eligible": 0,
                   "score_nonzero_count": 0, "score_variance": 0.0,
                   "status": "unknown"}
        return {
            "observe_only": True, "status": "unknown",
            "factors": {k: dict(unknown) for k in
                        ("revenue_growth", "fcf_yield", "roe", "pe", "trend")},
            "filters": {k: {"evaluable": 0, "rejections": 0, "status": "unknown"}
                        for k in ("rev_growth_min", "pe_bubble_guard",
                                  "fcf_negative_guard", "trend_200dma")},
            "inert_components": [], "suppresses_sleeve": False,
            "reasons": ["no_eligible_universe"],
        }

    field_of = {"revenue_growth": "revenueGrowth", "fcf_yield": "freeCashFlowYield",
                "roe": "roe", "pe": "peRatio"}
    resolution = {k: 0 for k in field_of}
    contributions: dict[str, list[float]] = {k: [] for k in
                                             ("revenue_growth", "fcf_yield", "roe", "pe", "trend")}
    trend_input = 0
    guard = {"rev_growth_min": [0, 0], "pe_bubble_guard": [0, 0],
             "fcf_negative_guard": [0, 0], "trend_200dma": [0, 0]}
    negative_pe_passing = 0

    for symbol in eligible:
        m = metrics.get(symbol) if isinstance(metrics.get(symbol), dict) else {}
        q = quotes.get(symbol) if isinstance(quotes.get(symbol), dict) else {}

        for name, field in field_of.items():
            if _is_present(m.get(field)):
                resolution[name] += 1

        px = q.get("price")
        dma = q.get("priceAvg200")
        if _is_present(px) and _is_present(dma) and float(dma) > 0:
            trend_input += 1

        bd = score_breakdown(m, q, min_rev_growth=min_rev_growth)
        for name, pts in bd.items():
            contributions[name].append(pts)

        # Hard filters: evaluable = the input needed for the condition exists.
        if _is_present(m.get("revenueGrowth")):
            guard["rev_growth_min"][0] += 1
            if float(m["revenueGrowth"]) < min_rev_growth:
                guard["rev_growth_min"][1] += 1
        if _is_present(m.get("peRatio")):
            guard["pe_bubble_guard"][0] += 1
            pe_val = float(m["peRatio"])
            if pe_val > 50:
                guard["pe_bubble_guard"][1] += 1
            elif pe_val <= 0:
                # `pe > 50` PASSES a negative PE, i.e. a loss-making company is
                # admitted by a guard meant to exclude expensive ones.
                negative_pe_passing += 1
        if _is_present(m.get("freeCashFlowYield")):
            guard["fcf_negative_guard"][0] += 1
            if float(m["freeCashFlowYield"]) < 0:
                guard["fcf_negative_guard"][1] += 1
        if trend_filter_enabled and _is_present(px) and _is_present(dma) and float(dma) > 0:
            guard["trend_200dma"][0] += 1
            if float(px) < float(dma):
                guard["trend_200dma"][1] += 1

    factors: dict[str, Any] = {}
    for name in contributions:
        if name == "trend":
            resolved = trend_input
            applicable = True
        else:
            resolved = resolution[name]
            applicable = True
        coverage = resolved / n
        pts = contributions[name]
        nonzero = sum(1 for p in pts if p > 0)
        var = round(_variance(pts), 6)
        factors[name] = {
            "input_field": field_of.get(name, "price+priceAvg200"),
            "eligible": n,
            "field_resolution": resolved,
            "input_coverage": round(coverage, 4),
            "score_nonzero_count": nonzero,
            "score_variance": var,
            "max_points": {"revenue_growth": 30.0, "fcf_yield": 25.0, "roe": 20.0,
                           "pe": 15.0, "trend": 10.0}[name],
            "candidate_count": len(cand_rows),
            "status": _factor_status(coverage, var, nonzero, applicable),
        }

    filters: dict[str, Any] = {}
    for name, (evaluable, rejections) in guard.items():
        if name == "trend_200dma" and not trend_filter_enabled:
            status = "not_applicable"
        elif evaluable == 0:
            status = "inert"
        elif evaluable < n * FACTOR_LIVE_MIN_COVERAGE:
            status = "degraded"
        else:
            status = "live"
        entry = {"eligible": n, "evaluable": evaluable, "rejections": rejections,
                 "status": status}
        if name == "pe_bubble_guard":
            entry["negative_earnings_passing"] = negative_pe_passing
        filters[name] = entry

    inert = sorted([k for k, v in factors.items() if v["status"] == "inert"]
                   + [k for k, v in filters.items() if v["status"] == "inert"])
    degraded = [k for k, v in {**factors, **filters}.items()
                if v["status"] in ("degraded", "low_information")]
    reasons = ([f"inert:{','.join(inert)}"] if inert else []) + \
              ([f"degraded:{','.join(sorted(degraded))}"] if degraded else [])
    status = "degraded" if (inert or degraded) else "live"

    return {
        "observe_only": True,
        "status": status,
        "factors": factors,
        "filters": filters,
        "inert_components": inert,
        # Explicitly NOT a suppression input: see docstring.
        "suppresses_sleeve": False,
        "reasons": reasons,
    }
