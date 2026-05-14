"""
Tests for tools/daily_sandbox_run.py
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from unittest import mock

import pytest

from tools import daily_sandbox_run as runner
from tools.daily_sandbox_run import (
    SandboxRunResult,
    StepResult,
    _SAFETY_DISCLAIMER,
    _RUN_MODE_LITERAL,
    _STATUS_JSON_RELATIVE,
    _STATUS_MD_RELATIVE,
    _build_status_payload,
    _render_status_markdown,
    _safe_step,
    _skipped,
    main,
    run_daily_sandbox,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sandbox_dir(base: Path) -> Path:
    d = base / "outputs" / "sandbox" / "discovery"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _latest_dir(base: Path) -> Path:
    d = base / "outputs" / "latest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_emerging(base: Path, candidates: list[dict] | None = None) -> Path:
    if candidates is None:
        candidates = [
            {
                "ticker": "NVDA",
                "status": "WATCH",
                "score": 3.5,
                "first_seen": "2026-05-14T10:00:00+00:00",
                "last_seen": "2026-05-14T10:00:00+00:00",
                "mention_count": 4,
                "unique_source_count": 3,
                "event_type": "earnings",
                "discovery_only": True,
                "sandbox_only": True,
            },
        ]
    p = _sandbox_dir(base) / "emerging_candidates.json"
    p.write_text(
        json.dumps({
            "generated_at": "2026-05-14T10:00:00+00:00",
            "candidates": candidates,
            "observe_only": True,
            "discovery_only": True,
            "sandbox_only": True,
        }),
        encoding="utf-8",
    )
    return p


def _write_rejected(base: Path) -> Path:
    p = _sandbox_dir(base) / "rejected_candidates.json"
    p.write_text(
        json.dumps({
            "generated_at": "2026-05-14T10:00:00+00:00",
            "candidates": [],
            "observe_only": True,
        }),
        encoding="utf-8",
    )
    return p


def _write_news_intelligence(base: Path) -> Path:
    p = _latest_dir(base) / "news_intelligence.json"
    p.write_text(
        json.dumps({
            "generated_at": "2026-05-14T09:00:00+00:00",
            "evidence_packets": [
                {
                    "entity_key": "NVDA",
                    "entity_type": "ticker",
                    "related_tickers": ["NVDA"],
                    "article_count": 3,
                    "source_count": 2,
                    "themes": ["earnings"],
                    "risk_flags": [],
                    "catalyst_flags": ["earnings_beat"],
                    "evidence_lane": "sandbox_discovery_research",
                    "observe_only": True,
                    "no_trade": True,
                    "not_recommendation": True,
                },
            ],
            "observe_only": True,
            "no_trade": True,
        }),
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# 1. _safe_step + _skipped wrappers
# ---------------------------------------------------------------------------

class TestSafeStep:
    def test_returns_succeeded_step(self):
        s = _safe_step("ok", lambda: {"count": 1})
        assert s.status == "succeeded"
        assert s.summary == {"count": 1}
        assert s.error is None

    def test_exception_recorded_as_failed(self):
        def _boom():
            raise RuntimeError("boom")
        s = _safe_step("bad", _boom)
        assert s.status == "failed"
        assert s.error == "boom"
        assert s.summary == {}

    def test_module_internal_error_is_failure(self):
        s = _safe_step("err", lambda: {"error": "internal"})
        assert s.status == "failed"
        assert s.error == "internal"

    def test_skipped_helper(self):
        s = _skipped("name", "because")
        assert s.status == "skipped"
        assert s.skip_reason == "because"
        assert s.error is None


# ---------------------------------------------------------------------------
# 2. Happy path — all modules available
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_runner_succeeds_when_all_modules_available(self, tmp_path):
        _write_emerging(tmp_path)
        _write_rejected(tmp_path)
        _write_news_intelligence(tmp_path)

        result = run_daily_sandbox(base_dir=tmp_path)

        assert isinstance(result, SandboxRunResult)
        # Three steps attempted: news integration, automatic promotion, replay (skipped)
        assert result.steps_attempted == 3
        names = [s.name for s in result.steps]
        assert "discovery_news_integration" in names
        assert "automatic_promotion_governance" in names
        assert "discovery_replay" in names

        # No fatal failures
        assert result.steps_failed == 0
        # Replay is skipped without inputs
        skipped_names = [s.name for s in result.steps if s.status == "skipped"]
        assert "discovery_replay" in skipped_names

    def test_status_artifacts_written(self, tmp_path):
        _write_emerging(tmp_path)
        result = run_daily_sandbox(base_dir=tmp_path)
        status_json = tmp_path / "outputs" / "sandbox" / _STATUS_JSON_RELATIVE
        status_md = tmp_path / "outputs" / "sandbox" / _STATUS_MD_RELATIVE
        assert status_json.exists()
        assert status_md.exists()
        payload = json.loads(status_json.read_text(encoding="utf-8"))
        assert payload["observe_only"] is True
        assert payload["no_trade"] is True
        assert payload["not_recommendation"] is True
        assert payload["run_mode"] == _RUN_MODE_LITERAL
        assert payload["source"] == "daily_sandbox_run"
        assert payload["disclaimer"] == _SAFETY_DISCLAIMER
        assert isinstance(payload["steps"], list)
        assert "candidate_counts" in payload
        assert "news_evidence_counts" in payload
        assert "automatic_promotion_counts" in payload

    def test_status_payload_includes_safety_flags(self, tmp_path):
        result = run_daily_sandbox(base_dir=tmp_path)
        status_json = tmp_path / "outputs" / "sandbox" / _STATUS_JSON_RELATIVE
        payload = json.loads(status_json.read_text(encoding="utf-8"))
        for flag in (
            "observe_only", "no_trade", "not_recommendation",
            "discovery_only", "no_portfolio_mutation", "no_watchlist_mutation",
            "no_allocation_policy_change", "no_decision_override",
            "no_score_mutation",
        ):
            assert payload[flag] is True, f"missing/false safety flag: {flag}"


# ---------------------------------------------------------------------------
# 3. Degrade safely when optional artifacts are missing
# ---------------------------------------------------------------------------

class TestDegradeSafely:
    def test_no_existing_artifacts(self, tmp_path):
        # No outputs/ directory at all
        result = run_daily_sandbox(base_dir=tmp_path)
        # Status artifact still written
        status_json = tmp_path / "outputs" / "sandbox" / _STATUS_JSON_RELATIVE
        assert status_json.exists()
        # Counts are None where data is missing
        payload = json.loads(status_json.read_text(encoding="utf-8"))
        assert payload["candidate_counts"]["emerging"] is None
        # Steps still completed (possibly without artifacts) — no exception bubbled up
        assert result.steps_attempted == 3

    def test_missing_news_intelligence_input(self, tmp_path):
        # Only emerging candidates exist, no news intelligence
        _write_emerging(tmp_path)
        result = run_daily_sandbox(base_dir=tmp_path)
        # Should not raise; should not have status==failed for missing optional input
        # (modules return empty/safe results when their inputs are missing)
        assert result.steps_attempted == 3

    def test_malformed_emerging_candidates_does_not_crash(self, tmp_path):
        bad = _sandbox_dir(tmp_path) / "emerging_candidates.json"
        bad.write_text("not valid json {{{", encoding="utf-8")
        result = run_daily_sandbox(base_dir=tmp_path)
        # Counts come back as None instead of crashing
        status_json = tmp_path / "outputs" / "sandbox" / _STATUS_JSON_RELATIVE
        payload = json.loads(status_json.read_text(encoding="utf-8"))
        assert payload["candidate_counts"]["emerging"] is None

    def test_replay_input_missing_skips_replay(self, tmp_path):
        result = run_daily_sandbox(base_dir=tmp_path)
        replay_steps = [s for s in result.steps if s.name == "discovery_replay"]
        assert len(replay_steps) == 1
        assert replay_steps[0].status == "skipped"
        assert "no replay input" in (replay_steps[0].skip_reason or "")

    def test_replay_input_malformed_skips_replay(self, tmp_path):
        replay_input = _sandbox_dir(tmp_path) / "replay_price_outcomes.json"
        replay_input.write_text("not json", encoding="utf-8")
        result = run_daily_sandbox(base_dir=tmp_path)
        replay_steps = [s for s in result.steps if s.name == "discovery_replay"]
        assert replay_steps[0].status == "skipped"

    def test_module_exception_recorded_as_failed_step(self, tmp_path):
        with mock.patch.object(
            runner, "_step_discovery_news_integration",
            side_effect=RuntimeError("simulated module crash"),
        ):
            result = run_daily_sandbox(base_dir=tmp_path)
        failed = [s for s in result.steps if s.status == "failed"]
        names = [s.name for s in failed]
        assert "discovery_news_integration" in names
        # Other steps still ran (non-blocking)
        other_steps = [s for s in result.steps if s.name != "discovery_news_integration"]
        assert len(other_steps) == 2


# ---------------------------------------------------------------------------
# 4. Namespace + write-boundary guarantees
# ---------------------------------------------------------------------------

class TestWriteBoundaries:
    def test_only_sandbox_and_status_paths_touched(self, tmp_path):
        """Runner must not create files outside outputs/sandbox/."""
        # Pre-create marker files we expect to remain unchanged
        portfolio_dir = tmp_path / "outputs" / "portfolio"
        portfolio_dir.mkdir(parents=True, exist_ok=True)
        marker = portfolio_dir / "marker.json"
        marker.write_text('{"untouched": true}', encoding="utf-8")
        latest_dir = tmp_path / "outputs" / "latest"
        latest_dir.mkdir(parents=True, exist_ok=True)
        latest_marker = latest_dir / "marker.json"
        latest_marker.write_text('{"untouched": true}', encoding="utf-8")
        config_path = tmp_path / "config.json"
        config_path.write_text('{"untouched": true}', encoding="utf-8")

        _write_emerging(tmp_path)
        run_daily_sandbox(base_dir=tmp_path)

        # Marker files preserved byte-for-byte
        assert json.loads(marker.read_text(encoding="utf-8")) == {"untouched": True}
        assert json.loads(latest_marker.read_text(encoding="utf-8")) == {"untouched": True}
        assert json.loads(config_path.read_text(encoding="utf-8")) == {"untouched": True}

        # Status artifact in sandbox only
        status_json = tmp_path / "outputs" / "sandbox" / _STATUS_JSON_RELATIVE
        assert status_json.exists()

    def test_runner_never_writes_portfolio_namespace(self, tmp_path):
        """outputs/portfolio/ must remain absent or untouched."""
        run_daily_sandbox(base_dir=tmp_path)
        portfolio_dir = tmp_path / "outputs" / "portfolio"
        if portfolio_dir.exists():
            # If it exists, runner must not have written anything new
            entries = list(portfolio_dir.iterdir())
            assert entries == [], f"unexpected portfolio writes: {entries}"

    def test_runner_never_writes_official_watchlist_or_config(self, tmp_path):
        """config.json and any watchlist file outside outputs/sandbox is read-only."""
        config_path = tmp_path / "config.json"
        original_config = {
            "investor": {"name": "Test"},
            "portfolio": {
                "holdings": [{"symbol": "QQQ", "shares": 6}],
                "cash_available": 100.0,
            },
        }
        config_path.write_text(json.dumps(original_config), encoding="utf-8")

        watchlist_path = tmp_path / "watchlist.json"
        watchlist_payload = {"watchlist": ["QQQ", "GLD"]}
        watchlist_path.write_text(json.dumps(watchlist_payload), encoding="utf-8")

        run_daily_sandbox(base_dir=tmp_path)

        assert json.loads(config_path.read_text(encoding="utf-8")) == original_config
        assert json.loads(watchlist_path.read_text(encoding="utf-8")) == watchlist_payload

    def test_dry_run_does_not_write_status(self, tmp_path):
        run_daily_sandbox(base_dir=tmp_path, dry_run=True)
        status_json = tmp_path / "outputs" / "sandbox" / _STATUS_JSON_RELATIVE
        status_md = tmp_path / "outputs" / "sandbox" / _STATUS_MD_RELATIVE
        assert not status_json.exists()
        assert not status_md.exists()


# ---------------------------------------------------------------------------
# 5. No trading-action tokens leak (except in fixed disclaimers)
# ---------------------------------------------------------------------------

_FORBIDDEN_TOKENS = (
    "BUY", "SELL", "HOLD", "ACTIONABLE", "PROMOTED",
    "VALIDATED", "APPROVED", "TRADE", "RECOMMENDATION",
)


def _scan_payload_for_tokens(payload, *, allowed_substrings: tuple[str, ...]) -> list[str]:
    """
    Walk *payload* and find any whole-word forbidden tokens that are NOT
    contained within an allowed disclaimer substring.
    """
    text_blobs: list[str] = []

    def _walk(node):
        if isinstance(node, str):
            text_blobs.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(payload)

    violations: list[str] = []
    for blob in text_blobs:
        # Strip allowed disclaimers from the blob first
        cleaned = blob
        for allowed in allowed_substrings:
            cleaned = cleaned.replace(allowed, "")
        for tok in _FORBIDDEN_TOKENS:
            if re.search(rf"\b{re.escape(tok)}\b", cleaned):
                violations.append(f"token={tok!r} in {blob!r}")
    return violations


class TestNoTradingTokens:
    def test_status_payload_has_no_action_tokens(self, tmp_path):
        result = run_daily_sandbox(base_dir=tmp_path)
        payload = _build_status_payload(result)
        violations = _scan_payload_for_tokens(
            payload, allowed_substrings=(_SAFETY_DISCLAIMER,)
        )
        assert violations == [], f"forbidden tokens leaked: {violations}"

    def test_status_markdown_has_no_action_tokens(self, tmp_path):
        result = run_daily_sandbox(base_dir=tmp_path)
        payload = _build_status_payload(result)
        md = _render_status_markdown(payload)
        # Strip the disclaimer block before scanning
        cleaned = md.replace(_SAFETY_DISCLAIMER, "")
        for tok in _FORBIDDEN_TOKENS:
            assert not re.search(rf"\b{tok}\b", cleaned), (
                f"forbidden token {tok!r} appears outside the disclaimer in the "
                f"markdown status artifact"
            )

    def test_source_module_avoids_action_tokens_outside_disclaimer(self):
        """
        The module source itself must not name action tokens in code paths,
        only in the safety disclaimer string.
        """
        src = Path(runner.__file__).read_text(encoding="utf-8")
        # Allow comments/docstrings that mention BUY/SELL/HOLD as part of a
        # safety disclaimer phrasing; we only forbid bare uppercase tokens
        # appearing outside disclaimer strings.
        cleaned = src.replace(_SAFETY_DISCLAIMER, "")
        # The phrase "BUY/SELL/HOLD" appears in safety wording inside docstrings
        # listing what the runner does NOT do — allow that specific phrasing.
        allowed_phrases = (
            "BUY/SELL/HOLD recommendations",
            "BUY/SELL/HOLD",
        )
        for ap in allowed_phrases:
            cleaned = cleaned.replace(ap, "")
        for tok in _FORBIDDEN_TOKENS:
            assert not re.search(rf"\b{tok}\b", cleaned), (
                f"forbidden token {tok!r} found in module source outside "
                f"allowed disclaimer phrasing"
            )


# ---------------------------------------------------------------------------
# 6. Run mode and lane independence
# ---------------------------------------------------------------------------

class TestRunModeAndLane:
    def test_run_mode_is_discovery(self, tmp_path):
        result = run_daily_sandbox(base_dir=tmp_path)
        payload = _build_status_payload(result)
        assert payload["run_mode"] == "discovery"

    def test_does_not_invoke_main_daily_pipeline(self, tmp_path):
        """
        The sandbox runner must not import or invoke the official daily
        pipeline.  We assert by inspecting module source — main.py and
        scanner.py must not appear as imports of the runner.
        """
        src = Path(runner.__file__).read_text(encoding="utf-8")
        assert "import main" not in src
        assert "from main" not in src
        assert "watchlist_scanner.scanner" not in src
        # Importing the daily memo module is also not the sandbox lane's job
        assert "watchlist_scanner.daily_memo" not in src


# ---------------------------------------------------------------------------
# 7. CLI smoke test
# ---------------------------------------------------------------------------

class TestCli:
    def test_cli_smoke(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Daily sandbox run" in out
        assert _SAFETY_DISCLAIMER in out

    def test_cli_dry_run_does_not_write_status(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "--dry-run"])
        assert rc == 0
        status_json = tmp_path / "outputs" / "sandbox" / _STATUS_JSON_RELATIVE
        assert not status_json.exists()


# ---------------------------------------------------------------------------
# 8. Aggregate counts
# ---------------------------------------------------------------------------

class TestAggregateCounts:
    def test_candidate_counts_reflect_existing_files(self, tmp_path):
        _write_emerging(tmp_path, candidates=[
            {"ticker": "AAA", "status": "WATCH"},
            {"ticker": "BBB", "status": "WATCH"},
            {"ticker": "CCC", "status": "DISCOVERED"},
        ])
        _write_rejected(tmp_path)
        result = run_daily_sandbox(base_dir=tmp_path)
        payload = _build_status_payload(result)
        assert payload["candidate_counts"]["emerging"] == 3
        assert payload["candidate_counts"]["rejected"] == 0

    def test_automatic_promotion_counts_default_when_missing(self, tmp_path):
        result = run_daily_sandbox(base_dir=tmp_path)
        payload = _build_status_payload(result)
        # No promotion file → counts are None or 0
        counts = payload["automatic_promotion_counts"]
        assert counts.get("decision_count") in (None, 0)
