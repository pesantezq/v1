"""
Theme Detector — extracts investing themes from headlines through the configured LLM provider.

In testing_mode (or when STOCKBOT_TESTING=1 env is set) no network call is made;
a deterministic mock response is returned instead.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from agent.llm_adapters import call_provider, resolve_ollama_base_url, resolve_provider

logger = logging.getLogger(__name__)

_CANONICAL_THEMES = [
    "AI Infrastructure",
    "Cybersecurity",
    "Cloud Infrastructure",
    "Semicap Equipment",
    "Payments",
    "Energy Transition",
    "Defense",
    "Healthcare Innovation",
    "Consumer Staples Resilience",
    "Industrial Automation",
]
_CANONICAL_LIST = "\n".join(f"- {t}" for t in _CANONICAL_THEMES)

_PROMPT_TEMPLATE = """\
You are a financial theme analyst. Analyze the following news headlines and identify \
up to 5 durable, long-term investing themes that a growth-oriented stock investor \
would care about. Focus on structural trends, not short-term noise.

You MUST use names from this approved list (exact spelling):
{canonical_list}

Output ONLY valid JSON in exactly this format — no markdown, no explanation:
{{
  "themes": [
    {{
      "name": "theme name from approved list above",
      "confidence": 0.85,
      "rationale": "one-sentence rationale (max 200 chars)",
      "evidence_items": ["headline 1", "headline 2"],
      "direct_mentions": ["TICKER1", "CompanyName2"]
    }}
  ]
}}

Headlines:
{headlines}
"""

_RETRY_SUFFIX = "\n\nReturn ONLY valid JSON. No explanation, no markdown."

# Compact prompt for OpenAI — returns name, confidence, and keywords.
# The richer optional fields (rationale, evidence_items, direct_mentions) are
# still accepted by _validate_themes when the model includes them.
_OPENAI_PROMPT_TEMPLATE = """\
You are a financial theme analyst. Analyze the following news headlines and identify \
up to 5 durable, long-term investing themes that a growth-oriented stock investor \
would care about. Focus on structural trends, not short-term noise.

You MUST use names from this approved list (exact spelling):
{canonical_list}

Return ONLY valid JSON in exactly this format — no markdown, no explanation:
{{
  "themes": [
    {{
      "name": "theme name from approved list above",
      "confidence": 0.85,
      "keywords": ["keyword1", "keyword2", "keyword3"]
    }}
  ]
}}

confidence: 0.0 to 1.0 (how strongly the headlines support this theme)
keywords: 2–5 key terms from the headlines that evidence this theme

