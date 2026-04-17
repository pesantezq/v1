"""Run the same LLM-backed task across providers and capture comparison rows."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REQUIRED_COLUMNS = [
    "run_id",
    "task",
    "provider",
    "model",
    "latency_ms",
    "success",
    "fallback_triggered",
    "output_file",
    "manual_score_relevance",
    "manual_score_clarity",
    "manual_score_structure",
    "manual_score_actionability",
    "manual_score_hallucination_risk",
    "notes",
]

EXTRA_COLUMNS = [
    "requested_provider",
    "resolved_provider",
    "actual_provider",
    "base_url",
    "error_type",
    "fallback_reason",
    "git_commit",
]


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _safe_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value.strip().lower()) or "unknown"


def _task_spec(task: str) -> dict[str, Any]:
    if task == "agent_daily":
        return {
            "task_name": "agent.daily",
            "metadata_path": Path("outputs/latest/agent_llm_metadata.json"),
            "default_output": Path("outputs/latest/decision_memo.md"),
            "command": lambda provider, root, config, profile: _agent_command(
                mode="daily",
                provider=provider,
                root=root,
                config=config,
                profile=profile,
            ),
        }
    if task == "agent_weekly":
        return {
            "task_name": "agent.weekly",
            "metadata_path": Path("outputs/latest/agent_llm_metadata.json"),
            "default_output": Path("outputs/latest/decision_memo.md"),
            "command": lambda provider, root, config, profile: _agent_command(
                mode="weekly",
                provider=provider,
                root=root,
                config=config,
                profile=profile,
            ),
        }
    if task == "agent_monthly":
        return {
            "task_name": "agent.monthly",
            "metadata_path": Path("outputs/latest/agent_llm_metadata.json"),
            "default_output": Path("outputs/latest/monthly_memo.md"),
            "command": lambda provider, root, config, profile: _agent_command(
                mode="monthly",
                provider=provider,
                root=root,
                config=config,
                profile=profile,
            ),
        }
    if task == "theme_daily":
        return {
            "task_name": "theme_engine.daily",
            "metadata_path": Path("outputs/latest/theme_engine_llm_metadata.json"),
            "default_output": Path("outputs/latest/theme_signals.json"),
            "command": lambda provider, root, config, profile: _theme_command(
                mode="daily",
                provider=provider,
                root=root,
                config=config,
                profile=profile,
            ),
        }
    if task == "theme_weekly":
        return {
            "task_name": "theme_engine.weekly",
            "metadata_path": Path("outputs/latest/theme_engine_llm_metadata.json"),
            "default_output": Path("outputs/latest/theme_signals.json"),
            "command": lambda provider, root, config, profile: _theme_command(
                mode="weekly",
                provider=provider,
                root=root,
                config=config,
                profile=profile,
            ),
        }
    if task == "theme_monthly":
        return {
            "task_name": "theme_engine.monthly",
            "metadata_path": Path("outputs/latest/theme_engine_llm_metadata.json"),
            "default_output": Path("outputs/latest/theme_signals.json"),
            "command": lambda provider, root, config, profile: _theme_command(
                mode="monthly",
                provider=provider,
                root=root,
                config=config,
                profile=profile,
            ),
        }
    raise RuntimeError(f"Unsupported evaluation task '{task}'.")


def _agent_command(*, mode: str, provider: str, root: Path, config: str, profile: str | None) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "agent",
        "--mode",
        mode,
        "--provider",
        provider,
        "--root",
        str(root),
        "--config",
        config,
    ]
    if profile:
        command.extend(["--profile", profile])
    return command


def _theme_command(*, mode: str, provider: str, root: Path, config: str, profile: str | None) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "theme_engine",
        "--mode",
        mode,
        "--provider",
        provider,
        "--root",
        str(root),
        "--config",
        config,
    ]
    if profile:
        command.extend(["--profile", profile])
    return command


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _select_metadata(sidecar: dict[str, Any], task_name: str) -> dict[str, Any]:
    if "tasks" in sidecar:
        for item in sidecar.get("tasks", []):
            if item.get("task") == task_name:
                return dict(item)
        raise RuntimeError(f"No task metadata found for {task_name}")
    metadata = sidecar.get("llm_metadata", {})
    if metadata.get("task") != task_name:
        raise RuntimeError(f"No task metadata found for {task_name}")
    return dict(metadata)


def _copy_output_artifact(
    *,
    root: Path,
    eval_dir: Path,
    task: str,
    provider: str,
    metadata: dict[str, Any],
    default_output: Path,
) -> str:
    source_rel = Path(metadata.get("output_file") or default_output)
    source_path = root / source_rel
    if not source_path.exists():
        return ""
    artifacts_dir = eval_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    target_name = _artifact_filename(
        task=task,
        requested_provider=provider,
        actual_provider=metadata.get("actual_provider", ""),
        suffix=source_path.suffix,
    )
    target_path = artifacts_dir / target_name
    shutil.copy2(source_path, target_path)
    try:
        return str(target_path.relative_to(root))
    except ValueError:
        return str(target_path)


def _artifact_filename(
    *,
    task: str,
    requested_provider: str,
    actual_provider: str,
    suffix: str,
) -> str:
    requested_slug = _safe_slug(requested_provider)
    task_slug = _safe_slug(task)
    actual_slug = _safe_slug(actual_provider) if actual_provider else ""
    if actual_slug and actual_slug != requested_slug:
        return f"{task_slug}__requested-{requested_slug}__actual-{actual_slug}{suffix}"
    return f"{task_slug}__requested-{requested_slug}{suffix}"


def _build_notes(metadata: dict[str, Any]) -> str:
    if metadata.get("fallback_triggered"):
        actual_provider = metadata.get("actual_provider", "(unknown)")
        reason = metadata.get("fallback_reason") or "fallback triggered"
        return f"actual_provider={actual_provider}; {reason}"
    if not metadata.get("success", False):
        error_type = metadata.get("error_type") or "UnknownError"
        reason = metadata.get("fallback_reason") or "run failed"
        return f"{error_type}; {reason}"
    return ""


def _build_eval_row(
    *,
    task: str,
    requested_provider: str,
    metadata: dict[str, Any],
    output_file: str,
) -> dict[str, Any]:
    return {
        "run_id": metadata.get("run_id", ""),
        "task": task,
        "provider": metadata.get("actual_provider") or requested_provider,
        "model": metadata.get("actual_model") or metadata.get("model", ""),
        "latency_ms": metadata.get("latency_ms", ""),
        "success": metadata.get("success", False),
        "fallback_triggered": metadata.get("fallback_triggered", False),
        "output_file": output_file,
        "manual_score_relevance": "",
        "manual_score_clarity": "",
        "manual_score_structure": "",
        "manual_score_actionability": "",
        "manual_score_hallucination_risk": "",
        "notes": _build_notes(metadata),
        "requested_provider": requested_provider,
        "resolved_provider": metadata.get("resolved_provider", requested_provider),
        "actual_provider": metadata.get("actual_provider", ""),
        "base_url": metadata.get("actual_base_url") or metadata.get("base_url", ""),
        "error_type": metadata.get("error_type", ""),
        "fallback_reason": metadata.get("fallback_reason", ""),
        "git_commit": metadata.get("git_commit", ""),
    }


def _run_and_collect(
    *,
    root: Path,
    task: str,
    provider: str,
    eval_dir: Path,
    config: str,
    profile: str | None,
    disable_fallback: bool = False,
) -> dict[str, Any]:
    spec = _task_spec(task)
    metadata_path = root / spec["metadata_path"]
    metadata_path.unlink(missing_ok=True)

    env = os.environ.copy()
    env.pop("STOCKBOT_LLM_PROVIDER", None)
    if disable_fallback:
        env["STOCKBOT_DISABLE_LLM_FALLBACK"] = "1"
    else:
        env.pop("STOCKBOT_DISABLE_LLM_FALLBACK", None)

    result = subprocess.run(
        spec["command"](provider, root, config, profile),
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    if not metadata_path.exists():
        synthetic_metadata = {
            "run_id": f"missing-metadata-{_timestamp_slug()}",
            "task": spec["task_name"],
            "resolved_provider": provider,
            "actual_provider": "unknown",
            "model": "",
            "actual_model": "",
            "base_url": "",
            "actual_base_url": "",
            "latency_ms": "",
            "success": False,
            "error_type": "MissingMetadata",
            "fallback_reason": f"process_returncode={result.returncode}",
            "fallback_triggered": False,
            "git_commit": "",
        }
        copied_output = ""
        row = _build_eval_row(
            task=task,
            requested_provider=provider,
            metadata=synthetic_metadata,
            output_file=copied_output,
        )
        row["notes"] = (result.stderr or result.stdout or "").strip()[:400]
        return row

    sidecar = _read_json(metadata_path)
    metadata = _select_metadata(sidecar, spec["task_name"])
    copied_output = _copy_output_artifact(
        root=root,
        eval_dir=eval_dir,
        task=task,
        provider=provider,
        metadata=metadata,
        default_output=spec["default_output"],
    )
    return _build_eval_row(
        task=task,
        requested_provider=provider,
        metadata=metadata,
        output_file=copied_output,
    )


def _write_eval_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS + EXTRA_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _write_eval_summary(
    path: Path,
    *,
    task: str,
    providers: list[str],
    rows: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Provider Evaluation Summary",
        "",
        f"- Task: `{task}`",
        f"- Providers requested: `{', '.join(providers)}`",
        "",
        "| Requested | Actual | Success | Fallback | Latency (ms) | Artifact |",
        "|---|---|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| {requested} | {actual} | {success} | {fallback} | {latency} | `{artifact}` |".format(
                requested=row.get("requested_provider", ""),
                actual=row.get("actual_provider", ""),
                success=str(row.get("success", False)),
                fallback=str(row.get("fallback_triggered", False)),
                latency=row.get("latency_ms", ""),
                artifact=row.get("output_file", ""),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.provider_eval",
        description="Run the same task across providers and capture evaluation rows.",
    )
    parser.add_argument(
        "--task",
        required=True,
        choices=[
            "agent_daily",
            "agent_weekly",
            "agent_monthly",
            "theme_daily",
            "theme_weekly",
            "theme_monthly",
        ],
        help="Task to run across providers.",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        required=True,
        help="Providers to compare, for example: ollama anthropic openai",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Config path passed through to the underlying command.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional config profile.",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root directory.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional CSV output path. Defaults to outputs/evals/<timestamp>_<task>/provider_eval.csv",
    )
    parser.add_argument(
        "--disable-fallback",
        action="store_true",
        help="Disable provider fallback for this evaluation run (best for measuring a single provider directly).",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    from utils import load_env

    load_env(str(root / ".env"))

    eval_dir = (
        Path(args.output).resolve().parent
        if args.output
        else root / "outputs" / "evals" / f"{_timestamp_slug()}_{args.task}"
    )
    csv_path = Path(args.output).resolve() if args.output else eval_dir / "provider_eval.csv"

    rows = [
        _run_and_collect(
            root=root,
            task=args.task,
            provider=provider,
            eval_dir=eval_dir,
            config=args.config,
            profile=args.profile,
            disable_fallback=args.disable_fallback,
        )
        for provider in args.providers
    ]
    _write_eval_csv(csv_path, rows)
    _write_eval_summary(
        eval_dir / "provider_eval_summary.md",
        task=args.task,
        providers=args.providers,
        rows=rows,
    )

    print(f"Provider evaluation complete: {csv_path}")
    for row in rows:
        print(
            f"- {row['task']} | requested={row['requested_provider']} | actual={row['actual_provider']} | "
            f"model={row['model'] or '(unset)'} | success={row['success']} | "
            f"fallback={row['fallback_triggered']} | latency_ms={row['latency_ms'] or '(n/a)'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
