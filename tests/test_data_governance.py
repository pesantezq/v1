"""
Tests for portfolio_automation/data_governance.py

Contracts verified:
- get_output_path returns correct subdirectory for every namespace
- LIVE and USER are user-scoped (user_id appears in path)
- HISTORICAL maps to outputs/backtest
- SANDBOX maps to outputs/sandbox
- POLICY maps to outputs/policy
- PORTFOLIO maps to outputs/portfolio
- LATEST maps to outputs/latest
- path traversal is rejected
- absolute path outside namespace is rejected
- wrong namespace is rejected by validate_output_path
- valid path is accepted by validate_output_path
- ensure_output_dir creates directories
- safe_write_text writes only inside namespace
- safe_write_json writes only inside namespace and serializes correctly
- namespace_for_existing_path detects known namespaces
- user_id defaults to "owner"
- user_id containing path traversal or slashes is rejected
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from portfolio_automation.data_governance import (
    DataGovernanceError,
    OutputNamespace,
    OutputPathPolicy,
    ensure_output_dir,
    get_output_path,
    get_policies,
    namespace_for_existing_path,
    safe_write_json,
    safe_write_text,
    validate_output_path,
)


class TestGetOutputPath(unittest.TestCase):

    def _base(self, tmp: str) -> Path:
        return Path(tmp)

    def test_live_namespace_includes_user_id(self):
        p = get_output_path(OutputNamespace.LIVE, "file.json", user_id="alice", base_dir="/tmp/out")
        self.assertEqual(p, Path("/tmp/out/live/alice/file.json"))

    def test_live_namespace_default_user_id(self):
        p = get_output_path(OutputNamespace.LIVE, "file.json", base_dir="/tmp/out")
        self.assertIn("owner", str(p))
        self.assertIn("live", str(p))

    def test_historical_maps_to_backtest(self):
        p = get_output_path(OutputNamespace.HISTORICAL, "cal.json", base_dir="/tmp/out")
        self.assertEqual(p, Path("/tmp/out/backtest/cal.json"))

    def test_historical_user_id_not_in_path(self):
        p = get_output_path(OutputNamespace.HISTORICAL, "cal.json", user_id="alice", base_dir="/tmp/out")
        self.assertNotIn("alice", str(p))

    def test_sandbox_maps_to_sandbox(self):
        p = get_output_path(OutputNamespace.SANDBOX, "test.json", base_dir="/tmp/out")
        self.assertEqual(p, Path("/tmp/out/sandbox/test.json"))

    def test_policy_maps_to_policy(self):
        p = get_output_path(OutputNamespace.POLICY, "eval.json", base_dir="/tmp/out")
        self.assertEqual(p, Path("/tmp/out/policy/eval.json"))

    def test_portfolio_maps_to_portfolio(self):
        p = get_output_path(OutputNamespace.PORTFOLIO, "snap.json", base_dir="/tmp/out")
        self.assertEqual(p, Path("/tmp/out/portfolio/snap.json"))

    def test_latest_maps_to_latest(self):
        p = get_output_path(OutputNamespace.LATEST, "decision_plan.json", base_dir="/tmp/out")
        self.assertEqual(p, Path("/tmp/out/latest/decision_plan.json"))

    def test_user_namespace_includes_user_id(self):
        p = get_output_path(OutputNamespace.USER, "prefs.json", user_id="bob", base_dir="/tmp/out")
        self.assertEqual(p, Path("/tmp/out/users/bob/prefs.json"))

    def test_user_id_defaults_to_owner(self):
        p = get_output_path(OutputNamespace.USER, "prefs.json", base_dir="/tmp/out")
        self.assertIn("owner", str(p))

    def test_subdirectory_filename_allowed(self):
        p = get_output_path(OutputNamespace.POLICY, "sub/file.json", base_dir="/tmp/out")
        self.assertEqual(p, Path("/tmp/out/policy/sub/file.json"))


class TestUserIdValidation(unittest.TestCase):

    def _call(self, user_id: str):
        get_output_path(OutputNamespace.LIVE, "f.json", user_id=user_id, base_dir="/tmp/out")

    def test_empty_user_id_rejected(self):
        with self.assertRaises(DataGovernanceError):
            self._call("")

    def test_slash_in_user_id_rejected(self):
        with self.assertRaises(DataGovernanceError):
            self._call("alice/bob")

    def test_dotdot_user_id_rejected(self):
        with self.assertRaises(DataGovernanceError):
            self._call("../etc")

    def test_backslash_user_id_rejected(self):
        with self.assertRaises(DataGovernanceError):
            self._call("alice\\bob")

    def test_null_byte_user_id_rejected(self):
        with self.assertRaises(DataGovernanceError):
            self._call("alice\x00bob")

    def test_valid_alphanumeric_accepted(self):
        p = self._call("alice123")
        # No exception raised

    def test_valid_hyphen_underscore_dot_accepted(self):
        self._call("alice_bob-test.v2")


class TestValidateOutputPath(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_path_returns_resolved(self):
        expected = get_output_path(OutputNamespace.POLICY, "eval.json", base_dir=self.base)
        result = validate_output_path(OutputNamespace.POLICY, expected, base_dir=self.base)
        self.assertEqual(result, expected.resolve())

    def test_wrong_namespace_rejected(self):
        policy_path = get_output_path(OutputNamespace.POLICY, "eval.json", base_dir=self.base)
        with self.assertRaises(DataGovernanceError):
            validate_output_path(OutputNamespace.LATEST, policy_path, base_dir=self.base)

    def test_path_traversal_rejected(self):
        traversal = self.base / "policy" / ".." / ".." / "etc" / "passwd"
        with self.assertRaises(DataGovernanceError):
            validate_output_path(OutputNamespace.POLICY, traversal, base_dir=self.base)

    def test_absolute_path_outside_namespace_rejected(self):
        outside = Path("/etc/passwd")
        with self.assertRaises(DataGovernanceError):
            validate_output_path(OutputNamespace.POLICY, outside, base_dir=self.base)

    def test_live_path_for_correct_user_accepted(self):
        live_path = get_output_path(
            OutputNamespace.LIVE, "out.json", user_id="alice", base_dir=self.base
        )
        result = validate_output_path(
            OutputNamespace.LIVE, live_path, user_id="alice", base_dir=self.base
        )
        self.assertTrue(str(result).endswith("out.json"))

    def test_live_path_for_wrong_user_rejected(self):
        alice_path = get_output_path(
            OutputNamespace.LIVE, "out.json", user_id="alice", base_dir=self.base
        )
        with self.assertRaises(DataGovernanceError):
            validate_output_path(
                OutputNamespace.LIVE, alice_path, user_id="bob", base_dir=self.base
            )

    def test_historical_path_accepted(self):
        hist_path = get_output_path(OutputNamespace.HISTORICAL, "cal.json", base_dir=self.base)
        result = validate_output_path(OutputNamespace.HISTORICAL, hist_path, base_dir=self.base)
        self.assertTrue(str(result).endswith("cal.json"))

    def test_sandbox_path_accepted(self):
        sb_path = get_output_path(OutputNamespace.SANDBOX, "test.json", base_dir=self.base)
        result = validate_output_path(OutputNamespace.SANDBOX, sb_path, base_dir=self.base)
        self.assertIn("sandbox", str(result))


class TestEnsureOutputDir(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_namespace_directory(self):
        d = ensure_output_dir(OutputNamespace.POLICY, base_dir=self.base)
        self.assertTrue(d.exists())
        self.assertTrue(d.is_dir())

    def test_returns_correct_directory(self):
        d = ensure_output_dir(OutputNamespace.HISTORICAL, base_dir=self.base)
        self.assertEqual(d, self.base / "backtest")

    def test_creates_parent_for_filename(self):
        d = ensure_output_dir(OutputNamespace.SANDBOX, "sub/file.json", base_dir=self.base)
        self.assertTrue(d.exists())
        self.assertEqual(d, self.base / "sandbox" / "sub")

    def test_user_scoped_directory_created(self):
        d = ensure_output_dir(OutputNamespace.LIVE, user_id="carol", base_dir=self.base)
        self.assertTrue(d.exists())
        self.assertIn("carol", str(d))

    def test_idempotent_second_call(self):
        ensure_output_dir(OutputNamespace.POLICY, base_dir=self.base)
        ensure_output_dir(OutputNamespace.POLICY, base_dir=self.base)  # no error


class TestSafeWriteText(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_file_to_correct_location(self):
        path = safe_write_text(OutputNamespace.POLICY, "eval.md", "# hello", base_dir=self.base)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(), "# hello")

    def test_returned_path_inside_namespace(self):
        path = safe_write_text(OutputNamespace.SANDBOX, "out.txt", "data", base_dir=self.base)
        self.assertIn("sandbox", str(path))

    def test_creates_parent_directories(self):
        path = safe_write_text(
            OutputNamespace.LATEST, "sub/decision_plan.md", "content", base_dir=self.base
        )
        self.assertTrue(path.exists())

    def test_historical_writes_to_backtest(self):
        path = safe_write_text(
            OutputNamespace.HISTORICAL, "calibration.md", "report", base_dir=self.base
        )
        self.assertIn("backtest", str(path))

    def test_live_writes_under_user_id(self):
        path = safe_write_text(
            OutputNamespace.LIVE, "out.txt", "live", user_id="dave", base_dir=self.base
        )
        self.assertIn("dave", str(path))
        self.assertIn("live", str(path))

    def test_portfolio_writes_to_portfolio(self):
        path = safe_write_text(
            OutputNamespace.PORTFOLIO, "snap.md", "snap", base_dir=self.base
        )
        self.assertIn("portfolio", str(path))


class TestSafeWriteJson(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_valid_json(self):
        payload = {"key": "value", "n": 42}
        path = safe_write_json(OutputNamespace.POLICY, "result.json", payload, base_dir=self.base)
        loaded = json.loads(path.read_text())
        self.assertEqual(loaded, payload)

    def test_writes_to_correct_namespace(self):
        path = safe_write_json(OutputNamespace.HISTORICAL, "cal.json", {}, base_dir=self.base)
        self.assertIn("backtest", str(path))

    def test_sandbox_namespace(self):
        path = safe_write_json(OutputNamespace.SANDBOX, "test.json", [1, 2, 3], base_dir=self.base)
        self.assertIn("sandbox", str(path))

    def test_user_scoped_json(self):
        path = safe_write_json(
            OutputNamespace.USER, "prefs.json", {"theme": "dark"},
            user_id="eve", base_dir=self.base
        )
        self.assertIn("users", str(path))
        self.assertIn("eve", str(path))
        loaded = json.loads(path.read_text())
        self.assertEqual(loaded["theme"], "dark")

    def test_non_serializable_uses_str_fallback(self):
        from datetime import datetime
        payload = {"ts": datetime(2026, 1, 1)}
        path = safe_write_json(OutputNamespace.SANDBOX, "dt.json", payload, base_dir=self.base)
        self.assertTrue(path.exists())


class TestNamespaceForExistingPath(unittest.TestCase):

    def test_detects_backtest(self):
        self.assertEqual(
            namespace_for_existing_path("outputs/backtest/cal.json"),
            OutputNamespace.HISTORICAL,
        )

    def test_detects_policy(self):
        self.assertEqual(
            namespace_for_existing_path("outputs/policy/recommendation_outcomes.json"),
            OutputNamespace.POLICY,
        )

    def test_detects_portfolio(self):
        self.assertEqual(
            namespace_for_existing_path("outputs/portfolio/snapshot.json"),
            OutputNamespace.PORTFOLIO,
        )

    def test_detects_latest(self):
        self.assertEqual(
            namespace_for_existing_path("outputs/latest/decision_plan.json"),
            OutputNamespace.LATEST,
        )

    def test_detects_sandbox(self):
        self.assertEqual(
            namespace_for_existing_path("/tmp/outputs/sandbox/test.json"),
            OutputNamespace.SANDBOX,
        )

    def test_detects_live(self):
        self.assertEqual(
            namespace_for_existing_path("outputs/live/alice/out.json"),
            OutputNamespace.LIVE,
        )

    def test_detects_users(self):
        self.assertEqual(
            namespace_for_existing_path("outputs/users/bob/prefs.json"),
            OutputNamespace.USER,
        )

    def test_unknown_path_returns_none(self):
        self.assertIsNone(namespace_for_existing_path("/some/random/path/file.json"))

    def test_absolute_path_works(self):
        self.assertEqual(
            namespace_for_existing_path("/opt/stockbot/outputs/policy/eval.json"),
            OutputNamespace.POLICY,
        )


class TestPoliciesHelper(unittest.TestCase):

    def test_all_namespaces_have_policy(self):
        policies = get_policies()
        for ns in OutputNamespace:
            self.assertIn(ns, policies)

    def test_live_is_user_scoped(self):
        policies = get_policies()
        self.assertTrue(policies[OutputNamespace.LIVE].user_scoped)

    def test_user_is_user_scoped(self):
        policies = get_policies()
        self.assertTrue(policies[OutputNamespace.USER].user_scoped)

    def test_historical_not_user_scoped(self):
        policies = get_policies()
        self.assertFalse(policies[OutputNamespace.HISTORICAL].user_scoped)

    def test_policy_returns_outputpathpolicy_instances(self):
        policies = get_policies()
        for _, pol in policies.items():
            self.assertIsInstance(pol, OutputPathPolicy)


if __name__ == "__main__":
    unittest.main()
