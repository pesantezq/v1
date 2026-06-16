"""
Shared LLM adapter layer for StockBot.

Supported provider values:
  openai      (primary)
  anthropic   (fallback)

OpenAI is the primary provider; Anthropic remains as a fallback. The
`call_claude()` and `call_openai()` entry points are preserved so existing
call sites keep working, while newer code routes through `call_provider()`.
`validate_openai_connection()` is the LLM-reachability health probe consumed
by the GUI health panel and the discovery-health checks.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from agent.io_utils import redact
from portfolio_automation.env import get_secret

logger = logging.getLogger("stockbot.agent.llm_adapters")

SUPPORTED_PROVIDERS = {"anthropic", "openai"}

_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"


def normalize_provider(provider: Optional[str]) -> Optional[str]:
    """Return a normalized provider name or None when unset."""
    if provider is None:
        return None
    raw = provider.strip().lower()
    if not raw:
        return None
    return resolve_provider(raw, default=raw)


def resolve_provider(provider: Optional[str], *, default: str = "openai") -> str:
    """Resolve the provider selection from arg/env/default."""
    raw = (provider or os.environ.get("STOCKBOT_LLM_PROVIDER") or default).strip().lower()
    if raw not in SUPPORTED_PROVIDERS:
        raise RuntimeError(
            f"Unsupported LLM provider '{raw}'. "
            f"Supported values: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
        )
    return raw


def resolve_task_provider(
    *,
    cli_provider: Optional[str] = None,
    task_provider: Optional[str] = None,
    fallback_task_provider: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve provider precedence without forcing a default provider.

    Order:
      1. explicit CLI provider
      2. STOCKBOT_LLM_PROVIDER global override
      3. task-specific config provider
      4. fallback task-specific config provider
      5. None (caller keeps existing default routing)
    """
    for candidate in (
        cli_provider,
        os.environ.get("STOCKBOT_LLM_PROVIDER"),
        task_provider,
        fallback_task_provider,
    ):
        normalized = normalize_provider(candidate)
        if normalized:
            return normalized
    return None


def _read_http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    return body


def _normalize_http_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise RuntimeError(
            f"Invalid base URL '{base_url}'. Expected an http(s) URL."
        )
    return normalized


def _extract_chat_text(body: dict[str, Any]) -> str:
    choices = body.get("choices", [])
    if not choices:
        raise RuntimeError(
            "malformed response from the chat completions endpoint: missing 'choices'. "
            "Verify OPENAI_BASE_URL points to an OpenAI-compatible /v1 endpoint."
        )
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "".join(text_parts).strip()
    text = str(content).strip()
    if not text:
        raise RuntimeError(
            "malformed response from the chat completions endpoint: empty message content. "
            "Verify the model is compatible with /v1/chat/completions."
        )
    return text


def _call_openai_compatible_chat(
    *,
    provider_label: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: int,
    base_url: str,
    api_key: str,
) -> str:
    if not model:
        raise RuntimeError(f"{provider_label} model is not configured.")

    url = f"{_normalize_http_base_url(base_url)}/chat/completions"
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_body = resp.read().decode("utf-8")
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"malformed JSON response from {provider_label} at {base_url}. "
                    "Verify the endpoint is OpenAI-compatible."
                ) from exc
        elapsed = time.monotonic() - t0
        text = _extract_chat_text(body)
        logger.debug("%s %s -> %d chars in %.1fs", provider_label, model, len(text), elapsed)
        return text
    except urllib.error.HTTPError as exc:
        body = _read_http_error_body(exc)
        redacted_body = redact(body or str(exc))
        raise RuntimeError(f"{provider_label} API error: HTTP {exc.code} - {redacted_body}") from exc
    except urllib.error.URLError as exc:
        reason = redact(str(getattr(exc, "reason", exc)))
        raise RuntimeError(f"{provider_label} connection failed for {base_url} ({reason})") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(
            f"{provider_label} request timed out when calling {base_url}. "
            "Verify the endpoint is reachable and the model is responsive."
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"{provider_label} API error: {redact(str(exc))}") from exc


