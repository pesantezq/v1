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

import hashlib

from portfolio_automation.release import systemd_validity as V
from portfolio_automation.release.observation import ObservationContext

UNITS = ("stockbot-daily.service", "stockbot-dashboard.service")
VERSION = "systemd 255 (255.4-1ubuntu8.17)"

#: The single production observation these fixtures are taken from. Gate
#: results must all carry it before they can be aggregated; tests that care
#: about MIS-matched provenance override it explicitly.
OBS = ObservationContext(host="stockbot-vps", observation_id="obs-m23d-000001")

#: The release the fixture host is running. Gate evidence must agree on it, not
#: merely on the observation token: a shared id survives a deployment landing
#: mid-flow, an observed release does not.
RELEASE = "1130da80832140c9ec4bc165c48b2768c84a8dbe"


def bound(result: dict) -> dict:
    """Tag a pointer-gate result with the shared observation AND release."""
    return {"target_sha": RELEASE, **result, **OBS.as_dict()}


def digest(*parts) -> str:
    """A stable, realistic-looking digest that changes when the inputs do."""
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()


def prov(unit, *, load_state="loaded", reload_needed=False, drop_ins=("/d/zz.conf",),
         fragment_digest=None, drop_in_digest=None,
         fragment_stat=None, drop_in_stat=None, search_path_anchor=None):
    return V.UnitProvenance(
        unit=unit,
        load_state=load_state,
        fragment_path=f"/etc/systemd/system/{unit}",
        drop_in_paths=drop_ins,
        need_daemon_reload=reload_needed,
        fragment_digest=(fragment_digest if fragment_digest is not None
                         else digest("fragment", unit)),
        drop_in_digest=(drop_in_digest if drop_in_digest is not None
                        else (digest("dropins", unit, *drop_ins) if drop_ins
                              else V.DIGEST_NONE)),
        fragment_stat=(fragment_stat if fragment_stat is not None
                       else digest("fragstat", unit)),
        drop_in_stat=(drop_in_stat if drop_in_stat is not None
                      else (digest("dropstat", unit, *drop_ins) if drop_ins
                            else V.DIGEST_NONE)),
        # Always present: the search-path anchor is recorded per unit whether
        # or not that unit has drop-ins, because its job is to witness a
        # drop-in that did not exist at either endpoint.
        search_path_anchor=(search_path_anchor if search_path_anchor is not None
                            else digest("searchpath", unit)),
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
        host=OBS.host,
        observation_id=OBS.observation_id,
        release_pointer_before=RELEASE,
        release_pointer_after=RELEASE,
    )
    base.update(kw)
    # A quiet host is the default: the configuration observed after the
    # verifier ran is the one observed before it. Tests about a concurrent
    # change pass an explicit ``recheck`` that differs.
    base.setdefault("recheck", dict(base["provenance"]))
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


