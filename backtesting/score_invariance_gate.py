"""
Step 5 safety gate — protected-score value regression  (additive | advisory-only | observe-only)

Pattern-Improvement Loop — the documented precondition #2 for *executing* a
governed weight apply (Step 5). Before any live `registry_apply.apply_approved_changes`,
this gate proves the apply is **semantically safe for the six protected scores**:
it recomputes them over a fixed, deterministic, offline fixture before and after
applying a candidate weight delta to a **temp copy** of the registry, and asserts
no score value changed.

Why bit-identical (not "bounded"): as of 2026-06-05 the registry `default_weight`
is **not read by any scoring function** — the six protected scores are provably
invariant to a `default_weight` delta (traced: scanner `_compute_signal_score`,
confidence `compute_confidence`, postprocess `effective_score = signal*confidence`,
conviction `apply_conviction_layer`, alert_ranking `apply_priority_score`, and the
separate allocation subsystem `policy_recommender`). So a Step 5 apply MUST leave
every score untouched. This gate locks that invariant in: if a future change ever
wires `default_weight` into a score, the gate flips RED and forces re-review
before any apply is permitted.

Primary scores driven via the REAL functions (degrade per-probe when a module is
unavailable, e.g. scanner needs pandas in a bare venv):
  - signal_score      (watchlist_scanner.scanner._compute_signal_score)
  - confidence_score  (watchlist_scanner.confidence.compute_confidence)
  - effective_score   (= signal_score * confidence_score; postprocess semantics)
  - final_rank_score  (watchlist_scanner.alert_ranking.apply_priority_score)
conviction_score is a pure weighted blend of effective + confidence (invariant by
construction when those are); recommendation_score is computed by a separate
allocation-policy subsystem that also never reads the registry — both are out of
this gate's signal-scoring scope and documented as decoupled.

Observe-only: operates entirely on a temp copy; the live config/signal_registry.yaml
is never mutated. Returns a verdict dict; writes no artifact of its own. Never raises.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

_OBSERVE_ONLY = True


def default_fixture() -> dict[str, Any]:
    """A deterministic, network-free set of scoring inputs. The exact values are
    immaterial — the gate only compares a score against ITSELF before/after an
    apply, so any fixed fixture works as long as it is reused on both sides."""
    return {
        "tech": {
            "price_change_1d": 3.0, "price_change_5d": 6.0, "volume_spike": True,
            "above_sma20": True, "above_sma50": True,
        },
        "theme_scores": {"AI Infrastructure": 0.8},
        "articles": [{"sentiment": 0.4}] * 6,
        "fundamentals": {"pe_ratio": 20.0, "revenue_growth": 0.12, "debt_ratio": 0.3},
        "fund_score": 0.6,
        "data_quality": "fresh",
        "ov_source": "fresh",
        "portfolio_fit_score": 0.5,
        "theme_alignment_score": 0.3,
        "evidence_count": 3,
    }


def compute_protected_scores(
    registry_path: str,
    fixture: dict[str, Any],
    extra_probes: dict[str, Callable[[str], float]] | None = None,
) -> dict[str, Any]:
    """Compute the protected scores over *fixture* using the REAL scoring
    functions. Returns ``{scores: {name: value}, unavailable: [name]}``.

    Each probe is isolated: a probe whose module cannot be imported in the
    current environment (e.g. scanner needs pandas) is recorded under
    ``unavailable`` and skipped, never crashing the gate. ``extra_probes`` map a
    name to ``fn(registry_path) -> float`` and are used to inject a deliberately
    registry-coupled probe (so the RED path is testable) — the real scorers
    ignore ``registry_path`` because they do not read the registry.
    """
    scores: dict[str, float] = {}
    unavailable: list[str] = []
    tech = fixture["tech"]
    articles = fixture["articles"]

    def _probe(name: str, fn: Callable[[], float]) -> None:
        try:
            scores[name] = float(fn())
        except Exception as exc:  # missing dep or import error → degrade, don't crash
            unavailable.append(f"{name}:{type(exc).__name__}")

    def _signal_score() -> float:
        from watchlist_scanner.scanner import _compute_signal_score
        s, _ = _compute_signal_score(tech, fixture["theme_scores"], articles, fixture["fund_score"])
        return s

    def _confidence_score() -> float:
        from watchlist_scanner.confidence import compute_confidence
        c, _, _ = compute_confidence(fixture["data_quality"], fixture["ov_source"],
                                     tech, fixture["fundamentals"], articles)
        return c

    def _final_rank_score() -> float:
        from watchlist_scanner.alert_ranking import apply_priority_score
        row = {
            "signal_score": scores.get("signal_score", 0.5),
            "augmented_signal_score": scores.get("signal_score", 0.5),
            "confidence_score": scores.get("confidence_score", 0.5),
            "portfolio_fit_score": fixture["portfolio_fit_score"],
            "theme_alignment_score": fixture["theme_alignment_score"],
            "evidence_count": fixture["evidence_count"],
            "data_quality": fixture["data_quality"],
        }
        apply_priority_score(row, ranking_config=None, approved_weights_config=None)
        return float(row["final_rank_score"])

    # Order matters: signal_score + confidence_score feed effective_score and the
    # final_rank row, so compute them first.
    _probe("signal_score", _signal_score)
    _probe("confidence_score", _confidence_score)
    if "signal_score" in scores and "confidence_score" in scores:
        _probe("effective_score", lambda: round(scores["signal_score"] * scores["confidence_score"], 6))
    _probe("final_rank_score", _final_rank_score)

    for name, fn in (extra_probes or {}).items():
        _probe(name, lambda fn=fn: fn(registry_path))

    return {"scores": scores, "unavailable": unavailable}


def assert_scores_invariant_across_apply(
    *,
    registry_path: str = "config/signal_registry.yaml",
    target_signal_id: str = "STRONG_MOVE_UP",
    sample_delta: float = 0.05,
    max_abs_delta: float = 0.05,
    fixture: dict[str, Any] | None = None,
    extra_probes: dict[str, Callable[[str], float]] | None = None,
) -> dict[str, Any]:
    """Apply a candidate weight delta to a TEMP copy of the registry and assert
    every protected score is bit-identical before/after.

    Verdict ``status``:
      - GREEN          — the apply changed the registry weight yet no score moved
                         (the expected, currently-architected outcome).
      - RED            — at least one score value changed after the apply (a
                         coupling regression; do NOT permit a live Step 5 apply).
      - inconclusive   — the apply was a no-op (delta capped, unknown signal,
                         registry unreadable), so invariance can't be judged.

    Observe-only: all writes land in a throwaway temp dir; the live registry and
    all protected scoring logic are untouched. Never raises.
    """
    from backtesting.registry_apply import apply_approved_changes

    fixture = fixture or default_fixture()
    try:
        live = Path(registry_path)
        if not live.exists():
            return {"observe_only": _OBSERVE_ONLY, "status": "inconclusive",
                    "reason": "registry_missing", "diffs": {}}

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            temp_reg = tmp / "signal_registry.yaml"
            shutil.copy2(live, temp_reg)

            def _registry_weight(path: Path) -> float | None:
                from portfolio_automation.signal_registry import load_signal_registry
                defn = load_signal_registry(str(path)).get(target_signal_id)
                return defn.default_weight if defn is not None else None

            weight_before = _registry_weight(temp_reg)
            before = compute_protected_scores(str(temp_reg), fixture, extra_probes)

            approval = {
                "approved_by": "score_invariance_gate_selftest",
                "changes": [{"signal_id": target_signal_id, "delta": sample_delta}],
            }
            import json
            (tmp / "approved_weight_changes.json").write_text(json.dumps(approval), encoding="utf-8")
            res = apply_approved_changes(
                registry_path=str(temp_reg),
                approval_path=str(tmp / "approved_weight_changes.json"),
                history_dir=str(tmp / "history"),
                base_dir=str(tmp / "outputs"),
                max_abs_delta=max_abs_delta,
            )

            weight_after = _registry_weight(temp_reg)
            after = compute_protected_scores(str(temp_reg), fixture, extra_probes)

            # Compare only scores computed on BOTH sides.
            common = set(before["scores"]) & set(after["scores"])
            diffs = {
                k: [before["scores"][k], after["scores"][k]]
                for k in sorted(common)
                if before["scores"][k] != after["scores"][k]
            }

            applied = res.get("status") == "applied" and weight_before != weight_after
            if not applied:
                status = "inconclusive"
            elif diffs:
                status = "RED"
            else:
                status = "GREEN"

            return {
                "observe_only": _OBSERVE_ONLY,
                "status": status,
                "target_signal_id": target_signal_id,
                "sample_delta": sample_delta,
                "apply_status": res.get("status"),
                "registry_weight_before": weight_before,
                "registry_weight_after": weight_after,
                "scores_before": before["scores"],
                "scores_after": after["scores"],
                "diffs": diffs,
                "unavailable_probes": sorted(set(before["unavailable"]) | set(after["unavailable"])),
                "note": ("Protected scores are computed by functions that do not read "
                         "signal_registry.default_weight; a weight apply is therefore "
                         "score-invariant by construction. This gate re-verifies that "
                         "decoupling on every run. Step 5 (live apply) remains owner-gated."),
            }
    except Exception as exc:  # degrade, never break a precondition check
        return {"observe_only": _OBSERVE_ONLY, "status": "inconclusive",
                "reason": f"gate_error:{exc}", "diffs": {}}
