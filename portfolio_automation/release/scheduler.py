"""Scheduler identity: every production workload must resolve to one release.

Why this exists
---------------
The first cutover design repointed only the two systemd GUI units. That is not
sufficient. The live host also drives production through **root cron**, which
invokes ``/opt/stockbot/scripts/*`` on thirteen separate schedules — daily,
weekly, monthly, yearly and a weekly doc-audit. Repointing systemd alone would
have left the entire batch lane executing legacy code while the GUI served the
approved release: a mixed-release production, which is worse than either
release alone because no single SHA describes what ran.

So certification must cover all three schedulers together:

    every StockBot systemd ExecStart
  + every path-bearing systemd directive (WorkingDirectory, EnvironmentFile)
  + every active StockBot cron entry
  + every StockBot timer/service

``ExecStart`` alone is not enough, and the live dashboard unit is the proof.
It reads::

    WorkingDirectory=/opt/stockbot
    ExecStart=/opt/stockbot/current/.venv/bin/uvicorn gui_v2.app:app

``uvicorn gui_v2.app:app`` names the application module **relatively**, so
Python resolves ``gui_v2`` from the process working directory. That unit runs
the release interpreter against **legacy application code**, and an
``ExecStart``-only parser certifies it as aligned. Path-bearing directives are
therefore part of the execution surface, not metadata.

Secrets are classified separately. ``EnvironmentFile=/opt/stockbot/.env`` is a
credential file, not release code; it must be *surfaced* when it sits outside
the release (the cutover has to re-provision it) but it is not application code
drift and is never treated as such.

Release path model
------------------
``DIRECT``   every scheduler names ``/opt/stockbot/releases/<sha>`` explicitly.
             Code identity is maximally explicit, but every release edits
             fifteen-plus scheduler entries, and a partial edit yields exactly
             the mixed-release state above.

``POINTER``  one stable ``/opt/stockbot/current`` symlink to
             ``releases/<sha>``; schedulers reference only ``current``.
             A release is one atomic symlink swap, rollback is the reverse
             swap, and mixed-release execution is impossible because there is
             a single switch. Old releases are never mutated.

``POINTER`` is recommended, and ``resolves_to_release`` accepts either shape so
a host mid-migration can be certified truthfully.

Path alignment is necessary but **not sufficient** for release identity: it
proves only that scheduler strings sit beneath ``current``, never that
``current`` points at the approved SHA. That proof lives in
``portfolio_automation.release.pointer`` and both are required — see
``certify_release_identity``.

Everything here is pure text analysis over caller-supplied unit and crontab
content. It never shells out to systemd or cron and never reads the live host,
so it is fully testable from fixtures.

Responsibility split
--------------------
This module answers exactly one question::

    what effective executable/application paths can this scheduler
    configuration invoke, and are they aligned with the approved release?

It deliberately does **not** answer "would systemd accept this unit?". Unit
validity — service types, how many start commands a type permits, whether a
unit without ``ExecStart`` may run — belongs to ``systemd-analyze verify``,
run against the real host units::

    systemd-analyze verify      ->  SYSTEMD_UNIT_VALIDITY
    this scheduler certifier    ->  SCHEDULER_ALIGNMENT

Certification requires **both**, and neither substitutes for the other. Note
that the cutover runbook does **not** yet invoke the verifier; until it does,
SYSTEMD_UNIT_VALIDITY is uncovered, and that gap is not closed by this module
pretending to cover it.

An earlier revision did emulate systemd validity. That emulation drew seven
consecutive review findings — including two false *rejections* that would have
blocked a correct cutover — because reproducing another program's acceptance
semantics from documentation is a losing game when the program itself is
available to ask. It was removed rather than extended. If a validity question
arises, run the verifier.
"""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .observation import (ObservationContext, bind_observations,
                          observation_of)

DIRECT = "DIRECT"
POINTER = "POINTER"
RECOMMENDED_MODEL = POINTER

# Conventional layout on the production host.
RELEASES_DIR = "/opt/stockbot/releases"
#: Reported when no ``systemd-analyze verify`` evidence was supplied. It is a
#: distinct value from FAIL on purpose: "nobody checked" and "the units are
#: broken" are different facts, and neither is eligibility.
VALIDITY_NOT_ESTABLISHED = "NOT_ESTABLISHED"

