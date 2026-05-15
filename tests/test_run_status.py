"""
Tests for portfolio_automation/run_status.py
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from portfolio_automation.run_status import (
    PipelineRunStatus,
    StepStatus,
    STATUS_JSON_RELATIVE,
    STATUS_MD_RELATIVE,
    build_status_payload,
    make_run_id,
    render_status_markdown,
    status_from_main_result,
    status_from_pipeline_steps,
    write_pipeline_run_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass
class _FakeStepResult:
    """Duck-typed stand-in for run_daily_pipeline.StepResult."""
    name: str
    status: str
    duration_sec: float
    notes: str = ""


@pytest.fixture
def base_outputs(tmp_path: Path) -> Path:
    out = tmp_path / "outputs"
    out.mkdir()
    return out


# ---------------------------------------------------------------------------
# StepStatus / PipelineRunStatus
# ---------------------------------------------------------------------------

class TestStepStatus:
    def test_to_dict_minimal(self):
        s = StepStatus(name="x", status="succeeded", duration_seconds=1.234)
        d = s.to_dict()
        assert d["name"] == "x"
        assert d["status"] == "succeeded"
        assert d["duration_seconds"] == pytest.approx(1.234)
        assert "error" not in d
        assert "skip_reason" not in d

    def test_to_dict_failed_includes_error(self):
        s = StepStatus(name="y", status="failed", duration_seconds=0.5, error="boom")
        d = s.to_dict()
        assert d["error"] == "boom"

    def test_to_dict_skipped_includes_reason(self):
        s = StepStatus(name="z", status="skipped", duration_seconds=0.0, skip_reason="disabled")
        d = s.to_dict()
        assert d["skip_reason"] == "disabled"

    def test_duration_seconds_rounded(self):
        s = StepStatus(name="x", status="succeeded", duration_seconds=1.123456789012)
        d = s.to_dict()
        assert d["duration_seconds"] == round(1.123456789012, 6)


class TestPipelineRunStatusCounts:
    def test_counts(self):
        status = PipelineRunStatus(
            generated_at="2026-05-14T10:00:00+00:00",
            run_id="2026-05-14_daily_official",
            run_mode="daily",
            source="main",
            success=True,
            exit_code=0,
            steps=[
                StepStatus("a", "succeeded", 0.1),
                StepStatus("b", "succeeded", 0.2),
                StepStatus("c", "failed", 0.3, error="oops"),
                StepStatus("d", "skipped", 0.0, skip_reason="off"),
            ],
        )
        assert status.steps_attempted == 4
        assert status.steps_succeeded == 2
        assert status.steps_failed == 1
        assert status.steps_skipped == 1


# ---------------------------------------------------------------------------
# make_run_id
# ---------------------------------------------------------------------------

class TestMakeRunId:
    def test_uses_generated_at_date(self):
        rid = make_run_id("daily", generated_at="2026-05-14T13:00:00+00:00")
        assert rid == "2026-05-14_daily_official"

    def test_falls_back_to_now_when_missing(self):
        rid = make_run_id("daily")
        # Format: YYYY-MM-DD_daily_official
        assert rid.endswith("_daily_official")
        assert len(rid.split("_")[0]) == 10  # date prefix


# ---------------------------------------------------------------------------
# Adapter: status_from_main_result
# ---------------------------------------------------------------------------

class TestStatusFromMainResult:
    def test_success_path(self):
        result = {
            "success": True,
            "errors": [],
            "warnings": ["missing FOO data"],
            "decision_plan": [{"action": "BUY"}, {"action": "HOLD"}],
            "decision_plan_summary": "all good",
            "drawdown_regime": "normal",
            "degraded_mode": False,
            "data_mode": "live",
            "scanner": {"candidates": [1, 2, 3]},
        }
        st = status_from_main_result(result, run_mode="daily", duration_seconds=12.5)
        assert st.success is True
        assert st.exit_code == 0
        assert st.run_mode == "daily"
        assert st.source == "main"
        assert len(st.steps) == 1
        assert st.steps[0].name == "run_portfolio_update"
        assert st.steps[0].status == "succeeded"
        assert st.steps[0].duration_seconds == pytest.approx(12.5)
        assert st.summary["decision_plan_count"] == 2
        assert st.summary["scanner_candidate_count"] == 3
        assert st.summary["drawdown_regime"] == "normal"
        assert st.warnings == ["missing FOO data"]

    def test_failure_path_with_errors(self):
        result = {
            "success": False,
            "errors": ["price fetch failed", "fmp timeout"],
            "warnings": [],
            "decision_plan": [],
        }
        st = status_from_main_result(result, run_mode="daily", duration_seconds=3.0)
        assert st.success is False
        assert st.exit_code == 1
        assert st.steps[0].status == "failed"
        assert "price fetch failed" in (st.steps[0].error or "")

    def test_missing_optional_fields_safe(self):
        st = status_from_main_result({"success": True}, run_mode="weekly")
        assert st.success is True
        assert st.run_mode == "weekly"
        assert st.summary["decision_plan_count"] == 0
        assert st.summary["scanner_candidate_count"] is None
        assert st.warnings == []
        assert st.errors == []

    def test_scanner_not_dict_safe(self):
        st = status_from_main_result({"success": True, "scanner": "not a dict"}, run_mode="daily")
        assert st.summary["scanner_candidate_count"] is None


# ---------------------------------------------------------------------------
# Adapter: status_from_pipeline_steps
# ---------------------------------------------------------------------------

class TestStatusFromPipelineSteps:
    def test_maps_ok_to_succeeded(self):
        steps = [_FakeStepResult("theme_discovery", "ok", 1.0, "3 themes")]
        st = status_from_pipeline_steps(steps, run_mode="daily")
        assert st.steps[0].status == "succeeded"
        assert st.steps[0].notes == "3 themes"
        assert st.success is True
        assert st.exit_code == 0

    def test_failed_step_records_error_not_notes(self):
        steps = [_FakeStepResult("scan", "failed", 0.5, "RuntimeError: x")]
        st = status_from_pipeline_steps(steps, run_mode="daily")
        assert st.steps[0].status == "failed"
        assert st.steps[0].error == "RuntimeError: x"
        assert st.steps[0].notes == ""
        assert st.success is False
        assert st.exit_code == 1

    def test_skipped_step_records_skip_reason(self):
        steps = [_FakeStepResult("scan", "skipped", 0.0, "--skip-scan")]
        st = status_from_pipeline_steps(steps, run_mode="daily")
        assert st.steps[0].status == "skipped"
        assert st.steps[0].skip_reason == "--skip-scan"

    def test_mixed_steps_success_only_if_no_failures(self):
        steps = [
            _FakeStepResult("a", "ok", 0.1, ""),
            _FakeStepResult("b", "skipped", 0.0, "off"),
        ]
        st = status_from_pipeline_steps(steps)
        assert st.success is True
        assert st.steps_succeeded == 1
        assert st.steps_skipped == 1

    def test_any_failure_marks_overall_failure(self):
        steps = [
            _FakeStepResult("a", "ok", 0.1, ""),
            _FakeStepResult("b", "failed", 0.2, "oops"),
        ]
        st = status_from_pipeline_steps(steps)
        assert st.success is False
        assert st.exit_code == 1
        assert st.steps_failed == 1

    def test_unknown_status_defaults_to_failed(self):
        steps = [_FakeStepResult("a", "weird", 0.1, "")]
        st = status_from_pipeline_steps(steps)
        assert st.steps[0].status == "failed"


# ---------------------------------------------------------------------------
# Payload + Markdown
# ---------------------------------------------------------------------------

class TestBuildStatusPayload:
    def _status(self) -> PipelineRunStatus:
        return PipelineRunStatus(
            generated_at="2026-05-14T10:00:00+00:00",
            run_id="2026-05-14_daily_official",
            run_mode="daily",
            source="main",
            success=True,
            exit_code=0,
            steps=[StepStatus("run_portfolio_update", "succeeded", 1.0, notes="ok")],
            warnings=["w1"],
            summary={"drawdown_regime": "normal"},
        )

    def test_payload_contains_safety_flags(self):
        payload = build_status_payload(self._status())
        assert payload["observe_only"] is True
        assert payload["no_trade"] is True
        assert "disclaimer" in payload
        assert "broker" in payload["disclaimer"].lower() or "trade" in payload["disclaimer"].lower()

    def test_payload_contains_run_metadata(self):
        payload = build_status_payload(self._status())
        assert payload["run_id"] == "2026-05-14_daily_official"
        assert payload["run_mode"] == "daily"
        assert payload["source"] == "main"
        assert payload["success"] is True
        assert payload["exit_code"] == 0
        assert payload["steps_attempted"] == 1
        assert payload["steps_succeeded"] == 1

    def test_payload_steps_are_serialised(self):
        payload = build_status_payload(self._status())
        assert isinstance(payload["steps"], list)
        assert payload["steps"][0]["name"] == "run_portfolio_update"
        assert payload["steps"][0]["status"] == "succeeded"

    def test_payload_warnings_and_summary_pass_through(self):
        payload = build_status_payload(self._status())
        assert payload["warnings"] == ["w1"]
        assert payload["summary"]["drawdown_regime"] == "normal"


class TestRenderStatusMarkdown:
    def test_renders_required_sections(self):
        payload = build_status_payload(
            PipelineRunStatus(
                generated_at="2026-05-14T10:00:00+00:00",
                run_id="rid",
                run_mode="daily",
                source="main",
                success=False,
                exit_code=1,
                steps=[StepStatus("a", "failed", 0.2, error="boom")],
                errors=["boom"],
            )
        )
        md = render_status_markdown(payload)
        assert "# Pipeline Run — Status" in md
        assert "Safety flags" in md
        assert "`observe_only`: True" in md
        assert "`no_trade`: True" in md
        assert "[FAIL] `a`" in md
        assert "boom" in md


# ---------------------------------------------------------------------------
# write_pipeline_run_status
# ---------------------------------------------------------------------------

class TestWritePipelineRunStatus:
    def _ok_status(self) -> PipelineRunStatus:
        return PipelineRunStatus(
            generated_at="2026-05-14T10:00:00+00:00",
            run_id="2026-05-14_daily_official",
            run_mode="daily",
            source="main",
            success=True,
            exit_code=0,
            steps=[StepStatus("run_portfolio_update", "succeeded", 1.0)],
        )

    def test_writes_json_and_md(self, base_outputs: Path):
        result = write_pipeline_run_status(self._ok_status(), base_dir=base_outputs)
        assert "error" not in result
        json_path = Path(result["pipeline_run_status_json"])
        md_path = Path(result["pipeline_run_status_md"])
        assert json_path.exists()
        assert md_path.exists()
        # Located inside outputs/latest/
        assert json_path.parent.name == "latest"
        assert md_path.parent.name == "latest"

    def test_json_payload_is_parseable_and_has_safety_flags(self, base_outputs: Path):
        result = write_pipeline_run_status(self._ok_status(), base_dir=base_outputs)
        payload = json.loads(Path(result["pipeline_run_status_json"]).read_text(encoding="utf-8"))
        assert payload["observe_only"] is True
        assert payload["no_trade"] is True
        assert payload["run_id"] == "2026-05-14_daily_official"
        assert payload["source"] == "main"

    def test_relative_path_constants_used(self, base_outputs: Path):
        result = write_pipeline_run_status(self._ok_status(), base_dir=base_outputs)
        assert Path(result["pipeline_run_status_json"]).name == STATUS_JSON_RELATIVE
        assert Path(result["pipeline_run_status_md"]).name == STATUS_MD_RELATIVE

    def test_does_not_raise_when_payload_unserialisable(self, base_outputs: Path, monkeypatch):
        # safe_write_json uses default=str, but force a write failure by making
        # the target directory a file.
        latest = base_outputs / "latest"
        latest.write_text("blocker", encoding="utf-8")
        result = write_pipeline_run_status(self._ok_status(), base_dir=base_outputs)
        assert "error" in result
        # No exception leaked

    def test_failure_status_round_trips(self, base_outputs: Path):
        st = PipelineRunStatus(
            generated_at="2026-05-14T10:00:00+00:00",
            run_id="rid",
            run_mode="daily",
            source="run_daily_pipeline",
            success=False,
            exit_code=1,
            steps=[StepStatus("scan", "failed", 0.5, error="RuntimeError")],
        )
        result = write_pipeline_run_status(st, base_dir=base_outputs)
        payload = json.loads(Path(result["pipeline_run_status_json"]).read_text(encoding="utf-8"))
        assert payload["success"] is False
        assert payload["exit_code"] == 1
        assert payload["steps_failed"] == 1
        assert payload["steps"][0]["error"] == "RuntimeError"
