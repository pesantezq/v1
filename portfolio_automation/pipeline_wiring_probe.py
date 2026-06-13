"""Pipeline wiring probe — observe-only root-cause layer for stale producers.

Motivation
----------
The artifact-registry validator answers "is artifact X stale?". It does NOT
answer "why". The 2026-06-11 stale-producer audit found three distinct reasons
a declared daily/weekly producer goes stale, all of which look identical to the
registry (just "stale"):

  - unwired           — no cron/script invokes the producer at all
  - cadence_mismatch  — the producer IS invoked, but by a script of a different
                        cadence than the registry declares (e.g. declared daily,
                        only called in run_weekly_safe.sh)
  - silently_skipped  — the producer is wired for the right cadence but a config
                        gate / missing param makes it a no-op (e.g. main.py
                        omitting scraped_intel_config)

This probe crosses two signals per registry producer:
  - freshness (authoritative): artifact mtime vs its cadence window
  - static caller-grep (classifier): which cadence's cron script names the
    producer (its module token), with main.py + the orchestrator modules it
    calls treated as the daily "core" corpus

Producers that are fresh are healthy; stale ones are classified into the three
buckets above so an operator (or the daily check) sees the root cause, not just
the symptom. A per-producer content layer additionally flags "looks fresh but
empty" for the highest-value artifacts.

Invariants
----------
  - observe_only=True hardcoded; never writes decision/score/allocation data.
  - Read-only over the registry yaml, cron scripts, orchestrator modules, and
    artifact mtimes. Writes only its own status artifact under outputs/latest/.
  - Never raises out of run_pipeline_wiring_probe (degrades to amber + error).
  - AMBER-max: it is a meta-monitor, never a RED authority.

Public API
----------
  classify_producers(registry, script_texts, artifact_ages_hours, *,
                     content_flags=None, config_gates=None) -> dict   (pure)
  run_pipeline_wiring_probe(root='.', write_files=True, now=None) -> dict
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.pipeline_wiring_probe")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "pipeline_wiring_probe"
_OBSERVE_ONLY = True

_OUTPUT_JSON_REL = "pipeline_wiring_status.json"
_OUTPUT_MD_REL = "pipeline_wiring_status.md"
_REGISTRY_REL = ("portfolio_automation", "artifact_registry.yaml")

_DISCLAIMER = (
    "Observe-only wiring audit. Crosses artifact freshness with static caller "
    "analysis to explain WHY a producer is stale (unwired / cadence_mismatch / "
    "silently_skipped). Heuristic: caller detection is a best-effort token grep, "
    "freshness is authoritative. Never blocks the decision core."
)

# Freshness windows per cadence (hours). A producer whose artifact is older than
# its window is "stale" and gets classified. on_demand artifacts have no cadence
# expectation and are not audited.
CADENCE_WINDOW_HOURS: dict[str, float] = {
    "daily": 30.0,        # ~1 day + cron slack
    "weekend": 192.0,     # ~8 days (runs Sat/Sun)
    "weekly": 192.0,      # ~8 days
    "monthly": 768.0,     # ~32 days
    "yearly": 8784.0,     # ~366 days
}

# Statuses that count as a wiring problem (drive AMBER + the summary).
_PROBLEM_STATUSES = frozenset(
    {"unwired", "cadence_mismatch", "silently_skipped", "fresh_but_empty"}
)

# Cadence-keys whose script names map to a cron wrapper. "core" is the daily
# code path reached via `python main.py --run-mode daily` (main.py + the
# orchestrator modules it calls); a token found there is wired for "daily".
_CORE_CADENCE = "daily"


# ── pure classification core (fully unit-testable) ───────────────────────────

def _producer_tokens(artifact_name: str, producer: str | None) -> set[str]:
    """Tokens to grep for in the script corpus to detect a caller.

    The registry `producer` field is sometimes a loose label (e.g. mislabeled
    'discovery_pulse' for the scraped-intel comparison), so we also grep the
    artifact basename. Either match counts as "found".
    """
    tokens: set[str] = set()
    if producer:
        tokens.add(producer.strip())
    base = artifact_name
    for suffix in (".json", ".jsonl", ".md", ".csv"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base:
        tokens.add(base)
    return {t for t in tokens if t}


def _caller_cadences(tokens: set[str], script_texts: dict[str, str]) -> list[str]:
    """Cadences whose script text contains any producer token.

    The 'core' corpus (main.py + orchestrator modules) is folded into the daily
    cadence, since those run via the daily cron.
    """
    found: set[str] = set()
    for cadence_key, text in script_texts.items():
        if not text:
            continue
        if any(tok in text for tok in tokens):
            found.add(_CORE_CADENCE if cadence_key == "core" else cadence_key)
    return sorted(found)


def classify_producers(
    registry: dict[str, dict[str, Any]],
    script_texts: dict[str, str],
    artifact_ages_hours: dict[str, float | None],
    *,
    content_flags: dict[str, bool] | None = None,
    config_gates: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Classify every registry producer. Pure function (no IO).

    Args:
        registry: {artifact_name: {producer, cadence, role, ...}}.
        script_texts: {cadence_key: text} for 'daily'/'weekly'/'monthly'/'core'
            (and any other cadence-keyed scripts). 'core' counts as daily.
        artifact_ages_hours: {artifact_name: age_hours or None if missing}.
        content_flags: optional {artifact_name: content_ok}. A fresh artifact
            whose flag is False is reported 'fresh_but_empty'.
        config_gates: optional {artifact_name: enabled}. A stale artifact whose
            gate is False is 'disabled' (expected), not 'silently_skipped'.

    Returns:
        {producers: [...], summary: {...}, overall_status: green|amber}.
    """
    content_flags = content_flags or {}
    config_gates = config_gates or {}

    producers: list[dict[str, Any]] = []
    for name, meta in registry.items():
        producer = (meta or {}).get("producer")
        cadence = (meta or {}).get("cadence")
        role = (meta or {}).get("role")
        if not producer:
            continue  # not a produced artifact (e.g. pure input/telemetry stub)

        # Append-only event logs (telemetry .jsonl) grow only when an event
        # fires; missing/stale is the expected idle state, not a wiring fault.
        if name.endswith(".jsonl") and role == "telemetry":
            producers.append({
                "artifact": name, "producer": producer, "cadence": cadence,
                "status": "event_log_idle", "caller_cadences": [], "age_hours": None,
            })
            continue

        window = CADENCE_WINDOW_HOURS.get(cadence)
        if window is None:
            # on_demand / unknown cadence → no freshness expectation.
            producers.append({
                "artifact": name, "producer": producer, "cadence": cadence,
                "status": "not_audited", "caller_cadences": [], "age_hours": None,
            })
            continue

        age = artifact_ages_hours.get(name)
        is_stale = age is None or age > window
        caller_cadences = _caller_cadences(_producer_tokens(name, producer), script_texts)

        if not is_stale:
            # Fresh → healthy, unless its content is degenerate.
            content_ok = content_flags.get(name, True)
            status = "healthy" if content_ok else "fresh_but_empty"
        elif config_gates.get(name) is False:
            status = "disabled"  # stale-by-design; intentionally turned off
        elif not caller_cadences:
            status = "unwired"
        elif cadence in caller_cadences:
            status = "silently_skipped"
        else:
            status = "cadence_mismatch"

        producers.append({
            "artifact": name, "producer": producer, "cadence": cadence,
            "status": status, "caller_cadences": caller_cadences,
            "age_hours": round(age, 1) if isinstance(age, (int, float)) else None,
        })

    summary = {
        "total_audited": sum(1 for p in producers if p["status"] != "not_audited"),
        "healthy": sum(1 for p in producers if p["status"] == "healthy"),
        "unwired": sum(1 for p in producers if p["status"] == "unwired"),
        "cadence_mismatch": sum(1 for p in producers if p["status"] == "cadence_mismatch"),
        "silently_skipped": sum(1 for p in producers if p["status"] == "silently_skipped"),
        "fresh_but_empty": sum(1 for p in producers if p["status"] == "fresh_but_empty"),
        "disabled": sum(1 for p in producers if p["status"] == "disabled"),
        "not_audited": sum(1 for p in producers if p["status"] == "not_audited"),
        "event_log_idle": sum(1 for p in producers if p["status"] == "event_log_idle"),
    }
    problems = sum(summary[k] for k in ("unwired", "cadence_mismatch",
                                        "silently_skipped", "fresh_but_empty"))
    overall_status = "amber" if problems else "green"
    return {"producers": producers, "summary": summary, "overall_status": overall_status}


