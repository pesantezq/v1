"""schwab_sync v2: auth authority -> canonical evidence -> gateway -> LKG model
-> compatibility projections. Fixture-driven; no live credential."""
from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_automation.brokers import broker_evidence_store as ES
from portfolio_automation.brokers import schwab_sync as sync
from portfolio_automation.brokers.schwab_auth_manager import AuthResult, AuthState

FIX = Path("tests/fixtures/schwab")
TS = "2026-09-20T12:00:00+00:00"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "cid")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "csec")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/cb")
    monkeypatch.delenv("SCHWAB_READ_ONLY_MODE", raising=False)


def _fake_client(monkeypatch, raw=None, nums=None, fail=None):
    import portfolio_automation.brokers.schwab_client as cl
    raw = json.loads((FIX / "accounts_positions.json").read_text()) if raw is None else raw
    nums = json.loads((FIX / "account_numbers.json").read_text()) if nums is None else nums

    def get_nums(self):
        if fail:
            raise fail
        return nums
    monkeypatch.setattr(cl.SchwabClient, "get_account_numbers", get_nums)
    monkeypatch.setattr(cl.SchwabClient, "get_accounts", lambda self, **kw: raw)


def _auth(monkeypatch, state=AuthState.OK, token="TOK", detail="", telemetry=None):
    monkeypatch.setattr(sync, "_acquire_auth",
                        lambda: AuthResult(state=state, access_token=token, detail=detail,
                                           telemetry=telemetry or {"refresh_token_rotated": False}))


def _latest(root, name):
    return json.loads((root / "outputs/latest" / name).read_text())


def test_successful_sync_admits_evidence_and_projects_compat_files(tmp_path, configured, monkeypatch):
    _auth(monkeypatch); _fake_client(monkeypatch)
    st = sync.run_sync(root=tmp_path, now=TS)
    assert st["overall_status"] == "ok" and st["auth_state"] == "OK"
    assert st["evidence_truth_status"] == "CURRENT" and st["evidence_snapshot_id"].startswith("evs_")
    admitted = _latest(tmp_path, ES.LATEST_ADMITTED_FILE)
    attempt = _latest(tmp_path, ES.LATEST_ATTEMPT_FILE)
    assert attempt["outcome"] == "ADMITTED" and attempt["snapshot_id"] == admitted["snapshot_id"]
    snap = _latest(tmp_path, "schwab_portfolio_snapshot.json")
    pos = _latest(tmp_path, "schwab_positions.json")
    assert snap["evidence_snapshot_id"] == admitted["snapshot_id"] == pos["evidence_snapshot_id"]
    assert snap["projection_of"] == ES.LATEST_ADMITTED_FILE
    assert pos["positions"] and all(p["account_ref_masked"].startswith("…") for p in pos["positions"])
    assert st["position_count"] == len(pos["positions"]) and st["account_count"] == len(snap["accounts"])
    # archive write-once
    day = TS[:10]
    assert (tmp_path / "outputs/archive/broker_evidence" / day / f"{admitted['snapshot_id']}.json").exists()
    # lifecycle telemetry present and secret-free
    life = _latest(tmp_path, sync.TOKEN_LIFECYCLE_FILE)
    assert life["auth_state"] == "OK" and life["access_token_present"] is True
    assert "TOK" not in json.dumps(life) and "access_token\"" not in json.dumps(life).replace("access_token_present", "")


