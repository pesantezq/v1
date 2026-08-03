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

import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path

from portfolio_automation.data_governance import (
    OutputNamespace,
    ensure_output_dir,
    get_output_path,
    safe_write_json,
)
from portfolio_automation.sim_governance import schemas as S

logger = logging.getLogger("stockbot.sim_governance.promotion_approvals")

_APPROVALS_FILE = "approved_proposals.json"
_APPROVALS_LOCK_FILE = "approved_proposals.json.lock"


@contextlib.contextmanager
def _approvals_write_lock(base_dir: str):
    """Advisory exclusive lock serializing ``record_approval``'s read-modify-write.

    ``safe_write_text``'s tempfile+``os.replace`` gives atomicity of a SINGLE
    write, but does nothing to make "read the document, append a record, write
    it back" atomic as a unit across two concurrent callers — two humans (or
    two GUI tabs / a single-item click racing a bulk-approve loop) approving
    DIFFERENT proposals at the same moment can both read the same on-disk
    state, both append in memory, and the second writer's replace silently
    discards the first writer's approval — both calls still report ``ok: True``
    (confirmed by a thread-barrier interleaving experiment; see
    ``.superpowers/audit/ws-10-11-12-persistence.md`` WS11.2).

    ``fcntl.flock`` on a dedicated ``.lock`` sidecar file (never the document
    itself, so a lock holder never blocks a plain read of the approvals log)
    is the smallest mechanism that closes this for BOTH threads within one
    process and separate OS processes on Linux — the two shapes that matter
    here (single long-lived uvicorn process today; a second writer process is
    not ruled out for the future). Each call opens its own file descriptor,
    acquires the lock, and unconditionally releases + closes it in ``finally``
    — including on any exception raised by the guarded block — so a crash
    mid-write can never leave the lock held. Because every call opens a FRESH
    descriptor and never re-enters this context manager while already holding
    one (record_approval does not call itself, and nothing else acquires this
    lock), there is no path by which the same process can deadlock against
    itself: the only way to block is a DIFFERENT call (thread or process)
    holding the lock, and that call is guaranteed to release it.
    """
    ensure_output_dir(OutputNamespace.PROMOTION_APPROVALS, _APPROVALS_LOCK_FILE, base_dir=base_dir)
    lock_path = get_output_path(OutputNamespace.PROMOTION_APPROVALS, _APPROVALS_LOCK_FILE,
                                base_dir=base_dir)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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


def approvals_log_unreadable(base_dir: str) -> str | None:
    """Reason string when the approvals log EXISTS but cannot be trusted; else None.

    ``_load_raw`` degrades to ``{"approvals": []}`` on any failure, which cannot
    distinguish two very different situations:

      * the file is ABSENT — a legitimate "no approvals recorded yet";
      * the file is PRESENT but unparseable — the production authority itself is
        unreadable.

    That conflation was harmless while a lost approval merely blocked a NEW
    application. With durable overlays it is not: an empty approval set drops every
    carried op and rewrites the overlay as ``ops: []``, silently reversing
    established production membership — and the write SUCCEEDS, so nothing surfaces.
    Callers that rebuild durable state must fail closed on a non-None result.
    """
    path = Path(get_output_path(OutputNamespace.PROMOTION_APPROVALS, _APPROVALS_FILE,
                                base_dir=base_dir))
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"unreadable_file: {exc}"
    try:
        data = json.loads(raw)
    except Exception as exc:
        return f"unparseable_json: {exc}"
    if not isinstance(data, dict):
        return f"unexpected_top_level_type: {type(data).__name__}"
    if not isinstance(data.get("approvals", []), list):
        return "approvals_field_is_not_a_list"
    return None


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
        # PROVENANCE. `is_human_approver` establishes WHO recorded the decision, but
        # says nothing about how this record came to carry candidate reach. Without
        # that distinction a data migration could stamp candidate_ids onto historical
        # approvals and thereby grant them authority over proposals the operator
        # never saw — measured 2026-08-03: backfilling the 43 legacy records would
        # have silently approved 6 of 10 then-pending proposals. Only a candidate_id
        # supplied AT the human decision counts; see `_candidate_reach_is_human`.
        record["candidate_id_source"] = CANDIDATE_SOURCE_HUMAN
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
        # The unreadable-check and the read-modify-write must happen as ONE
        # atomic unit under the lock: checking outside it would leave a window
        # where a concurrent writer corrupts (or repairs) the file between the
        # check and the read, reintroducing exactly the race this guards
        # against. See ``_approvals_write_lock`` for why flock is sufficient.
        with _approvals_write_lock(base_dir):
            unreadable = approvals_log_unreadable(base_dir)
            if unreadable:
                msg = f"approvals_log_unreadable: {unreadable}"
                logger.error(
                    "promotion_approvals: REFUSING to record approval — %s; a "
                    "read-modify-write through the unreadable log would silently "
                    "discard the existing approval records", msg)
                return {"ok": False, "reason": msg, "record": None}

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
                safe_write_json(OutputNamespace.PROMOTION_APPROVALS, _APPROVALS_FILE, payload,
                                base_dir=base_dir)
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


