from __future__ import annotations
import ast, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
# Modules permitted to construct FMPClient directly:
SANCTIONED = {
    "fmp_client.py",
    "portfolio_automation/data_budget/governor.py",
    "portfolio_automation/data_budget/factory.py",
    "backtesting/fmp_backtester.py", "backtesting/run_loop.py",
    "backtesting/poc_simulation_harness.py",
    "portfolio_automation/historical_replay/replay_runner.py",
}
SANCTIONED_PREFIXES = ("scripts/", "tests/", ".worktrees/", ".venv/", ".claude/")


def _violations() -> list[str]:
    bad = []
    for py in ROOT.rglob("*.py"):
        rel = py.relative_to(ROOT).as_posix()
        if rel in SANCTIONED or rel.startswith(SANCTIONED_PREFIXES):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Collect every local name bound to fmp_client.FMPClient — including
        # aliases (`from fmp_client import FMPClient as _PnlFMP`) and module
        # imports (`import fmp_client` -> `fmp_client.FMPClient(...)`). A bare
        # name check alone misses aliased imports (the 2026-06-15 P&L bypass).
        bound_names: set[str] = set()
        module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "fmp_client":
                for a in node.names:
                    if a.name == "FMPClient":
                        bound_names.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "fmp_client":
                        module_aliases.add(a.asname or a.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in bound_names:
                bad.append(f"{rel}:{node.lineno}")
            elif isinstance(fn, ast.Attribute) and fn.attr == "FMPClient" \
                    and isinstance(fn.value, ast.Name) and fn.value.id in module_aliases:
                bad.append(f"{rel}:{node.lineno}")
    return bad


class TestNoDirectConstruction(unittest.TestCase):
    def test_no_module_constructs_fmpclient_directly(self):
        v = _violations()
        self.assertEqual(v, [], f"Direct FMPClient() construction outside governor: {v}")


if __name__ == "__main__":
    unittest.main()
