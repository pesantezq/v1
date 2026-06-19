"""
Tests for portfolio_automation/sector_mapping.py — the shared FMP sector
normalizer. FMP's profile `sector` is issuer-based, so every fund reports
"Financial Services / Asset Management". For attribution we want the fund's
*exposure*: sector-SPDRs map to their exposure sector; other funds bucket as
"ETF/Index"; non-fund equities keep their raw sector.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.sector_mapping import normalize_sector


class TestNormalizeSector(unittest.TestCase):
    def test_broad_etf_buckets_as_etf_index(self):
        self.assertEqual(
            normalize_sector("QQQ", "Financial Services", is_etf=True), "ETF/Index"
        )

    def test_sector_spdr_maps_to_exposure(self):
        self.assertEqual(
            normalize_sector("XLE", "Financial Services", is_etf=True), "Energy"
        )
        self.assertEqual(
            normalize_sector("XLK", "Financial Services", is_etf=True), "Technology"
        )

    def test_fund_flag_also_normalizes(self):
        self.assertEqual(
            normalize_sector("SOMEFUND", "Financial Services", is_fund=True), "ETF/Index"
        )

    def test_crypto_equity_keeps_raw_sector(self):
        # Not a fund — FMP-truth for an operating company.
        self.assertEqual(
            normalize_sector("RIOT", "Financial Services"), "Financial Services"
        )

    def test_plain_equity_unchanged(self):
        self.assertEqual(normalize_sector("NVDA", "Technology"), "Technology")

    def test_blank_sector_falls_to_unknown(self):
        self.assertEqual(normalize_sector("WAT", ""), "Unknown")
        self.assertEqual(normalize_sector("WAT", None), "Unknown")

    def test_custom_unknown_label(self):
        self.assertEqual(normalize_sector("WAT", "", unknown="N/A"), "N/A")

    def test_ticker_case_insensitive(self):
        self.assertEqual(
            normalize_sector("xle", "Financial Services", is_etf=True), "Energy"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
