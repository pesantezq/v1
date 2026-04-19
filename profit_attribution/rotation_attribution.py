"""
Profit Attribution — Rotation Attribution
==========================================
Observe-only analysis of rotation decision quality over time.

Reads rotation_events.jsonl (written by rotation_event_logger) and
computes summary metrics to evaluate whether momentum and compounder
rotation decisions are beneficial in practice.

Emits a RotationAttributionSummary with:
  - Overall rotation frequency and trigger rate
  - Margin-band analysis (below / near / moderate / strong threshold)
  - Strategy-type comparison (momentum vs compounder)
  - Challenger-freshness comparison (breakout vs non-breakout)
  - Observe-only recommendations

Pure computation with file loading — no live decision logic is modified.
observe_only is always True.

Public API:
  evaluate_rotation_attribution(history_path) -> RotationAttributionSummary
  write_rotation_reports(summary, policy_dir, dry_run) -> bool
  build_rotation_memo(summary) -> str
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger("profit_attribution.rotation_attribution")

DEFAULT_ROTATION_EVENTS_PATH = Path("outputs/policy/rotation_events.jsonl")
_DEFAULT_POLICY_DIR = Path("outputs/policy")

# Minimum events for recommendations
MIN_EVENTS_FOR_RECOMMENDATION: int = 10
SMALL_SAMPLE: int = 5

# Margin bands: relative gap above required_margin
_BAND_NEAR_UPPER: float = 4.0     # near_threshold = [0, +4)
_BAND_MODERATE_UPPER: float = 10.0  # moderate      = [+4, +10), strong = [+10, ∞)

# Recommendation heuristics
_NEAR_THRESHOLD_CHURN_RATE: float = 0.50   # >50% of triggered = near-threshold → flag
_CHURN_RATE_DIVERGENCE: float = 0.20       # momentum trigger_rate > compounder + 20pp → flag
_WIN_RATE_UNDERPERFORM: float = 0.10       # near-threshold win rate < overall − 10pp → flag
_BREAKOUT_WIN_RATE_GAP: float = 0.10       # breakout win rate < non_breakout − 10pp → flag


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class MarginBandSummary:
    """Rotation quality summary for one margin-size bucket."""
    band_label: str        # "below_threshold" | "near_threshold" | "moderate" | "strong"
    band_range: str        # human-readable range string
    total_events: int = 0
    triggered_count: int = 0
    with_outcome: int = 0  # events with forward_return_5d resolved
    win_count: int = 0
    returns_5d: List[float] = field(default_factory=list)
    small_sample: bool = False

    @property
    def trigger_rate(self) -> Optional[float]:
        if self.total_events == 0:
            return None
        return round(self.triggered_count / self.total_events, 4)

    @property
    def win_rate(self) -> Optional[float]:
        if not self.returns_5d:
            return None
        return round(self.win_count / len(self.returns_5d), 4)

    @property
    def avg_return_5d(self) -> Optional[float]:
        if not self.returns_5d:
            return None
        return round(sum(self.returns_5d) / len(self.returns_5d), 6)

    def to_dict(self) -> dict:
        return {
            "band_label": self.band_label,
            "band_range": self.band_range,
            "total_events": self.total_events,
            "triggered_count": self.triggered_count,
            "trigger_rate": self.trigger_rate,
            "with_outcome": self.with_outcome,
            "win_rate": self.win_rate,
            "avg_return_5d": self.avg_return_5d,
            "small_sample": self.small_sample,
        }


@dataclass
class StrategyRotationSummary:
    """Rotation frequency and outcome summary for one grouping dimension."""
    label: str            # e.g. "momentum" | "compounder" | "breakout" | "non_breakout"
    dimension: str        # "strategy_type" | "challenger_type"
    total_events: int = 0
    triggered_count: int = 0
    near_threshold_count: int = 0  # triggered AND in near_threshold band
    with_outcome: int = 0
    win_count: int = 0
    returns_5d: List[float] = field(default_factory=list)
    margins: List[float] = field(default_factory=list)   # actual_margin, triggered events only
    small_sample: bool = False

    @property
    def trigger_rate(self) -> Optional[float]:
        if self.total_events == 0:
            return None
        return round(self.triggered_count / self.total_events, 4)

    @property
    def near_threshold_pct(self) -> Optional[float]:
        if self.triggered_count == 0:
            return None
        return round(self.near_threshold_count / self.triggered_count, 4)

    @property
    def avg_actual_margin(self) -> Optional[float]:
        if not self.margins:
            return None
        return round(sum(self.margins) / len(self.margins), 4)

    @property
    def win_rate(self) -> Optional[float]:
        if not self.returns_5d:
            return None
        return round(self.win_count / len(self.returns_5d), 4)

    @property
    def avg_return_5d(self) -> Optional[float]:
        if not self.returns_5d:
            return None
        return round(sum(self.returns_5d) / len(self.returns_5d), 6)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "dimension": self.dimension,
            "total_events": self.total_events,
            "triggered_count": self.triggered_count,
            "trigger_rate": self.trigger_rate,
            "near_threshold_pct": self.near_threshold_pct,
            "avg_actual_margin": self.avg_actual_margin,
            "with_outcome": self.with_outcome,
            "win_rate": self.win_rate,
            "avg_return_5d": self.avg_return_5d,
            "small_sample": self.small_sample,
        }


@dataclass
class RotationAttributionSummary:
    """
    Full observe-only rotation attribution result.

    Always a valid object — degrades gracefully with zero records.
    observe_only is always True.
    """
    observe_only: bool          # always True — safety marker
    generated_at: str
    history_path: str
    total_events: int
    total_triggered: int
    by_strategy_type: List[StrategyRotationSummary]
    by_margin_band: List[MarginBandSummary]
    by_challenger_type: List[StrategyRotationSummary]
    recommendation: str
    recommendation_reason: str
    data_quality_notes: List[str]

    @property
    def trigger_rate(self) -> Optional[float]:
        if self.total_events == 0:
            return None
        return round(self.total_triggered / self.total_events, 4)

    def to_dict(self) -> dict:
        return {
            "observe_only": self.observe_only,
            "generated_at": self.generated_at,
            "history_path": self.history_path,
            "total_events": self.total_events,
            "total_triggered": self.total_triggered,
            "trigger_rate": self.trigger_rate,
            "by_strategy_type": [s.to_dict() for s in self.by_strategy_type],
            "by_margin_band": [b.to_dict() for b in self.by_margin_band],
            "by_challenger_type": [c.to_dict() for c in self.by_challenger_type],
            "recommendation": self.recommendation,
            "recommendation_reason": self.recommendation_reason,
            "data_quality_notes": self.data_quality_notes,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_rotation_attribution(
    history_path: Optional[Path] = None,
) -> RotationAttributionSummary:
    """
    Evaluate rotation decision quality from historical rotation events.

    Read-only: loads rotation_events.jsonl.
    Always returns a valid object — degrades gracefully on missing data.
    Never modifies any live decision logic or config.

    Args:
        history_path: Override default rotation_events.jsonl path.

    Returns:
        RotationAttributionSummary with observe_only=True always.
    """
    now_str = datetime.now().isoformat()
    path_str = str(history_path or DEFAULT_ROTATION_EVENTS_PATH)
    notes: List[str] = []

    events = _load_events(history_path)

    if not events:
        return _empty_summary(
            now_str, path_str,
            ["No rotation events found. Call append_rotation_events() after evaluate_exit() "
             "to begin accumulating history."],
        )

    total = len(events)
    total_triggered = sum(1 for e in events if e.get("rotation_triggered", False))

    by_strategy = _group_by_strategy(events)
    by_band = _group_by_margin_band(events)
    by_challenger = _group_by_challenger_type(events)

    recommendation, reason = _make_recommendation(
        events=events,
        total_triggered=total_triggered,
        by_strategy=by_strategy,
        by_band=by_band,
        by_challenger=by_challenger,
    )

    if total < MIN_EVENTS_FOR_RECOMMENDATION:
        notes.append(
            f"Small history ({total} events). Recommendations improve with "
            f"≥{MIN_EVENTS_FOR_RECOMMENDATION} events."
        )

    resolved = sum(1 for e in events if e.get("outcome_resolved", False))
    if resolved == 0 and total > 0:
        notes.append(
            "No forward outcomes resolved yet. Win-rate analysis becomes available "
            "after outcome enrichment."
        )

    return RotationAttributionSummary(
        observe_only=True,
        generated_at=now_str,
        history_path=path_str,
        total_events=total,
        total_triggered=total_triggered,
        by_strategy_type=by_strategy,
        by_margin_band=by_band,
        by_challenger_type=by_challenger,
        recommendation=recommendation,
        recommendation_reason=reason,
        data_quality_notes=notes,
    )


def write_rotation_reports(
    summary: RotationAttributionSummary,
    policy_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> bool:
    """
    Write rotation_attribution.json and rotation_attribution.md to disk.

    Args:
        summary:    Output from evaluate_rotation_attribution().
        policy_dir: Override default output directory.
        dry_run:    If True, build content but skip writing.

    Returns:
        True on success (or dry_run), False if any write failed.
    """
    import json as _json
    out_dir = Path(policy_dir) if policy_dir else _DEFAULT_POLICY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        logger.debug("rotation_attribution: dry_run — skipping writes")
        return True

    ok = True

    json_path = out_dir / "rotation_attribution.json"
    try:
        json_path.write_text(_json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
        logger.info("rotation_attribution: wrote %s", json_path)
    except OSError as exc:
        logger.warning("rotation_attribution: JSON write failed: %s", exc)
        ok = False

    md_path = out_dir / "rotation_attribution.md"
    try:
        md_path.write_text(_build_markdown(summary), encoding="utf-8")
        logger.info("rotation_attribution: wrote %s", md_path)
    except OSError as exc:
        logger.warning("rotation_attribution: MD write failed: %s", exc)
        ok = False

    return ok


def build_rotation_memo(summary: RotationAttributionSummary) -> str:
    """Build a short (4–6 line) plain-text memo for email digests."""
    lines = ["[Rotation Attribution]"]

    if summary.total_events == 0:
        lines.append("  No rotation events recorded yet.")
        return "\n".join(lines)

    tr = summary.trigger_rate
    tr_str = f"{tr * 100:.0f}%" if tr is not None else "—"
    lines.append(
        f"  {summary.total_triggered}/{summary.total_events} evaluations triggered "
        f"({tr_str} rotation rate)"
    )

    near = next((b for b in summary.by_margin_band if b.band_label == "near_threshold"), None)
    if near and near.triggered_count > 0 and summary.total_triggered > 0:
        near_pct = near.triggered_count / summary.total_triggered
        lines.append(
            f"  Near-threshold rotations: {near.triggered_count} ({near_pct:.0%} of triggered)"
        )

    m_s = next((s for s in summary.by_strategy_type if s.label == "momentum"), None)
    c_s = next((s for s in summary.by_strategy_type if s.label == "compounder"), None)
    if m_s and c_s:
        lines.append(
            f"  Strategy rate: momentum {_pct(m_s.trigger_rate)} | "
            f"compounder {_pct(c_s.trigger_rate)}"
        )

    rec = summary.recommendation
    if "balanced" not in rec.lower() and "no rotation" not in rec.lower():
        lines.append(f"  Advisory: {rec[:100]}")

    if summary.data_quality_notes:
        lines.append(f"  Note: {summary.data_quality_notes[0]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Margin band logic
# ---------------------------------------------------------------------------

def assign_margin_band(actual_margin: float, required_margin: float) -> str:
    """
    Assign a margin-quality band based on how far the actual margin exceeds
    the required threshold.

    Bands (gap = actual_margin − required_margin):
      below_threshold : gap < 0      (not triggered)
      near_threshold  : 0 ≤ gap < 4  (triggered, barely)
      moderate        : 4 ≤ gap < 10
      strong          : gap ≥ 10
    """
    gap = actual_margin - required_margin
    if gap < 0:
        return "below_threshold"
    if gap < _BAND_NEAR_UPPER:
        return "near_threshold"
    if gap < _BAND_MODERATE_UPPER:
        return "moderate"
    return "strong"


# ---------------------------------------------------------------------------
# Internal grouping helpers
# ---------------------------------------------------------------------------

def _load_events(history_path: Optional[Path]) -> List[dict]:
    src = history_path or DEFAULT_ROTATION_EVENTS_PATH
    if not Path(src).exists():
        return []
    events: List[dict] = []
    try:
        with open(src, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    events.append(json.loads(text))
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "rotation_attribution: bad JSONL line %d — %s", lineno, exc
                    )
    except Exception as exc:
        logger.warning("rotation_attribution: failed reading %s — %s", src, exc)
        return []
    return events


def _group_by_strategy(events: List[dict]) -> List[StrategyRotationSummary]:
    buckets: dict[str, StrategyRotationSummary] = {}

    for e in events:
        st = str(e.get("strategy_type") or "unknown")
        if st not in buckets:
            buckets[st] = StrategyRotationSummary(label=st, dimension="strategy_type")
        b = buckets[st]
        b.total_events += 1
        triggered = bool(e.get("rotation_triggered", False))
        actual = float(e.get("actual_margin") or 0.0)
        required = float(e.get("required_margin") or 0.0)
        if triggered:
            b.triggered_count += 1
            b.margins.append(actual)
            if assign_margin_band(actual, required) == "near_threshold":
                b.near_threshold_count += 1
        r5 = e.get("forward_return_5d")
        if r5 is not None:
            b.with_outcome += 1
            r5f = float(r5)
            b.returns_5d.append(r5f)
            if r5f > 0:
                b.win_count += 1

    for b in buckets.values():
        b.small_sample = b.total_events < SMALL_SAMPLE

    return sorted(buckets.values(), key=lambda b: b.label)


def _group_by_margin_band(events: List[dict]) -> List[MarginBandSummary]:
    bands: dict[str, MarginBandSummary] = {
        "below_threshold": MarginBandSummary(
            "below_threshold", "below required threshold (not triggered)"
        ),
        "near_threshold": MarginBandSummary(
            "near_threshold", "+0 to +4 pts above required"
        ),
        "moderate": MarginBandSummary(
            "moderate", "+4 to +10 pts above required"
        ),
        "strong": MarginBandSummary(
            "strong", "+10+ pts above required"
        ),
    }

    for e in events:
        actual = float(e.get("actual_margin") or 0.0)
        required = float(e.get("required_margin") or 0.0)
        triggered = bool(e.get("rotation_triggered", False))
        band_key = assign_margin_band(actual, required)
        b = bands[band_key]
        b.total_events += 1
        if triggered:
            b.triggered_count += 1
        r5 = e.get("forward_return_5d")
        if r5 is not None:
            b.with_outcome += 1
            r5f = float(r5)
            b.returns_5d.append(r5f)
            if r5f > 0:
                b.win_count += 1

    for b in bands.values():
        b.small_sample = b.total_events < SMALL_SAMPLE

    return [bands[k] for k in ("below_threshold", "near_threshold", "moderate", "strong")]


def _group_by_challenger_type(events: List[dict]) -> List[StrategyRotationSummary]:
    buckets: dict[str, StrategyRotationSummary] = {
        "breakout": StrategyRotationSummary(label="breakout", dimension="challenger_type"),
        "non_breakout": StrategyRotationSummary(label="non_breakout", dimension="challenger_type"),
    }

    for e in events:
        is_breakout = bool(e.get("challenger_is_breakout", False))
        key = "breakout" if is_breakout else "non_breakout"
        b = buckets[key]
        b.total_events += 1
        triggered = bool(e.get("rotation_triggered", False))
        actual = float(e.get("actual_margin") or 0.0)
        required = float(e.get("required_margin") or 0.0)
        if triggered:
            b.triggered_count += 1
            b.margins.append(actual)
            if assign_margin_band(actual, required) == "near_threshold":
                b.near_threshold_count += 1
        r5 = e.get("forward_return_5d")
        if r5 is not None:
            b.with_outcome += 1
            r5f = float(r5)
            b.returns_5d.append(r5f)
            if r5f > 0:
                b.win_count += 1

    for b in buckets.values():
        b.small_sample = b.total_events < SMALL_SAMPLE

    return [buckets["breakout"], buckets["non_breakout"]]


# ---------------------------------------------------------------------------
# Recommendation logic
# ---------------------------------------------------------------------------

def _make_recommendation(
    events: List[dict],
    total_triggered: int,
    by_strategy: List[StrategyRotationSummary],
    by_band: List[MarginBandSummary],
    by_challenger: List[StrategyRotationSummary],
) -> tuple[str, str]:
    total = len(events)

    if total < MIN_EVENTS_FOR_RECOMMENDATION:
        return (
            "No recommendation — insufficient rotation history.",
            f"need ≥{MIN_EVENTS_FOR_RECOMMENDATION} events (have {total})",
        )

    if total_triggered == 0:
        return (
            "No rotations have triggered yet. Continue monitoring rotation evaluations.",
            "rotation_triggered=False for all evaluated events",
        )

    issues: list[str] = []
    parts: list[str] = []

    # Issue 1: near-threshold proportion of triggered rotations
    near_band = next((b for b in by_band if b.band_label == "near_threshold"), None)
    near_triggered = near_band.triggered_count if near_band else 0
    near_pct = near_triggered / total_triggered if total_triggered > 0 else 0.0

    if near_pct > _NEAR_THRESHOLD_CHURN_RATE:
        issues.append(f"near_threshold_pct={near_pct:.0%}")
        parts.append(
            f"Near-threshold rotations represent {near_pct:.0%} of all triggered events. "
            "A high near-threshold rate may indicate the rotation threshold is set too low; "
            "consider reviewing the rotation gap configuration later."
        )

    # Issue 2: momentum churn rate vs compounder
    m_sum = next((s for s in by_strategy if s.label == "momentum"), None)
    c_sum = next((s for s in by_strategy if s.label == "compounder"), None)
    m_rate = m_sum.trigger_rate if m_sum else None
    c_rate = c_sum.trigger_rate if c_sum else None

    if m_rate is not None and c_rate is not None and m_rate > (c_rate + _CHURN_RATE_DIVERGENCE):
        issues.append(f"momentum_rate={m_rate:.2f}_vs_compounder={c_rate:.2f}")
        parts.append(
            f"Momentum rotation fires at {m_rate:.0%} vs compounder at {c_rate:.0%} — "
            "significantly higher churn in momentum positions."
        )

    # Issue 3: near-threshold outcome weakness (requires forward returns)
    overall_wr = _overall_win_rate(events)
    if near_band and near_band.win_rate is not None and overall_wr is not None:
        if near_band.win_rate < overall_wr - _WIN_RATE_UNDERPERFORM:
            issues.append(
                f"near_threshold_wr={near_band.win_rate:.2f}_lt_overall={overall_wr:.2f}"
            )
            parts.append(
                f"Near-threshold rotations underperform overall "
                f"({near_band.win_rate:.0%} vs {overall_wr:.0%} win rate). "
                "Review the rotation gap threshold later."
            )

    # Issue 4: breakout challenger quality (requires forward returns)
    b_sum = next((c for c in by_challenger if c.label == "breakout"), None)
    nb_sum = next((c for c in by_challenger if c.label == "non_breakout"), None)
    b_wr = b_sum.win_rate if b_sum else None
    nb_wr = nb_sum.win_rate if nb_sum else None

    if b_wr is not None and nb_wr is not None and b_wr < nb_wr - _BREAKOUT_WIN_RATE_GAP:
        issues.append(f"breakout_wr={b_wr:.2f}_lt_non_breakout={nb_wr:.2f}")
        parts.append(
            f"Breakout challengers underperform non-breakout challengers "
            f"({b_wr:.0%} vs {nb_wr:.0%}). "
            "Consider reviewing rotation logic for fresh-breakout challengers."
        )

    if not issues:
        return (
            "Rotation distribution appears balanced. Continue accumulating outcome data.",
            "no issues detected in frequency, margin distribution, or available outcome data",
        )

    recommendation = " ".join(parts)
    reason = "; ".join(issues)
    return recommendation, reason


def _overall_win_rate(events: List[dict]) -> Optional[float]:
    outcomes = [
        float(e["forward_return_5d"])
        for e in events
        if e.get("forward_return_5d") is not None
    ]
    if not outcomes:
        return None
    wins = sum(1 for r in outcomes if r > 0)
    return round(wins / len(outcomes), 4)


# ---------------------------------------------------------------------------
# Empty summary helper
# ---------------------------------------------------------------------------

def _empty_summary(
    now_str: str, path_str: str, notes: List[str]
) -> RotationAttributionSummary:
    return RotationAttributionSummary(
        observe_only=True,
        generated_at=now_str,
        history_path=path_str,
        total_events=0,
        total_triggered=0,
        by_strategy_type=[],
        by_margin_band=[
            MarginBandSummary("below_threshold", "below required threshold (not triggered)"),
            MarginBandSummary("near_threshold",  "+0 to +4 pts above required"),
            MarginBandSummary("moderate",        "+4 to +10 pts above required"),
            MarginBandSummary("strong",          "+10+ pts above required"),
        ],
        by_challenger_type=[
            StrategyRotationSummary(label="breakout",     dimension="challenger_type"),
            StrategyRotationSummary(label="non_breakout", dimension="challenger_type"),
        ],
        recommendation=(
            "No rotation events recorded yet. "
            "Rotation quality analysis requires event history."
        ),
        recommendation_reason="no events",
        data_quality_notes=notes,
    )


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_markdown(s: RotationAttributionSummary) -> str:
    lines = [
        "# Rotation Attribution Report",
        "",
        f"*Generated: {s.generated_at}*",
        "",
        "> *Observe-only advisory. "
        "This output does not modify any live decision logic or thresholds.*",
        "",
        "## Overview",
        "",
        "| Metric | Value |",
        "| ------ | ----- |",
        f"| Total rotation evaluations | {s.total_events} |",
        f"| Rotations triggered | {s.total_triggered} |",
        f"| Trigger rate | {_pct(s.trigger_rate)} |",
        "",
    ]

    if s.data_quality_notes:
        lines += ["### Data Quality Notes", ""]
        for note in s.data_quality_notes:
            lines.append(f"- {note}")
        lines.append("")

    # Margin band table
    lines += [
        "## Margin Band Analysis",
        "",
        "> *Margin = challenger\\_score − incumbent\\_score. "
        "Bands are relative to the required\\_margin threshold.*",
        "",
        "| Band | Range | Events | Triggered | Trigger Rate | "
        "With Outcome | Win Rate | Avg 5d Return | ⚠ |",
        "| ---- | ----- | ------ | --------- | ------------ | "
        "------------ | -------- | ------------- | - |",
    ]
    for b in s.by_margin_band:
        lines.append(
            f"| {b.band_label} "
            f"| {b.band_range} "
            f"| {b.total_events} "
            f"| {b.triggered_count} "
            f"| {_pct(b.trigger_rate)} "
            f"| {b.with_outcome} "
            f"| {_pct(b.win_rate)} "
            f"| {_pct(b.avg_return_5d)} "
            f"| {'⚠' if b.small_sample else ''} |"
        )
    lines += [
        "",
        "_Win rate and avg 5d return require forward-return enrichment "
        "(not available in initial data collection phase)._",
        "",
    ]

    # Strategy type breakdown
    if s.by_strategy_type:
        lines += [
            "## Rotation by Strategy Type",
            "",
            "| Strategy | Events | Triggered | Trigger Rate | "
            "Near-Threshold% | Avg Margin | With Outcome | Win Rate | ⚠ |",
            "| -------- | ------ | --------- | ------------ | "
            "--------------- | ---------- | ------------ | -------- | - |",
        ]
        for b in s.by_strategy_type:
            lines.append(
                f"| {b.label} "
                f"| {b.total_events} "
                f"| {b.triggered_count} "
                f"| {_pct(b.trigger_rate)} "
                f"| {_pct(b.near_threshold_pct)} "
                f"| {_score(b.avg_actual_margin)} "
                f"| {b.with_outcome} "
                f"| {_pct(b.win_rate)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    # Challenger type breakdown
    lines += [
        "## Rotation by Challenger Type",
        "",
        "| Type | Events | Triggered | Trigger Rate | Near-Threshold% | Win Rate | ⚠ |",
        "| ---- | ------ | --------- | ------------ | --------------- | -------- | - |",
    ]
    for b in s.by_challenger_type:
        lines.append(
            f"| {b.label} "
            f"| {b.total_events} "
            f"| {b.triggered_count} "
            f"| {_pct(b.trigger_rate)} "
            f"| {_pct(b.near_threshold_pct)} "
            f"| {_pct(b.win_rate)} "
            f"| {'⚠' if b.small_sample else ''} |"
        )
    lines.append("")

    # Observe-only recommendation
    lines += [
        "## Observe-Only Recommendation",
        "",
        f"**Recommendation:** {s.recommendation}",
    ]
    if s.recommendation_reason:
        lines.append(f"*Reason: {s.recommendation_reason}*")
    lines += [
        "",
        "> *This recommendation is non-binding and does not modify live rotation behavior.*",
        "",
        "---",
        "## Methodology",
        "",
        "| Parameter | Value |",
        "| --------- | ----- |",
        "| Data source | rotation\\_events.jsonl (from rotation\\_event\\_logger) |",
        f"| Margin bands | below\\_threshold / near\\_threshold (+0–+{_BAND_NEAR_UPPER:.0f}) "
        f"/ moderate (+{_BAND_NEAR_UPPER:.0f}–+{_BAND_NEAR_UPPER + _BAND_MODERATE_UPPER:.0f}) "
        f"/ strong (+{_BAND_NEAR_UPPER + _BAND_MODERATE_UPPER:.0f}+) |",
        f"| Near-threshold flag | >  {_NEAR_THRESHOLD_CHURN_RATE:.0%} of triggered events |",
        f"| Churn divergence flag | momentum trigger rate > compounder + {_CHURN_RATE_DIVERGENCE:.0%} |",
        f"| Outcome underperform flag | near-threshold win rate < overall − {_WIN_RATE_UNDERPERFORM:.0%} |",
        "| Small sample warning | < 5 events |",
        "",
        "Read-only evaluation layer — no live decision logic is modified.",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _pct(v: Any) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100:+.1f}%"


def _score(v: Any) -> str:
    if v is None:
        return "—"
    return f"{float(v):.1f}"
