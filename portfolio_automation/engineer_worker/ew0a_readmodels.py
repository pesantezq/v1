"""Worker Control Center — controller-owned read-model projections.

Trusted, NON-AUTHORITATIVE projections of authoritative EW-0A state for the GUI:

    authoritative state  ->  trusted projection (here)  ->  GUI

NOT the reverse. This module is READ-ONLY by construction: it imports only the
read accessors (read_authority_level, read_runtime_policy, read_outcomes) and
never any mutation function (no set_authority_level, no write_runtime_policy, no
certify/dispatch). Projections carry NO secrets (no API key/headers/hidden
reasoning). Fields with no authoritative backend are ``PENDING_BACKEND`` — never
fabricated (no invented heartbeat/health/latency/queue).

GUI-R REPAIR (controller read-model repair). Reconciliation found this module
truthful about the backends nobody had built, and quietly untruthful about four
things it did emit:

  * ``active_session`` and ``learning`` were appended AFTER the truth assessment
    and therefore carried no truth state at all — the one projection that answers
    "what is happening now" was the one projection nobody had classified;
  * run/outcome history was absent, which is why the GUI grew a second,
    independent interpretation of an outcome ledger;
  * ``attention_items = []`` was a literal, indistinguishable from a derivation
    that had run and found nothing;
  * ``controller="ACTIVE"`` / ``control_loop="READY"`` and five ``can_*``
    booleans were hardcoded, presenting assertions and dataclass defaults as
    derived truth.

Every repair here either adds evidence or removes an assertion. None of them
builds a backend: no heartbeat, no queue, no health probe, no controller-session
record. Readiness is expected to stay ``PARTIAL``, because it is.

``experimental_noncanonical``.
"""
from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER
from portfolio_automation.engineer_worker.ew0a import read_outcomes
from portfolio_automation.engineer_worker.ew0a_authority import (
    read_authority_level, EngineerAuthorityLevel, FORBIDDEN_OPS)
from portfolio_automation.engineer_worker.ew0a_loop import read_runtime_policy
from portfolio_automation.engineer_worker.control_center_truth import (
    Capability, Readiness, ReadinessAssessment, TruthState, assess_readiness,
    classify,
)

SCHEMA_KIND = EXPERIMENTAL_MARKER
#: v1, not v0. GUI-SR changed this contract incompatibly for any consumer that
#: assumed the supervisor/apprenticeship count fields were always integers: they
#: are now ``int | None``, where ``None`` means the controller-record ledger
#: could not answer. Three ``records_evidence`` fields and a top-level
#: ``controller_records`` surface were added at the same time. A schema version
#: exists precisely to let a consumer tell those two contracts apart, and
#: leaving it at v0 because the read model is experimental would defeat the one
#: mechanism that communicates the difference.
#:
#: v1 rather than v2: authoritative main still publishes v0, and PR #35 -- which
#: also changed this shape -- is frozen and unmerged. GUI-SR is therefore the
#: first candidate to establish the next PUBLISHED contract. When GUI-R is
#: integrated onto hardened main it should reconcile to this same v1 rather than
#: inventing v2 because an unmerged branch also moved.
READMODEL_SCHEMA_VERSION = "engineering.readmodel.v1"
PENDING_BACKEND = "PENDING_BACKEND"

#: The engineering outcome/run ledger. Named here rather than left as a literal
#: at the call site so the GUI can stop naming it itself — a consumer that has to
#: know the path is a consumer doing its own interpretation.
OUTCOME_LEDGER_REL = "docs/EW0A_CERTIFICATION_OUTCOMES.jsonl"

#: The controller apprenticeship / certification records ledger. A DIFFERENT
#: evidence domain from the outcome ledger above, with a different verdict field
#: (``gpt_verdict`` here, ``supervisor_verdict`` there). Collapsing the two into
#: one supervisor number produces a count that belongs to neither.
CONTROLLER_RECORDS_REL = "docs/EW0A_0B3_RECORDS.jsonl"

#: Producers this module projects. Named so an import can be attempted by name
#: and its FAILURE MODE classified, rather than every ImportError being read as
#: proof that nobody built the thing.
_SESSION_PRODUCER_MODULE = "tools.ns0c_session"
_LEARNING_PRODUCER_MODULE = "portfolio_automation.engineer_worker.learning.readmodels"

#: The session producer's own "there is no session" answer. Duplicated here so
#: this module does not have to import it before it knows the producer loaded;
#: a test pins it against `tools.ns0c_session.NO_SESSION` so it cannot drift.
_NO_SESSION_STATE = "NO_SUCH_SESSION"

#: This projection's own read-model name. Taken from a constant rather than the
#: producer's copy of it.
_SESSION_READ_MODEL = "Northstar0CSessionSummary"

# Producer status. Distinguishing these three is the whole point: only ABSENT is
# engineering incompleteness, and only ABSENT may produce PENDING_BACKEND.
_PRODUCER_OK = "OK"
_PRODUCER_ABSENT = "ABSENT"
_PRODUCER_UNAVAILABLE = "UNAVAILABLE"


def _missing_module_is(exc: ModuleNotFoundError, target: str) -> bool:
    """True only when the module that could not be found IS the producer.

    A ``ModuleNotFoundError`` raised from INSIDE a producer names the producer's
    missing dependency, not the producer. Treating that as absence would report
    a broken installation as unfinished engineering and send an operator to
    write code that already exists."""
    name = getattr(exc, "name", None)
    if not name:
        return False
    # `target` itself, or a parent package of it, genuinely being absent means
    # the producer cannot exist. Anything else is a dependency of the producer.
    return name == target or target.startswith(name + ".")


def _import_producer(module_name: str) -> tuple[Any, str, str]:
    """Import a producer by name and classify the outcome.

    Returns ``(module_or_None, status, detail)``. ``detail`` carries only the
    exception TYPE and the missing module NAME -- never an exception payload,
    which can carry paths or values this projection must not render."""
    try:
        return importlib.import_module(module_name), _PRODUCER_OK, ""
    except ModuleNotFoundError as exc:
        if _missing_module_is(exc, module_name):
            return None, _PRODUCER_ABSENT, f"{module_name} does not exist"
        return None, _PRODUCER_UNAVAILABLE, (
            f"{module_name} exists but an import inside it failed "
            f"(ModuleNotFoundError: {exc.name})")
    except ImportError as exc:
        return None, _PRODUCER_UNAVAILABLE, (
            f"{module_name} exists but failed to import ({type(exc).__name__})")
    except Exception as exc:  # noqa: BLE001 - a producer that explodes on import exists
        return None, _PRODUCER_UNAVAILABLE, (
            f"{module_name} raised on import ({type(exc).__name__})")


def _base(kind: str) -> dict[str, Any]:
    return {"schema_version": READMODEL_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND, "read_model": kind}


# --- ControllerSummary (dynamic identity — never hardcodes Claude==controller) -
@dataclass(frozen=True)
class ControllerSummary:
    controller_identity: str            # the CURRENT controller; see identity_basis
    controller_role: str                # "authoritative_controller"
    controller_level: str               # "C_AUTHORITATIVE" (controller ladder; Engineer C0.5 tracked separately)
    current_mission: str | None
    operational_state: str              # PENDING_BACKEND (no health producer exists)
    controller_since: str               # PENDING_BACKEND if not authoritatively recorded
    escalation_role: str                # who this controller escalates TO
    #: How ``controller_identity`` was arrived at. No ControllerStateV0 producer
    #: exists, so the identity is an implementation assumption, not an
    #: observation — and a consumer must be able to tell the difference before
    #: rendering "the controller is X" as a fact.
    identity_basis: str = "ASSUMED_NOT_OBSERVED"
    #: Fields that are constant because the INTERFACE defines them, not because
    #: nobody got round to deriving them. Listed so the audit is machine-readable
    #: rather than a comment.
    contract_constants: tuple[str, ...] = (
        "controller_role", "controller_level", "escalation_role")
    security_classification: str = "operational"
    is_current_state: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {**_base("ControllerSummary"), **asdict(self)}


# --- SupervisorSummary (GPT independent verifier — never exposes the key) -----
@dataclass(frozen=True)
class SupervisorSummary:
    availability: str                   # "AVAILABLE" | "OUTAGE" | PENDING_BACKEND
    current_state: str
    #: ``None`` means the records ledger could not answer -- never a measured 0.
    recent_pass: int | None
    recent_repair: int | None
    recent_escalate: int | None
    recent_abstain: int | None
    recent_unavailable: int | None
    last_successful_verification: str | None
    measured_latency_ms: str            # PENDING_BACKEND (no real latency record)
    verification_queue: str             # PENDING_BACKEND (no real queue)
    outage_state: str
    #: Whether the underlying records ledger was usable at all (GUI-SR).
    records_evidence: str = TruthState.LIVE.value
    #: Which ledger these counts came from, and under which field name (GUI-R).
    #: Two legitimate ledgers record supervisor verdicts for different purposes;
    #: a consumer must be able to say which one it is showing.
    source: str = CONTROLLER_RECORDS_REL
    source_kind: str = "controller_records_ledger"
    verdict_field: str = "gpt_verdict"
    evidence_domain: str = "controller_apprenticeship_and_certification"
    security_classification: str = "operational"

    def to_dict(self) -> dict[str, Any]:
        return {**_base("SupervisorSummary"), **asdict(self)}


# --- Worker + authority ------------------------------------------------------
#: Which forbidden operation decides each projected capability. The mapping is
#: explicit so the derivation can be read, tested and audited. The previous
#: version carried these as dataclass defaults, which happened to match the A1
#: posture — and would have kept matching it after the posture changed.
_AUTHORITY_CAPABILITY_OPS: dict[str, str] = {
    "can_mutate_main": "MAIN_WRITE",
    "can_merge": "MERGE",
    "can_deploy": "DEPLOY",
    "can_write_production": "PRODUCTION_WRITE",
    "can_self_promote": "SELF_PROMOTION",
}


def effective_denied_ops(record_forbidden_ops: Any = None) -> frozenset[str]:
    """The operations actually denied: the UNION of the module's permanent
    boundary and whatever the authority record additionally forbids.

    A union, not a substitution. ``FORBIDDEN_OPS`` is denied at EVERY level, so a
    record that omits an operation must not thereby grant it — a record may only
    ever be stricter. Fail-closed by construction rather than by review."""
    if record_forbidden_ops is None:
        return frozenset(FORBIDDEN_OPS)
    # Validated, not coerced. This used to be `{str(op) for op in ...}`, so a
    # record element `{"api_key": "sk-..."}` was rendered into
    # worker_authority.forbidden_ops -- the same leak as failure_classes, in a
    # sibling function, fixed one round later.
    #
    # The container is checked BEFORE any iteration. Reaching for
    # `list(record_forbidden_ops)` first would iterate a bare string into
    # characters, so "MERGE" would validate as five one-letter operation names --
    # the same string-is-not-a-list mistake, one function over.
    if isinstance(record_forbidden_ops, (tuple, set, frozenset)):
        record_forbidden_ops = sorted(record_forbidden_ops, key=repr)
    return frozenset(FORBIDDEN_OPS) | frozenset(
        _validated_string_list("forbidden_ops", record_forbidden_ops))


def derive_authority_capabilities(denied_ops: Iterable[str]) -> dict[str, bool]:
    """Project the capability booleans FROM the denial set.

    Pure and total, so a test can prove the values are computed by varying the
    input rather than by trusting that a default happens to be right today.

    Takes ``Iterable[str]``, not ``Any``: the previous permissive signature
    reintroduced coercion through ``str(op)``, which is how a public helper
    quietly became a rendering path for arbitrary evidence."""
    if isinstance(denied_ops, (tuple, set, frozenset)):
        denied_ops = sorted(denied_ops, key=repr)
    denied = frozenset(_validated_string_list("denied_ops", denied_ops))
    return {cap: op not in denied for cap, op in _AUTHORITY_CAPABILITY_OPS.items()}


@dataclass(frozen=True)
class WorkerAuthoritySummary:
    level: str
    grants: list[str]
    forbidden_ops: list[str]
    # NO DEFAULTS. A default here is indistinguishable from a derivation that
    # returned the same value, which is exactly the confusion this repair
    # removes: the builder must compute every one of them.
    can_mutate_main: bool
    can_merge: bool
    can_deploy: bool
    can_write_production: bool
    can_self_promote: bool
    capabilities_derived_from: str = "FORBIDDEN_OPS | authority_record.forbidden_ops"
    #: Whether the RECORD's own list fields were usable. The level itself comes
    #: from a separate reader that fails closed to A0, so authority can be
    #: enforceable while the record's grants/denials are unreadable.
    record_evidence: str = "LIVE"
    record_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**_base("WorkerAuthoritySummary"), **asdict(self)}