def _candidate_reach_is_human(rec: dict) -> bool:
    """True only when this record's candidate_id came FROM the human decision.

    A record whose ``candidate_id`` was written by anything other than the
    ``record_approval`` call the human made — most obviously a backfill/migration —
    must not confer candidate-level approval reach, because the operator never
    decided about that candidate. Missing provenance is treated as NOT human: the
    legacy records predate the field, and silently promoting them is precisely the
    failure this guards. They remain fully effective by ``proposal_id`` via
    ``effective_approvals``.
    """
    return rec.get("candidate_id_source") == CANDIDATE_SOURCE_HUMAN


def effective_approvals_by_candidate(base_dir: str) -> dict[str, str]:
    """Fold the approval log to the latest valid decision per candidate_id.

    Mirrors ``effective_approvals`` but keys on the DURABLE identity. Two classes of
    record are skipped, both intentionally:
      - no ``candidate_id`` at all (every record written before the field existed);
      - a ``candidate_id`` without ``candidate_id_source == "human_decision"``
        provenance, i.e. one that arrived by migration rather than by a human
        decision (see ``_candidate_reach_is_human``).
    Both remain fully effective by ``proposal_id`` via ``effective_approvals``.

    Returns {candidate_id: 'approve'|'reject'}; file order is chronological, so
    the last record wins.
    """
    latest: dict[str, str] = {}
    for rec in load_valid_approvals(base_dir):
        cid = rec.get("candidate_id")
        if cid and _candidate_reach_is_human(rec):
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


# Provenance marker for `candidate_id` on an approval record. Only a candidate_id
# supplied at the moment of the human decision confers candidate-level reach; see
# `_candidate_reach_is_human`. Anything else (absent, "derived", a migration's own
# label) is treated as non-human and grants no reach.
CANDIDATE_SOURCE_HUMAN = "human_decision"

_REVOCATIONS_FILE = "production_revocations.jsonl"


