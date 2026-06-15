"""
Multi-source Crowd Radar runner (observe-only, sandbox-only, no-extra-cost).

Orchestrates the whole no-extra-cost crowd lane and writes its artifacts under
OutputNamespace.SANDBOX (→ outputs/sandbox/discovery/):
  - crowd_source_dev_doc_audit.json   (validated official-doc audit)
  - crowd_source_health.json          (per-source health + entitlement)
  - crowd_multi_source_velocity.json  (aggregated per-ticker crowd metrics)
  - crowd_multi_source_summary.md     (operator-glanceable source health + records)

It also (re)writes docs/CROWD_SOURCE_DEV_DOC_AUDIT.md from the audit module.

Never raises into the pipeline; never writes decision_plan / config / registry;
crowd signals adjust sandbox research priority only.
"""
from __future__ import annotations

import json
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
from portfolio_automation.social_intelligence.base import base_envelope, utc_now_iso
from portfolio_automation.social_intelligence.multi_source_crowd_aggregator import (
    aggregate_crowd_sources,
)
from portfolio_automation.social_sources.base import SourceResult
from portfolio_automation.social_sources.dev_doc_audit import (
    build_dev_doc_audit,
    render_dev_doc_audit_md,
)
from portfolio_automation.social_sources.source_health import (
    build_sources,
    collect_health,
)

logger = logging.getLogger("stockbot.social_sources.run_multi_source")

_AUDIT_PATH = "discovery/crowd_source_dev_doc_audit.json"
_HEALTH_PATH = "discovery/crowd_source_health.json"
_VELOCITY_PATH = "discovery/crowd_multi_source_velocity.json"
_SUMMARY_MD_PATH = "discovery/crowd_multi_source_summary.md"
_AUDIT_DOC = "docs/CROWD_SOURCE_DEV_DOC_AUDIT.md"


def _load_crowd_cfg(root: Path) -> dict[str, Any]:
    try:
        raw = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
        return dict(raw.get("crowd_radar") or {})
    except Exception:
        return {}


def _render_summary_md(health: list[SourceResult], agg: dict[str, Any]) -> str:
    lines = [
        "# Crowd Radar — Multi-Source (Sandbox)",
        "",
        "_Sandbox research intelligence only. Not a trade recommendation. No paid data sources enabled._",
        "",
        "## Source Health",
    ]
    for r in health:
        lines.append(f"- **{r.source_name}**: {r.status.value}"
                     + (f" — {'; '.join(r.warnings)}" if r.warnings else ""))
    lines.append("")
    lines.append(f"## Crowd Records ({agg.get('record_count', 0)})")
    if agg.get("labels"):
        lines.append(f"_Labels: {', '.join(agg['labels'])}_")
    for rec in (agg.get("records") or [])[:15]:
        lines.append(
            f"- **{rec['ticker']}** · velocity {rec.get('mention_velocity')} · "
            f"breadth {rec.get('source_breadth')} · hype {rec.get('hype_risk_score')} · "
            f"conf {rec.get('confidence')} · {', '.join(rec.get('labels') or []) or 'ok'}"
        )
    if not agg.get("records"):
        lines.append("- No active no-extra-cost source produced records this run.")
    lines.append("")
    lines.append("_Crowd signals adjust research priority only; they cannot trigger any trade._")
    return "\n".join(lines) + "\n"


def run_multi_source_crowd(root: str | Path = ".", run_mode: str = "discovery") -> dict[str, Any]:
    """Top-level entry. Never raises; returns a status dict."""
    try:
        root_path = Path(root).resolve()
        mode = normalize_run_mode(run_mode)
        run_id = utc_now_iso()
        cfg = _load_crowd_cfg(root_path)
        crowd_enabled = bool(cfg.get("enabled"))
        warnings: list[str] = []

        sources = build_sources(cfg)
        health = collect_health(sources)

        # Fetch + normalize only sources that are configured + reachable. Probe-only
        # / blocked sources contribute no records (their fetch returns inert status).
        normalized: list[SourceResult] = []
        for name, conn in sources.items():
            try:
                if not conn.is_configured():
                    continue
                raw = conn.fetch()
                normalized.append(conn.normalize(raw))
            except Exception as exc:  # pragma: no cover - connectors are fail-safe
                warnings.append(f"{name}_fetch_error:{type(exc).__name__}")

        agg = aggregate_crowd_sources(normalized)

        # --- payloads ---
        audit = build_dev_doc_audit(run_id=run_id, run_mode=mode.value, created_at=run_id)

        health_env = base_envelope(run_id=run_id, run_mode=mode.value,
                                   source_status="ok" if crowd_enabled else "disabled",
                                   data_quality_status="ok", warnings=warnings)
        health_env.update({
            "source": "crowd_source_health",
            "crowd_radar_enabled": crowd_enabled,
            "record_count": len(health),
            "records": [r.health_dict() for r in health],
        })

        velocity_env = base_envelope(run_id=run_id, run_mode=mode.value,
                                     source_status="ok" if agg["records"] else "insufficient_data",
                                     data_quality_status="ok", warnings=list(agg.get("labels") or []))
        velocity_env.update({
            "source": "crowd_multi_source_velocity",
            "contributing_sources": agg["contributing_sources"],
            "source_breadth_max": agg["source_breadth_max"],
            "labels": agg["labels"],
            "record_count": agg["record_count"],
            "records": agg["records"],
        })

        summary_md = _render_summary_md(health, agg)
        audit_md = render_dev_doc_audit_md(audit)

        artifacts: dict[str, str] = {}
        try:
            assert_can_write_namespace(mode, OutputNamespace.SANDBOX)
            base = root_path / "outputs"
            artifacts["dev_doc_audit"] = str(safe_write_json(OutputNamespace.SANDBOX, _AUDIT_PATH, audit, base_dir=base))
            artifacts["source_health"] = str(safe_write_json(OutputNamespace.SANDBOX, _HEALTH_PATH, health_env, base_dir=base))
            artifacts["multi_source_velocity"] = str(safe_write_json(OutputNamespace.SANDBOX, _VELOCITY_PATH, velocity_env, base_dir=base))
            artifacts["summary_md"] = str(safe_write_text(OutputNamespace.SANDBOX, _SUMMARY_MD_PATH, summary_md, base_dir=base))
            # The audit doc is a repo doc (not a namespaced artifact) — write directly.
            (root_path / _AUDIT_DOC).write_text(audit_md, encoding="utf-8")
            artifacts["audit_doc"] = _AUDIT_DOC
        except Exception as exc:
            warnings.append(f"write_skipped:{exc}")

        return {
            "status": "ok" if crowd_enabled else "disabled",
            "observe_only": True,
            "contributing_sources": agg["contributing_sources"],
            "record_count": agg["record_count"],
            "artifacts": artifacts,
            "warnings": warnings,
        }
    except Exception as exc:  # pragma: no cover
        logger.warning("run_multi_source_crowd failed: %s", exc)
        return {"status": "error", "error": str(exc), "observe_only": True}


if __name__ == "__main__":  # pragma: no cover
    import argparse

    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Multi-source Crowd Radar runner (observe-only)")
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
    print(json.dumps(run_multi_source_crowd(root=args.root, run_mode=args.run_mode), indent=2, default=str))