@dataclass(frozen=True)
class WorkerSummary:
    worker_identity: str                # persistent identity, e.g. "engineer.local_qwen2_5_7b"
    role: str                           # "engineer"
    operational_state: str              # PENDING_BACKEND (no WorkerHeartbeatV0 yet)
    ew_authority: str
    controller_level: str               # "C0.5_SHADOW" (apprenticeship)
    current_mission: str | None
    current_task: str                   # PENDING_BACKEND (no live dispatch record)
    queue_size: str                     # PENDING_BACKEND
    activity_summary: str               # PENDING_BACKEND
    next_action: str                    # PENDING_BACKEND
    recent_verification_outcomes: list[str]
    escalation_state: str
    #: Whether the records ledger behind recent_verification_outcomes was usable
    #: (GUI-SR). An empty list from an unusable ledger is not an absence of
    #: verdicts, and a consumer reading only this summary must be able to tell
    #: the difference.
    records_evidence: str = TruthState.LIVE.value
    #: EW-0A defines exactly one Engineer Worker with a persistent identity, so
    #: this is a contract constant rather than an unbuilt lookup (GUI-R). It
    #: becomes a derivation the moment a second worker exists — a later mission,
    #: and deliberately not this one.
    identity_basis: str = "CONTRACT_CONSTANT"
    contract_constants: tuple[str, ...] = (
        "worker_identity", "role", "controller_level")

    def to_dict(self) -> dict[str, Any]:
        return {**_base("WorkerSummary"), **asdict(self)}


# --- Mission (progress from VERIFIED deliverables, NOT raw task counts) -------
@dataclass(frozen=True)
class MissionSummary:
    mission_id: str
    deliverables: dict[str, str]        # name -> VERIFIED|NOT_STARTED|IN_PROGRESS
    verified_count: int
    total_required: int
    is_complete: bool                   # only when ALL required milestone deliverables VERIFIED
    completion_note: str = "progress is (verified required deliverables); never raw task count"

    def to_dict(self) -> dict[str, Any]:
        return {**_base("MissionSummary"), **asdict(self)}


# --- Verification ladder (worker_complete != VERIFIED) -----------------------
@dataclass(frozen=True)
class VerificationSummary:
    task_id: str
    implementation_result: str          # e.g. "COMPLETE" (a CLAIM, not authority)
    scope_policy_gate: str              # PASS|FAIL
    deterministic_verification: str     # PASS|FAIL
    acceptance_criteria: str            # PASS|FAIL|NOT_EVALUATED
    gpt_verdict: str                    # PASS|REPAIR|ESCALATE|ABSTAIN|NOT_CONSULTED|UNAVAILABLE
    final_status: str                   # VERIFIED only if deterministic PASS AND gpt PASS
    worker_complete_is_not_verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {**_base("VerificationSummary"), **asdict(self)}


def project_verification(task_id: str, implementation_result: str, scope_policy_gate: str,
                         deterministic: str, acceptance: str, gpt_verdict: str) -> VerificationSummary:
    """Project the verification ladder + final status. A deterministic failure that
    short-circuits the supervisor projects GPT as NOT_CONSULTED (never PASS/FAIL)."""
    det_ok = deterministic == "PASS" and scope_policy_gate == "PASS"
    if not det_ok:
        gpt = "NOT_CONSULTED"
        final = "NOT_VERIFIED"
    else:
        gpt = gpt_verdict
        final = "VERIFIED" if gpt_verdict == "PASS" and acceptance in ("PASS", "NOT_EVALUATED") else "NOT_VERIFIED"
    return VerificationSummary(task_id=task_id, implementation_result=implementation_result,
                               scope_policy_gate=scope_policy_gate, deterministic_verification=deterministic,
                               acceptance_criteria=acceptance, gpt_verdict=gpt, final_status=final)


# --- Apprenticeship (honest; never smooths negative evidence) ----------------
@dataclass(frozen=True)
class ApprenticeshipSummary:
    controller_level: str
    #: ``None`` means the records ledger could not answer -- never a measured 0.
    decisions_shadowed: int | None
    task_selection_agreements: int | None
    risk_agreements: int | None
    routing_agreements: int | None
    missed_escalations: int | None
    unsafe_underclassifications: int | None
    authority_expansion_proposals: int | None
    c1_readiness: str                   # NOT_READY | CANDIDATE | READY_FOR_CERTIFICATION
    #: Whether the underlying records ledger was usable at all.
    records_evidence: str = TruthState.LIVE.value

    def to_dict(self) -> dict[str, Any]:
        return {**_base("ApprenticeshipSummary"), **asdict(self)}


@dataclass(frozen=True)
class AttentionItem:
    kind: str                           # E4 | CAPITAL_POLICY | ARCHITECTURE_FORK | SECURITY | COMPLIANCE | SPENDING | UNRESOLVED_ESCALATION | CERTIFICATION_APPROVAL
    summary: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {**_base("AttentionItem"), **asdict(self)}


@dataclass(frozen=True)
class AttentionCoverage:
    """Whether the emitted attention list is an ANSWER or merely an empty list.

    ``attention_items`` was previously a literal ``[]``. A consumer could not
    distinguish "a derivation ran and found nothing outstanding" from "nothing
    has ever derived this", and those two license opposite operator behaviour.
    The list stays where it was, for compatibility; this states what it means."""

    items: list[dict[str, Any]]
    item_count: int
    derivation_state: str               # PENDING_BACKEND while no producer exists
    #: The load-bearing field. False means: do NOT render "nothing needs you".
    zero_items_is_authoritative: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {**_base("AttentionCoverage"), **asdict(self)}


@dataclass(frozen=True)
class SystemHealthSummary:
    controller: str
    gpt_supervisor: str
    engineer_runtime: str
    sandbox: str
    evidence_bridge: str
    authority: str
    control_loop: str
    #: Component liveness and configuration readability are different questions.
    #: Readability is recorded separately and labelled, because a readable
    #: protected config proves what the system is ALLOWED to do and proves
    #: nothing whatever about whether anything is running.
    config_readability: dict[str, str] = field(default_factory=dict)
    health_note: str = ("component health requires a health producer; none exists. "
                        "config_readability is FILE READABILITY, never liveness")

    def to_dict(self) -> dict[str, Any]:
        return {**_base("SystemHealthSummary"), **asdict(self)}


# --- Run / outcome history (canonical reader, provenance preserved) ----------
@dataclass(frozen=True)
class RunHistorySummary:
    """Controller-owned projection of the engineering outcome ledger.

    Exists so the GUI stops parsing that ledger itself. Built on the canonical
    domain reader (``ew0a.read_outcomes``) rather than a third hand-written JSONL
    interpretation of the same file."""

    source: str
    source_kind: str
    availability: str                   # LIVE | UNAVAILABLE
    record_count: int
    runs: list[dict[str, Any]]
    #: Verdict counts from THIS ledger's ``supervisor_verdict`` field. Named and
    #: sourced so they can never be mistaken for the SupervisorSummary counts,
    #: which come from a different ledger and a different field.
    verdict_counts: dict[str, int]
    verdict_field: str = "supervisor_verdict"
    evidence_domain: str = "engineering_outcome_runs"
    ordering: str = "ledger_append_order"
    provenance_note: str = ("mission_id is projected exactly as recorded; a record "
                            "without one stays None. The runtime mission is NEVER "
                            "stamped onto historical runs")
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**_base("RunHistorySummary"), **asdict(self)}


# ---------------------------------------------------------------------------
# Projection-boundary schema certification
#
# Three consecutive review rounds found the same defect class rather than three
# unrelated bugs: syntactically valid but SCHEMA-INVALID authoritative evidence
# crossed this boundary unvalidated, where Python coercion or raw copying could
# leak payload content (``str()`` on a dict renders the dict), silently rewrite
# evidence (``x is True`` turns a corrupt value into a clean ``False``), or
# produce contradictory state. Fixing the reported field each round could not
# converge, because the hole was the boundary, not the field.
#
# THE RULE. Validate first; copy only validated values; never stringify
# arbitrary evidence. Converting a Path to str, or an Enum to ``.value``, is
# conversion of something this module owns. ``str(record_field)`` is not
# validation and is never a substitute for it. Invalid evidence is not
# sanitised into valid-looking evidence -- it makes the projection UNAVAILABLE.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _OutcomeField:
    """One ``OutcomeRecord`` field as this projection consumes it.

    ``required`` means present AND non-null. Optional fields may be absent or
    explicitly null: every record in the real ledger omits ``mission_id`` and
    ``candidate_sha`` entirely and carries ``supervisor_verdict: null``, so
    treating those as invalid would condemn the true history."""

    name: str
    kind: str                    # "str" | "int" | "bool" | "list[str]"
    required: bool = True


#: The enumerable contract. Only fields ``_project_run`` actually reads are
#: listed -- this certifies the boundary, it does not start projecting more.
_OUTCOME_FIELDS: tuple[_OutcomeField, ...] = (
    _OutcomeField("task_id", "str"),
    _OutcomeField("title", "str"),
    _OutcomeField("risk_class", "str"),
    _OutcomeField("executor", "str"),
    _OutcomeField("final_status", "str"),
    _OutcomeField("recorded_at", "str"),
    _OutcomeField("disposition", "str"),
    _OutcomeField("attempt_count", "int"),
    _OutcomeField("escalated", "bool"),
    _OutcomeField("policy_violation", "bool"),
    _OutcomeField("human_intervention", "bool"),
    _OutcomeField("failure_classes", "list[str]", required=False),
    _OutcomeField("supervisor_verdict", "str", required=False),
    _OutcomeField("mission_id", "str", required=False),
    _OutcomeField("candidate_sha", "str", required=False),
)


def _validated_string_list(name: str, value: Any) -> list[str]:
    """A ``list[str]`` means the container AND every element.

    Copied, never coerced. Only the type name reaches the message: a projection
    must not render content it has just declared unusable."""
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of strings, got {type(value).__name__}")
    for element in value:
        if not isinstance(element, str):
            raise ValueError(
                f"{name} elements must be strings, got {type(element).__name__}")
    return list(value)


def _validated_scalar(name: str, kind: str, value: Any) -> Any:
    if kind == "str":
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string, got {type(value).__name__}")
        return value
    if kind == "int":
        # bool is a subclass of int in Python, so `isinstance(True, int)` is
        # True. A flag is not a count, and accepting one would let `true` pass
        # as an attempt number.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer, got {type(value).__name__}")
        if value < 0:
            # Every attempt_count in the real ledger is 1, 2 or 4. A negative
            # count is not a value this contract can mean.
            raise ValueError(f"{name} must be >= 0")
        return value
    if kind == "bool":
        # NOT `value is True`. That silently turned "true", 1 and {} into a
        # clean False -- schema-invalid evidence becoming clean NEGATIVE
        # evidence, which is the most dangerous direction for a field named
        # policy_violation.
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean, got {type(value).__name__}")
        return value
    raise ValueError(f"{name} has an unsupported declared kind {kind!r}")


def validate_outcome_record(rec: dict[str, Any], index: int | None = None
                            ) -> dict[str, Any]:
    """Validate every source field this projection consumes, or refuse the row.

    Raises ``ValueError``, which :func:`build_run_history` converts into its
    whole-ledger UNAVAILABLE envelope. Nothing is dropped, defaulted or
    stringified on the way."""
    where = "" if index is None else f"row {index}: "
    out: dict[str, Any] = {}
    for spec in _OUTCOME_FIELDS:
        label = f"{where}{spec.name}"
        value = rec.get(spec.name)
        if spec.name not in rec or value is None:
            if spec.required:
                raise ValueError(
                    f"{label} is required by OutcomeRecord and is "
                    f"{'absent' if spec.name not in rec else 'null'}")
            out[spec.name] = [] if spec.kind == "list[str]" else None
            continue
        if spec.kind == "list[str]":
            out[spec.name] = _validated_string_list(label, value)
        else:
            out[spec.name] = _validated_scalar(label, spec.kind, value)
    return out


def _projected_failure_classes(rec: dict[str, Any]) -> list[str]:
    """Project ``failure_classes`` or refuse the record.

    ``OutcomeRecord`` declares ``list[str]``. A syntactically valid JSON row can
    still carry any other type, and the previous comprehension simply iterated
    whatever was there -- so ``123`` raised TypeError and ``"TEST_FAILURE"``
    would have silently become a list of single characters. Both are corrupt
    evidence; only one announced itself.

    Absent and ``None`` stay ``[]`` because records written before the field
    existed are legitimately shaped that way. Everything else that is not a list
    raises ``ValueError``, which :func:`build_run_history` already converts into
    its UNAVAILABLE envelope. Coercing a corrupt value to ``[]`` instead would
    manufacture clean evidence out of unusable evidence, which is the failure
    mode this whole projection layer exists to prevent."""
    raw = rec.get("failure_classes")
    if raw is None:
        return []
    return _validated_string_list("failure_classes", raw)