#: Execution-surface origins are ``"<kind>:<name>"``; only systemd origins name
#: a unit that the validity gate can have verified. Cron surfaces have no unit.
SYSTEMD_ORIGIN_PREFIX = "systemd"

CURRENT_POINTER = "/opt/stockbot/current"
LEGACY_CHECKOUT = "/opt/stockbot"

# Executables that are system/external infrastructure rather than StockBot
# release code. These are allowed not to resolve to a release, but must be
# declared explicitly rather than silently skipped.
SYSTEM_TRANSITIONAL: tuple[str, ...] = (
    "/usr/local/bin/cloudflared",
    "/usr/local/sbin/stockbot-engineer-read",
    "/usr/local/sbin/stockbot-evidence-publish",
)

#: Directives whose values are paths that affect application identity.
#: ExecStop/ExecStopPost are included because they run release code just as
#: ExecStart does — a stop command still pointing at the legacy checkout is
#: legacy code executing on the host, which is exactly what this module exists
#: to detect. How MANY entries each directive may legally carry is a validity
#: question and is deliberately not modelled here.
EXEC_DIRECTIVES = ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecReload",
                   "ExecStop", "ExecStopPost")
#: Both affect code identity, but they are INDEPENDENT settings: resetting one
#: must not discard the other. They are tracked separately in
#: ``parse_systemd_unit`` and both participate in the code-identity check.
PATH_DIRECTIVES = ("WorkingDirectory", "RootDirectory")
WORKDIR_DIRECTIVE = "WorkingDirectory"
ROOTDIR_DIRECTIVE = "RootDirectory"
#: ``RootDirectoryStartOnly=yes`` applies the chroot to ``ExecStart`` ONLY;
#: ExecStartPre/Post, ExecReload, ExecStop and ExecStopPost keep resolving on
#: the host (``systemd.service(5)``, default false).
#:
#: An unparseable assignment — including a BLANK one — is ignored by systemd,
#: leaving the previous effective value in place. Verified against the
#: installed systemd 255 by asking it rather than reading about it::
#:
#:     RootDirectoryStartOnly=yes + RootDirectoryStartOnly=garbage -> yes
#:     RootDirectoryStartOnly=yes + RootDirectoryStartOnly=        -> yes
#:     RootDirectoryStartOnly=yes + RootDirectoryStartOnly=no      -> no
#:
#: (``systemctl --user show -p RootDirectoryStartOnly``; the ignored lines log
#: "Failed to parse boolean value, ignoring".) Note that blank does NOT reset
#: this scalar to its default, so "last assignment wins" is wrong here.
#:
#: Getting this wrong is a false-APPROVE risk in BOTH directions, which is why
#: the effective value has to be modelled rather than approximated. Applying a
#: root that does not apply is not merely a false reject: ``_in_root`` rewrites
#: a legacy host path such as ``/legacy/stop.sh`` into
#: ``/opt/stockbot/current/legacy/stop.sh``, which sits under the release root
#: and therefore PASSES — laundering legacy code into release-looking code.
ROOTDIR_START_ONLY_DIRECTIVE = "RootDirectoryStartOnly"
#: systemd's boolean vocabulary (``parse_boolean``).
_BOOL_TRUE = frozenset({"1", "yes", "true", "on"})
_BOOL_FALSE = frozenset({"0", "no", "false", "off"})


def _parse_systemd_bool(value: str) -> bool | None:
    """systemd's ``parse_boolean``: ``None`` when systemd would ignore it."""
    token = value.strip().lower()
    if token in _BOOL_TRUE:
        return True
    if token in _BOOL_FALSE:
        return False
    return None
SECRET_DIRECTIVES = ("EnvironmentFile",)
#: Every directive this module reads is a [Service] key. systemd ignores them
#: in any other section ("Unknown key name 'ExecStart' in section 'Unit',
#: ignoring"), so the parser must be section-aware or it will treat a directive
#: systemd discards as a live execution surface.
SERVICE_SECTION = "Service"

_SECTION_HEADER = re.compile(r"^\[(?P<name>[^\]]+)\]$")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A 5-field cron schedule, or an @-shortcut such as @daily.
_CRON_SCHEDULE = re.compile(r"^(?:@\w+|(?:\S+\s+){4}\S+)\s+")


