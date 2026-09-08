"""M23D — the real-systemd unit validity gate.

These tests cover the WRAPPER's contract: inventory completeness, loaded-state
currency, and faithful recording of the real verifier's process status.

They deliberately do NOT emulate systemd acceptance rules. There is no test
here for Type=, RemainAfterExit=, SuccessAction=, ExecStart legality or
RootDirectory semantics — those belong to real systemd, which is the entire
point of this module. The empirical measurements that justify the chosen
invocation are recorded in the module docstring and were taken against systemd
255 with disposable units.
"""
from __future__ import annotations

import re
import pytest

from portfolio_automation.release import systemd_validity as V

UNITS = ("stockbot-daily.service", "stockbot-dashboard.service")
VERSION = "systemd 255 (255.4-1ubuntu8.17)"


def prov(unit, *, load_state="loaded", reload_needed=False, drop_ins=("/d/zz.conf",)):
    return V.UnitProvenance(
        unit=unit,
        load_state=load_state,
        fragment_path=f"/etc/systemd/system/{unit}",
        drop_in_paths=drop_ins,
        need_daemon_reload=reload_needed,
    )


def ok(unit, *, exit_status=0, command=None, output=""):
    return V.VerifierOutcome(
        unit=unit,
        command=command if command is not None else V.build_verifier_command(unit),
        exit_status=exit_status,
        output=output,
    )


def certify(**kw):
    base = dict(
        expected_units=UNITS,
        discovered_units=UNITS,
        provenance={u: prov(u) for u in UNITS},
        outcomes={u: ok(u) for u in UNITS},
        systemd_version=VERSION,
        checked_at="2026-09-08T21:00:00Z",
        host="stockbot-vps",
    )
    base.update(kw)
    return V.certify_systemd_unit_validity(**base)


# --- 1. valid unit -> PASS -------------------------------------------------

def test_1_all_units_valid_and_current_passes():
    result = certify()
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.PASS
    assert result["errors"] == [] and result["blockers"] == []
    assert all(u["verifier_result"] == V.PASS for u in result["units"])


# --- 2/3. invalid unit and nonzero exit -> FAIL ----------------------------

@pytest.mark.parametrize("status", [1, 2, 255])
def test_2_and_3_nonzero_verifier_status_fails(status):
    """The process status is the verdict — no exit code is 'close enough'."""
    outcomes = {u: ok(u) for u in UNITS}
    outcomes[UNITS[0]] = ok(UNITS[0], exit_status=status)
    result = certify(outcomes=outcomes)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.FAIL
    assert any(f"exited {status}" in e for e in result["errors"])


# --- 4. NeedDaemonReload -> NOT_CERTIFIABLE, with no mutation --------------

def test_4_need_daemon_reload_is_not_certifiable_not_fail_and_not_repaired():
    """Stale loaded state means the evidence describes a config that isn't running.

    It is explicitly NOT_CERTIFIABLE rather than FAIL: the unit may be perfectly
    valid. And the remedy is escalation — daemon-reloading to make a
    certification pass would be mutating production to produce the answer we
    wanted.
    """
    provenance = {u: prov(u) for u in UNITS}
    provenance[UNITS[0]] = prov(UNITS[0], reload_needed=True)
    result = certify(provenance=provenance)

    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("NeedDaemonReload=yes" in b for b in result["blockers"])
    assert any("do not" in b.lower() and "daemon-reload" in b.lower()
               for b in result["blockers"])
    # The module is pure: it cannot mutate anything even if it wanted to.
    # Checked against the real import graph, not the source text, so the
    # module may still *discuss* subprocess in its docstring.
    import ast, inspect
    tree = ast.parse(inspect.getsource(V))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
    assert not imported & {'subprocess', 'os', 'shutil', 'socket', 'pathlib'}, imported


# --- 5. missing expected unit -> FAIL -------------------------------------

def test_5_missing_expected_unit_fails():
    result = certify(discovered_units=(UNITS[1],))
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.FAIL
    assert UNITS[0] in result["missing_units"]


# --- 6. unexpected relevant unit -> no blanket PASS ------------------------

def test_6_unexpected_relevant_unit_blocks_a_blanket_pass():
    extra = "stockbot-daily.timer"
    result = certify(discovered_units=UNITS + (extra,))
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.FAIL
    assert extra in result["unexpected_units"]