def _project_run(rec: dict[str, Any], index: int) -> dict[str, Any]:
    """One outcome record -> one projected run.

    Identifiers are preserved, never synthesized: ``task_id`` is the record's own
    identity and ``ledger_index`` disambiguates repeats without inventing a
    composite id and presenting it as one the control plane issued."""
    fields = validate_outcome_record(rec, index)
    # Every value below came out of the validator. There is no `.get()` fallback,
    # no `_s()` that quietly turns a malformed identifier into None while the
    # ledger still reports LIVE, no `is True` that rewrites a corrupt flag as
    # clean False, and no str() anywhere.
    projected = {"ledger_index": index}
    projected.update(fields)
    # Provenance exactly as recorded. OutcomeRecord.mission_id defaults to None
    # and every record in the real ledger omits it, so None is the true answer —
    # and the current runtime mission is not a substitute for it.
    return projected


def build_run_history(repo_root: str | Path, rel: str = OUTCOME_LEDGER_REL) -> RunHistorySummary:
    """Project the outcome ledger through the canonical domain reader.

    Truth states follow the lattice: the producer (the certification runner plus
    ``ew0a.append_outcome``) exists in this repository, so an absent or unreadable
    ledger is UNAVAILABLE — an operational condition — and never PENDING_BACKEND,
    which would claim nobody had built it."""
    path = Path(repo_root) / rel
    if not path.exists():
        return RunHistorySummary(
            source=rel, source_kind="engineering_outcome_ledger",
            availability=TruthState.UNAVAILABLE.value, record_count=0, runs=[],
            verdict_counts={}, detail=f"{rel} is absent")
    try:
        records = read_outcomes(str(path))
    except (OSError, ValueError) as exc:
        # The canonical reader raises on a malformed line. That is ITS rule, and
        # this projection does not soften it into a partial list; it reports the
        # ledger as unusable and says why.
        return RunHistorySummary(
            source=rel, source_kind="engineering_outcome_ledger",
            availability=TruthState.UNAVAILABLE.value, record_count=0, runs=[],
            verdict_counts={},
            detail=f"{rel} unreadable via ew0a.read_outcomes ({type(exc).__name__})")

    try:
        runs = []
        for index, rec in enumerate(records):
            if not isinstance(rec, dict):
                # Previously this row was silently FILTERED OUT and the
                # remainder reported LIVE, so a three-row ledger with one
                # corrupt row projected two records as a complete history --
                # and a ledger of nothing but corrupt rows projected an empty
                # history as complete. Silent evidence loss is worse than a
                # crash: a crash at least announces itself.
                raise ValueError(
                    f"row {index} is not a JSON object, got {type(rec).__name__}")
            runs.append(_project_run(rec, index))
    except ValueError as exc:
        # One schema-invalid record makes the whole history unusable. Dropping
        # the bad row and serving the rest would be a partial-ledger semantic
        # that no authoritative contract establishes, and the consumer could not
        # tell a complete history from a quietly truncated one.
        return RunHistorySummary(
            source=rel, source_kind="engineering_outcome_ledger",
            availability=TruthState.UNAVAILABLE.value, record_count=0, runs=[],
            verdict_counts={},
            detail=f"{rel} contains a schema-invalid record ({exc})")
    counts: dict[str, int] = {}
    for run in runs:
        verdict = run["supervisor_verdict"]
        if verdict:
            counts[verdict] = counts.get(verdict, 0) + 1
    return RunHistorySummary(
        source=rel, source_kind="engineering_outcome_ledger",
        availability=TruthState.LIVE.value, record_count=len(runs), runs=runs,
        verdict_counts=counts, detail=f"{len(runs)} record(s) via ew0a.read_outcomes")


# ---------------------------------------------------------------------------
# Builders over authoritative sources (READ-ONLY)
# ---------------------------------------------------------------------------
# INTEGRATION NOTE. GUI-R's raw `_read_records` lived here: it admitted
# non-object rows and silently skipped unparseable lines, which is exactly the
# blocker-C defect GUI-SR repaired. It is deliberately NOT restored. The
# hardened `read_controller_records` below is the single admission path, and
# `_read_records` survives only as the backward-compatible accessor it defines.
# CONTROLLER_RECORDS_REL is declared once, in the constants block at the top,
# because SupervisorSummary uses it as a field default at class-creation time.


@dataclass(frozen=True)
class _RecordField:
    """One controller-record field AS THE WCC READ MODEL CONSUMES IT."""

    name: str
    kind: str                    # "str" | "bool"
    #: Present on every legitimate record. Determined from the tracked ledger,
    #: not assumed -- the ledger is heterogeneous and most fields are not.
    required: bool = False
    non_empty: bool = False
    consumers: tuple[str, ...] = ()


#: THE WCC CONSUMED-FIELD CONTRACT.
#:
#: `read_controller_records` previously established "valid JSON + row is a dict"
#: and then declared the ledger LIVE, while the projections went on to read
#: individual FIELDS out of those rows. So a row with
#: ``gpt_verdict: {"api_key": "..."}`` was admitted, stringified into
#: worker.recent_verification_outcomes, and a dict-valued ``recorded_at`` on a
#: PASS row was copied straight into supervisor.last_successful_verification --
#: arbitrary nested payload reaching the GUI through a field nobody thinks of as
#: a payload carrier. Validating the container and not the contents is the same
#: mistake this repository has now recorded in two separate missions.
#:
#: This is a BOUNDED CONSUMPTION CONTRACT, not certification of every field of
#: every historical record kind. Fields the WCC does not read stay opaque and
#: unprojected, and a record is never rejected merely for carrying them.
#:
#: Presence semantics come from the 24 tracked records: `kind` is on all 24;
#: `gpt_verdict` on 10; `recorded_at` on 22 (two legitimately omit it); the four
#: apprenticeship booleans only on ApprenticeshipComparison rows. Making any of
#: the optional ones required would condemn the true history.
WCC_CONSUMED_RECORD_FIELDS: tuple[_RecordField, ...] = (
    _RecordField("kind", "str", required=True, non_empty=True,
                 consumers=("build_apprenticeship_summary",)),
    _RecordField("gpt_verdict", "str", non_empty=True,
                 consumers=("build_supervisor_summary", "_assess_backend_truth",
                            "_recent_verification_outcomes")),
    _RecordField("recorded_at", "str", non_empty=True,
                 consumers=("build_supervisor_summary", "_assess_backend_truth")),
    _RecordField("engineer_proposed_task_relates_to_experimentspec", "bool",
                 consumers=("build_apprenticeship_summary",)),
    _RecordField("risk_agreement", "bool",
                 consumers=("build_apprenticeship_summary",)),
    _RecordField("routing_agreement", "bool",
                 consumers=("build_apprenticeship_summary",)),
    _RecordField("danger_underclassified_architecture_as_engineer", "bool",
                 consumers=("build_apprenticeship_summary",)),
)

#: Exported so a scoped AST test can require every constant field name the
#: consumers actually read to be represented here. A hand-maintained list on its
#: own is what let GUI-R omit a real dependency twice.
WCC_CONSUMED_RECORD_FIELD_NAMES: frozenset[str] = frozenset(
    f.name for f in WCC_CONSUMED_RECORD_FIELDS)


def _record_field_violation(row: dict[str, Any]) -> str | None:
    """The first WCC-consumption violation in this row, or None.

    Only field names and structural type names are returned. An explicit JSON
    null on an OPTIONAL field is treated as absence: no tracked record does it,
    it creates no leak, and the consumers already handle a missing field. A
    wrong TYPE is never treated as absence -- that is the distinction between
    "not applicable to this record kind" and "malformed value that happens to be
    falsey", and conflating them is how a corrupt boolean becomes clean negative
    evidence."""
    for spec in WCC_CONSUMED_RECORD_FIELDS:
        if spec.name not in row or row[spec.name] is None:
            if spec.required:
                raise_reason = "absent" if spec.name not in row else "null"
                return f"{spec.name} is required by the WCC contract and is {raise_reason}"
            continue
        value = row[spec.name]
        if spec.kind == "bool":
            # bool ONLY. Not 0/1, not "true", not [] or {} through truthiness.
            if not isinstance(value, bool):
                return (f"{spec.name} must be a boolean per the WCC contract, "
                        f"got {type(value).__name__}")
            continue
        if not isinstance(value, str):
            return (f"{spec.name} must be a string per the WCC contract, "
                    f"got {type(value).__name__}")
        if spec.non_empty and not value:
            return f"{spec.name} must be a non-empty string per the WCC contract"
    return None


@dataclass(frozen=True)
class ControllerRecordsRead:
    """The outcome of reading the controller records ledger.

    The previous reader was unsafe in both directions at once. A malformed JSON
    line was silently skipped, so an unreadable ledger produced a SHORTER list
    that consumers then reported as measured history — an unparseable file became
    ``0 PASS`` as though zero had been observed. And a syntactically valid
    non-object row was admitted into the list, so the first ``row.get(...)``
    downstream raised ``AttributeError`` and took the dashboard with it.

    Neither is repaired by filtering: dropping bad rows is precisely how partial
    evidence comes to masquerade as complete evidence. The ledger is either
    usable in full or it is unusable, and a genuinely empty ledger stays
    distinguishable from a corrupt one."""

    availability: str                       # LIVE | UNAVAILABLE
    records: list[dict[str, Any]]
    #: The ledger identifier THIS read actually used. Instance provenance, not
    #: the module default: ``to_dict()`` hardcoded ``CONTROLLER_RECORDS_REL``, so
    #: reading an alternate ledger produced one evidence object whose ``detail``
    #: named the file read and whose ``source`` named a different file. An
    #: evidence result that contradicts itself is worse than one that is merely
    #: incomplete. Carried unchanged -- never canonicalised or synthesised.
    source: str = CONTROLLER_RECORDS_REL
    detail: str = ""

    @property
    def is_usable(self) -> bool:
        return self.availability == TruthState.LIVE.value

    def to_dict(self) -> dict[str, Any]:
        return {**_base("ControllerRecordsRead"),
                "availability": self.availability,
                "record_count": len(self.records),
                "source": self.source,
                "detail": self.detail}


def read_controller_records(repo_root: str | Path,
                            rel: str = CONTROLLER_RECORDS_REL) -> ControllerRecordsRead:
    """Read the controller records ledger, or refuse it whole.

    Absence is UNAVAILABLE rather than PENDING_BACKEND: the controller writes
    these records, so a missing ledger is an operational condition and not
    evidence that nobody built the producer."""
    path = Path(repo_root) / rel
    if not path.exists():
        return ControllerRecordsRead(TruthState.UNAVAILABLE.value, [], rel,
                                     f"{rel} is absent")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return ControllerRecordsRead(
            TruthState.UNAVAILABLE.value, [], rel,
            f"{rel} is unreadable ({type(exc).__name__})")

    rows: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # NOT skipped. A ledger we cannot fully parse is not a shorter ledger.
            return ControllerRecordsRead(
                TruthState.UNAVAILABLE.value, [], rel,
                f"{rel} line {index} is not valid JSON")
        if not isinstance(row, dict):
            # Type name only; the malformed payload is never echoed.
            return ControllerRecordsRead(
                TruthState.UNAVAILABLE.value, [], rel,
                f"{rel} line {index} is not a JSON object, got {type(row).__name__}")
        # Consumed-field validation happens HERE, before the row is admitted, so
        # the three WCC consumers cannot receive evidence this read result has
        # already called usable without its consumed fields being checked. It is
        # deliberately not repeated in each consumer.
        violation = _record_field_violation(row)
        if violation is not None:
            return ControllerRecordsRead(
                TruthState.UNAVAILABLE.value, [], rel,
                f"{rel} line {index}: {violation}")
        rows.append(row)
    return ControllerRecordsRead(TruthState.LIVE.value, rows, rel,
                                 f"{len(rows)} record(s) from {rel}")


def _read_records(repo_root: Path, rel: str = CONTROLLER_RECORDS_REL) -> list[dict[str, Any]]:
    """Backward-compatible accessor: the usable rows, or none at all.

    Retained so existing callers keep working. New code should use
    :func:`read_controller_records` and consult ``availability`` -- an empty list
    from here cannot distinguish an empty ledger from an unusable one."""
    return read_controller_records(repo_root, rel).records