class SchedulerParseError(ValueError):
    """Raised when scheduler content cannot be parsed.

    Parsing failure must never be reported as "no problems found" — an
    unparseable unit is an unknown execution surface, so callers fail closed.
    """


def _under(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` itself or lies beneath it.

    String prefixing alone is wrong: ``/opt/stockbot/current-old`` is not under
    ``/opt/stockbot/current``. Compare path components.
    """
    p = PurePosixPath(path)
    r = PurePosixPath(str(root).rstrip("/"))
    return p == r or r in p.parents


def _looks_like_path(token: str) -> bool:
    return token.startswith("/")


@dataclass(frozen=True)
class ExecutionSurface:
    """One thing production executes on a schedule, and every path it binds."""

    origin: str                 # e.g. "systemd:stockbot-dashboard.service"
    raw: str                    # the raw ExecStart / cron command
    executable: str             # resolved leading executable path
    referenced_paths: tuple[str, ...] = field(default_factory=tuple)
    #: WorkingDirectory. Decides where Python resolves a relatively-named
    #: application module, so it is code identity.
    working_directory: str | None = None
    #: RootDirectory. Independent of WorkingDirectory: a process chrooted under
    #: the legacy checkout runs legacy code even when its executable and
    #: working directory both name the release, so this is code identity too.
    root_directory: str | None = None
    #: EnvironmentFile paths. Credentials, not release code.
    environment_files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_system_transitional(self) -> bool:
        """Allowlisted host binary — compared AFTER chroot resolution.

        The allowlist names host paths such as ``/usr/local/bin/cloudflared``.
        Under ``RootDirectory=/opt/stockbot`` the binary systemd actually
        executes is ``/opt/stockbot/usr/local/bin/cloudflared``, which lives in
        the legacy checkout and is *not* the approved host binary. Matching the
        lexical executable would let the allowlist short-circuit the whole
        code-path check and certify legacy code as approved.
        """
        return self._in_root(self.executable) in SYSTEM_TRANSITIONAL

    def _in_root(self, path: str) -> str:
        """Resolve *path* to the host path the process actually reaches.

        ``root_directory`` is the root this command EFFECTIVELY runs under —
        ``parse_systemd_unit`` already accounts for ``RootDirectoryStartOnly=``
        — so this method does not re-derive it.

        ``RootDirectory=`` is implemented with ``chroot(2)``
        (``systemd.exec(5)``), so a unit with ``RootDirectory=/srv/jail`` and
        ``ExecStart=/opt/stockbot/current/scripts/x.sh`` executes the HOST path
        ``/srv/jail/opt/stockbot/current/scripts/x.sh`` — which has nothing to
        do with the approved release pointer. Comparing the lexical
        ``ExecStart`` value against the release root would approve it.

        ``WorkingDirectory=`` is likewise interpreted inside the root, so it
        gets the same treatment.
        """
        if not self.root_directory:
            return path
        return posixpath.join(self.root_directory, path.lstrip("/"))

    def _code_paths(self) -> tuple[str, ...]:
        """Every host path that determines which application code runs.

        Deliberately excludes environment_files: a credential file is not code.
        """
        paths = [self._in_root(self.executable)]
        paths += [self._in_root(p) for p in self.referenced_paths]
        if self.working_directory:
            paths.append(self._in_root(self.working_directory))
        if self.root_directory:
            # The root itself is a host path and is compared as one.
            paths.append(self.root_directory)
        return tuple(paths)

    def legacy_paths(self, *, release_root: str) -> tuple[str, ...]:
        """Code paths that keep this surface bound to the legacy checkout."""
        if self.is_system_transitional:
            return ()
        return tuple(
            p for p in self._code_paths()
            if p.startswith(LEGACY_CHECKOUT) and not _under(p, release_root)
        )

    def legacy_secret_paths(self, *, release_root: str) -> tuple[str, ...]:
        """Secret paths outside the release — surfaced, not counted as drift."""
        return tuple(
            p for p in self.environment_files
            if p.startswith(LEGACY_CHECKOUT) and not _under(p, release_root)
        )

    def resolves_to_release(self, *, release_root: str) -> bool:
        """True when every code path this surface binds lives in the release.

        ``release_root`` may be a concrete ``releases/<sha>`` directory (DIRECT)
        or the stable ``current`` pointer (POINTER); both are accepted.
        """
        if self.is_system_transitional:
            return True
        code = self._code_paths()
        stockbot_paths = [p for p in code if p.startswith(LEGACY_CHECKOUT)]
        if not stockbot_paths:
            # Nothing under /opt/stockbot at all: not a release surface.
            return False
        return not self.legacy_paths(release_root=release_root)


def _split_command(command: str, *, origin: str) -> tuple[str, tuple[str, ...]]:
    """Return (executable, other absolute paths) from a command line."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError as exc:
        raise SchedulerParseError(f"{origin}: unparseable command: {exc}") from exc
    tokens = [t for t in tokens if t]
    if not tokens:
        raise SchedulerParseError(f"{origin}: empty command")
    # systemd ExecStart prefixes ('-', '@', '+', '!') alter execution but not
    # the path we care about.
    exe = tokens[0].lstrip("-@+!:")
    rest = tuple(t for t in tokens[1:] if _looks_like_path(t))
    # An interpreter invocation ("/bin/bash /opt/stockbot/scripts/x.sh") hides
    # the real release binding in argv[1]; promote it.
    if exe in ("/bin/sh", "/bin/bash", "/usr/bin/env", "/usr/bin/python3") and rest:
        return rest[0], tuple(rest[1:])
    return exe, rest


def _logical_lines(content: str) -> list[str]:
    """Join systemd backslash continuations into logical directive lines.

    The live streamlit unit puts ``--server.address 127.0.0.1`` on a
    continuation line; a parser that reads lines independently sees an
    ExecStart with no bind address and would report loopback hardening as
    absent.
    """
    out: list[str] = []
    buffer = ""
    for line in content.splitlines():
        stripped = line.strip()
        if buffer:
            buffer = buffer + " " + stripped.rstrip("\\").strip()
        elif stripped.startswith("#") or stripped.startswith(";") or not stripped:
            continue
        else:
            buffer = stripped.rstrip("\\").strip()
        if line.rstrip().endswith("\\"):
            continue
        out.append(buffer)
        buffer = ""
    if buffer:
        out.append(buffer)
    return out


def parse_systemd_unit(content: str, *, origin: str) -> list[ExecutionSurface]:
    """Extract the effective execution surfaces from systemd unit *content*.

    The question this answers is narrow and deliberate:

        which executable/application paths can this unit actually invoke?

    It is **not** "would systemd accept this unit?" — that is unit *validity*
    and belongs to ``systemd-analyze verify``. See the responsibility split in
    the module docstring.

    Effective state, not textual assignments
    ---------------------------------------
    systemd list directives support reset::

        ExecStart=/legacy/a
        ExecStart=            <- clears the accumulated list
        ExecStart=/opt/stockbot/current/new

    which yields exactly one effective command. That is how a drop-in replaces
    an inherited command, and it is what the production release drop-ins use.

    Skipping blank assignments would be worse than mis-parsing them: on
    concatenated ``systemctl cat`` output the inherited legacy command would
    survive into the effective list, and the certifier would report a legacy
    execution path that systemd does not actually run.

    Reset semantics implemented:

    * ``Exec*``            list directives (``ExecStart``, ``ExecStartPre``,
                           ``ExecStartPost``, ``ExecReload``, ``ExecStop``,
                           ``ExecStopPost``); blank clears, non-blank appends.
                           Stop commands are included because they execute
                           release code just as start commands do — a stop
                           command still pointing at the legacy checkout is
                           legacy code running on the host.
    * ``EnvironmentFile``  list directive; blank clears, non-blank appends.
    * ``WorkingDirectory`` and ``RootDirectory``  not lists; last non-blank
                           wins, blank resets to unset. Tracked
                           **independently**: resetting the working directory
                           must not discard a still-effective root directory,
                           or a process chrooted under the legacy checkout
                           would certify as release-aligned.

    All are ``[Service]`` keys and the parser is **section-aware**. systemd
    ignores them elsewhere (``Unknown key name 'ExecStart' in section 'Unit',
    ignoring``), so a directive outside ``[Service]`` must not become an
    execution surface.

    Each surface inherits the unit's effective ``WorkingDirectory``,
    ``RootDirectory`` and ``EnvironmentFile``, because the first two decide
    which application code a relatively-named module resolves to and the third
    is classified separately as external configuration.

    Fail-closed behaviour
    ---------------------
    A malformed **non-empty** command still raises: an unparseable command is
    an unknown execution surface.

    A unit that yields **no** executable surfaces — a bare ``EnvironmentFile``
    drop-in fragment, or content whose ``Exec*`` directives all sit outside
    ``[Service]`` — parses to an empty list rather than raising. That is
    correct for a *parser*: a fragment genuinely declares no commands. The
    guard that matters lives at the certification layer instead, where
    ``certify_scheduler_identity`` requires each **expected production
    service** to contribute at least one surface, so a service that silently
    yields nothing cannot be aggregated into a PASS.
    """
    # (section, key, value) in file order, so only [Service] directives count.
    entries: list[tuple[str, str, str]] = []
    section: str | None = None
    for entry in _logical_lines(content):
        header = _SECTION_HEADER.match(entry)
        if header:
            section = header.group("name").strip()
            continue
        if "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        entries.append((section or "", key.strip(), value.strip()))

    working_dir: str | None = None
    root_dir: str | None = None
    root_start_only = False
    env_files: list[str] = []
    exec_lists: dict[str, list[str]] = {d: [] for d in EXEC_DIRECTIVES}

    for entry_section, key, value in entries:
        if entry_section != SERVICE_SECTION:
            # systemd: "Unknown key name '<k>' in section '<s>', ignoring."
            continue
        if key in EXEC_DIRECTIVES:
            if not value:
                exec_lists[key].clear()      # reset, not an error, not a command
            else:
                exec_lists[key].append(value)
        elif key == WORKDIR_DIRECTIVE:
            working_dir = value.split()[0] if value else None
        elif key == ROOTDIR_DIRECTIVE:
            # Independent of WorkingDirectory — see the docstring.
            root_dir = value.split()[0] if value else None
        elif key == ROOTDIR_START_ONLY_DIRECTIVE:
            parsed = _parse_systemd_bool(value)
            if parsed is not None:
                root_start_only = parsed
            # else: systemd logs "Failed to parse boolean value, ignoring" and
            # keeps the previous effective value, so this must not clobber it.
        elif key in SECRET_DIRECTIVES:
            if not value:
                env_files.clear()            # EnvironmentFile is also a list
            else:
                # systemd allows a leading '-' meaning "optional".
                env_files.append(value.lstrip("-").split()[0])

    surfaces: list[ExecutionSurface] = []
    for directive in EXEC_DIRECTIVES:
        # The EFFECTIVE root for this command, which is what the surface must
        # carry: under RootDirectoryStartOnly=yes a non-start command is not
        # chrooted at all, so its paths resolve on the host.
        effective_root = (
            None if (root_start_only and directive != "ExecStart") else root_dir
        )
        for value in exec_lists[directive]:
            exe, rest = _split_command(value, origin=origin)
            surfaces.append(ExecutionSurface(
                origin=origin, raw=value, executable=exe, referenced_paths=rest,
                working_directory=working_dir, root_directory=effective_root,
                environment_files=tuple(env_files),
            ))
    return surfaces


def parse_crontab(content: str, *, origin: str = "cron") -> list[ExecutionSurface]:
    """Extract execution surfaces from crontab *content*.

    Skips comments and ``VAR=value`` assignments (``SHELL=``, ``PATH=``), strips
    the five schedule fields and any ``/etc/cron.d`` user field, and drops
    redirections so a log path on the right of ``>>`` is not mistaken for
    executed code.
    """
    surfaces: list[ExecutionSurface] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _ENV_ASSIGN.match(stripped):
            continue
        match = _CRON_SCHEDULE.match(stripped)
        if not match:
            raise SchedulerParseError(
                f"{origin}:{lineno}: not a schedule or assignment: {stripped!r}")
        command = stripped[match.end():].strip()
        if not command:
            raise SchedulerParseError(f"{origin}:{lineno}: schedule with no command")
        tokens = command.split()
        if tokens and not _looks_like_path(tokens[0]) and len(tokens) > 1 \
                and _looks_like_path(tokens[1]):
            command = " ".join(tokens[1:])
        command = re.split(r"\s(?:\d?>>?|2>&1)", command)[0].strip()
        exe, rest = _split_command(command, origin=f"{origin}:{lineno}")
        surfaces.append(ExecutionSurface(origin=f"{origin}:{lineno}", raw=command,
                                         executable=exe, referenced_paths=rest))
    return surfaces


def unresolved_surfaces(surfaces: list[ExecutionSurface], *,
                        release_root: str) -> list[ExecutionSurface]:
    """Surfaces that would still execute legacy code after a cutover."""
    return [s for s in surfaces if not s.resolves_to_release(release_root=release_root)]


def certify_scheduler_identity(surfaces: list[ExecutionSurface], *,
                               release_root: str,
                               expected_origins: tuple[str, ...] | None = None,
                               observation: ObservationContext | None = None,
                               ) -> dict:
    """Report whether every surface's code paths resolve to the approved release.

    Fails closed on an empty surface list: "we found nothing to check" must not
    be reportable as "everything checks out".

    ``expected_origins`` names the production services that MUST each
    contribute at least one executable surface. This is where the no-surface
    guard belongs: a parser may legitimately return nothing for a drop-in
    fragment, but a known production service that yields nothing has been
    misconfigured or mis-parsed and must not be aggregated silently into a PASS
    alongside aligned surfaces from other units and cron.

    This certifies **path alignment only**. It cannot establish
    ``production_code_sha == approved_release_sha`` — see ``pointer`` and
    ``certify_release_identity``.
    """
    # Provenance rides along with the verdict rather than beside it, so a
    # result cannot be separated from the observation that produced it while
    # being passed between processes, files or hosts.
    provenance = (observation or ObservationContext("", "")).as_dict()

    if not surfaces:
        return {"status": "FAILED",
                "errors": ["no execution surfaces supplied — cannot certify"],
                "unresolved": [], "system_transitional": [],
                "secret_paths_outside_release": [],
                "missing_expected": list(expected_origins or ()),
                **provenance}

    missing_expected: list[str] = []
    if expected_origins:
        present = {s.origin for s in surfaces}
        missing_expected = [
            want for want in expected_origins
            if not any(o == want or o.startswith(f"{want}:") for o in present)
        ]
    unresolved = unresolved_surfaces(surfaces, release_root=release_root)
    secrets: list[str] = []
    for s in surfaces:
        for p in s.legacy_secret_paths(release_root=release_root):
            secrets.append(f"{s.origin}: {p}")
    return {
        "status": "OK" if not unresolved and not missing_expected else "FAILED",
        "errors": [
            f"{s.origin} does not resolve to the release: "
            f"{', '.join(s.legacy_paths(release_root=release_root)) or s.executable}"
            for s in unresolved
        ] + [
            f"{want} contributed no executable surface — a known production "
            f"service cannot silently contribute a PASS"
            for want in missing_expected
        ],
        "missing_expected": missing_expected,
        "unresolved": [s.origin for s in unresolved],
        "system_transitional": [s.origin for s in surfaces if s.is_system_transitional],
        # Surfaced, never counted as code drift: a credential file is not code,
        # but the cutover still has to re-provision it.
        "secret_paths_outside_release": secrets,
        "scope": "path_alignment_only",
        **provenance,
    }


def certify_release_identity(surfaces: list[ExecutionSurface], *,
                             pointer_result: dict,
                             release_root: str = CURRENT_POINTER,
                             expected_origins: tuple[str, ...] | None = None,
                             validity_result: dict | None = None,
                             expected_validity_units: tuple[str, ...] | None = None,
                             observation: ObservationContext | None = None,
                             ) -> dict:
    """Path alignment AND pointer-target SHA together.

    Either alone is insufficient. Aligned schedulers under a ``current`` that
    targets an unapproved release is precisely the failure this guards, and so
    is an approved pointer that half the schedulers ignore.

    ``expected_origins`` is forwarded to ``certify_scheduler_identity``. It has
    to be: this is the real certification entry point, and a missing-service
    guard reachable only by direct callers of the inner function is a guard
    that production certification never actually runs.

    ``status`` covers SCHEDULER_ALIGNMENT and RELEASE_POINTER_IDENTITY only.
    **It is not production release identity**, because a unit can name the
    approved release perfectly and still be one systemd refuses to load. That
    third gate is ``SYSTEMD_UNIT_VALIDITY``, owned by
    ``release.systemd_validity`` and the real ``systemd-analyze verify``.

    So the aggregate is reported separately in ``production_release_identity``,
    and it is ``NOT_ESTABLISHED`` unless a validity result is actually supplied
    and passing. Absence of that evidence is never eligibility: without it this
    function cannot distinguish "the units are fine" from "nobody checked".

    A passing verdict is not enough on its own, either — it has to be a verdict
    **about the same units**. Both gates are scoped by their callers, so a
    validity artifact covering only ``stockbot-daily.service`` could otherwise
    be combined with scheduler evidence covering only
    ``stockbot-dashboard.service``, and the aggregate would claim three-gate
    coverage of a system where no unit had passed both. So the validity
    artifact's ``verified_units`` must cover every systemd unit named by
    ``expected_origins``, and without ``expected_origins`` there is nothing to
    bind the two gates together — which is itself ``NOT_ESTABLISHED``.

    Scheduler origins are not sufficient on their own, though, because they
    only ever name units that bind a *path*. A ``[Timer]`` unit produces no
    execution surface, so ``stockbot-daily.timer`` — which is what actually
    starts the daily run — can never appear in ``expected_origins`` and could
    therefore never be demanded of the validity evidence. So the deployment's
    systemd inventory is declared separately in ``expected_validity_units``,
    and the aggregate requires that too: without it, a validity artifact
    collected with a narrowed inventory could satisfy every path-bearing origin
    while leaving the timers entirely unverified.

    ``observation`` declares which production observation THIS call's inputs
    were taken from, and the aggregate refuses to combine gate results that do
    not agree on it. The three legs are not symmetric, and deliberately so:

    * ``validity_result`` and ``pointer_result`` are separately-collected
      artifacts that travel between processes, files and hosts, so each carries
      its own ``(host, observation_id)`` and is genuinely *checked* here. This
      is the leg that caught a real staging certificate.
    * the scheduler leg has no artifact to check — ``surfaces`` is raw parsed
      unit text handed straight in, so its provenance IS this parameter. The
      comparison for that leg is therefore an assertion by the caller, not an
      independent verification, and it cannot detect a caller that mislabels
      where the surfaces came from.

    That asymmetry is a property of the evidence, not an oversight: there is no
    scheduler artifact to stamp at collection time. What the binding does
    guarantee is that separately-collected evidence cannot be composed across
    hosts or runs without the mismatch surfacing.

    A declared unit counts as covered if the artifact verified it, or if the
    artifact both **expected** it and lists it as optional. Both halves matter:
    "optional and installed implies verified" only holds for units the artifact
    actually considered, so a name that appears in ``optional_units`` while
    missing from the artifact's ``expected_units`` was never a candidate for
    verification, and treating it as covered would let a narrowed collection
    wave a unit through simply by calling it optional.
    """
    sched = certify_scheduler_identity(surfaces, release_root=release_root,
                                       expected_origins=expected_origins,
                                       observation=observation)
    ok = sched["status"] == "OK" and pointer_result.get("status") == "OK"

    validity = (validity_result or {}).get("SYSTEMD_UNIT_VALIDITY",
                                           VALIDITY_NOT_ESTABLISHED)
    errors = list(sched["errors"]) + list(pointer_result.get("errors") or [])
    if validity != "PASS":
        errors.append(
            f"SYSTEMD_UNIT_VALIDITY = {validity} — production release identity "
            f"requires all three gates; scheduler alignment and pointer identity "
            f"alone cannot establish it"
        )

    # Three green gates are not one certified system unless the evidence
    # describes one observation of one host. A genuine validity PASS collected
    # on staging covers the same unit NAMES as production, so name-level
    # coverage above cannot detect it; only provenance can. Host alone is not
    # enough either -- a pointer read before a deploy and a unit verification
    # after it are both truthful about the same host and jointly describe a
    # system that never existed.
    #
    # Nothing here mints or copies an id: the aggregator proving agreement it
    # manufactured itself would be no proof at all. Evidence that cannot say
    # which run it came from is NOT_ESTABLISHED, not assumed co-located.
    unbound = bind_observations({
        "scheduler alignment": observation_of(sched),
        "release pointer identity": observation_of(pointer_result),
        "systemd unit validity": observation_of(validity_result),
    })
    # A shared observation id proves the collectors were TOLD they belong to
    # one run. It cannot prove the system held still during it: if a deployment
    # lands between the pointer reading and the unit verification, both still
    # carry the same id while describing different releases. So the gates are
    # bound to observed STATE as well as to a token -- the release the validity
    # collector saw must be the release the pointer gate certified.
    validity_release = str((validity_result or {}).get("release_pointer") or "").strip()
    pointer_release = str((pointer_result or {}).get("target_sha") or "").strip()
    if not validity_release:
        unbound.append(
            "systemd unit validity: evidence records no release pointer, so a "
            "deployment between it and the pointer gate could not be detected"
        )
    if not pointer_release:
        unbound.append(
            "release pointer identity: evidence records no target SHA, so there "
            "is no release state to bind the other gates against"
        )
    if validity_release and pointer_release and validity_release != pointer_release:
        unbound.append(
            f"the validity evidence was taken against release "
            f"{validity_release[:12]} while the pointer gate certified "
            f"{pointer_release[:12]} — a deployment landed between the two "
            f"observations, so they describe different systems however they "
            f"were labelled"
        )

    errors.extend(unbound)

    # The two gates must be about the same units, not merely both green.
    origin_units = {
        origin.split(":", 1)[1]
        for origin in (expected_origins or ())
        if origin.startswith(f"{SYSTEMD_ORIGIN_PREFIX}:")
    }
    declared_units = set(expected_validity_units or ())
    required_units = tuple(sorted(origin_units | declared_units))

    verified_units = set((validity_result or {}).get("verified_units") or ())
    # "Optional and installed implies verified" only holds for units the
    # artifact actually CONSIDERED. A name listed as optional but absent from
    # the artifact's expected inventory was never a candidate for verification,
    # so treating it as covered would let a narrowed collection wave a unit
    # through by naming it optional. Tolerance therefore requires BOTH.
    validity_expected = set((validity_result or {}).get("expected_units") or ())
    validity_discovered = set((validity_result or {}).get("discovered_units") or ())
    # ...and only where the unit is genuinely ABSENT. "Optional" licenses a
    # missing unit, never an unverified one: an optional unit that is installed
    # is verified exactly like a required one. An artifact that lists a unit as
    # discovered AND optional AND expected while omitting it from
    # verified_units is internally inconsistent -- stale, narrowed or edited --
    # and reading its optional flag as coverage would let it waive an installed
    # unit that nothing checked.
    tolerated = {
        u for u in (set((validity_result or {}).get("optional_units") or ())
                    & validity_expected)
        if u not in validity_discovered
    }
    installed_but_unverified = tuple(sorted(
        u for u in required_units
        if u not in verified_units and u in validity_discovered
    ))
    uncovered = tuple(
        u for u in required_units if u not in verified_units and u not in tolerated
    )
    bound = (bool(expected_origins) and bool(declared_units)
             and not uncovered and not unbound)

    if not expected_origins:
        errors.append(
            "no expected_origins supplied — there is nothing to bind the "
            "scheduler and validity evidence together, so three-gate coverage "
            "cannot be established"
        )
    if not declared_units:
        errors.append(
            "no expected_validity_units supplied — scheduler origins name only "
            "path-bearing units, so timers and other non-path units would go "
            "undemanded and a narrowed validity inventory would pass unnoticed"
        )
    for unit in uncovered:
        if unit in installed_but_unverified:
            errors.append(
                f"{unit}: present on the host according to the validity "
                f"evidence's own discovery, yet absent from its verified_units "
                f"— an installed unit is verified whether or not it is "
                f"optional, so this artifact cannot waive it"
            )
        else:
            errors.append(
                f"{unit}: covered by scheduler alignment but absent from the "
                f"validity evidence — a unit that passed only one gate cannot "
                f"contribute to production release identity"
            )

    return {
        "status": "OK" if ok else "FAILED",
        "scheduler": sched,
        "pointer": pointer_result,
        "systemd_unit_validity": validity,
        "validity_uncovered_units": list(uncovered),
        "validity_installed_but_unverified": list(installed_but_unverified),
        # Empty when the three gates are provably one observation; otherwise
        # the reasons they are not, which is why the aggregate is withheld.
        "provenance_conflicts": list(unbound),
        **(observation or ObservationContext("", "")).as_dict(),
        "production_release_identity": (
            "PASS" if (ok and validity == "PASS" and bound) else "NOT_ESTABLISHED"
        ),
        "errors": errors,
    }
