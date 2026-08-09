"""Governed provider identity for the Intraday Lab. Research-only, HISTORICAL.

WHY THIS EXISTS
===============

`acquire()` used to take a bare `fetcher(symbol, start, end)` callable and then
stamp the resulting evidence with a HARDCODED ``provider="fmp"`` and a
hardcoded endpoint string. That is a provenance lie waiting to happen: the
identity written into immutable raw evidence was an assumption about the
callable, not a property of it. Swap the callable and the evidence still claims
FMP. Since provider + endpoint are now part of RAW IDENTITY, a wrong claim there
silently mis-addresses the object.

A provider is therefore an OBJECT THAT KNOWS WHAT IT IS. The pipeline asks it
for its own identity rather than asserting one on its behalf.

DELIBERATELY SMALL
==================

This is an interface plus two implementations, not a framework. The governed
FMP implementation opens NO sockets of its own: it delegates to the sanctioned
`FMPClient.get_json`, which supplies cache-first reads, the daily budget guard,
the rate limiter and the call ledger. Re-implementing raw HTTP here would route
research traffic around every FMP governance control in the repo.

The endpoint is read from `fmp_endpoint_registry.REGISTRY`, so an unregistered
timeframe cannot be fetched at all — the registry stays the single source of
truth for what this account is entitled to call.
"""
from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

SCHEMA_VERSION = "1"

# Registry key for the Intraday Lab's research bars. Registered 2026-08-08.
FMP_REGISTRY_KEY = "intraday_chart"


class ProviderError(RuntimeError):
    """Provider could not answer. Recorded as evidence, never swallowed."""


class ProviderBudgetRefusal(ProviderError):
    """The governed client refused the call to protect the daily budget.

    Distinct from "the provider had no data": a refusal means WE declined to
    ask. Collapsing it into DATA_UNAVAILABLE would record a market-data gap that
    never happened and let a budget problem masquerade as a source problem.
    """


class UnsupportedTimeframe(ProviderError):
    """The timeframe is not registered/entitled for this account."""


@runtime_checkable
class IntradayProvider(Protocol):
    """What the pipeline needs from any source of intraday research bars."""

    provider_id: str

    def endpoint_for(self, timeframe: str) -> str:
        """The exact endpoint identity that will serve this timeframe."""

    def fetch(self, symbol: str, start: str, end: str,
              timeframe: str) -> tuple[Any, int | None]:
        """Return `(rows, http_status)`. Raise ProviderError to record failure."""

    def provenance(self) -> dict:
        """Self-description persisted alongside acquisition evidence."""


class GovernedFMPIntradayProvider:
    """FMP intraday bars through the sanctioned, budget-governed client.

    Holds no HTTP logic. `client` must expose `get_json(path, params, ttl_seconds)`
    — the repo's `FMPClient`. Injected rather than constructed so tests can
    supply a double without reaching the network, and so the caller decides the
    budget policy the client was built with.
    """

    provider_id = "fmp"

    def __init__(self, client: Any, *, ttl_seconds: int = 24 * 3600,
                 registry: dict | None = None) -> None:
        if not hasattr(client, "get_json"):
            raise ProviderError(
                "client must expose get_json() — the Intraday Lab must not open "
                "its own HTTP path around the governed FMP client")
        self._client = client
        self._ttl = ttl_seconds
        if registry is None:
            from fmp_endpoint_registry import REGISTRY as _R
            registry = _R
        self._registry = registry

    def endpoint_for(self, timeframe: str) -> str:
        """Endpoint from the REGISTRY, never composed from a format string.

        The registry declares exactly one probed-and-entitled intraday endpoint.
        Building `/stable/historical-chart/{tf}` by interpolation would happily
        produce a path for 1min, which this account is NOT entitled to (HTTP
        402, probed 2026-08-08) — advertising capability that does not exist is
        the error class this lab exists to prevent.
        """
        entry = self._registry.get(FMP_REGISTRY_KEY) or {}
        endpoint = entry.get("endpoint")
        if not endpoint:
            raise UnsupportedTimeframe(
                f"{FMP_REGISTRY_KEY!r} is not present in the FMP endpoint registry")
        if not endpoint.rstrip("/").endswith(timeframe):
            raise UnsupportedTimeframe(
                f"timeframe {timeframe!r} is not the registered intraday "
                f"endpoint ({endpoint}); register and probe entitlement first")
        return endpoint

    def fetch(self, symbol: str, start: str, end: str,
              timeframe: str) -> tuple[Any, int | None]:
        endpoint = self.endpoint_for(timeframe)
        rows = self._client.get_json(
            endpoint, {"symbol": symbol, "from": start, "to": end},
            ttl_seconds=self._ttl)
        if rows is None:
            # get_json returns None when the budget guard refuses AND no stale
            # cache exists. That is our refusal, not the provider's silence.
            raise ProviderBudgetRefusal(
                f"governed FMP client returned no body for {symbol} "
                f"{start}..{end} — daily budget guard refused the call and no "
                f"cached response was available")
        return rows, None

    def provenance(self) -> dict:
        entry = self._registry.get(FMP_REGISTRY_KEY) or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "governed": True,
            "client": type(self._client).__name__,
            "registry_key": FMP_REGISTRY_KEY,
            "endpoint": entry.get("endpoint"),
            "starter_safe": entry.get("starter_safe"),
            "classification": entry.get("classification"),
            "cache_ttl_seconds": self._ttl,
            "notes": "cache-first, budget-guarded and ledgered by FMPClient.get_json",
        }


