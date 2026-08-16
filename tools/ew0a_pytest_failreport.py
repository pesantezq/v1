"""pytest plugin: capture exact failing node IDs to JSON.

Loaded explicitly with ``-p ew0a_pytest_failreport``; deliberately NOT wired
into a root ``conftest.py``. The repository has no root conftest, and adding
one would change every unrelated test run in the tree.

WHY A REPORT HOOK RATHER THAN OUTPUT PARSING.

``report.nodeid`` is the same string pytest itself accepts for selection, so a
captured identity round-trips back into ``pytest <nodeid>``. The alternatives
lose information: ``--junit-xml`` renders ``path::Class::test`` as
``classname="path.Class" name="test"``, which cannot be inverted because
directory separators, the class separator and parametrised ids all collapse to
``.``; and the ``-q`` short summary puts an unescaped ``" - "`` between a node
id and a message either of which may contain it, and wraps at terminal width.
A guess in the failure-identity layer is precisely what this replaces.

Usage:
    pytest -p ew0a_pytest_failreport --ew0a-failreport=/path/out.json [...]
"""
from __future__ import annotations

import json
import os


def pytest_addoption(parser):
    parser.addoption("--ew0a-failreport", action="store", default=None,
                     help="write exact failing node IDs as JSON to this path")


class _FailReport:
    def __init__(self, out_path: str) -> None:
        self.out_path = out_path
        self.failed: set[str] = set()
        self.collected: set[str] = set()
        self.collect_errors: set[str] = set()

    # Every non-passing outcome counts, including setup/teardown errors: a
    # fixture that blows up is a failing identity, not an absent one.
    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors.add(report.nodeid)

    def pytest_itemcollected(self, item):
        self.collected.add(item.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        payload = {
            "failure_node_ids": sorted(self.failed),
            "collected_node_ids": sorted(self.collected),
            "collect_errors": sorted(self.collect_errors),
            # Recorded IN BAND. Exit status observed through a shell wrapper is
            # not reliable, so run health is never derived from the caller.
            "pytest_exitstatus": int(exitstatus),
        }
        tmp = self.out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=True, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.out_path)


def pytest_configure(config):
    out = config.getoption("--ew0a-failreport")
    if out:
        config.pluginmanager.register(_FailReport(out), "ew0a-failreport")
