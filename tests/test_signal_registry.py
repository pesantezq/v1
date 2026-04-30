"""
Tests for portfolio_automation.signal_registry.

Contracts verified:
- registry loads from YAML config
- get() returns known signal or None
- require() returns known signal or raises SignalRegistryError
- duplicate signal_id rejected at construction
- disabled signals excluded from enabled()
- by_category returns matching signals
- by_source_domain returns matching signals
- is_actionable returns True only for known actionable signals
- is_discovery_only returns True for discovery signals and unknown signals
- requires_corroboration returns True for corroboration signals and unknown
- unknown signals are not actionable
- discovery_only=True requires requires_corroboration=True (validated at load)
- invalid default_weight rejected
- actionable + discovery_only both True rejected
- annotate_signal returns correct shape for known signal
- annotate_signal returns safe non-actionable shape for unknown signal
- seeded registry contains all expected repo signal IDs
- validate_signal_id returns True for known, False for unknown
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from portfolio_automation.signal_registry import (
    SignalDefinition,
    SignalRegistry,
    SignalRegistryError,
    load_signal_registry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_YAML = textwrap.dedent("""\
    signals:
      - signal_id: TEST_SIGNAL
        display_name: Test Signal
        category: price_action
        source_domain: scanner
        actionable: true
        discovery_only: false
        requires_corroboration: false
        default_weight: 0.40
        confidence_floor: 0.50
        description: A test signal for unit tests.
        enabled: true
""")

_DISABLED_YAML = textwrap.dedent("""\
    signals:
      - signal_id: ACTIVE_SIGNAL
        display_name: Active
        category: price_action
        source_domain: scanner
        actionable: true
        discovery_only: false
        requires_corroboration: false
        default_weight: 0.40
        confidence_floor: null
        description: Active signal.
        enabled: true
      - signal_id: INACTIVE_SIGNAL
        display_name: Inactive
        category: volume
        source_domain: scanner
        actionable: false
        discovery_only: false
        requires_corroboration: true
        default_weight: 0.10
        confidence_floor: null
        description: Disabled signal.
        enabled: false
""")

_DISCOVERY_YAML = textwrap.dedent("""\
    signals:
      - signal_id: DISCOVERY_SIG
        display_name: Discovery Signal
        category: price_action
        source_domain: discovery
        actionable: false
        discovery_only: true
        requires_corroboration: true
        default_weight: 0.20
        confidence_floor: null
        description: Discovery-only signal.
        enabled: true
