"""Hermetic tests for tools/fmp_dependency_inventory.py (FMP-1, offline).

Synthetic mini-repos under tmp_path only; the real repo is read, never written.
No network, no credentials, never imports scanned files.
"""
from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "fmp_dependency_inventory_under_test", REPO / "tools" / "fmp_dependency_inventory.py")
inv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inv)

SECRET = "sk-FAKE-SECRET-0123456789"

FMP_CLIENT_SRC = '''\
_EP_QUOTE = "quote"
_EP_V3_SP500 = "v3/sp500_constituent"
_EP_V3_PROFILE_BATCH = "v3/profile"
_EP_HISTORICAL_DIVADJ = "historical-price-eod/dividend-adjusted"

class FMPClient:
    def get_sp500_constituents(self):
        return self._get_cached("sp500", _EP_V3_SP500)
    def get_batch_profiles_v3(self, syms):
        return self._raw_get(f"{_EP_V3_PROFILE_BATCH}/{','.join(syms)}", {})
    def get_quote(self, s):
        return self._raw_get(_EP_QUOTE, {"symbol": s})
    def adj(self, s):
        return self._raw_get(_EP_HISTORICAL_DIVADJ, {"symbol": s})
'''

CONSUMER_SRC = '''\
from fmp_client import FMPClient, CallBudgetExceeded

def run(path_from_registry, method_name):
    c = FMPClient(daily_budget=230)
    c.get_batch_quotes(["AAPL"])
    c.get_historical_prices("SPY")
    c.get_historical_prices_dividend_adjusted("SPY")
    c.get_stock_news(["AAPL"])
    c.get_key_metrics("AAPL")
    c.get_json(path_from_registry, {})
    getattr(fmp_client_obj, method_name)()
    try:
        pass
    except CallBudgetExceeded:
        pass
'''

SHELL_SRC = '''\
#!/usr/bin/env bash
# comment mentioning FMP_API_KEY is ignored
[ -n "${FMP_API_KEY:-}" ] || fail "missing"
export FMP_API_KEY=%s
python -m fmp_endpoint_compliance
''' % SECRET


def _mini_repo(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True)
    (root / "pkg").mkdir()
    (root / "fmp_client.py").write_text(FMP_CLIENT_SRC, encoding="utf-8")
    (root / "pkg" / "consumer.py").write_text(CONSUMER_SRC, encoding="utf-8")
    (root / "scripts" / "preflight.sh").write_text(SHELL_SRC, encoding="utf-8")
    (root / "config.json").write_text(
        '{\n  "data_budget": {\n    "rate_per_min": 240,\n'
        f'    "fmp_api_key": "{SECRET}"\n  }}\n}}\n', encoding="utf-8")
    (root / ".env").write_text(f"FMP_API_KEY={SECRET}\n", encoding="utf-8")
    return root


def _find(result, **kw):
    return [f for f in result["findings"]
            if all(f.get(k) == v for k, v in kw.items())]


def test_classifies_direct_calls_with_exact_lines(tmp_path):
    r = inv.build_inventory(_mini_repo(tmp_path))
    rel = "pkg/consumer.py"
    assert _find(r, path=rel, symbol="get_batch_quotes", risk="quote", line=5)
    assert _find(r, path=rel, symbol="get_historical_prices", risk="eod_history", line=6)
    assert _find(r, path=rel, symbol="get_historical_prices_dividend_adjusted",
                 risk="adjusted_history_vs002", line=7)
    assert _find(r, path=rel, symbol="get_stock_news", risk="news", line=8)
    assert _find(r, path=rel, symbol="get_key_metrics", risk="fundamentals", line=9)
    assert _find(r, path=rel, kind="client_construction", symbol="FMPClient", line=4)
    assert _find(r, path=rel, kind="quota_kwarg", risk="quota_budget", line=4)
    assert _find(r, path=rel, kind="quota_reference", symbol="CallBudgetExceeded")


def test_resolves_endpoint_constants_and_flags_legacy_v3(tmp_path):
    r = inv.build_inventory(_mini_repo(tmp_path))
    sp = _find(r, path="fmp_client.py", symbol="_get_cached", line=8)
    assert sp and sp[0]["detail"] == "v3/sp500_constituent"
    assert sp[0]["risk"] == "universe" and sp[0]["legacy_v3_v4"]
    prof = _find(r, path="fmp_client.py", symbol="_raw_get", line=10)
    assert prof and prof[0]["detail"] == "v3/profile/{...}" and prof[0]["legacy_v3_v4"]
    q = _find(r, path="fmp_client.py", symbol="_raw_get", line=12)
    assert q[0]["risk"] == "quote" and q[0]["legacy_v3_v4"] is None
    adj = _find(r, path="fmp_client.py", symbol="_raw_get", line=14)
    assert adj[0]["risk"] == "adjusted_history_vs002"
    assert r["summary"]["legacy_v3_v4"] >= 2


def test_dynamic_paths_are_unknown_not_silently_complete(tmp_path):
    r = inv.build_inventory(_mini_repo(tmp_path))
    rel = "pkg/consumer.py"
    assert _find(r, path=rel, kind="dynamic_endpoint", risk="unknown", line=10)
    assert _find(r, path=rel, kind="dynamic_dispatch", risk="unknown", line=11)
    assert r["summary"]["unknown"] >= 2


