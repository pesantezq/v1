"""
Tests for tools/manual_portfolio_update.py
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from tools.manual_portfolio_update import (
    ManualPortfolioUpdateError,
    ManualUpdateResult,
    UpdateDiff,
    parse_holdings_csv,
    parse_holdings_json,
    run_manual_portfolio_update,
    main,
    _parse_as_of,
    _parse_cash,
    _SAFETY_DISCLAIMER,
    _AUDIT_JSONL_RELATIVE,
    _ALLOWED_COLUMNS,
)
from portfolio_automation.run_mode_governance import RunMode, RunModeViolation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_config(base: Path, *, holdings=None, cash=464.16) -> Path:
    if holdings is None:
        holdings = [
            {"symbol": "QQQ", "shares": 6, "target_weight": 0.35,
             "asset_class": "us_equity", "is_leveraged": False, "leverage_factor": 1},
            {"symbol": "GLD", "shares": 4, "target_weight": 0.20,
             "asset_class": "commodity", "is_leveraged": False, "leverage_factor": 1},
        ]
    payload = {
        "investor": {"name": "Test", "age": 30, "risk_tolerance": "moderate"},
        "portfolio": {
            "holdings": holdings,
            "cash_available": cash,
            "target_cash_weight": 0.05,
            "rebalance_rules": {"min_drift": 0.05},
        },
        "providers": {"alpha_vantage": {"enabled": True}},
    }
    path = base / "config.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_csv(base: Path, body: str, name: str = "update.csv") -> Path:
    path = base / name
    path.write_text(body, encoding="utf-8")
    return path


def _write_json_input(base: Path, payload: dict, name: str = "update.json") -> Path:
    path = base / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. CSV parsing — validation rules
# ---------------------------------------------------------------------------

class TestCsvParsing:
    def test_valid_csv_parses(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,6\nGLD,4\n")
        holdings = parse_holdings_csv(csv)
        assert len(holdings) == 2
        assert holdings[0].symbol == "QQQ"
        assert holdings[0].shares == 6.0

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ManualPortfolioUpdateError, match="not found"):
            parse_holdings_csv(tmp_path / "nope.csv")

    def test_missing_symbol_column_rejected(self, tmp_path):
        csv = _write_csv(tmp_path, "ticker,shares\nQQQ,6\n")
        with pytest.raises(ManualPortfolioUpdateError, match="Missing required column"):
            parse_holdings_csv(csv)

    def test_missing_shares_column_rejected(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,quantity\nQQQ,6\n")
        with pytest.raises(ManualPortfolioUpdateError, match="Missing required column"):
            parse_holdings_csv(csv)

    def test_unsupported_column_rejected(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares,price\nQQQ,6,100\n")
        with pytest.raises(ManualPortfolioUpdateError, match="Unsupported column"):
            parse_holdings_csv(csv)

    def test_invalid_symbol_rejected(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares\nabc$,6\n")
        with pytest.raises(ManualPortfolioUpdateError, match="invalid symbol"):
            parse_holdings_csv(csv)

    def test_lowercase_symbol_upcased(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares\nqqq,6\n")
        holdings = parse_holdings_csv(csv)
        assert holdings[0].symbol == "QQQ"

    def test_negative_shares_rejected(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,-1\n")
        with pytest.raises(ManualPortfolioUpdateError, match="non-negative"):
            parse_holdings_csv(csv)

    def test_non_numeric_shares_rejected(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,abc\n")
        with pytest.raises(ManualPortfolioUpdateError, match="shares must be numeric"):
            parse_holdings_csv(csv)

    def test_duplicate_symbol_rejected(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,6\nQQQ,4\n")
        with pytest.raises(ManualPortfolioUpdateError, match="duplicate"):
            parse_holdings_csv(csv)

    def test_empty_csv_rejected(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares\n")
        with pytest.raises(ManualPortfolioUpdateError, match="no data rows"):
            parse_holdings_csv(csv)

    def test_blank_rows_skipped(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,6\n\n,\nGLD,4\n")
        holdings = parse_holdings_csv(csv)
        assert {h.symbol for h in holdings} == {"QQQ", "GLD"}

    def test_optional_columns_parsed(self, tmp_path):
        csv = _write_csv(
            tmp_path,
            "symbol,shares,target_weight,asset_class,is_leveraged,leverage_factor\n"
            "QQQ,6,0.35,us_equity,false,1\n"
        )
        h = parse_holdings_csv(csv)[0]
        assert h.target_weight == 0.35
        assert h.asset_class == "us_equity"
        assert h.is_leveraged is False
        assert h.leverage_factor == 1

    def test_target_weight_out_of_range_rejected(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares,target_weight\nQQQ,6,1.5\n")
        with pytest.raises(ManualPortfolioUpdateError, match=r"target_weight must be in"):
            parse_holdings_csv(csv)

    def test_invalid_leverage_factor_rejected(self, tmp_path):
        csv = _write_csv(tmp_path, "symbol,shares,leverage_factor\nQQQ,6,0\n")
        with pytest.raises(ManualPortfolioUpdateError, match="leverage_factor must be >= 1"):
            parse_holdings_csv(csv)


# ---------------------------------------------------------------------------
# 2. JSON input parsing
# ---------------------------------------------------------------------------

class TestJsonInput:
    def test_valid_json_parses(self, tmp_path):
        path = _write_json_input(tmp_path, {
            "holdings": [{"symbol": "QQQ", "shares": 6}]
        })
        holdings = parse_holdings_json(path)
        assert holdings[0].symbol == "QQQ"

    def test_invalid_json_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(ManualPortfolioUpdateError, match="Invalid JSON"):
            parse_holdings_json(path)

    def test_missing_holdings_key_rejected(self, tmp_path):
        path = _write_json_input(tmp_path, {"foo": "bar"})
        with pytest.raises(ManualPortfolioUpdateError, match="holdings"):
            parse_holdings_json(path)

    def test_unsupported_field_in_json_rejected(self, tmp_path):
        path = _write_json_input(tmp_path, {
            "holdings": [{"symbol": "QQQ", "shares": 6, "price": 100}]
        })
        with pytest.raises(ManualPortfolioUpdateError, match="unsupported field"):
            parse_holdings_json(path)


# ---------------------------------------------------------------------------
# 3. Date / cash parsers
# ---------------------------------------------------------------------------

class TestDateCash:
    def test_valid_date(self):
        assert _parse_as_of("2026-05-12") == "2026-05-12"

    def test_invalid_date_format(self):
        for bad in ("05/12/2026", "May 12 2026", "2026-13-01", "bad"):
            with pytest.raises(ManualPortfolioUpdateError, match="YYYY-MM-DD"):
                _parse_as_of(bad)

    def test_valid_cash(self):
        assert _parse_cash("100.50") == 100.5
        assert _parse_cash(0) == 0.0

    def test_negative_cash_rejected(self):
        with pytest.raises(ManualPortfolioUpdateError, match="non-negative"):
            _parse_cash("-10")

    def test_non_numeric_cash_rejected(self):
        with pytest.raises(ManualPortfolioUpdateError, match="numeric"):
            _parse_cash("abc")


# ---------------------------------------------------------------------------
# 4. Run-mode governance enforcement
# ---------------------------------------------------------------------------

class TestRunModeEnforcement:
    def test_approved_false_blocks_writes(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\n")
        with pytest.raises(RunModeViolation):
            run_manual_portfolio_update(
                input_path=csv,
                cash=500.0,
                as_of="2026-05-12",
                approved=False,
                config_path=config,
                base_dir=tmp_path,
            )
        # config.json must be untouched
        new = json.loads(config.read_text())
        assert new["portfolio"]["cash_available"] == 464.16
        assert not (tmp_path / "outputs").exists()

    def test_approved_true_allowed(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        result = run_manual_portfolio_update(
            input_path=csv,
            cash=500.0,
            as_of="2026-05-12",
            approved=True,
            config_path=config,
            base_dir=tmp_path,
        )
        assert result.success is True


# ---------------------------------------------------------------------------
# 5. End-to-end behavior
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_config_updated_with_new_shares(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,10\nGLD,4\n")
        run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        new = json.loads(config.read_text())
        qqq = next(h for h in new["portfolio"]["holdings"] if h["symbol"] == "QQQ")
        assert qqq["shares"] == 10.0
        assert new["portfolio"]["cash_available"] == 500.0

    def test_existing_metadata_preserved(self, tmp_path):
        """Existing target_weight/asset_class must survive when CSV omits them."""
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,10\nGLD,4\n")
        run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        new = json.loads(config.read_text())
        qqq = next(h for h in new["portfolio"]["holdings"] if h["symbol"] == "QQQ")
        assert qqq["target_weight"] == 0.35
        assert qqq["asset_class"] == "us_equity"
        assert qqq["leverage_factor"] == 1

    def test_new_symbol_gets_defaults(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,6\nGLD,4\nVOO,12\n")
        run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        new = json.loads(config.read_text())
        voo = next(h for h in new["portfolio"]["holdings"] if h["symbol"] == "VOO")
        assert voo["shares"] == 12.0
        assert voo["target_weight"] == 0.0
        assert voo["asset_class"] == "us_equity"
        assert voo["is_leveraged"] is False
        assert voo["leverage_factor"] == 1

    def test_removed_symbol_dropped_from_config(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,6\n")  # GLD missing
        result = run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        new = json.loads(config.read_text())
        symbols = {h["symbol"] for h in new["portfolio"]["holdings"]}
        assert symbols == {"QQQ"}
        assert "GLD" in result.diff.removed

    def test_other_config_keys_untouched(self, tmp_path):
        config = _write_config(tmp_path)
        before = json.loads(config.read_text())
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        after = json.loads(config.read_text())
        # investor, providers, rebalance_rules, target_cash_weight unchanged
        assert after["investor"] == before["investor"]
        assert after["providers"] == before["providers"]
        assert after["portfolio"]["rebalance_rules"] == before["portfolio"]["rebalance_rules"]
        assert after["portfolio"]["target_cash_weight"] == before["portfolio"]["target_cash_weight"]


# ---------------------------------------------------------------------------
# 6. Backup behavior
# ---------------------------------------------------------------------------

class TestBackup:
    def test_backup_file_written(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        result = run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        assert result.backup_path is not None
        assert result.backup_path.exists()
        assert "portfolio_backups" in str(result.backup_path)

    def test_backup_contains_pre_update_state(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,99\nGLD,4\n")
        result = run_manual_portfolio_update(
            input_path=csv, cash=999.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        backup = json.loads(result.backup_path.read_text())
        # Backup must contain prior state, not new state
        assert backup["portfolio"]["cash_available"] == 464.16
        qqq = next(h for h in backup["portfolio"]["holdings"] if h["symbol"] == "QQQ")
        assert qqq["shares"] == 6  # prior value, not 99

    def test_backup_in_policy_namespace(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        result = run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        # POLICY namespace = outputs/policy/
        assert (tmp_path / "outputs" / "policy" / "portfolio_backups").exists()
        assert "outputs" in str(result.backup_path)
        assert "policy" in str(result.backup_path)


# ---------------------------------------------------------------------------
# 7. Audit JSONL
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_audit_record_appended(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        result = run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        audit = tmp_path / "outputs" / "policy" / _AUDIT_JSONL_RELATIVE
        assert audit.exists()
        lines = audit.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        for key in (
            "run_id", "timestamp", "as_of", "mode", "approved",
            "prior_cash", "new_cash", "cash_delta",
            "prior_holdings_count", "new_holdings_count",
            "added", "removed", "changed",
            "observe_only", "no_trade", "not_recommendation",
            "no_allocation_policy_change", "no_watchlist_mutation",
            "no_discovery_promotion",
            "source", "safety_disclaimer",
        ):
            assert key in record, f"Audit missing key: {key}"

    def test_audit_safety_flags_all_true(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        audit = tmp_path / "outputs" / "policy" / _AUDIT_JSONL_RELATIVE
        record = json.loads(audit.read_text().strip())
        for flag in ("observe_only", "no_trade", "not_recommendation",
                     "no_allocation_policy_change",
                     "no_watchlist_mutation", "no_discovery_promotion"):
            assert record[flag] is True

    def test_audit_mode_is_manual_update(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        audit = tmp_path / "outputs" / "policy" / _AUDIT_JSONL_RELATIVE
        record = json.loads(audit.read_text().strip())
        assert record["mode"] == RunMode.MANUAL_UPDATE.value

    def test_two_runs_two_audit_lines(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        for _ in range(2):
            run_manual_portfolio_update(
                input_path=csv, cash=500.0, as_of="2026-05-12",
                approved=True, config_path=config, base_dir=tmp_path,
            )
        audit = tmp_path / "outputs" / "policy" / _AUDIT_JSONL_RELATIVE
        lines = audit.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_audit_diff_reflects_changes(self, tmp_path):
        config = _write_config(tmp_path)
        # QQQ 6→10, GLD removed, VOO new
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,10\nVOO,5\n")
        run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        audit = tmp_path / "outputs" / "policy" / _AUDIT_JSONL_RELATIVE
        record = json.loads(audit.read_text().strip())
        assert record["added"] == ["VOO"]
        assert record["removed"] == ["GLD"]
        changed_symbols = {c["symbol"] for c in record["changed"]}
        assert "QQQ" in changed_symbols


# ---------------------------------------------------------------------------
# 8. Dry-run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_no_files_written(self, tmp_path):
        config = _write_config(tmp_path)
        original = config.read_text()
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,99\nGLD,4\n")
        result = run_manual_portfolio_update(
            input_path=csv, cash=999.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
            dry_run=True,
        )
        assert result.dry_run is True
        assert config.read_text() == original
        assert not (tmp_path / "outputs").exists()

    def test_dry_run_returns_diff(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,99\nGLD,4\n")
        result = run_manual_portfolio_update(
            input_path=csv, cash=999.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
            dry_run=True,
        )
        assert result.diff.cash_delta == pytest.approx(999.0 - 464.16)
        assert any(c["symbol"] == "QQQ" for c in result.diff.changed)


# ---------------------------------------------------------------------------
# 9. CLI entrypoint
# ---------------------------------------------------------------------------

class TestCli:
    def test_cli_missing_approve_returns_2(self, tmp_path, capsys):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        rc = main([
            "--input", str(csv), "--cash", "500", "--as-of", "2026-05-12",
            "--config", str(config), "--base-dir", str(tmp_path),
        ])
        assert rc == 2
        out = capsys.readouterr()
        assert "--approve" in out.err

    def test_cli_success_returns_0(self, tmp_path, capsys):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        rc = main([
            "--input", str(csv), "--cash", "500", "--as-of", "2026-05-12",
            "--config", str(config), "--base-dir", str(tmp_path), "--approve",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Manual portfolio update" in out
        assert _SAFETY_DISCLAIMER in out

    def test_cli_dry_run_no_writes(self, tmp_path):
        config = _write_config(tmp_path)
        original = config.read_text()
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,99\nGLD,4\n")
        rc = main([
            "--input", str(csv), "--cash", "999", "--as-of", "2026-05-12",
            "--config", str(config), "--base-dir", str(tmp_path),
            "--approve", "--dry-run",
        ])
        assert rc == 0
        assert config.read_text() == original

    def test_cli_invalid_date_returns_2(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        rc = main([
            "--input", str(csv), "--cash", "500", "--as-of", "bad-date",
            "--config", str(config), "--base-dir", str(tmp_path), "--approve",
        ])
        assert rc == 2

    def test_cli_negative_cash_returns_2(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        rc = main([
            "--input", str(csv), "--cash", "-100", "--as-of", "2026-05-12",
            "--config", str(config), "--base-dir", str(tmp_path), "--approve",
        ])
        assert rc == 2

    def test_cli_missing_input_returns_2(self, tmp_path):
        config = _write_config(tmp_path)
        rc = main([
            "--input", str(tmp_path / "nope.csv"),
            "--cash", "500", "--as-of", "2026-05-12",
            "--config", str(config), "--base-dir", str(tmp_path), "--approve",
        ])
        assert rc == 2


# ---------------------------------------------------------------------------
# 10. Read-only invariant for non-target areas
# ---------------------------------------------------------------------------

class TestNoMutationBeyondScope:
    def test_no_latest_namespace_writes(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        # No outputs/latest/ writes from this tool
        latest_dir = tmp_path / "outputs" / "latest"
        if latest_dir.exists():
            files = list(latest_dir.iterdir())
            assert all("manual_portfolio_update" not in f.name for f in files)

    def test_no_sandbox_writes(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        # No sandbox / portfolio_snapshots / discovery writes
        assert not (tmp_path / "outputs" / "sandbox").exists()
        assert not (tmp_path / "outputs" / "portfolio").exists()

    def test_audit_does_not_emit_trading_action_strings(self, tmp_path):
        config = _write_config(tmp_path)
        csv = _write_csv(tmp_path, "symbol,shares\nQQQ,7\nGLD,4\n")
        run_manual_portfolio_update(
            input_path=csv, cash=500.0, as_of="2026-05-12",
            approved=True, config_path=config, base_dir=tmp_path,
        )
        audit = tmp_path / "outputs" / "policy" / _AUDIT_JSONL_RELATIVE
        record = json.loads(audit.read_text().strip())
        record_str = json.dumps(record)
        stripped = record_str.replace(_SAFETY_DISCLAIMER, "")
        for action in ("BUY", "SELL", "HOLD", "ACTIONABLE", "PROMOTED", "VALIDATED"):
            assert re.search(rf"\b{action}\b", stripped) is None, \
                f"Audit record leaks action token {action!r}"
