"""Synthetic-only certification of the thin VS-002 execution adapter.

NONE of these tests open, enumerate, validate, or evaluate the real frozen
evidence package. ``consumer.validate`` and ``result_runner.run`` are monkeypatched
to synthetic stand-ins; the real certified runner's science is exercised by its
own suite, not re-litigated here. The adapter's job — explicit path, validate
once, identity gates, canonical assembly, governed immutable write, deterministic
verification/replay — is what is under test.

The frozen package IDENTITY STRINGS used below are read from the committed
preregistration (metadata), never from the package itself.
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_automation import data_governance as DG
from portfolio_automation.vs002_evidence import consumer as CON
from portfolio_automation.vs002_evidence import result_contract as RC
from portfolio_automation.vs002_evidence import result_runner as RR
from portfolio_automation.vs002_evidence import execution as E
from portfolio_automation.vs002_evidence.execution import (
    VS002ExecutionError,
    VS002ExecutionRequest,
    execute_frozen_vs002,
    verify_frozen_vs002_result,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GEN_AT = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
RUNNER_SHA = "05c4b6dd5eb43c75a3281d9283e8c3863a3cbf43"


# ───────────────────────────── shared fixtures ──────────────────────────────
@pytest.fixture(scope="module")
def prereg() -> dict:
    """The real committed frozen preregistration (a committed JSON, NOT the
    evidence package)."""
    return RR.load_preregistration(REPO_ROOT)


@pytest.fixture(scope="module")
def binding(prereg):
    return RR.verify_preregistration(prereg)


@pytest.fixture
def ident(binding):
    return binding.evidence_binding


def make_snapshot(ident, *, package_id=None, code_sha=None, schema=None) -> CON.ValidatedSnapshot:
    """A synthetic ValidatedSnapshot whose manifest echoes the frozen identity
    strings. Overrides let a test force a specific mismatch."""
    return CON.ValidatedSnapshot(
        root=Path("/synthetic/package"),
        manifest={
            "eligible_universe": ["AAA", "BBB"],
            "signal_evidence_cutoff": "2025-01-01",
            "package_id": package_id if package_id is not None else ident.package_id,
            "code_sha": code_sha if code_sha is not None else ident.source_production_sha,
            "schema_version": schema if schema is not None else ident.evidence_schema_version,
        },
        signals=[],
        returns=[],
    )


def make_result(evidence_identity, generated_at) -> RC.VS002Result:
    """A deterministic synthetic VS002Result (INCONCLUSIVE, vs002_executed=False).

    Binds the preregistration identity + the supplied evidence identity so the
    adapter's provenance/verification pass exactly as with the real runner."""
    iso = generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    empty_pop = RC.PopulationSummary(
        base_population_rows=0, scored_population_rows=0, evaluated_signal_rows=0,
        selected_cohort_dates=(), exclusion_counts={},
    )
    h1 = RC.H1Result(status=RC.H1Status.INCONCLUSIVE, cohort_count=0,
                     experiment_mean=None, interval=None)
    h2 = RC.H2Result(status=RC.H2Status.INCONCLUSIVE, valid_cohort_ic_count=0,
                     point_ic=None, reporting_interval=None)
    na = RC.NoActionResult(status=RC.NoActionStatus.NOT_BEAT, cohort_count=0,
                           mean_net_no_action_return=None, interval=None)
    return RC.VS002Result(
        schema_version=RC.RESULT_SCHEMA_VERSION,
        schema_kind=RC.RESULT_SCHEMA_KIND,
        runner_id=RC.DEFAULT_RUNNER_ID,
        runner_version=RC.RUNNER_VERSION,
        generated_at=iso,
        preregistration_id=RR.EXPECTED_PREREG_ID,
        preregistration_freeze_digest=RR.EXPECTED_FREEZE_DIGEST,
        preregistration_schema_version=RR.EXPECTED_PREREG_SCHEMA,
        preregistration_verified=True,
        evidence_binding=evidence_identity,
        population=empty_pop,
        h1=h1, h2=h2, no_action=na,
        criterion_outcome=RC.CriterionOutcome.INCONCLUSIVE,
        skill_outcome=RC.SkillOutcome.NOT_ESTABLISHED,
    )


