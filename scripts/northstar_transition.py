from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _write(path: str, text: str) -> None:
    (REPO_ROOT / path).write_text(text, encoding="utf-8")


def _replace_one(
    text: str,
    pattern: str,
    replacement: str,
    *,
    flags: int = 0,
) -> str:
    out, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(
            f"expected exactly one replacement for {pattern!r}, got {count}"
        )
    return out


def _indent_block(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else prefix for line in text.splitlines())


def _restore_paused_adapter(
    from_mission: str,
    to_mission: str,
    completion_sha: str,
) -> None:
    if from_mission != "northstar_continuous_mission_orchestration_foundation":
        raise RuntimeError("unsupported automatic transition source")
    if to_mission != "northstar_vs002_execution_adapter_foundation":
        raise RuntimeError("unsupported automatic transition target")

    phase_path = ".agent/phase_status.yaml"
    project_path = ".agent/project_state.yaml"
    runtime_path = "config/ew0a_runtime.json"

    phase_text = _read(phase_path)
    phase_data = yaml.safe_load(phase_text)
    ns = phase_data["stockbot_northstar_redesign"]
    p0c = ns["phases"]["northstar_phase_0c"]
    bounded = p0c["bounded_authorization"]
    paused: dict[str, Any] = bounded["paused_bounded_authorization"]

    if (
        paused["authorized_mission"] != to_mission
        or paused["status"] != "PAUSED_NOT_EXECUTED"
    ):
        raise RuntimeError("paused authorization does not match transition target")

    scope = str(paused["scope"])
    authorized_at = (
        paused["authorized_at"].isoformat()
        if hasattr(paused["authorized_at"], "isoformat")
        else str(paused["authorized_at"])
    )
    previous = bounded.get("prior_bounded_authorization") or {}
    previous_yaml = yaml.safe_dump(
        previous,
        sort_keys=False,
        width=1000,
    ).rstrip()

    new_authorization = f"""      bounded_authorization:
        authorized_by: operator
        authorized_at: {authorized_at}
        authorized_mission: {to_mission}
        scope: >-
{_indent_block(scope, 10)}
        not_yet_executed: true   # candidate implementation may exist, but the adapter is not durable on main and VS-002 has not executed
        prior_bounded_authorization:
          authorized_by: operator
          authorized_at: 2026-10-04
          authorized_mission: {from_mission}
          status: COMPLETE
          merged_main_sha: {completion_sha}
          post_merge_main_ci_result: SUCCESS
          note: >-
            Continuous mission orchestration foundation completed durably. GitHub now owns exact-head
            CI/review waiting, SHA-guarded merge, post-merge certification, protected transition handoff,
            and dispatch of only already-authorized Claude missions. It granted no production/capital/C1
            or real-evidence authority. The prior result-runner authorization remains preserved below.
          prior_bounded_authorization:
{_indent_block(previous_yaml, 12)}
"""

    phase_text = _replace_one(
        phase_text,
        r"^\s{6}bounded_authorization:\n.*?(?=^\s{6}depends_on:)",
        new_authorization,
        flags=re.M | re.S,
    )
    phase_text = _replace_one(
        phase_text,
        r"^(\s{4}mission_id:)\s*\S+",
        rf"\1 {to_mission}",
        flags=re.M,
    )
    phase_text = _replace_one(
        phase_text,
        r"^(\s{4}last_authorized_mission:)\s*\S+",
        rf"\1 {from_mission}",
        flags=re.M,
    )
    phase_text = _replace_one(
        phase_text,
        r"^(\s{6}step:)\s*\S+.*$",
        rf"\1 {to_mission}   # automatically restored from the exact paused operator authorization after orchestration became durable",
        flags=re.M,
    )

    if "\n        continuous_mission_orchestration_foundation:\n" not in phase_text:
        phase_text = _replace_one(
            phase_text,
            r"^(\s{8}historical_price_evidence_prerequisite:)",
            "        continuous_mission_orchestration_foundation:\n"
            "          status: complete\n"
            "          durable: true\n"
            f"          merged_main_sha: {completion_sha}\n"
            "          post_merge_main_ci_result: SUCCESS\n"
            "          grants_authority: false\n"
            "          delivered: exact-head merge gate + post-merge certification + protected transition/Claude handoff\n"
            r"\1",
            flags=re.M,
        )

    _write(phase_path, phase_text)

    project_text = _read(project_path)
    project_text = _replace_one(
        project_text,
        r"^current_step:\s*\S+.*$",
        f"current_step: {to_mission}  # restored automatically from the exact operator-approved paused authorization after orchestration post-merge certification",
        flags=re.M,
    )
    project_text = _replace_one(
        project_text,
        r"^(\s{2}primary:)\s*\S+.*$",
        rf"\1 {to_mission}  # exact paused authorization restored after orchestration durability",
        flags=re.M,
    )
    project_text = _replace_one(
        project_text,
        r"^(\s{2}secondary:)\s*.*$",
        r"\1 []",
        flags=re.M,
    )
    project_text = _replace_one(
        project_text,
        r"^(\s{2}prior_primary:)\s*\S+.*$",
        rf"\1 {from_mission}  # COMPLETE and durable at {completion_sha}; post-merge CI SUCCESS",
        flags=re.M,
    )

    if f"  - {from_mission}" not in project_text:
        project_text = _replace_one(
            project_text,
            r"^(completed_steps:\n)",
            rf"\1  - {from_mission}  # durable at {completion_sha}; post-merge CI SUCCESS\n",
            flags=re.M,
        )

    _write(project_path, project_text)

    runtime = json.loads(_read(runtime_path))
    if runtime.get("mission_id") != from_mission:
        raise RuntimeError("runtime mission drift before transition")
    runtime["mission_id"] = to_mission
    _write(runtime_path, json.dumps(runtime, indent=2) + "\n")

    # Reconcile the most operationally important prose surfaces. The generated
    # governance PR still goes through full CI + Codex, which catches new drift.
    for path in [
        "docs/NORTHSTAR_REDESIGN.md",
        "docs/roadmap.md",
        "docs/vs002_production_evidence_runner.md",
    ]:
        text = _read(path)
        text = text.replace(
            "current bounded step `northstar_continuous_mission_orchestration_foundation`",
            f"current bounded step `{to_mission}`",
        )
        text = text.replace(
            "Current authorized bounded step: `northstar_continuous_mission_orchestration_foundation`",
            f"Current authorized bounded step: `{to_mission}`",
        )
        text = text.replace(
            "The current authorized bounded step is `northstar_continuous_mission_orchestration_foundation`",
            f"The current authorized bounded step is `{to_mission}`",
        )
        _write(path, text)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--from-mission", required=True)
    apply_parser.add_argument("--to-mission", required=True)
    apply_parser.add_argument("--completion-sha", required=True)

    args = parser.parse_args()
    _restore_paused_adapter(
        args.from_mission,
        args.to_mission,
        args.completion_sha,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