def test_legacy_projection_shape_is_preserved_for_consumers(tmp_path, configured, monkeypatch):
    _auth(monkeypatch); _fake_client(monkeypatch)
    sync.run_sync(root=tmp_path, now=TS)
    snap = _latest(tmp_path, "schwab_portfolio_snapshot.json")
    pos = _latest(tmp_path, "schwab_positions.json")
    for k in ("generated_at", "observe_only", "source", "snapshot_timestamp", "accounts", "totals"):
        assert k in snap
    for k in ("account_id_masked", "account_type", "total_market_value", "cash", "positions_count"):
        assert k in snap["accounts"][0]
    for k in ("symbol", "quantity", "market_value", "average_cost", "asset_type", "account_ref_masked", "source_timestamp"):
        assert k in pos["positions"][0]
    # holdings_resolver still resolves from the projections
    from portfolio_automation import holdings_resolver as hr
    (tmp_path / "config.json").write_text(json.dumps({"portfolio": {"broker_aware": {"enabled": True}, "holdings": [], "cash_available": 0}}))
    from datetime import datetime, timezone
    res = hr.resolve_holdings(tmp_path, prefer_broker=True, now=datetime.fromisoformat(TS))
    assert res["holdings_source"] == "broker" and res["holdings"]


def test_reauth_required_writes_attempt_and_status_but_never_empties_truth(tmp_path, configured, monkeypatch):
    _auth(monkeypatch); _fake_client(monkeypatch)
    sync.run_sync(root=tmp_path, now=TS)                              # establish LKG
    before_pos = (tmp_path / "outputs/latest/schwab_positions.json").read_bytes()
    before_adm = (tmp_path / "outputs/latest" / ES.LATEST_ADMITTED_FILE).read_bytes()
    _auth(monkeypatch, state=AuthState.REAUTH_REQUIRED, token=None, detail="no usable token refresh_token=LEAK")
    st = sync.run_sync(root=tmp_path, now="2026-09-21T12:00:00+00:00")
    assert st["overall_status"] == "error" and st["auth_state"] == "REAUTH_REQUIRED"
    assert "LEAK" not in json.dumps(st)
    assert st["evidence_truth_status"] == "STALE_LAST_KNOWN_GOOD"
    assert st["position_count"] == 0 and st["account_count"] == 0     # counts describe THIS attempt
    assert (tmp_path / "outputs/latest/schwab_positions.json").read_bytes() == before_pos   # projections untouched
    assert (tmp_path / "outputs/latest" / ES.LATEST_ADMITTED_FILE).read_bytes() == before_adm
    attempt = _latest(tmp_path, ES.LATEST_ATTEMPT_FILE)
    assert attempt["outcome"] == "REAUTH_REQUIRED" and attempt["implies_empty_portfolio"] is False
    assert ES.read_state(tmp_path).positions()                        # LKG still readable


def test_transient_auth_failure_is_not_reported_as_reauth(tmp_path, configured, monkeypatch):
    _auth(monkeypatch, state=AuthState.SCHWAB_UNAVAILABLE, token=None, detail="token endpoint 503")
    st = sync.run_sync(root=tmp_path, now=TS)
    assert st["auth_state"] == "SCHWAB_UNAVAILABLE" and "transient" in st["last_error"]
    assert _latest(tmp_path, ES.LATEST_ATTEMPT_FILE)["outcome"] == "AUTH_UNAVAILABLE"
    assert not (tmp_path / "outputs/latest/schwab_positions.json").exists()


def test_acquisition_failure_leaves_prior_truth_and_redacts(tmp_path, configured, monkeypatch):
    _auth(monkeypatch); _fake_client(monkeypatch)
    sync.run_sync(root=tmp_path, now=TS)
    _fake_client(monkeypatch, fail=RuntimeError("GET /accounts -> 500 Authorization: Bearer LEAKTOKEN"))
    st = sync.run_sync(root=tmp_path, now="2026-09-21T12:00:00+00:00")
    assert st["overall_status"] == "error" and st["last_error"].startswith("ACQUISITION_FAILED")
    assert "LEAKTOKEN" not in json.dumps(st) and "LEAKTOKEN" not in json.dumps(_latest(tmp_path, ES.LATEST_ATTEMPT_FILE))
    assert st["evidence_truth_status"] == "STALE_LAST_KNOWN_GOOD"


