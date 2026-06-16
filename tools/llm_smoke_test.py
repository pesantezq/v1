"""Lightweight LLM connectivity checks for local validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent.llm_adapters import (
    call_provider,
    resolve_provider,
    validate_openai_connection,
)


def _run_generic_provider_check(provider: str, model: str, prompt: str, timeout: int) -> dict:
    try:
        response = call_provider(
            provider=provider,
            model=model,
            prompt=prompt,
            max_tokens=24,
            timeout=timeout,
        )
        return {
            "ok": True,
            "provider": provider,
            "model": model,
            "message": f"{provider} responded successfully.",
            "response": response,
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "message": str(exc),
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.llm_smoke_test",
        description="Run a small LLM connectivity check using the current StockBot provider config.",
    )
    parser.add_argument(
        "--provider",
        default=None,
        choices=["openai", "anthropic"],
        help="Provider to check (openai | anthropic). Defaults to STOCKBOT_LLM_PROVIDER or openai.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model override for the smoke test.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=12,
        help="HTTP timeout in seconds (default: 12).",
    )
    parser.add_argument(
        "--prompt",
        default="Reply with the single word OK.",
        help="Tiny test prompt to send after connectivity succeeds.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw result object as JSON.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    from utils import load_env

    load_env(str(root / ".env"))

    provider = resolve_provider(args.provider, default="openai")
    if provider == "openai":
        result = validate_openai_connection(
            model=args.model,
            timeout=args.timeout,
        )
    else:
        model = args.model or os.environ.get("ANTHROPIC_MODEL", "")
        result = _run_generic_provider_check(
            provider=provider,
            model=model,
            prompt=args.prompt,
            timeout=args.timeout,
        )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        status = "SUCCESS" if result.get("ok") else "FAIL"
        print(f"[{status}] {result.get('message', '')}")
        if result.get("base_url"):
            print(f"Base URL: {result['base_url']}")
        if result.get("model"):
            print(f"Model: {result['model']}")
        if result.get("latency_ms") is not None:
            print(f"Latency: {result['latency_ms']} ms")
        if result.get("response"):
            print(f"Response: {result['response']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