#: A complete capture in the shape the collector actually emits, including the
#: post-verification re-observation. The RECHECK section repeating the SHOW
#: values IS the evidence that nothing moved while the verifier ran.
CAPTURE = """##HOST
stockbot-vps
##OBSERVATION_ID
obs-m23d-000001
##RELEASE_POINTER_BEFORE
1130da80832140c9ec4bc165c48b2768c84a8dbe
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
NorthstarFragmentDigest=3f1c2b7a9d4e5068a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718
NorthstarDropInDigest=b7e6d5c4a39281706f5e4d3c2b1a09876f5e4d3c2b1a09876f5e4d3c2b1a0987
NorthstarFragmentStat=9c8b7a6d5e4f30291827364554637281900aabbccddeeff00112233445566778
NorthstarDropInStat=1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809
NorthstarSearchPathAnchor=4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c
##VERIFYCMD stockbot-daily.service
systemd-analyze verify --recursive-errors=no stockbot-daily.service
##VERIFY stockbot-daily.service 0
##RECHECK stockbot-daily.service
Id=stockbot-daily.service
LoadState=loaded
FragmentPath=/etc/systemd/system/stockbot-daily.service
DropInPaths=/etc/systemd/system/stockbot-daily.service.d/zz-release.conf
NeedDaemonReload=no
LoadError=
NorthstarFragmentDigest=3f1c2b7a9d4e5068a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718
NorthstarDropInDigest=b7e6d5c4a39281706f5e4d3c2b1a09876f5e4d3c2b1a09876f5e4d3c2b1a0987
NorthstarFragmentStat=9c8b7a6d5e4f30291827364554637281900aabbccddeeff00112233445566778
NorthstarDropInStat=1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809
NorthstarSearchPathAnchor=4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c
##RELEASE_POINTER_AFTER
1130da80832140c9ec4bc165c48b2768c84a8dbe
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
        assert "$u" in call, call
    # Invoked through an argv array, so a unit name is never word-split.
    assert 'out=$("${CMD[@]}" 2>&1)' in body


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
        surfaces, observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
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
        surfaces, observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        expected_validity_units=("stockbot-daily.service",),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,
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
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        expected_validity_units=("stockbot-daily.service",),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,
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
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        expected_validity_units=("stockbot-daily.service",),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,
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
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,"SYSTEMD_UNIT_VALIDITY": "PASS",
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
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service", "cron"),
        expected_validity_units=("stockbot-daily.service",),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,"SYSTEMD_UNIT_VALIDITY": "PASS",
                         "verified_units": ["stockbot-daily.service"]},
    )
    assert combined["validity_uncovered_units"] == []
    assert combined["production_release_identity"] == "PASS"


def test_the_capture_supplies_the_command_that_actually_ran():
    """The certifier must not reconstruct the ideal invocation.

    A capture taken by a collector revision that omitted the required flag
    would otherwise be credited with an invocation it never used, and without
    that flag a zero exit status means nothing.
    """
    parsed = _cli().parse_evidence(CAPTURE)
    outcome = parsed["outcomes"]["stockbot-daily.service"]
    assert outcome.command == ("systemd-analyze", "verify",
                               "--recursive-errors=no",
                               "stockbot-daily.service")
    assert outcome.uses_required_flag


def test_a_capture_without_a_recorded_command_fails_closed():
    capture = "\n".join(
        line for line in CAPTURE.splitlines()
        if "VERIFYCMD" not in line and not line.startswith("systemd-analyze")
    ) + "\n"
    parsed = _cli().parse_evidence(capture)
    outcome = parsed["outcomes"]["stockbot-daily.service"]
    assert outcome.command == ()
    assert not outcome.uses_required_flag
    verdict = V.certify_systemd_unit_validity(classified_units=(), **parsed)
    assert verdict["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE


def test_a_capture_taken_without_the_required_flag_fails_closed():
    """The exact scenario: an older collector that ran a bare verify."""
    capture = CAPTURE.replace(
        "systemd-analyze verify --recursive-errors=no stockbot-daily.service",
        "systemd-analyze verify stockbot-daily.service")
    parsed = _cli().parse_evidence(capture)
    assert not parsed["outcomes"]["stockbot-daily.service"].uses_required_flag
    verdict = V.certify_systemd_unit_validity(classified_units=(), **parsed)
    assert verdict["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any(V.REQUIRED_VERIFIER_FLAG in b for b in verdict["blockers"])


def test_the_collector_emits_the_command_it_ran():
    body = COLLECTOR.read_text(encoding="utf-8")
    assert "##VERIFYCMD" in body


@pytest.mark.parametrize("stamp", [
    "2026-99-99T99:99:99Z",   # shape-correct, impossible
    "2026-13-01T00:00:00Z",   # month 13
    "2026-02-30T00:00:00Z",   # never existed
    "2026-09-08T25:00:00Z",   # hour 25
    "2026-09-08T00:60:00Z",   # minute 60
])
def test_an_impossible_timestamp_is_not_a_timestamp(stamp):
    """An impossible collection time cannot establish certificate freshness."""
    assert certify(checked_at=stamp)["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE


@pytest.mark.parametrize("stamp", [
    "2026-09-08T21:22:07Z",
    "2024-02-29T23:59:59Z",   # a real leap day
])
def test_real_timestamps_are_accepted(stamp):
    assert certify(checked_at=stamp)["SYSTEMD_UNIT_VALIDITY"] == V.PASS


def test_a_timer_that_was_never_verified_blocks_the_aggregate():
    """Scheduler origins only ever name units that bind a PATH.

    A [Timer] unit produces no execution surface, so stockbot-daily.timer --
    which is what actually starts the daily run -- can never appear in
    expected_origins and could never be demanded of the validity evidence.
    """
    from portfolio_automation.release import scheduler as S

    combined = S.certify_release_identity(
        _daily_surfaces(),
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        expected_validity_units=("stockbot-daily.service",
                                 "stockbot-daily.timer"),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,
            "SYSTEMD_UNIT_VALIDITY": "PASS",
            # A narrowed collection: the service was verified, the timer wasn't.
            "verified_units": ["stockbot-daily.service"],
        },
    )
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert "stockbot-daily.timer" in combined["validity_uncovered_units"]


def test_a_verified_timer_satisfies_the_binding():
    from portfolio_automation.release import scheduler as S

    combined = S.certify_release_identity(
        _daily_surfaces(),
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        expected_validity_units=("stockbot-daily.service",
                                 "stockbot-daily.timer"),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,
            "SYSTEMD_UNIT_VALIDITY": "PASS",
            "verified_units": ["stockbot-daily.service",
                               "stockbot-daily.timer"],
        },
    )
    assert combined["production_release_identity"] == "PASS"


def test_a_tolerated_optional_absence_still_binds():
    """An optional unit that is installed is verified anyway.

    So tolerating it here can only ever tolerate a genuine declared absence,
    not an unchecked installed unit.
    """
    from portfolio_automation.release import scheduler as S

    combined = S.certify_release_identity(
        _daily_surfaces(),
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        expected_validity_units=("stockbot-daily.service",
                                 "stockbot-sandbox-daily.timer"),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,
            "SYSTEMD_UNIT_VALIDITY": "PASS",
            "expected_units": ["stockbot-daily.service",
                               "stockbot-sandbox-daily.timer"],
            "verified_units": ["stockbot-daily.service"],
            "optional_units": ["stockbot-sandbox-daily.timer"],
        },
    )
    assert combined["production_release_identity"] == "PASS"


def test_without_a_declared_inventory_the_aggregate_is_not_established():
    from portfolio_automation.release import scheduler as S

    combined = S.certify_release_identity(
        _daily_surfaces(),
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,"SYSTEMD_UNIT_VALIDITY": "PASS",
                         "verified_units": ["stockbot-daily.service"]},
    )
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert any("expected_validity_units" in e for e in combined["errors"])


@pytest.mark.parametrize("command,reason", [
    (("true", "--recursive-errors=no", "stockbot-daily.service"), "not the verifier"),
    (("systemd-analyze", "verify", "--recursive-errors=no", "another.service"),
     "verified a different unit"),
    (("systemd-analyze", "verify", "--recursive-errors=no"), "no target at all"),
])
def test_only_the_exact_expected_command_certifies(command, reason):
    """Flag membership is not command identity.

    `true --recursive-errors=no` exits 0 having verified nothing, and a command
    naming another unit says nothing about this one. The operand is material:
    systemd-analyze documents the subcommand as `verify FILE...`.
    """
    unit = "stockbot-daily.service"
    result = certify(
        expected_units=(unit,),
        discovered_units=(unit,),
        provenance={unit: prov(unit)},
        outcomes={unit: V.VerifierOutcome(unit=unit, command=command,
                                          exit_status=0)},
    )
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE, reason
    assert any("only certifies the command that produced it" in b
               or V.REQUIRED_VERIFIER_FLAG in b for b in result["blockers"])


@pytest.mark.parametrize("leak", [
    'API_KEY="alpha beta gamma"',
    "TOKEN='one two three'",
    'STOCKBOT_SECRET="s p a c e d"',
])
def test_quoted_secret_values_are_redacted_whole(leak):
    """systemd echoes malformed assignments back, quotes and all.

    The previous pattern stopped at the first space, leaving the tail of a
    quoted value in the artifact. Malformed units are exactly what this gate
    processes, so their echoed text is the likeliest place for a leak.
    """
    tail = leak.split("=", 1)[1].strip("\"'").split(" ", 1)[1]
    outcomes = {u: ok(u) for u in UNITS}
    outcomes[UNITS[0]] = ok(
        UNITS[0], exit_status=1,
        output=f"EnvironmentFile= path is not absolute, ignoring: {leak}")
    result = certify(outcomes=outcomes)
    assert tail not in repr(result)


def test_a_narrowed_collection_cannot_wave_a_unit_through_as_optional():
    """'Optional and installed implies verified' only holds if it was considered.

    A unit named in optional_units but missing from the artifact's expected
    inventory was never a candidate for verification, so tolerating it would
    let a narrowed collection excuse a unit it never looked at.
    """
    from portfolio_automation.release import scheduler as S

    combined = S.certify_release_identity(
        _daily_surfaces(),
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        expected_validity_units=("stockbot-daily.service",
                                 "stockbot-daily.timer"),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,
            "SYSTEMD_UNIT_VALIDITY": "PASS",
            # Collection was narrowed: the timer is not in expected_units...
            "expected_units": ["stockbot-daily.service"],
            "verified_units": ["stockbot-daily.service"],
            # ...but it is still named optional, which must not excuse it.
            "optional_units": ["stockbot-daily.timer"],
        },
    )
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert "stockbot-daily.timer" in combined["validity_uncovered_units"]


def test_a_genuinely_considered_optional_absence_is_still_tolerated():
    """The guard must not reject a legitimate declared-and-absent optional."""
    from portfolio_automation.release import scheduler as S

    combined = S.certify_release_identity(
        _daily_surfaces(),
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        expected_validity_units=("stockbot-daily.service",
                                 "stockbot-sandbox-daily.timer"),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,
            "SYSTEMD_UNIT_VALIDITY": "PASS",
            "expected_units": ["stockbot-daily.service",
                               "stockbot-sandbox-daily.timer"],
            "verified_units": ["stockbot-daily.service"],
            "optional_units": ["stockbot-sandbox-daily.timer"],
        },
    )
    assert combined["production_release_identity"] == "PASS"


@pytest.mark.parametrize("leak,secret", [
    ('EnvironmentFile=DATABASE_URL="postgres://user:secret pass@host/db"',
     "secret pass"),
    ("DATABASE_URL=postgres://user:hunter2@host/db", "hunter2"),
    ('CONNECTION="mongodb://admin:p@ss w0rd@cluster/db"', "p@ss w0rd"),
    ('SMTP_URI="smtps://mailer:letmein@smtp.example.com"', "letmein"),
])
def test_credentials_without_secret_keywords_are_redacted(leak, secret):
    """A keyword allowlist cannot enumerate every credential name.

    DATABASE_URL carries a password and matches no keyword, and its value can
    contain spaces and punctuation that defeat an opaque-token heuristic. So
    redaction is driven by assignment SHAPE instead of by name.
    """
    outcomes = {u: ok(u) for u in UNITS}
    outcomes[UNITS[0]] = ok(
        UNITS[0], exit_status=1,
        output=f"unit is malformed, ignoring: {leak}")
    assert secret not in repr(certify(outcomes=outcomes))


@pytest.mark.parametrize("diagnostic", [
    "pb-noexec.service: Service has no ExecStart=, ExecStop=, or "
    "SuccessAction=. Refusing.",
    "/run/systemd/system/x.service:6: Unknown section 'Bogus'. Ignoring.",
    "x.service: Command /definitely/not/here is not executable: No such file",
    "/x.service:6: Failed to parse TimeoutStartSec= parameter, ignoring: "
    "not-a-time",
])
def test_redaction_leaves_real_diagnostics_readable(diagnostic):
    """Evidence that says nothing is its own kind of failure.

    Redaction is scoped to assignment values, so bare quoted names and
    empty-valued directives in systemd's own messages survive intact.
    """
    assert V.redact(diagnostic) == diagnostic


def test_malformed_exit_status_fails_closed_through_the_result_path(tmp_path):
    """An unreadable status is malformed evidence, not a passing unit.

    Raising would abort before the structured artifact could be emitted, so
    operators would lose the blockers that distinguish an unreadable capture
    from an established unit failure.
    """
    import json
    import subprocess

    capture = CAPTURE.replace("##VERIFY stockbot-daily.service 0",
                              "##VERIFY stockbot-daily.service interrupted")
    path = tmp_path / "malformed.txt"
    path.write_text(capture, encoding="utf-8")

    proc = subprocess.run(["python3", str(CERTIFIER), str(path)],
                          capture_output=True, text=True, cwd=str(REPO))
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr, "must not crash"
    payload = json.loads(proc.stdout)
    assert payload["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("unreadable exit status" in b for b in payload["blockers"])


def test_a_missing_exit_status_is_still_a_failure(tmp_path):
    import json
    import subprocess

    capture = CAPTURE.replace("##VERIFY stockbot-daily.service 0",
                              "##VERIFY stockbot-daily.service")
    path = tmp_path / "nostatus.txt"
    path.write_text(capture, encoding="utf-8")
    proc = subprocess.run(["python3", str(CERTIFIER), str(path)],
                          capture_output=True, text=True, cwd=str(REPO))
    assert proc.returncode != 0
    assert json.loads(proc.stdout)["SYSTEMD_UNIT_VALIDITY"] != V.PASS


def test_absent_need_daemon_reload_is_unknown_not_no():
    """A partial capture must not certify the property this gate exists to prove.

    Defaulting absence to `no` would let evidence that never established
    loaded-state currency produce a PASS.
    """
    captured = (
        "Id=stockbot-daily.service\n"
        "LoadState=loaded\n"
        "FragmentPath=/etc/systemd/system/stockbot-daily.service\n"
        # NeedDaemonReload deliberately absent
    )
    p = V.provenance_from_show(captured, unit="stockbot-daily.service")
    assert p.need_daemon_reload is None

    result = certify(provenance={**{u: prov(u) for u in UNITS}, UNITS[0]: p})
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("NeedDaemonReload was not captured" in b
               for b in result["blockers"])


@pytest.mark.parametrize("value,expected", [
    ("no", False), ("yes", True),
    ("", None), ("maybe", None), ("NO", False), ("Yes", True),
])
def test_need_daemon_reload_is_tri_state(value, expected):
    captured = f"Id=x.service\nLoadState=loaded\nNeedDaemonReload={value}\n"
    assert V.provenance_from_show(captured, unit="x.service").need_daemon_reload \
        is expected


@pytest.mark.parametrize("leak,secret", [
    ("EnvironmentFile= path is not absolute, ignoring: AUTH=hunter2", "hunter2"),
    ("DB=short1", "short1"),
    ("X=abc", "abc"),
])
def test_bare_assignment_values_are_redacted_without_a_keyword_list(leak, secret):
    """`AUTH` matches no keyword list worth maintaining.

    Redaction covers bare assignment values by shape, so short, unremarkable
    variable names do not become the gap.
    """
    outcomes = {u: ok(u) for u in UNITS}
    outcomes[UNITS[0]] = ok(UNITS[0], exit_status=1, output=leak)
    assert secret not in repr(certify(outcomes=outcomes))


def test_a_damaged_verifier_command_record_does_not_abort_certification(tmp_path):
    """One unreadable line must not cost the whole structured result."""
    import json
    import subprocess

    capture = CAPTURE.replace(
        "systemd-analyze verify --recursive-errors=no stockbot-daily.service",
        'systemd-analyze verify --recursive-errors=no "unterminated')
    path = tmp_path / "badcmd.txt"
    path.write_text(capture, encoding="utf-8")

    proc = subprocess.run(["python3", str(CERTIFIER), str(path)],
                          capture_output=True, text=True, cwd=str(REPO))
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("unreadable verifier command" in b for b in payload["blockers"])


def test_escaped_quotes_do_not_end_a_redacted_value():
    """An escaped quote is part of the value, not the end of it."""
    leak = 'EnvironmentFile=API_KEY="alpha\\" beta gamma"'
    outcomes = {u: ok(u) for u in UNITS}
    outcomes[UNITS[0]] = ok(UNITS[0], exit_status=1, output=leak)
    assert "beta gamma" not in repr(certify(outcomes=outcomes))


def test_a_concatenated_capture_is_not_a_completed_one():
    """A complete run followed by a truncated retry contains ##END in the middle.

    Reading that as 'the capture completed' would attribute the first run's
    unit evidence to the second run's host and time.
    """
    cli = _cli()
    doubled = CAPTURE + "##HOST\nother-host\n##CHECKED_AT\n2026-09-09T01:00:00Z\n"
    defects = cli.stream_defects(doubled)
    assert defects, "records after the terminator must be rejected"
    assert any("past its terminator" in d or "more than one capture" in d
               for d in defects)


def test_two_terminators_are_rejected():
    cli = _cli()
    assert cli.stream_defects(CAPTURE + CAPTURE)


def test_a_complete_capture_has_no_stream_defects():
    """The structural check must not reject a legitimate capture."""
    assert _cli().stream_defects(CAPTURE) == []


def test_a_concatenated_capture_cannot_pass_through_the_cli(tmp_path):
    import json
    import subprocess

    doubled = CAPTURE + "##HOST\nother-host\n##CHECKED_AT\n2026-09-09T01:00:00Z\n"
    path = tmp_path / "doubled.txt"
    path.write_text(doubled, encoding="utf-8")
    proc = subprocess.run(["python3", str(CERTIFIER), str(path)],
                          capture_output=True, text=True, cwd=str(REPO))
    assert proc.returncode != 0
    assert json.loads(proc.stdout)["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE


def test_provenance_for_one_unit_cannot_certify_another():
    """Whatever section it was filed under, the Id is what it describes."""
    mismatched = V.provenance_from_show(
        "Id=stockbot-dashboard.service\nLoadState=loaded\n"
        "NeedDaemonReload=no\n",
        unit=UNITS[0],
    )
    result = certify(provenance={**{u: prov(u) for u in UNITS},
                                 UNITS[0]: mismatched})
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("cannot certify another" in b for b in result["blockers"])


def test_release_evidence_is_restricted_to_an_approved_namespace():
    """Containment is not belonging.

    safe_write_json proves a write stays inside the namespace it was given, not
    that a production release certificate belongs there. `historical` maps to
    outputs/backtest/, the replay-only tree.
    """
    cli = _cli()
    assert "policy" in cli.APPROVED_NAMESPACES
    for forbidden in ("historical", "sandbox", "latest", "portfolio"):
        assert forbidden not in cli.APPROVED_NAMESPACES, forbidden


def test_the_cli_rejects_an_unapproved_namespace(tmp_path):
    import subprocess

    path = tmp_path / "c.txt"
    path.write_text(CAPTURE, encoding="utf-8")
    proc = subprocess.run(
        ["python3", str(CERTIFIER), str(path), "--namespace", "historical"],
        capture_output=True, text=True, cwd=str(REPO))
    assert proc.returncode != 0
    assert "invalid choice" in proc.stderr or "historical" in proc.stderr


# ---------------------------------------------------------------------------
# M23D review round 5 — the three findings raised against 5cf8f96, each
# reproduced against real systemd 255 before it was fixed.
# ---------------------------------------------------------------------------

# --- P1-C. escaped whitespace inside a bare assignment value ---------------

def test_an_escaped_space_does_not_leave_the_secret_tail_behind():
    r"""The exact systemd 255 emission that leaked, reproduced on the host.

    ``EnvironmentFile=AUTH=alpha\ beta`` in a unit makes systemd 255 report
    ``EnvironmentFile= path is not absolute, ignoring: AUTH=alpha\ beta``. A
    value pattern of ``\S+`` stops at the escaped space and persists ``beta``
    -- the tail of the credential -- in the artifact.
    """
    line = (r"/etc/systemd/system/x.service:6: EnvironmentFile= path is not "
            r"absolute, ignoring: AUTH=alpha\ beta")
    cleaned = V.redact(line)
    assert "alpha" not in cleaned
    assert "beta" not in cleaned, cleaned
    assert V._REDACTED in cleaned


@pytest.mark.parametrize("value", [
    r"alpha\ beta",
    r"pa\ ss\ word",
    r"one\ two\ three\ four",
])
def test_every_escaped_segment_of_a_bare_value_is_consumed(value):
    """One escape or many: no fragment of the value may survive."""
    cleaned = V.redact("ignoring: SECRET=" + value)
    for fragment in value.replace(chr(92), " ").split():
        assert fragment not in cleaned, (fragment, cleaned)


@pytest.mark.parametrize("diagnostic", [
    "Service has no ExecStart=, ExecStop=, or SuccessAction=. Refusing.",
    "Unknown section 'Bogus'. Ignoring.",
    "/etc/systemd/system/x.service:5: Unknown key name 'Frobnicate' in "
    "section 'Service', ignoring.",
    "Failed to parse TimeoutStartSec= parameter, ignoring: not-a-number",
])
def test_ordinary_diagnostics_survive_the_escape_aware_redactor(diagnostic):
    """Redaction that eats the diagnostics makes the evidence useless.

    The escape-awareness must not widen the match into systemd's own prose,
    where a directive name is followed by punctuation rather than a value.
    """
    assert V.redact(diagnostic) == diagnostic


def test_a_word_after_an_unescaped_space_is_not_part_of_the_value():
    """The escape is what extends the value -- a plain space still ends it."""
    assert V.redact("AUTH=secret plaintext") == V._REDACTED + " plaintext"


# --- P1-B. the loaded-state / verifier race --------------------------------

def test_without_a_post_verification_observation_nothing_is_certifiable():
    """A capture that never looked again cannot say the config held still."""
    result = certify(recheck={})
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("re-observed after verification" in b for b in result["blockers"])


def test_a_reload_becoming_pending_during_verification_is_not_certifiable():
    """NeedDaemonReload=no before, =yes after: a deploy landed mid-window.

    Measured on systemd 255: rewriting a loaded unit's file after
    ``systemctl show`` and before ``systemd-analyze verify`` yields a clean
    exit status for the NEW on-disk text while the captured reload state still
    describes the OLD loaded-vs-disk relationship.
    """
    after = {u: prov(u) for u in UNITS}
    after[UNITS[0]] = prov(UNITS[0], reload_needed=True)
    result = certify(recheck=after)

    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("re-observed after verification" in b and "NeedDaemonReload=yes" in b
               for b in result["blockers"])
    # The remedy is escalation, never a reload to tidy production up.
    assert any("do not daemon-reload" in b.lower() for b in result["blockers"])


def test_a_settled_redeploy_inside_the_window_is_caught_by_the_digest():
    """The case NeedDaemonReload structurally CANNOT see.

    A deployment that writes the unit AND reloads inside the collection window
    leaves NeedDaemonReload=no at both ends, because each observation is
    internally consistent -- yet the verifier read bytes that are no longer
    what PID 1 has loaded. Only the configuration digest reveals it. Confirmed
    against real systemd 255 by splicing two genuine captures of one unit
    taken either side of a real write-and-reload.
    """
    after = {u: prov(u) for u in UNITS}
    after[UNITS[0]] = prov(UNITS[0], fragment_digest=digest("fragment", "REDEPLOYED"))
    result = certify(recheck=after)

    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("configuration changed while the verifier ran" in b
               for b in result["blockers"])
    # Both observations reported a current loaded state, so the pre-existing
    # NeedDaemonReload check cannot be what caught this.
    assert not any("NeedDaemonReload=yes" in b for b in result["blockers"])


def test_a_drop_in_appearing_during_the_window_is_caught():
    """A new drop-in changes what systemd would load even if no file changed."""
    after = {u: prov(u) for u in UNITS}
    after[UNITS[0]] = prov(UNITS[0], drop_ins=("/d/zz.conf", "/d/99-hotfix.conf"))
    result = certify(recheck=after)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("configuration changed while the verifier ran" in b
               for b in result["blockers"])


@pytest.mark.parametrize("digest_value", ["", V.DIGEST_UNREADABLE])
def test_a_configuration_that_could_not_be_digested_is_not_certifiable(digest_value):
    """Unpinnable bytes are missing evidence, not a passing state."""
    before = {u: prov(u) for u in UNITS}
    before[UNITS[0]] = prov(UNITS[0], fragment_digest=digest_value)
    result = certify(provenance=before, recheck=before)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE


def test_a_recheck_filed_under_the_wrong_unit_cannot_establish_stability():
    """Evidence for one unit cannot establish another's stability."""
    after = {u: prov(u) for u in UNITS}
    after[UNITS[0]] = prov(UNITS[1])          # evidence for a DIFFERENT unit
    result = certify(recheck=after)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE


