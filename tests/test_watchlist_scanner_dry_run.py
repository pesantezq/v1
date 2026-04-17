import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class _FakeCache:
    calls_today = 0


class _FakeScanner:
    def __init__(self, *args, **kwargs):
        pass

    def run(self, dry_run: bool = False):
        return {
            "alerts": [],
            "results": [],
            "generated_at": "2026-04-14T00:00:00",
            "run_date": "2026-04-14",
            "calls_used": 0,
            "scan_summary": {},
        }


class TestWatchlistScannerDryRun(unittest.TestCase):

    def test_dry_run_skips_all_output_writes(self):
        from watchlist_scanner.__main__ import run

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "outputs"
            with patch("watchlist_scanner.cache_manager.CacheManager", return_value=_FakeCache()):
                with patch("watchlist_scanner.alpha_vantage_client.WatchlistAVClient"):
                    with patch("watchlist_scanner.scanner.WatchlistScanner", _FakeScanner):
                        with patch("watchlist_scanner.__main__._write_signals_json") as mock_signals:
                            with patch("watchlist_scanner.__main__._write_alerts_csv") as mock_alerts:
                                with patch("watchlist_scanner.__main__._write_summary_md") as mock_summary:
                                    run(
                                        config={},
                                        dry_run=True,
                                        output_dir=str(output_dir),
                                        extended_watchlist_config={"enabled": False},
                                        scraped_intel_config={"enabled": False},
                                    )

        mock_signals.assert_not_called()
        mock_alerts.assert_not_called()
        mock_summary.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
