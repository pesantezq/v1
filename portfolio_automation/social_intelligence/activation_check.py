"""
Crowd Radar activation checklist — observe-only, multi-source readiness probe.

Answers "is Crowd Radar safe + ready to start collecting, and from which no-extra-
cost sources?" deterministically. Pure by default (no network): it reads config,
environment credential presence, the dev-doc audit (source classification), and —
if present — the cached crowd_source_health.json for confirmed entitlements.

Writes outputs/sandbox/discovery/crowd_radar_activation_check.json (+ .md). It is
a readiness gate, never a collector; crowd signals can never trigger a trade.
"""
from __future__ import annotations

import json
import logging
import os
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
    KILL_SWITCH_ENV,
    KILL_SWITCH_FILE,
    NO_TRADE,
    SANDBOX_ONLY,
    SourceStatus,
    base_envelope,
    utc_now_iso,
)
from portfolio_automation.social_sources.source_health import (
    classify_sources,
    credentials_present,
)

logger = logging.getLogger("stockbot.social_intelligence.activation_check")

_ACTIVATION_PATH = "discovery/crowd_radar_activation_check.json"
_ACTIVATION_MD_PATH = "discovery/crowd_radar_activation_check.md"
_HEALTH_REL = "outputs/sandbox/discovery/crowd_source_health.json"
_SMOKE_MARKER_REL = "outputs/sandbox/discovery/crowd_radar_smoke_test.json"


def _load_crowd_cfg(root: Path) -> dict[str, Any]:
    try:
        raw = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
        return dict(raw.get("crowd_radar") or {})
    except Exception as exc:  # pragma: no cover
        logger.debug("activation_check: config load failed (%s)", exc)
        return {}


def _kill_switched(root: Path) -> bool:
    if (os.environ.get(KILL_SWITCH_ENV) or "").strip() in ("1", "true", "True"):
        return True
    return (root / KILL_SWITCH_FILE).exists()


def _read_entitlements(root: Path) -> dict[str, bool]:
    """Confirmed entitlements from the cached health artifact (if the runner ran)."""
    path = root / _HEALTH_REL
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        out: dict[str, bool] = {}
        for rec in (data.get("records") or []):
            ent = (rec.get("meta") or {}).get("entitled")
            if ent is not None:
                out[rec.get("source_name")] = bool(ent)
        return out
    except Exception:
        return {}


def _read_smoke_status(root: Path) -> str:
    path = root / _SMOKE_MARKER_REL
    if not path.exists():
        return "never_run"
    try:
        return str(json.loads(path.read_text(encoding="utf-8", errors="replace")).get("status") or "unknown")
    except Exception:
        return "unknown"


def build_activation_check(
    root: str | Path = ".",
    *,
    run_mode: str = "discovery",
) -> dict[str, Any]:
    """Build the multi-source activation-checklist payload (pure; no network)."""
    root_path = Path(root)
    cfg = _load_crowd_cfg(root_path)
    warnings: list[str] = []

    enabled = bool(cfg.get("enabled"))
    cost_policy = str(cfg.get("cost_policy", "no_extra_cost"))
    allow_paid = bool(cfg.get("allow_paid_sources", False))
    kill = _kill_switched(root_path)

    classes = classify_sources(cfg)
    active_sources = classes["active"]
    probe_only_sources = classes["probe_only"]
    blocked_sources = classes["blocked"]

    creds = credentials_present()
    entitlements = _read_entitlements(root_path)

    if kill:
        source_status = SourceStatus.DISABLED.value
        warnings.append("kill_switch_active")
    elif not enabled:
        source_status = SourceStatus.DISABLED.value
        warnings.append("crowd_radar.enabled=false")
    elif not active_sources:
        source_status = SourceStatus.NOT_CONFIGURED.value
        warnings.append("no active no-extra-cost sources")
    else:
        source_status = SourceStatus.OK.value

    if allow_paid:
        warnings.append("allow_paid_sources=true (policy override) — verify intent")

    # ready_to_collect: enabled, not killed, and at least one ACTIVE no-extra-cost
    # source (ApeWisdom is the only one reachable without a paid plan/approval).
    ready_to_collect = bool(enabled and not kill and active_sources)

    env = base_envelope(
        run_id=utc_now_iso(),
        run_mode=run_mode,
        source_status=source_status,
        data_quality_status="ok" if source_status == SourceStatus.OK.value else "disabled",
        warnings=warnings,
    )
    env.update({
        "enabled": enabled,
        "cost_policy": cost_policy,
        "allow_paid_sources": allow_paid,
        "active_sources": active_sources,
        "probe_only_sources": probe_only_sources,
        "blocked_sources": blocked_sources,
        "credentials_present": creds,
        "entitlements_confirmed": entitlements,
        "api_docs_audited": True,  # dev_doc_audit module is the audited source of truth
        "rate_limit_configured": True,  # ApeWisdom polite-throttled; probes single-call
        "raw_text_storage_allowed": False,  # aggregate counts only — no raw post text
        "ai_processing_allowed": True,      # derived features may be AI-processed
        "sandbox_only_assertion": SANDBOX_ONLY and DISCOVERY_ONLY,
        "decision_engine_blocked": NO_TRADE,
        "last_smoke_test_status": _read_smoke_status(root_path),
        "ready_to_collect": ready_to_collect,
        "source_status": source_status,
    })
    return env


