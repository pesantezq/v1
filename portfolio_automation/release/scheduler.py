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
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

DIRECT = "DIRECT"
POINTER = "POINTER"
RECOMMENDED_MODEL = POINTER

# Conventional layout on the production host.
RELEASES_DIR = "/opt/stockbot/releases"
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
EXEC_DIRECTIVES = ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecReload")
PATH_DIRECTIVES = ("WorkingDirectory", "RootDirectory")
SECRET_DIRECTIVES = ("EnvironmentFile",)

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
    #: WorkingDirectory / RootDirectory. Decides where Python resolves a
    #: relatively-named application module, so it is code identity.
    working_directory: str | None = None
    #: EnvironmentFile paths. Credentials, not release code.
    environment_files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_system_transitional(self) -> bool:
        return self.executable in SYSTEM_TRANSITIONAL

    def _code_paths(self) -> tuple[str, ...]:
        """Every path that determines which application code runs.

        Deliberately excludes environment_files: a credential file is not code.
        """
        paths = [self.executable, *self.referenced_paths]
        if self.working_directory:
            paths.append(self.working_directory)
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
    """Extract execution surfaces from systemd unit file *content*.

    Every Exec* directive becomes a surface, and each surface inherits the
    unit's ``WorkingDirectory`` and ``EnvironmentFile`` values — because those
    decide, respectively, which application code a relatively-named module
    resolves to and which credentials it loads.
    """
    entries: list[tuple[str, str]] = []
    for entry in _logical_lines(content):
        if "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        entries.append((key.strip(), value.strip()))

    working_dir: str | None = None
    env_files: list[str] = []
    for key, value in entries:
        if key in PATH_DIRECTIVES and value:
            working_dir = value.split()[0]
        elif key in SECRET_DIRECTIVES and value:
            # systemd allows a leading '-' meaning "optional".
            env_files.append(value.lstrip("-").split()[0])

    surfaces: list[ExecutionSurface] = []
    for key, value in entries:
        if key not in EXEC_DIRECTIVES:
            continue
        if not value:
            raise SchedulerParseError(f"{origin}: {key} has no command")
        exe, rest = _split_command(value, origin=origin)
        surfaces.append(ExecutionSurface(
            origin=origin, raw=value, executable=exe, referenced_paths=rest,
            working_directory=working_dir, environment_files=tuple(env_files),
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
                               release_root: str) -> dict:
    """Report whether every surface's code paths resolve to the approved release.

    Fails closed on an empty surface list: "we found nothing to check" must not
    be reportable as "everything checks out".

    This certifies **path alignment only**. It cannot establish
    ``production_code_sha == approved_release_sha`` — see ``pointer`` and
    ``certify_release_identity``.
    """
    if not surfaces:
        return {"status": "FAILED",
                "errors": ["no execution surfaces supplied — cannot certify"],
                "unresolved": [], "system_transitional": [],
                "secret_paths_outside_release": []}
    unresolved = unresolved_surfaces(surfaces, release_root=release_root)
    secrets: list[str] = []
    for s in surfaces:
        for p in s.legacy_secret_paths(release_root=release_root):
            secrets.append(f"{s.origin}: {p}")
    return {
        "status": "OK" if not unresolved else "FAILED",
        "errors": [
            f"{s.origin} still binds legacy code: "
            f"{', '.join(s.legacy_paths(release_root=release_root)) or s.executable}"
            for s in unresolved
        ],
        "unresolved": [s.origin for s in unresolved],
        "system_transitional": [s.origin for s in surfaces if s.is_system_transitional],
        # Surfaced, never counted as code drift: a credential file is not code,
        # but the cutover still has to re-provision it.
        "secret_paths_outside_release": secrets,
        "scope": "path_alignment_only",
    }


def certify_release_identity(surfaces: list[ExecutionSurface], *,
                             pointer_result: dict,
                             release_root: str = CURRENT_POINTER) -> dict:
    """Path alignment AND pointer-target SHA together.

    Either alone is insufficient. Aligned schedulers under a ``current`` that
    targets an unapproved release is precisely the failure this guards, and so
    is an approved pointer that half the schedulers ignore.
    """
    sched = certify_scheduler_identity(surfaces, release_root=release_root)
    ok = sched["status"] == "OK" and pointer_result.get("status") == "OK"
    return {
        "status": "OK" if ok else "FAILED",
        "scheduler": sched,
        "pointer": pointer_result,
        "errors": list(sched["errors"]) + list(pointer_result.get("errors") or []),
    }
