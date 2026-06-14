"""
Crowd Radar activation checklist — observe-only pre-flight gate.

Answers one operator question deterministically: *is Crowd Radar safe and ready
to start collecting?* It inspects every activation prerequisite — feature flag,
Reddit credentials, source-terms compliance, rate-limit config, raw-text-storage
and AI-processing policy, the sandbox-only / decision-engine-blocked invariants,
and the last smoke-test result — and writes
``outputs/sandbox/discovery/crowd_radar_activation_check.json``.

Sandbox-only, observe-only. Reads config + environment + the source registry;
it NEVER fetches from a network source, never trades, and never mutates the
portfolio or ``outputs/latest/decision_plan.json``. It is a read-only readiness
probe, not the collector.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)
from portfolio_automation.run_mode_governance import (
    assert_can_write_namespace,
    normalize_run_mode,
)
from portfolio_automation.social_intelligence.base import (
    DISCOVERY_ONLY,
    NO_TRADE,
    SANDBOX_ONLY,
    SourceStatus,
    base_envelope,
    utc_now_iso,
)
from portfolio_automation.social_intelligence.reddit_connector import RedditCredentials
from portfolio_automation.social_intelligence.source_registry import (
    DEFAULT_SOURCES,
    get_source,
)

logger = logging.getLogger("stockbot.social_intelligence.activation_check")

# Relative to OutputNamespace.SANDBOX root → outputs/sandbox/.
_ACTIVATION_PATH = "discovery/crowd_radar_activation_check.json"
_ACTIVATION_MD_PATH = "discovery/crowd_radar_activation_check.md"
# Optional smoke-test marker the activation check reads (written by a live
# --smoke run, when one exists). Absent → "never_run".
_SMOKE_MARKER_REL = "outputs/sandbox/discovery/crowd_radar_smoke_test.json"

_DEFAULT_CONFIG = {
    "enabled": False,
    "sources": ["reddit"],
}

# Source-terms states that block collection (anything that is not "approved").
_TERMS_APPROVED = "approved"


def _load_config(root: Path) -> dict[str, Any]:
    """Read config.json crowd_radar block; defaults on any failure."""
    import json

    try:
        raw = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
        cfg = dict(_DEFAULT_CONFIG)
        cfg.update(raw.get("crowd_radar") or {})
        return cfg
    except Exception as exc:  # pragma: no cover - config read is best-effort
        logger.debug("activation_check: config load failed (%s) — using defaults", exc)
        return dict(_DEFAULT_CONFIG)


def _kill_switched(root: Path) -> bool:
    """Mirror the orchestrator's kill-switch surfaces (env var + sentinel file)."""
    import os

    from portfolio_automation.social_intelligence.base import (
        KILL_SWITCH_ENV,
        KILL_SWITCH_FILE,
    )

    if (os.environ.get(KILL_SWITCH_ENV) or "").strip() in ("1", "true", "True"):
        return True
    return (root / KILL_SWITCH_FILE).exists()


def _read_smoke_status(root: Path) -> str:
    """Last smoke-test status from the marker artifact, else 'never_run'."""
    import json

    path = root / _SMOKE_MARKER_REL
    if not path.exists():
        return "never_run"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return str(data.get("status") or "unknown")
    except Exception:
        return "unknown"