def build_supervisor_summary(records: list[dict[str, Any]],
                            records_evidence: str = TruthState.LIVE.value
                            ) -> SupervisorSummary:
    """Project supervisor verdict history.

    ``records_evidence`` defaults to LIVE so existing callers passing a plain
    list are unchanged. When the ledger is UNUSABLE the counts are ``None`` --
    NOT zero. A measured zero and an unanswerable question look identical as
    ``0``, and for a verdict history that difference is the whole point: an
    operator reading ``0 REPAIR`` from an unreadable ledger would conclude
    nothing had gone wrong."""
    usable = records_evidence == TruthState.LIVE.value
    if not usable:
        return SupervisorSummary(
            availability=PENDING_BACKEND, current_state=PENDING_BACKEND,
            recent_pass=None, recent_repair=None, recent_escalate=None,
            recent_abstain=None, recent_unavailable=None,
            last_successful_verification=None, measured_latency_ms=PENDING_BACKEND,
            verification_queue=PENDING_BACKEND, outage_state=PENDING_BACKEND,
            records_evidence=records_evidence)
    # No str() anywhere below: read_controller_records has already guaranteed
    # that a present gpt_verdict/recorded_at is a non-empty string. Coercion was
    # how an arbitrary object became displayable in the first place.
    verdicts = [v for v in (r.get("gpt_verdict") for r in records) if v]
    def c(v):
        return sum(1 for x in verdicts if x.upper() == v)
    last_pass = next((r.get("recorded_at") for r in reversed(records)
                      if (r.get("gpt_verdict") or "").upper() == "PASS"), None)
    return SupervisorSummary(
        availability=PENDING_BACKEND, current_state=PENDING_BACKEND,
        recent_pass=c("PASS"), recent_repair=c("REPAIR"), recent_escalate=c("ESCALATE"),
        recent_abstain=c("ABSTAIN"), recent_unavailable=c("SUPERVISOR_UNAVAILABLE"),
        last_successful_verification=last_pass, measured_latency_ms=PENDING_BACKEND,
        verification_queue=PENDING_BACKEND, outage_state=PENDING_BACKEND,
        records_evidence=records_evidence)


def _recent_verification_outcomes(records: list[dict[str, Any]],
                                  limit: int = 5) -> list[str]:
    """The last few recorded GPT verdicts, copied as strings -- not coerced.

    Extracted from build_dashboard so the no-coercion guard covers a small,
    named function instead of the whole assembler."""
    return [v for v in (r.get("gpt_verdict") for r in records) if v][-limit:]


def build_apprenticeship_summary(records: list[dict[str, Any]],
                                 records_evidence: str = TruthState.LIVE.value
                                 ) -> ApprenticeshipSummary:
    """Project the C0.5 apprenticeship metrics.

    When the ledger is unusable every count is ``None``, not zero. Zero
    ``unsafe_underclassifications`` is the single most flattering number this
    projection can emit, and it must never be produced by a ledger that could
    not be read."""
    if records_evidence != TruthState.LIVE.value:
        return ApprenticeshipSummary(
            controller_level="C0.5_SHADOW", decisions_shadowed=None,
            task_selection_agreements=None, risk_agreements=None,
            routing_agreements=None, missed_escalations=None,
            unsafe_underclassifications=None, authority_expansion_proposals=None,
            c1_readiness="NOT_READY", records_evidence=records_evidence)
    comps = [r for r in records if r.get("kind") == "ApprenticeshipComparison"]
    shadowed = len([r for r in records if r.get("kind") == "ControllerDecisionCandidateV0"])
    # `is True` rather than truthiness. The values are validated booleans by the
    # time they arrive, so the two are equivalent today -- but stating it
    # explicitly means a later reader cannot mistake this for the truthiness test
    # that used to turn a malformed value into clean negative evidence.
    return ApprenticeshipSummary(
        controller_level="C0.5_SHADOW",
        decisions_shadowed=shadowed,
        task_selection_agreements=sum(
            1 for c in comps
            if c.get("engineer_proposed_task_relates_to_experimentspec") is True),
        risk_agreements=sum(1 for c in comps if c.get("risk_agreement") is True),
        routing_agreements=sum(1 for c in comps if c.get("routing_agreement") is True),
        missed_escalations=sum(
            1 for c in comps
            if c.get("danger_underclassified_architecture_as_engineer") is True),
        unsafe_underclassifications=sum(
            1 for c in comps
            if c.get("danger_underclassified_architecture_as_engineer") is True),
        authority_expansion_proposals=0,
        c1_readiness="NOT_READY", records_evidence=records_evidence)


#: Distinguishes "the caller passed nothing" from "the record carried null".
#: `record.get("grants", [])` erased the difference between missing, null and
#: empty -- three states with three different meanings -- and reported an
#: authority record with no list fields at all as good evidence.
_MISSING = object()


#: The authority levels the enum recognises. Membership is checked against the
#: canonical enum rather than a copied list, so it cannot drift from it.
_AUTHORITY_LEVEL_VALUES = frozenset(lvl.value for lvl in EngineerAuthorityLevel)


def _validated_raw_level(raw_level: Any, effective: EngineerAuthorityLevel) -> None:
    """Validate the RECORD's own ``level`` field, or refuse the record.

    EFFECTIVE LEVEL AND RECORD EVIDENCE ARE DIFFERENT QUESTIONS, and conflating
    them was the defect. ``read_authority_level`` fails closed to A0 on a corrupt
    record -- correct, and enforcement depends on it -- but that fallback is not
    evidence that the stored record says A0. Certifying the two list fields while
    ignoring ``level`` let a record with an absent or garbage level report
    ``record_evidence: LIVE``, so a corrupt protected record was presented as
    trustworthy configuration evidence through a REQUIRED capability.

    Raises ``ValueError``. Only field names, type names and enum-membership facts
    reach the message; the raw value never does."""
    if raw_level is _MISSING:
        raise ValueError("level is required in an authority record and is absent")
    if raw_level is None:
        raise ValueError("level is required in an authority record and is null")
    if not isinstance(raw_level, str):
        raise ValueError(f"level has invalid type {type(raw_level).__name__}")
    if not raw_level:
        raise ValueError("level is empty")
    if raw_level not in _AUTHORITY_LEVEL_VALUES:
        # Deliberately does not echo the value: an unrecognised level is exactly
        # the kind of arbitrary record content this projection must not render.
        raise ValueError("level is not a recognized authority enum value")
    if raw_level != effective.value:
        # Two reads of the same protected record disagreeing is not something to
        # resolve by picking one and reporting LIVE.
        raise ValueError(
            "level disagrees with the canonical reader result; the two reads of "
            "the protected record are inconsistent")


def build_worker_authority_summary(level: EngineerAuthorityLevel,
                                   grants: Any = _MISSING,
                                   forbidden_ops: Any = _MISSING,
                                   raw_level: Any = _MISSING
                                   ) -> WorkerAuthoritySummary:
    """Project authority, validating the record's own ``list[str]`` fields.

    TWO OUTCOMES, and the distinction is load-bearing.

    Valid record: capability booleans derived from the effective denial set,
    which is the union of the permanent ``FORBIDDEN_OPS`` boundary with whatever
    the record additionally forbids -- a record may only ever be stricter.

    Malformed ``grants`` or ``forbidden_ops``: the record's contents are NOT
    rendered and NOT silently filtered. Filtering would drop restrictions the
    record meant to impose while still reporting LIVE authority evidence, which
    is dishonest in the dangerous direction. Instead the record evidence is
    marked UNAVAILABLE, and the permanent denial boundary is projected on its
    own -- so every forbidden operation stays forbidden. Capability safety never
    depends on the record being well-formed."""
    try:
        # The record's own level is projection-relevant evidence, so it is
        # validated alongside the lists. This certifies the fields THIS
        # projection consumes -- actor/updated_at/schema_* are not consumed and
        # are deliberately not validated here.
        _validated_raw_level(raw_level, level)
        # `set_authority_level` always writes BOTH fields, so a record lacking
        # either is not a complete authority record. An empty list IS valid
        # evidence (A0 legitimately grants nothing); absent and null are not.
        if grants is _MISSING or grants is None:
            raise ValueError(
                f"grants is required in an authority record and is "
                f"{'absent' if grants is _MISSING else 'null'}")
        if forbidden_ops is _MISSING or forbidden_ops is None:
            raise ValueError(
                f"forbidden_ops is required in an authority record and is "
                f"{'absent' if forbidden_ops is _MISSING else 'null'}")
        denied = effective_denied_ops(forbidden_ops)
        projected_grants = _validated_string_list("grants", grants)
    except (ValueError, TypeError) as exc:
        permanent = frozenset(FORBIDDEN_OPS)
        return WorkerAuthoritySummary(
            level=level.value, grants=[], forbidden_ops=sorted(permanent),
            **derive_authority_capabilities(permanent),
            record_evidence=TruthState.UNAVAILABLE.value,
            record_detail=(
                f"the authority record is unusable ({exc}); its contents are not "
                "projected. The permanent FORBIDDEN_OPS boundary is still "
                "enforced, so no operation is reported as newly allowed"))
    return WorkerAuthoritySummary(
        level=level.value, grants=projected_grants, forbidden_ops=sorted(denied),
        **derive_authority_capabilities(denied))


def build_attention_coverage(items: list[dict[str, Any]] | None = None,
                             derivation_exists: bool = False) -> AttentionCoverage:
    """Say whether the attention list is an answer.

    No attention derivation producer exists. Deriving one from the outcome ledger
    was considered and rejected: that ledger's only ``policy_violation`` is
    certification mission M5 (``tools/ew0a_certify.py``), a protected-op attack
    whose GATE WAS THAT IT BE DENIED. Promoting a passed security control into an
    unresolved human incident is the bug the GUI has today, and it is not
    improved by moving it upstream."""
    entries = list(items or [])
    if not derivation_exists:
        return AttentionCoverage(
            items=entries, item_count=len(entries),
            derivation_state=TruthState.PENDING_BACKEND.value,
            zero_items_is_authoritative=False,
            detail=("no attention derivation producer exists; an empty list is NOT "
                    "evidence that nothing requires the human. Ordinary REPAIR, an "
                    "ordinary deterministic test failure, and a successfully denied "
                    "protected-op drill are none of them human attention items"))
    return AttentionCoverage(
        items=entries, item_count=len(entries),
        derivation_state=TruthState.LIVE.value,
        zero_items_is_authoritative=True,
        detail="derived from an authoritative attention producer")


_NORTHSTAR_0B3 = ("ExperimentSpec", "ExperimentResult", "CapitalProposal",
                  "ExitProposal", "OutcomeRecord", "StrategyPassport")

# Deliverable sets are MISSION-SCOPED. The six 0B.3 contracts describe that
# milestone and nothing else; reporting them under a different mission would
# show a completed phase's progress as the current phase's progress. That drift
# was caught in senior review of the first 0C session, where the runtime mission
# had moved to 0C while this projection still reported the 0B.3 six.
_MISSION_DELIVERABLES: dict[str, tuple[str, ...]] = {
    "northstar_0b_decision_outcome_passport_contracts": _NORTHSTAR_0B3,
}


def build_mission_summary(mission_id: str, present: set[str]) -> MissionSummary:
    """Project mission progress ONLY for a mission whose deliverable set is known.

    For any other mission the deliverables are reported as unknown rather than
    borrowed from a different milestone: a dashboard that shows 0B completion
    while the controller is running 0C is worse than one that admits it does not
    know, because the first is confidently wrong."""
    required = _MISSION_DELIVERABLES.get(mission_id)
    if required is None:
        return MissionSummary(
            mission_id=mission_id, deliverables={}, verified_count=0,
            total_required=0, is_complete=False,
            completion_note=(
                f"{PENDING_BACKEND}: no authoritative deliverable set is projected "
                f"for mission {mission_id!r}; progress is deliberately NOT inferred "
                f"from another mission's deliverables"))
    deliverables = {name: ("VERIFIED" if name in present else "NOT_STARTED")
                    for name in required}
    verified = sum(1 for v in deliverables.values() if v == "VERIFIED")
    return MissionSummary(mission_id=mission_id, deliverables=deliverables,
                          verified_count=verified, total_required=len(required),
                          is_complete=(verified == len(required)))


# --- active session: truth state and mission consistency ---------------------
#: Values the session projection uses for "there is nothing here". Treated as
#: absence of evidence, never as a mission name to compare against.
_SESSION_NON_VALUES = frozenset({PENDING_BACKEND, "NO_SUCH_SESSION", ""})


def _session_producer_failed(detail: str) -> dict[str, Any]:
    """Truthful envelope for a session producer that exists and could not answer.

    Deliberately NOT a partially-filled session shape: a consumer must not be
    able to read half a session out of a failure."""
    return {"read_model": _SESSION_READ_MODEL, "schema_kind": SCHEMA_KIND,
            "session_present": False, "session_state": _PRODUCER_UNAVAILABLE,
            "truth_state": TruthState.UNAVAILABLE.value,
            "mission_consistency": "UNDETERMINED",
            "consistency_detail": "the session producer could not answer",
            "safe_to_present_as_current_work": False,
            "freshness_evidence": "no session evidence was returned, so no age exists to measure",
            "producer_detail": detail}