""")


def _write_temp_yaml(content: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


def _make_registry(*defs: SignalDefinition) -> SignalRegistry:
    return SignalRegistry(list(defs))


def _minimal_def(**overrides) -> SignalDefinition:
    defaults = dict(
        signal_id="SIG_A",
        display_name="Signal A",
        category="price_action",
        source_domain="scanner",
        actionable=True,
        discovery_only=False,
        requires_corroboration=False,
        default_weight=0.40,
        confidence_floor=0.50,
        description="Minimal test signal.",
        enabled=True,
    )
    defaults.update(overrides)
    return SignalDefinition(**defaults)


# ---------------------------------------------------------------------------
# Load from YAML
# ---------------------------------------------------------------------------

class TestLoadSignalRegistry(unittest.TestCase):

    def test_loads_from_yaml_file(self):
        p = _write_temp_yaml(_MINIMAL_YAML)
        registry = load_signal_registry(p)
        self.assertIsInstance(registry, SignalRegistry)

    def test_loads_from_path_string(self):
        p = _write_temp_yaml(_MINIMAL_YAML)
        registry = load_signal_registry(str(p))
        self.assertIsInstance(registry, SignalRegistry)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_signal_registry("/nonexistent/path/signal_registry.yaml")

    def test_invalid_yaml_raises(self):
        p = _write_temp_yaml("signals: [invalid: : yaml")
        with self.assertRaises(SignalRegistryError):
            load_signal_registry(p)

    def test_missing_signals_key_raises(self):
        p = _write_temp_yaml("not_signals:\n  - foo: bar\n")
        with self.assertRaises(SignalRegistryError):
            load_signal_registry(p)

    def test_signals_not_list_raises(self):
        p = _write_temp_yaml("signals: not_a_list\n")
        with self.assertRaises(SignalRegistryError):
            load_signal_registry(p)

    def test_missing_required_field_raises(self):
        yaml = textwrap.dedent("""\
            signals:
              - signal_id: INCOMPLETE
                display_name: Incomplete
                # missing category and other required fields
        """)
        p = _write_temp_yaml(yaml)
        with self.assertRaises(SignalRegistryError):
            load_signal_registry(p)

    def test_loads_seeded_registry(self):
        """Default config/signal_registry.yaml must load without error."""
        registry = load_signal_registry(
            Path(__file__).parent.parent / "config" / "signal_registry.yaml"
        )
        self.assertGreater(len(registry.all()), 0)


# ---------------------------------------------------------------------------
# get / require
# ---------------------------------------------------------------------------

class TestGetAndRequire(unittest.TestCase):

    def setUp(self):
        p = _write_temp_yaml(_MINIMAL_YAML)
        self.registry = load_signal_registry(p)

    def test_get_known_signal_returns_definition(self):
        d = self.registry.get("TEST_SIGNAL")
        self.assertIsNotNone(d)
        self.assertEqual(d.signal_id, "TEST_SIGNAL")

    def test_get_unknown_signal_returns_none(self):
        self.assertIsNone(self.registry.get("NONEXISTENT"))

    def test_require_known_signal_returns_definition(self):
        d = self.registry.require("TEST_SIGNAL")
        self.assertEqual(d.signal_id, "TEST_SIGNAL")

    def test_require_unknown_signal_raises(self):
        with self.assertRaises(SignalRegistryError):
            self.registry.require("NONEXISTENT")

    def test_require_error_message_contains_signal_id(self):
        try:
            self.registry.require("MY_MISSING_SIGNAL")
            self.fail("Expected SignalRegistryError")
        except SignalRegistryError as exc:
            self.assertIn("MY_MISSING_SIGNAL", str(exc))


# ---------------------------------------------------------------------------
# Duplicate signal_id
# ---------------------------------------------------------------------------

class TestDuplicateSignalId(unittest.TestCase):

    def test_duplicate_in_yaml_raises(self):
        yaml = textwrap.dedent("""\
            signals:
              - signal_id: DUP_SIG
                display_name: First
                category: price_action
                source_domain: scanner
                actionable: true
                discovery_only: false
                requires_corroboration: false
                default_weight: 0.40
                confidence_floor: null
                description: First definition.
                enabled: true
              - signal_id: DUP_SIG
                display_name: Second
                category: volume
                source_domain: scanner
                actionable: false
                discovery_only: false
                requires_corroboration: true
                default_weight: 0.20
                confidence_floor: null
                description: Duplicate.
                enabled: true
        """)
        p = _write_temp_yaml(yaml)
        with self.assertRaises(SignalRegistryError):
            load_signal_registry(p)

    def test_duplicate_in_constructor_raises(self):
        d1 = _minimal_def(signal_id="X")
        d2 = _minimal_def(signal_id="X")
        with self.assertRaises(SignalRegistryError):
            SignalRegistry([d1, d2])


# ---------------------------------------------------------------------------
# enabled()
# ---------------------------------------------------------------------------

class TestEnabled(unittest.TestCase):

    def setUp(self):
        p = _write_temp_yaml(_DISABLED_YAML)
        self.registry = load_signal_registry(p)

    def test_enabled_excludes_disabled(self):
        ids = {d.signal_id for d in self.registry.enabled()}
        self.assertIn("ACTIVE_SIGNAL", ids)
        self.assertNotIn("INACTIVE_SIGNAL", ids)

    def test_all_includes_disabled(self):
        ids = {d.signal_id for d in self.registry.all()}
        self.assertIn("ACTIVE_SIGNAL", ids)
        self.assertIn("INACTIVE_SIGNAL", ids)

    def test_get_disabled_signal_still_returns_it(self):
        d = self.registry.get("INACTIVE_SIGNAL")
        self.assertIsNotNone(d)
        self.assertFalse(d.enabled)


# ---------------------------------------------------------------------------
# by_category / by_source_domain
# ---------------------------------------------------------------------------

class TestFiltering(unittest.TestCase):

    def setUp(self):
        yaml = textwrap.dedent("""\
            signals:
              - signal_id: PRICE_SIG
                display_name: Price Signal
                category: price_action
                source_domain: scanner
                actionable: true
                discovery_only: false
                requires_corroboration: false
                default_weight: 0.40
                confidence_floor: null
                description: Price action signal.
                enabled: true
              - signal_id: VOL_SIG
                display_name: Volume Signal
                category: volume
                source_domain: scanner
                actionable: false
                discovery_only: false
                requires_corroboration: true
                default_weight: 0.20
                confidence_floor: null
                description: Volume signal.
                enabled: true
              - signal_id: PORT_SIG
                display_name: Portfolio Signal
                category: portfolio_risk
                source_domain: portfolio
                actionable: true
                discovery_only: false
                requires_corroboration: false
                default_weight: 0.80
                confidence_floor: 0.60
                description: Portfolio risk signal.
                enabled: true
        """)
        p = _write_temp_yaml(yaml)
        self.registry = load_signal_registry(p)

    def test_by_category_returns_matching(self):
        result = self.registry.by_category("price_action")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].signal_id, "PRICE_SIG")

    def test_by_category_empty_for_unknown_category(self):
        self.assertEqual(self.registry.by_category("nonexistent_cat"), [])

    def test_by_source_domain_scanner(self):
        ids = {d.signal_id for d in self.registry.by_source_domain("scanner")}
        self.assertIn("PRICE_SIG", ids)
        self.assertIn("VOL_SIG", ids)
        self.assertNotIn("PORT_SIG", ids)

    def test_by_source_domain_portfolio(self):
        result = self.registry.by_source_domain("portfolio")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].signal_id, "PORT_SIG")

    def test_by_source_domain_empty_for_unknown(self):
        self.assertEqual(self.registry.by_source_domain("nonexistent"), [])


# ---------------------------------------------------------------------------
# Boolean predicates
# ---------------------------------------------------------------------------

class TestBooleanPredicates(unittest.TestCase):

    def setUp(self):
        p = _write_temp_yaml(_MINIMAL_YAML + _DISCOVERY_YAML.replace("signals:\n", ""))
        # Build by combining both YAMLs cleanly
        combined = textwrap.dedent("""\
            signals:
              - signal_id: ACTIONABLE_SIG
                display_name: Actionable
                category: price_action
                source_domain: scanner
                actionable: true
                discovery_only: false
                requires_corroboration: false
                default_weight: 0.40
                confidence_floor: 0.50
                description: Actionable signal.
                enabled: true
              - signal_id: NON_ACTIONABLE_SIG
                display_name: Non-Actionable
                category: volume
                source_domain: scanner
                actionable: false
                discovery_only: false
                requires_corroboration: true
                default_weight: 0.20
                confidence_floor: null
                description: Not actionable, requires corroboration.
                enabled: true
              - signal_id: DISCOVERY_SIG
                display_name: Discovery
                category: price_action
                source_domain: discovery
                actionable: false
                discovery_only: true
                requires_corroboration: true
                default_weight: 0.15
                confidence_floor: null
                description: Discovery-only signal.
                enabled: true
        """)
        self.registry = load_signal_registry(_write_temp_yaml(combined))

    def test_is_actionable_true_for_actionable(self):
        self.assertTrue(self.registry.is_actionable("ACTIONABLE_SIG"))

    def test_is_actionable_false_for_non_actionable(self):
        self.assertFalse(self.registry.is_actionable("NON_ACTIONABLE_SIG"))

    def test_is_actionable_false_for_unknown(self):
        self.assertFalse(self.registry.is_actionable("TOTALLY_UNKNOWN"))

    def test_is_discovery_only_true_for_discovery(self):
        self.assertTrue(self.registry.is_discovery_only("DISCOVERY_SIG"))

    def test_is_discovery_only_false_for_actionable(self):
        self.assertFalse(self.registry.is_discovery_only("ACTIONABLE_SIG"))

    def test_is_discovery_only_true_for_unknown(self):
        self.assertTrue(self.registry.is_discovery_only("UNKNOWN_SIG"))

    def test_requires_corroboration_true_for_non_actionable(self):
        self.assertTrue(self.registry.requires_corroboration("NON_ACTIONABLE_SIG"))

    def test_requires_corroboration_false_for_actionable(self):
        self.assertFalse(self.registry.requires_corroboration("ACTIONABLE_SIG"))

    def test_requires_corroboration_true_for_unknown(self):
        self.assertTrue(self.registry.requires_corroboration("UNKNOWN_SIG"))

    def test_validate_signal_id_true_for_known(self):
        self.assertTrue(self.registry.validate_signal_id("ACTIONABLE_SIG"))

    def test_validate_signal_id_false_for_unknown(self):
        self.assertFalse(self.registry.validate_signal_id("MYSTERY_SIG"))


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------

class TestValidationRules(unittest.TestCase):

    def _yaml_for(self, **overrides) -> Path:
        base = dict(
            signal_id="VAL_SIG",
            display_name="Val Signal",
            category="price_action",
            source_domain="scanner",
            actionable="true",
            discovery_only="false",
            requires_corroboration="false",
            default_weight="0.40",
            confidence_floor="null",
            description="Validation test signal.",
            enabled="true",
        )
        base.update({k: str(v) for k, v in overrides.items()})
        lines = ["signals:"]
        lines.append("  - signal_id: " + base["signal_id"])
        lines.append("    display_name: " + base["display_name"])
        lines.append("    category: " + base["category"])
        lines.append("    source_domain: " + base["source_domain"])
        lines.append("    actionable: " + base["actionable"])
        lines.append("    discovery_only: " + base["discovery_only"])
        lines.append("    requires_corroboration: " + base["requires_corroboration"])
        lines.append("    default_weight: " + base["default_weight"])
        lines.append("    confidence_floor: " + base["confidence_floor"])
        lines.append("    description: " + base["description"])
        lines.append("    enabled: " + base["enabled"])
        return _write_temp_yaml("\n".join(lines) + "\n")

    def test_weight_above_1_rejected(self):
        with self.assertRaises(SignalRegistryError):
            load_signal_registry(self._yaml_for(default_weight="1.5"))

    def test_weight_below_0_rejected(self):
        with self.assertRaises(SignalRegistryError):
            load_signal_registry(self._yaml_for(default_weight="-0.1"))

    def test_weight_0_accepted(self):
        reg = load_signal_registry(self._yaml_for(default_weight="0.0"))
        self.assertIsNotNone(reg.get("VAL_SIG"))

    def test_weight_1_accepted(self):
        reg = load_signal_registry(self._yaml_for(default_weight="1.0"))
        self.assertIsNotNone(reg.get("VAL_SIG"))

    def test_actionable_and_discovery_only_both_true_rejected(self):
        with self.assertRaises(SignalRegistryError):
            load_signal_registry(self._yaml_for(
                actionable="true",
                discovery_only="true",
                requires_corroboration="true",
            ))

    def test_discovery_only_without_corroboration_rejected(self):
        with self.assertRaises(SignalRegistryError):
            load_signal_registry(self._yaml_for(
                actionable="false",
                discovery_only="true",
                requires_corroboration="false",
            ))

    def test_discovery_only_with_corroboration_accepted(self):
        reg = load_signal_registry(self._yaml_for(
            actionable="false",
            discovery_only="true",
            requires_corroboration="true",
        ))
        self.assertIsNotNone(reg.get("VAL_SIG"))


# ---------------------------------------------------------------------------
# annotate_signal
# ---------------------------------------------------------------------------

class TestAnnotateSignal(unittest.TestCase):

    def setUp(self):
        p = _write_temp_yaml(_MINIMAL_YAML)
        self.registry = load_signal_registry(p)

    def test_annotate_known_signal_returns_correct_shape(self):
        ann = self.registry.annotate_signal("TEST_SIGNAL")
        self.assertEqual(ann["signal_id"], "TEST_SIGNAL")
        self.assertTrue(ann["known"])
        self.assertEqual(ann["category"], "price_action")
        self.assertEqual(ann["source_domain"], "scanner")
        self.assertTrue(ann["actionable"])
        self.assertFalse(ann["discovery_only"])
        self.assertFalse(ann["requires_corroboration"])
        self.assertAlmostEqual(ann["default_weight"], 0.40)
        self.assertAlmostEqual(ann["confidence_floor"], 0.50)
        self.assertIn("description", ann)

    def test_annotate_unknown_signal_returns_safe_defaults(self):
        ann = self.registry.annotate_signal("UNKNOWN_XYZ")
        self.assertEqual(ann["signal_id"], "UNKNOWN_XYZ")
        self.assertFalse(ann["known"])
        self.assertFalse(ann["actionable"])
        self.assertTrue(ann["discovery_only"])
        self.assertTrue(ann["requires_corroboration"])
        self.assertEqual(ann["default_weight"], 0.0)
        self.assertIsNone(ann["confidence_floor"])
        self.assertEqual(ann["category"], "unknown")
        self.assertEqual(ann["source_domain"], "unknown")

    def test_annotate_merges_extra_metadata(self):
        ann = self.registry.annotate_signal("TEST_SIGNAL", metadata={"extra_key": "extra_value"})
        self.assertEqual(ann["extra_key"], "extra_value")
        self.assertTrue(ann["known"])

    def test_annotate_unknown_merges_extra_metadata(self):
        ann = self.registry.annotate_signal("UNKNOWN", metadata={"foo": 42})
        self.assertFalse(ann["known"])
        self.assertEqual(ann["foo"], 42)


# ---------------------------------------------------------------------------
# Seeded registry — expected repo signal IDs
# ---------------------------------------------------------------------------

_EXPECTED_SIGNAL_IDS = {
    # event_detection.py EventType enum
    "STRONG_MOVE_UP",
    "STRONG_MOVE_DOWN",
    "VOLUME_SPIKE",
    "BREAKOUT_PROXY",
    "VOLATILITY_EXPANSION",
    # portfolio structural violations (decision_engine.py violation_type)
    "LEVERAGE_VIOLATION",
    "CONCENTRATION_VIOLATION",
    "DRIFT_VIOLATION",
    # finance advisory (decision_engine.py recommendation id)
    "PORTFOLIO_DRIFT",
    # historical replay (replay_decision_simulator.py STRATEGY_NAME)
    "HISTORICAL_MOMENTUM_PROXY",
}


class TestSeededRegistry(unittest.TestCase):

    def setUp(self):
        self.registry = load_signal_registry(
            Path(__file__).parent.parent / "config" / "signal_registry.yaml"
        )

    def test_all_expected_signal_ids_present(self):
        known_ids = {d.signal_id for d in self.registry.all()}
        for sid in _EXPECTED_SIGNAL_IDS:
            self.assertIn(sid, known_ids, f"Expected signal_id {sid!r} not in seeded registry")

    def test_event_detection_signals_are_scanner_domain(self):
        for sid in ("STRONG_MOVE_UP", "STRONG_MOVE_DOWN", "VOLUME_SPIKE",
                    "BREAKOUT_PROXY", "VOLATILITY_EXPANSION"):
            d = self.registry.require(sid)
            self.assertEqual(d.source_domain, "scanner", f"{sid} should be scanner domain")

    def test_portfolio_violations_are_actionable(self):
        for sid in ("LEVERAGE_VIOLATION", "CONCENTRATION_VIOLATION", "DRIFT_VIOLATION"):
            d = self.registry.require(sid)
            self.assertTrue(d.actionable, f"{sid} should be actionable")

    def test_historical_momentum_proxy_is_not_actionable(self):
        d = self.registry.require("HISTORICAL_MOMENTUM_PROXY")
        self.assertFalse(d.actionable)
        self.assertTrue(d.discovery_only)
        self.assertEqual(d.source_domain, "historical_replay")

    def test_volatility_expansion_is_discovery_only(self):
        d = self.registry.require("VOLATILITY_EXPANSION")
        self.assertFalse(d.actionable)
        self.assertTrue(d.discovery_only)
        self.assertTrue(d.requires_corroboration)

    def test_volume_spike_requires_corroboration(self):
        d = self.registry.require("VOLUME_SPIKE")
        self.assertFalse(d.actionable)
        self.assertTrue(d.requires_corroboration)

    def test_breakout_proxy_has_confidence_floor(self):
        d = self.registry.require("BREAKOUT_PROXY")
        self.assertIsNotNone(d.confidence_floor)
        self.assertGreater(d.confidence_floor, 0.0)

    def test_leverage_violation_has_highest_weight(self):
        lev = self.registry.require("LEVERAGE_VIOLATION")
        all_weights = [d.default_weight for d in self.registry.all()]
        self.assertEqual(lev.default_weight, max(all_weights))

    def test_all_enabled_by_default(self):
        for d in self.registry.all():
            self.assertTrue(d.enabled, f"{d.signal_id} should be enabled in seeded registry")

    def test_no_signal_is_both_actionable_and_discovery_only(self):
        for d in self.registry.all():
            self.assertFalse(
                d.actionable and d.discovery_only,
                f"{d.signal_id} cannot be both actionable and discovery_only"
            )

    def test_all_discovery_only_require_corroboration(self):
        for d in self.registry.all():
            if d.discovery_only:
                self.assertTrue(
                    d.requires_corroboration,
                    f"{d.signal_id}: discovery_only=True must imply requires_corroboration=True"
                )


if __name__ == "__main__":
    unittest.main()
