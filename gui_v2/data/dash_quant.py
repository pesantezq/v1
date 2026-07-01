"""Quant cockpit — observe/proposal-only evidence view.

Composes normalized `shared.card(...)` cards from quantitative analysis
artifacts. Every card is labeled explicitly:
  - "Insufficient history" / "Thin sample" when n < threshold
  - "Proposal only" for retune / gate proposals
  - "Observe only" for quant_watch items
  - "Caution" for inverted/weak calibration
  - "Improving" / "Mixed" / "Weak" for efficacy trends

SAFETY: No buy/sell/hold/execute/trade language anywhere in this module.
All artifacts are observe-only or proposal-only evidence. Only
decision_plan / system_decision_summary represent official advisory
actions — those are NOT sourced here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gui_v2.data.shared import card, _read_json

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_THIN_SAMPLE_N = 30           # below this → "Thin sample"
_INSUFFICIENT_HISTORY_N = 10  # below this → "Insufficient history"
_CALIBRATION_GAP_CAUTION = 0.15  # above this gap → flag as caution
_CALIBRATION_TREND_EPS = 0.02     # |latest-earliest gap| within this → "Stable"
_BUCKET_OVERCONFIDENT_GAP = 0.10  # bucket gap beyond ±this → over/under-confident


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _n_label(n: int | None) -> str | None:
    """Return a sample-size caution label or None if sample is adequate."""
    if n is None:
        return "Insufficient history"
    if n < _INSUFFICIENT_HISTORY_N:
        return "Insufficient history"
    if n < _THIN_SAMPLE_N:
        return "Thin sample"
    return None


def _calibration_gap_history(root: Path) -> list[tuple[str, float]]:
    """Read overall_calibration_gap from each outputs/history/<date>/ snapshot.

    Returns a chronologically-sorted list of (date, gap). ISO date dir names
    sort lexically into chronological order. Observe-only; never raises.
    """
    hist_root = root / "outputs" / "history"
    series: list[tuple[str, float]] = []
    if not hist_root.is_dir():
        return series
    for snap in sorted(hist_root.glob("*/confidence_calibration.json")):
        data = _read_json(snap) or {}
        gap = data.get("overall_calibration_gap")
        if isinstance(gap, (int, float)):
            series.append((snap.parent.name, float(gap)))
    return series


def _bucket_confidence_annotation(buckets: list | None) -> str:
    """Summarize which confidence buckets are over/under-confident.

    A positive bucket calibration_gap means average confidence exceeds the
    realized hit rate (overconfident); negative means underconfident.
    """
    over: list[str] = []
    under: list[str] = []
    for b in buckets or []:
        if not isinstance(b, dict):
            continue
        gap = b.get("calibration_gap")
        label = b.get("label")
        if not isinstance(gap, (int, float)) or not label:
            continue
        if gap > _BUCKET_OVERCONFIDENT_GAP:
            over.append(str(label))
        elif gap < -_BUCKET_OVERCONFIDENT_GAP:
            under.append(str(label))
    parts: list[str] = []
    if over:
        parts.append(f"Overconfident buckets: {', '.join(over)}")
    if under:
        parts.append(f"Underconfident buckets: {', '.join(under)}")
    return "; ".join(parts) if parts else "Buckets within tolerance"


def _calibration_trend_card(calib: dict, history: list[tuple[str, float]]) -> dict:
    """Build the 'Calibration Trend' card from the gap history + latest buckets.

    Trend compares the earliest vs latest gap in the available history window:
    a shrinking gap is 'Improving' (better calibrated), a growing gap is
    'Worsening'. Needs >=2 snapshots; otherwise 'Insufficient history'.
    """
    bucket_note = _bucket_confidence_annotation(calib.get("buckets_5"))

    if len(history) < 2:
        return card(
            "Calibration Trend",
            status="unknown",
            label="Insufficient history",
            summary=(
                "Need >=2 daily snapshots to trend the calibration gap. "
                + bucket_note
            ),
            source_artifacts=["confidence_calibration.json"],
            updated_at=calib.get("generated_at"),
        )

    earliest_gap = history[0][1]
    latest_gap = history[-1][1]
    delta = latest_gap - earliest_gap

    if delta < -_CALIBRATION_TREND_EPS:
        label, status = "Improving", "ok"
    elif delta > _CALIBRATION_TREND_EPS:
        label, status = "Worsening", "warning"
    else:
        label, status = "Stable", "info"

    summary = (
        f"Gap {earliest_gap:.3f} -> {latest_gap:.3f} over {len(history)} snapshots "
        f"(delta {delta:+.3f}). {bucket_note}"
    )
    return card(
        "Calibration Trend",
        status=status,
        label=label,
        summary=summary,
        source_artifacts=["confidence_calibration.json"],
        updated_at=calib.get("generated_at"),
    )


def _efficacy_label_and_status(
    snapshots_consumed: int,
    rows_matched: int,
    by_tag: dict | None,
    lookback_days: int,
) -> tuple[str, str]:
    """
    Derive a card label and status for a pattern_efficacy artifact.

    Returns (label, card_status).
    """
    # Thin-sample check first
    n_check = _n_label(snapshots_consumed)
    if n_check:
        return n_check, "warning"

    if rows_matched < _THIN_SAMPLE_N:
        return "Thin sample", "warning"

    # Summarise by_tag performance if available
    if not isinstance(by_tag, dict) or not by_tag:
        return f"{lookback_days}d window", "info"

    improving = 0
    weak = 0
    for tag_data in by_tag.values():
        if not isinstance(tag_data, dict):
            continue
        significance = (tag_data.get("significance") or "").lower()
        if significance in ("winner", "improving"):
            improving += 1
        elif significance in ("loser", "weak"):
            weak += 1

    total_tags = len(by_tag)
    if total_tags == 0:
        return f"{lookback_days}d window", "info"

    if improving > 0 and weak == 0:
        return "Improving", "ok"
    if weak > 0 and improving == 0:
        return "Weak", "warning"
    if improving > 0 and weak > 0:
        return "Mixed", "warning"
    return f"{lookback_days}d window", "info"


def _humanize_token(text: Any) -> str:
    """snake_case / identifier -> 'Sentence case' (upper first char, rest intact).

    e.g. ``liquidity_shock`` -> ``Liquidity shock``. Preserves interior casing
    (does not lowercase acronyms the way ``.title()`` would).
    """
    s = str(text or "").replace("_", " ").strip()
    return (s[:1].upper() + s[1:]) if s else s


def _sqg_loop_cards(root: Path) -> list[dict[str, Any]]:
    """Observe-only cards for the Simulation/Quant/Governance loop artifacts.

    All six are observe-only / sandbox / production-gated — NONE feed
    decision_plan.json. The page-level banner already establishes the
    observe/proposal-only framing, so each card badge carries its *actual*
    state (Complete / No findings / Healthy / ...) rather than repeating
    "Observe only". Cards degrade gracefully when an artifact is absent.
    No trade/execution language anywhere.

    Emitted in operational reading order (this list order is what the template
    renders, via selectattr): did the run complete → is the system coherent →
    is attribution usable → what does the stress view show → is strategy
    documentation complete → what research is registered.
    """
    latest = root / "outputs" / "latest"
    sandbox = root / "outputs" / "sandbox"
    policy = root / "outputs" / "policy"
    out: list[dict[str, Any]] = []

    # 1. Run Lineage manifest (Phase 1) — did the run complete?
    rm = _read_json(policy / "run_manifest.json")
    if rm:
        rid = rm.get("run_id") or "unknown"
        rstatus = (rm.get("status") or "unknown").lower()
        commit = (rm.get("source_commit") or "")[:8]
        # complete → healthy; complete_with_warnings/running/failed → warning
        # (failed stays warning, not red: this observe-only cockpit never raises a
        # production-critical red for a run-status signal). missing → neutral.
        _run_map = {
            "complete": ("ok", "Complete"),
            "complete_with_warnings": ("warning", "Complete with warnings"),
            "running": ("warning", "Running"),
            "failed": ("warning", "Failed"),
        }
        c_status, label = _run_map.get(rstatus, ("unknown", "Unknown"))
        summary = f"Run {rid}" + (f" at commit {commit}" if commit else "")
        out.append(card(
            "Run Lineage", status=c_status, label=label, summary=summary,
            source_artifacts=["run_manifest.json"],
            updated_at=rm.get("completed_at") or rm.get("started_at")))
    else:
        out.append(card(
            "Run Lineage", status="unknown", label="Unknown",
            summary="No run manifest yet — produced by the daily pipeline.",
            source_artifacts=["run_manifest.json"]))

    # 2. Semantic Liveness meta-monitor (Phase 6) — is the system coherent?
    #    AMBER-max by design: an unexpected red is defensively downgraded to a
    #    warning here (the underlying status is preserved in the summary) so this
    #    observe-only meta-monitor never claims a production-critical red.
    sl = _read_json(latest / "semantic_liveness_status.json")
    if sl:
        ov = (sl.get("overall_status") or "unknown").lower()
        fc = int(sl.get("finding_count") or 0)
        c_status = {"ok": "ok", "green": "ok", "amber": "warning",
                    "warning": "warning", "red": "warning"}.get(ov, "unknown")
        label = "No findings" if fc == 0 else ("1 finding" if fc == 1 else f"{fc} findings")
        if ov == "red":
            summary = ("Distribution monitor reported red — shown as a warning "
                       "(observe-only meta-monitor is AMBER-max).")
        else:
            summary = f"Distribution monitor reported {ov}."
        out.append(card(
            "Semantic Liveness", status=c_status, label=label, summary=summary,
            source_artifacts=["semantic_liveness_status.json"],
            updated_at=sl.get("generated_at")))
    else:
        out.append(card(
            "Semantic Liveness", status="unknown", label="Unknown",
            summary="No liveness report yet — produced by the daily pipeline.",
            source_artifacts=["semantic_liveness_status.json"]))

    # 3. Quant Feedback attribution (Phase 5) — is attribution evidence usable?
    qf = _read_json(latest / "quant_feedback.json")
    if qf:
        fb = qf.get("fallback_rate")
        n_res = int(qf.get("n_resolved_outcomes") or 0)
        n_ctx = int(qf.get("n_context_records") or 0)
        has_fb = isinstance(fb, (int, float))
        if n_res == 0:
            c_status, label = "unknown", "Insufficient history"
            summary = f"{n_ctx} decisions captured; none resolved to outcomes yet."
        elif has_fb and fb >= 0.50:
            c_status, label = "warning", "High fallback"
            summary = (f"{fb:.0%} of {n_res} resolved outcomes could not be joined "
                       f"to decision context ({n_ctx} captured).")
        else:
            c_status, label = "ok", "Healthy"
            summary = f"{n_res} resolved outcomes joined to context ({n_ctx} captured)"
            summary += (f"; fallback {fb:.0%}." if has_fb else ".")
        out.append(card(
            "Quant Feedback (Attribution)", status=c_status, label=label,
            summary=summary, source_artifacts=["quant_feedback.json"],
            updated_at=qf.get("generated_at")))
    else:
        out.append(card(
            "Quant Feedback (Attribution)", status="unknown",
            label="Insufficient history",
            summary="No attribution report yet — produced by the daily pipeline.",
            source_artifacts=["quant_feedback.json"]))

    # 4. Scenario Risk stress illustrations (Phase 11) — deterministic stress view.
    scn = _read_json(latest / "scenario_risk.json")
    if scn:
        n_pos = int(scn.get("n_positions") or 0)
        degraded = bool(scn.get("degraded"))
        wc = scn.get("worst_case_scenario")
        wc_label = (wc.get("name") or wc.get("scenario") or wc.get("label")
                    ) if isinstance(wc, dict) else wc
        if degraded:
            c_status, label = "warning", "Degraded"
        elif n_pos == 0:
            c_status, label = "unknown", "Insufficient history"
        else:
            c_status, label = "info", "Available"
        summary = f"{n_pos} position(s) stress-tested"
        if wc_label:
            summary += f"; worst case {_humanize_token(wc_label)}"
        summary += " — deterministic illustration, not a forecast."
        out.append(card(
            "Scenario Risk (Stress)", status=c_status, label=label,
            summary=summary, source_artifacts=["scenario_risk.json"],
            updated_at=scn.get("generated_at")))
    else:
        out.append(card(
            "Scenario Risk (Stress)", status="unknown", label="Insufficient history",
            summary="No scenario report yet — produced by the daily pipeline.",
            source_artifacts=["scenario_risk.json"]))

    # 5. Strategy Mandates coverage (Phase 9) — is strategy documentation complete?
    sm = _read_json(sandbox / "strategy_mandates.json")
    if sm:
        cc = bool(sm.get("coverage_complete"))
        unmandated = sm.get("unmandated") or []
        n_mandates = len(sm.get("mandates") or {})
        if cc:
            c_status, label = "ok", "Complete"
            summary = f"All {n_mandates} strategies carry a documented mandate."
        else:
            c_status, label = "warning", "Coverage gap"
            names = ", ".join(_humanize_token(u) for u in unmandated)
            summary = (f"{n_mandates} strategies; {len(unmandated)} without a mandate"
                       + (f": {names}." if names else "."))
        out.append(card(
            "Strategy Mandates", status=c_status, label=label, summary=summary,
            source_artifacts=["strategy_mandates.json"],
            updated_at=sm.get("generated_at")))
    else:
        out.append(card(
            "Strategy Mandates", status="unknown", label="Insufficient history",
            summary="No mandates yet — produced by the weekly pipeline.",
            source_artifacts=["strategy_mandates.json"]))

    # 6. Experiment Registry research ledger (Phase 8) — accepts list or {registry:[...]}.
    er_raw = _read_json(sandbox / "experiment_registry.json")
    rows: list = []
    if isinstance(er_raw, list):
        rows = er_raw
    elif isinstance(er_raw, dict):
        rows = er_raw.get("registry") or er_raw.get("experiments") or []
    if rows:
        by_status: dict[str, int] = {}
        for e in rows:
            if isinstance(e, dict):
                st = str(e.get("status") or "unknown")
                by_status[st] = by_status.get(st, 0) + 1
        # natural-language breakdown, no dense bracketed dump
        phrase = ", ".join(f"{v} {_humanize_token(k).lower()}"
                           for k, v in sorted(by_status.items()))
        summary = f"{len(rows)} experiment(s) registered" + (f" ({phrase})." if phrase else ".")
        out.append(card(
            "Experiment Registry", status="info", label=f"{len(rows)} tracked",
            summary=summary, source_artifacts=["experiment_registry.json"],
            updated_at=er_raw.get("generated_at") if isinstance(er_raw, dict) else None))
    else:
        out.append(card(
            "Experiment Registry", status="info", label="No experiments yet",
            summary="No research experiments registered yet.",
            source_artifacts=["experiment_registry.json"]))

    return out


# ---------------------------------------------------------------------------
# Public collector
# ---------------------------------------------------------------------------

def collect_quant_view(root: Path) -> dict[str, Any]:
    """
    Persona collector for /dashboard/quant.

    Returns::

        {
          "cards": [ <card dicts> ],
          "persona": "quant",
          "efficacy_rows": [ ... ],   # per-period summary rows for the table
        }
    """
    root = Path(root)
    latest = root / "outputs" / "latest"
    cards: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 1. Confidence Calibration
    # ------------------------------------------------------------------
    calib = _read_json(latest / "confidence_calibration.json") or {}

    if calib:
        total_resolved = calib.get("total_resolved") or 0
        min_required = calib.get("min_required") or 20
        insufficient = calib.get("insufficient_data") or False
        hit_rate = calib.get("overall_hit_rate")
        calib_gap = calib.get("overall_calibration_gap")
        summary_line = calib.get("summary_line") or ""

        n_lbl = _n_label(total_resolved)
        if insufficient or n_lbl == "Insufficient history":
            lbl = "Insufficient history"
            c_status = "warning"
        elif n_lbl == "Thin sample":
            lbl = "Thin sample"
            c_status = "warning"
        elif calib_gap is not None and calib_gap > _CALIBRATION_GAP_CAUTION:
            lbl = "Caution"
            c_status = "warning"
        else:
            lbl = f"{int(total_resolved)} resolved"
            c_status = "ok"

        parts = [summary_line] if summary_line else []
        if hit_rate is not None:
            parts.append(f"Hit rate: {hit_rate:.1%}")
        if calib_gap is not None:
            parts.append(f"Calibration gap: {calib_gap:.3f}")

        cards.append(card(
            "Confidence Calibration",
            status=c_status,
            label=lbl,
            summary="; ".join(parts) or "Calibration data available",
            source_artifacts=["confidence_calibration.json"],
            updated_at=calib.get("generated_at"),
        ))
    else:
        cards.append(card(
            "Confidence Calibration",
            status="unknown",
            label="Insufficient history",
            summary="confidence_calibration.json absent — run daily pipeline",
            source_artifacts=["confidence_calibration.json"],
        ))

    # 1b. Calibration Trend (history-aware + over/under-confident bucket annotation)
    if calib:
        cards.append(
            _calibration_trend_card(calib, _calibration_gap_history(root))
        )

    # ------------------------------------------------------------------
    # 2–4. Pattern Efficacy (weekly / monthly / yearly)
    # ------------------------------------------------------------------
    efficacy_rows: list[dict[str, Any]] = []
    for period, fname in (
        ("weekly", "pattern_efficacy_weekly.json"),
        ("monthly", "pattern_efficacy_monthly.json"),
        ("yearly", "pattern_efficacy_yearly.json"),
    ):
        pe = _read_json(latest / fname) or {}
        if pe:
            snapshots = pe.get("snapshots_consumed") or 0
            rows_matched = pe.get("rows_matched_to_outcomes") or 0
            lookback = pe.get("lookback_days") or 0
            by_tag = pe.get("by_tag") or {}

            lbl, c_status = _efficacy_label_and_status(
                snapshots_consumed=snapshots,
                rows_matched=rows_matched,
                by_tag=by_tag,
                lookback_days=lookback,
            )
            parts = [f"{snapshots} snapshots, {rows_matched} matched outcomes"]
            if lookback:
                parts.append(f"{lookback}d window")

            efficacy_rows.append({
                "period": period.capitalize(),
                "snapshots": snapshots,
                "rows_matched": rows_matched,
                "lookback_days": lookback,
                "label": lbl,
                "status": c_status,
            })

            cards.append(card(
                f"Pattern Efficacy ({period.capitalize()})",
                status=c_status,
                label=lbl,
                summary="; ".join(parts),
                source_artifacts=[fname],
                updated_at=pe.get("generated_at"),
            ))
        else:
            cards.append(card(
                f"Pattern Efficacy ({period.capitalize()})",
                status="unknown",
                label="Insufficient history",
                summary=f"{fname} absent — run pattern learning pipeline",
                source_artifacts=[fname],
            ))

    # ------------------------------------------------------------------
    # 5. Retune Impact
    # ------------------------------------------------------------------
    retune = _read_json(latest / "retune_impact.json") or {}

    if retune:
        history_size = retune.get("history_size") or 0
        changes_count = retune.get("changes_count") or 0
        oa = retune.get("outcome_attribution") or {}
        oa_available = oa.get("available") if isinstance(oa, dict) else False

        n_lbl = _n_label(history_size)
        if n_lbl:
            lbl = n_lbl
            c_status = "warning"
        else:
            lbl = "Observe only"
            c_status = "info"

        parts: list[str] = [f"{changes_count} tracked changes, {history_size} versions"]
        if oa_available:
            fp_count = oa.get("fingerprint_count") or 0
            total_sigs = oa.get("total_signals") or 0
            parts.append(f"{fp_count} fingerprints, {total_sigs} total signals")

        cards.append(card(
            "Retune Impact",
            status=c_status,
            label=lbl,
            summary="; ".join(parts),
            source_artifacts=["retune_impact.json"],
            updated_at=retune.get("generated_at"),
        ))
    else:
        cards.append(card(
            "Retune Impact",
            status="unknown",
            label="Insufficient history",
            summary="retune_impact.json absent — run retune pipeline",
            source_artifacts=["retune_impact.json"],
        ))

    # ------------------------------------------------------------------
    # 6. Gate / Retune Suggestions
    # ------------------------------------------------------------------
    gate = _read_json(latest / "gate_retune_suggestions.json") or {}

    if gate:
        available = gate.get("available") or False
        weight_proposals = gate.get("weight_proposals") or []
        gate_proposal = gate.get("gate_proposal") or {}
        auto_count = gate.get("auto_applicable_count") or 0

        if not available:
            lbl = "Insufficient history"
            c_status = "unknown"
            summary = "Insufficient signal history for gate retune proposals"
        else:
            lbl = "Proposal only"
            c_status = "info"
            n_props = len(weight_proposals) if isinstance(weight_proposals, list) else 0
            gp_delta = (gate_proposal.get("delta") or 0) if isinstance(gate_proposal, dict) else 0
            parts_g: list[str] = [f"{n_props} weight proposals"]
            if isinstance(gate_proposal, dict) and gate_proposal.get("parameter"):
                parts_g.append(
                    f"Gate proposal: {gate_proposal.get('parameter')} Δ={gp_delta:+.4f}"
                )
            parts_g.append(f"{auto_count} auto-applicable")
            summary = "; ".join(parts_g)

        cards.append(card(
            "Gate / Retune Suggestions",
            status=c_status,
            label=lbl,
            summary=summary,
            source_artifacts=["gate_retune_suggestions.json"],
            updated_at=gate.get("generated_at"),
        ))
    else:
        cards.append(card(
            "Gate / Retune Suggestions",
            status="unknown",
            label="Insufficient history",
            summary="gate_retune_suggestions.json absent — run pattern learning pipeline",
            source_artifacts=["gate_retune_suggestions.json"],
        ))

    # ------------------------------------------------------------------
    # 7. Alpha Attribution
    # ------------------------------------------------------------------
    alpha = _read_json(latest / "alpha_attribution_report.json") or {}

    if alpha:
        summary_line = alpha.get("summary_line") or ""
        min_n = alpha.get("min_n_required") or 20
        best_sharpe = alpha.get("best_sharpe_source") or ""
        by_source = alpha.get("by_source") or {}

        # Count sources with enough data
        sufficient_count = 0
        total_sources = 0
        if isinstance(by_source, dict):
            for src_data in by_source.values():
                if isinstance(src_data, dict):
                    total_sources += 1
                    n_returns = src_data.get("n_returns") or 0
                    if n_returns >= min_n:
                        sufficient_count += 1

        if total_sources == 0 or sufficient_count == 0:
            lbl = "Insufficient history"
            c_status = "warning"
        elif sufficient_count < total_sources:
            lbl = "Mixed"
            c_status = "warning"
        else:
            lbl = "Observe only"
            c_status = "info"

        parts_a: list[str] = []
        if summary_line:
            parts_a.append(summary_line)
        if best_sharpe:
            parts_a.append(f"Best Sharpe source: {best_sharpe}")

        cards.append(card(
            "Alpha Attribution",
            status=c_status,
            label=lbl,
            summary="; ".join(parts_a) or "Alpha attribution data available",
            source_artifacts=["alpha_attribution_report.json"],
            updated_at=alpha.get("generated_at"),
        ))
    else:
        cards.append(card(
            "Alpha Attribution",
            status="unknown",
            label="Insufficient history",
            summary="alpha_attribution_report.json absent — run attribution pipeline",
            source_artifacts=["alpha_attribution_report.json"],
        ))

    # ------------------------------------------------------------------
    # 8. Quant Watch Status (optional — may be absent)
    # ------------------------------------------------------------------
    qwatch = _read_json(latest / "quant_watch_status.json")

    if qwatch is not None:
        overall_status = qwatch.get("overall_status") or "unknown"
        active_count = qwatch.get("active_count") or 0

        _qw_map = {"ok": "ok", "green": "ok", "amber": "warning", "red": "red"}
        c_status = _qw_map.get(overall_status.lower(), "warning")

        cards.append(card(
            "Quant Watch",
            status=c_status,
            label="Observe only",
            summary=f"{active_count} active concerns (status: {overall_status})",
            source_artifacts=["quant_watch_status.json"],
            updated_at=qwatch.get("generated_at"),
        ))
    else:
        cards.append(card(
            "Quant Watch",
            status="info",
            label="Observe only",
            summary="quant_watch_status.json absent — quant watch not configured",
            source_artifacts=["quant_watch_status.json"],
        ))

    # ------------------------------------------------------------------
    # 9. Kelly Sizing (advisory — observe only)
    # ------------------------------------------------------------------
    kelly = _read_json(latest / "kelly_sizing_advisor.json") or {}

    if kelly:
        summary_line = kelly.get("summary_line") or ""
        min_resolved = kelly.get("min_resolved_required") or 20
        half_kelly = kelly.get("half_kelly") or False
        by_decision = kelly.get("by_decision") or {}

        # Count decision groups with sufficient data.
        # NOTE: total_groups is a small count of categories (e.g. 3),
        # not a sample-size.  We use sufficient_groups / total_groups
        # to determine readiness, not _n_label on the group count.
        sufficient_groups = 0
        total_groups = 0
        if isinstance(by_decision, dict):
            for grp_data in by_decision.values():
                if isinstance(grp_data, dict):
                    total_groups += 1
                    n_res = grp_data.get("n_resolved") or 0
                    if n_res >= min_resolved:
                        sufficient_groups += 1

        if total_groups == 0 or sufficient_groups == 0:
            lbl = "Insufficient history"
            c_status = "warning"
        else:
            lbl = "Proposal only"
            c_status = "info"

        parts_k: list[str] = []
        if summary_line:
            parts_k.append(summary_line)
        if half_kelly:
            parts_k.append("Half-Kelly cap applied")

        cards.append(card(
            "Kelly Sizing (Advisory)",
            status=c_status,
            label=lbl,
            summary="; ".join(parts_k) or "Kelly sizing data available",
            source_artifacts=["kelly_sizing_advisor.json"],
            updated_at=kelly.get("generated_at"),
        ))
    else:
        cards.append(card(
            "Kelly Sizing (Advisory)",
            status="unknown",
            label="Insufficient history",
            summary="kelly_sizing_advisor.json absent — run sizing pipeline",
            source_artifacts=["kelly_sizing_advisor.json"],
        ))

    # ------------------------------------------------------------------
    # 10. Simulation / Quant / Governance loop (SQG program) — observe-only
    # ------------------------------------------------------------------
    cards.extend(_sqg_loop_cards(root))

    return {
        "cards": cards,
        "persona": "quant",
        "efficacy_rows": efficacy_rows,
        "observe_only": True,
    }