def test_a_quiet_host_still_reports_its_configuration_as_stable():
    """The new evidence must not make an unchanged host uncertifiable."""
    result = certify()
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.PASS
    assert all(u["configuration_stable"] is True for u in result["units"])


def test_the_collector_re_observes_after_verifying():
    """The evidence the fix depends on has to actually be collected."""
    body = COLLECTOR.read_text(encoding="utf-8")
    assert "##RECHECK" in body
    assert body.index("##VERIFY ") < body.index("##RECHECK"), \
        "the re-observation must come AFTER the verifier, or it proves nothing"
    # Still read-only: hashing reads bytes, it does not change them.
    assert "sha256sum" in body


# --- P1-A. binding the three gates to one observation ----------------------

STAGING = ObservationContext(host="staging-box.internal",
                             observation_id=OBS.observation_id)
LATER_RUN = ObservationContext(host=OBS.host, observation_id="obs-m23d-000002")


def _aggregate(*, validity_obs=OBS, pointer_obs=OBS, aggregate_obs=OBS):
    from portfolio_automation.release import scheduler as S
    unit = "stockbot-daily.service"
    return S.certify_release_identity(
        S.parse_systemd_unit(
            "[Service]\nExecStart=/opt/stockbot/current/scripts/run.sh\n",
            origin="systemd:" + unit),
        pointer_result={"status": "OK", "errors": [], "target_sha": RELEASE,
                        **pointer_obs.as_dict()},
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:" + unit,),
        expected_validity_units=(unit,),
        validity_result={**validity_obs.as_dict(),
                         "release_pointer": RELEASE,
                         "SYSTEMD_UNIT_VALIDITY": "PASS",
                         "verified_units": [unit]},
        observation=aggregate_obs,
    )


