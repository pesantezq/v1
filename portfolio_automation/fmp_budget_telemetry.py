"""
FMP / News Budget Telemetry — observe-only call-budget monitor.

The system has a daily FMP budget (config.json api_limits.fmp_daily_calls_budget).
The scanner can easily exhaust it, leaving downstream stages (news intel,
fundamentals refresh) with nothing. Until this module existed, the only
way to spot that was to grep logs after the fact.

This producer takes one daily snapshot of:

  1. FMP daily call counter state (today's count vs budget, headroom).
  2. News intelligence fetch outcome (article_count_raw, packet_count).
  3. Discovery enrichment outcome (enriched_count from sandbox status).
  4. Cache hit/miss approximation (size of fmp_cache directory + recency).

It appends a row to `data/fmp_budget_history.jsonl` so the operator can
spot trends over time (budget exhaustion, news fetch dropping to zero,
cache disk usage drift), and writes `outputs/latest/fmp_budget_status.json`
+ `.md`. A single status line is also surfaced in the daily memo.

Hard guarantees:
  - observe_only=True hardcoded.
  - No FMP API calls of its own (read-only of counters and cache).
  - No mutation of decision/score/allocation/recommendation state.

Public API:
  read_budget_state(root) -> dict
  read_news_outcome(root) -> dict
  read_cache_stats(root) -> dict
  build_fmp_budget_status(root) -> dict
  append_to_history(payload, root) -> None
  run_fmp_budget_telemetry(root, write_files) -> dict
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.fmp_budget_telemetry")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "fmp_budget_telemetry"
_OBSERVE_ONLY = True

_DISCLAIMER = (
    "Observe-only FMP budget + news-fetch telemetry. Reads counters; never "
    "calls the API or mutates portfolio state."
)

_CALL_COUNTER_REL = ("data", "fmp_cache", "call_counter.json")
_CACHE_DIR_REL    = ("data", "fmp_cache")
_NEWS_INTEL_REL   = ("outputs", "latest", "news_intelligence.json")
_DISCOVERY_NEWS_REL = ("outputs", "sandbox", "discovery", "news_enriched_candidates.json")
_CONFIG_REL       = ("config.json",)
_HISTORY_REL      = ("data", "fmp_budget_history.jsonl")


def _load_json_safe(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.debug("fmp_budget: failed to load %s — %s", path, exc)
        return None


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------


def read_budget_state(root: Path) -> dict[str, Any]:
    """Return today's FMP call count + budget + headroom."""
    counter_path = root.joinpath(*_CALL_COUNTER_REL)
    if not counter_path.exists():
        return {"available": False, "reason": "no_counter"}
    counter = _load_json_safe(counter_path) or {}
    cfg = _load_json_safe(root.joinpath(*_CONFIG_REL)) or {}
    limits = (cfg.get("api_limits") or {}) if isinstance(cfg, dict) else {}

    if not isinstance(counter, dict):
        return {"available": False, "reason": "no_counter"}
    # Distinguish an explicit 0 (= "no daily cap", the operator's uncapped
    # convention) from an absent key (genuinely unconfigured). Coalescing both
    # to None misreported uncapped-0 as no_budget_configured.
    if "fmp_daily_calls_budget" not in limits:
        return {"available": False, "reason": "no_budget_configured"}
    budget = _safe_int(limits.get("fmp_daily_calls_budget"))

    count_today = _safe_int(counter.get("count"))

    if budget <= 0:
        # Uncapped: FMPClient.would_exceed treats budget <= 0 as no daily cap.
        # Report an available, ok, uncapped state (status "ok" keeps the daily
        # check's budget gate GREEN, which is correct — there is no cap to hit).
        return {
            "available": True,
            "date": counter.get("date"),
            "count_today": count_today,
            "budget": 0,
            "headroom": None,
            "pct_used": None,
            "status": "ok",
            "uncapped": True,
        }

    headroom = max(0, budget - count_today)
    pct_used = round(count_today / budget, 4)
    status = "ok"
    if count_today >= budget:
        status = "exhausted"
    elif count_today >= int(budget * 0.90):
        status = "near_cap"
    return {
        "available": True,
        "date": counter.get("date"),
        "count_today": count_today,
        "budget": budget,
        "headroom": headroom,
        "pct_used": pct_used,
        "status": status,
        "uncapped": False,
    }


def read_news_outcome(root: Path) -> dict[str, Any]:
    """Return the latest news_intelligence fetch outcome counts."""
    ni = _load_json_safe(root.joinpath(*_NEWS_INTEL_REL))
    if not isinstance(ni, dict):
        return {"available": False, "reason": "no_news_intelligence_artifact"}
    return {
        "available": True,
        "generated_at": ni.get("generated_at"),
        "article_count_raw": _safe_int(ni.get("article_count_raw")),
        "article_count_deduped": _safe_int(ni.get("article_count_deduped")),
        "evidence_packet_count": _safe_int(ni.get("evidence_packet_count")),
        "official_monitoring_count": _safe_int(ni.get("official_monitoring_count")),
        "sandbox_count": _safe_int(ni.get("sandbox_count")),
    }


