"""
Manual Portfolio Update — operator-driven, no-GUI workflow.
============================================================

Updates the operator's current holdings and cash in ``config.json`` from a
simple CSV (or JSON) input.  This is the **only** sanctioned path for
mutating official portfolio state outside the daily pipeline's price-refresh
step, and it is gated by run-mode governance:

  - Hard-coded ``RunMode.MANUAL_UPDATE``
  - Requires the explicit ``--approve`` CLI flag
  - Delegates the approval check to
    ``assert_can_update_portfolio_state(mode, approved=True)`` so the
    existing governance layer is the single source of truth

Safety invariants (hardcoded):
  - observe_only: true
  - no_trade: true
  - not_recommendation: true
  - no_allocation_policy_change: true
  - no_watchlist_mutation: true
  - no_discovery_promotion: true
  - No broker/API calls
  - No LLM/AI calls
  - Only writes:
      * ``config.json`` (only ``portfolio.holdings`` and
        ``portfolio.cash_available`` are touched; all other keys preserved)
      * ``outputs/policy/portfolio_backups/config.<YYYYMMDD_HHMMSS>.json``
        (pre-update backup)
      * ``outputs/policy/manual_portfolio_updates.jsonl`` (append-only audit)

CLI::

    python -m tools.manual_portfolio_update \\
        --input inputs/manual_portfolio_update.csv \\
        --cash 464.16 \\
        --as-of 2026-05-12 \\
        --approve

Required CSV header: ``symbol,shares``.
Optional columns: ``target_weight``, ``asset_class``, ``is_leveraged``,
``leverage_factor``.  Any other column is rejected.

For existing symbols, missing optional columns preserve the prior value.
For new symbols, missing optional columns get conservative defaults
(``target_weight=0``, ``asset_class="us_equity"``, ``is_leveraged=False``,
``leverage_factor=1``).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
)
from portfolio_automation.run_mode_governance import (
    RunMode,
    RunModeViolation,
    assert_can_update_portfolio_state,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_LABEL = "manual_portfolio_update"

_REQUIRED_COLUMNS: frozenset[str] = frozenset({"symbol", "shares"})
_OPTIONAL_COLUMNS: frozenset[str] = frozenset({
    "target_weight", "asset_class", "is_leveraged", "leverage_factor",
})
_ALLOWED_COLUMNS: frozenset[str] = _REQUIRED_COLUMNS | _OPTIONAL_COLUMNS

# Symbol regex: 1-10 chars, starts with uppercase letter, allows letters,
# digits, '.', '-'.  Matches typical US/international ticker conventions.
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_BACKUP_SUBDIR = "portfolio_backups"
_BACKUP_PATH_TEMPLATE = "portfolio_backups/config.{ts}.json"
_AUDIT_JSONL_RELATIVE = "manual_portfolio_updates.jsonl"

_SAFETY_DISCLAIMER = (
    "Manual operator update of holdings and cash. "
    "No trade executed, no broker API call, no recommendation emitted. "
    "Allocation policy, scoring, watchlist, discovery, and recommendations "
    "are not modified by this workflow."
)

_DEFAULT_NEW_SYMBOL_FIELDS: dict[str, Any] = {
    "target_weight": 0.0,
    "asset_class": "us_equity",
    "is_leveraged": False,
    "leverage_factor": 1,
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ManualPortfolioUpdateError(ValueError):
    """Raised when the input or runtime state is invalid for an update."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ParsedHolding:
    symbol: str
    shares: float
    # Optional fields; None means "preserve prior value or use default"
    target_weight: float | None = None
    asset_class: str | None = None
    is_leveraged: bool | None = None
    leverage_factor: int | None = None


@dataclass
class UpdateDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)
    unchanged_count: int = 0
    prior_cash: float = 0.0
    new_cash: float = 0.0
    cash_delta: float = 0.0
    prior_holdings_count: int = 0
    new_holdings_count: int = 0


# ---------------------------------------------------------------------------
# CSV / JSON input parsing
# ---------------------------------------------------------------------------

def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n", ""):
        return False
    raise ManualPortfolioUpdateError(f"Cannot parse boolean from {value!r}")


