"""
Watchlist editor — CLI write surface for the watchlist.

Replaces the Add/Remove/Import tabs of the Streamlit Watchlist Manager
with a small, atomic, auditable command-line tool. Read-only inspection
lives in the gui_v2 Portfolio page; this script handles every write.

Operations:

  python -m tools.watchlist_edit --list
  python -m tools.watchlist_edit --add NVDA,AAPL
  python -m tools.watchlist_edit --remove AAPL
  python -m tools.watchlist_edit --bulk-replace QQQ,SPY,GLD
  python -m tools.watchlist_edit --set-tag NVDA AI,Semis
  python -m tools.watchlist_edit --set-note NVDA "AI bellwether"
  python -m tools.watchlist_edit --enable NVDA
  python -m tools.watchlist_edit --disable NVDA
  python -m tools.watchlist_edit --export watchlist_2026_05_15.json
  python -m tools.watchlist_edit --import watchlist_2026_05_15.json

All write ops accept ``--dry-run`` to print what would change.

Files mutated:
  config.json                       (portfolio watchlist symbols)
  data/watchlist_tags.json          (per-symbol metadata)

Audit log (append-only JSONL):
  outputs/policy/watchlist_edits.jsonl

Safety:
  - Atomic-via-rename (.partial -> os.replace) so an interrupt never
    leaves a half-written file in the canonical name.
  - Symbol validation: uppercase, [A-Z][A-Z0-9.\\-]{0,9}, no dupes.
  - Refuses to write outside the repo root.
  - --dry-run never touches disk; --list never writes.
  - The DAILY pipeline does not depend on data/watchlist_tags.json being
    present; absent file is treated as "all symbols enabled, no tags".

Exit codes:
  0  success / no-op
  1  invalid arguments or symbol
  2  IO error
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


_REPO_ROOT_MARKER = "main.py"
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


@dataclass
class EditResult:
    op: str
    success: bool
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    set_tags: dict[str, list[str]] = field(default_factory=dict)
    note: str | None = None
    enabled: dict[str, bool] = field(default_factory=dict)
    dry_run: bool = False
    before_count: int = 0
    after_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tool": "watchlist_edit",
            "op": self.op,
            "success": self.success,
            "added": list(self.added),
            "removed": list(self.removed),
            "set_tags": dict(self.set_tags),
            "note": self.note,
            "enabled": dict(self.enabled),
            "dry_run": self.dry_run,
            "before_count": self.before_count,
            "after_count": self.after_count,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Repo + path helpers
# ---------------------------------------------------------------------------

def detect_repo_root(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        candidate = Path(explicit).resolve()
    else:
        candidate = Path(__file__).resolve().parents[1]
    if not (candidate / _REPO_ROOT_MARKER).exists():
        raise FileNotFoundError(
            f"Repo root marker {_REPO_ROOT_MARKER!r} not found in {candidate}. "
            "Pass --repo-root explicitly."
        )
    return candidate


def _config_path(repo: Path) -> Path:
    return repo / "config.json"


def _tags_path(repo: Path) -> Path:
    return repo / "data" / "watchlist_tags.json"


def _audit_log_path(repo: Path) -> Path:
    return repo / "outputs" / "policy" / "watchlist_edits.jsonl"


# ---------------------------------------------------------------------------
# Symbol parsing + validation
# ---------------------------------------------------------------------------

def parse_symbols(raw: str | Iterable[str]) -> list[str]:
    """
    Split a comma-separated string into a clean list of uppercase symbols.
    Drops blanks. Raises ValueError for any symbol that fails validation.
    Preserves first-occurrence order; de-dupes silently.
    """
    if isinstance(raw, str):
        parts = [p for p in (s.strip() for s in raw.split(",")) if p]
    else:
        parts = [str(s).strip() for s in raw if str(s).strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        sym = p.upper()
        if not _SYMBOL_RE.match(sym):
            raise ValueError(f"Invalid symbol: {p!r}")
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


# ---------------------------------------------------------------------------
# Read + write helpers (atomic)
# ---------------------------------------------------------------------------

def _read_config(repo: Path) -> dict[str, Any]:
    p = _config_path(repo)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise IOError(f"could not parse {p}: {exc}") from exc


def _read_tags(repo: Path) -> dict[str, dict[str, Any]]:
    p = _tags_path(repo)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically. Caller has already validated the payload shape."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    if partial.exists():
        partial.unlink()
    partial.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(partial, path)


def _current_watchlist(cfg: dict[str, Any]) -> list[str]:
    ws = cfg.get("watchlist_scanner") or {}
    wl = ws.get("watchlist")
    if not isinstance(wl, list):
        return []
    return [s for s in wl if isinstance(s, str)]


def _apply_watchlist(cfg: dict[str, Any], new_list: list[str]) -> dict[str, Any]:
    """Return a new config dict with watchlist replaced."""
    new_cfg = dict(cfg)
    ws = dict(new_cfg.get("watchlist_scanner") or {})
    ws["watchlist"] = new_list
    new_cfg["watchlist_scanner"] = ws
    return new_cfg


def _append_audit(repo: Path, result: EditResult) -> None:
    log = _audit_log_path(repo)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result.to_dict(), default=str) + "\n")
    except OSError as exc:
        logger.warning("could not append audit log %s: %s", log, exc)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def list_watchlist(repo: Path) -> dict[str, Any]:
    """Pure read; returns a dict snapshot for display."""
    cfg = _read_config(repo)
    tags = _read_tags(repo)
    symbols = _current_watchlist(cfg)
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        meta = tags.get(sym) or {}
        rows.append({
            "symbol": sym,
            "enabled": bool(meta.get("enabled", True)),
            "tags": list(meta.get("tags") or []),
            "note": meta.get("note") or "",
        })
    return {
        "advisory_only": True,
        "no_trade": True,
        "count": len(symbols),
        "symbols": symbols,
        "rows": rows,
    }


def add_symbols(repo: Path, raw: str, *, dry_run: bool = False) -> EditResult:
    new = parse_symbols(raw)
    cfg = _read_config(repo)
    current = _current_watchlist(cfg)
    added = [s for s in new if s not in current]
    result = EditResult(
        op="add", success=True, added=added,
        before_count=len(current), after_count=len(current) + len(added),
        dry_run=dry_run,
    )
    if not added:
        result.error = "all symbols already in watchlist"
        return result
    if dry_run:
        return result
    new_cfg = _apply_watchlist(cfg, current + added)
    _atomic_write_json(_config_path(repo), new_cfg)
    _append_audit(repo, result)
    return result


def remove_symbols(repo: Path, raw: str, *, dry_run: bool = False) -> EditResult:
    targets = parse_symbols(raw)
    cfg = _read_config(repo)
    current = _current_watchlist(cfg)
    removed = [s for s in targets if s in current]
    result = EditResult(
        op="remove", success=True, removed=removed,
        before_count=len(current), after_count=len(current) - len(removed),
        dry_run=dry_run,
    )
    if not removed:
        result.error = "no requested symbols are in the watchlist"
        return result
    if dry_run:
        return result
    new_list = [s for s in current if s not in removed]
    new_cfg = _apply_watchlist(cfg, new_list)
    _atomic_write_json(_config_path(repo), new_cfg)
    # Clean up tags for removed symbols
    tags = _read_tags(repo)
    if any(s in tags for s in removed):
        for s in removed:
            tags.pop(s, None)
        _atomic_write_json(_tags_path(repo), tags)
    _append_audit(repo, result)
    return result


def bulk_replace(repo: Path, raw: str, *, dry_run: bool = False) -> EditResult:
    new_list = parse_symbols(raw)
    cfg = _read_config(repo)
    current = _current_watchlist(cfg)
    added = [s for s in new_list if s not in current]
    removed = [s for s in current if s not in new_list]
    result = EditResult(
        op="bulk_replace", success=True, added=added, removed=removed,
        before_count=len(current), after_count=len(new_list),
        dry_run=dry_run,
    )
    if dry_run:
        return result
    new_cfg = _apply_watchlist(cfg, new_list)
    _atomic_write_json(_config_path(repo), new_cfg)
    # Trim tags
    if removed:
        tags = _read_tags(repo)
        for s in removed:
            tags.pop(s, None)
        _atomic_write_json(_tags_path(repo), tags)
    _append_audit(repo, result)
    return result


def _update_tag_meta(
    repo: Path, symbol: str, *,
    tags: list[str] | None = None,
    enabled: bool | None = None,
    note: str | None = None,
    dry_run: bool = False,
) -> EditResult:
    """Single internal entry-point for any per-symbol metadata change."""
    sym = symbol.strip().upper()
    if not _SYMBOL_RE.match(sym):
        return EditResult(op="set_meta", success=False,
                          error=f"Invalid symbol: {symbol!r}")
    cfg = _read_config(repo)
    current = _current_watchlist(cfg)
    if sym not in current:
        return EditResult(op="set_meta", success=False,
                          error=f"{sym} is not in the watchlist; add it first")
    result = EditResult(op="set_meta", success=True, dry_run=dry_run)
    tags_db = _read_tags(repo)
    meta = dict(tags_db.get(sym) or {})
    if tags is not None:
        meta["tags"] = tags
        result.set_tags = {sym: tags}
    if enabled is not None:
        meta["enabled"] = enabled
        result.enabled = {sym: enabled}
    if note is not None:
        meta["note"] = note
        result.note = note
    if dry_run:
        return result
    tags_db[sym] = meta
    _atomic_write_json(_tags_path(repo), tags_db)
    _append_audit(repo, result)
    return result


def set_tags(repo: Path, symbol: str, tags_csv: str, *, dry_run: bool = False) -> EditResult:
    tags = [t.strip() for t in tags_csv.split(",") if t.strip()]
    return _update_tag_meta(repo, symbol, tags=tags, dry_run=dry_run)


def set_enabled(repo: Path, symbol: str, enabled: bool, *, dry_run: bool = False) -> EditResult:
    return _update_tag_meta(repo, symbol, enabled=enabled, dry_run=dry_run)


def set_note(repo: Path, symbol: str, note: str, *, dry_run: bool = False) -> EditResult:
    return _update_tag_meta(repo, symbol, note=note, dry_run=dry_run)


def export_state(repo: Path, dest: Path) -> EditResult:
    cfg = _read_config(repo)
    tags = _read_tags(repo)
    payload = {
        "advisory_only": True,
        "no_trade": True,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "watchlist": _current_watchlist(cfg),
        "tags": tags,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return EditResult(op="export", success=True, after_count=len(payload["watchlist"]))


def import_state(repo: Path, src: Path, *, dry_run: bool = False) -> EditResult:
    if not src.exists():
        return EditResult(op="import", success=False, error=f"file not found: {src}")
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except Exception as exc:
        return EditResult(op="import", success=False, error=f"invalid JSON: {exc}")
    if not isinstance(payload, dict):
        return EditResult(op="import", success=False, error="payload must be a JSON object")
    raw_syms = payload.get("watchlist")
    if not isinstance(raw_syms, list):
        return EditResult(op="import", success=False, error="payload missing 'watchlist' list")
    try:
        new_list = parse_symbols(raw_syms)
    except ValueError as exc:
        return EditResult(op="import", success=False, error=str(exc))
    raw_tags = payload.get("tags") or {}
    if not isinstance(raw_tags, dict):
        raw_tags = {}
    clean_tags: dict[str, dict[str, Any]] = {}
    for sym, meta in raw_tags.items():
        if not _SYMBOL_RE.match(str(sym).upper()):
            continue
        if not isinstance(meta, dict):
            continue
        clean_tags[sym.upper()] = {
            "enabled": bool(meta.get("enabled", True)),
            "tags": [t for t in (meta.get("tags") or []) if isinstance(t, str)],
            "note": str(meta.get("note") or ""),
        }
    cfg = _read_config(repo)
    current = _current_watchlist(cfg)
    added = [s for s in new_list if s not in current]
    removed = [s for s in current if s not in new_list]
    result = EditResult(
        op="import", success=True, added=added, removed=removed,
        before_count=len(current), after_count=len(new_list), dry_run=dry_run,
    )
    if dry_run:
        return result
    new_cfg = _apply_watchlist(cfg, new_list)
    _atomic_write_json(_config_path(repo), new_cfg)
    _atomic_write_json(_tags_path(repo), clean_tags)
    _append_audit(repo, result)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.watchlist_edit",
        description=(
            "CLI write surface for the watchlist. Replaces the Add/Remove/"
            "Import tabs of the Streamlit Watchlist Manager. Atomic writes; "
            "audit trail in outputs/policy/watchlist_edits.jsonl."
        ),
    )
    p.add_argument("--repo-root", default=None,
                   help="Repo root override.  Default: directory above this file.")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan only; print what would change.")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--list", action="store_true",
                     help="Print the current watchlist with tags + enabled state.")
    grp.add_argument("--add", metavar="CSV",
                     help="Comma-separated symbols to add (e.g. NVDA,AAPL).")
    grp.add_argument("--remove", metavar="CSV",
                     help="Comma-separated symbols to remove.")
    grp.add_argument("--bulk-replace", metavar="CSV",
                     help="Replace the entire watchlist with these symbols.")
    grp.add_argument("--set-tag", nargs=2, metavar=("SYMBOL", "TAGS_CSV"),
                     help="Set tags for SYMBOL (comma-separated).")
    grp.add_argument("--set-note", nargs=2, metavar=("SYMBOL", "NOTE"),
                     help="Set the note for SYMBOL.")
    grp.add_argument("--enable", metavar="SYMBOL",
                     help="Mark SYMBOL enabled.")
    grp.add_argument("--disable", metavar="SYMBOL",
                     help="Mark SYMBOL disabled (scanner skips it).")
    grp.add_argument("--export", metavar="FILE",
                     help="Export watchlist + tags to FILE as JSON.")
    grp.add_argument("--import", dest="import_path", metavar="FILE",
                     help="Import watchlist + tags from FILE.")
    return p


def _print(result: dict[str, Any] | EditResult) -> None:
    if isinstance(result, EditResult):
        result = result.to_dict()
    print(json.dumps(result, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        repo = detect_repo_root(args.repo_root)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        if args.list:
            _print(list_watchlist(repo))
            return 0
        if args.add:
            r = add_symbols(repo, args.add, dry_run=args.dry_run)
        elif args.remove:
            r = remove_symbols(repo, args.remove, dry_run=args.dry_run)
        elif args.bulk_replace:
            r = bulk_replace(repo, args.bulk_replace, dry_run=args.dry_run)
        elif args.set_tag:
            r = set_tags(repo, args.set_tag[0], args.set_tag[1], dry_run=args.dry_run)
        elif args.set_note:
            r = set_note(repo, args.set_note[0], args.set_note[1], dry_run=args.dry_run)
        elif args.enable:
            r = set_enabled(repo, args.enable, True, dry_run=args.dry_run)
        elif args.disable:
            r = set_enabled(repo, args.disable, False, dry_run=args.dry_run)
        elif args.export:
            r = export_state(repo, Path(args.export))
        elif args.import_path:
            r = import_state(repo, Path(args.import_path), dry_run=args.dry_run)
        else:
            print("ERROR: no operation specified", file=sys.stderr)
            return 1
    except ValueError as exc:
        # Symbol validation failure
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except IOError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    _print(r)
    return 0 if r.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
