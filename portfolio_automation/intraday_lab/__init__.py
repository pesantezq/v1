"""Governed Intraday Strategy Lab — research-only.

HARD ISOLATION: no broker, no orders, no execution, no writes to
decision_plan.json, no portfolio score mutation, no automatic promotion.
Artifacts land in OutputNamespace.HISTORICAL (offline research) or
OutputNamespace.SIMULATION (paper). There is no production path.

Session 1 scope: data foundation, temporal/point-in-time discipline, quality,
fingerprinting, provider feasibility. No strategies, no simulator, no costs,
no risk model, no walk-forward. Those are later sessions and must respect the
temporal contract fixed here (see validation.earliest_order_time).
"""
from portfolio_automation.intraday_lab.models import (  # noqa: F401
    IntradayBar, FeatureObservation, TIMEFRAMES, SCHEMA_VERSION,
    BarValidationError, TemporalViolation, ensure_utc,
)
from portfolio_automation.intraday_lab.validation import (  # noqa: F401
    admissible_inputs, assert_no_lookahead, earliest_order_time,
    canonicalize, profile_session, dataset_fingerprint, dataset_manifest,
    DuplicateBarError,
)