def parse_holdings_csv(csv_path: Path) -> list[ParsedHolding]:
    """
    Parse a holdings CSV.

    Required columns: symbol, shares.
    Optional columns: target_weight, asset_class, is_leveraged, leverage_factor.
    Any other column rejects the file.
    """
    if not csv_path.exists():
        raise ManualPortfolioUpdateError(f"Input file not found: {csv_path}")

    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ManualPortfolioUpdateError(
                f"Input file has no header row: {csv_path}"
            )
        header = {h.strip() for h in reader.fieldnames if h is not None}
        missing = _REQUIRED_COLUMNS - header
        if missing:
            raise ManualPortfolioUpdateError(
                f"Missing required column(s): {sorted(missing)}"
            )
        unknown = header - _ALLOWED_COLUMNS
        if unknown:
            raise ManualPortfolioUpdateError(
                f"Unsupported column(s): {sorted(unknown)}. "
                f"Allowed: {sorted(_ALLOWED_COLUMNS)}"
            )

        rows: list[dict[str, str]] = []
        for raw in reader:
            # Skip fully empty rows
            if not raw or all((v is None or str(v).strip() == "") for v in raw.values()):
                continue
            rows.append(raw)

    if not rows:
        raise ManualPortfolioUpdateError("Input file contains no data rows.")

    return _rows_to_holdings(rows)


def _rows_to_holdings(rows: list[dict[str, str]]) -> list[ParsedHolding]:
    seen: set[str] = set()
    holdings: list[ParsedHolding] = []
    for line_idx, raw in enumerate(rows, start=2):  # +1 for header, +1 for 1-based
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            raise ManualPortfolioUpdateError(
                f"Row {line_idx}: empty symbol"
            )
        if not _SYMBOL_PATTERN.match(symbol):
            raise ManualPortfolioUpdateError(
                f"Row {line_idx}: invalid symbol {symbol!r}. "
                "Expected 1-10 uppercase letters/digits, may include '.' or '-'."
            )
        if symbol in seen:
            raise ManualPortfolioUpdateError(
                f"Row {line_idx}: duplicate symbol {symbol!r}"
            )
        seen.add(symbol)

        shares_raw = str(raw.get("shares") or "").strip()
        if not shares_raw:
            raise ManualPortfolioUpdateError(
                f"Row {line_idx}: missing shares for {symbol}"
            )
        try:
            shares = float(shares_raw)
        except ValueError as exc:
            raise ManualPortfolioUpdateError(
                f"Row {line_idx}: shares must be numeric, got {shares_raw!r}"
            ) from exc
        if shares < 0:
            raise ManualPortfolioUpdateError(
                f"Row {line_idx}: shares must be non-negative for {symbol}, got {shares}"
            )

        holding = ParsedHolding(symbol=symbol, shares=shares)

        # Optional columns
        if "target_weight" in raw and str(raw["target_weight"]).strip() != "":
            try:
                tw = float(raw["target_weight"])
            except ValueError as exc:
                raise ManualPortfolioUpdateError(
                    f"Row {line_idx}: target_weight must be numeric for {symbol}"
                ) from exc
            if not (0.0 <= tw <= 1.0):
                raise ManualPortfolioUpdateError(
                    f"Row {line_idx}: target_weight must be in [0, 1] for {symbol}, "
                    f"got {tw}"
                )
            holding.target_weight = tw

        if "asset_class" in raw and str(raw["asset_class"]).strip() != "":
            holding.asset_class = str(raw["asset_class"]).strip()

        if "is_leveraged" in raw and str(raw["is_leveraged"]).strip() != "":
            holding.is_leveraged = _parse_bool(raw["is_leveraged"])

        if "leverage_factor" in raw and str(raw["leverage_factor"]).strip() != "":
            try:
                lf = int(raw["leverage_factor"])
            except ValueError as exc:
                raise ManualPortfolioUpdateError(
                    f"Row {line_idx}: leverage_factor must be an integer for {symbol}"
                ) from exc
            if lf < 1:
                raise ManualPortfolioUpdateError(
                    f"Row {line_idx}: leverage_factor must be >= 1 for {symbol}, "
                    f"got {lf}"
                )
            holding.leverage_factor = lf

        holdings.append(holding)
    return holdings