# ── orchestrator: assemble real inputs, classify, write artifact ─────────────

# Per-producer content checks for the highest-value artifacts. Each maps an
# artifact name to a predicate over its loaded JSON payload → True if the
# content is non-degenerate. Catches "looks fresh but empty".
def _content_predicates() -> dict[str, Any]:
    def _narr(d: dict) -> bool:
        return bool((d.get("key_themes") or d.get("themes")) or d.get("themes_found"))

    def _evidence(d: dict) -> bool:
        return bool(d.get("data_available") or d.get("ticker_contexts")
                    or d.get("ticker_context_count"))

    def _quant(d: dict) -> bool:
        return (d.get("ledger_liveness") or {}).get("status") == "ok" or bool(d.get("active"))

    def _scraped(d: dict) -> bool:
        rows = d.get("comparison") or d.get("rows") or d.get("comparison_rows")
        return bool(rows) or int(d.get("symbols_total") or 0) > 0

    return {
        "market_narrative_daily.json": _narr,
        "market_narrative_weekly.json": _narr,
        "market_narrative_monthly.json": _narr,
        "news_evidence_layer.json": _evidence,
        "quant_watch_status.json": _quant,
        "scraped_intel_comparison.json": _scraped,
    }


# Config gates: artifacts whose production is intentionally gated by a config
# flag. (path-into-config.json, default_when_absent).
_CONFIG_GATES = {
    "scraped_intel_comparison.json": (("scraped_intel", "comparison_mode"), False),
    # Crowd Radar ships default-disabled; its persisted mention-history ledger
    # only materializes once crowd_radar.enabled=true (and a run yields posts).
    # Until then the artifact is absent + the static caller-grep can't attribute
    # a cadence, which would otherwise read as `unwired`. Gating it reclassifies
    # that to the non-AMBER `disabled` (stale-by-design). See daily-tool-analysis
    # Crowd Radar dispatch + docs/pipeline_wiring_probe.md.
    "crowd_mention_history.json": (("crowd_radar", "enabled"), False),
}


