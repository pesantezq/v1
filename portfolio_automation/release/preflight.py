"""Venv preflight: prove imports resolve from the release, not the legacy tree.

Why this exists
---------------
Mission 19 observed that ``requirements.txt`` is byte-identical between the
live production commit and the approved release, and concluded the existing
``.venv`` was safe to reuse. That inference is not sound. ``requirements.txt``
describes *third-party dependency versions*; it says nothing about where the
interpreter resolves **project** modules from. A venv can bind the application
to a specific source tree in several ways that leave the requirements file
untouched:

* an editable install (``pip install -e /opt/stockbot``) writes a ``.pth`` or
  ``__editable__*`` finder that injects the legacy path onto ``sys.path``;
* a ``*.egg-link`` records an absolute source directory;
* ``dist-info/direct_url.json`` records the absolute tree it was built from;
* a stray ``.pth`` file can add the legacy checkout unconditionally.

Any of those would let a cutover repoint systemd and cron to the release while
the interpreter kept importing ``portfolio_automation`` from ``/opt/stockbot``.
Production would report the new SHA and run the old code — the most deceptive
possible outcome, because every identity check would pass.

So the gate is not "requirements unchanged" but:

    application imports executed with the selected release environment
    resolve project modules from the approved release,
    not the legacy checkout

``scan_environment`` finds the static bindings; ``verify_import_origin`` proves
the dynamic outcome from an interpreter's actual ``sys.path``/module origins.
Both are pure functions over supplied inputs: no venv is built or modified here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Project packages whose import origin decides which code actually ran.
PROJECT_MODULES: tuple[str, ...] = (
    "portfolio_automation",
    "watchlist_scanner",
    "policy_evaluator",
    "agent",
    "theme_engine",
)

PREFLIGHT_OK = "VENV_PREFLIGHT_OK"
PREFLIGHT_FAILED = "VENV_PREFLIGHT_FAILED"


@dataclass
class PreflightResult:
    status: str = PREFLIGHT_FAILED
    findings: list[str] = field(default_factory=list)
    inspected: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == PREFLIGHT_OK


def _under(path: str, root: str) -> bool:
    p = PurePosixPath(str(path).replace("\\", "/"))
    r = PurePosixPath(str(root).replace("\\", "/").rstrip("/"))
    return p == r or r in p.parents


def scan_environment(venv_dir: Path | str, *, legacy_root: str,
                     release_root: str) -> PreflightResult:
    """Find static bindings in ``venv_dir`` that point at ``legacy_root``.

    Reads only ``.pth``, ``*.egg-link`` and ``dist-info/direct_url.json``. Never
    executes anything out of the environment, so it is safe to point at a
    production venv copy.
    """
    result = PreflightResult()
    venv = Path(venv_dir)
    if not venv.is_dir():
        result.findings.append(f"venv directory not found: {venv}")
        return result

    site_dirs = sorted(venv.glob("lib/python*/site-packages"))
    if not site_dirs:
        # Fail closed: an environment we cannot inspect is not an environment
        # we can certify.
        result.findings.append(f"no site-packages under {venv} — cannot certify")
        return result

    for site in site_dirs:
        result.inspected.append(str(site))

        for pth in sorted(site.glob("*.pth")):
            try:
                text = pth.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                result.findings.append(f"{pth.name}: unreadable ({exc.__class__.__name__})")
                continue
            for line in text.splitlines():
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                if entry.startswith(("import ", "import\t")):
                    # An editable-install finder executes code at startup; the
                    # path it injects is not visible here, so treat the whole
                    # file as suspect when it names the legacy tree.
                    if legacy_root in entry:
                        result.findings.append(
                            f"{pth.name}: executable .pth references legacy tree")
                    continue
                if _under(entry, legacy_root) and not _under(entry, release_root):
                    result.findings.append(
                        f"{pth.name}: sys.path entry binds legacy tree: {entry}")

        for link in sorted(site.glob("*.egg-link")):
            try:
                target = link.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                result.findings.append(f"{link.name}: unreadable ({exc.__class__.__name__})")
                continue
            for entry in (t.strip() for t in target):
                if entry and entry != "." and _under(entry, legacy_root) \
                        and not _under(entry, release_root):
                    result.findings.append(
                        f"{link.name}: editable install points at {entry}")

        for durl in sorted(site.glob("*.dist-info/direct_url.json")):
            try:
                doc = json.loads(durl.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                result.findings.append(
                    f"{durl.parent.name}: direct_url.json unreadable "
                    f"({exc.__class__.__name__})")
                continue
            url = str(doc.get("url", ""))
            local = url[len("file://"):] if url.startswith("file://") else url
            editable = bool((doc.get("dir_info") or {}).get("editable"))
            if local and _under(local, legacy_root) and not _under(local, release_root):
                result.findings.append(
                    f"{durl.parent.name}: {'editable ' if editable else ''}"
                    f"install recorded against {local}")

    result.status = PREFLIGHT_OK if not result.findings else PREFLIGHT_FAILED
    return result


def verify_import_origin(module_origins: dict[str, str], *, release_root: str,
                         required: tuple[str, ...] = PROJECT_MODULES,
                         ) -> PreflightResult:
    """Check resolved module ``__file__`` origins against the approved release.

    ``module_origins`` maps module name to the ``__file__`` an interpreter
    actually resolved — collected by the cutover runbook with the release
    environment, e.g.::

        python -c 'import json,portfolio_automation as p; \\
                   print(json.dumps({"portfolio_automation": p.__file__}))'

    A module missing from the mapping is a failure, not a pass: absence of
    evidence is not evidence that the import resolved correctly.
    """
    result = PreflightResult()
    for name in required:
        origin = module_origins.get(name)
        if not origin:
            result.findings.append(f"{name}: no import origin recorded — cannot certify")
            continue
        result.inspected.append(f"{name} -> {origin}")
        if not _under(origin, release_root):
            result.findings.append(
                f"{name}: resolves from {origin}, outside the approved release "
                f"{release_root}")
    result.status = PREFLIGHT_OK if not result.findings else PREFLIGHT_FAILED
    return result
