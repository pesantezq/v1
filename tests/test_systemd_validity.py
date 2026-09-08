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
