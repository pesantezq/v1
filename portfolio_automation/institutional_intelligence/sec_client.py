"""
Governed SEC EDGAR client — the ONLY sanctioned network surface for the
Institutional Intelligence subsystem.

Design (mirrors portfolio_automation/data_budget/ governance, adapted for a
free but rate-limited public source):

  * User-Agent is sourced from the ``SEC_EDGAR_USER_AGENT`` environment variable
    (never config, never hardcoded) and is NEVER written to the request ledger,
    cache metadata, or any artifact — SEC requires a descriptive contact UA, and
    that contact value must not leak.
  * Conservative rate limit strictly below SEC's 10 req/s courtesy limit.
  * All responses are cached to disk (content + a metadata sidecar WITHOUT the UA).
  * Every request is recorded to an append-only SQLite ledger (no UA).
  * Bounded exponential backoff; ``Retry-After`` respected; transient statuses
    (429/5xx) retried, permanent ones (400/403/404/malformed) never retried.
  * Offline-first: fixtures resolve before cache before live. Live is attempted
    ONLY when ``live_enabled`` is true AND a UA is present. Unit tests set
    ``live_enabled=False`` and provide a fixtures dir, so they never touch the
    network. Accession number is the stable filing identity used downstream.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from portfolio_automation import env

# EDGAR endpoints (public).
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# Conservative default rate: 5 req/s, half of SEC's 10 req/s courtesy limit.
DEFAULT_REQUESTS_PER_SECOND = 5
DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT_S = 20.0
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 30.0

# Statuses worth a bounded retry; everything else is terminal (never retried).
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
# Explicitly terminal — never retry (malformed request / not found / forbidden).
_TERMINAL_STATUSES = frozenset({400, 401, 403, 404, 410})

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS institutional_ingestion_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,          -- fixture | cache | live | disabled | error
    status INTEGER,               -- HTTP status when live, else NULL
    bytes INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    retry_after REAL,
    content_hash TEXT,
    error_class TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingestion_ledger_url ON institutional_ingestion_ledger(url);
"""


class SECClientError(Exception):
    """Raised for a terminal SEC transport failure (never for a disabled run)."""


@dataclass(frozen=True)
class SECClientConfig:
    live_enabled: bool = False
    requests_per_second: int = DEFAULT_REQUESTS_PER_SECOND
    max_retries: int = DEFAULT_MAX_RETRIES
    timeout_s: float = DEFAULT_TIMEOUT_S
    cache_dir: Path = field(default_factory=lambda: Path("data/institutional_cache"))
    fixtures_dir: Path | None = None
    db_path: Path = field(default_factory=lambda: Path("data/institutional_intelligence.db"))
    # Cache is durable by default (SEC filings are immutable by accession); a
    # positive value bounds re-use of the mutable submissions index.
    cache_ttl_seconds: float | None = None


@dataclass(frozen=True)
class SECResponse:
    """A fetched (or resolved) SEC response. ``body`` is the raw text.

    ``source`` records provenance (fixture/cache/live/disabled). ``content_hash``
    is the sha256 of the body for downstream idempotence. The User-Agent is
    deliberately absent — it is never surfaced.
    """

    url: str
    source: str
    status: int | None
    body: str | None
    content_hash: str | None
    from_cache: bool = False
    from_fixture: bool = False

    @property
    def ok(self) -> bool:
        return self.body is not None


def cik_to_padded(cik: str | int) -> str:
    """Normalize a CIK to the 10-digit zero-padded form EDGAR URLs use."""
    digits = "".join(ch for ch in str(cik) if ch.isdigit())
    if not digits:
        raise SECClientError(f"invalid CIK: {cik!r}")
    return digits.zfill(10)


def should_retry(status: int | None) -> bool:
    """Pure retry decision (unit-testable without network)."""
    if status is None:
        return False
    return status in _TRANSIENT_STATUSES


def backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """Bounded exponential backoff; honors an explicit Retry-After."""
    if retry_after is not None and retry_after >= 0:
        return min(float(retry_after), _BACKOFF_MAX_S)
    return min(_BACKOFF_BASE_S * (2 ** max(attempt, 0)), _BACKOFF_MAX_S)


def _sanitize_url_to_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class GovernedSECClient:
    """Offline-first governed EDGAR client. Never bypasses this class for I/O."""

    def __init__(
        self,
        config: SECClientConfig | None = None,
        *,
        user_agent: str | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.time,
        urlopen: Callable | None = None,
    ) -> None:
        self.config = config or SECClientConfig()
        # UA from env unless explicitly injected (tests inject a dummy). Held in
        # memory only; NEVER written to ledger/cache-meta/artifacts.
        self._user_agent = user_agent or env.get_optional("SEC_EDGAR_USER_AGENT")
        self._sleep = sleep_fn
        self._clock = clock_fn
        self._urlopen = urlopen or urllib.request.urlopen
        self._min_interval = (
            1.0 / self.config.requests_per_second
            if self.config.requests_per_second > 0 else 0.0
        )
        self._last_request_at = 0.0
        self._ensure_ledger()

    # -- readiness ------------------------------------------------------
    @property
    def live_ready(self) -> bool:
        """True only when live ingestion is enabled AND a UA is configured."""
        return bool(self.config.live_enabled and self._user_agent)

    def readiness(self) -> dict:
        """Non-secret readiness summary (never includes the UA value)."""
        return {
            "live_enabled": bool(self.config.live_enabled),
            "user_agent_present": bool(self._user_agent),
            "live_ready": self.live_ready,
            "requests_per_second": self.config.requests_per_second,
            "fixtures_dir_set": self.config.fixtures_dir is not None,
        }

    # -- public fetch ---------------------------------------------------
    def fetch(self, url: str) -> SECResponse:
        """Resolve a URL: fixture → cache → live → disabled. Never raises for a
        disabled/absent result; raises SECClientError only on a terminal live
        failure."""
        # 1) Fixture (offline, deterministic — used by all unit tests).
        fixture = self._read_fixture(url)
        if fixture is not None:
            resp = SECResponse(url, "fixture", None, fixture,
                               _hash(fixture), from_fixture=True)
            self._record(resp, retries=0)
            return resp

        # 2) Cache.
        cached = self._read_cache(url)
        if cached is not None:
            resp = SECResponse(url, "cache", None, cached, _hash(cached),
                               from_cache=True)
            self._record(resp, retries=0)
            return resp

        # 3) Live — ONLY when ready. Otherwise disabled (never touches network).
        if not self.live_ready:
            resp = SECResponse(url, "disabled", None, None, None)
            self._record(resp, retries=0)
            return resp

        return self._fetch_live(url)

    def fetch_submissions(self, cik: str | int) -> SECResponse:
        return self.fetch(SUBMISSIONS_URL.format(cik=cik_to_padded(cik)))

    # -- live path ------------------------------------------------------
    def _fetch_live(self, url: str) -> SECResponse:
        retries = 0
        last_status: int | None = None
        while True:
            self._rate_limit()
            try:
                status, body, retry_after = self._raw_get(url)
            except SECClientError:
                self._record(SECResponse(url, "error", None, None, None),
                             retries=retries, error_class="transport")
                raise
            last_status = status
            if status == 200 and body is not None:
                self._write_cache(url, body)
                resp = SECResponse(url, "live", status, body, _hash(body))
                self._record(resp, retries=retries)
                return resp
            if status in _TERMINAL_STATUSES or not should_retry(status):
                self._record(SECResponse(url, "error", status, None, None),
                             retries=retries, retry_after=retry_after,
                             error_class=f"http_{status}")
                raise SECClientError(f"terminal SEC status {status} for {url}")
            if retries >= self.config.max_retries:
                self._record(SECResponse(url, "error", status, None, None),
                             retries=retries, retry_after=retry_after,
                             error_class="retries_exhausted")
                raise SECClientError(
                    f"SEC retries exhausted ({retries}) for {url} (last {status})")
            self._sleep(backoff_delay(retries, retry_after))
            retries += 1

    def _raw_get(self, url: str) -> tuple[int, str | None, float | None]:
        """The single network call site. UA set here, never persisted."""
        req = urllib.request.Request(url, headers={"User-Agent": self._user_agent})
        try:
            with self._urlopen(req, timeout=self.config.timeout_s) as fh:
                status = getattr(fh, "status", 200) or 200
                data = fh.read()
            body = data.decode("utf-8", errors="replace")
            return int(status), body, None
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            retry_after = _parse_retry_after(exc.headers)
            return int(exc.code), None, retry_after
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SECClientError(f"SEC transport error for {url}: {exc.__class__.__name__}") from exc

    def _rate_limit(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = self._clock() - self._last_request_at
        if elapsed < self._min_interval:
            self._sleep(self._min_interval - elapsed)
        self._last_request_at = self._clock()

    # -- fixtures / cache ----------------------------------------------
    def _read_fixture(self, url: str) -> str | None:
        if self.config.fixtures_dir is None:
            return None
        key = _sanitize_url_to_key(url)
        # Prefer a hashed filename; also allow a human-named manifest mapping.
        for cand in (self.config.fixtures_dir / f"{key}.txt",
                     self.config.fixtures_dir / f"{key}.json",
                     self.config.fixtures_dir / f"{key}.xml"):
            if cand.exists():
                return cand.read_text(encoding="utf-8")
        manifest = self.config.fixtures_dir / "manifest.json"
        if manifest.exists():
            import json
            mapping = json.loads(manifest.read_text(encoding="utf-8"))
            fname = mapping.get(url)
            if fname:
                fpath = self.config.fixtures_dir / fname
                if fpath.exists():
                    return fpath.read_text(encoding="utf-8")
        return None

    def _cache_path(self, url: str) -> Path:
        return self.config.cache_dir / f"{_sanitize_url_to_key(url)}.body"

    def _read_cache(self, url: str) -> str | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        if self.config.cache_ttl_seconds is not None:
            age = self._clock() - path.stat().st_mtime
            if age > self.config.cache_ttl_seconds:
                return None
        return path.read_text(encoding="utf-8")

    def _write_cache(self, url: str, body: str) -> None:
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        # Body only; no UA, no request headers persisted.
        self._cache_path(url).write_text(body, encoding="utf-8")

    # -- ledger ---------------------------------------------------------
    def _ensure_ledger(self) -> None:
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.config.db_path) as cx:
            cx.executescript(_LEDGER_DDL)

    def _record(self, resp: SECResponse, *, retries: int,
                retry_after: float | None = None,
                error_class: str | None = None) -> None:
        try:
            n_bytes = len(resp.body.encode("utf-8")) if resp.body else 0
            with sqlite3.connect(self.config.db_path) as cx:
                cx.execute(
                    "INSERT INTO institutional_ingestion_ledger "
                    "(ts, url, source, status, bytes, retries, retry_after, "
                    "content_hash, error_class) VALUES (?,?,?,?,?,?,?,?,?)",
                    (self._clock(), resp.url, resp.source, resp.status, n_bytes,
                     retries, retry_after, resp.content_hash, error_class),
                )
        except Exception:  # noqa: BLE001 - telemetry must never break a run
            pass

    def ledger_rows(self, limit: int = 100) -> list[dict]:
        with sqlite3.connect(self.config.db_path) as cx:
            cx.row_factory = sqlite3.Row
            rows = cx.execute(
                "SELECT ts, url, source, status, bytes, retries, retry_after, "
                "content_hash, error_class FROM institutional_ingestion_ledger "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _parse_retry_after(headers) -> float | None:
    try:
        val = headers.get("Retry-After") if headers else None
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None