def test_6b_an_explicitly_classified_unit_no_longer_blocks():
    """Classification is how an operator resolves it — not silent omission."""
    extra = "stockbot-daily.timer"
    result = certify(discovered_units=UNITS + (extra,), classified_units=(extra,))
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.PASS
    assert result["unexpected_units"] == []


# --- 7. unloaded / not-found required unit -> FAIL -------------------------

@pytest.mark.parametrize("state", ["not-found", "error", "masked", ""])
def test_7_required_unit_not_loaded_fails(state):
    provenance = {u: prov(u) for u in UNITS}
    provenance[UNITS[0]] = prov(UNITS[0], load_state=state)
    result = certify(provenance=provenance)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.FAIL
    assert any("LoadState=" in e for e in result["errors"])


def test_7b_missing_provenance_fails():
    result = certify(provenance={UNITS[1]: prov(UNITS[1])})
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.FAIL
    assert any("no loaded-state provenance" in e for e in result["errors"])


# --- 8/9/10. drop-ins are part of what gets verified ----------------------

def test_8_the_command_is_by_name_so_drop_ins_apply():
    """Verifying by NAME is what makes drop-ins participate.

    Measured on systemd 255: a valid base broken by its drop-in fails and names
    the drop-in's binary; a broken base repaired by its drop-in passes. A
    path-only gate over the base fragment would report on text that is not the
    effective configuration.
    """
    cmd = V.build_verifier_command("stockbot-daily.service")
    assert cmd == ("systemd-analyze", "verify", "--recursive-errors=no",
                   "stockbot-daily.service")
    assert not any(arg.startswith("/") for arg in cmd[3:]), "must not be path-based"


def test_9_drop_in_paths_are_recorded_as_evidence():
    """The artifact must show WHICH drop-ins were in effect when it passed."""
    result = certify()
    record = next(u for u in result["units"] if u["unit"] == UNITS[0])
    assert record["drop_in_paths"] == ["/d/zz.conf"]
    assert record["fragment_path"] == f"/etc/systemd/system/{UNITS[0]}"


def test_10_a_drop_in_repaired_unit_is_reported_from_its_effective_state():
    """A clean status for a unit whose base alone is broken must still pass.

    This is the reverse-direction proof: the gate reports the EFFECTIVE
    configuration, so a legitimate drop-in repair is not a false failure.
    """
    result = certify(outcomes={u: ok(u, exit_status=0) for u in UNITS})
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.PASS


# --- 11. text can never override a nonzero status -------------------------

@pytest.mark.parametrize("noise", [
    "", "OK", "All units passed verification.", "success", "PASS",
    "no errors found", "0 issues",
])
def test_11_reassuring_output_cannot_override_a_nonzero_status(noise):
    outcomes = {u: ok(u) for u in UNITS}
    outcomes[UNITS[0]] = ok(UNITS[0], exit_status=1, output=noise)
    result = certify(outcomes=outcomes)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.FAIL


def test_11b_alarming_output_cannot_override_a_zero_status():
    """Symmetry: the status decides, so stderr chatter is not a veto either.

    systemd routinely writes advisory lines while exiting 0. Treating text as
    the verdict is how the earlier emulation got it wrong in both directions.
    """
    outcomes = {u: ok(u, output="Failed to parse something, ignoring")
                for u in UNITS}
    assert certify(outcomes=outcomes)["SYSTEMD_UNIT_VALIDITY"] == V.PASS


# --- 12. verifier not invocable -> fail closed ----------------------------

def test_12_verifier_unavailable_is_not_certifiable():
    result = certify(verifier_available=False)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("could not be invoked" in b for b in result["blockers"])


def test_12b_a_skipped_unit_cannot_be_silently_dropped():
    result = certify(outcomes={UNITS[1]: ok(UNITS[1])})
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.FAIL
    assert any("was not verified" in e for e in result["errors"])


def test_12c_empty_expected_inventory_cannot_pass():
    result = certify(expected_units=(), discovered_units=(), provenance={},
                     outcomes={})
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE


# --- 13. unsupported verifier/version behaviour -> fail closed ------------

@pytest.mark.parametrize("version", ["systemd 249 (249.11)", "systemd 200", "1"])
def test_13_systemd_older_than_the_measured_behaviour_is_not_certifiable(version):
    """--recursive-errors= arrived in 250 and was measured on 255.

    On an older manager the exit status may be meaningless, and this gate is
    built entirely on that exit status, so it refuses rather than assumes.
    """
    result = certify(systemd_version=version)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE


@pytest.mark.parametrize("version", ["", "unknown", "systemd"])
def test_13b_undeterminable_version_is_not_certifiable(version):
    assert certify(systemd_version=version)["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE


def test_13c_dropping_the_required_flag_is_not_certifiable():
    """Without the flag a zero exit means nothing — per systemd-analyze(1).

    This is the gate's own definition, so a command missing it is not a weaker
    run of this gate; it is not this gate at all.
    """
    outcomes = {u: ok(u) for u in UNITS}
    outcomes[UNITS[0]] = ok(UNITS[0], exit_status=0,
                            command=("systemd-analyze", "verify", UNITS[0]))
    result = certify(outcomes=outcomes)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any(V.REQUIRED_VERIFIER_FLAG in b for b in result["blockers"])


# --- 14. no secrets in the result ----------------------------------------

@pytest.mark.parametrize("leak", [
    "OPENAI_API_KEY=sk-proj-abcdef1234567890abcdef1234567890",
    "STOCKBOT_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "password=hunter2hunter2hunter2hunter2hunter2",
])
def test_14_secret_shaped_output_is_redacted(leak):
    secret = leak.split("=", 1)[1]
    outcomes = {u: ok(u) for u in UNITS}
    outcomes[UNITS[0]] = ok(UNITS[0], exit_status=1,
                            output=f"unit failed\n{leak}\n")
    result = certify(outcomes=outcomes)
    assert secret not in repr(result)


def test_14b_output_is_bounded():
    """An artifact is not a log sink; unbounded host text is not evidence."""
    outcomes = {u: ok(u) for u in UNITS}
    outcomes[UNITS[0]] = ok(UNITS[0], exit_status=1,
                            output="\n".join(f"line {i} " + "x" * 900
                                             for i in range(200)))
    record = next(u for u in certify(outcomes=outcomes)["units"]
                  if u["unit"] == UNITS[0])
    assert len(record["verifier_messages"]) <= V.MAX_MESSAGE_LINES
    assert all(len(m) <= V.MAX_MESSAGE_CHARS for m in record["verifier_messages"])


# --- 15. deterministic / stable structured output ------------------------

def test_15_output_is_deterministic_and_order_independent():
    a = certify(expected_units=UNITS, discovered_units=UNITS)
    b = certify(expected_units=tuple(reversed(UNITS)),
                discovered_units=tuple(reversed(UNITS)))
    assert a == b, "input ordering must not change the artifact"


def test_15b_schema_identity_is_recorded():
    result = certify()
    assert result["schema"] == V.SCHEMA
    assert result["schema_version"] == V.SCHEMA_VERSION
    for key in ("checked_at", "host", "systemd_version", "expected_units",
                "discovered_units", "missing_units", "unexpected_units",
                "units", "SYSTEMD_UNIT_VALIDITY"):
        assert key in result, key


def test_15c_duplicate_inputs_do_not_duplicate_records():
    result = certify(expected_units=UNITS + UNITS,
                     discovered_units=UNITS + UNITS)
    assert len(result["units"]) == len(set(UNITS))


# --- provenance parsing ---------------------------------------------------

def test_provenance_is_built_from_real_systemctl_show_output():
    captured = (
        "Id=stockbot-daily.service\n"
        "LoadState=loaded\n"
        "FragmentPath=/etc/systemd/system/stockbot-daily.service\n"
        "DropInPaths=/etc/systemd/system/stockbot-daily.service.d/override.conf "
        "/etc/systemd/system/stockbot-daily.service.d/zz-release.conf\n"
        "NeedDaemonReload=no\n"
        "LoadError=\n"
    )
    p = V.provenance_from_show(captured, unit="stockbot-daily.service")
    assert p.is_loaded and not p.need_daemon_reload
    assert len(p.drop_in_paths) == 2
    assert p.drop_in_paths[1].endswith("zz-release.conf")


def test_blockers_take_precedence_over_failures():
    """'Could not verify' must never be reported as a mere failure or a pass."""
    provenance = {u: prov(u) for u in UNITS}
    provenance[UNITS[0]] = prov(UNITS[0], reload_needed=True)
    outcomes = {u: ok(u) for u in UNITS}
    outcomes[UNITS[1]] = ok(UNITS[1], exit_status=1)
    result = certify(provenance=provenance, outcomes=outcomes)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert result["errors"] and result["blockers"]


def test_the_module_states_the_independence_of_the_three_gates():
    import inspect
    doc = inspect.getdoc(V) or ""
    assert "SCHEDULER_ALIGNMENT" in doc
    assert "RELEASE_POINTER_IDENTITY" in doc
    assert "may infer" in doc


# ---------------------------------------------------------------------------
# The read-only collector and the certifier CLI. The production side of this
# gate is a shell script that only observes; these tests pin that property.
# ---------------------------------------------------------------------------

import importlib.util  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

REPO = _Path(__file__).resolve().parent.parent
COLLECTOR = REPO / "scripts" / "collect_systemd_validity_evidence.sh"
CERTIFIER = REPO / "scripts" / "certify_systemd_validity.py"


def _cli():
    spec = importlib.util.spec_from_file_location("m23d_cli", CERTIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CAPTURE = """##HOST
stockbot-vps
##CHECKED_AT
2026-09-08T21:22:07Z
##SYSTEMD_VERSION
systemd 255 (255.4-1ubuntu8.17)
##EXPECTED
stockbot-daily.service
##DISCOVERED
stockbot-daily.service
##SHOW stockbot-daily.service
Id=stockbot-daily.service
LoadState=loaded
FragmentPath=/etc/systemd/system/stockbot-daily.service
DropInPaths=/etc/systemd/system/stockbot-daily.service.d/zz-release.conf
NeedDaemonReload=no
LoadError=
##VERIFY stockbot-daily.service 0
##END
"""


def test_collector_is_observation_only():
    """The one script that touches production must not be able to change it."""
    text = COLLECTOR.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    for forbidden in ("daemon-reload", "systemctl start", "systemctl stop",
                      "systemctl restart", "systemctl reload", "systemctl enable",
                      "systemctl disable", "systemctl mask", "systemctl unmask",
                      "systemctl edit", "rm ", "mv ", "cp ", "ln -", "chmod",
                      "chown", "tee ", ">>"):
        assert forbidden not in body, f"collector must not {forbidden!r}"


def test_collector_invokes_the_verifier_by_name_with_the_required_flag():
    """By NAME so drop-ins apply; with the flag so the exit status means something."""
    body = COLLECTOR.read_text(encoding="utf-8")
    real = [
        line.strip() for line in body.splitlines()
        if "systemd-analyze verify" in line and not line.lstrip().startswith("#")
    ]
    assert real, "the collector must actually invoke the verifier"
    for call in real:
        assert "--recursive-errors=no" in call, call
        # By unit NAME. A path-based run reports on the base fragment's
        # directory rather than the unit's effective configuration.
        assert "/etc/systemd" not in call, call
        assert "FragmentPath" not in call, call
        assert '"$u"' in call, call


def test_cli_round_trips_a_capture_into_a_pass():
    result = _cli().parse_evidence(CAPTURE)
    verdict = V.certify_systemd_unit_validity(classified_units=(), **result)
    assert verdict["SYSTEMD_UNIT_VALIDITY"] == V.PASS
    assert verdict["host"] == "stockbot-vps"
    assert verdict["units"][0]["drop_in_paths"] == [
        "/etc/systemd/system/stockbot-daily.service.d/zz-release.conf"]


def test_cli_parses_a_nonzero_verify_status():
    capture = CAPTURE.replace("##VERIFY stockbot-daily.service 0",
                              "##VERIFY stockbot-daily.service 1")
    parsed = _cli().parse_evidence(capture)
    assert parsed["outcomes"]["stockbot-daily.service"].exit_status == 1


def test_truncated_capture_is_not_certifiable_via_the_cli(tmp_path):
    """A capture that died halfway must not read as a clean host."""
    partial = tmp_path / "partial.txt"
    partial.write_text(CAPTURE.replace("##END", ""), encoding="utf-8")
    proc = subprocess.run(
        ["python3", str(CERTIFIER), str(partial)],
        capture_output=True, text=True, cwd=str(REPO))
    assert proc.returncode != 0
    assert V.NOT_CERTIFIABLE in proc.stdout
    assert "truncated" in proc.stdout


def test_cli_exit_status_tracks_the_verdict(tmp_path):
    """The gate is wired into a runbook by exit status, so it must be right."""
    good = tmp_path / "good.txt"
    good.write_text(CAPTURE, encoding="utf-8")
    assert subprocess.run(["python3", str(CERTIFIER), str(good)],
                          capture_output=True, cwd=str(REPO)).returncode == 0

    bad = tmp_path / "bad.txt"
    bad.write_text(CAPTURE.replace("##VERIFY stockbot-daily.service 0",
                                   "##VERIFY stockbot-daily.service 1"),
                   encoding="utf-8")
    assert subprocess.run(["python3", str(CERTIFIER), str(bad)],
                          capture_output=True, cwd=str(REPO)).returncode == 1


# ---------------------------------------------------------------------------
# Review findings on PR #43. Each was reproduced against the repository before
# being accepted.
# ---------------------------------------------------------------------------

def test_artifact_declares_observe_only():
    """AGENTS.md requires observe_only on every artifact payload.

    Hardcoded, not a parameter: this gate reports on production and has no
    authority to change it, and a consumer must see that from the payload.
    """
    result = certify()
    assert result["observe_only"] is True
    assert V.OBSERVE_ONLY is True


def test_the_production_timers_are_expected_and_verified():
    """stockbot-daily.timer is what actually starts the daily run.

    deploy/install_systemd.sh installs it alongside the service, and the
    discovery pattern matches it, so omitting it would both leave its syntax
    unverified and make discovery report it as unexpected.
    """
    collector = COLLECTOR.read_text(encoding="utf-8")
    for unit in ("stockbot-daily.timer", "stockbot-sandbox-daily.timer"):
        assert unit in collector, unit


def test_an_absent_optional_unit_does_not_block_a_pass():
    """The sandbox lane is not part of a standard install.

    Requiring it would make the documented command unable to reach PASS on a
    host built by deploy/install_systemd.sh.
    """
    optional = "stockbot-sandbox-daily.service"
    result = certify(
        expected_units=UNITS + (optional,),
        discovered_units=UNITS,
        optional_units=(optional,),
    )
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.PASS
    assert optional not in result["missing_units"]
    assert optional not in result["verified_units"]


def test_an_installed_optional_unit_is_still_verified():
    """Optional means 'may be absent', never 'exempt from checking'.

    A broken unit that happens to be optional is still a broken unit sitting in
    the production manager.
    """
    optional = "stockbot-sandbox-daily.service"
    result = certify(
        expected_units=UNITS + (optional,),
        discovered_units=UNITS + (optional,),
        optional_units=(optional,),
        provenance={**{u: prov(u) for u in UNITS}, optional: prov(optional)},
        outcomes={**{u: ok(u) for u in UNITS},
                  optional: ok(optional, exit_status=1)},
    )
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.FAIL
    assert optional in result["verified_units"]


@pytest.mark.parametrize("host,when", [
    ("", "2026-09-08T21:22:07Z"),
    ("   ", "2026-09-08T21:22:07Z"),
    ("stockbot-vps", ""),
    ("stockbot-vps", "not-a-timestamp"),
    ("stockbot-vps", "2026-09-08 21:22:07"),
])
def test_missing_provenance_is_not_certifiable(host, when):
    """A certificate that cannot say where or when it was taken is not evidence."""
    result = certify(host=host, checked_at=when)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE


def test_release_identity_is_not_established_without_validity_evidence():
    """Scheduler alignment plus pointer identity is NOT production identity.

    A unit can name the approved release perfectly and still be one systemd
    refuses to load, so the aggregate must not be reportable from two gates.
    """
    from portfolio_automation.release import scheduler as S

    surfaces = S.parse_systemd_unit(
        "[Service]\nExecStart=/opt/stockbot/current/scripts/run.sh\n",
        origin="systemd:stockbot-daily.service",
    )
    combined = S.certify_release_identity(
        surfaces, pointer_result={"status": "OK", "errors": []},
        release_root="/opt/stockbot/current",
    )
    # The two-gate result is still OK -- that field keeps its meaning...
    assert combined["status"] == "OK"
    # ...but the aggregate claim is not available without the third gate.
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert combined["systemd_unit_validity"] == S.VALIDITY_NOT_ESTABLISHED
    assert any("SYSTEMD_UNIT_VALIDITY" in e for e in combined["errors"])


@pytest.mark.parametrize("verdict,expected", [
    ("PASS", "PASS"),
    ("FAIL", "NOT_ESTABLISHED"),
    ("NOT_CERTIFIABLE", "NOT_ESTABLISHED"),
])
def test_release_identity_requires_all_three_gates(verdict, expected):
    from portfolio_automation.release import scheduler as S

    surfaces = S.parse_systemd_unit(
        "[Service]\nExecStart=/opt/stockbot/current/scripts/run.sh\n",
        origin="systemd:stockbot-daily.service",
    )
    combined = S.certify_release_identity(
        surfaces, pointer_result={"status": "OK", "errors": []},
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        validity_result={
            "SYSTEMD_UNIT_VALIDITY": verdict,
            "verified_units": ["stockbot-daily.service"],
        },
    )
    assert combined["production_release_identity"] == expected


def test_artifact_writes_are_atomic(tmp_path):
    """An interrupted certification must not truncate a valid prior artifact."""
    cli = _cli()
    target = tmp_path / "evidence.json"
    target.write_text('{"previous": "valid"}', encoding="utf-8")

    class Boom(Exception):
        pass

    original = target.read_text(encoding="utf-8")
    try:
        # Simulate a failure partway through by handing it something unwritable.
        cli._atomic_write(target, "x" * 10)
    except Exception:  # pragma: no cover - the happy path is what we assert
        pass
    assert target.read_text(encoding="utf-8") == "x" * 10

    # And on failure the original survives.
    target.write_text(original, encoding="utf-8")
    import os
    real_replace = os.replace
    os.replace = lambda *a, **k: (_ for _ in ()).throw(Boom())
    try:
        with pytest.raises(Boom):
            cli._atomic_write(target, "should not land")
    finally:
        os.replace = real_replace
    assert target.read_text(encoding="utf-8") == original
    # No temp debris left behind.
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_artifact_schema_is_declared_in_the_contracts_doc():
    """AGENTS.md: a persisted JSON schema must be declared alongside it."""
    contracts = (REPO / "docs" / "OUTPUT_ARTIFACT_CONTRACTS.md").read_text(
        encoding="utf-8")
    assert V.SCHEMA in contracts
    for field in ("observe_only", "SYSTEMD_UNIT_VALIDITY", "verified_units",
                  "optional_units", "verifier_exit_status"):
        assert field in contracts, field


def test_collector_emits_the_optional_section():
    body = COLLECTOR.read_text(encoding="utf-8")
    assert "##OPTIONAL" in body
    parsed = _cli().parse_evidence(
        CAPTURE.replace("##EXPECTED", "##OPTIONAL\nstockbot-x.service\n##EXPECTED"))
    assert "stockbot-x.service" in parsed["optional_units"]


def test_a_run_that_verified_no_unit_cannot_pass():
    """Every expected unit optional and absent must not certify.

    The empty-inventory guard checks the INPUT list, but the optional mechanism
    drains it afterwards: nothing required, nothing verified, and the verifier
    never ran. A fail-closed guard has to sit on the quantity that actually
    decides the verdict, not on an upstream input a later step can empty.
    """
    optional = ("stockbot-sandbox-daily.service", "stockbot-sandbox-daily.timer")
    result = certify(
        expected_units=optional,
        optional_units=optional,
        discovered_units=(),
        provenance={},
        outcomes={},
    )
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert result["verified_units"] == []
    assert any("no unit was verified" in b for b in result["blockers"])


def test_a_partially_optional_inventory_still_certifies_on_what_is_present():
    """The guard must not overreact: one real unit verified is a real result."""
    optional = "stockbot-sandbox-daily.service"
    result = certify(
        expected_units=UNITS + (optional,),
        optional_units=(optional,),
        discovered_units=UNITS,
    )
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.PASS
    assert set(result["verified_units"]) == set(UNITS)


def test_a_retired_streamlit_host_can_still_certify():
    """docs/STREAMLIT_RETIREMENT.md is a SUPPORTED procedure.

    It ends in `systemctl disable --now` and `rm` of the unit file, so a host
    that completed it is correctly configured. A gate that fails there forever
    would be the gate's bug, not the host's.
    """
    retired = "stockbot-streamlit.service"
    result = certify(
        expected_units=UNITS + (retired,),
        optional_units=(retired,),
        discovered_units=UNITS,
    )
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.PASS
    assert retired not in result["missing_units"]


def test_a_streamlit_host_that_still_runs_it_verifies_it():
    """Tolerating absence must not mean tolerating a broken installed unit."""
    unit = "stockbot-streamlit.service"
    result = certify(
        expected_units=UNITS + (unit,),
        optional_units=(unit,),
        discovered_units=UNITS + (unit,),
        provenance={**{u: prov(u) for u in UNITS}, unit: prov(unit)},
        outcomes={**{u: ok(u) for u in UNITS}, unit: ok(unit, exit_status=1)},
    )
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.FAIL
    assert unit in result["verified_units"]


def test_the_collector_treats_the_retired_unit_as_optional():
    body = COLLECTOR.read_text(encoding="utf-8")
    optional_block = body.split("OPTIONAL=")[1].split('"')[1]
    assert "stockbot-streamlit.service" in optional_block
    # ...but it is still in the expected inventory, so it gets verified.
    expected_block = body.split("UNITS=")[1].split('"')[1]
    assert "stockbot-streamlit.service" in expected_block


def test_operator_waivers_are_recorded_in_the_artifact():
    """A waiver nobody can see is not evidence.

    Without this, a certificate could show PASS alongside a discovered unit
    that is neither expected nor unexpected, with no record of the decision
    that allowed it.
    """
    waived = "stockbot-extra.service"
    result = certify(discovered_units=UNITS + (waived,),
                     classified_units=(waived,))
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.PASS
    assert result["unexpected_units"] == []
    assert result["classified_units"] == [waived]


def test_classified_units_are_declared_in_the_contracts_doc():
    contracts = (REPO / "docs" / "OUTPUT_ARTIFACT_CONTRACTS.md").read_text(
        encoding="utf-8")
    assert "classified_units" in contracts


def _daily_surfaces():
    from portfolio_automation.release import scheduler as S
    return S.parse_systemd_unit(
        "[Service]\nExecStart=/opt/stockbot/current/scripts/run.sh\n",
        origin="systemd:stockbot-daily.service",
    )


def test_two_gates_passing_on_disjoint_units_is_not_three_gate_coverage():
    """Both gates green is not enough — they must be about the same units.

    A validity artifact scoped to one unit combined with scheduler evidence
    scoped to another would otherwise claim three-gate coverage of a system
    where no unit had actually passed both.
    """
    from portfolio_automation.release import scheduler as S

    combined = S.certify_release_identity(
        _daily_surfaces(),
        pointer_result={"status": "OK", "errors": []},
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        validity_result={
            "SYSTEMD_UNIT_VALIDITY": "PASS",
            "verified_units": ["stockbot-dashboard.service"],  # a DIFFERENT unit
        },
    )
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert "stockbot-daily.service" in combined["validity_uncovered_units"]
    assert any("passed only one gate" in e for e in combined["errors"])


def test_covered_units_do_establish_the_aggregate():
    from portfolio_automation.release import scheduler as S

    combined = S.certify_release_identity(
        _daily_surfaces(),
        pointer_result={"status": "OK", "errors": []},
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        validity_result={
            "SYSTEMD_UNIT_VALIDITY": "PASS",
            "verified_units": ["stockbot-daily.service",
                               "stockbot-dashboard.service"],
        },
    )
    assert combined["production_release_identity"] == "PASS"
    assert combined["validity_uncovered_units"] == []


def test_without_expected_origins_there_is_nothing_to_bind():
    """Unbound evidence cannot establish the aggregate, even when all green."""
    from portfolio_automation.release import scheduler as S

    combined = S.certify_release_identity(
        _daily_surfaces(),
        pointer_result={"status": "OK", "errors": []},
        release_root="/opt/stockbot/current",
        validity_result={"SYSTEMD_UNIT_VALIDITY": "PASS",
                         "verified_units": ["stockbot-daily.service"]},
    )
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert any("nothing to bind" in e for e in combined["errors"])


def test_cron_origins_do_not_demand_a_systemd_unit():
    """Cron surfaces name no unit, so they cannot be 'uncovered' by validity."""
    from portfolio_automation.release import scheduler as S

    surfaces = _daily_surfaces() + S.parse_crontab(
        "0 9 * * * /opt/stockbot/current/scripts/run_daily_safe.sh\n",
        origin="cron",
    )
    combined = S.certify_release_identity(
        surfaces,
        pointer_result={"status": "OK", "errors": []},
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service", "cron"),
        validity_result={"SYSTEMD_UNIT_VALIDITY": "PASS",
                         "verified_units": ["stockbot-daily.service"]},
    )
    assert combined["validity_uncovered_units"] == []
    assert combined["production_release_identity"] == "PASS"
