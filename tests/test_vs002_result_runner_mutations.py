"""VS-002 result runner — mutation certification + real-package tripwire.

Proves the runner REFUSES or structurally cannot perform the scientifically
dangerous alternatives the frozen preregistration forbids, and that the whole
synthetic suite cannot reach the real frozen evidence package. All data is
fabricated; nothing here opens, discovers, or reads the real package.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_automation.vs002_evidence import contracts as C
from portfolio_automation.vs002_evidence import result_runner as RR
from portfolio_automation.vs002_evidence import result_contract as RC
from portfolio_automation.vs002_evidence.result_contract import (
    EvidenceIdentity, H1Status, H2Status, NoActionStatus,
)
from tests.test_vs002_result_runner import (
    PREREG, FROZEN_IDENTITY, GEN_AT, CohortSpec, RowSpec, build_snapshot,
    _uniform_cohorts, _run, _d,
)

REPO = Path(__file__).resolve().parent.parent
BINDING = RR.verify_preregistration(PREREG)


def _snap10():
    return build_snapshot(_uniform_cohorts(10), betas={"AAA": 1.0})


def _mutate_binding_core(path_keys, value):
    p = copy.deepcopy(PREREG)
    d = p["binding_core"]
    for k in path_keys[:-1]:
        d = d[k]
    d[path_keys[-1]] = value
    return p


# ───────────────────────── preregistration identity ─────────────────────────
def test_m01_freeze_digest_mismatch_fails_closed():
    p = copy.deepcopy(PREREG)
    p["preregistration_freeze_digest"] = "0" * 64
    with pytest.raises(RR.PreregistrationMismatch):
        RR.run(_snap10(), p, evidence_identity=FROZEN_IDENTITY, generated_at=GEN_AT)


def test_m02_preregistration_id_mismatch_fails_closed():
    p = copy.deepcopy(PREREG)
    p["preregistration_id"] = "vs002prereg_deadbeef"
    with pytest.raises(RR.PreregistrationMismatch):
        RR.run(_snap10(), p, evidence_identity=FROZEN_IDENTITY, generated_at=GEN_AT)


def _bad_identity(**over):
    base = dict(package_id=FROZEN_IDENTITY.package_id,
                package_transport_digest=FROZEN_IDENTITY.package_transport_digest,
                source_production_sha=FROZEN_IDENTITY.source_production_sha,
                evidence_schema_version=FROZEN_IDENTITY.evidence_schema_version)
    base.update(over)
    return EvidenceIdentity(**base)


def test_m03_package_id_mismatch_fails_closed():
    with pytest.raises(RR.EvidenceBindingMismatch):
        RR.run(_snap10(), PREREG, evidence_identity=_bad_identity(package_id="vs002evd_wrong"),
               generated_at=GEN_AT)


def test_m04_transport_digest_mismatch_fails_closed():
    with pytest.raises(RR.EvidenceBindingMismatch):
        RR.run(_snap10(), PREREG,
               evidence_identity=_bad_identity(package_transport_digest="f" * 64),
               generated_at=GEN_AT)


def test_m05_source_production_sha_mismatch_fails_closed():
    with pytest.raises(RR.EvidenceBindingMismatch):
        RR.run(_snap10(), PREREG,
               evidence_identity=_bad_identity(source_production_sha="deadbeef"),
               generated_at=GEN_AT)


def test_m06_evidence_schema_mismatch_fails_closed():
    with pytest.raises(RR.EvidenceBindingMismatch):
        RR.run(_snap10(), PREREG,
               evidence_identity=_bad_identity(evidence_schema_version="engineering.vs002_evidence.v2"),
               generated_at=GEN_AT)


def test_m07_observe_only_false_fails_closed():
    p = copy.deepcopy(PREREG)
    p["observe_only"] = False
    with pytest.raises(RR.PreregistrationMismatch):
        RR.run(_snap10(), p, evidence_identity=FROZEN_IDENTITY, generated_at=GEN_AT)


# ─────────── altering a frozen rule changes the digest → fail closed ─────────
def test_m08_altered_friction_fails_closed():
    p = _mutate_binding_core(["row_arithmetic", "friction_pp"], 0.2)
    with pytest.raises(RR.PreregistrationMismatch):
        RR.verify_preregistration(p)


def test_m09_altered_minimum_cohorts_fails_closed():
    p = _mutate_binding_core(["cohort_construction", "minimum_cohorts"], 5)
    with pytest.raises(RR.PreregistrationMismatch):
        RR.verify_preregistration(p)


def test_m10_altered_beta_minimum_observations_fails_closed():
    p = _mutate_binding_core(["risk_adjustment", "minimum_joint_observations"], 30)
    with pytest.raises(RR.PreregistrationMismatch):
        RR.verify_preregistration(p)


def test_m11_altered_beta_session_window_fails_closed():
    p = _mutate_binding_core(["risk_adjustment", "estimation_window_sessions"], 120)
    with pytest.raises(RR.PreregistrationMismatch):
        RR.verify_preregistration(p)


# ─────────────── forbidden SPY-join alternatives (structural) ────────────────
def test_m12_no_nearest_spy_fallback():
    # SPY exists one second away — a nearest-scan join would match; exact must not
    base = 739000
    dstr = _d(base)
    snap = build_snapshot([CohortSpec(base, 1.0, [RowSpec("AAA", 0.5, 3.5, time="10:00:00")])],
                          betas={"AAA": 1.0})
    snap.signals[:] = [s for s in snap.signals if s["ticker"] != "SPY"]
    snap.signals.append({"ticker": "SPY", "signal_time": f"{dstr}T10:00:01Z",
                         "signal_score": None, "price_at_signal": 400.0,
                         "outcome_return_7d": 1.0, "outcome_price_7d": 404.0,
                         "evaluated_at_7d": f"{dstr}T10:00:01Z",
                         "prediction_intent": "benchmark", "data_mode": "synthetic"})
    rows, excl, _ = RR._prepare_rows(snap, BINDING)
    assert rows == [] and excl["no_exact_spy_timestamp_match"] == 1


def test_m13_no_calendar_date_only_join():
    # SPY same calendar date, different clock time → calendar-date join would
    # match; exact-timestamp must exclude.
    base = 739000
    dstr = _d(base)
    snap = build_snapshot([CohortSpec(base, 1.0, [RowSpec("AAA", 0.5, 3.5, time="14:30:00")])],
                          betas={"AAA": 1.0})
    snap.signals[:] = [s for s in snap.signals if s["ticker"] != "SPY"]
    snap.signals.append({"ticker": "SPY", "signal_time": f"{dstr}T09:00:00Z",
                         "signal_score": None, "price_at_signal": 400.0,
                         "outcome_return_7d": 1.0, "outcome_price_7d": 404.0,
                         "evaluated_at_7d": f"{dstr}T09:00:00Z",
                         "prediction_intent": "benchmark", "data_mode": "synthetic"})
    rows, excl, _ = RR._prepare_rows(snap, BINDING)
    assert rows == [] and excl["no_exact_spy_timestamp_match"] == 1


def test_m14_forward_outcome_is_recorded_not_bar_derived():
    # Identical signals, DIFFERENT stock bar paths. NO_ACTION uses no bars, so it
    # must be invariant; if the forward outcome were recomputed from bars it would move.
    cohorts = _uniform_cohorts(10)
    snap_a = build_snapshot(cohorts, betas={"AAA": 1.0})
    snap_b = build_snapshot(cohorts, betas={"AAA": 3.0})  # very different stock bars
    a = _run(snap_a); b = _run(snap_b)
    assert a.no_action.to_dict() == b.no_action.to_dict()
    # and the recorded stock outcome drives net_no_action = outcome - friction
    # (first row's recorded stock_out is 3.3 → net_no_action = 3.3 - 0.1 = 3.2)
    rows, _, _ = RR._prepare_rows(snap_a, BINDING)
    assert rows[0].net_no_action_return_pct == pytest.approx(3.3 - 0.1, abs=1e-9)


# ───────────────── H2 must be per-cohort, never pooled rows ──────────────────
def test_m15_h2_is_per_cohort_mean_not_pooled_rows():
    # Per-cohort rho = +1 in every cohort (→ point IC +1 → MET), but a pooled-row
    # Spearman across all rows is negative. A pooled implementation would flip.
    base = 739000
    cohorts = []
    all_scores, all_nets = [], []
    for i in range(10):
        if i % 2 == 0:
            scores, nets = (0.1, 0.2, 0.3), (5.0, 6.0, 7.0)   # low score, high net
        else:
            scores, nets = (0.7, 0.8, 0.9), (1.0, 2.0, 3.0)   # high score, low net
        rows = [RowSpec("AAA", scores[k], nets[k] + 0.1, time=f"1{k}:00:00")
                for k in range(3)]  # spy_out=0 → net_excess = stock_out-0.1 = net
        cohorts.append(CohortSpec(base + i * 8, 0.0, rows))
        all_scores += list(scores); all_nets += list(nets)
    res = _run(build_snapshot(cohorts, betas={"AAA": 1.0}))
    assert res.h2.point_ic == pytest.approx(1.0, abs=1e-9)
    assert res.h2.status is H2Status.MET
    pooled = RR.spearman_rho(all_scores, all_nets)
    assert pooled < 0   # a pooled-row gate would have said NOT_MET


def test_m16_cohorts_equal_weighted_not_row_weighted():
    base = 739000
    big = CohortSpec(base, 0.0, [RowSpec("AAA", 0.5, 10.1)])  # net = 10
    smalls = [CohortSpec(base + i * 8, 0.0,
                         [RowSpec("AAA", s, 0.1, time=f"1{k}:00:00")
                          for k, s in enumerate([0.2, 0.5, 0.8])]) for i in range(1, 11)]
    res = _run(build_snapshot([big] + smalls, betas={"AAA": 1.0}))
    equal_weight = (10.0 + 0.0 * 10) / 11
    row_weight = (10.0 + 0.0 * 30) / (1 + 30)
    assert res.h1.experiment_mean == pytest.approx(equal_weight, abs=1e-6)
    assert res.h1.experiment_mean != pytest.approx(row_weight, abs=1e-6)


# ───────────────── interval method must be tabulated Student-t ───────────────
def test_m17_h1_interval_uses_tabulated_t_not_normal_196():
    from tests.test_vs002_result_runner import _const_cohorts
    res = _run(build_snapshot(_const_cohorts(10, 2.0, spread=0.1), betas={"AAA": 1.0}))
    iv = res.h1.interval
    assert iv.n == 10 and iv.t_critical == RC.student_t_critical(9)   # tabulated t(9), not 1.96
    assert iv.ci_low == pytest.approx(iv.mean - RC.student_t_critical(9) * iv.se, abs=1e-12)
    assert iv.t_critical != pytest.approx(1.96, abs=1e-3)


def test_m18_h2_reporting_ci_never_gates():
    # point IC > 0 but reporting CI lower <= 0 → still MET (CI is reporting only)
    base = 739000
    cohorts = []
    for i in range(12):
        if i % 2 == 0:
            sc, so = (0.2, 0.5, 0.8), (3.3, 3.5, 3.7)   # rho +1
        else:
            sc, so = (0.2, 0.5, 0.8), (3.7, 3.4, 3.6)   # rho -0.5
        rows = [RowSpec("AAA", sc[k], so[k], time=f"1{k}:00:00") for k in range(3)]
        cohorts.append(CohortSpec(base + i * 8, 1.0, rows))
    res = _run(build_snapshot(cohorts, betas={"AAA": 1.0}))
    assert res.h2.point_ic > 0
    assert res.h2.reporting_interval.ci_low <= 0
    assert res.h2.status is H2Status.MET


def test_m19_no_beta_applied_to_no_action():
    # beta != 1, spy_out != 0 → if beta were applied to NO_ACTION the number moves.
    snap = build_snapshot([CohortSpec(739000, spy_out=4.0, rows=[RowSpec("AAA", 0.5, 6.0)])],
                          betas={"AAA": 2.0})
    rows, _, _ = RR._prepare_rows(snap, BINDING)
    r = rows[0]
    assert r.net_no_action_return_pct == pytest.approx(6.0 - 0.1, abs=1e-9)   # no beta
    # the signal arm DOES use beta: expected_market = 2.0*4.0 = 8.0 ; net = 6-8-0.1
    assert r.net_risk_adjusted_excess_pct == pytest.approx(6.0 - 8.0 - 0.1, abs=1e-9)


# ───────────────────── G15 real-package access tripwire ──────────────────────
def test_m20_runner_does_no_package_discovery():
    src = (REPO / "portfolio_automation" / "vs002_evidence" / "result_runner.py").read_text()
    csrc = (REPO / "portfolio_automation" / "vs002_evidence" / "result_contract.py").read_text()
    forbidden = ("glob(", "iglob", "listdir", "os.walk", "rglob", "\"latest\"", "'latest'",
                 "requests", "urllib", "http://", "https://", "socket.")
    for token in forbidden:
        assert token not in src, f"result_runner.py must not contain {token!r}"
        assert token not in csrc, f"result_contract.py must not contain {token!r}"
    # the real package id is NEVER hardcoded for discovery — only the preregistration
    # identity (id + freeze digest) is pinned, which are not package paths.
    assert "vs002evd_" not in src and "vs002evd_" not in csrc


def test_run_never_opens_or_validates_a_package_from_disk(monkeypatch):
    import portfolio_automation.vs002_evidence.consumer as CON
    def _boom(*a, **k):
        raise RuntimeError("real-package validation/discovery must never be called by run()")
    monkeypatch.setattr(CON, "validate", _boom, raising=True)
    # run() still succeeds — it consumes the in-memory snapshot, never the disk.
    res = _run(_snap10())
    assert res.preregistration_verified is True


def test_run_makes_no_network_connection(monkeypatch):
    import socket
    def _no_net(*a, **k):
        raise AssertionError("the runner must make no network connection")
    monkeypatch.setattr(socket, "socket", _no_net, raising=True)
    res = _run(_snap10())
    assert res.criterion_outcome is not None


def test_importing_runner_does_no_module_level_io():
    # Static proof that importing either module performs NO IO: no module-level
    # statement calls open/read/load/network/discovery. All such calls live only
    # inside function bodies (e.g. load_preregistration), never at import time.
    import ast
    io_names = {"open", "load", "loads", "read", "read_text", "read_bytes",
                "get", "post", "urlopen", "connect", "socket", "glob", "iglob",
                "listdir", "walk", "rglob", "validate"}
    for mod in ("result_runner.py", "result_contract.py"):
        src = (REPO / "portfolio_automation" / "vs002_evidence" / mod).read_text()
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Import, ast.ImportFrom)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    name = (f.attr if isinstance(f, ast.Attribute)
                            else f.id if isinstance(f, ast.Name) else "")
                    assert name not in io_names, f"{mod} does IO at module scope: {name}"