def project_active_session(session: Any, runtime_mission: str | None,
                           now: str | None,
                           producer_status: str = _PRODUCER_OK,
                           producer_detail: str = "") -> tuple[Any, TruthState]:
    """Attach truth state and mission consistency to the session projection.

    TWO INDEPENDENT QUESTIONS, deliberately not merged:

    *Freshness* — the session contract exposes ``session_started_at`` and no
    last-activity timestamp. A start time is not a liveness signal, and no named
    freshness threshold for session age exists in ``FRESHNESS_SECONDS``. Age is
    therefore unmeasurable, which the lattice already answers: ``UNKNOWN``. It is
    NOT ``STALE`` — calling it stale would assert an age nobody measured, from a
    field that does not even mean what the assertion needs it to mean.

    *Consistency* — whether the session's own recorded mission is the mission the
    runtime is on. A mismatch is a fact about identity, not about age, so it is
    reported separately and NEVER by downgrading freshness.

    Only when both are satisfied may a consumer present the session as current
    work, and that conclusion is published as one boolean rather than left for
    the GUI to re-derive.

    PENDING_BACKEND IS RESERVED FOR AN ABSENT PRODUCER. ``tools/ns0c_session.py``
    exists, so "there is no session right now" is an ANSWER the producer gave,
    not a subsystem nobody built, and a producer that raises is an outage rather
    than missing engineering. Those three used to collapse into PENDING_BACKEND,
    which told an operator to go build something that was already there."""
    if producer_status == _PRODUCER_ABSENT:
        return PENDING_BACKEND, TruthState.PENDING_BACKEND
    if producer_status == _PRODUCER_UNAVAILABLE:
        return (_session_producer_failed(producer_detail or "producer unavailable"),
                TruthState.UNAVAILABLE)
    if not isinstance(session, dict):
        # The producer loaded and returned something unusable. That is an
        # operational condition; classifying it as PENDING_BACKEND would claim
        # nobody had built it.
        return (_session_producer_failed(
            f"producer returned {type(session).__name__}, expected a projection"),
            TruthState.UNAVAILABLE)

    if session.get("session_state") == _NO_SESSION_STATE:
        if not _is_empty_session_envelope(session):
            # NO_SUCH_SESSION alongside populated work evidence is not the
            # producer's empty envelope; it is a contradiction, and accepting it
            # would hand the GUI LIVE evidence carrying a task id.
            return (_session_producer_failed(
                "the producer reported NO_SUCH_SESSION alongside populated "
                "session evidence; the shape is not its empty-session envelope"),
                TruthState.UNAVAILABLE)
        # The producer answered the question -- "is there a session?" -- with
        # "no". A usable answer whose truth does not decay: there is no recorded
        # session value whose age would have to be inferred.
        #
        # Assembled field by field from the structurally-verified envelope, NOT
        # copied: an uncontracted key the producer adds later must not appear
        # here by default, on this branch either.
        projected = {name: session.get(name)
                     for name in _NO_SESSION_PROJECTED_FIELDS}
        projected.update({
            "read_model": _SESSION_READ_MODEL,
            "schema_kind": SCHEMA_KIND,
            "session_present": False,
            "truth_state": classify(producer_exists=True,
                                    value=session.get("session_state"),
                                    requires_freshness=False).value,
            "runtime_mission_id": runtime_mission,
            "mission_consistency": "UNDETERMINED",
            "consistency_detail": "there is no session to compare against the runtime mission",
            "safe_to_present_as_current_work": False,
            "freshness_evidence": ("no session exists, so there is no age to measure; "
                                   "this is an answer, not a gap"),
        })
        return projected, TruthState.LIVE

    # A session may only be reported PRESENT if the producer gave it a usable
    # identity. Without this, a corrupt SessionStarted record missing its
    # session_id produced truth_state=UNAVAILABLE alongside
    # session_present=true -- a contradictory half-session carrying current-work
    # fields, which a GUI could render as a phantom active session.
    session_id = session.get("session_id")
    if (not isinstance(session_id, str) or not session_id.strip()
            or session_id in _SESSION_NON_VALUES):
        return (_session_producer_failed(
            "the producer returned a session without a usable session_id "
            f"({type(session_id).__name__})"), TruthState.UNAVAILABLE)

    # Every published field is validated BEFORE anything is assembled. This
    # replaced `enriched = dict(session)`, which made the projection's schema
    # whatever the producer returned -- so a TaskStage title of
    # {"api_key": "sk-..."} reached the dashboard through current_task_title.
    try:
        fields = _validated_session_fields(session)
    except ValueError as exc:
        return (_session_producer_failed(
            f"the populated session projection is unusable ({exc})"),
            TruthState.UNAVAILABLE)

    session_mission = fields["mission_id"]
    if not isinstance(session_mission, str) or session_mission in _SESSION_NON_VALUES:
        consistency, consistency_detail = "UNDETERMINED", (
            "the session records no usable mission_id")
    elif not runtime_mission:
        consistency, consistency_detail = "UNDETERMINED", (
            "the runtime policy provides no mission_id to compare against")
    elif session_mission == runtime_mission:
        consistency, consistency_detail = "AGREES", (
            "session mission matches the runtime mission")
    else:
        consistency, consistency_detail = "MISMATCH", (
            f"session mission {session_mission!r} is NOT the runtime mission "
            f"{runtime_mission!r}; this session is evidence about a different "
            f"mission and must not be presented as the current one")

    # recorded_at is deliberately not supplied: no last-activity timestamp exists
    # in the session contract, so classify() reaches UNKNOWN through the same
    # rule that governs every other unmeasurable age.
    state = classify(producer_exists=True, value=session_id,
                     recorded_at=None, now=now)

    projected = dict(fields)
    projected.update({
        # read_model/schema_kind are this module's constants, not the producer's
        # copy: a projection should not inherit its own identity from evidence.
        "read_model": _SESSION_READ_MODEL,
        "schema_kind": SCHEMA_KIND,
        "session_present": True,
        "truth_state": state.value,
        "runtime_mission_id": runtime_mission,
        "mission_consistency": consistency,
        "consistency_detail": consistency_detail,
        "safe_to_present_as_current_work": (
            state is TruthState.LIVE and consistency == "AGREES"),
        "freshness_evidence": (
            "session_started_at is a START time, not a last-activity time; the "
            "session contract publishes no last-activity timestamp and no named "
            "session freshness threshold exists, so age is unmeasurable"),
    })
    return projected, state


#: The producer's empty-session envelope, field by field. Matching only
#: ``session_state`` was not enough: a corrupted ledger whose last SessionState
#: happens to read NO_SUCH_SESSION yields a POPULATED projection, which was then
#: accepted as the legitimate "no session" answer and copied wholesale -- so a
#: dashboard received session_present=false and truth_state=LIVE while the dict
#: still carried session_id, current_task_id and current_stage.
_NO_SESSION_SENTINELS = ("session_state", "session_objective", "mission_id",
                         "session_started_at", "starting_main_sha")
_NO_SESSION_NULLS = ("current_task_id", "current_task_title", "current_stage")
_NO_SESSION_COUNTERS = ("tasks_attempted", "tasks_verified", "tasks_repaired",
                        "tasks_escalated", "tasks_abstained", "tasks_incomplete")


@dataclass(frozen=True)
class _SessionField:
    """One producer-published session field, as this projection publishes it."""

    name: str
    kind: str                     # "str" | "int" | "bool" | "list[str]"
    nullable: bool = False


#: THE POPULATED-SESSION OUTPUT SCHEMA.
#:
#: The projection used to be `dict(session)` -- a wholesale copy, which made the
#: published schema equal to "whatever the producer happens to return today or
#: tomorrow". That is not a certified interface, and it is how a TaskStage title
#: of `{"api_key": "sk-..."}` arrived in the dashboard under
#: current_task_title. Every field below is validated and copied individually;
#: anything the producer adds later does NOT appear here until it is added to
#: this table deliberately.
_SESSION_FIELDS: tuple[_SessionField, ...] = (
    # identity -- the logical/recorded distinction is preserved
    _SessionField("session_id", "str"),
    _SessionField("recorded_session_id", "str", nullable=True),
    _SessionField("identity_corrected", "bool"),
    # mission, objective and provenance
    _SessionField("mission_id", "str"),
    _SessionField("session_objective", "str"),
    _SessionField("session_started_at", "str"),
    _SessionField("starting_main_sha", "str"),
    _SessionField("session_state", "str"),
    # current work
    _SessionField("current_task_id", "str", nullable=True),
    _SessionField("current_task_title", "str", nullable=True),
    _SessionField("current_stage", "str", nullable=True),
    # counters
    _SessionField("tasks_attempted", "int"),
    _SessionField("tasks_verified", "int"),
    _SessionField("tasks_repaired", "int"),
    _SessionField("tasks_escalated", "int"),
    _SessionField("tasks_abstained", "int"),
    _SessionField("tasks_incomplete", "int"),
    # collections
    _SessionField("blockers", "list[str]"),
    _SessionField("known_sessions", "list[str]"),
    # boundaries the producer surfaces deliberately. worker_heartbeat and
    # supervisor_latency_ms legitimately carry the string "PENDING_BACKEND" in
    # the producer's own contract, so `str` accepts them without this module
    # inventing a sentinel rule of its own.
    _SessionField("authority", "str"),
    _SessionField("c1_status", "str"),
    _SessionField("auto_merge", "bool"),
    _SessionField("production_mutation", "bool"),
    _SessionField("capital_action", "bool"),
    _SessionField("worker_heartbeat", "str"),
    _SessionField("supervisor_latency_ms", "str"),
)

#: Producer-derived keys the ActiveSession projection publishes. Exported so a
#: test can assert emitted-keys == validated-keys mechanically instead of an
#: audit table maintained in prose -- which is exactly what missed this defect.
SESSION_PROJECTED_SOURCE_FIELDS: tuple[str, ...] = tuple(f.name for f in _SESSION_FIELDS)

#: Keys this module adds itself. Never copied from the producer.
SESSION_MODULE_FIELDS: tuple[str, ...] = (
    "read_model", "schema_kind", "session_present", "truth_state",
    "runtime_mission_id", "mission_consistency", "consistency_detail",
    "safe_to_present_as_current_work", "freshness_evidence")

#: The subset the no-session envelope publishes: identity, the sentinels, the
#: nulled current-work fields, the zero counters and the empty collections.
_NO_SESSION_PROJECTED_FIELDS: tuple[str, ...] = (
    "session_id", "session_state", "session_objective", "mission_id",
    "session_started_at", "starting_main_sha", "current_task_id",
    "current_task_title", "current_stage", "tasks_attempted", "tasks_verified",
    "tasks_repaired", "tasks_escalated", "tasks_abstained", "tasks_incomplete",
    "blockers", "known_sessions")


def _validated_session_fields(session: dict[str, Any],
                              names: tuple[str, ...] | None = None
                              ) -> dict[str, Any]:
    """Validate the contracted session fields, or refuse the answer.

    Raises ``ValueError``; :func:`project_active_session` converts that into the
    unusable-producer envelope. Only field names, declared kinds and actual type
    names ever reach the message."""
    wanted = set(names) if names is not None else None
    out: dict[str, Any] = {}
    for spec in _SESSION_FIELDS:
        if wanted is not None and spec.name not in wanted:
            continue
        value = session.get(spec.name)
        if spec.name not in session or value is None:
            if not spec.nullable:
                raise ValueError(
                    f"session field {spec.name} is required and is "
                    f"{'absent' if spec.name not in session else 'null'}")
            out[spec.name] = None
            continue
        if spec.kind == "list[str]":
            out[spec.name] = _validated_string_list(f"session field {spec.name}", value)
        else:
            out[spec.name] = _validated_scalar(
                f"session field {spec.name}", spec.kind, value)
    return out


def _is_empty_session_envelope(session: dict[str, Any]) -> bool:
    """Whether this really is the producer's no-session answer.

    ``session_id`` is deliberately NOT required to be None: the producer
    legitimately echoes back a requested-but-unknown session id in its
    no-session envelope. Everything that would represent actual work must be
    absent or zero."""
    if any(session.get(name) != _NO_SESSION_STATE for name in _NO_SESSION_SENTINELS):
        return False
    if any(session.get(name) is not None for name in _NO_SESSION_NULLS):
        return False
    for name in _NO_SESSION_COUNTERS:
        value = session.get(name)
        # `False == 0` in Python, so an explicit bool check is required or a
        # counter of False would pass as zero.
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            return False
    blockers = session.get("blockers")
    if not isinstance(blockers, list) or blockers:
        return False
    # The two fields the envelope legitimately carries still have declared
    # types, and the boundary audit found both unchecked: an envelope forged
    # with session_id={"api_key": ...} or a non-string known_sessions element
    # was copied into a LIVE no-session projection, leaking the payload. Same
    # class as the run-history and authority leaks, in the check written to
    # close them.
    session_id = session.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return False
    known = session.get("known_sessions")
    if not isinstance(known, list):
        return False
    return all(isinstance(entry, str) for entry in known)


#: The COMPLETE published vocabulary of _readability. Certified finite so that a
#: regression returning file contents cannot masquerade as a state -- which is
#: exactly what Codex finding 3960949424 showed the old guard could not detect.
READABILITY_STATES: frozenset[str] = frozenset({"ABSENT", "UNREADABLE", "READABLE"})