def test_parse_error_is_reported_as_unknown(tmp_path):
    (tmp_path / "broken.py").write_text("def x(:\n  pass\n", encoding="utf-8")
    r = inv.build_inventory(tmp_path)
    assert _find(r, path="broken.py", kind="parse_error", risk="unknown")


def test_output_is_deterministic_and_sorted(tmp_path):
    root = _mini_repo(tmp_path)
    a = inv.to_json(inv.build_inventory(root))
    b = inv.to_json(inv.build_inventory(root))
    assert a == b
    findings = inv.build_inventory(root)["findings"]
    assert findings == sorted(findings, key=inv._sort_key)
    assert str(tmp_path) not in a  # no absolute machine paths in the payload


def test_status_is_always_not_ready(tmp_path):
    r = inv.build_inventory(_mini_repo(tmp_path))
    assert r["status"] == "NOT_READY"
    assert "UNVERIFIED" in r["status_reason"]


def test_no_network_used(tmp_path, monkeypatch):
    def _deny(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    monkeypatch.setattr(socket, "getaddrinfo", _deny)
    r = inv.build_inventory(_mini_repo(tmp_path))
    assert r["findings"]


def test_missing_root_and_absent_sources(tmp_path, capsys):
    r = inv.build_inventory(tmp_path / "does-not-exist")
    assert r["status"] == "ERROR" and r["error"] == "repo_root_not_found"
    assert inv.main(["--repo-root", str(tmp_path / "does-not-exist")]) == 2
    capsys.readouterr()
    empty = inv.build_inventory(tmp_path)
    assert empty["absent_expected_sources"] == sorted(inv.EXPECTED_SOURCES)
    assert empty["findings"] == []


def test_never_reads_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", SECRET + "-ENV")
    root = _mini_repo(tmp_path)
    payload = inv.to_json(inv.build_inventory(root))
    assert SECRET not in payload
    assert "sk-FAKE" not in payload
    scanned = [p.name for p in inv.iter_source_files(root)]
    assert ".env" not in scanned
    # the credential gate is recorded by NAME only
    assert '"credential_presence_gate"' in payload


def test_never_imports_or_executes_scanned_files(tmp_path):
    sentinel = tmp_path / "EXECUTED"
    (tmp_path / "evil_fmp.py").write_text(
        f"open({str(sentinel)!r}, 'w').write('x')\nraise SystemExit(9)\n"
        "client.get_batch_quotes(['A'])\n", encoding="utf-8")
    r = inv.build_inventory(tmp_path)
    assert not sentinel.exists()
    assert "evil_fmp" not in sys.modules
    assert _find(r, path="evil_fmp.py", symbol="get_batch_quotes", line=3)


def test_tests_dir_excluded_by_default(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("x.get_batch_quotes([])\n", encoding="utf-8")
    assert inv.build_inventory(tmp_path)["findings"] == []
    assert inv.build_inventory(tmp_path, include_tests=True)["findings"]


def test_cli_writes_only_explicit_output(tmp_path):
    root = _mini_repo(tmp_path / "repo")
    out = tmp_path / "inv.json"
    assert inv.main(["--repo-root", str(root), "--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == inv.to_json(inv.build_inventory(root))


def test_real_repo_read_only_smoke():
    """Read-only scan of THIS checkout; pins anchors cited in the coverage doc."""
    r = inv.build_inventory(REPO)
    assert r["status"] == "NOT_READY"
    assert r["absent_expected_sources"] == []
    assert _find(r, path="market_data.py", symbol="get_batch_quotes", line=40)
    assert _find(r, path="market_data.py", symbol="governed_client", line=33)
    assert _find(r, path="universe/fmp_universe.py",
                 symbol="get_sp500_constituents", line=200)
    assert _find(r, path="universe/fmp_universe.py",
                 symbol="get_batch_profiles_v3", line=220)
    assert _find(r, path="portfolio_automation/vs002_evidence/production_runner.py",
                 symbol="get_dividend_adjusted_bars_strict_live", line=169)
    v3 = _find(r, path="fmp_client.py", symbol="_get_cached", line=461)
    assert v3 and v3[0]["detail"] == "v3/sp500_constituent"



def test_redacts_embedded_credential_literals(tmp_path):
    token = "TOP_SECRET_987"
    (tmp_path / "fmp_client.py").write_text(
        'BASE = "https://financialmodelingprep.com/stable/quote?apikey='
        + token + '"\n'
        + 'def run(client):\n'
        + '    client.get_json("v3/profile?apikey=' + token + '")\n'
        + '    getattr(client, "get_batch_" + "' + token + '")([])\n'
        + '    FMPClient(api_key="' + token + '")\n',
        encoding="utf-8",
    )
    (tmp_path / "task.sh").write_text(
        'FMP_API_KEY="' + token + '"\n', encoding="utf-8")
    report = inv.build_inventory(tmp_path)
    payload = inv.to_json(report)
    assert token not in payload
    assert "query_elided" in payload
    assert report["status"] == "NOT_READY"