def read_discovery_outcome(root: Path) -> dict[str, Any]:
    """Return the discovery news enrichment outcome (candidates joined to news)."""
    payload = _load_json_safe(root.joinpath(*_DISCOVERY_NEWS_REL))
    if not isinstance(payload, dict):
        return {"available": False, "reason": "no_discovery_news_artifact"}
    return {
        "available": True,
        "generated_at": payload.get("generated_at"),
        "enriched_count": _safe_int(payload.get("enriched_count")),
        "with_news_count": _safe_int(payload.get("with_news_count")),
        "candidate_count": _safe_int(payload.get("candidate_count")),
    }


def read_cache_stats(root: Path) -> dict[str, Any]:
    """Return disk-usage stats for the FMP cache directory."""
    cache_dir = root.joinpath(*_CACHE_DIR_REL)
    if not cache_dir.exists() or not cache_dir.is_dir():
        return {"available": False, "reason": "no_cache_dir"}
    try:
        files = [p for p in cache_dir.iterdir() if p.is_file()]
        total_size = sum(p.stat().st_size for p in files)
        newest = max((p.stat().st_mtime for p in files), default=0.0)
        oldest = min((p.stat().st_mtime for p in files), default=0.0)
        return {
            "available": True,
            "file_count": len(files),
            "total_size_bytes": total_size,
            "total_size_kb": round(total_size / 1024, 1),
            "newest_mtime": datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()
                            if newest else None,
            "oldest_mtime": datetime.fromtimestamp(oldest, tz=timezone.utc).isoformat()
                            if oldest else None,
        }
    except Exception as exc:
        logger.debug("fmp_budget: cache stats failed — %s", exc)
        return {"available": False, "reason": "stat_error"}


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------


