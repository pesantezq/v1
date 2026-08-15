"""Install the four proven bootstrap lessons through LIVE independent GPT validation.

Each lesson runs the same anti-poisoning gate as an extracted one: evidence refs
must resolve, the event must be corroborated by authoritative records, the
correction must be supported, the principle must not be overgeneralized, and an
INDEPENDENT GPT reviewer must return PASS. A lesson that fails stays CANDIDATE and
is reported as rejected — importing it anyway would be exactly the lesson poisoning
this kernel exists to prevent.
"""
from __future__ import annotations

import datetime
import json
import sys
import time

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)

from portfolio_automation.engineer_worker.gpt_supervisor import (  # noqa: E402
    SupervisorConfig, review)
from portfolio_automation.engineer_worker.learning import bootstrap, store  # noqa: E402
from portfolio_automation.engineer_worker.learning.config import (  # noqa: E402
    read_learning_config)
from portfolio_automation.engineer_worker.learning.validation import (  # noqa: E402
    LESSON_REVIEW_SYSTEM, consensus_reviewer)

KEY = "/home/pesan/.ew0a_openai_key"
ACTOR = "claude_code"


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


CFG = SupervisorConfig(key_file=KEY, model="gpt-4o", max_completion_tokens=1200)


def reviewer(packet):
    """Independent semantic review of a lesson candidate.

    Reuses the trusted supervisor transport (same credential discipline: the key is
    read only inside the transport, never enters the packet) with the lesson-review
    system prompt substituted for the code-review one."""
    import portfolio_automation.engineer_worker.gpt_supervisor as sup
    original = sup.SUPERVISOR_SYSTEM
    sup.SUPERVISOR_SYSTEM = LESSON_REVIEW_SYSTEM
    try:
        return review(packet, CFG, now)
    finally:
        sup.SUPERVISOR_SYSTEM = original


def main() -> int:
    cfg = read_learning_config(REPO)
    # Majority vote over independent reviews, applied uniformly to every candidate
    # BEFORE any verdict is seen (see validation.consensus_reviewer).
    voting_reviewer = consensus_reviewer(
        reviewer, samples=3, required_passes=2, transport_retries=3,
        backoff_fn=lambda attempt: time.sleep(2 * (attempt + 1)))
    installed, rejected = bootstrap.install_bootstrap_lessons(
        REPO, cfg=cfg, actor=ACTOR, now=now(), semantic_reviewer=voting_reviewer)

    print("== Bootstrap lesson import (live independent GPT validation) ==")
    for lesson in installed:
        print(json.dumps({"lesson_id": lesson.lesson_id, "capability": lesson.capability,
                          "status": lesson.status, "confidence": lesson.confidence,
                          "evidence_refs": len(lesson.evidence_refs),
                          "principle": lesson.principle[:110] + "..."}, indent=2))
    for r in rejected:
        print("REJECTED:", json.dumps({"lesson_id": r["lesson_id"],
                                       "failed_checks": r["failed_checks"],
                                       "semantic_verdict": r["semantic_verdict"],
                                       "reasons": r["reasons"][:3]}, indent=2))
    print(f"\nactivated={len(installed)} rejected={len(rejected)} "
          f"active_total={len(store.active_lessons(REPO))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
