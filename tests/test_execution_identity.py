"""Execution identity: which configuration produced this decision?

The point is not to collect metadata. It is that a future audit can group
decisions by the configuration that produced them, and can tell the difference
between "we know this attribute" and "we never captured it". These tests are
therefore mostly about refusing to invent things.
"""
from __future__ import annotations

import json

import pytest

from portfolio_automation.engineer_worker.execution_identity import (
    EXECUTION_IDENTITY_SCHEMA_VERSION, LEGACY_UNATTRIBUTED, UNAVAILABLE,
    ExecutionIdentity, build_execution_identity, safe_toolset_identity,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


def _identity(**over):
    base = dict(worker_id="engineer.local_qwen2_5_7b", worker_role="engineer",
                authority_level="A1_ASSISTED_ENGINEERING",
                model_provider="ollama", model_name="qwen2.5:7b",
                prompt_version="ew0a.1", toolset="engineer.tools",
                repository_sha=SHA_A, candidate_sha=SHA_A,
                task_id="t1", mission_id="m1", input_id="pkt_1")
    base.update(over)
    return build_execution_identity(**base)


# ── AC11: deterministic identity, and what changes it ──────────────────────
def test_identical_material_yields_identical_execution_id():
    assert _identity().execution_id() == _identity().execution_id()


def test_recorded_at_is_metadata_and_never_changes_identity():
    """The load-bearing determinism property. If the timestamp participated,
    every replay would mint a new identity and nothing could be grouped."""
    a = _identity(recorded_at="2026-08-17T10:00:00+00:00")
    b = _identity(recorded_at="2026-08-17T23:59:59+00:00")
    assert a.execution_id() == b.execution_id()
    assert a.to_dict()["recorded_at"] != b.to_dict()["recorded_at"]


@pytest.mark.parametrize("field,value", [
    ("worker_id", "engineer.other"),
    ("model_name", "qwen2.5:14b"),
    ("model_provider", "openai"),
    ("model_version", "2026-08-01"),
    ("prompt_version", "ew0a.2"),
    ("authority_level", "A2"),
    ("candidate_sha", SHA_B),
    ("repository_sha", SHA_B),
    ("task_id", "t2"),
    ("mission_id", "m2"),
    ("input_id", "pkt_2"),
])
def test_changing_any_material_attribute_changes_the_identity(field, value):
    """NEGATIVE CONTROL against an id that ignores its inputs."""
    assert _identity().execution_id() != _identity(**{field: value}).execution_id()


def test_different_toolsets_remain_distinguishable():
    assert _identity(toolset="engineer.tools").execution_id() != \
        _identity(toolset="engineer.tools.v2").execution_id()


def test_different_tool_config_changes_the_digest_and_the_identity():
    a = _identity(tool_config={"timeout": 30})
    b = _identity(tool_config={"timeout": 60})
    assert a.toolset_digest != b.toolset_digest
    assert a.execution_id() != b.execution_id()


def test_identity_is_insensitive_to_dict_ordering():
    a = build_execution_identity(worker_id="w", model_name="m", prompt_version="p",
                                 toolset="t", tool_config={"mode": "x", "timeout": 5})
    b = build_execution_identity(prompt_version="p", toolset="t", model_name="m",
                                 worker_id="w", tool_config={"timeout": 5, "mode": "x"})
    assert a.execution_id() == b.execution_id()


def test_the_identity_field_list_is_explicit():
    """A field cannot silently join or leave identity."""
    assert "recorded_at" not in ExecutionIdentity.IDENTITY_FIELDS
    assert "schema_version" not in ExecutionIdentity.IDENTITY_FIELDS
    for required in ("worker_id", "model_provider", "model_name", "prompt_version",
                     "toolset_id", "candidate_sha", "authority_level"):
        assert required in ExecutionIdentity.IDENTITY_FIELDS


# ── AC3: honest unknowns ───────────────────────────────────────────────────
def test_missing_model_version_stays_unavailable_and_is_never_the_model_name():
    """The specific fabrication this prevents: a provider that does not expose
    a build id must not have the configured NAME recorded as its version."""
    ident = _identity(model_name="gpt-4o")
    assert ident.model_version == UNAVAILABLE
    assert ident.model_version != ident.model_name
    assert "model_version" in ident.unavailable_attributes()


def test_the_unavailable_sentinel_cannot_be_mistaken_for_a_real_identifier():
    assert UNAVAILABLE == "UNAVAILABLE_AT_RECORD_TIME"
    for plausible in ("legacy", "unknown", "default", "latest", "v1", ""):
        assert UNAVAILABLE != plausible


def test_an_unsupplied_attribute_defaults_to_unavailable_not_to_a_guess():
    bare = build_execution_identity()
    assert set(bare.unavailable_attributes()) == set(ExecutionIdentity.IDENTITY_FIELDS)
    assert bare.is_fully_attributed is False


def test_a_fully_attributed_record_reports_itself_as_such():
    full = _identity(model_version="2026-08-01", instruction_version="one-shot",
                     branch="main")
    assert full.unavailable_attributes() == ()
    assert full.is_fully_attributed is True


# ── AC4/AC5: backward compatibility and schema versioning ──────────────────
def test_a_legacy_record_loads_without_manufacturing_metadata():
    """Records written before this schema must stay visibly unattributed.
    Inferring today's configuration would attribute a past decision to a
    configuration that may never have produced it."""
    legacy = ExecutionIdentity.from_dict({"recorded_at": "2026-01-01T00:00:00+00:00"})
    assert legacy.schema_version == LEGACY_UNATTRIBUTED
    assert legacy.worker_id == LEGACY_UNATTRIBUTED
    assert legacy.model_name == UNAVAILABLE
    assert legacy.prompt_version == UNAVAILABLE
    assert legacy.recorded_at == "2026-01-01T00:00:00+00:00"


def test_an_absent_identity_block_is_legacy_not_an_error():
    for empty in (None, {}):
        assert ExecutionIdentity.from_dict(empty).schema_version == LEGACY_UNATTRIBUTED


def test_current_records_carry_the_schema_version():
    assert _identity().to_dict()["schema_version"] == EXECUTION_IDENTITY_SCHEMA_VERSION


def test_an_unknown_future_schema_fails_loudly_rather_than_being_misread():
    """A silent misreading would attribute records to configurations that never
    produced them -- worse than refusing to load."""
    with pytest.raises(ValueError) as exc:
        ExecutionIdentity.from_dict({"schema_version": "engineering.execution_identity.v99"})
    assert "v99" in str(exc.value)


def test_round_trip_preserves_identity_without_semantic_drift():
    original = _identity(model_version="2026-08-01", branch="main")
    reloaded = ExecutionIdentity.from_dict(json.loads(json.dumps(original.to_dict())))
    assert reloaded.execution_id() == original.execution_id()
    assert reloaded.identity_material() == original.identity_material()


# ── AC6: secrets never enter identity ──────────────────────────────────────
@pytest.mark.parametrize("key,value", [
    ("api_key", "sk-" + "a" * 32),
    ("token", "ghp_" + "b" * 36),
    ("authorization", "Bearer " + "c" * 40),
    ("key_file", "/home/pesan/.ew0a_openai_key"),
    ("password", "hunter2"),
    ("client_secret", "d" * 40),
])
def test_credential_shaped_config_never_reaches_the_identity_record(key, value):
    """NEGATIVE CONTROL. Identity is durable, replicated and append-only, so a
    leak here is permanent."""
    ident = _identity(tool_config={"timeout": 30, key: value})
    blob = json.dumps(ident.to_dict())
    assert value not in blob
    assert key not in ident.toolset_safe_config
    assert key in safe_toolset_identity("t", {key: value})["dropped_keys"]


def test_a_secret_smuggled_under_an_allowlisted_key_is_still_dropped():
    """SUPERSEDED SEMANTICS, retained as a case rather than as the proof.

    This originally passed only because the word "Bearer" was recognised, which
    was the weakness: it proved a word filter caught a word, not that the field
    was safe. It now passes structurally -- the value is not a lowercase token
    -- and the real proof lives in the bare-credential tests below."""
    safe = safe_toolset_identity("t", {"mode": "Bearer sk-abc123secrettoken"})
    assert "mode" not in safe["safe_config"]
    assert "mode" in safe["dropped_keys"]


# ── STRUCTURAL VALIDATION: bare credential shapes carry no giveaway word ───
@pytest.mark.parametrize("field,value", [
    ("mode", "sk-" + "A1b2C3d4E5f6G7h8"),
    ("protocol", "ghp_" + "z" * 30),
    ("toolset_version", "AKIA" + "Q" * 16),
    ("toolset", "ghp_" + "z" * 30),
    # Deliberately NOT a real vendor prefix. The structural contract is the
    # defence, so the test should not depend on recognising a provider's
    # format -- and a realistic vendor-shaped literal trips push protection,
    # which is the scanner correctly refusing to carry credential-shaped text.
    ("mode", "Zq7-" + "Xy9Kp2Lm4Nb6" + "-Rt8Vw"),
    ("protocol", "A" * 20),
])
def test_a_bare_credential_shaped_value_cannot_enter_safe_config(field, value):
    """THE REPAIR. None of these contain key/token/secret/password/bearer, so a
    substring filter accepts every one of them. The field's structural contract
    is what rejects them: uppercase, underscores and opaque shapes are not
    lowercase hyphenated tokens or dotted identifiers."""
    safe = safe_toolset_identity("t", {field: value})
    assert field not in safe["safe_config"]
    assert field in safe["dropped_keys"]
    assert value not in json.dumps(safe)


@pytest.mark.parametrize("value", [
    "https://user:password@example.com",
    "https://example.com?token=fake",
    "https://example.com/#secret",
    "user:pw@example.com",
    "https://example.com/v1/secret-path",
    "sk-" + "A1b2C3d4",
])
def test_unsafe_url_material_cannot_survive_through_api_base_host(value):
    """POLICY: reject outright, never strip. A URL carrying userinfo, a query or
    a fragment is evidence the caller is passing secret-bearing material here;
    keeping just the hostname would discard that signal."""
    safe = safe_toolset_identity("t", {"api_base_host": value})
    assert "api_base_host" not in safe["safe_config"]
    assert value not in json.dumps(safe)


@pytest.mark.parametrize("field,value", [
    ("timeout", "sk-fake"),
    ("timeout", "60"),
    ("timeout", True),
    ("timeout", 0),
    ("timeout", 999_999),
    ("max_tokens", {"secret": "x"}),
    ("max_tokens", 3.5),
    ("mode", ["one-shot"]),
    ("protocol", {"p": "one-shot"}),
])
def test_wrong_typed_values_cannot_enter_safe_config(field, value):
    """`timeout` accepting a string at all was the plainest evidence there was
    no type contract. bool is rejected explicitly: it is an int subclass, so
    True would otherwise masquerade as 1."""
    safe = safe_toolset_identity("t", {field: value})
    assert field not in safe["safe_config"]


def test_unknown_keys_remain_fail_closed():
    safe = safe_toolset_identity("t", {"endpoint": "https://x", "region": "us",
                                       "api_key": "sk-fake"})
    assert safe["safe_config"] == {}
    assert set(safe["dropped_keys"]) == {"endpoint", "region", "api_key"}


# ── POSITIVE CONTROLS: the repair must not become "drop everything" ────────
def test_legitimate_configuration_is_still_represented():
    safe = safe_toolset_identity("gpt_supervisor.review", {
        "toolset": "gpt_supervisor.review", "toolset_version": "1.2.3",
        "protocol": "one-shot", "mode": "strict", "timeout": 60,
        "max_tokens": 4096, "max_completion_tokens": 2000,
        "api_base_host": "api.openai.com"})
    assert safe["dropped_keys"] == []
    assert safe["safe_config"]["protocol"] == "one-shot"
    assert safe["safe_config"]["timeout"] == 60
    assert safe["safe_config"]["max_completion_tokens"] == 2000
    assert safe["safe_config"]["api_base_host"] == "api.openai.com"


def test_token_named_fields_are_not_dropped_for_their_names():
    """Caught by a positive control during the repair: an over-eager key-name
    screen dropped max_tokens and max_completion_tokens because their NAMES
    contain "token". The allowlist decides keys; screening applies to values."""
    safe = safe_toolset_identity("t", {"max_tokens": 4096,
                                       "max_completion_tokens": 2000})
    assert safe["safe_config"] == {"max_tokens": 4096, "max_completion_tokens": 2000}


@pytest.mark.parametrize("value,expected", [
    ("api.openai.com", "api.openai.com"),
    ("https://api.openai.com", "api.openai.com"),
    ("https://api.openai.com/", "api.openai.com"),
    ("localhost:11434", "localhost:11434"),
    ("API.OpenAI.com", "api.openai.com"),
])
def test_a_clean_host_is_accepted_and_normalised(value, expected):
    safe = safe_toolset_identity("t", {"api_base_host": value})
    assert safe["safe_config"]["api_base_host"] == expected


@pytest.mark.parametrize("value", ["v1", "1.2.3", "one-shot", "2"])
def test_legitimate_versions_are_accepted(value):
    assert safe_toolset_identity("t", {"toolset_version": value})["safe_config"]


def test_an_over_long_value_is_dropped_rather_than_embedded():
    safe = safe_toolset_identity("t", {"mode": "x" * 500})
    assert "mode" in safe["dropped_keys"]


# ── PRODUCTION PATH: a real certification record carries the envelope ──────
# A helper that passes unit tests but no producer uses would not satisfy this
# mission. These exercise dispatch_durably -- the mandatory certification path
# the operating loop is forced through.

def _fake_supervisor(packet):
    from portfolio_automation.engineer_worker.gpt_supervisor import (
        SupervisorDecision, SupervisorVerdict)
    return SupervisorDecision(verdict=SupervisorVerdict.PASS, reasons=["ok"])


def test_a_real_dispatched_review_record_carries_execution_identity(
        durable_ctx, stationary_binding):
    from portfolio_automation.engineer_worker.durable_certification import (
        dispatch_durably)
    from portfolio_automation.engineer_worker.review_journal import (
        LifecycleKind, read_events_strict)

    out = dispatch_durably(
        {"task": {"task_id": "t1"}, "requirements": [], "acceptance_criteria": ["ac"],
         "diff": "", "tests_run": [], "test_results": {}, "changed_files": []},
        _fake_supervisor, context=durable_ctx, candidate_sha=SHA_A,
        attempt_id="a1", task_id="t1", acceptance_criteria=["ac"],
        candidate_binding=stationary_binding)
    assert out.dispatched is True

    events, intact = read_events_strict(durable_ctx.journal.path)
    assert intact
    called = [e for e in events if e["kind"] == LifecycleKind.REVIEWER_CALLED.value]
    assert called, "the write-ahead record must exist"
    ident = called[0]["execution_identity"]

    # AC2: the record answers who/what/under-what, from the real producer.
    assert ident["schema_version"] == EXECUTION_IDENTITY_SCHEMA_VERSION
    assert ident["execution_id"].startswith("exid_")
    assert ident["worker_role"] == "independent_reviewer"
    assert ident["model_name"] == "stub"          # from the reviewer identity
    assert ident["candidate_sha"] == SHA_A
    assert ident["task_id"] == "t1"
    assert ident["mission_id"] == durable_ctx.mission_id
    assert ident["input_id"] == out.packet_hash   # bound to the reviewed packet
    # AC3: the served build id is not exposed by the API, so it stays unavailable
    assert ident["model_version"] == UNAVAILABLE
    assert "model_version" in ident["unavailable_attributes"]


def test_every_lifecycle_record_naming_the_review_agrees_on_the_identity(
        durable_ctx, stationary_binding):
    """One execution, one identity. Divergence between records would make the
    attribution useless for grouping."""
    from portfolio_automation.engineer_worker.durable_certification import (
        dispatch_durably)
    from portfolio_automation.engineer_worker.review_journal import read_events_strict

    dispatch_durably(
        {"task": {"task_id": "t1"}, "acceptance_criteria": ["ac"]},
        _fake_supervisor, context=durable_ctx, candidate_sha=SHA_A,
        attempt_id="a1", task_id="t1", acceptance_criteria=["ac"],
        candidate_binding=stationary_binding)

    events, _ = read_events_strict(durable_ctx.journal.path)
    carrying = [e for e in events if "execution_identity" in e]
    ids = {e["execution_identity"]["execution_id"] for e in carrying}
    assert len(ids) == 1, f"one execution must have one identity, got {ids}"
    assert len(carrying) >= 3, "packet built, reviewer called, verdict persisted"


def test_the_production_record_leaks_no_credential(durable_ctx, stationary_binding):
    """In real operation the reviewer identity carries a key_file path. It must
    not survive into the durable record."""
    from portfolio_automation.engineer_worker.durable_certification import (
        ReviewContext, dispatch_durably)

    ctx = ReviewContext.open(
        durable_ctx.store.repo_root, mission_id="m1", session_id="s1",
        reviewer_identity={"provider": "openai", "model": "gpt-4o",
                           "protocol": "one-shot",
                           "key_file": "/home/pesan/.ew0a_openai_key",
                           "api_key": "sk-" + "z" * 32},
        repo=stationary_binding.repo, candidate_binding=stationary_binding)
    dispatch_durably({"task": {"task_id": "t1"}, "acceptance_criteria": ["ac"]},
                     _fake_supervisor, context=ctx, candidate_sha=SHA_A,
                     attempt_id="a1", task_id="t1", acceptance_criteria=["ac"],
                     candidate_binding=stationary_binding)

    blob = ctx.journal.path.read_text(encoding="utf-8")
    assert "sk-" + "z" * 32 not in blob
    assert ".ew0a_openai_key" not in blob


def test_a_different_candidate_sha_yields_a_different_execution_identity(durable_ctx):
    """AC7. The reviewed candidate is material: a verdict must not appear to
    cover a tree the reviewer never saw."""
    a = durable_ctx.execution_identity(candidate_sha=SHA_A, task_id="t1")
    b = durable_ctx.execution_identity(candidate_sha=SHA_B, task_id="t1")
    assert a.execution_id() != b.execution_id()
    assert a.candidate_sha != b.candidate_sha
