"""Regression baselines compared by identity, and refused when identity is absent.

The crashed session stored ``baseline_failures: 15``. These tests pin why that
was not enough: equal counts can hide a swap, a deleted test is not a fixed
test, and a baseline that cannot be found must never read as agreement.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.failure_identity import (
    Comparability, FailureDelta, FailureSet, RegressionStatus,
    compare_failure_sets, env_fingerprint,
)

REPO = Path(__file__).resolve().parents[1]


def _fs(nodes, *, exitstatus=1, selection=("tests",), collect_errors=(), env=None):
    return FailureSet(node_ids=tuple(nodes), exitstatus=exitstatus,
                      selection_args=tuple(selection),
                      collect_errors=tuple(collect_errors),
                      environment=env if env is not None else {"python_version": "3.12.3"})


# ── the defect the crash exposed ───────────────────────────────────────────
def test_same_count_different_nodes_is_a_new_failure():
    """Fifteen and fifteen, and still a regression."""
    baseline = _fs(["tests/t.py::test_a", "tests/t.py::test_b"])
    candidate = _fs(["tests/t.py::test_a", "tests/t.py::test_c"])
    assert baseline.count == candidate.count

    d = compare_failure_sets(baseline, candidate)
    assert d.comparability is Comparability.COMPARABLE
    assert d.newly_failing == ("tests/t.py::test_c",)
    assert d.fixed == ("tests/t.py::test_b",)
    assert d.new_relevant_failures == 1
    assert d.regression_status is RegressionStatus.NEW_FAILURES


def test_reordered_identical_nodes_compare_equal():
    baseline = _fs(["tests/t.py::test_b", "tests/t.py::test_a"])
    candidate = _fs(["tests/t.py::test_a", "tests/t.py::test_b"])

    d = compare_failure_sets(baseline, candidate)
    assert d.regression_status is RegressionStatus.NO_NEW_FAILURES
    assert d.new_relevant_failures == 0
    assert baseline.digest() == candidate.digest(), "digest is order-free"


# ── missing identity must not read as agreement ────────────────────────────
def test_missing_baseline_identity_is_unknown_not_zero():
    """The highest-value silent-pass route, closed by the type itself."""
    d = compare_failure_sets(None, _fs(["tests/t.py::test_a"]))
    assert d.comparability is Comparability.BASELINE_IDENTITY_UNAVAILABLE
    assert d.regression_status is RegressionStatus.UNKNOWN
    assert d.new_relevant_failures is None, "must not be representable as 0"


def test_new_relevant_failures_is_none_for_every_uncomparable_reason():
    cand = _fs(["tests/t.py::test_a"])
    cases = [
        compare_failure_sets(None, cand),
        compare_failure_sets(_fs([], exitstatus=2), cand),
        compare_failure_sets(_fs([], collect_errors=("tests/broken.py",)), cand),
        compare_failure_sets(_fs([]), _fs([], exitstatus=2)),
        compare_failure_sets(_fs([], selection=("tests/a",)),
                             _fs([], selection=("tests/b",))),
        compare_failure_sets(_fs([], env={"python_version": "3.11.0"}),
                             _fs([], env={"python_version": "3.12.3"})),
    ]
    for d in cases:
        assert d.new_relevant_failures is None
        assert d.regression_status is RegressionStatus.UNKNOWN


def test_collection_error_is_never_a_clean_sheet():
    """An empty failure list from a suite that could not be imported is not
    zero failures; it is no information."""
    broken = _fs([], exitstatus=2, collect_errors=("tests/test_broken.py",))
    assert broken.usable() is False
    d = compare_failure_sets(broken, _fs([]))
    assert d.comparability is Comparability.UNUSABLE_BASELINE_RUN
    assert d.new_relevant_failures is None


def test_no_tests_collected_is_not_usable():
    assert _fs([], exitstatus=5).usable() is False


# ── a deleted test is not a fixed test ─────────────────────────────────────
def test_deleted_failing_test_is_not_counted_as_fixed():
    baseline = _fs(["tests/t.py::test_gone", "tests/t.py::test_a"])
    candidate = _fs(["tests/t.py::test_a"])
    collected = frozenset({"tests/t.py::test_a"})

    d = compare_failure_sets(baseline, candidate, candidate_collected=collected)
    assert d.fixed == ()
    assert d.removed_no_longer_collected == ("tests/t.py::test_gone",)


def test_genuinely_fixed_test_is_reported_as_fixed():
    baseline = _fs(["tests/t.py::test_was_broken", "tests/t.py::test_a"])
    candidate = _fs(["tests/t.py::test_a"])
    collected = frozenset({"tests/t.py::test_a", "tests/t.py::test_was_broken"})

    d = compare_failure_sets(baseline, candidate, candidate_collected=collected)
    assert d.fixed == ("tests/t.py::test_was_broken",)
    assert d.removed_no_longer_collected == ()


# ── durable round trip: recompute with no filesystem and no pytest ─────────
def test_delta_is_recomputable_from_persisted_records_alone():
    baseline = _fs(["tests/t.py::test_a", "tests/t.py::test_b"])
    candidate = _fs(["tests/t.py::test_a", "tests/t.py::test_c"])

    frozen = json.dumps({"baseline": baseline.to_dict(),
                         "candidate": candidate.to_dict()}, sort_keys=True)

    reloaded = json.loads(frozen)
    d = compare_failure_sets(FailureSet.from_dict(reloaded["baseline"]),
                             FailureSet.from_dict(reloaded["candidate"]))
    assert d.new_relevant_failures == 1
    assert d.newly_failing == ("tests/t.py::test_c",)


def test_node_ids_survive_parametrisation_and_classes_byte_exact():
    ids = ["tests/t.py::TestK::test_inner",
           "tests/t.py::test_param[c[d]]",
           "tests/t.py::test_param[e.f]",
           "tests/t.py::test_unicode[café]"]
    reloaded = FailureSet.from_dict(_fs(ids).to_dict())
    assert set(reloaded.node_ids) == set(ids)


def test_failure_count_cannot_drift_from_the_identities():
    fs = _fs(["tests/t.py::test_a", "tests/t.py::test_b"])
    assert fs.to_dict()["failure_count"] == len(fs.to_dict()["failure_node_ids"])


# ── the capture plugin actually captures what pytest selects ───────────────
def test_plugin_captures_exact_node_ids_including_setup_errors(tmp_path):
    """End-to-end against a real pytest run, not a simulation."""
    suite = tmp_path / "test_probe.py"
    suite.write_text(
        "import pytest\n"
        "def test_ok():\n    assert True\n"
        "def test_fail():\n    assert False\n"
        "@pytest.mark.parametrize('v', ['c[d]', 'e.f'])\n"
        "def test_param(v):\n    assert False\n"
        "class TestK:\n    def test_inner(self):\n        assert False\n"
        "@pytest.fixture\ndef boom():\n    raise RuntimeError('setup')\n"
        "def test_setup_error(boom):\n    assert True\n",
        encoding="utf-8")
    out = tmp_path / "fail.json"

    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
         "-p", "ew0a_pytest_failreport", f"--ew0a-failreport={out}", str(suite)],
        cwd=tmp_path, capture_output=True,
        env={**__import__("os").environ,
             "PYTHONPATH": str(REPO / "tools") + ":" + str(REPO)})

    payload = json.loads(out.read_text(encoding="utf-8"))
    names = {n.split("::", 1)[1] for n in payload["failure_node_ids"]}
    assert "test_fail" in names
    assert "TestK::test_inner" in names
    assert "test_setup_error" in names, "a fixture error is a failing identity"
    assert "test_param[c[d]]" in names, "bracketed params survive byte-exact"
    assert "test_param[e.f]" in names
    assert "test_ok" not in names
    assert payload["pytest_exitstatus"] == 1


def test_plugin_reports_collection_errors_rather_than_zero_failures(tmp_path):
    broken = tmp_path / "test_broken.py"
    broken.write_text("import a_module_that_does_not_exist\n", encoding="utf-8")
    out = tmp_path / "fail.json"

    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
         "-p", "ew0a_pytest_failreport", f"--ew0a-failreport={out}", str(broken)],
        cwd=tmp_path, capture_output=True,
        env={**__import__("os").environ,
             "PYTHONPATH": str(REPO / "tools") + ":" + str(REPO)})

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["collect_errors"], "the import failure must be visible"
    fs = FailureSet.from_dict({**payload, "selection_args": []})
    assert fs.usable() is False