def _readability(path: Path) -> str:
    """File readability — NOT component health. Named so it cannot be mistaken.

    A direct source PROBE, not a canonical reader: it answers one structural
    question and returns a member of :data:`READABILITY_STATES`. It must never
    emit file contents, parsed JSON, exception text quoting the source, or a
    repr of any payload -- see the marker proofs in the GUI-RI test block.

    Deliberately carries no runtime self-check. Its contract is that it never
    raises, and an internal assertion that could raise would trade that
    guarantee for a redundant one."""
    if not path.exists():
        return "ABSENT"
    try:
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        # UnicodeDecodeError is a ValueError, NOT an OSError, so it used to
        # escape and take the whole dashboard down. A file this projection
        # cannot decode is exactly what UNREADABLE means. Deliberately narrow:
        # catching Exception here would disguise a programming defect as file
        # unreadability.
        return "UNREADABLE"
    return "READABLE"


def _assess_backend_truth(*, level: Any, policy: Any, records: list[dict[str, Any]],
                          worker: Any, now: str | None,
                          session_state: TruthState,
                          learning_state: TruthState,
                          run_history: RunHistorySummary,
                          authority_evidence: str = "LIVE",
                          records_evidence: str = TruthState.LIVE.value
                          ) -> ReadinessAssessment:
    """Classify every oversight capability from the evidence actually present.

    Each capability declares whether a PRODUCER exists. That is an engineering
    fact about this repository, not a runtime observation, and it is what keeps a
    missing subsystem reported as PENDING_BACKEND instead of as an outage.

    Nothing here builds a backend. A capability with no producer stays pending;
    the honest answer is the deliverable."""
    # Every row here has been validated as an object by read_controller_records,
    # so .get() is safe. When the ledger is unusable there is no verification to
    # age, and supervisor_state resolves to UNAVAILABLE rather than to a
    # freshness verdict computed over evidence nobody could read.
    last_verification = None
    if records_evidence == TruthState.LIVE.value:
        for rec in reversed(records):
            if rec.get("gpt_verdict") and rec.get("recorded_at"):
                last_verification = rec["recorded_at"]
                break

    caps = [
        # Authority and mission come from protected config files that are read
        # directly. They are authoritative-by-file and do not decay, so
        # demanding a timestamp would manufacture UNKNOWNs.
        Capability("controller_state",
                   classify(producer_exists=True,
                            value=policy.mission_id if policy else None,
                            requires_freshness=False),
                   required=True,
                   detail="config/ew0a_runtime.json (protected, read-only here)"),
        Capability("worker_authority",
                   classify(producer_exists=True,
                            # An unusable authority RECORD makes this capability
                            # UNAVAILABLE even though the level itself read
                            # closed to A0. worker_authority is part of the
                            # oversight floor, so readiness drops accordingly --
                            # which is the honest answer when an operator cannot
                            # see what the worker is permitted to do.
                            value=(getattr(level, "value", None)
                                   if authority_evidence == TruthState.LIVE.value
                                   else None),
                            requires_freshness=False),
                   required=True,
                   detail=("config/ew0a_authority.json (protected, read-only here); "
                           f"record evidence {authority_evidence}")),
        Capability("mission_state",
                   classify(producer_exists=True,
                            value=policy.mission_id if policy else None,
                            requires_freshness=False),
                   required=True, detail="runtime policy mission_id"),
        # A producer EXISTS for verification history (the records ledger), so
        # its freshness is measurable and it can legitimately go STALE.
        Capability("supervisor_state",
                   classify(producer_exists=True, value=last_verification,
                            recorded_at=last_verification, now=now,
                            threshold="verification"),
                   required=True,
                   detail=(f"recorded gpt_verdict history; records ledger "
                           f"{records_evidence}")),
        # No producer has been built for any of these. Building them is
        # explicitly out of scope for this mission.
        Capability("worker_activity",
                   classify(producer_exists=False, value=None),
                   required=True,
                   detail="no WorkerHeartbeatV0 producer exists"),
        Capability("queue_state", classify(producer_exists=False, value=None),
                   required=False, detail="no dispatch queue producer exists"),
        Capability("component_health", classify(producer_exists=False, value=None),
                   required=False,
                   detail=("no health-probe producer exists; config readability is "
                           "reported separately and is not liveness")),
        Capability("controller_since", classify(producer_exists=False, value=None),
                   required=False, detail="no controller-session record exists"),
        # --- GUI-R: projections that used to be emitted with no truth state ---
        Capability("active_session", session_state, required=False,
                   detail=("session ledger projection; freshness is UNKNOWN while the "
                           "contract publishes no last-activity timestamp. Mission "
                           "consistency is reported separately, never as staleness")),
        Capability("learning", learning_state, required=False,
                   detail=("learning store projection; lesson records are historical "
                           "evidence, so no freshness threshold is imposed")),
        Capability("run_history",
                   classify(producer_exists=True,
                            value=(run_history.record_count
                                   if run_history.availability == TruthState.LIVE.value
                                   else None),
                            requires_freshness=False),
                   required=False,
                   detail=f"{run_history.source} via ew0a.read_outcomes"),
        Capability("attention_derivation",
                   classify(producer_exists=False, value=None), required=False,
                   detail=("no attention derivation producer exists; an empty item "
                           "list is not an authoritative 'nothing needs you'")),
        Capability("controller_identity",
                   classify(producer_exists=False, value=None), required=False,
                   detail=("no ControllerStateV0 producer exists; the projected "
                           "identity is an assumption, see controller.identity_basis")),
    ]
    return assess_readiness(caps)


#: Why the learning payload is withheld. Module-owned text: nothing from the
#: producer, and no exception message, reaches a consumer through this.
_LEARNING_QUARANTINE_DETAIL = (
    "the learning producer exists and exposes build_learning_dashboard, but its "
    "nested WCC projection contract is not yet certified; the payload is "
    "deliberately not admitted (see GUI-L)")


def _quarantined_learning() -> dict[str, Any]:
    """The learning envelope while the payload contract is uncertified.

    Deliberately carries NO producer-derived key -- no recent_lessons, no
    capability_competence, no lesson_transfer, no graduation_readiness. The
    marker cannot be sanitised out of a payload that was never admitted."""
    return {"schema_version": READMODEL_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
            "read_model": "LearningDashboard",
            "truth_state": TruthState.UNAVAILABLE.value,
            "freshness": "NOT_APPLICABLE_HISTORICAL_EVIDENCE",
            "detail": _LEARNING_QUARANTINE_DETAIL}


def _project_learning(root: Path, worker_identity: str, now: str | None
                      ) -> tuple[Any, TruthState]:
    """Project the learning dashboard with an HONEST truth state.

    The previous version wrapped everything in one ``except`` and returned
    ``PENDING_BACKEND``, which told an operator that nobody had built learning
    while the learning package sat in the tree with lessons in it. The two cases
    are now distinguished, because they lead to different actions: a missing
    producer is engineering work, a failing one is an incident.

    No freshness threshold is imposed. Lesson records are historical evidence;
    inventing an age limit for them would manufacture STALE out of nothing."""
    def _unavailable(detail: str):
        return ({"schema_version": READMODEL_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
                 "read_model": "LearningDashboard",
                 "truth_state": TruthState.UNAVAILABLE.value,
                 "detail": f"{detail}; operational condition, NOT missing engineering"},
                TruthState.UNAVAILABLE)

    module, status, detail = _import_producer(_LEARNING_PRODUCER_MODULE)
    if status == _PRODUCER_ABSENT:
        return PENDING_BACKEND, TruthState.PENDING_BACKEND
    if status != _PRODUCER_OK:
        return _unavailable(detail)

    builder = getattr(module, "build_learning_dashboard", None)
    if builder is None:
        # The module is there and does not expose what this interface expects.
        # That is an incompatible producer, not an unbuilt one.
        return _unavailable(
            f"{_LEARNING_PRODUCER_MODULE} exposes no build_learning_dashboard")

    # QUARANTINE.
    #
    # The producer exists and its entry point is present, so this is NOT
    # PENDING_BACKEND -- claiming nobody built learning would send an operator
    # to write code that is in the tree with lessons in it. But its payload was
    # admitted on a shape check alone (a dict containing "recent_lessons") and
    # then emitted wholesale, so a lesson whose `principle` is
    # {"api_key": "..."} reached the dashboard as trusted GUI evidence.
    #
    # Validating `principle` would be another instance-level repair, and this PR
    # has already demonstrated where that leads: the learning dashboard carries
    # four independently shaped projections (recent_lessons,
    # capability_competence, lesson_transfer, graduation_readiness) which
    # themselves derive from stored lessons, competence, retrieval and
    # evaluation records. Certifying that is its own bounded mission (GUI-L).
    #
    # Until then the honest classification is: producer exists, WCC-safe
    # projection does not. The builder is deliberately NOT INVOKED -- there is
    # no payload to discard, so there is nothing to leak, and no uncertified
    # work is executed to produce a result this module would throw away.
    return _quarantined_learning(), TruthState.UNAVAILABLE


class ProjectionBoundary(str, Enum):
    """Who owns a top-level dashboard surface's boundary, and whether it is certified.

    An AUDIT artifact, not a second truth engine. It derives no authority, no
    readiness, no mission state, no health and no freshness -- those stay where
    they are. It answers only: what does build_dashboard emit, who owns that
    boundary, and has it been certified?"""

    #: Built entirely from this module's own constants and derivations.
    MODULE_OWNED = "MODULE_OWNED"
    #: Source evidence validated field by field against a declared contract in
    #: this module. The name carries lineage, not meaning: it covers GUI-SR's
    #: controller-record admission contract as well as GUI-R's projections.
    GUI_R_VALIDATED = "GUI_R_VALIDATED"
    #: Producer exists; its WCC payload contract is not certified, so the
    #: payload is withheld and the projection reports UNAVAILABLE.
    UNAVAILABLE_PENDING_CERTIFICATION = "UNAVAILABLE_PENDING_CERTIFICATION"
    #: Consumes the output of a canonical reader that GUI-SR made TOTAL. The
    #: surface does not validate that evidence itself, but the reader can no
    #: longer raise or hand it silently-truncated data. This replaced
    #: KNOWN_SOURCE_READER_BLOCKER for every surface once A/B/C were repaired.
    HARDENED_SOURCE_READER = "HARDENED_SOURCE_READER"
    #: Reaches this module through a canonical source reader with a KNOWN,
    #: deliberately unrepaired failure mode. Claimed by nothing today -- retained
    #: because that distinction is the one this registry exists to make, and a
    #: future reader could regress into it.
    KNOWN_SOURCE_READER_BLOCKER = "KNOWN_SOURCE_READER_BLOCKER"
    #: Computed from surfaces registered above; introduces no direct dependency
    #: on raw authoritative evidence. Claimed by nothing today.
    DERIVED_FROM_REGISTERED_INPUTS = "DERIVED_FROM_REGISTERED_INPUTS"


class RawSourceReader(str, Enum):
    """The canonical readers a dashboard surface depends on.

    These were GUI-R's blockers A, B and C -- three readers that could raise, or
    hand a consumer silently-truncated evidence, BEFORE their own fail-closed
    paths ran. GUI-SR repaired all three, so this enum no longer records a
    DEFECT; it records a DEPENDENCY. The letters are kept as historical anchors
    because the PR trail, the interface document and the deferred-debt tables
    all refer to them by letter.

    A surface declaring one of these consumes hardened reader output, which is
    why HARDENED_SOURCE_READER replaced KNOWN_SOURCE_READER_BLOCKER at
    integration -- and why the declarations were reassessed rather than deleted.
    The dependency is still real and still worth being able to see."""

    #: A -- now TOTAL over file and JSON shapes; fail-closed to A0, never raises.
    AUTHORITY_LEVEL = "A:ew0a_authority.read_authority_level"
    #: B -- now TOTAL over malformed roots, and enforcing its declared field types.
    RUNTIME_POLICY = "B:ew0a_loop.read_runtime_policy"
    #: C -- GUI-R's raw _read_records is gone; this is the whole-ledger admission
    #: path carrying the WCC consumed-field contract.
    CONTROLLER_RECORDS = "C:ew0a_readmodels.read_controller_records"


class SourceAccessKind(str, Enum):
    """HOW a dashboard surface's evidence was acquired.

    A canonical reader and a readability probe are not the same kind of thing,
    and collapsing them into one enum is what let four direct file reads hide
    behind a declaration naming one reader. Kind is what makes the difference
    between them expressible."""

    #: Admits and INTERPRETS evidence: validates shape, applies a contract, and
    #: fails closed. Readers A/B/C.
    CANONICAL_READER = "CANONICAL_READER"
    #: Opens and PARSES an authoritative file inline, without a canonical reader.
    #: The consuming projection owns validation.
    DIRECT_EVIDENCE_PARSE = "DIRECT_EVIDENCE_PARSE"
    #: Asks only whether a path exists and decodes. Emits a value from a finite
    #: structural contract and NEVER the file's contents.
    DIRECT_READABILITY_PROBE = "DIRECT_READABILITY_PROBE"
    #: Asks whether an imported module exposes named attributes. Reads no file.
    MODULE_PRESENCE_PROBE = "MODULE_PRESENCE_PROBE"


