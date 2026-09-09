"""Certify that production's systemd units are valid — by asking real systemd.

Why this exists
---------------
The scheduler certifier (``scheduler``) answers one question: which executable
paths can this configuration invoke, and are they the approved release? It
deliberately does **not** answer "would systemd accept this unit?".

An earlier revision did try to answer that, by emulating systemd's acceptance
rules. That emulation drew seven consecutive review findings — including two
false *rejections* that would have blocked a correct cutover — and was removed
rather than extended, because reproducing another program's acceptance
semantics from documentation is a losing game when the program itself is
available to ask. Removing it left the question genuinely uncovered::

    systemd-analyze verify      ->  SYSTEMD_UNIT_VALIDITY      <- this module
    scheduler certifier         ->  SCHEDULER_ALIGNMENT
    release pointer             ->  RELEASE_POINTER_IDENTITY

Certification requires all three. **None of them may infer another.** A unit
can be perfectly valid and still execute the wrong release; it can name the
right release and still be a unit systemd refuses to load.

This module closes the validity gap by *orchestrating and recording the real
verifier*. It contains no systemd parser and adds no acceptance rules of its
own. Everything it decides is derived from evidence a caller captured from the
host: ``systemctl show`` provenance and the real ``systemd-analyze verify``
exit status.

The verifier invocation, established empirically
-----------------------------------------------
Every choice below was measured against the installed systemd (255) with
disposable units, not inferred from documentation.

**Invoke by unit NAME in the normal search path**, so drop-ins apply with
normal precedence. Proven in both directions: a valid base broken by its
drop-in fails and names the drop-in's binary; a broken base repaired by its
drop-in passes silently. A gate that validated only the base fragment would
have missed both, and every production application unit here carries a
``zz-release.conf`` drop-in.

**Pass ``--recursive-errors=no``.** This is not a weakening — it is what makes
the exit status trustworthy at all. Per ``systemd-analyze(1)``: "If this option
is not specified, zero is returned as the exit status regardless whether
warnings arise during verification or not." Measured::

    unit                       (none)   no    one   yes
    pb-valid                     0      0     0     0
    pb-badval  (bad TimeoutStartSec)    0      1     1     1
    pb-badsec  (unknown section)        0      1     1     1
    pb-clean-with-dep                   0      0     0     1

The first two rows are the M23B trap: the verifier complains on stderr and
still exits 0. Without the flag this gate would pass malformed units.

The last row is why the mode is ``no`` and not ``yes``: ``pb-clean-with-dep``
is itself clean and merely ``Requires=`` a unit that has a warning. Under
``yes`` it fails. Production units depend on ``network-online.target`` and
similar, so ``yes`` would manufacture failures owned by unrelated system units.
``no`` still returns non-zero for warnings on *the specified unit*, which is
exactly the scope of this gate.

``--man=no`` is deliberately **not** passed. It changed no verdict in any
measured case, so there is no evidence it is needed, and suppressing a class of
check to make a gate greener is how gates stop meaning anything. If a
production unit ever trips on a man-page check, that is reported, not silenced.

Why loaded-state provenance is part of the verdict
--------------------------------------------------
``systemd-analyze verify`` validates unit files **on disk**. If PID 1 reports
``NeedDaemonReload=yes``, the on-disk text is not what the manager currently
has loaded, so a clean verifier result would be evidence about a configuration
that is not running. That is ``NOT_CERTIFIABLE``, never ``PASS`` — and the
remedy is escalation, not a daemon-reload, which would be a production
mutation performed to make a certification pass.

Fixture-driven by design, in the manner of ``pointer``: nothing here reads the
host, shells out, or imports subprocess. Callers capture evidence read-only and
hand it in, so the whole contract is testable without a VPS, root, or systemd.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

#: Schema identity for the durable artifact. Bump when the shape changes.
SCHEMA = "northstar.systemd_unit_validity"
SCHEMA_VERSION = 1

#: The verifier this gate is defined in terms of.
VERIFIER = "systemd-analyze"
VERIFIER_SUBCOMMAND = "verify"
#: Required because the exit status is otherwise meaningless — see the module
#: docstring for the measurements. A command lacking it is not this gate.
REQUIRED_VERIFIER_FLAG = "--recursive-errors=no"

#: Every artifact payload in this repository carries ``observe_only: true``
#: (AGENTS.md, "Required Behavior Before Code Changes"). It is hardcoded, not a
#: parameter: this gate reports on production and has no authority to change it,
#: and a downstream consumer must be able to see that from the payload alone.
OBSERVE_ONLY = True

#: Verdicts. "Could not verify" is never PASS.
PASS = "PASS"
FAIL = "FAIL"
NOT_CERTIFIABLE = "NOT_CERTIFIABLE"

#: The minimum systemd major version whose behaviour was actually measured.
#: Older versions are refused rather than assumed: --recursive-errors= was
#: added in 250, and this gate's semantics were established on 255.
MIN_MEASURED_SYSTEMD_MAJOR = 250

#: Collection time must be a real UTC stamp, not whatever the shell produced.
#: Parsed as a calendar date rather than pattern-matched: ``2026-99-99T99:99:99Z``
#: is shape-correct and impossible, and an impossible collection time cannot
#: establish when a certificate was taken.
def _is_utc_timestamp(value: str) -> bool:
    try:
        datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return False
    return True

#: Redaction is driven by assignment SHAPE, not by variable name. A keyword
#: allowlist cannot enumerate every credential: ``DATABASE_URL=`` carries a
#: password and matches no keyword, and its value may contain spaces and
#: punctuation that defeat an opaque-token heuristic. So ANY quoted assignment
#: value is redacted whole, whatever it is called.
#:
#: Deliberately scoped to *assignments*: a bare quoted string elsewhere in a
#: diagnostic (``Unknown section 'Bogus'. Ignoring.``) is not a value and is
#: left readable, because a gate whose evidence says nothing is its own problem.
#: Escape-aware: ``API_KEY="alpha\\" beta gamma"`` is ONE value, and a pattern
#: that stops at the escaped quote leaves ``beta gamma"`` behind.
_QUOTED_ASSIGNMENT = re.compile(
    r"""\b[A-Za-z_][A-Za-z0-9_]*\s*=\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
)
#: BARE assignment values, redacted without consulting a name vocabulary:
#: ``AUTH=hunter2`` is a credential and matches no keyword list worth
#: maintaining. The negative lookahead keeps systemd's own prose intact, where
#: a directive name is followed by punctuation rather than a value --
#: ``Service has no ExecStart=, ExecStop=, or SuccessAction=. Refusing.``
_BARE_ASSIGNMENT = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\s*=(?![\s,.;:)\]}])(\S+)"
)
#: Credentials embedded in a URL, which carry the secret in the value itself.
_CREDENTIAL_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/@]*:[^\s/@]*@\S*")
_LONG_OPAQUE = re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}\b")
_REDACTED = "<redacted>"