def _aggregate_terms_status(enabled_source_names: list[str]) -> tuple[str, bool, bool, bool]:
    """
    Return (source_terms_status, rate_limit_configured, raw_text_storage_allowed,
    ai_processing_allowed) aggregated over the enabled sources.

    - terms_status: 'approved' only if every enabled source is approved; otherwise
      the worst status seen ('blocked' dominates 'review_needed').
    - rate_limit_configured: every enabled source carries a non-empty rate_limit.
    - raw_text_storage_allowed / ai_processing_allowed: the conservative AND across
      enabled sources (so the gate reflects the strictest policy in force).
    """
    if not enabled_source_names:
        return ("no_sources", False, False, False)

    statuses: list[str] = []
    rate_ok = True
    raw_ok = True
    ai_ok = True
    for name in enabled_source_names:
        src = get_source(name, DEFAULT_SOURCES)
        if src is None:
            statuses.append("unregistered")
            rate_ok = False
            raw_ok = False
            ai_ok = False
            continue
        statuses.append(src.compliance_status)
        rate_ok = rate_ok and bool((src.rate_limit or "").strip())
        raw_ok = raw_ok and bool(src.raw_text_storage_allowed)
        ai_ok = ai_ok and bool(src.ai_processing_allowed)

    if "blocked" in statuses:
        terms = "blocked"
    elif any(s != _TERMS_APPROVED for s in statuses):
        terms = "review_needed"
    else:
        terms = _TERMS_APPROVED
    return (terms, rate_ok, raw_ok, ai_ok)


def build_activation_check(
    root: str | Path = ".",
    *,
    run_mode: str = "discovery",
    credentials_present: bool | None = None,
    smoke_test_status: str | None = None,
) -> dict[str, Any]:
    """
    Build the activation-checklist payload (pure; no writes, no network).

    ``credentials_present`` / ``smoke_test_status`` are injectable seams for tests;
    when None they are derived from the environment and the smoke marker.
    """
    root_path = Path(root)
    cfg = _load_config(root_path)
    warnings: list[str] = []

    enabled = bool(cfg.get("enabled"))
    kill = _kill_switched(root_path)
    enabled_sources = [str(s) for s in (cfg.get("sources") or [])]

    if credentials_present is None:
        credentials_present = RedditCredentials.from_env() is not None

    terms_status, rate_limit_configured, raw_text_allowed, ai_allowed = (
        _aggregate_terms_status(enabled_sources)
    )

    smoke = smoke_test_status if smoke_test_status is not None else _read_smoke_status(root_path)

    # Derive the effective source_status the layer WOULD report on activation,
    # without fetching. Order mirrors the orchestrator's gating precedence.
    if kill:
        source_status = SourceStatus.DISABLED.value
        warnings.append("kill_switch_active")
    elif not enabled:
        source_status = SourceStatus.DISABLED.value
        warnings.append("crowd_radar.enabled=false")
    elif not credentials_present:
        source_status = SourceStatus.NO_CREDENTIALS.value
        warnings.append("REDDIT_* credentials not set")
    elif terms_status == "blocked":
        source_status = SourceStatus.SOURCE_TERMS_BLOCKED.value
        warnings.append("source_terms_blocked")
    else:
        source_status = SourceStatus.OK.value

    if enabled and credentials_present and terms_status == "review_needed":
        warnings.append("source_terms_review_needed")
    if enabled and credentials_present and not rate_limit_configured:
        warnings.append("rate_limit_not_configured")
    if not enabled_sources:
        warnings.append("no_sources_configured")

    # ready_to_collect = every activation gate is green.
    ready_to_collect = bool(
        enabled
        and credentials_present
        and not kill
        and terms_status == _TERMS_APPROVED
        and rate_limit_configured
        and source_status == SourceStatus.OK.value
    )

    data_quality = "ok" if source_status == SourceStatus.OK.value else "disabled"
    env = base_envelope(
        run_id=utc_now_iso(),
        run_mode=run_mode,
        source_status=source_status,
        data_quality_status=data_quality,
        warnings=warnings,
    )
    env.update({
        "enabled": enabled,
        "credentials_present": bool(credentials_present),
        "source_status": source_status,
        "source_terms_status": terms_status,
        "rate_limit_configured": bool(rate_limit_configured),
        "raw_text_storage_allowed": bool(raw_text_allowed),
        "ai_processing_allowed": bool(ai_allowed),
        # Hardcoded invariants — these can never be flipped on at activation time.
        "sandbox_only_assertion": SANDBOX_ONLY and DISCOVERY_ONLY,
        "decision_engine_blocked": NO_TRADE,
        "last_smoke_test_status": smoke,
        "ready_to_collect": ready_to_collect,
        "enabled_sources": enabled_sources,
    })
    return env


