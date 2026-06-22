# Confidence Calibration

## Purpose

Retrospective analysis of how well the system's confidence scores predict actual decision outcomes. Observe-only — produces visibility artifacts, never modifies live scoring or registry entries automatically.

## Observe-Only Contract

- All artifacts carry `"observe_only": true`
- No scoring, allocation, recommendation, or registry values are changed by this layer
- Tuning recommendations in the output are advisory notes for operator review only
- `discovery_only` signals are excluded from tuning recommendations

## Inputs

| Source | Path |
|--------|------|
| Resolved decision outcomes | `outputs/policy/decision_outcomes.jsonl` |
| Data quality report (optional) | `outputs/latest/data_quality_report.json` |
| Signal registry (optional) | `config/signal_registry.yaml` |

Only rows with `"resolved": true` are included in analysis.

## Outputs

| Artifact | Path | Namespace |
|----------|------|-----------|
| Enhanced calibration JSON | `outputs/latest/confidence_calibration.json` | LATEST |
| Enhanced calibration Markdown | `outputs/latest/confidence_calibration.md` | LATEST |
| Legacy calibration JSON (GUI) | `outputs/policy/confidence_calibration.json` | POLICY |
| Legacy calibration Markdown | `outputs/policy/confidence_calibration.md` | POLICY |

Both write paths coexist. The GUI reads from POLICY. LATEST carries the enhanced 5-bucket and per-signal analysis.

## Artifact Contract — `outputs/latest/confidence_calibration.json`

The JSON schema is stable regardless of data availability:

| Field | Type | Always present |
|-------|------|----------------|
| `generated_at` | ISO timestamp string | Yes |
| `observe_only` | `true` | Yes |
| `available` | bool | Yes |
| `insufficient_data` | bool | Yes |
| `total_resolved` | int | Yes |
| `min_required` | int | Yes |
| `overall_hit_rate` | float or null | Yes |
| `overall_average_confidence` | float or null | Yes |
| `overall_calibration_gap` | float or null | Yes |
| `buckets_5` | array of 5 objects | **Always — even when `insufficient_data=true`** |
| `signal_results` | array | Yes (may be empty) |
| `dq_warnings` | array | Yes (may be empty) |
| `summary_line` | string | Yes |

### `buckets_5` contract

`buckets_5` always contains exactly 5 bucket objects in this order:
`very_low`, `low`, `medium`, `high`, `very_high`.

Each bucket object always has these keys: `label`, `lower`, `upper`, `count`, `hit_rate`, `average_confidence`, `calibration_gap`.

When `insufficient_data=true`, `count` will be 0 or low and `hit_rate`/`average_confidence`/`calibration_gap` will be `null` for empty buckets. **The schema shape does not change.** Consumers can always iterate all 5 buckets without a null-check on the array itself.

## Metrics

### Overall Calibration Gap

```
calibration_gap = average_confidence - hit_rate
```

- Positive → system is overconfident
- Negative → system is underconfident
- Target: `abs(gap) < 0.15`

### 5-Bucket System

Confidence scores are binned into 5 ranges:

| Bucket | Range |
|--------|-------|
| very_low | [0.00, 0.25) |
| low | [0.25, 0.50) |
| medium | [0.50, 0.70) |
| high | [0.70, 0.85) |
| very_high | [0.85, 1.00] |

Per-bucket metrics: `count`, `hit_rate`, `average_confidence`, `calibration_gap`.

### Per-Signal Calibration

Groups resolved decisions by `source` field. Signals with fewer than 5 resolved decisions are excluded.

Per-signal fields:
- `hit_rate` — fraction of correct direction predictions
- `average_confidence` — mean confidence when this signal was present
- `calibration_gap` — `average_confidence - hit_rate`
- `overconfident` — `gap > 0.15`
- `underconfident` — `gap < -0.15`
- `suggested_review` — `True` only when miscalibrated AND not discovery-only
- `note` — human-readable advisory

## Signal Registry Integration

When `config/signal_registry.yaml` is available, each signal is checked:
- `validate_signal_id` — whether the signal is known
- `is_discovery_only` — discovery-only signals are never flagged for tuning

Unknown signals (not in registry) are treated as discovery-only.

## Data Quality Integration

If `outputs/latest/data_quality_report.json` exists, critical and warning issues are surfaced in the `dq_warnings` list of the calibration artifact. This provides context for interpreting calibration results when data was degraded.

## Minimum Data Gate

Analysis requires at least 20 resolved decisions (configurable via `min_resolved`). Below this threshold, the artifact reports `insufficient_data: true` and `available: true` — the artifact is written but contains no metrics.

## No Automatic Registry Edits

This layer never edits `config/signal_registry.yaml`. The `suggested_review` flag and `note` fields are advisory only. Confidence floor tuning requires explicit operator action.

## GUI: Calibration Trend Card

The `/dashboard/quant` page renders a "Calibration Trend" card (built by
`_calibration_trend_card` in `gui_v2/data/dash_quant.py`) next to the existing
"Confidence Calibration" card in the "Confidence & Pattern Efficacy" section.
It is observe-only and never raises.

It trends the calibration gap over time by reading
`overall_calibration_gap` from each `outputs/history/<date>/confidence_calibration.json`
snapshot (`_calibration_gap_history`; ISO-date directory names sort
chronologically). The label compares the earliest vs latest gap in the available
history window:

| Label | Status | Condition |
|-------|--------|-----------|
| Improving | ok | latest − earliest < −0.02 (gap shrinking → better calibrated) |
| Worsening | warning | latest − earliest > +0.02 (gap growing) |
| Stable | info | within ±0.02 |
| Insufficient history | unknown | fewer than 2 snapshots |

The ±0.02 stable band is `_CALIBRATION_TREND_EPS`.

The card also annotates over/under-confident buckets from the latest
`buckets_5` (`_bucket_confidence_annotation`): a bucket whose `calibration_gap`
exceeds +0.10 is flagged overconfident, below −0.10 underconfident
(`_BUCKET_OVERCONFIDENT_GAP`); otherwise "Buckets within tolerance".

## Pipeline Integration

`run_calibration()` is called from `main.py` via `_run_calibration`. When `write_files=True`:
1. Writes legacy metrics to `outputs/policy/` (POLICY namespace)
2. Calls `write_confidence_calibration_report()` to write enhanced metrics to `outputs/latest/` (LATEST namespace)

The LATEST write is non-blocking — failures are logged as warnings and do not interrupt the pipeline.

## API

```python
from portfolio_automation.confidence_calibration import (
    evaluate_confidence_calibration,
    write_confidence_calibration_report,
    load_decision_outcomes,
    load_data_quality_report,
    ConfidenceCalibrationSummary,
)

# Pure evaluation (no I/O)
resolved = [r for r in load_decision_outcomes() if r.get("resolved")]
summary = evaluate_confidence_calibration(resolved, dq_report={}, registry=None)

# Load + evaluate + write to LATEST
summary = write_confidence_calibration_report(root=Path("."))
```
