#!/usr/bin/env python3
"""
Agent context check — prints a concise project state summary for Claude/Codex/GPT.

Reads .agent/project_state.yaml and .agent/phase_status.yaml.
Exits nonzero if required files are missing or required keys are absent.
Does not modify any files. Does not require external services.

Usage:
    python scripts/agent_context_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

def _find_repo_root() -> Path:
    """Prefer cwd if it has a .agent/ directory, else fall back to script location."""
    cwd_root = Path.cwd()
    if (cwd_root / ".agent").is_dir():
        return cwd_root
    return Path(__file__).resolve().parent.parent


REPO_ROOT = _find_repo_root()
STATE_FILE = REPO_ROOT / ".agent" / "project_state.yaml"
PHASE_FILE = REPO_ROOT / ".agent" / "phase_status.yaml"

REQUIRED_STATE_KEYS = [
    "project_name",
    "mode",
    "no_auto_trading",
    "ai_role",
    "current_phase",
    "current_step",
    "completed_steps",
    "next_official_step",
    "deferred_steps",
    "forbidden_changes",
    "required_test_policy",
    "output_namespace_policy",
    "role_split",
]

REQUIRED_PHASE_KEYS = [
    "phase_0",
    "agent_orchestration_layer",
    "post_phase_0",
    "permanently_deferred",
]


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        print(
            "ERROR: PyYAML is not installed.\n"
            "Run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    if not path.exists():
        print(f"ERROR: Required file not found: {path}", file=sys.stderr)
        sys.exit(1)

    with path.open(encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            print(f"ERROR: Could not parse {path}: {exc}", file=sys.stderr)
            sys.exit(1)

    if not isinstance(data, dict):
        print(f"ERROR: {path} must be a YAML mapping (dict at top level).", file=sys.stderr)
        sys.exit(1)

    return data


def _validate_keys(data: dict, required: list[str], path: Path) -> list[str]:
    missing = [k for k in required if k not in data]
    return missing


def _format_list(items: object, indent: int = 4) -> str:
    prefix = " " * indent
    if isinstance(items, list):
        return "\n".join(f"{prefix}- {item}" for item in items) or f"{prefix}(none)"
    if isinstance(items, dict):
        primary = items.get("primary", "")
        secondary = items.get("secondary", [])
        lines = [f"{prefix}primary: {primary}"]
        if secondary:
            for s in secondary:
                lines.append(f"{prefix}  - {s}")
        return "\n".join(lines)
    return f"{prefix}{items}"


def main() -> int:
    state = _load_yaml(STATE_FILE)
    phase = _load_yaml(PHASE_FILE)

    errors: list[str] = []

    missing_state = _validate_keys(state, REQUIRED_STATE_KEYS, STATE_FILE)
    if missing_state:
        errors.append(
            f"project_state.yaml is missing required keys: {', '.join(missing_state)}"
        )

    missing_phase = _validate_keys(phase, REQUIRED_PHASE_KEYS, PHASE_FILE)
    if missing_phase:
        errors.append(
            f"phase_status.yaml is missing required keys: {', '.join(missing_phase)}"
        )

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    # ── Print summary ────────────────────────────────────────────────────────
    print("=" * 60)
    print("  Portfolio Automation — Agent Context")
    print("=" * 60)

    print(f"\n  Project:       {state.get('project_name', '—')}")
    print(f"  Mode:          {state.get('mode', '—')}")
    print(f"  Advisory-only: {str(state.get('mode', '') == 'advisory_only').lower()}")
    print(f"  No auto-trade: {str(state.get('no_auto_trading', False)).lower()}")
    print(f"  AI role:       {state.get('ai_role', '—')}")

    print(f"\n  Current phase: {state.get('current_phase', '—')}")
    print(f"  Current step:  {state.get('current_step', '—')}")

    completed = state.get("completed_steps", [])
    print(f"\n  Completed steps: {len(completed) if isinstance(completed, list) else '?'}")
    if isinstance(completed, list):
        for step in completed[-5:]:
            print(f"    - {step}")
        if len(completed) > 5:
            print(f"    ... and {len(completed) - 5} more")

    print("\n  Next official step(s):")
    print(_format_list(state.get("next_official_step", "(none)")))

    deferred = state.get("deferred_steps", [])
    print(f"\n  Deferred steps: {len(deferred) if isinstance(deferred, list) else '?'}")

    forbidden = state.get("forbidden_changes", [])
    forbidden_count = len(forbidden) if isinstance(forbidden, list) else "?"
    print(f"\n  Forbidden changes: {forbidden_count}")
    if isinstance(forbidden, list):
        for item in forbidden[:5]:
            print(f"    - {item}")
        if len(forbidden) > 5:
            print(f"    ... and {len(forbidden) - 5} more (see project_state.yaml)")

    print("\n  Role split:")
    role_split = state.get("role_split", {})
    if isinstance(role_split, dict):
        for role in ("gpt", "claude", "codex", "user"):
            if role in role_split:
                print(f"    {role}: {', '.join(str(r) for r in role_split[role][:2])}{'...' if len(role_split[role]) > 2 else ''}")

    print("\n  VPS note: Claude runs locally. Return VPS commands for manual user execution.")

    print("\n" + "=" * 60)
    print("  Files: .agent/project_state.yaml | .agent/phase_status.yaml")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
