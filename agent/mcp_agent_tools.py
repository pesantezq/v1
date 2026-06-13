"""
StockBot — AI Agent MCP Testing Tools
======================================

A standalone MCP server that exposes safe testing and observability tools
for the AI agent layer (memos, LLM connectivity, guardrail validation,
patch-approval gating, scheduler readiness).

Server name : stockbot-agent-mcp
Runnable via: py -m agent.mcp_agent_tools

Safety contract
---------------
- Never exposes secret values (API keys, passwords)
- Never calls FMP endpoints
- Never modifies config.json or investment allocations
- Never imports investment-logic modules (adjustment, scoring, portfolio, etc.)
- All reads and writes stay inside the repo root
- Ollama and Claude calls are optional; graceful fallback if unavailable

Tools
-----
  agent_health_check()          — env dirs, db, env-var presence
  test_ollama_connection()      — ping localhost:11434
  test_claude_connection()      — minimal Anthropic API probe
  simulate_agent_run(mode)      — synthesise bundle → LLM → test_decision_memo.md
  validate_guardrails()         — cap checks from config + data files (no investment imports)
  verify_agent_outputs()        — existence check for expected agent artifacts
  test_patch_approval_flow()    — approved_actions.json gate check
  scheduler_readiness_check()   — run-lock, db, dir write-test, last_success freshness

----
AI Agent MCP Testing Tools — Quick-start README
----

Enable server in .mcp.json:

  "agent-mcp": {
    "command": "py",
    "args": ["-m", "agent.mcp_agent_tools"]
  }

Install dependency (same as stockbot-mcp):

  pip install mcp

Optional LLM environment variables:

  OLLAMA_MODEL        default: gemma3:4b
  ANTHROPIC_API_KEY   required for test_claude_connection / Claude fallback
  ANTHROPIC_MODEL     default: claude-haiku-4-5-20251001

Example usage from Claude Code:

  agent_health_check()
    → structured JSON: status ok/warning/error + per-check details

  test_ollama_connection()
    → {"ollama_status": "ok", "latency_ms": 312, "model_used": "gemma3:4b"}

  simulate_agent_run(mode="daily")
    → generates outputs/latest/test_decision_memo.md using Ollama (or Claude fallback)
    → returns {"memo_created": true, "model_used": "ollama", "output_path": "..."}
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from portfolio_automation.env import get_secret

try:
    import mcp.types as types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
except ImportError:
    sys.exit(
        "ERROR: 'mcp' package not installed.\n"
        "Run:  pip install mcp\n"
        "Then retry:  py -m agent.mcp_agent_tools"
    )

# Repo root is two levels up from this file (agent/mcp_agent_tools.py → v1/)
ROOT = Path(__file__).parent.parent.resolve()

# Load .env so ANTHROPIC_API_KEY and FMP_API_KEY are available
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ── Secret redaction (same pattern as stockbot_mcp_server) ───────────────────
_SECRET_RE = re.compile(
    r'((?:apikey|api_key|password|passwd|token|secret)=[^\s&"\'<>\n]+)',
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: m.group(0).split("=")[0] + "=<REDACTED>", text)


def _text_result(content: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=_redact(content))]


def _json_result(obj: dict) -> list[types.TextContent]:
    return _text_result(json.dumps(obj, indent=2, default=str))


# ── LLM config helpers ────────────────────────────────────────────────────────

def _ollama_model() -> str:
    return os.environ.get("OLLAMA_MODEL", "gemma3:4b")


def _claude_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


# ── MCP Server ────────────────────────────────────────────────────────────────
server = Server("stockbot-agent-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="agent_health_check",
            description=(
                "Verify that the AI agent environment is healthy. "
                "Checks: required directories, agent_bundle.json, SQLite state file, "
                "and whether required env vars are set (FMP_API_KEY, ANTHROPIC_API_KEY). "
                "Never returns secret values."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="test_ollama_connection",
            description=(
                "Verify that the local Ollama LLM runtime is reachable at "
                "localhost:11434. Sends a minimal generate request and measures latency. "
                "Returns {ollama_status, latency_ms, model_used}."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="test_claude_connection",
            description=(
                "Verify that the Anthropic Claude API is reachable and authenticated. "
                "Sends a single-token probe using the configured model. "
                "Returns {claude_status, model, latency_ms}. Never prints API keys."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="simulate_agent_run",
            description=(
                "Simulate the AI agent pipeline for the given run mode without "
                "triggering real investment actions. Loads (or synthesises) "
                "outputs/latest/agent_bundle.json, generates a test memo via "
                "Ollama (or Claude fallback), and writes "
                "outputs/latest/test_decision_memo.md. "
                "Returns {memo_created, model_used, output_path}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly"],
                        "default": "daily",
                        "description": "Simulated run mode (affects memo framing only).",
                    }
                },
                "required": [],
            },
        ),
        types.Tool(
            name="validate_guardrails",
            description=(
                "Test the guardrail system against live data files. "
                "Reads config.json + drawdown_state.json + price_cache.json "
                "to check: concentration cap, leverage cap, anti-panic sleeve block, "
                "and sleeve allocation limits. Does NOT import investment-logic modules. "
                "Returns {guardrails_pass, violations, drawdown_pct, portfolio_value}."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="verify_agent_outputs",
            description=(
                "Confirm that the AI agent generated its expected artifacts. "
                "Checks for: decision_memo.md, monthly_memo.md, email_draft.md, "
                "escalation_packet.md in the repo root. "
                "Returns {files_found, missing_files}."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="test_patch_approval_flow",
            description=(
                "Verify maintainer-agent safety gating. Checks whether "
                "approved_actions.json exists and whether the patch-generation "
                "path is blocked or open. "
                "Returns {approval_required, approved, patch_generation_allowed}."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="scheduler_readiness_check",
            description=(
                "Verify the system can safely run unattended. Checks: run-lock "
                "mechanism (stale lock detection), SQLite state store is readable, "
                "cache directories are writable, and last_success.json freshness. "
                "Returns {ready_for_scheduler, issues}."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@server.call_tool()
async def call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent]:
    args = arguments or {}
    if name == "agent_health_check":
        return await _tool_agent_health_check()
    if name == "test_ollama_connection":
        return await _tool_test_ollama_connection()
    if name == "test_claude_connection":
        return await _tool_test_claude_connection()
    if name == "simulate_agent_run":
        return await _tool_simulate_agent_run(args)
    if name == "validate_guardrails":
        return await _tool_validate_guardrails()
    if name == "verify_agent_outputs":
        return await _tool_verify_agent_outputs()
    if name == "test_patch_approval_flow":
        return await _tool_test_patch_approval_flow()
    if name == "scheduler_readiness_check":
        return await _tool_scheduler_readiness_check()
    return _text_result(f"Unknown tool: {name}")


# ── Tool: agent_health_check ──────────────────────────────────────────────────

async def _tool_agent_health_check() -> list[types.TextContent]:
    checks: list[dict] = []
    overall = "ok"

    def _chk(name: str, status: str, detail: str = "") -> None:
        nonlocal overall
        checks.append({"name": name, "status": status, "detail": detail})
        if status == "error" and overall != "error":
            overall = "error"
        elif status == "warning" and overall == "ok":
            overall = "warning"

    # 1. Required directories
    required_dirs = [
        "outputs/latest",
        "outputs/history",
        "logs",
        "data",
        "agent",
        "data/fmp_cache",
    ]
    for d in required_dirs:
        p = ROOT / d
        if p.exists():
            _chk(f"dir:{d}", "ok")
        else:
            try:
                p.mkdir(parents=True, exist_ok=True)
                _chk(f"dir:{d}", "warning", "created (was missing)")
            except Exception as exc:
                _chk(f"dir:{d}", "error", str(exc))

    # 2. agent_bundle.json (outputs/latest/agent_bundle.json)
    bundle_path = ROOT / "outputs" / "latest" / "agent_bundle.json"
    if bundle_path.exists():
        try:
            size = bundle_path.stat().st_size
            _chk("agent_bundle.json", "ok", f"{size:,} bytes")
        except Exception as exc:
            _chk("agent_bundle.json", "error", str(exc))
    else:
        _chk(
            "agent_bundle.json",
            "warning",
            "not found — simulate_agent_run will synthesise from data files",
        )

    # 3. SQLite state store
    db_path = ROOT / "data" / "portfolio.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            conn.close()
            _chk(
                "data/portfolio.db",
                "ok",
                f"tables: {[t[0] for t in tables]}",
            )
        except Exception as exc:
            _chk("data/portfolio.db", "error", str(exc))
    else:
        _chk("data/portfolio.db", "warning", "not yet created (created on first live run)")

    # 4. Supporting data files
    data_files = {
        "data/drawdown_state.json": "required",
        "data/price_cache.json": "required",
        "data/finance_history.json": "optional",
        "data/last_success.json": "optional",
    }
    for rel, importance in data_files.items():
        p = ROOT / rel
        if p.exists():
            age_h = (time.time() - p.stat().st_mtime) / 3600
            _chk(rel, "ok", f"age: {age_h:.1f}h")
        elif importance == "required":
            _chk(rel, "error", "missing")
        else:
            _chk(rel, "warning", "not yet created")

    # 5. Environment variables — presence only, never values
    env_required = ("FMP_API_KEY", "ANTHROPIC_API_KEY")
    env_optional = ("OLLAMA_MODEL", "ANTHROPIC_MODEL", "EMAIL_PASSWORD")
    for var in env_required:
        if os.environ.get(var):
            _chk(f"env:{var}", "ok", "set")
        else:
            _chk(f"env:{var}", "warning", "NOT SET")
    for var in env_optional:
        val = os.environ.get(var)
        _chk(f"env:{var}", "ok", f"set ({val})" if var == "OLLAMA_MODEL" and val else ("set" if val else "not set (optional)"))

    # 6. mcp package importable
    try:
        import importlib
        importlib.import_module("mcp")
        _chk("mcp_package", "ok", "importable")
    except Exception as exc:
        _chk("mcp_package", "error", str(exc))

    return _json_result({"status": overall, "checks": checks})


# ── Tool: test_ollama_connection ──────────────────────────────────────────────

def _do_ollama_ping(model: str) -> dict:
    """Blocking Ollama request — run via asyncio.to_thread."""
    payload = json.dumps({
        "model": model,
        "prompt": "Respond with the single word: OK",
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        latency_ms = int((time.monotonic() - t0) * 1000)
        response_text = body.get("response", "").strip()
        return {
            "ollama_status": "ok",
            "latency_ms": latency_ms,
            "model_used": model,
            "response": response_text,
        }
    except urllib.error.URLError as exc:
        return {
            "ollama_status": "error",
            "latency_ms": None,
            "model_used": model,
            "error": f"Connection failed: {exc.reason}",
        }
    except Exception as exc:
        return {
            "ollama_status": "error",
            "latency_ms": None,
            "model_used": model,
            "error": str(exc),
        }


async def _tool_test_ollama_connection() -> list[types.TextContent]:
    model = _ollama_model()
    result = await asyncio.to_thread(_do_ollama_ping, model)
    return _json_result(result)


# ── Tool: test_claude_connection ──────────────────────────────────────────────

def _do_claude_ping(model: str, api_key: str) -> dict:
    """Blocking Claude API probe — run via asyncio.to_thread."""
    try:
        import anthropic
    except ImportError:
        return {
            "claude_status": "error",
            "model": model,
            "latency_ms": None,
            "error": "anthropic package not installed — run: pip install anthropic",
        }
    t0 = time.monotonic()
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=5,
            messages=[{"role": "user", "content": "Respond with the single word: OK"}],
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        response_text = msg.content[0].text.strip() if msg.content else ""
        return {
            "claude_status": "ok",
            "model": model,
            "latency_ms": latency_ms,
            "response": response_text,
        }
    except Exception as exc:
        return {
            "claude_status": "error",
            "model": model,
            "latency_ms": None,
            "error": _redact(str(exc)),
        }


async def _tool_test_claude_connection() -> list[types.TextContent]:
    api_key = get_secret("ANTHROPIC_API_KEY") or ""
    if not api_key:
        return _json_result({
            "claude_status": "error",
            "model": _claude_model(),
            "latency_ms": None,
            "error": "ANTHROPIC_API_KEY not set in environment",
        })
    model = _claude_model()
    result = await asyncio.to_thread(_do_claude_ping, model, api_key)
    return _json_result(result)


# ── Tool: simulate_agent_run ──────────────────────────────────────────────────

def _load_or_synthesise_bundle(mode: str) -> dict:
    """Load agent_bundle.json, or synthesise one from available data files."""
    bundle_path = ROOT / "outputs" / "latest" / "agent_bundle.json"
    if bundle_path.exists():
        try:
            return json.loads(bundle_path.read_text(encoding="utf-8"))
        except Exception:
            pass  # fall through to synthesis

    bundle: dict = {
        "synthetic": True,
        "mode": mode,
        "generated_at": datetime.now().isoformat(),
    }

    # drawdown_state.json
    dd_path = ROOT / "data" / "drawdown_state.json"
    if dd_path.exists():
        try:
            bundle["drawdown"] = json.loads(dd_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Latest finance_history entry
    fh_path = ROOT / "data" / "finance_history.json"
    if fh_path.exists():
        try:
            history = json.loads(fh_path.read_text(encoding="utf-8"))
            if history:
                bundle["latest_snapshot"] = history[-1]
        except Exception:
            pass

    # Price cache (without exposing keys)
    pc_path = ROOT / "data" / "price_cache.json"
    if pc_path.exists():
        try:
            bundle["prices"] = json.loads(pc_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Config summary — caps and basic settings only
    cfg_path = ROOT / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            portfolio = cfg.get("portfolio", {})
            growth = cfg.get("growth_mode", {})
            bundle["config_summary"] = {
                "investor_name": cfg.get("investor", {}).get("name", "unknown"),
                "holdings": [
                    {
                        "symbol": h["symbol"],
                        "shares": h.get("shares", 0),
                        "target_weight": h.get("target_weight", 0),
                        "is_leveraged": h.get("is_leveraged", False),
                        "leverage_factor": h.get("leverage_factor", 1),
                    }
                    for h in portfolio.get("holdings", [])
                ],
                "cash_available": portfolio.get("cash_available", 0),
                "monthly_contribution": portfolio.get("monthly_contribution", 0),
                "concentration_cap": growth.get("concentration_cap", 0.40),
                "leverage_cap": growth.get("leverage_cap", 0.15),
                "growth_mode": growth.get("mode", "unknown"),
                "scanner_enabled": cfg.get("scanner", {}).get("enabled", False),
                "sleeve_enabled": cfg.get("speculative_sleeve", {}).get("enabled", False),
            }
        except Exception:
            pass

    return bundle


def _build_memo_prompt(bundle: dict, mode: str) -> str:
    """Build a concise LLM prompt from the bundle. Keeps total tokens low."""
    snapshot = bundle.get("latest_snapshot", {})
    dd = bundle.get("drawdown", {})
    cfg = bundle.get("config_summary", {})
    prices = bundle.get("prices", {})

    # Calculate approximate portfolio value from prices + holdings
    portfolio_value = dd.get("current_value") or snapshot.get("portfolio_value", "unknown")
    ath = dd.get("all_time_high", "unknown")
    drifts = snapshot.get("drifts_by_symbol", {})
    worst_drift = max(drifts.items(), key=lambda x: abs(x[1]), default=("?", 0)) if drifts else ("?", 0)

    data_summary = (
        f"Run mode: {mode}\n"
        f"Portfolio value: ${portfolio_value:,.2f}\n" if isinstance(portfolio_value, (int, float))
        else f"Run mode: {mode}\nPortfolio value: {portfolio_value}\n"
    )
    data_summary += (
        f"All-time high: ${ath:,.2f}\n" if isinstance(ath, (int, float))
        else f"All-time high: {ath}\n"
    )
    data_summary += f"Max drift position: {worst_drift[0]} at {worst_drift[1]:+.1%}\n" if isinstance(worst_drift[1], float) else ""
    data_summary += f"Growth mode: {cfg.get('growth_mode', 'unknown')}\n"
    data_summary += f"Monthly contribution: ${cfg.get('monthly_contribution', 0):,}\n"
    data_summary += f"Concentration cap: {cfg.get('concentration_cap', 0.4):.0%}\n"
    data_summary += f"Leverage cap: {cfg.get('leverage_cap', 0.15):.0%}\n"
    data_summary += f"Scanner enabled: {cfg.get('scanner_enabled', False)}\n"
    data_summary += f"Current prices: {json.dumps({k: v.get('price') for k, v in prices.items()}, indent=None)}\n"

    return (
        "You are a portfolio monitoring assistant for a rules-based long-term investing system.\n"
        "Review the following portfolio data and write a brief TEST decision memo with:\n"
        "- 3 bullet executive summary\n"
        "- 1 key action item\n"
        "- A note that this is a TEST memo generated by the AI agent test harness\n\n"
        f"PORTFOLIO DATA:\n{data_summary}\n"
        "Output only the memo text, no additional commentary."
    )


def _do_ollama_generate(model: str, prompt: str) -> tuple[str, int]:
    """Call Ollama /api/generate. Returns (response_text, latency_ms)."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 300},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    latency_ms = int((time.monotonic() - t0) * 1000)
    return body.get("response", "").strip(), latency_ms


