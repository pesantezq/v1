"""Research-side consumer for the frozen VS-002 evidence package.

THE RULE THIS MODULE EXISTS TO ENFORCE.

A file is not evidence because it arrived. The consumer recomputes every digest
before exposing a single row, and refuses a package whose contents, universe or
cutoff do not match its own manifest. That is the same discipline
``agent_export.validate_agent_snapshot`` applies, applied again on this side of
the transport -- because the transport is exactly where silent corruption or
substitution would occur, and a validator that trusts its input is decoration.

It is deliberately NOT a Research Store. It is an immutable snapshot reader: no
persistence, no query planner, no write path. If VS-002 later proves a store is
needed, that is a separate justified mission.

``experimental_noncanonical``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from portfolio_automation.vs002_evidence import contracts as C
from portfolio_automation.vs002_evidence.builder import (
    BARS_REL, MANIFEST_REL, RETURNS_REL, SIGNALS_REL,
    verify_adjustment_semantics)

EXPECTED_ARTIFACTS = frozenset({SIGNALS_REL, RETURNS_REL, MANIFEST_REL})


class SnapshotInvalid(ValueError):
    """Refusal to read a package that failed validation. Never a warning."""


@dataclass(frozen=True)
class ValidatedSnapshot:
    """Only constructible via :func:`validate`. Holding one means it passed."""

    root: Path
    manifest: dict[str, Any]
    signals: list[dict[str, Any]]
    returns: list[dict[str, Any]]
    bars: list[dict[str, Any]] = field(default_factory=list)
    _returns_index: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    # ---- deterministic access -------------------------------------------
    @property
    def eligible_universe(self) -> list[str]:
        return list(self.manifest["eligible_universe"])

    @property
    def cutoff(self) -> str:
        return str(self.manifest["signal_evidence_cutoff"])

    def signals_for(self, symbol: str) -> list[dict[str, Any]]:
        return [s for s in self.signals if s["ticker"] == symbol]

    def returns_for(self, symbol: str) -> list[dict[str, Any]]:
        """Chronological. Ordering is part of the contract, not incidental."""
        return [r for r in self.returns if r["symbol"] == symbol]

    # ---- the dividend-adjusted bar panel ----------------------------------
    @property
    def has_bars(self) -> bool:
        return bool(self.manifest.get("bar_endpoint"))

    @property
    def risk_free_rate_7d(self) -> dict[str, Any]:
        """The preregistered rf assumption. Read from the manifest so the
        result can never be described as having used an observed rate."""
        return dict(self.manifest.get("risk_free_rate_7d") or {})

    def bars_for(self, symbol: str) -> list[dict[str, Any]]:
        """Chronological, exactly as stored. Ordering is contract."""
        return [b for b in self.bars if b["symbol"] == symbol]

    def bar(self, symbol: str, session_date: str) -> Optional[dict[str, Any]]:
        """Exact (symbol, session_date) lookup. No forward fill, no nearest
        neighbour, no implicit calendar: an absent session is None, and
        whatever asked for it decides what absence means — never this reader."""
        for b in self.bars_for(symbol):
            if b["session_date"] == session_date:
                return b
        return None

    def bars_before(self, symbol: str, boundary_date: str) -> list[dict[str, Any]]:
        """Every bar session STRICTLY before the boundary — the structural
        anti-lookahead edge of the beta window, same strictness as
        :meth:`returns_before`."""
        return [b for b in self.bars_for(symbol)
                if b["session_date"] < boundary_date]

    def returns_before(self, symbol: str, boundary_date: str) -> list[dict[str, Any]]:
        """Every session STRICTLY before the boundary.

        Strict is the point: a session dated the same day as the signal is not
        admissible pre-signal evidence, and an off-by-one here is precisely the
        leak the whole experiment is built to avoid."""
        return [r for r in self.returns_for(symbol)
                if r["session_date"] < boundary_date]


def _digest_mismatch(name: str, expected: str, actual: str) -> str:
    return f"{name} digest mismatch: manifest {expected} != recomputed {actual}"


def validate(snapshot_dir: Path) -> ValidatedSnapshot:
    """Recompute everything. Raise on the first thing that does not reconcile."""
    root = Path(snapshot_dir)
    errors: list[str] = []

    if not (root / MANIFEST_REL).is_file():
        raise SnapshotInvalid(f"manifest absent: {root / MANIFEST_REL}")
    manifest = json.loads((root / MANIFEST_REL).read_text(encoding="utf-8"))

    # A package that declares the bar panel must carry it; one that does not
    # declare it must not smuggle it. The EXPECTED set is manifest-driven so
    # neither direction can pass silently.
    declares_bars = bool(manifest.get("bar_endpoint"))
    expected = set(EXPECTED_ARTIFACTS) | ({BARS_REL} if declares_bars else set())
    present = {p.name for p in root.iterdir() if p.is_file()}
    missing = sorted(expected - present)
    extra = sorted(present - expected)
    if missing:
        errors.append(f"missing artifact(s): {missing}")
    if extra:
        # An unexpected file is refused rather than ignored: a package with an
        # extra artifact is not the package whose digest was computed.
        errors.append(f"unexpected artifact(s): {extra}")
    if errors:
        raise SnapshotInvalid("; ".join(errors))

    signals = json.loads((root / SIGNALS_REL).read_text(encoding="utf-8"))
    returns = json.loads((root / RETURNS_REL).read_text(encoding="utf-8"))
    bars: list[dict[str, Any]] = []
    if declares_bars:
        bars = json.loads((root / BARS_REL).read_text(encoding="utf-8"))

    declared = manifest.get("artifact_digests") or {}
    artifact_pairs = [(SIGNALS_REL, signals), (RETURNS_REL, returns)]
    if declares_bars:
        artifact_pairs.append((BARS_REL, bars))
    for name, payload in artifact_pairs:
        actual = C.artifact_digest(payload)
        if declared.get(name) != actual:
            errors.append(_digest_mismatch(name, declared.get(name), actual))

    core = {k: v for k, v in manifest.items()
            if k not in ("generated_at", "package_id")}
    recomputed_id = C.package_id(core)
    if manifest.get("package_id") != recomputed_id:
        errors.append(
            f"package_id mismatch: manifest {manifest.get('package_id')} != "
            f"recomputed {recomputed_id}")

    if manifest.get("pit_certification") != C.PIT_CERTIFICATION:
        errors.append(
            f"missing or wrong PIT certification: {manifest.get('pit_certification')}")

    if manifest.get("schema_version") != C.SCHEMA_VERSION:
        errors.append(f"unsupported schema_version {manifest.get('schema_version')!r}")

    # Content must agree with what the manifest claims about it.
    if len(signals) != manifest.get("signal_row_count"):
        errors.append(
            f"signal row count {len(signals)} != manifest "
            f"{manifest.get('signal_row_count')}")
    if len(returns) != manifest.get("return_row_count"):
        errors.append(
            f"return row count {len(returns)} != manifest "
            f"{manifest.get('return_row_count')}")

    eligible = set(manifest.get("eligible_universe") or [])
    seen = {s["ticker"] for s in signals} - {C.BENCHMARK}
    if not seen <= eligible:
        errors.append(f"signals contain non-eligible symbols: {sorted(seen - eligible)}")

    cutoff = manifest.get("signal_evidence_cutoff")
    late = [s["signal_time"] for s in signals if s["signal_time"] > str(cutoff)]
    if late:
        errors.append(f"{len(late)} signal(s) exceed the declared cutoff {cutoff}")

    unmatured = [s for s in signals if s.get("outcome_return_7d") is None]
    if unmatured:
        errors.append(f"{len(unmatured)} unmatured signal(s) present")

    if declares_bars:
        # The bar panel's own contract, revalidated INDEPENDENTLY: endpoint
        # identity, counts, chronology, duplicates, and the same adjustment-
        # semantics battery the builder ran — recomputed here because the
        # transport is exactly where substitution would occur.
        if manifest.get("bar_endpoint") != C.AUTHORIZED_ENDPOINT:
            errors.append(
                f"bar panel declares endpoint {manifest.get('bar_endpoint')!r}, "
                f"not the authorized {C.AUTHORIZED_ENDPOINT!r}")
        if manifest.get("bar_source_id") != C.data_source_descriptor().source_id:
            errors.append("bar_source_id does not match the authorized "
                          "dividend-adjusted source descriptor")
        if len(bars) != manifest.get("bar_row_count"):
            errors.append(
                f"bar row count {len(bars)} != manifest "
                f"{manifest.get('bar_row_count')}")
        rf = manifest.get("risk_free_rate_7d") or {}
        if (rf.get("value") != C.RISK_FREE_RATE_7D_ASSUMPTION["value"]
                or rf.get("basis") != C.RISK_FREE_RATE_7D_ASSUMPTION["basis"]
                or rf.get("observed_evidence") is not False):
            errors.append(
                "risk_free_rate_7d is missing or is not the preregistered "
                "operator assumption — the result must never claim an "
                "observed risk-free rate")
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for b in bars:
            by_symbol.setdefault(str(b.get("symbol")), []).append(b)
        for sym, rows in sorted(by_symbol.items()):
            dates = [r["session_date"] for r in rows]
            if dates != sorted(dates):
                errors.append(f"{sym}: bar series is not chronologically ordered")
            if len(set(dates)) != len(dates):
                errors.append(f"{sym}: duplicate bar session dates")
            bar_rows = [C.BarRow(symbol=sym, session_date=r["session_date"],
                                 close=float(r["close"]),
                                 adj_close=float(r["adj_close"]),
                                 volume=int(r["volume"])) for r in rows]
            findings = verify_adjustment_semantics(
                bar_rows, is_benchmark=sym == C.BENCHMARK)
            errors.extend(findings)

    if errors:
        raise SnapshotInvalid("; ".join(errors))

    return ValidatedSnapshot(root=root, manifest=manifest,
                             signals=signals, returns=returns, bars=bars)