@pytest.fixture
def harness(monkeypatch, binding, ident):
    """Install synthetic validate()/run() with call counters; return handles."""
    state = {"validate_calls": 0, "run_calls": 0, "snap": make_snapshot(ident)}

    def fake_validate(path):
        state["validate_calls"] += 1
        return state["snap"]

    def fake_run(snap, prereg, *, evidence_identity, generated_at,
                 runner_id=RC.DEFAULT_RUNNER_ID, runner_version=RC.RUNNER_VERSION):
        state["run_calls"] += 1
        assert snap is state["snap"], "run must receive the SAME validated snapshot"
        return make_result(evidence_identity, generated_at)

    monkeypatch.setattr(E.CON, "validate", fake_validate)
    monkeypatch.setattr(E.RR, "run", fake_run)
    return state


def make_request(tmp_path, binding, *, filename="run1.json", package_path="outputs/pkg/vs002",
                 generated_at=GEN_AT) -> VS002ExecutionRequest:
    return VS002ExecutionRequest(
        package_path=Path(package_path),
        preregistration=RR.load_preregistration(REPO_ROOT),
        historical_transport_digest=binding.evidence_binding.package_transport_digest,
        runner_source_sha=RUNNER_SHA,
        generated_at=generated_at,
        result_filename=filename,
        base_dir=str(tmp_path),
    )


# ─────────────────────────────── happy path ─────────────────────────────────
def test_happy_path_writes_governed_result(tmp_path, binding, harness):
    req = make_request(tmp_path, binding)
    out = execute_frozen_vs002(req)
    written = Path(out["written_path"])
    assert written.exists()
    assert written == (tmp_path / "vs002_result" / "run1.json")
    assert out["vs002_executed"] is False
    assert out["package_validation_count"] == 1
    assert out["criterion_outcome"] == "EVIDENCE_INCONCLUSIVE"


def test_validate_called_exactly_once(tmp_path, binding, harness):
    execute_frozen_vs002(make_request(tmp_path, binding))
    # includes the post-write deterministic replay, which must NOT re-validate
    assert harness["validate_calls"] == 1


def test_replay_reuses_snapshot_no_reopen(tmp_path, binding, harness):
    execute_frozen_vs002(make_request(tmp_path, binding))
    # run invoked twice (execute + replay), validate only once
    assert harness["run_calls"] == 2
    assert harness["validate_calls"] == 1


def test_standalone_verify_does_not_reopen(tmp_path, binding, harness):
    req = make_request(tmp_path, binding)
    out = execute_frozen_vs002(req)
    before = harness["validate_calls"]
    res = verify_frozen_vs002_result(Path(out["written_path"]), req,
                                     snap=harness["snap"],
                                     expected_identity=harness["snap"].manifest and
                                     E.EvidenceIdentity(
                                         package_id=harness["snap"].manifest["package_id"],
                                         package_transport_digest=req.historical_transport_digest,
                                         source_production_sha=harness["snap"].manifest["code_sha"],
                                         evidence_schema_version=harness["snap"].manifest["schema_version"]))
    assert res["verified"] is True
    assert harness["validate_calls"] == before  # no package reopen


# ─────────────────────────── explicit path / discovery ──────────────────────
@pytest.mark.parametrize("bad", ["outputs/latest", "outputs/*.pkg", "pkg/??", "x/newest/p",
                                 "x/current/p", "a[b]"])
def test_discovery_patterns_refused(tmp_path, binding, harness, bad):
    req = make_request(tmp_path, binding, package_path=bad)
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(req)
    assert ei.value.category == "PACKAGE_DISCOVERY_FORBIDDEN"
    assert harness["validate_calls"] == 0


def test_blank_path_refused(tmp_path, binding, harness):
    # a whitespace-only path carries no explicit package selection
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(make_request(tmp_path, binding, package_path="   "))
    assert ei.value.category == "PACKAGE_PATH_INVALID"
    assert harness["validate_calls"] == 0


# ─────────────────────────── identity gates (fail closed) ───────────────────
def test_package_id_mismatch_blocks_and_writes_nothing(tmp_path, binding, ident, monkeypatch, harness):
    harness["snap"] = make_snapshot(ident, package_id="vs002evd_deadbeef")
    req = make_request(tmp_path, binding)
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(req)
    assert ei.value.category == "PACKAGE_ID_MISMATCH"
    assert not (tmp_path / "vs002_result" / "run1.json").exists()


def test_source_production_sha_mismatch_blocks(tmp_path, binding, ident, harness):
    harness["snap"] = make_snapshot(ident, code_sha="0" * 40)
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(make_request(tmp_path, binding))
    assert ei.value.category == "SOURCE_PRODUCTION_SHA_MISMATCH"