def parse_holdings_json(json_path: Path) -> list[ParsedHolding]:
    """
    Parse a holdings JSON file as a thin alternative to CSV.

    Expected shape::

        {
          "holdings": [
            {"symbol": "QQQ", "shares": 6},
            {"symbol": "GLD", "shares": 4}
          ]
        }

    Same validation rules as CSV.
    """
    if not json_path.exists():
        raise ManualPortfolioUpdateError(f"Input file not found: {json_path}")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManualPortfolioUpdateError(f"Invalid JSON in {json_path}: {exc}") from exc
    if not isinstance(payload, dict) or "holdings" not in payload:
        raise ManualPortfolioUpdateError(
            "JSON input must be an object with a top-level 'holdings' array."
        )
    items = payload["holdings"]
    if not isinstance(items, list):
        raise ManualPortfolioUpdateError("'holdings' must be a list.")
    rows: list[dict[str, str]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ManualPortfolioUpdateError(
                f"holdings[{i}] is not an object."
            )
        for key in item:
            if key not in _ALLOWED_COLUMNS:
                raise ManualPortfolioUpdateError(
                    f"holdings[{i}]: unsupported field {key!r}. "
                    f"Allowed: {sorted(_ALLOWED_COLUMNS)}"
                )
        rows.append({k: ("" if v is None else str(v)) for k, v in item.items()})
    return _rows_to_holdings(rows)


def _parse_as_of(value: str) -> str:
    """Parse and re-emit an as_of date as ISO YYYY-MM-DD."""
    s = str(value).strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ManualPortfolioUpdateError(
            f"--as-of must be YYYY-MM-DD, got {value!r}"
        ) from exc


def _parse_cash(value: str | float) -> float:
    try:
        cash = float(value)
    except (TypeError, ValueError) as exc:
        raise ManualPortfolioUpdateError(
            f"--cash must be numeric, got {value!r}"
        ) from exc
    if cash < 0:
        raise ManualPortfolioUpdateError(
            f"--cash must be non-negative, got {cash}"
        )
    return cash


# ---------------------------------------------------------------------------
# Config load / merge
# ---------------------------------------------------------------------------

def _load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise ManualPortfolioUpdateError(f"config not found: {config_path}")
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManualPortfolioUpdateError(
            f"config is not valid JSON: {config_path} ({exc})"
        ) from exc
    if not isinstance(payload, dict):
        raise ManualPortfolioUpdateError(
            f"config root must be a JSON object: {config_path}"
        )
    return payload


def _merge_holdings(
    prior_holdings: list[dict[str, Any]],
    new_holdings: list[ParsedHolding],
) -> tuple[list[dict[str, Any]], UpdateDiff]:
    """
    Merge ``new_holdings`` over ``prior_holdings`` preserving existing
    metadata for known symbols and applying conservative defaults for new
    symbols.

    Returns the merged list (preserving the input ordering of ``new_holdings``)
    and an :class:`UpdateDiff`.
    """
    prior_by_symbol: dict[str, dict[str, Any]] = {}
    for h in prior_holdings:
        if not isinstance(h, dict):
            continue
        sym = str(h.get("symbol") or "").strip().upper()
        if sym:
            prior_by_symbol[sym] = h

    diff = UpdateDiff(
        prior_holdings_count=len(prior_by_symbol),
        new_holdings_count=len(new_holdings),
    )
    merged: list[dict[str, Any]] = []
    new_symbols: set[str] = set()

    for nh in new_holdings:
        new_symbols.add(nh.symbol)
        if nh.symbol in prior_by_symbol:
            base = dict(prior_by_symbol[nh.symbol])
            prior_shares = float(base.get("shares") or 0)
            base["shares"] = float(nh.shares)
            if nh.target_weight is not None:
                base["target_weight"] = nh.target_weight
            if nh.asset_class is not None:
                base["asset_class"] = nh.asset_class
            if nh.is_leveraged is not None:
                base["is_leveraged"] = nh.is_leveraged
            if nh.leverage_factor is not None:
                base["leverage_factor"] = nh.leverage_factor
            merged.append(base)
            if abs(prior_shares - float(nh.shares)) > 1e-9:
                diff.changed.append({
                    "symbol": nh.symbol,
                    "prior_shares": prior_shares,
                    "new_shares": float(nh.shares),
                    "delta": float(nh.shares) - prior_shares,
                })
            else:
                diff.unchanged_count += 1
        else:
            new_entry: dict[str, Any] = {
                "symbol": nh.symbol,
                "shares": float(nh.shares),
                "target_weight": nh.target_weight if nh.target_weight is not None
                                  else _DEFAULT_NEW_SYMBOL_FIELDS["target_weight"],
                "asset_class": nh.asset_class if nh.asset_class is not None
                                else _DEFAULT_NEW_SYMBOL_FIELDS["asset_class"],
                "is_leveraged": nh.is_leveraged if nh.is_leveraged is not None
                                 else _DEFAULT_NEW_SYMBOL_FIELDS["is_leveraged"],
                "leverage_factor": nh.leverage_factor if nh.leverage_factor is not None
                                    else _DEFAULT_NEW_SYMBOL_FIELDS["leverage_factor"],
            }
            merged.append(new_entry)
            diff.added.append(nh.symbol)

    # Removed = prior symbols not present in the new set
    for sym in prior_by_symbol:
        if sym not in new_symbols:
            diff.removed.append(sym)

    return merged, diff


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON to *path* (write to temp, then os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup; let the original exception propagate
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Backup + audit JSONL append
# ---------------------------------------------------------------------------

def _outputs_root(project_root: Path) -> Path:
    """Return the outputs/ root from a project root path.

    data_governance helpers treat ``base_dir`` as the namespace root
    (``outputs/``), but this CLI's ``--base-dir`` is the project root that
    *contains* ``outputs/``.  This helper bridges the two.
    """
    return Path(project_root) / "outputs"


def _write_backup(
    config_payload: dict[str, Any],
    project_root: Path,
    timestamp: str,
) -> Path:
    relative = _BACKUP_PATH_TEMPLATE.format(ts=timestamp)
    path = safe_write_json(
        OutputNamespace.POLICY,
        relative,
        config_payload,
        base_dir=_outputs_root(project_root),
    )
    return Path(path)


def _append_audit_record(
    record: dict[str, Any],
    project_root: Path,
) -> Path:
    """Append a single JSONL line to outputs/policy/manual_portfolio_updates.jsonl."""
    path = get_output_path(
        OutputNamespace.POLICY, _AUDIT_JSONL_RELATIVE,
        base_dir=_outputs_root(project_root),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return path


# ---------------------------------------------------------------------------
# Orchestrator (pure function — easy to test)
# ---------------------------------------------------------------------------

@dataclass
class ManualUpdateResult:
    success: bool
    diff: UpdateDiff
    config_path: Path
    backup_path: Path | None
    audit_path: Path | None
    audit_record: dict[str, Any]
    dry_run: bool


def run_manual_portfolio_update(
    *,
    input_path: Path,
    cash: float,
    as_of: str,
    approved: bool,
    config_path: Path,
    base_dir: Path | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
) -> ManualUpdateResult:
    """
    Core, testable update orchestrator.

    Parameters
    ----------
    input_path:
        Path to CSV or JSON input.  File extension determines parser
        (``.json`` → JSON, otherwise CSV).
    cash:
        Pre-validated cash amount (non-negative float).
    as_of:
        Pre-validated ISO date string (YYYY-MM-DD).
    approved:
        Must be True for any write to occur.  Delegated to
        ``assert_can_update_portfolio_state``.
    config_path:
        Path to ``config.json``.
    base_dir:
        Output root (parent of ``outputs/``).  Defaults to ``config_path.parent``.
    run_id:
        Optional run identifier; defaults to timestamp-based.
    dry_run:
        If True, validate and compute the diff but skip all writes.

    Returns
    -------
    ManualUpdateResult
    """
    base = Path(base_dir) if base_dir is not None else Path(config_path).parent
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    iso_ts = datetime.now(timezone.utc).isoformat()
    _run_id = run_id or f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}_manual_portfolio_update_{timestamp}"

    # Run-mode governance — hardcoded MANUAL_UPDATE + approval delegation.
    # Raises RunModeViolation if approved is False.
    assert_can_update_portfolio_state(RunMode.MANUAL_UPDATE, approved=approved)

    # Parse input
    suffix = input_path.suffix.lower()
    if suffix == ".json":
        parsed = parse_holdings_json(input_path)
    else:
        parsed = parse_holdings_csv(input_path)

    # Load current config
    config = _load_config(config_path)
    portfolio = config.get("portfolio") if isinstance(config.get("portfolio"), dict) else {}
    prior_holdings = portfolio.get("holdings") if isinstance(portfolio.get("holdings"), list) else []
    prior_cash = float(portfolio.get("cash_available") or 0.0)

    merged_holdings, diff = _merge_holdings(prior_holdings, parsed)
    diff.prior_cash = prior_cash
    diff.new_cash = cash
    diff.cash_delta = cash - prior_cash

    # Build audit record (always produced, even in dry_run)
    audit_record: dict[str, Any] = {
        "run_id": _run_id,
        "timestamp": iso_ts,
        "as_of": as_of,
        "mode": RunMode.MANUAL_UPDATE.value,
        "approved": approved,
        "dry_run": dry_run,
        "source_input_path": str(input_path),
        "config_path": str(config_path),
        "prior_cash": prior_cash,
        "new_cash": cash,
        "cash_delta": diff.cash_delta,
        "prior_holdings_count": diff.prior_holdings_count,
        "new_holdings_count": diff.new_holdings_count,
        "added": list(diff.added),
        "removed": list(diff.removed),
        "changed": list(diff.changed),
        "unchanged_count": diff.unchanged_count,
        # Safety flags — hardcoded
        "observe_only": True,
        "no_trade": True,
        "not_recommendation": True,
        "no_allocation_policy_change": True,
        "no_watchlist_mutation": True,
        "no_discovery_promotion": True,
        "source": _SOURCE_LABEL,
        "safety_disclaimer": _SAFETY_DISCLAIMER,
    }

    if dry_run:
        return ManualUpdateResult(
            success=True,
            diff=diff,
            config_path=config_path,
            backup_path=None,
            audit_path=None,
            audit_record=audit_record,
            dry_run=True,
        )

    # 1) Backup the pre-update config
    backup_path = _write_backup(config, base, timestamp)
    audit_record["backup_path"] = str(backup_path)

    # 2) Update the config in place — only portfolio.holdings + cash
    new_portfolio = dict(portfolio)
    new_portfolio["holdings"] = merged_holdings
    new_portfolio["cash_available"] = float(cash)
    new_config = dict(config)
    new_config["portfolio"] = new_portfolio
    _atomic_write_json(config_path, new_config)

    # 3) Append audit JSONL
    audit_path = _append_audit_record(audit_record, base)
    audit_record["audit_path"] = str(audit_path)

    return ManualUpdateResult(
        success=True,
        diff=diff,
        config_path=config_path,
        backup_path=backup_path,
        audit_path=audit_path,
        audit_record=audit_record,
        dry_run=False,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_diff_summary(result: ManualUpdateResult) -> str:
    d = result.diff
    lines = [
        f"Cash:    {d.prior_cash:>12.2f} → {d.new_cash:>12.2f}  (Δ {d.cash_delta:+.2f})",
        f"Symbols: prior={d.prior_holdings_count}  new={d.new_holdings_count}",
    ]
    if d.added:
        lines.append(f"  Added:     {', '.join(d.added)}")
    if d.removed:
        lines.append(f"  Removed:   {', '.join(d.removed)}")
    if d.changed:
        lines.append("  Changed:")
        for ch in d.changed:
            lines.append(
                f"    {ch['symbol']:<8} {ch['prior_shares']:>10.3f} → "
                f"{ch['new_shares']:>10.3f}  (Δ {ch['delta']:+.3f})"
            )
    if d.unchanged_count:
        lines.append(f"  Unchanged: {d.unchanged_count}")
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.manual_portfolio_update",
        description=(
            "Manually update holdings/cash in config.json from a CSV/JSON "
            "file.  Observe-only system — never trades.  Requires --approve."
        ),
    )
    p.add_argument("--input", required=True, type=Path,
                   help="Path to CSV (preferred) or JSON input file.")
    p.add_argument("--cash", required=True,
                   help="New cash_available value (non-negative number).")
    p.add_argument("--as-of", required=True, dest="as_of",
                   help="As-of date in YYYY-MM-DD format.")
    p.add_argument("--approve", action="store_true",
                   help="Required explicit approval flag.  Without this, no "
                        "writes occur and the tool exits with an error.")
    p.add_argument("--config", type=Path, default=Path("config.json"),
                   help="Path to config.json (default: ./config.json).")
    p.add_argument("--base-dir", type=Path, default=None,
                   help="Output root containing outputs/ (default: config dir).")
    p.add_argument("--run-id", default=None,
                   help="Optional run identifier (default: timestamp-based).")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate and print the diff without writing anything.")
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint; returns process exit code."""
    args = _build_arg_parser().parse_args(argv)

    if not args.approve:
        print(
            "ERROR: --approve is required.  Manual portfolio updates require "
            "explicit operator approval (run mode is hardcoded to "
            "manual_update; see run_mode_governance.py).",
            file=sys.stderr,
        )
        return 2

    try:
        cash = _parse_cash(args.cash)
        as_of = _parse_as_of(args.as_of)
    except ManualPortfolioUpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        result = run_manual_portfolio_update(
            input_path=args.input,
            cash=cash,
            as_of=as_of,
            approved=True,
            config_path=args.config,
            base_dir=args.base_dir,
            run_id=args.run_id,
            dry_run=args.dry_run,
        )
    except (ManualPortfolioUpdateError, RunModeViolation) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print("Manual portfolio update — summary")
    print("---------------------------------")
    print(_format_diff_summary(result))
    print()
    if result.dry_run:
        print("DRY RUN — no files were written.")
    else:
        print(f"Config updated:  {result.config_path}")
        print(f"Backup written:  {result.backup_path}")
        print(f"Audit appended:  {result.audit_path}")
    print()
    print(_SAFETY_DISCLAIMER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