def test_a_genuine_validity_pass_from_another_host_cannot_certify_production():
    """The reported failure: staging validity + production scheduler/pointer.

    Every gate is genuinely green and the unit NAMES match, so the unit-
    coverage binding cannot see it. Only provenance can.
    """
    combined = _aggregate(validity_obs=STAGING)
    assert combined["systemd_unit_validity"] == "PASS"
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert any("more than one observation" in c
               for c in combined["provenance_conflicts"])


def test_the_same_host_observed_twice_is_still_two_observations():
    """A host match alone is not enough: the runs must match too.

    A pointer read before a deploy and a unit verification after it are each
    truthful about the same host and jointly describe a system that never
    existed.
    """
    combined = _aggregate(validity_obs=LATER_RUN)
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert any("more than one observation" in c
               for c in combined["provenance_conflicts"])


def test_pointer_evidence_from_a_different_run_also_breaks_the_binding():
    combined = _aggregate(pointer_obs=LATER_RUN)
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("bad_id", ["", "   ", "short", "!!!!!!!!"])
def test_an_unusable_observation_id_cannot_bind_anything(bad_id):
    """Blank or malformed ids must not let unrelated runs 'agree'."""
    ctx = ObservationContext(host=OBS.host, observation_id=bad_id)
    combined = _aggregate(validity_obs=ctx, pointer_obs=ctx, aggregate_obs=ctx)
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert combined["provenance_conflicts"]