def build_fmp_budget_status(
    *,
    root: str | Path = ".",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return the full status artifact (no file writes)."""
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    root_path = Path(root).resolve()

    budget = read_budget_state(root_path)
    news = read_news_outcome(root_path)
    discovery = read_discovery_outcome(root_path)
    cache = read_cache_stats(root_path)

    overall_status = "ok"
    if budget.get("available"):
        bs = budget.get("status")
        if bs == "exhausted":
            overall_status = "exhausted"
        elif bs == "near_cap":
            overall_status = "near_cap"
    if news.get("available") and news.get("article_count_raw", 0) == 0 and overall_status == "ok":
        # Fresh budget but zero articles — could be FMP outage or empty universe.
        overall_status = "news_empty"

    return {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "overall_status": overall_status,
        "budget": budget,
        "news": news,
        "discovery": discovery,
        "cache": cache,
        "disclaimer": _DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def append_to_history(
    payload: dict[str, Any],
    *,
    root: str | Path = ".",
) -> bool:
    """
    Append a compact row to data/fmp_budget_history.jsonl. Deduplicated by
    (date, count_today, article_count_raw) so re-runs within a day don't
    spam the ledger.
    """
    root_path = Path(root).resolve()
    history_path = root_path.joinpath(*_HISTORY_REL)
    history_path.parent.mkdir(parents=True, exist_ok=True)

    budget = payload.get("budget") or {}
    news = payload.get("news") or {}
    discovery = payload.get("discovery") or {}

    dedup_key = (
        budget.get("date"),
        budget.get("count_today"),
        news.get("article_count_raw"),
        discovery.get("enriched_count"),
    )

    # Read the last row to compare its dedup key.
    last_key: tuple | None = None
    if history_path.exists():
        try:
            with history_path.open("r", encoding="utf-8") as f:
                last_line = ""
                for line in f:
                    line = line.strip()
                    if line:
                        last_line = line
                if last_line:
                    last = json.loads(last_line)
                    last_key = (
                        last.get("date"),
                        last.get("count_today"),
                        last.get("article_count_raw"),
                        last.get("enriched_count"),
                    )
        except Exception as exc:
            logger.debug("fmp_budget: history tail read failed — %s", exc)

    if last_key == dedup_key and last_key != (None, None, None, None):
        return False

    row = {
        "ts": payload.get("generated_at"),
        "date": budget.get("date"),
        "count_today": budget.get("count_today"),
        "budget": budget.get("budget"),
        "headroom": budget.get("headroom"),
        "budget_status": budget.get("status"),
        "article_count_raw": news.get("article_count_raw"),
        "evidence_packet_count": news.get("evidence_packet_count"),
        "enriched_count": discovery.get("enriched_count"),
        "overall_status": payload.get("overall_status"),
    }
    try:
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return True
    except Exception as exc:
        logger.warning("fmp_budget: history append failed — %s", exc)
        return False


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_fmp_budget_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a(f"# FMP Budget & News Telemetry — {payload.get('generated_at', '')[:10]}")
    a("")
    a(f"**Generated:** {payload.get('generated_at', '')}  ")
    a(f"**Overall status:** {payload.get('overall_status', 'ok')}")
    a("")
    a(f"> {payload.get('disclaimer', _DISCLAIMER)}")
    a("")

    budget = payload.get("budget") or {}
    if budget.get("available"):
        a("## FMP Daily Call Budget")
        a(
            f"- **{budget.get('count_today')}/{budget.get('budget')}** calls used "
            f"({(budget.get('pct_used') or 0):.1%}) — "
            f"headroom **{budget.get('headroom')}** — status: `{budget.get('status')}`"
        )
        a(f"- Date: `{budget.get('date')}`")
        a("")
    else:
        a("## FMP Daily Call Budget")
        a(f"_Not available: {budget.get('reason', 'unknown')}._")
        a("")

    news = payload.get("news") or {}
    if news.get("available"):
        a("## News Intelligence Fetch")
        a(
            f"- Raw articles: **{news.get('article_count_raw')}**  "
            f"deduped: **{news.get('article_count_deduped')}**  "
            f"packets: **{news.get('evidence_packet_count')}**"
        )
        a(
            f"- Lanes — official monitoring: {news.get('official_monitoring_count')}, "
            f"sandbox: {news.get('sandbox_count')}"
        )
        a(f"- Last produced: `{news.get('generated_at')}`")
        a("")
    else:
        a("## News Intelligence Fetch")
        a(f"_Not available: {news.get('reason', 'unknown')}._")
        a("")

    discovery = payload.get("discovery") or {}
    if discovery.get("available"):
        a("## Discovery News Enrichment")
        a(
            f"- Enriched candidates: **{discovery.get('enriched_count')}** "
            f"(with news: {discovery.get('with_news_count')}, "
            f"total candidates: {discovery.get('candidate_count')})"
        )
        a("")

    cache = payload.get("cache") or {}
    if cache.get("available"):
        a("## FMP Cache")
        a(
            f"- {cache.get('file_count')} cache files — "
            f"{cache.get('total_size_kb'):.1f} KB"
        )
        a(f"- Newest: `{cache.get('newest_mtime')}`")
        a("")

    a("---")
    a("_Observe-only telemetry._")
    return "\n".join(lines)


def build_memo_line(payload: dict[str, Any]) -> str:
    """One-line summary suitable for the daily memo's Advisor Stack section."""
    budget = payload.get("budget") or {}
    news = payload.get("news") or {}
    if not budget.get("available"):
        return "FMP budget telemetry: status unknown."
    bits = [
        f"FMP budget {budget.get('count_today')}/{budget.get('budget')} "
        f"({budget.get('status')})"
    ]
    if news.get("available"):
        bits.append(
            f"news {news.get('article_count_raw')} articles → "
            f"{news.get('evidence_packet_count')} packets"
        )
    return " · ".join(bits)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def run_fmp_budget_telemetry(
    *,
    root: str | Path = ".",
    write_files: bool = True,
) -> dict[str, Any]:
    """Compose payload, append history, write artifacts."""
    root_path = Path(root).resolve()
    try:
        payload = build_fmp_budget_status(root=root_path)
        appended = append_to_history(payload, root=root_path)
        payload["history_row_appended"] = appended

        artifacts: dict[str, str] = {}
        if write_files:
            md = render_fmp_budget_md(payload)
            json_path = safe_write_json(
                OutputNamespace.LATEST,
                "fmp_budget_status.json",
                payload,
                base_dir=root_path / "outputs",
            )
            md_path = safe_write_text(
                OutputNamespace.LATEST,
                "fmp_budget_status.md",
                md,
                base_dir=root_path / "outputs",
            )
            artifacts = {
                "fmp_budget_json": str(json_path),
                "fmp_budget_md": str(md_path),
            }

        return {
            "status": "ok",
            "overall_status": payload.get("overall_status"),
            "memo_line": build_memo_line(payload),
            "artifacts": artifacts,
        }
    except Exception as exc:
        logger.error("fmp_budget_telemetry failed: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}


if __name__ == "__main__":
    import sys
    r = run_fmp_budget_telemetry(root=Path(__file__).resolve().parents[1])
    print(f"fmp_budget: status={r.get('status')} overall={r.get('overall_status')}")
    print(f"  memo_line: {r.get('memo_line')}")
    sys.exit(0)
