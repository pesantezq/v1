"""Shared adversarial-probe assertion helpers (Phase E5 / WS17).

Every helper here raises ``AssertionError`` directly (rather than returning a
bool) so a probe can call ``assertions.assert_x(...)`` as a plain statement.
Each one encodes a general, reusable form of ONE false-GREEN failure shape
confirmed in this repo on 2026-07-28
(``docs/reliability-program/2026-07-28-findings.md``):

  assert_meaningful_population           -- F13.1 (key-mismatch => 0 candidates)
  assert_nonzero_variance                -- F9.1  (55% tied ranking)
  assert_artifact_fresh_for_session      -- F8.2  (flat wall-clock freshness)
  assert_decision_consumer_parity        -- F-mobile-memo-panel class
  assert_oos_evidence_supported          -- F2.1  (`is False` never matches None)
  assert_no_single_block_controls_result -- F2.3/one_fold_controls_result
  assert_fail_closed_on_denial_state_corruption -- F10.1/F11.1 (audit/revocation logs)
  assert_no_quality_screen_mislabeling   -- F15.1 (MARA bypass mislabeling)

These are intentionally generic: they take plain values/dicts/callables, not
repo-specific types, so a FUTURE module can reuse them (per CLAUDE.md's
"Analysis + Health Coverage Requirement" -- every new producer needs a
health/adversarial check; these helpers are the reusable building blocks).
"""
from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence


# ---------------------------------------------------------------------------
# 1. assert_meaningful_population
# ---------------------------------------------------------------------------

def assert_meaningful_population(
    records: Sequence[Any] | None,
    *,
    min_count: int = 1,
    predicate: Callable[[Any], bool] | None = None,
    context: str = "",
) -> None:
    """Fail unless *records* exists and contains >= min_count entries that
    satisfy *predicate* (default: any non-None entry).

    Guards against the F13.1 shape of defect: a producer/consumer wiring that
    "runs successfully" (no exception, artifact present, schema valid) while
    silently carrying zero real records -- e.g. a container-key mismatch, an
    empty upstream feed, or an over-eager filter. An artifact that looks
    fresh but is functionally empty must not read as healthy.
    """
    predicate = predicate or (lambda r: r is not None)
    if records is None:
        raise AssertionError(
            f"{context}: population is None (expected >= {min_count} real record(s))")
    real = [r for r in records if predicate(r)]
    if len(real) < min_count:
        raise AssertionError(
            f"{context}: only {len(real)} real record(s) out of {len(records)} total "
            f"(need >= {min_count}) -- artifact exists but is functionally empty")


# ---------------------------------------------------------------------------
# 2. assert_nonzero_variance
# ---------------------------------------------------------------------------

def assert_nonzero_variance(
    values: Sequence[Any],
    *,
    min_distinct: int = 2,
    min_sample: int = 1,
    context: str = "",
) -> None:
    """Fail if a ranking/score column has fewer than *min_distinct* distinct
    values across >= min_sample samples -- i.e. it cannot discriminate.

    Guards against the F9.1 shape of defect: a ranking that "ran" and
    produced N ordered rows where the scores in fact all tie and the
    apparent order is a silent, content-free tiebreak (alphabetical or
    otherwise). Fewer than *min_sample* values present is not this probe's
    concern (returns silently -- a separate population check owns that).
    """
    vals = [v for v in values if v is not None]
    if len(vals) < min_sample:
        return
    distinct = len(set(vals))
    if distinct < min_distinct:
        raise AssertionError(
            f"{context}: only {distinct} distinct value(s) across {len(vals)} sample(s) "
            f"(need >= {min_distinct}) -- ranking/score column does not discriminate")


# ---------------------------------------------------------------------------
# 3. assert_artifact_fresh_for_session
# ---------------------------------------------------------------------------

def _completed_weekday_sessions_between(ts: datetime, now: datetime) -> int:
    """Count distinct weekdays (Mon-Fri) strictly after ts's date, up to and
    including now's date -- a minimal stand-in for a real NYSE session
    calendar (no holiday awareness; the one calendar-aware set that exists
    today, ``resolution_due_probe._NYSE_HOLIDAYS``, is not reused here on
    purpose -- this helper is deliberately self-contained test-only logic,
    not a production dependency)."""
    if now < ts:
        return 0
    cur = ts.date()
    end = now.date()
    count = 0
    while cur < end:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            count += 1
    return count