def test_evidence_with_no_provenance_at_all_is_not_established():
    """Absence of provenance is not tolerance -- it fails closed."""
    from portfolio_automation.release import scheduler as S
    unit = "stockbot-daily.service"
    combined = S.certify_release_identity(
        S.parse_systemd_unit(
            "[Service]\nExecStart=/opt/stockbot/current/scripts/run.sh\n",
            origin="systemd:" + unit),
        pointer_result={"status": "OK", "errors": []},
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:" + unit,),
        expected_validity_units=(unit,),
        validity_result={"SYSTEMD_UNIT_VALIDITY": "PASS",
                         "verified_units": [unit]},
    )
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert any("no observation id" in c for c in combined["provenance_conflicts"])
    assert any("no release pointer" in c for c in combined["provenance_conflicts"])


def test_one_host_and_one_run_does_establish_the_aggregate():
    """The fix must not make a correctly-collected production run uncertifiable."""
    combined = _aggregate()
    assert combined["provenance_conflicts"] == []
    assert combined["production_release_identity"] == "PASS"


def test_the_aggregator_cannot_mint_an_observation_id():
    """Agreement the aggregator manufactured would be no evidence at all.

    Pinned structurally rather than by behaviour: the binding module must have
    no way to produce an identifier, so a later change cannot quietly add a
    default that makes every gate 'agree'.
    """
    import ast, inspect
    from portfolio_automation.release import observation as O
    tree = ast.parse(inspect.getsource(O))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"uuid", "random", "secrets", "time", "datetime",
                           "socket", "os", "subprocess"}, imported