def _load_registry(root: Path) -> dict[str, dict[str, Any]]:
    import yaml

    path = root.joinpath(*_REGISTRY_REL)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("artifacts", {}) or {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _build_script_corpus(root: Path) -> dict[str, str]:
    """Map cadence-key → concatenated script text.

    'core' is the daily code path reached via main.py: main.py itself plus the
    orchestrator modules it calls that in turn invoke producers (so producers
    wired only inside the scanner are still detected as daily-wired).
    """
    daily = _read_text(root / "scripts" / "run_daily_safe.sh")
    weekly = _read_text(root / "scripts" / "run_weekly_safe.sh")
    monthly = _read_text(root / "scripts" / "monthly_check.sh")
    core = "\n".join(
        _read_text(root / p)
        for p in (
            "main.py",
            Path("watchlist_scanner") / "__main__.py",
            Path("theme_engine") / "__main__.py",
        )
    )
    return {"daily": daily, "weekly": weekly, "monthly": monthly, "core": core}


def _artifact_ages_hours(
    root: Path, registry: dict[str, dict[str, Any]], now: datetime
) -> dict[str, float | None]:
    ages: dict[str, float | None] = {}
    for name, meta in registry.items():
        rel = (meta or {}).get("path") or f"outputs/latest/{name}"
        p = root / rel
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            ages[name] = (now - mtime).total_seconds() / 3600.0
        except Exception:
            ages[name] = None  # missing
    return ages


def _content_flags(root: Path) -> dict[str, bool]:
    import json

    flags: dict[str, bool] = {}
    for name, pred in _content_predicates().items():
        p = root / "outputs" / "latest" / name
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            flags[name] = bool(pred(payload))
        except Exception:
            # Missing/unreadable is handled by the freshness signal, not content.
            continue
    return flags


def _config_gates(root: Path) -> dict[str, bool]:
    import json

    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    gates: dict[str, bool] = {}
    for name, (path_keys, default) in _CONFIG_GATES.items():
        node: Any = cfg
        for key in path_keys:
            node = node.get(key) if isinstance(node, dict) else None
        gates[name] = bool(node) if node is not None else bool(default)
    return gates


def render_pipeline_wiring_md(payload: dict[str, Any]) -> str:
    s = payload.get("summary", {})
    lines = [
        "# Pipeline Wiring Status",
        "",
        f"_{_DISCLAIMER}_",
        "",
        f"- Overall: **{payload.get('overall_status')}**",
        f"- Audited: {s.get('total_audited', 0)} producers "
        f"({s.get('healthy', 0)} healthy, {s.get('disabled', 0)} disabled, "
        f"{s.get('not_audited', 0)} not-audited on_demand)",
        f"- Problems: {s.get('unwired', 0)} unwired · "
        f"{s.get('cadence_mismatch', 0)} cadence-mismatch · "
        f"{s.get('silently_skipped', 0)} silently-skipped · "
        f"{s.get('fresh_but_empty', 0)} fresh-but-empty",
        "",
    ]
    problems = [p for p in payload.get("producers", []) if p["status"] in _PROBLEM_STATUSES]
    if problems:
        lines.append("## Flagged producers")
        lines.append("")
        lines.append("| artifact | producer | declared | status | caller cadences | age (h) |")
        lines.append("|---|---|---|---|---|---|")
        for p in problems:
            lines.append(
                f"| {p['artifact']} | {p['producer']} | {p['cadence']} | "
                f"**{p['status']}** | {', '.join(p['caller_cadences']) or '—'} | "
                f"{p['age_hours'] if p['age_hours'] is not None else '—'} |"
            )
    else:
        lines.append("All audited producers are healthy.")
    lines.append("")
    return "\n".join(lines)


def run_pipeline_wiring_probe(
    *,
    root: str | Path = ".",
    write_files: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    now = now or datetime.now(timezone.utc)
    try:
        registry = _load_registry(root_path)
        scripts = _build_script_corpus(root_path)
        ages = _artifact_ages_hours(root_path, registry, now)
        content = _content_flags(root_path)
        gates = _config_gates(root_path)

        classified = classify_producers(
            registry, scripts, ages, content_flags=content, config_gates=gates
        )
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "source": _SOURCE_LABEL,
            "generated_at": now.isoformat(),
            "observe_only": _OBSERVE_ONLY,
            "overall_status": classified["overall_status"],
            "summary": classified["summary"],
            "producers": classified["producers"],
            "disclaimer": _DISCLAIMER,
        }

        artifacts: dict[str, str] = {}
        if write_files:
            json_path = safe_write_json(
                OutputNamespace.LATEST, _OUTPUT_JSON_REL, payload,
                base_dir=root_path / "outputs",
            )
            md_path = safe_write_text(
                OutputNamespace.LATEST, _OUTPUT_MD_REL,
                render_pipeline_wiring_md(payload), base_dir=root_path / "outputs",
            )
            artifacts = {"pipeline_wiring_json": str(json_path),
                         "pipeline_wiring_md": str(md_path)}

        return {
            "observe_only": _OBSERVE_ONLY,
            "overall_status": payload["overall_status"],
            "summary": payload["summary"],
            "producers": payload["producers"],
            "artifacts": artifacts,
        }
    except Exception as exc:
        logger.error("pipeline_wiring_probe failed: %s", exc, exc_info=True)
        # Degrade to a valid amber payload — never crash, never red.
        return {
            "observe_only": _OBSERVE_ONLY,
            "overall_status": "amber",
            "summary": {},
            "producers": [],
            "error": str(exc),
        }


if __name__ == "__main__":
    r = run_pipeline_wiring_probe(root=Path(__file__).resolve().parents[1])
    s = r.get("summary", {})
    print(
        f"pipeline_wiring: {r.get('overall_status')} · "
        f"audited {s.get('total_audited', 0)} · "
        f"unwired {s.get('unwired', 0)} · mismatch {s.get('cadence_mismatch', 0)} · "
        f"skipped {s.get('silently_skipped', 0)} · empty {s.get('fresh_but_empty', 0)}"
    )