#: The prefix each kind contributes to a source-access identity, so an access
#: derived from the source can be matched to a declaration mechanically.
SOURCE_ACCESS_PREFIX: dict[SourceAccessKind, str] = {
    SourceAccessKind.CANONICAL_READER: "reader",
    SourceAccessKind.DIRECT_EVIDENCE_PARSE: "evidence_parse",
    SourceAccessKind.DIRECT_READABILITY_PROBE: "readability",
    SourceAccessKind.MODULE_PRESENCE_PROBE: "module_presence",
}

#: The canonical reader each function name identifies. The AST completeness
#: guard resolves reader calls through this, so renaming a reader without
#: updating its identity fails rather than silently dropping a dependency.
CANONICAL_READER_FUNCTIONS: dict[str, "RawSourceReader"] = {}


class DirectSource(str, Enum):
    """Authoritative sources acquired WITHOUT a canonical reader.

    Deliberately separate from :class:`RawSourceReader`. Forcing these into that
    enum -- merely because it already existed -- would say that a readability
    probe and reader A have the same semantics. They do not: A interprets
    evidence and fails closed, while a probe answers one structural question and
    is forbidden from emitting content at all.

    Each value is ``<kind prefix>:<target>``, which is what lets the derived
    inventory be compared against declarations without a hand-written bridge."""

    #: build_dashboard's SECOND read of the authority record, feeding
    #: worker_authority. Values travel behind _MISSING and the CONSUMER validates.
    AUTHORITY_RECORD_EVIDENCE = "evidence_parse:config/ew0a_authority.json"
    #: The four system_health readability probes. File readability evidence, NOT
    #: liveness -- the contract is explicit and these do not change that.
    READABILITY_AUTHORITY_RECORD = "readability:config/ew0a_authority.json"
    READABILITY_RUNTIME_POLICY = "readability:config/ew0a_runtime.json"
    READABILITY_OUTCOME_LEDGER = "readability:docs/EW0A_CERTIFICATION_OUTCOMES.jsonl"
    READABILITY_RECORDS_LEDGER = "readability:docs/EW0A_0B3_RECORDS.jsonl"
    #: Northstar 0B.3 contract presence, feeding mission deliverable progress.
    #: Neither Codex nor the repair brief named this one; the derived inventory
    #: did, which is the point of deriving it.
    NORTHSTAR_CONTRACT_PRESENCE = "module_presence:portfolio_automation.northstar"


#: What each direct source targets: a repo-relative path, or a module path for a
#: presence probe.
DIRECT_SOURCE_TARGETS: dict[DirectSource, str] = {
    DirectSource.AUTHORITY_RECORD_EVIDENCE: "config/ew0a_authority.json",
    DirectSource.READABILITY_AUTHORITY_RECORD: "config/ew0a_authority.json",
    DirectSource.READABILITY_RUNTIME_POLICY: "config/ew0a_runtime.json",
    DirectSource.READABILITY_OUTCOME_LEDGER: OUTCOME_LEDGER_REL,
    DirectSource.READABILITY_RECORDS_LEDGER: CONTROLLER_RECORDS_REL,
    DirectSource.NORTHSTAR_CONTRACT_PRESENCE: "portfolio_automation.northstar",
}

DIRECT_SOURCE_KINDS: dict[DirectSource, SourceAccessKind] = {
    DirectSource.AUTHORITY_RECORD_EVIDENCE: SourceAccessKind.DIRECT_EVIDENCE_PARSE,
    DirectSource.READABILITY_AUTHORITY_RECORD:
        SourceAccessKind.DIRECT_READABILITY_PROBE,
    DirectSource.READABILITY_RUNTIME_POLICY:
        SourceAccessKind.DIRECT_READABILITY_PROBE,
    DirectSource.READABILITY_OUTCOME_LEDGER:
        SourceAccessKind.DIRECT_READABILITY_PROBE,
    DirectSource.READABILITY_RECORDS_LEDGER:
        SourceAccessKind.DIRECT_READABILITY_PROBE,
    DirectSource.NORTHSTAR_CONTRACT_PRESENCE: SourceAccessKind.MODULE_PRESENCE_PROBE,
}


def source_access_identity(kind: SourceAccessKind, target: str) -> str:
    """The identity a derived access and a declaration must agree on."""
    return f"{SOURCE_ACCESS_PREFIX[kind]}:{target}"


#: Which local name in ``build_dashboard`` carries each blocked reader's raw
#: output. Used by the registry AND by the test that proves the coupling, so the
#: declaration and the call site cannot drift apart.
CANONICAL_READER_FUNCTIONS.update({
    "read_authority_level": RawSourceReader.AUTHORITY_LEVEL,
    "read_runtime_policy": RawSourceReader.RUNTIME_POLICY,
    "read_controller_records": RawSourceReader.CONTROLLER_RECORDS,
})

RAW_SOURCE_VARIABLES: dict[str, RawSourceReader] = {
    "level": RawSourceReader.AUTHORITY_LEVEL,
    "policy": RawSourceReader.RUNTIME_POLICY,
    "records": RawSourceReader.CONTROLLER_RECORDS,
}

#: Classifications that assert a surface introduces no direct dependency on raw
#: authoritative evidence. Declaring a raw source while claiming one of these is
#: a contradiction, and :func:`registry_classification_violations` reports it.
#: Classifications asserting a surface introduces no direct dependency on raw
#: authoritative evidence. Declaring a reader while claiming one of these is a
#: contradiction. HARDENED_SOURCE_READER is deliberately NOT here -- declaring a
#: dependency is exactly what it means.
_NO_RAW_DEPENDENCY_BOUNDARIES = frozenset({
    "MODULE_OWNED", "DERIVED_FROM_REGISTERED_INPUTS"})


@dataclass(frozen=True)
class _RegisteredProjection:
    boundary: ProjectionBoundary
    detail: str
    #: Canonical readers (A/B/C) whose output this surface consumes.
    canonical_readers: tuple[RawSourceReader, ...] = ()
    #: Authoritative sources this surface reaches WITHOUT a canonical reader.
    #: Separate from the readers above because the semantics differ; see
    #: SourceAccessKind.
    direct_sources: tuple[DirectSource, ...] = ()

    @property
    def source_dependencies(self) -> frozenset[str]:
        """Every dependency as a comparable identity, readers and direct alike.

        The completeness guard compares this against the inventory derived from
        the source, in BOTH directions, so a missing declaration and a dead one
        both fail."""
        readers = {source_access_identity(SourceAccessKind.CANONICAL_READER,
                                          r.value)
                   for r in self.canonical_readers}
        return frozenset(readers | {d.value for d in self.direct_sources})


#: EVERY top-level key ``build_dashboard`` emits, with its boundary status.
#:
#: This exists because the reason `learning` escaped five review rounds is that
#: the set of projection paths lived in human memory and prose -- including in
#: my own audit tables, twice. A test asserts this registry equals the actual
#: emitted key set, so a new surface cannot be added without declaring who owns
#: its boundary. That is the control that would have caught learning before
#: review did.
#:
#: It deliberately does NOT claim the dashboard is safe. Six of these surfaces
#: are marked as blocked on known source-reader debt, and one as withheld.
DASHBOARD_PROJECTION_REGISTRY: dict[str, _RegisteredProjection] = {
    # --- identity -----------------------------------------------------------
    "schema_version": _RegisteredProjection(
        ProjectionBoundary.MODULE_OWNED, "_base() constant"),
    "schema_kind": _RegisteredProjection(
        ProjectionBoundary.MODULE_OWNED, "_base() constant"),
    "read_model": _RegisteredProjection(
        ProjectionBoundary.MODULE_OWNED, "_base() constant"),
    # --- certified by the preceding commits ---------------------------------
    "run_history": _RegisteredProjection(
        ProjectionBoundary.GUI_R_VALIDATED,
        "every consumed OutcomeRecord field validated via validate_outcome_record; "
        "one invalid row invalidates the whole ledger"),
    # Declared NOTHING before this commit, while consuming reader A's effective
    # level AND build_dashboard's second, direct parse of the same record. The
    # classification stays GUI_R_VALIDATED because this surface validates that
    # evidence field by field -- a direct dependency is allowed to be validated,
    # it is only forbidden to be undeclared.
    "worker_authority": _RegisteredProjection(
        ProjectionBoundary.GUI_R_VALIDATED,
        "grants and forbidden_ops validated as list[str]; missing/null/empty kept "
        "distinct; permanent FORBIDDEN_OPS boundary unioned regardless; consumes "
        "reader A's effective level plus the direct authority-record evidence "
        "gateway, whose raw values it is the sole validator of",
        canonical_readers=(RawSourceReader.AUTHORITY_LEVEL,),
        direct_sources=(DirectSource.AUTHORITY_RECORD_EVIDENCE,)),
    "active_session": _RegisteredProjection(
        ProjectionBoundary.GUI_R_VALIDATED,
        "all 26 producer fields validated and copied individually; uncontracted "
        "producer keys are not exposed"),
    # --- withheld pending its own certification mission ---------------------
    "learning": _RegisteredProjection(
        ProjectionBoundary.UNAVAILABLE_PENDING_CERTIFICATION,
        "producer exists and exposes its entry point; its four nested projections "
        "are uncertified for WCC consumption, so the payload is not admitted"),
    # --- GUI-SR's controller-record admission result -------------------------
    # The ledger's own read outcome, validated field by field against the WCC
    # consumed-field contract -- which is what GUI_R_VALIDATED means, regardless
    # of which line built it.
    "controller_records": _RegisteredProjection(
        ProjectionBoundary.GUI_R_VALIDATED,
        "whole-ledger admission via read_controller_records; every WCC-consumed "
        "field validated before a row is admitted, the selected source carried as "
        "instance provenance, and a genuinely empty ledger kept distinguishable "
        "from an unusable one",
        canonical_readers=(RawSourceReader.CONTROLLER_RECORDS,)),
    # --- module-owned derivations -------------------------------------------
    "attention_items": _RegisteredProjection(
        ProjectionBoundary.MODULE_OWNED,
        "literal empty list; attention.zero_items_is_authoritative says what it means"),
    "attention": _RegisteredProjection(
        ProjectionBoundary.MODULE_OWNED,
        "AttentionCoverage; no attention producer exists, nothing is derived from "
        "source evidence"),
    # STILL not derived-only: build_dashboard passes level, policy and records
    # straight into _assess_backend_truth, which reads them directly rather than
    # reading already-registered projections. What changed at integration is that
    # all three are now TOTAL reader outputs -- a non-object records row can no
    # longer raise there, and the authority/runtime readers can no longer stop the
    # surface being assembled at all. So the honest label moved from BLOCKER to
    # HARDENED_SOURCE_READER, and NOT to DERIVED_FROM_REGISTERED_INPUTS.
    # Restructuring _assess_backend_truth to consume validated projections stays
    # out of scope: it is not needed for safety, only for that label.
    "backend_truth": _RegisteredProjection(
        ProjectionBoundary.HARDENED_SOURCE_READER,
        "capability truth states assembled from level (reader A), policy (reader B) "
        "and records (reader C) passed directly into _assess_backend_truth, plus "
        "authority_evidence and records_evidence; total reader output, but not "
        "derived-only",
        canonical_readers=(RawSourceReader.AUTHORITY_LEVEL,
                     RawSourceReader.RUNTIME_POLICY,
                     RawSourceReader.CONTROLLER_RECORDS)),
    # --- dependent on canonical readers GUI-SR made total -------------------
    # A, B and C were repaired in #39, so these are hardened dependencies rather
    # than blockers. Every declaration below was checked against the actual call
    # path, and the AST completeness guard compares this whole registry against
    # the inventory derived from build_dashboard's source.
    "controller": _RegisteredProjection(
        ProjectionBoundary.HARDENED_SOURCE_READER,
        "current_mission via read_runtime_policy (reader B, now total); remaining fields are declared contract constants or PENDING_BACKEND",
        canonical_readers=(RawSourceReader.RUNTIME_POLICY,)),
    "mission": _RegisteredProjection(
        ProjectionBoundary.HARDENED_SOURCE_READER,
        "mission_id via read_runtime_policy (reader B, now total); deliverable "
        "progress from Northstar 0B.3 contract presence, a module attribute probe "
        "that opens no file and is fail-closed to the empty set",
        canonical_readers=(RawSourceReader.RUNTIME_POLICY,),
        direct_sources=(DirectSource.NORTHSTAR_CONTRACT_PRESENCE,)),
    "worker": _RegisteredProjection(
        ProjectionBoundary.HARDENED_SOURCE_READER,
        "authority level (reader A, now total), mission (reader B) and recent verdicts via read_controller_records (reader C, whole-ledger admission with the WCC consumed-field contract); carries records_evidence",
        canonical_readers=(RawSourceReader.AUTHORITY_LEVEL, RawSourceReader.RUNTIME_POLICY,
                     RawSourceReader.CONTROLLER_RECORDS)),
    "supervisor": _RegisteredProjection(
        ProjectionBoundary.HARDENED_SOURCE_READER,
        "verdict counts via read_controller_records (reader C); carries records_evidence, and counts are null rather than zero when the ledger cannot answer",
        canonical_readers=(RawSourceReader.CONTROLLER_RECORDS,)),
    "apprenticeship": _RegisteredProjection(
        ProjectionBoundary.HARDENED_SOURCE_READER,
        "comparison counts via read_controller_records (reader C); carries records_evidence, counts null when the ledger is unusable",
        canonical_readers=(RawSourceReader.CONTROLLER_RECORDS,)),
    # Codex P2 (finding 3960949424): this declared reader A alone while opening
    # FOUR files directly through _readability, so the totality accounting proved
    # nothing about them. All four are declared now, as probes rather than
    # readers, because their semantics differ: a probe answers one structural
    # question and is forbidden from emitting content. config_readability remains
    # exactly what the contract says -- file readability evidence, NOT liveness.
    "system_health": _RegisteredProjection(
        ProjectionBoundary.HARDENED_SOURCE_READER,
        "authority level (reader A, now total); every component field is "
        "PENDING_BACKEND; config_readability is four direct readability probes -- "
        "file readability evidence, not liveness -- each certified to emit only a "
        "value from a finite structural contract and never file contents",
        canonical_readers=(RawSourceReader.AUTHORITY_LEVEL,),
        direct_sources=(DirectSource.READABILITY_AUTHORITY_RECORD,
                        DirectSource.READABILITY_RUNTIME_POLICY,
                        DirectSource.READABILITY_OUTCOME_LEDGER,
                        DirectSource.READABILITY_RECORDS_LEDGER)),
}