def call_openai(
    model: str,
    prompt: str,
    max_tokens: int = 1200,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 90,
) -> str:
    """Call the OpenAI chat completions API (or an OpenAI-compatible endpoint)."""
    resolved_model = (model or os.environ.get("OPENAI_MODEL") or "").strip()
    if not resolved_model:
        raise RuntimeError(
            "OPENAI_MODEL is not set. Set OPENAI_MODEL in your .env or shell environment."
        )
    key = (api_key or get_secret("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY environment variable is not set. "
            "Set it in your .env file or shell environment."
        )
    resolved_base_url = base_url or os.environ.get("OPENAI_BASE_URL") or _DEFAULT_OPENAI_BASE_URL
    return _call_openai_compatible_chat(
        provider_label="OpenAI",
        model=resolved_model,
        prompt=prompt,
        max_tokens=max_tokens,
        timeout=timeout,
        base_url=resolved_base_url,
        api_key=key,
    )


def call_claude(
    model: str,
    prompt: str,
    max_tokens: int = 2000,
    api_key: Optional[str] = None,
) -> str:
    """
    Call the Anthropic Claude API (Messages API).
    """
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(
            "anthropic package not installed. Run: pip install anthropic"
        ) from exc

    resolved_model = (model or os.environ.get("ANTHROPIC_MODEL") or _DEFAULT_CLAUDE_MODEL).strip()
    key = api_key or get_secret("ANTHROPIC_API_KEY") or ""
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Set it in your .env file or shell environment."
        )

    client = anthropic.Anthropic(api_key=key)
    t0 = time.monotonic()
    try:
        msg = client.messages.create(
            model=resolved_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        elapsed = time.monotonic() - t0
        text = msg.content[0].text.strip() if msg.content else ""
        logger.debug("Claude %s -> %d chars in %.1fs", resolved_model, len(text), elapsed)
        return text
    except Exception as exc:
        raise RuntimeError(f"Claude API error: {redact(str(exc))}") from exc


def call_provider(
    *,
    provider: str,
    model: str,
    prompt: str,
    max_tokens: int = 1200,
    timeout: int = 90,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Route a prompt to the selected provider and return plain text."""
    resolved_provider = resolve_provider(provider, default=provider)
    if resolved_provider == "openai":
        return call_openai(
            model=model,
            prompt=prompt,
            max_tokens=max_tokens,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
    return call_claude(
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
        api_key=api_key,
    )


def validate_openai_connection(
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """
    Verify OpenAI reachability by running a tiny prompt. Returns a status dict
    (same shape consumed by the GUI health panel + discovery-health checks).
    """
    resolved_model = (model or os.environ.get("OPENAI_MODEL") or "").strip()
    resolved_base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or _DEFAULT_OPENAI_BASE_URL).strip()
    key = (api_key or get_secret("OPENAI_API_KEY") or "").strip()

    if not resolved_model:
        return {
            "ok": False,
            "provider": "openai",
            "base_url": resolved_base_url,
            "model": resolved_model,
            "message": "OPENAI_MODEL is not set. Set OPENAI_MODEL in your .env or shell environment.",
        }
    if not key:
        return {
            "ok": False,
            "provider": "openai",
            "base_url": resolved_base_url,
            "model": resolved_model,
            "message": "OPENAI_API_KEY is not set. Set it in your .env file or shell environment.",
        }

    try:
        t0 = time.monotonic()
        response = call_openai(
            model=resolved_model,
            prompt="Reply with the single word OK.",
            max_tokens=8,
            api_key=key,
            base_url=resolved_base_url,
            timeout=max(5, timeout),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "provider": "openai",
            "base_url": resolved_base_url,
            "model": resolved_model,
            "latency_ms": latency_ms,
            "response": response,
            "message": (
                f"OpenAI responded successfully via {resolved_base_url} "
                f"using model '{resolved_model}'."
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "openai",
            "base_url": resolved_base_url,
            "model": resolved_model,
            "message": redact(str(exc)),
        }
