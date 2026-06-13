"""
Environment Variable Registry — Phase E
========================================

Single source of truth for every environment variable the system reads.
Provides typed accessors, declared defaults, and a ``--check`` CLI that
lists current state without leaking secret values.

Design constraints:

- **Additive.** Existing call sites continue to use ``os.environ.get(...)``
  directly. This module's accessors are opt-in.
- **Read-only.** Importing this module performs no I/O and never mutates
  ``os.environ``.
- **Secret-safe.** Secret values are never printed by ``--check`` and are
  redacted by :func:`redact_secrets` for inclusion in log/error messages.
- **No reference shadowing.** The registry documents existing variables;
  it does not introduce new ones.

Usage::

    from portfolio_automation.env import get_required, get_optional, is_truthy

    api_key = get_required("FMP_API_KEY")       # raises MissingEnvVar if absent
    model   = get_optional("ANTHROPIC_MODEL")    # falls back to registered default
    test    = is_truthy("STOCKBOT_TESTING")      # "1" / "true" / "yes" → True

CLI::

    python -m portfolio_automation.env --check
    python -m portfolio_automation.env --check --format json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Logical grouping. Used by --check for sectioned output.
GROUP_DATA    = "data"      # market data providers
GROUP_LLM     = "llm"       # LLM provider selection + credentials
GROUP_EMAIL   = "email"     # SMTP / memo delivery
GROUP_RUNTIME = "runtime"   # feature flags, runtime toggles
GROUP_CONFIG  = "config"    # config file selection
GROUP_BROKER  = "broker"    # read-only broker sync (Schwab) credentials

ALLOWED_GROUPS: frozenset[str] = frozenset({
    GROUP_DATA, GROUP_LLM, GROUP_EMAIL, GROUP_RUNTIME, GROUP_CONFIG, GROUP_BROKER,
})


class MissingEnvVar(RuntimeError):
    """Raised when :func:`get_required` finds an unset environment variable."""


@dataclass(frozen=True)
class EnvVar:
    """One declared environment variable."""
    name: str
    required: bool
    default: str | None
    secret: bool
    description: str
    group: str
    aliases: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REGISTRY: tuple[EnvVar, ...] = (

    # ---- Config / runtime ----
    EnvVar(
        name="CONFIG_PATH",
        required=False,
        default="config.json",
        secret=False,
        description="Path to config.json (or to a directory containing base.json).",
        group=GROUP_CONFIG,
    ),
    EnvVar(
        name="CONFIG_PROFILE",
        required=False,
        default=None,
        secret=False,
        description="Optional profile overlay name (e.g. conservative / growth / tactical).",
        group=GROUP_CONFIG,
    ),
    EnvVar(
        name="REPO_ROOT",
        required=False,
        default=None,
        secret=False,
        description="Repo root override used by shell scripts. Auto-detected if unset.",
        group=GROUP_RUNTIME,
    ),
    EnvVar(
        name="STOCKBOT_ENV",
        required=False,
        default="development",
        secret=False,
        description="Deployment environment label (production / staging / development).",
        group=GROUP_RUNTIME,
    ),
    EnvVar(
        name="STOCKBOT_TESTING",
        required=False,
        default="0",
        secret=False,
        description="Truthy enables offline/testing mode in some modules.",
        group=GROUP_RUNTIME,
    ),
    EnvVar(
        name="STOCKBOT_DISABLE_LLM_FALLBACK",
        required=False,
        default="0",
        secret=False,
        description="Truthy disables the LLM fallback chain on primary failure.",
        group=GROUP_RUNTIME,
    ),
    EnvVar(
        name="DRY_RUN_MODE",
        required=False,
        default="0",
        secret=False,
        description="Truthy puts the daily pipeline in advisory-only dry-run mode.",
        group=GROUP_RUNTIME,
    ),
    EnvVar(
        name="AI_VALIDATOR_USE_LLM",
        required=False,
        default="0",
        secret=False,
        description="Truthy enables optional LLM enhancement in the AI decision validator.",
        group=GROUP_RUNTIME,
    ),
    EnvVar(
        name="PYTHONUNBUFFERED",
        required=False,
        default=None,
        secret=False,
        description="Set by systemd to flush stdout/stderr for log tailing.",
        group=GROUP_RUNTIME,
    ),

    # ---- Market data ----
    EnvVar(
        name="FMP_API_KEY",
        required=True,
        default=None,
        secret=True,
        description="Financial Modeling Prep API key. Required when scanner is enabled.",
        group=GROUP_DATA,
    ),
    # ---- LLM provider selection ----
    EnvVar(
        name="STOCKBOT_LLM_PROVIDER",
        required=False,
        default="ollama",
        secret=False,
        description="LLM provider selection: 'ollama' | 'openai' | 'anthropic'.",
        group=GROUP_LLM,
    ),
    EnvVar(
        name="OLLAMA_BASE_URL",
        required=False,
        default="http://localhost:11434/v1",
        secret=False,
        description="Ollama OpenAI-compatible API endpoint.",
        group=GROUP_LLM,
    ),
    EnvVar(
        name="OLLAMA_MODEL",
        required=False,
        default="gemma3:4b",
        secret=False,
        description="Ollama model tag.",
        group=GROUP_LLM,
    ),
    EnvVar(
        name="OLLAMA_API_KEY",
        required=False,
        default="ollama",
        secret=True,
        description="Ollama API key (only required if Ollama server enforces auth).",
        group=GROUP_LLM,
    ),
    EnvVar(
        name="OPENAI_API_KEY",
        required=False,  # conditional: required only when provider == openai
        default=None,
        secret=True,
        description="OpenAI API key. Required when STOCKBOT_LLM_PROVIDER=openai.",
        group=GROUP_LLM,
    ),
    EnvVar(
        name="OPENAI_MODEL",
        required=False,
        default="gpt-4o-mini",
        secret=False,
        description="OpenAI model name.",
        group=GROUP_LLM,
    ),
    EnvVar(
        name="OPENAI_BASE_URL",
        required=False,
        default="https://api.openai.com/v1",
        secret=False,
        description="OpenAI API base URL override.",
        group=GROUP_LLM,
    ),
    EnvVar(
        name="ANTHROPIC_API_KEY",
        required=False,  # conditional: required only when provider == anthropic
        default=None,
        secret=True,
        description="Anthropic API key. Required when STOCKBOT_LLM_PROVIDER=anthropic.",
        group=GROUP_LLM,
    ),
    EnvVar(
        name="ANTHROPIC_MODEL",
        required=False,
        default="claude-haiku-4-5-20251001",
        secret=False,
        description="Anthropic Claude model name.",
        group=GROUP_LLM,
    ),

    # ---- Email / memo delivery ----
    EnvVar(
        name="MEMO_EMAIL_ENABLED",
        required=False,
        default="0",
        secret=False,
        description="Truthy enables memo email delivery via memo_email_sender.",
        group=GROUP_EMAIL,
    ),
    EnvVar(
        name="MEMO_EMAIL_SMTP_HOST",
        required=False,
        default=None,
        secret=False,
        description="SMTP server hostname for memo_email_sender. Required when MEMO_EMAIL_ENABLED=1.",
        group=GROUP_EMAIL,
        aliases=("SMTP_SERVER", "SMTP_HOST"),
    ),
    EnvVar(
        name="MEMO_EMAIL_SMTP_PORT",
        required=False,
        default="587",
        secret=False,
        description="SMTP port for memo_email_sender.",
        group=GROUP_EMAIL,
        aliases=("SMTP_PORT",),
    ),
    EnvVar(
        name="MEMO_EMAIL_USERNAME",
        required=False,
        default=None,
        secret=False,
        description="SMTP username for memo_email_sender. Required when MEMO_EMAIL_ENABLED=1.",
        group=GROUP_EMAIL,
        aliases=("EMAIL_USER", "EMAIL_SENDER"),
    ),
    EnvVar(
        name="MEMO_EMAIL_PASSWORD",
        required=False,
        default=None,
        secret=True,
        description="SMTP password for memo_email_sender. Required when MEMO_EMAIL_ENABLED=1.",
        group=GROUP_EMAIL,
        aliases=("EMAIL_PASS", "EMAIL_PASSWORD"),
    ),
    EnvVar(
        name="MEMO_EMAIL_FROM",
        required=False,
        default=None,
        secret=False,
        description="From address for memo_email_sender. Required when MEMO_EMAIL_ENABLED=1.",
        group=GROUP_EMAIL,
    ),
    EnvVar(
        name="MEMO_EMAIL_TO",
        required=False,
        default=None,
        secret=False,
        description="Recipient address(es) for memo_email_sender. Required when MEMO_EMAIL_ENABLED=1.",
        group=GROUP_EMAIL,
        aliases=("EMAIL_TO", "EMAIL_RECIPIENT"),
    ),

    # ---- Schwab read-only broker sync (observe-only; NEVER required) ----
    # The layer self-reports `unconfigured` when these are absent, so none may be
    # required — preflight must stay green before provisioning. Trading is not
    # implemented regardless of any value here (AST-enforced in brokers/).
    EnvVar(
        name="SCHWAB_CLIENT_ID",
        required=False,
        default=None,
        secret=True,
        description="Schwab OAuth Client ID (developer portal). Enables read-only broker sync.",
        group=GROUP_BROKER,
    ),
    EnvVar(
        name="SCHWAB_CLIENT_SECRET",
        required=False,
        default=None,
        secret=True,
        description="Schwab OAuth Client Secret (developer portal). Required for read-only sync.",
        group=GROUP_BROKER,
    ),
    EnvVar(
        name="SCHWAB_REDIRECT_URI",
        required=False,
        default=None,
        secret=False,
        description="Exact redirect URI registered with the Schwab app, e.g. https://127.0.0.1/callback.",
        group=GROUP_BROKER,
    ),
    EnvVar(
        name="SCHWAB_READ_ONLY_MODE",
        required=False,
        default="true",
        secret=False,
        description="Activates the read-only broker layer. Trading is NOT implemented regardless.",
        group=GROUP_BROKER,
    ),
    EnvVar(
        name="TRADING_ENABLED",
        required=False,
        default="false",
        secret=False,
        description="Must remain false. Documentation signal only; no trading path exists in the codebase.",
        group=GROUP_BROKER,
    ),
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def find_var(name: str) -> EnvVar | None:
    """Return the :class:`EnvVar` for *name*, or ``None`` if unregistered."""
    for var in REGISTRY:
        if var.name == name:
            return var
    return None


def all_vars() -> tuple[EnvVar, ...]:
    """Return the full registry."""
    return REGISTRY


def vars_for_group(group: str) -> tuple[EnvVar, ...]:
    """Return all entries in *group*."""
    return tuple(v for v in REGISTRY if v.group == group)


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "y", "on"})


def get_required(name: str) -> str:
    """
    Return the value of *name* from the environment.

    Raises :class:`MissingEnvVar` if the variable is unset or empty.  The
    error message references the variable name but never its (possibly
    leaked) value from elsewhere.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingEnvVar(f"Required environment variable {name!r} is unset or empty")
    return value


