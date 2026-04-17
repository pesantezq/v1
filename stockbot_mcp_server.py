"""
StockBot MCP Server

Exposes the portfolio automation tool as a safe, local MCP server for Claude Code.
Runnable via:  py -m stockbot_mcp_server

Tools
-----
  doctor()                                   — environment health check
  run(mode, no_email, no_api, dry_run)       — execute main.py safely
  latest_summary()                           — parse outputs/latest/ into brief text
  tail_log(lines=80)                         — last N lines of newest log file

Safety
------
  - Windows named-mutex + asyncio.Lock prevent overlapping run() calls
  - Any "apikey=…" substring is redacted before returning output
  - API keys are never echoed, not even in doctor()
  - Only py main.py is ever invoked — no arbitrary shell access
"""

import asyncio
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import mcp.types as types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
except ImportError:
    sys.exit(
        "ERROR: 'mcp' package not installed.\n"
        "Run:  pip install mcp\n"
        "Then retry:  py -m stockbot_mcp_server"
    )

# Repo root is the directory that contains this file
ROOT = Path(__file__).parent.resolve()

# Load .env at startup so env-var checks and subprocesses inherit secrets
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass  # python-dotenv not installed; env vars must be set another way

# ── Secret redaction ──────────────────────────────────────────────────────────
_SECRET_RE = re.compile(
    r'((?:apikey|api_key|password|passwd|token|secret)=[^\s&"\'<>\n]+)',
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    """Replace apikey=…, password=…, token=…, secret=… values with <REDACTED>."""
    return _SECRET_RE.sub(lambda m: m.group(0).split("=")[0] + "=<REDACTED>", text)


# ── Windows named-mutex (cross-process run lock) ──────────────────────────────
_MUTEX_NAME = "Global\\StockBotMCPRun"
_mutex_handle = None


def _acquire_mutex() -> bool:
    """Try to acquire the Windows named mutex. Returns False if already held."""
    global _mutex_handle
    if sys.platform != "win32":
        return True  # Non-Windows: rely on asyncio.Lock only
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            if handle:
                kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception:
        return True  # If ctypes fails, asyncio.Lock is still in effect


def _release_mutex() -> None:
    """Release and close the Windows named mutex."""
    global _mutex_handle
    if sys.platform != "win32" or _mutex_handle is None:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.ReleaseMutex(_mutex_handle)
        kernel32.CloseHandle(_mutex_handle)
    except Exception:
        pass
    finally:
        _mutex_handle = None


# Within-process async lock (prevents concurrent run() calls in same event loop)
_run_lock = asyncio.Lock()

# ── MCP Server ────────────────────────────────────────────────────────────────
server = Server("stockbot-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="doctor",
            description=(
                "Health-check for the StockBot environment. Creates missing "
                "directories, verifies config.json exists, checks that core "
                "Python modules are importable, and reports whether required "
                "env vars are set — never echoes their values."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="run",
            description=(
                "Execute py main.py with chosen options. "
                "Stdout/stderr are captured to logs/<mode>_run_<timestamp>.log. "
                "Returns {exit_code, skipped, ran_command, log_file, latest_outputs_files}. "
                "Overlapping calls are blocked by a Windows named mutex; "
                "if locked, returns {skipped: true} immediately."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly"],
                        "default": "daily",
                        "description": "Run mode passed as --run-mode.",
                    },
                    "no_email": {
                        "type": "boolean",
                        "default": True,
                        "description": "Pass --skip-email to suppress email sending.",
                    },
                    "no_api": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Reserved for future --no-api flag. "
                            "Acknowledged but not forwarded — main.py does not yet "
                            "implement --no-api."
                        ),
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": True,
                        "description": "Pass --dry-run to skip file writes and email.",
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="latest_summary",
            description=(
                "Read outputs/latest/ and return a brief textual summary: "
                "portfolio value, drawdown %, top 5 drift positions, sleeve %, "
                "and top 5 scanner candidates (if candidates_top20.csv exists). "
                "If files are missing, returns a helpful message."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="tail_log",
            description=(
                "Return the last N lines of the most recently modified "
                "YYYY-MM-DD.log (or mode_run_*.log) file in logs/."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "default": 80,
                        "description": "Number of tail lines to return (default 80).",
                    }
                },
                "required": [],
            },
        ),
    ]


