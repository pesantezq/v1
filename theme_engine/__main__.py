"""
Theme Engine CLI.

Usage:
    py -m theme_engine --mode daily|weekly|monthly [--config config.json]
                       [--dry-run] [--root .]

Run modes:
    daily   - collect RSS, detect themes, write theme_signals.json + watch_candidates.json
    weekly  - same as daily, plus compute 7-day theme persistence
    monthly - same as weekly; persistence gate active for scanner boost

All modes are safe to run without Ollama when theme_engine.testing_mode = true
(or STOCKBOT_TESTING=1 env var is set).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from degraded_mode import build_data_health_context
from agent.llm_adapters import (
    resolve_ollama_base_url,
    resolve_provider,
    resolve_task_provider,
)

logger = logging.getLogger(__name__)


def _configure_stdio_utf8() -> None:
    """Avoid cp1252 write failures on Windows consoles during CLI runs."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _load_sp500_from_cache(root: str) -> list[str]:
    """Load S&P 500 symbols from the FMP disk cache (no API call)."""
    cache_path = Path(root) / "data" / "fmp_cache" / "sp500_constituents.json"
    if not cache_path.exists():
        return []
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        constituents = data.get("data", [])
        return sorted({c["symbol"] for c in constituents if c.get("symbol")})
    except Exception as exc:
        logger.warning("Could not load SP500 cache: %s", exc)
        return []


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2) + "\n"
    tmp_path = path.with_name(
        f"{path.stem}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    )
    try:
        tmp_path.write_text(rendered, encoding="utf-8")

        # On Windows, a just-written temp file can remain transiently locked.
        for attempt in range(3):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 2:
                    break
                time.sleep(0.1 * (attempt + 1))
        # Some Windows setups allow direct overwrite of an in-use file but
        # reject rename-overwrite semantics. Fall back to an explicit write.
        path.write_text(rendered, encoding="utf-8")
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _current_timestamp() -> str:
    return datetime.now().isoformat()


def _build_run_id(prefix: str, mode: str) -> str:
    return f"{prefix}-{mode}-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}"


def _git_commit_hash(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        commit = result.stdout.strip()
        return commit or None
    except Exception:
        return None


def _log_llm_summary(metadata: dict[str, Any]) -> None:
    logger.info(
        "Theme engine LLM summary: task=%s resolved=%s actual=%s model=%s fallback=%s",
        metadata.get("task", "(unknown)"),
        metadata.get("resolved_provider", "(unknown)"),
        metadata.get("actual_provider", "(unknown)"),
        metadata.get("actual_model") or metadata.get("model") or "(unset)",
        "yes" if metadata.get("fallback_triggered") else "no",
    )


def _resolve_theme_task_context(
    *,
    mode: str,
    config: Any,
    provider_override: str | None = None,
) -> dict[str, Any]:
    """Resolve provider/model/base URL for a theme-engine task."""
    te_cfg: dict[str, Any] = (
        config.theme_engine if hasattr(config, "theme_engine") else config
    )
    task_providers = te_cfg.get("task_providers", {}) if isinstance(te_cfg.get("task_providers"), dict) else {}
    task_name = f"theme_engine.{mode}"
    provider_preference = resolve_task_provider(
        cli_provider=provider_override,
        task_provider=task_providers.get(mode),
        fallback_task_provider=te_cfg.get("llm_provider"),
    )
    provider = provider_preference or resolve_provider(None, default="ollama")
    fallback_chain = [provider]
    if provider == "anthropic":
        model = (
            os.environ.get("ANTHROPIC_MODEL")
            or te_cfg.get("anthropic_model", "claude-haiku-4-5-20251001")
        )
        base_url = "(n/a)"
    elif provider == "openai":
        model = os.environ.get("OPENAI_MODEL") or te_cfg.get("openai_model", "")
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    else:
        model = os.environ.get("OLLAMA_MODEL") or te_cfg.get("ollama_model", "gemma3:4b")
        try:
            base_url = resolve_ollama_base_url(
                os.environ.get("OLLAMA_BASE_URL") or te_cfg.get("ollama_base_url")
            )
        except Exception as exc:
            base_url = f"<invalid: {exc}>"
    return {
        "task_name": task_name,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "fallback_chain": fallback_chain,
        "theme_config": te_cfg,
    }


def run(
    mode: str,
    config: Any,  # utils.Config instance or dict-like with .theme_engine
    dry_run: bool = False,
    root: str = ".",
    provider_override: str | None = None,
) -> dict[str, Any]:
    """Execute the theme engine pipeline.

    Args:
        mode:    'daily' | 'weekly' | 'monthly'
        config:  Config object (or any object with .theme_engine dict attribute)
        dry_run: If True, skip writing files.
        root:    Repository root directory.

    Returns:
        dict with keys 'themes' and 'watch_candidates'.
    """
    context = _resolve_theme_task_context(
        mode=mode,
        config=config,
        provider_override=provider_override,
    )
    root_path = Path(root)
    run_id = _build_run_id("theme-engine", mode)
    run_started_at = _current_timestamp()
    git_commit = _git_commit_hash(root_path)
    te_cfg = context["theme_config"]
    feeds: list[str] = te_cfg.get("rss_feeds", [])
    max_items: int = int(te_cfg.get("max_items_per_run", 30))
    provider: str = context["provider"]
    fallback_chain = context["fallback_chain"]
    llm_model = context["model"]
    ollama_base_url: str | None = te_cfg.get("ollama_base_url")
    ollama_api_key: str | None = te_cfg.get("ollama_api_key")
    output_dir: str = te_cfg.get("output_dir", "outputs/latest")
    testing_mode: bool = bool(te_cfg.get("testing_mode", False))
    min_confidence: float = float(te_cfg.get("min_confidence", 0.6))

    # Resolve paths relative to root
    cache_path = str(root_path / "data" / "rss_seen.json")
    db_path = str(root_path / "data" / "portfolio.db")
    catalog_path = str(root_path / "data" / "themes_catalog.json")
    resolved_output = str(root_path / output_dir)

    # ── Step 1: Collect RSS headlines ────────────────────────────────────────
    from theme_engine.rss_collector import RSSCollector
    collector = RSSCollector(feeds=feeds, max_items=max_items, cache_path=cache_path)
    headlines = collector.collect()
    logger.info("Theme engine: collected %d headlines", len(headlines))

    # ── Step 2: Detect themes ─────────────────────────────────────────────────
    from theme_engine.theme_detector import ThemeDetector
    detector = ThemeDetector(
        model=llm_model,
        provider=provider,
        base_url=ollama_base_url,
        api_key=ollama_api_key,
        testing_mode=testing_mode,
    )
    logger.info(
        "Theme engine startup: task=%s provider=%s model=%s base_url=%s fallback_chain=%s",
        context["task_name"],
        provider,
        llm_model or "(unset)",
        context["base_url"],
        " -> ".join(fallback_chain),
    )
    task_started_at = _current_timestamp()
    t0 = time.monotonic()
    success = True
    error_type: str | None = None
    try:
        raw_themes = detector.detect(headlines)
    except Exception as exc:
        success = False
        error_type = type(exc).__name__
        raw_themes = []
        raise
    finally:
        task_completed_at = _current_timestamp()
        latency_ms = int((time.monotonic() - t0) * 1000)
    logger.info("Theme engine: detected %d raw themes", len(raw_themes))
    data_health = build_data_health_context(
        extra_sources=["rss", "sp500_cache"],
    )
    llm_metadata = {
        "run_id": run_id,
        "git_commit": git_commit,
        "task": context["task_name"],
        "resolved_provider": provider,
        "actual_provider": provider,
        "model": llm_model or "(unset)",
        "actual_model": llm_model or "(unset)",
        "base_url": context["base_url"],
        "actual_base_url": context["base_url"],
        "started_at": task_started_at,
        "completed_at": task_completed_at,
        "latency_ms": latency_ms,
        "success": success,
        "error_type": error_type,
        "fallback_reason": None,
        "fallback_chain": fallback_chain,
        "fallback_triggered": False,
        "llm_fallback_triggered": False,
        "data_fallback_triggered": data_health["data_fallback_triggered"],
        "degraded_mode": data_health["degraded_mode"],
        "degraded_reason": data_health["degraded_reason"],
        "data_sources_used": data_health["data_sources_used"],
        "data_mode": data_health["data_mode"],
        "data_latency_ms": data_health["data_latency_ms"],
        "fallback_depth": data_health["fallback_depth"],
        "degraded_confidence_penalty": data_health["degraded_confidence_penalty"],
        "output_file": "outputs/latest/theme_signals.json",
    }
    _log_llm_summary(llm_metadata)

    # ── Step 3: Load S&P 500 symbol list (from cache, no API call) ───────────
    sp500_symbols = _load_sp500_from_cache(root)

    # ── Step 4: Map themes to tickers ─────────────────────────────────────────
    from theme_engine.theme_mapper import ThemeMapper
    mapper = ThemeMapper(catalog_path=catalog_path, sp500_symbols=sp500_symbols)
    enriched_themes, watch_candidates = mapper.map_themes(raw_themes)

    # ── Step 5: Compute persistence (weekly / monthly) ────────────────────────
    from theme_engine.theme_store import ThemeStore
    store = ThemeStore(db_path=db_path, output_dir=resolved_output)

    if mode in ("weekly", "monthly"):
        recent = store.get_recent_signals(days=7)
        seen_days: dict[str, set[str]] = {}
        for row in recent:
            seen_days.setdefault(row["theme_name"], set()).add(row["run_date"])
        for theme in enriched_themes:
            name = theme.get("name", "")
            persistence = len(seen_days.get(name, set()))
            theme["persistence_7d"] = persistence
    else:
        for theme in enriched_themes:
            theme["persistence_7d"] = 0

    # Filter to themes meeting min_confidence (for audit; all themes are saved to DB)
    confident_themes = [t for t in enriched_themes if t.get("confidence", 0) >= min_confidence]
    logger.info(
        "Theme engine: %d themes meet min_confidence=%.2f",
        len(confident_themes),
        min_confidence,
    )

    # ── Step 6: Save signals and watch candidates ─────────────────────────────
    if not dry_run:
        store.save_signals(enriched_themes, watch_candidates, metadata=data_health)
        _write_json_atomic(
            Path(resolved_output) / "theme_engine_llm_metadata.json",
            {
                "generated_at": _current_timestamp(),
                "run_id": run_id,
                "started_at": run_started_at,
                "completed_at": _current_timestamp(),
                "git_commit": git_commit,
                "degraded_mode": data_health["degraded_mode"],
                "degraded_reason": data_health["degraded_reason"],
                "data_sources_used": data_health["data_sources_used"],
                "data_mode": data_health["data_mode"],
                "llm_metadata": llm_metadata,
            },
        )
    else:
        logger.info("Theme engine: dry-run — skipping file writes")

    return {
        "themes": enriched_themes,
        "watch_candidates": watch_candidates,
        "llm_metadata": llm_metadata,
    }


def main() -> None:
    """CLI entry point for `py -m theme_engine`."""
    _configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        prog="theme_engine",
        description="Stock Bot Theme Engine — RSS + Ollama theme detection",
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "monthly"],
        default="daily",
        help="Run mode (default: daily)",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config.json or config/ directory (default: config.json)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional structured config profile name",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip file writes (still detects themes and prints results)",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root directory (default: current directory)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Optional provider override for this run (ollama | anthropic | openai)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # Load config
    config_path = Path(args.root) / args.config
    try:
        sys.path.insert(0, str(Path(args.root)))
        from utils import load_config, load_env  # type: ignore[import]
        load_env(str(Path(args.root) / ".env"))
        config = load_config(str(config_path), profile=args.profile)
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        sys.exit(1)

    if not config.theme_engine_enabled and not args.dry_run:
        # When testing_mode is set, allow run even if enabled=false
        if not config.theme_engine.get("testing_mode"):
            print("Theme engine is disabled in config (theme_engine.enabled=false).")
            print("Set theme_engine.enabled=true or pass --dry-run to test.")
            sys.exit(0)

    result = run(
        mode=args.mode,
        config=config,
        dry_run=args.dry_run,
        root=args.root,
        provider_override=args.provider,
    )

    print(f"\nTheme Engine run complete ({args.mode} mode)")
    print(f"  Themes detected: {len(result['themes'])}")
    print(f"  Watch candidates: {len(result['watch_candidates'])}")
    for t in result["themes"]:
        conf_pct = int(t.get("confidence", 0) * 100)
        persist = t.get("persistence_7d", 0)
        tickers = ", ".join(t.get("tickers", [])[:5])
        print(
            f"  [{conf_pct}% conf | {persist}d persist] {t['name']}"
            + (f" → {tickers}" if tickers else "")
        )

    if not args.dry_run:
        out = Path(args.root) / config.theme_engine.get("output_dir", "outputs/latest")
        print(f"\nOutput files written to: {out}/")
        print("  theme_signals.json")
        print("  watch_candidates.json")


if __name__ == "__main__":
    main()