def get_optional(name: str, default: str | None = None) -> str | None:
    """
    Return the value of *name*, falling back to *default* (or the registered
    default if *default* is None and a registry entry exists).
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if default is not None:
        return default
    var = find_var(name)
    if var is not None and var.default is not None:
        return var.default
    return None


def get_secret(name: str, default: str | None = None) -> str | None:
    """
    Alias of :func:`get_optional` but intended to mark intent at the call
    site.  No different behavior today; future hooks (e.g. masking) can
    attach here without changing call sites.
    """
    return get_optional(name, default=default)


def is_truthy(name: str, *, default: bool = False) -> bool:
    """
    Return True if the env var's value is a recognised truthy string
    (case-insensitive: ``1 / true / yes / y / on``).  Default applies
    when the variable is unset or empty.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

def _secret_names() -> tuple[str, ...]:
    """Every name + alias declared as secret."""
    names: list[str] = []
    for var in REGISTRY:
        if not var.secret:
            continue
        names.append(var.name)
        names.extend(var.aliases)
    return tuple(names)


# Match `NAME=value` (e.g. inside a shell-style trace or an exception that
# rendered an env dict). The boundary character class avoids gobbling the
# subsequent shell separator.
_REDACT_NAMES_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in _secret_names()) + r")\s*=\s*[^\s&\"'<>\n]+",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """
    Mask any ``SECRET_NAME=value`` pair in *text* so secrets cannot leak via
    error messages or logs.  Names recognised: every entry where
    ``secret=True`` in the registry, plus their declared aliases.
    """
    if not text:
        return text
    return _REDACT_NAMES_RE.sub(lambda m: f"{m.group(1)}=<REDACTED>", text)


