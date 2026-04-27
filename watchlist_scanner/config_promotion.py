"""
Manual promotion workflow for approved ranking-weight proposals.

Reads:  outputs/performance/config_proposal.json
Writes: outputs/performance/approved_ranking_config.json
        outputs/performance/config_promotion_audit.jsonl  (append-only)

Run dry-run preview (default, no files written):
    python -m watchlist_scanner.config_promotion

Promote to approved artifact:
    python -m watchlist_scanner.config_promotion --approve
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("watchlist_scanner.config_promotion")

_DEFAULT_PROPOSAL_PATH = Path("outputs/performance/config_proposal.json")
_DEFAULT_OUTPUT_DIR = Path("outputs/performance")

_APPROVAL_NOTE = (
    "Manually approved from config_proposal.json. "
    "Not yet applied to live scoring."
)


class ConfigPromotionError(ValueError):
    """Raised when a proposal fails validation and cannot be promoted."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_proposal(proposal: dict[str, Any]) -> None:
    """
    Validate that a proposal dict meets all promotion requirements.

    Raises ConfigPromotionError with a descriptive message on the first failure.
    """
    if not isinstance(proposal, dict):
        raise ConfigPromotionError("Proposal is not a valid dict")

    status = proposal.get("proposal_status")
    if status != "not_applied":
        raise ConfigPromotionError(
            f"Proposal status is '{status}', expected 'not_applied'"
        )

    if proposal.get("applied") is not False:
        raise ConfigPromotionError("Proposal 'applied' field must be False")

    candidate = proposal.get("recommended_candidate")
    if not candidate or not str(candidate).strip():
        raise ConfigPromotionError("Proposal missing recommended_candidate")

    weights = proposal.get("proposed_weights")
    if not weights or not isinstance(weights, dict):
        raise ConfigPromotionError("Proposal missing proposed_weights")


# ---------------------------------------------------------------------------
# Artifact builders
# ---------------------------------------------------------------------------

def build_approved_config(
    proposal: dict[str, Any],
    *,
    simulation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build the approved_ranking_config artifact from a validated proposal.

    applied_to_live is always False — the artifact is a human-approved record,
    not an instruction to the live system.

    If policy_simulation.json data is supplied, sample_size and low_sample_warning
    are pulled from the recommended_policy section for auditability.
    """
    sample_size: int | None = None
    low_sample_warning: bool = True
    if simulation and isinstance(simulation, dict):
        rec = simulation.get("recommended_policy") or {}
        if rec.get("sample_size") is not None:
            sample_size = int(rec["sample_size"])
        if rec.get("low_sample_warning") is not None:
            low_sample_warning = bool(rec["low_sample_warning"])

    return {
        "approved_at": datetime.now().isoformat(),
        "source_proposal_generated_at": proposal.get("generated_at"),
        "recommended_candidate": proposal.get("recommended_candidate"),
        "proposed_weights": dict(proposal.get("proposed_weights") or {}),
        "current_weights": dict(proposal.get("current_weights") or {}),
        "weight_deltas": dict(proposal.get("weight_deltas") or {}),
        "performance_delta": dict(proposal.get("performance_delta") or {}),
        "sample_size": sample_size,
        "low_sample_warning": low_sample_warning,
        "applied_to_live": False,
        "approval_note": _APPROVAL_NOTE,
    }


def _append_audit_row(
    audit_path: Path,
    approved_config: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    row = {
        "event": "dry_run_preview" if dry_run else "approved",
        "approved_at": approved_config.get("approved_at"),
        "recommended_candidate": approved_config.get("recommended_candidate"),
        "proposed_weights": approved_config.get("proposed_weights"),
        "applied_to_live": approved_config.get("applied_to_live"),
        "dry_run": dry_run,
    }
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def promote_proposal(
    *,
    proposal_path: str | Path = _DEFAULT_PROPOSAL_PATH,
    output_dir: str | Path = _DEFAULT_OUTPUT_DIR,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Promote a config_proposal.json into an approved ranking config artifact.

    dry_run=True (default): validate and return a preview. No files are written.
    dry_run=False (requires --approve flag): write approved_ranking_config.json
        and append a row to config_promotion_audit.jsonl.

    Raises ConfigPromotionError if validation fails.
    """
    proposal_path = Path(proposal_path)
    out_dir = Path(output_dir)
    approved_path = out_dir / "approved_ranking_config.json"
    audit_path = out_dir / "config_promotion_audit.jsonl"
    sim_path = out_dir / "policy_simulation.json"

    if not proposal_path.exists():
        raise ConfigPromotionError(f"No config proposal found at {proposal_path}")

    try:
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigPromotionError(f"Could not read proposal: {exc}") from exc

    validate_proposal(proposal)

    simulation: dict[str, Any] | None = None
    if sim_path.exists():
        try:
            simulation = json.loads(sim_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    approved_config = build_approved_config(proposal, simulation=simulation)

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "approved_config": approved_config,
        "paths": {
            "approved_config": str(approved_path),
            "audit_log": str(audit_path),
        },
    }

    if dry_run:
        result["status"] = "dry_run"
        result["message"] = (
            f"Dry run only — pass --approve to write artifacts. "
            f"Would promote candidate '{approved_config['recommended_candidate']}'."
        )
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    approved_path.write_text(json.dumps(approved_config, indent=2), encoding="utf-8")
    _append_audit_row(audit_path, approved_config, dry_run=False)
    logger.info("Approved config written: %s", approved_path)
    logger.info("Audit row appended: %s", audit_path)

    result["status"] = "approved"
    result["message"] = (
        f"Promoted '{approved_config['recommended_candidate']}' to approved config. "
        "Not yet applied to live scoring."
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m watchlist_scanner.config_promotion",
        description=(
            "Promote a reviewed config proposal to an approved ranking config artifact. "
            "Omit --approve for a dry-run preview (no files written)."
        ),
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Write the approved artifact and append to the audit log.",
    )
    parser.add_argument(
        "--proposal-path",
        default=str(_DEFAULT_PROPOSAL_PATH),
        metavar="PATH",
        help=f"Path to config_proposal.json (default: {_DEFAULT_PROPOSAL_PATH})",
    )
    parser.add_argument(
        "--output-dir",
        default=str(_DEFAULT_OUTPUT_DIR),
        metavar="DIR",
        help=f"Output directory for artifacts (default: {_DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()

    try:
        result = promote_proposal(
            proposal_path=args.proposal_path,
            output_dir=args.output_dir,
            dry_run=not args.approve,
        )
        print(json.dumps(result, indent=2))
    except ConfigPromotionError as exc:
        print(json.dumps({"error": str(exc), "status": "failed"}, indent=2))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    _main()
