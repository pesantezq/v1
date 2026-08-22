"""G1 benchmark runner — evaluate one configuration against the case corpus.

WHY THE SUPERVISOR IS INJECTED.

The runner takes a callable, not a model name. Two consequences that matter:

  * the deterministic tests drive it with a scripted decision function, so
    metric behaviour is reproducible without a network or a credential;
  * the live run drives it with ``gpt_supervisor.review`` -- the SAME governed
    path production uses, including its secret screen and its fail-closed
    error handling. G1 does not get a private route to the model.

WHY CONFIGURATION IS DATA.

``model``, ``prompt_version`` and ``toolset`` are recorded per result rather
than baked into the storage format, so prompt A and prompt B can be compared
without rewriting history (AC16). A record already written is never mutated;
a new configuration produces new records with a different ``config_id``.

WHAT THIS MODULE REFUSES TO DO.

It does not tune anything, and it will not read HELD_OUT unless the caller asks
for that split by name. It also never stamps its own timestamps -- ``now_fn`` is
injected, because a record that reads the clock cannot be replayed.

``experimental_noncanonical``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from portfolio_automation.engineer_worker.execution_identity import (
    UNAVAILABLE, build_execution_identity, supervisor_prompt_version,
)
from portfolio_automation.engineer_worker.g1 import G1_NAMESPACE, G1_SCHEMA_KIND
from portfolio_automation.engineer_worker.g1.contracts import (
    EvaluationCaseV0, MeasurementConfig, RunPopulation,
    SupervisorEvaluationRecordV0, classify,
)
from portfolio_automation.engineer_worker.g1.corpus import SplitLeakError
from portfolio_automation.engineer_worker.g1.taxonomy import OutcomeClass, Population
from portfolio_automation.engineer_worker.g1.contracts import Split
from portfolio_automation.engineer_worker.gpt_supervisor import (
    SupervisorDecision, SupervisorVerdict,
)

RUNNER_SCHEMA_VERSION = f"{G1_NAMESPACE}.runner.v1"

#: A decision function: packet -> SupervisorDecision. Exactly the shape
#: ``certify_attempt`` already passes around, so the same fakes work in both.
SupervisorFn = Callable[[Mapping[str, Any]], SupervisorDecision]

_VERDICT_TO_OUTCOME = {
    SupervisorVerdict.PASS: OutcomeClass.PASS,
    SupervisorVerdict.REPAIR: OutcomeClass.REPAIR,
    SupervisorVerdict.ESCALATE: OutcomeClass.ESCALATE,
    SupervisorVerdict.ABSTAIN: OutcomeClass.ABSTAIN,
    SupervisorVerdict.SUPERVISOR_UNAVAILABLE: OutcomeClass.SUPERVISOR_UNAVAILABLE,
}


def outcome_of(decision: SupervisorDecision) -> OutcomeClass:
    """Map a decision onto the taxonomy, distinguishing HOW it failed.

    An outage is not one thing. A parse failure and an auth failure both leave
    the work unverified, but they call for different repairs, and collapsing
    them loses the only signal that says which."""
    verdict = _VERDICT_TO_OUTCOME.get(
        decision.verdict, OutcomeClass.SUPERVISOR_UNAVAILABLE)
    if verdict is not OutcomeClass.SUPERVISOR_UNAVAILABLE:
        return verdict
    err = (decision.error or "").lower()
    if "unparseable" in err or "no json" in err or "malformed" in err \
            or "non-decision" in err:
        return OutcomeClass.MALFORMED_RESPONSE
    if "timeout" in err:
        return OutcomeClass.TIMEOUT
    if "auth" in err or "401" in err or "key" in err:
        return OutcomeClass.AUTH_FAILURE
    if "link failed" in err or "urlerror" in err or "connection" in err \
            or "oserror" in err:
        return OutcomeClass.TRANSPORT_FAILURE
    return OutcomeClass.SUPERVISOR_UNAVAILABLE


@dataclass(frozen=True)
class RunResult:
    records: tuple[SupervisorEvaluationRecordV0, ...]
    config: MeasurementConfig
    splits_run: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": RUNNER_SCHEMA_VERSION,
                "schema_kind": G1_SCHEMA_KIND,
                "config": self.config.to_dict(),
                "splits_run": list(self.splits_run),
                "records": [r.to_dict() for r in self.records]}


def _identity(case: EvaluationCaseV0, cfg: MeasurementConfig, *,
              worker_id: str) -> dict[str, Any]:
    """Build a REAL ExecutionIdentity. Not a G1-local imitation of one."""
    ident = build_execution_identity(
        worker_id=worker_id,
        worker_role="independent_reviewer",
        model_provider=cfg.model_provider,
        model_name=cfg.model_name,
        # Left UNAVAILABLE on purpose: the pre-call identity cannot know the
        # build the API will serve. The served build is recorded on the RECORD
        # (served_model_version), where it is a post-call observation. It is
        # deliberately NOT on MeasurementConfig: a configuration whose identity
        # changes once it has run cannot be joined back to its own records.
        model_version=UNAVAILABLE,
        prompt_version=cfg.prompt_version,
        instruction_version=cfg.instruction_version,
        toolset=cfg.toolset_id,
        tool_config={"toolset": cfg.toolset_id, "protocol": cfg.instruction_version},
        candidate_sha=str(case.packet.get("candidate_sha") or UNAVAILABLE),
        task_id=str((case.packet.get("task") or {}).get("task_id") or UNAVAILABLE),
        mission_id=str(case.packet.get("mission_id") or UNAVAILABLE),
        input_id=case.fingerprint())
    return ident.to_dict()


class FreezeNotReady(RuntimeError):
    """Formal scoring was attempted before the freeze could be verified.

    The gate lives in the library, not only in the run script, so a future
    caller cannot reach the supervisor by writing its own loop. Verification is
    performed here rather than accepted as an argument: a boolean supplied by
    the party that wants to score is not evidence."""


def _assert_freeze_ready(repo_root, expected_digest: str) -> None:
    from portfolio_automation.engineer_worker.g1.preregistration import verify_freeze

    v = verify_freeze(repo_root)
    if not v.ok:
        raise FreezeNotReady(
            f"refusing to score: the preregistration does not verify: "
            f"{list(v.reasons)}")
    if v.current_digest != expected_digest:
        raise FreezeNotReady(
            f"refusing to score: the run declares freeze {expected_digest} but "
            f"the working tree registers {v.current_digest}; the corpus or "
            "criteria moved after the freeze")
    if not v.commit_available:
        # Indeterminate, not refuted -- but a FORMAL run is exactly where the
        # commit-level proof matters, so it is required here even though
        # verify_freeze itself tolerates absence.
        raise FreezeNotReady(
            "refusing to score: the freeze commit object is not present in this "
            f"checkout, so containment cannot be proven: "
            f"{list(v.indeterminate_reasons)}")


def run_cases(cases: Sequence[EvaluationCaseV0], supervisor: SupervisorFn, *,
              config: MeasurementConfig, now_fn: Callable[[], str],
              run_id: str, repo_root: Any = None,
              population: RunPopulation = RunPopulation.PREREGISTERED_FORMAL,
              preregistration_digest: str = UNAVAILABLE,
              worker_id: str = "g1_supervisor_benchmark",
              measure_latency: bool = False,
              allow_held_out: bool = False) -> RunResult:
    """Score ``cases`` under one configuration.

    ``allow_held_out`` must be set explicitly to touch the held-out split. The
    guard is not about mistrusting the caller; it is about making a held-out
    read appear in the source, where a reviewer will see it.

    ``run_id`` is REQUIRED and undefaulted. Records from two runs of the same
    corpus under the same configuration must be distinguishable, or the second
    run looks like a correction of the first rather than a second observation.

    ``population`` and ``preregistration_digest`` travel on every record so a
    later reader can tell a preregistered result from an exploratory one without
    knowing which file it came out of."""
    if population is RunPopulation.PREREGISTERED_FORMAL:
        if preregistration_digest == UNAVAILABLE:
            raise ValueError(
                "a PREREGISTERED_FORMAL run requires a preregistration_digest; "
                "without one there is nothing proving what was frozen before "
                "the measurement, which is the only thing that makes it "
                "preregistered")
        if repo_root is None:
            raise ValueError(
                "a PREREGISTERED_FORMAL run requires repo_root so the freeze "
                "can be verified HERE, before any call is made. A caller-"
                "supplied 'the freeze is fine' boolean would be "
                "self-certification")
        _assert_freeze_ready(repo_root, preregistration_digest)
    leaked = [c.case_id for c in cases if c.split is Split.HELD_OUT]
    if leaked and not allow_held_out:
        raise SplitLeakError(
            f"held-out cases reached the runner without allow_held_out=True: "
            f"{leaked}. Tuning against held-out data is the one thing the split "
            "exists to prevent.")

    records: list[SupervisorEvaluationRecordV0] = []
    for case in cases:
        started = time.perf_counter() if measure_latency else None
        decision = supervisor(dict(case.packet))
        latency_ms = (int((time.perf_counter() - started) * 1000)
                      if started is not None else None)
        actual = outcome_of(decision)
        records.append(SupervisorEvaluationRecordV0(
            case_id=case.case_id,
            case_fingerprint=case.fingerprint(),
            expected_verdict=case.expected_supervisor_verdict,
            actual_outcome=actual,
            match_class=classify(case, actual),
            severity=case.severity,
            split=case.split,
            gold_basis=case.gold_basis,
            execution_identity=_identity(case, config, worker_id=worker_id),
            config=config,
            candidate_sha=str(case.packet.get("candidate_sha") or UNAVAILABLE),
            served_model_version=str(getattr(decision, "model", None) or UNAVAILABLE),
            supervisor_reasons=tuple(getattr(decision, "reasons", ()) or ())[:6],
            supervisor_error=getattr(decision, "error", None),
            latency_ms=latency_ms,
            recorded_at=now_fn(),
            protected_high_impact=case.protected_high_impact,
            run_id=run_id, population=population,
            preregistration_digest=preregistration_digest))
    return RunResult(records=tuple(records), config=config,
                     splits_run=tuple(sorted({c.split.value for c in cases})))


def live_supervisor(key_file: str, *, model: str = "gpt-4o",
                    max_completion_tokens: int = 900) -> tuple[SupervisorFn, dict]:
    """The GOVERNED live path. Returns ``(fn, observed)``.

    ``observed`` accumulates the served build the API reports, which is the one
    identity attribute that is genuinely only knowable after a call. Nothing
    here reads or returns credential material: the key is opened inside
    ``gpt_supervisor``'s transport, only to build an Authorization header."""
    from portfolio_automation.engineer_worker.gpt_supervisor import (
        SupervisorConfig, review)

    cfg = SupervisorConfig(key_file=key_file, model=model,
                           max_completion_tokens=max_completion_tokens)
    observed: dict[str, Any] = {"served_models": set(), "calls": 0, "errors": []}

    def fn(packet: Mapping[str, Any]) -> SupervisorDecision:
        d = review(dict(packet), cfg, lambda: UNAVAILABLE)
        observed["calls"] += 1
        if d.model:
            observed["served_models"].add(d.model)
        if d.error:
            observed["errors"].append(d.error)
        return d

    return fn, observed


def config_for_live(model: str, *, provider: str = "openai") -> MeasurementConfig:
    """Configuration describing the live production supervisor.

    ``prompt_version`` is the digest of the ACTUAL instruction text, via the
    same helper the production path uses -- so a prompt edit shows up here as a
    different configuration rather than an unchanged label."""
    from portfolio_automation.engineer_worker import gpt_supervisor

    return MeasurementConfig(
        model_provider=provider,
        model_name=model,
        prompt_version=supervisor_prompt_version(
            getattr(gpt_supervisor, "SUPERVISOR_SYSTEM", "")),
        instruction_version="one-shot",
        toolset_id="gpt_supervisor.review")
