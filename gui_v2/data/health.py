"""Health page — observability over every probe."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
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


def _ai_cost_trend(repo_root: Path, days: int = 30) -> dict[str, Any]:
    """
    Compute a per-day AI cost + token series from
    outputs/policy/ai_usage_events.jsonl over the last *days* days.

    Returns ``{"available": True, "days": [{date, cost_usd, tokens, event_count}],
    "total_cost_usd", "total_tokens", "max_cost_usd", "seven_day_cost_usd"}``.

    Falls back to ``{"available": False}`` when the log is missing or
    empty.  Never raises.
    """
    path = Path(repo_root) / "outputs" / "policy" / "ai_usage_events.jsonl"
    if not path.exists():
        return {"available": False}

    today = datetime.now(timezone.utc).date()
    window_start = today - timedelta(days=days - 1)
    buckets: dict[date, dict[str, float]] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                ts = ev.get("timestamp")
                if not isinstance(ts, str):
                    continue
                try:
                    dt = datetime.fromisoformat(ts)
                except Exception:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                day = dt.date()
                if day < window_start or day > today:
                    continue
                bucket = buckets.setdefault(day, {"cost_usd": 0.0, "tokens": 0.0, "event_count": 0})
                try:
                    bucket["cost_usd"] += float(ev.get("estimated_cost_usd") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    bucket["tokens"] += float(ev.get("total_tokens") or 0)
                except (TypeError, ValueError):
                    pass
                bucket["event_count"] += 1
    except OSError:
        return {"available": False}

    # Fill missing days with zeros for a stable chart
    series: list[dict[str, Any]] = []
    for i in range(days):
        day = window_start + timedelta(days=i)
        b = buckets.get(day, {"cost_usd": 0.0, "tokens": 0.0, "event_count": 0})
        series.append({
            "date": day.isoformat(),
            "cost_usd": round(b["cost_usd"], 6),
            "tokens": int(b["tokens"]),
            "event_count": int(b["event_count"]),
        })

    total_cost = sum(s["cost_usd"] for s in series)
    total_tokens = sum(s["tokens"] for s in series)
    seven_day_cost = sum(s["cost_usd"] for s in series[-7:])
    max_cost = max((s["cost_usd"] for s in series), default=0.0)

    return {
        "available": True,
        "days": series,
        "window_days": days,
        "total_cost_usd": round(total_cost, 6),
        "total_tokens": total_tokens,
        "seven_day_cost_usd": round(seven_day_cost, 6),
        "max_cost_usd": round(max_cost, 6),
    }


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

    # 30-day AI cost trend (additive — independent of tools.status / smoke)
    try:
        out["ai_cost_trend"] = _ai_cost_trend(repo_root)
    except Exception as exc:
        out["ai_cost_trend"] = {"available": False, "error": f"trend_failed: {exc}"}

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
