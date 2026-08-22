"""The G1 preregistration freeze — content-digested, commit-anchored, verifiable.

WHAT WAS WRONG BEFORE THIS MODULE EXISTED.

``criteria.py`` carried ``CRITERIA_FROZEN_AT_CANDIDATE = "3bdb329a..."``, the
parent commit. That commit does not contain ``criteria.py`` at all -- G1 did not
exist yet. The claim was therefore not merely imprecise, it was checkable and
false: ``git cat-file -e 3bdb329a:.../g1/criteria.py`` fails.

A preregistration that names a commit not containing the registered material is
worse than none. It reads as a stronger guarantee than "we wrote the criteria
first" while actually proving less, and it would have survived indefinitely
because nobody re-checks a SHA that looks plausible.

HOW THE FREEZE WORKS NOW.

The freeze is a CONTENT DIGEST over everything that must not move after
measurement begins: the evaluation criteria, the outcome taxonomy, and for every
case its version, packet fingerprint, expected verdict, acceptable alternates,
gold basis, gold provenance, split and severity.

The digest is computed from that material alone -- never from a commit id -- so
it is stable across the commit that introduces it and any later commit that adds
a pointer to it. That property is what resolves the obvious ordering problem: a
commit cannot contain its own SHA.

    1. commit A introduces the frozen material and ``preregistration.json``,
       which carries the digest.
    2. commit A's SHA is read afterwards and recorded in the pointer artifact.
    3. verification proves that the CURRENT code still digests to the value
       recorded in commit A -- by reading that file out of commit A with git,
       not by trusting the working tree.

Step 3 is the load-bearing one. Anyone can re-run it, and it fails if the
corpus, the criteria or the taxonomy moved after the freeze.

WHAT THIS DOES NOT CLAIM.

It does not claim the gold labels are correct, or human-adjudicated. It claims
only that they were fixed BEFORE the scored run and have not changed since. That
is a narrow guarantee, and stating it narrowly is the point.

``experimental_noncanonical``.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from portfolio_automation.engineer_worker.g1 import G1_NAMESPACE, G1_SCHEMA_KIND
from portfolio_automation.engineer_worker.g1 import corpus as CORPUS
from portfolio_automation.engineer_worker.g1 import criteria as CRIT
from portfolio_automation.engineer_worker.g1 import taxonomy as TAX

PREREGISTRATION_SCHEMA_VERSION = f"{G1_NAMESPACE}.preregistration.v1"

#: Where the frozen content lives, relative to the repository root.
PREREGISTRATION_REL = "evals/g1/preregistration.json"
#: Where the commit anchor lives. A SEPARATE file, written after the freeze
#: commit exists, so the frozen content never has to contain its own SHA.
FREEZE_POINTER_REL = "evals/g1/preregistration_freeze.json"

UNCOMMITTED = "UNCOMMITTED"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), default=str)


def frozen_content() -> dict[str, Any]:
    """Everything that must not move once scoring begins.

    Deliberately explicit rather than "hash the source files": a whitespace or
    docstring edit should not invalidate a freeze, and a changed gold label
    must. Hashing MEANING, not bytes."""
    cases = []
    for c in CORPUS.ALL_CASES:
        cases.append({
            "case_id": c.case_id,
            "case_version": c.case_version,
            "fingerprint": c.fingerprint(),
            "source_class": c.source_class.value,
            "split": c.split.value,
            "severity": c.severity.value,
            "expected_supervisor_verdict": c.expected_supervisor_verdict.value,
            "acceptable_alternate_verdicts": sorted(
                v.value for v in c.acceptable_alternate_verdicts),
            "gold_basis": c.gold_basis.value,
            "gold_provenance": c.gold_provenance,
            "protected_high_impact": c.protected_high_impact,
        })
    return {
        "schema_version": PREREGISTRATION_SCHEMA_VERSION,
        "schema_kind": G1_SCHEMA_KIND,
        "criteria": CRIT.criteria_manifest(),
        "taxonomy": TAX.taxonomy_manifest(),
        "corpus": {
            "rotation_epoch": CORPUS.ROTATION_EPOCH,
            "n_cases": len(CORPUS.ALL_CASES),
            "by_split": {s.value: len(CORPUS.cases(s))
                         for s in {c.split for c in CORPUS.ALL_CASES}},
            "cases": sorted(cases, key=lambda d: d["case_id"]),
        },
        "audit_policy": {
            "min_human_audit_fraction": CRIT.MIN_HUMAN_AUDIT_FRACTION,
            "rule": CRIT.MIN_HUMAN_AUDIT_RULE,
            "selection": ("deterministic, keyed on record_id, drawn from the "
                          "same scored population compute_metrics uses, target "
                          "computed with ceil so the fraction is a true "
                          "minimum"),
            "membership": ("an adjudication counts only if its record_id is in "
                           "the selected sample"),
        },
        "sample_size_policy": {
            "min_cell_n_for_rate": CRIT.MIN_CELL_N_FOR_RATE,
            "rule": CRIT.SAMPLE_SIZE_RULE,
        },
        "scoring_semantics": {
            "accuracy_population": TAX.ACCURACY_POPULATION.value,
            "false_pass": CRIT.FALSE_PASS_DEFINITION,
            "false_fail": CRIT.FALSE_FAIL_DEFINITION,
            "excluded": CRIT.EXCLUDED_CLASSES,
            "pass_never_an_alternate": (
                "PASS may never be an acceptable alternate verdict for a case "
                "whose expected verdict is a refusal; enforced in "
                "EvaluationCaseV0.__post_init__"),
            "threshold_position": CRIT.THRESHOLD_POSITION,
        },
    }


def freeze_digest() -> str:
    """Digest of the frozen content. Never includes a commit id."""
    return "g1freeze_" + hashlib.sha256(
        _canonical(frozen_content()).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class FreezeVerification:
    ok: bool
    reasons: tuple[str, ...]
    recorded_commit: str
    recorded_digest: str
    current_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": list(self.reasons),
                "recorded_commit": self.recorded_commit,
                "recorded_digest": self.recorded_digest,
                "current_digest": self.current_digest}


def _git(repo_root: Path, *args: str) -> Optional[str]:
    try:
        done = subprocess.run(("git", *args), cwd=str(repo_root),
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def verify_freeze(repo_root: str | Path) -> FreezeVerification:
    """Prove the freeze, or say exactly why it cannot be proven.

    Three independent checks, all of which must hold:

      1. the pointer artifact names a commit and a digest;
      2. the CURRENT code digests to that same value -- so nothing in the
         criteria, taxonomy, splits or gold labels has moved;
      3. the preregistration file AS COMMITTED at that commit carries the same
         digest -- read with ``git show``, not from the working tree, because a
         working-tree read would compare the freeze to itself.

    Check 3 is the one that makes this more than a self-assertion."""
    root = Path(repo_root)
    reasons: list[str] = []
    current = freeze_digest()

    pointer_path = root / FREEZE_POINTER_REL
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return FreezeVerification(
            False, (f"freeze pointer unreadable at {FREEZE_POINTER_REL}: {exc}",),
            UNCOMMITTED, UNCOMMITTED, current)

    commit = str(pointer.get("preregistration_commit") or UNCOMMITTED)
    recorded = str(pointer.get("freeze_digest") or UNCOMMITTED)

    if commit == UNCOMMITTED or not commit:
        reasons.append("the pointer records no preregistration commit")
    if recorded != current:
        reasons.append(
            f"the frozen material has CHANGED since the freeze: recorded "
            f"{recorded}, current {current}")

    if commit and commit != UNCOMMITTED:
        blob = _git(root, "show", f"{commit}:{PREREGISTRATION_REL}")
        if blob is None:
            reasons.append(
                f"commit {commit[:12]} does not contain {PREREGISTRATION_REL}; "
                "a freeze point that does not hold the registered material "
                "proves nothing")
        else:
            try:
                committed = json.loads(blob)
            except ValueError as exc:
                reasons.append(f"committed preregistration unparseable: {exc}")
            else:
                committed_digest = str(committed.get("freeze_digest", ""))
                if committed_digest != current:
                    reasons.append(
                        f"the digest committed at {commit[:12]} "
                        f"({committed_digest}) does not match the current "
                        f"material ({current})")

    return FreezeVerification(not reasons, tuple(reasons), commit, recorded,
                              current)


def preregistration_artifact() -> dict[str, Any]:
    """The file written into the freeze commit. Contains no commit id."""
    content = frozen_content()
    return {**content, "freeze_digest": freeze_digest()}


def freeze_pointer(commit: str) -> dict[str, Any]:
    """The anchor, written AFTER the freeze commit exists."""
    return {
        "schema_version": PREREGISTRATION_SCHEMA_VERSION,
        "schema_kind": G1_SCHEMA_KIND,
        "preregistration_commit": commit,
        "freeze_digest": freeze_digest(),
        "registered_file": PREREGISTRATION_REL,
        "verification": (
            "verify_freeze() re-derives the digest from current code and "
            "compares it against the copy of preregistration.json committed at "
            "preregistration_commit, read via git show. It does not trust the "
            "working tree."),
        "scope": (
            "This proves the criteria, taxonomy, splits, gold labels and gold "
            "provenance were fixed before the scored run and have not changed "
            "since. It does NOT claim the gold labels are correct, nor that "
            "they are human-adjudicated."),
    }