# ---------------------------------------------------------------------------
# State inspection
# ---------------------------------------------------------------------------

def check_state() -> dict[str, Any]:
    """
    Return a structured snapshot of every declared env var's state without
    revealing secret values.

    Shape::

        {
          "advisory_only": true,
          "no_trade": true,
          "summary": {
              "total": <int>,
              "required_set": <int>,
              "required_missing": <int>,
              "optional_set": <int>,
              "optional_default": <int>,
              "secrets_set": <int>,
          },
          "groups": {
              "data":    [ <var-state>, ... ],
              "llm":     [ ... ],
              "email":   [ ... ],
              "runtime": [ ... ],
              "config":  [ ... ],
          },
          "missing_required": [ "FMP_API_KEY", ... ],
        }

    Per-var state has the following keys::

        {
          "name": str,
          "group": str,
          "required": bool,
          "secret": bool,
          "set": bool,                 # True if env has a non-empty value
          "source": "env" | "default" | "missing",
          "value": str | None,         # actual value if not secret; None otherwise
          "default": str | None,
          "description": str,
          "aliases_set": [str, ...],   # legacy aliases that *are* set
        }
    """
    groups: dict[str, list[dict[str, Any]]] = {g: [] for g in sorted(ALLOWED_GROUPS)}
    summary = {
        "total": 0,
        "required_set": 0,
        "required_missing": 0,
        "optional_set": 0,
        "optional_default": 0,
        "secrets_set": 0,
    }
    missing_required: list[str] = []

    for var in REGISTRY:
        raw = os.environ.get(var.name, "").strip()
        is_set = bool(raw)
        aliases_set = [a for a in var.aliases if os.environ.get(a, "").strip()]
        if is_set:
            source = "env"
            value = "<REDACTED>" if var.secret else raw
        elif var.default is not None:
            source = "default"
            value = "<REDACTED>" if var.secret else var.default
        else:
            source = "missing"
            value = None

        summary["total"] += 1
        if var.required:
            if is_set:
                summary["required_set"] += 1
            else:
                summary["required_missing"] += 1
                missing_required.append(var.name)
        else:
            if is_set:
                summary["optional_set"] += 1
            elif source == "default":
                summary["optional_default"] += 1
        if var.secret and is_set:
            summary["secrets_set"] += 1

        groups[var.group].append({
            "name": var.name,
            "group": var.group,
            "required": var.required,
            "secret": var.secret,
            "set": is_set,
            "source": source,
            "value": value,
            "default": "<REDACTED>" if (var.secret and var.default) else var.default,
            "description": var.description,
            "aliases_set": aliases_set,
        })

    return {
        "advisory_only": True,
        "no_trade": True,
        "summary": summary,
        "groups": groups,
        "missing_required": missing_required,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_text(state: dict[str, Any]) -> str:
    """Render :func:`check_state` output as a plain-text table."""
    lines: list[str] = []
    lines.append("Environment Variable Check")
    lines.append("=" * 40)
    if state.get("dotenv_loaded_from"):
        lines.append(f".env: {state['dotenv_loaded_from']}")
    s = state["summary"]
    lines.append(
        f"Total: {s['total']}   "
        f"required_set: {s['required_set']}   "
        f"required_missing: {s['required_missing']}   "
        f"secrets_set: {s['secrets_set']}"
    )
    if state["missing_required"]:
        lines.append("")
        lines.append("MISSING REQUIRED:")
        for n in state["missing_required"]:
            lines.append(f"  - {n}")
    lines.append("")
    for group in sorted(state["groups"].keys()):
        items = state["groups"][group]
        if not items:
            continue
        lines.append(f"[{group}]")
        for it in items:
            marker = "REQ" if it["required"] else "opt"
            secret = " (secret)" if it["secret"] else ""
            src = it["source"]
            value_display = it["value"] if it["value"] is not None else "<unset>"
            lines.append(
                f"  {marker} {it['name']:30s} {src:7s} = {value_display}{secret}"
            )
            if it["aliases_set"]:
                lines.append(
                    f"      (also-set aliases: {', '.join(it['aliases_set'])})"
                )
        lines.append("")
    lines.append("Advisory only — no trades executed.")
    return "\n".join(lines) + "\n"


def render_json(state: dict[str, Any]) -> str:
    return json.dumps(state, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _find_dotenv_for_cli() -> Path | None:
    """
    Locate a ``.env`` file near the operator: first the current working
    directory, then the repo root (two levels up from this file).  Returns
    None when no ``.env`` exists.  Library-mode imports never call this —
    it is invoked only from :func:`main`.
    """
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]
    seen: set[Path] = set()
    for c in candidates:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r in seen:
            continue
        seen.add(r)
        if r.is_file():
            return r
    return None


def _load_dotenv_for_cli() -> Path | None:
    """
    Best-effort ``.env`` load for CLI invocations.  Uses python-dotenv when
    available; falls back to a small parser when it isn't.  Never overrides
    values already present in the process environment.  Returns the file
    that was loaded, or None.
    """
    path = _find_dotenv_for_cli()
    if path is None:
        return None
    # Prefer python-dotenv when present.
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        load_dotenv = None  # type: ignore

    if load_dotenv is not None:
        try:
            load_dotenv(path, override=False)
            return path
        except Exception:
            pass  # fall through to manual parser

    # Manual parser — handles KEY=VALUE lines, strips comments / whitespace,
    # never overrides existing env vars.
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip a single layer of matching quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        return None
    return path


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m portfolio_automation.env",
        description=(
            "Inspect declared environment variables. Secret values are never "
            "printed. Use --check to print state; --strict exits non-zero when "
            "any required variable is missing."
        ),
    )
    p.add_argument("--check", action="store_true", help="Print env var state.")
    p.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format. Default: text.",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="Exit 1 when any required variable is missing.",
    )
    p.add_argument(
        "--no-dotenv", action="store_true",
        help="Skip auto-loading .env. Use when you want to inspect the bare "
             "process environment.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if not args.check:
        # Default behaviour: same as --check. Friendlier for `python -m`.
        args.check = True

    # Best-effort .env load so an interactive invocation matches what the
    # daily run would see. Process-env values always win; we never override.
    loaded_from: Path | None = None
    if not args.no_dotenv:
        loaded_from = _load_dotenv_for_cli()

    state = check_state()
    if loaded_from is not None:
        state["dotenv_loaded_from"] = str(loaded_from)
    if args.format == "json":
        print(render_json(state))
    else:
        print(render_text(state), end="")

    if args.strict and state["missing_required"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