def registry_classification_violations() -> list[str]:
    """Surfaces claiming freedom from raw evidence while declaring a raw source.

    The registry alone was a hand-maintained assertion, and it shipped with
    ``backend_truth`` marked derived-only while it consumed three raw readers --
    an optimistic entry in the very artifact built to prevent optimistic entries.
    Coupling the declaration to the classification is what makes it a control."""
    violations: list[str] = []
    for name, entry in DASHBOARD_PROJECTION_REGISTRY.items():
        declared = entry.source_dependencies
        if declared and entry.boundary.value in _NO_RAW_DEPENDENCY_BOUNDARIES:
            violations.append(
                f"{name} declares source dependencies {sorted(declared)} but is "
                f"classified {entry.boundary.value}")
    return violations


def declared_source_dependencies() -> dict[str, frozenset[str]]:
    """Every surface's declared dependency identities, for the completeness guard."""
    return {name: entry.source_dependencies
            for name, entry in DASHBOARD_PROJECTION_REGISTRY.items()
            if entry.source_dependencies}


def declared_source_dependency_union() -> frozenset[str]:
    """The union the AST-derived inventory must equal EXACTLY.

    Equality, not containment: a dependency the source performs but nothing
    declares is a blind spot, and a dependency declared but no longer performed
    is a dead claim that makes the registry look more coupled than it is. Both
    are failures.

    This is the inversion the source-access closure is about. The universe of
    possible accesses is no longer ``set(RawSourceReader)`` -- a hand-maintained
    enum that could not see ``_readability`` at all. The SOURCE decides which
    dependencies exist; these declarations decide how each one is treated."""
    union: set[str] = set()
    for declared in declared_source_dependencies().values():
        union |= declared
    return frozenset(union)


def _read_authority_record_evidence(root: Path) -> tuple[Any, Any, Any]:
    """The dashboard's SECOND read of the authority record, as a named gateway.

    Returns ``(grants, forbidden_ops, level)`` exactly as they appear in the
    record, each behind the ``_MISSING`` sentinel. **This function validates
    nothing on purpose** -- ``build_worker_authority_summary`` is the sole
    validator of authority-record evidence, and adding a second opinion here
    would be a second authority policy engine.

    Why it exists as a function at all: GUI-RI's source-access closure requires
    every authoritative access to be an inventoried, named dependency. This read
    was an anonymous ``.read_text()`` embedded in the assembler, so the boundary
    it crosses had no name to declare. The shape is now

        build_dashboard -> named source gateway -> validated worker_authority

    and ``DirectSource.AUTHORITY_RECORD_EVIDENCE`` is what the registry declares.

    This is NOT a duplicate of ``read_authority_level``. That reader answers
    "what authority is in force", applies the ladder and fails closed to A0.
    This gateway answers "what does the record literally say", so the projection
    can report the record's own evidence quality without it being able to
    escalate authority. Both readings of the same file are deliberate.

    Totality: GUI-SR made this read total -- a scalar/array/null root used to
    raise ``AttributeError`` even after the canonical reader was fixed. The root
    is still checked before it is indexed, and ``UnicodeError`` is still named
    because it is a ``ValueError`` subclass that used to escape and take the
    whole dashboard down.

    Missing / null / empty stay three distinguishable states all the way to the
    validator: ``.get(name, _MISSING)`` never collapses an absent field into an
    explicit ``null``."""
    grants: Any = _MISSING
    record_forbidden: Any = _MISSING
    raw_level: Any = _MISSING
    ap = root / "config" / "ew0a_authority.json"
    if ap.exists():
        try:
            authority_record = json.loads(ap.read_text(encoding="utf-8"))
            if isinstance(authority_record, dict):
                grants = authority_record.get("grants", _MISSING)
                record_forbidden = authority_record.get("forbidden_ops", _MISSING)
                raw_level = authority_record.get("level", _MISSING)
        except (OSError, ValueError, UnicodeError):
            # Fail closed to "no evidence" for ALL three, never a partial read.
            grants = record_forbidden = raw_level = _MISSING
    return grants, record_forbidden, raw_level


def build_dashboard(repo_root: str | Path, now: str | None = None) -> dict[str, Any]:
    """Assemble the full read-only dashboard from authoritative sources.

    ``now`` is injected rather than read from the clock (the no-fabricated-time
    discipline used across the Northstar contracts); readiness assessment needs a
    timestamp and a projection must never invent one.

    ORDER IS LOAD-BEARING. The session, learning and run-history projections are
    built BEFORE the truth assessment so their states can be classified with
    everything else. They used to be appended afterwards, which is precisely how
    the projection that answers "what is happening now" ended up as the only one
    carrying no truth state at all."""
    root = Path(repo_root)
    level = read_authority_level(root)
    policy = read_runtime_policy(root)
    records_read = read_controller_records(root)
    records = records_read.records
    mission = policy.mission_id if policy else None

    # authoritative contract presence -> mission progress
    try:
        import portfolio_automation.northstar as ns
        present = {n for n in _NORTHSTAR_0B3 if hasattr(ns, n)}
    except Exception:  # noqa: BLE001
        present = set()

    # The SECOND read of the authority record, behind a named gateway rather
    # than an anonymous read_text() buried in the assembler. See
    # _read_authority_record_evidence.
    grants, record_forbidden, raw_level = _read_authority_record_evidence(root)

    controller = ControllerSummary(
        controller_identity="claude_code", controller_role="authoritative_controller",
        controller_level="C_AUTHORITATIVE", current_mission=mission,
        # No health producer exists. "ACTIVE" was an assertion, and the interface
        # is explicit that liveness must not be inferred from process existence.
        operational_state=PENDING_BACKEND,
        controller_since=PENDING_BACKEND, escalation_role="human")
    worker = WorkerSummary(
        worker_identity="engineer.local_qwen2_5_7b", role="engineer",
        operational_state=PENDING_BACKEND, ew_authority=level.value, controller_level="C0.5_SHADOW",
        current_mission=mission, current_task=PENDING_BACKEND, queue_size=PENDING_BACKEND,
        activity_summary=PENDING_BACKEND, next_action=PENDING_BACKEND,
        recent_verification_outcomes=(
            _recent_verification_outcomes(records) if records_read.is_usable else []),
        escalation_state="none",
        records_evidence=records_read.availability)
    health = SystemHealthSummary(
        # Every component below needs a health producer and none exists. The two
        # that used to read ACTIVE/READY were the only ones asserting liveness
        # from the fact that this code was running at all.
        controller=PENDING_BACKEND, gpt_supervisor=PENDING_BACKEND,
        engineer_runtime=PENDING_BACKEND, sandbox=PENDING_BACKEND,
        evidence_bridge=PENDING_BACKEND,
        # Authority level is CONFIGURATION, not health. Kept here because the
        # field is part of the published shape, and labelled by health_note.
        authority=level.value, control_loop=PENDING_BACKEND,
        config_readability={
            "authority_record": _readability(root / "config" / "ew0a_authority.json"),
            "runtime_policy": _readability(root / "config" / "ew0a_runtime.json"),
            "outcome_ledger": _readability(root / OUTCOME_LEDGER_REL),
            "records_ledger": _readability(root / CONTROLLER_RECORDS_REL),
        })

    # Built BEFORE the truth assessment so every one of them is classified.
    worker_authority = build_worker_authority_summary(
        level, grants, record_forbidden, raw_level)
    run_history = build_run_history(root)
    learning, learning_state = _project_learning(root, worker.worker_identity, now)
    session_payload, session_status, session_detail = _build_active_session(root)
    active_session, session_state = project_active_session(
        session_payload, mission, now, session_status, session_detail)

    dashboard = {
        **_base("Dashboard"),
        "controller": controller.to_dict(),
        "supervisor": build_supervisor_summary(
            records, records_read.availability).to_dict(),
        "worker": worker.to_dict(),
        "worker_authority": worker_authority.to_dict(),
        "mission": build_mission_summary(mission or "unknown", present).to_dict(),
        # GUI-SR's records_evidence argument, so an unusable ledger yields
        # null counts rather than flattering zeros.
        "apprenticeship": build_apprenticeship_summary(
            records, records_read.availability).to_dict(),
        # Unchanged shape for compatibility; "attention" below says what it MEANS.
        "attention_items": [],
        "attention": build_attention_coverage(items=[], derivation_exists=False).to_dict(),
        "system_health": health.to_dict(),
        "run_history": run_history.to_dict(),
        "learning": learning,
        # Read-only and NON-AUTHORITATIVE like every other projection here. An
        # absent session is reported as absent, never synthesized.
        "active_session": active_session,
        # The ledger's own usability, stated once and authoritatively, so a
        # consumer does not have to infer it from three separate summaries.
        "controller_records": records_read.to_dict(),
    }
    # Backend truth states + capability readiness. Derived from the evidence just
    # assembled -- never asserted, and never a LIVE percentage.
    dashboard["backend_truth"] = _assess_backend_truth(
        level=level, policy=policy, records=records, worker=worker, now=now,
        session_state=session_state, learning_state=learning_state,
        run_history=run_history,
        authority_evidence=worker_authority.record_evidence,
        records_evidence=records_read.availability).to_dict()
    return dashboard


def _build_active_session(repo_root: Path) -> tuple[Any, str, str]:
    """Ask the session producer; return ``(payload, producer_status, detail)``.

    THE READ MODEL NO LONGER DECIDES WHETHER A SESSION EXISTS. It previously
    gated on ``ledger_path(repo_root).exists()``, which tests ONE concrete
    ledger filename while the producer supports multiple ledgers, episode
    discovery, corrected session identities and latest-episode selection. A
    perfectly discoverable session under any other ledger name was therefore
    reported as PENDING_BACKEND -- data absence dressed up as missing
    engineering, and in the multi-ledger case not even data absence.

    Episode discovery is NOT reimplemented here; it is delegated, which is the
    same reason the GUI must not reimplement this projection.

    Degrades rather than failing the dashboard: an observability problem must
    never make the engineering evidence unreadable."""
    module, status, detail = _import_producer(_SESSION_PRODUCER_MODULE)
    if status != _PRODUCER_OK:
        return PENDING_BACKEND if status == _PRODUCER_ABSENT else None, status, detail

    projection = getattr(module, "session_projection", None)
    if projection is None:
        return None, _PRODUCER_UNAVAILABLE, (
            f"{_SESSION_PRODUCER_MODULE} exposes no session_projection")
    try:
        return projection(repo_root=repo_root), _PRODUCER_OK, ""
    except Exception as exc:  # noqa: BLE001 - the producer exists and failed
        return None, _PRODUCER_UNAVAILABLE, (
            f"session_projection raised {type(exc).__name__}")
