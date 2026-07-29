"""Phase E5 (WS17) — reusable false-GREEN adversarial-probe suite.

See ``docs/reliability-program/2026-07-28-findings.md`` and
``docs/reliability-program/2026-07-28-spec.md`` ("E5 WS17 false-GREEN
adversarial probe suite + shared assertions") for the program context. Every
probe in this package corresponds to a real defect confirmed in this repo
(some already fixed on ``main``, some still open — each probe's docstring
says which). ``assertions.py`` holds the shared, reusable assertion helpers;
the ``test_probe_*`` modules apply them against real repo modules and
fixtures.

Validation-only: this package never mutates production code or protected
scoring semantics. It is a test-and-helpers change.
"""
from __future__ import annotations
