"""Regenerate the execution-directive manifest the shell collector reads.

The manifest exists only because the collector is shell and the certifier is
Python. Its CONTENT is owned here, derived from ``EXEC_DIRECTIVES``, so the
file can never be edited into disagreeing with the code that defines it.
"""
from __future__ import annotations

from pathlib import Path

from .scheduler import EXEC_DIRECTIVES

MANIFEST = Path(__file__).with_name("exec_directives.manifest")

HEADER = """# GENERATED -- the authority is EXEC_DIRECTIVES in
# portfolio_automation/release/scheduler.py. Do not edit by hand.
#
# This exists because the evidence collector is shell and the certifier is
# Python, and two hand-maintained lists of the same contract drift: the
# collector captured only ExecStart while the certifier treated six
# directives as release-identity surfaces, so a legacy ExecStartPre was
# invisible to the scheduler artifact and could execute outside the
# approved release without anything objecting. A test asserts this file
# equals EXEC_DIRECTIVES exactly, so the two cannot separate again.
#
# Regenerate with: python -m portfolio_automation.release.exec_manifest
"""


def render() -> str:
    return HEADER + "\n".join(EXEC_DIRECTIVES) + "\n"


def directives_in_manifest(text: str) -> tuple[str, ...]:
    """The directives a reader -- shell or Python -- would take from the file."""
    return tuple(
        line.strip() for line in (text or "").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


if __name__ == "__main__":
    MANIFEST.write_text(render(), encoding="utf-8")
    print(MANIFEST)
