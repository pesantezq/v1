"""Governed production runner for the bounded VS-002 historical-price evidence
build (mission ``northstar_0c_historical_price_evidence_for_vs002``).

The library layers already exist — :mod:`.builder`, :mod:`.consumer`,
:mod:`.readiness`, and ``FMPDividendAdjustedProvider``. What was missing, and
what this module supplies, is the single supported entry point that wires them
together while closing the three behaviours a live production audit flagged as
unsafe to inherit:

1. **Retry amplification.** ``FMPClient`` defaults to ``retry_max=3``, so 22
   logical acquisitions could become >22 HTTP attempts. The runner acquires
   through :meth:`FMPClient.get_dividend_adjusted_bars_strict_live`, which makes
   exactly one attempt per symbol, and a mission-local accountant proves at most
   22 outbound requests.
2. **Stale-cache fallback.** ``get_historical_prices_dividend_adjusted`` serves
   fresh or stale cache and falls back to stale on error/budget exhaustion. The
   strict-live path reads no cache and never serves stale — it fails closed.
3. **Fixed in-place output.** ``builder.build`` writes a directory in place. The
   runner builds into a fresh per-run staging directory, validates it, and only
   then publishes it, by atomic rename, to a content-addressed
   ``packages/<package_id>`` that is never overwritten.

The runner ORCHESTRATES; it does not re-implement builder/consumer/readiness
semantics, and it never executes VS-002.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import builder as B
from . import consumer as CON
from . import contracts as C
from . import readiness as R

MISSION = "northstar_0c_historical_price_evidence_for_vs002"

# --- result classes + deterministic exit codes ---------------------------
PASS = "PASS"
NOT_READY = "NOT_READY"
BLOCKED_PREFLIGHT = "BLOCKED_PREFLIGHT"
FAIL_PROVIDER = "FAIL_PROVIDER"
FAIL_BUILD = "FAIL_BUILD"
FAIL_PACKAGE_VALIDATION = "FAIL_PACKAGE_VALIDATION"
FAIL_READINESS_CONTRACT = "FAIL_READINESS_CONTRACT"

EXIT_CODES: dict[str, int] = {
    PASS: 0,
    NOT_READY: 10,
    BLOCKED_PREFLIGHT: 20,
    FAIL_PROVIDER: 30,
    FAIL_BUILD: 31,
    FAIL_PACKAGE_VALIDATION: 32,
    FAIL_READINESS_CONTRACT: 33,
}

_REQUIRED_SIGNAL_COLUMNS = (
    "ticker", "signal_time", "signal_score", "price_at_signal",
    "outcome_return_7d", "outcome_price_7d", "evaluated_at_7d",
    "prediction_intent", "data_mode",
)


class StrictLiveAcquisitionError(RuntimeError):
    """A violation of the runner's acquisition contract (plan membership,
    SPY-first, no duplicate, hard 22-request cap)."""


def acquisition_plan() -> list[str]:
    """The frozen acquisition plan: SPY first, then the frozen non-benchmark
    universe in deterministic order. Exactly 22 symbols, no ticker exception."""
    return [C.BENCHMARK] + sorted(C.FROZEN_UNIVERSE)


class StrictLiveAcquirer:
    """The client the runner hands to ``FMPDividendAdjustedProvider``.

    It exposes the one method the provider calls,
    ``get_historical_prices_dividend_adjusted``, but routes it through the
    strict-live FMP path and OWNS the acquisition invariants regardless of how
    the builder drives the fetch loop:

    * every symbol must be in the frozen plan;
    * the benchmark (SPY) must be acquired first;
    * no symbol is acquired twice;
    * the number of outbound requests can never exceed the plan size (22).

    ``attempts`` records the actual outbound HTTP requests (an attempt is logged
    immediately before the request goes out, so a request that then fails still
    counts). A budget refusal, which sends nothing, is not an attempt.
    """

    def __init__(self, client: Any, plan: list[str]) -> None:
        self._client = client
        self._plan = list(plan)
        self._planset = set(self._plan)
        self._done: list[str] = []
        self.attempts: list[dict[str, Any]] = []

    def _record_attempt(self, symbol: str) -> None:
        self.attempts.append(
            {"n": len(self.attempts) + 1, "symbol": symbol, "success": None})

    def get_historical_prices_dividend_adjusted(self, symbol: str) -> list[dict]:
        sym = str(symbol).upper()
        if sym not in self._planset:
            raise StrictLiveAcquisitionError(
                f"{sym} is not in the frozen {len(self._plan)}-symbol "
                f"acquisition plan — the universe is frozen, no ticker exception")
        if sym in self._done:
            raise StrictLiveAcquisitionError(
                f"{sym} would be acquired more than once")
        if not self._done and sym != self._plan[0]:
            raise StrictLiveAcquisitionError(
                f"benchmark {self._plan[0]} must be acquired first; got {sym}")
        if len(self._done) >= len(self._plan):
            raise StrictLiveAcquisitionError(
                f"acquisition cap ({len(self._plan)}) reached; a further "
                f"request is refused — no 23rd acquisition is possible")
        self._done.append(sym)
        try:
            rows = self._client.get_dividend_adjusted_bars_strict_live(
                sym, on_attempt=self._record_attempt)
        except Exception:
            if (self.attempts and self.attempts[-1]["symbol"] == sym
                    and self.attempts[-1]["success"] is None):
                self.attempts[-1]["success"] = False
            raise
        if (self.attempts and self.attempts[-1]["symbol"] == sym
                and self.attempts[-1]["success"] is None):
            self.attempts[-1]["success"] = True
        return rows

    @property
    def attempted_http_requests(self) -> int:
        return len(self.attempts)

    @property
    def had_failure(self) -> bool:
        return any(a["success"] is False for a in self.attempts)


@dataclass
class RunnerResult:
    mission: str = MISSION
    endpoint: str = C.AUTHORIZED_ENDPOINT
    code_sha: Optional[str] = None
    run_id: Optional[str] = None
    frozen_universe: list[str] = field(default_factory=list)
    acquisition_order: list[str] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    max_http_attempts: int = 22
    package_staging_rel: Optional[str] = None
    package_path: Optional[str] = None
    package_id: Optional[str] = None
    package_published: bool = False
    package_overwrote_existing: bool = False
    consumer_validation: Optional[str] = None
    readiness_status: Optional[str] = None
    readiness_reasons: list[str] = field(default_factory=list)
    cohort_count: Optional[int] = None
    cohort_dates: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    exact_blockers: list[str] = field(default_factory=list)

    @property
    def attempted_http_requests(self) -> int:
        return len(self.attempts)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.result or "", 40)

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in (
            "mission", "code_sha", "endpoint", "run_id", "frozen_universe",
            "acquisition_order", "attempts", "max_http_attempts",
            "package_staging_rel", "package_path", "package_id",
            "package_published", "package_overwrote_existing",
            "consumer_validation", "readiness_status", "readiness_reasons",
            "cohort_count", "cohort_dates", "detail", "result",
            "exact_blockers")}
        d["attempted_http_requests"] = self.attempted_http_requests
        return d


def _default_credential_present() -> bool:
    try:
        from fmp_client import get_secret
    except Exception:
        return False
    return bool(get_secret("FMP_API_KEY"))


def _default_client_factory() -> Any:
    # Construction lives in the sanctioned data_budget factory, not here.
    from portfolio_automation.data_budget.factory import vs002_strict_evidence_client
    return vs002_strict_evidence_client()


def _git_sha(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15)
        sha = out.stdout.strip()
        return sha or "UNAVAILABLE"
    except Exception:
        return "UNAVAILABLE"


def _safe_rmtree(path: Path) -> None:
    """Remove ONLY a fresh per-run staging directory we created."""
    if path.name.startswith(".staging-") and path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _publish(root: Path, staging_rel: str, final_rel: str) -> tuple[bool, bool]:
    """Atomically publish the validated staging package to its content-addressed
    destination. NEVER overwrites an existing package: an identical package_id
    means byte-identical evidence already exists."""
    staging = root / staging_rel
    final = root / final_rel
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        _safe_rmtree(staging)            # our own fresh dir; the existing one is untouched
        return False, False
    os.replace(staging, final)           # atomic rename on one filesystem
    return True, False


def _only_cohort_deficit(readiness: R.Readiness) -> bool:
    """True iff the sole readiness blocker is the non-overlapping cohort count."""
    if readiness.status != R.NOT_READY or len(readiness.reasons) != 1:
        return False
    r = readiness.reasons[0]
    return "non-overlapping" in r and "cohort" in r and "required" in r


def run(repo_root: Any, *, code_sha: Optional[str] = None,
        client: Any = None, client_factory: Optional[Callable[[], Any]] = None,
        credential_present: Optional[Callable[[], bool]] = None,
        clock: Any = None, run_id: Optional[str] = None,
        generated_at: Optional[str] = None,
        out_root_rel: str = B.DEFAULT_OUT_REL) -> RunnerResult:
    """Run the bounded evidence build end to end and classify the outcome.

    Never executes VS-002. Makes no network call itself — acquisition happens
    through the injected/constructed strict-live client only after every
    preflight passes.
    """
    root = Path(repo_root)
    plan = acquisition_plan()
    now = datetime.now(timezone.utc)
    run_id = run_id or (now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    generated_at = generated_at or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    staging_rel = f"{out_root_rel}/.staging-{run_id}"
    res = RunnerResult(run_id=run_id, frozen_universe=list(plan),
                       package_staging_rel=staging_rel)

    # ---- G10 preflight: every failable check before attempt #1 ----------
    blockers: list[str] = []
    if len(C.FROZEN_UNIVERSE) != 21:
        blockers.append(
            f"frozen non-benchmark universe is {len(C.FROZEN_UNIVERSE)}, not 21")
    if C.BENCHMARK != "SPY":
        blockers.append(f"benchmark is {C.BENCHMARK!r}, not SPY")
    if len(set(plan)) != 22 or len(plan) != 22:
        blockers.append("acquisition plan is not exactly 22 unique symbols")
    if plan[0] != C.BENCHMARK:
        blockers.append("SPY is not first in the acquisition plan")
    if getattr(B.FMPDividendAdjustedProvider, "endpoint", None) != C.AUTHORIZED_ENDPOINT:
        blockers.append("provider endpoint identity is not the authorized endpoint")
    if not root.is_dir():
        blockers.append(f"repo root not found: {root}")
    db = root / B.DEFAULT_DB_REL
    if not db.is_file():
        blockers.append(f"signal database not found: {B.DEFAULT_DB_REL}")
    else:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                tbl = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='watchlist_signal_feedback'").fetchone()
                if not tbl:
                    blockers.append("watchlist_signal_feedback table is absent")
                else:
                    cols = {row[1] for row in
                            con.execute("PRAGMA table_info(watchlist_signal_feedback)")}
                    missing = [c for c in _REQUIRED_SIGNAL_COLUMNS if c not in cols]
                    if missing:
                        blockers.append(
                            f"signal table missing columns: {missing}")
            finally:
                con.close()
        except sqlite3.Error as exc:
            blockers.append(f"signal database not readable (ro): {exc}")
    present = (credential_present or _default_credential_present)()
    if not present:
        blockers.append("FMP credential is not present in the environment")
    if (root / staging_rel).exists():
        blockers.append(f"fresh staging path already exists: {staging_rel}")
    if blockers:
        res.result = BLOCKED_PREFLIGHT
        res.exact_blockers = blockers
        return res

    # ---- construct the client + last capacity preflight (still no network)
    if client is None:
        try:
            client = (client_factory or _default_client_factory)()
        except Exception as exc:  # noqa: BLE001 — credential/config unavailable
            res.result = BLOCKED_PREFLIGHT
            res.exact_blockers = [
                f"FMP client could not be constructed: {type(exc).__name__}: {exc}"]
            return res
    if hasattr(client, "can_admit") and not client.can_admit(len(plan)):
        res.result = BLOCKED_PREFLIGHT
        res.exact_blockers = [
            f"daily FMP budget cannot admit the {len(plan)}-request mission "
            f"before request #1 — refusing a partial panel"]
        return res

    # ---- acquisition + build (adjusted-bar mode) ------------------------
    acquirer = StrictLiveAcquirer(client, plan)
    provider = B.FMPDividendAdjustedProvider(acquirer)
    res.code_sha = code_sha or _git_sha(root)
    try:
        B.build(root, out_rel=staging_rel, code_sha=res.code_sha,
                generated_at=generated_at, bar_provider=provider,
                bar_clock=clock)
    except B.BuildError as exc:
        res.attempts = acquirer.attempts
        res.acquisition_order = [a["symbol"] for a in acquirer.attempts]
        res.result = FAIL_PROVIDER if acquirer.had_failure else FAIL_BUILD
        res.exact_blockers = [f"{type(exc).__name__}: {exc}"]
        _safe_rmtree(root / staging_rel)
        return res
    except Exception as exc:  # noqa: BLE001 — StrictLiveAcquisitionError,
        res.attempts = acquirer.attempts   # CallBudgetExceeded, FMPError, ...
        res.acquisition_order = [a["symbol"] for a in acquirer.attempts]
        res.result = FAIL_PROVIDER
        res.exact_blockers = [f"{type(exc).__name__}: {exc}"]
        _safe_rmtree(root / staging_rel)
        return res
    res.attempts = acquirer.attempts
    res.acquisition_order = [a["symbol"] for a in acquirer.attempts]

    # ---- mandatory independent validation -------------------------------
    try:
        validated = CON.validate(root / staging_rel)
    except CON.SnapshotInvalid as exc:
        res.consumer_validation = "FAIL"
        res.result = FAIL_PACKAGE_VALIDATION
        res.exact_blockers = [f"SnapshotInvalid: {exc}"]
        _safe_rmtree(root / staging_rel)
        return res
    res.consumer_validation = "PASS"
    res.package_id = validated.manifest.get("package_id")
    res.cohort_count = int(validated.manifest.get("non_overlapping_cohort_count") or 0)
    res.cohort_dates = list(validated.manifest.get("non_overlapping_cohort_dates") or [])

    # ---- readiness (assessment only; VS-002 is NOT executed) ------------
    readiness = R.evaluate(validated)
    res.readiness_status = readiness.status
    res.readiness_reasons = list(readiness.reasons)
    res.detail = dict(readiness.detail)

    # ---- publish the validated package (immutable, non-overwriting) -----
    final_rel = f"{out_root_rel}/packages/{res.package_id}"
    published, overwrote = _publish(root, staging_rel, final_rel)
    res.package_path = final_rel
    res.package_published = published
    res.package_overwrote_existing = overwrote

    # ---- classify -------------------------------------------------------
    if readiness.status == R.READY and not readiness.reasons \
            and (res.cohort_count or 0) >= C.MIN_COHORTS:
        res.result = PASS
    elif _only_cohort_deficit(readiness):
        res.result = NOT_READY
        res.exact_blockers = ["INSUFFICIENT_NON_OVERLAPPING_COHORTS"]
    else:
        res.result = FAIL_READINESS_CONTRACT
        res.exact_blockers = list(readiness.reasons)
    return res


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m portfolio_automation.vs002_evidence.production_runner",
        description="Governed production runner for the bounded VS-002 "
                    "historical-price evidence build. Acquires the frozen "
                    "22-symbol dividend-adjusted panel strict-live (one HTTP "
                    "attempt per symbol, no cache, no fallback), builds a fresh "
                    "immutable package, validates it, and assesses readiness. "
                    "It does NOT execute VS-002.")
    p.add_argument("--repo-root", required=True,
                   help="deployed release root, e.g. /opt/stockbot/current")
    p.add_argument("--run-id", default=None,
                   help="optional explicit run id for the staging directory")
    p.add_argument("--json", action="store_true",
                   help="print the machine-readable result to stdout")
    # Deliberately NO flags for symbols, benchmark, endpoint, MIN_COHORTS,
    # retries, cache/stale, provider, or synthetic mode: the frozen contract is
    # the only configuration.
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    res = run(args.repo_root, run_id=args.run_id)
    payload = res.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"result={res.result} readiness={res.readiness_status} "
              f"cohorts={res.cohort_count} package_id={res.package_id} "
              f"http_attempts={res.attempted_http_requests}")
        if res.exact_blockers:
            print("blockers: " + "; ".join(res.exact_blockers), file=sys.stderr)
    return res.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
