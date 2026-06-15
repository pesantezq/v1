"""Phase 1 tests: FMP crowd-intelligence capability registry + probe.

Observe-only capability discovery. No adapters/scoring/decision logic here.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.crowd_intelligence import endpoint_registry as reg
from portfolio_automation.crowd_intelligence import fmp_capability_probe as probe
from portfolio_automation.crowd_intelligence.capability_store import CapabilityStore


class TestClassifier(unittest.TestCase):
    def test_available_200_nonempty(self):
        self.assertEqual(probe.classify(200, [{"symbol": "AAPL", "rating": "A"}], None), probe.AVAILABLE)

    def test_empty_ok_200_empty_list(self):
        self.assertEqual(probe.classify(200, [], None), probe.EMPTY_OK)

    def test_plan_locked_403(self):
        self.assertEqual(probe.classify(403, None, None), probe.PLAN_LOCKED)

    def test_plan_locked_402(self):
        self.assertEqual(probe.classify(402, None, None), probe.PLAN_LOCKED)

    def test_plan_locked_200_error_message(self):
        self.assertEqual(probe.classify(200, {"Error Message": "Exclusive Endpoint"}, None), probe.PLAN_LOCKED)

    def test_auth_error_401(self):
        self.assertEqual(probe.classify(401, None, None), probe.AUTH_ERROR)

    def test_not_found_404(self):
        self.assertEqual(probe.classify(404, None, None), probe.NOT_FOUND)

    def test_rate_limited_429(self):
        self.assertEqual(probe.classify(429, None, None), probe.RATE_LIMITED)

    def test_schema_changed_200_wrong_fields(self):
        self.assertEqual(
            probe.classify(200, [{"unexpected": 1}], None, expected_fields=["rating", "symbol"]),
            probe.SCHEMA_CHANGED)

    def test_schema_changed_200_unusable_shape(self):
        self.assertEqual(probe.classify(200, "not-json-ish", None), probe.SCHEMA_CHANGED)

    def test_network_error_negative_status(self):
        self.assertEqual(probe.classify(-1, None, "Timeout"), probe.NETWORK_ERROR)

    def test_network_error_none_status(self):
        self.assertEqual(probe.classify(None, None, None), probe.NETWORK_ERROR)


class TestProbeAll(unittest.TestCase):
    def test_classifies_each_and_records_fields(self):
        entries = [
            {"endpoint_id": "a", "path": "/stable/x", "params_template": {"symbol": "{symbol}"},
             "category": "news", "expected_fields": ["title"]},
            {"endpoint_id": "b", "path": "/stable/y", "params_template": {},
             "category": "analyst", "expected_fields": ["rating"]},
        ]

        def fake(path, params):
            if path == "/stable/x":
                return 200, [{"title": "hi"}], ""
            return 403, None, "HTTP 403"

        out = probe.probe_all(entries, fake, now_iso="2026-06-15T00:00:00Z")
        by_id = {r["endpoint_id"]: r for r in out}
        self.assertEqual(by_id["a"]["status"], probe.AVAILABLE)
        self.assertEqual(by_id["a"]["sample_fields"], ["title"])
        self.assertEqual(by_id["b"]["status"], probe.PLAN_LOCKED)

    def test_symbol_substitution_in_params(self):
        seen = {}

        def fake(path, params):
            seen.update(params)
            return 200, [{"title": 1}], ""

        probe.probe_all([{"endpoint_id": "a", "path": "/p",
                          "params_template": {"symbol": "{symbol}"}, "expected_fields": ["title"]}],
                        fake, symbol="MSFT")
        self.assertEqual(seen.get("symbol"), "MSFT")

    def test_one_failure_never_aborts(self):
        def boom(path, params):
            raise RuntimeError("connector exploded")

        out = probe.probe_all(
            [{"endpoint_id": "a", "path": "/p", "params_template": {}, "expected_fields": []}],
            boom)
        self.assertEqual(out[0]["status"], probe.NETWORK_ERROR)

    def test_hard_call_cap(self):
        entries = [{"endpoint_id": f"e{i}", "path": "/p", "params_template": {},
                    "expected_fields": []} for i in range(5)]
        calls = {"n": 0}

        def fake(path, params):
            calls["n"] += 1
            return 200, [{"x": 1}], ""

        out = probe.probe_all(entries, fake, max_calls=2)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(sum(1 for r in out if r["status"] == probe.SKIPPED_CAP), 3)


class TestRegistryIntegrity(unittest.TestCase):
    def test_every_entry_has_required_keys(self):
        for e in reg.all_entries():
            missing = reg._REQUIRED_KEYS - set(e.keys())
            self.assertEqual(missing, set(), f"{e.get('endpoint_id')} missing {missing}")

    def test_probe_targets_exclude_confirmed_baseline(self):
        ids = {e["endpoint_id"] for e in reg.probe_targets()}
        self.assertTrue(reg.CONFIRMED_BASELINE.isdisjoint(ids))
        # baseline endpoints still exist in the full registry
        all_ids = {e["endpoint_id"] for e in reg.all_entries()}
        self.assertTrue(reg.CONFIRMED_BASELINE.issubset(all_ids))

    def test_categories_cover_spec(self):
        cats = {e["category"] for e in reg.all_entries()}
        for expected in ("social", "news", "analyst", "insider", "congress", "attention"):
            self.assertIn(expected, cats)


class TestCanonicalRegistryCoverage(unittest.TestCase):
    def test_every_crowd_path_is_governed_by_canonical_registry(self):
        """CLAUDE.md: don't bypass the FMP registry. Every crowd candidate path
        must appear in fmp_endpoint_registry (as an endpoint or legacy_endpoint)."""
        import fmp_endpoint_registry as canon
        canonical_paths = set()
        for v in canon.REGISTRY.values():
            canonical_paths.add(v.get("endpoint"))
            if v.get("legacy_endpoint"):
                canonical_paths.add(v["legacy_endpoint"])
        uncovered = [e["path"] for e in reg.all_entries() if e["path"] not in canonical_paths]
        self.assertEqual(uncovered, [], f"crowd paths not in canonical registry: {uncovered}")

    def test_crowd_keys_not_in_stable_method_map(self):
        """Crowd endpoints are probe/adapter targets, NOT implemented client
        methods — they must stay out of STABLE_METHOD_MAP so compliance is intact."""
        from fmp_endpoint_compliance import STABLE_METHOD_MAP
        mapped_registry_keys = {rk for _, rk in STABLE_METHOD_MAP.values()}
        net_new = {"fmp_articles", "general_news", "stock_news_latest", "crypto_news",
                   "forex_news", "stock_grades", "grades_consensus",
                   "latest_insider_trading", "search_insider_trades",
                   "insider_trade_statistics", "senate_trading", "house_trading",
                   "biggest_gainers", "most_active", "sector_performance_snapshot"}
        self.assertTrue(net_new.isdisjoint(mapped_registry_keys))


class TestCapabilityStore(unittest.TestCase):
    def test_round_trip_and_upsert(self):
        with tempfile.TemporaryDirectory() as td:
            store = CapabilityStore(Path(td) / "crowd_intelligence.db")
            store.upsert([{"endpoint_id": "a", "status": "AVAILABLE", "http_status": 200,
                           "response_bytes": 12, "sample_fields": ["title"],
                           "last_checked_at": "2026-06-15T00:00:00Z", "error_summary": ""}])
            # upsert same id again (status change) -> no duplicate, value updated
            store.upsert([{"endpoint_id": "a", "status": "EMPTY_OK", "http_status": 200,
                           "response_bytes": 2, "sample_fields": [],
                           "last_checked_at": "2026-06-15T01:00:00Z", "error_summary": ""}])
            rows = store.all_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "EMPTY_OK")
            self.assertEqual(rows[0]["sample_fields"], [])


if __name__ == "__main__":
    unittest.main()
