"""
Human approval mechanism (spec §6).

Human approval is the production gate. A human records an approve/reject decision
against a pending proposal; the decision is validated (real human approver, known
decision, timestamp) and appended to:

  * outputs/promotion_approvals/approved_proposals.json

Structural guarantees enforced here (and re-checked at application time):
  * AI cannot self-approve — an approver that looks like the AI reviewer is
    rejected by schemas.is_human_approver.
  * Invalid approval metadata is ignored — never counted as an approval.

``effective_approvals`` folds the log to the latest valid decision per proposal,
so a later reject overrides an earlier approve (and vice-versa).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
)
from portfolio_automation.sim_governance import schemas as S

logger = logging.getLogger("stockbot.sim_governance.promotion_approvals")

_APPROVALS_FILE = "approved_proposals.json"


def _looks_like_repo_root(base_dir: str) -> bool:
    """True when *base_dir* points at the project root instead of its outputs dir.

    The output-namespace convention is ``base_dir=<root>/outputs``; passing
    ``<root>`` (e.g. ".") silently resolves to ``<root>/promotion_approvals/`` —
    a location no production loader reads, so the approval would look recorded
    but never apply. Detect that misuse via repo-root markers that are absent
    from both an ``outputs`` dir and a bare tmp dir.
    """
    try:
        p = Path(base_dir)
        return (p / "config.json").is_file() and (p / "CLAUDE.md").is_file()
    except Exception:
        return False


def _load_raw(base_dir: str) -> dict:
    path = get_output_path(OutputNamespace.PROMOTION_APPROVALS, _APPROVALS_FILE, base_dir=base_dir)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"approvals": []}


def record_approval(
    proposal_id: str,
    decision: str,
    approver: str,
    now: str,
    *,
    base_dir: str,
    notes: str | None = None,
    review_date: str | None = None,
    candidate_id: str | None = None,
    write_files: bool = True,
) -> dict:
    """Record a human approve/reject decision against a proposal.

    Returns {"ok": bool, "reason": str, "record": dict|None}. When ``ok`` is
    False the decision was rejected as invalid (e.g., AI tried to self-approve)
    and nothing is written.
    """
    record = {
        "proposal_id": proposal_id,
        "decision": decision,
        "approver": approver,
        "timestamp": now,
        "notes": notes,
        "review_date": review_date,
    }
    if candidate_id:
        # The durable identity. make_proposal_id is clock-salted, so proposal_id
        # churns every run for an unchanged fact; candidate_id does not. Recording
        # it lets one approval outlive the proposal id it was filed against.
        record["candidate_id"] = candidate_id
    ok, reason = S.is_valid_approval_record(record)
    if not ok:
        logger.warning("promotion_approvals: rejecting invalid approval (%s): %s", reason, record)
        return {"ok": False, "reason": reason, "record": None}

    if write_files and _looks_like_repo_root(base_dir):
        msg = (
            f"base_dir_is_repo_root: {base_dir!r} looks like the project root; "
            "approvals must be written under <root>/outputs (pass base_dir=<root>/outputs)"
        )
        logger.warning("promotion_approvals: refusing misdirected write — %s", msg)
        return {"ok": False, "reason": msg, "record": None}

    if write_files:
        data = _load_raw(base_dir)
        approvals = list(data.get("approvals", []))
        approvals.append(record)
        payload = {
            "generated_at": now,
            "schema": "approved_proposals.v1",
            "note": "Human approvals only. AI/product review cannot approve production.",
            "approvals": approvals,
        }
        try:
            safe_write_json(OutputNamespace.PROMOTION_APPROVALS, _APPROVALS_FILE, payload, base_dir=base_dir)
        except Exception as exc:
            logger.warning("promotion_approvals: write failed: %s", exc)
            return {"ok": False, "reason": f"write_failed: {exc}", "record": record}

    return {"ok": True, "reason": "ok", "record": record}


def load_valid_approvals(base_dir: str) -> list[dict]:
    """All structurally-valid approval records (invalid metadata filtered out)."""
    data = _load_raw(base_dir)
    valid: list[dict] = []
    for rec in data.get("approvals", []) or []:
        ok, _ = S.is_valid_approval_record(rec)
        if ok:
            valid.append(rec)
    return valid


def effective_approvals(base_dir: str) -> dict[str, str]:
    """Fold the approval log to the latest valid decision per proposal_id.

    Returns {proposal_id: 'approve'|'reject'}. Order in the file is treated as
    chronological (records are appended), so the last record wins.
    """
    latest: dict[str, str] = {}
    for rec in load_valid_approvals(base_dir):
        latest[rec["proposal_id"]] = rec["decision"]
    return latest


def approved_proposal_ids(base_dir: str) -> set[str]:
    """proposal_ids whose latest valid human decision is 'approve'."""
    return {pid for pid, dec in effective_approvals(base_dir).items() if dec == S.HUMAN_APPROVE}


def rejected_proposal_ids(base_dir: str) -> set[str]:
    return {pid for pid, dec in effective_approvals(base_dir).items() if dec == S.HUMAN_REJECT}


def effective_approvals_by_candidate(base_dir: str) -> dict[str, str]:
    """Fold the approval log to the latest valid decision per candidate_id.

    Mirrors ``effective_approvals`` but keys on the DURABLE identity. Records
    without a ``candidate_id`` (every record written before this field existed)
    are skipped here — they remain fully effective via ``effective_approvals``.

    Returns {candidate_id: 'approve'|'reject'}; file order is chronological, so
    the last record wins.
    """
    latest: dict[str, str] = {}
    for rec in load_valid_approvals(base_dir):
        cid = rec.get("candidate_id")
        if cid:
            latest[str(cid)] = rec["decision"]
    return latest


def approved_candidate_ids(base_dir: str) -> set[str]:
    """candidate_ids whose latest valid human decision is 'approve'."""
    return {cid for cid, dec in effective_approvals_by_candidate(base_dir).items()
            if dec == S.HUMAN_APPROVE}


def rejected_candidate_ids(base_dir: str) -> set[str]:
    """candidate_ids whose latest valid human decision is 'reject'."""
    return {cid for cid, dec in effective_approvals_by_candidate(base_dir).items()
            if dec == S.HUMAN_REJECT}