def _do_claude_generate(model: str, api_key: str, prompt: str) -> tuple[str, int]:
    """Call Claude API. Returns (response_text, latency_ms)."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    t0 = time.monotonic()
    msg = client.messages.create(
        model=model,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    return msg.content[0].text.strip() if msg.content else "", latency_ms


def _static_stub_memo(bundle: dict, mode: str) -> str:
    """Fallback memo when no LLM is available — static summary from bundle data."""
    snapshot = bundle.get("latest_snapshot", {})
    dd = bundle.get("drawdown", {})
    cfg = bundle.get("config_summary", {})
    value = dd.get("current_value") or snapshot.get("portfolio_value", "unknown")
    drifts = snapshot.get("drifts_by_symbol", {})
    worst = max(drifts.items(), key=lambda x: abs(x[1]), default=("?", 0)) if drifts else ("?", 0)

    return (
        f"# TEST Decision Memo — {mode.upper()} (static stub, no LLM configured)\n\n"
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"## Executive Summary\n\n"
        f"- Portfolio value: ${value:,.2f}" if isinstance(value, (int, float))
        else f"- Portfolio value: {value}"
    ) + (
        f"\n- Max drift: {worst[0]} at {worst[1]:+.1%}" if isinstance(worst[1], float) else ""
    ) + (
        f"\n- Growth mode: {cfg.get('growth_mode', 'unknown')} | "
        f"Contribution: ${cfg.get('monthly_contribution', 0):,}/month\n\n"
        f"## Key Action\n\n"
        f"No LLM configured. Set OLLAMA_MODEL or ANTHROPIC_API_KEY to enable "
        f"AI-generated memos.\n\n"
        f"---\n*This is a TEST memo generated by the AI agent test harness.*\n"
    )


async def _tool_simulate_agent_run(args: dict) -> list[types.TextContent]:
    mode = args.get("mode", "daily")
    if mode not in ("daily", "weekly", "monthly"):
        return _json_result({"error": f"Invalid mode '{mode}'"})

    # Step 1: load or synthesise bundle
    bundle = await asyncio.to_thread(_load_or_synthesise_bundle, mode)

    # Step 2: build prompt
    prompt = _build_memo_prompt(bundle, mode)

    # Step 3: LLM routing — Ollama → Claude → static stub
    model_used = "none"
    memo_text = ""
    error_detail = ""

    ollama_model = _ollama_model()
    try:
        memo_text, _ = await asyncio.to_thread(_do_ollama_generate, ollama_model, prompt)
        model_used = f"ollama:{ollama_model}"
    except Exception as ollama_exc:
        # Try Claude fallback
        api_key = get_secret("ANTHROPIC_API_KEY") or ""
        if api_key:
            claude_model = _claude_model()
            try:
                import anthropic  # check importable before threading
                memo_text, _ = await asyncio.to_thread(
                    _do_claude_generate, claude_model, api_key, prompt
                )
                model_used = f"claude:{claude_model}"
            except ImportError:
                error_detail = "anthropic package not installed"
                memo_text = _static_stub_memo(bundle, mode)
                model_used = "static_stub"
            except Exception as claude_exc:
                error_detail = _redact(str(claude_exc))
                memo_text = _static_stub_memo(bundle, mode)
                model_used = "static_stub"
        else:
            error_detail = f"Ollama unavailable ({_redact(str(ollama_exc))}); ANTHROPIC_API_KEY not set"
            memo_text = _static_stub_memo(bundle, mode)
            model_used = "static_stub"

    # Prepend test header if not already present
    if "TEST" not in memo_text[:60]:
        memo_text = f"<!-- TEST memo — generated by agent_mcp_tools simulate_agent_run -->\n\n{memo_text}"

    # Step 4: write output
    out_dir = ROOT / "outputs" / "latest"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "test_decision_memo.md"

    try:
        out_path.write_text(memo_text, encoding="utf-8")
        memo_created = True
    except Exception as write_exc:
        memo_created = False
        error_detail = str(write_exc)

    result: dict = {
        "memo_created": memo_created,
        "model_used": model_used,
        "output_path": str(out_path.relative_to(ROOT)).replace("\\", "/"),
        "bundle_synthetic": bundle.get("synthetic", False),
        "mode": mode,
    }
    if error_detail:
        result["note"] = error_detail
    return _json_result(result)


# ── Tool: validate_guardrails ─────────────────────────────────────────────────
# Reads only from config.json, drawdown_state.json, price_cache.json.
# Does NOT import any investment-logic modules.

async def _tool_validate_guardrails() -> list[types.TextContent]:
    violations: list[dict] = []
    meta: dict = {}

    # -- Load config (caps) ---------------------------------------------------
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        return _json_result({"error": "config.json not found"})
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _json_result({"error": f"config.json unreadable: {exc}"})

    growth = cfg.get("growth_mode", {})
    concentration_cap = growth.get("concentration_cap", 0.40)
    leverage_cap = growth.get("leverage_cap", 0.15)
    sleeve_cfg = cfg.get("speculative_sleeve", {})
    sleeve_max_total = sleeve_cfg.get("max_total", 0.10)
    sleeve_max_per_pos = sleeve_cfg.get("max_per_position", 0.05)
    sleeve_enabled = sleeve_cfg.get("enabled", False)

    holdings_cfg = cfg.get("portfolio", {}).get("holdings", [])
    cash = cfg.get("portfolio", {}).get("cash_available", 0.0)

    # -- Load prices ----------------------------------------------------------
    pc_path = ROOT / "data" / "price_cache.json"
    prices: dict = {}
    if pc_path.exists():
        try:
            prices = json.loads(pc_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # -- Load drawdown --------------------------------------------------------
    dd_path = ROOT / "data" / "drawdown_state.json"
    drawdown_pct = 0.0
    current_val = 0.0
    if dd_path.exists():
        try:
            dd = json.loads(dd_path.read_text(encoding="utf-8"))
            current_val = float(dd.get("current_value", 0))
            ath = float(dd.get("all_time_high", current_val))
            drawdown_pct = (ath - current_val) / ath if ath > 0 else 0.0
        except Exception:
            pass

    # -- Reconstruct portfolio weights from prices + holdings -----------------
    total = cash
    position_values: dict[str, float] = {}
    for h in holdings_cfg:
        sym = h["symbol"]
        price_entry = prices.get(sym, {})
        price = float(price_entry.get("price", 0))
        val = h.get("shares", 0) * price
        position_values[sym] = val
        total += val

    meta["portfolio_value_from_prices"] = round(total, 2)
    meta["drawdown_state_value"] = round(current_val, 2)
    meta["drawdown_pct"] = round(drawdown_pct, 4)

    if total <= 0:
        return _json_result({
            "guardrails_pass": False,
            "violations": [{"rule": "data_error", "detail": "Cannot compute weights: total portfolio value is 0"}],
            "meta": meta,
        })

    # -- Check 1: Concentration cap -------------------------------------------
    for h in holdings_cfg:
        sym = h["symbol"]
        weight = position_values.get(sym, 0) / total
        if weight > concentration_cap:
            violations.append({
                "rule": "concentration_cap",
                "symbol": sym,
                "actual_weight": round(weight, 4),
                "cap": concentration_cap,
                "excess": round(weight - concentration_cap, 4),
                "action": f"Trim {sym}: reduce by {(weight - concentration_cap) * total:.2f}",
            })

    # -- Check 2: Effective leveraged exposure --------------------------------
    total_leveraged_exposure = 0.0
    for h in holdings_cfg:
        if h.get("is_leveraged", False):
            sym = h["symbol"]
            weight = position_values.get(sym, 0) / total
            factor = h.get("leverage_factor", 1)
            total_leveraged_exposure += weight * factor

    if total_leveraged_exposure > leverage_cap:
        violations.append({
            "rule": "leverage_cap",
            "actual_effective_exposure": round(total_leveraged_exposure, 4),
            "cap": leverage_cap,
            "excess": round(total_leveraged_exposure - leverage_cap, 4),
            "action": "Reduce leveraged position(s) or dilute via non-leveraged contributions",
        })

    # -- Check 3: Anti-panic sleeve gate (drawdown > 20%) --------------------
    if drawdown_pct > 0.20 and sleeve_enabled:
        violations.append({
            "rule": "anti_panic_sleeve_block",
            "drawdown_pct": round(drawdown_pct, 4),
            "threshold": 0.20,
            "detail": "No new sleeve positions permitted; drawdown exceeds 20% gate",
        })

    # -- Check 4: Sleeve per-position and total caps -------------------------
    # Read spec_sleeve_plan.csv if present to check live sleeve plan
    sleeve_plan_path = ROOT / "outputs" / "latest" / "spec_sleeve_plan.csv"
    if sleeve_plan_path.exists() and sleeve_enabled:
        import csv
        try:
            with open(sleeve_plan_path, encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            total_sleeve_add = sum(float(r.get("MaxAddDollars", 0)) for r in rows)
            sleeve_total_weight = total_sleeve_add / total if total > 0 else 0
            if sleeve_total_weight > sleeve_max_total:
                violations.append({
                    "rule": "sleeve_total_cap",
                    "planned_add": round(total_sleeve_add, 2),
                    "as_pct_of_portfolio": round(sleeve_total_weight, 4),
                    "cap": sleeve_max_total,
                    "detail": "Sleeve plan exceeds max_total allocation",
                })
            for row in rows:
                sym = row.get("Symbol", row.get("symbol", "?"))
                add = float(row.get("MaxAddDollars", 0))
                per_pos_weight = add / total if total > 0 else 0
                if per_pos_weight > sleeve_max_per_pos:
                    violations.append({
                        "rule": "sleeve_per_position_cap",
                        "symbol": sym,
                        "planned_add": round(add, 2),
                        "as_pct_of_portfolio": round(per_pos_weight, 4),
                        "cap": sleeve_max_per_pos,
                    })
        except Exception:
            pass

    return _json_result({
        "guardrails_pass": len(violations) == 0,
        "violations": violations,
        "meta": meta,
    })


# ── Tool: verify_agent_outputs ────────────────────────────────────────────────

async def _tool_verify_agent_outputs() -> list[types.TextContent]:
    expected = [
        "decision_memo.md",
        "monthly_memo.md",
        "email_draft.md",
        "escalation_packet.md",
        "outputs/latest/test_decision_memo.md",
    ]
    found: list[dict] = []
    missing: list[str] = []

    for rel in expected:
        p = ROOT / rel
        if p.exists():
            stat = p.stat()
            age_m = (time.time() - stat.st_mtime) / 60
            found.append({
                "file": rel,
                "size_bytes": stat.st_size,
                "age_minutes": round(age_m, 1),
            })
        else:
            missing.append(rel)

    return _json_result({
        "files_found": found,
        "missing_files": missing,
        "all_present": len(missing) == 0,
    })


# ── Tool: test_patch_approval_flow ────────────────────────────────────────────

async def _tool_test_patch_approval_flow() -> list[types.TextContent]:
    approval_path = ROOT / "approved_actions.json"
    approved = approval_path.exists()

    result: dict = {
        "approval_required": True,
        "approved": approved,
        "patch_generation_allowed": approved,
        "approved_actions_path": str(approval_path.relative_to(ROOT)).replace("\\", "/"),
    }

    if approved:
        try:
            data = json.loads(approval_path.read_text(encoding="utf-8"))
            actions = data.get("actions", [])
            result["action_count"] = len(actions)
            result["action_ids"] = [a.get("id", "?") for a in actions]
            result["note"] = "approved_actions.json found — maintainer patch generation is unblocked"
        except Exception as exc:
            result["approved"] = False
            result["patch_generation_allowed"] = False
            result["note"] = f"File exists but could not be parsed: {exc}"
    else:
        result["note"] = (
            "approved_actions.json not found — maintainer patch generation is BLOCKED. "
            "Create approved_actions.json with an 'actions' list to unlock."
        )

    return _json_result(result)


# ── Tool: scheduler_readiness_check ──────────────────────────────────────────

async def _tool_scheduler_readiness_check() -> list[types.TextContent]:
    issues: list[str] = []
    details: dict = {}

    # 1. run.lock — stale lock detection
    lock_path = ROOT / "data" / "run.lock"
    if lock_path.exists():
        details["run_lock_present"] = True
        try:
            lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
            acquired_at_str = lock_data.get("acquired_at", "")
            acquired_at = datetime.fromisoformat(acquired_at_str)
            age_s = (datetime.now() - acquired_at).total_seconds()
            details["run_lock_age_minutes"] = round(age_s / 60, 1)
            details["run_lock_pid"] = lock_data.get("pid")
            if age_s > 1800:  # 30-minute stale threshold (matches run_lock.py)
                issues.append(
                    f"Stale run.lock detected (age: {age_s/60:.0f}m, PID: {lock_data.get('pid')}). "
                    "May need manual cleanup."
                )
        except Exception as exc:
            issues.append(f"run.lock exists but could not be parsed: {exc}")
    else:
        details["run_lock_present"] = False

    # 2. SQLite state store readable
    db_path = ROOT / "data" / "portfolio.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("SELECT 1")
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            conn.close()
            details["state_store_tables"] = tables
            # Check for idempotency table
            if "run_history" not in tables:
                issues.append("SQLite run_history table missing — idempotency check will fail on first run")
        except Exception as exc:
            issues.append(f"SQLite state_store not readable: {exc}")
    else:
        details["state_store_present"] = False
        issues.append(
            "data/portfolio.db not yet created. "
            "State store will be initialised on first live (non-dry-run) execution."
        )

    # 3. Cache directories writable — write and immediately delete a sentinel file
    writable_dirs = ["data", "outputs/latest", "logs", "data/fmp_cache"]
    for d in writable_dirs:
        target_dir = ROOT / d
        target_dir.mkdir(parents=True, exist_ok=True)
        sentinel = target_dir / ".write_test_agent_mcp"
        try:
            sentinel.write_text("ok", encoding="utf-8")
            sentinel.unlink()
        except Exception as exc:
            issues.append(f"{d}/ not writable: {exc}")

    # 4. last_success.json freshness (25-hour threshold for daily runs)
    ls_path = ROOT / "data" / "last_success.json"
    if ls_path.exists():
        try:
            ls = json.loads(ls_path.read_text(encoding="utf-8"))
            ts_str = ls.get("timestamp") or ls.get("completed_at", "")
            ts = datetime.fromisoformat(ts_str)
            age_h = (datetime.now() - ts).total_seconds() / 3600
            details["last_success_age_hours"] = round(age_h, 1)
            details["last_success_mode"] = ls.get("run_mode", "unknown")
            if age_h > 25:
                issues.append(
                    f"last_success.json is {age_h:.1f}h old (threshold: 25h). "
                    "System may not have run today."
                )
        except Exception as exc:
            issues.append(f"last_success.json unreadable: {exc}")
    else:
        details["last_success_present"] = False
        issues.append(
            "data/last_success.json not found. "
            "System has never completed a successful live (non-dry-run) execution."
        )

    return _json_result({
        "ready_for_scheduler": len(issues) == 0,
        "issues": issues,
        "details": details,
    })


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