def test_the_validity_gate_alone_does_not_require_an_observation_id():
    """Gate independence: whether systemd accepts these units is true anyway.

    The id binds gates TOGETHER; it is not part of what the verifier
    established about the host, so requiring it here would couple a gate to an
    aggregation concern it does not own.
    """
    result = certify(observation_id="")
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.PASS
    assert result["observation_id"] == ""


def test_the_collector_passes_the_observation_id_through_rather_than_inventing_one():
    """An id the collector invented could only ever match itself."""
    body = COLLECTOR.read_text(encoding="utf-8")
    assert "##OBSERVATION_ID" in body
    assert "STOCKBOT_OBSERVATION_ID" in body
    for minted in ("uuidgen", "$RANDOM", "/proc/sys/kernel/random"):
        assert minted not in body, "the collector must not mint an id: " + minted


# ---------------------------------------------------------------------------
# M23D review round 6 — findings raised against 5ced770, each reproduced
# against real systemd 255 before it was fixed.
# ---------------------------------------------------------------------------

# --- N4. an assignment whose quote is never closed -------------------------

def test_an_unterminated_quoted_value_is_redacted_to_end_of_line():
    """Reproduced on the host: systemd 255 echoes the malformed value back.

    A unit containing ``EnvironmentFile=AUTH="alpha beta gamma`` is reported
    verbatim. The terminated-quote pattern cannot match it, and the bare
    fallback stopped at the first space -- redacting ``AUTH="alpha`` and
    persisting ``beta gamma``. Malformed units are this gate's primary input,
    so this is the common case, not an exotic one.
    """
    line = ('/etc/systemd/system/x.service:6: EnvironmentFile= path is not '
            'absolute, ignoring: AUTH="alpha beta gamma')
    cleaned = V.redact(line)
    for fragment in ("alpha", "beta", "gamma"):
        assert fragment not in cleaned, (fragment, cleaned)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_both_quote_styles_redact_when_unterminated(quote):
    cleaned = V.redact(f"ignoring: SECRET={quote}alpha beta gamma")
    for fragment in ("alpha", "beta", "gamma"):
        assert fragment not in cleaned, (fragment, cleaned)


def test_a_terminated_value_still_ends_at_its_closing_quote():
    """The end-of-line rule must not swallow text after a CLOSED value."""
    cleaned = V.redact('AUTH="alpha beta" and then prose')
    assert "alpha" not in cleaned and "beta" not in cleaned
    assert "and then prose" in cleaned


def test_balanced_quotes_in_ordinary_diagnostics_still_survive():
    """Redaction that eats systemd's own prose makes the evidence useless."""
    for diagnostic in (
        "Unknown section 'Bogus'. Ignoring.",
        "/etc/systemd/system/x.service:5: Unknown key name 'Frobnicate' in "
        "section 'Service', ignoring.",
    ):
        assert V.redact(diagnostic) == diagnostic