#: Verifier output is diagnostic only and is bounded before it is recorded.
MAX_MESSAGE_LINES = 12
MAX_MESSAGE_CHARS = 400


def redact(text: str) -> str:
    """Strip anything secret-shaped from host output before it is recorded.

    The artifact is meant to be readable by a GUI and a controller, so it must
    never become a side channel for credentials. Verifier output is diagnostic
    text from the host, so it is treated as untrusted for this purpose.
    """
    cleaned = _QUOTED_ASSIGNMENT.sub(_REDACTED, text)
    cleaned = _CREDENTIAL_URL.sub(_REDACTED, cleaned)
    cleaned = _BARE_ASSIGNMENT.sub(_REDACTED, cleaned)
    return _LONG_OPAQUE.sub(_REDACTED, cleaned)


def _messages(text: str) -> tuple[str, ...]:
    lines = [redact(line).strip() for line in (text or "").splitlines()]
    return tuple(line[:MAX_MESSAGE_CHARS] for line in lines if line)[:MAX_MESSAGE_LINES]


def build_verifier_command(unit: str) -> tuple[str, ...]:
    """The exact command this gate is defined in terms of.

    By NAME, not by path, so drop-ins resolve with normal precedence.
    """
    return (VERIFIER, VERIFIER_SUBCOMMAND, REQUIRED_VERIFIER_FLAG, unit)


