"""
Controlled activation support for rank-aware allocation.

Evaluates whether an approved_allocation_policy artifact can be issued based on
evidence from the allocation policy simulation. Approval ONLY creates an advisory
artifact — it does NOT mutate portfolio state, trigger trades, or alter alert gating.

Default behavior (no flags) is a dry-run that evaluates all rules and prints a
structured report without writing any file.

Reads:
  outputs/performance/allocation_policy_simulation.json
  outputs/performance/allocation_policy_preview.json   (informational only)

Writes (--approve only, when all rules pass):
  outputs/performance/approved_allocation_policy.json
  outputs/performance/allocation_policy_activation_audit.jsonl

CLI:
  python -m watchlist_scanner.allocation_policy_activation           (dry-run)
  python -m watchlist_scanner.allocation_policy_activation --approve (write artifact)
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("watchlist_scanner.allocation_policy_activation")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SIMULATION_REL = ("outputs", "performance", "allocation_policy_simulation.json")
_PREVIEW_REL = ("outputs", "performance", "allocation_policy_preview.json")
_ARTIFACT_REL = ("outputs", "performance", "approved_allocation_policy.json")
_AUDIT_REL = ("outputs", "performance", "allocation_policy_activation_audit.jsonl")

# ---------------------------------------------------------------------------
# Rule identifiers
# ---------------------------------------------------------------------------

RULE_SIMULATION_EXISTS: str = "simulation_exists"
RULE_OBSERVE_ONLY: str = "observe_only"
RULE_NOT_APPLIED: str = "not_applied"
RULE_SAMPLE_SIZE: str = "sample_size_sufficient"
RULE_EFFICIENCY_POSITIVE: str = "efficiency_delta_positive"
RULE_RANK_AWARE_BEATS_BASELINE: str = "rank_aware_beats_baseline"

ALL_RULES: tuple[str, ...] = (
    RULE_SIMULATION_EXISTS,
    RULE_OBSERVE_ONLY,
    RULE_NOT_APPLIED,
    RULE_SAMPLE_SIZE,
    RULE_EFFICIENCY_POSITIVE,
    RULE_RANK_AWARE_BEATS_BASELINE,
)

_DEFAULT_MIN_SAMPLE_SIZE: int = 30
_DEFAULT_APPROVAL_NOTE: str = (
    "Rank-aware allocation approved from simulation evidence. "
    "Advisory only — applied_to_live is false."
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _rule_result(passed: bool, reason: str) -> dict[str, Any]:
    return {"passed": passed, "reason": reason}


# ---------------------------------------------------------------------------
# Core evaluation (pure — no I/O)
# ---------------------------------------------------------------------------

def evaluate_activation_rules(
    simulation: dict[str, Any] | None,
    *,
    min_sample_size: int = _DEFAULT_MIN_SAMPLE_SIZE,
) -> dict[str, Any]:
    """
    Evaluate all activation rules against a simulation dict.

    Returns:
      {
        "all_passed": bool,
        "rules": {
          rule_name: {"passed": bool, "reason": str},
          ...
        }
      }

    If simulation is None every rule fails with an appropriate reason.
    Rules that depend on later data (efficiency, etc.) fail automatically when
    simulation_exists fails, so callers never need to check preconditions.
    """
    # Rule 1: simulation exists
    if simulation is None:
        unavailable = "simulation unavailable"
        return {
            "all_passed": False,
            "rules": {
                RULE_SIMULATION_EXISTS: _rule_result(False, "simulation file missing or unreadable"),
                RULE_OBSERVE_ONLY: _rule_result(False, unavailable),
                RULE_NOT_APPLIED: _rule_result(False, unavailable),
                RULE_SAMPLE_SIZE: _rule_result(False, unavailable),
                RULE_EFFICIENCY_POSITIVE: _rule_result(False, unavailable),
                RULE_RANK_AWARE_BEATS_BASELINE: _rule_result(False, unavailable),
            },
        }

    rules: dict[str, dict[str, Any]] = {}
    rules[RULE_SIMULATION_EXISTS] = _rule_result(True, "simulation loaded")

    # Rule 2: observe_only flag must be True
    observe_only = simulation.get("observe_only")
    rules[RULE_OBSERVE_ONLY] = (
        _rule_result(True, "observe_only is True")
        if observe_only is True
        else _rule_result(False, f"observe_only is {observe_only!r}, expected True")
    )

    # Rule 3: not_applied flag must be True
    not_applied = simulation.get("not_applied")
    rules[RULE_NOT_APPLIED] = (
        _rule_result(True, "not_applied is True")
        if not_applied is True
        else _rule_result(False, f"not_applied is {not_applied!r}, expected True")
    )

    # Rule 4: sufficient sample size
    sample_size = int(simulation.get("sample_size") or 0)
    if sample_size >= min_sample_size:
        rules[RULE_SAMPLE_SIZE] = _rule_result(
            True, f"sample_size {sample_size} >= {min_sample_size}"
        )
    else:
        rules[RULE_SAMPLE_SIZE] = _rule_result(
            False, f"sample_size {sample_size} < minimum {min_sample_size}"
        )

    # Rule 5: efficiency delta must be strictly positive
    delta = dict(simulation.get("delta") or {})
    efficiency_delta = float(delta.get("efficiency_delta") or 0.0)
    if efficiency_delta > 0.0:
        rules[RULE_EFFICIENCY_POSITIVE] = _rule_result(
            True, f"efficiency_delta {efficiency_delta:+.4f} > 0"
        )
    else:
        rules[RULE_EFFICIENCY_POSITIVE] = _rule_result(
            False, f"efficiency_delta {efficiency_delta:+.4f} is not positive"
        )

    # Rule 6: rank-aware capital efficiency must be >= baseline
    baseline = dict(simulation.get("baseline") or {})
    rank_aware = dict(simulation.get("rank_aware") or {})
    b_eff = float(baseline.get("capital_efficiency") or 0.0)
    ra_eff = float(rank_aware.get("capital_efficiency") or 0.0)
    if ra_eff >= b_eff:
        rules[RULE_RANK_AWARE_BEATS_BASELINE] = _rule_result(
            True,
            f"rank_aware efficiency {ra_eff:.4f} >= baseline {b_eff:.4f}",
        )
    else:
        rules[RULE_RANK_AWARE_BEATS_BASELINE] = _rule_result(
            False,
            f"rank_aware efficiency {ra_eff:.4f} < baseline {b_eff:.4f}",
        )

    all_passed = all(r["passed"] for r in rules.values())
    return {"all_passed": all_passed, "rules": rules}


def build_approved_allocation_policy(
    simulation: dict[str, Any],
    rule_results: dict[str, Any],
    *,
    min_sample_size: int = _DEFAULT_MIN_SAMPLE_SIZE,
    approval_note: str = _DEFAULT_APPROVAL_NOTE,
) -> dict[str, Any]:
    """
    Build the approved_allocation_policy artifact from a validated simulation.

    The artifact is advisory only:
      - applied_to_live is always False
      - activation_status is "approved_not_live"

    This function does NOT write any file — that is the caller's responsibility.
    Does NOT mutate simulation or rule_results.
    """
    rules = dict(rule_results.get("rules") or {})
    rules_passed = [name for name in ALL_RULES if rules.get(name, {}).get("passed")]
    rules_failed = [name for name in ALL_RULES if not rules.get(name, {}).get("passed")]
    sample_size = int(simulation.get("sample_size") or 0)

    return {
        "approved_at": datetime.now().isoformat(),
        "applied_to_live": False,
        "activation_status": "approved_not_live",
        "approval_note": approval_note,
        "min_sample_size": min_sample_size,
        "low_sample_warning": sample_size < min_sample_size,
        "sample_size": sample_size,
        "primary_window_days": simulation.get("primary_window_days"),
        "simulation_generated_at": simulation.get("generated_at"),
        "baseline": dict(simulation.get("baseline") or {}),
        "rank_aware": dict(simulation.get("rank_aware") or {}),
        "delta": dict(simulation.get("delta") or {}),
        "rules_passed": rules_passed,
        "rules_failed": rules_failed,
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _load_simulation(simulation_path: Path) -> dict[str, Any] | None:
    """Load allocation_policy_simulation.json. Returns None on missing/malformed."""
    if not simulation_path.exists():
        logger.info("allocation_policy_activation: simulation not found at %s", simulation_path)
        return None
    try:
        data = json.loads(simulation_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("allocation_policy_activation: could not read simulation — %s", exc)
        return None


def _append_audit_row(
    audit_path: Path,
    *,
    event: str,
    approved_at: str,
    dry_run: bool,
    rule_results: dict[str, Any],
    simulation: dict[str, Any] | None,
) -> None:
    """Append one audit row to the activation JSONL log."""
    rules = dict(rule_results.get("rules") or {})
    rules_passed = [name for name in ALL_RULES if rules.get(name, {}).get("passed")]
    rules_failed = [name for name in ALL_RULES if not rules.get(name, {}).get("passed")]
    row: dict[str, Any] = {
        "event": event,
        "approved_at": approved_at,
        "dry_run": dry_run,
        "applied_to_live": False,
        "all_rules_passed": rule_results.get("all_passed", False),
        "rules_passed": rules_passed,
        "rules_failed": rules_failed,
    }
    if simulation is not None:
        row["sample_size"] = simulation.get("sample_size")
        delta = simulation.get("delta") or {}
        row["efficiency_delta"] = delta.get("efficiency_delta")
        row["total_return_delta"] = delta.get("total_return_delta")

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_activation_check(
    *,
    root: Path | str | None = None,
    output_dir: Path | str | None = None,
    min_sample_size: int = _DEFAULT_MIN_SAMPLE_SIZE,
    approve: bool = False,
    approval_note: str = _DEFAULT_APPROVAL_NOTE,
) -> dict[str, Any]:
    """
    Evaluate activation rules and, when approve=True and all rules pass, write
    the approved_allocation_policy artifact and append an audit row.

    Dry-run (approve=False): evaluates rules and returns report, writes nothing.

    Returns a report dict with:
      - all_rules_passed: bool
      - rules: per-rule pass/fail details
      - approved: bool (True only when approve=True and all rules passed)
      - artifact_written: bool
      - audit_written: bool
    """
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    out_dir = (
        Path(output_dir) if output_dir is not None
        else root_path.joinpath(*_ARTIFACT_REL).parent
    )

    simulation_path = root_path.joinpath(*_SIMULATION_REL)
    artifact_path = out_dir / "approved_allocation_policy.json"
    audit_path = out_dir / "allocation_policy_activation_audit.jsonl"

    simulation = _load_simulation(simulation_path)
    rule_results = evaluate_activation_rules(simulation, min_sample_size=min_sample_size)
    all_passed = rule_results["all_passed"]

    now_iso = datetime.now().isoformat()
    approved = False
    artifact_written = False
    audit_written = False

    if approve:
        if all_passed:
            artifact = build_approved_allocation_policy(
                simulation,
                rule_results,
                min_sample_size=min_sample_size,
                approval_note=approval_note,
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
            approved = True
            artifact_written = True
            logger.info(
                "allocation_policy_activation: approved_allocation_policy written to %s",
                artifact_path,
            )
        else:
            failed = [r for r in ALL_RULES if not rule_results["rules"][r]["passed"]]
            logger.warning(
                "allocation_policy_activation: approval rejected — failed rules: %s",
                failed,
            )

        _append_audit_row(
            audit_path,
            event="approved" if approved else "rejected",
            approved_at=now_iso,
            dry_run=False,
            rule_results=rule_results,
            simulation=simulation,
        )
        audit_written = True

    return {
        "generated_at": now_iso,
        "dry_run": not approve,
        "all_rules_passed": all_passed,
        "rules": rule_results["rules"],
        "approved": approved,
        "artifact_written": artifact_written,
        "audit_written": audit_written,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m watchlist_scanner.allocation_policy_activation",
        description=(
            "Evaluate controlled activation rules for rank-aware allocation. "
            "Default is dry-run preview — no files written. "
            "Pass --approve to write the approved artifact when all rules pass."
        ),
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Write approved_allocation_policy.json and audit row if all rules pass",
    )
    parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Project root (default: two levels above this module)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="Output directory (default: outputs/performance/ relative to root)",
    )
    parser.add_argument(
        "--min-sample",
        type=int,
        default=_DEFAULT_MIN_SAMPLE_SIZE,
        metavar="N",
        help=f"Minimum resolved-signal sample size required (default: {_DEFAULT_MIN_SAMPLE_SIZE})",
    )
    parser.add_argument(
        "--note",
        default=_DEFAULT_APPROVAL_NOTE,
        metavar="TEXT",
        help="Approval note embedded in the artifact",
    )
    args = parser.parse_args()

    report = run_activation_check(
        root=args.root,
        output_dir=args.output_dir,
        min_sample_size=args.min_sample,
        approve=args.approve,
        approval_note=args.note,
    )

    if args.approve:
        print(
            "[APPROVED]" if report["approved"] else "[REJECTED]",
            "—",
            "artifact written" if report["artifact_written"] else "no artifact (rules failed)",
        )
    else:
        print("[DRY-RUN] Activation rule evaluation:")

    print()
    for rule_name in ALL_RULES:
        r = report["rules"][rule_name]
        icon = "PASS" if r["passed"] else "FAIL"
        print(f"  [{icon}] {rule_name}: {r['reason']}")

    print()
    print("All rules passed:", report["all_rules_passed"])
    if not args.approve:
        print("(Pass --approve to write the artifact when all rules pass.)")


if __name__ == "__main__":
    _main()