def test_transport_digest_mismatch_is_binding_error(tmp_path, binding, harness):
    req = VS002ExecutionRequest(
        package_path=Path("outputs/pkg/vs002"),
        preregistration=RR.load_preregistration(REPO_ROOT),
        historical_transport_digest="not-the-frozen-attestation",
        runner_source_sha=RUNNER_SHA,
        generated_at=GEN_AT,
        result_filename="run1.json",
        base_dir=str(tmp_path),
    )
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(req)
    assert ei.value.category == "EVIDENCE_BINDING_MISMATCH"


def test_preregistration_mismatch_blocks(tmp_path, binding, harness):
    bad = RR.load_preregistration(REPO_ROOT)
    # Corrupt a binding_core numeric rule so the freeze digest flips.
    bad = json.loads(json.dumps(bad))
    core = bad.get("binding_core") or bad
    # flip any nested numeric we can find deterministically
    def _bump(d):
        for k, v in list(d.items()):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                d[k] = v + 1
                return True
            if isinstance(v, dict) and _bump(v):
                return True
        return False
    assert _bump(core)
    req = VS002ExecutionRequest(
        package_path=Path("outputs/pkg/vs002"),
        preregistration=bad,
        historical_transport_digest=binding.evidence_binding.package_transport_digest,
        runner_source_sha=RUNNER_SHA, generated_at=GEN_AT,
        result_filename="run1.json", base_dir=str(tmp_path),
    )
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(req)
    assert ei.value.category == "PREREGISTRATION_MISMATCH"


# ─────────────────── transport digest NEVER recomputed ──────────────────────
def test_transport_digest_recorded_as_historical_attestation(tmp_path, binding, harness):
    out = execute_frozen_vs002(make_request(tmp_path, binding))
    doc = json.loads(Path(out["written_path"]).read_text())
    ap = doc["adapter_provenance"]
    assert ap["transport_digest_recomputed"] is False
    assert ap["transport_digest_verification_mode"] == "HISTORICAL_ATTESTATION_ONLY"
    assert ap["transport_digest_historical_attestation"] == binding.evidence_binding.package_transport_digest


def test_identity_strings_reused_not_recomputed(tmp_path, binding, ident, harness):
    out = execute_frozen_vs002(make_request(tmp_path, binding))
    doc = json.loads(Path(out["written_path"]).read_text())
    eb = doc["experiment_result"]["observations"]["evidence_binding"]
    assert eb["package_id"] == ident.package_id
    assert eb["source_production_sha"] == ident.source_production_sha
    assert eb["evidence_schema_version"] == ident.evidence_schema_version


# ─────────────────── StudentTTableError is an ENGINEERING blocker ────────────
def test_student_t_table_error_is_engineering_not_scientific(tmp_path, binding, monkeypatch, harness):
    def boom(*a, **k):
        harness["run_calls"] += 1
        raise RC.StudentTTableError("df 1001 outside certified static table")
    monkeypatch.setattr(E.RR, "run", boom)
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(make_request(tmp_path, binding))
    assert ei.value.category == "UNSUPPORTED_STUDENT_T_DF"
    # never degraded into a scientific outcome, and nothing written
    assert "INCONCLUSIVE" not in str(ei.value)
    assert not (tmp_path / "vs002_result" / "run1.json").exists()


# ─────────────────────────── canonical ExperimentResult ─────────────────────
def test_canonical_experiment_result_shape(tmp_path, binding, harness):
    out = execute_frozen_vs002(make_request(tmp_path, binding))
    doc = json.loads(Path(out["written_path"]).read_text())
    er = doc["experiment_result"]
    assert er["contract_type"] == "experiment_result"
    assert er["experiment_result_id"].startswith("exr_")
    assert er["experiment_spec_id"].startswith("exs_")
    assert er["observations"]["schema_version"] == RC.RESULT_SCHEMA_VERSION
    assert doc["schema_version"] == "engineering.vs002_execution_result.v1"


def test_authority_screen_rejects_authority_keys():
    from portfolio_automation.northstar.experiments import ExperimentResult
    from portfolio_automation.northstar.provenance import Provenance, PRODUCER_SYSTEM
    spec_prov = Provenance(producer_id="t", producer_type=PRODUCER_SYSTEM,
                           recorded_at=GEN_AT, code_version="v1")
    with pytest.raises(ValueError):
        ExperimentResult(experiment_spec_id="exs_" + "0" * 32, provenance=spec_prov,
                         windows_evaluated=("7d",), observations={"approved": True})