@dataclass(frozen=True)
class UnitProvenance:
    """What PID 1 says about a unit, captured read-only via ``systemctl show``."""

    unit: str
    load_state: str
    fragment_path: str = ""
    drop_in_paths: tuple[str, ...] = field(default_factory=tuple)
    #: Tri-state on purpose. ``None`` means the capture never established the
    #: fact, which is not the same as establishing that no reload is pending.
    #: Defaulting absence to ``False`` would let a partial capture certify the
    #: very property this gate exists to prove.
    need_daemon_reload: bool | None = None
    load_error: str = ""

    @property
    def is_loaded(self) -> bool:
        return self.load_state == "loaded"


@dataclass(frozen=True)
class VerifierOutcome:
    """One real ``systemd-analyze verify`` invocation and its process status.

    ``exit_status`` is the *only* thing that decides pass/fail here. Output is
    recorded for humans and never consulted for the verdict: a gate that reads
    stdout can be talked out of a failure by text, and this one cannot.
    """

    unit: str
    command: tuple[str, ...]
    exit_status: int
    output: str = ""

    @property
    def succeeded(self) -> bool:
        return self.exit_status == 0

    @property
    def uses_required_flag(self) -> bool:
        return REQUIRED_VERIFIER_FLAG in self.command

    @property
    def is_the_expected_command(self) -> bool:
        """The whole argv must match, not merely contain the required flag.

        Membership alone would accept ``true --recursive-errors=no`` (which
        exits 0 having verified nothing) or a command that verified a DIFFERENT
        unit and was filed under this one. The operand is material: on systemd
        255 ``systemd-analyze --help`` documents ``verify FILE...``, so the
        target is part of what the exit status is about.
        """
        return tuple(self.command) == build_verifier_command(self.unit)


def parse_show_properties(text: str) -> dict[str, str]:
    """Parse ``systemctl show`` ``KEY=VALUE`` output.

    This parses the *verifier's own output format*, not unit files. It is not a
    systemd configuration parser and must never grow into one.
    """
    props: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key.strip()] = value.strip()
    return props


def provenance_from_show(text: str, *, unit: str) -> UnitProvenance:
    """Build provenance from captured ``systemctl show`` output."""
    props = parse_show_properties(text)
    drop_ins = tuple(p for p in props.get("DropInPaths", "").split() if p)
    return UnitProvenance(
        unit=props.get("Id") or unit,
        load_state=props.get("LoadState", ""),
        fragment_path=props.get("FragmentPath", ""),
        drop_in_paths=drop_ins,
        need_daemon_reload=_tri_state_bool(props.get("NeedDaemonReload")),
        load_error=props.get("LoadError", ""),
    )


def _tri_state_bool(value: str | None) -> bool | None:
    """``yes``/``no`` to a bool; anything else (including absence) to ``None``."""
    if value is None:
        return None
    token = value.strip().lower()
    if token == "yes":
        return True
    if token == "no":
        return False
    return None


def systemd_major(version: str) -> int | None:
    """Leading integer of a ``systemd-analyze --version`` banner, or None."""
    match = re.search(r"\b(\d+)\b", version or "")
    return int(match.group(1)) if match else None


