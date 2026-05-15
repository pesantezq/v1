"""Health page — observability over every probe."""
from __future__ import annotations

from pathlib import Path
from typing import Any

SEV_OK = "OK"
SEV_INFO = "INFO"
SEV_WARN = "WARN"
SEV_FAIL = "FAIL"
_ORDER = {SEV_OK: 0, SEV_INFO: 1, SEV_WARN: 2, SEV_FAIL: 3}


def _safe(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def collect_health_view(repo_root: Path) -> dict[str, Any]:
    """Aggregate every probe + registry view into a single dict. Never raises."""
    out: dict[str, Any] = {
        "advisory_only": True,
        "no_trade": True,
        "repo_root": str(repo_root),
    }

    # tools.status
    try:
        from tools.status import collect_status
        report, err = _safe(collect_status, repo_root)
        out["status"] = report.to_dict() if (err is None and report is not None) \
                        else {"error": err or "no report"}
    except Exception as exc:
        out["status"] = {"error": f"import_failed: {exc}"}

    # tools.smoke_test
    try:
        from tools.smoke_test import validate_registry
        report, err = _safe(validate_registry, repo_root)
        out["smoke"] = report.to_dict() if (err is None and report is not None) \
                       else {"error": err or "no report"}
    except Exception as exc:
        out["smoke"] = {"error": f"import_failed: {exc}"}

    # portfolio_automation.env
    try:
        from portfolio_automation.env import check_state
        state, err = _safe(check_state)
        out["env"] = state if (err is None and state is not None) \
                     else {"error": err or "no state"}
    except Exception as exc:
        out["env"] = {"error": f"import_failed: {exc}"}

    # artifacts registry inventory
    try:
        from portfolio_automation.artifacts_registry import REGISTRY
        by_ns: dict[str, int] = {}
        entries: list[dict[str, Any]] = []
        for art in REGISTRY:
            ns = art.namespace.value
            by_ns[ns] = by_ns.get(ns, 0) + 1
            entries.append({
                "name": art.name,
                "namespace": ns,
                "relative_path": art.relative_path,
                "format": art.format,
                "optional": art.optional,
                "append_only": art.append_only,
                "observe_only_required": art.observe_only_required,
            })
        out["registry"] = {
            "total": len(entries),
            "by_namespace": by_ns,
            "entries": entries,
        }
    except Exception as exc:
        out["registry"] = {"error": f"import_failed: {exc}"}

    return out


def overall_severity(health: dict[str, Any]) -> str:
    """FAIL > WARN > INFO > OK; missing required env promotes to at least WARN."""
    worst = SEV_OK
    for key in ("status", "smoke"):
        section = health.get(key, {})
        if isinstance(section, dict):
            sev = section.get("overall_severity") or SEV_OK
            if _ORDER.get(sev, 0) > _ORDER.get(worst, 0):
                worst = sev
    env = health.get("env") if isinstance(health.get("env"), dict) else {}
    env_summary = env.get("summary", {}) if isinstance(env, dict) else {}
    if env_summary.get("required_missing", 0) > 0 and _ORDER["WARN"] > _ORDER.get(worst, 0):
        worst = SEV_WARN
    return worst