class FakeIntradayProvider:
    """Deterministic in-memory provider for tests and dry runs.

    Declares its OWN identity, so evidence produced from a fake is addressed as
    a fake. A test fixture that claimed `provider="fmp"` would mint raw
    identities colliding with real research objects — the exact failure the
    provider-identity work removes.
    """

    def __init__(self, rows_by_symbol: dict[str, Any], *,
                 provider_id: str = "fake", endpoint: str = "/fake/intraday",
                 http_status: int | None = None,
                 raises: dict[str, Exception] | None = None) -> None:
        self.provider_id = provider_id
        self._rows = rows_by_symbol
        self._endpoint = endpoint
        self._http = http_status
        self._raises = raises or {}

    def endpoint_for(self, timeframe: str) -> str:
        return self._endpoint

    def fetch(self, symbol: str, start: str, end: str,
              timeframe: str) -> tuple[Any, int | None]:
        if symbol in self._raises:
            raise self._raises[symbol]
        return self._rows.get(symbol), self._http

    def provenance(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "provider_id": self.provider_id,
                "governed": False, "endpoint": self._endpoint,
                "notes": "in-memory test double — not a research source"}


class CallableIntradayProvider:
    """Adapter for a bare `fetcher(symbol, start, end)` callable.

    Kept so existing callers and tests keep working, but provider identity is
    REQUIRED rather than assumed — that assumption was the original defect.
    """

    def __init__(self, fetcher: Callable[[str, str, str], tuple[Any, int | None]],
                 *, provider_id: str, endpoint: str) -> None:
        if not provider_id or not endpoint:
            raise ProviderError(
                "provider_id and endpoint are required — raw identity now "
                "protects source semantics and must not be guessed")
        self.provider_id = provider_id
        self._fetcher = fetcher
        self._endpoint = endpoint

    def endpoint_for(self, timeframe: str) -> str:
        return self._endpoint

    def fetch(self, symbol: str, start: str, end: str,
              timeframe: str) -> tuple[Any, int | None]:
        return self._fetcher(symbol, start, end)

    def provenance(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "provider_id": self.provider_id,
                "governed": False, "endpoint": self._endpoint,
                "notes": "adapter around an injected callable"}


def coerce_provider(source: Any, *, timeframe: str) -> IntradayProvider:
    """Accept a provider, or adapt a legacy callable, refusing to invent identity.

    A bare callable cannot describe itself, so it is adapted only under the
    explicitly legacy `callable:` provider id. That keeps old tests running
    while making it impossible for an unidentified callable to produce evidence
    that CLAIMS to be FMP.
    """
    if hasattr(source, "fetch") and hasattr(source, "provider_id"):
        return source
    if callable(source):
        return CallableIntradayProvider(
            source, provider_id="callable:unspecified",
            endpoint=f"/unspecified/intraday/{timeframe}")
    raise ProviderError(
        f"cannot use {type(source).__name__} as an intraday provider")