Headlines:
{headlines}
"""

# Deterministic mock output used in testing_mode or when STOCKBOT_TESTING=1
MOCK_THEMES: list[dict[str, Any]] = [
    {
        "name": "AI Infrastructure",
        "confidence": 0.90,
        "rationale": "Multiple headlines highlight GPU demand and data center buildout.",
        "evidence_items": ["Nvidia posts record revenue on AI demand", "Cloud giants expand GPU clusters"],
        "direct_mentions": ["NVDA", "MSFT"],
    },
    {
        "name": "Cybersecurity",
        "confidence": 0.75,
        "rationale": "Nation-state cyber attacks drive enterprise security spending.",
        "evidence_items": ["Major breach exposes federal agency data"],
        "direct_mentions": ["CRWD", "PANW"],
    },
]


class ThemeDetector:
    """Detect investing themes from a list of headline dicts using the selected provider.

    Args:
        model:        LLM model name for the selected provider.
        endpoint:     Backward-compatible Ollama endpoint hint.
        provider:     Provider name: ollama | anthropic | openai.
        base_url:     Optional OpenAI-compatible base URL for Ollama/OpenAI.
        api_key:      Optional API key override.
        testing_mode: If True, return MOCK_THEMES without any network call.
        timeout:      HTTP request timeout in seconds.
    """

    def __init__(
        self,
        model: str = "gemma3:4b",
        endpoint: str = "http://localhost:11434/api/generate",
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        testing_mode: bool = False,
        timeout: int = 60,
    ) -> None:
        self.model = model
        self.provider = resolve_provider(provider, default="ollama")
        self.endpoint = endpoint
        self.base_url = self._resolve_base_url(base_url, endpoint)
        self.api_key = api_key
        self.timeout = timeout
        # Respect env var as well as constructor flag
        self.testing_mode = testing_mode or bool(os.getenv("STOCKBOT_TESTING"))

    # ── Public API ────────────────────────────────────────────────────────────

    def detect(self, headlines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return a list of detected theme dicts.

        Each theme dict has: name, confidence, rationale, evidence_items, direct_mentions.
        Returns MOCK_THEMES in testing_mode (no network call).
        Returns [] on unrecoverable failure.
        """
        if self.testing_mode:
            logger.info("ThemeDetector: testing_mode — returning mock themes")
            return list(MOCK_THEMES)

        if not headlines:
            logger.info("ThemeDetector: no headlines provided, skipping LLM call")
            return []

        headlines_text = "\n".join(
            f"{i+1}. {h['title']}" for i, h in enumerate(headlines[:50])
        )
        template = _OPENAI_PROMPT_TEMPLATE if self.provider == "openai" else _PROMPT_TEMPLATE
        prompt = template.format(
            canonical_list=_CANONICAL_LIST,
            headlines=headlines_text,
        )

        raw = self._call_ollama(prompt)
        if raw is None:
            return []

        themes = self._parse_response(raw)
        if themes is None:
            # One retry with stricter instruction
            logger.warning("ThemeDetector: JSON parse failed, retrying once")
            raw2 = self._call_ollama(prompt + _RETRY_SUFFIX)
            if raw2 is None:
                return []
            themes = self._parse_response(raw2)

        if themes is None:
            logger.error("ThemeDetector: could not parse valid JSON after retry")
            return []

        return self._validate_themes(themes)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_base_url(self, base_url: str | None, endpoint: str | None) -> str | None:
        """Map legacy Ollama endpoint inputs onto the new OpenAI-compatible base URL."""
        if self.provider != "ollama":
            return base_url
        if base_url:
            return resolve_ollama_base_url(base_url)
        if endpoint:
            legacy = endpoint.rstrip("/")
            if legacy.endswith("/api/generate"):
                legacy = legacy[: -len("/api/generate")]
            return resolve_ollama_base_url(legacy)
        return resolve_ollama_base_url()

    def _call_ollama(self, prompt: str) -> str | None:
        """Backwards-compatible call seam; now routes through the selected provider."""
        try:
            return call_provider(
                provider=self.provider,
                model=self.model,
                prompt=prompt,
                timeout=self.timeout,
                max_tokens=900,
                base_url=self.base_url,
                api_key=self.api_key,
            )
        except Exception as exc:
            logger.error(
                "ThemeDetector: %s request failed for model %s: %s",
                self.provider,
                self.model,
                exc,
            )
            return None

    def _parse_response(self, raw: str) -> list[dict[str, Any]] | None:
        """Try to parse JSON from LLM response.  Returns None if invalid."""
        # Strip markdown code fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first and last fence lines
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON object substring
            start = text.find("{")
            end = text.rfind("}") + 1
            if start == -1 or end <= start:
                return None
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                return None

        if not isinstance(data, dict):
            return None
        themes = data.get("themes")
        if not isinstance(themes, list):
            return None
        return themes

    def _validate_themes(self, raw_themes: list[Any]) -> list[dict[str, Any]]:
        """Sanitise and cap at 5 themes.

        Accepts both the full format (rationale, evidence_items, direct_mentions)
        and the compact OpenAI format (keywords only).  When evidence_items is
        absent but keywords is present, keywords fill the evidence_items slot so
        downstream consumers (ThemeStore, ThemeMapper) remain unaffected.
        """
        result = []
        for item in raw_themes[:5]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            confidence = float(item.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
            rationale = str(item.get("rationale", ""))[:200]
            keywords = [str(k) for k in item.get("keywords", []) if k][:10]
            evidence = [str(e) for e in item.get("evidence_items", []) if e][:5]
            if not evidence and keywords:
                evidence = keywords[:5]
            mentions = [str(m).upper() for m in item.get("direct_mentions", []) if m][:10]
            result.append({
                "name": name,
                "confidence": confidence,
                "rationale": rationale,
                "keywords": keywords,
                "evidence_items": evidence,
                "direct_mentions": mentions,
            })
        return result