@pytest.mark.parametrize("raw,nums,outcome", [
    ({"error": "not a list"}, [], "SCHEMA_DRIFT"),
    ([], [], "NORMALIZATION_FAILED"),                       # empty response != empty portfolio
    ([{"securitiesAccount": "garbage"}], [], "NORMALIZATION_FAILED"),
])
def test_malformed_or_empty_response_is_not_an_empty_portfolio(tmp_path, configured, monkeypatch, raw, nums, outcome):
    _auth(monkeypatch); _fake_client(monkeypatch, raw=raw, nums=nums)
    st = sync.run_sync(root=tmp_path, now=TS)
    assert st["overall_status"] == "error"
    assert _latest(tmp_path, ES.LATEST_ATTEMPT_FILE)["outcome"] == outcome
    assert not (tmp_path / "outputs/latest/schwab_positions.json").exists()
    assert ES.read_state(tmp_path).positions() is None


def test_refused_evidence_is_not_projected_into_truth(tmp_path, configured, monkeypatch):
    _auth(monkeypatch); _fake_client(monkeypatch)
    from portfolio_automation.brokers import schwab_evidence_adapter as AD
    from portfolio_automation.evidence_gateway.admission import AdmissionDecision, AdmissionReason
    monkeypatch.setattr(sync.AD, "admit_broker_snapshot",
                        lambda evs, as_of, ref=None: AdmissionDecision(admitted=False, reason=AdmissionReason.PAYLOAD_HASH_MISMATCH, detail="tampered"))
    st = sync.run_sync(root=tmp_path, now=TS)
    assert st["overall_status"] == "error" and st["last_error"].startswith("EVIDENCE_REFUSED")
    assert _latest(tmp_path, ES.LATEST_ATTEMPT_FILE)["outcome"] == "EVIDENCE_REFUSED"
    assert not (tmp_path / "outputs/latest" / ES.LATEST_ADMITTED_FILE).exists()
    assert not (tmp_path / "outputs/latest/schwab_positions.json").exists()