def assert_artifact_fresh_for_session(
    generated_at: str | None,
    *,
    now: datetime,
    max_sessions_stale: int = 1,
    context: str = "",
) -> None:
    """Fail unless *generated_at* falls within the last *max_sessions_stale*
    completed weekday sessions of *now* -- a calendar-aware freshness check,
    not a flat wall-clock window.

    Guards against the F8.2 shape of defect: a flat N-hour wall-clock
    freshness window has no notion of "last completed market session," so it
    either misclassifies Friday's data as stale every Monday, or -- worse --
    has no per-consumer cadence concept at all and cannot tell "one session
    old" from "N sessions old" for a weekday-only producer.
    """
    if not generated_at:
        raise AssertionError(f"{context}: no generated_at/data_as_of timestamp present")
    try:
        ts = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except Exception as exc:
        raise AssertionError(f"{context}: unparsable timestamp {generated_at!r} ({exc})")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    sessions_elapsed = _completed_weekday_sessions_between(ts, now)
    if sessions_elapsed > max_sessions_stale:
        raise AssertionError(
            f"{context}: artifact generated_at={generated_at} is {sessions_elapsed} "
            f"completed weekday session(s) old as of {now.isoformat()} "
            f"(allowed <= {max_sessions_stale})")


# ---------------------------------------------------------------------------
# 4. assert_decision_consumer_parity
# ---------------------------------------------------------------------------

def assert_decision_consumer_parity(
    *,
    decision_value: Any,
    consumer_values: dict[str, Any],
    context: str = "",
) -> None:
    """Fail if any named consumer's value disagrees with the decision
    artifact's value. Memo, GUI, and ``decision_plan.json`` must present ONE
    story (CLAUDE.md: "GUI, memo, and explanation layers are artifact
    consumers only" -- consumers must AGREE with the source of truth, not
    just each independently "run successfully"). Equality is exact; callers
    normalize/round before calling.
    """
    mismatches = {name: v for name, v in consumer_values.items() if v != decision_value}
    if mismatches:
        raise AssertionError(
            f"{context}: decision value {decision_value!r} disagrees with "
            f"consumer(s) {mismatches!r}")


def extract_literal_header_calls(module: Any, helper_name: str = "h") -> list[str]:
    """Statically extract every literal string passed to a local
    ``h("...")``-style header-emission helper inside *module*'s source, via
    an AST scan (no execution required, so it works regardless of which
    branches a fixture happens to hit).

    General-purpose: reusable against any renderer in this repo that follows
    the ``def h(title): out.append(f"## {title}")`` local-closure convention
    (e.g. ``capital_plan_view.render_capital_plan_md``) to get the FULL set
    of headers a renderer can ever emit, rather than a hand-maintained
    literal list that can silently go stale (the exact fragility a prior
    fixture for this pattern exhibited -- see
    ``tests/test_gui_dashboard_memo.py``'s ``SHIPPED_CAPITAL_PLAN_HEADERS``).
    """
    src = inspect.getsource(module)
    tree = ast.parse(src)
    out: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == helper_name and node.args):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append(arg.value)
    return out


# ---------------------------------------------------------------------------
# 5. assert_oos_evidence_supported
# ---------------------------------------------------------------------------

_UNSUPPORTED_STATES = ("OOS_NOT_TESTED", "OOS_DATA_BLOCKED", "OOS_INSUFFICIENT", None)


def assert_oos_evidence_supported(
    evidence: dict[str, Any] | None,
    *,
    min_folds: int,
    state_key: str = "state",
    folds_key: str = "folds",
    supported_value: str = "OOS_SUPPORTED",
    failed_value: str = "OOS_FAILED",
    context: str = "",
) -> None:
    """Fail unless *evidence* is a structured dict carrying an explicit state
    plus >= min_folds of fold-level support -- NOT a bare boolean/None.

    Guards against the F2.1 shape of defect: ``still_works_oos: null`` (never
    tested) must never be treated as "passed" just because it also isn't
    literally ``False``.
    """
    if not isinstance(evidence, dict):
        raise AssertionError(
            f"{context}: OOS evidence is {type(evidence).__name__}, not a structured "
            "evidence dict -- a bare boolean/None cannot support a validity claim")
    state = evidence.get(state_key)
    if state in _UNSUPPORTED_STATES:
        raise AssertionError(
            f"{context}: OOS state is {state!r} -- absence of failure is not evidence "
            "of validity")
    if state == failed_value:
        raise AssertionError(f"{context}: OOS state is {failed_value} -- explicitly not supported")
    folds = evidence.get(folds_key)
    if folds is None or folds < min_folds:
        raise AssertionError(
            f"{context}: only {folds!r} fold(s) of OOS evidence (need >= {min_folds}) -- "
            f"state={state!r} is not backed by sufficient folds")
    if state != supported_value:
        raise AssertionError(
            f"{context}: OOS state {state!r} is not {supported_value!r} -- not evidence "
            "of a validated result")