def certify_systemd_unit_validity(
    *,
    expected_units: tuple[str, ...],
    discovered_units: tuple[str, ...],
    provenance: dict[str, UnitProvenance],
    outcomes: dict[str, VerifierOutcome],
    systemd_version: str,
    checked_at: str,
    host: str,
    verifier_available: bool = True,
    classified_units: tuple[str, ...] = (),
    optional_units: tuple[str, ...] = (),
    malformed_evidence: tuple[str, ...] = (),
) -> dict:
    """Combine inventory, loaded state and real verifier results into a verdict.

    ``PASS`` requires every one of: at least one unit actually verified, a
    complete expected inventory, every required unit loaded, every required unit current with PID 1, a real
    verifier result for every required unit obtained with the required flag,
    every such result clean, and no relevant discovered unit left unclassified.

    ``classified_units`` are discovered units an operator has explicitly waived
    from the expected inventory. The waiver is recorded in the artifact: a
    certificate that shows a discovered unit which is neither expected nor
    unexpected, with no record of who waived it, is asking the reader to trust
    an invisible decision.

    ``optional_units`` are units that some deployments install and some do not
    (the sandbox lane is the example). Absence is not a failure; **presence is
    not a free pass** — an optional unit that exists is verified exactly like a
    required one, because a broken unit that happens to be optional is still a
    broken unit sitting in the production manager.

    Anything that prevents establishing those facts is ``NOT_CERTIFIABLE``;
    anything that establishes them as false is ``FAIL``. Neither is ``PASS``.
    """
    errors: list[str] = []
    blockers: list[str] = []          # reasons we *cannot* certify

    # Evidence the reader could not make sense of. This is deliberately a
    # blocker and not a failure: an unreadable capture and an established unit
    # failure are different facts, and collapsing them would tell an operator
    # to go fix a unit that may be perfectly fine.
    for entry in malformed_evidence:
        blockers.append(f"malformed evidence — {entry}")

    # A certificate that cannot say where or when it was taken is not evidence.
    if not (host or "").strip():
        blockers.append("no host recorded — the evidence has no provenance")
    if not _is_utc_timestamp(checked_at or ""):
        blockers.append(
            f"checked_at {checked_at!r} is not an ISO-8601 UTC timestamp — the "
            f"evidence has no trustworthy collection time"
        )

    if not expected_units:
        blockers.append(
            "no expected units supplied — an empty inventory cannot certify, "
            "because 'nothing to check' must never read as 'everything checks out'"
        )

    if not verifier_available:
        blockers.append(
            f"{VERIFIER} could not be invoked — validity is unproven, not proven"
        )

    major = systemd_major(systemd_version)
    if major is None:
        blockers.append(
            f"systemd version could not be determined from {systemd_version!r}"
        )
    elif major < MIN_MEASURED_SYSTEMD_MAJOR:
        blockers.append(
            f"systemd {major} predates the measured behaviour of "
            f"{REQUIRED_VERIFIER_FLAG} (needs >= {MIN_MEASURED_SYSTEMD_MAJOR}); "
            f"refusing to assume the exit status is meaningful"
        )

    optional = set(optional_units)
    expected = tuple(sorted(set(expected_units)))
    discovered = tuple(sorted(set(discovered_units)))
    classified = set(classified_units)

    # Required = expected minus the ones this deployment may legitimately lack.
    required = tuple(u for u in expected if u not in optional)
    missing_units = tuple(u for u in required if u not in set(discovered))
    for unit in missing_units:
        errors.append(f"{unit}: expected production unit not present on the host")

    # An optional unit that IS installed is verified like any other; only its
    # absence is tolerated.
    verified_units = tuple(sorted(
        set(required) | {u for u in expected if u in optional and u in set(discovered)}
    ))

    # The empty-inventory guard above checks the INPUT list, but the optional
    # mechanism can drain it afterwards: if every expected unit is optional and
    # none is installed, nothing is required and nothing is verified, and the
    # verdict would be PASS having run the verifier zero times. A fail-closed
    # guard has to sit on the quantity that actually decides the verdict.
    if expected_units and not verified_units:
        blockers.append(
            "no unit was verified — every expected unit is optional and absent, "
            "so this run establishes nothing. 'Nothing to check' must never "
            "read as 'everything checks out'."
        )

    unexpected_units = tuple(
        u for u in discovered if u not in set(expected) and u not in classified
    )
    for unit in unexpected_units:
        errors.append(
            f"{unit}: relevant unit is neither expected nor classified — an "
            f"unrecognised scheduler surface must not ride along inside a PASS"
        )

    unit_records: list[dict] = []
    for unit in verified_units:
        prov = provenance.get(unit)
        outcome = outcomes.get(unit)

        if prov is None:
            errors.append(f"{unit}: no loaded-state provenance captured")
        else:
            if prov.unit and prov.unit != unit:
                blockers.append(
                    f"{unit}: provenance records Id={prov.unit} — evidence for "
                    f"one unit cannot certify another, whatever section it was "
                    f"filed under"
                )
            if not prov.is_loaded:
                errors.append(
                    f"{unit}: LoadState={prov.load_state or '<empty>'} "
                    f"(required: loaded)"
                    + (f" — {prov.load_error}" if prov.load_error else "")
                )
            if prov.need_daemon_reload is None:
                blockers.append(
                    f"{unit}: NeedDaemonReload was not captured — the evidence "
                    f"never established that the on-disk unit is what PID 1 has "
                    f"loaded, and an unproven fact is not a proven one"
                )
            elif prov.need_daemon_reload:
                blockers.append(
                    f"{unit}: NeedDaemonReload=yes — the on-disk unit is not what "
                    f"PID 1 has loaded, so a clean verifier result would describe "
                    f"a configuration that is not running. Escalate; do not "
                    f"daemon-reload to make this pass."
                )

        if outcome is None:
            errors.append(
                f"{unit}: required unit was not verified — a skipped unit cannot "
                f"contribute to a PASS"
            )
        else:
            if not outcome.uses_required_flag:
                blockers.append(
                    f"{unit}: verifier ran without {REQUIRED_VERIFIER_FLAG}, so a "
                    f"zero exit status does not mean the unit is clean"
                )
            elif not outcome.is_the_expected_command:
                blockers.append(
                    f"{unit}: recorded command {' '.join(outcome.command)!r} is "
                    f"not {' '.join(build_verifier_command(unit))!r} — an exit "
                    f"status only certifies the command that produced it"
                )
            elif not outcome.succeeded:
                errors.append(
                    f"{unit}: {VERIFIER} {VERIFIER_SUBCOMMAND} exited "
                    f"{outcome.exit_status}"
                )

        unit_records.append({
            "unit": unit,
            "fragment_path": prov.fragment_path if prov else None,
            "drop_in_paths": list(prov.drop_in_paths) if prov else None,
            "load_state": prov.load_state if prov else None,
            "need_daemon_reload": prov.need_daemon_reload if prov else None,
            "verifier_command": list(outcome.command) if outcome else None,
            "verifier_exit_status": outcome.exit_status if outcome else None,
            "verifier_result": (
                None if outcome is None else (PASS if outcome.succeeded else FAIL)
            ),
            "verifier_messages": list(_messages(outcome.output)) if outcome else [],
        })

    if blockers:
        verdict = NOT_CERTIFIABLE
    elif errors:
        verdict = FAIL
    else:
        verdict = PASS

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "observe_only": OBSERVE_ONLY,
        "checked_at": checked_at,
        "host": host,
        "systemd_version": redact(systemd_version),
        "verifier_flag": REQUIRED_VERIFIER_FLAG,
        "expected_units": list(expected),
        "optional_units": sorted(optional),
        "classified_units": sorted(classified),
        "verified_units": list(verified_units),
        "discovered_units": list(discovered),
        "missing_units": list(missing_units),
        "unexpected_units": list(unexpected_units),
        "units": unit_records,
        "errors": errors,
        "blockers": blockers,
        "SYSTEMD_UNIT_VALIDITY": verdict,
    }
