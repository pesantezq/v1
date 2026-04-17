import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from main import _persist_guardrail_violations


class _FakeStore:
    def __init__(self):
        self.upserted = []
        self.cleared = []
        self.rows = [
            {"violation_key": "stale|OLD|stale", "days_active": 9, "escalation_level": 1},
        ]

    def upsert_structural_violation(self, violation_key):
        self.upserted.append(violation_key)
        row = {
            "violation_key": violation_key,
            "days_active": 3,
            "escalation_level": 1,
        }
        self.rows = [r for r in self.rows if r["violation_key"] != violation_key]
        self.rows.append(row)
        return row

    def get_all_structural_violations(self):
        return list(self.rows)

    def clear_structural_violation(self, violation_key):
        self.cleared.append(violation_key)
        self.rows = [r for r in self.rows if r["violation_key"] != violation_key]


class TestPersistGuardrailViolations(unittest.TestCase):
    def test_persists_serialized_violations_and_clears_stale_rows(self):
        store = _FakeStore()
        violations = [
            {
                "symbol": "QQQ",
                "violation_type": "concentration",
                "required_action": "Trim QQQ",
            },
            {
                "symbol": "PORTFOLIO",
                "violation_type": "leverage",
                "required_action": "Reduce leveraged exposure",
            },
        ]

        _persist_guardrail_violations(store, violations, logging.getLogger("test"))

        self.assertEqual(
            store.upserted,
            [
                "concentration|QQQ|concentration",
                "leverage|PORTFOLIO|leverage",
            ],
        )
        self.assertEqual(store.cleared, ["stale|OLD|stale"])
        self.assertEqual(violations[0]["days_active"], 3)
        self.assertEqual(violations[0]["escalation_level"], 1)
        self.assertEqual(violations[1]["days_active"], 3)
        self.assertEqual(violations[1]["escalation_level"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