def test_unconfigured_records_attempt_and_stays_inert(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    called = []
    monkeypatch.setattr(sync, "_acquire_auth", lambda: called.append(1))
    st = sync.run_sync(root=tmp_path, now=TS)
    assert st["overall_status"] == "unconfigured" and called == []
    assert _latest(tmp_path, ES.LATEST_ATTEMPT_FILE)["outcome"] == "UNCONFIGURED"


def test_no_written_artifact_carries_a_secret(tmp_path, configured, monkeypatch):
    _auth(monkeypatch, telemetry={"refresh_token_rotated": True, "refresh_token_fingerprint": "abcd1234abcd1234"})
    _fake_client(monkeypatch)
    sync.run_sync(root=tmp_path, now=TS)
    blob = "".join(p.read_text() for p in (tmp_path / "outputs/latest").glob("*.json"))
    nums = json.loads((FIX / "account_numbers.json").read_text())
    for leak in ("TOK", "csec", '"access_token"', '"refresh_token"', "client_secret", *[str(n["accountNumber"]) for n in nums]):
        assert leak not in blob, leak


def test_cli_status_prints_auth_and_evidence_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    orig = sync.run_status
    monkeypatch.setattr(sync, "run_status", lambda **kw: orig(root=tmp_path, **kw))
    assert sync.main(["--status"]) == 0
    out = capsys.readouterr().out
    assert "READ-ONLY MODE ACTIVE" in out and "auth_state=" in out and "evidence=" in out


def test_archive_day_uses_injected_time_not_wall_clock(tmp_path, configured, monkeypatch):
    """Regression: the write-once broker-evidence archive day (and admitted_at /
    generated_at) must come from the injected sync/evidence instant, NOT the
    wall clock at persist time. Proven by pinning the wall clock to a far date
    and requiring the archive to still land under the injected 2026-09-20."""
    import datetime as _dt
    _auth(monkeypatch)
    _fake_client(monkeypatch)
    real = _dt.datetime

    class _FarClock(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return real(2030, 1, 2, 3, 4, 5, tzinfo=tz or _dt.timezone.utc)

    monkeypatch.setattr(ES, "datetime", _FarClock)
    st = sync.run_sync(root=tmp_path, now=TS)
    assert st["overall_status"] == "ok"
    admitted = _latest(tmp_path, ES.LATEST_ADMITTED_FILE)
    day = TS[:10]                                   # 2026-09-20, the injected day
    archived = (tmp_path / "outputs/archive/broker_evidence" / day
                / f"{admitted['snapshot_id']}.json")
    assert archived.exists(), "archive must use the injected time, not wall clock"
    assert not (tmp_path / "outputs/archive/broker_evidence" / "2030-01-02").exists()
    assert admitted["admitted_at"].startswith("2026-09-20")
    assert admitted["generated_at"].startswith("2026-09-20")


# ---------------------------------------------------------------------------
# Attempt-clock coherence (residual follow-up to the admitted/archive repair).
#
# One `run_sync(now=T)` is ONE observation, so every record describing it must
# carry T. The admitted record and its write-once archive day were already put
# on that clock; the attempt record is the other half of the same observation
# and was still stamped from the wall clock on every outcome path. Nothing was
# corrupted by that -- no partition or truth decision reads the attempt time --
# but a replayed or backdated sync emitted two disagreeing dates for one run,
# which is the same determinism defect one layer down.
#
# Severity is low; these tests exist so the class stays closed.
# ---------------------------------------------------------------------------

def _attempt(root):
    return _latest(root, ES.LATEST_ATTEMPT_FILE)


def test_successful_sync_puts_attempt_and_admitted_on_one_clock(tmp_path, configured, monkeypatch):
    """A. The same sync no longer reports two dates."""
    _auth(monkeypatch); _fake_client(monkeypatch)
    sync.run_sync(root=tmp_path, now=TS)
    attempt = _attempt(tmp_path)
    admitted = _latest(tmp_path, ES.LATEST_ADMITTED_FILE)
    assert attempt["generated_at"] == TS
    assert admitted["admitted_at"] == TS                     # preserved from the admitted repair
    assert admitted["generated_at"] == TS                    # preserved
    assert (tmp_path / "outputs/archive/broker_evidence" / TS[:10]
            / f"{admitted['snapshot_id']}.json").exists()    # preserved
    assert attempt["generated_at"] == admitted["admitted_at"], "one sync, one clock"
    # the injected instant is the evidence's own possession instant. The PIT
    # fields round-trip through `str(datetime)`, which uses a space separator,
    # so normalise before comparing rather than asserting a display format.
    retrieved = admitted["evidence"]["pit"]["retrieved_at"].replace(" ", "T")
    assert retrieved.startswith(TS[:19]), retrieved


def _scenario(name, monkeypatch):
    """Set up one non-admitted outcome path. Returns the expected outcome."""
    if name == "REAUTH_REQUIRED":
        _auth(monkeypatch, state=AuthState.REAUTH_REQUIRED, token=None, detail="no usable token")
        return "REAUTH_REQUIRED"
    if name == "AUTH_UNAVAILABLE":
        _auth(monkeypatch, state=AuthState.SCHWAB_UNAVAILABLE, token=None, detail="token endpoint 503")
        return "AUTH_UNAVAILABLE"
    if name == "ACQUISITION_FAILED":
        _auth(monkeypatch); _fake_client(monkeypatch, fail=RuntimeError("GET /accounts -> 500"))
        return "ACQUISITION_FAILED"
    if name == "SCHEMA_DRIFT":
        _auth(monkeypatch); _fake_client(monkeypatch, raw={"error": "not a list"})
        return "SCHEMA_DRIFT"
    if name == "NORMALIZATION_FAILED":
        _auth(monkeypatch); _fake_client(monkeypatch, raw=[])
        return "NORMALIZATION_FAILED"
    if name == "EVIDENCE_REFUSED":
        from portfolio_automation.evidence_gateway.admission import AdmissionDecision, AdmissionReason
        _auth(monkeypatch); _fake_client(monkeypatch)
        monkeypatch.setattr(sync.AD, "admit_broker_snapshot",
                            lambda evs, as_of, ref=None: AdmissionDecision(
                                admitted=False, reason=AdmissionReason.PAYLOAD_HASH_MISMATCH,
                                detail="refused for the test"))
        return "EVIDENCE_REFUSED"
    raise AssertionError(name)


@pytest.mark.parametrize("scenario", [
    "REAUTH_REQUIRED", "AUTH_UNAVAILABLE", "ACQUISITION_FAILED",
    "SCHEMA_DRIFT", "NORMALIZATION_FAILED", "EVIDENCE_REFUSED",
])
def test_failed_attempts_use_the_injected_clock_on_every_path(tmp_path, configured, monkeypatch, scenario):
    """B. The repair covers the CLASS, not one call site: every non-admitted
    outcome stamps the attempt with the injected instant."""
    expected = _scenario(scenario, monkeypatch)
    sync.run_sync(root=tmp_path, now=TS)
    attempt = _attempt(tmp_path)
    assert attempt["outcome"] == expected
    assert attempt["generated_at"] == TS, f"{scenario} attempt fell back to the wall clock"
    assert attempt["implies_empty_portfolio"] is False


def test_unconfigured_attempt_uses_the_injected_clock(tmp_path, monkeypatch):
    """B (cont). The earliest return path, before any auth call, is covered too."""
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    sync.run_sync(root=tmp_path, now=TS)
    attempt = _attempt(tmp_path)
    assert attempt["outcome"] == "UNCONFIGURED"
    assert attempt["generated_at"] == TS


def test_default_time_behaviour_is_preserved(tmp_path, configured, monkeypatch):
    """C. `now=None` still means the real current time; production is not frozen."""
    _auth(monkeypatch); _fake_client(monkeypatch)
    before = datetime.now(timezone.utc)
    sync.run_sync(root=tmp_path)
    after = datetime.now(timezone.utc)
    attempt = _attempt(tmp_path)
    admitted = _latest(tmp_path, ES.LATEST_ADMITTED_FILE)
    stamped_attempt = datetime.fromisoformat(attempt["generated_at"])
    stamped_admitted = datetime.fromisoformat(admitted["admitted_at"])
    assert before <= stamped_attempt <= after
    assert before <= stamped_admitted <= after
    assert stamped_attempt == stamped_admitted, "one sync, one clock, in default mode too"
    # Derive the expected partition from the RECORDED instant, not from `before`:
    # a run starting just before UTC midnight and reading its clock just after it
    # would correctly archive under the next date, and pinning `before.date()`
    # would make this assertion flake at exactly one boundary per day -- the same
    # date-dependence this whole change exists to remove.
    assert (tmp_path / "outputs/archive/broker_evidence"
            / stamped_admitted.date().isoformat()).is_dir()


def test_clock_propagation_does_not_change_evidence_semantics(tmp_path, configured, monkeypatch):
    """D. Timestamp coherence is the ONLY intended behavioural change."""
    _auth(monkeypatch); _fake_client(monkeypatch)
    st = sync.run_sync(root=tmp_path, now=TS)
    admitted = _latest(tmp_path, ES.LATEST_ADMITTED_FILE)
    snap = _latest(tmp_path, "schwab_portfolio_snapshot.json")
    pos = _latest(tmp_path, "schwab_positions.json")

    from portfolio_automation.northstar.evidence import EvidenceSnapshot
    rebuilt = EvidenceSnapshot.from_dict(admitted["evidence"])
    assert rebuilt.snapshot_id == admitted["snapshot_id"]           # identity
    assert rebuilt.payload_hash == admitted["payload_hash"]         # payload hash
    assert admitted["admission"]["admitted"] is True                # admission decision
    assert admitted["admission"]["reason"] == "ADMITTED"
    assert st["account_count"] == len(snap["accounts"])             # account count
    assert st["position_count"] == len(pos["positions"])            # positions
    assert snap["evidence_snapshot_id"] == admitted["snapshot_id"]  # projection semantics
    assert snap["evidence_payload_hash"] == admitted["payload_hash"]
    assert snap["projection_of"] == ES.LATEST_ADMITTED_FILE

    state = ES.read_state(tmp_path)                                  # truth status + LKG
    assert state.truth_status is ES.TruthStatus.CURRENT
    assert state.integrity_error is None
    assert len(state.positions()) == len(pos["positions"])
    assert st["auth_state"] == "OK" and st["evidence_truth_status"] == "CURRENT"
    assert st["read_only_mode"] is True and st["trading_enabled"] is False


def test_reauth_classification_is_unchanged_and_preserves_last_known_good(tmp_path, configured, monkeypatch):
    """D (cont). A later failed attempt still cannot replace admitted truth, and
    re-auth is still classified as re-auth rather than an empty portfolio."""
    _auth(monkeypatch); _fake_client(monkeypatch)
    sync.run_sync(root=tmp_path, now=TS)
    admitted_before = (tmp_path / "outputs/latest" / ES.LATEST_ADMITTED_FILE).read_bytes()
    projections_before = (tmp_path / "outputs/latest/schwab_positions.json").read_bytes()

    later = "2026-09-21T12:00:00+00:00"
    _auth(monkeypatch, state=AuthState.REAUTH_REQUIRED, token=None, detail="expired")
    st = sync.run_sync(root=tmp_path, now=later)
    assert st["auth_state"] == "REAUTH_REQUIRED"
    assert st["evidence_truth_status"] == "STALE_LAST_KNOWN_GOOD"
    assert _attempt(tmp_path)["generated_at"] == later
    assert (tmp_path / "outputs/latest" / ES.LATEST_ADMITTED_FILE).read_bytes() == admitted_before
    assert (tmp_path / "outputs/latest/schwab_positions.json").read_bytes() == projections_before
    assert ES.read_state(tmp_path).positions()


def test_same_injected_time_reproduces_the_same_time_bearing_records(tmp_path, configured, monkeypatch):
    """E. Replay determinism, without turning write-once into overwrite."""
    _auth(monkeypatch); _fake_client(monkeypatch)
    sync.run_sync(root=tmp_path, now=TS)
    admitted = _latest(tmp_path, ES.LATEST_ADMITTED_FILE)
    entry = (tmp_path / "outputs/archive/broker_evidence" / TS[:10]
             / f"{admitted['snapshot_id']}.json")
    first_archive = entry.read_bytes()
    first_attempt = _attempt(tmp_path)

    sync.run_sync(root=tmp_path, now=TS)                 # identical inputs, same instant
    assert _attempt(tmp_path) == first_attempt           # attempt reproduces exactly
    assert entry.read_bytes() == first_archive           # write-once archive not rewritten
    assert sorted(p.name for p in (tmp_path / "outputs/archive/broker_evidence").iterdir()) == [TS[:10]]

    # an isolated root at the same instant produces the same time-bearing values
    other = tmp_path / "other"
    (other).mkdir()
    sync.run_sync(root=other, now=TS)
    assert _attempt(other)["generated_at"] == first_attempt["generated_at"]
    assert _latest(other, ES.LATEST_ADMITTED_FILE)["admitted_at"] == admitted["admitted_at"]
    assert _latest(other, ES.LATEST_ADMITTED_FILE)["snapshot_id"] == admitted["snapshot_id"]


def test_every_evidence_record_call_in_run_sync_receives_the_clock(tmp_path):
    """F. Mechanical guard. A future outcome branch that calls record_attempt or
    record_admitted WITHOUT the clock seam fails here rather than silently
    reintroducing a wall-clock stamp on one path."""
    src = Path(sync.__file__).read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "run_sync")
    missing = []
    seen = 0
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("record_attempt", "record_admitted")):
            seen += 1
            if "now" not in {kw.arg for kw in node.keywords}:
                missing.append(f"{node.func.attr} at line {node.lineno}")
    assert seen >= 8, f"expected every outcome path to record an attempt, found {seen} calls"
    assert missing == [], f"evidence records written off the sync clock: {missing}"