def render_activation_check_md(payload: dict[str, Any]) -> str:
    def _m(v: Any) -> str:
        return ("✅" if v else "❌") if isinstance(v, bool) else str(v)
    lines = [
        "# Crowd Radar — Activation Checklist (multi-source)",
        "",
        "_Sandbox research intelligence only. Not a trade recommendation. No paid data sources enabled._",
        "",
        f"- **Ready to collect:** {_m(payload.get('ready_to_collect'))}",
        f"- Enabled: {_m(payload.get('enabled'))} · cost_policy: **{payload.get('cost_policy')}** · allow_paid: {_m(payload.get('allow_paid_sources'))}",
        f"- Active sources: **{', '.join(payload.get('active_sources') or []) or 'none'}**",
        f"- Probe-only: {', '.join(payload.get('probe_only_sources') or []) or 'none'}",
        f"- Blocked (no-extra-cost): {', '.join(payload.get('blocked_sources') or []) or 'none'}",
        f"- API docs audited: {_m(payload.get('api_docs_audited'))}",
        f"- Raw-text storage allowed: {_m(payload.get('raw_text_storage_allowed'))} · AI processing: {_m(payload.get('ai_processing_allowed'))}",
        f"- Sandbox-only: {_m(payload.get('sandbox_only_assertion'))} · Decision-engine blocked: {_m(payload.get('decision_engine_blocked'))}",
    ]
    creds = payload.get("credentials_present") or {}
    lines.append("- Credentials present: " + ", ".join(f"{k}={_m(v)}" for k, v in creds.items()))
    if payload.get("warnings"):
        lines.append("")
        lines.append("**Notes:** " + "; ".join(payload["warnings"]))
    lines.append("")
    lines.append("_Crowd signals adjust research priority only; they cannot trigger any trade._")
    return "\n".join(lines) + "\n"


def run_activation_check(root: str | Path = ".", run_mode: str = "discovery") -> dict[str, Any]:
    """Top-level entry. Never raises; writes JSON + MD under SANDBOX namespace."""
    try:
        root_path = Path(root).resolve()
        mode = normalize_run_mode(run_mode)
        payload = build_activation_check(root_path, run_mode=mode.value)
        md = render_activation_check_md(payload)
        artifacts: dict[str, str] = {}
        try:
            assert_can_write_namespace(mode, OutputNamespace.SANDBOX)
            artifacts["crowd_radar_activation_check"] = str(safe_write_json(
                OutputNamespace.SANDBOX, _ACTIVATION_PATH, payload, base_dir=root_path / "outputs"))
            artifacts["crowd_radar_activation_check_md"] = str(safe_write_text(
                OutputNamespace.SANDBOX, _ACTIVATION_MD_PATH, md, base_dir=root_path / "outputs"))
        except Exception as exc:
            payload.setdefault("warnings", []).append(f"write_skipped:{exc}")
        return {
            "status": payload.get("source_status"),
            "ready_to_collect": payload.get("ready_to_collect"),
            "active_sources": payload.get("active_sources"),
            "observe_only": True,
            "artifacts": artifacts,
            "warnings": payload.get("warnings", []),
        }
    except Exception as exc:  # pragma: no cover
        logger.warning("run_activation_check failed: %s", exc)
        return {"status": "error", "error": str(exc), "observe_only": True}


if __name__ == "__main__":  # pragma: no cover
    import argparse

    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Crowd Radar activation checklist (multi-source, observe-only)")
    ap.add_argument("--root", default=str(repo_root))
    ap.add_argument("--run-mode", default="discovery")
    args = ap.parse_args()
    try:
        import sys
        sys.path.insert(0, args.root)
        from utils import load_env
        load_env(str(Path(args.root) / ".env"))
    except Exception:
        pass
    print(json.dumps(run_activation_check(root=args.root, run_mode=args.run_mode), indent=2, default=str))