@server.call_tool()
async def call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent]:
    args = arguments or {}
    if name == "doctor":
        return await _tool_doctor()
    if name == "run":
        return await _tool_run(args)
    if name == "latest_summary":
        return await _tool_latest_summary()
    if name == "tail_log":
        return await _tool_tail_log(args)
    return _text_result(f"Unknown tool: {name}")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _text_result(content: str) -> list[types.TextContent]:
    """Wrap redacted text as an MCP TextContent list."""
    return [types.TextContent(type="text", text=_redact(content))]


def _json_result(obj: dict) -> list[types.TextContent]:
    """Serialise a dict to redacted JSON TextContent."""
    return _text_result(json.dumps(obj, indent=2))


def _newest_log() -> Path | None:
    """Return the most-recently-modified log file in logs/, or None."""
    log_dir = ROOT / "logs"
    if not log_dir.exists():
        return None
    log_pat = re.compile(r'(\d{4}-\d{2}-\d{2}\.log|.+_run_\d{8}_\d{6}\.log)$')
    candidates = [
        f for f in log_dir.iterdir()
        if f.is_file() and log_pat.search(f.name)
    ]
    return max(candidates, key=lambda f: f.stat().st_mtime) if candidates else None


def _read_csv_safe(path: Path) -> list[dict]:
    """Read a UTF-8-BOM CSV; return [] on any error."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _col(row: dict, *candidates: str) -> str | None:
    """Return the first key that matches any candidate (case-insensitive)."""
    lower = {k.lower(): k for k in row}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None


# ── Tool: doctor ──────────────────────────────────────────────────────────────

async def _tool_doctor() -> list[types.TextContent]:
    status: dict = {
        "dirs": {},
        "config_json": None,
        "python_imports": None,
        "env_vars": {},
        "state_files": {},
        "overall": "ok",
    }

    # 1. Directories — create if missing
    required_dirs = ("outputs", "outputs/latest", "logs", "data")
    for d in required_dirs:
        p = ROOT / d
        if p.exists():
            status["dirs"][d] = "exists"
        else:
            p.mkdir(parents=True, exist_ok=True)
            status["dirs"][d] = "created"

    # 2. config.json
    cfg = ROOT / "config.json"
    if cfg.exists():
        status["config_json"] = f"ok ({cfg.stat().st_size:,} bytes)"
    else:
        status["config_json"] = "MISSING"
        status["overall"] = "error"

    # 3. Python imports
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import importlib
        for mod in ("utils", "market_data", "portfolio", "main"):
            importlib.import_module(mod)
        status["python_imports"] = "ok"
    except Exception as exc:
        status["python_imports"] = f"FAIL: {exc}"
        status["overall"] = "error"

    # 4. Environment variables — presence only, never values
    required_vars = ("FMP_API_KEY", "ALPHA_VANTAGE_API_KEY")
    optional_vars = ("EMAIL_PASSWORD", "EMAIL_SENDER", "EMAIL_RECIPIENT")

    for var in required_vars:
        if os.environ.get(var):
            status["env_vars"][var] = "set"
        else:
            status["env_vars"][var] = "NOT SET"
            if status["overall"] == "ok":
                status["overall"] = "warn"

    for var in optional_vars:
        status["env_vars"][var] = "set" if os.environ.get(var) else "not set (optional)"

    # 5. Persistent state files
    for rel in (
        "data/portfolio.db",
        "data/drawdown_state.json",
        "data/last_success.json",
        "data/fmp_cache/top100_watchlist.json",
    ):
        p = ROOT / rel
        status["state_files"][rel] = "exists" if p.exists() else "not yet created"

    return _json_result(status)


# ── Tool: run ─────────────────────────────────────────────────────────────────

async def _tool_run(args: dict) -> list[types.TextContent]:
    mode = args.get("mode", "daily")
    no_email = bool(args.get("no_email", True))
    dry_run = bool(args.get("dry_run", True))
    no_api = bool(args.get("no_api", False))

    if mode not in ("daily", "weekly", "monthly"):
        return _json_result({
            "exit_code": None,
            "skipped": True,
            "reason": f"Invalid mode '{mode}'. Must be daily, weekly, or monthly.",
        })

    # ── Within-process lock (asyncio) ─────────────────────────────────────────
    if _run_lock.locked():
        return _json_result({
            "exit_code": None,
            "skipped": True,
            "reason": "run() is already executing in this session.",
        })

    async with _run_lock:
        # ── Cross-process lock (Windows named mutex) ──────────────────────────
        if not _acquire_mutex():
            return _json_result({
                "exit_code": None,
                "skipped": True,
                "reason": "Another StockBot process is already running main.py.",
            })
        try:
            return await _do_run(mode, no_email, dry_run, no_api)
        finally:
            _release_mutex()


async def _do_run(
    mode: str, no_email: bool, dry_run: bool, no_api: bool
) -> list[types.TextContent]:
    """Build command, launch subprocess, capture to log, return summary."""

    # Build command — only pass flags main.py actually supports
    # Flags that DO exist:    --run-mode, --config, --skip-email, --dry-run
    # Flags that DO NOT exist: --output, --no-api  (noted below)
    cmd = [
        sys.executable,
        str(ROOT / "main.py"),
        "--run-mode", mode,
        "--config", "config.json",
    ]
    if no_email:
        cmd.append("--skip-email")   # main.py uses --skip-email, not --no-email
    if dry_run:
        cmd.append("--dry-run")

    notes: list[str] = []
    if no_api:
        notes.append("no_api=True requested but --no-api is not yet implemented in main.py; ignored.")

    # Log file for this run
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{mode}_run_{ts}.log"

    # Write log header
    with open(log_file, "w", encoding="utf-8") as lf:
        lf.write("# StockBot MCP run\n")
        lf.write(f"# Command : {_redact(' '.join(cmd))}\n")
        lf.write(f"# Started : {datetime.now().isoformat()}\n")
        for n in notes:
            lf.write(f"# Note    : {n}\n")
        lf.write("=" * 60 + "\n\n")

    # Force UTF-8 in the subprocess (avoids charmap crash on Windows)
    sub_env = os.environ.copy()
    sub_env["PYTHONIOENCODING"] = "utf-8"
    sub_env["PYTHONUTF8"] = "1"

    # Run in a thread so blocking subprocess.run doesn't stall the event loop.
    # asyncio.create_subprocess_exec with a file handle is unreliable on
    # Windows ProactorEventLoop — subprocess.run is simpler and always works.
    def _blocking_run() -> int:
        try:
            with open(log_file, "a", encoding="utf-8") as lf:
                result = subprocess.run(
                    cmd,
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    env=sub_env,
                    timeout=300,
                )
                lf.write(f"\n{'=' * 60}\n")
                lf.write(f"# Exit code : {result.returncode}\n")
                lf.write(f"# Finished  : {datetime.now().isoformat()}\n")
                return result.returncode
        except subprocess.TimeoutExpired:
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write("\n[TIMEOUT] Process killed after 300 s\n")
            return -1
        except Exception as exc:
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write(f"\n[ERROR] {_redact(str(exc))}\n")
            return -1

    try:
        returncode = await asyncio.to_thread(_blocking_run)
    except Exception as exc:
        return _json_result({
            "exit_code": -1,
            "skipped": False,
            "ran_command": _redact(" ".join(cmd)),
            "log_file": str(log_file.relative_to(ROOT)).replace("\\", "/"),
            "latest_outputs_files": [],
            "error": _redact(str(exc)),
        })

    # Collect outputs/latest/ file list
    latest_dir = ROOT / "outputs" / "latest"
    output_files: list[str] = (
        sorted(f.name for f in latest_dir.iterdir() if f.is_file())
        if latest_dir.exists() else []
    )

    result: dict = {
        "exit_code": returncode,
        "skipped": False,
        "ran_command": _redact(" ".join(cmd)),
        "log_file": str(log_file.relative_to(ROOT)).replace("\\", "/"),
        "latest_outputs_files": output_files,
    }
    if notes:
        result["notes"] = notes

    return _json_result(result)


# ── Tool: latest_summary ──────────────────────────────────────────────────────

async def _tool_latest_summary() -> list[types.TextContent]:
    latest_dir = ROOT / "outputs" / "latest"

    if not latest_dir.exists() or not any(latest_dir.iterdir()):
        return _text_result(
            "outputs/latest/ is empty or does not exist.\n"
            "Run the portfolio tool first:\n"
            "  run(mode='daily', dry_run=False, no_email=True)"
        )

    parts: list[str] = ["Latest Portfolio Summary", "=" * 54]
    files_present = sorted(f.name for f in latest_dir.iterdir() if f.is_file())
    parts.append(f"Files: {', '.join(files_present)}\n")

    # ── Portfolio value & drawdown % ─────────────────────────────────────────
    dd_path = ROOT / "data" / "drawdown_state.json"
    if dd_path.exists():
        try:
            dd = json.loads(dd_path.read_text(encoding="utf-8"))
            drawdown_pct = dd.get("drawdown_from_12m_high", dd.get("drawdown_pct", None))
            ath = dd.get("all_time_high", "?")
            current = dd.get("current_value", "?")
            regime = dd.get("regime", "?")
            parts.append("── Drawdown State ──────────────────────────────────────")
            parts.append(f"  Current Value      : {current}")
            parts.append(f"  All-Time High      : {ath}")
            if drawdown_pct is not None:
                parts.append(f"  Drawdown (12m high): {float(drawdown_pct):.2%}")
            parts.append(f"  Regime             : {regime}")
        except Exception:
            pass

    # ── Holdings snapshot — top 5 by absolute drift ──────────────────────────
    snapshot = latest_dir / "portfolio_snapshot.csv"
    if snapshot.exists():
        rows = _read_csv_safe(snapshot)
        if rows:
            parts.append("\n── Holdings Snapshot ───────────────────────────────────")
            sym_k  = _col(rows[0], "symbol", "Symbol", "ticker")
            drift_k = _col(rows[0], "drift", "Drift", "drift_pct", "drift_percent")
            weight_k = _col(rows[0], "current_weight", "CurrentWeight", "actual_weight", "weight")
            val_k  = _col(rows[0], "current_value", "CurrentValue", "value", "market_value")

            # Total row
            total = next(
                (r for r in rows if sym_k and r.get(sym_k, "").upper() in ("TOTAL", "")),
                None,
            )
            if total and val_k:
                parts.append(f"  Portfolio Value : {total.get(val_k, '?')}")

            holding_rows = [
                r for r in rows
                if sym_k
                and r.get(sym_k, "").strip()
                and r.get(sym_k, "").upper() not in ("TOTAL", "CASH")
            ]

            if drift_k and holding_rows:
                def _abs_drift(r: dict) -> float:
                    try:
                        return abs(float(r[drift_k]))
                    except Exception:
                        return 0.0

                top5 = sorted(holding_rows, key=_abs_drift, reverse=True)[:5]
                parts.append("  Top 5 by absolute drift:")
                for r in top5:
                    sym   = r.get(sym_k, "?")   if sym_k   else "?"
                    drift = r.get(drift_k, "?")
                    wt    = r.get(weight_k, "?") if weight_k else "?"
                    vl    = r.get(val_k, "?")    if val_k   else "?"
                    parts.append(f"    {sym:<8}  drift={drift:>8}  weight={wt:>7}  value={vl}")
    else:
        parts.append("\nportfolio_snapshot.csv: not found")

    # ── Speculative sleeve % ─────────────────────────────────────────────────
    sleeve_path = latest_dir / "spec_sleeve_plan.csv"
    if sleeve_path.exists():
        rows = _read_csv_safe(sleeve_path)
        if rows:
            parts.append("\n── Speculative Sleeve Plan ─────────────────────────────")
            sym_k    = _col(rows[0], "symbol", "Symbol")
            dollars_k = _col(rows[0], "MaxAddDollars", "max_add_dollars", "dollars")
            new_k    = _col(rows[0], "IsNewPosition", "is_new_position", "new")
            for r in rows:
                sym     = r.get(sym_k,     "?") if sym_k     else "?"
                dollars = r.get(dollars_k, "?") if dollars_k else "?"
                is_new  = r.get(new_k,     "?") if new_k     else "?"
                parts.append(f"  {sym:<8}  add=${dollars}  new_position={is_new}")
    else:
        parts.append("\nspec_sleeve_plan.csv: not present (scanner/sleeve disabled or dry-run)")

    # ── Contribution plan ────────────────────────────────────────────────────
    contrib = latest_dir / "contribution_plan.csv"
    if contrib.exists():
        rows = _read_csv_safe(contrib)
        if rows:
            parts.append("\n── Contribution Plan (top 5) ───────────────────────────")
            sym_k    = _col(rows[0], "symbol", "Symbol")
            dol_k    = _col(rows[0], "RecommendedContributionDollars", "recommended_dollars", "dollars")
            drift_k  = _col(rows[0], "drift", "Drift")
            reason_k = _col(rows[0], "reason", "Reason")
            for r in rows[:5]:
                sym    = r.get(sym_k,    "?") if sym_k    else "?"
                dol    = r.get(dol_k,    "?") if dol_k    else "?"
                drift  = r.get(drift_k,  "?") if drift_k  else "?"
                reason = r.get(reason_k, "")  if reason_k else ""
                parts.append(f"  {sym:<8}  ${dol:>9}  drift={drift:>8}  {reason}")
    else:
        parts.append("\ncontribution_plan.csv: not present (growth mode off or dry-run)")

    # ── Scanner top-5 candidates ─────────────────────────────────────────────
    cands_path = latest_dir / "candidates_top20.csv"
    if cands_path.exists():
        rows = _read_csv_safe(cands_path)
        if rows:
            parts.append("\n── Scanner Candidates (top 5) ──────────────────────────")
            sym_k      = _col(rows[0], "symbol", "Symbol")
            score_k    = _col(rows[0], "score", "Score")
            sector_k   = _col(rows[0], "sector", "Sector")
            rev_k      = _col(rows[0], "rev_growth", "RevGrowth")
            reasons_k  = _col(rows[0], "reasons", "Reasons")
            for r in rows[:5]:
                sym     = r.get(sym_k,     "?") if sym_k     else "?"
                score   = r.get(score_k,   "?") if score_k   else "?"
                sector  = r.get(sector_k,  "?") if sector_k  else "?"
                rev     = r.get(rev_k,     "?") if rev_k     else "?"
                reasons = r.get(reasons_k, "")  if reasons_k else ""
                parts.append(f"  {sym:<8}  score={score:>5}  rev={rev:>6}  {sector}  {reasons}")
    else:
        parts.append("\ncandidates_top20.csv: not present (scanner disabled or dry-run)")

    # ── Compounding dashboard ────────────────────────────────────────────────
    dash = latest_dir / "compounding_dashboard.txt"
    if dash.exists():
        parts.append("\n── Compounding Dashboard ───────────────────────────────")
        parts.append(dash.read_text(encoding="utf-8"))

    # ── Last successful run heartbeat ────────────────────────────────────────
    hb_path = ROOT / "data" / "last_success.json"
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            parts.append("\n── Last Successful Run ─────────────────────────────────")
            for k, v in hb.items():
                parts.append(f"  {k}: {v}")
        except Exception:
            pass

    return _text_result("\n".join(parts))


# ── Tool: tail_log ────────────────────────────────────────────────────────────

async def _tool_tail_log(args: dict) -> list[types.TextContent]:
    n = max(1, int(args.get("lines", 80)))
    log_path = _newest_log()
    if log_path is None:
        return _text_result(
            "No log files found in logs/.\n"
            "Run the portfolio tool first:  run(mode='daily')"
        )
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        tail = all_lines[-n:]
        header = f"[Log: {log_path.name} | last {len(tail)} of {len(all_lines)} lines]\n"
        return _text_result(header + "\n".join(tail))
    except Exception as exc:
        return _text_result(f"ERROR reading {log_path.name}: {exc}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def _amain() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(_amain())
