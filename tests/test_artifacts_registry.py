"""
Tests for portfolio_automation/artifacts_registry.py

Focus: registry self-consistency and the small lookup API.  Does not exercise
writers — registering an artifact does not require its writer to be importable
in test fixtures.  A future contract-test layer can add per-artifact
load-and-validate checks; this file establishes the baseline.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from portfolio_automation.artifacts_registry import (
    ALLOWED_FORMATS,
    Artifact,
    ArtifactNotRegistered,
    FORMAT_JSON,
    FORMAT_JSONL,
    FORMAT_MARKDOWN,
    REGISTRY,
    all_artifacts,
    artifact_path,
    artifacts_by_writer,
    artifacts_for_namespace,
    check_registry_consistency,
    find_artifact,
    get_artifact,
)
from portfolio_automation.data_governance import OutputNamespace


# ---------------------------------------------------------------------------
# Registry presence and shape
# ---------------------------------------------------------------------------

class TestRegistryPresence:
    def test_registry_is_non_empty(self):
        assert len(REGISTRY) > 0

    def test_registry_returns_tuple(self):
        assert isinstance(REGISTRY, tuple)
        assert isinstance(all_artifacts(), tuple)

    def test_pipeline_run_status_registered(self):
        art = get_artifact("pipeline_run_status")
        assert art.namespace == OutputNamespace.LATEST
        assert art.relative_path == "pipeline_run_status.json"
        assert art.format == FORMAT_JSON
        assert art.writer_module == "portfolio_automation.run_status"
        assert art.writer_function == "write_pipeline_run_status"
        assert art.observe_only_required is True
        assert art.append_only is False
        assert art.optional is False

    def test_pipeline_run_status_md_registered(self):
        art = get_artifact("pipeline_run_status_md")
        assert art.namespace == OutputNamespace.LATEST
        assert art.relative_path == "pipeline_run_status.md"
        assert art.format == FORMAT_MARKDOWN

    def test_sandbox_run_status_registered(self):
        art = get_artifact("sandbox_run_status")
        assert art.namespace == OutputNamespace.SANDBOX
        assert art.relative_path == "discovery/sandbox_run_status.json"

    def test_core_artifacts_present(self):
        """Smoke test: the most-consumed artifacts must be in the registry."""
        for name in (
            "decision_plan",
            "ai_decision_validation",
            "decision_explanations",
            "system_decision_summary",
            "data_quality_report",
            "ai_budget_summary",
            "watchlist_signals",
            "decision_outcomes",
            "decision_outcome_summary",
            "portfolio_snapshot",
            "memo_delivery_status",
            "daily_memo_txt",
            "daily_memo_md",
        ):
            assert find_artifact(name) is not None, f"core artifact missing: {name}"


# ---------------------------------------------------------------------------
# Self-consistency
# ---------------------------------------------------------------------------

class TestRegistryConsistency:
    def test_registry_passes_self_consistency(self):
        errors = check_registry_consistency()
        assert errors == [], "Registry has consistency errors:\n" + "\n".join(errors)

    def test_check_detects_duplicate_name(self):
        bad = (
            _make_art(name="dup"),
            _make_art(name="dup", relative_path="other.json"),
        )
        errors = check_registry_consistency(bad)
        assert any("duplicate name" in e for e in errors)

    def test_check_detects_duplicate_namespace_path(self):
        bad = (
            _make_art(name="a", relative_path="x.json"),
            _make_art(name="b", relative_path="x.json"),
        )
        errors = check_registry_consistency(bad)
        assert any("duplicate (namespace, relative_path)" in e for e in errors)

    def test_check_detects_absolute_path(self):
        bad = (_make_art(name="a", relative_path="/absolute/x.json"),)
        errors = check_registry_consistency(bad)
        assert any("must not start with '/'" in e for e in errors)

    def test_check_detects_bad_format(self):
        bad = (_make_art(name="a", relative_path="x.xml", format="xml"),)
        errors = check_registry_consistency(bad)
        assert any("format must be one of" in e for e in errors)

    def test_check_detects_extension_mismatch(self):
        bad = (_make_art(name="a", relative_path="x.md", format=FORMAT_JSON),)
        errors = check_registry_consistency(bad)
        assert any("must end with '.json'" in e for e in errors)

    def test_check_detects_jsonl_without_append_only(self):
        bad = (_make_art(name="a", relative_path="x.jsonl", format=FORMAT_JSONL, append_only=False),)
        errors = check_registry_consistency(bad)
        assert any("requires append_only=True" in e for e in errors)

    def test_check_detects_non_snake_case_name(self):
        bad = (_make_art(name="Has-Hyphen", relative_path="x.json"),)
        errors = check_registry_consistency(bad)
        assert any("snake_case" in e for e in errors)

    def test_check_detects_empty_writer_module(self):
        bad = (_make_art(name="a", relative_path="x.json", writer_module=""),)
        errors = check_registry_consistency(bad)
        assert any("writer_module must be non-empty" in e for e in errors)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

class TestLookups:
    def test_get_artifact_returns_entry(self):
        art = get_artifact("pipeline_run_status")
        assert isinstance(art, Artifact)
        assert art.name == "pipeline_run_status"

    def test_get_artifact_raises_for_unknown(self):
        with pytest.raises(ArtifactNotRegistered):
            get_artifact("does_not_exist")

    def test_find_artifact_returns_none_for_unknown(self):
        assert find_artifact("does_not_exist") is None

    def test_artifacts_for_namespace_latest(self):
        latest = artifacts_for_namespace(OutputNamespace.LATEST)
        names = {a.name for a in latest}
        assert "pipeline_run_status" in names
        assert "decision_plan" in names
        # SANDBOX entries should be excluded
        assert "sandbox_run_status" not in names

    def test_artifacts_for_namespace_sandbox(self):
        sandbox = artifacts_for_namespace(OutputNamespace.SANDBOX)
        names = {a.name for a in sandbox}
        assert "sandbox_run_status" in names

    def test_artifacts_for_namespace_policy(self):
        policy = artifacts_for_namespace(OutputNamespace.POLICY)
        names = {a.name for a in policy}
        assert "decision_outcomes" in names
        assert "ai_usage_events" in names

    def test_artifacts_by_writer(self):
        results = artifacts_by_writer("portfolio_automation.run_status")
        names = {a.name for a in results}
        assert names == {"pipeline_run_status", "pipeline_run_status_md"}


# ---------------------------------------------------------------------------
# artifact_path — path resolution via data_governance
# ---------------------------------------------------------------------------

class TestArtifactPath:
    def test_resolves_to_namespace_dir(self, tmp_path: Path):
        path = artifact_path("pipeline_run_status", base_dir=tmp_path)
        # outputs/latest/ under tmp_path
        assert path.parent.name == "latest"
        assert path.name == "pipeline_run_status.json"

    def test_sandbox_artifact_path(self, tmp_path: Path):
        path = artifact_path("sandbox_run_status", base_dir=tmp_path)
        # outputs/sandbox/discovery/sandbox_run_status.json
        assert path.name == "sandbox_run_status.json"
        assert path.parent.name == "discovery"
        assert path.parent.parent.name == "sandbox"

    def test_unknown_artifact_raises(self, tmp_path: Path):
        with pytest.raises(ArtifactNotRegistered):
            artifact_path("nope", base_dir=tmp_path)


# ---------------------------------------------------------------------------
# Cross-checks against existing code
# ---------------------------------------------------------------------------

class TestCrossChecks:
    """
    The registry must agree with the actual writer modules where they exist.
    These checks make the registry an enforceable contract, not just a doc.
    """

    def test_pipeline_run_status_constants_match_run_status_module(self):
        run_status = importlib.import_module("portfolio_automation.run_status")
        art_json = get_artifact("pipeline_run_status")
        art_md = get_artifact("pipeline_run_status_md")
        assert run_status.STATUS_JSON_RELATIVE == art_json.relative_path
        assert run_status.STATUS_MD_RELATIVE == art_md.relative_path

    def test_sandbox_run_status_constants_match_runner_module(self):
        runner = importlib.import_module("tools.daily_sandbox_run")
        art_json = get_artifact("sandbox_run_status")
        art_md = get_artifact("sandbox_run_status_md")
        # Runner uses paths relative to the SANDBOX namespace root, no leading
        # "discovery/" prefix duplication — match the registered relative_path.
        assert art_json.relative_path == "discovery/" + runner._STATUS_JSON_RELATIVE.split("/", 1)[-1] \
               or art_json.relative_path == runner._STATUS_JSON_RELATIVE
        assert art_md.relative_path == "discovery/" + runner._STATUS_MD_RELATIVE.split("/", 1)[-1] \
               or art_md.relative_path == runner._STATUS_MD_RELATIVE

    def test_every_writer_module_is_importable(self):
        """All declared writer_module values must point at importable Python modules."""
        seen: set[str] = set()
        errors: list[str] = []
        for art in REGISTRY:
            if art.writer_module in seen:
                continue
            seen.add(art.writer_module)
            try:
                importlib.import_module(art.writer_module)
            except Exception as exc:
                errors.append(f"{art.name}: writer_module {art.writer_module} not importable: {exc}")
        assert errors == [], "Unimportable writer modules:\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_art(
    *,
    name: str = "x",
    namespace: OutputNamespace = OutputNamespace.LATEST,
    relative_path: str = "x.json",
    format: str = FORMAT_JSON,
    writer_module: str = "portfolio_automation.run_status",
    writer_function: str | None = None,
    consumers: tuple[str, ...] = (),
    schema_version: int = 1,
    append_only: bool = False,
    optional: bool = False,
    observe_only_required: bool = False,
    documented_in: str | None = None,
    description: str = "",
) -> Artifact:
    return Artifact(
        name=name,
        namespace=namespace,
        relative_path=relative_path,
        format=format,
        writer_module=writer_module,
        writer_function=writer_function,
        consumers=consumers,
        schema_version=schema_version,
        append_only=append_only,
        optional=optional,
        observe_only_required=observe_only_required,
        documented_in=documented_in,
        description=description,
    )
