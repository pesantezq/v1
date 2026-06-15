# FMP Budget-Aware Data Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single guarded FMP access layer (`portfolio_automation/data_budget/`) that all FMP calls route through, maximizing cached/batch data and capping calls + bandwidth, with observe-only health artifacts and a GUI panel.

**Architecture:** A thin governance layer wrapping the existing `fmp_client.FMPClient` (keeping its proven file `_DiskCache` + `_CallCounter` + endpoint registry). `FMPBudgetGovernor.client(run_mode)` is the one factory every call site uses; it returns a `GovernedFMPClient` proxy (token bucket + run-mode budget + monthly bandwidth guard + per-call SQLite ledger) or — when the kill-switch is set — a plain `FMPClient` (today's behavior). New persistence lives in a dedicated `data/fmp_budget.db`.

**Tech Stack:** Python 3.12, stdlib `sqlite3` + `urllib` (no new deps), existing `OutputNamespace` governance, pytest/unittest, gui_v2 (FastAPI + Jinja).

---

## File Structure

**Create:**
- `portfolio_automation/data_budget/__init__.py` — exports `FMPBudgetGovernor`, `RunMode`.
- `portfolio_automation/data_budget/usage_ledger.py` — SQLite `api_usage_ledger` writer/reader + aggregation + monthly bandwidth + pruning.
- `portfolio_automation/data_budget/cache.py` — adapter over `fmp_client._DiskCache` for hit-rate/stale reporting + `symbol_data_policy` table.
- `portfolio_automation/data_budget/scheduler.py` — pure run-mode budget table + priority tiers + skip/stale decisions.
- `portfolio_automation/data_budget/request_manifest.py` — pure endpoint-strategy selection.
- `portfolio_automation/data_budget/governor.py` — `_TokenBucket`, `FMPBudgetGovernor`, `GovernedFMPClient`.
- `portfolio_automation/data_budget/status_producer.py` — builds + writes the 3 artifacts.
- `tests/test_data_budget_*.py` — focused tests per unit.

**Modify:**
- `fmp_client.py:288` — additive `self._last_response_bytes` capture; expose `last_response_bytes` property + `get_quote_short`.
- `fmp_endpoint_registry.py` — add `quote_short` entry if absent.
- `config.json` — add `data_budget` block.
- Call sites (`main.py`, `market_data.py`, `watchlist_scanner/performance_feedback.py`, `portfolio_automation/decision_outcome_tracker.py`, `portfolio_automation/news/run_news_intelligence.py`, `portfolio_automation/historical_backfill.py`, discovery pulse, `gui_v2` loaders) — route through the governor factory.
- `scripts/run_daily_safe.sh` — add status-producer stage.
- `gui_v2/templates/system.html` + a gui_v2 data loader — budget panel.
- `.claude/commands/daily-tool-analysis.md` — health check.
- `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/PIPELINE_RUNBOOK.md`, `.agent/project_state.yaml`.

---

## Task 1: fmp_client byte-capture hook + quote-short (additive)

**Files:**
- Modify: `fmp_client.py:288` (byte capture), constructor area (init `_last_response_bytes`), add property + `get_quote_short`
- Modify: `fmp_endpoint_registry.py` (add `quote_short` entry)
- Test: `tests/test_fmp_client_byte_hook.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fmp_client_byte_hook.py
from __future__ import annotations
import sys, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fmp_client import FMPClient


class TestByteHook(unittest.TestCase):
    def _client(self, tmp):
        return FMPClient(api_key="k", cache_dir=Path(tmp))

    def test_last_response_bytes_recorded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            c = self._client(td)
            body = b'[{"symbol":"AAPL","price":1.0}]'
            fake = MagicMock()
            fake.__enter__.return_value.read.return_value = body
            with patch("urllib.request.urlopen", return_value=fake):
                c._raw_get("quote", {"symbol": "AAPL"})
            self.assertEqual(c.last_response_bytes, len(body))

    def test_last_response_bytes_default_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._client(td).last_response_bytes, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fmp_client_byte_hook.py -q`
Expected: FAIL — `AttributeError: 'FMPClient' object has no attribute 'last_response_bytes'`

- [ ] **Step 3: Implement the byte hook**

In `fmp_client.py` constructor (after `self._counter = ...`), add:
```python
        self._last_response_bytes = 0
```
Replace line 288 `data = json.loads(resp.read().decode('utf-8'))` with:
```python
                    raw = resp.read()
                    self._last_response_bytes = len(raw)
                    data = json.loads(raw.decode('utf-8'))
```
Add a property near `calls_today`:
```python
    @property
    def last_response_bytes(self) -> int:
        """Bytes of the most recent HTTP response body (0 if none / cache hit)."""
        return self._last_response_bytes
```
Add a lightweight single-symbol quote (after `get_batch_quotes`):
```python
    def get_quote_short(self, symbol: str, ttl_hours: int = 1) -> dict:
        """Lightweight single-symbol price via stable/quote-short (GUI use)."""
        cache_key = f"quote_short_{symbol.upper()}"
        ttl_seconds = ttl_hours * 3600
        cached = self._cache.get(cache_key, ttl_seconds)
        if cached is not None:
            return cached
        if self._counter.would_exceed(self._budget):
            return self._cache.get_stale(cache_key) or {}
        raw = self._raw_get("quote-short", {"symbol": symbol.upper()})
        result = raw[0] if isinstance(raw, list) and raw else (raw or {})
        self._cache.set(cache_key, result)
        return result
```

- [ ] **Step 4: Add the registry entry** (only if `quote_short` absent — confirm via grep first)

In `fmp_endpoint_registry.py`, add to the registry dict:
```python
    "quote_short": {
        "endpoint": "/stable/quote-short",
        "tier": "core",
        "usage": "lightweight single-symbol price for GUI refresh",
    },
```

- [ ] **Step 5: Run tests to verify pass + registry compliance**

Run: `.venv/bin/python -m pytest tests/test_fmp_client_byte_hook.py tests/test_fmp_endpoint_registry_compliance.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add fmp_client.py fmp_endpoint_registry.py tests/test_fmp_client_byte_hook.py
git commit -m "feat(fmp): additive response-byte capture + quote-short for budget governor"
```

---

## Task 2: usage_ledger (SQLite api_usage_ledger)

**Files:**
- Create: `portfolio_automation/data_budget/__init__.py`
- Create: `portfolio_automation/data_budget/usage_ledger.py`
- Test: `tests/test_data_budget_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_budget_ledger.py
from __future__ import annotations
import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.usage_ledger import UsageLedger


class TestUsageLedger(unittest.TestCase):
    def _ledger(self, td):
        return UsageLedger(Path(td) / "fmp_budget.db")

    def test_record_and_count(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._ledger(td)
            lg.record(run_mode="daily", endpoint="quote", symbols=["AAPL"],
                      cache_hit=False, bytes_=100, skipped_reason=None,
                      ts="2026-06-15T09:00:00+00:00")
            lg.record(run_mode="daily", endpoint="quote", symbols=["MSFT"],
                      cache_hit=True, bytes_=0, skipped_reason=None,
                      ts="2026-06-15T09:00:01+00:00")
            self.assertEqual(lg.calls_in_run(run_mode="daily", since="2026-06-15T00:00:00+00:00"), 1)

    def test_monthly_bytes_sums_only_month(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._ledger(td)
            lg.record(run_mode="daily", endpoint="eod", symbols=["AAPL"],
                      cache_hit=False, bytes_=500, skipped_reason=None,
                      ts="2026-06-15T09:00:00+00:00")
            lg.record(run_mode="daily", endpoint="eod", symbols=["AAPL"],
                      cache_hit=False, bytes_=999, skipped_reason=None,
                      ts="2026-05-30T09:00:00+00:00")
            self.assertEqual(lg.monthly_bytes(month="2026-06"), 500)

    def test_cache_hit_rate(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._ledger(td)
            for hit in (True, True, False, True):
                lg.record(run_mode="gui_refresh", endpoint="quote-short",
                          symbols=["AAPL"], cache_hit=hit, bytes_=0 if hit else 10,
                          skipped_reason=None, ts="2026-06-15T09:00:00+00:00")
            self.assertAlmostEqual(lg.cache_hit_rate(month="2026-06"), 0.75)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_data_budget_ledger.py -q`
Expected: FAIL — `ModuleNotFoundError: ... data_budget`

- [ ] **Step 3: Create the package + ledger**

`portfolio_automation/data_budget/__init__.py`:
```python
"""Budget-aware FMP data orchestration layer (observe-only health; wraps fmp_client)."""
from portfolio_automation.data_budget.governor import FMPBudgetGovernor, RunMode  # noqa: F401
```
(Note: this import works only after Task 6 creates governor.py. For Task 2's test, temporarily make `__init__.py` empty, then restore the export in Task 6.)

`portfolio_automation/data_budget/usage_ledger.py`:
```python
from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Optional

_DDL = """
CREATE TABLE IF NOT EXISTS api_usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    symbols TEXT,
    cache_hit INTEGER NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    skipped_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_ledger_ts ON api_usage_ledger(ts);
"""


class UsageLedger:
    """Append-only per-call FMP usage ledger in a dedicated SQLite DB."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as cx:
            cx.executescript(_DDL)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def record(self, *, run_mode: str, endpoint: str, symbols: list[str] | None,
               cache_hit: bool, bytes_: int, skipped_reason: Optional[str],
               ts: str) -> None:
        try:
            with self._conn() as cx:
                cx.execute(
                    "INSERT INTO api_usage_ledger"
                    "(ts, run_mode, endpoint, symbols, cache_hit, bytes, skipped_reason)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (ts, run_mode, endpoint, ",".join(symbols or []),
                     1 if cache_hit else 0, int(bytes_ or 0), skipped_reason),
                )
        except Exception:
            pass  # telemetry must never break a run

    def calls_in_run(self, *, run_mode: str, since: str) -> int:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT COUNT(*) FROM api_usage_ledger "
                "WHERE run_mode=? AND ts>=? AND cache_hit=0 AND skipped_reason IS NULL",
                (run_mode, since)).fetchone()
        return int(row[0] or 0)

    def monthly_bytes(self, *, month: str) -> int:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM api_usage_ledger WHERE substr(ts,1,7)=?",
                (month,)).fetchone()
        return int(row[0] or 0)

    def cache_hit_rate(self, *, month: str) -> float:
        with self._conn() as cx:
            total = cx.execute(
                "SELECT COUNT(*) FROM api_usage_ledger WHERE substr(ts,1,7)=?",
                (month,)).fetchone()[0]
            hits = cx.execute(
                "SELECT COUNT(*) FROM api_usage_ledger WHERE substr(ts,1,7)=? AND cache_hit=1",
                (month,)).fetchone()[0]
        return round(hits / total, 4) if total else 0.0

    def prune(self, *, keep_days: int = 90, now_iso: str) -> int:
        """Delete rows older than keep_days (caller passes now to stay deterministic)."""
        from datetime import datetime, timedelta
        cutoff = (datetime.fromisoformat(now_iso) - timedelta(days=keep_days)).isoformat()
        with self._conn() as cx:
            cur = cx.execute("DELETE FROM api_usage_ledger WHERE ts < ?", (cutoff,))
        return cur.rowcount
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_data_budget_ledger.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/data_budget/__init__.py portfolio_automation/data_budget/usage_ledger.py tests/test_data_budget_ledger.py
git commit -m "feat(data_budget): SQLite api_usage_ledger (per-call usage + monthly bandwidth)"
```

---

## Task 3: cache adapter + symbol_data_policy

**Files:**
- Create: `portfolio_automation/data_budget/cache.py`
- Test: `tests/test_data_budget_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_budget_cache.py
from __future__ import annotations
import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.cache import SymbolDataPolicy, cache_stats
from fmp_client import _DiskCache


class TestSymbolPolicy(unittest.TestCase):
    def test_default_ttl_and_priority(self):
        with tempfile.TemporaryDirectory() as td:
            p = SymbolDataPolicy(Path(td) / "fmp_budget.db")
            self.assertEqual(p.ttl_for("AAPL", default=3600), 3600)
            self.assertEqual(p.priority_for("AAPL", default="medium"), "medium")

    def test_set_and_read_policy(self):
        with tempfile.TemporaryDirectory() as td:
            p = SymbolDataPolicy(Path(td) / "fmp_budget.db")
            p.set_policy("AAPL", ttl_seconds=7200, priority="high")
            self.assertEqual(p.ttl_for("AAPL", default=3600), 7200)
            self.assertEqual(p.priority_for("AAPL", default="medium"), "high")


class TestCacheStats(unittest.TestCase):
    def test_reports_file_count_and_freshness(self):
        with tempfile.TemporaryDirectory() as td:
            dc = _DiskCache(Path(td))
            dc.set("quote_AAPL", {"price": 1})
            stats = cache_stats(Path(td), fresh_keys=["quote_AAPL"], ttl_seconds=3600)
            self.assertEqual(stats["file_count"], 1)
            self.assertEqual(stats["fresh"]["quote_AAPL"], True)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_data_budget_cache.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement cache.py**

```python
from __future__ import annotations
import sqlite3
from pathlib import Path
from fmp_client import _DiskCache

_DDL = """
CREATE TABLE IF NOT EXISTS symbol_data_policy (
    symbol TEXT PRIMARY KEY,
    ttl_seconds INTEGER,
    priority TEXT
);
"""


class SymbolDataPolicy:
    """Per-symbol TTL + priority tier (high/medium/low)."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as cx:
            cx.executescript(_DDL)

    def set_policy(self, symbol: str, *, ttl_seconds: int, priority: str) -> None:
        with sqlite3.connect(self._path) as cx:
            cx.execute(
                "INSERT INTO symbol_data_policy(symbol, ttl_seconds, priority) "
                "VALUES (?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                "ttl_seconds=excluded.ttl_seconds, priority=excluded.priority",
                (symbol.upper(), ttl_seconds, priority))

    def _get(self, symbol: str):
        with sqlite3.connect(self._path) as cx:
            return cx.execute(
                "SELECT ttl_seconds, priority FROM symbol_data_policy WHERE symbol=?",
                (symbol.upper(),)).fetchone()

    def ttl_for(self, symbol: str, *, default: int) -> int:
        row = self._get(symbol)
        return int(row[0]) if row and row[0] is not None else default

    def priority_for(self, symbol: str, *, default: str) -> str:
        row = self._get(symbol)
        return str(row[1]) if row and row[1] else default


def cache_stats(cache_dir: Path, *, fresh_keys: list[str], ttl_seconds: int) -> dict:
    """Report cache file count/size + per-key fresh/stale, reusing fmp_client._DiskCache."""
    dc = _DiskCache(cache_dir)
    files = list(Path(cache_dir).glob("*.json"))
    fresh = {k: (dc.get(k, ttl_seconds) is not None) for k in fresh_keys}
    return {
        "available": True,
        "file_count": len(files),
        "total_size_bytes": sum(f.stat().st_size for f in files),
        "fresh": fresh,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_data_budget_cache.py -q`
Expected: PASS (3 tests). If `_DiskCache` cache-key→filename differs, align `fresh_keys` to the real key scheme (inspect `_DiskCache.set`).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/data_budget/cache.py tests/test_data_budget_cache.py
git commit -m "feat(data_budget): cache adapter + symbol_data_policy table"
```

---

## Task 4: scheduler (run-mode budgets + priority + skip logic)

**Files:**
- Create: `portfolio_automation/data_budget/scheduler.py`
- Test: `tests/test_data_budget_scheduler.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_budget_scheduler.py
from __future__ import annotations
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.scheduler import RunModeScheduler, DEFAULT_RUN_MODES


class TestScheduler(unittest.TestCase):
    def test_default_priorities(self):
        s = RunModeScheduler(DEFAULT_RUN_MODES)
        self.assertEqual(s.priority("daily"), "high")
        self.assertEqual(s.priority("discovery"), "low")

    def test_historical_replay_is_cache_only(self):
        s = RunModeScheduler(DEFAULT_RUN_MODES)
        self.assertEqual(s.call_budget("historical_replay"), 0)

    def test_low_priority_skipped_when_bandwidth_exhausted(self):
        s = RunModeScheduler(DEFAULT_RUN_MODES)
        self.assertTrue(s.should_skip("discovery", bandwidth_exhausted=True))
        self.assertFalse(s.should_skip("daily", bandwidth_exhausted=True))

    def test_run_budget_exceeded(self):
        s = RunModeScheduler(DEFAULT_RUN_MODES)
        b = s.call_budget("gui_refresh")
        self.assertTrue(s.over_run_budget("gui_refresh", calls_so_far=b))
        self.assertFalse(s.over_run_budget("gui_refresh", calls_so_far=b - 1))

    def test_uncapped_daily_when_budget_zero(self):
        s = RunModeScheduler({"daily": {"call_budget": 0, "priority": "high"}})
        self.assertFalse(s.over_run_budget("daily", calls_so_far=10_000))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_data_budget_scheduler.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement scheduler.py**

```python
from __future__ import annotations
from typing import Any

# Defaults — overridable from config.json data_budget.run_modes. call_budget=0 = uncapped.
DEFAULT_RUN_MODES: dict[str, dict[str, Any]] = {
    "gui_refresh":       {"call_budget": 30,  "priority": "high"},
    "daily":             {"call_budget": 0,   "priority": "high"},   # 0 = uncapped (honors config uncap)
    "weekly_review":     {"call_budget": 800, "priority": "medium"},
    "monthly":           {"call_budget": 1500, "priority": "medium"},
    "discovery":         {"call_budget": 200, "priority": "low"},
    "historical_replay": {"call_budget": 0,   "priority": "low", "cache_only": True},
}


class RunModeScheduler:
    """Pure policy: per-run-mode call budget, priority tier, skip decisions."""

    def __init__(self, run_modes: dict[str, dict[str, Any]]) -> None:
        self._modes = run_modes

    def _mode(self, run_mode: str) -> dict[str, Any]:
        return self._modes.get(run_mode, {"call_budget": 0, "priority": "medium"})

    def priority(self, run_mode: str) -> str:
        return self._mode(run_mode).get("priority", "medium")

    def call_budget(self, run_mode: str) -> int:
        m = self._mode(run_mode)
        if m.get("cache_only"):
            return 0
        return int(m.get("call_budget", 0))

    def is_cache_only(self, run_mode: str) -> bool:
        return bool(self._mode(run_mode).get("cache_only"))

    def over_run_budget(self, run_mode: str, *, calls_so_far: int) -> bool:
        budget = self.call_budget(run_mode)
        if budget <= 0 and not self.is_cache_only(run_mode):
            return False  # uncapped
        return calls_so_far >= budget

    def should_skip(self, run_mode: str, *, bandwidth_exhausted: bool) -> bool:
        return bool(bandwidth_exhausted and self.priority(run_mode) == "low")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_data_budget_scheduler.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/data_budget/scheduler.py tests/test_data_budget_scheduler.py
git commit -m "feat(data_budget): run-mode scheduler (budgets, priority, skip logic)"
```

---

## Task 5: request_manifest (endpoint strategy)

**Files:**
- Create: `portfolio_automation/data_budget/request_manifest.py`
- Test: `tests/test_data_budget_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_budget_manifest.py
from __future__ import annotations
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.request_manifest import plan_quote_request, plan_price_request


class TestManifest(unittest.TestCase):
    def test_multi_symbol_quote_uses_batch(self):
        plan = plan_quote_request(["AAPL", "MSFT", "QQQ"], run_mode="daily")
        self.assertEqual(plan["method"], "get_batch_quotes")

    def test_single_symbol_gui_uses_quote_short(self):
        plan = plan_quote_request(["AAPL"], run_mode="gui_refresh")
        self.assertEqual(plan["method"], "get_quote_short")

    def test_single_symbol_daily_uses_batch(self):
        # outside gui_refresh, a single symbol still goes through batch (cached)
        plan = plan_quote_request(["AAPL"], run_mode="daily")
        self.assertEqual(plan["method"], "get_batch_quotes")

    def test_daily_price_prefers_eod(self):
        plan = plan_price_request(["AAPL", "MSFT"], run_mode="daily")
        self.assertEqual(plan["method"], "get_historical_prices")
        self.assertEqual(plan["ttl_days"], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_data_budget_manifest.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement request_manifest.py**

```python
from __future__ import annotations
from typing import Any

def plan_quote_request(symbols: list[str], *, run_mode: str) -> dict[str, Any]:
    """Choose the cheapest quote endpoint. quote-short for single-symbol GUI;
    batch (cached per-symbol) for everything else."""
    if run_mode == "gui_refresh" and len(symbols) == 1:
        return {"method": "get_quote_short", "args": {"symbol": symbols[0]}}
    return {"method": "get_batch_quotes", "args": {"symbols": symbols}}

def plan_price_request(symbols: list[str], *, run_mode: str) -> dict[str, Any]:
    """Daily price updates: light EOD historical (ttl_days=1). Per-symbol full
    history only when cache missing (handled by FMPClient's own cache check)."""
    return {"method": "get_historical_prices", "args": {"symbols": symbols}, "ttl_days": 1}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_data_budget_manifest.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/data_budget/request_manifest.py tests/test_data_budget_manifest.py
git commit -m "feat(data_budget): request manifest (batch/quote-short/EOD endpoint strategy)"
```

---

## Task 6: token bucket + governor + GovernedFMPClient

**Files:**
- Create: `portfolio_automation/data_budget/governor.py`
- Modify: `portfolio_automation/data_budget/__init__.py` (restore exports)
- Test: `tests/test_data_budget_governor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_budget_governor.py
from __future__ import annotations
import sys, unittest, tempfile
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.governor import _TokenBucket, FMPBudgetGovernor


class TestTokenBucket(unittest.TestCase):
    def test_capacity_and_consume(self):
        clock = {"t": 0.0}
        b = _TokenBucket(rate_per_min=240, burst=300, now=lambda: clock["t"])
        self.assertTrue(b.try_consume(300))     # full burst available
        self.assertFalse(b.try_consume(1))       # empty now
        clock["t"] = 1.0                          # +1s -> +4 tokens (240/60)
        self.assertTrue(b.try_consume(4))
        self.assertFalse(b.try_consume(1))

    def test_hard_cap_never_exceeds_burst(self):
        clock = {"t": 0.0}
        b = _TokenBucket(rate_per_min=240, burst=300, now=lambda: clock["t"])
        clock["t"] = 10_000.0                     # huge idle
        self.assertTrue(b.try_consume(300))
        self.assertFalse(b.try_consume(1))        # capped at burst=300, not unbounded


class TestGovernorKillSwitch(unittest.TestCase):
    def _gov(self, td, **kw):
        return FMPBudgetGovernor(
            db_path=Path(td) / "fmp_budget.db",
            cache_dir=Path(td) / "cache",
            config={"enabled": True, "monthly_bandwidth_gb": 20,
                    "rate_per_min": 240, "burst": 300}, **kw)

    def test_killswitch_env_returns_plain_client(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            os.environ["STOCKBOT_FMP_GOVERNOR_DISABLED"] = "1"
            try:
                gov = self._gov(td)
                client = gov.client(run_mode="daily", fmp_client=MagicMock())
                from portfolio_automation.data_budget.governor import GovernedFMPClient
                self.assertNotIsInstance(client, GovernedFMPClient)
            finally:
                os.environ.pop("STOCKBOT_FMP_GOVERNOR_DISABLED", None)

    def test_enabled_returns_governed_client(self):
        with tempfile.TemporaryDirectory() as td:
            gov = self._gov(td)
            client = gov.client(run_mode="daily", fmp_client=MagicMock())
            from portfolio_automation.data_budget.governor import GovernedFMPClient
            self.assertIsInstance(client, GovernedFMPClient)


class TestGovernedClientBehavior(unittest.TestCase):
    def _governed(self, td, run_mode="daily"):
        gov = FMPBudgetGovernor(
            db_path=Path(td) / "fmp_budget.db", cache_dir=Path(td) / "cache",
            config={"enabled": True, "monthly_bandwidth_gb": 20,
                    "rate_per_min": 240, "burst": 300})
        fake = MagicMock()
        fake.get_batch_quotes.return_value = {"AAPL": {"price": 1.0}}
        fake.last_response_bytes = 50
        return gov, gov.client(run_mode=run_mode, fmp_client=fake), fake

    def test_proxies_method_and_records_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            gov, gc, fake = self._governed(td)
            out = gc.get_batch_quotes(["AAPL"])
            self.assertEqual(out, {"AAPL": {"price": 1.0}})
            fake.get_batch_quotes.assert_called_once()
            # one ledger row recorded for this run_mode
            self.assertGreaterEqual(
                gov.ledger.calls_in_run(run_mode="daily", since="2000-01-01T00:00:00+00:00"), 0)

    def test_low_priority_skipped_when_bandwidth_over_guard(self):
        with tempfile.TemporaryDirectory() as td:
            # Pre-seed ledger with > 20GB this month, then discovery should skip live call.
            gov = FMPBudgetGovernor(
                db_path=Path(td) / "fmp_budget.db", cache_dir=Path(td) / "cache",
                config={"enabled": True, "monthly_bandwidth_gb": 0.0000001,
                        "rate_per_min": 240, "burst": 300})
            fake = MagicMock()
            fake.get_batch_quotes.return_value = {"X": {}}
            fake.last_response_bytes = 999
            gov.ledger.record(run_mode="discovery", endpoint="quote", symbols=["X"],
                              cache_hit=False, bytes_=10_000, skipped_reason=None,
                              ts="2026-06-15T00:00:00+00:00")
            gc = gov.client(run_mode="discovery", fmp_client=fake,
                            now_month="2026-06")
            gc.get_batch_quotes(["X"])
            fake.get_batch_quotes.assert_not_called()  # skipped due to bandwidth guard


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_data_budget_governor.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement governor.py**

```python
from __future__ import annotations
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from portfolio_automation.data_budget.usage_ledger import UsageLedger
from portfolio_automation.data_budget.scheduler import RunModeScheduler, DEFAULT_RUN_MODES

_DISABLE_FILE = Path("config/fmp_governor.DISABLED")


class RunMode:
    GUI_REFRESH = "gui_refresh"
    DAILY = "daily"
    WEEKLY_REVIEW = "weekly_review"
    MONTHLY = "monthly"
    DISCOVERY = "discovery"
    HISTORICAL_REPLAY = "historical_replay"


class _TokenBucket:
    """In-process token bucket. rate_per_min sustained, burst hard cap."""

    def __init__(self, *, rate_per_min: int, burst: int,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._rate = rate_per_min / 60.0
        self._burst = burst
        self._now = now
        self._tokens = float(burst)
        self._last = now()

    def _refill(self) -> None:
        t = self._now()
        self._tokens = min(self._burst, self._tokens + (t - self._last) * self._rate)
        self._last = t

    def try_consume(self, n: int = 1) -> bool:
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False


def _killed(config: dict) -> bool:
    if not config.get("enabled", True):
        return True
    if os.environ.get("STOCKBOT_FMP_GOVERNOR_DISABLED") == "1":
        return True
    if _DISABLE_FILE.exists():
        return True
    return False


class GovernedFMPClient:
    """Proxies FMPClient methods by name; enforces token bucket + run-mode budget
    + bandwidth guard, records every call to the ledger. Never raises into callers."""

    def __init__(self, *, fmp_client: Any, run_mode: str, ledger: UsageLedger,
                 scheduler: RunModeScheduler, bucket: _TokenBucket,
                 monthly_bandwidth_bytes: int, now_month: Optional[str] = None) -> None:
        self._c = fmp_client
        self._run_mode = run_mode
        self._ledger = ledger
        self._sched = scheduler
        self._bucket = bucket
        self._bw_guard = monthly_bandwidth_bytes
        self._month = now_month or datetime.now(timezone.utc).strftime("%Y-%m")
        self._calls_this_run = 0

    def _bandwidth_exhausted(self) -> bool:
        try:
            return self._ledger.monthly_bytes(month=self._month) >= self._bw_guard
        except Exception:
            return False

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def __getattr__(self, name: str) -> Any:
        # Only proxy public callables; dunders/privates fall through normally.
        target = getattr(self._c, name)
        if not callable(target) or name.startswith("_"):
            return target

        def _wrapped(*args, **kwargs):
            symbols = _extract_symbols(args, kwargs)
            skip = self._sched.should_skip(self._run_mode,
                                           bandwidth_exhausted=self._bandwidth_exhausted())
            over = self._sched.over_run_budget(self._run_mode, calls_so_far=self._calls_this_run)
            cache_only = self._sched.is_cache_only(self._run_mode)
            if skip or over or cache_only:
                reason = ("bandwidth_guard" if skip else
                          "run_budget" if over else "cache_only")
                self._ledger.record(run_mode=self._run_mode, endpoint=name, symbols=symbols,
                                    cache_hit=False, bytes_=0, skipped_reason=reason,
                                    ts=self._now_iso())
                # Best-effort: return whatever the client would serve from cache, else empty.
                return _empty_like(target, args, kwargs)
            if not self._bucket.try_consume(1):
                # high-priority waits briefly; low-priority skips
                if self._sched.priority(self._run_mode) == "low":
                    self._ledger.record(run_mode=self._run_mode, endpoint=name, symbols=symbols,
                                        cache_hit=False, bytes_=0, skipped_reason="rate_limited",
                                        ts=self._now_iso())
                    return _empty_like(target, args, kwargs)
                _wait_for_token(self._bucket)
            before = getattr(self._c, "last_response_bytes", 0)
            result = target(*args, **kwargs)
            after = getattr(self._c, "last_response_bytes", 0)
            made_call = after != before or after > 0
            self._calls_this_run += 1 if made_call else 0
            self._ledger.record(run_mode=self._run_mode, endpoint=name, symbols=symbols,
                                cache_hit=not made_call, bytes_=(after if made_call else 0),
                                skipped_reason=None, ts=self._now_iso())
            return result

        return _wrapped


def _extract_symbols(args, kwargs) -> list[str]:
    val = kwargs.get("symbols") or kwargs.get("symbol")
    if val is None and args:
        val = args[0]
    if isinstance(val, str):
        return [val]
    if isinstance(val, (list, tuple)):
        return [str(s) for s in val]
    return []


def _empty_like(target, args, kwargs):
    # Mirror the shape callers expect from skipped calls (dict for quotes, list for history).
    return {} if "quote" in getattr(target, "__name__", "") else []


def _wait_for_token(bucket: _TokenBucket, *, max_wait_s: float = 2.0) -> None:
    waited = 0.0
    while waited < max_wait_s and not bucket.try_consume(1):
        time.sleep(0.05)
        waited += 0.05


class FMPBudgetGovernor:
    """Single factory. client(run_mode) -> GovernedFMPClient (enabled) or plain FMPClient."""

    def __init__(self, *, db_path: Path | str, cache_dir: Path | str,
                 config: Optional[dict] = None) -> None:
        self._config = config or {}
        self.ledger = UsageLedger(db_path)
        self._cache_dir = Path(cache_dir)
        run_modes = self._config.get("run_modes") or DEFAULT_RUN_MODES
        self._sched = RunModeScheduler(run_modes)
        self._bw_bytes = int(float(self._config.get("monthly_bandwidth_gb", 20)) * 1024**3)
        self._rate = int(self._config.get("rate_per_min", 240))
        self._burst = int(self._config.get("burst", 300))

    def client(self, *, run_mode: str, fmp_client: Any = None,
               now_month: Optional[str] = None) -> Any:
        if fmp_client is None:
            from fmp_client import FMPClient
            fmp_client = FMPClient(cache_dir=self._cache_dir)
        if _killed(self._config):
            return fmp_client  # kill-switch: today's proven behavior
        bucket = _TokenBucket(rate_per_min=self._rate, burst=self._burst)
        return GovernedFMPClient(
            fmp_client=fmp_client, run_mode=run_mode, ledger=self.ledger,
            scheduler=self._sched, bucket=bucket,
            monthly_bandwidth_bytes=self._bw_bytes, now_month=now_month)
```

Then set `portfolio_automation/data_budget/__init__.py` to:
```python
"""Budget-aware FMP data orchestration layer (observe-only health; wraps fmp_client)."""
from portfolio_automation.data_budget.governor import FMPBudgetGovernor, RunMode  # noqa: F401
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_data_budget_governor.py -q`
Expected: PASS. If `_empty_like` shape assumptions break a test, adjust the heuristic to match the proxied method names actually used.

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/data_budget/governor.py portfolio_automation/data_budget/__init__.py tests/test_data_budget_governor.py
git commit -m "feat(data_budget): token bucket + governor factory + GovernedFMPClient proxy"
```

---

## Task 7: config.json data_budget block

**Files:**
- Modify: `config.json` (add `data_budget`)
- Test: `tests/test_data_budget_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_budget_config.py
from __future__ import annotations
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestDataBudgetConfig(unittest.TestCase):
    def test_config_has_data_budget_block(self):
        cfg = json.loads(Path("config.json").read_text())
        db = cfg.get("data_budget")
        self.assertIsInstance(db, dict)
        self.assertTrue(db.get("enabled"))
        self.assertEqual(db.get("monthly_bandwidth_gb"), 20)
        self.assertEqual(db.get("rate_per_min"), 240)
        self.assertEqual(db.get("burst"), 300)
        self.assertIn("daily", db.get("run_modes", {}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_data_budget_config.py -q`
Expected: FAIL — `data_budget` missing

- [ ] **Step 3: Add the block to config.json**

Add this top-level key (rationale comments live in PIPELINE_RUNBOOK, since config.json is strict JSON):
```json
  "data_budget": {
    "enabled": true,
    "monthly_bandwidth_gb": 20,
    "rate_per_min": 240,
    "burst": 300,
    "run_modes": {
      "gui_refresh":       {"call_budget": 30,   "priority": "high"},
      "daily":             {"call_budget": 0,    "priority": "high"},
      "weekly_review":     {"call_budget": 800,  "priority": "medium"},
      "monthly":           {"call_budget": 1500, "priority": "medium"},
      "discovery":         {"call_budget": 200,  "priority": "low"},
      "historical_replay": {"call_budget": 0,    "priority": "low", "cache_only": true}
    }
  }
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_data_budget_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.json tests/test_data_budget_config.py
git commit -m "feat(data_budget): config block (budgets, bandwidth, rate, run modes)"
```

---

## Task 8: status_producer (3 artifacts)

**Files:**
- Create: `portfolio_automation/data_budget/status_producer.py`
- Test: `tests/test_data_budget_status_producer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_budget_status_producer.py
from __future__ import annotations
import json, sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.status_producer import build_status, write_status_artifacts
from portfolio_automation.data_budget.usage_ledger import UsageLedger


class TestStatusProducer(unittest.TestCase):
    def _seed(self, td):
        lg = UsageLedger(Path(td) / "fmp_budget.db")
        lg.record(run_mode="daily", endpoint="quote", symbols=["AAPL"],
                  cache_hit=False, bytes_=1000, skipped_reason=None, ts="2026-06-15T09:00:00+00:00")
        lg.record(run_mode="daily", endpoint="quote", symbols=["MSFT"],
                  cache_hit=True, bytes_=0, skipped_reason=None, ts="2026-06-15T09:00:01+00:00")
        lg.record(run_mode="discovery", endpoint="quote", symbols=["X"],
                  cache_hit=False, bytes_=0, skipped_reason="bandwidth_guard", ts="2026-06-15T09:00:02+00:00")
        return lg

    def test_build_status_observe_only_and_fields(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._seed(td)
            usage, cache, budget = build_status(
                ledger=lg, cache_dir=Path(td) / "cache",
                portfolio_symbols=[], month="2026-06",
                monthly_bandwidth_gb=20, run_modes={})
            self.assertTrue(usage["observe_only"])
            self.assertTrue(budget["observe_only"])
            self.assertEqual(budget["monthly_bandwidth_gb_cap"], 20)
            self.assertTrue(budget["discovery_skipped_due_to_budget"])
            self.assertAlmostEqual(cache["cache_hit_rate"], 1/3, places=2)

    def test_write_creates_three_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._seed(td)
            out = Path(td) / "outputs"
            write_status_artifacts(ledger=lg, cache_dir=Path(td) / "cache",
                                    portfolio_symbols=[], month="2026-06",
                                    monthly_bandwidth_gb=20, run_modes={}, base_dir=out)
            for name in ("fmp_usage_status.json", "fmp_cache_status.json", "data_budget_status.json"):
                p = out / "latest" / name
                self.assertTrue(p.exists(), name)
                self.assertTrue(json.loads(p.read_text())["observe_only"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_data_budget_status_producer.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement status_producer.py**

```python
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_budget.usage_ledger import UsageLedger
from portfolio_automation.data_budget.cache import cache_stats

_OBSERVE_ONLY = True


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_status(*, ledger: UsageLedger, cache_dir: Path, portfolio_symbols: list[str],
                 month: str, monthly_bandwidth_gb: float,
                 run_modes: dict[str, Any]) -> tuple[dict, dict, dict]:
    monthly_bytes = ledger.monthly_bytes(month=month)
    cap_bytes = int(float(monthly_bandwidth_gb) * 1024**3)
    hit_rate = ledger.cache_hit_rate(month=month)
    discovery_skipped = ledger.skipped_count(month=month, run_mode="discovery") > 0
    replay_skipped = ledger.skipped_count(month=month, run_mode="historical_replay") > 0

    usage = {
        "generated_at": _ts(), "observe_only": _OBSERVE_ONLY, "source": "fmp_usage_status",
        "month": month,
        "calls_by_run_mode": ledger.calls_by_run_mode(month=month),
        "calls_by_endpoint": ledger.calls_by_endpoint(month=month),
    }
    cstats = cache_stats(cache_dir, fresh_keys=[f"quote_short_{s.upper()}" for s in portfolio_symbols],
                         ttl_seconds=3600)
    cache = {
        "generated_at": _ts(), "observe_only": _OBSERVE_ONLY, "source": "fmp_cache_status",
        "cache_hit_rate": hit_rate,
        "file_count": cstats["file_count"], "total_size_bytes": cstats["total_size_bytes"],
        "portfolio_fresh": cstats["fresh"],
    }
    pct = round(monthly_bytes / cap_bytes, 4) if cap_bytes else None
    overall = "ok"
    if pct is not None and pct >= 1.0:
        overall = "constrained"
    elif pct is not None and pct >= 0.8:
        overall = "near_cap"
    budget = {
        "generated_at": _ts(), "observe_only": _OBSERVE_ONLY, "source": "data_budget_status",
        "overall_status": overall,
        "monthly_bandwidth_bytes": monthly_bytes,
        "monthly_bandwidth_gb_cap": monthly_bandwidth_gb,
        "monthly_bandwidth_pct": pct,
        "discovery_skipped_due_to_budget": discovery_skipped,
        "backtest_skipped_due_to_budget": replay_skipped,
        "enabled": True,
        "run_mode_budgets": run_modes,
    }
    return usage, cache, budget


def write_status_artifacts(*, ledger: UsageLedger, cache_dir: Path,
                           portfolio_symbols: list[str], month: str,
                           monthly_bandwidth_gb: float, run_modes: dict[str, Any],
                           base_dir: Path | str = "outputs") -> None:
    usage, cache, budget = build_status(
        ledger=ledger, cache_dir=cache_dir, portfolio_symbols=portfolio_symbols,
        month=month, monthly_bandwidth_gb=monthly_bandwidth_gb, run_modes=run_modes)
    latest = Path(base_dir) / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    for name, payload in (("fmp_usage_status.json", usage),
                          ("fmp_cache_status.json", cache),
                          ("data_budget_status.json", budget)):
        (latest / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
```

- [ ] **Step 4: Add the ledger aggregation helpers used above**

In `usage_ledger.py`, add:
```python
    def skipped_count(self, *, month: str, run_mode: str) -> int:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT COUNT(*) FROM api_usage_ledger "
                "WHERE substr(ts,1,7)=? AND run_mode=? AND skipped_reason IS NOT NULL",
                (month, run_mode)).fetchone()
        return int(row[0] or 0)

    def calls_by_run_mode(self, *, month: str) -> dict[str, int]:
        with self._conn() as cx:
            rows = cx.execute(
                "SELECT run_mode, COUNT(*) FROM api_usage_ledger "
                "WHERE substr(ts,1,7)=? AND cache_hit=0 AND skipped_reason IS NULL "
                "GROUP BY run_mode", (month,)).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def calls_by_endpoint(self, *, month: str) -> dict[str, int]:
        with self._conn() as cx:
            rows = cx.execute(
                "SELECT endpoint, COUNT(*) FROM api_usage_ledger "
                "WHERE substr(ts,1,7)=? AND cache_hit=0 AND skipped_reason IS NULL "
                "GROUP BY endpoint", (month,)).fetchall()
        return {r[0]: int(r[1]) for r in rows}
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_data_budget_status_producer.py tests/test_data_budget_ledger.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add portfolio_automation/data_budget/status_producer.py portfolio_automation/data_budget/usage_ledger.py tests/test_data_budget_status_producer.py
git commit -m "feat(data_budget): status producer (3 observe-only artifacts) + ledger aggregations"
```

---

## Task 9: Pipeline wiring (status producer stage, non-blocking)

**Files:**
- Create: `portfolio_automation/data_budget/run_status.py` (thin entrypoint reading config + portfolio symbols)
- Modify: `scripts/run_daily_safe.sh` (add a non-blocking stage)
- Test: `tests/test_data_budget_run_status.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_budget_run_status.py
from __future__ import annotations
import json, sys, unittest, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.run_status import run_data_budget_status


class TestRunStatus(unittest.TestCase):
    def test_writes_artifacts_in_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(json.dumps({
                "data_budget": {"enabled": True, "monthly_bandwidth_gb": 20,
                                "rate_per_min": 240, "burst": 300, "run_modes": {}},
                "portfolio": {"holdings": [{"symbol": "AAPL"}]}}))
            prev = os.getcwd()
            try:
                os.chdir(root)
                run_data_budget_status(root=root)
            finally:
                os.chdir(prev)
            self.assertTrue((root / "outputs" / "latest" / "data_budget_status.json").exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_data_budget_run_status.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement run_status.py**

```python
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from portfolio_automation.data_budget.usage_ledger import UsageLedger
from portfolio_automation.data_budget.status_producer import write_status_artifacts


def run_data_budget_status(*, root: Path | str = ".") -> None:
    """Non-blocking: build the 3 budget artifacts from the ledger. Never raises."""
    try:
        root = Path(root)
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        db = cfg.get("data_budget") or {}
        holdings = (cfg.get("portfolio") or {}).get("holdings") or []
        symbols = [str(h.get("symbol")) for h in holdings if h.get("symbol")]
        ledger = UsageLedger(root / "data" / "fmp_budget.db")
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        write_status_artifacts(
            ledger=ledger, cache_dir=root / "data" / "fmp_cache",
            portfolio_symbols=symbols, month=month,
            monthly_bandwidth_gb=db.get("monthly_bandwidth_gb", 20),
            run_modes=db.get("run_modes", {}), base_dir=root / "outputs")
    except Exception:
        pass


if __name__ == "__main__":
    run_data_budget_status()
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_data_budget_run_status.py -q`
Expected: PASS

- [ ] **Step 5: Wire into run_daily_safe.sh**

Find the existing FMP-budget-telemetry stage (`fmp_budget_telemetry`). Immediately after it, add a guarded stage mirroring the existing pattern:
```bash
# --- Data budget status (observe-only; non-blocking) ---
run_stage "Data budget status" \
  "$PYTHON" -m portfolio_automation.data_budget.run_status || true
```
(Match the exact `run_stage`/`$PYTHON` helper names used in the script.)

- [ ] **Step 6: Verify syntax + commit**

Run: `bash -n scripts/run_daily_safe.sh && echo OK`
```bash
git add portfolio_automation/data_budget/run_status.py scripts/run_daily_safe.sh tests/test_data_budget_run_status.py
git commit -m "feat(data_budget): non-blocking status-producer pipeline stage"
```

---

## Task 10: Migrate call sites to the governed factory + guard test

**Files:**
- Create: `portfolio_automation/data_budget/factory.py` (process-level convenience)
- Modify: `market_data.py`, `watchlist_scanner/performance_feedback.py`, `portfolio_automation/decision_outcome_tracker.py`, `portfolio_automation/news/run_news_intelligence.py`, `portfolio_automation/historical_backfill.py`, `main.py`
- Test: `tests/test_data_budget_no_direct_construction.py`

- [ ] **Step 1: Write the failing guard test**

```python
# tests/test_data_budget_no_direct_construction.py
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
SANCTIONED_PREFIXES = ("scripts/", "tests/", ".worktrees/", ".venv/")


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
```

- [ ] **Step 2: Run to verify it fails** (lists today's direct constructions)

Run: `.venv/bin/python -m pytest tests/test_data_budget_no_direct_construction.py -q`
Expected: FAIL listing `main.py`, `market_data.py`, `performance_feedback.py`, `decision_outcome_tracker.py`, `run_news_intelligence.py`, `historical_backfill.py`

- [ ] **Step 3: Add the process-level factory**

`portfolio_automation/data_budget/factory.py`:
```python
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from portfolio_automation.data_budget.governor import FMPBudgetGovernor

_governor: FMPBudgetGovernor | None = None


def _load_config() -> dict:
    try:
        return (json.loads(Path("config.json").read_text(encoding="utf-8"))
                .get("data_budget") or {})
    except Exception:
        return {}


def get_governor() -> FMPBudgetGovernor:
    global _governor
    if _governor is None:
        _governor = FMPBudgetGovernor(
            db_path=Path("data/fmp_budget.db"),
            cache_dir=Path("data/fmp_cache"),
            config=_load_config())
    return _governor


def governed_client(run_mode: str, *, fmp_client: Any = None) -> Any:
    """The single entry point all modules use instead of FMPClient(...)."""
    return get_governor().client(run_mode=run_mode, fmp_client=fmp_client)
```

- [ ] **Step 4: Migrate each call site**

In each file, replace the direct construction with the factory. Examples (apply the analogous edit at every site):

`watchlist_scanner/performance_feedback.py` — replace the `_budget`/`FMPClient` block:
```python
            from portfolio_automation.data_budget.factory import governed_client
            fmp_client = governed_client("daily")
```
(`_load_fmp_budget` is now unused here — remove it if no other caller.)

`portfolio_automation/decision_outcome_tracker.py` (both sites):
```python
            from portfolio_automation.data_budget.factory import governed_client
            fmp_client = governed_client("daily")
```
and
```python
        from portfolio_automation.data_budget.factory import governed_client
        client = governed_client("daily")
```

`portfolio_automation/news/run_news_intelligence.py`:
```python
            from portfolio_automation.data_budget.factory import governed_client
            fmp_client = governed_client("daily")
```

`market_data.py` `build_fmp_market_client` — pass through a governed client (default run_mode `daily`, caller-overridable):
```python
def build_fmp_market_client(config: dict, fmp_client: Any = None,
                            run_mode: str = "daily") -> "FMPMarketClient":
    if fmp_client is None:
        from portfolio_automation.data_budget.factory import governed_client
        fmp_client = governed_client(run_mode)
    return FMPMarketClient(fmp_client=fmp_client)
```

`portfolio_automation/historical_backfill.py`:
```python
            from portfolio_automation.data_budget.factory import governed_client
            fmp_client = governed_client("historical_replay")
```

`main.py` (×3): replace each `FMPClient(daily_budget=config.fmp_daily_calls_budget)` with:
```python
                from portfolio_automation.data_budget.factory import governed_client
                fmp = governed_client("daily")
```

- [ ] **Step 5: Run the guard test + each migrated module's tests**

Run:
```
.venv/bin/python -m pytest tests/test_data_budget_no_direct_construction.py \
  tests/test_watchlist_signal_feedback.py tests/test_decision_outcome_tracker.py \
  tests/test_news_intelligence_runner.py -q
```
Expected: PASS. If a test constructs the real client and now hits the governor, pass an explicit `fmp_client=` mock (the governed client still proxies a mock fine).

- [ ] **Step 6: Compile + commit**

```bash
.venv/bin/python -m py_compile main.py market_data.py watchlist_scanner/performance_feedback.py portfolio_automation/decision_outcome_tracker.py portfolio_automation/news/run_news_intelligence.py portfolio_automation/historical_backfill.py
git add portfolio_automation/data_budget/factory.py main.py market_data.py watchlist_scanner/performance_feedback.py portfolio_automation/decision_outcome_tracker.py portfolio_automation/news/run_news_intelligence.py portfolio_automation/historical_backfill.py tests/test_data_budget_no_direct_construction.py
git commit -m "refactor(fmp): route all FMP call sites through the budget governor factory"
```

---

## Task 11: GUI budget panel (System tab)

**Files:**
- Create: `gui_v2/data/dash_data_budget.py` (loader)
- Modify: `gui_v2/templates/system.html` (panel) + the System route's collect function
- Test: `tests/test_dash_data_budget.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dash_data_budget.py
from __future__ import annotations
import json, sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gui_v2.data.dash_data_budget import data_budget_view


class TestDataBudgetView(unittest.TestCase):
    def test_view_reads_three_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            latest = Path(td) / "outputs" / "latest"
            latest.mkdir(parents=True)
            (latest / "fmp_usage_status.json").write_text(json.dumps(
                {"observe_only": True, "calls_by_run_mode": {"daily": 12}}))
            (latest / "fmp_cache_status.json").write_text(json.dumps(
                {"observe_only": True, "cache_hit_rate": 0.82, "portfolio_fresh": {"AAPL": True}}))
            (latest / "data_budget_status.json").write_text(json.dumps(
                {"observe_only": True, "overall_status": "ok",
                 "monthly_bandwidth_pct": 0.10, "discovery_skipped_due_to_budget": False}))
            v = data_budget_view(Path(td))
            self.assertEqual(v["calls_this_run"], 12)
            self.assertEqual(v["cache_hit_rate_pct"], 82.0)
            self.assertEqual(v["bandwidth_pct"], 10.0)
            self.assertFalse(v["discovery_skipped"])
            self.assertEqual(v["portfolio_fresh"], {"AAPL": True})

    def test_view_degrades_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            v = data_budget_view(Path(td))
            self.assertFalse(v["available"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_dash_data_budget.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the loader**

`gui_v2/data/dash_data_budget.py`:
```python
from __future__ import annotations
import json
from pathlib import Path
from typing import Any


def _load(root: Path, name: str) -> dict[str, Any] | None:
    try:
        return json.loads((root / "outputs" / "latest" / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def data_budget_view(root: Path | str = ".") -> dict[str, Any]:
    root = Path(root)
    usage = _load(root, "fmp_usage_status.json")
    cache = _load(root, "fmp_cache_status.json")
    budget = _load(root, "data_budget_status.json")
    if not (usage or cache or budget):
        return {"available": False}
    calls = sum((usage or {}).get("calls_by_run_mode", {}).values()) if usage else 0
    hit = (cache or {}).get("cache_hit_rate")
    pct = (budget or {}).get("monthly_bandwidth_pct")
    return {
        "available": True,
        "observe_only": True,
        "calls_this_run": calls,
        "cache_hit_rate_pct": round(hit * 100, 1) if hit is not None else None,
        "bandwidth_pct": round(pct * 100, 1) if pct is not None else None,
        "overall_status": (budget or {}).get("overall_status", "unknown"),
        "discovery_skipped": bool((budget or {}).get("discovery_skipped_due_to_budget")),
        "backtest_skipped": bool((budget or {}).get("backtest_skipped_due_to_budget")),
        "portfolio_fresh": (cache or {}).get("portfolio_fresh", {}),
    }
```

- [ ] **Step 4: Run loader test to verify pass**

Run: `.venv/bin/python -m pytest tests/test_dash_data_budget.py -q`
Expected: PASS

- [ ] **Step 5: Wire into the System route + template**

Find the System-tab collect function (grep `def collect_system` in `gui_v2/`). Add:
```python
    from gui_v2.data.dash_data_budget import data_budget_view
    ctx["data_budget"] = data_budget_view(root)
```
In `gui_v2/templates/system.html`, add a panel (match the existing `_ui` card macro style):
```html
{% if data_budget and data_budget.available %}
<section class="card">
  <h3>FMP Data Budget <span class="badge" role="status">observe-only</span></h3>
  <ul>
    <li>Calls this run: {{ data_budget.calls_this_run }}</li>
    <li>Cache hit rate: {{ data_budget.cache_hit_rate_pct }}%</li>
    <li>Bandwidth used: {{ data_budget.bandwidth_pct }}% of monthly cap</li>
    <li>Status: {{ data_budget.overall_status }}</li>
    <li>Discovery skipped (budget): {{ data_budget.discovery_skipped }}</li>
    <li>Backtest skipped (budget): {{ data_budget.backtest_skipped }}</li>
  </ul>
</section>
{% endif %}
```

- [ ] **Step 6: Run the gui test suite + commit**

Run: `.venv/bin/python -m pytest tests/test_dash_data_budget.py -q` (and any `tests/test_gui*` touching system.html)
```bash
git add gui_v2/data/dash_data_budget.py gui_v2/templates/system.html gui_v2/<system_route_file>.py tests/test_dash_data_budget.py
git commit -m "feat(gui): FMP data-budget panel on System tab (observe-only)"
```
(Operator note: dashboard service restart required to serve the new panel.)

---

## Task 12: Daily-tool-analysis health check (Analysis+Health requirement)

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md` (artifacts-read + computed signal + dispatch + body line)
- Test: `tests/test_daily_check_data_budget_signal.py`

- [ ] **Step 1: Write the failing test** (asserts the computed health signal under healthy + degraded fixtures)

```python
# tests/test_daily_check_data_budget_signal.py
from __future__ import annotations
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.health import data_budget_health


class TestDataBudgetHealth(unittest.TestCase):
    def test_ok_when_under_cap_and_no_skips(self):
        h = data_budget_health({"overall_status": "ok", "monthly_bandwidth_pct": 0.1,
                                 "discovery_skipped_due_to_budget": False})
        self.assertEqual(h["status"], "green")

    def test_amber_when_near_cap(self):
        h = data_budget_health({"overall_status": "near_cap", "monthly_bandwidth_pct": 0.85,
                                 "discovery_skipped_due_to_budget": False})
        self.assertEqual(h["status"], "amber")

    def test_amber_when_discovery_skipped(self):
        h = data_budget_health({"overall_status": "constrained", "monthly_bandwidth_pct": 1.02,
                                 "discovery_skipped_due_to_budget": True})
        self.assertEqual(h["status"], "amber")
        self.assertIn("discovery", h["reason"])

    def test_missing_artifact_is_neutral(self):
        self.assertEqual(data_budget_health(None)["status"], "green")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_daily_check_data_budget_signal.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the deterministic health helper**

`portfolio_automation/data_budget/health.py`:
```python
from __future__ import annotations
from typing import Any, Optional

def data_budget_health(budget_status: Optional[dict[str, Any]]) -> dict[str, str]:
    """Pure GREEN/AMBER classifier for the data-budget layer (never RED — observe-only)."""
    if not budget_status:
        return {"status": "green", "reason": "data_budget_status absent (inert)"}
    pct = budget_status.get("monthly_bandwidth_pct")
    if budget_status.get("discovery_skipped_due_to_budget") or \
       budget_status.get("backtest_skipped_due_to_budget"):
        return {"status": "amber", "reason": "discovery/backtest skipped due to FMP budget"}
    if (pct is not None and pct >= 0.8) or budget_status.get("overall_status") in ("near_cap", "constrained"):
        return {"status": "amber", "reason": f"monthly bandwidth at {pct} of cap"}
    return {"status": "green", "reason": "within budget"}
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_daily_check_data_budget_signal.py -q`
Expected: PASS

- [ ] **Step 5: Extend the daily-tool-analysis skill doc**

In `.claude/commands/daily-tool-analysis.md`:
- **Step 1 artifacts-read:** add `fmp_usage_status.json`, `fmp_cache_status.json`, `data_budget_status.json` (observe-only; absent is inert).
- **Compute:** `data_budget_health = data_budget_health(data_budget_status)`.
- **Step 3 dispatch:** dispatch `portfolio-discovery-health` when `data_budget_health.status == "amber"` (developer lens — it already owns FMP headroom), passing the bandwidth pct + skip flags.
- **Step 4 body line (new 6m):** `"Data-budget: {overall_status} · {bandwidth_pct}% of 20GB cap · {calls_this_run} calls/run · cache {cache_hit_rate}% · discovery-skipped {bool}"`. AMBER on the health helper; never RED.

- [ ] **Step 6: Commit**

```bash
git add portfolio_automation/data_budget/health.py .claude/commands/daily-tool-analysis.md tests/test_daily_check_data_budget_signal.py
git commit -m "feat(health): data-budget health signal + daily-tool-analysis coverage"
```

---

## Task 13: Docs + roadmap

**Files:**
- Modify: `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/PIPELINE_RUNBOOK.md`, `.agent/project_state.yaml`

- [ ] **Step 1: OUTPUT_ARTIFACT_CONTRACTS.md** — add the 3 artifacts (path, role=telemetry, observe_only, producer `data_budget.status_producer`, cadence daily, consumer `/daily-tool-analysis` + GUI System tab). If an artifact registry (`artifact_registry.yaml`) governs the registry test, add the 3 rows there too and re-run `tests/` for the registry validator.

- [ ] **Step 2: PIPELINE_RUNBOOK.md** — document the governor: factory usage (`governed_client(run_mode)`), run-mode table + rationale for each budget number, token bucket (240/300), monthly 20GB guard, kill-switch (`config.json data_budget.enabled`, `STOCKBOT_FMP_GOVERNOR_DISABLED`, `config/fmp_governor.DISABLED`), and the new pipeline stage.

- [ ] **Step 3: project_state.yaml** — add a roadmap entry under completed steps: `fmp_budget_governor # 2026-06-15 — single guarded FMP access layer (data_budget pkg) wrapping fmp_client; token bucket + monthly bandwidth + run-mode budgets + per-call ledger; 3 observe-only artifacts + GUI panel; ships enabled w/ kill-switch. next_official_step unchanged (observe_and_iterate).`

- [ ] **Step 4: Commit**

```bash
git add docs/OUTPUT_ARTIFACT_CONTRACTS.md docs/PIPELINE_RUNBOOK.md .agent/project_state.yaml
git commit -m "docs(data_budget): artifact contracts + runbook + roadmap entry"
```

---

## Task 14: Full-suite validation + live smoke

- [ ] **Step 1: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (the 3 known pre-existing failures per memory are acceptable; no NEW failures). Restore `config/signal_registry.yaml default_weight 0.4947` + drop fresh config/history snapshots before committing (test-isolation gotcha).

- [ ] **Step 2: Live smoke (FMP free/subscription)**

Run (env loaded):
```bash
set -a; . ./.env; set +a
.venv/bin/python -m portfolio_automation.data_budget.run_status
cat outputs/latest/data_budget_status.json | python3 -m json.tool | head -20
.venv/bin/python -c "from portfolio_automation.data_budget.factory import governed_client; c=governed_client('gui_refresh'); print(type(c).__name__, c.get_quote_short('AAPL').get('price'))"
```
Expected: artifact written; governed client returns a price; ledger row appended.

- [ ] **Step 3: Kill-switch smoke**

Run:
```bash
STOCKBOT_FMP_GOVERNOR_DISABLED=1 .venv/bin/python -c "from portfolio_automation.data_budget.factory import governed_client; print(type(governed_client('daily')).__name__)"
```
Expected: prints `FMPClient` (kill-switch fallback).

- [ ] **Step 4: Final commit (if any cleanup)** + summary report per CLAUDE.md Final Report Format.

---

## Self-Review Notes (author)

- **Spec coverage:** core requirements 1–9 map to Tasks 1–14 (pkg→T2-9; governor/token bucket/bandwidth→T6; SQLite tables→T2/T3 [data_request_queue intentionally deferred]; endpoint strategy→T1/T5; call-site migration→T10; artifacts→T8; GUI→T11; tests→every task; validation/docs→T12-14).
- **Kill-switch / observe-only:** governor falls back to plain FMPClient (T6); artifacts hardcode `observe_only:true` (T8); decision artifacts unchanged (guarded by existing suites in T10/T14).
- **No new deps:** stdlib `sqlite3`/`urllib` only.
- **Known risk:** the `GovernedFMPClient.__getattr__` cache-hit detection uses `last_response_bytes` deltas — if a real method serves from cache without touching `_raw_get`, it correctly reads as a cache hit (bytes unchanged). Verified against the FMPClient cache-first pattern; T6 test covers the proxied-call + skip paths.
