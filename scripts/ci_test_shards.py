#!/usr/bin/env python3
"""Auditable CI test sharding for the Northstar certification gate.

WHY THIS EXISTS
    The official full-suite command (.agent/project_state.yaml
    required_test_policy.full_suite_command, mirrored in AGENTS.md) is one
    serial pytest invocation over tests/. It is the certification universe and
    is NOT changed by this file. CI runs that universe as a small number of
    logical DOMAIN SHARDS, each with pytest-xdist workers, so the exact-head
    gate finishes in minutes instead of tens of minutes without dropping,
    duplicating, or re-ordering any required test.

THE CONTRACT (enforced by `verify` and by tests/test_ci_test_shards.py)
    * every test FILE the official command collects belongs to EXACTLY ONE
      shard (rules are ordered; the last shard is the catch-all, so a new test
      file can never be silently dropped -- it lands in `core`);
    * the UNION of the shards' collected node IDs == the official command's
      collected node IDs, exactly, and shards are pairwise disjoint;
    * the CI deselect list lives HERE once, so local and CI runs agree;
    * tests marked `serial` (pytest.ini) are excluded from the parallel phase
      of every shard and run in one process afterwards; `serial-count`
      reports how many (nine as of 2026-09-27, each justified in place).

This file has no runtime consumers. It is test infrastructure only.

USAGE
    python scripts/ci_test_shards.py list                # shard -> files
    python scripts/ci_test_shards.py files <shard>       # newline-separated
    python scripts/ci_test_shards.py pytest-args         # shared --ignore/--deselect flags
    python scripts/ci_test_shards.py verify              # collection equivalence (runs pytest --collect-only)
    python scripts/ci_test_shards.py serial-count        # number of `serial` tests collected
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"

#: Files the official command ignores (they are not part of the certification universe).
OFFICIAL_IGNORES: tuple[str, ...] = (
    "tests/test_gui_api_health.py",
    "tests/test_gui_insight_cards.py",
)

#: Node IDs CI deselects because they validate LIVE production runtime state or
#: production-host assumptions (see .github/workflows/northstar-ci.yml for the
#: 2026-08-09 classification). They still run in the VPS-side full suite.
CI_DESELECT: tuple[str, ...] = (
    "tests/test_artifact_registry.py::test_sqg_registration_keeps_registry_green_and_debt_free",
    "tests/test_gui_dashboard_memo.py::test_memo_route_all_six_section_headings_present",
    "tests/test_gui_dashboard_memo.py::test_memo_route_has_stacked_sections",
    "tests/test_gui_dashboard_quant.py::test_quant_route_mobile_card_stack_present",
    "tests/test_strategy_projection_anchor.py::test_projection_sets_anchor_strategy_id_when_selected",
    "tests/test_broker_overlay.py::test_apply_overlay_to_config_object",
    "tests/test_broker_overlay.py::test_apply_overlay_writes_source_artifact",
    "tests/test_broker_overlay.py::test_apply_overlay_config_fallback_returns_unchanged",
    "tests/test_broker_overlay.py::test_apply_overlay_records_config_source_on_fallback",
    "tests/test_operator_worker_runner.py::test_safe_repair_uses_accept_edits_and_strips_api_key",
)

#: Ordered domain rules: (shard, predicate over the repo-relative posix path).
#: First match wins; `core` catches everything else. Rules are by ownership
#: (which subsystem the test certifies), not by size.
def _pref(*prefixes: str):
    return lambda p: any(p.startswith(f"tests/{x}") for x in prefixes)


SHARD_RULES: tuple[tuple[str, object], ...] = (
    ("governance", _pref(
        "test_agent_", "test_northstar", "test_doc_audit", "test_operator_control",
        "test_ew0", "test_ns0c", "test_control_center", "test_evidence_gateway",
        "test_roadmap", "test_systemd", "test_release_", "test_worker_", "test_rd_",
        "test_engineer", "test_durable", "test_review_", "test_supervisor", "test_gpt_",
        "test_run_loop", "test_runtime_policy", "test_certification", "test_vertical_slice",
        "test_backup", "test_preflight", "test_run_daily_safe", "test_daily_check",
        "test_governance", "test_promotion", "test_sim_governance", "test_auto_approval",
        "test_learning", "test_apprentice", "probes/",
    )),
    ("evidence_data", _pref(
        "test_vs002", "test_data_budget", "test_fmp", "test_intraday_lab", "historical_replay/",
        "test_data_governance", "test_data_quality", "test_backfill", "test_env_registry",
        "test_main_", "test_pipeline", "test_price", "test_calendar", "test_universe",
        "test_dividend", "test_weekly_etf", "test_etf", "test_market_", "test_news",
        "test_scraped", "test_ws13", "migrations/",
    )),
    ("broker_portfolio", _pref(
        "test_schwab", "test_broker", "test_holdings", "test_portfolio", "test_allocation",
        "test_tax", "test_capital", "test_position", "test_rebalance", "test_risk",
        "portfolio_sim/", "test_next_stage", "test_run_mode",
    )),
    ("strategy_research", _pref(
        "test_strategy", "test_simulation", "test_walk_forward", "test_weight", "test_backtest",
        "test_registry", "test_signal", "test_regime", "test_decision", "test_policy",
        "test_pattern", "test_retune", "test_confidence", "test_conviction", "test_explain",
        "test_watchlist", "test_theme", "test_discovery", "discovery/", "crowd_intelligence/",
        "social_", "institutional_intelligence/", "test_crowd", "test_institutional",
        "test_finbert", "test_sentiment", "test_extended_watchlist",
    )),
    ("gui_readmodels", _pref(
        "test_gui", "test_dash", "gui_v2/", "test_memo", "test_daily_memo", "test_weekly_report",
        "test_email", "test_operator_worker", "test_run_summary", "test_flock",
    )),
    ("core", lambda p: True),
)
SHARD_NAMES: tuple[str, ...] = tuple(name for name, _ in SHARD_RULES)


def official_test_files() -> list[str]:
    """Every test file the official command would consider (minus its ignores)."""
    out = []
    for p in sorted(TESTS.rglob("test_*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel in OFFICIAL_IGNORES or "__pycache__" in rel:
            continue
        out.append(rel)
    return out


def shard_of(rel: str) -> str:
    for name, pred in SHARD_RULES:
        if pred(rel):
            return name
    raise AssertionError(f"unreachable: {rel}")  # `core` matches everything


def shard_files() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {n: [] for n in SHARD_NAMES}
    for rel in official_test_files():
        out[shard_of(rel)].append(rel)
    return out


def pytest_args() -> list[str]:
    args = [f"--ignore={x}" for x in OFFICIAL_IGNORES]
    for node in CI_DESELECT:
        args += ["--deselect", node]
    return args


def _collect(paths: list[str], extra: list[str] | None = None) -> set[str]:
    """Node IDs pytest collects for `paths` (official ignores/deselects applied)."""
    cmd = [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider",
           *pytest_args(), *(extra or []), *paths]
    res = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if res.returncode not in (0, 5):
        sys.stderr.write(res.stdout[-4000:] + res.stderr[-2000:])
        raise SystemExit(f"collection failed for {paths[:3]}...: rc={res.returncode}")
    return {ln.strip() for ln in res.stdout.splitlines() if "::" in ln and not ln.startswith(" ")}


def verify() -> int:
    """Collection equivalence, exactly as CI executes it:

        UNION over shards of collect(shard files, -m "not serial")
        UNION            collect(tests,       -m "serial")
        ==               collect(tests)   (the official command + CI deselects)

    with every phase pairwise disjoint (no node runs twice)."""
    official = _collect(["tests"])
    union: set[str] = set()
    counts: dict[str, int] = {}
    ok = True
    for name, files in shard_files().items():
        ids = _collect(files, ["-m", "not serial"]) if files else set()
        counts[name] = len(ids)
        dup = union & ids
        if dup:
            ok = False
            print(f"DUPLICATE across shards ({name}): {sorted(dup)[:5]} ...")
        union |= ids
    serial = _collect(["tests"], ["-m", "serial"])
    dup = union & serial
    if dup:
        ok = False
        print(f"DUPLICATE between parallel and serial phases: {sorted(dup)[:5]} ...")
    union |= serial
    print(f"SERIAL_TEST_COUNT={len(serial)}")
    for n in sorted(serial):
        print("  serial:", n)
    missing = official - union
    extra = union - official
    for label, s in (("MISSING from shards", missing), ("EXTRA in shards", extra)):
        if s:
            ok = False
            print(f"{label}: {len(s)}")
            for n in sorted(s)[:20]:
                print("   ", n)
    print(f"OFFICIAL_COLLECTION_NODE_COUNT={len(official)}")
    print(f"ACCELERATED_COLLECTION_NODE_COUNT={len(union)}")
    for name in SHARD_NAMES:
        print(f"  shard {name}: files={len(shard_files()[name])} nodes={counts[name]}")
    print("COLLECTION_EQUIVALENCE=" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def serial_count() -> int:
    ids = _collect(["tests"], ["-m", "serial"])
    print(len(ids))
    return 0


def main(argv: list[str]) -> int:
    os.chdir(REPO)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        for name, files in shard_files().items():
            print(f"[{name}] {len(files)} files")
            for f in files:
                print("   ", f)
        return 0
    if cmd == "files":
        if len(rest) != 1 or rest[0] not in SHARD_NAMES:
            print(f"usage: files <{'|'.join(SHARD_NAMES)}>", file=sys.stderr)
            return 2
        print("\n".join(shard_files()[rest[0]]))
        return 0
    if cmd == "pytest-args":
        print(" ".join(pytest_args()))
        return 0
    if cmd == "verify":
        return verify()
    if cmd == "serial-count":
        return serial_count()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
