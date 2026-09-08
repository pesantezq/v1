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
READMODEL_SCHEMA_VERSION = "engineering.readmodel.v0"
PENDING_BACKEND = "PENDING_BACKEND"

#: The engineering outcome/run ledger. Named here rather than left as a literal
#: at the call site so the GUI can stop naming it itself — a consumer that has to
#: know the path is a consumer doing its own interpretation.
OUTCOME_LEDGER_REL = "docs/EW0A_CERTIFICATION_OUTCOMES.jsonl"

#: The controller apprenticeship / certification records ledger. A DIFFERENT
#: evidence domain from the outcome ledger above, with a different verdict field
#: (``gpt_verdict`` here, ``supervisor_verdict`` there). Collapsing the two into
#: one supervisor number produces a count that belongs to neither.
RECORDS_LEDGER_REL = "docs/EW0A_0B3_RECORDS.jsonl"

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
    recent_pass: int
    recent_repair: int
    recent_escalate: int
    recent_abstain: int
    recent_unavailable: int
    last_successful_verification: str | None
    measured_latency_ms: str            # PENDING_BACKEND (no real latency record)
    verification_queue: str             # PENDING_BACKEND (no real queue)
    outage_state: str
    #: Which ledger these counts came from, and under which field name. Two
    #: legitimate ledgers record supervisor verdicts for different purposes; a
    #: consumer must be able to say which one it is showing.
    source: str = RECORDS_LEDGER_REL
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
    #: EW-0A defines exactly one Engineer Worker with a persistent identity, so
    #: this is a contract constant rather than an unbuilt lookup. It becomes a
    #: derivation the moment a second worker exists — a later mission, and
    #: deliberately not this one.
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
    decisions_shadowed: int
    task_selection_agreements: int
    risk_agreements: int
    routing_agreements: int
    missed_escalations: int
    unsafe_underclassifications: int
    authority_expansion_proposals: int
    c1_readiness: str                   # NOT_READY | CANDIDATE | READY_FOR_CERTIFICATION

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
def _read_records(repo_root: Path, rel: str = RECORDS_LEDGER_REL) -> list[dict[str, Any]]:
    p = repo_root / rel
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def build_supervisor_summary(records: list[dict[str, Any]]) -> SupervisorSummary:
    verdicts = [r.get("gpt_verdict") for r in records if r.get("gpt_verdict")]
    def c(v):
        return sum(1 for x in verdicts if str(x).upper() == v)
    last_pass = next((r.get("recorded_at") for r in reversed(records)
                      if str(r.get("gpt_verdict", "")).upper() == "PASS"), None)
    return SupervisorSummary(
        availability=PENDING_BACKEND, current_state=PENDING_BACKEND,
        recent_pass=c("PASS"), recent_repair=c("REPAIR"), recent_escalate=c("ESCALATE"),
        recent_abstain=c("ABSTAIN"), recent_unavailable=c("SUPERVISOR_UNAVAILABLE"),
        last_successful_verification=last_pass, measured_latency_ms=PENDING_BACKEND,
        verification_queue=PENDING_BACKEND, outage_state=PENDING_BACKEND)


def build_apprenticeship_summary(records: list[dict[str, Any]]) -> ApprenticeshipSummary:
    comps = [r for r in records if r.get("kind") == "ApprenticeshipComparison"]
    shadowed = len([r for r in records if r.get("kind") == "ControllerDecisionCandidateV0"])
    return ApprenticeshipSummary(
        controller_level="C0.5_SHADOW",
        decisions_shadowed=shadowed,
        task_selection_agreements=sum(1 for c in comps if c.get("engineer_proposed_task_relates_to_experimentspec")),
        risk_agreements=sum(1 for c in comps if c.get("risk_agreement")),
        routing_agreements=sum(1 for c in comps if c.get("routing_agreement")),
        missed_escalations=sum(1 for c in comps if c.get("danger_underclassified_architecture_as_engineer")),
        unsafe_underclassifications=sum(1 for c in comps if c.get("danger_underclassified_architecture_as_engineer")),
        authority_expansion_proposals=0,
        c1_readiness="NOT_READY")


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


def _readability(path: Path) -> str:
    """File readability — NOT component health. Named so it cannot be mistaken."""
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
                          authority_evidence: str = "LIVE") -> ReadinessAssessment:
    """Classify every oversight capability from the evidence actually present.

    Each capability declares whether a PRODUCER exists. That is an engineering
    fact about this repository, not a runtime observation, and it is what keeps a
    missing subsystem reported as PENDING_BACKEND instead of as an outage.

    Nothing here builds a backend. A capability with no producer stays pending;
    the honest answer is the deliverable."""
    last_verification = None
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
                   required=True, detail="recorded gpt_verdict history"),
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
    #: Source evidence validated field by field against a declared contract.
    GUI_R_VALIDATED = "GUI_R_VALIDATED"
    #: Producer exists; its WCC payload contract is not certified, so the
    #: payload is withheld and the projection reports UNAVAILABLE.
    UNAVAILABLE_PENDING_CERTIFICATION = "UNAVAILABLE_PENDING_CERTIFICATION"
    #: Reaches this module through a canonical source reader with a known,
    #: deliberately unrepaired failure mode. NOT certified.
    KNOWN_SOURCE_READER_BLOCKER = "KNOWN_SOURCE_READER_BLOCKER"
    #: Computed from surfaces registered above; carries no fresh source evidence.
    DERIVED_FROM_REGISTERED_INPUTS = "DERIVED_FROM_REGISTERED_INPUTS"


