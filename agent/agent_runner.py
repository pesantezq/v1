"""
agent/agent_runner.py — AI Agent runner for the portfolio automation system.

This is the main entry point for the Hybrid 3 AI agent layer.  It sits
downstream of the deterministic engine (main.py) and generates human-readable
memos, escalation packets, and (when approved) maintainer patches.

CLI:
    py -m agent.agent_runner --mode daily|weekly|monthly
    py -m agent --mode daily

Optional flags:
    --no-network             Force offline mode — no LLM calls, templated memo
    --openai-model <name>    Override OpenAI model (default: env OPENAI_MODEL)
    --claude-model <name>    Override Claude model (default: env ANTHROPIC_MODEL or claude-haiku-4-5-20251001)

Offline trigger:
    --no-network flag OR env STOCKBOT_TESTING=1

LLM routing:
    daily/weekly:  OpenAI → (fallback Claude) → (fallback offline stub)
    monthly:       OpenAI → (fallback Claude) → (fallback offline stub)
    maintainer:    OpenAI → (fallback Claude) (gated by approved_actions.json)

Safety contract:
    - Never places trades
    - Never calls FMP
    - Never modifies config.json or any investment-logic module
    - Never prints API key values
    - All output files are inside the repo (outputs/latest/ or root)
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from degraded_mode import build_data_health_context, summarize_data_health
from agent.bundle_builder import build_bundle
from agent.io_utils import read_json_safe, redact, tail_latest_log, write_markdown_atomic
from agent.llm_adapters import (
    call_provider,
    resolve_provider,
    resolve_task_provider,
)
from agent.prompts import build_daily_weekly_prompt, build_maintainer_prompt, build_monthly_prompt
from agent.repo_tree import get_repo_tree

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent.resolve()
_DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_OPENAI_MODEL = ""

logger = logging.getLogger("stockbot.agent.runner")


def _configure_stdio_utf8() -> None:
    """Avoid cp1252 write failures on Windows consoles during CLI runs."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    _configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        prog="agent",
        description="StockBot AI Agent — generate memos and patches from engine outputs",
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "monthly"],
        default="daily",
        help="Run mode (default: daily)",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        default=False,
        help="Offline mode — skip LLM calls, write templated memos",
    )
    parser.add_argument(
        "--openai-model",
        default=None,
        help="OpenAI model name (overrides OPENAI_MODEL env var)",
    )
    parser.add_argument(
        "--claude-model",
        default=None,
        help="Claude model name (overrides ANTHROPIC_MODEL env var)",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Repo root directory (default: auto-detected)",
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
        "--provider",
        choices=["openai", "anthropic"],
        default=None,
        help="Optional provider override for this run",
    )
    args = parser.parse_args()

    # Configure basic logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = Path(args.root).resolve() if args.root else ROOT
    from utils import load_config_dict, load_env

    load_env(str(root / ".env"))
    offline = args.no_network or os.environ.get("STOCKBOT_TESTING", "").strip() == "1"
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    try:
        runtime_config = load_config_dict(str(config_path), profile=args.profile, record_history=False)
    except Exception as exc:
        logger.warning("Agent config load failed (non-fatal): %s", redact(str(exc)))
        runtime_config = {}
    agent_config = runtime_config.get("agent", {}) if isinstance(runtime_config, dict) else {}
    provider = args.provider or os.environ.get("STOCKBOT_LLM_PROVIDER", "").strip() or None

    claude_model = (
        args.claude_model
        or os.environ.get("ANTHROPIC_MODEL", "")
        or _DEFAULT_CLAUDE_MODEL
    )
    openai_model = (
        (args.openai_model or "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()
        or _DEFAULT_OPENAI_MODEL
    )

    logger.info(
        "Agent runner | mode=%s offline=%s provider=%s config=%s profile=%s openai_model=%s claude_model=%s",
        args.mode,
        offline,
        provider or "auto",
        config_path,
        args.profile or "(default)",
        openai_model or "(unset)",
        claude_model,
    )

    try:
        run(
            mode=args.mode,
            offline=offline,
            provider=provider,
            claude_model=claude_model,
            openai_model=openai_model,
            root=root,
            agent_config=agent_config,
        )
    except Exception as exc:
        logger.error("Agent runner failed: %s", redact(str(exc)))
        sys.exit(1)


def run(
    mode: str,
    offline: bool = False,
    provider: str | None = None,
    claude_model: str = _DEFAULT_CLAUDE_MODEL,
    openai_model: str = _DEFAULT_OPENAI_MODEL,
    root: Path = ROOT,
    agent_config: dict[str, Any] | None = None,
) -> dict:
    """
    Execute the AI agent pipeline.

    Args:
        mode:         "daily", "weekly", or "monthly".
        offline:      If True, skip all LLM calls and write templated memos.
        provider:     Optional provider preference (openai | anthropic).
        claude_model: Anthropic Claude model ID to use.
        openai_model: OpenAI model ID to use when provider=openai.
        root:         Repository root directory.

    Returns:
        dict with keys: mode, files_written, offline, errors
    """
    root = Path(root).resolve()
    today = datetime.now().strftime("%Y-%m-%d")
    run_started_at = _current_timestamp()
    run_id = _build_run_id("agent", mode)
    git_commit = _git_commit_hash(root)
    out_dir = root / "outputs" / "latest"
    out_dir.mkdir(parents=True, exist_ok=True)
    data_context = _load_data_context(root)
    agent_cfg = agent_config if isinstance(agent_config, dict) else {}
    task_providers = (
        agent_cfg.get("task_providers", {})
        if isinstance(agent_cfg.get("task_providers"), dict)
        else {}
    )

    files_written: list[str] = []
    errors: list[str] = []
    llm_metadata_records: list[dict[str, Any]] = []
    logger.info("Agent data context: %s", summarize_data_health(data_context))
    memo_prefix = _build_data_mode_header(data_context)

    # ------------------------------------------------------------------
    # Step 1: Build (or load) agent bundle
    # ------------------------------------------------------------------
    logger.info("Step 1/5: Building agent bundle...")
    bundle = build_bundle(mode=mode, root=root)
    watchlist_signal_summary = (
        bundle.get("watchlist_signal_summary")
        if isinstance(bundle.get("watchlist_signal_summary"), dict)
        else {}
    )
    bundle["data_health"] = {
        "data_mode": data_context.get("data_mode", "live"),
        "degraded_mode": bool(data_context.get("degraded_mode", False)),
        "degraded_reason": data_context.get("degraded_reason"),
        "data_sources_used": list(data_context.get("data_sources_used", ["live"])),
        "data_fallback_triggered": bool(data_context.get("data_fallback_triggered", False)),
        "degraded_confidence_penalty": data_context.get("degraded_confidence_penalty", 0.0),
        "suppressed_signals": int(watchlist_signal_summary.get("suppressed_signals_count", 0) or 0),
        "cooldown_hits": int(watchlist_signal_summary.get("cooldown_hits", 0) or 0),
    }
    _write_json_atomic(out_dir / "agent_bundle.json", bundle)
    bundle_str = json.dumps(bundle, indent=2, default=str)
    files_written.append("outputs/latest/agent_bundle.json")

    # ------------------------------------------------------------------
    # Step 2: Tail latest log
    # ------------------------------------------------------------------
    logger.info("Step 2/5: Reading engine log tail...")
    log_tail = tail_latest_log(root / "logs", n=80)

    # ------------------------------------------------------------------
    # Step 3: Daily / Weekly — decision_memo.md + optional escalation
    # ------------------------------------------------------------------
    if mode in ("daily", "weekly"):
        resolved_provider = _resolve_agent_task_provider(
            task_name=mode,
            cli_provider=provider,
            task_providers=task_providers,
        )
        startup_metadata = _log_task_startup(
            task_name=f"agent.{mode}",
            resolved_provider=resolved_provider,
            fallback_chain=_provider_chain(
                mode=mode,
                preferred_provider=resolved_provider,
                openai_model=openai_model,
            ),
            claude_model=claude_model,
            openai_model=openai_model,
            run_id=run_id,
            git_commit=git_commit,
        )
        logger.info("Step 3/5: Generating %s decision memo...", mode)
        memo, memo_metadata = _generate_daily_weekly_memo(
            bundle=bundle,
            bundle_str=bundle_str,
            log_tail=log_tail,
            mode=mode,
            today=today,
            offline=offline,
            provider=resolved_provider,
            claude_model=claude_model,
            openai_model=openai_model,
            errors=errors,
        )
        memo_path = out_dir / "decision_memo.md"
        write_markdown_atomic(memo_path, memo_prefix + memo)
        files_written.append("outputs/latest/decision_memo.md")
        logger.info("Written: decision_memo.md")
        memo_metadata.update(startup_metadata)
        memo_metadata = _augment_with_data_context(memo_metadata, data_context)
        memo_metadata = _augment_with_watchlist_summary(memo_metadata, bundle)
        memo_metadata = _augment_with_market_regime(memo_metadata, bundle)
        memo_metadata = _augment_with_policy_recommendation(memo_metadata, bundle)
        memo_metadata["output_file"] = "outputs/latest/decision_memo.md"
        llm_metadata_records.append(memo_metadata)
        _log_llm_summary("Agent", memo_metadata)

        # Escalation check
        if _needs_escalation(bundle):
            logger.info("Escalation triggered — writing escalation_packet.md")
            escalation = memo_prefix + _build_escalation_packet(bundle, today)
            esc_path = out_dir / "escalation_packet.md"
            write_markdown_atomic(esc_path, escalation)
            files_written.append("outputs/latest/escalation_packet.md")
            logger.info("Written: escalation_packet.md")

    # ------------------------------------------------------------------
    # Step 4: Monthly — monthly_memo.md + email_draft.md
    # ------------------------------------------------------------------
    elif mode == "monthly":
        resolved_provider = _resolve_agent_task_provider(
            task_name="monthly",
            cli_provider=provider,
            task_providers=task_providers,
        )
        startup_metadata = _log_task_startup(
            task_name="agent.monthly",
            resolved_provider=resolved_provider,
            fallback_chain=_provider_chain(
                mode="monthly",
                preferred_provider=resolved_provider,
                openai_model=openai_model,
            ),
            claude_model=claude_model,
            openai_model=openai_model,
            run_id=run_id,
            git_commit=git_commit,
        )
        logger.info("Step 3/5: Generating monthly memo...")
        memo, memo_metadata = _generate_monthly_memo(
            bundle=bundle,
            bundle_str=bundle_str,
            log_tail=log_tail,
            today=today,
            offline=offline,
            provider=resolved_provider,
            claude_model=claude_model,
            openai_model=openai_model,
            errors=errors,
        )
        memo_path = out_dir / "monthly_memo.md"
        write_markdown_atomic(memo_path, memo_prefix + memo)
        files_written.append("outputs/latest/monthly_memo.md")
        logger.info("Written: monthly_memo.md")
        memo_metadata.update(startup_metadata)
        memo_metadata = _augment_with_data_context(memo_metadata, data_context)
        memo_metadata = _augment_with_watchlist_summary(memo_metadata, bundle)
        memo_metadata = _augment_with_market_regime(memo_metadata, bundle)
        memo_metadata = _augment_with_policy_recommendation(memo_metadata, bundle)
        memo_metadata["output_file"] = "outputs/latest/monthly_memo.md"
        llm_metadata_records.append(memo_metadata)
        _log_llm_summary("Agent", memo_metadata)

        # Email draft
        if bundle.get("should_email"):
            email_content = _build_email_draft(bundle, memo_prefix + memo, today)
        else:
            email_content = (
                f"NO_EMAIL\n\n"
                f"Reason: {bundle.get('email_reason', 'email.enabled=false')}\n"
                f"Generated: {today}\n"
            )
        email_path = out_dir / "email_draft.md"
        write_markdown_atomic(email_path, email_content)
        files_written.append("outputs/latest/email_draft.md")
        logger.info("Written: email_draft.md")

    # ------------------------------------------------------------------
    # Step 5: Maintainer gate — only if approved_actions.json exists
    # ------------------------------------------------------------------
    logger.info("Step 4/5: Checking maintainer gate...")
    approval_path = root / "approved_actions.json"
    if approval_path.exists():
        maintainer_provider = _resolve_agent_task_provider(
            task_name="maintainer",
            cli_provider=provider,
            task_providers=task_providers,
        )
        startup_metadata = _log_task_startup(
            task_name="agent.maintainer",
            resolved_provider=maintainer_provider,
            fallback_chain=_provider_chain(
                mode="monthly",
                preferred_provider=maintainer_provider,
                openai_model=openai_model,
            ),
            claude_model=claude_model,
            openai_model=openai_model,
            run_id=run_id,
            git_commit=git_commit,
        )
        logger.info("approved_actions.json found — running maintainer agent...")
        maintainer_metadata = _run_maintainer(
            approval_path=approval_path,
            root=root,
            out_dir=out_dir,
            provider=maintainer_provider,
            claude_model=claude_model,
            openai_model=openai_model,
            offline=offline,
            files_written=files_written,
            errors=errors,
        )
        maintainer_metadata.update(startup_metadata)
        maintainer_metadata = _augment_with_data_context(maintainer_metadata, data_context)
        maintainer_metadata = _augment_with_watchlist_summary(maintainer_metadata, bundle)
        maintainer_metadata = _augment_with_market_regime(maintainer_metadata, bundle)
        maintainer_metadata = _augment_with_policy_recommendation(maintainer_metadata, bundle)
        maintainer_metadata["output_file"] = "outputs/latest/maintainer_patch.diff"
        llm_metadata_records.append(maintainer_metadata)
        _log_llm_summary("Agent", maintainer_metadata)
    else:
        note_path = out_dir / "maintainer_patch.diff"
        _write_blocked_note(note_path)
        files_written.append("outputs/latest/maintainer_patch.diff")
        logger.info("Maintainer gate: approval not found, patch blocked")

    logger.info("Step 5/5: Done. Files written: %s", files_written)
    if llm_metadata_records:
        metadata_path = out_dir / "agent_llm_metadata.json"
        regime = bundle.get("market_regime") if isinstance(bundle.get("market_regime"), dict) else {}
        recommendation_payload = bundle.get("policy_recommendation") if isinstance(bundle.get("policy_recommendation"), dict) else {}
        recommendation = recommendation_payload.get("recommendation") if isinstance(recommendation_payload.get("recommendation"), dict) else {}
        _write_json_atomic(
            metadata_path,
            {
                "generated_at": _current_timestamp(),
                "run_id": run_id,
                "started_at": run_started_at,
                "completed_at": _current_timestamp(),
                "git_commit": git_commit,
                "mode": mode,
                "degraded_mode": data_context.get("degraded_mode", False),
                "degraded_reason": data_context.get("degraded_reason"),
                "data_sources_used": data_context.get("data_sources_used", []),
                "data_mode": data_context.get("data_mode", "live"),
                "suppressed_signals": int(watchlist_signal_summary.get("suppressed_signals_count", 0) or 0),
                "cooldown_hits": int(watchlist_signal_summary.get("cooldown_hits", 0) or 0),
                "regime_label": regime.get("regime_label", "neutral"),
                "regime_confidence": regime.get("regime_confidence", 0.0),
                "regime_inputs": dict(regime.get("regime_inputs") or {}),
                "regime_data_quality": regime.get("regime_data_quality", "limited"),
                "recommended_policy": recommendation.get("recommended_policy"),
                "recommended_profile": recommendation.get("recommended_profile"),
                "recommendation_confidence": recommendation.get("recommendation_confidence"),
                "recommendation_reasoning": list(recommendation.get("recommendation_reasoning") or []),
                "recommendation_inputs": dict(recommendation.get("recommendation_inputs") or {}),
                "recommendation_data_quality": recommendation.get("recommendation_data_quality"),
                "recommendation_source": recommendation.get("recommendation_source"),
                "tasks": llm_metadata_records,
            },
        )
        files_written.append("outputs/latest/agent_llm_metadata.json")
        logger.info("Written: agent_llm_metadata.json")

    return {
        "mode": mode,
        "files_written": files_written,
        "offline": offline,
        "errors": errors,
        "today": today,
        "llm_metadata": llm_metadata_records,
    }


# ---------------------------------------------------------------------------
# Internal: daily/weekly memo generation
# ---------------------------------------------------------------------------


def _provider_chain(
    *,
    mode: str,
    preferred_provider: str | None,
    openai_model: str,
) -> list[str]:
    """Return provider fallback order: OpenAI primary, Anthropic fallback."""
    disable_fallback = os.environ.get("STOCKBOT_DISABLE_LLM_FALLBACK", "").strip() == "1"
    default_chain = ["openai", "anthropic"]
    chain: list[str] = []

    if preferred_provider:
        chain.append(resolve_provider(preferred_provider, default=preferred_provider))
    if disable_fallback:
        if chain:
            return chain
        return [default_chain[0]]
    for provider in default_chain:
        if provider not in chain:
            chain.append(provider)
    return chain


def _resolve_agent_task_provider(
    *,
    task_name: str,
    cli_provider: str | None,
    task_providers: dict[str, Any],
) -> str | None:
    """Resolve task-specific provider preference without changing default fallback order."""
    return resolve_task_provider(
        cli_provider=cli_provider,
        task_provider=task_providers.get(task_name),
        fallback_task_provider=task_providers.get("standalone"),
    )


def _log_task_startup(
    *,
    task_name: str,
    resolved_provider: str | None,
    fallback_chain: list[str],
    claude_model: str,
    openai_model: str,
    run_id: str,
    git_commit: str | None,
) -> dict[str, Any]:
    selected_provider = fallback_chain[0] if fallback_chain else resolved_provider or "offline"
    model = _model_for_provider(
        selected_provider,
        claude_model=claude_model,
        openai_model=openai_model,
    )
    base_url = _base_url_for_provider(selected_provider)
    logger.info(
        "Agent task startup: task=%s resolved_provider=%s model=%s base_url=%s fallback_chain=%s",
        task_name,
        selected_provider,
        model or "(unset)",
        base_url,
        " -> ".join(fallback_chain) if fallback_chain else "(offline)",
    )
    return {
        "run_id": run_id,
        "git_commit": git_commit,
        "task": task_name,
        "resolved_provider": selected_provider,
        "model": model or "(unset)",
        "base_url": base_url,
        "fallback_chain": fallback_chain,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically so scheduled runs do not leave partial metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as tmp:
        json.dump(payload, tmp, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _base_url_for_provider(provider: str) -> str:
    if provider == "openai":
        return os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    return "(n/a)"


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


def _format_fallback_reason(failure_details: list[dict[str, str]]) -> str | None:
    if not failure_details:
        return None
    first = failure_details[0]
    return f"{first['provider']} failed: {first['message']}"


def _log_llm_summary(label: str, metadata: dict[str, Any]) -> None:
    logger.info(
        "%s LLM summary: task=%s resolved=%s actual=%s model=%s llm_fallback=%s data_fallback=%s",
        label,
        metadata.get("task", "(unknown)"),
        metadata.get("resolved_provider", "(unknown)"),
        metadata.get("actual_provider", "(unknown)"),
        metadata.get("actual_model") or metadata.get("model") or "(unset)",
        "yes" if metadata.get("fallback_triggered") else "no",
        "yes" if metadata.get("data_fallback_triggered") else "no",
    )


def _load_data_context(root: Path) -> dict[str, Any]:
    summary = read_json_safe(root / "outputs" / "latest" / "scraped_intel_run_summary.json") or {}
    if not isinstance(summary, dict):
        summary = {}
    if summary:
        scanner = summary.get("scanner") or {}
        context = build_data_health_context(
            fmp_attempted=bool(scanner.get("fmp_attempted", False)),
            fmp_succeeded=bool(scanner.get("fmp_succeeded", False)),
            fmp_error=scanner.get("fmp_error"),
            fallback_used=bool(
                scanner.get("data_fallback_triggered")
                or scanner.get("fallback_used")
            ),
            watchlist_source=str(scanner.get("watchlist_source", "none")),
            data_latency_ms=scanner.get("data_latency_ms"),
        )
        if summary.get("data_sources_used"):
            context["data_sources_used"] = list(summary.get("data_sources_used", []))
            if summary.get("data_mode"):
                context["data_mode"] = summary.get("data_mode", context["data_mode"])
            context["fallback_depth"] = scanner.get("fallback_depth", context["fallback_depth"])
            context["degraded_confidence_penalty"] = scanner.get(
                "degraded_confidence_penalty",
                context["degraded_confidence_penalty"],
            )
        return context
    return build_data_health_context(extra_sources=["engine_outputs"])


def _augment_with_data_context(metadata: dict[str, Any], data_context: dict[str, Any]) -> dict[str, Any]:
    augmented = dict(metadata)
    augmented["llm_fallback_triggered"] = bool(metadata.get("fallback_triggered", False))
    augmented["data_fallback_triggered"] = bool(data_context.get("data_fallback_triggered", False))
    augmented["degraded_mode"] = bool(data_context.get("degraded_mode", False))
    augmented["degraded_reason"] = data_context.get("degraded_reason")
    augmented["data_sources_used"] = list(data_context.get("data_sources_used", []))
    augmented["data_mode"] = data_context.get("data_mode", "live")
    augmented["data_latency_ms"] = data_context.get("data_latency_ms")
    augmented["fallback_depth"] = data_context.get("fallback_depth", 0)
    augmented["degraded_confidence_penalty"] = data_context.get("degraded_confidence_penalty", 0.0)
    return augmented


def _augment_with_watchlist_summary(metadata: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    augmented = dict(metadata)
    summary = bundle.get("watchlist_signal_summary")
    if not isinstance(summary, dict):
        augmented.setdefault("suppressed_signals", 0)
        augmented.setdefault("cooldown_hits", 0)
        return augmented
    augmented["suppressed_signals"] = int(summary.get("suppressed_signals_count", 0) or 0)
    augmented["cooldown_hits"] = int(summary.get("cooldown_hits", 0) or 0)
    return augmented


def _augment_with_market_regime(metadata: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    augmented = dict(metadata)
    regime = bundle.get("market_regime")
    if not isinstance(regime, dict):
        return augmented
    augmented["regime_label"] = regime.get("regime_label", "neutral")
    augmented["regime_confidence"] = regime.get("regime_confidence", 0.0)
    augmented["regime_inputs"] = dict(regime.get("regime_inputs") or {})
    augmented["regime_data_quality"] = regime.get("regime_data_quality", "limited")
    return augmented


def _augment_with_policy_recommendation(metadata: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    augmented = dict(metadata)
    recommendation_payload = bundle.get("policy_recommendation")
    if not isinstance(recommendation_payload, dict):
        return augmented

    recommendation = recommendation_payload.get("recommendation")
    if not isinstance(recommendation, dict):
        return augmented

    augmented["recommended_policy"] = recommendation.get("recommended_policy")
    augmented["recommended_profile"] = recommendation.get("recommended_profile")
    augmented["recommendation_confidence"] = recommendation.get("recommendation_confidence")
    augmented["recommendation_reasoning"] = list(recommendation.get("recommendation_reasoning") or [])
    augmented["recommendation_inputs"] = dict(recommendation.get("recommendation_inputs") or {})
    augmented["recommendation_data_quality"] = recommendation.get("recommendation_data_quality")
    augmented["recommendation_source"] = recommendation.get("recommendation_source")
    return augmented


def _render_strategy_recommendation_section(bundle: dict[str, Any]) -> str:
    recommendation_payload = bundle.get("policy_recommendation")
    if not isinstance(recommendation_payload, dict):
        return ""

    recommendation = recommendation_payload.get("recommendation")
    if not isinstance(recommendation, dict):
        return ""

    alternatives = list(((recommendation_payload.get("alternatives") or {}).get("policies")) or [])
    alternative_names = [str(item.get("name") or "") for item in alternatives if item.get("name")]
    reasoning = list(recommendation.get("recommendation_reasoning") or [])
    why_line = " ".join(reasoning[:2]) if reasoning else "No recommendation reasoning available."
    quality_note = recommendation.get("recommendation_quality_note")
    note_line = f"- Note: {quality_note}\n" if quality_note else ""

    return (
        f"## Strategy Recommendation\n"
        f"- Recommended profile: {recommendation.get('recommended_profile') or 'n/a'}\n"
        f"- Recommended policy: {recommendation.get('recommended_policy') or 'n/a'}\n"
        f"- Why: {why_line}\n"
        f"- Confidence: {float(recommendation.get('recommendation_confidence') or 0.0):.2f}\n"
        f"- Alternatives: {', '.join(alternative_names[:3]) if alternative_names else 'n/a'}\n"
        f"- Source: {recommendation.get('recommendation_source') or 'rule_based_fallback'}\n"
        f"{note_line}\n"
    )


def _build_data_mode_header(data_context: dict[str, Any]) -> str:
    data_mode = data_context.get("data_mode", "live")
    if not data_context.get("degraded_mode"):
        return (
            f"> Data mode: `{data_mode}`\n"
            f"> Data sources: {', '.join(data_context.get('data_sources_used', ['live']))}\n\n"
        )
    reason = data_context.get("degraded_reason") or "unknown"
    penalty = data_context.get("degraded_confidence_penalty", 0.0)
    return (
        "[DEGRADED DATA MODE] Operating in degraded data mode - decisions are lower confidence\n\n"
        f"> Data mode: `{data_mode}`\n"
        f"> Reason: `{reason}`\n"
        f"> Sources: {', '.join(data_context.get('data_sources_used', ['fallback']))}\n"
        f"> Confidence penalty (informational): -{penalty:.2f}\n\n"
    )


def _model_for_provider(
    provider: str,
    *,
    claude_model: str,
    openai_model: str,
) -> str:
    if provider == "anthropic":
        return claude_model
    return openai_model or os.environ.get("OPENAI_MODEL", "").strip()


def _call_provider_for_prompt(
    *,
    provider: str,
    prompt: str,
    max_tokens: int,
    claude_model: str,
    openai_model: str,
) -> tuple[str, str]:
    model = _model_for_provider(
        provider,
        claude_model=claude_model,
        openai_model=openai_model,
    )
    if provider == "openai" and not model:
        raise RuntimeError(
            "OPENAI_MODEL is not set. Set OPENAI_MODEL before using STOCKBOT_LLM_PROVIDER=openai."
        )
    text = call_provider(
        provider=provider,
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
    )
    return text, model


def _generate_daily_weekly_memo(
    bundle: dict,
    bundle_str: str,
    log_tail: str,
    mode: str,
    today: str,
    offline: bool,
    provider: str | None,
    claude_model: str,
    openai_model: str,
    errors: list,
) -> tuple[str, dict[str, Any]]:
    started_at = _current_timestamp()
    t0 = time.monotonic()
    if offline:
        return _offline_stub_memo(bundle, mode, today), {
            "task": f"agent.{mode}",
            "actual_provider": "offline_stub",
            "actual_model": "(offline)",
            "actual_base_url": "(n/a)",
            "started_at": started_at,
            "completed_at": _current_timestamp(),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "success": False,
            "error_type": None,
            "fallback_reason": None,
            "fallback_triggered": False,
        }

    prompt = build_daily_weekly_prompt(
        agent_bundle_json=bundle_str,
        log_tail=log_tail,
        mode=mode,
        today=today,
    )

    chain = _provider_chain(
        mode=mode,
        preferred_provider=provider,
        openai_model=openai_model,
    )
    failure_details: list[dict[str, str]] = []
    for candidate in chain:
        try:
            text, model_used = _call_provider_for_prompt(
                provider=candidate,
                prompt=prompt,
                max_tokens=1200,
                claude_model=claude_model,
                openai_model=openai_model,
            )
            logger.info("Daily/weekly memo via %s (%s)", candidate, model_used)
            return text, {
                "task": f"agent.{mode}",
                "actual_provider": candidate,
                "actual_model": model_used,
                "actual_base_url": _base_url_for_provider(candidate),
                "started_at": started_at,
                "completed_at": _current_timestamp(),
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "success": True,
                "error_type": failure_details[0]["error_type"] if failure_details else None,
                "fallback_reason": _format_fallback_reason(failure_details),
                "fallback_triggered": candidate != chain[0],
            }
        except Exception as exc:
            err = redact(str(exc))
            logger.warning("%s failed: %s - trying next fallback", candidate, err)
            errors.append(f"{candidate}_failed: {err}")
            failure_details.append(
                {
                    "provider": candidate,
                    "error_type": type(exc).__name__,
                    "message": err,
                }
            )

    return _offline_stub_memo(bundle, mode, today), {
        "task": f"agent.{mode}",
        "actual_provider": "offline_stub",
        "actual_model": "(offline)",
        "actual_base_url": "(n/a)",
        "started_at": started_at,
        "completed_at": _current_timestamp(),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "success": False,
        "error_type": failure_details[-1]["error_type"] if failure_details else None,
        "fallback_reason": _format_fallback_reason(failure_details),
        "fallback_triggered": True,
    }


# ---------------------------------------------------------------------------
# Internal: monthly memo generation
# ---------------------------------------------------------------------------

def _generate_monthly_memo(
    bundle: dict,
    bundle_str: str,
    log_tail: str,
    today: str,
    offline: bool,
    provider: str | None,
    claude_model: str,
    openai_model: str,
    errors: list,
) -> tuple[str, dict[str, Any]]:
    started_at = _current_timestamp()
    t0 = time.monotonic()
    if offline:
        return _offline_monthly_stub(bundle, today), {
            "task": "agent.monthly",
            "actual_provider": "offline_stub",
            "actual_model": "(offline)",
            "actual_base_url": "(n/a)",
            "started_at": started_at,
            "completed_at": _current_timestamp(),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "success": False,
            "error_type": None,
            "fallback_reason": None,
            "fallback_triggered": False,
        }

    prompt = build_monthly_prompt(
        agent_bundle_json=bundle_str,
        log_tail=log_tail,
        today=today,
    )

    chain = _provider_chain(
        mode="monthly",
        preferred_provider=provider,
        openai_model=openai_model,
    )
    failure_details: list[dict[str, str]] = []
    for candidate in chain:
        try:
            text, model_used = _call_provider_for_prompt(
                provider=candidate,
                prompt=prompt,
                max_tokens=2000,
                claude_model=claude_model,
                openai_model=openai_model,
            )
            logger.info("Monthly memo via %s (%s)", candidate, model_used)
            return text, {
                "task": "agent.monthly",
                "actual_provider": candidate,
                "actual_model": model_used,
                "actual_base_url": _base_url_for_provider(candidate),
                "started_at": started_at,
                "completed_at": _current_timestamp(),
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "success": True,
                "error_type": failure_details[0]["error_type"] if failure_details else None,
                "fallback_reason": _format_fallback_reason(failure_details),
                "fallback_triggered": candidate != chain[0],
            }
        except Exception as exc:
            err = redact(str(exc))
            logger.warning("%s monthly failed: %s - trying next fallback", candidate, err)
            errors.append(f"{candidate}_monthly_failed: {err}")
            failure_details.append(
                {
                    "provider": candidate,
                    "error_type": type(exc).__name__,
                    "message": err,
                }
            )

    return _offline_monthly_stub(bundle, today), {
        "task": "agent.monthly",
        "actual_provider": "offline_stub",
        "actual_model": "(offline)",
        "actual_base_url": "(n/a)",
        "started_at": started_at,
        "completed_at": _current_timestamp(),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "success": False,
        "error_type": failure_details[-1]["error_type"] if failure_details else None,
        "fallback_reason": _format_fallback_reason(failure_details),
        "fallback_triggered": True,
    }


# ---------------------------------------------------------------------------
# Internal: maintainer agent
# ---------------------------------------------------------------------------

def _run_maintainer(
    approval_path: Path,
    root: Path,
    out_dir: Path,
    provider: str | None,
    claude_model: str,
    openai_model: str,
    offline: bool,
    files_written: list,
    errors: list,
) -> dict[str, Any]:
    started_at = _current_timestamp()
    t0 = time.monotonic()
    approved_json_str = ""
    try:
        approved_json_str = approval_path.read_text(encoding="utf-8")
        # Validate parseable
        json.loads(approved_json_str)
    except Exception as exc:
        err = f"approved_actions.json unreadable: {redact(str(exc))}"
        logger.error(err)
        errors.append(err)
        _write_blocked_note(out_dir / "maintainer_patch.diff", reason=err)
        files_written.append("outputs/latest/maintainer_patch.diff")
        return {
            "task": "agent.maintainer",
            "actual_provider": "blocked",
            "actual_model": "(blocked)",
            "actual_base_url": "(n/a)",
            "started_at": started_at,
            "completed_at": _current_timestamp(),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "success": False,
            "error_type": type(exc).__name__,
            "fallback_reason": err,
            "fallback_triggered": False,
        }

    repo_tree_str = get_repo_tree(root, max_depth=3)

    # Collect relevant code snippets (the files mentioned in approved_actions)
    snippets = _collect_snippets(root, approved_json_str)

    if offline:
        note = (
            "# Maintainer Patch — OFFLINE MODE\n\n"
            "Patch generation requires an enabled LLM provider. "
            "Remove --no-network and configure STOCKBOT_LLM_PROVIDER plus matching credentials if needed.\n"
        )
        write_markdown_atomic(out_dir / "maintainer_patch.diff", note)
        write_markdown_atomic(out_dir / "maintainer_plan.md", note)
        files_written += [
            "outputs/latest/maintainer_patch.diff",
            "outputs/latest/maintainer_plan.md",
        ]
        return {
            "task": "agent.maintainer",
            "actual_provider": "offline_stub",
            "actual_model": "(offline)",
            "actual_base_url": "(n/a)",
            "started_at": started_at,
            "completed_at": _current_timestamp(),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "success": False,
            "error_type": None,
            "fallback_reason": None,
            "fallback_triggered": False,
        }

    prompt = build_maintainer_prompt(
        approved_actions_json=approved_json_str,
        repo_tree=repo_tree_str,
        snippets=snippets,
    )

    response = ""
    provider_errors: list[str] = []
    failure_details: list[dict[str, str]] = []
    chain = _provider_chain(
        mode="monthly",
        preferred_provider=provider,
        openai_model=openai_model,
    )
    for candidate in chain:
        try:
            response, model_used = _call_provider_for_prompt(
                provider=candidate,
                prompt=prompt,
                max_tokens=3000,
                claude_model=claude_model,
                openai_model=openai_model,
            )
            logger.info("Maintainer patch via %s (%s)", candidate, model_used)
            break
        except Exception as exc:
            err = redact(str(exc))
            logger.error("Maintainer %s call failed: %s", candidate, err)
            provider_errors.append(f"{candidate}: {err}")
            errors.append(f"maintainer_{candidate}_failed: {err}")
            failure_details.append(
                {
                    "provider": candidate,
                    "error_type": type(exc).__name__,
                    "message": err,
                }
            )
    if not response:
        reason = "; ".join(provider_errors) or "no provider produced a maintainer patch"
        _write_blocked_note(out_dir / "maintainer_patch.diff", reason=reason)
        files_written.append("outputs/latest/maintainer_patch.diff")
        return {
            "task": "agent.maintainer",
            "actual_provider": "blocked",
            "actual_model": "(blocked)",
            "actual_base_url": "(n/a)",
            "started_at": started_at,
            "completed_at": _current_timestamp(),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "success": False,
            "error_type": failure_details[-1]["error_type"] if failure_details else None,
            "fallback_reason": _format_fallback_reason(failure_details),
            "fallback_triggered": True,
        }

    # Split response: diff comes first, then the plan (separated by a heading)
    diff_text, plan_text = _split_maintainer_response(response)

    write_markdown_atomic(out_dir / "maintainer_patch.diff", diff_text)
    write_markdown_atomic(out_dir / "maintainer_plan.md", plan_text)
    files_written += [
        "outputs/latest/maintainer_patch.diff",
        "outputs/latest/maintainer_plan.md",
    ]
    logger.info("Maintainer patch and plan written")
    return {
        "task": "agent.maintainer",
        "actual_provider": candidate,
        "actual_model": model_used,
        "actual_base_url": _base_url_for_provider(candidate),
        "started_at": started_at,
        "completed_at": _current_timestamp(),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "success": True,
        "error_type": failure_details[0]["error_type"] if failure_details else None,
        "fallback_reason": _format_fallback_reason(failure_details),
        "fallback_triggered": candidate != chain[0],
    }


def _collect_snippets(root: Path, approved_json_str: str) -> str:
    """
    Extract short snippets from files mentioned in approved_actions.json.
    Limits each snippet to 100 lines to keep the prompt manageable.
    """
    try:
        data = json.loads(approved_json_str)
        actions = data.get("actions", [])
    except Exception:
        return "(could not parse approved_actions.json)"

    snippets: list[str] = []
    seen_files: set[str] = set()

    for action in actions:
        file_path: str = action.get("file", "")
        if not file_path or file_path in seen_files:
            continue
        seen_files.add(file_path)
        full_path = root / file_path
        if not full_path.exists():
            snippets.append(f"# {file_path} — FILE NOT FOUND\n")
            continue
        try:
            lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
            # Find relevant window (lines around the specified line_range if given)
            line_range = action.get("line_range")
            if line_range and isinstance(line_range, list) and len(line_range) == 2:
                start = max(0, int(line_range[0]) - 5)
                end = min(len(lines), int(line_range[1]) + 5)
                excerpt = lines[start:end]
                header = f"# {file_path} (lines {start+1}–{end})\n"
            else:
                excerpt = lines[:100]
                header = f"# {file_path} (first {len(excerpt)} lines)\n"
            snippets.append(header + "\n".join(excerpt))
        except Exception as exc:
            snippets.append(f"# {file_path} — read error: {exc}\n")

    return "\n\n".join(snippets) if snippets else "(no relevant snippets found)"


def _split_maintainer_response(response: str) -> tuple[str, str]:
    """
    Split the Claude maintainer response into diff and plan sections.

    Heuristic: look for a markdown heading that starts the plan section
    (e.g. "## IMPLEMENTATION PLAN" or "# Implementation Plan").
    """
    lower = response.lower()
    for marker in ["## implementation plan", "# implementation plan", "## plan"]:
        idx = lower.find(marker)
        if idx > 0:
            return response[:idx].rstrip(), response[idx:]
    # If no plan section found, treat entire response as diff
    return response, "(no separate plan section in response)"


def _write_blocked_note(path: Path, reason: str = "approved_actions.json not found") -> None:
    content = (
        f"# Maintainer Patch — BLOCKED\n\n"
        f"Patch generation is blocked.\n\n"
        f"**Reason:** {reason}\n\n"
        f"To unblock: create `approved_actions.json` in the repo root with an "
        f"`actions` list describing the changes to implement.\n"
    )
    write_markdown_atomic(path, content)


# ---------------------------------------------------------------------------
# Internal: escalation
# ---------------------------------------------------------------------------

def _needs_escalation(bundle: dict) -> bool:
    """Return True if the bundle indicates an escalation is needed."""
    guardrails = bundle.get("guardrails", {})
    if not guardrails.get("pass", True):
        return True
    drawdown = bundle.get("drawdown", {})
    if float(drawdown.get("drawdown_pct", 0.0)) > 0.15:
        return True
    return False


def _build_escalation_packet(bundle: dict, today: str) -> str:
    guardrails = bundle.get("guardrails", {})
    violations = guardrails.get("violations", [])
    drawdown = bundle.get("drawdown", {})
    drawdown_pct = float(drawdown.get("drawdown_pct", 0.0))

    lines = [
        f"# Escalation Packet — {today}",
        "",
        "## Trigger Summary",
        "",
    ]

    if violations:
        lines.append(f"**{len(violations)} guardrail violation(s) detected:**")
        for v in violations:
            lines.append(f"- {v.get('rule', '?')}: {json.dumps(v)}")
    if drawdown_pct > 0.15:
        lines.append(f"- Drawdown {drawdown_pct:.1%} exceeds 15% escalation threshold")

    lines += [
        "",
        "## Recommended Actions",
        "",
    ]
    for v in violations:
        rule = v.get("rule", "")
        if rule == "concentration_cap":
            sym = v.get("symbol", "?")
            excess = v.get("excess", 0)
            lines.append(
                f"1. **{sym} concentration violation** (excess: {excess:.1%}): "
                f"Direct next 2+ monthly contributions entirely to non-{sym} positions."
            )
        elif rule == "leverage_cap":
            exp = v.get("actual_effective_exposure", 0)
            cap = v.get("cap", 0.15)
            lines.append(
                f"2. **Leverage cap violation** (effective: {exp:.1%} > {cap:.1%}): "
                f"No new leveraged purchases; dilute via non-leveraged contributions."
            )
        elif rule == "anti_panic_sleeve_block":
            lines.append(
                "3. **Anti-panic gate active**: No new speculative sleeve positions until "
                "drawdown recovers below 20%."
            )

    if drawdown_pct > 0.15:
        lines.append(
            f"- **Drawdown alert ({drawdown_pct:.1%})**: Review drawdown_state.json. "
            "Consider increasing equity tilt per growth_mode.drawdown_thresholds."
        )

    lines += [
        "",
        "---",
        f"*Generated by AI agent — {today}*",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal: email draft
# ---------------------------------------------------------------------------

def _build_email_draft(bundle: dict, memo_text: str, today: str) -> str:
    portfolio_value = bundle.get("portfolio_value", 0.0)
    drawdown_pct = bundle.get("drawdown", {}).get("drawdown_pct", 0.0)
    subject = (
        f"[StockBot] Monthly Portfolio Memo — {today} "
        f"(${portfolio_value:,.0f}, drawdown {drawdown_pct:.1%})"
        if isinstance(portfolio_value, (int, float)) and isinstance(drawdown_pct, (int, float))
        else f"[StockBot] Monthly Portfolio Memo — {today}"
    )
    return (
        f"Subject: {subject}\n\n"
        f"---\n\n"
        f"{memo_text}\n\n"
        f"---\n"
        f"*Generated by StockBot AI Agent — {today}*\n"
    )


# ---------------------------------------------------------------------------
# Internal: offline stubs
# ---------------------------------------------------------------------------

def _offline_stub_memo(bundle: dict, mode: str, today: str) -> str:
    """Deterministic templated memo — no LLM required."""
    drawdown = bundle.get("drawdown", {})
    value = bundle.get("portfolio_value", drawdown.get("current_value", "unknown"))
    ath = drawdown.get("all_time_high", "unknown")
    drawdown_pct = drawdown.get("drawdown_pct", 0.0)
    regime = bundle.get("drawdown_regime", "unknown")
    guardrails = bundle.get("guardrails", {})
    violations = guardrails.get("violations", [])
    cfg = bundle.get("config", {})
    contribution = cfg.get("monthly_contribution", 0)
    cagr = bundle.get("expected_cagr", 0.0)
    plan = bundle.get("contribution_plan", [])

    value_str = f"${value:,.2f}" if isinstance(value, (int, float)) else str(value)
    ath_str = f"${ath:,.2f}" if isinstance(ath, (int, float)) else str(ath)
    dd_str = f"{drawdown_pct:.1%}" if isinstance(drawdown_pct, float) else str(drawdown_pct)

    guardrail_str = "No violations." if not violations else "\n".join(
        f"- {v.get('rule', '?')}: {v.get('symbol', '')} "
        f"{v.get('actual_weight', v.get('actual_effective_exposure', ''))} "
        f"> {v.get('cap', '')}"
        for v in violations
    )

    plan_str = "\n".join(
        f"- {p['symbol']}: ${p.get('dollars', 0):,.0f} ({p.get('reason', '')})"
        for p in plan[:4]
    ) if plan else "Run engine first to get contribution plan."
    watchlist_signal_summary = bundle.get("watchlist_signal_summary") or {}
    high_confidence = watchlist_signal_summary.get("high_confidence_signals") or []
    high_conviction = watchlist_signal_summary.get("high_conviction_candidates") or []
    starter_sized = watchlist_signal_summary.get("starter_sized_ideas") or []
    deferred = watchlist_signal_summary.get("deferred_signals") or []
    suppressed = watchlist_signal_summary.get("suppressed_signals") or []
    portfolio_view = bundle.get("portfolio_construction_view") or {}
    market_regime = bundle.get("market_regime") or {}
    performance_summary = bundle.get("signal_performance_summary") or {}
    regime_performance_summary = bundle.get("regime_performance_summary") or {}
    policy_simulation_summary = bundle.get("policy_simulation_summary") or {}
    strong_tickers = performance_summary.get("historically_strong_tickers") or []
    low_reliability = performance_summary.get("low_reliability_tickers") or []
    strategy_recommendation_section = _render_strategy_recommendation_section(bundle)
    conviction_summary_line = watchlist_signal_summary.get("conviction_summary_line") or "Conviction summary unavailable."
    portfolio_summary_line = portfolio_view.get("summary_line") or "Portfolio construction view unavailable."
    portfolio_label = portfolio_view.get("summary_label") or "balanced"
    portfolio_warnings = portfolio_view.get("warnings") or []
    regime_summary_line = market_regime.get("regime_summary_line") or "Market regime unavailable."
    regime_fit = market_regime.get("regime_portfolio_fit") or "unknown"
    regime_commentary = market_regime.get("regime_portfolio_commentary") or "No regime commentary available."
    regime_performance = (regime_performance_summary.get("by_regime") or {}).get(
        market_regime.get("regime_label", "neutral"),
        {},
    ) if isinstance(regime_performance_summary, dict) else {}
    simulated_policies = list(policy_simulation_summary.get("policies") or []) if isinstance(policy_simulation_summary, dict) else []
    simulation_comparison = dict(policy_simulation_summary.get("comparison") or {}) if isinstance(policy_simulation_summary, dict) else {}
    top_allocations = portfolio_view.get("high_allocation_candidates") or []
    capped_candidates = portfolio_view.get("capped_candidates") or []
    signal_lines = "\n".join(
        f"- {item.get('ticker', '?')}: effective {float(item.get('effective_score') or 0.0):.2f} "
        f"(signal {float(item.get('signal_score') or 0.0):.2f}, confidence {float(item.get('confidence_score') or 0.0):.2f})"
        for item in high_confidence[:3]
    ) if high_confidence else "- None surfaced above the confidence-aware action bar."
    suppressed_lines = "\n".join(
        f"- {item.get('ticker', '?')}: {item.get('notification_status', 'suppressed')} "
        f"({item.get('notification_reason', 'no reason recorded')})"
        for item in suppressed[:3]
    ) if suppressed else "- None suppressed by cooldown/confidence layer."
    high_conviction_lines = "\n".join(
        f"- {item.get('ticker', '?')}: conviction {float(item.get('conviction_score') or 0.0):.2f}, "
        f"size {item.get('target_allocation_band', 'n/a')}"
        for item in high_conviction[:3]
    ) if high_conviction else "- No high-conviction candidates today."
    starter_lines = "\n".join(
        f"- {item.get('ticker', '?')}: conviction {float(item.get('conviction_score') or 0.0):.2f}, "
        f"starter band {item.get('target_allocation_band', 'n/a')}"
        for item in starter_sized[:3]
    ) if starter_sized else "- No starter-sized ideas today."
    deferred_lines = "\n".join(
        f"- {item.get('ticker', '?')}: {item.get('conviction_band', 'defer')} "
        f"({item.get('notification_reason', 'deferred by conviction layer')})"
        for item in deferred[:3]
    ) if deferred else "- No conviction-driven deferrals today."
    portfolio_lines = "\n".join(
        f"- {item.get('ticker', '?')}: {float(item.get('normalized_allocation') or 0.0):.1%} "
        f"normalized ({item.get('conviction_band', 'observe')}, sector {item.get('sector', 'Unknown')})"
        for item in top_allocations[:3]
    ) if top_allocations else "- No actionable portfolio allocations today."
    capped_lines = "\n".join(
        f"- {item.get('ticker', '?')}: capped via {item.get('allocation_cap_reason', 'allocation cap')}"
        for item in capped_candidates[:3]
    ) if capped_candidates else "- No capped positions."
    warning_lines = "\n".join(
        f"- {warning}" for warning in portfolio_warnings[:5]
    ) if portfolio_warnings else "- No concentration warnings."
    regime_perf_lines = [
        f"- Current regime win rate: {float(regime_performance.get('win_rate') or 0.0):.1%}"
        if regime_performance.get("win_rate") is not None else "- Current regime win rate: n/a",
        f"- Avg return: {float(regime_performance.get('avg_return_pct') or 0.0):+.2f}%"
        if regime_performance.get("avg_return_pct") is not None else "- Avg return: n/a",
        f"- Best conviction band: {regime_performance.get('best_conviction_band') or 'n/a'}",
        f"- Worst conviction band: {regime_performance.get('worst_conviction_band') or 'n/a'}",
        f"- Note: {regime_performance.get('degraded_data_impact_note') or 'No regime performance note available.'}",
    ] if regime_performance else ["- No regime performance history available yet."]
    simulation_lines = "\n".join(
        f"- {item.get('policy', 'policy')}: win rate {float(item.get('win_rate') or 0.0):.1%}, "
        f"avg return {float(item.get('avg_return_pct') or 0.0):+.2f}%, trades {int(item.get('total_trades') or 0)}"
        for item in simulated_policies[:3]
    ) if simulated_policies else "- No policy simulation results available yet."
    current_regime_name = str(market_regime.get("regime_label") or "neutral")
    strategy_policy_lines = [
        f"- Best recent policy by win rate: {simulation_comparison.get('best_by_win_rate') or 'n/a'}",
        f"- Best recent policy by drawdown: {simulation_comparison.get('best_by_drawdown') or 'n/a'}",
        f"- Best policy for current regime ({current_regime_name}): "
        f"{(simulation_comparison.get('best_policy_by_regime') or {}).get(current_regime_name, 'n/a')}",
        f"- Best policy under degraded mode: {simulation_comparison.get('best_degraded_mode_policy') or 'n/a'}",
    ] if simulation_comparison else ["- No strategy policy summary available yet."]
    strong_lines = "\n".join(
        f"- {item.get('ticker', '?')}: score {float(item.get('historical_performance_score') or 0.0):.2f}, "
        f"win rate {float(item.get('win_rate') or 0.0):.1%}, avg return {float(item.get('avg_return_pct') or 0.0):+.2f}%"
        for item in strong_tickers[:3]
    ) if strong_tickers else "- No historically strong tickers identified yet."
    weak_lines = "\n".join(
        f"- {item.get('ticker', '?')}: reliability {item.get('signal_reliability', 'weak')}, "
        f"win rate {float(item.get('win_rate') or 0.0):.1%}, avg return {float(item.get('avg_return_pct') or 0.0):+.2f}%"
        for item in low_reliability[:3]
    ) if low_reliability else "- No low-reliability signals flagged yet."

    return (
        f"# {mode.capitalize()} Decision Memo — {today}\n"
        f"*[OFFLINE MODE — templated stub, no LLM configured]*\n\n"
        f"## Executive Summary\n"
        f"- Portfolio: {value_str} (ATH: {ath_str}, drawdown: {dd_str})\n"
        f"- Regime: {regime} | Expected CAGR: {cagr:.1%}\n"
        f"- Guardrails: {'VIOLATIONS' if violations else 'clean'}\n\n"
        f"## Key Action Item\n"
        f"Deploy ${contribution:,} monthly contribution per plan below.\n\n"
        f"## Contribution Guidance\n"
        f"{plan_str}\n\n"
        f"## Watchlist Actionability\n"
        f"High-confidence signals:\n"
        f"{signal_lines}\n\n"
        f"Suppressed signals:\n"
        f"{suppressed_lines}\n\n"
        f"## Conviction And Sizing\n"
        f"{conviction_summary_line}\n\n"
        f"High conviction candidates:\n"
        f"{high_conviction_lines}\n\n"
        f"Starter-sized ideas:\n"
        f"{starter_lines}\n\n"
        f"Deferred due to degraded mode / cooldown / low reliability:\n"
        f"{deferred_lines}\n\n"
        f"## Market Regime View\n"
        f"- {regime_summary_line}\n"
        f"- Portfolio fit: {regime_fit}\n"
        f"- Commentary: {regime_commentary}\n\n"
        f"## Regime Performance Insights\n"
        f"{chr(10).join(regime_perf_lines)}\n\n"
        f"## Policy Simulation Insights\n"
        f"{simulation_lines}\n\n"
        f"## Strategy Policy View\n"
        f"{chr(10).join(strategy_policy_lines)}\n\n"
        f"{strategy_recommendation_section}"
        f"## Portfolio Construction View\n"
        f"- Portfolio view: {portfolio_label}\n"
        f"- {portfolio_summary_line}\n\n"
        f"Top suggested allocations:\n"
        f"{portfolio_lines}\n\n"
        f"Concentration warnings:\n"
        f"{warning_lines}\n\n"
        f"Capped positions:\n"
        f"{capped_lines}\n\n"
        f"## Historical Signal Performance\n"
        f"Historically strong tickers:\n"
        f"{strong_lines}\n\n"
        f"Low reliability signals:\n"
        f"{weak_lines}\n\n"
        f"## Risk Flags\n"
        f"{guardrail_str}\n\n"
        f"---\n"
        f"*Set OPENAI_MODEL + OPENAI_API_KEY (or ANTHROPIC_API_KEY for the fallback) to enable AI-generated memos.*\n"
    )


def _offline_monthly_stub(bundle: dict, today: str) -> str:
    """Deterministic monthly memo stub — no LLM required."""
    drawdown = bundle.get("drawdown", {})
    value = bundle.get("portfolio_value", drawdown.get("current_value", "unknown"))
    ath = drawdown.get("all_time_high", "unknown")
    drawdown_pct = drawdown.get("drawdown_pct", 0.0)
    cagr = bundle.get("expected_cagr", 0.0)
    target_cagr = bundle.get("config", {}).get("target_cagr", 0.09)
    contribution = bundle.get("config", {}).get("monthly_contribution", 0)
    plan = bundle.get("contribution_plan", [])
    guardrails = bundle.get("guardrails", {})
    violations = guardrails.get("violations", [])
    strategy_recommendation_section = _render_strategy_recommendation_section(bundle)

    value_str = f"${value:,.2f}" if isinstance(value, (int, float)) else str(value)
    ath_str = f"${ath:,.2f}" if isinstance(ath, (int, float)) else str(ath)
    dd_str = f"{drawdown_pct:.1%}" if isinstance(drawdown_pct, float) else str(drawdown_pct)

    plan_str = "\n".join(
        f"| {p['symbol']} | ${p.get('dollars', 0):,.0f} | {p.get('reason', '')} |"
        for p in plan[:4]
    ) if plan else "| — | — | Run engine first |"

    guardrail_str = "No guardrail violations." if not violations else "\n".join(
        f"- **{v.get('rule', '?')}**: {json.dumps(v)}"
        for v in violations
    )

    # Rough 10-year projection
    proj_conservative = _project_10yr(value, cagr, contribution)
    proj_target = _project_10yr(value, target_cagr, contribution)

    return (
        f"# Monthly Capital Deployment Memo — {today}\n"
        f"*[OFFLINE MODE — templated stub, no LLM configured]*\n\n"
        f"## Executive Summary\n"
        f"- Portfolio: {value_str} (ATH: {ath_str})\n"
        f"- Drawdown: {dd_str}\n"
        f"- Expected CAGR: {cagr:.1%} vs target {target_cagr:.1%}\n\n"
        f"## Portfolio Headline\n"
        f"| Metric | Value |\n"
        f"|--------|-------|\n"
        f"| Portfolio Value | {value_str} |\n"
        f"| All-Time High | {ath_str} |\n"
        f"| Drawdown | {dd_str} |\n"
        f"| Expected CAGR | {cagr:.1%} |\n"
        f"| Target CAGR | {target_cagr:.1%} |\n\n"
        f"## Monthly Contribution Plan (${contribution:,} + available cash)\n"
        f"| Symbol | Deploy | Reason |\n"
        f"|--------|--------|--------|\n"
        f"{plan_str}\n\n"
        f"{strategy_recommendation_section}"
        f"## Guardrails & Risk\n"
        f"{guardrail_str}\n\n"
        f"## 10-Year Projections\n"
        f"- Conservative ({cagr:.1%} CAGR): ${proj_conservative:,.0f}\n"
        f"- Target ({target_cagr:.1%} CAGR): ${proj_target:,.0f}\n\n"
        f"---\n"
        f"*Set ANTHROPIC_API_KEY to enable Claude-powered monthly memos.*\n"
    )


def _project_10yr(value: float, cagr: float, monthly_contribution: float) -> float:
    """Simple 10-year FV projection with monthly contributions."""
    if not isinstance(value, (int, float)):
        return 0.0
    monthly_rate = cagr / 12
    fv = value
    for _ in range(120):  # 10 years * 12 months
        fv = fv * (1 + monthly_rate) + monthly_contribution
    return fv


if __name__ == "__main__":
    main()
