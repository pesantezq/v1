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
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
               and node.func.id == "FMPClient":
                bad.append(f"{rel}:{node.lineno}")
    return bad


class TestNoDirectConstruction(unittest.TestCase):
    def test_no_module_constructs_fmpclient_directly(self):
        v = _violations()
        self.assertEqual(v, [], f"Direct FMPClient() construction outside governor: {v}")


if __name__ == "__main__":
    unittest.main()