# ---------------------------------------------------------------------------
# 6. assert_no_single_block_controls_result
# ---------------------------------------------------------------------------

def assert_no_single_block_controls_result(
    block_contributions: Sequence[float],
    *,
    max_share: float = 0.5,
    context: str = "",
) -> None:
    """Fail if one fold/observation/week's |magnitude| accounts for more
    than *max_share* of the sum of |magnitude| across all blocks -- an
    aggregate "pass" resting on one extreme block is fragile, not supported
    (mirrors ``oos_state.py``'s ``ONE_FOLD_DOMINANCE_SHARE`` rule).

    Fewer than 2 blocks, or a zero total, cannot be judged by this rule and
    return silently.
    """
    mags = [abs(float(v)) for v in block_contributions]
    total = sum(mags)
    if total <= 0 or len(mags) < 2:
        return
    largest_share = max(mags) / total
    if largest_share > max_share:
        raise AssertionError(
            f"{context}: one block contributes {largest_share:.1%} of the aggregate "
            f"(allowed <= {max_share:.0%}) -- result is controlled by a single observation")


# ---------------------------------------------------------------------------
# 7. assert_fail_closed_on_denial_state_corruption
# ---------------------------------------------------------------------------

def assert_fail_closed_on_denial_state_corruption(
    *,
    unreadable_check: Callable[[], str | None],
    corrupt_writer: Callable[[], None],
    torn_tail_writer: Callable[[], None] | None = None,
    context: str = "",
) -> None:
    """Verify a DENIAL-list guard (revocation/approval/audit log) fails
    CLOSED on total corruption, and -- if *torn_tail_writer* is given --
    tolerates a merely-torn trailing line (the last-writer-crashed case,
    which must NOT be conflated with total corruption or every crash-mid-
    write becomes an outage).

    Calls *corrupt_writer* then asserts *unreadable_check* returns a
    non-empty reason (fail-closed). If *torn_tail_writer* is given, calls it
    and asserts *unreadable_check* then returns None (tolerated).
    """
    corrupt_writer()
    reason = unreadable_check()
    if not reason:
        raise AssertionError(
            f"{context}: total corruption was NOT classified unreadable -- fails OPEN "
            "(silently proceeds) instead of fail-closed")
    if torn_tail_writer is not None:
        torn_tail_writer()
        reason2 = unreadable_check()
        if reason2:
            raise AssertionError(
                f"{context}: a merely torn trailing line was classified unreadable "
                f"({reason2!r}) -- over-strict, conflates a crash-mid-write artifact "
                "with total corruption")


# ---------------------------------------------------------------------------
# 8. assert_no_quality_screen_mislabeling
# ---------------------------------------------------------------------------

_SCREENED_LABELS = ("screened", "passed_screen", "screened_and_passed")


def assert_no_quality_screen_mislabeling(
    *,
    watchlist_source: str | None,
    screened_filters: Sequence[str] | None,
    label: str | None,
    context: str = "",
) -> None:
    """Fail if a candidate whose provenance bypassed real screening
    (``watchlist_source in {None, "fallback", "static"}`` with no recorded
    ``screened_filters``) is nonetheless labelled as having passed
    screening.

    Guards against F15.1: provenance records WHERE a symbol came from but
    never WHICH filters ran or passed -- nothing today distinguishes
    "screened and passed" from "never screened".
    """
    bypassed = (watchlist_source in (None, "fallback", "static")) and not screened_filters
    if bypassed and label in _SCREENED_LABELS:
        raise AssertionError(
            f"{context}: watchlist_source={watchlist_source!r} bypassed real screening "
            f"(screened_filters={screened_filters!r}) but is labelled {label!r}")