def revoke_application(
    target_id: str,
    approver: str,
    now: str,
    *,
    base_dir: str,
    notes: str | None = None,
    write_files: bool = True,
) -> dict:
    """Record a human revocation of a previously-applied production op.

    ``target_id`` may be a proposal_id OR a candidate_id — both are matched when
    the durable watchlist overlay is rebuilt.

    Revocation is the ONLY thing that un-applies a durable watchlist op: evidence
    going stale never restores a symbol, so production membership changes only on
    a recorded human decision. Human-gated exactly like approval.

    Returns {"ok": bool, "reason": str, "record": dict|None}.
    """
    if not target_id:
        return {"ok": False, "reason": "missing target_id", "record": None}
    if not S.is_human_approver(approver):
        logger.warning("promotion_approvals: rejecting non-human revocation by %r", approver)
        return {"ok": False,
                "reason": f"approver {approver!r} is not a valid human approver",
                "record": None}
    if not now:
        return {"ok": False, "reason": "missing timestamp", "record": None}

    record = {"target_id": target_id, "approver": approver,
              "timestamp": now, "notes": notes}

    if write_files and _looks_like_repo_root(base_dir):
        return {"ok": False,
                "reason": f"base_dir_is_repo_root: {base_dir!r}; pass <root>/outputs",
                "record": None}

    if write_files:
        # Append-only JSONL, mirroring production_application's audit log
        # (production_application.py's _AUDIT_FILE append). Never read the
        # existing ledger on the write path and never rewrite the whole
        # document — a single appended line can neither race-lose a concurrent
        # revocation nor be destroyed by another revocation's corrupt read.
        try:
            ensure_output_dir(OutputNamespace.PROMOTION_APPROVALS, _REVOCATIONS_FILE,
                              base_dir=base_dir)
            path = get_output_path(OutputNamespace.PROMOTION_APPROVALS,
                                    _REVOCATIONS_FILE, base_dir=base_dir)
            with Path(path).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            logger.warning("promotion_approvals: revocation write failed: %s", exc)
            return {"ok": False, "reason": f"write_failed: {exc}", "record": record}

    return {"ok": True, "reason": "ok", "record": record}


def revocations_log_unreadable(base_dir: str) -> str | None:
    """Reason string when the revocation ledger EXISTS but cannot be trusted; else None.

    Mirrors ``approvals_log_unreadable`` for the same "absent vs. present-but-
    unparseable" distinction, adapted for a line-oriented (JSONL) append-only
    log rather than a single JSON document:

      * file ABSENT — a legitimate "no revocations recorded yet" → None.
      * file present, every non-blank line parses as a JSON object (including
        the degenerate case of an all-blank/empty file) → None.
      * file present but unreadable at the filesystem level (permissions, I/O
        error) → a reason.
      * file present, non-empty, and NOT ONE non-blank line parses as a JSON
        object → a reason ("wholly corrupt").

    The line-count-zero-parsed rule is the deliberate boundary between two very
    different failure shapes:

      * A crash mid-append can leave a single torn TRAILING line (e.g. a
        partially-flushed write). ``revoked_ids`` already skips any line that
        fails to parse, so a torn tail costs nothing but that one record and
        must not fail the whole pipeline closed — this is a known, accepted
        residual risk of an append-only line log, not an authority failure.
      * A file where NO line parses at all — every line corrupt, or the file
        truncated/overwritten with non-JSONL content — means nothing in it can
        be recovered even partially. Treating that as "readable" would let
        ``revoked_ids`` silently degrade toward ``set()`` and resurrect a
        revoked op into the live production overlay, exactly the defect this
        guard exists to close.

    So: any file with at least one parseable line is trusted (partial-tail-loss
    tolerated); a file with zero parseable lines despite being non-empty is
    unreadable (total corruption is not tolerated).
    """
    path = Path(get_output_path(OutputNamespace.PROMOTION_APPROVALS,
                                _REVOCATIONS_FILE, base_dir=base_dir))
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"unreadable_file: {exc}"

    total = 0
    parsed = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        total += 1
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            parsed += 1

    if total == 0:
        return None
    if parsed == 0:
        return f"wholly_corrupt: 0 of {total} line(s) parsed as a JSON object"
    return None


def revoked_ids(base_dir: str) -> set[str]:
    """Target ids (proposal_id or candidate_id) with a valid human revocation."""
    out: set[str] = set()
    try:
        path = Path(get_output_path(OutputNamespace.PROMOTION_APPROVALS,
                                    _REVOCATIONS_FILE, base_dir=base_dir))
        if not path.exists():
            return set()
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            # One corrupt line must not discard the valid revocations around it.
            continue
        if not isinstance(rec, dict):
            continue
        tid = rec.get("target_id")
        if tid and rec.get("timestamp") and S.is_human_approver(rec.get("approver")):
            out.add(str(tid))
    return out