# --- N3. an installed optional unit can never be waived --------------------

def _artifact_with(**over):
    base = {
        **OBS.as_dict(),
        "release_pointer": RELEASE,
        "SYSTEMD_UNIT_VALIDITY": "PASS",
        "expected_units": ["stockbot-daily.service", "stockbot-daily.timer"],
        "optional_units": ["stockbot-daily.timer"],
        "discovered_units": ["stockbot-daily.service"],
        "verified_units": ["stockbot-daily.service"],
    }
    base.update(over)
    return base


def _combine(artifact):
    from portfolio_automation.release import scheduler as S
    svc = "stockbot-daily.service"
    return S.certify_release_identity(
        S.parse_systemd_unit(
            "[Service]\nExecStart=/opt/stockbot/current/scripts/run.sh\n",
            origin="systemd:" + svc),
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:" + svc,),
        expected_validity_units=(svc, "stockbot-daily.timer"),
        validity_result=artifact,
        observation=OBS,
    )


def test_an_installed_optional_unit_cannot_be_waived_as_absent():
    """'Optional' licenses a MISSING unit, never an unverified installed one.

    An artifact listing a timer as expected, optional and discovered while
    omitting it from verified_units is internally inconsistent -- stale,
    narrowed or edited -- and must not be read as coverage.
    """
    combined = _combine(_artifact_with(
        discovered_units=["stockbot-daily.service", "stockbot-daily.timer"]))
    assert "stockbot-daily.timer" in combined["validity_uncovered_units"]
    assert "stockbot-daily.timer" in combined["validity_installed_but_unverified"]
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert any("installed unit is verified whether or not it is optional" in e
               for e in combined["errors"])


def test_a_genuinely_absent_optional_unit_is_still_tolerated():
    """The fix must not break the documented optional-and-absent case."""
    combined = _combine(_artifact_with())
    assert combined["validity_uncovered_units"] == []
    assert combined["validity_installed_but_unverified"] == []
    assert combined["production_release_identity"] == "PASS"


def test_an_installed_optional_unit_that_was_verified_is_covered():
    combined = _combine(_artifact_with(
        discovered_units=["stockbot-daily.service", "stockbot-daily.timer"],
        verified_units=["stockbot-daily.service", "stockbot-daily.timer"]))
    assert combined["production_release_identity"] == "PASS"


# --- N2. a configuration that changes and returns --------------------------

def test_a_change_and_return_during_verification_is_caught_by_stat():
    """The case the CONTENT digest structurally cannot see.

    A deployment that moves a unit A -> B and restores A before the
    re-observation leaves both endpoint digests equal to A, while the verifier
    actually read B. Confirmed against real systemd 255 by splicing three
    genuine captures (A, B, restored-A). Rewriting a file advances its mtime
    and ctime even when the bytes are identical, so the stat signature differs
    where the digest does not.
    """
    after = {u: prov(u) for u in UNITS}
    after[UNITS[0]] = prov(UNITS[0], fragment_stat=digest("fragstat", "RESTORED"))
    result = certify(recheck=after)

    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("configuration changed while the verifier ran" in b
               for b in result["blockers"])
    # Content was identical at both ends, so the digest cannot be what caught it.
    before = certify()["units"][0]
    assert before["configuration_stable"] is True


@pytest.mark.parametrize("stat_value", ["", V.DIGEST_UNREADABLE])
def test_a_configuration_whose_stat_is_unusable_is_not_certifiable(stat_value):
    before = {u: prov(u) for u in UNITS}
    before[UNITS[0]] = prov(UNITS[0], fragment_stat=stat_value)
    result = certify(provenance=before, recheck=before)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE


def test_the_collector_records_a_stat_signature_as_well_as_a_digest():
    body = COLLECTOR.read_text(encoding="utf-8")
    assert "NorthstarFragmentStat" in body and "NorthstarDropInStat" in body
    assert "NorthstarSearchPathAnchor" in body
    assert "stat -c" in body, "the stat signature must come from real stat(1)"


# --- N1. binding gates to observed state, not only to a token --------------

def test_a_deployment_between_two_gates_breaks_the_binding():
    """A shared observation id survives a mid-flow deployment; state does not.

    One collection flow issues one id. If the pointer gate reads before a
    deployment and the unit evidence is taken after it, both legitimately carry
    the same id while describing different releases.
    """
    from portfolio_automation.release import scheduler as S
    svc = "stockbot-daily.service"
    other = "ddf4ebe57ecd75b90dfe35b319651952ac34f136"
    combined = S.certify_release_identity(
        S.parse_systemd_unit(
            "[Service]\nExecStart=/opt/stockbot/current/scripts/run.sh\n",
            origin="systemd:" + svc),
        pointer_result={"status": "OK", "errors": [], "target_sha": RELEASE,
                        **OBS.as_dict()},
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:" + svc,),
        expected_validity_units=(svc,),
        validity_result={**OBS.as_dict(), "release_pointer": other,
                         "SYSTEMD_UNIT_VALIDITY": "PASS",
                         "verified_units": [svc]},
        observation=OBS,
    )
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert any("a deployment landed between the two observations" in c
               for c in combined["provenance_conflicts"])


def test_a_deployment_during_the_collection_itself_is_not_certifiable():
    """The collector brackets its run, so a mid-run deploy cannot pass."""
    result = certify(release_pointer_before=RELEASE,
                     release_pointer_after="ddf4ebe57ecd75b90dfe35b319651952ac34f136")
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("release pointer moved during collection" in b
               for b in result["blockers"])
    assert result["release_pointer"] == ""


def test_a_quiet_run_records_the_release_it_observed():
    result = certify()
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.PASS
    assert result["release_pointer"] == RELEASE