class RawSourceReader(str, Enum):
    """Canonical readers with known, deliberately unrepaired failure modes.

    Named A/B/C to match the deferred GUI-SR mission, so a surface's exposure is
    machine-readable rather than described in a comment."""

    #: A -- raises TypeError on a non-object JSON root.
    AUTHORITY_LEVEL = "A:ew0a_authority.read_authority_level"
    #: B -- raises AttributeError on a non-object JSON root.
    RUNTIME_POLICY = "B:ew0a_loop.read_runtime_policy"
    #: C -- admits non-dict rows whose .get() then raises downstream.
    CONTROLLER_RECORDS = "C:ew0a_readmodels._read_records"


#: Which local name in ``build_dashboard`` carries each blocked reader's raw
#: output. Used by the registry AND by the test that proves the coupling, so the
#: declaration and the call site cannot drift apart.
RAW_SOURCE_VARIABLES: dict[str, RawSourceReader] = {
    "level": RawSourceReader.AUTHORITY_LEVEL,
    "policy": RawSourceReader.RUNTIME_POLICY,
    "records": RawSourceReader.CONTROLLER_RECORDS,
}

#: Classifications that assert a surface introduces no direct dependency on raw
#: authoritative evidence. Declaring a raw source while claiming one of these is
#: a contradiction, and :func:`registry_classification_violations` reports it.
_NO_RAW_DEPENDENCY_BOUNDARIES = frozenset({
    "MODULE_OWNED", "DERIVED_FROM_REGISTERED_INPUTS"})


@dataclass(frozen=True)
class _RegisteredProjection:
    boundary: ProjectionBoundary
    detail: str
    #: Raw blocked readers this surface consumes DIRECTLY, without a GUI-R
    #: validator in between.
    raw_sources: tuple[RawSourceReader, ...] = ()


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
    "worker_authority": _RegisteredProjection(
        ProjectionBoundary.GUI_R_VALIDATED,
        "grants and forbidden_ops validated as list[str]; missing/null/empty kept "
        "distinct; permanent FORBIDDEN_OPS boundary unioned regardless"),
    "active_session": _RegisteredProjection(
        ProjectionBoundary.GUI_R_VALIDATED,
        "all 26 producer fields validated and copied individually; uncontracted "
        "producer keys are not exposed"),
    # --- withheld pending its own certification mission ---------------------
    "learning": _RegisteredProjection(
        ProjectionBoundary.UNAVAILABLE_PENDING_CERTIFICATION,
        "producer exists and exposes its entry point; its four nested projections "
        "are uncertified for WCC consumption, so the payload is not admitted"),
    # --- module-owned derivations -------------------------------------------
    "attention_items": _RegisteredProjection(
        ProjectionBoundary.MODULE_OWNED,
        "literal empty list; attention.zero_items_is_authoritative says what it means"),
    "attention": _RegisteredProjection(
        ProjectionBoundary.MODULE_OWNED,
        "AttentionCoverage; no attention producer exists, nothing is derived from "
        "source evidence"),
    # NOT derived-only, which is what this entry used to claim. build_dashboard
    # passes RAW level, policy and records straight into _assess_backend_truth,
    # which reads them directly -- so a non-object records row raises there, and
    # the authority/runtime readers can prevent this surface from being assembled
    # at all. The optimistic classification mattered because a GUI-SR mission
    # could have repaired the six visible blocked surfaces and signed off while
    # this seventh dependent surface stayed unsafe. Reclassification here is
    # deliberate; restructuring _assess_backend_truth to consume validated
    # projections belongs to GUI-SR.
    "backend_truth": _RegisteredProjection(
        ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER,
        "capability truth states assembled from RAW level (blocker A), RAW policy "
        "(blocker B) and RAW records (blocker C) passed directly into "
        "_assess_backend_truth; not derived-only",
        raw_sources=(RawSourceReader.AUTHORITY_LEVEL,
                     RawSourceReader.RUNTIME_POLICY,
                     RawSourceReader.CONTROLLER_RECORDS)),
    # --- blocked on known, deliberately unrepaired source-reader debt -------
    # A: ew0a_authority.read_authority_level raises TypeError on a non-object
    #    JSON root. B: ew0a_loop.read_runtime_policy raises AttributeError on the
    #    same shape. C: _read_records admits non-dict rows whose .get() then
    #    raises in build_supervisor_summary. None is repaired in this PR, and C
    #    must NOT be repaired by filtering.
    "controller": _RegisteredProjection(
        ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER,
        "current_mission via read_runtime_policy (blocker B); remaining fields are "
        "declared contract constants or PENDING_BACKEND",
        raw_sources=(RawSourceReader.RUNTIME_POLICY,)),
    "mission": _RegisteredProjection(
        ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER,
        "mission_id via read_runtime_policy (blocker B)",
        raw_sources=(RawSourceReader.RUNTIME_POLICY,)),
    "worker": _RegisteredProjection(
        ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER,
        "authority level (blocker A), mission (blocker B) and recent verdicts via "
        "_read_records (blocker C)",
        raw_sources=(RawSourceReader.AUTHORITY_LEVEL, RawSourceReader.RUNTIME_POLICY,
                     RawSourceReader.CONTROLLER_RECORDS)),
    "supervisor": _RegisteredProjection(
        ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER,
        "verdict counts via _read_records (blocker C)",
        raw_sources=(RawSourceReader.CONTROLLER_RECORDS,)),
    "apprenticeship": _RegisteredProjection(
        ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER,
        "comparison counts via _read_records (blocker C)",
        raw_sources=(RawSourceReader.CONTROLLER_RECORDS,)),
    "system_health": _RegisteredProjection(
        ProjectionBoundary.KNOWN_SOURCE_READER_BLOCKER,
        "authority level (blocker A); every component field is PENDING_BACKEND and "
        "config_readability is file readability, not liveness",
        raw_sources=(RawSourceReader.AUTHORITY_LEVEL,)),
}


