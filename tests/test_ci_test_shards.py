"""Static invariants of the CI sharding contract (scripts/ci_test_shards.py).

The load-bearing proof -- collected node IDs of the shard union == the official
command's collected node IDs -- is `python scripts/ci_test_shards.py verify`
(it runs pytest --collect-only, so it lives in the CI governance job rather
than inside a test). These tests pin everything that can drift statically:
file membership, disjointness, the catch-all, the shared deselect list, the
workflow matrix, and marker registration.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "northstar-ci.yml"
PYTEST_INI = REPO / "pytest.ini"


_MODULE = None


def _load():
    """Load the helper once per test session: its universe functions run pytest
    collection subprocesses (cached inside the module), so sharing one module
    object keeps this file fast."""
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location("ci_test_shards", REPO / "scripts" / "ci_test_shards.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULE = mod
    return _MODULE


def test_every_official_test_file_belongs_to_exactly_one_shard():
    m = _load()
    files = m.official_test_files()
    assert files, "no test files found"
    shards = m.shard_files()
    seen: dict[str, str] = {}
    for name, members in shards.items():
        for f in members:
            assert f not in seen, f"{f} in both {seen[f]} and {name}"
            seen[f] = name
    assert set(seen) == set(files)
    assert Path(__file__).relative_to(REPO).as_posix() in seen


def test_catch_all_shard_prevents_silent_drops(tmp_path, monkeypatch):
    """A brand-new test file with an unknown prefix lands in `core`, never nowhere."""
    m = _load()
    assert m.shard_of("tests/test_zzz_totally_new_subsystem.py") == "core"
    assert m.shard_of("tests/some_new_dir/test_thing.py") == "core"
    assert m.shard_of("test_demo.py") == "core"            # repository root
    assert m.shard_of("tools/smoke_test.py") == "core"     # out of tree
    assert m.SHARD_NAMES[-1] == "core"


def test_universe_is_repository_root_discovery_not_only_tests_dir():
    """The official command has no path operand, so pytest discovers from the
    repository root; files outside tests/ must be in the universe (Codex P1)."""
    m = _load()
    files = m.official_test_files()
    outside = [f for f in files if not f.startswith("tests/")]
    assert outside, "expected root-level / tools test files in the universe"
    for f in outside:
        assert m.shard_of(f) == "core", f


def test_official_ignores_are_not_sharded_and_deselects_point_at_real_files():
    m = _load()
    files = set(m.official_test_files())
    for ign in m.OFFICIAL_IGNORES:
        assert ign not in files
        assert (REPO / ign).exists(), ign
    for node in m.CI_DESELECT:
        path, _, _ = node.partition("::")
        assert (REPO / path).exists(), node
        assert path in files, node


def test_no_shard_is_empty():
    m = _load()
    for name, members in m.shard_files().items():
        assert members, f"shard {name} is empty; adjust SHARD_RULES"


def test_workflow_matrix_matches_shard_names_and_uses_the_shared_args():
    text = WORKFLOW.read_text(encoding="utf-8")
    m = _load()
    matrix = re.search(r"shard:\s*\[([^\]]+)\]", text)
    assert matrix, "no shard matrix in workflow"
    names = [x.strip().strip('"').strip("'") for x in matrix.group(1).split(",")]
    assert tuple(names) == m.SHARD_NAMES, (names, m.SHARD_NAMES)
    assert "ci_test_shards.py pytest-args" in text
    assert "ci_test_shards.py verify" in text
    assert "ci_test_shards.py serial-count" in text
    assert "requirements-dev.txt" in text
    # the exact-head gate never infers from a previous run (checked on the
    # executable lines only; comments may name the forbidden flags to explain why)
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for forbidden in ("--lf", "--ff", "testmon", "--last-failed", "--cache"):
        assert forbidden not in code, forbidden
    assert "-p no:cacheprovider" in code


def test_serial_marker_is_registered_and_xdist_is_a_dev_dependency_only():
    ini = PYTEST_INI.read_text(encoding="utf-8")
    assert re.search(r"^\s*serial:", ini, re.M)
    directives = [ln for ln in ini.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any(ln.lstrip().startswith("addopts") for ln in directives)  # plain `python -m pytest -q` is unchanged
    dev = (REPO / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "pytest-xdist" in dev
    runtime = (REPO / "requirements.txt").read_text(encoding="utf-8")
    assert "xdist" not in runtime