def test_observations_carry_no_authority_keys(tmp_path, binding, harness):
    out = execute_frozen_vs002(make_request(tmp_path, binding))
    doc = json.loads(Path(out["written_path"]).read_text())
    obs_keys = set(doc["experiment_result"]["observations"].keys())
    assert "execution" not in obs_keys and "execute" not in obs_keys
    # the authority-sensitive adapter provenance lives at top level, not in obs
    assert "adapter_provenance" in doc and "adapter_provenance" not in doc["experiment_result"]["observations"]


# ─────────────────────────── provenance wrapper ─────────────────────────────
def test_adapter_provenance_fields(tmp_path, binding, harness):
    out = execute_frozen_vs002(make_request(tmp_path, binding))
    doc = json.loads(Path(out["written_path"]).read_text())
    ap = doc["adapter_provenance"]
    assert ap["adapter_id"] == "vs002_evidence.execution_adapter"
    assert ap["runner_source_sha"] == RUNNER_SHA
    assert ap["run_identity"] == out["run_identity"]
    assert ap["preregistration_freeze_digest"] == RR.EXPECTED_FREEZE_DIGEST


# ─────────────────────────── deterministic digest/identity ──────────────────
def test_deterministic_digest_and_identity(tmp_path, binding, harness, ident):
    out1 = execute_frozen_vs002(make_request(tmp_path / "a", binding))
    out2 = execute_frozen_vs002(make_request(tmp_path / "b", binding))
    assert out1["result_digest"] == out2["result_digest"]
    assert out1["run_identity"] == out2["run_identity"]


def test_result_digest_recomputes_on_reload(tmp_path, binding, harness):
    out = execute_frozen_vs002(make_request(tmp_path, binding))
    doc = json.loads(Path(out["written_path"]).read_text())
    from portfolio_automation.northstar.canonical import content_hash
    recomputed = {k: v for k, v in doc.items() if k != "result_digest"}
    assert content_hash(recomputed) == doc["result_digest"] == out["result_digest"]


# ─────────────────────────── governed write / namespace ─────────────────────
def test_result_lands_in_vs002_result_namespace(tmp_path, binding, harness):
    out = execute_frozen_vs002(make_request(tmp_path, binding))
    assert Path(out["written_path"]).parent == tmp_path / "vs002_result"


def test_path_traversal_refused(tmp_path, binding, harness):
    req = make_request(tmp_path, binding, filename="../escape.json")
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(req)
    assert ei.value.category == "INVALID_RESULT_DESTINATION"
    assert not (tmp_path / "escape.json").exists()


def test_collision_refused_no_overwrite(tmp_path, binding, harness):
    req = make_request(tmp_path, binding)
    out = execute_frozen_vs002(req)
    original = Path(out["written_path"]).read_text()
    # a second run with the same filename must refuse, leaving the original intact
    req2 = make_request(tmp_path, binding)
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(req2)
    assert ei.value.category == "RESULT_COLLISION"
    assert Path(out["written_path"]).read_text() == original


def test_preexisting_file_blocks_before_validate(tmp_path, binding, harness):
    dest = tmp_path / "vs002_result" / "run1.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("SENTINEL")
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(make_request(tmp_path, binding))
    assert ei.value.category == "RESULT_COLLISION"
    assert dest.read_text() == "SENTINEL"
    assert harness["validate_calls"] == 0  # refused before opening anything


# ─────────────────────────── determinism / no wall-clock ────────────────────
def test_naive_datetime_refused(tmp_path, binding, harness):
    req = make_request(tmp_path, binding, generated_at=datetime(2026, 10, 4, 12, 0, 0))
    with pytest.raises(VS002ExecutionError) as ei:
        execute_frozen_vs002(req)
    assert ei.value.category == "NON_DETERMINISTIC_TIMESTAMP"


# ─────────────────────────── no network ─────────────────────────────────────
def test_no_network_during_execution(tmp_path, binding, harness, monkeypatch):
    def no_sockets(*a, **k):
        raise AssertionError("adapter attempted a network socket")
    monkeypatch.setattr(socket, "socket", no_sockets)
    out = execute_frozen_vs002(make_request(tmp_path, binding))
    assert Path(out["written_path"]).exists()


# ─────────────────────────── no governance mutation ─────────────────────────
def test_only_writes_under_base_dir(tmp_path, binding, harness):
    out = execute_frozen_vs002(make_request(tmp_path, binding))
    written = Path(out["written_path"]).resolve()
    assert str(written).startswith(str(tmp_path.resolve()))
    # the only artifact created is the single result file
    created = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert created == [written]