def render_activation_check_md(payload: dict[str, Any]) -> str:
    """Operator-glanceable Markdown rendering of the checklist."""
    def _mark(v: Any) -> str:
        if isinstance(v, bool):
            return "✅" if v else "❌"
        return str(v)

    lines = [
        "# Crowd Radar — Activation Checklist",
        "",
        "_Observe-only readiness probe. Not a collector; never trades._",
        "",
        f"- **Ready to collect:** {_mark(payload.get('ready_to_collect'))}",
        f"- Enabled (config): {_mark(payload.get('enabled'))}",
        f"- Credentials present: {_mark(payload.get('credentials_present'))}",
        f"- Source status: **{payload.get('source_status')}**",
        f"- Source-terms status: **{payload.get('source_terms_status')}**",
        f"- Rate limit configured: {_mark(payload.get('rate_limit_configured'))}",
        f"- Raw-text storage allowed: {_mark(payload.get('raw_text_storage_allowed'))}",
        f"- AI processing allowed: {_mark(payload.get('ai_processing_allowed'))}",
        f"- Sandbox-only assertion: {_mark(payload.get('sandbox_only_assertion'))}",
        f"- Decision-engine blocked: {_mark(payload.get('decision_engine_blocked'))}",
        f"- Last smoke test: **{payload.get('last_smoke_test_status')}**",
    ]
    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("**Blocking / notes:**")
        lines.extend(f"- {w}" for w in warnings)
    lines.append("")
    lines.append("_Crowd signals adjust research priority only; they cannot trigger any trade._")
    return "\n".join(lines) + "\n"


def run_activation_check(
    root: str | Path = ".",
    run_mode: str = "discovery",
) -> dict[str, Any]:
    """
    Top-level entry. Never raises — returns a status dict and writes the artifact
    (JSON + MD) under OutputNamespace.SANDBOX. On any unhandled error returns a
    degraded-state dict and writes nothing.
    """
    try:
        root_path = Path(root).resolve()
        mode = normalize_run_mode(run_mode)
        payload = build_activation_check(root_path, run_mode=mode.value)
        md = render_activation_check_md(payload)

        artifacts: dict[str, str] = {}
        try:
            assert_can_write_namespace(mode, OutputNamespace.SANDBOX)
            artifacts["crowd_radar_activation_check"] = str(
                safe_write_json(
                    OutputNamespace.SANDBOX, _ACTIVATION_PATH, payload,
                    base_dir=root_path / "outputs",
                )
            )
            artifacts["crowd_radar_activation_check_md"] = str(
                safe_write_text(
                    OutputNamespace.SANDBOX, _ACTIVATION_MD_PATH, md,
                    base_dir=root_path / "outputs",
                )
            )
        except Exception as exc:
            payload.setdefault("warnings", []).append(f"write_skipped:{exc}")

        return {
            "status": payload.get("source_status"),
            "ready_to_collect": payload.get("ready_to_collect"),
            "observe_only": True,
            "artifacts": artifacts,
            "warnings": payload.get("warnings", []),
        }
    except Exception as exc:  # pragma: no cover - defensive top-level guard
        logger.warning("run_activation_check failed: %s", exc)
        return {"status": "error", "error": str(exc), "observe_only": True}


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import json as _json
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Crowd Radar activation checklist (observe-only)")
    ap.add_argument("--root", default=str(repo_root))
    ap.add_argument("--run-mode", default="discovery")
    args = ap.parse_args()

    try:
        sys.path.insert(0, args.root)
        from utils import load_env

        load_env(str(Path(args.root) / ".env"))
    except Exception:
        pass
    print(_json.dumps(run_activation_check(root=args.root, run_mode=args.run_mode), indent=2, default=str))