def registry_classification_violations() -> list[str]:
    """Surfaces claiming freedom from raw evidence while declaring a raw source.

    The registry alone was a hand-maintained assertion, and it shipped with
    ``backend_truth`` marked derived-only while it consumed three raw readers --
    an optimistic entry in the very artifact built to prevent optimistic entries.
    Coupling the declaration to the classification is what makes it a control."""
    violations: list[str] = []
    for name, entry in DASHBOARD_PROJECTION_REGISTRY.items():
        if entry.raw_sources and entry.boundary.value in _NO_RAW_DEPENDENCY_BOUNDARIES:
            violations.append(
                f"{name} declares raw sources "
                f"{sorted(s.value for s in entry.raw_sources)} but is classified "
                f"{entry.boundary.value}")
    return violations


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
    records = _read_records(root)
    mission = policy.mission_id if policy else None

    # authoritative contract presence -> mission progress
    try:
        import portfolio_automation.northstar as ns
        present = {n for n in _NORTHSTAR_0B3 if hasattr(ns, n)}
    except Exception:  # noqa: BLE001
        present = set()

    # Raw record values are passed through UNVALIDATED on purpose: the builder
    # owns the validation, so there is exactly one place where authority record
    # evidence is checked.
    grants: Any = _MISSING
    record_forbidden: Any = _MISSING
    raw_level: Any = _MISSING
    ap = root / "config" / "ew0a_authority.json"
    if ap.exists():
        try:
            authority_record = json.loads(ap.read_text(encoding="utf-8"))
            if isinstance(authority_record, dict):
                # `.get(name, _MISSING)` so an absent field stays distinguishable
                # from an explicit null all the way to the validator.
                grants = authority_record.get("grants", _MISSING)
                record_forbidden = authority_record.get("forbidden_ops", _MISSING)
                raw_level = authority_record.get("level", _MISSING)
        except (OSError, ValueError, UnicodeError):
            grants = _MISSING

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
        recent_verification_outcomes=[str(r.get("gpt_verdict")) for r in records if r.get("gpt_verdict")][-5:],
        escalation_state="none")
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
            "records_ledger": _readability(root / RECORDS_LEDGER_REL),
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
        "supervisor": build_supervisor_summary(records).to_dict(),
        "worker": worker.to_dict(),
        "worker_authority": worker_authority.to_dict(),
        "mission": build_mission_summary(mission or "unknown", present).to_dict(),
        "apprenticeship": build_apprenticeship_summary(records).to_dict(),
        # Unchanged shape for compatibility; "attention" below says what it MEANS.
        "attention_items": [],
        "attention": build_attention_coverage(items=[], derivation_exists=False).to_dict(),
        "system_health": health.to_dict(),
        "run_history": run_history.to_dict(),
        "learning": learning,
        # Read-only and NON-AUTHORITATIVE like every other projection here. An
        # absent session is reported as absent, never synthesized.
        "active_session": active_session,
    }
    # Backend truth states + capability readiness. Derived from the evidence just
    # assembled -- never asserted, and never a LIVE percentage.
    dashboard["backend_truth"] = _assess_backend_truth(
        level=level, policy=policy, records=records, worker=worker, now=now,
        session_state=session_state, learning_state=learning_state,
        run_history=run_history,
        authority_evidence=worker_authority.record_evidence).to_dict()
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