def test_the_collector_brackets_its_run_with_the_release_pointer():
    body = COLLECTOR.read_text(encoding="utf-8")
    assert "##RELEASE_POINTER_BEFORE" in body
    assert "##RELEASE_POINTER_AFTER" in body
    assert body.index("##RELEASE_POINTER_BEFORE") < body.index("##VERIFY ")
    assert body.index("##VERIFY ") < body.index("##RELEASE_POINTER_AFTER")
    # Read-only: resolving a symlink and reading a git HEAD change nothing.
    assert "readlink -f" in body and "rev-parse HEAD" in body


# ---------------------------------------------------------------------------
# P1-2: a change that leaves no trace at either endpoint
# ---------------------------------------------------------------------------

def test_a_transient_drop_in_is_caught_by_the_search_path_anchor():
    """A drop-in that appears and vanishes inside the window must fail closed.

    This is the case the per-file anchors are structurally blind to. Measured
    on systemd 255: a drop-in added before ``systemd-analyze verify`` and
    removed before the re-observation is absent from ``DropInPaths`` at BOTH
    ends, so its content digest and its stat signature each read the same
    value twice, while the verifier -- which reads the search path from DISK
    rather than from the loaded manager state -- demonstrably parsed it. Only
    the containing DIRECTORY witnesses it, because adding or removing an entry
    advances that directory's mtime and ctime.
    """
    unit = UNITS[0]
    quiet = {u: prov(u, drop_ins=()) for u in UNITS}
    moved = dict(quiet)
    moved[unit] = prov(unit, drop_ins=(),
                       search_path_anchor=digest("searchpath", unit, "TRANSIENT"))
    result = certify(provenance=quiet, recheck=moved)
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any(unit in b for b in result["blockers"]), result["blockers"]
    # ...and every per-file anchor was identical across the window, which is
    # exactly why nothing else could have caught it.
    assert quiet[unit].fragment_digest == moved[unit].fragment_digest
    assert quiet[unit].fragment_stat == moved[unit].fragment_stat
    assert quiet[unit].drop_in_digest == moved[unit].drop_in_digest
    assert quiet[unit].drop_in_stat == moved[unit].drop_in_stat


def test_a_missing_search_path_anchor_is_not_a_pass():
    """An absent anchor is missing evidence, not a quiet host."""
    blank = {u: prov(u, search_path_anchor="") for u in UNITS}
    result = certify(provenance=blank, recheck=dict(blank))
    assert result["SYSTEMD_UNIT_VALIDITY"] == V.NOT_CERTIFIABLE
    assert any("search path anchor" in b for b in result["blockers"]), \
        result["blockers"]


def test_a_quiet_window_keeps_the_anchor_stable():
    """The control: an untouched window must still certify."""
    assert certify()["SYSTEMD_UNIT_VALIDITY"] == "PASS"


# ---------------------------------------------------------------------------
# P1-3: optional never waives an INSTALLED unit
# ---------------------------------------------------------------------------

def _optional_aggregate(*, discovered, verified):
    from portfolio_automation.release import scheduler as S
    return S.certify_release_identity(
        _daily_surfaces(),
        observation=OBS,
        pointer_result=bound({"status": "OK", "errors": []}),
        release_root="/opt/stockbot/current",
        expected_origins=("systemd:stockbot-daily.service",),
        expected_validity_units=("stockbot-daily.service",),
        validity_result={**OBS.as_dict(), "release_pointer": RELEASE,
            "SYSTEMD_UNIT_VALIDITY": "PASS",
            "expected_units": ["stockbot-daily.service"],
            "optional_units": ["stockbot-daily.service"],
            "discovered_units": list(discovered),
            "verified_units": list(verified),
        },
    )


def test_optional_cannot_waive_a_unit_the_evidence_says_is_installed():
    """expected + optional + DISCOVERED + unverified must not certify.

    A unit that appears in the artifact's own discovery is present on the
    host. "Optional" licenses a MISSING unit, never an unverified installed
    one -- an artifact that says a unit is both installed and optional while
    omitting it from ``verified_units`` is internally inconsistent, and
    reading its optional flag as coverage would wave through a unit nothing
    checked.
    """
    combined = _optional_aggregate(
        discovered=["stockbot-daily.service"], verified=[])
    assert combined["production_release_identity"] == "NOT_ESTABLISHED"
    assert "stockbot-daily.service" in combined["validity_installed_but_unverified"]
    assert any("cannot waive it" in e for e in combined["errors"]), \
        combined["errors"]


def test_optional_still_waives_a_unit_that_is_genuinely_absent():
    """The legitimate case must keep working: expected + optional + ABSENT."""
    combined = _optional_aggregate(discovered=[], verified=[])
    assert combined["production_release_identity"] == "PASS", combined["errors"]
    assert combined["validity_installed_but_unverified"] == []


def test_an_installed_optional_unit_that_was_verified_still_certifies():
    combined = _optional_aggregate(
        discovered=["stockbot-daily.service"],
        verified=["stockbot-daily.service"])
    assert combined["production_release_identity"] == "PASS", combined["errors"]


# ---------------------------------------------------------------------------
# P1-4: an unterminated quote has no closing boundary to stop at
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line, secret", [
    ('EnvironmentFile=AUTH="alpha beta gamma', ("alpha", "beta", "gamma")),
    ("EnvironmentFile=AUTH='alpha beta gamma", ("alpha", "beta", "gamma")),
    ('x.service:5: Ignoring invalid environment assignment: AUTH="hunter2 more',
     ("hunter2", "more")),
    ('A="alpha" B="beta gamma', ("alpha", "beta", "gamma")),
])
def test_an_unterminated_quoted_value_does_not_leak_its_tail(line, secret):
    """The bare fallback stops at the first space and persists the rest.

    With no closing quote there is no value boundary left to find, so the only
    safe reading is that the value runs to end of line.
    """
    out = V.redact(line)
    for fragment in secret:
        assert fragment not in out, out


def test_redaction_stops_at_end_of_line_not_end_of_text():
    """One stray quote must not blind every diagnostic that follows it."""
    blob = ('x.service:3: Ignoring invalid environment assignment: AUTH="s3cr3t\n'
            "x.service:4: Unknown section 'Bogus'. Ignoring.\n")
    out = V.redact(blob)
    assert "s3cr3t" not in out
    assert "Unknown section 'Bogus'" in out
